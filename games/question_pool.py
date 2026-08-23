#!/usr/bin/env python3
"""
QQ 群机器人 - 题库管理模块
包含：LLM 调用、题目池管理、自动出题、自选提问、历史记录
"""
import json
import os
import re
import hashlib
import random
import sqlite3
import time
import threading
import queue
import urllib.request
import urllib.error
import logging
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger("qq-bot")

# ============ 题库文件路径 ============
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(_SCRIPT_DIR), "data")
QUESTION_BANK_DIR = os.path.join(DATA_DIR, "question_bank")
os.makedirs(QUESTION_BANK_DIR, exist_ok=True)
TRUTH_FILE = os.path.join(QUESTION_BANK_DIR, "truth_questions.txt")
DARE_FILE = os.path.join(QUESTION_BANK_DIR, "dare_questions.txt")

# LLM 调用桥接（导入统一队列的同步接口）
from core.llm import call_llm_sync_from_thread, llm_enabled

# 真心话大冒险 LLM 调用优先级（-1 = 最高，高于用户指令 0 和定时任务 1）
# 2026-08-21 配置化：默认值来自 config.yaml truth_dare.priority（GUI 只读展示）
_PRIORITY_TD = -1

# ============ 真心话大冒险配置（2026-08-21 起读 config.yaml truth_dare 段）============
# 配置单一事实源：core/config.py DEFAULTS["truth_dare"]（热加载后 CONFIG["TD_CFG"] 实时生效，
# 各调用点每次读 _td_cfg()，不缓存模块级常量）。
# 提示词默认值 + 渲染：core/truth_dare_prompts.py（用户定制存 config.yaml
# truth_dare.prompts，渲染时 TD_PROMPT_<KEY> 优先 → 代码默认兜底）。
from core.config import CONFIG as _TD_CONFIG
import core.truth_dare_prompts as _tdp


def _td_cfg() -> dict:
    """当前 truth_dare 配置（CONFIG 缺失/异常时回退 DEFAULTS，永不崩溃）。"""
    try:
        td = _TD_CONFIG.get("TD_CFG")
        if isinstance(td, dict) and td:
            return td
    except Exception:
        pass
    from core.config import DEFAULTS
    return DEFAULTS["truth_dare"]


def _td_section(name: str) -> dict:
    try:
        s = _td_cfg().get(name)
        if isinstance(s, dict):
            return s
    except Exception:
        pass
    from core.config import DEFAULTS
    return DEFAULTS["truth_dare"].get(name, {}) or {}


def _td_get(section: str, key: str, default):
    try:
        v = _td_section(section).get(key)
        if v is not None:
            return v
    except Exception:
        pass
    from core.config import DEFAULTS
    return DEFAULTS["truth_dare"][section][key] if (
        section in DEFAULTS["truth_dare"]
        and isinstance(DEFAULTS["truth_dare"][section], dict)
        and key in DEFAULTS["truth_dare"][section]
    ) else default


def _td_llm_kwargs(stage: str) -> dict:
    """阶段 LLM 参数 → 调用关键字（thinking: on/off/low/max 映射，与 core/persona.py 同构）。

    on=不传参（后端默认，DeepSeek 默认 max=原行为）/ off=disable_thinking /
    low·max=reasoning_effort。
    """
    d = _td_section("llm").get(stage) or {}
    kw = {
        "max_tokens": int(d.get("max_tokens", 8192)),
        "temperature": float(d.get("temperature", 0.9)),
        "json_mode": bool(d.get("json_mode", True)),
        "timeout": int(d.get("timeout", 1800)),
    }
    th = str(d.get("thinking", "on")).lower()
    if th == "off":
        kw["disable_thinking"] = True
    elif th in ("low", "max"):
        kw["reasoning_effort"] = th
    return kw


def _td_dare_probability() -> float:
    """大冒险概率（0.0-1.0），群内 /概率 可单游戏覆盖。"""
    try:
        return max(0.0, min(1.0, float(_td_get("game", "dare_probability", 15)) / 100))
    except Exception:
        return 0.15


def _td_default_spiciness() -> int:
    """新游戏开局默认色度档位（0-6，越界自动钳制）"""
    try:
        return max(0, min(6, int(_td_get("game", "default_spiciness", 4))))
    except Exception:
        return 4


# 兼容旧引用（entertainment.py 等 from question_pool import DARE_PROBABILITY）
DARE_PROBABILITY = 0.15

# ============ 用户做过题目历史数据库 ============
TD_HISTORY_DB = os.path.join(DATA_DIR, "truth_dare.db")


@contextmanager
def _get_db(db_path: str):
    """数据库连接上下文管理器 — 统一 WAL 模式 + try-finally 防泄漏

    M10 修复：connect 加 timeout=30——WAL 模式单写者 + 默认 busy timeout 5s，
    多线程（refill 线程 / _bg_generate_questions / _async_summarize / 主流程）
    并发写 truth_dare.db 时偶发 "database is locked"，题目被 except 吞掉静默丢失。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_td_history_db() -> None:
    """初始化用户做过题目的历史记录数据库"""
    with _get_db(TD_HISTORY_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_question_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                question_type TEXT NOT NULL,
                group_id INTEGER,
                answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_type ON user_question_history(user_id, question_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_group ON user_question_history(user_id, group_id)")


_init_td_history_db()

# ============ 自选提问记录数据库 ============
_SELF_SELECT_DB = TD_HISTORY_DB  # 已合并到 truth_dare.db


def _init_self_select_db() -> None:
    """初始化自选提问记录数据库"""
    os.makedirs(os.path.dirname(_SELF_SELECT_DB), exist_ok=True)
    with _get_db(_SELF_SELECT_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS self_select_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                winner_id INTEGER NOT NULL,
                winner_name TEXT NOT NULL,
                target_id INTEGER,
                target_name TEXT,
                original_messages TEXT NOT NULL,
                extracted_questions TEXT NOT NULL,
                game_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_group ON self_select_questions(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_winner ON self_select_questions(winner_id)")


_init_self_select_db()

# ============ 自动模式题目历史数据库 ============
_AUTO_QUESTIONS_DB = TD_HISTORY_DB  # 已合并到 truth_dare.db

# 题目池配置（2026-08-21 配置化：原硬编码常量改为运行时读 config.yaml truth_dare.pool 段，
# 每次调用实时取值 → 热加载生效；GUI「⚙️ 题库规则」弹窗管理）
_QUESTION_SPICINESS_LEVELS = list(range(7))  # 0-6 每一级都预填充  # 预填充的色色程度档位（上限 6，2026-08-14 原 4）


def _td_persona_threshold() -> int:
    """人设题库：单玩家单档位低于 N 道触发补充（原 _QUESTION_POOL_THRESHOLD=8）"""
    try:
        return int(_td_get("pool", "persona_threshold", 8))
    except Exception:
        return 8


def _td_generic_threshold() -> int:
    """通用题库：单档位低于 N 道触发补充（原 _QUESTION_GENERIC_THRESHOLD=40）"""
    try:
        return int(_td_get("pool", "generic_threshold", 40))
    except Exception:
        return 40


def _td_persona_batch() -> int:
    """人设题库每批生成道数（原写死 10）"""
    try:
        return int(_td_get("pool", "persona_batch_size", 10))
    except Exception:
        return 10


def _td_generic_batch() -> int:
    """通用题库每批生成道数（原写死 15）"""
    try:
        return int(_td_get("pool", "generic_batch_size", 15))
    except Exception:
        return 15


def _td_anti_dup_history() -> int:
    """防重历史抓取上限（原 LIMIT 50；通用题库 prompt 注入同此值）"""
    try:
        return int(_td_get("pool", "anti_dup_history", 50))
    except Exception:
        return 50


def _td_prompt_history() -> int:
    """人设题库（批量+现场）prompt 注入的历史条数（原 history[:20]）"""
    try:
        return int(_td_get("pool", "prompt_history", 20))
    except Exception:
        return 20


def _td_persona_max_chars() -> int:
    """出题/入库时人设文本截断字数（原 profile_text[:2000]）"""
    try:
        return int(_td_get("pool", "persona_text_max_chars", 2000))
    except Exception:
        return 2000

# ============================================================
#  色度档位提示词（0-6 七档，每档 note/detail/examples 三段）
#  2026-08-21 起默认值迁至 core/truth_dare_prompts.py（spice_note_<N> 等 21 项，
#  用户可在 GUI「📝 出题提示词」弹窗定制），三处出题函数统一走
#  _tdp.render_prompt() 渲染——天然解决"三处提示词必须同步"的坑
#  （原 _SPICE_LEVEL_PROMPTS 常量表已删除，避免双份文案漂移）。
#
#  全档位通用的图片红线（注入通用规则区）：
#  - 可要求玩家发送【自己的】普通照片（自拍/生活照/相册随机/晒物）
#  - 禁止要求裸露或私密部位照片（淫秽定性）
#  - 禁止要求发送他人照片（第三人肖像权/隐私权，民法典 1019/1032）
#  - 禁止要求证件、定位等敏感个人信息（个人信息保护法）
# ============================================================

# 通用题库补充防重入锁：{spiciness: bool}（一次补充覆盖所有题型）
_GENERIC_REFILL_LOCKS: dict[int, bool] = {}

# 个人题库补充防重入锁：{(user_id, group_id, spiciness): bool}
_PERSONAL_REFILL_LOCKS: dict[tuple[int, int, int], bool] = {}

# 人设浓缩内存缓存：{f"{user_id}:{group_id}:{text_hash}": condensed_persona}
_persona_condense_cache: dict[str, str] = {}

# 对话生成配置
MULTILINE = re.compile(r"^", re.MULTILINE)

# ============ LLM 调用（走统一任务优先级队列）============
# 真心话大冒险优先级 = _PRIORITY_TD = -1（最高，高于用户指令 0 和定时任务 1）

def _call_llm_chat(
    system_prompt: str, user_prompt: str,
    max_tokens: int = 2048, temperature: float = 0.7, timeout: int = 1800,
    priority: int = _PRIORITY_TD, json_mode: bool = False,
    disable_thinking: bool = False, reasoning_effort: str | None = None,
) -> str:
    """调用 LLM 并返回内容字符串（走统一任务优先级队列）

    Args:
        priority: -1=真心话大冒险(最高), 0=用户指令, 1=定时任务
        json_mode: 强制 JSON 输出（response_format=json_object）——出题用，
                   从机制上杜绝评审/规划/序号文本混入
        disable_thinking: DeepSeek 后端关闭思考模式（2026-08-21 新增透传，
                   truth_dare.llm.<stage>.thinking=off 时用）
        reasoning_effort: DeepSeek 思考强度 low/medium/high/max（同上，
                   thinking=low/max 时用；None=后端默认）
    """
    return call_llm_sync_from_thread(
        system_prompt, user_prompt, max_tokens=max_tokens, temperature=temperature,
        timeout=timeout, priority=priority, json_mode=json_mode,
        disable_thinking=disable_thinking, reasoning_effort=reasoning_effort,
    )


def _sync_call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 2048, temperature: float = 0.7, timeout: int = 1800) -> str:
    """同步调用 LLM（后台线程用，别名，走统一队列）"""
    return call_llm_sync_from_thread(
        system_prompt, user_prompt, max_tokens=max_tokens, temperature=temperature,
        timeout=timeout, priority=_PRIORITY_TD,
    )


def _init_auto_questions_db() -> None:
    """初始化自动模式题目历史数据库"""
    os.makedirs(os.path.dirname(_AUTO_QUESTIONS_DB), exist_ok=True)
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                question_type TEXT NOT NULL,
                profile_snapshot TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auto_user_group ON auto_questions(user_id, group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auto_user ON auto_questions(user_id)")

    # 题目池表
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_question_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                question_type TEXT NOT NULL,
                spiciness INTEGER DEFAULT 4,
                profile_snapshot TEXT,
                used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_user_type ON auto_question_pool(user_id, group_id, question_type, spiciness)")
        # 兼容旧表结构：如果没有 spiciness 列则添加
        try:
            conn.execute("ALTER TABLE auto_question_pool ADD COLUMN spiciness INTEGER DEFAULT 4")
            conn.execute("UPDATE auto_question_pool SET spiciness = 4 WHERE spiciness IS NULL")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 兼容旧表结构：如果没有 source 列则添加
        try:
            conn.execute("ALTER TABLE auto_question_pool ADD COLUMN source TEXT DEFAULT 'persona'")
            conn.execute("UPDATE auto_question_pool SET source = 'persona' WHERE source IS NULL")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 通用题目索引：group_id + question_type + spiciness
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_generic ON auto_question_pool(group_id, question_type, spiciness, source, used)")


_init_auto_questions_db()


# ============ LLM 调用 ============


def _parse_numbered_list(content: str) -> list[str]:
    """解析 '1. 题目一\n2. 题目二' 格式的编号列表"""
    import re
    lines = content.strip().split("\n")
    results = []
    for line in lines:
        m = re.match(r"^\d+[.、]\s*(.+)", line.strip())
        if m and m.group(1).strip():
            results.append(m.group(1).strip())
    return results


def _clean_question_text(text: str) -> str:
    """清洗 LLM 出题文本，移除解析残留（2026-08-07 修复带序号脏题 + 评审/规划文本）：
    1. 剥离开头数字序号（"1. " / "1、" / "1) " / "1：" / "1 空格"）
    2. 剥离 LLM 编号/规划前缀（"第9题：题目" / "第9条改为：题目" / "或者第9条改：题目"）
    3. 截断自我评审尾巴（破折号后 25 字内出现评审关键词；或问号/句号+评审词结尾）
    4. 仍含明显评审/指令残留特征 → 返回空串（判废）
    """
    t = text.strip()
    # 1. 剥离开头数字序号：标点必选（1. / 1、/ 1) / 1：）或空格必选（"1 题目"）
    #    数字后直接跟汉字（"12月" "183的"）是正文，绝不剥离
    t = re.sub(r"^\s*\d+\s*[.、)．:：]\s*", "", t)
    t = re.sub(r"^\s*\d+\s+", "", t)
    # 1.5 剥离 JSON 对象 key 残留（2026-08-10：json_mode 失效时 LLM 输出 {"1": "题目"...}，
    #    降级行提取后题目带 `1": "` 前缀——`"1": "发一条语音...` 或 `1": "发一条语音...`；
    #    以及数字已被旧清洗剥掉后残留的 `: "` 前缀）
    t = re.sub(r"^\"?\d+\"\s*:\s*\"", "", t)
    t = re.sub(r"^[:：]\s*\"", "", t)
    # 2. 剥离 LLM 编号/规划前缀（"第9题：题目" / "第9条改为：题目" / "或者第9条改：题目" /
    #    "好，现在第9题：题目" / "把第9题换成：题目"）——剥离后若仍是评审文本会被第 4 步判废
    m = re.match(
        r"^(?:第\s*\d+\s*[题条个](?:\s*(?:改为|可以改成|改成|换成|换成一个更))?"
        r"|或者第\s*\d+\s*[题条个]\s*改"
        r"|好，现在第\s*\d+\s*[题条个]"
        r"|把第\s*\d+\s*[题条个]\s*(?:换成|改为))"
        r"[:：]?\s*",
        t,
    )
    if m:
        t = t[m.end():].lstrip("：:'\"“” 「」")
    # 3a. 破折号评审尾巴：——/—/-- 后 25 字内出现评审关键词 → 从破折号截断
    m = re.search(
        r"[—–-]\s*(?=.{0,25}(已有|重复|避免|可算新|算新|允许|不可靠|接近|类似|更接近|"
        r"但感觉|核心动作|感觉一般|略重复|算重复|有点重复|无[。]?|已问过|等等|太露骨|有点黄|这个可以|不行|可以[。，]))",
        t,
    )
    if m:
        t = t[:m.start()].rstrip()
    # 3b. 无破折号尾部评审：问号/句号 + 评审词收尾
    m2 = re.search(r"[？?。]\s*(不可靠|不允许|不太行|太麻烦|不现实|不适用)\s*$", t)
    if m2:
        t = t[:m2.start()].rstrip()
    # 3c. 尾部类型标注（LLM 清单格式）："...一个语音。"/"...一个操作。" 截断
    m3 = re.search(r"[”'\"]?\s*一个(语音|操作|动作|文字|截图|图片|消息|投票|视频)[。！]?\s*$", t)
    if m3:
        t = t[:m3.start()].rstrip().rstrip("\"'”")
    # 4. 判废：仍含明显评审/指令残留特征（这些词正常题目不会出现）
    if re.search(
        r"(我认为|我觉得|可算新|算新|替代|变体|备选|感觉一般|以内：|我来数|不可靠|不允许|不现实|"
        r"可能重复|略重复|我需要|可以留|换掉|都是.*相关|"
        r"输出要求|JSON 数组|直接输出|解释性文字|格式示例|请基于|以下是|以上是|"
        r"检查第|注意第|其实第|担心第|^检查|^再检查|^等等|^或者|^还需要|^还要|^让我把|^让我来|^让我决定|"
        r"^我是否|^嗯，|^好吧|^但为了|^不过担心|我担心|会不会太|OK[。！]?$|"
        r"^字数[:：]|^让我再|^让我重新|^让我修改|^让我改进|^让我做|^让我们|^我再想想|^新挑战|"
        r"^不过第|^但第\s*\d|^其实，|^再想想|^我修改|^修改第|^把第\s*\d+\s*[题条个]\s*(?:改|换)|"
        r"^可以改|^另一个想法|^另一个约束|^没有假设|^不确定性|^我的第|^#\s*\d+|"
        r"^好，现在|^好，把|^最后，也许|^还需|^再想想|^让我们努力|"
        r"#\s*\d|^Let |^Wait,|^Wait |关于#|在#|和#|↔ #|^重新标记|^另一个候选|^现在，题|^机器人[:：]|"
        r"^我想再|^我再加)",
        t,
    ):
        return ""
    # 5. 清洗后过短（≤3 字）视为评审残渣判废
    if len(t) <= 3:
        return ""
    return t.strip()


def _parse_llm_json_array(content: str) -> list[str]:
    """从 LLM 返回内容中提取 JSON 数组，含容错降级"""
    content = content.strip()
    # 去掉代码块标记（M5 修复：原实现只取第一行非围栏行，
    # 多行 JSON 数组会被截断只剩 "[" → 解析失败 → 补充中断）
    if "```" in content:
        fence_lines = [
            line for line in content.split("\n")
            if not line.strip().startswith("```")
        ]
        content = "\n".join(fence_lines).strip()

    # 尝试提取 JSON 数组（2026-08-10：同时支持 dict——json_mode 下 LLM 常输出
    # {"1": "题目"...} 对象，原实现只找 [ ] 导致 dict 永远走降级行提取：
    # 单行 dict 被 { 开头跳过（题目全丢）、多行键值对产生 `1": "` 前缀污染）
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end == -1:
        start = content.find("{")
        end = content.rfind("}")
    if start != -1 and end != -1:
        try:
            parsed = json.loads(content[start:end + 1])
            if isinstance(parsed, list):
                # 清洗每个元素：剥离序号、截断评审尾巴（2026-08-07 修复）
                cleaned = []
                for q in parsed:
                    c = _clean_question_text(q) if isinstance(q, str) else ""
                    if c:
                        cleaned.append(c)
                return cleaned
            if isinstance(parsed, dict):
                # json_object 模式可能返回 {"key": ["题目1", ...]} 或 {"key": "题目"}
                # 提取所有字符串/字符串列表值（2026-08-07 json_mode 支持）
                items: list[str] = []
                for v in parsed.values():
                    if isinstance(v, str):
                        items.append(v)
                    elif isinstance(v, list):
                        items.extend(x for x in v if isinstance(x, str))
                cleaned = [_clean_question_text(q) for q in items]
                return [c for c in cleaned if c]
        except json.JSONDecodeError:
            pass

    # 降级：按行提取
    lines = [l.strip().strip('-"\'').strip() for l in content.split("\n") if l.strip()]
    results = []
    for l in lines:
        if not l or l.startswith("[") or l.startswith("{") or l == "[]":
            continue
        c = _clean_question_text(l)
        if c:
            results.append(c)
    return results


def _call_llm_questions(system_prompt: str, question_type: str, count: int,
                        stage: str = "batch_persona") -> list[str]:
    """LLM 出题统一调用：解析 + 质量校验 + 失败重试（2026-08-10 建立，2026-08-21 配置化）。

    参数全部来自 config.yaml truth_dare.llm.<stage>（GUI「🤖 LLM 参数」弹窗管理）：
    max_tokens / temperature / thinking / json_mode / timeout；
    重试次数来自 truth_dare.llm_retries（额外重试，原行为=1）。

    第一遍失败时 _parse_llm_json_array 内部先走降级行提取抢救（有货用货）；
    结果为空或质量校验全挂 → 按 llm_retries 重试（偶发畸形/抽风的完整恢复，
    重试大概率输出干净 JSON——比继续从垃圾里捞碎渣更好）；
    仍失败 → 返回 []（调用方走默认题兜底）。
    """
    # LLM 总开关关闭时直接返回空列表——否则会走 call_llm 的降级文案返回路径，
    # 降级文案 "🔕 LLM 已关闭..." 被 _parse_llm_json_array 的行降级提取当成 1 道
    # "题目"解析出来（非空列表），调用方 while 补充循环去重入库恒为 0 →
    # current_count 永远到不了阈值 → 无限循环刷日志（2026-08-21 事故：
    # 15 小时空转 1833 万行 / 3.5GB bot.log）。根治：LLM 不可用时不产出"假题目"。
    if not llm_enabled():
        return []
    d = _td_section("llm").get(stage) or {}
    max_tokens = int(d.get("max_tokens", 8192))
    temperature = float(d.get("temperature", 0.9))
    timeout = int(d.get("timeout", 1800))
    json_mode = bool(d.get("json_mode", True))
    th = str(d.get("thinking", "on")).lower()
    try:
        attempts = 1 + max(0, int(_td_cfg().get("llm_retries", 1)))
    except Exception:
        attempts = 2
    for attempt in range(1, attempts + 1):
        content = _call_llm_chat(system_prompt, "", max_tokens=max_tokens,
                                 temperature=temperature, timeout=timeout,
                                 priority=_PRIORITY_TD, json_mode=json_mode,
                                 disable_thinking=(th == "off"),
                                 reasoning_effort=th if th in ("low", "max") else None)
        if content:
            parsed = _parse_llm_json_array(content)
            valid = [q for q in parsed if _validate_question_quality(q, question_type)]
            if valid:
                return valid[:count]
            logger.warning(
                f"[出题] 第{attempt}次解析到 {len(parsed)} 道但质量校验全挂，"
                f"{'重试' if attempt < attempts else '放弃'}"
            )
        else:
            logger.warning(
                f"[出题] 第{attempt}次 LLM 返回空，{'重试' if attempt < attempts else '放弃'}"
            )
    return []
# ============ 自选提问 ============

def _save_self_select(group_id: int, winner_id: int, winner_name: str,
                      target_id: int | None, target_name: str | None,
                      original_msgs: list[str], extracted: list[str],
                      game_type: str) -> None:
    """保存自选提问记录到数据库"""
    with _get_db(_SELF_SELECT_DB) as conn:
        conn.execute(
            "INSERT INTO self_select_questions "
            "(group_id, winner_id, winner_name, target_id, target_name, "
            "original_messages, extracted_questions, game_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                group_id, winner_id, winner_name,
                target_id, target_name,
                json.dumps(original_msgs, ensure_ascii=False),
                json.dumps(extracted, ensure_ascii=False),
                game_type,
            ),
        )


def _parse_self_select_questions(messages: list[str], game_type: str) -> list[str]:
    """
    调用 LLM 判断哪些消息是真心话/大冒险的提问问题。
    返回提取出的问题列表。
    """
    if not messages:
        return []

    # 过滤掉纯表情、过短消息、@通知等
    valid_msgs = [m for m in messages if len(str(m).lstrip("@").strip()) >= 2]
    if not valid_msgs:
        return []

    msg_lines = "\n".join(f"- {msg}" for msg in valid_msgs)
    question_type = "真心话或大冒险" if game_type == "mixed" else ("真心话" if game_type == "truth" else "大冒险")

    system_prompt = (
        f"你是一个真心话大冒险游戏的辅助助手。"
        f"当前是自选模式，赢家可以向输家自由提问{question_type}。"
        f"以下是赢家在轮次期间发送的所有群消息。"
        f"请判断哪一条（或哪几条）是向输家提出的真心话/大冒险问题。"
        f"\n\n规则：\n"
        f"1. 有效提问可以是疑问句、陈述句或挑战指令——不一定带问号，只要是向输家提出的问题或挑战就算。\n"
        f"2. 闲聊、表情回复、与游戏无关的内容不算提问。\n"
        f"3. 如果多条消息构成一个完整问题，合并为一条。\n"
        f"4. 如果没有发现有效提问，返回空数组。\n\n"
        f"请只返回 JSON 数组格式，例如 [\"问题1\", \"问题2\"]，不要包含其他解释。"
    )

    user_prompt = f"以下是赢家发送的消息列表：\n{msg_lines}\n\n请提取有效提问（JSON数组）："

    content = _call_llm_chat(system_prompt, user_prompt, max_tokens=8192, temperature=0.3, json_mode=True)
    if content:
        return _parse_llm_json_array(content)

    # 降级策略
    logging.getLogger("qq-bot").warning("自选提问 LLM 解析失败，使用降级策略")
    fallback = [m for m in valid_msgs if "？" in str(m) or "?" in str(m)]
    return fallback if fallback else valid_msgs[:1]


def _generate_suggestions(game: dict, user_desc: str, winner_name: str, target_name: str) -> list[str]:
    """
    调用 LLM 根据用户的自然语言描述生成 4 个符合当前轮次类型的真心话/大冒险问题。
    返回问题列表（最多 4 个）。
    """
    round_type = game.get("round_type", "truth")
    type_cn = "真心话" if round_type == "truth" else "大冒险"
    action = "回答" if round_type == "truth" else "完成"

    system_prompt = (
        f"你是一个真心话大冒险游戏的题目设计助手，风格大胆、犀利、有冲击力——敢于触及隐私和性相关话题。"
        f"当前轮次类型是「{type_cn}」。"
        f"赢家「{winner_name}」需要向输家「{target_name}」提出{type_cn}。"
        f"用户给出了一段自然语言描述，请根据这段描述设计 4 个符合描述的{type_cn}题目。"
        f"\n\n规则：\n"
        f"1. 题目必须是{type_cn}——{'疑问句，直击隐私、秘密、情感、尴尬经历、内心想法或性相关话题' if round_type == 'truth' else '挑战指令，让对方完成社死、尴尬、突破舒适区或带有性暗示的行动'}。\n"
        f"2. 4 个题目要有递进层次：第 1 题是入门热身，第 2 题开始深入，第 3 题大胆犀利，第 4 题尺度最大——可以直接涉及性经历、性器官、性幻想或最私密的身体体验。\n"
        f"3. 根据用户描述的尺度来调整——描述越大胆，题目就要越有冲击力，不要保守。第 4 题可以点名提及生殖器官、性行为细节。\n"
        f"4. 题目要具体、有场景感，让对方难以用'没有''不知道'敷衍过去。\n"
        f"5. 只返回 JSON 数组，例如 [\"题目1\", \"题目2\", \"题目3\", \"题目4\"]，不要包含解释。\n"
    )

    user_prompt = f"用户描述：{user_desc}\n\n请生成 4 个{type_cn}题目（JSON数组）："

    content = _call_llm_chat(system_prompt, user_prompt, max_tokens=4096, temperature=0.8, json_mode=True)
    if content:
        parsed = _parse_llm_json_array(content)
        return parsed[:4]

    return [f"基于「{user_desc}」设计一个{type_cn}题目"]


def list_self_select(group_id: int) -> str:
    """列出本群的自选提问历史"""
    with _get_db(_SELF_SELECT_DB) as conn:
        rows = conn.execute(
            "SELECT winner_name, target_name, extracted_questions, game_type, created_at "
            "FROM self_select_questions WHERE group_id = ? ORDER BY id DESC LIMIT 10",
            (group_id,),
        ).fetchall()

    if not rows:
        return "📋 本群暂无自选提问历史"

    result = "📋 最近的自选提问记录：\n\n"
    for row in rows:
        winner, target, questions, qtype, created = row
        qs = json.loads(questions) if isinstance(questions, str) else questions
        type_tag = "🔵 真心话" if qtype == "truth" else "🔴 大冒险"
        result += f"{type_tag} @{winner} → @{target or '未知'}\n"
        for q in qs[:2]:
            result += f"  「{q[:50]}...」\n"
        result += f"  {created[:16]}\n\n"

    return result


# ============ 提问建议处理器（需要 TD 游戏状态，回调由 entertainment 传入） ============

def handle_suggest_question(group_id: int, user_id: int, nickname: str,
                            user_desc: str, game: dict) -> tuple[str, list[int]]:
    """
    赢家描述想要问的方向，AI 生成 4 个选项供选择。
    需要传入 TD 游戏状态 game。
    """
    if not game:
        return ("🎮 当前没有正在进行的真心话大冒险游戏", [])

    max_uids = game.get("max_players", [])
    if user_id not in max_uids:
        return ("🤔 只有最大点数者才能使用提问建议", [])

    winner_name = nickname or f"用户{user_id}"
    target_uids = game.get("min_players", [])
    # L13 修复：原实现读 game["min_player_names"]（从未被写入，恒为"未知"），
    # 改为从 players 列表按 uid 解析名字（game.players = [{"id", "name"}, ...]）
    player_names = {p.get("id"): p.get("name", "") for p in game.get("players", [])}
    target_name = "、".join(
        player_names.get(uid, f"用户{uid}") for uid in target_uids
    ) or "未知"

    game["pending_suggestions"] = None
    game["suggestion_winner_uid"] = user_id

    suggestions = _generate_suggestions(game, user_desc, winner_name, target_name)

    if not suggestions:
        return ("⚠️ AI 出题失败，请稍后重试", [])

    game["pending_suggestions"] = suggestions
    result = f"🎯 @{winner_name} 的提问建议（回复数字 1-4 选择）：\n\n"
    for i, s in enumerate(suggestions, 1):
        result += f"【{i}】{s}\n"
    result += "\n💡 发送数字选择，或发送「/取消」放弃建议"

    return result, list(set(max_uids + target_uids))


def handle_confirm_suggestion(group_id: int, user_id: int, choice: int) -> tuple[str, list[int]]:
    """
    赢家确认选择第几个建议。

    BUG 修复（2026-08-03）：此函数曾是 stub（永远返回占位文本）。
    实际确认逻辑已移至 entertainment.py 的 _handle_message 数字确认分支
    （它有 _TRUTH_DARE_GAMES 状态引用），这里保留签名仅作兼容兜底。
    """
    return ("⚠️ 提问建议确认已由新流程处理，请直接在群里回复数字 1-4", [])


def _handle_confirm_impl(group_id: int, user_id: int, choice: int) -> str:
    """（废弃）确认提问建议的旧 stub 实现，保留仅供兼容引用"""
    return "⚠️ 旧版确认接口已废弃，请直接在群里回复数字 1-4"


def handle_cancel_suggestion(group_id: int, user_id: int) -> tuple[str, list[int]]:
    """取消提问建议"""
    return ("🗑️ 已取消提问建议，你可以直接发送问题或发送「/下一轮」结算本轮", [])


# ============ 历史记录管理 ============

def _get_done_questions(user_ids: list[int], question_type: str) -> set[str]:
    """获取一组用户已经做过的题目集合"""
    with _get_db(TD_HISTORY_DB) as conn:
        placeholders = ",".join("?" for _ in user_ids)
        rows = conn.execute(
            f"SELECT DISTINCT question_text FROM user_question_history "
            f"WHERE user_id IN ({placeholders}) AND question_type = ?",
            user_ids + [question_type],
        ).fetchall()
    return {row[0] for row in rows}


def _record_question(user_ids: list[int], question: str, question_type: str) -> None:
    """记录用户做过的题目"""
    with _get_db(TD_HISTORY_DB) as conn:
        for uid in user_ids:
            conn.execute(
                "INSERT INTO user_question_history (user_id, question_text, question_type) "
                "VALUES (?, ?, ?)",
                (uid, question, question_type),
            )


def _clear_history(user_id: int, group_id: int | None) -> int:
    """清空用户的题目历史，返回删除的记录数"""
    with _get_db(TD_HISTORY_DB) as conn:
        if group_id is not None:
            cursor = conn.execute(
                "DELETE FROM user_question_history WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM user_question_history WHERE user_id = ?",
                (user_id,),
            )
        count = cursor.rowcount
    return count


def _get_history_count(user_id: int) -> int:
    """获取用户做过的题目总数"""
    with _get_db(TD_HISTORY_DB) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM user_question_history WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row[0] if row else 0


# ============ 自动模式历史记录 ============

def _get_auto_history(user_id: int, group_id: int | None = None) -> list[str]:
    """获取用户在自动模式下已被问过的问题列表（最近 N 道，N=pool.anti_dup_history）"""
    limit = _td_anti_dup_history()
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        if group_id is not None:
            rows = conn.execute(
                "SELECT question_text FROM auto_questions WHERE user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, group_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT question_text FROM auto_questions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [row[0] for row in rows]


def _record_auto_question(user_id: int, group_id: int, question: str, question_type: str, profile_snapshot: str = "") -> None:
    """记录自动模式下的提问"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        conn.execute(
            "INSERT INTO auto_questions (user_id, group_id, question_text, question_type, profile_snapshot) VALUES (?, ?, ?, ?, ?)",
            (user_id, group_id, question, question_type, profile_snapshot[:_td_persona_max_chars()]),
        )


# ============ 题目池管理 ============

def _get_pool_count(user_id: int, group_id: int, question_type: str, spiciness: int | None = None) -> int:
    """获取用户某类型题目池中未使用的题目数量。可选指定 spiciness 过滤。"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        if spiciness is not None:
            row = conn.execute(
                "SELECT COUNT(*) FROM auto_question_pool WHERE user_id = ? AND group_id = ? AND question_type = ? AND used = 0 AND spiciness = ?",
                (user_id, group_id, question_type, spiciness),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM auto_question_pool WHERE user_id = ? AND group_id = ? AND question_type = ? AND used = 0",
                (user_id, group_id, question_type),
            ).fetchone()
    return row[0]


def _get_generic_pool_count(question_type: str, spiciness: int) -> int:
    """获取通用题库中某类型/色度的未使用题目数量（全局共享，不按群区分）。"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM auto_question_pool WHERE question_type = ? AND used = 0 AND spiciness = ? AND source = 'generic'",
            (question_type, spiciness),
        ).fetchone()
    return row[0]


def _pop_question_from_pool(user_id: int, group_id: int, question_type: str, spiciness: int | None = None) -> str | None:
    """从用户个人池中按 FIFO 取一道未使用的题，标记为已用。可选指定 spiciness 过滤。返回 None 表示池空。"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        if spiciness is not None:
            row = conn.execute(
                "SELECT id, question_text FROM auto_question_pool WHERE user_id = ? AND group_id = ? AND question_type = ? AND used = 0 AND spiciness = ? ORDER BY created_at ASC LIMIT 1",
                (user_id, group_id, question_type, spiciness),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, question_text FROM auto_question_pool WHERE user_id = ? AND group_id = ? AND question_type = ? AND used = 0 ORDER BY created_at ASC LIMIT 1",
                (user_id, group_id, question_type),
            ).fetchone()
        if row:
            conn.execute(
                "UPDATE auto_question_pool SET used = 1, used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row[0],),
            )
            return row[1]
        return None


def _pop_cross_group_question(user_id: int, exclude_group_id: int, question_type: str, spiciness: int | None = None) -> str | None:
    """从该用户的其他群池中取一道未使用的题（跨群回退机制）。

    按题目创建时间倒序（优先取最近的群），命中后标记已用。
    返回 None 表示所有群都没有可用题目。
    """
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        if spiciness is not None:
            row = conn.execute(
                "SELECT id, question_text FROM auto_question_pool "
                "WHERE user_id = ? AND group_id != ? AND question_type = ? "
                "AND used = 0 AND spiciness = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id, exclude_group_id, question_type, spiciness),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, question_text FROM auto_question_pool "
                "WHERE user_id = ? AND group_id != ? AND question_type = ? "
                "AND used = 0 "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id, exclude_group_id, question_type),
            ).fetchone()
        if row:
            conn.execute(
                "UPDATE auto_question_pool SET used = 1, used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row[0],),
            )
            logger.info(f"[跨群回退] 从其他群为 @{user_id} 取到{question_type}题目")
            return row[1]
        return None


def _pop_generic_question_from_pool(question_type: str, spiciness: int) -> str | None:
    """从通用池中取一道未使用的题，标记为已用（全局共享，不按群区分）。返回 None 表示池空。"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        row = conn.execute(
            "SELECT id, question_text FROM auto_question_pool WHERE question_type = ? AND used = 0 AND spiciness = ? AND source = 'generic' ORDER BY created_at ASC LIMIT 1",
            (question_type, spiciness),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE auto_question_pool SET used = 1, used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row[0],),
            )
            return row[1]
        return None
    # M9 修复：删除不可达死代码（with 块已关闭连接，此处 conn.close()/return 永不执行）


def _add_questions_to_pool(user_id: int, group_id: int, questions: list[str], question_type: str,
                           profile_snapshot: str = "", spiciness: int = 4, source: str = "persona") -> int:
    """批量添加题目到池中。source: 'persona'（人设题）或 'generic'（通用题）
    通用题 group_id 强制为 0（全局共享）。入库前跳过已存在的题目（防重复）。
    返回实际插入数量（去重后）。"""
    added = 0
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        for q in questions:
            effective_group = 0 if source == "generic" else group_id
            # 防重复：检查相同文本是否已存在
            row = conn.execute(
                "SELECT id FROM auto_question_pool WHERE question_text = ? AND question_type = ? AND spiciness = ? AND source = ? AND group_id = ?",
                (q, question_type, spiciness, source, effective_group),
            ).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO auto_question_pool (user_id, group_id, question_text, question_type, spiciness, profile_snapshot, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, effective_group, q, question_type, spiciness, profile_snapshot, source),
            )
            added += 1
    return added


def _get_pool_used_history(user_id: int, group_id: int, question_type: str) -> list[str]:
    """获取用户已有题目列表（含已用+未用，避免重复生成相同题目；条数走配置）"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        rows = conn.execute(
            "SELECT question_text FROM auto_question_pool WHERE user_id = ? AND group_id = ? AND question_type = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, group_id, question_type, _td_anti_dup_history()),
        ).fetchall()
    return [row[0] for row in rows]


# ============ 题目质量验证 ============

_micro_regexes = [
    r"具体.{0,8}画面", r"具体.{0,5}反应", r"具体.{0,3}是啥",
    r"具体.{0,3}是什么", r"具体.{0,3}感受", r"具体.{0,3}感觉",
    r"具体.{0,3}描述", r"具体.{0,3}回答", r"具体.{0,3}做法",
]


def _validate_question_quality(question: str, question_type: str) -> bool:
    """
    题目质量过滤：不合格的题目直接丢弃。
    返回 True 表示通过，False 表示不合格。
    """
    # 解析残留检查（2026-08-07 修复带序号脏题）：以数字序号开头（1. / 1、/ 1) 等）→ 不合格
    if re.match(r"^\s*\d+\s*[.、)．:：]", question):
        return False
    # 评审特征检查：清洗函数判废（含"我认为/可算新/替代/变体"等纯评审词）→ 不合格
    if not _clean_question_text(question):
        return False
    # 基本长度检查
    if len(question) < 10:
        return False
    # 真心话：不要超过 100 字（场景编排过重）
    if question_type == "truth" and len(question) > 100:
        return False
    # 大冒险：不要超过 120 字
    if question_type == "dare" and len(question) > 120:
        return False

    # 复合提问检查：超过 2 个问号/句号+问号，大概率是复合提问
    question_marks = question.count("？") + question.count("?")
    if question_marks > 2:
        return False

    # 微观量化：没人能凭空回答的问题（精确匹配 + 正则模糊匹配）
    micro_patterns = [
        "几分", "硬到多离谱", "具体画面是啥", "具体画面是什么",
        "具体频率", "具体步骤", "具体描述一下", "几分硬度",
        "胀硬到几分", "胀痛几分", "硬到几分",
    ]
    for pattern in micro_patterns:
        if pattern in question:
            return False
    # 模糊匹配：具体.{0,8}画面/反应/是啥/是什么/感受/感觉/描述
    for regex in _micro_regexes:
        if re.search(regex, question):
            return False

    # 人设硬套：把推测的 XP 当事实来问
    persona_hard_patterns = [
        "你极度洁癖", "你M属性", "你洁癖重到", "你XP里",
        "你总口嗨", "你总爱在群里口嗨", "你爱穿刺",
        "你痴迷", "你重度迷恋", "你偏好强势",
    ]
    for pattern in persona_hard_patterns:
        if pattern in question:
            return False

    # 大冒险：必须包含行动指令关键词
    if question_type == "dare":
        action_keywords = ["发", "改", "截", "拍", "连", "去", "翻", "把", "设", "改成"]
        has_action = any(kw in question for kw in action_keywords)
        if not has_action:
            return False

    return True


# ============ 批量生成题目 ============

def _batch_generate_questions(user_id: int, nickname: str, profile_text: str,
                               history: list[str], spiciness: int, question_type: str,
                               group_id: int, count: int = 5, gender: str | None = None) -> list[str]:
    """批量生成题目供入库——LLM 一次性输出多道（2026-08-21 起提示词统一走
    core/truth_dare_prompts.render_prompt 共用块，三处出题函数同步，用户可在
    GUI「📝 出题提示词」弹窗定制任意一条）"""
    count_str = "真心话" if question_type == "truth" else "大冒险"
    count_num = "1道" if count == 1 else str(count) + "道"

    # 色度分级说明（每档独立提示词，走提示词配置：spice_note/detail/examples_<N>）
    _sp = max(0, min(6, int(spiciness)))
    spice_note = _tdp.render_prompt(f"spice_note_{_sp}")
    spice_detail = _tdp.render_prompt(f"spice_detail_{_sp}")
    spice_examples = _tdp.render_prompt(f"spice_examples_{_sp}")

    # 题型规则（2026-08-21 统一为详细版，三处共用同一份可定制提示词）
    type_rule = _tdp.render_prompt("truth_rules" if question_type == "truth" else "dare_rules")

    gender_note = ""
    if gender and gender not in ("未知", ""):
        gender_note = f"⚠️ 对方性别：{gender}"

    hist_n = _td_prompt_history()
    history_str = "\n".join(f"- {q}" for q in history[:hist_n]) if history else "（暂无历史记录）"

    system_prompt = f"""{_tdp.render_prompt("opening")}

你要为 {nickname} 生成 {count} 道{count_str}题目。

{type_rule}

{_tdp.render_prompt("common_rules")}
{_tdp.render_prompt("persona_guidance")}
{_tdp.render_prompt("dedup_rules", {"history_label": "已问过的题目"})}

{_tdp.render_prompt("image_rules")}

**色度分级标准**：
{spice_detail}

{spice_examples}

用户人设：{profile_text if profile_text else "暂无人设信息"}
已问过的题目（避免重复）：
{history_str}

---
{spice_note}
{gender_note}
{_tdp.render_prompt("output_discipline")}

{_tdp.render_prompt("output_format", {"count": count})}
"""

    # LLM 参数/超时来自 config.yaml truth_dare.llm.batch_persona（GUI 可配）
    return _call_llm_questions(system_prompt, question_type, count, stage="batch_persona")


def _batch_generate_generic_questions(question_type: str, spiciness: int, count: int = 5) -> list[str]:
    """批量生成通用题目（不依赖人设，适用于无人设用户；提示词走共用块，
    题型规则 2026-08-21 统一为详细版）"""
    count_str = "真心话" if question_type == "truth" else "大冒险"

    # 色度分级说明（每档独立提示词，走提示词配置）
    _sp = max(0, min(6, int(spiciness)))
    spice_note = _tdp.render_prompt(f"spice_note_{_sp}")
    spice_detail = _tdp.render_prompt(f"spice_detail_{_sp}")
    spice_examples = _tdp.render_prompt(f"spice_examples_{_sp}")

    # 题型规则（2026-08-21 统一为详细版，三处共用同一份可定制提示词）
    type_rule = _tdp.render_prompt("truth_rules" if question_type == "truth" else "dare_rules")

    # 获取通用题库中已有的题目，避免重复（抓取上限走配置 anti_dup_history）
    try:
        from games.question_pool import _get_db, _AUTO_QUESTIONS_DB
        with _get_db(_AUTO_QUESTIONS_DB) as conn:
            existing = conn.execute(
                "SELECT question_text FROM auto_question_pool WHERE question_type = ? AND spiciness = ? AND source = 'generic' ORDER BY created_at DESC LIMIT ?",
                (question_type, spiciness, _td_anti_dup_history()),
            ).fetchall()
        existing_text = "\n".join(f"- {row[0]}" for row in existing) if existing else "（暂无）"
    except Exception:
        existing_text = "（暂无）"

    system_prompt = f"""{_tdp.render_prompt("opening")}

你要生成 {count} 道通用{count_str}题目（适用于任何人，不依赖具体人设）。

{type_rule}

{_tdp.render_prompt("common_rules")}
{_tdp.render_prompt("dedup_rules", {"history_label": "已有题目"})}

{_tdp.render_prompt("image_rules")}

**色度分级标准**：
{spice_detail}

{spice_examples}

已有题目（避免重复）：
{existing_text}

---
{spice_note}
{_tdp.render_prompt("output_discipline")}

{_tdp.render_prompt("output_format", {"count": count})}
"""

    # LLM 参数/超时来自 config.yaml truth_dare.llm.batch_generic（GUI 可配）
    return _call_llm_questions(system_prompt, question_type, count, stage="batch_generic")


# ============ 题目池自动补充 ============

def _ensure_player_pool(user_id: int, group_id: int, nickname: str, profile_text: str, spiciness: int, gender: str | None = None) -> None:
    """确保指定玩家在题目池中有足够的题目（低于阈值时循环补充，每批 N 道直到超过阈值；
    阈值/批次数读 config.yaml truth_dare.pool，GUI 可配）"""
    # 防重入锁：同一玩家同一档位正在补充时跳过
    lock_key = (user_id, group_id, spiciness)
    if _PERSONAL_REFILL_LOCKS.get(lock_key):
        return
    _PERSONAL_REFILL_LOCKS[lock_key] = True
    try:
        threshold = _td_persona_threshold()
        batch_size = _td_persona_batch()
        max_chars = _td_persona_max_chars()
        for qtype in ["truth", "dare"]:
            current_count = _get_pool_count(user_id, group_id, qtype, spiciness)
            if current_count >= threshold:
                continue

            history = _get_pool_used_history(user_id, group_id, qtype)
            batch = 0
            no_progress = 0
            # 循环补充：每批 batch_size 道，直到超过阈值。
            # 双重防死循环（2026-08-21 事故：LLM 关闭时此循环空转 15 小时 / 1833 万行）：
            #   ① 批次数上限 50（正常 8 道阈值 1-2 批即达，跑满 50 批纯属异常）
            #   ② 连续 3 批入库 0 题（去重全命中 / LLM 返回无效内容）即停止
            while current_count < threshold:
                batch += 1
                if batch > 50:
                    logger.warning(f"[题目池] @{nickname} {qtype} spiciness={spiciness}: 已跑 {batch-1} 批仍未达阈值 {threshold}，触发批次上限停止（疑似 LLM 失效/去重死锁）")
                    break
                questions = _batch_generate_questions(
                    user_id, nickname, profile_text, history, spiciness, qtype, group_id, batch_size,
                    gender=gender,
                )
                if questions:
                    # M3 修复：用实际入库数（去重后）累加，而非 LLM 生成数——
                    # 全重复时池实际不足阈值却误以为已补满，每轮重复触发补充浪费 LLM
                    added_count = _add_questions_to_pool(user_id, group_id, questions, qtype, profile_text[:max_chars], spiciness, "persona")
                    history.extend(questions)
                    current_count += added_count
                    no_progress = 0 if added_count > 0 else no_progress + 1
                    logger.info(f"[题目池] @{nickname} {qtype} spiciness={spiciness}: 第{batch}次 +{added_count}/{len(questions)} (当前 {current_count})")
                    if no_progress >= 3:
                        logger.warning(f"[题目池] @{nickname} {qtype} spiciness={spiciness}: 连续 {no_progress} 批入库 0 题，停止补充避免死循环")
                        break
                else:
                    break
    except Exception as e:
        logger.error(f"[题目池] 为用户 @{nickname} 补充异常: {e}")
    finally:
        _PERSONAL_REFILL_LOCKS.pop(lock_key, None)


def _refill_generic_pool(group_id: int, spiciness: int) -> None:
    """补充通用题目池（无人设用户的后备题库，每批 N 道直到超过阈值；
    阈值/批次数读 config.yaml truth_dare.pool，GUI 可配）"""
    threshold = _td_generic_threshold()
    batch_size = _td_generic_batch()
    for qtype in ["truth", "dare"]:
        current_count = _get_generic_pool_count(qtype, spiciness)
        if current_count >= threshold:
            continue

        batch = 0
        no_progress = 0
        # 循环补充：每批 batch_size 道，直到超过阈值。
        # 防死循环（2026-08-21 事故）：批次数上限 50 + 连续 3 批入库 0 题即停止；
        # 同时改用实际入库数（去重后）累加——原 len(questions) 累加会在去重全命中时
        # 让 current_count 虚增到阈值提前退出（表面正常，实际池仍缺题）。
        while current_count < threshold:
            batch += 1
            if batch > 50:
                logger.warning(f"[通用题库] {qtype} spiciness={spiciness}: 已跑 {batch-1} 批仍未达阈值 {threshold}，触发批次上限停止（疑似 LLM 失效/去重死锁）")
                break
            questions = _batch_generate_generic_questions(qtype, spiciness, batch_size)
            if questions:
                added_count = _add_questions_to_pool(0, group_id, questions, qtype, "", spiciness, "generic")
                current_count += added_count
                no_progress = 0 if added_count > 0 else no_progress + 1
                logger.info(f"[通用题库] {qtype} spiciness={spiciness}: 第{batch}次 +{added_count}/{len(questions)} (当前 {current_count})")
                if no_progress >= 3:
                    logger.warning(f"[通用题库] {qtype} spiciness={spiciness}: 连续 {no_progress} 批入库 0 题，停止补充避免死循环")
                    break
            else:
                break


def _refill_generic_pool_safe(group_id: int, spiciness: int, question_type: str) -> None:
    """通用题库补充的线程安全包装器，完成后释放防重入锁"""
    count_str = "真心话" if question_type == "truth" else "大冒险"
    try:
        logger.info(f"[通用题库] 开始后台补充 {count_str} (spiciness={spiciness})")
        _refill_generic_pool(group_id, spiciness)
    except Exception as e:
        logger.error(f"[通用题库] 后台补充异常: {e}")
    finally:
        _GENERIC_REFILL_LOCKS.pop(spiciness, None)
        logger.info(f"[通用题库] 后台补充完成 {count_str} (spiciness={spiciness})")


def _refill_generic_pool_all_safe(group_id: int, spiciness: int) -> None:
    """通用题库补充（truth+dare 全类型）的线程安全包装器，防重入锁粒度 = spiciness。

    供 _periodic_pool_refill 使用：一次线程补齐两个类型，锁键与
    _get_or_pop_question 的 _refill_generic_pool_safe 保持一致，避免同档位双线程并发补充。
    """
    try:
        logger.info(f"[通用题库] 开始后台补充全部类型 (spiciness={spiciness})")
        _refill_generic_pool(group_id, spiciness)
    except Exception as e:
        logger.error(f"[通用题库] 后台补充异常: {e}")
    finally:
        _GENERIC_REFILL_LOCKS.pop(spiciness, None)
        logger.info(f"[通用题库] 后台补充完成全部类型 (spiciness={spiciness})")


def _refill_pool(user_id: int, group_id: int, nickname: str, profile_text: str, gender: str | None = None) -> None:
    """为用户补充所有色度档位的题目池（内部调用 _ensure_player_pool 复用锁逻辑）"""
    for spiciness in _QUESTION_SPICINESS_LEVELS:
        _ensure_player_pool(user_id, group_id, nickname, profile_text, spiciness, gender)


def _get_persona_nickname(user_id: int, group_id: int) -> tuple[str | None, str | None, str | None]:
    """
    获取用户昵称、人设文本和性别。
    返回 (nickname, persona_text, gender) 或 (None, None, None)。
    """
    try:
        from core.persona import get_active_persona, persona_to_text
        active_persona = get_active_persona(user_id, group_id)
        persona_text = persona_to_text(active_persona) if active_persona else ""
        nickname = None
        gender = None
        if active_persona:
            nickname = active_persona.get("nickname") or f"用户{user_id}"
            gender = active_persona.get("gender")
        return nickname, persona_text, gender
    except Exception as e:
        logger.warning(f"[获取人设] 失败: {e}")
        return None, "", None


def _get_all_players_to_refill(group_id: int) -> list[tuple[int, str, str | None]]:
    """
    获取所有有答题记录的玩家列表。
    返回 [(user_id, nickname, profile_text), ...]（gender 不需要，定时补充不需要性别）
    """
    try:
        with _get_db(_AUTO_QUESTIONS_DB) as conn:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM auto_questions WHERE group_id = ?",
                (group_id,),
            ).fetchall()
    except Exception:
        return []

    players = []
    for row in rows:
        uid = row[0]
        nickname, persona_text, _ = _get_persona_nickname(uid, group_id)
        players.append((uid, nickname or f"用户{uid}", persona_text))
    return players


def _get_all_group_ids() -> list[int]:
    """获取所有有答题记录的群 ID 列表"""
    try:
        with _get_db(_AUTO_QUESTIONS_DB) as conn:
            rows = conn.execute(
                "SELECT DISTINCT group_id FROM auto_questions WHERE group_id > 0",
            ).fetchall()
        return [row[0] for row in rows]
    except Exception:
        return []


def _periodic_pool_refill(group_id: int) -> None:
    """定期为群内所有玩家补充题目池（无锁，与其他 LLM 调用并行）"""
    players = _get_all_players_to_refill(group_id)
    for uid, nickname, profile_text in players:
        if profile_text:
            # BUG 修复（2026-08-03）：_get_persona_nickname 返回 (nickname, persona_text, gender)，
            # 原解包 gender, _, _ 取到的是 nickname。正确解包第三个元素。
            _, _, gender = _get_persona_nickname(uid, group_id)
            _refill_pool(uid, group_id, nickname, profile_text, gender)
        else:
            # M4 修复：原实现直接调 _refill_generic_pool（绕过防重入锁），
            # 与 _get_or_pop_question 的 _refill_generic_pool_safe 线程并发时
            # 同档位双线程重复补充（LLM 调用翻倍、排队加剧）。统一走带锁包装。
            for spiciness in _QUESTION_SPICINESS_LEVELS:
                if _GENERIC_REFILL_LOCKS.get(spiciness):
                    continue
                _GENERIC_REFILL_LOCKS[spiciness] = True
                threading.Thread(
                    target=_refill_generic_pool_all_safe,
                    args=(group_id, spiciness),
                    daemon=True,
                    name=f"bg-refill-generic-{spiciness}",
                ).start()


def _background_refill_all(group_id: int) -> None:
    """后台补充所有群的题目池"""
    try:
        groups = [group_id] if group_id else _get_all_group_ids()
        for gid in groups:
            _periodic_pool_refill(gid)
    except Exception as e:
        logger.warning(f"[后台补充] 异常: {e}")


# ============ 自动出题 ============

def _get_or_pop_question(user_id: int, group_id: int, question_type: str,
                          profile_text: str, spiciness: int, nickname: str, gender: str | None = None) -> str:
    """
    优先从题目池中取，池空则实时生成。
    返回题目字符串。
    """
    # 1. 优先从人设池取（本群）
    question = _pop_question_from_pool(user_id, group_id, question_type, spiciness)
    if question:
        return question

    # 1b. 本群池空，回退到该用户的其他群
    question = _pop_cross_group_question(user_id, group_id, question_type, spiciness)
    if question:
        return question

    # 2. 从通用池取
    question = _pop_generic_question_from_pool(question_type, spiciness)
    if question:
        return question

    # 2b. 通用池空了，启动后台补充（非阻塞，带防重入锁）
    if not _GENERIC_REFILL_LOCKS.get(spiciness):
        _GENERIC_REFILL_LOCKS[spiciness] = True
        threading.Thread(
            target=_refill_generic_pool_safe,
            args=(group_id, spiciness, question_type),
            daemon=True,
        ).start()

    # 3. 实时生成（降级方案；提示词走共用块，题型规则 2026-08-21 统一为详细版）
    history = _get_auto_history(user_id, group_id)
    count_str = "真心话" if question_type == "truth" else "大冒险"

    # 色度分级说明（每档独立提示词，走提示词配置）
    _sp = max(0, min(6, int(spiciness)))
    spice_note = _tdp.render_prompt(f"spice_note_{_sp}")
    spice_detail = _tdp.render_prompt(f"spice_detail_{_sp}")
    spice_examples = _tdp.render_prompt(f"spice_examples_{_sp}")

    # 题型规则（2026-08-21 统一为详细版，三处共用同一份可定制提示词）
    type_rule = _tdp.render_prompt("truth_rules" if question_type == "truth" else "dare_rules")

    gender_note = ""
    if gender and gender not in ("未知", ""):
        gender_note = f"对方性别：{gender}"

    hist_n = _td_prompt_history()
    history_str = "\n".join(f"- {q}" for q in history[:hist_n]) if history else "（暂无）"

    system_prompt = f"""{_tdp.render_prompt("opening")}

你要为 {nickname} 生成 2 道{count_str}题目。

{type_rule}

{_tdp.render_prompt("common_rules")}
{_tdp.render_prompt("persona_guidance")}
{_tdp.render_prompt("dedup_rules", {"history_label": "已问过的题目"})}

{_tdp.render_prompt("image_rules")}

**色度分级标准**：
{spice_detail}

{spice_examples}

用户人设：{profile_text if profile_text else "暂无人设信息"}
已问过的题目（避免重复）：
{history_str}

---
{spice_note}
{gender_note}
{_tdp.render_prompt("output_discipline")}

{_tdp.render_prompt("output_format", {"count": 2})}
"""

    # LLM 参数/超时来自 config.yaml truth_dare.llm.live（GUI 可配）
    valid = _call_llm_questions(system_prompt, question_type, 2, stage="live")
    if valid:
        # 入库以备后用（人设文本截断走配置）
        extra = valid[1:]
        if extra:
            _add_questions_to_pool(user_id, group_id, extra, question_type, profile_text[:_td_persona_max_chars()], spiciness, "persona")
        logger.info(f"[现场出题] 为 @{nickname} 生成{count_str} (spiciness={spiciness})")
        return valid[0]
    logger.warning(f"[现场出题] 重试后仍无有效题目，降级使用默认{count_str}")
    # 兜底题走提示词配置（default_truth/default_dare，每行一道）；空则回退硬编码
    default_truth = _tdp.render_fallback_questions("default_truth") or [
        "你最近一次哭是因为什么？", "你最尴尬的一次经历是什么？", "你有没有偷偷关注过群里的某人？"]
    default_dare = _tdp.render_fallback_questions("default_dare") or [
        "发一张手机里最新的照片到群里", "改成群昵称「我是社死王」并保持 5 分钟", "连麦群里的一个人，说 3 句土味情话"]
    if question_type == "truth":
        return random.choice(default_truth)
    return random.choice(default_dare)
def handle_spiciness(game: dict, text: str) -> str:
    """
    处理 /色色程度 命令。
    需要传入 TD 游戏状态 game。
    """
    parts = text.strip().split()
    if len(parts) >= 1:
        try:
            level = int(parts[0])
            level = max(0, min(6, level))
            game["spiciness"] = level
            spiciness_desc = {
                0: "纯清水/日常破冰", 1: "轻微暧昧/朋友间可聊", 2: "轻度八卦/带点私人",
                3: "中等尺度/有点害羞", 4: "偏大胆/触及隐私",
                5: "深水私密/过程细节（短答）", 6: "深渊私密/量化逼问+现场感（短答）",
            }
            reply = f"🌶️ 色色程度已调整为 {level}（{spiciness_desc.get(level, '自定义')}）\n💡 AI 出题将根据这个尺度调整"
            # 6 级风险提示（2026-08-14：露骨内容可能触发 QQ 风控折叠/禁言）
            if level >= 6:
                reply += "\n\n🚨 6 级为最高尺度：露骨问答可能被 QQ 折叠/禁言，请玩家注意回答分寸，风险自担"
            return reply
        except ValueError:
            return "🤔 请输入 0-6 的数字"

    # 无参数：显示当前设置（未设置过色度的游戏按默认档位显示）
    current = game.get("spiciness", _td_default_spiciness())
    spiciness_desc = {
        0: "纯清水/日常破冰", 1: "轻微暧昧/朋友间可聊", 2: "轻度八卦/带点私人",
        3: "中等尺度/有点害羞", 4: "偏大胆/触及隐私",
        5: "深水私密/过程细节（短答）", 6: "深渊私密/量化逼问+现场感（短答）",
    }
    return (
        f"🌶️ 当前色色程度：{current}（{spiciness_desc.get(current, '自定义')}）\n\n"
        f"色度档位说明：\n"
        f"0 - 纯清水/日常破冰\n"
        f"1 - 轻微暧昧/朋友间可聊\n"
        f"2 - 轻度八卦/带点私人\n"
        f"3 - 中等尺度/有点害羞\n"
        f"4 - 偏大胆/触及隐私\n"
        f"5 - 深水私密（可问过程细节，回答限1-2句）\n"
        f"6 - 深渊私密（量化逼问/现场感，回答限1-2句，风控风险高）\n\n"
        f"💡 发送「/色色程度 [0-6]」调整 AI 出题尺度（仅管理员可修改）"
    )


# ============ 题库加载 ============

_DEFAULT_TRUTH = [
    "你最近一次哭是因为什么？", "你最尴尬的一次经历是什么？",
    "你有没有偷偷关注过群里的某人？", "你最害怕什么？",
    "你有没有做过什么至今不敢告诉别人的事？",
]

_DEFAULT_DARE = [
    "发一张手机里最新的照片到群里", "改成群昵称「我是社死王」并保持 5 分钟",
    "连麦群里的一个人，说 3 句土味情话", "发一段自己唱的歌到群里",
]


def _load_questions(filepath: str, default: list[str]) -> list[str]:
    """从文件加载题库，文件不存在则使用默认值"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            questions = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            return questions if questions else default
    except FileNotFoundError:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(default))
        return default


def _append_question(filepath: str, questions: list[str], content: str) -> None:
    """追加题目到文件和内存列表"""
    questions.append(content)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(content + "\n")


# ============ 题目加载（全局题库） ============

TRUTH_QUESTIONS = _load_questions(TRUTH_FILE, _DEFAULT_TRUTH)
DARE_QUESTIONS = _load_questions(DARE_FILE, _DEFAULT_DARE)


def reload_question_bank() -> dict:
    """重载真心话/大冒险固定题库（GUI「游戏管理」改文件后热重载）。

    ⚠️ 必须**原地改**列表（clear+extend）：games/entertainment.py 用
    `from .question_pool import TRUTH_QUESTIONS` 按值绑定，重绑新对象
    不会同步到调用方。
    """
    truth = _load_questions(TRUTH_FILE, _DEFAULT_TRUTH)
    dare = _load_questions(DARE_FILE, _DEFAULT_DARE)
    TRUTH_QUESTIONS.clear()
    TRUTH_QUESTIONS.extend(truth)
    DARE_QUESTIONS.clear()
    DARE_QUESTIONS.extend(dare)
    return {"truth": len(TRUTH_QUESTIONS), "dare": len(DARE_QUESTIONS)}


# ============ GUI 题库管理：LLM 重新生成（2026-08-21） ============
# GUI「🤖 LLM 重新生成」入口：后台线程调用现有批量生成函数，
# 按既有补充策略把指定 (题库, 题型, 色度档) 补到阈值（入库前查重，防重复入库）。
# 状态经 get_regen_status() 轮询（GUI 侧显示"生成中/完成 +N 道/失败"）。

_REGEN_STATUS: dict[tuple, dict] = {}
_REGEN_LOCKS: dict[tuple, bool] = {}


def _regen_count(user_id: int, group_id: int, question_type: str,
                 spiciness: int, source: str) -> int:
    """当前该 (题库, 题型, 色度档) 的总题量（已用+未用，与补充阈值口径一致）。"""
    with _get_db(_AUTO_QUESTIONS_DB) as conn:
        if source == "generic":
            row = conn.execute(
                "SELECT COUNT(*) FROM auto_question_pool WHERE source='generic' AND question_type=? AND spiciness=?",
                (question_type, spiciness)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM auto_question_pool WHERE source='persona' AND user_id=? AND group_id=? AND question_type=? AND spiciness=?",
                (user_id, group_id, question_type, spiciness)).fetchone()
    return int(row[0]) if row else 0


def start_regen(user_id: int, group_id: int, question_type: str,
                spiciness: int, source: str = "persona") -> dict:
    """启动 LLM 重新生成（后台线程）。已运行则拒绝。
    source='generic' 补到 _QUESTION_GENERIC_THRESHOLD（每批 15 道）；
    source='persona' 补到 _QUESTION_POOL_THRESHOLD（每批 10 道）。
    """
    import threading

    if question_type not in ("truth", "dare"):
        return {"ok": False, "error": f"未知题型: {question_type}"}
    spiciness = max(0, min(6, int(spiciness)))
    key = (source, group_id, user_id, question_type, spiciness)
    if _REGEN_LOCKS.get(key):
        return {"ok": False, "error": "该档位已在生成中，请稍候"}

    _REGEN_LOCKS[key] = True
    _REGEN_STATUS[key] = {"status": "running", "added": 0, "error": "",
                          "started_at": time.time()}

    def _worker():
        added_total = 0
        threshold = _td_generic_threshold() if source == "generic" else _td_persona_threshold()
        batch_size = _td_generic_batch() if source == "generic" else _td_persona_batch()
        max_chars = _td_persona_max_chars()
        try:
            if source == "generic":
                while _regen_count(0, 0, question_type, spiciness, "generic") < threshold:
                    qs = _batch_generate_generic_questions(question_type, spiciness, count=batch_size)
                    qs = [q for q in qs if q and q.strip()]
                    if not qs:
                        break
                    added_total += _add_questions_to_pool(
                        0, 0, qs, question_type, spiciness=spiciness, source="generic")
            else:
                if _PERSONAL_REFILL_LOCKS.get((user_id, group_id, spiciness)):
                    raise RuntimeError("bot 正在后台补充该玩家题库，请稍后再试")
                nickname, persona_text, gender = _get_persona_nickname(user_id, group_id)
                if not persona_text or not persona_text.strip():
                    raise RuntimeError("该玩家没有人设数据，无法生成定制题目（请先在人设画像页生成）")
                while _regen_count(user_id, group_id, question_type, spiciness, "persona") < threshold:
                    history = _get_pool_used_history(user_id, group_id, question_type)
                    qs = _batch_generate_questions(
                        user_id, nickname or str(user_id), persona_text, history,
                        spiciness, question_type, group_id, count=batch_size, gender=gender)
                    qs = [q for q in qs if q and q.strip()]
                    if not qs:
                        break
                    history.extend(qs)
                    added_total += _add_questions_to_pool(
                        user_id, group_id, qs, question_type,
                        profile_snapshot=persona_text[:max_chars], spiciness=spiciness, source="persona")
            _REGEN_STATUS[key] = {"status": "done", "added": added_total,
                                  "error": "", "started_at": _REGEN_STATUS.get(key, {}).get("started_at", 0)}
            logger.info(f"[题库GUI] LLM 重新生成完成: {source} uid={user_id} gid={group_id} {question_type} 档{spiciness} +{added_total}")
        except Exception as e:
            logger.exception(f"[题库GUI] LLM 重新生成失败: {e}")
            st = _REGEN_STATUS.get(key, {})
            _REGEN_STATUS[key] = {"status": "error", "added": st.get("added", 0),
                                  "error": str(e)[:300], "started_at": st.get("started_at", 0)}
        finally:
            _REGEN_LOCKS[key] = False

    t = threading.Thread(target=_worker, daemon=True, name=f"gui-regen-{question_type}-{spiciness}")
    t.start()
    return {"ok": True, "message": "已启动 LLM 生成（分钟级，可继续操作，稍后刷新）"}


def get_regen_status() -> list[dict]:
    """所有生成任务状态（GUI 轮询用）。"""
    out = []
    for (source, group_id, user_id, question_type, spiciness), st in _REGEN_STATUS.items():
        out.append({
            "source": source, "group_id": group_id, "user_id": user_id,
            "question_type": question_type, "spiciness": spiciness,
            **st,
        })
    return out
