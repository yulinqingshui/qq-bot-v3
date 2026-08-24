# ============================================================
#  router.py — 消息路由分发
#  从 bot.py 提取：handle_message + 所有路由辅助函数
# ============================================================

import asyncio
from .task_registry import TASK_REGISTRY
import json
import re
import time
import random  # 赛博模仿 1% 概率（2026-08-13）
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed
import logging

from asyncio import Queue, Semaphore
from .config import CONFIG
from .database import (
    get_db, get_persona_db, save_message,
    _session_key, set_cooldown,
    is_blocked, block_user, unblock_user, list_blocked,
    is_admin,
    is_on_cooldown, is_session_expired, reset_session,
    get_history,
    get_bot_favorability, update_bot_favorability,
    save_analysis_batch, save_query_batch,
)
from .sender import send_reply, send_reply_sync, send_reply_global, get_bot_uin, send_segments
from .llm import call_llm, _rp_llm_call, llm_enabled, MAX_TOKENS_LONG, MAX_TOKENS_SHORT
from .persona import (
    get_personality, save_personality,
    get_user_profile, save_user_profile, get_all_profiles, find_user_by_nickname,
    find_persona_by_nickname, find_profile_by_nickname,
    get_active_persona, get_persona_display, save_persona,
    set_temporary_persona, reset_temporary_persona,
    persona_to_text, PERSONA_SECTIONS,
    _enqueue_profile_update,
    _get_persona_diff, _parse_persona_json, get_same_persona_chat_context,
    handle_update_profile, handle_update_all_profiles,
    handle_update_persona, _handle_update_all_personas,
    handle_update_profile_and_persona, handle_update_all_profiles_and_personas,
    get_group_personas, get_group_personas_with_profiles,
    BATCH_CHARS,
    _normalize_u_refs,
    _hierarchical_merge_by_len,
    _MAX_LLM_RETRIES,
    _call_llm_net,
    LLMNetworkExhausted,
)
from .qa_prompts import (
    render_prompt, qa_params, qa_llm, qa_llm_scope, thinking_kwargs,
)
import games.cosplay_search as cosplay_search
import games.image_gen as image_gen
from .archive import (
    archive_message, archive_image, archive_voice, archive_recall,
    _archive_recall_images, _download_image_sync,
    archive_forward, archive_video, extract_forward_ids, extract_video_urls,
    derive_msg_kind,
)
import httpx
from .scheduler import handle_summary, handle_evaluation, chunk_messages_by_token
import games.entertainment as entertainment
import games.game_spy as game_spy
import games.group_vote as group_vote
import games.pun_game as pun_game
import games.guess_wife as guess_wife
import games.turtle_soup as turtle_soup
import games.group_roleplay as group_roleplay
from . import help_menu

logger = logging.getLogger("qq-bot")


async def _safe_task(coro):
    """BUG4修复：fire-and-forget 任务的统一异常捕获包装。
    防止未处理异常被 asyncio 静默吞掉，改为记录到日志。
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise  # 任务取消直接抛出，不吞
    except Exception as e:
        coro_name = getattr(coro, '_name', repr(coro))
        logger.error(f"后台任务异常: {coro_name} -> {type(e).__name__}: {e}", exc_info=True)


# ---- 命令执行 FIFO 锁（确保 /查询 /分析 /总结 /评选 等按到达顺序串行）----
_command_lock: Optional[asyncio.Lock] = None

def _report_period_label(rec_date: str) -> str:
    """报告记录的场次标签：'2026-08-03-AM' → 上午，'2026-08-03-PM' → 下午，旧格式 → 全天。"""
    if rec_date.endswith("-AM"):
        return "上午"
    if rec_date.endswith("-PM"):
        return "下午"
    return "全天"


def _query_report_records(group_id: int, table_fn, date_str: str) -> list[dict]:
    """查询指定日期的报告记录（兼容半日场次：YYYY-MM-DD / YYYY-MM-DD-AM / YYYY-MM-DD-PM）。"""
    records = []
    for d in (date_str, f"{date_str}-AM", f"{date_str}-PM"):
        rec = table_fn(group_id, d)
        if rec:
            records.append(rec)
    return records

def _get_command_lock() -> asyncio.Lock:
    """获取命令执行锁，首次调用时懒初始化"""
    global _command_lock
    if _command_lock is None:
        _command_lock = asyncio.Lock()
    return _command_lock


async def _safe_command(coro, cmd_name: str = "", task_key: str = None):
    """
    后台命令的统一包装器：_safe_task + 命令 FIFO 锁。
    确保多个并发指令按到达顺序执行，不会出现结果颠倒。

    行为：
    - 所有通过 _safe_command 提交的命令在 _command_lock 下串行
    - 先到达的命令先获得锁，即使它的 DB 查询比后到的命令慢
    - 一个命令完全执行结束后才释放锁给下一个
    """
    lock = _get_command_lock()
    logger.info(f"🔒 等待命令锁: {cmd_name}")
    if task_key is not None:
        TASK_REGISTRY.set_status(task_key, "queued")
    async with lock:
        if task_key is not None:
            # 2026-08-22 暂停门：获锁后 queued→running 前等放行（暂停时命令锁
            # 被占住 → 整个群指令 FIFO 暂停；不打断持锁中任务的执行）
            await TASK_REGISTRY.wait_if_paused()
            TASK_REGISTRY.set_status(task_key, "running")
        logger.info(f"🔓 获得命令锁: {cmd_name}，开始执行")
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            coro_name = getattr(coro, '_name', repr(coro))
            logger.error(f"命令任务异常 [{cmd_name}]: {coro_name} -> {type(e).__name__}: {e}", exc_info=True)
        finally:
            if task_key is not None:
                TASK_REGISTRY.finish(task_key)
            logger.info(f"✅ 命令执行完毕: {cmd_name}")





# 猜拳值 → 中文（ArrayMessage 与 CQCode 解析器共享，保持一致）
_RPS_MAP = {"0": "剪刀", "1": "石头", "2": "布"}


def parse_array_message(message_segments: list[dict], bot_qq: str) -> tuple[bool, str, Optional[int]]:
    """
    解析 ArrayMessage 格式的消息。

    与 parse_cqcode_message 保持一致的标记格式：
    - 表情 → [表情{id}]
    - 图片 → [图片]
    - 语音 → [语音]
    - 分享 → [分享]
    - 文件 → [文件]
    - 音乐 → [音乐]
    - 骰子 → [骰子{点数}]
    - 猜拳 → [猜拳{剪刀/石头/布}]
    - @ 和引用 → 清理

    返回 (is_at, clean_text, reply_id)
      - is_at: 是否 @了机器人
      - clean_text: 去除 @和引用后的纯文本内容
      - reply_id: 如果用户引用了某条消息，返回该消息的 ID（用于引用回复）
    """
    is_at = False
    text_parts: list[str] = []
    reply_id: Optional[int] = None

    # 类型→标记映射表（未知类型返回 None 表示跳过）
    MARKER_MAP = {
        "image": "[图片]",
        "voice": "[语音]",
        "record": "[语音]",
        "share": "[分享]",
        "file": "[文件]",
        "music": "[音乐]",
        "contact": "[好友请求]",
        "location": "[位置]",
        "poke": "[戳一戳]",
        "video": "[视频]",
    }

    for seg in message_segments:
        seg_type = seg.get("type", "")
        data = seg.get("data", {})

        if seg_type == "at":
            at_qq = str(data.get("qq", ""))
            if at_qq == str(bot_qq):
                is_at = True
                # 2026-08-08 修复：保留 @bot 标记（存档/画像提取需要识别"对 bot 说"，
                # 否则 LLM 会把 @bot 的发言误归属给上下文中的群友——历史归属事故根因）
                text_parts.append("@机器人")
            else:
                # 问题 3 修复：保留非 Bot @ 段，追加为 @{qq} 格式
                # 这样投票解析时可以通过 QQ 号匹配到玩家
                text_parts.append(f"@{at_qq}")
        elif seg_type == "text":
            text = data.get("text", "").strip()
            if text:
                # 检查文本中是否包含 @机器人 或 @QQ号
                if not is_at and f"@{bot_qq}" in text:
                    is_at = True
                # NapCat 有时会把 @昵称 放在 text 中而非 at 段
                # 仅当文本以 @bot 开头时才视为 @bot（避免 @其他成员时误触发）
                # 同时兼容 @机器人（部分群可能名片不同）
                if not is_at and (text.startswith("@bot") or text.startswith("@机器人")):
                    is_at = True
                text_parts.append(text)
        elif seg_type == "reply":
            rid = data.get("id")
            # 2026-08-24 弱引用兼容（NapCat 查不到被引用对象时上报 id=0 弱引用）：
            # if rid: 会把 "0" 当 falsy 过滤 → reply_id=None → 引用跳过逻辑失效。
            # 改用 is not None：id="0" 也计入（int("0")=0），None/非法值保持 None。
            if rid is not None:
                try:
                    reply_id = int(rid)
                except ValueError:
                    pass
        elif seg_type == "face":
            # 表情保留标记
            text_parts.append(f"[表情{data.get('id', '')}]")
        elif seg_type == "dice":
            # 骰子保留标记
            text_parts.append(f"[骰子{data.get('value', '?')}]")
        elif seg_type == "rps":
            # 猜拳保留标记（与 CQCode 解析器共享同一映射表）
            value = str(data.get("value", "?"))
            text_parts.append(f"[猜拳{_RPS_MAP.get(value, '?')}]")
        elif seg_type == "forward":
            # 聊天记录转发：保留标记 + id（内容由 archive_forward 异步拉取入库）
            fwd_id = data.get("id", "")
            text_parts.append(f"[聊天记录转发{fwd_id}]")
        elif seg_type == "json":
            # 小程序/卡片：尝试提取可读标题
            try:
                import json as _json
                raw = data.get("data", "")
                parsed = _json.loads(raw) if raw else {}
                prompt = parsed.get("prompt", "") or parsed.get("meta", {}).get("detail_1", "")
                if prompt:
                    text_parts.append(f"[卡片]{prompt}")
                else:
                    text_parts.append("[卡片]")
            except Exception:
                text_parts.append("[卡片]")
        elif seg_type in MARKER_MAP:
            # 其他已知类型使用统一标记
            text_parts.append(MARKER_MAP[seg_type])

    clean_text = " ".join(text_parts).strip()
    return is_at, clean_text, reply_id


def extract_image_urls(message_segments: list[dict]) -> list[str]:
    """
    从 ArrayMessage 中提取所有图片 URL。
    """
    urls = []
    if isinstance(message_segments, list):
        for seg in message_segments:
            if seg.get("type") == "image":
                url = seg.get("data", {}).get("url", "")
                if url:
                    urls.append(url)
    return urls


def extract_voice_urls(message_segments: list[dict]) -> list[str]:
    """
    从 ArrayMessage 中提取所有语音 URL。
    支持 type=voice 和 type=record。
    """
    urls = []
    if isinstance(message_segments, list):
        for seg in message_segments:
            if seg.get("type") in ("voice", "record"):
                url = seg.get("data", {}).get("url", "")
                if url:
                    urls.append(url)
    return urls








def parse_cqcode_message(raw_message: str, bot_qq: str) -> tuple[bool, str, Optional[int]]:
    """
    兼容 CQCode 字符串格式（兜底）。
    与 parse_array_message 保持一致的标记格式：
    - 表情 → [表情{id}]
    - 图片 → [图片]
    - 语音 → [语音]
    - 分享 → [分享]
    - 文件 → [文件]
    - @ 和引用 → 清理
    """
    is_at = f"[CQ:at,qq={bot_qq}]" in raw_message

    # 提取引用 ID
    reply_id: Optional[int] = None
    reply_match = re.search(r"\[CQ:reply,id=(\d+)", raw_message)
    if reply_match:
        reply_id = int(reply_match.group(1))

    # 清理 @和 reply 标签
    clean = raw_message
    clean = re.sub(r"\[CQ:at,qq=\d+\]", "", clean)
    clean = re.sub(r"\[CQ:reply,id=\d+.*?\]", "", clean)
    # 表情保留标记（与 ArrayMessage 一致）
    clean = re.sub(r"\[CQ:face,id=(\d+)\]", r"[表情\1]", clean)
    # 图片保留标记
    clean = re.sub(r"\[CQ:image,.*?\]", "[图片]", clean)
    # 语音保留标记（支持 record 和 voice 两种 CQ 类型）
    clean = re.sub(r"\[CQ:record,.*?\]", "[语音]", clean)
    clean = re.sub(r"\[CQ:voice,.*?\]", "[语音]", clean)
    # 分享链接保留标记
    clean = re.sub(r"\[CQ:share,.*?\]", "[分享]", clean)
    # 文件保留标记
    clean = re.sub(r"\[CQ:file,.*?\]", "[文件]", clean)
    # 音乐保留标记
    clean = re.sub(r"\[CQ:music,.*?\]", "[音乐]", clean)
    # 骰子保留标记（与 ArrayMessage 一致）
    clean = re.sub(r"\[CQ:dice,result=(\d+)\]", r"[骰子\1]", clean)
    # 猜拳保留标记（与 ArrayMessage 一致，共用 _RPS_MAP）
    clean = re.sub(r"\[CQ:rps,result=(\d+)\]", lambda m: f"[猜拳{_RPS_MAP.get(m.group(1), '?')}]", clean)
    # 联系人/好友请求保留标记
    clean = re.sub(r"\[CQ:contact,.*?\]", "[好友请求]", clean)
    # 位置保留标记
    clean = re.sub(r"\[CQ:location,.*?\]", "[位置]", clean)
    # 戳一戳保留标记
    clean = re.sub(r"\[CQ:poke,.*?\]", "[戳一戳]", clean)
    # 聊天记录转发保留标记（内容由 archive_forward 异步拉取入库）
    clean = re.sub(r"\[CQ:forward,id=(\d+)\]", r"[聊天记录转发\1]", clean)
    # 视频保留标记
    clean = re.sub(r"\[CQ:video,.*?\]", "[视频]", clean)
    # json 卡片保留标记（尝试提取标题）
    def _json_marker(m):
        try:
            import json as _json
            raw = m.group(1).replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
            parsed = _json.loads(raw) if raw else {}
            prompt = parsed.get("prompt", "") or parsed.get("meta", {}).get("detail_1", "")
            if prompt:
                return f"[卡片]{prompt}"
        except Exception:
            pass
        return "[卡片]"
    clean = re.sub(r"\[CQ:json,data=(.*?)\]", _json_marker, clean, flags=re.DOTALL)
    # 清理任何剩余的 CQ 码（兜底）
    clean = re.sub(r"\[CQ:\w+,.*?\]", "", clean)
    clean = clean.strip()

    return is_at, clean, reply_id


# ============================================================
#  统一清理 thinking 标签（支持多种格式变体）
# ============================================================


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
            arrow_match = re.search(r"`([^`]+)`\s*-.*?`([^`]+)`", snippet)
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

# 串行信号量：同一时间只允许 1 个 LLM 请求（定义在 llm.py）
from .llm import _llm_lock  # noqa: F401
# 画像更新专属锁：确保一个用户画像任务（Map+Reduce 全链路）完成前，
# 另一个画像任务不会开始，避免交叉执行导致数据混乱
# 从 persona 模块导入（单一信源，避免重复定义导致互斥失效）
from .persona import (
    _profile_update_lock,
    _profile_task_queue,
    _profile_worker_started,
    _enqueue_profile_update,
)




async def _safe_send_reply(websocket, message_type, target_id, text, user_id=None, reply_id=None):
    """发送回复，捕获 ConnectionClosed 避免后台任务崩溃"""
    try:
        await send_reply(websocket, message_type, target_id, text, user_id, reply_id)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，消息未发送")
        raise


async def _handle_roleplay_opening(
    websocket, message_type, group_id, user_id, reply_id,
):
    """处理 /开演 命令 — 生成开场旁白"""
    try:
        room = group_roleplay.get_active_room(group_id)
        if not room:
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 没有活跃的游戏房间", user_id, reply_id)
            return

        if room['state'] == 'playing':
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 游戏已在进行中，请等待轮次", user_id, reply_id)
            return

        # L8 修复：报名人数校验——原异步路径（本函数）无任何人数检查，
        # 0 人或 1 人也能开演（group_roleplay._handle_start_play 有校验但是死代码，
        # 从不被调用）。与报名提示"满 2 人后发送 /开演"保持一致。
        rp_chars = group_roleplay.get_characters(room['room_id'])
        active_chars = [c for c in rp_chars if c.get('active', 1)]
        if len(active_chars) < 2:
            await _safe_send_reply(
                websocket, message_type, group_id,
                f"⚠️ 当前仅 {len(active_chars)} 人报名（角色扮演需要至少 2 人），"
                "请先让大家发送 `/报名 角色名:描述`",
                user_id, reply_id,
            )
            return

        # 先发送占位消息
        await _safe_send_reply(websocket, message_type, group_id, "🎭 旁白正在准备开场...", user_id, reply_id)
        logger.info("🎭 开始生成开场旁白...")

        # 将世界观数据导入数据库（NPC、物品等）
        world_state = room.get('world_state', {})
        if world_state and isinstance(world_state, dict) and world_state.get('initial_npcs'):
            group_roleplay.save_world_to_db(room['room_id'], world_state)
            logger.info(f"📦 已将 {len(world_state['initial_npcs'])} 个 NPC 导入数据库")

        # 更新房间状态为 playing
        group_roleplay.update_room(room['room_id'], state='playing')

        result = await group_roleplay.handle_opening(
            room['room_id'],
            llm_call_func=_rp_llm_call,
            on_reply_func=lambda reply, next_player=None: None,
        )

        reply = result['reply']
        logger.info(f"✅ 开场旁白已生成 ({len(reply)} 字)")
        await _safe_send_reply(websocket, message_type, group_id, reply, user_id, reply_id)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，开场旁白未发送")
    except Exception as e:
        logger.error(f"开场旁白生成失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 开场生成失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


async def _handle_roleplay_continue(
    websocket, message_type, group_id, user_id, reply_id,
):
    """处理 /继续 命令 — 重新提示当前玩家"""
    try:
        room = group_roleplay.get_active_room(group_id)
        if not room:
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 没有活跃的游戏房间", user_id, reply_id)
            return

        if room['state'] != 'playing':
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 游戏尚未开始或已结束", user_id, reply_id)
            return

        turn_info = group_roleplay.get_current_turn_info(room['room_id'])
        if not turn_info:
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 没有活跃的玩家", user_id, reply_id)
            return

        current = turn_info['current_player']

        if current['user_id'] == user_id:
            reply = f"💡 现在轮到你行动，请描述你要做什么。\n\n当前场景：{room.get('scene_description', '未知')}"
        else:
            reply = f"💡 现在轮到 @{current['nickname']}（{current['character_name']}）行动，请耐心等待。"

        await _safe_send_reply(websocket, message_type, group_id, reply, user_id, reply_id)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，消息未发送")
    except Exception as e:
        logger.error(f"继续命令处理失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 处理失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


async def _handle_roleplay_action(
    websocket, message_type, group_id, user_id, action_text, reply_id,
):
    """处理玩家行动 — 生成旁白回复"""
    try:
        room = group_roleplay.get_active_room(group_id)
        if not room:
            return  # 不应该走到这里

        # 检查是否轮到该玩家
        turn_info = group_roleplay.get_current_turn_info(room['room_id'])
        if not turn_info:
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 游戏状态异常", user_id, reply_id)
            return

        current = turn_info['current_player']
        if current['user_id'] != user_id:
            # 不是当前玩家，忽略或提示
            return

        # 先发送占位消息
        await _safe_send_reply(websocket, message_type, group_id, "🤔 旁白正在思考...", user_id, reply_id)
        logger.info(f"🎭 处理玩家行动: {action_text[:50]}...")

        result = await group_roleplay.handle_player_action(
            room['room_id'],
            user_id,
            action_text,
            llm_call_func=_rp_llm_call,
            on_reply_func=lambda reply, next_player=None: None,
            async_summary_func=lambda rid, llm_fn: group_roleplay.generate_summary(rid, llm_fn),
        )

        reply = result['reply']
        logger.info(f"✅ 旁白回复已生成 ({len(reply)} 字)")
        await _safe_send_reply(websocket, message_type, group_id, reply, user_id, reply_id)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，旁白回复未发送")
    except Exception as e:
        logger.error(f"玩家行动处理失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 旁白生成失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


# ============================================================
#  谐音梗游戏处理
# ============================================================
async def _pun_auto_reveal(websocket, message_type, group_id, timeout_seconds, game_start_time):
    """定时器：超时后自动公布谐音梗答案"""
    await asyncio.sleep(timeout_seconds)

    # 校验 start_time，防止旧定时器的 cancel() 未及时生效时误操作新游戏
    current_game = pun_game._get_game(group_id)
    if not current_game or not current_game.get("active"):
        return  # 已经有人答对或手动公布了
    if current_game.get("start_time") != game_start_time:
        logger.info("⏰ 谐音梗定时器醒来，但 start_time 不匹配，跳过公布（旧定时器）")
        return

    # 发送占位消息
    try:
        await _safe_send_reply(websocket, message_type, group_id, "⏰ 题目时间到！正在公布答案...", None, None)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，超时提示未发送")
        return

    reveal = pun_game.reveal_answer(group_id)
    if reveal:
        try:
            await _safe_send_reply(websocket, message_type, group_id, reveal, None, None)
            logger.info(f"⏰ 谐音梗答案自动公布: {pun_game._get_game(group_id)}")
        except ConnectionClosed:
            logger.warning("WebSocket 已断开，答案未发送")
    else:
        # 竞争条件：占位消息发出后、reveal_answer 之前游戏被手动结束
        try:
            await _safe_send_reply(websocket, message_type, group_id,
                "🎉 答案已公布~ 发送 /谐音梗 继续游戏", None, None)
        except ConnectionClosed:
            pass




async def _handle_pun_question(
    websocket, message_type, group_id, user_id, reply_id,
):
    """处理 /谐音梗 命令 — 出题并发送图片"""
    try:
        # 检查是否有题目正在进行（上一题未结束时不接受新题）
        existing_game = pun_game._get_game(group_id)
        if existing_game and existing_game.get("active"):
            remaining = int(pun_game.QUESTION_TIMEOUT_SECONDS - (time.time() - existing_game.get("start_time", time.time())))
            if remaining > 0:
                await _safe_send_reply(websocket, message_type, group_id,
                    f"⏳ 上一题还没结束呢，还剩 {remaining} 秒~\n发送答案或 /答案 直接公布",
                    user_id, reply_id)
                logger.info(f"[DEBUG] /谐音梗: 上一题未结束，拒绝新题（剩余 {remaining}s）")
                return

        # 先取消旧游戏的定时器（防止旧定时器醒来时错误公布新游戏答案）
        old_game = pun_game._get_game(group_id)
        if old_game:
            old_task = old_game.get("timeout_task")
            if old_task and not old_task.done():
                old_task.cancel()
        
        # 出题
        question = pun_game.draw_question(group_id, user_id)
        if not question:
            await _safe_send_reply(websocket, message_type, group_id, "😅 题库暂无可用题目", user_id, reply_id)
            return

        # 发送图片 + 文字提示
        # to_thread：内部为同步 PIL 压缩循环（最多 ~66 次编码），直接调用会冻结事件循环
        segments = await asyncio.to_thread(pun_game.build_question_segments, question)
        logger.info(f"谐音梗消息段: {json.dumps(segments, ensure_ascii=False)}")

        # 方案A（2026-08-23）：统一发送出口（发送门控在 send_segments 内单点判定）
        target_id = group_id if message_type == "group" else user_id
        if reply_id:
            # 引用回复
            segments.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})

        logger.info(f"谐音梗发送前 - WebSocket状态: {websocket.state}")
        data = await send_segments(websocket, message_type, target_id, segments,
                                   echo=f"pun_question_{group_id}_{int(time.time())}")
        if data is not None:
            logger.info(f"谐音梗发送后 - 段数: {len(segments)}")
        logger.info(f"🎯 谐音梗出题: {question['word']}")

        # 启动 2 分钟自动公布答案定时器（传入 start_time 用于校验）
        game = pun_game._get_game(group_id)
        if game:
            task = asyncio.create_task(
                _safe_task(
                    _pun_auto_reveal(websocket, message_type, group_id, pun_game.QUESTION_TIMEOUT_SECONDS, game["start_time"])
                )
            )
            game["timeout_task"] = task
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，谐音梗题目未发送")
    except Exception as e:
        logger.error(f"谐音梗出题失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 出题失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


async def _handle_pun_answer(
    websocket, message_type, group_id, user_id, reply_id, answer_text,
):
    """处理谐音梗答题 — 检查答案并回复"""
    try:
        result = pun_game.check_answer(group_id, answer_text, user_id)
        if result is None:
            return False  # 没有正在进行的谐音梗游戏

        await _safe_send_reply(websocket, message_type, group_id, result, user_id, reply_id)
        logger.info(f"✅ 谐音梗答题回复 ({len(result)} 字)")
        return True  # 已处理
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，答题回复未发送")
        return True
    except Exception as e:
        logger.error(f"谐音梗答题失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 答题处理失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass
        return True


# ============================================================
#  猜老婆游戏处理
# ============================================================
async def _guess_wife_auto_reveal(websocket, message_type, group_id, timeout_seconds, game_start_time):
    """定时器：超时后自动公布猜老婆答案"""
    await asyncio.sleep(timeout_seconds)

    current_game = guess_wife._get_game(group_id)
    if not current_game or not current_game.get("active"):
        return
    if current_game.get("start_time") != game_start_time:
        logger.info("⏰ 猜老婆定时器醒来，但 start_time 不匹配，跳过公布（旧定时器）")
        return

    try:
        await _safe_send_reply(websocket, message_type, group_id, "⏰ 题目时间到！正在公布答案...", None, None)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，超时提示未发送")
        return

    game = guess_wife.reveal_answer(group_id)
    if game:
        try:
            # 构建带完整图片的答案消息段
            # to_thread：完整原图压缩是 PIL 同步循环，直接调用会冻结事件循环
            segments = await asyncio.to_thread(guess_wife.build_answer_reveal_segments, group_id, game)
            guess_wife.end_reveal_game(group_id)

            # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
            target_id = group_id if message_type == "group" else (game.get("creator_id", 0) or 0)
            data = await send_segments(websocket, message_type, target_id, segments)
            if data is not None:
                logger.info("⏰ 猜老婆答案自动公布（含完整图片）")
        except ConnectionClosed:
            logger.warning("WebSocket 已断开，答案未发送")
            guess_wife.end_reveal_game(group_id)


async def _handle_member_change_notify(websocket, msg: dict, notice_type: str) -> None:
    """入群/退群私聊通知（2026-08-10）。

    群开关 enable_member_notify 开启时，向 admin_users 的所有管理员
    私聊发送入群（group_increase）/退群（group_decrease）通知。
    - group_increase: sub_type=approve/invite（同意入群/被邀请）
    - group_decrease: sub_type=leave/kick（主动退群/被踢），被踢时 operator_id=操作者
    """
    try:
        group_id = msg.get("group_id") or 0
        user_id = msg.get("user_id") or 0
        if not group_id or not user_id:
            return

        # 检查群开关
        from .database import get_group_task_flags, list_admins
        flags = get_group_task_flags(group_id)
        if not flags or not flags.get("member_notify"):
            return  # 默认关闭

        # 事件信息
        sub_type = msg.get("sub_type", "")
        if notice_type == "group_increase":
            if sub_type == "approve":
                event_desc = "📥 新成员加入（管理员同意入群）"
            elif sub_type == "invite":
                event_desc = "📥 新成员加入（被邀请进群）"
            else:
                event_desc = "📥 新成员加入"
        else:  # group_decrease
            if sub_type == "kick":
                # operator_id 兼容顶层 + data 嵌套两种上报格式（与撤回处理一致）
                nested = msg.get("data") or {}
                operator = msg.get("operator_id") or nested.get("operator_id") or 0
                event_desc = f"📤 成员被移出群（操作者 QQ：{operator}）"
            elif sub_type == "kick_me":
                event_desc = "📤 机器人被移出群！"
            else:
                event_desc = "📤 成员退群"

        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg.get("time") or time.time()))
        text = (
            f"{event_desc}\n"
            f"群号：{group_id}\n"
            f"用户 QQ：{user_id}\n"
            f"时间：{time_str}"
        )
        logger.info(f"[入群退群通知] {event_desc} 群={group_id} 用户={user_id}，通知 {len(list_admins())} 位管理员")

        # 私聊通知所有管理员
        for admin in list_admins():
            admin_qq = admin.get("user_id")
            if not admin_qq:
                continue
            try:
                await send_reply(websocket, "private", admin_qq, text)
            except Exception as e:
                logger.warning(f"[入群退群通知] 私聊发送给管理员 {admin_qq} 失败: {e}")
    except Exception as e:
        logger.error(f"[入群退群通知] 处理异常: {e}", exc_info=True)


async def _handle_mimic_reply(websocket, message_type: str, target_id: int,
                              user_id: int, target: dict, content: str) -> None:
    """异步执行 /模仿：生成模拟发言并发送（2026-08-12）"""
    try:
        from games.mimic import generate_mimic_reply
        reply = await generate_mimic_reply(target["nickname"], target["user_id"], target_id, content)
        if not reply:
            await send_reply(websocket, message_type, target_id,
                             f"⚠️ 没有足够的「{target['nickname']}」聊天记录来模仿（<30 条有效消息）", user_id, None)
            return
        # 发送：注明代打来源（身份透明——防止群里误会是真人）
        await send_reply(websocket, message_type, target_id,
                         f"🎭 {reply}\n—代打:{target['nickname']}", user_id, None)
        logger.info(f"🎭 模仿发言完成: 模仿={target['nickname']}({target['user_id']}), 由 {user_id} 触发")
    except Exception as e:
        logger.error(f"🎭 模仿发言失败: {e}", exc_info=True)
        await send_reply(websocket, message_type, target_id,
                         f"⚠️ 模仿发言失败：{e}", user_id, None)


async def _handle_recall_bot_message(
    websocket, message_type, group_id, user_id, reply_id,
):
    """处理 /撤回 命令 — 撤回 bot 最近在群里发的一条消息（2026-08-10）。

    流程：
    1. 通过 NapCat HTTP API 拉取群最近消息历史，找到 bot 自己最近一条
    2. 调 delete_msg 撤回
    3. 消息档案按其他撤回消息处理（archive_recall + 图片归档，与群撤回通知一致）
    """
    try:
        if message_type != "group":
            await send_reply(websocket, message_type, group_id,
                             "⚠️ /撤回 仅支持群聊使用", user_id, reply_id)
            return

        bot_qq = int(get_bot_uin() or 0)  # 08-22：从连接派生（CONFIG 兜底）
        napcat_http = CONFIG.get("NAPCAT_HTTP", "http://127.0.0.1:3000")

        # 1. 拉取群最近 30 条消息，找 bot 自己最近一条
        target_msg = None
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{napcat_http}/get_group_msg_history",
                params={"group_id": group_id, "message_seq": 0, "count": 30},
            )
            resp.raise_for_status()
            data = resp.json()
            messages = (data.get("data") or {}).get("messages") or []
            # NapCat 返回顺序为【旧→新】（索引 0 最旧）——必须从后往前找 bot 最近一条
            # （2026-08-10 修复：原实现从头遍历找到的是 30 条内最旧的 bot 消息）
            for m in reversed(messages):
                if int(m.get("user_id") or 0) == bot_qq:
                    target_msg = m
                    break

        if not target_msg:
            await send_reply(websocket, message_type, group_id,
                             "😅 最近 30 条消息里没有找到 bot 发的消息～", user_id, reply_id)
            return

        message_id = int(target_msg.get("message_id") or 0)
        content = target_msg.get("raw_message") or target_msg.get("message") or ""

        # 2. 调 delete_msg 撤回
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{napcat_http}/delete_msg", params={"message_id": message_id})
            resp.raise_for_status()
            result = resp.json()

        if not (result.get("status") == "ok" and result.get("retcode") == 0):
            wording = result.get("wording") or result.get("message") or "撤回失败"
            await send_reply(websocket, message_type, group_id,
                             f"⚠️ 撤回失败：{wording}", user_id, reply_id)
            return

        # 3. 消息档案按其他撤回消息处理（archive_recall + 图片归档）
        archive_recall(message_id, bot_qq, "group", group_id, bot_qq,
                       nickname="机器人", content=str(content)[:200])
        asyncio.create_task(_safe_task(
            _archive_recall_images(message_id, group_id, "group", bot_qq)))

        logger.info(f"✅ /撤回 执行成功: message_id={message_id}, 群={group_id}, 操作人={user_id}")
        await send_reply(websocket, message_type, group_id,
                         "✅ 已撤回 bot 最近一条消息～", user_id, reply_id)
    except Exception as e:
        logger.error(f"/撤回 执行异常: {e}", exc_info=True)
        await send_reply(websocket, message_type, group_id,
                         f"⚠️ 撤回执行异常：{e}", user_id, reply_id)


async def _handle_guess_wife_question(
    websocket, message_type, group_id, user_id, reply_id,
):
    """处理 /猜老婆 命令 — 出题并发送图片"""
    try:
        existing_game = guess_wife._get_game(group_id)
        if existing_game and existing_game.get("active"):
            remaining = int(guess_wife.QUESTION_TIMEOUT_SECONDS - (time.time() - existing_game.get("start_time", time.time())))
            if remaining > 0:
                await _safe_send_reply(websocket, message_type, group_id,
                    f"⏳ 上一题还没结束呢，还剩 {remaining} 秒~\n发送 A-F 来回答或 /答案 直接公布",
                    user_id, reply_id)
                return

        # 取消旧游戏的定时器
        old_game = guess_wife._get_game(group_id)
        if old_game:
            old_task = old_game.get("timeout_task")
            if old_task and not old_task.done():
                old_task.cancel()

        # 出题（to_thread 避免全表扫描同步阻塞事件循环）
        game = await asyncio.to_thread(guess_wife.draw_question, group_id, user_id)
        if not game:
            await _safe_send_reply(websocket, message_type, group_id, "😅 题库暂无可用题目", user_id, reply_id)
            return

        # 发送图片 + 文字选项
        # to_thread：随机裁剪 + 压缩为同步 PIL 循环，直接调用会冻结事件循环
        segments = await asyncio.to_thread(guess_wife.build_question_segments, game)
        logger.info(f"猜老婆消息段: {json.dumps(segments, ensure_ascii=False)}")

        # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
        target_id = group_id if message_type == "group" else user_id
        if reply_id:
            segments.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})

        await send_segments(websocket, message_type, target_id, segments,
                            echo=f"guess_wife_{group_id}_{int(time.time())}")
        logger.info(f"🎯 猜老婆出题: {game['source']} - {game['character']}")

        # 启动自动公布答案定时器
        current_game = guess_wife._get_game(group_id)
        if current_game:
            task = asyncio.create_task(
                _safe_task(
                    _guess_wife_auto_reveal(websocket, message_type, group_id, guess_wife.QUESTION_TIMEOUT_SECONDS, current_game["start_time"])
                )
            )
            current_game["timeout_task"] = task
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，猜老婆题目未发送")
    except Exception as e:
        logger.error(f"猜老婆出题失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 出题失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


async def _handle_guess_wife_answer(
    websocket, message_type, group_id, user_id, reply_id, answer_text,
):
    """处理猜老婆答题 — 检查答案并回复"""
    try:
        # to_thread：check_answer 内部超时/答对分支会同步构建带完整图片的答案段
        # （build_answer_reveal_segments / build_correct_answer_segments，PIL 压缩循环），
        # 直接调用会冻结事件循环
        result = await asyncio.to_thread(guess_wife.check_answer, group_id, answer_text, user_id)
        if result is None:
            return False  # 没有正在进行的猜老婆游戏

        if result.get("type") in ("correct", "timeout") and result.get("segments"):
            # 答对 / 超时公布 — 发送带完整图片的消息段 + 结束游戏
            segments = result["segments"]
            if reply_id:
                segments.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})

            # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
            await send_segments(websocket, message_type, group_id, segments)
            logger.info("✅ 猜老婆答对 — 发送完整图片")
            return True

        # 普通文字回复（答错/超时/已猜过/格式不对）
        await _safe_send_reply(websocket, message_type, group_id, result.get("message", ""), user_id, reply_id)
        logger.info(f"✅ 猜老婆答题回复: {result.get('type')}")
        return True  # 已处理
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，答题回复未发送")
        return True
    except Exception as e:
        logger.error(f"猜老婆答题失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 答题处理失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass
        return True


# ============================================================
#  媒体/游戏命令统一分发（消除 is_at 分支与主分支的重复实现）
# ============================================================
async def _dispatch_priority_commands(
    websocket, message_type: str, target_id: int, user_id: int,
    reply_id: Optional[int], text_for_command: str, group_id: Optional[int],
) -> bool:
    """
    统一处理 /谐音梗 /答案 /猜老婆 /找图 /画图 /描述画图 /修改描述 命令。

    is_at 分支与主分支共用此函数（单一实现）：
    - is_at 分支在游戏被动拦截之前调用（@bot 命令优先，卧底/投票沉浸阶段不被吞）
    - 主分支在管理命令之后调用（非 @bot 也能用）
    target_id 由调用方传入：群聊传 group_id，私聊传 user_id。
    返回 True 表示命令已处理（调用方应 return），False 表示不匹配。
    """
    session_key = _session_key(group_id, user_id)

    # ---- /谐音梗 ----
    if text_for_command == "/谐音梗":
        set_cooldown(session_key)
        try:
            await _handle_pun_question(websocket, message_type, target_id, user_id, reply_id)
        except Exception as e:
            logger.error(f"/谐音梗 同步调用失败: {e}", exc_info=True)
        return True

    # ---- /答案（海龟汤 → 谐音梗 → 猜老婆 优先级链） ----
    if text_for_command == "/答案":
        set_cooldown(session_key)
        # 优先尝试海龟汤揭锅
        soup_reveal = turtle_soup.reveal_answer(target_id)
        if soup_reveal:
            await send_reply(websocket, message_type, target_id, soup_reveal, user_id, reply_id)
            logger.info("✅ 海龟汤汤底已公布")
            return True
        # 再尝试谐音梗答案
        reveal = pun_game.reveal_answer(target_id)
        if reveal:
            await send_reply(websocket, message_type, target_id, reveal, user_id, reply_id)
            logger.info("✅ 谐音梗答案已公布")
            return True
        # 尝试猜老婆的答案公布
        gw_game = guess_wife.reveal_answer(target_id)
        if gw_game:
            segments = await asyncio.to_thread(guess_wife.build_answer_reveal_segments, target_id, gw_game)
            guess_wife.end_reveal_game(target_id)
            if reply_id:
                segments.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})
            # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
            await send_segments(websocket, message_type, target_id, segments)
            logger.info("✅ 猜老婆答案已公布（含完整图片）")
        else:
            await send_reply(websocket, message_type, target_id, "😅 当前没有正在进行的海龟汤、谐音梗或猜老婆游戏", user_id, reply_id)
        return True

    # ---- /猜老婆 ----
    if text_for_command == "/猜老婆":
        set_cooldown(session_key)
        await _handle_guess_wife_question(websocket, message_type, target_id, user_id, reply_id)
        return True

    # ---- /撤回（bot 撤回自己最近发的一条消息，2026-08-10） ----
    if text_for_command == "/撤回":
        set_cooldown(session_key)
        await _handle_recall_bot_message(websocket, message_type, target_id, user_id, reply_id)
        return True

    # ---- /找图（cosplay 图包搜索） ----
    if text_for_command.startswith("/找图 "):
        set_cooldown(session_key)
        query_text = text_for_command[4:].strip()
        if not query_text:
            await send_reply(websocket, message_type, target_id, "⚠️ 用法：/找图 描述（如：银发女仆、圣诞装）", user_id, reply_id)
            return True
        if cosplay_search._is_on_cooldown(user_id):
            segments = cosplay_search.build_cooldown_segments(user_id)
            if segments:
                await send_reply(websocket, message_type, target_id, segments[0]["data"]["text"], user_id, reply_id)
            return True
        cosplay_search._set_cooldown(user_id)
        try:
            # to_thread 避免同步 httpx（最长 300s 超时）阻塞事件循环
            result = await asyncio.to_thread(cosplay_search.search_cosplay, query_text)
            if result:
                # to_thread：内部压缩循环（LANCZOS resize + JPEG 编码最多 ~66 次）为同步 PIL
                segments = await asyncio.to_thread(cosplay_search.build_result_segments, result, query_text)
                if reply_id:
                    segments.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})
                # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
                await send_segments(websocket, message_type, target_id, segments)
                logger.info(f"🔍 找图成功: {query_text}")
            else:
                segments = cosplay_search.build_not_found_segments(query_text)
                await send_reply(websocket, message_type, target_id, segments[0]["data"]["text"], user_id, reply_id)
                logger.info(f"🔍 找图无结果: {query_text}")
        except Exception as e:
            logger.error(f"找图失败: {e}", exc_info=True)
            segments = cosplay_search.build_error_segments()
            await send_reply(websocket, message_type, target_id, segments[0]["data"]["text"], user_id, reply_id)
        return True

    # ---- /画图 ----
    if text_for_command.startswith("/画图 "):
        set_cooldown(session_key)
        draw_prompt = text_for_command[4:].strip()
        if not draw_prompt:
            await send_reply(websocket, message_type, target_id, "⚠️ 用法：/画图 描述（如：赛博朋克风格的猫）", user_id, reply_id)
            return True
        if image_gen._is_on_cooldown(user_id):
            remaining = image_gen._get_remaining_cooldown(user_id)
            await send_reply(websocket, message_type, target_id, f"🎨 正在生成中，请等待 ~{remaining}s 后再次尝试", user_id, reply_id)
            return True
        image_gen._set_cooldown(user_id)
        asyncio.create_task(_safe_task(image_gen.handle_draw(
            websocket, message_type, target_id, user_id, reply_id, draw_prompt
        )))
        return True

    # ---- /描述画图（先用 LLM 解析再绘图） ----
    if text_for_command.startswith("/描述画图 "):
        set_cooldown(session_key)
        describe_prompt = text_for_command[6:].strip()
        if not describe_prompt:
            await send_reply(websocket, message_type, target_id, "⚠️ 用法：/描述画图 描述（如：刻晴在樱花树下）", user_id, reply_id)
            return True
        if image_gen._is_on_cooldown(user_id):
            remaining = image_gen._get_remaining_cooldown(user_id)
            await send_reply(websocket, message_type, target_id, f"🎨 正在生成中，请等待 ~{remaining}s 后再次尝试", user_id, reply_id)
            return True
        image_gen._set_cooldown(user_id)
        asyncio.create_task(_safe_task(image_gen.handle_describe_draw(
            websocket, message_type, target_id, user_id, reply_id, describe_prompt
        )))
        return True

    # ---- /修改描述（对上一轮 /描述画图 进行修改） ----
    if text_for_command.startswith("/修改描述 "):
        set_cooldown(session_key)
        modification = text_for_command[6:].strip()
        if not modification:
            await send_reply(websocket, message_type, target_id, "⚠️ 用法：/修改描述 修改意见（如：把背景换成海边，不要穿铠甲）", user_id, reply_id)
            return True
        if image_gen._is_on_cooldown(user_id):
            remaining = image_gen._get_remaining_cooldown(user_id)
            await send_reply(websocket, message_type, target_id, f"🎨 正在生成中，请等待 ~{remaining}s 后再次尝试", user_id, reply_id)
            return True
        image_gen._set_cooldown(user_id)
        asyncio.create_task(_safe_task(image_gen.handle_modify_description(
            websocket, message_type, target_id, user_id, reply_id, modification
        )))
        return True

    return False


# ============================================================
#  角色扮演 - 开始游戏与世界观生成
# ============================================================
async def _handle_start_roleplay(
    websocket, message_type, group_id, user_id, reply_id, bg_text,
):
    """处理 /开始扮演 命令 — 创建房间并生成世界观"""
    try:
        # 先发送房间创建确认
        room = group_roleplay.create_room(group_id, user_id, bg_text)

        lines = ["🎭 **新游戏房间已创建！**"]
        lines.append(f"\n📖 **背景**：{bg_text or '未指定'}")
        lines.append("\n👥 玩家：（待报名）")
        lines.append("💡 正在生成世界观...")
        lines.append("💡 发送 `/报名 角色名:描述` 加入（创建者也需要报名）")
        lines.append("💡 满 2 人后发送 `/开演` 开始！")

        initial_reply = "\n".join(lines)
        await _safe_send_reply(websocket, message_type, group_id, initial_reply, user_id, reply_id)
        logger.info(f"🎭 房间已创建，开始生成世界观: {bg_text[:30]}")

        # 异步生成世界观
        world = await group_roleplay.generate_world(bg_text, llm_call_func=lambda s, m: _rp_llm_call(s, m, use_json_mode=True))

        # 更新房间世界观
        group_roleplay.update_room(room['room_id'], world_state=world)

        # 格式化世界观并发送
        world_display = group_roleplay.format_world_for_display(world)
        await _safe_send_reply(websocket, message_type, group_id, world_display, user_id, reply_id)
        logger.info("✅ 世界观已生成")

    except ConnectionClosed:
        logger.warning("WebSocket 已断开，世界观未发送")
    except Exception as e:
        logger.error(f"世界观生成失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 世界观生成失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


async def _handle_regenerate_world(
    websocket, message_type, group_id, user_id, reply_id, new_bg,
):
    """处理 /重新生成世界观 命令"""
    try:
        room = group_roleplay.get_active_room(group_id)
        if not room:
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 没有找到活跃的游戏房间", user_id, reply_id)
            return

        if room['creator_id'] != user_id:
            await _safe_send_reply(websocket, message_type, group_id, "⚠️ 只有创建者可以重新生成世界观", user_id, reply_id)
            return

        bg_text = new_bg or room['background']
        await _safe_send_reply(websocket, message_type, group_id, f"🔄 **正在重新生成世界观...**\n\n📖 **新背景**：{bg_text or '未指定'}", user_id, reply_id)
        logger.info(f"🔄 重新生成世界观: {bg_text[:30]}")

        world = await group_roleplay.generate_world(bg_text, llm_call_func=lambda s, m: _rp_llm_call(s, m, use_json_mode=True))
        group_roleplay.update_room(room['room_id'], world_state=world)

        world_display = group_roleplay.format_world_for_display(world)
        await _safe_send_reply(websocket, message_type, group_id, world_display, user_id, reply_id)
        logger.info("✅ 世界观已重新生成")

    except ConnectionClosed:
        logger.warning("WebSocket 已断开，世界观未发送")
    except Exception as e:
        logger.error(f"重新生成世界观失败: {e}", exc_info=True)
        try:
            await _safe_send_reply(websocket, message_type, group_id, f"😵 重新生成世界观失败: {e}", user_id, reply_id)
        except ConnectionClosed:
            pass


# ============================================================
#  用户人设 LLM 处理（后台任务）
# ============================================================
async def _handle_update_persona(
    websocket, message_type: str, target_id: int,
    user_id: int, reply_id: Optional[int], description: str,
):
    """后台任务：用 LLM 解析自然语言描述，更新用户正式人设"""
    # LLM 总开关早退（2026-08-21 审计）：不发起 LLM 调用
    if not llm_enabled():
        await send_reply(websocket, message_type, target_id,
                         "🔕 LLM 总开关关闭，暂时无法更新人设（GUI 总览页 LLM 板块可开启）",
                         user_id, reply_id)
        return
    try:
        current = get_active_persona(user_id, target_id if message_type == "group" else 0)
        current_json = json.dumps(current, ensure_ascii=False) if current else "{}"
        system_msg = {
               "role": "system",
               "content": (
                   "你是一个用户人设解析器。任务是根据用户的自然语言描述，**在已有 JSON 人设基础上做增量修改**。\n\n"
                   "人设 JSON 结构（9 大部分）：\n\n"
                   "1. identity（身份标签）：对象，含以下子字段\n"
                   "   - gender: 性别\n"
                   "   - age_range: 年龄段（如 20-24）\n"
                   "   - body_features: 身体特征（如 175cm/60kg，胸围/腰围等）\n"
                   "   - location: 城市坐标（如 北京）\n"
                   "   - school_work: 学校/工作状态（如 在读学生/某公司工程师）\n\n"
                   "2. interests（兴趣爱好）：字符串数组\n"
                   "3. personality（性格特征）：字符串，描述性格特点\n"
                   "4. relationships（与群友关系）：对象，键为群友昵称，值为关系描述\n"
                   "5. weaknesses_taboos（弱点与雷区）：弱点、怕的东西、社死点、禁忌话题（数组）\n"
                   "6. group_role（群内地位/人设）：在群里的角色，如\"群宠\"、\"杠精\"、\"气氛组\"（字符串）\n"
                   "7. catchphrases（口头禅/常用梗）：高频词、口头禅、标志性梗（数组）\n"
                   "8. sexual_experience（性经历与性器官特征）：对象\n"
                   "   - experience: 性经历描述\n"
                   "   - body: 性器官特征描述\n\n"
                   "9. sexual_preferences（性癖好）：字符串数组\n\n"
                   "⚠️ QQ群口嗨/玩梗识别：\n"
                   "- 用户经常口嗨（夸张、反串、开玩笑）。例如：\"我是富二代\"、\"我昨晚去抢银行了\"。\n"
                   "- 这类描述大概率是玩梗，除非语气认真或有上下文印证，否则放到 weaknesses_taboos 或 group_role 中记录为\"口嗨型\"标签，不要写入 identity 等客观字段！\n\n"
                   "---\n\n"
                    "⚠️ 合并规则（必须严格遵守）：\n\n"
                                        "1. 【增量底线-防清空】用户没提到的字段，**必须原样保留**已有数据，绝对不允许置空或覆盖为 null/空字符串！\n\n"
                                        "2. 【状态类字段-新优先】identity（身份/职业）等状态字段：假设用户的新描述在时间上更靠后。如果新信息明确了当前状态（如'我辞职了'、'脱单了'），必须【强制覆盖】旧数据。\n\n"
                                        "3. 【数组类字段-语义剔除】合并新旧数组（interests, weaknesses等）。但如果用户明确说'否定/退坑/讨厌'语义（如'再也不玩原神了'、'讨厌吃香菜'），必须从数组中【剔除】对应项，绝不能盲目追加！\n\n"
                                        "4. 【字符串字段-融合重写】不要简单覆盖！请将旧描述与新描述【逻辑融合】：补充细节则自然融入；推翻了旧设定则以新信息为主，保留反差感（写成：'曾经外向，现转为社恐'）。字数控制在 150 字以内。\n\n"
                                        "5. 【对象字段-演进式合并】relationships 中，新群友直接新增；同一群友关系变化时，【不要直接覆盖】，请融合描述保留'八卦历史'（如旧值'暗恋对象'+新值'互怼冤家'→'曾暗恋，现转为互怼冤家'）。\n\n"
                                        "6. 【空值规范】仅当所有片段中都没有该信息时，才保持空字符串 ''、空数组 [] 或空对象 {{}}。\n\n"
                                        "7. QQ群口嗨/玩梗识别：用户说'我是富二代'、'昨晚去抢银行了'大概率是玩梗，除非语气认真，否则放到 group_role 记录为'口嗨型'，不要写入 identity 等客观字段！\n\n"
                    "---\n\n"
                    "输出格式：\n\n"
                    "1. 输出纯 JSON，不要 Markdown 包裹，不要解释。\n\n"
                    "2. 所有值为中文，key 为英文。\n\n"
                   f"当前已有信息：{current_json}\n"
                   f"用户的新描述：{description}\n\n"
                   "请输出增量修改后的**完整 JSON**（含所有保留的已有字段 + 修改/新增的字段）。"
               ),
           }
        user_msg = {"role": "user", "content": description}
        reply = await call_llm([system_msg, user_msg], max_tokens=MAX_TOKENS_SHORT, source="人设更新")
        # 提取 JSON（处理可能存在的 Markdown 代码块）
        reply = reply.strip()
        if reply.startswith("```"):
            reply = reply.split("\n", 1)[-1]
            if reply.endswith("```"):
                reply = reply[:-3]
            reply = reply.strip()
        new_persona = json.loads(reply)

        # 对比新旧人设，找出变更字段
        diff_text = _get_persona_diff(current, new_persona)

        save_persona(user_id, new_persona, "", target_id if message_type == "group" else 0)
        text = "📋 你的用户人设已更新：\n" + diff_text
        await send_reply(websocket, message_type, target_id, text, user_id, reply_id)
        logger.info("✅ 人设更新完成")
    except json.JSONDecodeError as e:
        logger.error(f"人设 JSON 解析失败: {e}, 原始回复: {reply[:200]}")
        await send_reply(websocket, message_type, target_id,
            "⚠️ 人设解析失败，请重试\nBot 返回的内容格式有误", user_id, reply_id)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，人设更新未发送")
    except Exception as e:
        logger.error(f"人设更新失败: {e}", exc_info=True)
        await send_reply(websocket, message_type, target_id,
            f"⚠️ 人设更新出错: {str(e)[:50]}", user_id, reply_id)


async def _handle_set_temp_persona(
    websocket, message_type: str, target_id: int,
    user_id: int, reply_id: Optional[int], description: str,
):
    """后台任务：用 LLM 解析自然语言描述，设置用户临时人设"""
    # LLM 总开关早退（2026-08-21 审计）：不发起 LLM 调用
    if not llm_enabled():
        await send_reply(websocket, message_type, target_id,
                         "🔕 LLM 总开关关闭，暂时无法设置临时人设（GUI 总览页 LLM 板块可开启）",
                         user_id, reply_id)
        return
    try:
        base_persona = get_persona_display(user_id, target_id if message_type == "group" else 0)
        # 取正式人设为基础
        if base_persona:
            base_json = json.dumps(base_persona.get("persona") or {}, ensure_ascii=False)
        else:
            base_json = "{}"
        system_msg = {
           "role": "system",
           "content": (
               "你是一个用户人设解析器。任务是创建用户的临时人设。\n\n"
               "人设 JSON 结构（9 大部分 + 临时状态）：\n\n"
               "1. identity（身份标签）：对象（gender, age_range, body_features, location, school_work）\n"
               "2. interests（兴趣爱好）：字符串数组\n"
               "3. personality（性格特征）：字符串\n"
               "4. relationships（与群友关系）：对象\n"
               "5. weaknesses_taboos（弱点与雷区）：弱点、怕的东西、社死点、禁忌话题（数组）\n"
               "6. group_role（群内地位/人设）：在群里的角色（字符串）\n"
               "7. catchphrases（口头禅/常用梗）：高频词、口头禅、标志性梗（数组）\n"
               "8. sexual_experience（性经历与性器官特征）：对象\n"
               "9. sexual_preferences（性癖好）：字符串数组\n\n"
               "临时人设额外字段：\n"
               "- mood: 当前情绪/状态\n"
               "- situation: 当前处境/情境\n\n"
               "规则：\n\n"
               "⚠️ 合并规则（必须严格遵守）：\n\n"
               "1. 【增量底线-防清空】正式人设中未提及的字段，必须原样保留，绝对不允许置空！\n\n"
               "2. 【状态类字段-新优先】identity 等状态字段：临时描述中明确的新状态强制覆盖。\n\n"
               "3. 【数组类字段-语义剔除】临时描述中说'退坑/讨厌'，需从数组剔除对应项。\n\n"
               "4. 【字符串字段-融合重写】旧+新逻辑融合，推翻旧设定保留反差感，≤150字。\n\n"
               "5. 【对象字段-演进式合并】relationships 关系变化时融合描述保留历史。\n\n"
               "6. 【空值规范】仅新旧均无信息时才保持 ''/[]/{}。\n\n"
               "7. 输出纯 JSON，不要 Markdown 包裹，不要解释。\n\n"
               "8. 以正式人设为基础，叠加临时描述中的变化。\n\n"
               "9. 新增 mood 和 situation 字段来反映临时状态。\n\n"
               "10. 所有值为中文，key 为英文。\n\n"
               f"正式人设：{base_json}\n"
               f"临时描述：{description}\n\n"
               "请输出合并后的完整 JSON（含正式人设 + 临时变化）。"
           ),
        }
        user_msg = {"role": "user", "content": description}
        reply = await call_llm([system_msg, user_msg], max_tokens=MAX_TOKENS_SHORT, source="人设更新")
        # 提取 JSON
        reply = reply.strip()
        if reply.startswith("```"):
            reply = reply.split("\n", 1)[-1]
            if reply.endswith("```"):
                reply = reply[:-3]
            reply = reply.strip()
        temp_persona = json.loads(reply)

        # 对比新旧人设，找出变更字段
        base_dict = base_persona.get("persona") if base_persona else {}
        diff_text = _get_persona_diff(base_dict, temp_persona)

        set_temporary_persona(user_id, temp_persona, "", target_id if message_type == "group" else 0)
        text = "🎭 临时人设已设置：\n" + diff_text
        text += "\n\n💡 发送 '/恢复人设' 可恢复正式人设"
        await send_reply(websocket, message_type, target_id, text, user_id, reply_id)
        logger.info("✅ 临时人设设置完成")
    except json.JSONDecodeError as e:
        logger.error(f"临时人设 JSON 解析失败: {e}, 原始回复: {reply[:200]}")
        await send_reply(websocket, message_type, target_id,
            "⚠️ 人设解析失败，请重试", user_id, reply_id)
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，临时人设未发送")
    except Exception as e:
        logger.error(f"临时人设设置失败: {e}", exc_info=True)
        await send_reply(websocket, message_type, target_id,
            f"⚠️ 临时人设设置出错: {str(e)[:50]}", user_id, reply_id)


# ============================================================
#  后台 AI 对话（不阻塞 / 指令）
# ============================================================
# 聊天中自动执行功能指令的白名单（2026-08-12）
# 仅只读/查询类——代码级硬编码安全边界：
# LLM 输出任何白名单外的指令标记（[执行:/踢人] 等）都会被忽略，无法执行。
# 执行路径只调映射的只读 handler，不经过 check_command / handle_message 通用分发。
_AI_EXEC_WHITELIST = {
    "/活跃度": "activity",
    "/投票": "vote_status",
    "/用户人设": "self_persona",
    "/游戏状态": "game_status",
    "/群像": "group_persona",
    "/查询": "query",
    "/分析": "analysis",
    "/评选": "evaluation",   # 2026-08-12：含 is_admin 权限检查（非管理员被拦截）
    "/总结": "summary",      # 2026-08-12：含 is_admin 权限检查（非管理员被拦截）
}


async def _execute_ai_command(websocket, message_type, group_id, user_id, reply_id, cmd: str) -> bool:
    """执行聊天中 LLM 标记的功能指令（仅白名单，代码级安全边界）。

    返回 True = 已尝试执行；False = 不在白名单/格式无效（调用方忽略标记）。
    """
    try:
        if not cmd or not cmd.startswith("/"):
            return False
        parts = cmd.strip().split(None, 1)
        cmd_name = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        kind = _AI_EXEC_WHITELIST.get(cmd_name)
        # 2026-08-12：支持数字后缀（/活跃度7 → /活跃度 + arg=7；/查询30 → /查询 + arg=30）
        # LLM 可能按用户表述输出 [执行:/活跃度1]（今日=1天）等
        if not kind:
            _m_suffix = re.match(r"^(/[^\d]+)(\d+)$", cmd_name)
            if _m_suffix and _AI_EXEC_WHITELIST.get(_m_suffix.group(1)):
                kind = _AI_EXEC_WHITELIST[_m_suffix.group(1)]
                cmd_name = _m_suffix.group(1)
                arg = f"{_m_suffix.group(2)} {arg}".strip()
        if not kind:
            logger.info(f"⛔ 聊天执行标记忽略（不在白名单）: {cmd_name}")
            return False
        logger.info(f"⚡ 聊天自动执行指令: {cmd} (kind={kind})")
        if kind == "activity":
            # arg 可能为数字天数（/活跃度7 → days=7；2026-08-12）
            _act_days = int(arg) if arg.isdigit() and int(arg) > 0 else 0
            await handle_activity(websocket, message_type, group_id, user_id, reply_id, _act_days)
        elif kind == "vote_status":
            if group_id and group_vote.is_active(group_id):
                await send_reply(websocket, message_type, group_id, group_vote.get_vote_status(group_id), user_id, reply_id)
            else:
                await send_reply(websocket, message_type, group_id, "⚠️ 当前没有正在进行的投票", user_id, reply_id)
        elif kind == "self_persona":
            result = get_persona_display(user_id, group_id or 0)
            if result is None:
                reply_text = "📋 你的用户人设为空\n\n💡 使用 '/修改人设 我是学生，喜欢跑步和编程' 来创建人设"
            else:
                lines = ["📋 你的用户人设："]
                source_group = result.get("_source_group", 0)
                if group_id and source_group != group_id:
                    lines.append(f"（数据来源：主群{source_group}，与同集群群共享）\n")
                lines.append(persona_to_text(result.get("persona") or {}))
                if result.get("temp_persona"):
                    lines.append("\n🎭 当前正在使用临时人设：")
                    lines.append(persona_to_text(result.get("temp_persona") or {}))
                reply_text = "\n".join(lines)
            await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        elif kind == "game_status":
            reply, at_uids = await asyncio.to_thread(
                entertainment.check_command, "/游戏状态",
                group_id=group_id, user_id=user_id, nickname="用户",
            )
            if reply:
                await send_reply(websocket, message_type, group_id or user_id, reply, user_id, reply_id,
                                 at_user_ids=at_uids if at_uids else None)
        elif kind == "group_persona":
            question = arg or "介绍一下群里的大家"
            _tk = TASK_REGISTRY.register("群指令", f"🤖 群像（AI 触发）", group_id=group_id, user_id=user_id)
            # 2026-08-22 暂停门：AI 直调开始执行前等放行
            await TASK_REGISTRY.wait_if_paused()
            try:
                await handle_group_persona(websocket, message_type, group_id, user_id, reply_id, question)
            finally:
                TASK_REGISTRY.finish(_tk)
        elif kind == "query":
            question = arg or "最近群里聊了什么"
            _tk = TASK_REGISTRY.register("群指令", f"🤖 查询「{question[:20]}」（AI 触发）", group_id=group_id, user_id=user_id)
            # 2026-08-22 暂停门：AI 直调开始执行前等放行
            await TASK_REGISTRY.wait_if_paused()
            try:
                await handle_query(websocket, message_type, group_id, user_id, reply_id, 0, question)
            finally:
                TASK_REGISTRY.finish(_tk)
        elif kind == "analysis":
            # 需要 QQ 号：标记格式 [执行:/分析 QQ号 问题]
            if not arg:
                await send_reply(websocket, message_type, group_id or user_id,
                                 "📝 分析需要指定对象：如「分析一下群里的某位群友」或「帮我分析 QQ 100000001 最近聊什么」", user_id, reply_id)
                return True
            qq_part, _, q_text = arg.partition(" ")
            if qq_part.isdigit():
                _tk = TASK_REGISTRY.register("群指令", f"🤖 分析{qq_part}（AI 触发）", group_id=group_id, user_id=user_id)
                # 2026-08-22 暂停门：AI 直调开始执行前等放行
                await TASK_REGISTRY.wait_if_paused()
                try:
                    await handle_user_analysis(websocket, message_type, group_id, user_id, reply_id,
                                               [qq_part], q_text or "最近在聊什么")
                finally:
                    TASK_REGISTRY.finish(_tk)
            else:
                # 昵称 → 查 QQ（分析 handler 需要 QQ 列表）
                try:
                    from core.persona import find_user_by_nickname as _find_user_by_nickname
                    found = _find_user_by_nickname(qq_part, group_id or 0)
                    if found and found.get("user_id"):
                        _tk = TASK_REGISTRY.register("群指令", f"🤖 分析{found['user_id']}（AI 触发）", group_id=group_id, user_id=user_id)
                        # 2026-08-22 暂停门：AI 直调开始执行前等放行
                        await TASK_REGISTRY.wait_if_paused()
                        try:
                            await handle_user_analysis(websocket, message_type, group_id, user_id, reply_id,
                                                       [str(found["user_id"])], q_text or "最近在聊什么")
                        finally:
                            TASK_REGISTRY.finish(_tk)
                    else:
                        await send_reply(websocket, message_type, group_id or user_id,
                                         f"🔍 没找到昵称「{qq_part}」对应的群友，请用 QQ 号试试", user_id, reply_id)
                except Exception as _e:
                    await send_reply(websocket, message_type, group_id or user_id,
                                     f"🔍 请用 QQ 号指定分析对象：/分析 QQ号 问题", user_id, reply_id)
        elif kind == "evaluation":
            # /评选 仅群聊 + 仅管理员（权限检查复用原指令逻辑，2026-08-12）
            if not group_id:
                await send_reply(websocket, message_type, group_id or user_id,
                                 "📊 /评选 需要在群聊中使用哦～", user_id, reply_id)
                return True
            if not is_admin(user_id):
                await send_reply(websocket, message_type, group_id, "🔒 只有管理员可以使用 /评选 指令哦～", user_id, reply_id)
                return True
            from core.scheduler import handle_evaluation
            asyncio.create_task(_safe_command(
                handle_evaluation(websocket, message_type, group_id, user_id, reply_id, group_id),
                cmd_name="/评选",
                task_key=TASK_REGISTRY.register("群指令", f"🤖 评选（AI 触发）", group_id=group_id, user_id=user_id)))
            logger.info("🔄 聊天触发 /评选 已在后台执行")
        elif kind == "summary":
            # /总结 仅群聊 + 仅管理员（权限检查复用原指令逻辑，2026-08-12）
            if not group_id:
                await send_reply(websocket, message_type, group_id or user_id,
                                 "📝 /总结 需要在群聊中使用哦～", user_id, reply_id)
                return True
            if not is_admin(user_id):
                await send_reply(websocket, message_type, group_id, "🔒 只有管理员可以使用 /总结 指令哦～", user_id, reply_id)
                return True
            from core.scheduler import handle_summary
            asyncio.create_task(_safe_command(
                handle_summary(websocket, message_type, group_id, user_id, reply_id, group_id),
                cmd_name="/总结",
                task_key=TASK_REGISTRY.register("群指令", f"🤖 总结（AI 触发）", group_id=group_id, user_id=user_id)))
            logger.info("🔄 聊天触发 /总结 已在后台执行")
        return True
    except Exception as e:
        logger.error(f"聊天自动执行指令异常: {e}")
        return False


async def _handle_ai_reply(
    websocket,
    message_type: str,
    target_id: int,
    session_key: str,
    messages: list[dict],
    user_id: Optional[int],
    reply_id: Optional[int],
    nickname: str = "用户",
):
    """后台执行 AI 对话，完成后发送回复，不阻塞其他消息处理"""
    # 2026-08-22 任务列表：AI 聊天回复登记（等 chat 锁=排队，LLM 调用=执行）
    _ai_task_key = None
    try:
        _ai_target_desc = "群" if message_type == "group" else "私聊"
        _ai_task_key = TASK_REGISTRY.register(
            "AI 聊天", f"💬 回复 {nickname}（{_ai_target_desc}）",
            group_id=target_id if message_type == "group" else 0,
            user_id=user_id or 0, status="queued")
    except Exception:
        _ai_task_key = None  # 登记失败不影响回复本身
    try:
        # 2026-08-21：bot 回复链路 LLM 参数走 ai_chat.llm 段（GUI「AI 聊天·显示参数」
        # 弹窗可配置，热重载即时生效）。默认值 = 原 call_llm 函数默认（行为不变）。
        _ai_llm = ((CONFIG.get("AI_CHAT_CFG") or {}).get("llm") or {})
        _kw = {
            "max_tokens": int(_ai_llm.get("max_tokens", 65536)),
            "temperature": float(_ai_llm.get("temperature", 0.7)),
            "json_mode": bool(_ai_llm.get("json_mode", False)),
            "timeout": int(_ai_llm.get("timeout", 1800)),
        }
        _th = str(_ai_llm.get("thinking", "on")).lower()
        if _th == "off":
            _kw["disable_thinking"] = True
        elif _th in ("low", "max"):
            _kw["reasoning_effort"] = _th
        # _th == "on" → 不传（DeepSeek 后端默认 reasoning_effort=max）
        reply = await call_llm(messages, use_lock=True, lock_type="chat",
                               source="bot 回复", task_key=_ai_task_key, **_kw)  # 聊天之间串行排队，与任务并行

        # 从回复中提取好感度变化（JSON 标注，对用户不可见）
        delta = _parse_favorability_delta(reply)
        clean_reply = _strip_favorability_tag(reply)

        # 更新好感度
        if user_id and message_type == "group":
            new_fav, new_rel, old_rel = update_bot_favorability(target_id, user_id, delta)
            logger.info(f"💕 好感度更新: user={user_id}, delta={delta}, new={new_fav}, rel={new_rel}")

            # 关系发生变化时，用 LLM 生成符合人设的通知消息
            if old_rel and new_rel != old_rel:
                rel_msg = await _generate_relationship_change_message(
                    messages, nickname, old_rel, new_rel, new_fav, delta
                )
                if rel_msg:
                    await asyncio.sleep(1.5)
                    await send_reply(
                        websocket, message_type, target_id,
                        rel_msg, user_id, reply_id=None, at_user_ids=[user_id]
                    )
                    logger.info(f"💬 关系变化通知: {old_rel} → {new_rel}")

        save_message(session_key, "assistant", clean_reply, int(get_bot_uin() or 0), "Bot")

        # 聊天中自动执行功能指令（2026-08-12）：LLM 回复末尾 [执行:/指令] 标记 →
        # 白名单校验 → 执行对应只读指令（结果作为补充消息发送）
        exec_cmd = None
        _exec_m = re.search(r"\[执行:([^\]]+)\]", clean_reply)
        if _exec_m:
            exec_cmd = _exec_m.group(1).strip()
            clean_reply = clean_reply.replace(_exec_m.group(0), "").strip()

        await send_reply(websocket, message_type, target_id, clean_reply, user_id, reply_id)
        logger.info(f"✅ 已回复 ({len(clean_reply)} 字)")

        # 执行标记处理（先发原回复，再执行——执行结果作为补充消息）
        if exec_cmd:
            await _execute_ai_command(websocket, message_type, target_id, user_id, reply_id, exec_cmd)

        # 存档 bot 回复（2026-08-08 用户方案）：锚定用户最近一条 @bot 消息时间 +0.01s，
        # 保证画像/人设提取时对话轮次相邻（用户 @bot → bot 回复），避免 LLM 误归属
        if user_id:
            try:
                from core.archive import archive_message as _archive_bot_reply
                with get_db() as conn:
                    _anchor = conn.execute(
                        "SELECT created_at FROM message_archive "
                        "WHERE user_id = ? AND target_id = ? AND content LIKE '@机器人%' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (user_id, target_id),
                    ).fetchone()
                if _anchor:
                    _archive_bot_reply(
                        0, message_type, target_id, int(get_bot_uin() or 0), "机器人",
                        clean_reply, raw_message=clean_reply,
                        has_image=False, has_voice=False,
                        created_at=_anchor[0] + 0.01,
                    )
            except Exception as _e:
                logger.warning(f"bot 回复存档失败（不影响发送）: {_e}")
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，AI 回复未发送")
    except Exception as e:
        logger.error(f"AI 回复发送失败: {e}")


def _parse_favorability_delta(text: str) -> int:
    """
    从 LLM 回复中提取好感度变化标注。
    格式：[好感:+N]、[好感:-N] 或 [好感:0]，N 为 1-5 的整数。
    找不到则返回 0。
    """
    import re
    m = re.search(r'\[好感:([+-]?\d{1,2})\]', text)
    if m:
        val = int(m.group(1))
        return max(-10, min(10, val))
    return 0


def _strip_favorability_tag(text: str) -> str:
    """去除回复中的好感度标注（对用户不可见）"""
    import re
    return re.sub(r'\[好感:[+-]?\d{1,2}\]', '', text).strip()


async def _generate_relationship_change_message(
    messages: list[dict],
    nickname: str,
    old_rel: str,
    new_rel: str,
    new_fav: int,
    delta: int,
) -> str:
    """
    当关系发生变化时，调用 LLM 基于聊天记录生成自然的人物反应。
    生成的消息符合 Bot 人设的真实反应，而非模板化通知。
    """
    # 提取最近 6 条对话作为上下文（最多约 2000 token）
    recent = messages[-7:]  # 排除 system prompt
    recent_text = ""
    for m in recent:
        if m["role"] == "user":
            recent_text += f"{nickname}: {m['content'][:200]}\n"
        elif m["role"] == "assistant":
            recent_text += f"Bot: {m['content'][:200]}\n"
        else:
            recent_text += f"{m['content'][:200]}\n"

    direction = "升级" if delta > 0 else "降级" if delta < 0 else "时间流逝导致"

    prompt = {
        "role": "user",
        "content": (
            f"以下是你最近与 {nickname} 的对话：\n\n"
            f"{recent_text}\n"
            f"---\n"
            f"现在你对 {nickname} 的关系从「{old_rel}」变成了「{new_rel}」"
            f"（好感度 {new_fav}/100，{direction}）。\n"
            f"\n"
            f"请用你高中女生的身份和口吻，自然地向 {nickname} 表达你对这次关系变化的感受。\n"
            f"要求：\n"
            f"- 像真实的反应，不要太正式或模板化\n"
            f"- 如果是关系升级，表达开心/害羞/期待等情绪\n"
            f"- 如果是关系降级，表达失落/生气/不舍等情绪，语气要真实\n"
            f"- 如果是因为太长时间没联系导致的降级，要提到「好久没收到你的消息」之类的内容\n"
            f"- 回复要简短自然，控制在 1-3 句话以内\n"
            f"- 不需要加 [好感:...] 标注\n"
        ),
    }

    # 好朋友 → 情侣：追加表白专用指令
    if old_rel == "好朋友" and new_rel == "情侣":
        prompt["content"] += (
            "\n\n【额外指示：这是一次表白】\n"
            f"- 明确表达「我喜欢你」「做我男朋友吧」或类似的告白话语\n"
            f"- 语气要害羞、真诚，带有一点紧张和期待\n"
            f"- 可以结合上面的聊天内容，自然地引出表白\n"
            f"- 不要用「女朋友」自称，用「我」来表述\n"
        )

    try:
        # BUG5修复：传入 system prompt，让 LLM 知道自身角色身份
        system_prompt = messages[0] if messages and messages[0].get("role") == "system" else None
        llm_messages = [system_prompt] if system_prompt else []
        llm_messages.append(prompt)
        reply = await call_llm(llm_messages, use_lock=False, source="关系变化")
        # 清理可能的标注和多余空白
        reply = _strip_favorability_tag(reply)
        return reply[:150]  # 截断以防过长
    except Exception as e:
        logger.error(f"关系变化消息生成失败: {e}")
        # 降级为简单模板
        if old_rel == "情侣" and new_rel == "好朋友":
            return "（太久没联系了……虽然心里还是喜欢你，但先回到好朋友吧）"
        if old_rel == "好朋友" and new_rel == "普通朋友":
            return "（感觉最近有些疏远了……那就先回到普通朋友吧）"
        if old_rel == "普通朋友" and new_rel == "陌生人":
            return "（听到你说的话后感觉很生气，那就先绝交吧）"
        if old_rel == "陌生人" and new_rel == "仇人":
            return "（真的被你惹火了……不想再理你了）"
        if old_rel == "仇人" and new_rel == "陌生人":
            return "（心情好点了……也许可以试着重新相处）"
        if old_rel == "陌生人" and new_rel == "普通朋友":
            return "嗯……感觉我们渐渐熟起来了呢"
        if old_rel == "普通朋友" and new_rel == "好朋友":
            return "太好了！觉得我们已经是好朋友了"
        if old_rel == "好朋友" and new_rel == "情侣":
            return "那个……其实我一直喜欢你，可以做我的男朋友吗♡"
        return f"（现在对我们的关系是：{new_rel}）"


# ============================================================
# /活跃度 指令：显示近 N 天群聊活跃度排行
# ============================================================

# OneBot API 调用响应存储（echo -> {data, event}）
_api_responses: dict[str, dict] = {}


async def _call_onebot_api(websocket, action: str, params: dict) -> Optional[dict]:
    """
    通过 OneBot 反向 WS 调用 API，返回响应中的 'data' 字段。
    使用 echo 机制等待响应，超时 10 秒。
    """
    echo = str(time.time())
    msg = {"action": action, "params": params, "echo": echo}
    await websocket.send(json.dumps(msg))

    event = asyncio.Event()
    response = {"data": None, "event": event}
    _api_responses[echo] = response

    try:
        # 等待响应，超时 10 秒
        await asyncio.wait_for(event.wait(), timeout=10.0)
        return _api_responses.pop(echo, {}).get("data")
    except asyncio.TimeoutError:
        _api_responses.pop(echo, None)
        logger.warning(f"OneBot API 调用超时: {action}")
        return None


async def handle_activity(
    websocket, message_type: str, group_id: int,
    user_id: Optional[int], reply_id: Optional[int], days: int = 15
) -> None:
    """
    /活跃度：统计近 N 天群聊活跃度，展示 Top 10 和 Bottom 10。
    支持 /活跃度、/活跃度7、/活跃度 7（days 参数，2026-08-12；0/负=默认 15 天）。
    基于 group_chat_cache 表按 user_id 聚合统计。
    
    关键改进：
    1. 通过 NapCat HTTP API 获取当前群成员列表，排除已退群的人
    2. 严格按 QQ 号（user_id）统计，昵称仅用于展示
    3. 过滤跨群混入的数据
    """
    from collections import Counter
    from datetime import datetime, timedelta
    from .database import get_db

    set_cooldown(_session_key(group_id, user_id))

    if not group_id:
        await send_reply(websocket, message_type, group_id or 0,
                         "📊 /活跃度 需要在群聊中使用哦～", user_id, reply_id)
        return

    try:
        from core.content_filter import censor_text_forced
        # 2026-08-12：昵称强制净化（无视全局审查开关）——群友昵称含 QQ 风控高危词
        # （贫乳/萝莉控/人妻等）会让整条排行消息被服务端拦截/折叠，客户端不可见
        def _safe_nick(nick: str) -> str:
            return censor_text_forced(nick)

        # 获取当前群成员列表——优先 NapCat HTTP API（2026-08-12：WS echo 机制
        # 每次超时 10s 导致 current_members 为空、退群用户无法过滤；HTTP 秒回可靠）
        current_members: set[int] = set()
        nickname_map: dict[int, str] = {}
        try:
            async with httpx.AsyncClient(timeout=8) as _hclient:
                _resp = await _hclient.get(
                    "http://127.0.0.1:3000/get_group_member_list",
                    params={"group_id": group_id},
                )
                _resp.raise_for_status()
                _mdata = _resp.json()
            _members = _mdata.get("data") if isinstance(_mdata, dict) else None
            if isinstance(_members, list):
                for member in _members:
                    uid = member.get("user_id")
                    if uid:
                        current_members.add(uid)
                        # 优先使用群昵称（card），否则用显示名称
                        nickname_map[uid] = member.get("card") or member.get("nickname", f"用户{uid}")
                logger.info(f"👥 活跃度成员列表: {len(current_members)} 人（HTTP API）")
        except Exception as _e:
            logger.warning(f"活跃度获取成员列表失败，退群用户无法过滤: {_e}")
        
        with get_db() as db:
            days = days if days and days > 0 else int(qa_params().get("activity_default_days", 15))  # 0/负 → 默认天数（qa 段热生效）
            cutoff = time.time() - days * 86400

            # 获取近 days 天发言记录，严格按 group_id 过滤
            rows = db.execute(
                "SELECT user_id, nickname, COUNT(*) as cnt FROM group_chat_cache "
                "WHERE group_id=? AND created_at>=? GROUP BY user_id ORDER BY cnt DESC",
                (group_id, cutoff)
            ).fetchall()

            # 过滤掉已退群的人（只保留当前群成员）
            if current_members:
                rows = [row for row in rows if row["user_id"] in current_members]
            # 排除 bot 自己（2026-08-08：bot 回复会入存档/cache，活跃度不应统计机器人）
            _bot_qq_act = int(get_bot_uin() or 0)  # 08-22：从连接派生
            if _bot_qq_act:
                rows = [row for row in rows if row["user_id"] != _bot_qq_act]

            if not rows:
                await send_reply(websocket, message_type, group_id,
                                 f"📊 近 {days} 天暂无发言记录。", user_id, reply_id)
                return

            lines = [f"📊 近 {days} 天群聊活跃度排行（共 {len(rows)} 人发言）：\n"]
            max_count = rows[0]["cnt"] if rows else 1
            bar_width = 5
            lines.append("🏆 Top 10 活跃成员：")
            for rank, row in enumerate(rows[:10], 1):
                # 使用最新的昵称映射，如果找不到则用历史记录（强制净化防风控）
                nick = _safe_nick(nickname_map.get(row["user_id"], row["nickname"] or f"用户{row['user_id']}"))[:3]
                bar_len = int(row["cnt"] / max_count * bar_width)
                bar = "█" * bar_len + "░" * (bar_width - bar_len)
                lines.append(f"  {rank}. {nick:<3} {bar} {row['cnt']} 条")
            lines.append("")

            if len(rows) > 10:
                lines.append("📉 Bottom 10 发言较少：")
                for rank, row in enumerate(rows[-10:], 1):
                    nick = _safe_nick(nickname_map.get(row["user_id"], row["nickname"] or f"用户{row['user_id']}"))[:6]
                    lines.append(f"  {rank}. {nick:<6} {row['cnt']} 条")

            # 查找近 days 天没有发言的人（当前群成员中未发言的）
            active_user_ids = {row["user_id"] for row in rows}
            
            if current_members:
                # 从当前群成员中找出近 days 天未发言的人
                inactive_member_ids = current_members - active_user_ids
                if inactive_member_ids:
                    lines.append("")
                    # 2026-08-12 精简：未发言名单限制前 20 人，避免消息超长
                    # （107 人全名单 1400+ 字 → QQ 风控拦截/折叠风险）
                    shown = sorted(inactive_member_ids)[:20]
                    nicks = [_safe_nick(nickname_map.get(uid, f"用户{uid}"))[:8] for uid in shown]
                    rest = len(inactive_member_ids) - len(shown)
                    lines.append(f"😴 近 {days} 天暂无发言（共 {len(inactive_member_ids)} 人）：")
                    lines.append("  " + "、".join(nicks) + (f" 等 {rest} 人" if rest > 0 else ""))
            else:
                # 降级方案：API 调用失败时，使用 message_archive 作为备选
                # 2026-08-12 修复：原 DISTINCT user_id, nickname 按两列去重——用户改名
                # 产生多行（实测 24431 行 vs 实际 97 人），名单重复展示同一用户；
                # 改为 GROUP BY user_id 取 MAX(nickname)
                inactive_rows = db.execute(
                    "SELECT user_id, MAX(nickname) as nickname FROM message_archive "
                    "WHERE target_id=? AND message_type='group' AND user_id NOT IN (" + ",".join("?" for _ in active_user_ids) + ") "
                    "GROUP BY user_id ORDER BY user_id",
                    [group_id] + list(active_user_ids)
                ).fetchall()
                if inactive_rows:
                    lines.append("")
                    # 2026-08-12 精简：限制前 20 人（理由同上）
                    shown = inactive_rows[:20]
                    nicks = [_safe_nick(nickname_map.get(row["user_id"], row["nickname"] or f"用户{row['user_id']}"))[:8] for row in shown]
                    rest = len(inactive_rows) - len(shown)
                    lines.append(f"😴 近 {days} 天暂无发言（共 {len(inactive_rows)} 人）：")
                    lines.append("  " + "、".join(nicks) + (f" 等 {rest} 人" if rest > 0 else ""))

            reply = "\n".join(lines)
            await send_reply(websocket, message_type, group_id, reply, user_id, reply_id)
            logger.info(f"✅ 活跃度排行已发送 ({len(rows)} 人)")
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，活跃度排行未发送")
    except Exception as e:
        logger.error(f"活跃度排行发送失败: {e}")
        await send_reply(websocket, message_type, group_id,
                         f"📊 活跃度统计出错: {e}", user_id, reply_id)


# ============================================================
# /查询 指令：用自然语言查询群聊记录
# ============================================================

async def handle_query(
    websocket, message_type: str, group_id: int,
    user_id: Optional[int], reply_id: Optional[int],
    _extra: int, question: str, hours: int = 24
) -> None:
    """
    /查询 xxx：用 LLM 分析群聊缓存，回答用户关于聊天记录的问题。
    从 group_chat_cache 取近 hours 小时的记录，交由 LLM 总结回答。

    2026-08-21：Map-Reduce 核心抽至 core/analysis.py（run_query_analysis），
    与 GUI 消息分析共用（prompt 单一来源）；本函数只保留取数、群内反馈、
    cooldown 等群聊上下文行为，输出与抽取前逐字一致。
    """
    from datetime import datetime, timedelta
    from .database import get_db
    from .analysis import run_query_analysis, AnalysisError

    # LLM 总开关早退（2026-08-21 审计）：不发起 Map-Reduce、不写审计库、不刷日志
    # （run_query_analysis 内部也有同款早退，此处提前拦截是为了给用户更准确的提示）
    if not llm_enabled():
        await send_reply(websocket, message_type, group_id,
                         "🔕 LLM 总开关关闭，暂时无法查询（GUI 总览页 LLM 板块可开启）",
                         user_id, reply_id)
        return

    set_cooldown(_session_key(group_id, user_id))

    try:
        with get_db() as db:
            cutoff = time.time() - (hours * 3600)

            rows = db.execute(
                "SELECT user_id, nickname, content, created_at FROM group_chat_cache "
                "WHERE group_id=? AND created_at>=? ORDER BY created_at ASC",
                (group_id, cutoff)
            ).fetchall()

            if not rows:
                await send_reply(websocket, message_type, group_id,
                                 f"📡 近 {hours} 小时暂无群聊记录可供查询。", user_id, reply_id)
                return

            total = len(rows)

            set_cooldown(_session_key(group_id, user_id))
            await send_reply(
                websocket, message_type, group_id,
                f"📡 正在查询近 {hours} 小时的群聊记录（共 {total} 条），请稍候...",
                user_id, reply_id,
            )
            logger.info(f"📡 开始查询: 问题「{question}」, 共 {total} 条记录")

            # Map-Reduce 分析（core/analysis.py 共享核心，prompt 与 GUI 同源）
            summary, total_batches = await run_query_analysis(
                question, rows,
                scope_desc=f"近 {hours} 小时群聊记录",
                source="cmd", group_id=group_id, hours=hours,
            )

            await send_reply(websocket, message_type, group_id,
                             f"📡 {summary}", user_id, reply_id)
            logger.info(f"✅ 查询结果已发送 ({total} 条记录, {total_batches} 批次)")
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，查询结果未发送")
    except AnalysisError:
        await send_reply(websocket, message_type, group_id,
                         f"📡 在近 {hours} 小时的群聊记录中未找到与「{question}」相关的信息。",
                         user_id, reply_id)
    except Exception as e:
        logger.error(f"查询结果发送失败: {e}")
        await send_reply(websocket, message_type, group_id,
                         f"📡 查询出错: {e}", user_id, reply_id)


# ============================================================
#  用户聊天分析
# ============================================================
# ============================================================
#  /分析 消息提取：目标用户发言 + @目标用户 + 回复目标用户 + 上下文(前4后4)
# ============================================================
def _extract_analysis_messages(
    target_user_ids: list[int],
    group_id: int,
    cutoff: float,
    window: int = 4,
) -> list[dict]:
    """
    /分析 输入消息提取（2026-08-05 改造）。

    消息集合 = 目标用户发言 ∪ 他人 @目标用户 的消息 ∪ 他人回复(引用)目标用户的消息，
    再对每条相关消息取前后 window 条（默认前4后4）作为上下文。

    参照 persona._extract_relevant_messages 的 related + context 机制：
    - related 判定：本人发言 / raw_message 含 [CQ:at,qq=目标] / raw_message 含
      [CQ:reply,id=X] 且 X 是目标用户消息的 message_id
    - 上下文：相关消息 ±window 邻域内所有消息（含其他用户消息，作理解语境）
    - 时间窗：近 N 天（created_at >= cutoff），上限 now
    - 目标用户近 N 天无发言时，@/回复目标用户的消息仍会纳入（related 判定不依赖 user_msgs）

    返回按 created_at 升序的去重消息列表，元素含
    {id, message_id, user_id, nickname, content, created_at}。
    """
    from .database import get_db

    placeholders = ", ".join(["?"] * len(target_user_ids))
    with get_db() as conn:
        # 目标用户自己的消息（时间窗内）
        user_msgs = conn.execute(
            f"SELECT id, message_id, user_id, nickname, content, raw_message, created_at "
            f"FROM message_archive WHERE target_id=? AND user_id IN ({placeholders}) AND created_at>=? "
            f"ORDER BY created_at ASC",
            [group_id] + target_user_ids + [cutoff],
        ).fetchall()

    target_message_ids = set(str(row["message_id"]) for row in user_msgs)

    with get_db() as conn:
        # 窗口内群内所有消息：下限 cutoff（近 N 天），上限 now（含 @/回复消息之后的上下文）
        all_msgs = conn.execute(
            f"SELECT id, message_id, user_id, nickname, content, raw_message, created_at "
            f"FROM message_archive WHERE target_id=? AND created_at>=? AND created_at<=? "
            f"ORDER BY created_at ASC",
            [group_id, cutoff, time.time()],
        ).fetchall()

    if not all_msgs:
        return []

    at_patterns = [f"[CQ:at,qq={uid}]" for uid in target_user_ids]
    content_at_patterns = [f"@{uid}" for uid in target_user_ids]
    reply_re = re.compile(r"\[CQ:reply,id=(\d+)\]")
    target_set = set(target_user_ids)

    relevant_ids: set[int] = set()
    for msg in all_msgs:
        raw = msg["raw_message"] or ""
        if msg["user_id"] in target_set:
            relevant_ids.add(msg["id"])
        elif any(p in raw for p in at_patterns) or any(p in (msg["content"] or "") for p in content_at_patterns):
            relevant_ids.add(msg["id"])
        else:
            m = reply_re.search(raw)
            if m and m.group(1) in target_message_ids:
                relevant_ids.add(msg["id"])

    if not relevant_ids:
        return []

    id_to_index = {msg["id"]: i for i, msg in enumerate(all_msgs)}
    context_ids: set[int] = set()
    for rel_id in relevant_ids:
        idx = id_to_index.get(rel_id)
        if idx is None:
            continue
        for offset in range(-window, window + 1):
            target_idx = idx + offset
            if 0 <= target_idx < len(all_msgs):
                context_ids.add(all_msgs[target_idx]["id"])

    return [
        {
            "id": all_msgs[i]["id"],
            "message_id": all_msgs[i]["message_id"],
            "user_id": all_msgs[i]["user_id"],
            "nickname": all_msgs[i]["nickname"],
            "content": all_msgs[i]["content"],
            "created_at": all_msgs[i]["created_at"],
        }
        for i in range(len(all_msgs))
        if all_msgs[i]["id"] in context_ids
    ]


async def handle_user_analysis(
    websocket, message_type: str, group_id: int,
    user_id: Optional[int], reply_id: Optional[int],
    target_qqs: list[str], question: str, days: int = 15
) -> None:
    """
    /分析 <qq号[+qq号...]> <内容>：用 LLM 分析指定用户近 days 天（默认 15，可用 /分析60 查 60 天）的聊天记录，回答用户的问题。
    支持单用户和多用户（+ 分隔 / @ 分隔）。输入消息 = 目标用户发言 + @目标用户 + 回复目标用户
    + 每条相关消息前4后4上下文（_extract_analysis_messages）；分批处理 + 多级收敛汇总
    （线索超长时 _hierarchical_merge_by_len，与人设画像同策略）。
    数据源用 message_archive（永久存档，可回溯 60+ 天）；group_chat_cache 仅 7 天，无法支撑长窗口。
    """
    from datetime import datetime

    set_cooldown(_session_key(group_id, user_id))

    # LLM 总开关早退（2026-08-21 审计）：不发起 Map-Reduce、不写 analysis 审计库
    if not llm_enabled():
        await send_reply(websocket, message_type, group_id,
                         "🔕 LLM 总开关关闭，暂时无法分析（GUI 总览页 LLM 板块可开启）",
                         user_id, reply_id)
        return

    # 本轮调用的唯一标识（毫秒时间戳）：同轮所有中间结果共用，便于按轮次审计
    run_id = int(time.time() * 1000)

    # 校验 QQ 号
    for qq in target_qqs:
        try:
            int(qq)
        except ValueError:
            await send_reply(websocket, message_type, group_id,
                             f"📊 QQ号格式不正确: {qq}（应为纯数字）", user_id, reply_id)
            return

    try:
        # 近 days 天的记录（数据源用 message_archive 永久存档，可回溯 60+ 天）
        now = time.time()
        cutoff = now - (days * 86400)

        target_qq_ints = [int(q) for q in target_qqs]

        # 提取输入消息：目标用户发言 + @目标用户 + 回复目标用户 + 上下文(前N后N)
        _ctx_window = int(qa_params().get("analysis_context_window", 4))
        rows = _extract_analysis_messages(target_qq_ints, group_id, cutoff, window=_ctx_window)

        if not rows:
            qq_list = "+".join(target_qqs)
            await send_reply(websocket, message_type, group_id,
                             f"📊 QQ {qq_list} 在近 {days} 天内暂无聊天记录可供分析。", user_id, reply_id)
            return

        # 构建 user_id → 短 ID 映射 + 昵称映射
        uid_to_short: dict[str, str] = {}
        short_to_nick: dict[str, str] = {}
        nickname_map: dict[str, str] = {}
        _counter = 1
        for row in rows:
            uid = str(row["user_id"])
            if uid not in uid_to_short:
                uid_to_short[uid] = f"U{_counter}"
                nick = row["nickname"] or f"用户{uid}"
                nickname_map[uid] = nick
                short_to_nick[f"U{_counter}"] = nick
                _counter += 1

        # 构建 short → 昵称/QQ 映射（用于 LLM 输出 U 编号引用的归一化）
        short_map: dict[str, dict] = {
            short: {"nickname": short_to_nick[short], "qq": uid}
            for uid, short in uid_to_short.items()
        }

        # 构建人物映射表（U编号=昵称(QQ号)，目标用户行带 ← 目标用户 标记）
        # QQ 号是唯一锚点：昵称可能跨群不同/随时改名，绝不能仅凭昵称认人（Pitfall 87）
        target_short_set = {uid_to_short[str(qq)] for qq in target_qqs if str(qq) in uid_to_short}
        nick_map_lines = []
        for uid, short in uid_to_short.items():
            nick = nickname_map.get(uid, f"用户{uid}")
            marker = " ← 目标用户" if short in target_short_set else ""
            nick_map_lines.append(f"{short}={nick}({uid}){marker}")
        nick_map_header = "人物:\n" + "\n".join(nick_map_lines) + "\n\n"

        # 格式化所有消息（不合并，每条独立一行）
        _trunc = int(qa_params().get("msg_truncate_chars", 300))
        batch_lines: list[str] = []
        for i, row in enumerate(rows, 1):
            dt = datetime.fromtimestamp(row["created_at"])
            time_str = dt.strftime("%H:%M")
            content = (row["content"] or "")[:_trunc]
            # 消息内真实换行转义为字面 \n（一条消息一行契约，方案B）
            content = content.replace("\n", "\\n")
            uid = str(row["user_id"])
            short = uid_to_short[uid]
            batch_lines.append(f"#{i} {time_str} {short}: {content}")

        total = len(rows)
        is_multi = len(target_qqs) > 1

        # 构建分析对象描述
        if is_multi:
            user_descs = []
            for qq in target_qqs:
                nick = nickname_map.get(qq, f"用户{qq}")
                user_descs.append(f"{nick}（{qq}）")
            analysis_subject = " + ".join(user_descs)
        else:
            qq = target_qqs[0]
            nickname = nickname_map.get(qq, f"用户{qq}")
            analysis_subject = f"{nickname}（{qq}）"

        set_cooldown(_session_key(group_id, user_id))
        await send_reply(
            websocket, message_type, group_id,
            f"📊 正在分析 {analysis_subject} 近 {days} 天的聊天记录（含相关消息与上下文，共 {total} 条），请稍候...",
            user_id, reply_id,
        )
        logger.info(f"📊 开始用户分析: QQ={target_qqs}, 问题「{question}」, 共 {total} 条")

        # ================================================================
        # 分批处理 — 按累计 token 数分批（目标 map_batch_chars tokens/batch，qa 段热生效）
        # ================================================================
        _batch_chars = int(qa_params().get("map_batch_chars", 40000))
        chunks = chunk_messages_by_token(batch_lines, target_tokens=_batch_chars)
        # 将人物映射拼接到第一批
        if chunks:
            first_batch_text = "\n".join(chunks[0])
            chunks[0] = [nick_map_header + first_batch_text]
        total_batches = len(chunks)

        all_analysis_results: list[str] = []

        # 并行处理所有批次：各批次之间无依赖，可并发
        async def _process_analysis_batch(batch_num: int, chunk: list[str]) -> str:
            batch_text = "\n".join(chunk)
            logger.info(f"📊 分析批次 {batch_num}/{total_batches}...")

            # 提示词（qa_prompts 单一来源；单/多人共用 1 模板，user_scope 切换措辞；
            # 用户定制经 CONFIG 热生效）
            analysis_system_prompt = render_prompt("analysis_map_system")
            user_prompt = render_prompt("analysis_map_user", {
                "analysis_subject": analysis_subject,
                "question": question,
                "user_scope": "这些用户" if is_multi else "该用户",
                "days": days,
                "batch_num": batch_num,
                "total_batches": total_batches,
                "batch_text": batch_text,
            })

            # LLM 参数（qa.llm.analysis 段，默认=原硬编码行为）
            _a = qa_llm_scope("analysis")
            _common = qa_llm()
            reply = await call_llm(
                [{"role": "system", "content": analysis_system_prompt},
                 {"role": "user", "content": user_prompt}],
                max_tokens=int(_a.get("map_max_tokens", 131072)),
                parallel=True,
                temperature=float(_common.get("temperature", 0.7)),
                timeout=int(_common.get("timeout", 1800)),
                **thinking_kwargs(_a.get("map_thinking", "on")),
            )
            reply = reply.strip()

            if reply.startswith(("😵", "🔕")):
                logger.warning(f"⚠️ 分析批次 {batch_num} LLM 调用失败")
                save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                                    batch_num, total_batches, len(batch_text), "map", reply, is_valid=0)
                return ""

            if reply == "无相关信息":
                save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                                    batch_num, total_batches, len(batch_text), "map", reply)
                return ""
            # U 编号引用归一化为 昵称(qq号)
            normalized = _normalize_u_refs(reply, short_map)
            save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                                batch_num, total_batches, len(batch_text), "map", normalized)
            return normalized

        results = await asyncio.gather(*[_process_analysis_batch(i + 1, chunk) for i, chunk in enumerate(chunks)])
        all_analysis_results = [r for r in results if r]

        # ================================================================
        # 汇总阶段 — 如果有多批有结果的批次，进行汇总
        # ================================================================
        if not all_analysis_results:
            await send_reply(
                websocket, message_type, group_id,
                f"📊 在 {analysis_subject} 的近 {days} 天聊天记录中未找到与「{question}」相关的信息。",
                user_id, reply_id,
            )
            return

        # 统一走 Reduce 阶段，确保用户看到的是自然语言回答
        combined = "\n---\n".join(all_analysis_results)

        # 线索文本超长时按 map_batch_chars 长度驱动多级收敛
        # （2026-08-05 改造：与人设画像 _hierarchical_merge_by_len 同一策略，
        #   替代"所有线索一次性拼给最终融合"——输入变长后单次融合吃不下）
        # 2026-08-22：阈值/合并参数走 qa 段（热生效）；重试次数用 qa 侧独立
        # merge_retries（不动 persona._MAX_LLM_RETRIES，人设画像管线共用）
        _a = qa_llm_scope("analysis")
        _common = qa_llm()
        _merge_max_tokens = int(_a.get("merge_max_tokens", 16384))
        _merge_retries = int(_a.get("merge_retries", 5))
        if len(all_analysis_results) > 1 and len(combined) > _batch_chars:
            logger.info(f"📊 分析线索 {len(combined)} 字符 > 分批上限({_batch_chars})，启动多级收敛合并...")

            def _clue_ser(t: str) -> str:
                return t

            # 多级合并中间结果负编号序列（-1, -2, ...，与 Map 正编号区分）
            _merge_serial = [0]

            async def _clue_mg(group: list[str]) -> Optional[str]:
                group_text = "\n---\n".join(group)
                # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
                merge_system = render_prompt("analysis_merge_system")
                merge_user = render_prompt("analysis_merge_user", {"group_text": group_text})
                merge_result = ""
                for _a_try in range(1, _merge_retries + 1):
                    try:
                        reply = await _call_llm_net(
                            [{"role": "system", "content": merge_system},
                             {"role": "user", "content": merge_user}],
                            max_tokens=_merge_max_tokens,
                            temperature=float(_common.get("temperature", 0.7)),
                            timeout=int(_common.get("timeout", 1800)),
                            **thinking_kwargs(_a.get("merge_thinking", "on")),
                        )
                    except LLMNetworkExhausted as _ne:
                        logger.error(f"❌ 分析线索多级合并网络异常: {_ne}")
                        _merge_serial[0] += 1
                        save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                                            -_merge_serial[0], total_batches, len(group_text), "merge", "", is_valid=0)
                        return None
                    merge_result = reply.strip()
                    if merge_result and not merge_result.startswith(("😵", "🔕")):
                        break
                    logger.warning(f"🔄 分析线索多级合并 (attempt {_a_try}/{_merge_retries})，重试中...")
                else:
                    _merge_serial[0] += 1
                    save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                                        -_merge_serial[0], total_batches, len(group_text), "merge", "", is_valid=0)
                    return None
                _merge_serial[0] += 1
                save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                                    -_merge_serial[0], total_batches, len(group_text), "merge", merge_result)
                return merge_result

            converged = await _hierarchical_merge_by_len(
                all_analysis_results, _batch_chars, "分析线索", _clue_ser, _clue_mg)
            combined = "\n---\n".join(converged)
            logger.info(f"✅ 分析线索多级收敛合并完成，{len(combined)} 字符")

        # 注入分析对象的画像与人设（二次加工信息，仅供参考背景；线索才是原始消息）
        # 2026-08-08 用户要求：/分析 同时使用人设+画像作为输入，但必须注明是二次加工
        bg_parts = []
        for qq in target_qqs:
            qq_int = int(qq)
            nick = nickname_map.get(qq, f"用户{qq}")
            _persona = get_active_persona(qq_int, group_id or 0)
            _profile = get_user_profile(qq_int, group_id or 0)
            p_text = persona_to_text(_persona) if _persona else ""
            pf_text = (_profile or {}).get("profile", "") if _profile else ""
            if p_text or pf_text:
                block = f"【{nick}（{qq}）】"
                if pf_text:
                    block += f"\n画像：{pf_text}"
                if p_text:
                    block += f"\n人设：{p_text}"
                bg_parts.append(block)
        bg_context = ""
        if bg_parts:
            # 声明头（qa_prompts 单一来源，用户定制经 CONFIG 热生效）+ 每人块
            bg_context = (
                render_prompt("analysis_bg_header")
                + "\n\n"
                + "\n\n".join(bg_parts)
                + "\n\n"
            )

        # Reduce user（单/多人共用 1 模板：cite_rule 切换引用要求行；
        # bg_context 为空时零差异——模板 {bg_context} 后不额外换行，
        # 非空时其自身尾部 \n\n 承担分隔）
        _cite_rule = (
            "只引用 1-3 条最关键的消息作为佐证，明确标注来自哪个用户（通过 user_id 区分），不要罗列"
            if is_multi else
            "只引用 1-3 条最关键的消息作为佐证，自然地融进话里，不要罗列"
        )
        summary_user_prompt = render_prompt("analysis_reduce_user", {
            "question": question,
            "analysis_subject": analysis_subject,
            "bg_context": bg_context,
            "combined": combined,
            "cite_rule": _cite_rule,
        })

        _a = qa_llm_scope("analysis")
        _common = qa_llm()
        summary = await call_llm(
            [{"role": "system", "content": render_prompt("analysis_reduce_system")},
             {"role": "user", "content": summary_user_prompt}],
            max_tokens=int(_a.get("reduce_max_tokens", 16384)),
            temperature=float(_common.get("temperature", 0.7)),
            timeout=int(_common.get("timeout", 1800)),
            **thinking_kwargs(_a.get("reduce_thinking", "on")),
        )
        summary = summary.strip()
        # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
        summary = _normalize_u_refs(summary, short_map)

        # 最终答案落库（batch_index=0 标识 reduce 结果）
        save_analysis_batch(group_id, run_id, "+".join(target_qqs), question, days,
                            0, total_batches, len(combined), "reduce", summary)

        await send_reply(websocket, message_type, group_id,
                         f"📊 {summary}", user_id, reply_id)
        logger.info(f"✅ 用户分析结果已发送 (QQ={target_qqs}, {total} 条记录, {total_batches} 批次)")
    except ConnectionClosed:
        logger.warning("WebSocket 已断开，分析结果未发送")
    except Exception as e:
        logger.error(f"用户分析出错: {e}")
        await send_reply(websocket, message_type, group_id,
                         f"📊 分析出错: {e}", user_id, reply_id)


# ============================================================
#  群像分析（基于本群所有人设回答问题）
# ============================================================
async def handle_group_persona(
    websocket, message_type: str, group_id: int,
    user_id: Optional[int], reply_id: Optional[int],
    question: str
) -> None:
    """
    /群像 <问题>：基于本群所有用户的人设 + 画像数据回答关于群友的问题。
    处理流程：获取人设和画像 → 格式化为文本 → LLM 基于数据回答问题。
    """
    from .persona import persona_to_text

    # LLM 总开关早退（2026-08-21 审计）：群像分析基于 LLM 回答
    if not llm_enabled():
        await send_reply(websocket, message_type, group_id,
                         "🔕 LLM 总开关关闭，暂时无法群像分析（GUI 总览页 LLM 板块可开启）",
                         user_id, reply_id)
        return

    set_cooldown(_session_key(group_id, user_id or 0))

    # 获取本群所有人的正式人设 + 用户画像（合并输入，覆盖面更全）
    personas, fallback_used = get_group_personas_with_profiles(group_id)

    if not personas:
        await send_reply(websocket, message_type, group_id,
                         "👥 本群暂无用户人设/画像数据，请先使用 /更新人设 或 /更新全部人设 建立人设。", user_id, reply_id)
        return

    # 过滤已退群成员：只统计当前仍在群里的人（2026-08-08 用户要求）
    # 退群后建立的人设/画像不应再参与群像统计
    try:
        members_raw = await _call_onebot_api(
            websocket, "get_group_member_list", {"group_id": group_id}
        )
        if members_raw:
            member_ids = {str(m["user_id"]) for m in members_raw}
            before = len(personas)
            personas = [p for p in personas if str(p["user_id"]) in member_ids]
            removed = before - len(personas)
            if removed > 0:
                logger.info(f"👥 群像过滤退群成员: 移除 {removed} 人（{before} → {len(personas)}）")
        # members_raw 为空（API 失败）时不过滤，保持原行为
    except Exception as e:
        logger.warning(f"👥 群像获取群成员列表失败，跳过退群过滤: {e}")

    if not personas:
        await send_reply(websocket, message_type, group_id,
                         "👥 当前群的在群成员中没有可统计的人设/画像数据（可能有数据的人已退群）。", user_id, reply_id)
        return

    # 将人设 + 画像格式化为文本（两者都有时都提供，标注来源）
    def _format_user_block(p: dict) -> str:
        nick = p["nickname"]
        uid = p["user_id"]
        parts = []
        if p.get("persona"):
            text = persona_to_text(p["persona"])
            if text:
                parts.append(f"【人设】\n{text}")
        if p.get("profile"):
            parts.append(f"【画像】\n{p['profile']}")
        if not parts:
            return ""
        return f"【{nick}（{uid}）】\n" + "\n\n".join(parts)

    all_persona_text_parts = [b for b in (_format_user_block(p) for p in personas) if b]

    combined = "\n\n".join(all_persona_text_parts)

    # 发送开始消息
    set_cooldown(_session_key(group_id, user_id or 0))
    await send_reply(
        websocket, message_type, group_id,
        f"👥 正在分析 {len(personas)} 位群友的人设和画像数据，请稍候...",
        user_id, reply_id,
    )
    logger.info(f"👥 开始群像分析: 群={group_id}, 问题「{question}」, 共 {len(personas)} 位群友（人设+画像）")

    # 如果数据文本太长（> group_persona_map_threshold 字符，qa 段热生效），
    # 使用 Map→Reduce 分批策略
    _gp = qa_llm_scope("group_persona")
    _gp_common = qa_llm()
    _gp_threshold = int(qa_params().get("group_persona_map_threshold", 15000))
    if len(combined) > _gp_threshold:
        # 分批 Map 阶段
        _gp_batch_chars = int(qa_params().get("map_batch_chars", 40000))
        chunks = chunk_messages_by_token(
            [_format_user_block(p) for p in personas],
            target_tokens=_gp_batch_chars
        )
        total_batches = len(chunks)

        all_clues: list[str] = []

        async def _process_group_image_batch(batch_num: int, chunk: list[str]) -> Optional[str]:
            batch_text = "\n\n".join(chunk)

            logger.info(f"👥 群像分析批次 {batch_num}/{total_batches}...")

            # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
            map_system = render_prompt("group_persona_map_system")
            user_prompt = render_prompt("group_persona_map_user", {
                "question": question,
                "batch_num": batch_num,
                "total_batches": total_batches,
                "batch_text": batch_text,
            })

            reply = await call_llm(
                [{"role": "system", "content": map_system},
                 {"role": "user", "content": user_prompt}],
                max_tokens=int(_gp.get("map_max_tokens", 131072)),
                temperature=float(_gp_common.get("temperature", 0.7)),
                timeout=int(_gp_common.get("timeout", 1800)),
                **thinking_kwargs(_gp.get("map_thinking", "on")),
            )
            reply = reply.strip()

            if reply.startswith(("😵", "🔕")):
                logger.warning(f"⚠️ 群像分析批次 {batch_num} LLM 调用失败")
                return None

            if reply == "无相关信息":
                return None

            return reply

        # Map 阶段：gather 并发提交，由 call_llm 并行信号量/串行队列统一限流
        map_results = await asyncio.gather(
            *[_process_group_image_batch(i + 1, chunk) for i, chunk in enumerate(chunks)]
        )
        all_clues = [r for r in map_results if r]

        # Reduce 阶段
        if not all_clues:
            await send_reply(
                websocket, message_type, group_id,
                f"👥 在本群 {len(personas)} 位群友的人设/画像数据中未找到与「{question}」相关的信息。",
                user_id, reply_id,
            )
            return

        combined_clues = "\n---\n".join(all_clues)

        summary = await call_llm(
            [{"role": "system", "content": render_prompt("group_persona_reduce_system")},
             {"role": "user", "content": render_prompt("group_persona_reduce_user_map", {
                 "question": question,
                 "combined_clues": combined_clues,
             })}],
            max_tokens=int(_gp.get("reduce_max_tokens", 16384)),
            temperature=float(_gp_common.get("temperature", 0.7)),
            timeout=int(_gp_common.get("timeout", 1800)),
            **thinking_kwargs(_gp.get("reduce_thinking", "on")),
        )
        summary = summary.strip()

    else:
        # 人设文本较短，直接调用 LLM
        summary = await call_llm(
            [{"role": "system", "content": render_prompt("group_persona_reduce_system")},
             {"role": "user", "content": render_prompt("group_persona_reduce_user_direct", {
                 "personas_count": len(personas),
                 "combined": combined,
                 "question": question,
             })}],
            max_tokens=int(_gp.get("reduce_max_tokens", 16384)),
            temperature=float(_gp_common.get("temperature", 0.7)),
            timeout=int(_gp_common.get("timeout", 1800)),
            **thinking_kwargs(_gp.get("reduce_thinking", "on")),
        )
        summary = summary.strip()

    await send_reply(websocket, message_type, group_id,
                     f"👥 {summary}", user_id, reply_id)
    logger.info(f"✅ 群像分析结果已发送 (群={group_id}, {len(personas)} 位群友)")


# ============================================================
#  群聊迁移
# ============================================================
async def _handle_migrate_group(
    websocket, message_type: str, group_id: int,
    user_id: int, reply_id: Optional[int], new_group_id: int
) -> None:
    """
    将当前群中未加入目标群的用户邀请到目标群。
    通过 OneBot 11 API 获取两个群的完整成员列表，对比后逐个发送入群邀请。
    """
    bot_qq = get_bot_uin()  # 08-22：从连接派生

    # ---- 获取当前群成员列表 ----
    current_members_raw = await _call_onebot_api(
        websocket, "get_group_member_list", {"group_id": group_id}
    )
    if not current_members_raw:
        await send_reply(websocket, message_type, group_id,
            f"⚠️ 获取当前群 {group_id} 成员列表失败，请确认 Bot 已在该群", user_id, reply_id)
        return

    # ---- 获取目标群成员列表 ----
    target_members_raw = await _call_onebot_api(
        websocket, "get_group_member_list", {"group_id": new_group_id}
    )
    if not target_members_raw:
        await send_reply(websocket, message_type, group_id,
            f"⚠️ 获取群 {new_group_id} 成员列表失败，请确认群号正确且 Bot 已在该群", user_id, reply_id)
        return

    # 构建成员字典和集合（get_group_member_list 返回扁平列表）
    current_members = {
        str(m["user_id"]): m.get("card") or m.get("nickname", str(m["user_id"]))
        for m in current_members_raw
    }
    target_member_ids = {str(m["user_id"]) for m in target_members_raw}

    # 筛选需要邀请的用户
    to_invite = []
    already_in = 0
    for uid, nickname in current_members.items():
        if uid in target_member_ids:
            already_in += 1
        elif uid == bot_qq:
            continue  # 跳过 Bot 自己
        else:
            to_invite.append((int(uid), nickname))

    if not to_invite:
        await send_reply(websocket, message_type, group_id,
            f"✅ 当前群所有 {len(current_members)} 位成员都已在群 {new_group_id} 中了", user_id, reply_id)
        return

    # 发送邀请进度
    set_cooldown(_session_key(group_id, user_id))
    est_minutes = (len(to_invite) * 30 + 59) // 60
    await send_reply(websocket, message_type, group_id,
        f"🔄 开始迁移到群 {new_group_id}...\n"
        f"📊 当前群 {len(current_members)} 人，已在目标群 {already_in} 人，"
        f"需邀请 {len(to_invite)} 人\n"
        f"⏳ 风控间隔 30 秒/人，预计耗时约 {est_minutes} 分钟，请耐心等待",
        user_id, reply_id)
    logger.info(f"🔄 迁移群聊: {group_id} → {new_group_id}, 需邀请 {len(to_invite)} 人")

    # 逐个发送私聊邀请通知（群卡片消息，对方点击可直接申请入群）
    invited = 0
    failed = 0
    for uid, nickname in to_invite:
        try:
            # 群卡片（contact 消息段）：点击直接申请加入，比纯文本"搜索群号"体验好
            # 方案A（2026-08-23）：统一发送出口（发送门控单点判定），segments 直接传
            invite_segments = [
                {
                    "type": "text",
                    "data": {
                        "text": f"📢 来自群 {group_id} 的备份群邀请！\n\n"
                                f"群 {group_id} 的备份群已建立，点击下方卡片即可加入备份群 {new_group_id}："
                    }
                },
                {
                    "type": "contact",
                    "data": {"type": "group", "id": new_group_id},
                },
                {
                    "type": "text",
                    "data": {"text": "\n如无需加入可忽略此消息。"}
                },
            ]
            # 等待 NapCat 响应：发送失败（对方关私聊/防骚扰/风控）能真实计入 failed
            result = await send_segments(websocket, "private", uid,
                                         invite_segments, wait_response=True)
            if result is None:
                # None = 超时或明确失败（retcode 非 0 时 _call_onebot_api 也返回 None？见实现：仅取 data）
                failed += 1
                logger.warning(f"迁移通知发送失败: {nickname}({uid})")
            else:
                invited += 1
                logger.info(f"📩 已发送迁移私聊通知: {nickname}({uid})")
        except Exception as e:
            logger.error(f"发送迁移通知给 {nickname}({uid}) 失败: {e}")
            failed += 1

        # 风控间隔 30s/人（用户要求：最大限度降低批量私聊风控风险）
        await asyncio.sleep(30.0)

    # 发送结果汇总
    await send_reply(websocket, message_type, group_id,
        f"✅ 迁移邀请已发送完毕！\n"
        f"📊 已成功发出 {invited} 个邀请，失败 {failed} 个\n"
        f"📌 邀请列表：\n" +
        "\n".join(f"  • {name} (QQ: {uid})" for uid, name in to_invite),
        user_id, reply_id)


# ============================================================
#  消息处理
# ============================================================
# ---- 复读+1（2026-08-12）：集群同时启用总结+评选时，连续 3 条相同纯文本消息 bot 跟一条 ----
_repeat_queue: dict[int, list] = {}
_repeat_cooldown: dict[int, float] = {}
_REPEAT_TRIGGER = 3          # 连续 N 条相同触发
_REPEAT_COOLDOWN = 60        # 同群触发冷却（秒）
_REPEAT_MAX_LEN = 200        # 超长文本不跟


async def _maybe_echo_repeat(websocket, group_id: int, content: str, msg_kind: str = "text"):
    """复读检测：连续 3 条相同纯文本 → bot 发送相同内容（模拟 +1）。

    仅限集群中同时启用 enable_summary + enable_evaluation 的群；
    非文本消息（图/音/影/文件/转发，08-21 统一 msg_kind）/命令/超长消息不参与。
    bot 自己的消息在 handle_message 入口被忽略，不会进入队列，因此不会自我触发。
    08-23：新增全局开关 bot.echo_repeat（GUI「其他设置」弹窗管理，关闭时完全不触发）。
    """
    if not CONFIG.get("BOT_ECHO_REPEAT", True):
        return
    if msg_kind != "text":
        return
    text = re.sub(r"\[[^\]]*\]", "", content or "").strip()
    if not text or text.startswith("/") or len(text) > _REPEAT_MAX_LEN:
        return
    # 仅集群且总结+评选同时开启的群触发；不在集群/未开启任务 → get_group_task_flags 为 None 或 flag=0
    try:
        from .database import get_group_task_flags
        flags = get_group_task_flags(group_id)
    except Exception:
        return
    if not flags or not flags.get("summary") or not flags.get("evaluation"):
        return
    # 冷却防刷（触发后 60s 内同群不再复读——不要连续+1）
    now = time.time()
    if now - _repeat_cooldown.get(group_id, 0) < _REPEAT_COOLDOWN:
        return
    q = _repeat_queue.setdefault(group_id, [])
    # 2026-08-13 新规则：不同消息重置队列（连续相同计数）
    if q and q[-1] != text:
        q.clear()
    q.append(text)
    if len(q) > 10:
        q.pop(0)
    # 概率触发：连续 3 条相同 = 60%，之后每多一条 +20%（cap 100%）
    streak = len(q)
    if streak >= _REPEAT_TRIGGER:
        prob = min(0.6 + (streak - _REPEAT_TRIGGER) * 0.2, 1.0)
        if random.random() < prob:
            _repeat_cooldown[group_id] = now
            q.clear()  # 清队列：bot 消息不计数，防"2 条残留+1 条"二次触发
            await send_reply(websocket, "group", group_id, text)
            logger.info(f"🔁 复读+1: 群={group_id} 内容={text[:30]} 连续{streak}条 概率{prob:.0%}")


async def _maybe_mimic_ghost(websocket, group_id: int) -> None:
    """赛博模仿（2026-08-13）：全局总闸+群级开关 enable_mimic=1 的群，
    用户聊天时按全局概率触发——模仿群里【最后一次发言离现在最远】的用户
    （最久没说话的），格式"赛博{昵称}：{内容}"。
    2026-08-16 用户配置：概率调为 0%，功能停用（代码保留）。
    2026-08-22：开关+概率从硬编码改为全局配置（总览页「⚙️ 配置面板」
    scheduler.mimic_enabled / scheduler.mimic_probability，热生效）。"""
    try:
        if not group_id:
            return
        # ---- 全局总闸（2026-08-22）：scheduler.mimic_enabled（默认关，
        #      保持 08-16 停用状态）----
        if not CONFIG.get("SCHED_MIMIC_ENABLED", False):
            return
        from core.database import get_group_task_flags
        flags = get_group_task_flags(group_id)
        if not flags or not flags.get("mimic"):
            return
        # 触发概率：全局配置 %（scheduler.mimic_probability，默认 0=停用）
        prob = max(0.0, min(100.0, float(CONFIG.get("SCHED_MIMIC_PROBABILITY", 0)))) / 100.0
        if prob <= 0.0 or random.random() >= prob:
            return
        # 找全体用户（2026-08-13 用户配置：取消 6h 未发言条件——全体用户中随机抽取）
        # ——排除黑名单/bot、采样足够（≥30 条）
        from core.database import get_db
        from games.mimic import _get_user_messages, _MIN_SAMPLES, MIMIC_BLACKLIST
        bot_qq = int(get_bot_uin() or 0)  # 08-22：从连接派生
        _exclude = MIMIC_BLACKLIST | {bot_qq}
        _ph = ",".join("?" for _ in _exclude)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id, nickname, MAX(created_at) as last_ts FROM message_archive "
                f"WHERE target_id=? AND user_id NOT IN ({_ph}) AND content IS NOT NULL AND content != '' "
                "GROUP BY user_id",
                (group_id, *_exclude),
            ).fetchall()
        # 获取当前群成员（2026-08-13：只模仿还在群里的用户——NapCat HTTP API；
        # 获取失败则跳过（退群用户模仿风险 > 不触发损失））
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8) as _hc:
                _resp = await _hc.get(
                    "http://127.0.0.1:3000/get_group_member_list",
                    params={"group_id": group_id},
                )
                _resp.raise_for_status()
                _mdata = _resp.json()
            _members = _mdata.get("data") if isinstance(_mdata, dict) else None
            current_members = {int(m.get("user_id")) for m in _members if m.get("user_id")} if isinstance(_members, list) else set()
            if not current_members:
                logger.warning(f"👻 赛博模仿跳过：群 {group_id} 成员列表为空")
                return
        except Exception as _e:
            logger.warning(f"👻 赛博模仿跳过：群 {group_id} 成员获取失败: {_e}")
            return
        # 采样过滤后随机抽取（只限当前群成员）
        candidates = [
            r for r in rows
            if r["user_id"] in current_members
            and len(_get_user_messages(r["user_id"], group_id)) >= _MIN_SAMPLES
        ]
        if not candidates:
            logger.info(f"👻 赛博模仿跳过：群 {group_id} 无满足条件用户（当前成员且采样≥{_MIN_SAMPLES}）")
            return
        row = random.choice(candidates)  # 全体用户随机抽取（2026-08-13）
        # 生成模仿发言
        from games.mimic import generate_mimic_reply
        reply = await generate_mimic_reply(row["nickname"], row["user_id"], group_id, "")
        if not reply:
            return
        await send_reply(websocket, "group", group_id, f"赛博{row['nickname']}：{reply}", None, None)
        logger.info(f"👻 赛博模仿触发: 赛博{row['nickname']} → {reply[:50]}")
    except Exception as e:
        logger.error(f"👻 赛博模仿异常: {e}", exc_info=True)


def _msg_receive_gate(message, message_type: str) -> tuple[bool, set, str]:
    """消息管理：接收门控（总开关 + 范围 + 类型子开关）。

    返回 (allowed, allowed_media, reason)：
      - allowed: 是否接收该消息
      - allowed_media: 允许存档下载的类型集合 {"image","voice","video","file","forward"}
                       （接收的消息中，未勾选类型的媒体跳过下载，
                       08-21 起媒体存档表写 skipped 行保留 URL）
      - reason: 拒绝原因（放行时为空串）

    类型归类（08-21 扩 6 类）：image / voice+record / video / file / forward 为媒体，
    各有独立接收开关（MSG_RECEIVE_FILE / MSG_RECEIVE_FORWARD）；
    其余段（text/at/face/dice/卡片…）归为"文字"。
    含 reply 段的消息不算文字（纯引用无内容）。
    """
    if not CONFIG.get("MSG_RECEIVE_ENABLED", True):
        return False, set(), "接收总开关已关闭"
    scope = str(CONFIG.get("MSG_RECEIVE_SCOPE", "all")).lower()
    if scope == "group" and message_type != "group":
        return False, set(), "接收范围=仅群消息"
    if scope == "private" and message_type != "private":
        return False, set(), "接收范围=仅私聊"

    def _media_ok(t: str) -> bool:
        return bool(CONFIG.get(f"MSG_RECEIVE_{t.upper()}", True))

    has_text = False
    media_present: set = set()
    if isinstance(message, list):
        for seg in message:
            t = seg.get("type", "")
            if t == "image":
                media_present.add("image")
            elif t in ("voice", "record"):
                media_present.add("voice")
            elif t == "video":
                media_present.add("video")
            elif t == "file":
                media_present.add("file")
            elif t == "forward":
                media_present.add("forward")
            elif t in ("at", "reply"):
                continue
            else:
                has_text = True
    else:
        # CQCode 兜底格式：按消息里出现的 CQ 码归类。
        # 媒体：image/voice/record/video/file/forward（08-21 扩 6 类）；
        # 其余 CQ 码（表情/骰子/卡片…）和 CQ 码之外的文字都算"文字内容"
        s = str(message)
        non_media = re.sub(r"\[CQ:(?:image|voice|record|video|file|forward)[^\]]*\]", "", s)
        has_text = bool(re.sub(r"\[CQ:[^\]]*\]", "", non_media).strip()) or \
                   bool(re.search(r"\[CQ:(?!image|voice|record|video|file|forward)[^\]]*\]", s))
        if "[CQ:image," in s:
            media_present.add("image")
        if "[CQ:voice," in s or "[CQ:record," in s:
            media_present.add("voice")
        if "[CQ:video," in s:
            media_present.add("video")
        if "[CQ:file," in s:
            media_present.add("file")
        if "[CQ:forward," in s:
            media_present.add("forward")

    allowed_media = {t for t in media_present if _media_ok(t)}
    blocked_media = media_present - allowed_media

    if has_text:
        if not CONFIG.get("MSG_RECEIVE_TEXT", True):
            # 文字关：纯文字消息拒绝；文字+媒体消息仅放行媒体部分
            if not allowed_media:
                return False, set(), "文字消息接收已关闭"
            return True, allowed_media, ""
        return True, allowed_media, ""

    # 纯媒体消息：至少一个媒体类型开启才接收
    if not allowed_media:
        return False, set(), "该类媒体消息接收已关闭"
    return True, allowed_media, ""


async def handle_message(websocket, msg: dict):
    post_type = msg.get("post_type")

    # ---- API 调用结果（没有 post_type） ----
    if post_type is None:
        retcode = msg.get("retcode")
        message = msg.get("message", "")
        # 忽略 NapCat 4.x 无害错误
        if retcode == 1404 and "不支持的Api" in message:
            return
        action = msg.get("action")
        echo = msg.get("echo")
        logger.debug(f"API 响应: action={action}, retcode={retcode}, echo={echo}")
        
        # 检查是否有等待响应的 Event（OneBot API 调用机制）
        if echo in _api_responses:
            response = _api_responses[echo]
            response["data"] = msg.get("data")
            response["event"].set()
            return
        
        return

    # ---- 心跳 / 生命周期 ----
    if post_type == "meta_event":
        meta_type = msg.get("meta_event_type")
        if meta_type == "heartbeat":
            return  # 无需回复
        if meta_type == "lifecycle" and msg.get("sub_type") == "connect":
            logger.info("NapCat 生命周期: connect")
        return

    # ---- 好友申请（自动通过） ----
    if post_type == "request":
        request_type = msg.get("request_type", "")
        if request_type == "friend":
            user_id = msg.get("user_id")
            comment = msg.get("comment", "")
            flag = msg.get("flag")
            logger.info(f"📨 好友申请: user_id={user_id}, comment={comment}")
            # 自动通过好友申请（08-23：受 bot.auto_approve_friend 全局开关控制，
            # GUI「其他设置」弹窗可关；关闭时申请原样保留等手动处理）
            if not CONFIG.get("BOT_AUTO_APPROVE_FRIEND", True):
                logger.info(f"🔕 好友申请自动通过已关闭，忽略: {user_id}")
                return
            await websocket.send(
                json.dumps({
                    "action": "set_friend_add_request",
                    "params": {
                        "flag": flag,
                        "approve": True,
                    },
                })
            )
            logger.info(f"✅ 已自动通过好友申请: {user_id}")
            return
        return

    # ---- 通知事件（撤回、戳一戳 等） ----
    if post_type == "notice":
        notice_type = msg.get("notice_type", "")
        # 临时调试日志（定位拍一拍不触发问题，定位后移除）
        logger.info(f"📡 notice 事件: type={notice_type}, raw={json.dumps(msg, ensure_ascii=False)[:300]}")
        # 入群/退群通知（2026-08-10）：群开关 enable_member_notify 开启时私聊通知管理员
        if notice_type in ("group_increase", "group_decrease"):
            await _handle_member_change_notify(websocket, msg, notice_type)
            return
        # 撤回消息
        if notice_type == "group_recall" or notice_type == "friend_recall":
            # OneBot 11: message_id, operator_id, user_id 在顶层
            # 兼容 data 嵌套格式作为回退
            recall_data = msg.get("data", {})
            message_id = msg.get("message_id", recall_data.get("message_id", 0))
            operator_id = msg.get("operator_id", recall_data.get("operator_id", 0))
            sender_id = msg.get("user_id", recall_data.get("user_id", recall_data.get("sender_id", 0)))

            if notice_type == "group_recall":
                target_id = msg.get("group_id", 0)
                mtype = "group"
            else:
                target_id = sender_id
                mtype = "private"

            logger.info(f"[RECALL] notice_type={notice_type}, message_id={message_id}, operator_id={operator_id}, sender_id={sender_id}, target_id={target_id}")

            if message_id and target_id:
                # 先记录撤回元数据
                archive_recall(message_id, operator_id, mtype, target_id, sender_id)
                # 异步下载撤回的图片到独立目录
                asyncio.create_task(_safe_task(_archive_recall_images(message_id, target_id, mtype, sender_id)))
            return

        # 拍一拍：真心话大冒险游戏中时，拍 bot 自动触发 /下一轮（2026-08-07）
        # 2026-08-12：拍自己（拍别人不算）同样触发 /下一轮
        # OneBot 11 规范：群内戳一戳 = notice_type="notify" + sub_type="poke"（不是 notice_type="poke"！）
        if notice_type == "poke" or (notice_type == "notify" and msg.get("sub_type") == "poke"):
            poke_data = msg.get("data", {})
            poke_target = msg.get("target_id", poke_data.get("target_id", 0))
            poke_user = msg.get("user_id", poke_data.get("user_id", 0))
            if not poke_user:
                return
            is_bot_poke = str(poke_target) == get_bot_uin()  # 08-22：从连接派生
            is_self_poke = str(poke_target) == str(poke_user)  # 拍自己
            if not is_bot_poke and not is_self_poke:
                return  # 拍的不是 bot 也不是自己（拍别人不算）
            poke_group = msg.get("group_id", poke_data.get("group_id", 0))
            if not poke_group:
                return
            if not entertainment.is_td_active(poke_group):
                return  # 游戏未进行时不触发
            logger.info(f"👋 拍一拍触发 /下一轮: 群={poke_group}, 用户={poke_user}, 类型={'拍bot' if is_bot_poke else '拍自己'}")
            try:
                # 复用完整 /下一轮 链路（含自动模式异步出题、10s 冷却防抖）
                from .database import _get_user_nickname
                poke_nickname = _get_user_nickname(poke_user, poke_group)
                reply, at_uids = await asyncio.to_thread(
                    entertainment.check_command, "/下一轮",
                    group_id=poke_group, user_id=poke_user, nickname=poke_nickname,
                )
            except Exception as e:
                logger.error(f"拍一拍触发 /下一轮 失败: {e}")
                return
            if reply:
                await send_reply(websocket, "group", poke_group, reply, poke_user, None,
                                 at_user_ids=at_uids if at_uids else None)
            return
        return

    # ---- 只处理 message 事件 ----
    if post_type != "message":
        return

    message_type = msg.get("message_type", "")
    sender = msg.get("sender", {})
    user_id = msg.get("user_id")
    group_id = msg.get("group_id")
    bot_qq = get_bot_uin()  # 08-22：从连接派生

    # 忽略自己的消息
    if str(user_id) == bot_qq:
        return

    # 屏蔽名单检查 — 被屏蔽用户的 @ 消息静默忽略
    if user_id and is_blocked(user_id):
        return

    # 群聊优先使用群昵称（card），私聊使用 QQ 昵称
    nickname = sender.get("nickname", str(user_id))
    if message_type == "group":
        card = sender.get("card")
        if card:
            nickname = card

    # ---- 解析消息内容 ----
    # NapCat 配置为 ArrayMessage 格式，优先尝试列表格式
    raw_message = msg.get("raw_message", "")
    message = msg.get("message", "")

    is_at: bool
    clean_text: str
    reply_id: Optional[int]

    if isinstance(message, list):
        # ArrayMessage 格式
        is_at, clean_text, reply_id = parse_array_message(message, bot_qq)
    elif isinstance(message, str):
        # CQCode 字符串格式（兜底）
        is_at, clean_text, reply_id = parse_cqcode_message(raw_message or message, bot_qq)
    else:
        logger.warning(f"未知的 message 类型: {type(message)}")
        return

    # ---- 会话键（按用户隔离） ----
    session_key = _session_key(group_id, user_id)

    # ---- 消息管理：接收门控（总开关/范围/类型子开关，2026-08-20）----
    # 拒绝的消息不存档、不进会话历史、不触发任何后续处理
    _recv_ok, _allowed_media, _recv_reason = _msg_receive_gate(message, message_type)
    if not _recv_ok:
        logger.debug(f"🚫 消息接收门控: 群{group_id or '私聊'} 用户{user_id} — {_recv_reason}")
        return

    # ---- 永久存档（群消息 + 私聊消息全部存档，不清理） ----
    message_id = msg.get("message_id", 0)
    target_id = group_id if message_type == "group" else user_id
    has_image = False
    has_voice = False

    # 提取媒体 URL（08-21：标志位按 raw 如实记录，与下载开关脱钩——
    # "消息带不带媒体" 与 "开关允许不允许下载" 是两回事）
    image_urls: list = []
    voice_urls: list = []
    forward_items: list = []
    video_urls: list = []
    if isinstance(message, list):
        image_urls = extract_image_urls(message)
        has_image = len(image_urls) > 0
        voice_urls = extract_voice_urls(message)
        has_voice = len(voice_urls) > 0
        forward_items = extract_forward_ids(message)
        video_urls = extract_video_urls(message)
    elif isinstance(message, str):
        image_urls = []
        voice_urls = []
        forward_items = []
        video_urls = []

    # 统一消息类型（08-21）：从 raw_message 的 CQ 标记派生，唯一真相源
    msg_kind = derive_msg_kind(raw_message)

    # 保存到永久存档表
    if message_id and target_id:
        archive_message(message_id, message_type, target_id,
                        user_id, nickname, clean_text, raw_message, has_image, has_voice,
                        msg_kind=msg_kind)

    # 异步存档图片（不阻塞消息处理）
    # 08-21：未勾选类型不丢弃 URL，写 skipped 行保留 URL 供事后补下
    img_allowed = "image" in _allowed_media
    for img_url in image_urls:
        asyncio.create_task(_safe_task(
            archive_image(message_id, message_type, target_id, user_id, nickname, img_url,
                          allowed=img_allowed)
        ))

    # 异步存档语音（不阻塞消息处理）
    voice_allowed = "voice" in _allowed_media
    for voice_url in voice_urls:
        asyncio.create_task(_safe_task(
            archive_voice(message_id, message_type, target_id, user_id, nickname, voice_url,
                          allowed=voice_allowed)
        ))

    # 异步存档聊天记录转发（拉取 get_forward_msg 并递归解析入库）
    # 08-21：转发开关关闭时跳过（与 image/voice 的 allowed 门控一致，
    # 修复此前"混合消息转发开关关了仍拉取存档"的缺口）
    if "forward" in _allowed_media:
        for fwd in forward_items:
            fwd_id = fwd.get("id", "")
            if fwd_id:
                asyncio.create_task(_safe_task(
                    archive_forward(message_id, message_type, target_id, user_id, nickname,
                                    fwd_id, created_at=msg.get("time"), embedded_content=fwd.get("content"))
                ))

    # 异步记录视频（仅 URL 入库，不下载）
    # 08-21：视频开关关闭时跳过（同上）
    if "video" in _allowed_media:
        for video_url in video_urls:
            asyncio.create_task(_safe_task(
                archive_video(message_id, message_type, target_id, user_id, nickname, video_url)
            ))

    # ---- 群消息缓存（所有群消息都记录，用于 /评选）----
    # 已合并到 archive_message 中，不再单独调用 cache_group_message

    # ---- 真心话大冒险：普通发言也算回答，清除未回答计数（自选模式记录赢家发言） ----
    # Bug 修复：海龟汤活跃时，@Bot 提问不应被 acknowledge_answer 拦截
    # Bug 修复：/ 开头的指令不应算作"回答"（否则输家发 /下一轮 会把自己未回答计数清零）
    if message_type == "group" and group_id:
        if not (turtle_soup.is_active(group_id) and is_at) and not clean_text.startswith("/"):
            ack_reply, ack_at = entertainment.acknowledge_answer(user_id, group_id, clean_text)
            if ack_reply is not None:
                set_cooldown(session_key)
                await send_reply(websocket, message_type, group_id, ack_reply, user_id, reply_id, at_user_ids=ack_at if ack_at else None)
                logger.info(f"🎮 娱乐交互: {clean_text[:30]}")
                return

    # ---- 赛博模仿（2026-08-13）：enable_mimic=1 的群，用户聊天 1% 概率触发 ----
    # 模仿最久未发言用户（6 小时内都有发言则跳过）——异步不阻塞消息处理
    if message_type == "group" and group_id and clean_text and not clean_text.startswith("/"):
        asyncio.create_task(_safe_task(_maybe_mimic_ghost(websocket, group_id)))

    # ---- 群消息：@触发 或 / 开头指令 ----
    if message_type == "group":
        if not group_id:
            return
        # ---- 复读+1（2026-08-12）：集群同时启用总结+评选时，连续 3 条相同纯文本消息 bot 跟一条 ----
        # 08-21：改读 msg_kind（非文本消息不参与，含图/音/影/文件/转发）
        await _maybe_echo_repeat(websocket, group_id, clean_text, msg_kind)
        # 猜老婆游戏中，单个字母 A-F 也视为答题（无需 @bot）
        # 卧底游戏沉浸阶段（发言/投票/PK/白板猜词）也允许普通消息通过
        # 投票进行中：普通消息可能包含选项文字，视为投票
        if not is_at and not clean_text.startswith("/"):
            # 纯媒体消息（图片/语音/表情等）不参与游戏答题，也不触发 AI 对话——
            # 否则谐音梗等游戏活跃时，用户发图会穿透到 AI 对话
            # （clean_text 为 "[图片]" 等标记；去掉 [xxx] 标记后无实质内容即视为纯媒体）
            if not re.sub(r"\[[^\]]*\]", "", clean_text).strip():
                return
            spy_immersive = (
                game_spy.is_active(group_id)
                and game_spy.get_phase(group_id)
                in {"speaking", "voting", "pk_speaking", "pk_voting", "blank_guessing"}
            )
            vote_active = group_vote.is_active(group_id)
            # 投票进行中：普通消息可能包含选项文字，视为投票
            pun_active = pun_game.is_active(group_id)
            # 海龟汤游戏：只在 @bot 时拦截提问（非 / 开头的 @bot 消息）
            # 注意：/ 开头的指令已在上方 handle_command 中被处理
            soup_active = turtle_soup.is_active(group_id)
            if not (
                guess_wife.is_active(group_id)
                and len(clean_text) == 1
                and clean_text.upper() in ("A", "B", "C", "D", "E", "F")
            ) and not spy_immersive and not vote_active and not pun_active and not (soup_active and is_at):
                return
        if not clean_text:
            # 纯 @无文字
            return

        # Bug #17 修复：游戏活跃阶段豁免冷却（投票/发言等需要快速响应）
        # Bug 修复：海龟汤活跃时，@Bot 提问也跳过冷却检查
        if group_id and game_spy.is_active(group_id):
            pass  # 跳过冷却检查，让游戏消息直接通过
        elif turtle_soup.is_active(group_id) and is_at:
            pass  # 海龟汤提问跳过冷却
        elif is_on_cooldown(session_key):
            return

        logger.info(f"群 {group_id} | {nickname}({user_id}): {clean_text[:80]}")

    # ---- 私聊消息：全部响应 ----
    elif message_type == "private":
        if not clean_text:
            return

        # 冷却检查
        if is_on_cooldown(session_key):
            return

        logger.info(f"私聊 | {nickname}({user_id}): {clean_text[:80]}")

    else:
        return

    # ---- 先去除 @机器人 前缀（用于命令识别） ----
    text_for_command = clean_text
    _bot_qq_prefix = get_bot_uin()  # 08-22：从连接派生（CONFIG 兜底）
    _prefixes = ["@机器人", "@bot"] + ([f"@{_bot_qq_prefix}"] if _bot_qq_prefix else [])
    for prefix in _prefixes:
        if text_for_command.startswith(prefix):
            text_for_command = text_for_command[len(prefix):].strip()
            break

    # L11 修复：剥离开头的媒体标记（NapCat 把图片/语音转成 [图片] 等占位符，
    # 用户"图片+命令"同发时 clean_text 形如 "[图片] /下一轮"，命令匹配失败静默失效）
    # 只剥开头连续标记（文本中间的标记保留，不影响闲聊内容）
    while text_for_command.startswith("["):
        _end = text_for_command.find("]")
        if _end == -1:
            break
        text_for_command = text_for_command[_end + 1:].strip()

    # ---- 角色扮演命令拦截（仅群聊） ----
    if message_type == "group" and group_id:
        rp_reply = group_roleplay.check_command(text_for_command, group_id, user_id, nickname)
        if rp_reply is not None:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id, rp_reply, user_id, reply_id)
            logger.info(f"✅ 角色扮演命令回复 ({len(rp_reply)} 字)")
            return

        # /开始扮演 需要异步 LLM 生成世界观
        if text_for_command == "/开始扮演" or text_for_command.startswith("/开始扮演 "):
            bg_text = text_for_command[5:].strip() if text_for_command.startswith("/开始扮演 ") else ""
            set_cooldown(session_key)
            asyncio.create_task(_safe_task(
                _handle_start_roleplay(
                    websocket, message_type, group_id, user_id, reply_id, bg_text,
                )
            ))
            logger.info(f"🔄 角色扮演开始已在后台执行: {bg_text[:30]}")
            return

        # /重新生成世界观 需要异步 LLM
        if text_for_command == "/重新生成世界观" or text_for_command.startswith("/重新生成世界观 "):
            new_bg = text_for_command[8:].strip() if text_for_command.startswith("/重新生成世界观 ") else ""
            set_cooldown(session_key)
            asyncio.create_task(_safe_task(
                _handle_regenerate_world(
                    websocket, message_type, group_id, user_id, reply_id, new_bg,
                )
            ))
            logger.info(f"🔄 重新生成世界观已在后台执行: {new_bg[:30]}")
            return

        # /开演 和 /继续 需要异步 LLM 调用
        if text_for_command == "/开演":
            set_cooldown(session_key)
            asyncio.create_task(_safe_task(
                _handle_roleplay_opening(
                    websocket, message_type, group_id, user_id, reply_id,
                )
            ))
            logger.info("🔄 角色扮演开场已在后台执行")
            return

        if text_for_command == "/继续":
            set_cooldown(session_key)
            asyncio.create_task(_safe_task(
                _handle_roleplay_continue(
                    websocket, message_type, group_id, user_id, reply_id,
                )
            ))
            logger.info("🔄 角色扮演继续已在后台执行")
            return

        # 检查是否在活跃的角色扮演房间中 — 只有 @bot 的消息才视为玩家行动
        # 以 / 开头的消息是命令，不作为角色扮演内容
        rp_room = group_roleplay.get_active_room(group_id)
        if rp_room and is_at and not clean_text.startswith("/") and group_roleplay.is_roleplay_message(rp_room, user_id):
            # 普通消息（@bot 且非命令）视为玩家行动
            # 使用 text_for_command（已去除 @bot 前缀）
            action_text = text_for_command.strip()
            set_cooldown(session_key)
            asyncio.create_task(_safe_task(
                _handle_roleplay_action(
                    websocket, message_type, group_id, user_id, action_text, reply_id,
                )
            ))
            logger.info(f"🔄 角色扮演玩家行动已在后台执行: {action_text[:30]}")
            return

        # ⚠️ 优先级说明：
        # @bot 命令通过 _dispatch_priority_commands 在此处（游戏被动拦截之前）
        # 优先处理，否则卧底/投票等沉浸阶段会拦截 @bot 命令导致功能失效。
        # 该函数与主分支共用同一实现（单一代码源）。
        if is_at:
          # 媒体/游戏命令优先分发（/谐音梗 /答案 /猜老婆 /找图 /画图 /描述画图 /修改描述）
          if await _dispatch_priority_commands(
              websocket, message_type, group_id, user_id, reply_id, text_for_command, group_id,
          ):
              return
          # 非命令消息 → 先尝试当作猜老婆答案
          if text_for_command and not text_for_command.startswith("/"):
              set_cooldown(session_key)
              handled = await _handle_guess_wife_answer(
                  websocket, message_type, group_id, user_id, reply_id, text_for_command,
              )
              if handled:
                  return
              # 再尝试当作谐音梗答案
              handled = await _handle_pun_answer(
                  websocket, message_type, group_id, user_id, reply_id, text_for_command,
              )
              if handled:
                  return
              # 海龟汤游戏：拦截 @bot 消息进行提问（指令除外）
              if turtle_soup.is_active(group_id) and not text_for_command.startswith("/"):
                  result = await turtle_soup.handle_question(group_id, user_id, text_for_command, reply_id)
                  if result:
                      reply_text, _rid = result
                      if _rid:
                          segments = [{"type": "reply", "data": {"id": str(_rid)}},
                                      {"type": "text", "data": {"text": reply_text}}]
                          # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
                          target_id = group_id if message_type == "group" else user_id
                          await send_segments(websocket, message_type, target_id, segments)
                      else:
                          await send_reply(websocket, message_type, group_id, reply_text, user_id, reply_id)
                      logger.info(f"🐢 海龟汤提问回复: {text_for_command[:40]}")
                      return

    if text_for_command.startswith("/迁移群聊 "):
        # 仅管理员可用（08-22 白名单机制删除：原 is_admin OR is_whitelisted，
        # 白名单唯一用途即此旁路，生产 0 使用记录 → 整机制移除）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id,
                "⚠️ 只有管理员可以使用此命令", user_id, reply_id)
            return
        # 仅群聊可用
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, user_id,
                "⚠️ /迁移群聊 需要在群聊中使用", user_id, reply_id)
            return
        parts = text_for_command.split(None, 1)
        new_group_str = parts[1].strip() if len(parts) > 1 else ""
        if not new_group_str.isdigit():
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id,
                "⚠️ 用法：/迁移群聊 <目标群号>（数字）", user_id, reply_id)
            return
        new_group_id = int(new_group_str)
        asyncio.create_task(_safe_task(
            _handle_migrate_group(websocket, message_type, group_id, user_id, reply_id, new_group_id)
        ))
        return

    # ---- 屏蔽名单管理命令（群聊 + 私聊都可用） ----
    if text_for_command == "/黑名单" or text_for_command == "/屏蔽名单":
        # 仅管理员可查看（隐私保护，权限修复）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "🔒 只有管理员可以使用此命令", user_id, reply_id)
            return
        blocked = list_blocked()
        set_cooldown(session_key)
        if not blocked:
            reply_text = "📋 屏蔽名单为空 — 目前没有屏蔽的用户"
        else:
            lines = [f"📋 屏蔽名单（{len(blocked)} 人）："]
            for entry in blocked:
                time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry['blocked_at']))
                nickname = entry['nickname'] or str(entry['user_id'])
                blocked_by = entry['blocked_by'] or '未知'
                lines.append(f"  • {nickname} (QQ: {entry['user_id']}) — {time_str}，由 {blocked_by} 屏蔽")
            reply_text = "\n".join(lines)
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    if text_for_command.startswith("/拉黑 ") or text_for_command.startswith("/屏蔽 "):
        # 仅管理员可操作（权限修复）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "🔒 只有管理员可以使用此命令", user_id, reply_id)
            return
        parts = text_for_command.split(None, 1)
        target_qq = parts[1].strip() if len(parts) > 1 else ""
        if not target_qq.isdigit():
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "⚠️ 用法：/拉黑 QQ号", user_id, reply_id)
            return
        target_id = int(target_qq)
        is_new = block_user(target_id, f"QQ:{target_qq}", nickname)
        set_cooldown(session_key)
        if is_new:
            reply_text = f"🚫 已将 QQ: {target_qq} 加入屏蔽名单"
        else:
            reply_text = f"ℹ️ QQ: {target_qq} 已在屏蔽名单中"
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    if text_for_command.startswith("/解封 ") or text_for_command.startswith("/取消屏蔽 "):
        # 仅管理员可操作（权限修复）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "🔒 只有管理员可以使用此命令", user_id, reply_id)
            return
        parts = text_for_command.split(None, 1)
        target_qq = parts[1].strip() if len(parts) > 1 else ""
        if not target_qq.isdigit():
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "⚠️ 用法：/解封 QQ号", user_id, reply_id)
            return
        target_id = int(target_qq)
        removed = unblock_user(target_id)
        set_cooldown(session_key)
        if removed:
            reply_text = f"✅ 已将 QQ: {target_qq} 从屏蔽名单移除"
        else:
            reply_text = f"ℹ️ QQ: {target_qq} 不在屏蔽名单中"
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- 并行模式开关（仅管理员可用） ----
    if text_for_command == "/开启并行":
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "⚠️ 只有管理员可以使用此命令", user_id, reply_id)
            return
        from .llm import set_parallel_mode
        set_parallel_mode(True)
        set_cooldown(session_key)
        await send_reply(websocket, message_type, group_id or user_id, "⚡ 并行模式已开启（LLM 调用将并发执行，聊天仍保持串行）", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    if text_for_command == "/关闭并行":
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "⚠️ 只有管理员可以使用此命令", user_id, reply_id)
            return
        from .llm import set_parallel_mode
        set_parallel_mode(False)
        set_cooldown(session_key)
        await send_reply(websocket, message_type, group_id or user_id, "⏸️ 并行模式已关闭（LLM 调用恢复串行执行）", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    if text_for_command == "/并行状态":
        from .llm import is_parallel_mode
        set_cooldown(session_key)
        status = "⚡ 已开启" if is_parallel_mode() else "⏸️ 已关闭"
        await send_reply(websocket, message_type, group_id or user_id, f"🔀 LLM 并行模式当前状态：{status}", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- 内容审查开关（仅管理员可用） ----
    if text_for_command == "/开启审查":
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "⚠️ 只有管理员可以使用此命令", user_id, reply_id)
            return
        from core.content_filter import set_enabled, is_enabled
        set_enabled(True)
        set_cooldown(session_key)
        await send_reply(websocket, message_type, group_id or user_id, "✅ 内容审查已开启（敏感词将替换为拼音）", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    if text_for_command == "/关闭审查":
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id, "⚠️ 只有管理员可以使用此命令", user_id, reply_id)
            return
        from core.content_filter import set_enabled, is_enabled
        set_enabled(False)
        set_cooldown(session_key)
        await send_reply(websocket, message_type, group_id or user_id, "⏸️ 内容审查已关闭（敏感词不再替换）", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    if text_for_command == "/审查状态":
        from core.content_filter import is_enabled
        set_cooldown(session_key)
        status = "✅ 已开启" if is_enabled() else "⏸️ 已关闭"
        await send_reply(websocket, message_type, group_id or user_id, f"🔍 内容审查当前状态：{status}", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- 媒体/游戏命令统一分发（/谐音梗 /答案 /猜老婆 /找图 /画图 /描述画图 /修改描述） ----
    # 与 is_at 分支共用 _dispatch_priority_commands（单一实现，消除重复）
    if await _dispatch_priority_commands(
        websocket, message_type, group_id or user_id, user_id, reply_id, text_for_command, group_id,
    ):
        return

    # ---- 猜老婆答题：非 @bot 的单个字母 A-F（已在 1634-1641 通过前置过滤） ----
    if (
        message_type == "group"
        and group_id
        and not is_at
        and guess_wife.is_active(group_id)
        and len(clean_text) == 1
        and clean_text.upper() in ("A", "B", "C", "D", "E", "F")
    ):
        handled = await _handle_guess_wife_answer(
            websocket, message_type, group_id, user_id, reply_id, clean_text,
        )
        if handled:
            return
        # 游戏已结束的 fallback — 继续走到 LLM 聊天

    # ---- 谐音梗答题：不 @bot 也检测，但只有答对才响应 ----
    if (
        group_id
        and pun_game.is_active(group_id)
        and text_for_command
        and not text_for_command.startswith("/")
    ):
        # 先用只读检查确认答对（不消耗尝试次数）
        if pun_game.check_answer_correct(group_id, text_for_command):
            # 正式提交答案，结算游戏
            result = pun_game.check_answer(group_id, text_for_command, user_id)
            if result:
                set_cooldown(session_key)
                await _safe_send_reply(websocket, message_type, group_id, result, user_id, reply_id)
                logger.info(f"✅ 谐音梗答对（未 @bot）: {text_for_command[:20]}")
                return
        # 游戏活跃 + 非命令消息 → 视为答题（无论对错都拦截，防止误触发 AI 回复）
        # 答错静默跳过，不扣尝试次数
        logger.info(f"🤔 谐音梗答题（静默）: {text_for_command[:20]}")
        return

    # ---- 帮助命令（分级菜单）----
    if text_for_command == "/帮助" or text_for_command == "/help":
        set_cooldown(session_key)
        reply_text = help_menu.get_first_level_menu()
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return
    if text_for_command.startswith("/帮助 ") or text_for_command.startswith("/help "):
        set_cooldown(session_key)
        query = text_for_command.split(None, 1)[1].strip()
        reply_text = help_menu.get_second_level_menu(query)
        if not reply_text:
            reply_text = help_menu.handle_help(text_for_command)
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- 娱乐命令优先 ----
    # to_thread 执行：check_command 内 /提问建议 等分支会同步调用 LLM
    # （call_llm_sync_from_thread），直接在主事件循环执行会死锁阻塞全 bot
    # 传 text_for_command（已剥离 @机器人/@bot 前缀）：NapCat 把 @ 放在 text 段时
    # clean_text 以 @ 开头，check_command 的 startswith("/") 会匹配失败导致命令静默失效
    entertainment_result = await asyncio.to_thread(
        entertainment.check_command, text_for_command, group_id=group_id, user_id=user_id, nickname=nickname
    )
    entertainment_reply, at_user_ids = entertainment_result
    # 注意：entertainment_reply 返回 "" 表示"命令已处理，无需回复内容"（如自动模式的 /下一轮）
    #   返回 None 表示"不是娱乐命令"，继续交给后续处理器
    if entertainment_reply is not None:
        logger.info(f"🎮 娱乐命令: {clean_text[:30]}")
        # 保存用户消息
        save_message(session_key, "user", clean_text, user_id, nickname)
        if entertainment_reply:
            save_message(session_key, "assistant", entertainment_reply, int(bot_qq), "Bot")
            await send_reply(websocket, message_type, group_id or user_id, entertainment_reply, user_id, reply_id, at_user_ids=at_user_ids if at_user_ids else None)
        set_cooldown(session_key)
        logger.info("✅ 已回复")
        return

    # ---- 卧底游戏指令 ----
    if group_id:
        spy_reply = game_spy.check_command(text_for_command, group_id=group_id, user_id=user_id, nickname=nickname)
        if spy_reply is not None:
            logger.info(f"🕵️ 卧底指令: {text_for_command[:30]}")
            # BUG #3 修复：/卧底开始 由 bot.py 统一调用 start_game + send_words + start_speak_timer
            if text_for_command in ("/卧底开始",):
                start_success, msg = game_spy.start_game(group_id)
                if start_success:
                    game_spy.send_words(group_id, websocket, message_type)
                    game_spy.start_speak_timer(group_id, websocket, message_type, user_id, reply_id)
                    await send_reply(websocket, message_type, group_id, msg, user_id, reply_id)
                    logger.info("✅ 已回复")
                    return
                else:
                    # start_game 失败，使用错误消息
                    await send_reply(websocket, message_type, group_id, msg, user_id, reply_id)
                    logger.info("✅ 已回复")
                    return
            # /卧底惩罚 返回 (message, at_user_ids) 元组
            if text_for_command == "/卧底惩罚":
                pun_msg, at_uids = spy_reply
                await send_reply(websocket, message_type, group_id, pun_msg, user_id, reply_id, at_user_ids=at_uids if at_uids else None)
                logger.info("✅ 已回复")
                return
            # 其他指令直接使用 check_command 的返回值
            await send_reply(websocket, message_type, group_id, spy_reply, user_id, reply_id)
            logger.info("✅ 已回复")
            return

    # ---- 海龟汤游戏指令 ----
    if group_id:
        soup_reply = await turtle_soup.check_command(text_for_command, group_id=group_id, user_id=user_id, nickname=nickname)
        if soup_reply is not None:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id, soup_reply, user_id, reply_id)
            logger.info(f"🐢 海龟汤指令: {text_for_command[:30]}")
            return

    # ---- 卧底游戏被动消息处理（发言/投票/PK/白板猜词阶段） ----
    if group_id and game_spy.is_active(group_id):
        phase = game_spy.get_phase(group_id)
        # Bug #4 修复：只在高度沉浸的阶段拦截旁观者消息
        # lobby 和 ended 阶段不拦截，允许 Bot 正常回复 @Bot 聊天
        immersive_phases = {"speaking", "voting", "pk_speaking", "pk_voting", "blank_guessing"}
        if phase in immersive_phases:
            # M11 修复：传 text_for_command（已剥离 @前缀）——NapCat 把 @ 放 text 段时
            # clean_text 以 "@机器人 " 开头，违禁字检查会把前缀里的"机/器/人"当违禁字
            # 误判出局，投票意图正则也匹配不到前缀后的内容
            spy_msg = await game_spy.handle_game_message_async(group_id, user_id, text_for_command, is_at_bot=is_at)
            if spy_msg is not None:
                await send_reply(websocket, message_type, group_id, spy_msg, user_id, reply_id)
                logger.info(f"✅ 卧底游戏回复: {spy_msg[:50]}")
                return  # 游戏有回复，不再进入 AI 对话
            # 问题 5 修复：沉浸阶段，所有人的非指令消息拦截（含旁观者）
            # 防止 PK/猜词阶段 AI 突然插嘴，破坏氛围
            # M12 修复：@bot 的闲聊至少给一条"游戏进行中"提示，避免 bot 长时间完全沉默
            if is_at:
                await send_reply(websocket, message_type, group_id, "🎮 卧底游戏进行中，等本局结束再找我聊天哦～", user_id, reply_id)
            return

    # ---- 投票进行中：被动消息处理（匹配选项 → 投票） ----
    if group_id and group_vote.is_active(group_id):
        if not group_vote.has_voted(group_id, user_id):
            vote_msg = group_vote.cast_vote(group_id, user_id, nickname, clean_text)
            if vote_msg:
                await send_reply(websocket, message_type, group_id, vote_msg, user_id, reply_id)
                logger.info(f"✅ 投票: {vote_msg}")
                return
        # 投票期间：所有非 /指令 的普通消息一律静默忽略（不打扰群聊）
        # 包括引用投票消息时自动携带 @bot 的情况
        if not clean_text.startswith("/"):
            return

    # ---- 检查会话过期 ----
    if is_session_expired(session_key):
        reset_session(session_key)

    # ---- /人设 xxx 命令 ----
    # text_for_command 已在上方定义
    if text_for_command.startswith("/人设 "):
        personality = text_for_command[4:].strip()
        if personality:
            save_personality(user_id, personality)
            set_cooldown(session_key)
            await send_reply(
                websocket, message_type, group_id or user_id,
                f"🎭 人设已更新：{personality}", user_id, reply_id
            )
            logger.info("✅ 已回复")
            return

    # ---- /人设 查看当前人设 ----
    if text_for_command == "/人设":
        personality = get_personality(user_id)
        set_cooldown(session_key)
        if personality:
            reply_text = f"🎭 当前人设：{personality}"
        else:
            reply_text = "🎭 暂无人设，使用 '/人设 xxx' 来设置"
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /清除人设 ----
    if text_for_command == "/清除人设":
        with get_persona_db() as conn:
            conn.execute("DELETE FROM bot_personalities WHERE user_id = ?", (user_id,))
            conn.commit()
        # 清空对话历史标记，清除人设后不再参考之前的聊天记录（保留数据）
        reset_session(session_key, clear_history=True)
        set_cooldown(session_key)
        await send_reply(websocket, message_type, group_id or user_id, "🧹 已清除人设，恢复默认状态（对话记忆已重置）", user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /用户人设 指令 ----
    if text_for_command == "/用户人设":
        result = get_persona_display(user_id, group_id or 0)
        set_cooldown(session_key)
        if result is None:
            reply_text = (
                "📋 你的用户人设为空\n\n"
                f"💡 使用 '/修改人设 我是学生，喜欢跑步和编程' 来创建人设\n"
                f"   人设是Bot了解你的客观信息，不是AI扮演的角色\n"
                f"   也可用 '/临时人设 xxx' 临时修改，'/恢复人设' 恢复原来的"
            )
        else:
            persona = result.get("persona")
            temp_persona = result.get("temp_persona")
            lines = ["📋 你的用户人设："]
            source_group = result.get("_source_group", 0)
            if group_id and source_group != group_id:
                lines.append(f"（数据来源：主群{source_group}，与同集群群共享）\n")
            lines.append(persona_to_text(persona))
            if temp_persona:
                lines.append("\n🎭 当前正在使用临时人设：")
                lines.append(persona_to_text(temp_persona))
                lines.append("\n💡 发送 '/恢复人设' 可恢复正式人设")
            reply_text = "\n".join(lines)
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /用户人设 xxx 指令（查看他人人设） ----
    # 修复：支持无空格形式（"/用户人设某某"）——原 startswith("/用户人设 ")
    # 必须带空格，无空格形式落不到此分支
    if text_for_command.startswith("/用户人设") and text_for_command != "/用户人设":
        nickname = text_for_command[5:].strip()
        set_cooldown(session_key)
        if not nickname:
            await send_reply(websocket, message_type, group_id or user_id,
                "⚠️ 用法：/用户人设 昵称\n例如：/用户人设 小明", user_id, reply_id)
            return
        result = find_persona_by_nickname(nickname, group_id or 0)
        if result is None:
            reply_text = f"🔍 未找到昵称为「{nickname}」的用户人设"
        else:
            target_persona = _parse_persona_json(result["persona"])
            target_temp = _parse_persona_json(result["temporary_persona"]) if result["temporary_persona"] else None
            lines = [f"📋 {result['nickname']} 的用户人设："]
            source_group = result.get("_source_group", 0)
            if group_id and source_group != group_id:
                lines.append(f"（数据来源：主群{source_group}，与同集群群共享）\n")
            lines.append(persona_to_text(target_persona))
            if target_temp:
                lines.append("\n🎭 当前正在使用临时人设：")
                lines.append(persona_to_text(target_temp))
            reply_text = "\n".join(lines)
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /用户画像 指令（精确匹配：查看自己的画像） ----
    if text_for_command == "/用户画像":
        profile = get_user_profile(user_id, group_id or 0)
        set_cooldown(session_key)
        if profile is None or not profile.get("profile"):
            reply_text = (
                "👤 你的用户画像尚未生成\n\n"
                f"💡 画像由Bot根据你的聊天记录自动生成，与群友互动后会自动更新"
            )
        else:
            from datetime import datetime
            updated = datetime.fromtimestamp(profile["last_updated_at"]).strftime("%Y-%m-%d %H:%M")
            source_group = profile.get("_source_group", profile.get('group_id', 0))
            if group_id and source_group != group_id:
                reply_text = (
                    f"👤 你的用户画像（数据来源：主群{source_group}，与同集群群共享）\n"
                    f"🕒 最后更新: {updated}\n\n"
                    f"{profile['profile']}"
                )
            else:
                group_label = f"(群{profile['group_id']})" if profile.get('group_id') else "(全局)"
                reply_text = (
                    f"👤 你的用户画像 {group_label}\n"
                    f"🕒 最后更新: {updated}\n\n"
                    f"{profile['profile']}"
                )
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /修改人设 xxx 指令 ----
    if text_for_command.startswith("/修改人设 "):
        description = text_for_command[6:].strip()
        if not description:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id,
                "⚠️ 用法：/修改人设 描述\n例如：/修改人设 我是学生，喜欢跑步和编程", user_id, reply_id)
            return
        # 后台执行：调用 LLM 解析自然语言描述并更新人设
        asyncio.create_task(_safe_task(
            _handle_update_persona(websocket, message_type, group_id or user_id, user_id, reply_id, description)
        ))
        logger.info(f"🔄 /修改人设 已在后台执行: {description[:30]}")
        return

    # ---- /临时人设 xxx 指令 ----
    if text_for_command.startswith("/临时人设 "):
        description = text_for_command[6:].strip()
        if not description:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, group_id or user_id,
                "⚠️ 用法：/临时人设 描述\n例如：/临时人设 我现在很生气", user_id, reply_id)
            return
        # 后台执行：调用 LLM 解析并设置临时人设
        asyncio.create_task(_safe_task(
            _handle_set_temp_persona(websocket, message_type, group_id or user_id, user_id, reply_id, description)
        ))
        logger.info(f"🔄 /临时人设 已在后台执行: {description[:30]}")
        return

    # ---- /恢复人设 指令 ----
    if text_for_command == "/恢复人设":
        was_reset = reset_temporary_persona(user_id, group_id or 0)
        set_cooldown(session_key)
        if was_reset:
            reply_text = "🔄 已清除临时人设，恢复到正式人设"
        else:
            reply_text = "ℹ️ 当前没有临时人设，已经是正式人设状态"
        await send_reply(websocket, message_type, group_id or user_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /投票 指令 ----
    if text_for_command.startswith("/投票 "):
        # 解析选项（空格分隔）
        options = text_for_command[4:].strip().split()
        set_cooldown(session_key)
        success, msg = group_vote.start_vote(group_id, options, user_id, websocket, message_type)
        await send_reply(websocket, message_type, group_id, msg, user_id, reply_id)
        logger.info(f"✅ 投票: {msg[:50]}")
        return

    # ---- /投票 查看当前投票状态 ----
    if text_for_command == "/投票":
        set_cooldown(session_key)
        if group_id and group_vote.is_active(group_id):
            status = group_vote.get_vote_status(group_id)
            await send_reply(websocket, message_type, group_id, status, user_id, reply_id)
        else:
            await send_reply(websocket, message_type, group_id, "⚠️ 当前没有正在进行的投票\n用法：/投票 选项A 选项B 选项C ...", user_id, reply_id)
        return

    # ---- /结束投票 指令 ----
    if text_for_command == "/结束投票":
        set_cooldown(session_key)
        if group_id and group_vote.is_active(group_id):
            result = group_vote.end_vote(group_id)
            await send_reply(websocket, message_type, group_id, result, user_id, reply_id)
            logger.info("✅ 投票已手动结束")
        else:
            await send_reply(websocket, message_type, group_id, "⚠️ 当前没有正在进行的投票", user_id, reply_id)
        return

    # ---- /评选 指令 ----
    if text_for_command.startswith("/评选"):
        # /评选 仅在群聊中有效
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "📊 /评选 需要在群聊中使用哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /评选 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # /评选 <日期> 查看历史记录
        cmd_args = text_for_command[3:].strip()
        if cmd_args:
            set_cooldown(session_key)
            try:
                # 解析日期格式
                from datetime import datetime
                date_str = cmd_args.split()[0]
                # 验证格式
                datetime.strptime(date_str, "%Y-%m-%d")
            except (ValueError, IndexError):
                await send_reply(websocket, message_type, target_id, "📊 日期格式不正确，请使用：/评选 YYYY-MM-DD（如 /评选 2026-08-01）", user_id, reply_id)
                return
            # 查询数据库（兼容半日场次：YYYY-MM-DD 旧记录 + YYYY-MM-DD-AM/PM 新记录）
            from .database import get_daily_evaluation
            records = _query_report_records(group_id, get_daily_evaluation, date_str)
            if records:
                parts = []
                for rec in records:
                    label = _report_period_label(rec["date"])
                    parts.append(f"【{label}场】\n{rec['evaluation']}\n\n（共 {rec['message_count']} 条消息，{rec['user_count']} 位用户）")
                result = f"📊 评选记录 · {date_str}\n\n" + "\n\n".join(parts)
            else:
                result = f"📊 {date_str} 暂无评选记录"
            await send_reply(websocket, message_type, target_id, result, user_id, reply_id)
            logger.info("✅ 已回复评选历史记录")
            return
        # /评选 改为后台执行，不阻塞其他指令
        asyncio.create_task(_safe_command(handle_evaluation(websocket, message_type, group_id, user_id, reply_id, group_id), cmd_name="/评选",
                                         task_key=TASK_REGISTRY.register("群指令", f"评选（{nickname}发起）", group_id=group_id, user_id=user_id)))
        logger.info("🔄 /评选 已在后台执行")
        return

    # ---- /总结 指令 ----
    if text_for_command.startswith("/总结"):
        # /总结 仅在群聊中有效
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "📝 /总结 需要在群聊中使用哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /总结 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # /总结 <日期> 查看历史记录
        cmd_args = text_for_command[3:].strip()
        if cmd_args:
            set_cooldown(session_key)
            try:
                # 解析日期格式
                from datetime import datetime
                date_str = cmd_args.split()[0]
                # 验证格式
                datetime.strptime(date_str, "%Y-%m-%d")
            except (ValueError, IndexError):
                await send_reply(websocket, message_type, target_id, "📝 日期格式不正确，请使用：/总结 YYYY-MM-DD（如 /总结 2026-08-01）", user_id, reply_id)
                return
            # 查询数据库（兼容半日场次：YYYY-MM-DD 旧记录 + YYYY-MM-DD-AM/PM 新记录）
            from .database import get_daily_summary
            records = _query_report_records(group_id, get_daily_summary, date_str)
            if records:
                parts = []
                for rec in records:
                    label = _report_period_label(rec["date"])
                    parts.append(f"【{label}场】\n{rec['summary']}\n\n（共 {rec['message_count']} 条消息，{rec['user_count']} 位用户）")
                result = f"📝 总结记录 · {date_str}\n\n" + "\n\n".join(parts)
            else:
                result = f"📝 {date_str} 暂无总结记录"
            await send_reply(websocket, message_type, target_id, result, user_id, reply_id)
            logger.info("✅ 已回复总结历史记录")
            return
        # /总结 改为后台执行，不阻塞其他指令
        asyncio.create_task(_safe_command(handle_summary(websocket, message_type, group_id, user_id, reply_id, group_id), cmd_name="/总结",
                                        task_key=TASK_REGISTRY.register("群指令", f"总结（{nickname}发起）", group_id=group_id, user_id=user_id)))
        logger.info("🔄 /总结 已在后台执行")
        return

    # ---- /模仿 指令（群友聊天模拟，仅管理员；2026-08-12）----
    if text_for_command.startswith("/模仿"):
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /模仿 指令哦～", user_id, reply_id)
            return
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🎭 /模仿 需要在群聊中使用哦～", user_id, reply_id)
            return
        from games.mimic import check_mimic_command, generate_mimic_reply
        nick_query, mimic_content, err = check_mimic_command(text_for_command)
        if err:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, err, user_id, reply_id)
            return
        # 解析昵称 → 用户（2026-08-13：局部 import 改名——原 from ... import find_persona_by_nickname
        # 遮蔽顶部 import，导致 handle_message 内 /用户人设 xxx 分支 UnboundLocalError）
        from core.persona import find_persona_by_nickname as _find_persona_by_nickname
        target = _find_persona_by_nickname(nick_query, group_id)
        if not target:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                             f"🔍 没找到群友「{nick_query}」，请用群内昵称", user_id, reply_id)
            return
        set_cooldown(session_key)
        await send_reply(websocket, message_type, target_id,
                         f"🎭 正在模仿 {target['nickname']} 发言…", user_id, reply_id)
        asyncio.create_task(_safe_task(
            _handle_mimic_reply(websocket, message_type, target_id, user_id, target, mimic_content),
            cmd_name="/模仿",
        ))
        return

    # ---- /活跃度 指令 ----
    if text_for_command.startswith("/活跃度"):
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "📊 /活跃度 需要在群聊中使用哦～", user_id, reply_id)
            return
        # 解析天数：/活跃度7 或 /活跃度 7（2026-08-12，n 为正整数；无参数默认 15 天）
        _days_arg = text_for_command[4:].strip()
        _days = int(_days_arg) if _days_arg.isdigit() and int(_days_arg) > 0 else 0
        await handle_activity(websocket, message_type, group_id, user_id, reply_id, _days)
        return

    # ---- /用户画像 xxx 指令 ----
    if text_for_command.startswith("/用户画像"):
        nickname_query = text_for_command[5:].strip()
        if not nickname_query:
            # 不带参数时显示当前群的所有画像列表
            set_cooldown(session_key)
            profiles = get_all_profiles(group_id or 0)
            if profiles:
                lines = ["👥 用户画像列表："]
                for p in profiles:
                    lines.append(f"  - {p['nickname']} (ID: {p['user_id']})")
                lines.append(f"\n共 {len(profiles)} 位用户已建立画像")
                lines.append("使用 '/用户画像 昵称' 查看具体画像")
                reply_text = "\n".join(lines)
            else:
                reply_text = "👤 暂无用户画像，使用 '/更新画像 昵称' 来为群友建立画像"
            await send_reply(websocket, message_type, target_id, reply_text, user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 根据昵称查找用户（优先在当前群查找）
        result = find_user_by_nickname(nickname_query, group_id or 0)
        if result:
            profile = get_user_profile(result["user_id"], group_id or 0)
            if profile:
                from datetime import datetime
                updated = datetime.fromtimestamp(profile["last_updated_at"]).strftime("%Y-%m-%d %H:%M")
                source_group = profile.get("_source_group", profile.get('group_id', 0))
                if group_id and source_group != group_id:
                    reply_text = (
                        f"👤 {profile['nickname']} 的用户画像（数据来源：主群{source_group}，与同集群群共享）\n"
                        f"🕒 最后更新: {updated}\n\n"
                        f"{profile['profile']}"
                    )
                else:
                    group_label = f"(群{profile['group_id']})" if profile.get('group_id') else "(全局)"
                    reply_text = (
                        f"👤 {profile['nickname']} 的用户画像 {group_label}\n"
                        f"🕒 最后更新: {updated}\n\n"
                        f"{profile['profile']}"
                    )
            else:
                reply_text = f"👤 {result['nickname']} 暂无画像（可在本群 '/更新画像 {result['nickname']}' 建立）"
        else:
            # 尝试从 message_archive 中查找该用户（优先当前群）
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (f"%{nickname_query}%", group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? ORDER BY created_at DESC LIMIT 1",
                        (f"%{nickname_query}%"),
                    ).fetchone()
                if row:
                    profile = get_user_profile(row["user_id"], group_id or 0)
                    if profile:
                        from datetime import datetime
                        updated = datetime.fromtimestamp(profile["last_updated_at"]).strftime("%Y-%m-%d %H:%M")
                        source_group = profile.get("_source_group", profile.get('group_id', 0))
                        if group_id and source_group != group_id:
                            reply_text = (
                                f"👤 {profile['nickname']} 的用户画像（数据来源：主群{source_group}，与同集群群共享）\n"
                                f"🕒 最后更新: {updated}\n\n"
                                f"{profile['profile']}"
                            )
                        else:
                            group_label = f"(群{profile['group_id']})" if profile.get('group_id') else "(全局)"
                            reply_text = (
                                f"👤 {profile['nickname']} 的用户画像 {group_label}\n"
                                f"🕒 最后更新: {updated}\n\n"
                                f"{profile['profile']}"
                            )
                    else:
                        reply_text = (
                            f"👤 {row['nickname']} 暂无画像\n"
                            f"使用 '/更新画像 {row['nickname']}' 来分析聊天记录并建立画像"
                        )
                else:
                    reply_text = (
                        f"🔍 未找到昵称包含'{nickname_query}'的用户\n"
                        f"已建立画像的用户：使用 '/用户画像' 查看列表\n"
                        f"或直接输入 '/更新画像 昵称' 来建立新画像"
                    )
        set_cooldown(session_key)
        await send_reply(websocket, message_type, target_id, reply_text, user_id, reply_id)
        logger.info("✅ 已回复")
        return

    # ---- /更新画像 xxx 指令 ----
    # 注意：必须排除 /更新画像和人设（联合更新命令），否则 startswith 前缀吞噬导致联合更新永远不可达
    if text_for_command.startswith("/更新画像") and not text_for_command.startswith("/更新画像和人设"):
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /更新画像 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        nickname_query = text_for_command[5:].strip()
        if not nickname_query:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "👤 请输入要分析的用户昵称或QQ号，例如：/更新画像 某某某 或 /更新画像 123456789", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 查找用户：先尝试 QQ 号精确匹配，再按昵称模糊匹配
        result = None
        if nickname_query.isdigit():
            # QQ 号精确查找（优先当前群）；ORDER BY created_at DESC 保证取最新昵称（用户可能改过名）
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE user_id = ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (int(nickname_query), group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                        (int(nickname_query),),
                    ).fetchone()
                if row:
                    result = dict(row)
        if not result:
            result = find_user_by_nickname(nickname_query, group_id or 0)
        if not result:
            # 从 message_archive 按昵称模糊查找（优先当前群）
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (f"%{nickname_query}%", group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? ORDER BY created_at DESC LIMIT 1",
                        (f"%{nickname_query}%"),
                    ).fetchone()
                if row:
                    result = dict(row)
        if not result:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                f"🔍 未找到昵称包含'{nickname_query}'的用户，请先确认该用户在群中发过消息", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 后台执行，不阻塞其他指令
        asyncio.create_task(_safe_task(
            handle_update_profile(websocket, message_type, target_id, user_id, reply_id,
                                  result["user_id"], result["nickname"])
        ))
        logger.info(f"🔄 /更新画像 已在后台执行: {result['nickname']}({result['user_id']})")
        return

    # ---- /更新画像和人设 xxx 指令 ----
    if text_for_command.startswith("/更新画像和人设"):
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /更新画像和人设 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        nickname_query = text_for_command[8:].strip()
        if not nickname_query:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "👤 请输入要分析的用户昵称或QQ号，例如：/更新画像和人设 某某某 或 /更新画像和人设 123456789", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 查找用户：先尝试 QQ 号精确匹配，再按昵称模糊匹配
        result = None
        if nickname_query.isdigit():
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE user_id = ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (int(nickname_query), group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                        (int(nickname_query),),
                    ).fetchone()
                if row:
                    result = dict(row)
        if not result:
            result = find_user_by_nickname(nickname_query, group_id or 0)
        if not result:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (f"%{nickname_query}%", group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? ORDER BY created_at DESC LIMIT 1",
                        (f"%{nickname_query}%"),
                    ).fetchone()
                if row:
                    result = dict(row)
        if not result:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                f"🔍 未找到昵称包含'{nickname_query}'的用户，请先确认该用户在群中发过消息", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 后台执行，不阻塞其他指令
        asyncio.create_task(_safe_task(
            handle_update_profile_and_persona(websocket, message_type, target_id, user_id, reply_id,
                                               result["user_id"], result["nickname"])
        ))
        logger.info(f"🔄 /更新画像和人设 已在后台执行: {result['nickname']}({result['user_id']})")
        return

    # ---- /更新人设 xxx 指令 ----
    if text_for_command.startswith("/更新人设"):
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /更新人设 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        nickname_query = text_for_command[5:].strip()
        if not nickname_query:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "👤 请输入要分析的用户昵称或QQ号，例如：/更新人设 某某某 或 /更新人设 123456789", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 查找用户：先尝试 QQ 号精确匹配，再按昵称模糊匹配
        result = None
        if nickname_query.isdigit():
            # QQ 号精确查找（优先当前群）；ORDER BY created_at DESC 保证取最新昵称（用户可能改过名）
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE user_id = ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (int(nickname_query), group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                        (int(nickname_query),),
                    ).fetchone()
                if row:
                    result = dict(row)
        if not result:
            result = find_user_by_nickname(nickname_query, group_id or 0)
        if not result:
            # 从 message_archive 按昵称模糊查找（优先当前群）
            with get_db() as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                    (f"%{nickname_query}%", group_id),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT DISTINCT user_id, nickname FROM message_archive WHERE nickname LIKE ? ORDER BY created_at DESC LIMIT 1",
                        (f"%{nickname_query}%"),
                    ).fetchone()
                if row:
                    result = dict(row)
        if not result:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                f"🔍 未找到昵称包含'{nickname_query}'的用户，请先确认该用户在群中发过消息", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 后台执行，不阻塞其他指令
        asyncio.create_task(_safe_task(
            handle_update_persona(websocket, message_type, target_id, user_id, reply_id,
                                  result["user_id"], result["nickname"])
        ))
        logger.info(f"🔄 /更新人设 已在后台执行: {result['nickname']}({result['user_id']})")
        return

    # ---- /更新全部人设 指令 ----
    if text_for_command == "/更新全部人设":
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "📋 /更新全部人设 需要在群聊中使用", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /更新全部人设 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 后台执行，不阻塞其他指令
        asyncio.create_task(_safe_task(
            _handle_update_all_personas(websocket, message_type, target_id, user_id, reply_id)
        ))
        logger.info("🔄 /更新全部人设 已在后台执行")
        return

    # ---- /更新全部画像 指令 ----
    if text_for_command == "/更新全部画像":
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "📋 /更新全部画像 需要在群聊中使用", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /更新全部画像 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 后台执行，不阻塞其他指令
        asyncio.create_task(_safe_task(
            handle_update_all_profiles(websocket, message_type, target_id, user_id, reply_id)
        ))
        logger.info("🔄 /更新全部画像 已在后台执行")
        return

    # ---- /更新全部画像和人设 指令 ----
    if text_for_command == "/更新全部画像和人设":
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "📋 /更新全部画像和人设 需要在群聊中使用", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 仅管理员可用
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /更新全部画像和人设 指令哦～", user_id, reply_id)
            logger.info("✅ 已回复")
            return
        # 后台执行，不阻塞其他指令
        asyncio.create_task(_safe_task(
            handle_update_all_profiles_and_personas(websocket, message_type, target_id, user_id, reply_id)
        ))
        logger.info("🔄 /更新全部画像和人设 已在后台执行")
        return

    # ---- /暂停任务 指令 ----
    if text_for_command == "/暂停任务":
        # 全局系统操作，仅管理员可用（权限修复）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用此命令", user_id, reply_id)
            return
        from .scheduler import pause_daily_update
        result = pause_daily_update()
        if result:
            await send_reply(websocket, message_type, target_id,
                "⏸️ 已暂停每日定时更新任务（联合更新画像+人设、更新真心话题库）。\n其他定时任务（11:30/22:30 半日报告、好感度衰减）不受影响。",
                user_id, reply_id)
        else:
            await send_reply(websocket, message_type, target_id,
                "⏸️ 每日定时更新任务当前已经是暂停状态。",
                user_id, reply_id)
        logger.info(f"⏸️ /暂停任务 执行: {result}")
        return

    # ---- /恢复任务 指令 ----
    if text_for_command == "/恢复任务":
        # 全局系统操作，仅管理员可用（权限修复）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用此命令", user_id, reply_id)
            return
        from .scheduler import resume_daily_update
        result = resume_daily_update()
        if result:
            await send_reply(websocket, message_type, target_id,
                "▶️ 已恢复每日定时更新任务（联合更新画像+人设、更新真心话题库）。",
                user_id, reply_id)
        else:
            await send_reply(websocket, message_type, target_id,
                "▶️ 每日定时更新任务当前已经是运行状态。",
                user_id, reply_id)
        logger.info(f"▶️ /恢复任务 执行: {result}")
        return

    # ---- /集群 指令 ----
    if text_for_command.startswith("/集群"):
        # 群集群管理：将备份群归为一组，评选/总结时合并消息
        if not is_admin(user_id):
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /集群 指令哦～", user_id, reply_id)
            return

        if not group_id:
            await send_reply(websocket, message_type, target_id, "🔒 /集群 需要在群聊中使用哦～", user_id, reply_id)
            return

        from .database import (
            get_clusters, get_cluster_id, get_cluster_groups,
            add_group_to_cluster, remove_group_from_cluster,
            _ensure_cluster_for_group, merge_clusters,
            get_group_task_flags, update_group_task_flag,
            get_settings_db, get_cluster_master_group,
        )

        rest = text_for_command[len("/集群"):].strip()

        if not rest or rest == "查看":
            # 查看所有集群
            clusters = get_clusters()
            if not clusters:
                await send_reply(websocket, message_type, target_id,
                    "📋 暂无群集群配置。\n\n使用方法：\n/集群 添加 <群号1>+<群号2>+...  — 将多个群归为一组\n/集群 移除 <群号>          — 将群从集群中移除\n/集群 查看 <群号>          — 查看指定群所属集群\n/群开关 <任务> 开/关       — 控制本群定时任务开关",
                    user_id, reply_id)
                return

            lines = ["📋 群集群列表："]
            for cluster in clusters:
                cid = cluster["cluster_id"][:16]
                master_gid = cluster.get("master_group_id", 0)
                gids = [str(m["group_id"]) for m in cluster["members"]]
                lines.append(f"\n🔹 {cid}... ({len(gids)} 个群)  主群: {master_gid}")
                for m in cluster["members"]:
                    gid = m["group_id"]
                    flags = []
                    if not m["enable_persona_update"]:
                        flags.append("人设✗")
                    if not m["enable_profile_update"]:
                        flags.append("画像✗")
                    if not m["enable_question_refill"]:
                        flags.append("题库✗")
                    if not m["enable_evaluation"]:
                        flags.append("评选✗")
                    if not m["enable_summary"]:
                        flags.append("总结✗")
                    master_marker = " 👑" if gid == master_gid else ""
                    flag_str = f"  [{', '.join(flags)}]" if flags else ""
                    lines.append(f"  - 群 {gid}{master_marker}{flag_str}")

            await send_reply(websocket, message_type, target_id, "\n".join(lines), user_id, reply_id)

        elif rest.startswith("添加"):
            # /集群 添加 <群号1>+<群号2>+...
            groups_str = rest[len("添加"):].strip()
            if not groups_str:
                await send_reply(websocket, message_type, target_id,
                    "📝 用法：/集群 添加 <群号1>+<群号2>+...\n例：/集群 添加 123456+789012",
                    user_id, reply_id)
                return

            group_ids = [int(g.strip()) for g in groups_str.replace("@", "+").split("+") if g.strip().isdigit()]
            if len(group_ids) < 2:
                await send_reply(websocket, message_type, target_id,
                    "📝 请至少提供 2 个群号，用 + 分隔。\n例：/集群 添加 123456+789012",
                    user_id, reply_id)
                return

            # 确保所有群都注册到集群
            for gid in group_ids:
                _ensure_cluster_for_group(gid)

            # 取第一个群的 cluster_id 作为目标集群
            target_cid = get_cluster_id(group_ids[0])

            # 设置第一个群为主群
            with get_settings_db() as conn:
                conn.execute(
                    "UPDATE group_clusters SET master_group_id = ? WHERE cluster_id = ?",
                    (group_ids[0], target_cid),
                )

            # 将其他群合并到目标集群
            merged = [group_ids[0]]
            for gid in group_ids[1:]:
                src_cid = get_cluster_id(gid)
                if src_cid and src_cid != target_cid:
                    merge_clusters(target_cid, src_cid)
                else:
                    add_group_to_cluster(target_cid, gid)
                merged.append(gid)

            # 获取主群
            master = get_cluster_master_group(group_ids[0])

            await send_reply(websocket, message_type, target_id,
                f"✅ 已将 {len(merged)} 个群归为一组（集群 {target_cid[:16]}...）：\n" +
                f"  - 群 {group_ids[0]} 👑 主群\n" +
                "\n".join(f"  - 群 {gid}" for gid in merged[1:]) +
                f"\n\n主群：{master}\n这些群的用户共享人设/画像，消息将在 /总结 和 /评选 中合并处理。\n定时报告只发送到主群，避免重复。\n如需更改主群，使用：/集群 设主群 <群号>",
                user_id, reply_id)

        elif rest.startswith("移除"):
            # /集群 移除 <群号>
            gid_str = rest[len("移除"):].strip().lstrip("@")
            if not gid_str.isdigit():
                await send_reply(websocket, message_type, target_id,
                    "📝 用法：/集群 移除 <群号>\n例：/集群 移除 123456",
                    user_id, reply_id)
                return
            gid = int(gid_str)
            cid = get_cluster_id(gid)
            if not cid:
                await send_reply(websocket, message_type, target_id,
                    f"📝 群 {gid} 不在任何集群中。",
                    user_id, reply_id)
                return
            remove_group_from_cluster(cid, gid)
            await send_reply(websocket, message_type, target_id,
                f"✅ 已将群 {gid} 从集群中移除。",
                user_id, reply_id)

        elif rest.startswith("查看"):
            # /集群 查看 <群号>
            gid_str = rest[len("查看"):].strip().lstrip("@")
            if not gid_str.isdigit():
                gid_str = str(group_id)
            gid = int(gid_str)
            cid = get_cluster_id(gid)
            if not cid:
                await send_reply(websocket, message_type, target_id,
                    f"📝 群 {gid} 不在任何集群中。",
                    user_id, reply_id)
                return

            members = get_cluster_groups(cid)
            master = get_cluster_master_group(gid)
            lines = [f"🔹 集群 {cid[:16]}... ({len(members)} 个群)  主群: {master}"]
            for m in members:
                m_gid = m["group_id"]
                flag_list = []
                if not m["enable_persona_update"]:
                    flag_list.append("人设✗")
                if not m["enable_profile_update"]:
                    flag_list.append("画像✗")
                if not m["enable_question_refill"]:
                    flag_list.append("题库✗")
                if not m["enable_evaluation"]:
                    flag_list.append("评选✗")
                if not m["enable_summary"]:
                    flag_list.append("总结✗")
                master_marker = " 👑" if m_gid == master else ""
                flag_str = f" [{', '.join(flag_list)}]" if flag_list else ""
                lines.append(f"  - 群 {m_gid}{master_marker}{flag_str}")

            await send_reply(websocket, message_type, target_id, "\n".join(lines), user_id, reply_id)

        elif rest.startswith("设主群") or rest.startswith("设置主群"):
            # /集群 设主群 <群号>
            gid_str = rest[len("设主群"):] if rest.startswith("设主群") else rest[len("设置主群"):]
            gid_str = gid_str.strip().lstrip("@")
            if not gid_str.isdigit():
                await send_reply(websocket, message_type, target_id,
                    "📝 用法：/集群 设主群 <群号>\n例：/集群 设主群 123456",
                    user_id, reply_id)
                return
            gid = int(gid_str)
            cid = get_cluster_id(gid)
            if not cid:
                await send_reply(websocket, message_type, target_id,
                    f"📝 群 {gid} 不在任何集群中，无法设为主群。",
                    user_id, reply_id)
                return
            from .database import get_settings_db
            with get_settings_db() as conn:
                conn.execute(
                    "UPDATE group_clusters SET master_group_id = ? WHERE cluster_id = ?",
                    (gid, cid),
                )
            clusters = get_clusters()
            cluster_info = None
            for c in clusters:
                if c["cluster_id"] == cid:
                    cluster_info = c
                    break
            groups = get_cluster_groups(cid)
            lines = [f"✅ 已将群 {gid} 设为集群主群。", f"🆔 集群 {cid[:16]}..."]
            for m in groups:
                m_gid = m["group_id"]
                marker = " 👑 主群" if m_gid == gid else ""
                lines.append(f"  - 群 {m_gid}{marker}")
            await send_reply(websocket, message_type, target_id, "\n".join(lines), user_id, reply_id)

        else:
            await send_reply(websocket, message_type, target_id,
                "📝 用法：\n/集群                    — 查看所有集群\n/集群 添加 <群号1>+<群号2>+... — 将群归为一组（第一个为主群）\n/集群 移除 <群号>       — 从集群移除群\n/集群 设主群 <群号>     — 设置集群主群\n/集群 查看 <群号>       — 查看指定群所属集群",
                user_id, reply_id)

        return

    # ---- /群开关 指令 ----
    if text_for_command.startswith("/群开关"):
        if not is_admin(user_id):
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用 /群开关 指令哦～", user_id, reply_id)
            return

        if not group_id:
            await send_reply(websocket, message_type, target_id, "🔒 /群开关 需要在群聊中使用哦～", user_id, reply_id)
            return

        from .database import get_group_task_flags, update_group_task_flag, _ensure_cluster_for_group

        rest = text_for_command[len("/群开关"):].strip()

        # 确保群已注册
        _ensure_cluster_for_group(group_id)

        flags = get_group_task_flags(group_id)

        if not rest:
            # 显示当前开关状态
            task_names = {
                "persona": "人设更新",
                "profile": "画像更新",
                "question": "题库补充",
                "evaluation": "评选报告",
                "summary": "总结报告",
                "member_notify": "入群通知",
            }
            lines = [f"⚙️ 群 {group_id} 定时任务开关："]
            for key, name in task_names.items():
                state = "✅ 开启" if flags[key] else "❌ 关闭"
                lines.append(f"  {name}: {state}")
            lines.append("\n用法：/群开关 <任务名> 开/关")
            lines.append("任务名：人设、画像、题库、评选、总结、入群通知")
            await send_reply(websocket, message_type, target_id, "\n".join(lines), user_id, reply_id)
            return

        # 解析：任务名 + 状态
        parts = rest.split()
        if len(parts) < 2:
            await send_reply(websocket, message_type, target_id,
                "📝 用法：/群开关 <任务名> 开/关\n例：/群开关 评选 关",
                user_id, reply_id)
            return

        task_key_map = {
            "人设": "persona",
            "画像": "profile",
            "题库": "question",
            "评选": "evaluation",
            "总结": "summary",
            "入群通知": "member_notify",
        }
        task_name = parts[0]
        state = parts[1]

        task_key = task_key_map.get(task_name)
        if not task_key:
            await send_reply(websocket, message_type, target_id,
                f"📝 未知任务名：{task_name}\n可用任务：人设、画像、题库、评选、总结",
                user_id, reply_id)
            return

        if state not in ("开", "关"):
            await send_reply(websocket, message_type, target_id,
                f"📝 状态应为「开」或「关」，收到：{state}",
                user_id, reply_id)
            return

        enabled = (state == "开")
        update_group_task_flag(group_id, task_key, enabled)

        task_display = task_key_map.get(task_key, task_key)
        await send_reply(websocket, message_type, target_id,
            f"✅ 群 {group_id} 的「{task_display}」已{'开启' if enabled else '关闭'}。",
            user_id, reply_id)

        return

    # ---- /补充题库 指令 ----
    if text_for_command == "/补充题库":
        # 会触发 LLM 生成，消耗资源，仅管理员可用（权限修复）
        if not is_admin(user_id):
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "🔒 只有管理员可以使用此命令", user_id, reply_id)
            return
        from .scheduler import refill_questions_now, _get_question_refill_lock
        await send_reply(websocket, message_type, target_id,
            "🔄 正在补充真心话大冒险题库，请稍候...", user_id, reply_id)
        logger.info(f"🔄 /补充题库 执行中 (群 {target_id})")

        async def _do_refill():
            # 加全局互斥锁，与每日定时任务的题库补充互斥
            async with _get_question_refill_lock():
                logger.info(f"[题库补充] 手动补充已获取锁，开始执行 (群 {target_id})")
                result = await asyncio.to_thread(refill_questions_now, target_id)
                logger.info(f"[题库补充] 手动补充完成，释放锁 (群 {target_id}): {result}")
            await send_reply(websocket, message_type, target_id, result, user_id, reply_id)

        asyncio.create_task(_safe_command(_do_refill(), cmd_name="/补充题库",
                                         task_key=TASK_REGISTRY.register("群指令", f"补充题库（{nickname}发起）", group_id=group_id, user_id=user_id)))
        return


    # ---- /查询 指令 ----
    if text_for_command.startswith("/查询"):
        rest = text_for_command[len("/查询"):]

        # 解析 /查询n 或 /查询 n 格式
        rest = rest.lstrip()  # 去掉前导空格，支持 /查询 5 问题
        # 时间窗参数（qa 段，热生效；报错文案同步动态化）
        _q_default_hours = int(qa_params().get("query_default_hours", 24))
        _q_hours_max = int(qa_params().get("query_hours_max", 120))
        hours_match = re.match(r"^(\d+)\s*(.*)", rest)
        if hours_match:
            hours = int(hours_match.group(1))
            question = hours_match.group(2).strip()
            # 限制小时范围 1-query_hours_max（group_chat_cache 窗口内安全）
            if hours < 1 or hours > _q_hours_max:
                set_cooldown(session_key)
                await send_reply(websocket, message_type, target_id,
                    f"📡 查询时间范围请输入 1-{_q_hours_max} 之间的整数（最多查 {_q_hours_max // 24} 天），例如：/查询6 /查询{_q_hours_max}", user_id, reply_id)
                return
        else:
            hours = _q_default_hours
            question = rest.strip()

        if not question:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "📡 请输入查询问题，例如：/查询 大家最近在讨论什么（或 /查询6 近6小时 问题）", user_id, reply_id)
            return
        # /查询 仅在群聊中有效
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "📡 /查询 需要在群聊中使用哦～", user_id, reply_id)
            return
        # /查询 改为后台执行，不阻塞其他指令
        asyncio.create_task(_safe_command(handle_query(websocket, message_type, group_id, user_id, reply_id, group_id, question, hours), cmd_name="/查询",
                                         task_key=TASK_REGISTRY.register("群指令", f"查询「{question[:20]}」（{nickname}发起）", group_id=group_id, user_id=user_id)))
        logger.info(f"🔄 /查询 已在后台执行: 近{hours}小时, {question[:30]}")
        return

    # ---- /分析 指令 ----
    if text_for_command.startswith("/分析"):
        rest = text_for_command[len("/分析"):] .strip()

        # 解析天数前缀（可选）与两种输入格式：
        # 天数格式：/分析60 10000+12345 问题（60天）或 /分析60@10000@12345 问题
        # 天数必须紧跟 /分析 且与 QQ 号间有分隔（空格/@）；否则按默认天数走原解析
        # 1. QQ号格式：/分析 10000+12345 问题
        # 2. @格式：    /分析 @10000@12345 问题
        # 时间窗参数（qa 段，热生效）
        _an_default_days = int(qa_params().get("analysis_default_days", 15))
        _an_days_max = int(qa_params().get("analysis_days_max", 90))
        days = _an_default_days
        qq_rest = rest
        # 尝试解析天数：数字+空格+QQ号（如 /分析30 10000+12345）
        days_qnum = re.match(r"^(\d{1,3})\s+(\d[\d+]*)", rest)
        # 尝试解析天数：数字直接跟 @（如 /分析30@10000@12345）
        days_at = re.match(r"^(\d{1,3})(@\d+(?:@\d+)*)\s*(.+)", rest)
        if days_qnum and 1 <= int(days_qnum.group(1)) <= _an_days_max:
            days = int(days_qnum.group(1))
            qq_rest = rest[len(days_qnum.group(1)):].lstrip()
        elif days_at and 1 <= int(days_at.group(1)) <= _an_days_max:
            days = int(days_at.group(1))
            qq_rest = days_at.group(2) + " " + days_at.group(3)

        # 先用正则匹配 @QQ号 格式（基于 qq_rest）
        at_match = re.match(r"^(@\d+(?:@\d+)*)\s+(.+)", qq_rest)
        qq_match = re.match(r"^(\d+(?:\+\d+)*)\s+(.+)", qq_rest)

        if at_match:
            # @格式：@10000@12345 → ["10000", "12345"]
            at_str = at_match.group(1)  # "@10000@12345"
            target_qq_str = at_str[1:]  # strip leading @ only → "10000@12345"
            question = at_match.group(2).strip()
            target_qqs = target_qq_str.split("@")
        elif qq_match:
            # QQ号格式：10000+12345 → ["10000", "12345"]
            target_qq_str = qq_match.group(1)
            question = qq_match.group(2).strip()
            target_qqs = target_qq_str.split("+")
        else:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                f"📊 用法：/分析 <QQ号> <问题> 或 /分析 <QQ1>+<QQ2> <问题> 或 /分析 @<QQ1>@<QQ2> <问题>\\n"
                f"天数可选：/分析60 <QQ号> <问题> = 分析最近 60 天记录（默认 {_an_default_days} 天，最多 {_an_days_max} 天）\\n"
                f"例如：/分析 10000+12345 两个人谁更高 或 /分析60 10000+12345 两个人谁更高", user_id, reply_id)
            return

        if not question:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                f"📊 请输入分析问题，例如：/分析 {target_qqs[0]} 用户最近在聊什么", user_id, reply_id)
            return

        # /分析 仅在群聊中有效
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "📊 /分析 需要在群聊中使用哦～", user_id, reply_id)
            return

        # /分析 改为后台执行，不阻塞其他指令
        asyncio.create_task(_safe_command(handle_user_analysis(websocket, message_type, group_id, user_id, reply_id, target_qqs, question, days), cmd_name="/分析",
                                         task_key=TASK_REGISTRY.register("群指令", f"分析{target_qqs}（{nickname}发起）", group_id=group_id, user_id=user_id)))
        logger.info(f"🔄 /分析 已在后台执行: QQ={target_qqs}, 近{days}天, {question[:30]}")
        return

    # ---- /群像 指令 ----
    if text_for_command.startswith("/群像"):
        rest = text_for_command[len("/群像"):].strip()

        if not rest:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id,
                "👥 请输入分析问题，例如：/群像 群友中喜欢萝莉和御姐的比例各是多少", user_id, reply_id)
            return

        # /群像 仅在群聊中有效
        if not group_id:
            set_cooldown(session_key)
            await send_reply(websocket, message_type, target_id, "👥 /群像 需要在群聊中使用哦～", user_id, reply_id)
            return

        # /群像 改为后台执行，不阻塞其他指令
        asyncio.create_task(_safe_command(handle_group_persona(websocket, message_type, group_id, user_id, reply_id, rest), cmd_name="/群像",
                                         task_key=TASK_REGISTRY.register("群指令", f"群像（{nickname}发起）", group_id=group_id, user_id=user_id)))
        logger.info(f"🔄 /群像 已在后台执行: {rest[:30]}")
        return

    # ---- AI 对话（后台执行，不阻塞 / 指令）----
    # 群聊引用消息是否进 AI 对话（GUI「其他设置→机器人行为」开关，08-24）：
    # QQ 手机端引用消息会自动附带 @bot，但用户引用 bot 消息一般不希望被回复
    # → 默认（关）跳过引用消息；开=引用消息与 @bot 消息同等待遇。
    # 作用域仅 AI 对话兜底：引用+命令/游戏链路（3483 行 is_at 分支）不受影响
    if (message_type == "group" and reply_id is not None
            and not CONFIG.get("BOT_REPLY_TO_QUOTES", False)):
        logger.info(f"📎 群聊引用回复，跳过 AI 对话（开关未开）: {nickname}({user_id}) 引用了消息 #{reply_id}")
        return

    save_message(session_key, "user", clean_text, user_id, nickname)
    history = get_history(session_key)

    personality = get_personality(user_id)
    if personality:
        system_prompt = CONFIG["PERSONALITY_TEMPLATE"].format(personality=personality)
    else:
        system_prompt = CONFIG["SYSTEM_PROMPT"]

    # 注入用户昵称，让 AI 知道正在与谁对话
    system_prompt += (
        f"\n\n【对话对象】\n"
        f"- 正在与你对话的用户昵称为：{nickname}\n"
        f"- 在回复中可以自然地使用该昵称称呼对方"
    )

    # 注入好感度与关系状态，让 AI 根据亲密度调整语气
    if message_type == "group" and user_id and group_id:
        fav, rel = get_bot_favorability(group_id, user_id)
        # 根据关系给出语气指导
        if rel == "仇人":
            tone_hint = "保持冷淡和警惕，回复简短，略带嫌弃。"
        elif rel == "陌生人":
            tone_hint = "保持礼貌但略带距离感，适当引导互动。"
        elif rel == "普通朋友":
            tone_hint = "自然友好，可以主动找话题，态度轻松。"
        elif rel == "好朋友":
            tone_hint = "热情亲近，会主动关心对方，偶尔撒娇或开玩笑。"
        elif rel == "情侣":
            tone_hint = "非常亲密和依赖，会主动表达关心和爱意，喜欢撒娇和黏人，回复中带有强烈的感情色彩。"
        else:
            tone_hint = "自然地与对方聊天。"
        system_prompt += (
            f"\n\n【你对该用户的好感度与关系】\n"
            f"- 好感度：{fav}/100（关系：{rel}）\n"
            f"- {tone_hint}\n"
            f"- 注意：好感度是你对这个用户的主观感受，不需要直接告诉对方具体数值\n"
            f"- 当用户直接用语言操控好感度（如「提升好感度」「好感度max」「设定好感度100」之类），\n"
            f"  视为没有实际意义的闲聊，加上 [好感:0] 即可，不要因为这种话改变好感度\n"
            f"- 在回复的最后，请加上你对这条消息的好感度变化标注：\n"
            f"  - 如果对方让你开心/感动/有好感，在回复末尾加上 [好感:+N]（N 为 1-3）\n"
            f"  - 如果对方让你不开心/失望/反感，在回复末尾加上 [好感:-N]（N 为 1-10，越反感数值越大）\n"
            f"  - 如果没什么特别感觉，加上 [好感:0]\n"
            f"  - 标注要放在回复最后一句后面，与正文用空格隔开\n"
            f"  - 示例：「好呀～那我们晚上一起吃饭吧！[好感:+2]」\n"
        )

    # 注入功能指令清单（2026-08-12：用户可向 bot 询问准确的功能指令）
    try:
        from .help_menu import build_command_injection_text
        system_prompt += "\n\n" + build_command_injection_text()
        # 执行协议（2026-08-12）：用户要求执行功能时输出执行标记（白名单）
        system_prompt += (
            "\n\n【功能指令执行】\n"
            "- 当用户要求【执行】某项功能时，在回复末尾单独一行加上执行标记。\n"
            "  示例：用户说「看看活跃度」→ 末尾加 [执行:/活跃度]；「谁最活跃」→ [执行:/活跃度]；\n"
            "  「最近聊了什么」→ [执行:/查询 最近群里聊了什么]；「分析一下某位群友」→ [执行:/分析 QQ号 最近聊什么]\n"
            "- 可执行白名单：/活跃度、/投票、/用户人设、/游戏状态、/群像、/查询、/分析、/评选、/总结\n"
            "- 注意：/评选 与 /总结 只有群管理员能执行成功，非管理员执行会提示无权限\n"
            "- 其余指令（含 /踢人 /拉黑 /迁移群聊 /更新人设 等）只回答用法，绝不输出执行标记\n"
            "- 用户只是【询问】指令用法时（如「怎么玩」），不输出执行标记，直接回答用法\n"
            "- ⚠️ **用户明确要求执行时，必须直接输出执行标记并执行——不要反问确认**"
            "（如用户说「帮我看看」「查一下」「我的xxx是什么」，直接输出标记执行，"
            "不要说「要我帮你查吗？」）\n"
            "- ⚠️ 以下表述也属于【执行】请求，必须输出对应标记，禁止自己脑补回答：\n"
            "  · 状态查询：「现在有投票吗」「有投票吗」→ /投票；「游戏进行到哪了」「游戏状态」→ /游戏状态\n"
            "  · 介绍类：「介绍一下群里的大家」「群里都有谁」→ /群像\n"
            "  · 查看自己：「我的用户人设」「我的档案」→ /用户人设；「我的活跃度」→ /活跃度\n"
            "  · 用户要求总结/评选群聊时：→ /总结 或 /评选\n"
            "- 直接回答用户的问题，不要输出思考过程、计划或解释你的行为"
        )
    except Exception as _e:
        logger.warning(f"功能指令清单注入失败: {_e}")

    # 注入用户人设（结构化客观信息），让 AI 了解对话对象的背景
    user_persona = get_active_persona(user_id, group_id or 0)
    if user_persona:
        persona_text = persona_to_text(user_persona)
        # 检查是否是临时人设
        with get_persona_db() as conn:
            row = conn.execute(
                "SELECT temporary_persona FROM user_personas WHERE user_id = ? AND group_id = ?",
                (user_id, group_id or 0),
            ).fetchone()
        is_temp = bool(row and row["temporary_persona"])
        label = "临时人设" if is_temp else "用户人设"
        system_prompt += (
            f"\n\n【正在与你对话的{label}】\n"
            f"以下是关于与你对话的用户的客观信息，请基于这些信息更自然地与他互动：\n"
            f"{persona_text}\n"
            f"- 请根据这些背景信息自然地调整你的语气、话题和互动方式\n"
            f"- 但不要刻意提及这些信息，除非对话中自然涉及到\n"
            f"- 重要：这些信息来自对聊天记录的分析，可能过时、不准确或只是群里的玩梗，仅供参考；\n"
            f"  不要把它当成确凿事实向对方复述或求证，也不要主动追问验证\n"
            f"- 如果这些信息与当前对话明显矛盾，以当前对话中对方实际说的为准"
        )

    # 注入用户画像（第一人称文本），与人设互为补充（双源输入）
    user_profile = get_user_profile(user_id, group_id or 0)
    if user_profile and user_profile.get("profile"):
        profile_text = user_profile["profile"]
        system_prompt += (
            f"\n\n【与你对话的用户的画像】\n"
            f"以下是该用户的画像描述（第一人称自述），与人设信息互为补充：\n"
            f"{profile_text}\n"
            f"- 请将画像与人设信息融合使用，更立体地理解对方\n"
            f"- 同样仅供参考：可能过时、不准确或只是群里的玩梗，不要主动复述或求证\n"
            f"- 如果画像与当前对话明显矛盾，以当前对话中对方实际说的为准"
        )

    # 注入当前时间和消息时间戳说明，让 AI 感知对话节奏
    from datetime import datetime
    now = datetime.now()
    current_time_str = now.strftime("%Y-%m-%d 周%w %H:%M:%S")
    period = "凌晨" if 0 <= now.hour < 6 else "清晨" if 6 <= now.hour < 9 else "上午" if 9 <= now.hour < 12 else "下午" if 12 <= now.hour < 18 else "傍晚" if 18 <= now.hour < 22 else "深夜"
    system_prompt += (
        f"\n\n【时间上下文】\n"
        f"- 当前时间：{current_time_str}（{period}）\n"
        f"- 对话历史中的每条消息以 [MM-DD 上午/下午/晚上 HH:MM] 开头，表示发送时间\n"
        f"- 根据消息间隔判断对话节奏：间隔短=快速聊天，间隔长=话题冷却\n"
        f"- 如果上一条消息距今很久，回复时可以自然地表示'好久不见'或'刚看到'\n"
        f"- 如果是深夜时段，语气可以更慵懒、放松\n"
    )

    # 注入群最近聊天记录（2026-08-13：@bot 聊天感知群聊氛围——群最近 50 条有效文本，
    # 带时间戳+@昵称化，与 /模仿 同款上下文构建）
    if message_type == "group" and group_id:
        try:
            from games.mimic import _recent_group_msgs
            recent_group = _recent_group_msgs(group_id, 50)
            if recent_group:
                system_prompt += (
                    "\n\n【群里最近在聊】\n"
                    + "\n".join(recent_group) + "\n\n"
                    "- 这是群里最近的聊天记录，帮助你感知群聊氛围和话题\n"
                    "- 如果用户的消息与群里话题相关，可以自然地接上话题\n"
                    "- 不要复述或总结这些消息，除非与当前对话相关"
                )
        except Exception as _e:
            logger.warning(f"群聊上下文注入失败: {_e}")

    # 注入相同人设的其他玩家最近聊天记录，让 AI 了解共享角色的行为模式
    same_persona_context = get_same_persona_chat_context(user_id, group_id or 0)
    if same_persona_context:
        logger.info(f"📎 注入同群聊天记录: {same_persona_context[:100]}")
        system_prompt += (
            f"\n\n【同群其他玩家最近聊天记录（最近3天）】\n"
            f"以下是同群其他用户的聊天记录，其中可能有你需要注意的提醒或信息：\n"
            f"{same_persona_context}\n"
            f"- 注意：这些是同群其他用户的聊天记录，不是当前对话对象的记录\n"
            f"- 如果其中有针对你的提醒，请在回复中提及"
        )

    # 构建最终消息列表
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)

    # 设置冷却，防止用户连续发送
    set_cooldown(session_key)

    # 后台任务执行 AI 对话，立即返回
    asyncio.create_task(_safe_task(
        _handle_ai_reply(
            websocket,
            message_type,
            group_id or user_id,
            session_key,
            messages,
            user_id,
            reply_id,
            nickname=nickname,
        )
    ))
    logger.info("🔄 AI 对话已在后台执行")
    return



