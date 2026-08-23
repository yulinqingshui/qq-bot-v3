#!/usr/bin/env python3
"""
数据库操作模块 — 聊天数据库 CRUD、会话管理、屏蔽名单。
纯 DB 层，不含业务逻辑。
"""
import os
import sqlite3
import time
import logging
from contextlib import contextmanager
from typing import Optional

from .config import CONFIG

logger = logging.getLogger("qq-bot")

# ============================================================
#  聊天数据库 Schema（chat_history.db — 纯聊天/存档数据）
# ============================================================
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_key ON chat_messages(session_key);
CREATE TABLE IF NOT EXISTS session_metadata (
    session_key TEXT PRIMARY KEY,
    last_active REAL NOT NULL,
    message_count INTEGER DEFAULT 0,
    history_cleared_at REAL DEFAULT 0  -- /清除人设时标记，历史只取此时间之后的消息
);
-- 群消息缓存表（用于 /评选 分析所有群消息）
CREATE TABLE IF NOT EXISTS group_chat_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_group_date ON group_chat_cache(group_id, created_at);
CREATE INDEX IF NOT EXISTS idx_group_message_id ON group_chat_cache(message_id);
-- 永久消息存档（不清理）
CREATE TABLE IF NOT EXISTS message_archive (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 message_id INTEGER NOT NULL,
 message_type TEXT NOT NULL,
 target_id INTEGER NOT NULL,
 user_id INTEGER NOT NULL,
 nickname TEXT NOT NULL,
 content TEXT NOT NULL,
 raw_message TEXT DEFAULT '',
 has_image INTEGER DEFAULT 0,
 has_voice INTEGER DEFAULT 0,
 msg_kind TEXT NOT NULL DEFAULT 'text',
 created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_target ON message_archive(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_archive_user ON message_archive(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_archive_kind ON message_archive(msg_kind);
-- 撤回消息记录
CREATE TABLE IF NOT EXISTS message_recalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    operator_id INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT DEFAULT '',
    content TEXT DEFAULT '',
    has_image INTEGER DEFAULT 0,
    msg_kind TEXT NOT NULL DEFAULT 'text',
    recalled_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recalls_target ON message_recalls(target_id, recalled_at);
-- 撤回消息中的图片记录
CREATE TABLE IF NOT EXISTS recall_image (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT DEFAULT '',
    image_url TEXT NOT NULL,
    file_path TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    recalled_at REAL NOT NULL,
    FOREIGN KEY (recall_id) REFERENCES message_recalls(id)
);
CREATE INDEX IF NOT EXISTS idx_recall_img_target ON recall_image(target_id, recalled_at);
-- 群图片存档
CREATE TABLE IF NOT EXISTS image_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    image_url TEXT NOT NULL,
    md5_hash TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',  -- ok/failed/skipped/unknown（08-21：下载存档状态）
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_target ON image_archive(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_image_user ON image_archive(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_image_md5 ON image_archive(md5_hash);
-- 群语音存档
CREATE TABLE IF NOT EXISTS voice_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    voice_url TEXT NOT NULL,
    md5_hash TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',  -- ok/failed/skipped/unknown（08-21：下载存档状态）
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_target ON voice_archive(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_voice_user ON voice_archive(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_voice_md5 ON voice_archive(md5_hash);
-- 群视频存档（2026-08-15 新增：此前 video 消息 content 为空丢失）
CREATE TABLE IF NOT EXISTS video_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    video_url TEXT NOT NULL,
    file_path TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_target ON video_archive(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_video_user ON video_archive(user_id, created_at);
-- 聊天记录转发存档（2026-08-15 新增：此前 forward 消息 content 为空丢失，转发内容仅存 id）
CREATE TABLE IF NOT EXISTS forward_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forward_id TEXT NOT NULL,           -- CQ:forward 的 id
    message_id INTEGER NOT NULL,        -- 外层消息的 message_id
    message_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    content_json TEXT DEFAULT '',       -- 原始解析 JSON（递归展开）
    content_text TEXT DEFAULT '',       -- 可读文本（[昵称]: 内容 逐行）
    status TEXT DEFAULT 'pending',      -- pending/ok/failed/empty
    msg_count INTEGER DEFAULT 0,        -- 展开后的消息条数
    created_at REAL NOT NULL,           -- 外层消息时间
    fetched_at REAL DEFAULT 0           -- 拉取完成时间
);
CREATE INDEX IF NOT EXISTS idx_fwd_forward_id ON forward_archive(forward_id);
CREATE INDEX IF NOT EXISTS idx_fwd_target ON forward_archive(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_fwd_status ON forward_archive(status);
"""

# ============================================================
#  Bot 设置数据库 Schema（bot_settings.db — 管理/配置类数据）
# ============================================================
SETTINGS_DB_SCHEMA = """
-- 用户屏蔽名单表
CREATE TABLE IF NOT EXISTS user_blocklist (
    user_id INTEGER PRIMARY KEY,
    nickname TEXT DEFAULT '',
    blocked_at REAL NOT NULL,
    blocked_by TEXT DEFAULT ''
);
-- 管理员用户表（只能通过服务端数据表操作修改，不开放指令）
CREATE TABLE IF NOT EXISTS admin_users (
    user_id INTEGER PRIMARY KEY,
    nickname TEXT DEFAULT '',
    added_at REAL NOT NULL
);
-- Bot 对群用户的好感度与关系状态表
CREATE TABLE IF NOT EXISTS bot_favorability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    favorability INTEGER NOT NULL DEFAULT 40,
    relationship TEXT NOT NULL DEFAULT '陌生人',
    last_updated REAL NOT NULL,
    UNIQUE(group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_fav_group ON bot_favorability(group_id);
-- 群集群表：将视为"同一个群"的多个群号归为一组
-- 用于备份群场景：同一批用户在多个群聊天，评选/总结等指令合并处理
CREATE TABLE IF NOT EXISTS group_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id TEXT NOT NULL UNIQUE,  -- 集群唯一标识（UUID）
    master_group_id INTEGER NOT NULL DEFAULT 0,  -- 主群群号（添加集群时第一个群）
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_cluster_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    -- 定时任务独立开关（每个群单独控制）
    enable_persona_update INTEGER NOT NULL DEFAULT 0,   -- 人设更新
    enable_profile_update INTEGER NOT NULL DEFAULT 0,   -- 画像更新
    enable_question_refill INTEGER NOT NULL DEFAULT 0,  -- 题库补充
    enable_evaluation INTEGER NOT NULL DEFAULT 0,       -- 评选报告
    enable_summary INTEGER NOT NULL DEFAULT 0,          -- 总结报告
    enable_member_notify INTEGER NOT NULL DEFAULT 0,    -- 入群/退群私聊通知（2026-08-10）
    is_privileged_group INTEGER NOT NULL DEFAULT 0,     -- 最高权限群标识（1=可查看其他所有群人设，2026-08-12；与机器人管理权限无关）
    enable_mimic INTEGER NOT NULL DEFAULT 0,            -- 赛博模仿（1=1%概率触发模仿最久未发言用户，2026-08-13）
    created_at REAL NOT NULL,
    UNIQUE(cluster_id, group_id),
    FOREIGN KEY (cluster_id) REFERENCES group_clusters(cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_group ON group_cluster_members(group_id);
"""


# ============================================================
#  数据库连接
# ============================================================
def _resolve_db_path(name: str) -> str:
    """根据 DB_PATH 或 PERSONAS_DB_PATH 环境变量/配置获取路径"""
    if name == "chat":
        return CONFIG["DB_PATH"]
    return CONFIG["PERSONAS_DB_PATH"]


@contextmanager
def get_db():
    """获取聊天数据库连接（WAL 模式，读多写少友好）"""
    conn = sqlite3.connect(CONFIG["DB_PATH"], timeout=10)  # busy_timeout 10s：并行模式下多任务并发写不抛 locked
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_persona_db():
    """获取人设/画像数据库连接"""
    conn = sqlite3.connect(CONFIG["PERSONAS_DB_PATH"], timeout=10)  # busy_timeout 10s
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_settings_db():
    """获取 Bot 设置数据库连接（admin/blocklist/favorability/clusters）"""
    conn = sqlite3.connect(CONFIG["BOT_SETTINGS_DB_PATH"], timeout=10)  # busy_timeout 10s
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ============================================================
#  数据库初始化
# ============================================================
def _ensure_persona_db():
    """初始化人设/画像数据库"""
    from .persona import PERSONAS_SCHEMA
    os.makedirs(os.path.dirname(CONFIG["PERSONAS_DB_PATH"]), exist_ok=True)
    with get_persona_db() as conn:
        conn.executescript(PERSONAS_SCHEMA)
    logger.info(f"📦 人设数据库: {CONFIG['PERSONAS_DB_PATH']}")


DAILY_REPORTS_SCHEMA = """
-- 每日总结记录表
CREATE TABLE IF NOT EXISTS daily_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    user_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(group_id, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON daily_summaries(date);
CREATE INDEX IF NOT EXISTS idx_daily_summaries_group_date ON daily_summaries(group_id, date);

-- 每日评选记录表
CREATE TABLE IF NOT EXISTS daily_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    evaluation TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    user_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(group_id, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_evaluations_date ON daily_evaluations(date);
CREATE INDEX IF NOT EXISTS idx_daily_evaluations_group_date ON daily_evaluations(group_id, date);

-- 批次提取中间结果表（/总结、/评选 阶段1 落库，支持断点恢复，防重复消耗 token）
CREATE TABLE IF NOT EXISTS batch_extract_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,          -- 'summary' / 'evaluation'
    group_id INTEGER NOT NULL,
    date TEXT NOT NULL,               -- 轮次标识（YYYY-MM-DD）
    batch_index INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    batch_char_count INTEGER NOT NULL,
    raw_response TEXT NOT NULL,
    is_valid INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_extract_task_group_date ON batch_extract_results(task_type, group_id, date);

-- /分析 中间结果表（Map 批次 + 多级合并中间 + 最终答案落库，审计与断点排查用，2026-08-06）
CREATE TABLE IF NOT EXISTS analysis_batch_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,          -- 轮次标识（调用时刻毫秒时间戳，同一轮所有批次共用）
    target_qqs TEXT NOT NULL,         -- 分析目标（QQ号列表，+ 分隔）
    question TEXT NOT NULL,           -- 分析问题
    days INTEGER NOT NULL,            -- 时间窗（天）
    batch_index INTEGER NOT NULL,     -- 正=Map 批次；负=多级合并中间；0=最终答案
    total_batches INTEGER NOT NULL,
    batch_char_count INTEGER NOT NULL,
    stage TEXT NOT NULL,              -- 'map' / 'merge' / 'reduce'
    analysis_result TEXT NOT NULL,    -- LLM 输出原文（线索/中间合并/最终答案）
    is_valid INTEGER NOT NULL DEFAULT 1,  -- 0=LLM 调用失败，1=有效输出（含"无相关信息"）
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_batch_run ON analysis_batch_results(run_id);
CREATE INDEX IF NOT EXISTS idx_analysis_batch_group_time ON analysis_batch_results(group_id, created_at);

-- /查询 中间结果表（Map 批次 + 最终答案落库，审计与断点排查用，2026-08-06）
CREATE TABLE IF NOT EXISTS query_batch_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,          -- 轮次标识（调用时刻毫秒时间戳，同一轮所有批次共用）
    question TEXT NOT NULL,           -- 查询问题
    hours INTEGER NOT NULL,           -- 时间窗（小时）
    batch_index INTEGER NOT NULL,     -- 正=Map 批次；0=最终答案
    total_batches INTEGER NOT NULL,
    batch_char_count INTEGER NOT NULL,
    stage TEXT NOT NULL,              -- 'map' / 'reduce'
    analysis_result TEXT NOT NULL,    -- LLM 输出原文（线索/最终答案）
    is_valid INTEGER NOT NULL DEFAULT 1,  -- 0=LLM 调用失败，1=有效输出（含"无相关信息"）
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_batch_run ON query_batch_results(run_id);
CREATE INDEX IF NOT EXISTS idx_query_batch_group_time ON query_batch_results(group_id, created_at);
"""


def _ensure_settings_db():
    """初始化 Bot 设置数据库"""
    os.makedirs(os.path.dirname(CONFIG["BOT_SETTINGS_DB_PATH"]), exist_ok=True)
    with get_settings_db() as conn:
        conn.executescript(SETTINGS_DB_SCHEMA)
    logger.info(f"📦 Bot 设置数据库: {CONFIG['BOT_SETTINGS_DB_PATH']}")


def _ensure_daily_reports_db():
    """初始化每日报告数据库（总结/评选记录）"""
    os.makedirs(os.path.dirname(CONFIG["DAILY_REPORTS_DB_PATH"]), exist_ok=True)
    with get_daily_reports_db() as conn:
        conn.executescript(DAILY_REPORTS_SCHEMA)
    logger.info(f"📦 每日报告数据库: {CONFIG['DAILY_REPORTS_DB_PATH']}")


@contextmanager
def get_daily_reports_db():
    """获取每日报告数据库连接（总结/评选记录）"""
    conn = sqlite3.connect(CONFIG["DAILY_REPORTS_DB_PATH"], timeout=10)  # busy_timeout 10s
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ============================================================
#  /分析 /查询 中间结果落库（2026-08-06）
# ============================================================
_ANALYSIS_RESULTS_DDL = (
    "CREATE TABLE IF NOT EXISTS analysis_batch_results ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, "
    "run_id INTEGER NOT NULL, target_qqs TEXT NOT NULL, question TEXT NOT NULL, "
    "days INTEGER NOT NULL, batch_index INTEGER NOT NULL, total_batches INTEGER NOT NULL, "
    "batch_char_count INTEGER NOT NULL, stage TEXT NOT NULL, analysis_result TEXT NOT NULL, "
    "is_valid INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)"
)
_QUERY_RESULTS_DDL = (
    "CREATE TABLE IF NOT EXISTS query_batch_results ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, "
    "run_id INTEGER NOT NULL, question TEXT NOT NULL, "
    "hours INTEGER NOT NULL, batch_index INTEGER NOT NULL, total_batches INTEGER NOT NULL, "
    "batch_char_count INTEGER NOT NULL, stage TEXT NOT NULL, analysis_result TEXT NOT NULL, "
    "is_valid INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, "
    "source TEXT NOT NULL DEFAULT 'cmd')"
)


def save_analysis_batch(
    group_id: int, run_id: int, target_qqs: str, question: str, days: int,
    batch_index: int, total_batches: int, batch_char_count: int,
    stage: str, result: str, is_valid: int = 1,
) -> None:
    """保存 /分析 中间结果（Map 批次 / 多级合并 / 最终答案）。

    batch_index：正数=Map 批次号；负数=多级合并中间（-1, -2...）；0=最终答案。
    is_valid=0 表示 LLM 调用失败（失败批次也落库便于追溯）。
    """
    try:
        with get_daily_reports_db() as db:
            # 兜底：表不存在时先建（防止旧库未执行 schema 初始化）
            db.execute(_ANALYSIS_RESULTS_DDL)
            db.execute(
                "INSERT INTO analysis_batch_results (group_id, run_id, target_qqs, question, days, "
                "batch_index, total_batches, batch_char_count, stage, analysis_result, is_valid, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (group_id, run_id, target_qqs, question, days,
                 batch_index, total_batches, batch_char_count, stage, result, is_valid, time.time()),
            )
    except Exception as e:
        logger.warning(f"保存 /分析 中间结果失败 ({stage} {batch_index}): {e}")


def save_query_batch(
    group_id: int, run_id: int, question: str, hours: int,
    batch_index: int, total_batches: int, batch_char_count: int,
    stage: str, result: str, is_valid: int = 1,
    source: str = "cmd",
) -> None:
    """保存 /查询 中间结果（Map 批次 / 最终答案）。

    batch_index：正数=Map 批次号；0=最终答案。
    is_valid=0 表示 LLM 调用失败（失败批次也落库便于追溯）。
    source：cmd=群内 /查询 指令；gui=GUI 消息分析页（08-21 新增列，
    旧表无该列时自动 ALTER 补列）。
    """
    try:
        with get_daily_reports_db() as db:
            # 兜底：表不存在时先建（防止旧库未执行 schema 初始化）
            db.execute(_QUERY_RESULTS_DDL)
            # 08-21：source 列兜底迁移（旧表无此列）
            cols = [r[1] for r in db.execute("PRAGMA table_info(query_batch_results)")]
            if "source" not in cols:
                db.execute("ALTER TABLE query_batch_results "
                           "ADD COLUMN source TEXT NOT NULL DEFAULT 'cmd'")
            db.execute(
                "INSERT INTO query_batch_results (group_id, run_id, question, hours, "
                "batch_index, total_batches, batch_char_count, stage, analysis_result, "
                "is_valid, created_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (group_id, run_id, question, hours,
                 batch_index, total_batches, batch_char_count, stage, result,
                 is_valid, time.time(), source),
            )
    except Exception as e:
        logger.warning(f"保存 /查询 中间结果失败 ({stage} {batch_index}): {e}")


# ============================================================
#  每日报告 CRUD
# ============================================================
def save_daily_summary(group_id: int, date: str, summary: str, message_count: int = 0, user_count: int = 0) -> None:
    """保存每日总结记录"""
    import time
    with get_daily_reports_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_summaries (group_id, date, summary, message_count, user_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, date, summary, message_count, user_count, time.time()),
        )


def get_daily_summary(group_id: int, date: str) -> Optional[dict]:
    """查询指定日期的总结记录"""
    with get_daily_reports_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_summaries WHERE group_id = ? AND date = ?",
            (group_id, date),
        ).fetchone()
    return dict(row) if row else None


def save_daily_evaluation(group_id: int, date: str, evaluation: str, message_count: int = 0, user_count: int = 0) -> None:
    """保存每日评选记录"""
    import time
    with get_daily_reports_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_evaluations (group_id, date, evaluation, message_count, user_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, date, evaluation, message_count, user_count, time.time()),
        )


def get_daily_evaluation(group_id: int, date: str) -> Optional[dict]:
    """查询指定日期的评选记录"""
    with get_daily_reports_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_evaluations WHERE group_id = ? AND date = ?",
            (group_id, date),
        ).fetchone()
    return dict(row) if row else None


def _ensure_db():
    """初始化聊天数据库"""
    os.makedirs(os.path.dirname(CONFIG["DB_PATH"]), exist_ok=True)
    # 迁移：先补列，再执行 Schema（含 CREATE INDEX）
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE message_recalls ADD COLUMN has_image INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE image_archive ADD COLUMN md5_hash TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE message_archive ADD COLUMN has_voice INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 迁移：给 group_chat_cache 添加 message_id 字段
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE group_chat_cache ADD COLUMN message_id INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 迁移：给 session_metadata 添加 history_cleared_at 字段
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE session_metadata ADD COLUMN history_cleared_at REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 迁移（08-21）：消息类型统一列 msg_kind + 存档表下载状态 status
    # msg_kind: text/image/voice/video/file，从 raw_message 的 CQ 标记派生
    # （旧 has_image/has_voice 保留但语义修正为"raw 是否含对应标记"，与下载开关脱钩）
    _migrations = [
        ("ALTER TABLE message_archive ADD COLUMN msg_kind TEXT NOT NULL DEFAULT 'text'", "message_archive.msg_kind"),
        ("ALTER TABLE message_recalls ADD COLUMN msg_kind TEXT NOT NULL DEFAULT 'text'", "message_recalls.msg_kind"),
        ("ALTER TABLE image_archive ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'", "image_archive.status"),
        ("ALTER TABLE voice_archive ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'", "voice_archive.status"),
    ]
    for sql, label in _migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
            logger.info(f"📦 迁移: 新增列 {label}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    # 初始化/创建表
    with get_db() as conn:
        conn.executescript(DB_SCHEMA)
    # 清理 7 天前的缓存
    with get_db() as conn:
        conn.execute("DELETE FROM group_chat_cache WHERE created_at < strftime('%s','now','-7 days')")
    logger.info(f"📦 聊天数据库: {CONFIG['DB_PATH']}")


def ensure_all_dbs():
    """初始化所有数据库（入口函数）"""
    _ensure_db()
    _ensure_persona_db()
    _ensure_settings_db()
    _ensure_daily_reports_db()


# 向后兼容：_ensure_db 内部也初始化其他库
def _ensure_db_compat():
    """兼容旧版调用：同时初始化所有数据库"""
    ensure_all_dbs()


# ============================================================
#  会话管理
# ============================================================
def _session_key(group_id: Optional[int], user_id: int) -> str:
    """生成会话键：群聊 = group_user，私聊 = private_user"""
    if group_id:
        return f"group_{group_id}_user_{user_id}"
    return f"private_user_{user_id}"


def _format_timestamp(ts: float) -> str:
    """将 Unix 时间戳格式化为 [MM-DD 上午/下午/晚上 HH:MM] 样式"""
    from datetime import datetime
    dt = datetime.fromtimestamp(ts)
    period = "上午" if dt.hour < 12 else "下午" if dt.hour < 18 else "晚上"
    return f"[{dt.month:02d}-{dt.day:02d} {period} {dt.hour:02d}:{dt.minute:02d}]"


def get_history(session_key: str) -> list[dict]:
    """从数据库获取最近 N 条对话历史，每条附带时间戳标记
    只取 history_cleared_at 之后的消息（/清除人设后不再参考旧记录）"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT history_cleared_at FROM session_metadata WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        cleared_at = row["history_cleared_at"] if row else 0

        if cleared_at > 0:
            rows = conn.execute(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE session_key = ? AND created_at > ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_key, cleared_at, CONFIG["MAX_HISTORY_MESSAGES"]),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE session_key = ? ORDER BY created_at DESC LIMIT ?",
                (session_key, CONFIG["MAX_HISTORY_MESSAGES"]),
            ).fetchall()
    return [
        {"role": r["role"], "content": f"{_format_timestamp(r['created_at'])} {r['content']}"}
        for r in reversed(rows)
    ]


def save_message(session_key: str, role: str, content: str, user_id: int, nickname: str = ""):
    """保存单条消息到数据库"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (session_key, role, content, user_id, nickname, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_key, role, content, user_id, nickname, time.time()),
        )
        conn.execute(
            "INSERT INTO session_metadata (session_key, last_active, message_count) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "last_active = excluded.last_active, message_count = message_count + 1",
            (session_key, time.time()),
        )
        max_msgs = CONFIG["MAX_HISTORY_MESSAGES"] + 5
        conn.execute(
            "DELETE FROM chat_messages WHERE session_key = ? AND id NOT IN ("
            "SELECT id FROM chat_messages WHERE session_key = ? ORDER BY id DESC LIMIT ?"
            ")",
            (session_key, session_key, max_msgs),
        )


def is_session_expired(session_key: str) -> bool:
    """检查会话是否过期（30 分钟无对话）"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_active FROM session_metadata WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if row is None:
            return False
        return (time.time() - row["last_active"]) > CONFIG["SESSION_TIMEOUT"]


def reset_session(session_key: str, clear_history: bool = False):
    """重置会话状态
    Args:
        session_key: 会话键
        clear_history: True=/清除人设，设置历史截止时间戳（保留数据但不读取）
                       False=会话过期，只重置活跃时间和计数
    """
    with get_db() as conn:
        if clear_history:
            conn.execute(
                "UPDATE session_metadata SET last_active = ?, message_count = 0, history_cleared_at = ? WHERE session_key = ?",
                (time.time(), time.time(), session_key),
            )
            logger.info(f"🗑️ 会话历史标记已重置: {session_key}")
        else:
            conn.execute(
                "UPDATE session_metadata SET last_active = ?, message_count = 0 WHERE session_key = ?",
                (time.time(), session_key),
            )
            logger.info(f"🗑️ 会话过期清理: {session_key}")


# ============================================================
#  群消息缓存
# ============================================================
def _get_user_nickname(user_id: int, group_id: int) -> str:
    """从缓存获取用户昵称（供 pun_game 模块回调使用）"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT nickname FROM group_chat_cache WHERE user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id, group_id),
            ).fetchone()
            if row:
                return str(row[0])
    except Exception as e:
        logger.error(f"获取用户昵称失败: {e}")
    return str(user_id)


def cache_group_message(group_id: int, user_id: int, nickname: str, content: str, message_id: int = 0):
    """缓存群消息到本地（用于 /评选 分析）"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO group_chat_cache (message_id, group_id, user_id, nickname, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, group_id, user_id, nickname, content, time.time()),
        )


def get_today_chat_log(group_id: int) -> list[dict]:
    """获取指定群过去 24 小时的群聊消息（从缓存表读取）"""
    now = time.time()
    yesterday = now - 86400
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, nickname, content, created_at "
            "FROM group_chat_cache "
            "WHERE group_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC",
            (group_id, yesterday, now),
        ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "nickname": r["nickname"] or str(r["user_id"]),
            "content": r["content"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ============================================================
#  屏蔽名单
# ============================================================
def is_blocked(user_id: int) -> bool:
    """检查用户是否在屏蔽名单中"""
    with get_settings_db() as conn:
        row = conn.execute("SELECT 1 FROM user_blocklist WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


def block_user(user_id: int, nickname: str = "", blocked_by: str = "") -> bool:
    """将用户加入屏蔽名单，返回 True 表示是新加入，False 表示已在名单中"""
    with get_settings_db() as conn:
        row = conn.execute("SELECT nickname FROM user_blocklist WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE user_blocklist SET nickname = ? WHERE user_id = ?",
                (nickname, user_id),
            )
            return False
        conn.execute(
            "INSERT INTO user_blocklist (user_id, nickname, blocked_at, blocked_by) VALUES (?, ?, ?, ?)",
            (user_id, nickname, time.time(), blocked_by),
        )
        return True


def unblock_user(user_id: int) -> bool:
    """将用户从屏蔽名单移除，返回 True 表示成功移除，False 表示不在名单中"""
    with get_settings_db() as conn:
        cursor = conn.execute("DELETE FROM user_blocklist WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0


def list_blocked() -> list[dict]:
    """获取屏蔽名单，返回 [{user_id, nickname, blocked_at, blocked_by}, ...]"""
    with get_settings_db() as conn:
        rows = conn.execute(
            "SELECT user_id, nickname, blocked_at, blocked_by FROM user_blocklist ORDER BY blocked_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
#  管理员用户
# ============================================================
def is_admin(user_id: int) -> bool:
    """检查用户是否为管理员"""
    with get_settings_db() as conn:
        row = conn.execute("SELECT 1 FROM admin_users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


def list_admins() -> list[dict]:
    """获取管理员名单"""
    with get_settings_db() as conn:
        rows = conn.execute(
            "SELECT user_id, nickname, added_at FROM admin_users ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
#  内存冷却（不需要持久化）
# ============================================================
cooldown_until: dict[str, float] = {}


def is_on_cooldown(session_key: str) -> bool:
    now = time.time()
    if session_key in cooldown_until and now < cooldown_until[session_key]:
        return True
    return False


def set_cooldown(session_key: str):
    cooldown_until[session_key] = time.time() + CONFIG["COOLDOWN_SECONDS"]


# ============================================================
#  Bot 好感度与关系状态
# ============================================================
def get_bot_favorability(group_id: int, user_id: int) -> tuple[int, str]:
    """
    获取 Bot 对某用户的好感度和关系。
    不存在则创建默认记录（好感度=40, 关系=陌生人）。
    返回 (favorability, relationship)
    """
    with get_settings_db() as conn:
        row = conn.execute(
            "SELECT favorability, relationship FROM bot_favorability WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        if row:
            return row["favorability"], row["relationship"]
        conn.execute(
            "INSERT INTO bot_favorability (group_id, user_id, favorability, relationship, last_updated) "
            "VALUES (?, ?, 40, '陌生人', ?)",
            (group_id, user_id, time.time()),
        )
        return 40, "陌生人"


def update_bot_favorability(group_id: int, user_id: int, delta: int) -> tuple[int, str, str]:
    """
    更新 Bot 对某用户的好感度。

    单次聊天变化限制：delta 范围 [-10, +3]

    非线性变化：
    - 在50附近变化最快（delta 不变）
    - 越接近 0 或 100 变化越小（乘以缩放因子）

    关系转换阈值（含迟滞）：
    - 仇人 → 陌生人:    >= 35
    - 陌生人 → 普通朋友: >= 55
    - 普通朋友 → 好朋友: >= 75
    - 好朋友 → 情侣:     >= 95
    - 情侣 → 好朋友:     < 90
    - 好朋友 → 普通朋友:  < 70
    - 普通朋友 → 陌生人:  < 50
    - 陌生人 → 仇人:     < 30

    情侣关系每个群只能有一个。群内已有情侣时，其他用户好感度上限为 89。
    其他人好感度达到 89 以上时自动降回好朋友。

    返回更新后的 (favorability, new_relationship, old_relationship)
    """
    # 限制单次聊天变化范围：-10 到 +3
    delta = max(-10, min(3, delta))

    with get_settings_db() as conn:
        row = conn.execute(
            "SELECT favorability, relationship FROM bot_favorability WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        if not row:
            return get_bot_favorability(group_id, user_id) + ("",)

        current = row["favorability"]
        old_rel = row["relationship"]

        # 非线性缩放：在50附近变化最大，接近端点时减缓
        if current < 50:
            scale = 0.3 + 0.7 * (current / 50)
        else:
            scale = 0.3 + 0.7 * ((100 - current) / 50)

        if delta != 0:
            abs_scaled = abs(int(delta * scale))
            scaled_delta = max(1, abs_scaled) * (1 if delta > 0 else -1)
        else:
            scaled_delta = 0

        new_fav = max(0, min(100, current + scaled_delta))

        # 确定关系
        new_rel = row["relationship"]
        if new_fav >= 95 and row["relationship"] != "情侣":
            new_rel = "情侣"
        elif new_fav >= 75 and row["relationship"] in ("普通朋友", "陌生人", "仇人"):
            new_rel = "好朋友"
        elif new_fav >= 55 and row["relationship"] in ("陌生人", "仇人"):
            new_rel = "普通朋友"
        elif new_fav >= 35 and row["relationship"] == "仇人":
            new_rel = "陌生人"
        elif new_fav < 90 and row["relationship"] == "情侣":
            new_rel = "好朋友"
        elif new_fav < 70 and row["relationship"] == "好朋友":
            new_rel = "普通朋友"
        elif new_fav < 50 and row["relationship"] == "普通朋友":
            new_rel = "陌生人"
        elif new_fav < 30 and row["relationship"] == "陌生人":
            new_rel = "仇人"

        # 情侣唯一性：如果此人升为情侣，群内其他情侣降为好朋友
        if new_rel == "情侣" and row["relationship"] != "情侣":
            conn.execute(
                "UPDATE bot_favorability SET relationship = '好朋友', favorability = 90, last_updated = ? "
                "WHERE group_id = ? AND user_id != ? AND relationship = '情侣'",
                (time.time(), group_id, user_id),
            )

        # 情侣存在时的上限：群内有情侣且当前用户不是情侣时，好感度上限 89
        if new_rel != "情侣" and user_id != 0:
            existing_couple = conn.execute(
                "SELECT COUNT(*) FROM bot_favorability WHERE group_id = ? AND relationship = '情侣'",
                (group_id,),
            ).fetchone()[0]
            if existing_couple > 0 and new_fav > 89:
                new_fav = 89

        conn.execute(
            "UPDATE bot_favorability SET favorability = ?, relationship = ?, last_updated = ? "
            "WHERE group_id = ? AND user_id = ?",
            (new_fav, new_rel, time.time(), group_id, user_id),
        )

        return new_fav, new_rel, old_rel


def decay_bot_favorability(group_id: int) -> list[tuple[int, str, str, int]]:
    """
    对群内所有用户的好感度进行衰减（每隔 8 小时调用）。
    每次衰减 1-3 点，好感度越高衰减越多。
    好感度最低降至 40。
    返回关系发生变化的列表: [(user_id, new_rel, old_rel, new_fav), ...]

    注意：当前配置为每次衰减 0（实际不衰减）——直接返回空列表，
    不修改任何好感度/关系数据，也不触发关系变化通知。
    """
    # 2026-08-03 用户要求：好感度衰减改为每次衰减 0（实际不衰减）。
    # 原衰减逻辑见下方注释块，恢复时取消注释并删除本 return 即可。
    return []
    # ============ 以下为原有衰减逻辑（已禁用）============
    # with get_settings_db() as conn:
    #     rows = conn.execute(
    #         "SELECT user_id, favorability, relationship FROM bot_favorability WHERE group_id = ?",
    #         (group_id,),
    #     ).fetchall()
    #
    #     changes: list[tuple[int, str, str, int]] = []
    #     for row in rows:
    #         fav = row["favorability"]
    #         rel = row["relationship"]
    #
    #         if fav <= 20:
    #             decay = 1
    #         elif fav <= 50:
    #             decay = 1
    #         elif fav <= 80:
    #             decay = 2
    #         else:
    #             decay = 3
    #
    #         new_fav = max(40, fav - decay)
    #
    #         new_rel = rel
    #         if new_fav < 90 and new_rel == "情侣":
    #             new_rel = "好朋友"
    #         elif new_fav < 70 and new_rel == "好朋友":
    #             new_rel = "普通朋友"
    #         elif new_fav < 50 and new_rel == "普通朋友":
    #             new_rel = "陌生人"
    #         elif new_fav < 30 and new_rel == "陌生人":
    #             new_rel = "仇人"
    #
    #         conn.execute(
    #             "UPDATE bot_favorability SET favorability = ?, relationship = ?, last_updated = ? "
    #             "WHERE group_id = ? AND user_id = ?",
    #             (new_fav, new_rel, time.time(), group_id, row["user_id"]),
    #         )
    #
    #         if new_rel != rel:
    #             changes.append((row["user_id"], new_rel, rel, new_fav))
    #
    #     return changes


# ============================================================
#  群集群管理
# ============================================================
def _ensure_cluster_for_group(group_id: int) -> str:
    """确保群在一个集群中。如果不在任何集群，自动创建一个单人群簇。返回 cluster_id。"""
    import uuid
    with get_settings_db() as conn:
        row = conn.execute(
            "SELECT cluster_id FROM group_cluster_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row:
            return row["cluster_id"]
        # 自动创建单人群簇，该群即为主群
        cluster_id = f"cluster_{group_id}_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO group_clusters (cluster_id, master_group_id, created_at) VALUES (?, ?, ?)",
            (cluster_id, group_id, time.time()),
        )
        conn.execute(
            "INSERT INTO group_cluster_members (cluster_id, group_id, created_at) VALUES (?, ?, ?)",
            (cluster_id, group_id, time.time()),
        )
        return cluster_id


def get_cluster_id(group_id: int) -> Optional[str]:
    """获取群所属的 cluster_id，不在任何集群则返回 None。"""
    with get_settings_db() as conn:
        row = conn.execute(
            "SELECT cluster_id FROM group_cluster_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        return row["cluster_id"] if row else None


def get_cluster_groups(cluster_id: str) -> list[dict]:
    """获取集群中所有群的信息。返回 [{group_id, enable_*}, ...]"""
    with get_settings_db() as conn:
        rows = conn.execute(
            "SELECT group_id, enable_persona_update, enable_profile_update, "
            "enable_question_refill, enable_evaluation, enable_summary "
            "FROM group_cluster_members WHERE cluster_id = ? ORDER BY group_id",
            (cluster_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_cluster_master_group(group_id: int) -> Optional[int]:
    """获取群所在集群的主群（管理员指定的主群）。不在集群则返回 None。"""
    cluster_id = get_cluster_id(group_id)
    if not cluster_id:
        return None
    with get_settings_db() as conn:
        row = conn.execute(
            "SELECT gc.master_group_id FROM group_cluster_members mc "
            "INNER JOIN group_clusters gc ON mc.cluster_id = gc.cluster_id "
            "WHERE mc.group_id = ?",
            (group_id,),
        ).fetchone()
        return row["master_group_id"] if row else None


def merge_clusters(cluster_id_a: str, cluster_id_b: str) -> str:
    """合并两个集群，返回合并后的 cluster_id（保留 A 的 ID，删除 B）。"""
    with get_settings_db() as conn:
        # 将 B 的成员移到 A
        conn.execute(
            "UPDATE group_cluster_members SET cluster_id = ? WHERE cluster_id = ?",
            (cluster_id_a, cluster_id_b),
        )
        # 删除空壳 B
        conn.execute(
            "DELETE FROM group_clusters WHERE cluster_id = ?",
            (cluster_id_b,),
        )
    return cluster_id_a


def add_group_to_cluster(cluster_id: str, group_id: int) -> bool:
    """将群加入集群。如果已在其他集群，先从原集群移除。返回 True 表示成功。"""
    import uuid
    with get_settings_db() as conn:
        # 检查是否已存在
        existing = conn.execute(
            "SELECT cluster_id FROM group_cluster_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if existing:
            old_cid = existing["cluster_id"]
            if old_cid == cluster_id:
                return True
            # 移到新集群
            conn.execute(
                "UPDATE group_cluster_members SET cluster_id = ? WHERE group_id = ?",
                (cluster_id, group_id),
            )
            # 如果原集群空了，清理
            remaining = conn.execute(
                "SELECT COUNT(*) as cnt FROM group_cluster_members WHERE cluster_id = ?",
                (old_cid,),
            ).fetchone()["cnt"]
            if remaining == 0:
                conn.execute(
                    "DELETE FROM group_clusters WHERE cluster_id = ?",
                    (old_cid,),
                )
            return True
        # 确保集群存在
        cluster_exists = conn.execute(
            "SELECT 1 FROM group_clusters WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        if not cluster_exists:
            conn.execute(
                "INSERT INTO group_clusters (cluster_id, created_at) VALUES (?, ?)",
                (cluster_id, time.time()),
            )
        conn.execute(
            "INSERT INTO group_cluster_members (cluster_id, group_id, created_at) VALUES (?, ?, ?)",
            (cluster_id, group_id, time.time()),
        )
        return True


def remove_group_from_cluster(cluster_id: str, group_id: int) -> bool:
    """将群从集群中移除。"""
    with get_settings_db() as conn:
        cursor = conn.execute(
            "DELETE FROM group_cluster_members WHERE cluster_id = ? AND group_id = ?",
            (cluster_id, group_id),
        )
        removed = cursor.rowcount > 0
        # 如果集群空了，删除集群记录
        remaining = conn.execute(
            "SELECT COUNT(*) as cnt FROM group_cluster_members WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()["cnt"]
        if remaining == 0:
            conn.execute(
                "DELETE FROM group_clusters WHERE cluster_id = ?",
                (cluster_id,),
            )
        return removed


def update_group_task_flag(
    group_id: int, task: str, enabled: bool
) -> bool:
    """
    更新群的定时任务开关。
    task: 'persona' | 'profile' | 'question' | 'evaluation' | 'summary'
    """
    column_map = {
        "persona": "enable_persona_update",
        "profile": "enable_profile_update",
        "question": "enable_question_refill",
        "evaluation": "enable_evaluation",
        "summary": "enable_summary",
        "member_notify": "enable_member_notify",  # 入群/退群私聊通知（2026-08-10）
        "mimic": "enable_mimic",  # 赛博模仿（2026-08-13）
    }
    col = column_map.get(task)
    if not col:
        return False
    with get_settings_db() as conn:
        cursor = conn.execute(
            f"UPDATE group_cluster_members SET {col} = ? WHERE group_id = ?",
            (1 if enabled else 0, group_id),
        )
        return cursor.rowcount > 0


def get_group_task_flags(group_id: int) -> Optional[dict]:
    """获取群的所有定时任务开关状态。"""
    with get_settings_db() as conn:
        row = conn.execute(
            "SELECT enable_persona_update, enable_profile_update, "
            "enable_question_refill, enable_evaluation, enable_summary, enable_member_notify, enable_mimic "
            "FROM group_cluster_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "persona": row["enable_persona_update"],
            "profile": row["enable_profile_update"],
            "question": row["enable_question_refill"],
            "evaluation": row["enable_evaluation"],
            "summary": row["enable_summary"],
            "member_notify": row["enable_member_notify"],
            "mimic": row["enable_mimic"],
        }


def get_clusters() -> list[dict]:
    """获取所有集群及其成员。返回 [{cluster_id, master_group_id, members: [{group_id, ...}]}, ...]"""
    with get_settings_db() as conn:
        clusters = conn.execute(
            "SELECT cluster_id, master_group_id FROM group_clusters ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for c in clusters:
            cid = c["cluster_id"]
            members = conn.execute(
                "SELECT group_id, enable_persona_update, enable_profile_update, "
                "enable_question_refill, enable_evaluation, enable_summary "
                "FROM group_cluster_members WHERE cluster_id = ? ORDER BY group_id",
                (cid,),
            ).fetchall()
            if members:
                result.append({
                    "cluster_id": cid,
                    "master_group_id": c["master_group_id"],
                    "members": [dict(m) for m in members],
                })
        return result


def get_active_groups_by_task(task: str) -> list[int]:
    """
    获取开启了某定时任务的所有群 ID（用于定时任务遍历）。
    task: 'persona' | 'profile' | 'question' | 'evaluation' | 'summary'
    默认关闭：未注册的群、开关=0 的群均不执行。
    仅返回开关=1 的群。
    """
    column_map = {
        "persona": "enable_persona_update",
        "profile": "enable_profile_update",
        "question": "enable_question_refill",
        "evaluation": "enable_evaluation",
        "summary": "enable_summary",
        "member_notify": "enable_member_notify",  # 入群/退群私聊通知（2026-08-10）
        "mimic": "enable_mimic",  # 赛博模仿（2026-08-13）
    }
    col = column_map.get(task, "enable_evaluation")
    with get_settings_db() as settings_conn:
        enabled = settings_conn.execute(
            f"SELECT group_id FROM group_cluster_members WHERE {col} = 1"
        ).fetchall()
        return sorted({row["group_id"] for row in enabled})


def get_today_chat_log_merged(group_id: int, start_time: Optional[float] = None, end_time: Optional[float] = None) -> list[dict]:
    """
    获取群及其同集群群的合并消息（按时间排序）。
    默认取过去 24 小时（/总结 和 /评选 指令）；传入 start_time/end_time 时取
    指定时间窗口（半日定时报告用：上午场 [00:00, 11:30)、下午场 [昨天 22:30, 今天 22:30)）。
    """
    now = time.time()
    start = start_time if start_time is not None else (now - 86400)
    end = end_time if end_time is not None else now
    cluster_id = get_cluster_id(group_id)

    # 先获取需要查询的群列表
    query_groups = [group_id]
    if cluster_id:
        with get_settings_db() as sconn:
            rows = sconn.execute(
                "SELECT group_id FROM group_cluster_members WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchall()
            query_groups = [r["group_id"] for r in rows]

    # 再从聊天库查询消息
    with get_db() as conn:
        if len(query_groups) == 1:
            rows = conn.execute(
                "SELECT user_id, nickname, content, created_at, group_id "
                "FROM group_chat_cache "
                "WHERE group_id = ? AND created_at >= ? AND created_at < ? "
                "ORDER BY created_at ASC",
                (query_groups[0], start, end),
            ).fetchall()
        else:
            # 多群合并查询
            placeholders = ", ".join(["?"] * len(query_groups))
            rows = conn.execute(
                f"SELECT user_id, nickname, content, created_at, group_id "
                f"FROM group_chat_cache "
                f"WHERE group_id IN ({placeholders}) AND created_at >= ? AND created_at < ? "
                f"ORDER BY created_at ASC",
                (*query_groups, start, end),
            ).fetchall()

    return [
        {
            "user_id": r["user_id"],
            "nickname": r["nickname"] or str(r["user_id"]),
            "content": r["content"],
            "created_at": r["created_at"],
            "group_id": r["group_id"],
        }
        for r in rows
    ]
