#!/usr/bin/env python3
"""
LLM 调用封装模块
- call_llm: 通用异步 LLM 调用（带串行锁）
- _rp_llm_call: 角色扮演专用 LLM 调用
- _strip_thinking_tags: 移除 thinking/reasoning 标签
- _extract_final_answer: 从 reasoning_content 提取最终答案
- _extract_rpg_narration: 从 reasoning_content 提取角色扮演旁白
- _extract_awards_from_text: 从文本中提取评选结果
"""

import asyncio
import concurrent.futures
import json
import logging
import time
import warnings
import re

import httpx
from typing import Optional

logger = logging.getLogger("qq-bot")

# ============================================================
#  LLM 串行锁 + 优先级队列
# ============================================================

# _chat_lock: @bot 聊天消息专用锁，独立 FIFO 队列（聊天之间串行排队）
_chat_lock: asyncio.Lock = asyncio.Lock()  # Lock 保证 FIFO，Semaphore 不保证

# _task_queue: 任务 LLM 调用的优先级队列（替换原来的 _llm_lock）
# - 高优先级(priority=0): 用户手动指令（/查询 /总结 /评选 /画像 /人设）
# - 低优先级(priority=1): 定时任务（每日更新/定时评选/定时总结）
# - 同一优先级内 FIFO，高优先级可插队到低优先级前面
_task_queue = None  # lazy init

# 主事件循环引用（供后台线程通过 run_coroutine_threadsafe 使用）
_main_loop: asyncio.AbstractEventLoop | None = None

# ============================================================
#  LLM 并行模式开关
# ============================================================
# 并行模式控制：开启后任务队列允许并发执行多个 LLM 调用
# 默认关闭（串行模式），通过 /开启并行 和 /关闭并行 指令控制
# 注意：聊天锁 _chat_lock 始终串行，并行只影响任务队列
_parallel_mode = True  # 默认开启并发模式（2026-08-05 用户配置：默认并行 + DeepSeek 后端）
_MAX_PARALLEL = 3  # 本地模式最大并发数（Ollama 并发能力有限，保守限流；DeepSeek 模式见 DEEPSEEK_MAX_PARALLEL）

# 并行模式下 TD（priority<0）使用独立小信号量：出题不被普通任务批次占满
_TD_MAX_PARALLEL = 2

# ============================================================
# max_tokens 统一变量（2026-08-25 用户配置：长/短输入分档）
# MAX_TOKENS_LONG  = 长输入 QQ 聊天记录调用点（直接接收 BATCH_CHARS 分段的原始聊天文本
#                   或批次摘要拼接的 Reduce 汇总）：persona 3 Map + router 4 + scheduler 4。
#                   ⚠️ 2026-08-05 用户决定与免费版统一：MAX_TOKENS_LONG=131072（付费版支持 393216，
#                   但 131072 已够用，全库长输入批次不会触顶）。
# MAX_TOKENS_SHORT = 短输入调用点（Reduce 中间合并、单条 JSON、聊天等）：persona 11 + router 6 + scheduler 1。
# 修改分档只需改这里，调用点全部引用变量。
MAX_TOKENS_LONG = 131072
MAX_TOKENS_SHORT = 16384

# 429/503 限流重试（DeepSeek API 高并发下会触发限流）
_LLM_MAX_RETRIES = 3   # 最多尝试次数（含首次）
_LLM_RETRY_BASE = 2.0  # 指数退避基数：2s, 4s


def set_parallel_mode(enabled: bool) -> None:
    """设置并行模式开关"""
    global _parallel_mode
    _parallel_mode = enabled


def is_parallel_mode() -> bool:
    """检查是否处于并行模式"""
    return _parallel_mode


def _set_main_loop() -> None:
    """初始化时调用，保存主事件循环引用"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()


class _PriorityLLMQueue:
    """
    基于 asyncio.PriorityQueue 的 LLM 调用优先级队列。

    行为：
    - 同一时刻最多 1 个 LLM 任务在执行
    - 高优先级(0)可插队到低优先级(1)前面
    - 同优先级内严格 FIFO
    - 接口：async with await queue.acquire(priority): ...

    异常安全：
    - _dispatch 的 set_result 抛异常时自动恢复（不卡死队列）
    - Task 引用保存在 _dispatch_task，防止 GC
    - task_done() 在所有路径（正常/异常）均被调用
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, int, asyncio.Future[None]]] = (
            asyncio.PriorityQueue()
        )
        self._running = False
        self._counter = 0  # FIFO 计数器，同优先级内保证顺序
        self._dispatch_task: asyncio.Task[None] | None = None

    async def _dispatch(self) -> None:
        """从队列取下一个任务并放行"""
        if self._running or self._queue.empty():
            return
        self._running = True
        priority: int | None = None
        seq: int | None = None
        try:
            priority, seq, future = await self._queue.get()
        except Exception as e:
            logger.error(f"_dispatch: queue.get() 异常: {e}", exc_info=True)
            self._running = False
            self._dispatch_task = asyncio.create_task(self._dispatch())
            return

        # 检查 future 状态后再放行
        if future.cancelled():
            # future 被取消 → 调用方已放弃等待，不会再调 release()
            # 必须在 _dispatch 侧完成 task_done + 重置 _running
            logger.debug(f"_dispatch: future 已取消 (priority={priority}, seq={seq})")
            self._running = False
            self._queue.task_done()
            self._dispatch_task = asyncio.create_task(self._dispatch())
        elif future.done():
            # 已 done 但非 cancelled（极罕见）
            self._running = False
            self._queue.task_done()
            self._dispatch_task = asyncio.create_task(self._dispatch())
        else:
            try:
                future.set_result(None)
                # 正常路径: _running 保持 True + task_done() 由 release() 负责
            except asyncio.InvalidStateError:
                # set_result 时 future 恰好被取消 → 竞态
                logger.debug(f"_dispatch: future 竞态取消 (priority={priority}, seq={seq})")
                self._running = False
                self._queue.task_done()
                self._dispatch_task = asyncio.create_task(self._dispatch())

    async def acquire(self, priority: int = 1) -> asyncio.Future[None]:
        """入队并等待调度"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        seq = self._counter
        self._counter += 1
        await self._queue.put((priority, seq, future))
        # 入队后立即尝试调度（第一个任务/空闲时）
        self._dispatch_task = asyncio.create_task(self._dispatch())
        await future
        return future

    def release(self) -> None:
        """释放并触发下一个"""
        self._running = False
        self._queue.task_done()
        self._dispatch_task = asyncio.create_task(self._dispatch())


def _get_task_queue() -> _PriorityLLMQueue:
    global _task_queue
    if _task_queue is None:
        _task_queue = _PriorityLLMQueue()
    return _task_queue


# 向后兼容：导出 _llm_lock 名称（避免 import 报错）
_llm_lock = object()


# ============================================================
#  Thinking 标签清理
# ============================================================

def _strip_thinking_tags(text: str) -> str:
    """
    移除文本中的 thinking/reasoning 标签内容。
    支持的格式：
    - <|thinking|>...</|/thinking|> (DeepSeek/Qwen 通用)
    - <|thinking|>...</|thinking|> (变体)
    - <thought>...</thought>
    - <think>...</think> (含 HTML 转义)
    - <think>...</think> (DeepSeek)
    """
    # thinking 标签格式 — 同时匹配 <|/thinking|> 和 </|/thinking|> 两种变体（DeepSeek/Qwen）
    text = re.sub(r"<\|thinking\|>.*?</\|thinking\|>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|thinking\|>.*?<\|/thinking\|>", "", text, flags=re.DOTALL)
    # thought 标签
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
    # think 标签（含 HTML 转义形式 \x3c 和 \x3e）
    text = re.sub(r"(?:\x3c|<)think(?:\x3e|>).*?(?:\x3c|<)/think(?:\x3e|>)", "", text, flags=re.DOTALL)
    # DeepSeek 格式
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def _extract_json_block(text: str) -> str | None:
    """
    从文本中提取第一个完整可解析的 JSON 对象（单遍扫描，O(n)）。

    处理场景：DeepSeek V4 思考模式下 content 为空、只有 reasoning_content，
    reasoning 里模型通常草拟了完整 JSON。提取后供 JSON 任务（Combined Map/
    画像/人设/合并提取）直接使用；评选等文本任务提取不到 JSON 时仍走
    _extract_final_answer 原逻辑。
    """
    if not text:
        return None
    stack: list[int] = []
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}":
            if not stack:
                continue
            start = stack.pop()
            if not stack:  # 最外层闭合，尝试解析
                candidate = text[start:i + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass  # 该块不完整，继续找下一个
    return None


# ============================================================
#  从 reasoning_content 中提取角色扮演旁白正文
# ============================================================

def _extract_rpg_narration(reasoning: str) -> str:
    """
    从 reasoning_content 中提取真正的旁白正文，去掉思考过程。

    策略：
    1. 查找 "Final Output" 或 "Output:" 关键词，提取之后的中文段落
    2. 如果没找到，移除思考标签后逐行过滤，保留纯中文段落
    3. 清理字数标注和分析内容
    """
    # 策略 1：查找 "Final Output" 或 "Output:" 关键词
    output_match = re.search(r"(?:Final\s+)?Output\s*[:：*\s]*\n?\s*([\u4e00-\u9fff])", reasoning, re.IGNORECASE | re.DOTALL)
    if output_match:
        return reasoning[output_match.start(1):].strip()

    # 策略 2：查找 "Drafting"、"Draft \d*:" 或 "Draft \d*:" 关键词，只提取紧随其后的中文段落
    draft_match = re.search(r"(?:Drafting|Draft\s*\d*)\s*[:：*\s]*\n?\s*([\u4e00-\u9fff])", reasoning, re.IGNORECASE | re.DOTALL)
    if draft_match:
        # 从第一个中文字符开始，提取到下一个英文行或分析行为止
        start = draft_match.start(1)
        rest = reasoning[start:]
        # 按行分割，保留连续的中文段落
        lines = rest.split("\n")
        result_lines = []
        for line in lines:
            stripped = line.strip()
            # 跳过英文行（不含中文字符）
            if not re.search(r'[\u4e00-\u9fff]', stripped):
                break
            # 跳过数字括号标注
            if re.match(r'\s*\(\d+)\s*$', stripped):
                break
            # 跳过分析行
            if re.match(r'\d+\.\s*\*\*?\w+\s+Check', stripped, re.IGNORECASE):
                break
            # 清理行内数字括号标注
            stripped = re.sub(r'\s*\([^)]*approx[^)]*\)\s*', ' ', stripped)
            stripped = re.sub(r'\s*\(\d+)\s*', ' ', stripped)
            stripped = stripped.strip()
            if stripped:
                result_lines.append(stripped)
            else:
                break
        if result_lines:
            return "\n".join(result_lines).strip()

    # 策略 3：移除思考标签后，逐行过滤，保留纯中文段落
    cleaned = _strip_thinking_tags(reasoning)
    lines = [line for line in cleaned.split("\n") if line.strip()]

    # 过滤掉英文分析行，保留中文段落
    result_lines = []
    for line in lines:
        stripped = line.strip()
        # 跳过英文行（不含中文字符）
        if not re.search(r'[\u4e00-\u9fff]', stripped):
            continue
        # 跳过纯数字括号标注行
        if re.match(r'\s*\(\d+)\s*$', stripped):
            continue
        # 跳过分析行
        if re.match(r'\d+\.\s*\*\*?\w+\s+Check', stripped, re.IGNORECASE):
            continue
        if re.match(r"(?:Let|Thinking|Analyzing|Checking)", stripped, re.IGNORECASE):
            continue
        if re.match(r"(?:\d+\.\s*\*\*|\*Draft|\*Check|\*Count|\*Tone|\*Output)", stripped, re.IGNORECASE):
            continue
        # 跳过带 markdown 列表符号的行
        if re.match(r'(?:- \*\*|[*•] \*\*)', stripped):
            continue
        # 清理行内数字括号标注 (30)、(approx 30)
        stripped = re.sub(r'\s*\([^)]*approx[^)]*\)\s*', ' ', stripped)
        stripped = re.sub(r'\s*\(\d+)\s*', ' ', stripped)
        stripped = stripped.strip()
        if stripped:
            result_lines.append(stripped)

    return "\n".join(result_lines[-5:]).strip()


# ============================================================
#  从 reasoning_content 中提取最终答案
# ============================================================

def _extract_final_answer(reasoning: str) -> str:
    """
    从 reasoning_content 中提取最终答案。

    模型输出结构分析：
    - 模型可能在 thinking 标签内/外多次输出评选草稿
    - 真正的最终答案通常在 "===== 评选结果 =====" 分隔线之后
    - 如果没找到分隔线，回退到最后一个 <|/thinking|> 标签之后
    - 最终答案使用标准格式：🏆 最抽象：\n👤 昵称\n💬 消息\n💡 评语

    策略：
    1. 优先查找 "===== 评选结果 =====" 分隔线之后的内容
    2. 如果没找到分隔线，查找最后一个 <|/thinking|> 标签之后的内容
    3. 如果没找到标签，回退到查找带 emoji 的分类行
    4. 验证是否包含实际内容（不是占位符）
    """
    # 策略 0：优先查找 "===== 评选结果 =====" 分隔线
    delimiter_match = re.search(r"={3,}\s*评选结果\s*={3,}", reasoning)
    if delimiter_match:
        after_delimiter = reasoning[delimiter_match.end():]
        # 提取评选结果
        return _extract_awards_from_text(after_delimiter)

    # 策略 1：查找最后一个 thinking 标签之后的内容
    last_thinking_end = reasoning.rfind("<|/thinking|>")
    if last_thinking_end != -1:
        after_thinking = reasoning[last_thinking_end + len("<|/thinking|>"):]
        category_pattern = re.compile(
            r"(🏆\s*最抽象|🌸\s*最涩涩|🔥\s*最激情|🤣\s*最搞笑|🧠\s*最哲学)[:：]", re.MULTILINE
        )
        if category_pattern.search(after_thinking):
            return _extract_awards_from_text(after_thinking)

    # 策略 2：移除 thinking 标签内容后查找
    cleaned = _strip_thinking_tags(reasoning)

    # 查找带 emoji 的分类行（评选场景）
    category_pattern = re.compile(
        r"(🏆\s*最抽象|🌸\s*最涩涩|🔥\s*最激情|🤣\s*最搞笑|🧠\s*最哲学)[:：(]", re.MULTILINE
    )

    matches = list(category_pattern.finditer(cleaned))
    if not matches:
        lines = [line for line in cleaned.split("\n") if line.strip()]
        return "\n".join(lines[-10:]).strip()

    # 找到第一个"真正"的评选结果（而不是最后一个）
    # 因为输出可能被截断，从第一个真实结果开始提取能得到更多完整奖项
    first_real_match = None
    for m in matches:
        snippet = cleaned[m.start():m.start() + 300]
        # 检查标准格式
        if "👤" in snippet and "💬" in snippet:
            nickname_match = re.search(r"👤\s*(.+)", snippet)
            if nickname_match:
                nickname = nickname_match.group(1).strip()
                if nickname and nickname != "获奖者昵称":
                    first_real_match = m
                    break  # 找到第一个就停止
        # 检查紧凑格式：`昵称` - `消息` -> 评语
        if "`" in snippet and "->" in snippet:
            compact_pattern = re.compile(r"(🏆|🌸|🔥|🤣|🧠)\s*最\w+:.+?`([^`]+)`\s*-\s*`([^`]+`)")
            if compact_pattern.search(snippet):
                first_real_match = m
                break  # 找到第一个就停止

    # 如果没有找到真正的评选结果，回退到最后一个匹配
    if first_real_match is None:
        first_real_match = matches[0]

    # 从匹配处开始提取评选结果
    return _extract_awards_from_text(cleaned[first_real_match.start():])


def _extract_awards_from_text(text: str) -> str:
    """
    从文本中提取评选结果（用于处理分隔线或 thinking 标签之后的内容）。

    兼容两种格式：
    1. 标准格式：🏆 最抽象：\n👤 昵称\n💬 消息\n💡 评语
    2. Markdown 格式：*🏆 最抽象 (Abstract/Nonsensical)*\n- `[time] 昵称: 消息` -> 评语
    """
    # 清理 markdown 格式符号（* _ # `）
    text = re.sub(r"^\*\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\(.*?\)\s*\n", "\n", text)

    category_pattern = re.compile(
        r"(🏆\s*最抽象|🌸\s*最涩涩|🔥\s*最激情|🤣\s*最搞笑|🧠\s*最哲学)[:：(]", re.MULTILINE
    )

    matches = list(category_pattern.finditer(text))
    if not matches:
        return text.strip()

    # 找到第一个包含实际内容的匹配（不是占位符 "获奖者昵称"）
    # 使用第一个而不是最后一个，因为输出可能被截断，我们需要从第一个真实结果开始提取
    first_real_match = None
    for m in matches:
        snippet = text[m.start():m.start() + 300]
        # 检查标准格式：包含 👤 和 💬，且 👤 后面有实际昵称
        if "👤" in snippet and "💬" in snippet:
            nickname_match = re.search(r"👤\s*(.+)", snippet)
            if nickname_match:
                nickname = nickname_match.group(1).strip()
                if nickname and nickname != "获奖者昵称":
                    first_real_match = m
                    break  # 找到第一个就停止
        # 检查紧凑格式：`昵称` - `消息` -> 评语
        if "`" in snippet and (" -> " in snippet or " ->\n" in snippet):
            arrow_match = re.search(r"`([^`]+)`\s*-\s*`([^`]+)`", snippet)
            if arrow_match:
                first_real_match = m
                break  # 找到第一个就停止

    # 如果没有找到真正的评选结果，回退到最后一个匹配
    if first_real_match is None:
        first_real_match = matches[-1]

    # 从第一个真正的评选结果开始提取
    result = text[first_real_match.start():]

    # 逐行处理
    lines = result.split("\n")
    clean_lines = []
    found_summary = False
    in_thinking = False
    found_first_award = False

    for line in lines:
        stripped = line.strip()

        if in_thinking:
            break

        if not stripped:
            if clean_lines and (found_summary or (found_first_award and clean_lines[-1].strip().startswith("💡"))):
                clean_lines.append("")
            continue

        # 检查是否是评选结果相关的行
        if category_pattern.match(stripped):
            found_first_award = True
            clean_lines.append(line)
        elif stripped.startswith(("👤", "💬", "💡")):
            found_first_award = True
            clean_lines.append(line)
            if stripped.startswith("总结") or stripped.startswith("总结："):
                found_summary = True
        elif stripped.startswith("总结"):
            found_summary = True
            clean_lines.append(line)
        elif found_summary and not stripped.startswith(("🏆", "🌸", "🔥", "🤣", "🧠", "👤", "💬", "💡")):
            # 总结后面的内容可能是思考过程
            in_thinking = True
        elif found_first_award and (stripped.startswith("- ") or " -> " in stripped or "`" in stripped):
            # 兼容紧凑格式：- `[time] 昵称: 消息` -> 评语
            # 提取关键信息
            compact_match = re.search(r"`([^`]+)`\s*:\s*`([^`]+)`", stripped)
            if compact_match:
                nickname = compact_match.group(1)
                message = compact_match.group(2)
                clean_lines.append(f"👤 {nickname}")
                clean_lines.append(f"💬 {message}")
            else:
                # 尝试其他紧凑格式
                arrow_match = re.search(r"`([^`]+)`\s*-\s*`([^`]+)`", stripped)
                if arrow_match:
                    nickname = arrow_match.group(1)
                    message = arrow_match.group(2)
                    arrow_comment = stripped[arrow_match.end():].strip().lstrip("->").strip()
                    clean_lines.append(f"👤 {nickname}")
                    clean_lines.append(f"💬 {message}")
                    if arrow_comment:
                        clean_lines.append(f"💡 {arrow_comment}")
        elif found_first_award:
            # 保留其他行（可能是评语或分析）
            clean_lines.append(line)

    # 去掉尾部空白行
    while clean_lines and not clean_lines[-1].strip():
        clean_lines.pop()

    final = "\n".join(clean_lines).strip()
    final = final.rstrip('"\'').rstrip()
    return final if final else text.strip()


# ============================================================
#  LLM 调用
# ============================================================

def _get_config():
    """延迟导入 CONFIG，避免循环依赖"""
    # 优先从 bot 模块获取 CONFIG — 通过 sys.modules 安全访问
    # （bot.py 启动时 sys.modules['bot']=自身并持有 CONFIG）
    import sys
    bot_mod = sys.modules.get('bot')
    if bot_mod and hasattr(bot_mod, 'CONFIG'):
        return bot_mod.CONFIG
    # fallback：独立运行（测试脚本/工具脚本未加载 bot.py）时直接取 core.config
    from .config import CONFIG
    return CONFIG


# 并行模式的并发控制信号量
_parallel_semaphore: asyncio.Semaphore | None = None
_parallel_limit: int | None = None  # 当前信号量的并发上限（后端切换时重建）
_parallel_td_semaphore: asyncio.Semaphore | None = None


def _get_parallel_semaphore() -> asyncio.Semaphore:
    global _parallel_semaphore, _parallel_limit
    limit = _get_parallel_limit()
    # 后端切换（本地↔DeepSeek）时并发上限变化，重建信号量。
    # 等待旧信号量的协程持有旧对象引用，安全退出；新调用走新对象。
    if _parallel_semaphore is None or _parallel_limit != limit:
        _parallel_semaphore = asyncio.Semaphore(limit)
        _parallel_limit = limit
    return _parallel_semaphore


def _get_td_semaphore() -> asyncio.Semaphore:
    """并行模式下 TD（priority<0）专用小信号量，保证出题不被普通任务占满"""
    global _parallel_td_semaphore
    if _parallel_td_semaphore is None:
        _parallel_td_semaphore = asyncio.Semaphore(_TD_MAX_PARALLEL)
    return _parallel_td_semaphore


def _resolve_llm_backend(config: dict) -> tuple[str, str, dict, int]:
    """解析当前 LLM 后端配置（v2：按 yaml llm.backend 显式选择，可热切换）。

    backend=remote → 远程 API（任意 OpenAI 兼容：DeepSeek/OpenAI/网关…，
                      REMOTE_API_KEY 为空时回退本地并告警）；
    backend=local  → 本地 LLM（即使有 key 也不切远程）。

    Returns:
        (api_url, model, headers, max_tokens_cap)
        max_tokens_cap = 0 表示不钳制（本地 LLM 无输出上限）
    """
    backend = str(config.get("LLM_BACKEND", "remote")).lower()
    if backend == "remote" and config.get("REMOTE_API_KEY"):
        return (
            config["REMOTE_API"],
            config["REMOTE_MODEL"],
            {"Authorization": f"Bearer {config['REMOTE_API_KEY']}"},
            int(config.get("REMOTE_MAX_TOKENS", 393216)),
        )
    if backend == "remote":
        # 选了 remote 但没 key → 回退本地（启动时已告警）
        pass
    return config["LLM_API"], config["LLM_MODEL"], {}, 0


def llm_enabled() -> bool:
    """LLM 总开关（llm.enabled，GUI 总览页可热切）。

    关闭时所有 LLM 调用点应直接走降级路径（不调模型、不耗额度）。
    """
    try:
        return bool(_get_config().get("LLM_ENABLED", True))
    except Exception:
        return True


def _get_parallel_limit() -> int:
    """当前后端的并发上限：远程 API 按 API 并发限制，本地保守 10"""
    try:
        config = _get_config()
        backend = str(config.get("LLM_BACKEND", "remote")).lower()
        if backend == "remote" and config.get("REMOTE_API_KEY"):
            return int(config.get("REMOTE_MAX_PARALLEL", 500))
    except Exception:
        pass
    return _MAX_PARALLEL


async def _post_llm_chat(
    client: httpx.AsyncClient,
    api_url: str,
    model: str,
    headers: dict,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    disable_thinking: bool = False,
    json_mode: bool = False,
    reasoning_effort: Optional[str] = None,  # DeepSeek 思考强度（low/medium/high/max），None=默认 max
) -> dict:
    """发送 LLM chat/completions 请求，带 429/503 指数退避重试。

    Returns:
        {"content": str, "reasoning": str, "usage": dict}
        content/reasoning 均为去除空白后的原始字符串；usage 为响应里的
        usage 字段（可能为 {}，本地后端不一定返回），调用方可选记录统计。

    Raises:
        httpx.HTTPStatusError: 重试耗尽或非限流错误（由调用方兜底）
        httpx.TimeoutException / 其他 httpx 异常: 透传
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # 强制 JSON 输出：模型被约束只能输出合法 JSON（OpenAI 兼容 response_format）
        # 出题等结构化任务启用——评审/规划/序号文本从机制上无法混入 content
        payload["response_format"] = {"type": "json_object"}
    # DeepSeek 系后端识别：URL 或模型名含 deepseek 均视为 DeepSeek 系
    # （部分 OpenAI 兼容网关 URL 不含 deepseek，需靠模型名兜底）。
    is_deepseek_backend = "deepseek" in api_url.lower() or "deepseek" in model.lower()
    # 2026-08-24 后端方言兼容：本地 LLM（LM Studio + Qwen3 系模型）的 Jinja
    # 模板只接受 none/low/medium/high——收到 "max" 直接 500
    # （"Unexpected reasoning effort max"，赛博模仿硬编码 max 整链路报
    # "模型出了点小问题"的根因）。DeepSeek 系保留 max；非 DeepSeek 后端
    # 自动降 max→high（Qwen3 模板支持的最高档，语义等价"最深思考"）。
    if (reasoning_effort and not is_deepseek_backend
            and str(reasoning_effort).lower() == "max"):
        reasoning_effort = "high"
    if disable_thinking and is_deepseek_backend:
        # 批量 JSON 提取任务显式关闭思考：更快（~4s vs 71s）、更省（~70 vs 8000 tokens）、
        # content 稳定输出。仅 DeepSeek 后端传 thinking 参数，本地 Ollama 不传避免 400。
        payload["thinking"] = {"type": "disabled"}
    elif reasoning_effort:
        # 自定义思考强度（low/medium/high/max）：压缩等任务用 medium 平衡质量与 token 占用
        payload["reasoning_effort"] = reasoning_effort
    elif is_deepseek_backend:
        # 思考程度 max：DeepSeek V4 系列 reasoning_effort 控制思考深度（low/medium/high/max）
        payload["reasoning_effort"] = "max"
    for attempt in range(_LLM_MAX_RETRIES):
        try:
            resp = await client.post(
                f"{api_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            return {"content": content, "reasoning": reasoning,
                    "usage": data.get("usage") or {},
                    # 响应里的真实模型名（本地 LLM 常返回实际加载的模型 ID，
                    # 配置留空时这是用量统计唯一能拿到的模型名）
                    "model": data.get("model") or model,
                    # 08-22：finish_reason（"stop"=正常结束，"length"=被
                    # max_tokens 截断——GUI 弹窗提示"生成不完整"区分显示截断）
                    "finish_reason": data["choices"][0].get("finish_reason") or ""}
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 503) and attempt < _LLM_MAX_RETRIES - 1:
                backoff = _LLM_RETRY_BASE * (2 ** attempt)
                logger.warning(
                    f"LLM HTTP {e.response.status_code} 限流，{backoff}s 后重试 "
                    f"({attempt + 1}/{_LLM_MAX_RETRIES})"
                )
                await asyncio.sleep(backoff)
                continue
            raise


# ============================================================
#  LLM 健康状态（GUI 状态灯用；最近一次真实调用/连接测试的结果，
#  不烧 token 的后台心跳——灯只反映"最近一次"，无活动则保持旧态）
# ============================================================
LLM_HEALTH: dict = {
    "status": "idle",      # idle(未测试) | ok | fail
    "ts": None,            # 最近一次记录时间（epoch）
    "error": "",           # 失败原因（fail 时有值）
    "source": "",          # 来源标记（如 回复/人设/出题/连接测试）
}


def update_health(status: str, source: str = "", error: str = "") -> None:
    """记录一次 LLM 调用/测试结果到健康状态（GUI 轮询 /status 读取）。"""
    LLM_HEALTH["status"] = status
    LLM_HEALTH["ts"] = time.time()
    LLM_HEALTH["error"] = error[:200]
    LLM_HEALTH["source"] = source


async def call_llm(
    messages: list[dict],
    max_tokens: int = 65536,
    use_lock: bool = True,
    lock_type: str = "task",
    priority: int = 0,  # 0=高优先级(用户指令), 1=低优先级(定时任务)
    temperature: float = 0.7,
    parallel: bool = False,  # 显式请求并行调用（不受 _parallel_mode 影响）
    timeout: int = 1800,  # HTTP 超时秒数（默认 30 分钟，调用方可传短超时避免长阻塞）
    disable_thinking: bool = False,  # DeepSeek 后端关闭思考模式（batch-reduce JSON 任务用）
    json_mode: bool = False,  # 强制 JSON 输出（response_format=json_object，出题等结构化任务用）
    reasoning_effort: Optional[str] = None,  # DeepSeek 思考强度（low/medium/high/max），None=默认 max
    source: str = "",  # 调用来源标记（GUI 总览页"最近请求"行展示用，如 回复/人设/出题）
    task_key: Optional[str] = None,  # 2026-08-22 任务列表：提供时排队/执行状态透传给 TASK_REGISTRY
) -> str:
    """通用异步 LLM 调用

    Args:
        messages: 对话消息列表
        max_tokens: 最大 token 数
        use_lock: 是否使用串行锁（默认 True）
        lock_type: 锁类型，"chat"=聊天专用锁（独立 FIFO），"task"=任务优先级队列
        priority: 任务优先级（仅 lock_type="task" 时有效）
                  0=高优先级（用户手动指令），1=低优先级（定时任务）
                  高优先级可插队到低优先级前面，同优先级内 FIFO
        temperature: 温度参数（默认 0.7）
        parallel: 显式请求并行调用。为 True 时绕过串行锁，直接并发执行
                  常用于 batch-reduce 流程中各批次的独立提取阶段
        timeout: HTTP 请求超时秒数。注意：bot.py 消息循环严格串行，长超时
                 会让整个 bot 静默阻塞——交互式路径请传 60-120s 短超时
        disable_thinking: 为 True 且后端为 DeepSeek 时传 thinking={"type": "disabled"}
                 关闭思考模式。DeepSeek V4 思考模式下 max_tokens 是思考+正文共享
                 额度池，90K 输入下思考链会吃掉 99% 预算导致 content 为空/残缺
                 （哈基白事故根因）；批量 JSON 提取任务关闭思考后 4s/~70 tokens
                 即可稳定输出。本地后端忽略此参数。
    """
    config = _get_config()

    # LLM 总开关（llm.enabled）：关闭时直接降级，不发请求、不耗额度
    if not llm_enabled():
        logger.info("LLM 总开关关闭，跳过调用（降级处理）")
        # 2026-08-22 任务列表：提前返回也要 finish，防任务滞留
        if task_key is not None:
            from .task_registry import TASK_REGISTRY as _TR
            _TR.finish(task_key)
        return "🔕 LLM 已关闭（总览页 LLM 板块可开启）"

    async def _do_call():
        api_url, model, headers, max_tokens_cap = _resolve_llm_backend(config)
        # DeepSeek 输出上限 384K（393216）：超过会被 API 拒绝（400）。本地 LLM 不受限（cap=0）
        effective_max_tokens = min(max_tokens, max_tokens_cap) if max_tokens_cap > 0 else max_tokens
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            try:
                res = await _post_llm_chat(
                    client, api_url, model, headers, messages, temperature, effective_max_tokens,
                    disable_thinking=disable_thinking,
                    json_mode=json_mode,
                    reasoning_effort=reasoning_effort,
                )
                content, reasoning = res["content"], res["reasoning"]

                # 用量统计 + 最近请求记录（旁路，失败不影响主链路）
                try:
                    from . import llm_usage
                    if res.get("usage"):
                        await llm_usage.record_usage(res["usage"], res.get("model") or model)
                    # 最近请求摘要（GUI 总览页"最近 LLM 请求"行实时展示）
                    llm_usage.record_request(
                        res.get("model") or model,
                        res.get("content") or res.get("reasoning", ""),
                        source=source or "LLM 调用",
                        finish_reason=res.get("finish_reason", ""),
                    )
                except Exception:
                    pass

                # 健康状态：API 有响应即视为连接正常（GUI 状态灯，08-21）
                update_health("ok", source=source or "LLM 调用")

                # DeepSeek 思维链模型（reasoning_content 字段）：content 是最终回复，
                # reasoning_content 是思考过程。清理 content 中可能混入的 thinking 标签
                content = _strip_thinking_tags(content)

                if content:
                    return content

                # content 为空时，从 reasoning_content 提取最终答案
                if reasoning:
                    # JSON 任务优先：DeepSeek 思考模式下 content 常为空，
                    # reasoning 里通常有完整 JSON 草稿，直接提取（避免被
                    # _extract_final_answer 评选专用逻辑加工成垃圾文本）
                    json_block = _extract_json_block(reasoning)
                    if json_block:
                        return json_block
                    # 评选等文本任务：仍走评选专用提取器
                    return _extract_final_answer(reasoning)

                logger.warning("LLM 返回空内容")
                return "🤔 模型在想什么呢，请稍后再试～"
            except httpx.TimeoutException:
                logger.error("LLM 调用超时")
                update_health("fail", source=source or "LLM 调用",
                              error=f"超时（>{timeout}s）")
                return "⏳ 思考时间太长啦，请稍后再试～"
            except httpx.HTTPStatusError as e:
                error_body = e.response.text[:300]
                logger.error(f"LLM HTTP 错误: {e.response.status_code} - {error_body}")
                update_health("fail", source=source or "LLM 调用",
                              error=f"HTTP {e.response.status_code}: {error_body[:120]}")
                logger.debug(f"发送的消息: {messages}")
                return "😵 模型那边出了点小问题，请稍后再试～"
            except Exception as e:
                logger.error(f"LLM 调用异常: {e}")
                update_health("fail", source=source or "LLM 调用",
                              error=f"{type(e).__name__}: {e}")
                return "😵 模型那边出了点小问题，请稍后再试～"

    if not use_lock:
        return await _do_call()

    # 显式并行模式：绕过串行锁，用信号量控制并发数
    if parallel or (lock_type == "task" and is_parallel_mode()):
        # TD（priority<0）用独立小信号量，出题不被普通任务批次占满
        if priority < 0:
            sem = _get_td_semaphore()
        else:
            sem = _get_parallel_semaphore()
        async with sem:
            return await _do_call()

    if lock_type == "chat":
        # 聊天走独立 FIFO 信号量，与任务队列互不干扰
        # 2026-08-22 任务列表：等锁=排队，持锁=执行，结束=移除
        from .task_registry import TASK_REGISTRY as _TR
        if task_key is not None:
            _TR.set_status(task_key, "queued")
        try:
            async with _chat_lock:
                # 2026-08-22 暂停门：范围=全部（用户拍板）——持 chat 锁后、
                # 调 LLM 前等放行。暂停期间 @bot 消息在队列里排队等待继续。
                await _TR.wait_if_paused()
                if task_key is not None:
                    _TR.set_status(task_key, "running")
                return await _do_call()
        finally:
            if task_key is not None:
                _TR.finish(task_key)
    else:
        # 任务走优先级队列：高优先级可插队
        queue = _get_task_queue()
        await queue.acquire(priority)
        try:
            return await _do_call()
        finally:
            queue.release()


async def _rp_llm_call(system_prompt: str, messages: list[dict],
                       use_json_mode: bool = False) -> str:
    """
    角色扮演专用 LLM 调用适配器（async）。
    group_roleplay 期望的签名：llm_call_func(system_prompt, messages) -> str
    注意：此函数是 async，调用方需要 await。

    LLM 参数（2026-08-22 起实时读 config.yaml roleplay.llm 段，热生效）：
    max_tokens/temperature/thinking/json_mode/timeout，默认值 = 原硬编码行为
    （0.7 温度、min(32768, 后端上限)、不传 thinking、timeout=1800）。
    use_json_mode=True 仅世界观生成传入（_parse_world_json 依赖 JSON 解析，
    json_mode 从机制上提高解析成功率）；旁白/摘要是纯文本，即使配置开启
    json_mode 也不传（JSON 化会毁掉叙事输出）。

    DeepSeek 思维链模型（reasoning_content 字段）：reasoning_content 是思考过程，content 是纯净正文。
    直接取 content 即可，无需正则过滤。
    """
    config = _get_config()

    # LLM 总开关：关闭时角色扮演旁白降级（世界观解析会走兜底）
    if not llm_enabled():
        logger.info("LLM 总开关关闭，角色扮演旁白跳过")
        return "🔕 LLM 已关闭，本次无旁白（总览页 LLM 板块可开启）"

    # RP LLM 参数（实时读，热重载生效；缺项回退原硬编码默认）
    try:
        llm_p = (config.get("RP_CFG") or {}).get("llm") or {}
    except Exception:
        llm_p = {}
    rp_max_tokens = int(llm_p.get("max_tokens", 32768))
    rp_temperature = float(llm_p.get("temperature", 0.7))
    rp_thinking = str(llm_p.get("thinking", "on"))
    rp_json_mode = bool(llm_p.get("json_mode", False)) and use_json_mode
    rp_timeout = int(llm_p.get("timeout", 1800))

    api_url, model, headers, max_tokens_cap = _resolve_llm_backend(config)
    effective_max_tokens = min(rp_max_tokens, max_tokens_cap) if max_tokens_cap > 0 else rp_max_tokens
    # thinking 档位映射（与 persona 弹窗同款）：off→关思考 / low·max→reasoning_effort / on 不传
    disable_thinking = rp_thinking == "off"
    reasoning_effort = rp_thinking if rp_thinking in ("low", "max") else None

    full_messages = [{"role": "system", "content": system_prompt}]
    full_messages.extend(messages)

    async with _chat_lock:  # 角色扮演与聊天共享聊天锁，确保串行
        async with httpx.AsyncClient(timeout=rp_timeout, trust_env=False) as client:
            try:
                res = await _post_llm_chat(
                    client, api_url, model, headers, full_messages,
                    rp_temperature, effective_max_tokens,
                    disable_thinking=disable_thinking,
                    json_mode=rp_json_mode,
                    reasoning_effort=reasoning_effort,
                )
                content, reasoning = res["content"], res["reasoning"]

                # 用量统计 + 最近请求记录（旁路，失败不影响主链路）
                try:
                    from . import llm_usage
                    if res.get("usage"):
                        await llm_usage.record_usage(res["usage"], res.get("model") or model)
                    # 最近请求摘要（GUI 总览页"最近 LLM 请求"行实时展示）
                    llm_usage.record_request(
                        res.get("model") or model,
                        res.get("content") or res.get("reasoning", ""),
                        source="角色扮演旁白",
                        finish_reason=res.get("finish_reason", ""),
                    )
                except Exception:
                    pass

                # content 已经是纯净的旁白正文，直接返回
                # 清理 content 中可能混入的 thinking 标签（极少数情况）
                content = _strip_thinking_tags(content)

                if content:
                    return content

                # 兜底：content 为空时取 reasoning_content
                if reasoning:
                    return _extract_rpg_narration(reasoning)

                logger.warning("角色扮演 LLM 返回空内容")
                return "🤔 旁白正在思考中..."
            except httpx.TimeoutException:
                logger.error("角色扮演 LLM 调用超时")
                return "⏳ 旁白思考时间太长啦"
            except httpx.HTTPStatusError as e:
                error_body = e.response.text[:300]
                logger.error(f"角色扮演 LLM HTTP 错误: {e.response.status_code} - {error_body}")
                return "😵 旁白那边出了点小问题"
            except Exception as e:
                logger.error(f"角色扮演 LLM 调用异常: {e}")
                return "😵 旁白那边出了点小问题"


def call_llm_sync_from_thread(
    system_prompt: str, user_prompt: str,
    max_tokens: int = 8192, temperature: float = 0.7,
    timeout: int = 1800, priority: int = 0,
    disable_thinking: bool = False, json_mode: bool = False,
    reasoning_effort: Optional[str] = None,
) -> str:
    """
    后台线程同步调用 LLM（供 question_pool 等 threading 场景使用）。
    通过 asyncio.run_coroutine_threadsafe() 将请求投递到主事件循环，
    进入统一的任务优先级队列排队。

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        max_tokens: 最大 token 数
        temperature: 温度参数
        timeout: 超时时间（秒）
        priority: 任务优先级（-1=真心话大冒险最高, 0=用户指令, 1=定时任务）

    Returns:
        LLM 返回的内容字符串
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    async def _async_call():
        return await call_llm(
            messages=messages,
            max_tokens=max_tokens,
            use_lock=True,
            lock_type="task",
            priority=priority,
            disable_thinking=disable_thinking,
            json_mode=json_mode,
            reasoning_effort=reasoning_effort,
        )

    loop = _main_loop
    if loop is None:
        raise RuntimeError("_main_loop not initialized — call _set_main_loop() before using LLM")
    future = asyncio.run_coroutine_threadsafe(_async_call(), loop)
    try:
        return future.result(timeout=timeout + 10)
    except concurrent.futures.TimeoutError:
        # 排队/执行超过总超时：取消协程，避免它继续占用信号量/队列并白跑一次 LLM。
        # 取消在 await 点注入 CancelledError，async with sem / try-finally release 均正常释放。
        future.cancel()
        raise


def _call_llm_chat(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7, timeout: int = 120) -> str:
    """同步调用 LLM（向后兼容，供测试脚本等同步场景使用）。

    生产环境（主事件循环已初始化）走 call_llm_sync_from_thread → 统一任务队列/信号量，
    受并行模式与并发上限控制。
    主事件循环未初始化时（如独立测试脚本，无并发场景）回退到直接 HTTP 调用——
    此时不存在其他并发 LLM 调用，绕过并发控制无影响。
    """
    if _main_loop is not None:
        return call_llm_sync_from_thread(
            system_prompt, user_prompt,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout,
        )

    # 2026-08-22：回退路径（无事件循环，独立脚本）也尊重 LLM 总开关，与 call_llm 一致。
    # 此前回退直接发 HTTP，绕过 llm_enabled() 检查——内存开关"开"而磁盘"关"时
    # （带外改 config 未热加载），回退仍会发请求。统一走总开关，关闭时返回空串
    # （与下方异常路径一致，调用方按"无 LLM 结果"处理）。
    if not llm_enabled():
        logger.info("LLM 总开关关闭，_call_llm_chat 回退路径跳过（降级处理）")
        return ""

    # 回退：无事件循环环境（测试脚本），直接 HTTP 调用
    config = _get_config()
    api_url, model, headers, max_tokens_cap = _resolve_llm_backend(config)
    effective_max_tokens = min(max_tokens, max_tokens_cap) if max_tokens_cap > 0 else max_tokens
    try:
        data = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
        }, ensure_ascii=False).encode("utf-8")

        import urllib.request
        req = urllib.request.Request(
            f"{api_url}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json", **headers},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"LLM 调用失败: {e}")
        return ""
