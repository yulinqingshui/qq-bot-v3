"""
scheduler.py — 后台定时任务调度器

包含：
- 每日定时更新（人设 → 画像 → 题库）
- 每天 11:30 / 22:30 半日报告（评选 + 总结，分别覆盖上/下半天）
- /总结 与 /评选 指令处理
- 辅助函数：chunk_messages_by_token, _format_chat_messages
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, time as dtime
from typing import Optional

from .database import get_db, get_persona_db, get_today_chat_log, get_today_chat_log_merged, get_cluster_id, get_cluster_groups, get_active_groups_by_task, get_cluster_master_group, decay_bot_favorability
import core.sender as sender
from core.sender import get_bot_uin  # 2026-08-22：scheduler 多处用 get_bot_uin 判 bot 自己，函数内 import 作用域不跨函数（1059 行 NameError 修复）
from .llm import call_llm, llm_enabled
from .persona import (
    get_active_persona,
    persona_to_text,
    _do_update_persona,
    _do_update_profile,
    _do_update_profile_and_persona,
    BATCH_CHARS,
    _clean_cq_codes,
    _normalize_u_refs,
)
# 2026-08-22 查询/分析命令配置化：/总结 /评选 /定时半日报告 的提示词与
# LLM 参数走 qa 段（热生效），默认值 = 原硬编码行为
from .qa_prompts import (
    render_prompt, qa_params, qa_llm, qa_llm_scope, thinking_kwargs,
)
# 2026-08-22 任务列表：定时任务登记（总览页面板）
from .task_registry import TASK_REGISTRY

logger = logging.getLogger("qq-bot")

async def _periodic_napcat_watchdog() -> None:
    """NapCat 连接看门狗（2026-08-10）：每 60 秒检查连接状态。

    连接断开时：限频告警日志（每 5 分钟一次，避免刷屏）+ 更新状态文件。
    恢复时：记录恢复日志 + 更新状态文件。
    NapCat 掉线（QQ 会话过期需扫码）时 bot 无法收发消息/执行定时更新，
    通过日志与状态文件（data/napcat_status.txt）提示用户处理。
    """
    from .sender import _active_websocket, get_bot_uin

    last_warn_ts = 0.0
    last_connected_ts = 0.0
    while True:
        try:
            ws = _active_websocket
            if ws is None:
                now = time.time()
                if now - last_warn_ts >= 300:  # 限频：每 5 分钟告警一次
                    last_warn_ts = now
                    logger.error(
                        "🚨 NapCat 连接断开——bot 无法收发消息/定时更新！"
                        # 2026-08-23：文案纠偏——原"docker restart 多数自动恢复
                        # token"对登录态失效无效（08-23 复盘：QQ 踢登录态后
                        # watchdog 反复重启帮倒忙，二维码刷不停）。
                        "处理: 先 docker logs napcat 看根因——"
                        "有「请扫描下面的二维码/登录态已失效」= QQ 登录态失效，"
                        "GUI 总览页 NapCat 卡片扫码（重启无效）；"
                        "无扫码提示 = OneBot 服务半死，docker restart napcat"
                        "（状态文件: data/napcat_status.txt）"
                    )
                if last_connected_ts and now - last_connected_ts > 3600:
                    logger.warning(
                        f"⏳ NapCat 断开已持续 {int((now - last_connected_ts) / 60)} 分钟，"
                        f"请尽快处理（GUI 扫码 / docker restart napcat，见状态文件）"
                    )
                    last_connected_ts = now  # 重置，避免重复告警
            else:
                if last_connected_ts == 0 or ws is not None:
                    pass
                last_connected_ts = time.time()
        except Exception:
            pass
        await asyncio.sleep(60)


async def _periodic_napcat_primary() -> None:
    """NapCat 主账号配置巡检（2026-08-21 多账号抢连修复）。

    每 30 分钟检查 NapCat 配置漂移：非主账号的 onebot11_<uin>.json
    被改回 enable=true（手改 / 新账号回落生成 / 其他途径）→ 收敛回单主状态
    + docker restart 使配置生效。配置只在启动时读取，漂移必须重启才收敛。

    正常情况（配置合规）零日志、零操作。
    """
    from . import napcat_primary
    from .config import CONFIG

    await asyncio.sleep(120)  # 启动宽限：等首收敛与 NapCat 就绪
    while True:
        try:
            config_dir = CONFIG.get("NAPCAT_CONFIG_DIR", "")
            container = CONFIG.get("NAPCAT_CONTAINER", "napcat")
            if config_dir:
                primary = napcat_primary.get_primary()
                if primary:
                    bad = napcat_primary.drift_report(config_dir, primary["uin"])
                    if bad:
                        logger.warning(
                            f"🧹 NapCat 配置漂移: 非主账号桥被开启 uin={bad}"
                            f"（主账号 {primary['uin']}），收敛回单主状态"
                        )
                        r = await asyncio.to_thread(
                            napcat_primary.converge, config_dir,
                            primary["uin"], container
                        )
                        logger.warning(f"🧹 NapCat 巡检收敛完成: {r}")
        except Exception as e:
            logger.warning(f"⚠️ NapCat 主账号巡检异常: {e}")
        await asyncio.sleep(1800)


async def _periodic_retention_cleanup() -> None:
    """存档保留期清理（2026-08-20 消息管理）：每天 3:00 执行。

    按 config.yaml 的 archive.text_retention_days / media_retention_days
    （0=永久）清理超期数据：
      文本：message_archive + group_chat_cache 超期记录
      媒体：image/voice/video/recall_image 超期记录 + 对应文件
    只删"记录"和"文件"，不动会话历史 chat_messages（有独立 400 条限制）。
    """
    from .config import CONFIG
    from .database import get_db

    await asyncio.sleep(60)  # 等 bot 初始化完
    while True:
        try:
            # 等到当天 3:00
            now = datetime.now()
            target = datetime.combine(now.date(), dtime(3, 0, 0))
            if now.time() >= dtime(3, 0, 0):
                target = target + timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            text_days = int(CONFIG.get("TEXT_RETENTION_DAYS", 0))
            media_days = int(CONFIG.get("MEDIA_RETENTION_DAYS", 0))
            if text_days <= 0 and media_days <= 0:
                continue  # 全部永久保留，跳过

            cleaned = {"text": 0, "media": 0, "files": 0}
            with get_db() as conn:
                # ---- 文本保留期 ----
                if text_days > 0:
                    cutoff = time.time() - text_days * 86400
                    cur = conn.execute(
                        "DELETE FROM message_archive WHERE created_at < ?", (cutoff,))
                    cleaned["text"] += cur.rowcount
                    cur = conn.execute(
                        "DELETE FROM group_chat_cache WHERE created_at < ?", (cutoff,))
                    cleaned["text"] += cur.rowcount
                # ---- 媒体保留期（记录 + 文件）----
                if media_days > 0:
                    cutoff = time.time() - media_days * 86400
                    for table, ts_col in (
                        ("image_archive", "created_at"),
                        ("voice_archive", "created_at"),
                        ("video_archive", "created_at"),
                        ("recall_image", "recalled_at"),
                    ):
                        try:
                            rows = conn.execute(
                                f"SELECT file_path FROM {table} WHERE {ts_col} < ? AND file_path != ''",
                                (cutoff,)).fetchall()
                            for r in rows:
                                p = r["file_path"]
                                if p and os.path.isfile(p):
                                    try:
                                        os.remove(p)
                                        cleaned["files"] += 1
                                    except OSError:
                                        pass
                            cur = conn.execute(
                                f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,))
                            cleaned["media"] += cur.rowcount
                        except Exception as e:
                            logger.warning(f"清理 {table} 失败: {e}")
            total = sum(cleaned.values())
            if total:
                logger.info(f"🧹 存档保留期清理: 文本 {cleaned['text']} 条, "
                            f"媒体 {cleaned['media']} 条, 文件 {cleaned['files']} 个")
        except Exception as e:
            logger.error(f"存档保留期清理异常: {e}", exc_info=True)
        await asyncio.sleep(3600)  # 兜底：每小时醒一次，防时钟漂移/休眠错过 3:00


# ---- 定时任务标志位 ----
_daily_update_started = False
_reports_started = False  # 半日报告任务（11:30/22:30 评选+总结）启动标志
_napcat_watchdog_started = False  # NapCat 连接看门狗启动标志（2026-08-10）
_favorability_decay_started = False
_retention_cleanup_started = False  # 存档保留期清理（2026-08-20 消息管理）
_napcat_primary_started = False  # NapCat 主账号巡检（2026-08-21 多账号抢连修复）

# ---- 定时任务暂停控制 ----
_daily_update_paused = False

# ---- 定时任务锁（懒初始化，确保事件循环就绪后再创建）----
_schedule_lock: Optional[asyncio.Lock] = None

def _get_schedule_lock() -> asyncio.Lock:
    """获取定时任务锁，首次调用时初始化（确保事件循环已就绪）"""
    global _schedule_lock
    if _schedule_lock is None:
        _schedule_lock = asyncio.Lock()
    return _schedule_lock

# ---- 题库补充全局互斥锁（防止手动 /补充题库 与每日任务并发）----
_question_refill_lock: Optional[asyncio.Lock] = None

def _get_question_refill_lock() -> asyncio.Lock:
    """获取题库补充互斥锁，手动 /补充题库 与每日任务共享"""
    global _question_refill_lock
    if _question_refill_lock is None:
        _question_refill_lock = asyncio.Lock()
    return _question_refill_lock


# ============================================================
# 辅助函数
# ============================================================
def chunk_messages_by_token(messages: list[str], target_tokens: int = BATCH_CHARS) -> list[list[str]]:
    """
    按累计 token 数（~字符数）对消息列表分批，而不是按条数。
    中文 1 字符 ≈ 1 token，英文 1 词 ≈ 0.7 token；这里保守按 1 字符 = 1 token 估算。
    """
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_len = 0
    for msg in messages:
        msg_len = len(msg)
        if current_len + msg_len > target_tokens and current_chunk:
            chunks.append(current_chunk)
            current_chunk = [msg]
            current_len = msg_len
        else:
            current_chunk.append(msg)
            current_len += msg_len
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _format_chat_messages(chat_log: list[dict], start_idx: int = 0):
    """
    将 chat_log 格式化为紧凑消息行列表（节省 token）。
    
    格式：HH:MM U1: 内容
    - 时间只保留时分
    - 用户名用短 ID（U1, U2...）
    - 合并同一用户连续发言
    - 返回 (lines, short_map)；short_map: {short: {"nickname", "qq"}}，
      供 LLM 输出中的 U 编号引用归一化为 昵称(qq号)
    """
    from datetime import datetime
    
    # 构建 user_id → 短 ID 映射
    uid_to_short: dict[int, str] = {}
    short_to_nick: dict[str, str] = {}
    counter = 1
    for entry in chat_log:
        uid = entry.get("user_id", 0)
        if uid not in uid_to_short:
            short = f"U{counter}"
            uid_to_short[uid] = short
            short_to_nick[short] = entry["nickname"]
            counter += 1
    
    # 格式化 + 合并连续消息
    _trunc = int(qa_params().get("msg_truncate_chars", 300))  # 消息截断字数（qa 段热生效）
    formatted: list[tuple[str, int, str]] = []
    for entry in chat_log:
        dt = datetime.fromtimestamp(entry["created_at"])
        time_str = dt.strftime("%H:%M")
        # 与画像/人设一致的格式：CQ 码清理 + @数字 → @U短ID（uid_to_short 映射）
        content = _clean_cq_codes(entry.get("content") or "", 0, {}, uid_to_short)
        content = content[:_trunc]
        # 消息内真实换行转义为字面 \n（一条消息一行契约，方案B）
        content = content.replace("\n", "\\n")
        uid = entry.get("user_id", 0)
        short = uid_to_short[uid]
        
        # 合并同一用户连续消息
        if formatted and formatted[-1][1] == uid and formatted[-1][0] == time_str:
            prev_content = formatted[-1][2]
            merged = prev_content + content
            if len(merged) <= _trunc:
                formatted[-1] = (time_str, uid, merged)
            else:
                formatted.append((time_str, uid, content))
        else:
            formatted.append((time_str, uid, content))
    
    # 构建人物映射表（与画像/人设格式一致：U编号=昵称(QQ号)，表头后空行；join 时会补一个 \n 形成空行）
    # QQ 号是唯一锚点：昵称可能跨群不同/随时改名，绝不能仅凭昵称认人（Pitfall 87）
    nick_map_lines = [f"{short}={short_to_nick[short]}({uid})" for uid, short in uid_to_short.items()]
    header = f"人物:\n{chr(10).join(nick_map_lines)}\n"
    
    # 构建消息行（带序号）
    lines = [header]
    for i, (time_str, uid, content) in enumerate(formatted, 1):
        short = uid_to_short[uid]
        lines.append(f"#{i} {time_str} {short}: {content}")
    
    short_map = {
        short: {"nickname": short_to_nick[short], "qq": str(uid)}
        for uid, short in uid_to_short.items()
    }
    return (lines[start_idx:] if start_idx else lines), short_map


def _seconds_until_next_midnight() -> int:
    """计算距离下一个 0 点的秒数（若当前已过 0 点则到明天 0 点）。"""
    now = datetime.now()
    target_time = dtime(0, 0, 0)
    if now.time() < target_time:
        return int((datetime.combine(now.date(), target_time) - now).total_seconds())
    return int((datetime.combine(now.date() + timedelta(days=1), target_time) - now).total_seconds())


# ============================================================
# 批次提取中间结果落库（/总结、/评选 阶段1，断点恢复防重复消耗 token）
# ============================================================
_EXTRACT_CACHE_WINDOW = 7200  # 2 小时窗口：一轮总结/评选（含 LLM 重试）远小于此


def _save_extract_batch(
    task_type: str, group_id: int, date: str,
    batch_num: int, total_batches: int, batch_char_count: int,
    raw_response: str, is_valid: int,
) -> None:
    """保存 /总结、/评选 批次提取中间结果（失败批次 is_valid=0 也保存，便于追溯）。"""
    try:
        from .database import get_daily_reports_db
        with get_daily_reports_db() as db:
            # 兜底：表不存在时先建（防止旧库未执行 schema 初始化）
            db.execute(
                "CREATE TABLE IF NOT EXISTS batch_extract_results ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, task_type TEXT NOT NULL, group_id INTEGER NOT NULL, "
                "date TEXT NOT NULL, batch_index INTEGER NOT NULL, total_batches INTEGER NOT NULL, "
                "batch_char_count INTEGER NOT NULL, raw_response TEXT NOT NULL, "
                "is_valid INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO batch_extract_results (task_type, group_id, date, batch_index, total_batches, "
                "batch_char_count, raw_response, is_valid, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_type, group_id, date, batch_num, total_batches,
                 batch_char_count, raw_response, is_valid, time.time())
            )
    except Exception as e:
        logger.warning(f"保存批次提取中间结果失败 ({task_type} 批次 {batch_num}): {e}")


def _load_extract_batch_cache(task_type: str, group_id: int, date: str, total_batches: int) -> dict[int, dict]:
    """断点恢复：读取该群最近一轮同 task_type/date/total_batches 的成功批次结果。

    返回 {batch_num: {"raw_response": str, "batch_char_count": int}}。
    只有 is_valid=1（成功）的批次参与复用；失败批次重新调用 LLM。
    """
    cache: dict[int, dict] = {}
    try:
        from .database import get_daily_reports_db
        with get_daily_reports_db() as db:
            rows = db.execute(
                "SELECT batch_index, raw_response, batch_char_count FROM batch_extract_results "
                "WHERE task_type = ? AND group_id = ? AND date = ? AND total_batches = ? AND is_valid = 1 "
                "AND created_at >= (SELECT COALESCE(MAX(created_at), 0) FROM batch_extract_results "
                "                    WHERE task_type = ? AND group_id = ? AND date = ? AND total_batches = ?) - ?",
                (task_type, group_id, date, total_batches,
                 task_type, group_id, date, total_batches, _EXTRACT_CACHE_WINDOW)
            ).fetchall()
        for r in rows:
            cache[r["batch_index"]] = {"raw_response": r["raw_response"], "batch_char_count": r["batch_char_count"]}
    except Exception as e:
        logger.warning(f"加载批次提取缓存失败 ({task_type}): {e}")
    return cache


# ============================================================
# Reduce 阶段（阶段 2）：手动指令与定时任务共用
# ============================================================
async def _reduce_summary(
    group_id: int, today_str: str, summaries_text: str,
    users_text: str, total_messages: int, user_count: int,
    short_map: dict | None = None,
    period_word: str = "",
) -> str:
    """总结 Reduce：合并批次摘要为最终总结，落库并返回报告文本。

    today_str 同时作为落库轮次键（手动指令为 YYYY-MM-DD；定时半日报告为
    YYYY-MM-DD-AM / YYYY-MM-DD-PM，同日两场互不覆盖）。period_word 为
    "上午"/"下午" 时报告标题带场次标签（定时半日报告用），空串保持全天语义。
    """
    display_date = today_str[:10]
    period_suffix = f"（{period_word}）" if period_word else ""
    # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
    system_prompt = render_prompt("summary_reduce_system", {
        "display_date": display_date,
        "period_suffix": period_suffix,
    })

    user_prompt = render_prompt("summary_reduce_user", {
        "period_word": period_word,
        "users_text": users_text,
        "total_messages": total_messages,
        "user_count": user_count,
        "summaries_text": summaries_text,
    })

    # LLM 参数（qa.llm.summary 段；⚠️ Reduce 默认 131072=MAX_TOKENS_LONG 异类，忠实保留）
    _sm = qa_llm_scope("summary")
    _common = qa_llm()
    logger.info("📋 阶段2 开始生成最终总结...")
    reply = await call_llm(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        max_tokens=int(_sm.get("reduce_max_tokens", 131072)),
        temperature=float(_common.get("temperature", 0.7)),
        timeout=int(_common.get("timeout", 1800)),
        **thinking_kwargs(_sm.get("reduce_thinking", "on")),
    )
    reply = reply.strip()

    # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
    reply = _normalize_u_refs(reply, short_map)

    # ---- 保存到数据库 ----
    from .database import save_daily_summary
    save_daily_summary(group_id, today_str, reply, total_messages, user_count)
    return reply


async def _reduce_evaluation(
    group_id: int, today_str: str, candidates_text: str,
    users_text: str, total_messages: int, user_count: int,
    short_map: dict | None = None,
    period_word: str = "",
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
) -> str:
    """评选 Reduce：合并批次候选为最终评选（附活跃度排行），落库并返回报告文本。

    today_str 同时作为落库轮次键（手动指令为 YYYY-MM-DD；定时半日报告为
    YYYY-MM-DD-AM / YYYY-MM-DD-PM）。period_word 为 "上午"/"下午" 时报告标题
    带场次标签；start_time/end_time 用于活跃度排行与评选内容保持同一时间窗口。
    """
    from collections import Counter

    # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
    system_prompt = render_prompt("evaluation_reduce_system")
    user_prompt = render_prompt("evaluation_reduce_user", {
        "period_word": period_word,
        "users_text": users_text,
        "candidates_text": candidates_text,
    })

    # LLM 参数（qa.llm.evaluation 段）
    _ev = qa_llm_scope("evaluation")
    _common = qa_llm()
    logger.info("🏆 阶段2 开始最终评选...")
    reply = await call_llm(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        max_tokens=int(_ev.get("reduce_max_tokens", 16384)),
        temperature=float(_common.get("temperature", 0.7)),
        timeout=int(_common.get("timeout", 1800)),
        **thinking_kwargs(_ev.get("reduce_thinking", "on")),
    )
    reply = reply.strip()

    # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
    reply = _normalize_u_refs(reply, short_map)

    # ---- 生成活跃度排行图表（按 user_id 聚合，解决改昵称导致分开计算的问题）----
    # 时间窗口与评选内容一致（定时半日报告传 start_time/end_time；手动指令默认
    # 过去 report_window_hours 小时，qa 段热生效）
    if start_time is None or end_time is None:
        _now = time.time()
        _win_h = int(qa_params().get("report_window_hours", 24))
        _st = start_time if start_time is not None else _now - _win_h * 3600
        _en = end_time if end_time is not None else _now
    else:
        _st, _en = start_time, end_time
    chat_log = get_today_chat_log_merged(group_id, _st, _en)
    user_counts = Counter()
    user_latest_nick = {}  # user_id -> 最新的昵称（最后一条消息的昵称）
    for entry in chat_log:
        uid = entry.get("user_id")
        nick = entry.get("nickname", str(uid))
        if uid:
            user_counts[uid] += 1
            user_latest_nick[uid] = nick
    top_users = user_counts.most_common(10)
    max_count = top_users[0][1] if top_users else 1
    bar_width = 8
    ranking_lines = [f"\n\n📊 今日{period_word}发言活跃度排行："]
    for rank, (uid, count) in enumerate(top_users, 1):
        nickname = user_latest_nick[uid]
        bar_len = int(count / max_count * bar_width)
        nick = nickname[:4]
        nick_padded = nick + "\u00a0" * max(0, 4 - len(nick))  # NO-BREAK SPACE, QQ不吞
        bar = "█" * bar_len + "░" * (bar_width - bar_len)
        ranking_lines.append(f"  {rank}. {nick_padded} {bar} {count}")
    ranking_text = "\n".join(ranking_lines)

    # ---- 保存到数据库 ----
    from .database import save_daily_evaluation
    save_daily_evaluation(group_id, today_str, reply + ranking_text, total_messages, user_count)
    return reply + ranking_text


# ============================================================
# /总结 指令处理器
# ============================================================
async def handle_summary(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int], group_id: int
) -> None:
    """
    /总结 指令：总结概括当天的群聊聊天内容。
    两阶段策略：分批摘要（每批 2000 条）→ 汇总总结。
    """
    from .database import set_cooldown, _session_key

    # LLM 总开关早退（2026-08-21 审计）：不发起 Map-Reduce、不写库、不刷日志
    if not llm_enabled():
        await sender.send_reply(websocket, message_type, target_id,
                                "🔕 LLM 总开关关闭，暂时无法总结（GUI 总览页 LLM 板块可开启）",
                                user_id, reply_id)
        return

    # 使用合并消息：如果群有集群配置，则合并同集群所有群的消息
    # 时间窗=过去 report_window_hours 小时（qa 段热生效；原默认 24h）
    _win_h = int(qa_params().get("report_window_hours", 24))
    _now = time.time()
    chat_log = get_today_chat_log_merged(group_id, _now - _win_h * 3600, _now)

    if len(chat_log) < 2:
        await sender.send_reply(websocket, message_type, target_id, f"📝 近 {_win_h} 小时的聊天记录还不够丰富，等大家多聊点再总结吧～", user_id, reply_id)
        return

    total_messages = len(chat_log)

    # ---- 提取活跃用户列表 ----
    users = sorted(set(e["nickname"] for e in chat_log))
    users_text = "、".join(users)

    set_cooldown(_session_key(group_id, user_id))
    await sender.send_reply(websocket, message_type, target_id, f"🔍 正在总结近 {_win_h} 小时的 {total_messages} 条聊天记录，请稍候...", user_id, reply_id)
    logger.info(f"🔍 开始总结分析... (共 {total_messages} 条, {len(users)} 位用户)")

    # ================================================================
    # 阶段 1：按累计 token 数分批摘要（目标 map_batch_chars tokens/batch，qa 段热生效）
    # ================================================================
    chat_lines, short_map = _format_chat_messages(chat_log)
    chunks = chunk_messages_by_token(chat_lines, target_tokens=int(qa_params().get("map_batch_chars", 40000)))
    total_batches = len(chunks)

    # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
    extract_prompt = render_prompt("summary_map_system")

    today_str = datetime.now().strftime("%Y-%m-%d")
    batch_cache = _load_extract_batch_cache("summary", group_id, today_str, total_batches)

    all_summaries = []
    # LLM 参数（qa.llm.summary 段；priority=0 用户指令插队，保持原行为）
    _sm = qa_llm_scope("summary")
    _common = qa_llm()
    # 并行处理所有批次：各批次之间无依赖，可并发
    async def _process_batch(idx: int, chunk: list[str]) -> str:
        batch_num = idx + 1
        batch_text = "\n".join(chunk)
        # 断点恢复：同轮同内容（字符数一致）的成功批次直接复用，不重复调 LLM
        cached = batch_cache.get(batch_num)
        if cached is not None and cached["batch_char_count"] == len(batch_text):
            logger.info(f"♻️ 阶段1 批次 {batch_num}/{total_batches} 复用缓存")
            # 旧缓存可能含 U 编号引用，统一归一化为 昵称(qq号)
            return _normalize_u_refs(cached["raw_response"], short_map)
        logger.info(f"📝 阶段1 处理批次 {batch_num}/{total_batches}...")
        msg = render_prompt("summary_map_user", {
            "batch_num": batch_num,
            "total_batches": total_batches,
            "batch_text": batch_text,
        })
        reply = await call_llm([{"role": "system", "content": extract_prompt}, {"role": "user", "content": msg}], max_tokens=int(_sm.get("map_max_tokens", 131072)), priority=0, source="每日总结",
                               temperature=float(_common.get("temperature", 0.7)),
                               timeout=int(_common.get("timeout", 1800)),
                               **thinking_kwargs(_sm.get("map_thinking", "on")))
        reply = reply.strip()
        is_valid = 1 if reply and not reply.startswith(("😵", "🔕")) else 0
        # U 编号引用归一化为 昵称(qq号)（落库与 Reduce 输入均使用归一化文本）
        reply = _normalize_u_refs(reply, short_map)
        _save_extract_batch("summary", group_id, today_str, batch_num, total_batches,
                            len(batch_text), reply, is_valid)
        if not is_valid:
            logger.warning(f"⚠️ 批次 {batch_num} LLM 调用失败")
            return ""  # 失败批次不参与 Reduce（已落库 is_valid=0 可追溯）
        return reply

    all_summaries = await asyncio.gather(*[_process_batch(idx, chunk) for idx, chunk in enumerate(chunks)])

    # ================================================================
    # 阶段 2：汇总总结 — 将所有批次的摘要合并，生成最终总结
    # ================================================================
    summaries_text = "\n---\n".join(all_summaries)
    reply = await _reduce_summary(group_id, today_str, summaries_text, users_text, total_messages, len(users), short_map=short_map)

    await sender.send_reply(websocket, message_type, target_id, reply, user_id, reply_id)
    logger.info(f"✅ 总结结果已发送 ({len(reply)} 字)")


# ============================================================
# /评选 指令处理器
# ============================================================
async def handle_evaluation(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int], group_id: int
) -> None:
    """
    /评选 指令：分析当天聊天记录，评选 5 个有趣栏目。
    两阶段策略：分批摘要 → 汇总评选，处理全部消息而不超出上下文窗口。
    """
    from .database import set_cooldown, _session_key

    # LLM 总开关早退（2026-08-21 审计）：不发起 Map-Reduce、不写库、不刷日志
    if not llm_enabled():
        await sender.send_reply(websocket, message_type, target_id,
                                "🔕 LLM 总开关关闭，暂时无法评选（GUI 总览页 LLM 板块可开启）",
                                user_id, reply_id)
        return

    # 使用合并消息：如果群有集群配置，则合并同集群所有群的消息
    # 时间窗=过去 report_window_hours 小时（qa 段热生效；原默认 24h）
    _win_h = int(qa_params().get("report_window_hours", 24))
    _now = time.time()
    chat_log = get_today_chat_log_merged(group_id, _now - _win_h * 3600, _now)

    if len(chat_log) < 2:
        await sender.send_reply(websocket, message_type, target_id, f"📊 近 {_win_h} 小时的聊天记录还不够丰富，等大家多聊点再评选吧～", user_id, reply_id)
        return

    total_messages = len(chat_log)
    # ---- 提取活跃用户列表 ----
    users = sorted(set(e["nickname"] for e in chat_log))
    users_text = "、".join(users)

    set_cooldown(_session_key(group_id, user_id))
    await sender.send_reply(websocket, message_type, target_id, f"🔍 正在分析近 {_win_h} 小时的 {total_messages} 条聊天记录，请稍候...", user_id, reply_id)
    logger.info(f"🔍 开始评选分析... (共 {total_messages} 条, {len(users)} 位用户)")

    # ================================================================
    # 阶段 1：按累计 token 数分批提取"有趣瞬间"候选（目标 map_batch_chars tokens/batch，qa 段热生效）
    # ================================================================
    chat_lines, short_map = _format_chat_messages(chat_log)
    chunks = chunk_messages_by_token(chat_lines, target_tokens=int(qa_params().get("map_batch_chars", 40000)))
    total_batches = len(chunks)

    # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
    extract_prompt = render_prompt("evaluation_map_system")

    today_str = datetime.now().strftime("%Y-%m-%d")
    batch_cache = _load_extract_batch_cache("evaluation", group_id, today_str, total_batches)

    all_candidates = []
    # LLM 参数（qa.llm.evaluation 段）
    _ev = qa_llm_scope("evaluation")
    _common = qa_llm()
    # 并行处理所有批次：各批次之间无依赖，可并发
    async def _process_batch(idx: int, chunk: list[str]) -> str:
        batch_num = idx + 1
        batch_text = "\n".join(chunk)
        # 断点恢复：同轮同内容（字符数一致）的成功批次直接复用，不重复调 LLM
        cached = batch_cache.get(batch_num)
        if cached is not None and cached["batch_char_count"] == len(batch_text):
            logger.info(f"♻️ 阶段1 批次 {batch_num}/{total_batches} 复用缓存")
            # 旧缓存可能含 U 编号引用，统一归一化为 昵称(qq号)
            return _normalize_u_refs(cached["raw_response"], short_map)
        logger.info(f"📝 阶段1 处理批次 {batch_num}/{total_batches}...")
        msg = render_prompt("evaluation_map_user", {
            "batch_num": batch_num,
            "total_batches": total_batches,
            "batch_text": batch_text,
        })
        reply = await call_llm([{"role": "system", "content": extract_prompt}, {"role": "user", "content": msg}], max_tokens=int(_ev.get("map_max_tokens", 131072)),
                               temperature=float(_common.get("temperature", 0.7)),
                               timeout=int(_common.get("timeout", 1800)),
                               **thinking_kwargs(_ev.get("map_thinking", "on")))
        reply = reply.strip()
        is_valid = 1 if reply and not reply.startswith(("😵", "🔕")) else 0
        # U 编号引用归一化为 昵称(qq号)（落库与 Reduce 输入均使用归一化文本）
        reply = _normalize_u_refs(reply, short_map)
        _save_extract_batch("evaluation", group_id, today_str, batch_num, total_batches,
                            len(batch_text), reply, is_valid)
        if not is_valid:
            logger.warning(f"⚠️ 批次 {batch_num} LLM 调用失败")
            return ""  # 失败批次不参与 Reduce（已落库 is_valid=0 可追溯）
        return reply

    all_candidates = await asyncio.gather(*[_process_batch(idx, chunk) for idx, chunk in enumerate(chunks)])

    # ================================================================
    # 阶段 2：汇总评选 — 所有批次的候选合并，LLM 做出最终评选
    # ================================================================
    candidates_text = "\n---\n".join(all_candidates)
    report = await _reduce_evaluation(group_id, today_str, candidates_text, users_text, total_messages, len(users), short_map=short_map)

    await sender.send_reply(websocket, message_type, target_id, report, user_id, reply_id)
    logger.info(f"✅ 评选结果已发送 ({len(report)} 字)")


# ============================================================
# 合并提取（定时任务）：一次 LLM 调用同时产出总结摘要与评选候选
# 复用人设画像 Combined Map 的思路：同一批聊天记录只输入一次，
# 每场报告提取后落库，总结直接复用缓存，避免 token 消耗翻倍。
# 2026-08-22 配置化：提示词迁至 core/qa_prompts.py（scheduled_combined_extract），
# LLM 参数走 qa.llm.scheduled 段。
# ============================================================

def _parse_candidates_lines(text: str) -> list[dict]:
    """按行解析候选文本（"🏆 昵称: 消息"）→ list[dict]。"""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(🏆|🌸|🔥|🤣|🧠)\s*(.*)$", line)
        if m:
            dim = m.group(1)
            rest = m.group(2).strip()
            if ":" in rest or "：" in rest:
                nick, msg = re.split(r"[:：]", rest, 1)
                out.append({"dimension": dim, "nickname": nick.strip(), "message": msg.strip()})
            else:
                out.append({"dimension": dim, "nickname": "", "message": rest})
        else:
            out.append({"dimension": "", "nickname": "", "message": line})
    return out


def _normalize_candidates(candidates) -> list[dict]:
    """归一化 candidates 字段：数组（新格式）或字符串（旧格式/分隔符行）→ list[dict]。"""
    if isinstance(candidates, list):
        out: list[dict] = []
        for c in candidates:
            if isinstance(c, dict) and (c.get("message") or c.get("nickname")):
                out.append({
                    "dimension": str(c.get("dimension", "")).strip(),
                    "nickname": str(c.get("nickname", "")).strip(),
                    "message": str(c.get("message", "")).strip(),
                })
        return out
    if isinstance(candidates, str):
        return _parse_candidates_lines(candidates)
    return []


def _parse_combined_extract_reply(reply: str) -> tuple[str, list[dict]]:
    """解析合并提取结果，返回 (summary, candidates_list)。

    candidates_list 元素: {"dimension", "nickname", "message"}。
    三层兜底：
    1. JSON 优先（复用 persona 的健壮解析：去代码块/提取对象/strict=False）
    2. 分隔符拆分（候选部分按行解析）
    3. 整段视为摘要（候选为空列表）
    """
    # 1. JSON 解析
    try:
        from .persona import _parse_combined_map_json
        data = _parse_combined_map_json(reply)
        if isinstance(data, dict) and ("summary" in data or "candidates" in data):
            summary = str(data.get("summary", "")).strip()
            return summary, _normalize_candidates(data.get("candidates", []))
    except Exception:
        pass
    # 2. 分隔符兜底（兼容 "=== 候选 ===" / "===候选===" 等变体）
    m = re.search(r"===\s*候选\s*===", reply)
    if m:
        summary = reply[:m.start()].strip()
        return summary, _parse_candidates_lines(reply[m.end():].strip())
    # 3. 整段当摘要
    return reply.strip(), []


_COMBINED_LLM_RETRIES = 3  # 合并提取 LLM 重试次数（含首次）——qa 段未配置时的兜底


async def _combined_extract_batch(
    batch_text: str, group_id: int, date: str, batch_num: int, total_batches: int,
    priority: int = 0,
    short_map: dict | None = None,
) -> tuple[str, list[dict]]:
    """合并提取：一次 LLM 调用同时产出摘要与候选，落库（task_type='combined'）并返回两部分。

    返回 (summary_str, candidates_list)，candidates_list 元素: {"dimension","nickname","message"}。
    失败（空回复/LLM 错误）重试；解析后两部分全空视为失败批次（is_valid=0 落库可追溯，
    返回空串/空列表不参与 Reduce）。
    LLM 参数走 qa.llm.scheduled 段（热生效）：max_tokens/thinking/retries/json_mode。
    """
    msg = f"以下是群聊记录的一个片段（批次 {batch_num}/{total_batches}）：\n\n{batch_text}\n\n请完成分析："
    # 提示词（qa_prompts 单一来源；模板含字面 JSON 花括号，render 用字符串替换）
    system_prompt = render_prompt("scheduled_combined_extract")
    _sch = qa_llm_scope("scheduled")
    _common = qa_llm()
    _retries = int(_sch.get("retries", _COMBINED_LLM_RETRIES))
    _json_mode = bool(_sch.get("json_mode", False))
    reply = ""
    for attempt in range(1, _retries + 1):
        reply = await call_llm(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": msg}],
            max_tokens=int(_sch.get("max_tokens", 131072)), priority=priority,
            temperature=float(_common.get("temperature", 0.7)),
            timeout=int(_common.get("timeout", 1800)),
            json_mode=_json_mode,
            **thinking_kwargs(_sch.get("thinking", "on")),
        )
        reply = reply.strip()
        if reply and not reply.startswith(("😵", "🔕")):
            break
        logger.warning(f"🔄 合并提取批次 {batch_num} 返回无效 content (attempt {attempt}/{_retries})，重试中...")
    else:
        logger.error(f"❌ 合并提取批次 {batch_num} 达到最大重试次数，跳过该批次")
        _save_extract_batch(
            "combined", group_id, date, batch_num, total_batches, len(batch_text),
            json.dumps({"summary": "", "candidates": []}, ensure_ascii=False), 0,
        )
        return "", []

    summary_part, candidates_part = _parse_combined_extract_reply(reply)
    # U 编号引用归一化为 昵称(qq号)（落库与 Reduce 输入均使用归一化结果）
    summary_part = _normalize_u_refs(summary_part, short_map)
    candidates_part = _normalize_u_refs(candidates_part, short_map)
    is_valid = 1 if (summary_part or candidates_part) else 0
    _save_extract_batch(
        "combined", group_id, date, batch_num, total_batches, len(batch_text),
        json.dumps({"summary": summary_part, "candidates": candidates_part}, ensure_ascii=False),
        is_valid,
    )
    if not is_valid:
        logger.warning(f"⚠️ 合并提取批次 {batch_num} 解析结果为空")
        return "", []  # 失败批次不参与 Reduce（已落库 is_valid=0 可追溯）
    return summary_part, candidates_part


async def _combined_extract(
    group_id: int, date: str, priority: int = 0,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
) -> tuple[list[str], list[str], int, int, str, dict]:
    """定时任务合并提取：拉取指定时间窗口聊天记录，分批并发提取（带缓存复用）。

    返回 (summary_parts, candidates_parts, total_messages, user_count, users_text, short_map)。
    缓存命中（同轮同内容、JSON 可解析）时不再调用 LLM；失败批次重新提取。
    date 为轮次键（半日报告传 YYYY-MM-DD-AM / YYYY-MM-DD-PM，与手动指令互不复用）；
    start_time/end_time 指定消息时间窗口（默认 None = 过去 24 小时）。
    """
    chat_log = get_today_chat_log_merged(group_id, start_time, end_time)
    if len(chat_log) < 2:
        return [], [], len(chat_log), 0, "", {}
    total_messages = len(chat_log)
    users = sorted(set(e["nickname"] for e in chat_log))
    users_text = "、".join(users)

    chat_lines, short_map = _format_chat_messages(chat_log)
    chunks = chunk_messages_by_token(chat_lines, target_tokens=int(qa_params().get("map_batch_chars", 40000)))
    total_batches = len(chunks)

    # 断点恢复：加载缓存并解析 JSON
    batch_cache = _load_extract_batch_cache("combined", group_id, date, total_batches)
    parsed_cache: dict[int, dict] = {}
    for num, info in batch_cache.items():
        try:
            data = json.loads(info["raw_response"])
            if isinstance(data, dict):
                parsed_cache[num] = {
                    "summary": data.get("summary", ""),
                    "candidates": data.get("candidates", ""),
                    "batch_char_count": info["batch_char_count"],
                }
        except (json.JSONDecodeError, TypeError):
            pass  # 无法解析视为无缓存，重新提取

    async def _process_batch(idx: int, chunk: list[str]) -> tuple[str, list[dict]]:
        batch_num = idx + 1
        batch_text = "\n".join(chunk)
        cached = parsed_cache.get(batch_num)
        if cached is not None and cached["batch_char_count"] == len(batch_text):
            logger.info(f"♻️ 合并提取批次 {batch_num}/{total_batches} 复用缓存")
            # 旧缓存可能含 U 编号引用，统一归一化为 昵称(qq号)
            summary_c = _normalize_u_refs(cached["summary"], short_map)
            candidates_c = _normalize_u_refs(_normalize_candidates(cached.get("candidates", [])), short_map)
            return summary_c, candidates_c
        logger.info(f"📝 合并提取批次 {batch_num}/{total_batches}...")
        return await _combined_extract_batch(batch_text, group_id, date, batch_num, total_batches, priority, short_map=short_map)

    parts = await asyncio.gather(*[_process_batch(idx, chunk) for idx, chunk in enumerate(chunks)])
    summary_parts = [p[0] for p in parts if p[0]]

    # 候选按维度聚合：跨批同维度最多保留 2 条（保持维度首次出现顺序），
    # 合并为单个文本块——Reduce 输入更聚焦（每维度 2 条 vs 每批 5 条 × 批数）
    aggregated: dict[str, list[dict]] = {}
    for _, cand_list in parts:
        for c in cand_list:
            dim_key = (c.get("dimension") or "").split(" ", 1)[0]
            aggregated.setdefault(dim_key, []).append(c)
    cand_lines: list[str] = []
    for _dim_key, items in aggregated.items():
        for c in items[:2]:
            line_parts = []
            if c.get("dimension"):
                line_parts.append(c["dimension"])
            if c.get("nickname") and c.get("message"):
                line_parts.append(f"{c['nickname']}: {c['message']}")
            elif c.get("message"):
                line_parts.append(c["message"])
            line = " ".join(line_parts).strip()
            if line:
                cand_lines.append(line)
    candidates_parts = ["\n".join(cand_lines)] if cand_lines else []
    return summary_parts, candidates_parts, total_messages, len(users), users_text, short_map


# ============================================================
# 定时任务：每日更新（人设 → 画像 → 题库）
# ============================================================
async def _periodic_daily_update() -> None:
    """
    定时后台任务：脚本启动时立即执行一次，之后每天 0 点触发。
    三个任务串行排队执行，避免并发调用本地 LLM 造成阻塞：
    1. 联合更新画像+人设（Combined Map 方案）
    2. 真心话大冒险题库预填充

    在所有有消息的群中执行。
    """
    from .config import CONFIG

    first_run_done = False

    while True:
        # ---- 检查是否被暂停 ----
        if _daily_update_paused:
            logger.info("⏸️ 每日定时更新已暂停，等待恢复...")
            await asyncio.sleep(300)  # 每 5 分钟检查一次是否恢复
            continue

        # ---- WebSocket 就绪检查（放在 first_run 之前：未就绪时短等待重试，
        #      不会把首次执行错误推迟到 0 点）----
        ws = sender._active_websocket
        if ws is None:
            logger.warning("⏳ WebSocket 未就绪，等待 60 秒后重试")
            await asyncio.sleep(60)
            continue

        # ---- LLM 总开关检查（2026-08-21 审计：关闭时跳过每日更新，
        #      防止 LLM 调用降级串被当有效结果处理 + 批量 INFO 日志刷屏）----
        if not llm_enabled():
            logger.info("🔕 LLM 总开关关闭，跳过每日定时更新（画像/人设/题库均依赖 LLM）")
            # 等 30 分钟再查一次开关（GUI 总览页可随时开启，开启后自动恢复）
            await asyncio.sleep(1800)
            continue

        # ---- 首次启动立即执行；非首次则等待到每天 0 点 ----
        if not first_run_done:
            first_run_done = True
            logger.info("🔄 首次启动：每日定时更新立即执行一次")
        else:
            seconds_until = _seconds_until_next_midnight()
            logger.info(f"⏰ 距离下次每日定时更新（0 点）还有 {seconds_until} 秒")
            await asyncio.sleep(seconds_until)

        try:
            # ---- 每日更新：先准备数据，再短持锁执行 ----
            # 获取所有活跃的群 ID（合并画像+人设的群）
            profile_persona_groups = get_active_groups_by_task("profile") + get_active_groups_by_task("persona")
            # 去重
            profile_persona_groups = list(set(profile_persona_groups))
            question_groups = get_active_groups_by_task("question")

            # 已注册的群且开关=1 才会出现在列表中
            if not profile_persona_groups and not question_groups:
                logger.info("📋 暂无开启定时任务的群，跳过定时更新")
                await asyncio.sleep(60)
                continue

            # 集群去重：同一集群只处理一次，取集群内所有群合并活跃用户
            def _deduplicate_groups_by_cluster(groups, task):
                """按集群去重，返回 (代表群_id, 集群内所有群列表) 的列表

                task: "profile" | "persona" | "question" | "profile_persona"（合并列表用 OR 语义）
                """
                cluster_map = {}  # cid -> (代表群, [所有群])
                # 任务 → 开关列映射；profile_persona 需同时满足任一（OR）
                col_map = {
                    "persona": ("enable_persona_update",),
                    "profile": ("enable_profile_update",),
                    "question": ("enable_question_refill",),
                    "profile_persona": ("enable_profile_update", "enable_persona_update"),
                }
                cols = col_map.get(task, ("enable_profile_update",))
                for gid in groups:
                    cid = get_cluster_id(gid)
                    if cid:
                        if cid not in cluster_map:
                            members = get_cluster_groups(cid)
                            # 过滤：只保留开启了该任务的群（多列时任一开启即保留）
                            # 并剔除脏数据 group_id=0（主群迁移残留，曾导致 rep_gid=0 全量重建，2026-08-08 修复）
                            active_members = [
                                m["group_id"] for m in members
                                if m["group_id"] != 0
                                and any(m.get(col, 0) == 1 for col in cols)
                            ]
                            if active_members:
                                # 代表群优先用主群（master_group_id），不依赖成员列表顺序
                                master = get_cluster_master_group(active_members[0])
                                rep_gid = master if master and master != 0 else active_members[0]
                                cluster_map[cid] = (rep_gid, active_members)
                    else:
                        # 无集群的群（理论上不应出现，因为 get_active_groups_by_task 只查已注册群）
                        cluster_map[f"singleton_{gid}"] = (gid, [gid])
                return list(cluster_map.values())

            # 联合更新：合并画像和人设的群列表（OR 语义过滤，只开 persona 的群不丢失）
            profile_persona_tasks = _deduplicate_groups_by_cluster(profile_persona_groups, "profile_persona")
            question_tasks = _deduplicate_groups_by_cluster(question_groups, "question")

            logger.info(f"🔄 开始每日定时更新: {len(profile_persona_tasks)} 个集群联合更新(画像+人设), {len(question_tasks)} 个集群题库（静默模式）")

            # ---- 全局总闸（2026-08-22，总览页「⚙️ 配置面板」管理）：
            #      人设画像更新 / 题库自动补充 全局关闭 → 清空对应任务列表跳过
            #      （群级开关不受影响，恢复开启后原样生效）----
            if not CONFIG.get("SCHED_PERSONA_UPDATE", True):
                logger.info("🔕 全局总闸：人设画像更新关闭，本轮跳过联合更新")
                profile_persona_tasks = []
            if not CONFIG.get("SCHED_QUESTION_REFILL", True):
                logger.info("🔕 全局总闸：题库自动补充关闭，本轮跳过题库预填充")
                question_tasks = []

            # 任务 1：联合更新画像+人设
            for rep_gid, cluster_gids in profile_persona_tasks:
                # ---- 群级暂停检查 ----
                if _daily_update_paused:
                    logger.info("⏸️ 联合更新被暂停，跳过剩余集群")
                    break

                try:
                    # 合并集群内所有群的活跃用户（按时间倒序，去重保留最新昵称）
                    with get_db() as conn:
                        placeholders = ",".join(["?"] * len(cluster_gids))
                        all_users = conn.execute(
                            f"SELECT DISTINCT user_id, nickname FROM message_archive "
                            f"WHERE target_id IN ({placeholders}) ORDER BY created_at DESC",
                            cluster_gids,
                        ).fetchall()

                    if not all_users:
                        continue

                    bot_qq = int(get_bot_uin() or 0)  # 08-22：从连接派生
                    all_users = [dict(r) for r in all_users if dict(r)["user_id"] != bot_qq]

                    # 去重：保留最新昵称
                    seen = {}
                    for u in all_users:
                        uid = u["user_id"]
                        if uid not in seen:
                            seen[uid] = u
                    all_users = list(seen.values())

                    updated = 0
                    failed = 0
                    _batch_id = f"sdaily-{int(time.time())}-{rep_gid}"
                    _task_keys = TASK_REGISTRY.begin_batch(
                        _batch_id, "定时任务",
                        [f"⏰ 联合更新 {u['nickname']}({u['user_id']})" for u in all_users])
                    try:
                        for i, u in enumerate(all_users):
                            # 2026-08-22 暂停门：定时联合更新 queued→running 前等放行
                            await TASK_REGISTRY.wait_if_paused()
                            TASK_REGISTRY.set_status(_task_keys[i], "running")
                            try:
                                uid = u["user_id"]
                                nickname = u["nickname"]
                                try:
                                    # 每用户最多 4 小时（118+ 批 × ~30-60s/批 + Reduce 阶段）
                                    await asyncio.wait_for(
                                        _do_update_profile_and_persona(ws, "group", rep_gid, 0, None, uid, nickname, silent=True, priority=1),
                                        timeout=14400
                                    )
                                    updated += 1
                                except asyncio.TimeoutError:
                                    logger.warning(f"⏱️ 联合更新超时: {nickname}({uid}) in cluster {rep_gid}")
                                    failed += 1
                                except Exception as e:
                                    logger.error(f"❌ 联合更新失败 {nickname}({uid}): {e}")
                                    failed += 1
                            finally:
                                TASK_REGISTRY.finish(_task_keys[i])

                            if _daily_update_paused:
                                logger.info(f"⏸️ 联合更新被暂停，集群 {rep_gid} 已完成 {updated}/{len(all_users)}")
                                break
                    finally:
                        TASK_REGISTRY.finish_batch(_task_keys)

                    if all_users:
                        logger.info(f"📋 集群 {rep_gid} 联合更新(画像+人设)完成: {updated}/{len(all_users)} 成功, {failed} 失败")

                except Exception as e:
                    logger.error(f"❌ 集群 {rep_gid} 联合更新异常: {e}")

            # 任务 2：预填充真心话大冒险题库
            for rep_gid, cluster_gids in question_tasks:
                if _daily_update_paused:
                    logger.info("⏸️ 题库更新被暂停，跳过剩余集群")
                    break

                _qk = TASK_REGISTRY.register(
                    "题库维护", f"⏰ 题库补充 集群{rep_gid}",
                    group_id=rep_gid, status="queued")
                try:
                    await _get_question_refill_lock().acquire()
                    try:
                        # 2026-08-22 暂停门：获锁后转 running 前等放行
                        await TASK_REGISTRY.wait_if_paused()
                        TASK_REGISTRY.set_status(_qk, "running")
                        result = await asyncio.to_thread(refill_questions_now, rep_gid)
                        logger.info(f"[题库维护] 集群 {rep_gid}: {result}")
                    finally:
                        try:
                            _get_question_refill_lock().release()
                        except RuntimeError:
                            pass
                except Exception as e:
                    logger.warning(f"题库维护异常 (集群 {rep_gid}): {e}")
                finally:
                    TASK_REGISTRY.finish(_qk)

            # ---- 检查是否在任务执行期间被暂停 ----
            # 如果被暂停了，直接 continue 回到循环开头（那里会进入 sleep(300) 等待恢复）
            # 而不执行下面的等待，避免恢复后还要等 24 小时
            if _daily_update_paused:
                logger.info("⏸️ 检测到暂停标志，跳过本轮等待，回到循环起点")
                continue

            logger.info("🔄 每日定时更新全部完成（静默模式）")

        except Exception as e:
            logger.error(f"🔄 每日定时更新异常: {e}", exc_info=True)

        # 回到循环开头：非首次运行会在那里等待到下一个 0 点
        # （首次运行逻辑由 first_run_done 标志控制，这里不再 sleep(86400)）


def _dedup_cluster_groups(task_groups: list[int]) -> set[int]:
    """集群去重：一个集群只留一个代表群（优先主群，且主群必须在开启列表中）。"""
    cluster_map: dict[str, int] = {}
    for gid in task_groups:
        cid = get_cluster_id(gid)
        if cid:
            if cid not in cluster_map:
                master = get_cluster_master_group(gid)
                # 仅当主群本身也在开启列表中时用它，否则用当前开启的成员群
                cluster_map[cid] = master if master in task_groups else gid
        else:
            # 无集群的群独立处理
            cluster_map[f"singleton_{gid}"] = gid
    return set(cluster_map.values())


# ============================================================
# 定时任务：每天 11:30 / 22:30 半日报告（评选 + 总结）
# ============================================================
_AM_REPORT_TIME = dtime(11, 30, 0)   # 上午场：总结 [昨天 22:30, 今天 11:30)（含昨晚深夜，不遗漏）
_PM_REPORT_TIME = dtime(22, 30, 0)   # 下午场：总结 [今天 11:30, 今天 22:30)（两场独立不重叠）


def _next_report_slot(now: datetime) -> tuple[int, str, str]:
    """计算到下一个半日报告场次的等待秒数。

    返回 (seconds_until, period_key, period_word)：
    - period_key: 'AM'（11:30 上午场）/ 'PM'（22:30 下午场），用作落库轮次键后缀
    - period_word: '上午' / '下午'，用于报告标题与日志
    """
    t = now.time()
    if t < _AM_REPORT_TIME:
        return int((datetime.combine(now.date(), _AM_REPORT_TIME) - now).total_seconds()), "AM", "上午"
    if t < _PM_REPORT_TIME:
        return int((datetime.combine(now.date(), _PM_REPORT_TIME) - now).total_seconds()), "PM", "下午"
    return int((datetime.combine(now.date() + timedelta(days=1), _AM_REPORT_TIME) - now).total_seconds()), "AM", "上午"


async def _periodic_daily_reports() -> None:
    """
    定时后台任务：每天 11:30 / 22:30 各一次半日报告（评选 + 总结）。
    11:30 上午场总结 [昨天 22:30, 今天 11:30)（含昨晚深夜消息，保证覆盖所有聊天内容）；
    22:30 下午场总结 [今天 11:30, 今天 22:30)（两场时间窗口独立不重叠，互不包含）。
    每场合并提取聊天记录一次（摘要+候选），对开启评选的群发送评选报告、对开启总结的群发送总结报告；
    报告按场次落库（date = YYYY-MM-DD-AM / YYYY-MM-DD-PM），同日两场记录互不覆盖。
    """
    while True:
        try:
            ws = sender._active_websocket
            if ws is None:
                logger.warning("⏳ WebSocket 未就绪，等待 60 秒后重试")
                await asyncio.sleep(60)
                continue

            # ---- 半日报告：计算到下一场（11:30 上午 / 22:30 下午）的等待时间（锁外，避免长持锁）----
            now = datetime.now()
            seconds_until, period_key, period_word = _next_report_slot(now)
            slot_label = "11:30" if period_key == "AM" else "22:30"
            if seconds_until > 0:
                logger.info(f"⏰ 距离{period_word}场定时报告（{slot_label}）还有 {seconds_until} 秒")
                await asyncio.sleep(seconds_until)
            else:
                # 兜底：执行时刻恰好落在整点（seconds_until==0）时短眠 1 秒，
                # 防止报告结束后立即重跑同一场
                await asyncio.sleep(1)

            # ---- LLM 总开关检查（2026-08-21 审计：半日报告每 12h 自动跑，
            #      关闭时若执行，降级串会被当有效摘要落库 daily_summary/
            #      batch_extract_results 并作为报告发群 → 跳过本场，等下一场。
            #      位置必须在场次等待之后：每场只查一次，跳过后 continue 回循环顶
            #      _next_report_slot 自然推进到下一场（本场不再补发，用户可用
            #      /总结 /评选 手动补）。放在等待之前会形成忙等循环，勿移回。）----
            if not llm_enabled():
                logger.info("🔕 LLM 总开关关闭，跳过本场半日报告（评选/总结均依赖 LLM；可用 /总结 /评选 手动补发）")
                continue

            # ---- 全局总闸（2026-08-22，总览页「⚙️ 配置面板」管理）：
            #      每日总结/评选 全局关闭 → 跳过本场（与 LLM 总闸同款位置，
            #      每场只查一次；群级开关不受影响，恢复开启后原样生效）----
            from .config import CONFIG as _CFG
            if not _CFG.get("SCHED_DAILY_REPORT", True):
                logger.info("🔕 全局总闸：每日总结/评选关闭，跳过本场半日报告（可用 /总结 /评选 手动补发）")
                continue

            # ---- 准备数据，再短持锁执行 ----
            # 重新检查 WS（等待期间可能断开）
            ws = sender._active_websocket
            if ws is None:
                logger.warning("⏳ WebSocket 未就绪，等待 60 秒后重试")
                await asyncio.sleep(60)
                continue

            # 本场时间窗口：AM = [昨天 22:30, 今天 11:30)，PM = [今天 11:30, 今天 22:30)
            # 两场独立不重叠（2026-08-08 用户方案：修复 PM 场包含凌晨/上午消息的问题）
            today = datetime.now().date()
            if period_key == "AM":
                start_dt, end_dt = (
                    datetime.combine(today - timedelta(days=1), _PM_REPORT_TIME),
                    datetime.combine(today, _AM_REPORT_TIME),
                )
            else:
                start_dt, end_dt = (
                    datetime.combine(today, _AM_REPORT_TIME),
                    datetime.combine(today, _PM_REPORT_TIME),
                )
            start_ts, end_ts = start_dt.timestamp(), end_dt.timestamp()
            # 落库/缓存轮次键：同日两场互不覆盖（YYYY-MM-DD-AM / YYYY-MM-DD-PM）
            report_date = f"{today.strftime('%Y-%m-%d')}-{period_key}"

            # 群集合：评选开关群 ∪ 总结开关群（各自集群去重后并集），合并提取一次两份报告共用
            evaluation_groups = get_active_groups_by_task("evaluation")
            summary_groups = get_active_groups_by_task("summary")
            eval_report_groups = _dedup_cluster_groups(evaluation_groups)
            summary_report_groups = _dedup_cluster_groups(summary_groups)
            report_groups = sorted(eval_report_groups | summary_report_groups)

            if not report_groups:
                logger.info("📋 暂无群聊数据，跳过本场定时报告")
                continue

            logger.info(f"🔒 等待定时任务锁（{period_word}场报告）... 共 {len(report_groups)} 个群")
            async with _get_schedule_lock():
                logger.info("🔓 获取定时任务锁，开始半日报告")

                logger.info(f"📋 开始{period_word}场定时报告（评选 {len(eval_report_groups)} 群 / 总结 {len(summary_report_groups)} 群）")

                for gid in report_groups:
                    # 提取独立超时：失败则跳过该群（缓存已落库 is_valid=0，可追溯）
                    try:
                        summary_parts, candidates_parts, total_messages, user_count, users_text, short_map = \
                            await asyncio.wait_for(
                                _combined_extract(gid, report_date, start_time=start_ts, end_time=end_ts), timeout=3600
                            )
                    except asyncio.TimeoutError:
                        logger.warning(f"⏰ 定时报告提取超时 (群 {gid})")
                        continue
                    except Exception as e:
                        logger.error(f"❌ 定时报告提取异常 (群 {gid}): {e}")
                        continue
                    # 评选报告：仅对开启评选的群发送（独立异常，不影响总结）
                    if gid in eval_report_groups and candidates_parts:
                        try:
                            candidates_text = "\n---\n".join(candidates_parts)
                            report = await _reduce_evaluation(
                                gid, report_date, candidates_text, users_text, total_messages, user_count,
                                short_map=short_map, period_word=period_word, start_time=start_ts, end_time=end_ts,
                            )
                            await sender.send_reply(ws, "group", gid, report, None, None)
                        except Exception as e:
                            logger.error(f"❌ 评选报告生成异常 (群 {gid}): {e}")
                    # 总结报告：仅对开启总结的群发送（独立异常，不影响评选）
                    if gid in summary_report_groups and summary_parts:
                        try:
                            summaries_text = "\n---\n".join(summary_parts)
                            report = await _reduce_summary(
                                gid, report_date, summaries_text, users_text, total_messages, user_count,
                                short_map=short_map, period_word=period_word,
                            )
                            await sender.send_reply(ws, "group", gid, report, None, None)
                        except Exception as e:
                            logger.error(f"❌ 总结报告生成异常 (群 {gid}): {e}")

                logger.info(f"📋 {period_word}场定时报告全部完成")

        except Exception as e:
            logger.error(f"📋 定时报告异常: {e}", exc_info=True)

        # 回到循环开头：重新计算到下一场（11:30 / 22:30）的等待时间


# ---- 好感度衰减任务 ----
async def _periodic_favorability_decay() -> None:
    """
    定时后台任务：每 8 小时对好感度执行一次衰减。
    衰减后关系发生变化的用户会收到 LLM 生成的关系变化通知。
    """
    from .router import _generate_relationship_change_message

    from .database import get_settings_db

    while True:
        try:
            with get_settings_db() as conn:
                groups = conn.execute(
                    "SELECT DISTINCT group_id FROM bot_favorability"
                ).fetchall()

            for row in groups:
                gid = row["group_id"]
                changes = decay_bot_favorability(gid)

                if changes:
                    logger.info(f"💕 好感度衰减 (群 {gid}): {len(changes)} 人关系变化")

                    # 异步发送关系变化通知
                    ws = sender._active_websocket
                    if ws and not ws.closed:
                        for uid, new_rel, old_rel, new_fav in changes:
                            try:
                                rel_msg = await _generate_relationship_change_message(
                                    [], "用户", old_rel, new_rel, new_fav, 0
                                )
                                if rel_msg:
                                    await asyncio.sleep(0.5)
                                    await sender.send_reply(
                                        ws, "group", gid,
                                        rel_msg, uid, reply_id=None, at_user_ids=[uid]
                                    )
                                    logger.info(f"💬 衰减关系变化通知: {old_rel} → {new_rel} (用户 {uid})")
                            except Exception as e:
                                logger.error(f"❌ 衰减关系通知发送失败: {e}")

        except Exception as e:
            logger.error(f"💕 好感度衰减异常: {e}", exc_info=True)

        await asyncio.sleep(28800)  # 每 8 小时执行一次


# ============================================================
# 启动所有调度器
# ============================================================
# ---- 后台任务引用（防止 GC 回收）----
_scheduler_tasks: list[asyncio.Task[None]] = []


async def _start_schedulers() -> None:
    """
    启动所有后台定时任务。
    使用模块级标志位防止 NapCat 重连时重复启动。
    """
    global _daily_update_started, _reports_started, _favorability_decay_started, _napcat_watchdog_started, _retention_cleanup_started, _napcat_primary_started

    if not _daily_update_started:
        _daily_update_started = True
        task = asyncio.create_task(_periodic_daily_update())
        _scheduler_tasks.append(task)
        logger.info("🔄 每日定时更新任务已启动（启动时立即执行一次，之后每天 0 点触发）")

    if not _reports_started:
        _reports_started = True
        task = asyncio.create_task(_periodic_daily_reports())
        _scheduler_tasks.append(task)
        logger.info("📋 每天 11:30 / 22:30 半日定时报告任务已启动（11:30 总结上半天、22:30 总结下半天；评选/总结按群开关 enable_evaluation / enable_summary 分别触发）")

    if not _favorability_decay_started:
        _favorability_decay_started = True
        task = asyncio.create_task(_periodic_favorability_decay())
        _scheduler_tasks.append(task)
        logger.info("💕 好感度衰减任务已启动（每 8 小时执行一次）")

    # NapCat 连接看门狗：每 60 秒检查，断开时限频告警（2026-08-10）
    if not _napcat_watchdog_started:
        _napcat_watchdog_started = True
        task = asyncio.create_task(_periodic_napcat_watchdog())
        _scheduler_tasks.append(task)
        logger.info("👁️ NapCat 连接看门狗已启动（每 60 秒检查，断开告警 + 状态文件 data/napcat_status.txt）")

    # 存档保留期清理（2026-08-20）：每天 3:00 按 archive.text/media_retention_days 清理
    if not _retention_cleanup_started:
        _retention_cleanup_started = True
        task = asyncio.create_task(_periodic_retention_cleanup())
        _scheduler_tasks.append(task)
        logger.info("🧹 存档保留期清理任务已启动（每天 3:00 执行，保留期=0 时自动跳过）")

    # NapCat 主账号巡检（2026-08-21）：每 30 分钟检查配置漂移（非主账号桥被开启）
    if not _napcat_primary_started:
        _napcat_primary_started = True
        task = asyncio.create_task(_periodic_napcat_primary())
        _scheduler_tasks.append(task)
        logger.info("👑 NapCat 主账号巡检已启动（每 30 分钟检查配置漂移，正常情况零日志）")


# ============================================================
# 定时任务暂停/恢复控制
# ============================================================
def pause_daily_update() -> bool:
    """暂停每日定时更新（人设→画像→题库）。返回 True 表示本次操作生效."""
    global _daily_update_paused
    if not _daily_update_paused:
        _daily_update_paused = True
        logger.info("⏸️ 每日定时更新已暂停（/暂停任务）")
        return True
    return False


def resume_daily_update() -> bool:
    """恢复每日定时更新（人设→画像→题库）。返回 True 表示本次操作生效."""
    global _daily_update_paused
    if _daily_update_paused:
        _daily_update_paused = False
        logger.info("▶️ 每日定时更新已恢复（/恢复任务）")
        return True
    return False


def is_daily_update_paused() -> bool:
    """查询每日定时更新是否处于暂停状态."""
    return _daily_update_paused


def refill_questions_now(group_id: int) -> str:
    """手动立即补充指定群的真心话大冒险题库。
    逻辑等同于每日定时任务中的"任务3：预填充真心话大冒险题库"。
    全串行执行：按色度档位 → 真心话/大冒险 → 逐个玩家依次补充。
    返回状态消息。
    """
    try:
        from games.question_pool import (
            _get_persona_nickname,
            _get_db,
            DATA_DIR,
            _get_generic_pool_count,
            _get_pool_count,
            _QUESTION_SPICINESS_LEVELS,
            _refill_pool,
            _td_generic_threshold,  # 2026-08-22：原 _QUESTION_GENERIC_THRESHOLD 已改为可配置阈值函数
            _refill_generic_pool,
            _GENERIC_REFILL_LOCKS,
        )
        from core.persona import get_all_profiles

        # 维护通用题库（全串行：11 档位 × 2 题型 = 22 次 LLM）
        # M4 修复：尊重 question_pool 的防重入锁——游戏触发 _refill_generic_pool_safe
        # 线程占锁时跳过该档位，避免双线程并发补充（LLM 调用翻倍、排队加剧）
        # 2026-08-22：阈值改为可配置函数（原常量已删除），循环前取一次值
        _generic_threshold = _td_generic_threshold()
        for level in _QUESTION_SPICINESS_LEVELS:
            if _GENERIC_REFILL_LOCKS.get(level):
                logger.info(f"⏭️ 通用题库档位 {level} 正在后台补充中，跳过")
                continue
            if _get_generic_pool_count("truth", level) < _generic_threshold or \
                _get_generic_pool_count("dare", level) < _generic_threshold:
                _GENERIC_REFILL_LOCKS[level] = True
                try:
                    _refill_generic_pool(group_id, level)
                finally:
                    _GENERIC_REFILL_LOCKS.pop(level, None)

        # 获取有答题记录的玩家（从三个模式表中合并）
        td_db = os.path.join(DATA_DIR, "truth_dare.db")
        answered_users: set[int] = set()
        try:
            with _get_db(td_db) as conn:
                for table in ["auto_questions", "user_question_history", "self_select_questions"]:
                    try:
                        rows = conn.execute(
                            f"SELECT DISTINCT user_id FROM {table} WHERE group_id = ?",
                            (group_id,),
                        ).fetchall()
                        for row in rows:
                            answered_users.add(row[0])
                    except Exception:
                        pass
        except Exception:
            logger.warning("[题库补充] 读取答题记录失败")

        # 获取所有有人设的玩家（仅从 user_personas 获取，与 _get_persona_nickname 同源）
        from core.persona import get_persona_db
        persona_users: list[tuple[int, str]] = []

        with get_persona_db() as conn:
            rows = conn.execute(
                "SELECT user_id, nickname FROM user_personas WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            persona_users = [(row[0], row[1] or f"用户{row[0]}") for row in rows]

        logger.info(f"[题库补充] 群 {group_id}: user_personas 查询到 {len(persona_users)} 人")

        # 筛选有人设的玩家，按优先级排序
        players_with_record: list[tuple[int, str, str, str | None]] = []
        players_without_record: list[tuple[int, str, str, str | None]] = []

        for uid, nick in persona_users:
            nickname, persona_text, gender = _get_persona_nickname(uid, group_id)
            if persona_text and persona_text.strip():
                entry = (uid, nick, persona_text, gender)
                if uid in answered_users:
                    players_with_record.append(entry)
                else:
                    players_without_record.append(entry)
            else:
                logger.warning(f"[题库补充] uid={uid} ({nick}) 无人设文本，跳过")

        # 优先处理有答题记录的玩家，再补充没有记录的
        all_players = players_with_record + players_without_record
        for uid, nickname, persona_text, gender in all_players:
            # 检查是否被暂停
            if _daily_update_paused:
                logger.info("⏸️ 题库更新被暂停，跳过剩余玩家")
                break
            _refill_pool(uid, group_id, nickname, persona_text, gender)

        # 统计
        generic_count = sum(
            _get_generic_pool_count("truth", l) + _get_generic_pool_count("dare", l)
            for l in _QUESTION_SPICINESS_LEVELS
        )
        persona_count = sum(
            _get_pool_count(uid, group_id, "truth", l) + _get_pool_count(uid, group_id, "dare", l)
            for uid, _, _, _ in all_players
            for l in _QUESTION_SPICINESS_LEVELS
        )
        msg = f"✅ 题库补充完成！\n📚 通用题库: {generic_count} 道题\n"
        if all_players:
            msg += f"👤 人设题库: {len(all_players)} 人（{len(players_with_record)} 人有答题记录，{len(players_without_record)} 人无记录），共 {persona_count} 道题"
        else:
            msg += "👤 人设题库: 暂无（需有玩家画像才会生成个性化题目）"
        return msg

    except Exception as e:
        return f"❌ 题库补充异常: {e}"