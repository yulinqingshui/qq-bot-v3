#!/usr/bin/env python3
"""
群体角色扮演系统 — 混合架构
===========================
核心设计：结构化状态(SQLite) + 剧情摘要(长期记忆) + 短期窗口(对话风格)

混合式上下文构造（1 次 LLM 调用/行动）：
  1. System Prompt  ← 角色/NPC/物品/场景，来自 SQLite 精确查询 (~300 token)
  2. 剧情摘要        ← 每 5 轮 LLM 生成，存入数据库 (~200 token)
  3. 短期窗口        ← 最近 3-5 条原始消息 (~400 token)
  4. 当前玩家行动     ← 原始文本 (~50 token)
  总计: ~950 token, 延迟 3-5 秒
"""

import asyncio
import json
import os
import re
import sqlite3
import time
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import core.roleplay_prompts as rp_pp  # 提示词默认值 + 渲染（2026-08-22 配置化）

# ─── 配置（2026-08-22 起实时读 config.yaml roleplay 段，热生效）────────────
# 原硬编码常量（SUMMARY_INTERVAL 等）收编至 core/config.py DEFAULTS["roleplay"]，
# 默认值 = 原硬编码值（行为不变）。
# 调用点每次实时读 CONFIG——config.reload() 原地替换后无需重启即生效。

DB_PATH = Path(__file__).parent.parent / "data" / "group_roleplay.db"
DATA_DIR = Path(__file__).parent.parent / "data"
logger = logging.getLogger("group_roleplay")

# 默认值（与 core/config.py DEFAULTS["roleplay"] 保持一致；
# CONFIG 缺失该键时兜底，保证行为与旧版硬编码完全一致）
_RP_DEFAULTS = {
    "summary_interval": 5,      # 摘要触发间隔（轮次）
    "short_window_size": 5,     # 短期窗口大小
    "narrator_min_chars": 400,  # 旁白每幕字数下限
    "narrator_max_chars": 800,  # 旁白每幕字数上限
}


def _rp_rules() -> dict:
    """读取 RP 规则配置（config.yaml roleplay.rules 段，缺项回退默认）。"""
    try:
        from core.config import CONFIG
        raw = (CONFIG.get("RP_CFG") or {}).get("rules") or {}
    except Exception:
        raw = {}
    return {k: int(raw.get(k, v)) for k, v in _RP_DEFAULTS.items()}


def _rp_rule(key: str) -> int:
    return _rp_rules()[key]

# 每房间行动锁：防止"当前玩家校验 → advance_turn"之间被并发消息穿透，
# 连发两条 @bot 消息会双双通过 router 的回合检查，导致跳过玩家回合
_room_action_locks: dict[int, asyncio.Lock] = {}


def _get_room_action_lock(room_id: int) -> asyncio.Lock:
    """获取房间行动锁（懒初始化）"""
    lock = _room_action_locks.get(room_id)
    if lock is None:
        lock = asyncio.Lock()
        _room_action_locks[room_id] = lock
    return lock


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class CharacterState:
    """玩家角色状态（动态更新）"""
    name: str
    hp: int = 100
    fatigue: int = 0      # 0-100, 越高越累
    stress: int = 0       # 0-100, 越高越紧张
    morale: int = 50      # 0-100, 士气
    injuries: str = ""    # 伤病描述

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: str) -> 'CharacterState':
        if not data:
            return cls(name="")
        return cls(**json.loads(data))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ─── 数据库 ──────────────────────────────────────────────────────────────────

# L6 修复：数据库初始化一次性标志 + 线程安全锁
_DB_READY = False
_DB_INIT_LOCK = threading.Lock()


def _ensure_db():
    """创建数据库目录和表结构

    L6 修复：加模块级一次性标志——原实现每次调用都执行 7 个 CREATE TABLE
    IF NOT EXISTS + 2 个 mkdir（get_active_room 每消息调用），高并发群聊下
    产生大量冗余写事务。用锁保证首次初始化线程安全。
    """
    global _DB_READY
    if _DB_READY:
        return
    with _DB_INIT_LOCK:
        if _DB_READY:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        with get_rp_db() as conn:
            conn.executescript("""
                -- 房间
                CREATE TABLE IF NOT EXISTS rp_rooms (
                    room_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id     INTEGER NOT NULL,
                    creator_id   INTEGER NOT NULL,
                    background   TEXT NOT NULL DEFAULT '{}',
                    world_state  TEXT DEFAULT '{}',
                    state        TEXT NOT NULL DEFAULT 'waiting',
                    round_num    INTEGER NOT NULL DEFAULT 0,
                    current_turn INTEGER NOT NULL DEFAULT 0,
                    created_at   REAL NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at   REAL NOT NULL DEFAULT (strftime('%s','now')),
                    UNIQUE(group_id)
                );

                -- 玩家角色
                CREATE TABLE IF NOT EXISTS rp_characters (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id       INTEGER NOT NULL REFERENCES rp_rooms(room_id),
                    user_id       INTEGER NOT NULL,
                    nickname      TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    character_desc TEXT,
                    personality   TEXT DEFAULT '[]',
                    skills        TEXT DEFAULT '[]',
                    inventory     TEXT DEFAULT '[]',
                    status_json   TEXT DEFAULT '{}',
                    turn_order    INTEGER NOT NULL DEFAULT 0,
                    joined_at     REAL NOT NULL DEFAULT (strftime('%s','now')),
                    active        INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(room_id, user_id)
                );

                -- NPC
                CREATE TABLE IF NOT EXISTS rp_npcs (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id        INTEGER NOT NULL REFERENCES rp_rooms(room_id),
                    name           TEXT NOT NULL,
                    role           TEXT,
                    personality    TEXT DEFAULT '[]',
                    motivation     TEXT,
                    relationships  TEXT DEFAULT '{}',
                    inventory      TEXT DEFAULT '[]',
                    location       TEXT,
                    secret         TEXT,
                    reaction_rules TEXT DEFAULT '{}',
                    active         INTEGER NOT NULL DEFAULT 1,
                    created_at     REAL NOT NULL DEFAULT (strftime('%s','now')),
                    UNIQUE(room_id, name)
                );

                -- 物品
                CREATE TABLE IF NOT EXISTS rp_items (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id        INTEGER NOT NULL REFERENCES rp_rooms(room_id),
                    name           TEXT NOT NULL,
                    description    TEXT,
                    owner_user_id  INTEGER,
                    owner_npc_id   INTEGER,
                    location       TEXT,
                    state          TEXT DEFAULT 'normal',
                    created_at     REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                -- 剧情消息（全量保留，用于检索）
                CREATE TABLE IF NOT EXISTS rp_story (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id        INTEGER NOT NULL REFERENCES rp_rooms(room_id),
                    round_num      INTEGER NOT NULL,
                    sequence       INTEGER NOT NULL,
                    speaker_type   TEXT NOT NULL,
                    speaker_id     INTEGER,
                    speaker_name   TEXT,
                    content        TEXT NOT NULL,
                    event_tags     TEXT DEFAULT '[]',
                    created_at     REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                -- 场景状态 + 剧情摘要
                CREATE TABLE IF NOT EXISTS rp_scene_state (
                    room_id          INTEGER PRIMARY KEY REFERENCES rp_rooms(room_id),
                    round_num        INTEGER NOT NULL DEFAULT 0,
                    scene_description TEXT,
                    location         TEXT,
                    time_desc        TEXT,
                    status_notes     TEXT,
                    story_summary    TEXT,
                    summary_round    INTEGER DEFAULT 0,
                    pending_events   TEXT DEFAULT '[]',
                    corrections      TEXT DEFAULT '[]',
                    updated_at       REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                -- 事件索引（用于按需检索）
                CREATE TABLE IF NOT EXISTS rp_events (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id        INTEGER NOT NULL REFERENCES rp_rooms(room_id),
                    event_type     TEXT NOT NULL,
                    involved_ids   TEXT DEFAULT '[]',
                    content        TEXT NOT NULL,
                    round_num      INTEGER NOT NULL,
                    created_at     REAL NOT NULL DEFAULT (strftime('%s','now'))
                );

                CREATE INDEX IF NOT EXISTS idx_story_room ON rp_story(room_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_type ON rp_events(room_id, event_type);
            """)
        _DB_READY = True


@contextmanager
def get_rp_db():
    """数据库连接上下文管理器"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.isolation_level = None  # 自动提交模式
    try:
        yield conn
    finally:
        conn.close()


# ─── 房间管理 ────────────────────────────────────────────────────────────────

def get_active_room(group_id: int) -> Optional[dict]:
    """获取活跃房间"""
    _ensure_db()
    with get_rp_db() as conn:
        row = conn.execute(
            "SELECT * FROM rp_rooms WHERE group_id = ? AND state IN ('waiting', 'playing')",
            (group_id,)
        ).fetchone()
    return dict(row) if row else None


def create_room(group_id: int, creator_id: int, background: str,
                world_state: Optional[dict] = None) -> dict:
    """
    创建游戏房间。

    参数：
        group_id: QQ 群号
        creator_id: 创建者 QQ 号
        background: 背景描述（用户输入或世界观生成结果）
        world_state: 可选的世界观扩展数据
    """
    with get_rp_db() as conn:
        # 清理旧房间
        conn.execute("DELETE FROM rp_story WHERE room_id IN (SELECT room_id FROM rp_rooms WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM rp_scene_state WHERE room_id IN (SELECT room_id FROM rp_rooms WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM rp_characters WHERE room_id IN (SELECT room_id FROM rp_rooms WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM rp_npcs WHERE room_id IN (SELECT room_id FROM rp_rooms WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM rp_items WHERE room_id IN (SELECT room_id FROM rp_rooms WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM rp_events WHERE room_id IN (SELECT room_id FROM rp_rooms WHERE group_id = ?)", (group_id,))
        conn.execute("DELETE FROM rp_rooms WHERE group_id = ?", (group_id,))

        ws_json = json.dumps(world_state or {}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO rp_rooms (group_id, creator_id, background, world_state, state) VALUES (?, ?, ?, ?, 'waiting')",
            (group_id, creator_id, background, ws_json)
        )
        room_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 初始化场景状态
        conn.execute(
            "INSERT INTO rp_scene_state (room_id, round_num) VALUES (?, 0)",
            (room_id,)
        )

        conn.commit()
    return get_room(room_id)


def get_room(room_id: int) -> Optional[dict]:
    """获取房间信息"""
    with get_rp_db() as conn:
        row = conn.execute("SELECT * FROM rp_rooms WHERE room_id = ?", (room_id,)).fetchone()
    if row:
        d = dict(row)
        # 解析 world_state
        if d.get('world_state'):
            try:
                d['world_state'] = json.loads(d['world_state'])
            except (json.JSONDecodeError, TypeError):
                d['world_state'] = {}
        return d
    return None


def update_room(room_id: int, background: str = None, world_state: dict = None, state: str = None):
    """更新房间信息"""
    with get_rp_db() as conn:
        updates = []
        params = []

        if background is not None:
            updates.append("background = ?")
            params.append(background)

        if world_state is not None:
            updates.append("world_state = ?")
            params.append(json.dumps(world_state, ensure_ascii=False))

        if state is not None:
            updates.append("state = ?")
            params.append(state)

        if updates:
            params.append(room_id)
            conn.execute(
                f"UPDATE rp_rooms SET {', '.join(updates)}, updated_at = strftime('%s','now') WHERE room_id = ?",
                params
            )


def end_room(room_id: int):
    """结束房间，清理活跃状态"""
    with get_rp_db() as conn:
        conn.execute("UPDATE rp_rooms SET state = 'ended', updated_at = strftime('%s','now') WHERE room_id = ?", (room_id,))
    # 清理房间行动锁，防止内存泄漏
    _room_action_locks.pop(room_id, None)


def cleanup_room(room_id: int) -> int:
    """级联删除单个房间的全部数据（GUI 角色扮演页「清理已结束房间」，2026-08-22）。

    与 create_room 的旧房清理同语义：7 张表按 room_id 级联删除 +
    清内存行动锁。返回被删剧情条数（GUI 提示用）。
    ⚠️ 调用方必须自行校验 state=='ended'——进行中房间不可清（bot 正在写）。
    """
    with get_rp_db() as conn:
        story_n = conn.execute(
            "SELECT COUNT(*) FROM rp_story WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        for t in ("rp_story", "rp_scene_state", "rp_characters", "rp_npcs",
                  "rp_items", "rp_events", "rp_rooms"):
            conn.execute(f"DELETE FROM {t} WHERE room_id = ?", (room_id,))
        conn.commit()
    _room_action_locks.pop(room_id, None)
    return int(story_n)


def list_rooms() -> list[dict]:
    """全部房间（GUI 房间列表用，按 updated_at 倒序；含各房计数）。"""
    with get_rp_db() as conn:
        rooms = [dict(r) for r in conn.execute(
            "SELECT room_id, group_id, creator_id, background, world_state, state, "
            "round_num, current_turn, created_at, updated_at "
            "FROM rp_rooms ORDER BY updated_at DESC").fetchall()]
        # 各房计数（一次子查询，GUI 列表行显示）
        counts = {
            r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                "SELECT room_id, "
                "(SELECT COUNT(*) FROM rp_characters c WHERE c.room_id = rp_rooms.room_id AND c.active = 1), "
                "(SELECT COUNT(*) FROM rp_npcs n WHERE n.room_id = rp_rooms.room_id AND n.active = 1), "
                "(SELECT COUNT(*) FROM rp_story s WHERE s.room_id = rp_rooms.room_id) "
                "FROM rp_rooms").fetchall()}
    for r in rooms:
        ws = r.get("world_state") or "{}"
        try:
            ws = json.loads(ws) if isinstance(ws, str) else ws
        except (json.JSONDecodeError, TypeError):
            ws = {}
        r["world_state"] = ws
        c = counts.get(r["room_id"], (0, 0, 0))
        # NPC 未导入 DB 表时（/开演 前）回退 world_state.initial_npcs 计数
        if c[1] == 0 and isinstance(ws, dict):
            c = (c[0], len(ws.get("initial_npcs") or []), c[2])
        r["char_count"], r["npc_count"], r["story_count"] = c
    return rooms


# ─── 角色管理 ────────────────────────────────────────────────────────────────

def join_character(room_id: int, user_id: int, nickname: str,
                   character_name: str, character_desc: str = "",
                   personality: list = None, skills: list = None,
                   inventory: list = None) -> bool:
    """
    玩家加入角色。

    参数：
        personality: 性格关键词列表
        skills: 技能列表
        inventory: 初始物品列表
    """
    with get_rp_db() as conn:
        # 检查是否已加入
        existing = conn.execute(
            "SELECT id FROM rp_characters WHERE room_id = ? AND user_id = ?",
            (room_id, user_id)
        ).fetchone()
        if existing:
            return False

        # 计算回合顺序
        count = conn.execute(
            "SELECT COUNT(*) FROM rp_characters WHERE room_id = ? AND active = 1",
            (room_id,)
        ).fetchone()[0]

        conn.execute(
            """INSERT INTO rp_characters
               (room_id, user_id, nickname, character_name, character_desc,
                personality, skills, inventory, status_json, turn_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (room_id, user_id, nickname, character_name, character_desc,
             json.dumps(personality or [], ensure_ascii=False),
             json.dumps(skills or [], ensure_ascii=False),
             json.dumps(inventory or [], ensure_ascii=False),
             CharacterState(name=character_name).to_json(),
             count)
        )
    return True


def update_character_status(room_id: int, user_id: int, status_update: dict):
    """
    更新角色状态（LLM 建议 → 应用）
    
    status_update 格式：{"hp": -20, "fatigue": +10, "injuries": "左手划伤"}
    """
    with get_rp_db() as conn:
        row = conn.execute(
            "SELECT status_json FROM rp_characters WHERE room_id = ? AND user_id = ?",
            (room_id, user_id)
        ).fetchone()
        if not row:
            return

        state = CharacterState.from_json(row['status_json'])

        if 'hp' in status_update:
            state.hp = max(0, min(100, state.hp + status_update['hp']))
        if 'fatigue' in status_update:
            state.fatigue = max(0, min(100, state.fatigue + status_update['fatigue']))
        if 'stress' in status_update:
            state.stress = max(0, min(100, state.stress + status_update['stress']))
        if 'morale' in status_update:
            state.morale = max(0, min(100, state.morale + status_update['morale']))
        if 'injuries' in status_update:
            state.injuries = status_update['injuries']

        conn.execute(
            "UPDATE rp_characters SET status_json = ? WHERE room_id = ? AND user_id = ?",
            (state.to_json(), room_id, user_id)
        )


def leave_character(room_id: int, user_id: int) -> Optional[dict]:
    """玩家退出"""
    with get_rp_db() as conn:
        char = conn.execute(
            "SELECT * FROM rp_characters WHERE room_id = ? AND user_id = ? AND active = 1",
            (room_id, user_id)
        ).fetchone()
        if char:
            conn.execute(
                "UPDATE rp_characters SET active = 0 WHERE room_id = ? AND user_id = ?",
                (room_id, user_id)
            )
        return dict(char) if char else None


def get_characters(room_id: int, active_only: bool = True) -> list[dict]:
    """获取角色列表"""
    where = "WHERE room_id = ?" + (" AND active = 1" if active_only else "")
    with get_rp_db() as conn:
        rows = conn.execute(f"SELECT * FROM rp_characters {where} ORDER BY turn_order", (room_id,)).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d['status'] = CharacterState.from_json(d.get('status_json', '{}'))
        result.append(d)
    return result


def get_player_character(room_id: int, user_id: int) -> Optional[dict]:
    """获取玩家角色"""
    with get_rp_db() as conn:
        row = conn.execute(
            "SELECT * FROM rp_characters WHERE room_id = ? AND user_id = ? AND active = 1",
            (room_id, user_id)
        ).fetchone()
    if row:
        d = dict(row)
        d['status'] = CharacterState.from_json(d.get('status_json', '{}'))
        return d
    return None


# ─── NPC 管理 ────────────────────────────────────────────────────────────────

def add_npc(room_id: int, npc_data: dict) -> int:
    """
    添加 NPC。

    npc_data 格式：
    {
        "name": "老陈",
        "role": "废土商人",
        "personality": ["精明", "贪婪", "有底线"],
        "motivation": "赚取物资",
        "relationships": {"老王": "信任"},
        "inventory": ["抗生素x3", "子弹x50"],
        "location": "废墟市场",
        "secret": "曾是军方后勤官",
        "reaction_rules": {"面对威胁": "先评估实力"}
    }
    """
    with get_rp_db() as conn:
        conn.execute(
            """INSERT INTO rp_npcs
               (room_id, name, role, personality, motivation, relationships,
                inventory, location, secret, reaction_rules)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (room_id, npc_data['name'], npc_data.get('role', ''),
             json.dumps(npc_data.get('personality', []), ensure_ascii=False),
             npc_data.get('motivation', ''),
             json.dumps(npc_data.get('relationships', {}), ensure_ascii=False),
             json.dumps(npc_data.get('inventory', []), ensure_ascii=False),
             npc_data.get('location', ''),
             npc_data.get('secret', ''),
             json.dumps(npc_data.get('reaction_rules', {}), ensure_ascii=False))
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_npcs(room_id: int, active_only: bool = True) -> list[dict]:
    """获取 NPC 列表"""
    where = "WHERE room_id = ?" + (" AND active = 1" if active_only else "")
    with get_rp_db() as conn:
        rows = conn.execute(f"SELECT * FROM rp_npcs {where}", (room_id,)).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        for json_field in ['personality', 'relationships', 'inventory', 'reaction_rules']:
            d[json_field] = json.loads(d.get(json_field, '[]'))
        result.append(d)
    return result


def get_npc_by_name(room_id: int, name: str) -> Optional[dict]:
    """按名称获取 NPC"""
    with get_rp_db() as conn:
        row = conn.execute(
            "SELECT * FROM rp_npcs WHERE room_id = ? AND name = ? AND active = 1",
            (room_id, name)
        ).fetchone()
    if row:
        d = dict(row)
        for json_field in ['personality', 'relationships', 'inventory', 'reaction_rules']:
            d[json_field] = json.loads(d.get(json_field, '[]'))
        return d
    return None


def update_npc_relationship(room_id: int, npc_name: str, target: str, relationship: str):
    """更新 NPC 对某人的关系"""
    with get_rp_db() as conn:
        row = conn.execute(
            "SELECT relationships FROM rp_npcs WHERE room_id = ? AND name = ?",
            (room_id, npc_name)
        ).fetchone()
        if row:
            rels = json.loads(row['relationships'])
            rels[target] = relationship
            conn.execute(
                "UPDATE rp_npcs SET relationships = ? WHERE room_id = ? AND name = ?",
                (json.dumps(rels, ensure_ascii=False), room_id, npc_name)
            )


# ─── 物品管理 ────────────────────────────────────────────────────────────────

def add_item(room_id: int, name: str, description: str = "",
             location: str = "", owner_user_id: int = None,
             owner_npc_id: int = None) -> int:
    """添加物品"""
    with get_rp_db() as conn:
        conn.execute(
            """INSERT INTO rp_items (room_id, name, description, location, owner_user_id, owner_npc_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (room_id, name, description, location, owner_user_id, owner_npc_id)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_item(room_id: int, item_id: int) -> Optional[dict]:
    """获取物品信息"""
    with get_rp_db() as conn:
        row = conn.execute(
            "SELECT * FROM rp_items WHERE room_id = ? AND id = ?",
            (room_id, item_id)
        ).fetchone()
    return dict(row) if row else None


def transfer_item(room_id: int, item_id: int, to_user_id: int = None, to_npc_id: int = None):
    """转移物品归属"""
    with get_rp_db() as conn:
        conn.execute(
            """UPDATE rp_items
               SET owner_user_id = COALESCE(?, owner_user_id),
                   owner_npc_id = CASE WHEN ? IS NOT NULL THEN ? ELSE NULL END
               WHERE room_id = ? AND id = ?""",
            (to_user_id, to_npc_id, to_npc_id, room_id, item_id)
        )


def get_items_by_location(room_id: int, location: str) -> list[dict]:
    """获取某位置的物品"""
    with get_rp_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rp_items WHERE room_id = ? AND location = ?",
            (room_id, location)
        ).fetchall()
    return [dict(r) for r in rows]


def get_items_by_owner(room_id: int, user_id: int) -> list[dict]:
    """获取某玩家拥有的物品"""
    with get_rp_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rp_items WHERE room_id = ? AND owner_user_id = ?",
            (room_id, user_id)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── 剧情消息 ────────────────────────────────────────────────────────────────

def append_story(room_id: int, round_num: int, sequence: int,
                 speaker_type: str, speaker_id: int = None,
                 speaker_name: str = "", content: str = "",
                 event_tags: list = None) -> int:
    """
    追加剧情消息。

    speaker_type: 'narrator' | 'player' | 'system'
    event_tags: 事件标记，用于检索 ["combat", "item_found", "npc_interaction"]
    """
    with get_rp_db() as conn:
        conn.execute(
            """INSERT INTO rp_story
               (room_id, round_num, sequence, speaker_type, speaker_id, speaker_name, content, event_tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (room_id, round_num, sequence, speaker_type, speaker_id, speaker_name, content,
             json.dumps(event_tags or [], ensure_ascii=False))
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_story_recent(room_id: int, limit: int = None) -> list[dict]:
    """获取最近 N 条消息（短期窗口；limit=None 时实时读配置 short_window_size）"""
    if limit is None:
        limit = _rp_rule("short_window_size")
    with get_rp_db() as conn:
        rows = conn.execute(
            """SELECT * FROM rp_story WHERE room_id = ?
               ORDER BY round_num DESC, sequence DESC LIMIT ?""",
            (room_id, limit)
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d['event_tags'] = json.loads(d.get('event_tags', '[]'))
        result.append(d)
    return list(reversed(result))


def get_story_full(room_id: int) -> list[dict]:
    """获取全部剧情（用于剧本查看）"""
    with get_rp_db() as conn:
        rows = conn.execute(
            "SELECT * FROM rp_story WHERE room_id = ? ORDER BY round_num, sequence",
            (room_id,)
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d['event_tags'] = json.loads(d.get('event_tags', '[]'))
        result.append(d)
    return result


def search_events(room_id: int, event_type: str, limit: int = 5) -> list[dict]:
    """按事件类型检索历史（按需检索）"""
    with get_rp_db() as conn:
        rows = conn.execute(
            """SELECT * FROM rp_events WHERE room_id = ? AND event_type = ?
               ORDER BY created_at DESC LIMIT ?""",
            (room_id, event_type, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── 场景状态 + 剧情摘要 ────────────────────────────────────────────────────

def update_scene_state(room_id: int, round_num: int, scene_description: str,
                       location: str = "", time_desc: str = "",
                       status_notes: str = "", pending_events: list = None):
    """更新场景状态"""
    with get_rp_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO rp_scene_state
               (room_id, round_num, scene_description, location, time_desc,
                status_notes, pending_events, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))""",
            (room_id, round_num, scene_description, location, time_desc,
             status_notes, json.dumps(pending_events or [], ensure_ascii=False))
        )


def get_scene_state(room_id: int) -> Optional[dict]:
    """获取场景状态（含剧情摘要）"""
    with get_rp_db() as conn:
        row = conn.execute("SELECT * FROM rp_scene_state WHERE room_id = ?", (room_id,)).fetchone()
    if row:
        d = dict(row)
        d['pending_events'] = json.loads(d.get('pending_events', '[]'))
        d['corrections'] = json.loads(d.get('corrections', '[]'))
        return d
    return None


def update_story_summary(room_id: int, summary: str, round_num: int):
    """更新剧情摘要（异步调用）"""
    with get_rp_db() as conn:
        conn.execute(
            "UPDATE rp_scene_state SET story_summary = ?, summary_round = ? WHERE room_id = ?",
            (summary, round_num, room_id)
        )


# ─── 事件索引 ────────────────────────────────────────────────────────────────

def index_event(room_id: int, event_type: str, content: str,
                involved_ids: list = None, round_num: int = 0):
    """记录事件到索引（用于后续检索）"""
    with get_rp_db() as conn:
        conn.execute(
            """INSERT INTO rp_events (room_id, event_type, content, involved_ids, round_num)
               VALUES (?, ?, ?, ?, ?)""",
            (room_id, event_type, content,
             json.dumps(involved_ids or [], ensure_ascii=False), round_num)
        )


# ─── 回合调度 ────────────────────────────────────────────────────────────────

def advance_turn(room_id: int) -> dict:
    """
    推进到下一位玩家。
    返回 {"player": dict, "is_round_end": bool, "next_round_num": int}
    """
    with get_rp_db() as conn:
        room = conn.execute("SELECT * FROM rp_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not room:
            return {}

        room = dict(room)
        chars = conn.execute(
            "SELECT * FROM rp_characters WHERE room_id = ? AND active = 1 ORDER BY turn_order",
            (room_id,)
        ).fetchall()
        chars = [dict(c) for c in chars]
        total = len(chars)

        if total == 0:
            return {}

        current = room['current_turn'] % total
        round_num = room['round_num']

        # 当前玩家
        current_player = chars[current]

        # 下一位
        next_idx = (current + 1) % total
        is_round_end = (next_idx == 0)

        new_round = round_num + 1 if is_round_end else round_num
        new_turn = 0 if is_round_end else current + 1

        conn.execute(
            "UPDATE rp_rooms SET round_num = ?, current_turn = ?, updated_at = strftime('%s','now') WHERE room_id = ?",
            (new_round, new_turn, room_id)
        )

        # 当前玩家的状态
        status_json = current_player.get('status_json', '{}')
        current_player['status'] = CharacterState.from_json(status_json)

        # 下一位玩家
        next_player = chars[next_idx] if next_idx < total else chars[0]
        next_status_json = next_player.get('status_json', '{}')
        next_player['status'] = CharacterState.from_json(next_status_json)

        return {
            "current_player": current_player,
            "next_player": next_player,
            "is_round_end": is_round_end,
            "new_round_num": new_round,
            "total_players": total,
        }


def get_current_turn_info(room_id: int) -> Optional[dict]:
    """获取当前回合信息"""
    with get_rp_db() as conn:
        room = conn.execute("SELECT * FROM rp_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not room:
            return None

        room = dict(room)
        chars = conn.execute(
            "SELECT * FROM rp_characters WHERE room_id = ? AND active = 1 ORDER BY turn_order",
            (room_id,)
        ).fetchall()
        chars = [dict(c) for c in chars]

        if not chars:
            return None

        current_idx = room['current_turn'] % len(chars)
        current = chars[current_idx]
        current['status'] = CharacterState.from_json(current.get('status_json', '{}'))

        # 下一位
        next_idx = (current_idx + 1) % len(chars)
        next_char = chars[next_idx]
        next_char['status'] = CharacterState.from_json(next_char.get('status_json', '{}'))

        return {
            "room": room,
            "current_player": current,
            "next_player": next_char,
            "all_players": chars,
            "total": len(chars),
        }


# ─── 世界观生成 ──────────────────────────────────────────────────────────────

async def generate_world(background_text: str, llm_call_func) -> dict:
    """
    根据用户简要描述生成完整世界观。

    参数：
        background_text: 用户输入的背景描述
        llm_call_func: LLM 调用函数 (system, messages) -> str (async)

    返回：
        包含背景/场景/规则/势力/NPC(含关系)/物品/冲突/钩子/伏笔/旁白指引的完整世界观
    """
    system = rp_pp.render_prompt("world_gen")
    messages = [{"role": "user", "content": f"请为以下背景生成完整世界观：\n\n{background_text}"}]

    result = await llm_call_func(system, messages)
    logger.info(f"🌍 LLM 原始输出长度: {len(result)} 字符")
    logger.info(f"🌍 LLM 原始输出前 500 字符: {result[:500]}")
    world = _parse_world_json(result, background_text)
    # 检查是否触发了兜底
    if world.get('location') == '未指定' and world.get('time') == '未指定':
        logger.warning(f"⚠️ 世界观生成触发兜底！LLM 原始输出: {result[:200]}")

    # 验证关键字段
    world.setdefault("opening_hooks", [])
    world.setdefault("hidden_plots", [])
    world.setdefault("narrator_guidance", {"opening_tone": "", "pacing": "", "reveal_order": [], "tension_sources": []})

    return world


# Qwen 模型在 JSON 键名中会吞掉 'r' 或截断键名
# 这里维护一个纠错映射表
_WORLD_KEY_CORRECTIONS = {
    # 顶层键名
    "backgound": "background",
    "wold_ules": "world_rules",
    "actions": "factions",        # factions 被写成 actions
    "naato_guidance": "narrator_guidance",
    "atmosphee": "atmosphere",
    # NPC 相关键名
    "ole": "role",
    "pesonality": "personality",
    "elations": "relations",
    "taget": "target",
    "eason": "reason",
    "secet": "secret",
    # 势力/物品相关键名
    "desciption": "description",
    "leade": "leader",
    "stength": "strength",
    "owne": "owner",
    "signiicance": "significance",
    # 冲突/钩子相关键名
    "initial_conlicts": "initial_conflicts",  # 少了一个 f
    "paties": "parties",
    "ugency": "urgency",
    "playe_hook": "player_hook",
    "tigge": "trigger",
    # 旁白指引键名
    "eveal_ode": "reveal_order",
    "tension_souces": "tension_sources",
    "tension_souces": "tension_sources",
}


def _fix_keys_recursive(obj: object) -> object:
    """递归修正 JSON 对象中的键名错误"""
    if isinstance(obj, dict):
        fixed = {}
        for key, value in obj.items():
            correct_key = _WORLD_KEY_CORRECTIONS.get(key, key)
            fixed[correct_key] = _fix_keys_recursive(value)
        return fixed
    elif isinstance(obj, list):
        return [_fix_keys_recursive(item) for item in obj]
    else:
        return obj


def _parse_world_json(text: str, fallback_bg: str) -> dict:
    """
    从 LLM 输出中提取并解析世界观 JSON。
    多层容错：直接解析 → 正则提取 → 清理后解析 → 兜底
    解析成功后自动修正键名拼写错误
    """
    parsed = None

    # 尝试 1: 直接解析
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 提取 JSON 块（供尝试 2/3/4 共用）
    json_match = re.search(r'\{[\s\S]*\}', text)
    if not json_match:
        # 没有 {} 块，直接走兜底
        return _default_world(fallback_bg)

    json_str = json_match.group()
    json_str = json_str.replace('\n', '\n').replace('\\t', ' ')
    json_str = re.sub(r'[\\r\\f]', '', json_str)

    # 尝试 2: 直接解析提取出的 JSON 块
    if not parsed:
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 尝试 3: 修复字符串内未转义的换行符
    if not parsed:
        def fix_json_newlines(m):
            return m.group(0).replace('\n', '\n').replace('"', '\\"')
        json_str = re.sub(r'(?<=: ")[^"]*(?=")', fix_json_newlines, json_str)
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 尝试 4: 去掉 markdown 代码块标记
    if not parsed:
        json_str = re.sub(r'^```json\s*', '', json_str, flags=re.MULTILINE)
        json_str = re.sub(r'^```\s*$', '', json_str, flags=re.MULTILINE)
        json_str = json_str.strip()
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 解析失败 → 兜底
    if not parsed or not isinstance(parsed, dict):
        return _default_world(fallback_bg)

    # ✅ 解析成功 → 递归修正键名
    fixed = _fix_keys_recursive(parsed)

    # 验证关键字段是否存在
    if 'background' in fixed and fixed.get('location') != '未指定':
        logger.info("✅ 世界观 JSON 解析成功并修正键名")
    else:
        logger.warning(f"⚠️ 世界观 JSON 解析成功但关键字段可能缺失: {list(fixed.keys())}")

    # 兜底处理缺失的必填字段
    if not fixed.get('time'):
        fixed['time'] = '未指定'
        logger.warning("⚠️ LLM 未返回 time 字段，已设为默认值")
    if not fixed.get('atmosphere'):
        fixed['atmosphere'] = '未指定'
        logger.warning("⚠️ LLM 未返回 atmosphere 字段，已设为默认值")

    return fixed


def _default_world(background: str) -> dict:
    """默认世界观（兜底）"""
    return {
        "background": background,
        "location": "未指定",
        "time": "未指定",
        "world_rules": [],
        "factions": [],
        "initial_npcs": [],
        "initial_items": [],
        "initial_conflicts": [],
        "opening_hooks": [],
        "hidden_plots": [],
        "narrator_guidance": {"opening_tone": "", "pacing": "", "reveal_order": [], "tension_sources": []},
        "atmosphere": "未指定",
    }


def format_world_for_display(world: dict) -> str:
    """格式化世界观为可读文本（展示核心信息，隐藏伏笔）"""
    lines = []
    lines.append("🌍 **世界观已生成**")
    lines.append(f"\n📖 **背景**：{world.get('background', '')}")
    lines.append(f"📍 **主要场景**：{world.get('location', '未指定')}")
    lines.append(f"🕐 **时间设定**：{world.get('time', '未指定')}")

    if world.get('world_rules'):
        lines.append("\n📜 **世界规则**：")
        for rule in world['world_rules']:
            lines.append(f"  • {rule}")

    if world.get('factions'):
        lines.append("\n⚔️ **势力**：")
        for faction in world['factions']:
            if isinstance(faction, dict):
                lines.append(f"  • {faction.get('name', '?')}：{faction.get('description', '')}（态度：{faction.get('attitude', '中立')}）")
            else:
                lines.append(f"  • {faction}")

    if world.get('initial_npcs'):
        lines.append("\n👥 **已知 NPC**：")
        for npc in world['initial_npcs']:
            if isinstance(npc, dict):
                name = npc.get('name', '?')
                role = npc.get('role', '')
                personality = npc.get('personality', [])
                motivation = npc.get('motivation', '')
                personality_str = f"[{'/'.join(personality)}] " if isinstance(personality, list) and personality else ""
                lines.append(f"  • {name}（{role}）{personality_str}{motivation}")
            else:
                lines.append(f"  • {npc}")

    if world.get('initial_conflicts'):
        lines.append("\n🔥 **初始冲突**：")
        for conflict in world['initial_conflicts']:
            if isinstance(conflict, dict):
                lines.append(f"  • {conflict.get('description', '')}（{conflict.get('urgency', '')}）")
            else:
                lines.append(f"  • {conflict}")

    # 开局钩子 — 给玩家提示
    if world.get('opening_hooks'):
        lines.append("\n🎣 **剧情钩子**：")
        for hook in world['opening_hooks']:
            if isinstance(hook, dict):
                lines.append(f"  • {hook.get('hook', '')}")
            else:
                lines.append(f"  • {hook}")

    lines.append("\n💡 发送 `/报名 角色名:描述` 加入游戏！")

    return "\n".join(lines)


# ─── 混合上下文构建 ─────────────────────────────────────────────────────────

def build_llm_context(room_id: int, phase: str,
                      player_action: str = "", player_user_id: int = None) -> tuple[str, list[dict]]:
    """
    混合式上下文构造：
    1. System Prompt ← 结构化状态 (SQLite)
    2. 剧情摘要       ← 长期记忆 (rp_scene_state)
    3. 短期窗口       ← 最近 3-5 条原始消息
    4. 当前行动       ← 玩家原话
    """
    # ── Stage 1: 结构化状态 ──
    room = get_room(room_id)
    chars = get_characters(room_id, active_only=True)
    npcs = get_npcs(room_id, active_only=True)
    scene = get_scene_state(room_id)
    turn_info = get_current_turn_info(room_id)

    if not room:
        return "房间不存在", []

    # 解析背景
    bg = room.get('background', '{}')
    try:
        bg_parsed = json.loads(bg) if isinstance(bg, str) and bg.startswith('{') else {"背景": bg}
    except (json.JSONDecodeError, TypeError):
        bg_parsed = {"背景": bg}

    bg_text = bg_parsed.get("背景", bg_parsed.get("background", "未指定"))
    bg_location = bg_parsed.get("地点", bg_parsed.get("location", "未指定"))
    bg_time = bg_parsed.get("时间", bg_parsed.get("time", "未指定"))

    # 提取开局钩子
    hooks = bg_parsed.get("opening_hooks", [])
    hooks_str = "\n".join(f"  • {h.get('hook', h) if isinstance(h, dict) else h}" for h in hooks) if hooks else "无"

    # 提取旁白指引
    guidance = bg_parsed.get("narrator_guidance", {})
    if isinstance(guidance, dict):
        opening_tone = guidance.get("opening_tone", "")
        pacing = guidance.get("pacing", "")
        reveal_order = guidance.get("reveal_order", [])
        tension_sources = guidance.get("tension_sources", [])
    else:
        opening_tone = pacing = ""
        reveal_order = tension_sources = []
    reveal_str = " → ".join(reveal_order) if reveal_order else "无特别要求"
    tension_str = "、".join(tension_sources) if tension_sources else "无特别要求"

    # 构建角色信息
    char_lines = []
    for c in chars:
        status = c.get('status')
        status_str = ""
        if status:
            flags = []
            if status.hp < 80:
                flags.append(f"HP:{status.hp}")
            if status.fatigue > 30:
                flags.append(f"疲劳:{status.fatigue}")
            if status.stress > 30:
                flags.append(f"压力:{status.stress}")
            if status.injuries:
                flags.append(f"伤病:{status.injuries}")
            if flags:
                status_str = f" [{', '.join(flags)}]"
        char_lines.append(f"- {c['nickname']} → {c['character_name']}（{c.get('character_desc', '')}）{status_str}")

    char_desc = "\n".join(char_lines) if char_lines else "暂无玩家"

    # 构建 NPC 信息（含关系网和秘密）
    npc_lines = []
    for npc in npcs:
        personality = npc.get('personality', [])
        personality_str = f" 性格: {', '.join(personality)}" if personality else ""
        motivation_str = f" 动机: {npc.get('motivation', '')}" if npc.get('motivation') else ""
        location_str = f" 位置: {npc.get('location', npc.get('position', ''))}" if npc.get('location') or npc.get('position') else ""
        voice_str = f" 说话风格: {npc.get('voice_style', '')}" if npc.get('voice_style') else ""
        # 关系网
        # BUG 修复（2026-08-03）：原代码用 npc.get('relations')，但 schema 与写入均为
        # 'relationships'（dict 格式 {"老王": "信任"}）→ 关系网永远为空。改为正确键并兼容两种格式。
        relations = npc.get('relationships', {})
        relations_str = ""
        if relations:
            rel_parts = []
            if isinstance(relations, dict):
                for target, rel_type in relations.items():
                    rel_parts.append(f"{target}({rel_type})")
            elif isinstance(relations, list):
                for r in relations:
                    if isinstance(r, dict):
                        rel_parts.append(f"{r.get('target', '?')}({r.get('type', '?')})")
                    else:
                        rel_parts.append(str(r))
            relations_str = f" 关系: {', '.join(rel_parts)}"
        npc_lines.append(f"- {npc['name']}（{npc.get('role', '')}）{personality_str}{motivation_str}{location_str}{voice_str}{relations_str}")

    npc_desc = "\n".join(npc_lines) if npc_lines else "暂无 NPC"

    # 场景信息
    scene_desc = scene.get('scene_description', '') if scene else ''
    # 地点和时间（场景优先，空值回退到背景）
    location = (scene.get('location') or bg_location) if scene else bg_location
    time_desc = (scene.get('time_desc') or bg_time) if scene else bg_time

    # 待办事件
    pending = scene.get('pending_events', []) if scene else []
    pending_str = "\n".join(f"  • {e}" for e in pending) if pending else "无"

    # 逻辑修正
    corrections = scene.get('corrections', []) if scene else []
    corrections_str = "\n".join(f"  • {c}" for c in corrections) if corrections else "无"

    # ── System Prompt（2026-08-22 重构：动态段拼装 + 规则模板可配置）──
    # 原实现是一个大 f-string：8 个动态段（世界观/钩子/指引/角色/NPC/场景/
    # 待办/修正记录）与静态规则段混在一起。重构后：
    #   context_block = 代码拼装的 8 个动态段（占位符不变，逐字一致）
    #   规则模板     = roleplay_prompts.render_prompt("narrator")，
    #                  占位符 {context_block}/{min_chars}/{max_chars}
    # 默认模板下渲染结果与旧 f-string 逐字一致（门禁 test_rp_prompts_regression.py）
    import core.roleplay_prompts as rp_pp
    rules = _rp_rules()
    min_chars = rules["narrator_min_chars"]
    max_chars = rules["narrator_max_chars"]
    context_block = f"""【世界观】
背景：{bg_text}
地点：{location}
时间：{time_desc}

【开局剧情钩子】
{hooks_str}

【旁白指引】
开场基调：{opening_tone}
节奏建议：{pacing}
信息揭示顺序：{reveal_str}
【持续张力来源】（轻松基调理解为趣味与温馨，紧张基调理解为悬念与冲突）
{tension_str}

【在场角色】
{char_desc}

【在场 NPC】
{npc_desc}

【当前场景】
{scene_desc}

【待办事件】
{pending_str}

【逻辑修正记录】
{corrections_str}"""
    system_prompt = rp_pp.render_prompt("narrator", {
        "context_block": context_block,
        "min_chars": min_chars,
        "max_chars": max_chars,
    })

    # ── Stage 2: 剧情摘要 ──
    story_summary = scene.get('story_summary', '') if scene else ''

    # ── Stage 3: 短期窗口 ──
    recent = get_story_recent(room_id)

    # ── 组合 messages ──
    messages = []

    # 注入摘要 + 短期窗口
    context_parts = []

    if story_summary:
        context_parts.append(f"【剧情摘要】\n{story_summary}")

    if recent:
        context_parts.append("【最近对话】")
        for msg in recent:
            if msg['speaker_type'] == 'narrator':
                context_parts.append(f"[旁白] {msg['content']}")
            elif msg['speaker_type'] == 'player':
                context_parts.append(f"[{msg['speaker_name']}] {msg['content']}")
            else:
                context_parts.append(f"[系统] {msg['content']}")

    # 当前行动
    if player_action:
        player_name = ""
        if player_user_id:
            char = get_player_character(room_id, player_user_id)
            if char:
                player_name = char['character_name']
            else:
                player_name = f"玩家(player_{player_user_id})"
        else:
            player_name = "未知玩家"
        context_parts.append(f"【当前行动】[{player_name}] {player_action}")

    context_text = "\n\n".join(context_parts)

    # 阶段指令模板（2026-08-22 起可配置，默认值 = 原硬编码逐字）
    if phase == 'opening':
        messages.append({
            "role": "user",
            "content": rp_pp.render_prompt("phase_opening", {
                "char_desc": char_desc,
                "min_chars": min_chars,
                "max_chars": max_chars,
            }),
        })
    elif phase == 'round_end':
        messages.append({
            "role": "user",
            "content": rp_pp.render_prompt("phase_round_end", {
                "context_text": context_text,
                "min_chars": min_chars,
                "max_chars": max_chars,
            }),
        })
    else:
        messages.append({
            "role": "user",
            "content": rp_pp.render_prompt("phase_action", {
                "context_text": context_text,
            }),
        })

    return system_prompt, messages


# ─── 剧情摘要生成 ────────────────────────────────────────────────────────────

def should_generate_summary(room_id: int) -> bool:
    """判断是否需要生成新摘要"""
    scene = get_scene_state(room_id)
    if not scene:
        return False

    room = get_room(room_id)
    if not room:
        return False

    current_round = room.get('round_num', 0)
    summary_round = scene.get('summary_round', 0)

    return (current_round - summary_round) >= _rp_rule("summary_interval")


async def generate_summary(room_id: int, llm_call_func) -> str:
    """
    生成剧情摘要（异步调用，不阻塞主流程）。

    返回 100-150 字的剧情摘要，包含：
    1. 关键决策和因果关系
    2. 角色状态变化
    3. 物品获取/丢失
    4. 未解决的悬念/冲突
    """
    scene = get_scene_state(room_id)
    summary_round = scene.get('summary_round', 0) if scene else 0

    with get_rp_db() as conn:
        rows = conn.execute(
            """SELECT * FROM rp_story WHERE room_id = ? AND round_num > ?
               ORDER BY round_num, sequence""",
            (room_id, summary_round)
        ).fetchall()

    if not rows:
        return scene.get('story_summary', '') if scene else ''

    messages_text = []
    for row in rows:
        r = dict(row)
        if r['speaker_type'] == 'narrator':
            messages_text.append(f"[旁白] {r['content']}")
        elif r['speaker_type'] == 'player':
            messages_text.append(f"[{r['speaker_name']}] {r['content']}")

    system = rp_pp.render_prompt("summary")

    messages = [{"role": "user", "content": "\n".join(messages_text)}]
    summary = await llm_call_func(system, messages)

    # 保存摘要到数据库
    room = get_room(room_id)
    if room:
        update_story_summary(room_id, summary.strip(), room['round_num'])
        logger.info(f"📝 剧情摘要已更新（第 {room['round_num']} 轮）")

    return summary.strip()


# ─── 命令处理 ────────────────────────────────────────────────────────────────


def _handle_join(text: str, group_id: int, user_id: int, nickname: str) -> str:
    """处理 /报名 命令"""
    room = get_active_room(group_id)
    if not room:
        return "⚠️ 还没有人创建游戏房间，请先发送 `/开始扮演 背景:XXX`"

    # 解析 角色名:描述
    if ':' in text or '：' in text:
        separator = ':' if ':' in text else '：'
        parts = text.split(separator, 1)
        char_name = parts[0].strip()
        char_desc = parts[1].strip() if len(parts) > 1 else ""
    else:
        char_name = text.strip()
        char_desc = ""

    success = join_character(room['room_id'], user_id, nickname, char_name, char_desc)
    if not success:
        return f"⚠️ 你已经加入游戏了（角色：{char_name}）"

    chars = get_characters(room['room_id'])
    char_list = "\n".join(f"  • {c['nickname']} → {c['character_name']}（{c.get('character_desc', '')}）" for c in chars)

    lines = [f"✅ **{nickname} 加入游戏！**"]
    lines.append(f"\n🎭 角色：{char_name}（{char_desc}）")
    lines.append(f"\n👥 当前玩家（{len(chars)}人）：\n{char_list}")

    lines.append("💡 发送 `/开演` 开始！")

    return "\n".join(lines)


def _handle_leave(text: str, group_id: int, user_id: int, nickname: str) -> str:
    """处理 /退场 命令"""
    room = get_active_room(group_id)
    if not room:
        return "⚠️ 没有活跃的游戏房间"

    char = leave_character(room['room_id'], user_id)
    if not char:
        return "⚠️ 你还没有加入游戏"

    chars = get_characters(room['room_id'])
    remaining = len(chars)

    lines = [f"👋 {nickname}（{char['character_name']}）退出了游戏。"]
    lines.append(f"剩余 {remaining} 人。")

    if remaining < 1:
        lines.append("🔚 所有玩家已退场，房间结束。")
        end_room(room['room_id'])

    return "\n".join(lines)
def _handle_status(group_id: int, user_id: int) -> str:
    """处理 /状态 命令"""
    room = get_active_room(group_id)
    if not room:
        return "⚠️ 没有活跃的游戏房间"

    bg = room.get('background', '{}')
    try:
        bg_parsed = json.loads(bg) if isinstance(bg, str) and bg.startswith('{') else {"背景": bg}
    except (json.JSONDecodeError, TypeError):
        bg_parsed = {"背景": bg}

    bg_text = bg_parsed.get("背景", bg_parsed.get("background", "未指定"))

    chars = get_characters(room['room_id'])
    # L7 修复：指示器改用 get_current_turn_info（内部按 active=1 过滤 + 对活跃人数取模），
    # 原实现 room['current_turn'] % len(chars) 用全部角色数取模——玩家退场后
    # len(chars) 变小，指示器错位指向错误玩家；全员退场时 len(chars)==0 直接 ZeroDivisionError
    turn_info = get_current_turn_info(room['room_id']) if room['state'] == 'playing' else None
    current_uid = turn_info['current_player']['user_id'] if turn_info else None
    char_lines = []
    for c in chars:
        status = c.get('status')
        if room['state'] == 'playing' and current_uid is not None:
            status_icon = "▶️" if c['user_id'] == current_uid else "⏸️"
        else:
            status_icon = "⏸️"
        status_text = ""
        if status:
            if status.hp < 80:
                status_text += f" [HP:{status.hp}]"
            if status.fatigue > 30:
                status_text += f" [疲劳:{status.fatigue}]"
            if status.injuries:
                status_text += f" [{status.injuries}]"
        char_lines.append(f"  {status_icon} {c['nickname']} → {c['character_name']}{status_text}")

    lines = [f"⏳ **房间状态** ({room['state']})"]
    lines.append(f"\n📖 背景：{bg_text}")
    lines.append(f"\n👥 玩家（{len(chars)}人）：\n" + "\n".join(char_lines))
    lines.append(f"\n📊 已进行 {room['round_num']} 轮")

    if room['state'] == 'playing':
        if turn_info:
            lines.append(f"\n🎯 当前行动：{turn_info['current_player']['nickname']}（{turn_info['current_player']['character_name']}）")
            lines.append(f"⏭️ 下一位：{turn_info['next_player']['nickname']}")
        else:
            lines.append("\n🎯 当前行动：无（没有活跃玩家，请先 /报名）")

    return "\n".join(lines)


def _handle_continue(group_id: int, user_id: int, nickname: str) -> str:
    """处理 /继续 命令"""
    room = get_active_room(group_id)
    if not room:
        return "⚠️ 没有活跃的游戏房间"

    if room['state'] != 'playing':
        return "⚠️ 游戏尚未开始或已结束"

    turn_info = get_current_turn_info(room['room_id'])
    if not turn_info:
        return "⚠️ 没有活跃的玩家"

    current = turn_info['current_player']

    if current['user_id'] == user_id:
        return "💡 现在轮到你行动，请描述你要做什么。"
    else:
        return f"💡 现在轮到 {current['nickname']} 行动，请耐心等待。"


def _handle_end(group_id: int, user_id: int) -> str | None:
    """处理 /结束 命令 — 依次检查角色扮演 → 海龟汤 → 卧底 → 真心话大冒险"""
    room = get_active_room(group_id)
    if room:
        end_room(room['room_id'])

        chars = get_characters(room['room_id'])
        lines = ["🏁 **游戏结束！**"]
        lines.append(f"\n📊 共进行了 {room['round_num']} 轮，{len(chars)} 位玩家参与。")
        lines.append("📝 剧情存档已保存，发送 `/剧本` 查看剧情总结。")

        return "\n".join(lines)

    # 没有活跃角色扮演房间 — 检查其他游戏，返回 None 让 router 继续处理
    import games.turtle_soup as turtle_soup
    if turtle_soup.is_active(group_id):
        return None  # router 会走到 turtle_soup.check_command

    import games.game_spy as game_spy
    if game_spy.is_active(group_id):
        return None  # router 会走到 game_spy.check_command

    # 尝试结束真心话大冒险
    import games.entertainment as entertainment
    if group_id in entertainment._TRUTH_DARE_GAMES:
        return entertainment.end_game(group_id)

    return "⚠️ 当前没有正在进行的游戏哦～"


def _handle_script(group_id: int, user_id: int) -> str:
    """处理 /剧本 命令 — 总结剧情并输出"""
    room = get_active_room(group_id)
    if not room:
        # 尝试查找已结束的房间
        with get_rp_db() as conn:
            row = conn.execute(
                "SELECT * FROM rp_rooms WHERE group_id = ? ORDER BY created_at DESC LIMIT 1",
                (group_id,)
            ).fetchone()
        if row:
            room = dict(row)
        else:
            return "⚠️ 没有找到游戏记录"

    # 获取剧情摘要
    scene = get_scene_state(room['room_id'])
    story_summary = scene.get('story_summary', '') if scene else ''
    total_rounds = room.get('round_num', 0)
    summary_round = scene.get('summary_round', 0) if scene else 0

    # 获取角色信息
    chars = get_characters(room['room_id'])
    char_info = "\n".join(f"  • {c['nickname']} → {c['character_name']}" for c in chars)

    lines = ["📜 **剧情总结**"]
    lines.append(f"🎭 共 {total_rounds} 轮")

    if story_summary:
        lines.append(f"\n{story_summary}")
        if summary_round < total_rounds:
            # 摘要落后于当前轮次，提示最新进展
            with get_rp_db() as conn:
                recent = conn.execute(
                    """SELECT * FROM rp_story WHERE room_id = ? AND round_num > ?
                       ORDER BY round_num DESC LIMIT 3""",
                    (room['room_id'], summary_round)
                ).fetchall()
            if recent:
                lines.append(f"\n📌 最近进展（第 {summary_round + 1}-{total_rounds} 轮）：")
                for entry in recent:
                    r = dict(entry)
                    if r['speaker_type'] == 'narrator':
                        lines.append(f"  🤖 旁白: {r['content'][:50]}...")
                    elif r['speaker_type'] == 'player':
                        lines.append(f"  🗣️ {r['speaker_name']}: {r['content'][:30]}...")
    else:
        # 没有摘要，显示前 5 条消息作为简介
        story = get_story_full(room['room_id'])
        if story:
            lines.append("\n暂无剧情摘要，显示前 5 条消息：")
            for entry in story[:5]:
                r = dict(entry)
                if r['speaker_type'] == 'narrator':
                    lines.append(f"  🤖 旁白: {r['content'][:50]}...")
                elif r['speaker_type'] == 'player':
                    lines.append(f"  🗣️ {r['speaker_name']}: {r['content'][:30]}...")
        else:
            return "📝 暂无剧情记录"

    lines.append(f"\n👥 在场角色：\n{char_info}")
    return "\n".join(lines)


def _handle_start_room(text: str, group_id: int, user_id: int, nickname: str) -> str:
    """
    处理 /开始扮演 命令 — 创建房间并生成世界观。

    返回格式化文本，不包含世界观（由 bot.py 异步 LLM 调用后追加）。
    """
    bg_params = _parse_bg_params(text)
    background = bg_params.get('background', text)

    # 创建房间
    room = create_room(group_id, user_id, background)

    lines = ["🎭 **新游戏房间已创建！**"]
    lines.append(f"\n📖 **背景**：{bg_params.get('background', '未指定')}")
    if bg_params.get('location'):
        lines.append(f"📍 **地点**：{bg_params['location']}")
    if bg_params.get('time'):
        lines.append(f"🕐 **时间**：{bg_params['time']}")

    lines.append("\n👥 玩家：（待报名）")
    lines.append("💡 发送 `/报名 角色名:描述` 加入（创建者也需要报名）")
    lines.append("💡 报名后即可发送 `/开演` 开始！")
    lines.append("💡 发送 `/重新生成世界观 [新描述]` 可重新生成世界观")

    return "\n".join(lines)


def save_world_to_db(room_id: int, world: dict) -> None:
    """
    将 LLM 生成的世界观数据保存到数据库（NPC、物品等）。

    在 /开演 前调用，确保 world_state 中的结构化数据进入数据库表。
    """
    with get_rp_db() as conn:
        # 导入 NPC
        for npc in world.get("initial_npcs", []):
            if not isinstance(npc, dict):
                continue
            name = npc.get("name", "")
            if not name:
                continue
            existing = conn.execute(
                "SELECT id FROM rp_npcs WHERE room_id = ? AND name = ?",
                (room_id, name),
            ).fetchone()
            if existing:
                continue
            relations = npc.get("relations", npc.get("relationships", []))
            conn.execute(
                """INSERT INTO rp_npcs (room_id, name, role, personality, motivation,
                                       relationships, inventory, location, secret, reaction_rules)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    room_id,
                    name,
                    npc.get("role", ""),
                    json.dumps(npc.get("personality", []), ensure_ascii=False),
                    npc.get("motivation", ""),
                    json.dumps(relations, ensure_ascii=False),
                    json.dumps(npc.get("inventory", []), ensure_ascii=False),
                    npc.get("position", npc.get("location", "")),
                    npc.get("secret", ""),
                    json.dumps(npc.get("reaction_rules", {}), ensure_ascii=False),
                ),
            )

        # 导入物品
        for item in world.get("initial_items", []):
            if not isinstance(item, dict):
                continue
            conn.execute(
                """INSERT INTO rp_items (room_id, name, description, location, owner_user_id, owner_npc_id)
                   VALUES (?, ?, ?, ?, NULL, NULL)""",
                (
                    room_id,
                    item.get("name", ""),
                    item.get("description", ""),
                    item.get("location", ""),
                ),
            )


def _handle_start_story(text: str, group_id: int, user_id: int, nickname: str) -> str:
    """
    处理 /开始扮演剧情 指令 — 直接使用用户输入作为世界观，不调用世界观生成器。

    用法：
    /开始扮演剧情 你是大学宿舍里的一个普通学生，今天室友带回了一只流浪猫...
    """
    background = text.strip()
    if not background:
        return "⚠️ 请提供剧情内容。用法：`/开始扮演剧情 你的剧情描述...`"

    room = create_room(group_id, user_id, background)

    world = {
        "background": background,
        "background_text": background,
        "location": "由旁白根据剧情推断",
        "time": "由旁白根据剧情推断",
        "world_rules": [],
        "factions": [],
        "initial_npcs": [],
        "initial_items": [],
        "initial_conflicts": [],
        "opening_hooks": [],
        "hidden_plots": [],
        "narrator_guidance": {
            "opening_tone": "根据剧情内容自行判断",
            "pacing": "由旁白根据剧情决定",
            "reveal_order": [],
            "tension_sources": [],
        },
        "atmosphere": [],
    }
    update_room(room["room_id"], world_state=world)

    lines = ["🎭 **剧情房间已创建！**"]
    lines.append(f"\n📖 **剧情**：{background[:100]}{'...' if len(background) > 100 else ''}")
    lines.append("\n👥 玩家：（待报名）")
    lines.append("💡 发送 `/报名 角色名:描述` 加入（创建者也需要报名）")
    lines.append("💡 报名后即可发送 `/开演` 开始！")
    lines.append("💡 旁白将根据你写的剧情自动生成开场")

    return "\n".join(lines)


def _handle_regenerate_world(text: str, group_id: int, user_id: int, nickname: str) -> str:
    """
    处理 /重新生成世界观 命令。

    返回房间信息 + 提示正在生成世界观（由 bot.py 异步 LLM 调用后追加）。
    """
    room = get_active_room(group_id)
    if not room:
        return "⚠️ 没有找到活跃的游戏房间"

    if room['creator_id'] != user_id:
        return "⚠️ 只有创建者可以重新生成世界观"

    bg_params = _parse_bg_params(text)
    new_background = bg_params.get('background', room['background'])

    lines = ["🔄 **正在重新生成世界观...**"]
    lines.append(f"\n📖 **新背景**：{bg_params.get('background', '未指定')}")
    lines.append("💡 世界观生成完成后，你可以继续 `/报名` 或 `/开演`")

    return "\n".join(lines)


# ─── 后置处理 ────────────────────────────────────────────────────────────────

def _enforce_length(text: str) -> str:
    """
    对旁白文本做基础清理（strip）。
    不做字数截断 — 让模型自由输出完整内容。
    """
    return text.strip()


# ─── 与 bot.py 对接的拦截函数 ──────────────────────────────────────────────────

def check_command(text: str, group_id: int, user_id: int, nickname: str = "") -> Optional[str]:
    """
    检查是否为角色扮演相关命令。
    如果是，返回回复文本；否则返回 None。

    注意：/开演 和 /继续 需要异步 LLM 调用，返回 None 由 bot.py 处理。
    """
    text = text.strip()

    # 去除 @机器人 前缀（含 @botQQ号 动态前缀——运行时从连接派生，不写死）
    prefixes = ["@机器人"]
    try:
        from core.sender import get_bot_uin
        _bq = str(get_bot_uin() or "").strip()
        if _bq:
            prefixes.append(f"@{_bq}")
    except Exception:
        pass
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    if text == "/开始扮演":
        return None  # 由 bot.py 异步处理（自动生成世界观）
    elif text.startswith("/开始扮演 "):
        return None  # 由 bot.py 异步处理（自动生成世界观）
    elif text.startswith("/开始扮演剧情"):
        # 直接使用用户输入作为世界观，不调用世界观生成器
        if len(text) > len("/开始扮演剧情"):
            return _handle_start_story(text[len("/开始扮演剧情"):].strip(), group_id, user_id, nickname)
        return _handle_start_story("", group_id, user_id, nickname)
    elif text == "/重新生成世界观":
        return None  # 由 bot.py 异步处理
    elif text.startswith("/重新生成世界观 "):
        return None  # 由 bot.py 异步处理
    elif text == "/报名":
        return _handle_join("", group_id, user_id, nickname)
    elif text.startswith("/报名 "):
        return _handle_join(text[4:].strip(), group_id, user_id, nickname)
    elif text == "/退场":
        return _handle_leave("", group_id, user_id, nickname)
    elif text.startswith("/退场 "):
        return _handle_leave(text[4:].strip(), group_id, user_id, nickname)
    elif text == "/开演":
        return None  # 开演需要异步 LLM 调用，由 bot.py 处理
    elif text == "/状态":
        return _handle_status(group_id, user_id)
    elif text == "/继续":
        return None  # 继续需要异步 LLM 调用
    elif text == "/结束":
        return _handle_end(group_id, user_id)
    elif text == "/剧本":
        return _handle_script(group_id, user_id)

    return None


def is_roleplay_message(room: dict, user_id: int) -> bool:
    """
    检查当前消息是否应该作为角色扮演中的玩家行动处理。
    返回 True 表示这是一个活跃房间中的玩家消息。
    """
    if not room:
        return False
    # 检查用户是否是房间中的玩家
    chars = get_characters(room['room_id'])
    return any(c['user_id'] == user_id for c in chars)

async def handle_player_action(room_id: int, user_id: int, action: str,
                        llm_call_func, on_reply_func=None,
                        async_summary_func=None) -> dict:
    """
    处理玩家行动（核心流程，async）。

    参数：
        room_id: 房间 ID
        user_id: 行动玩家的用户 ID
        action: 玩家行动文本
        llm_call_func: LLM 调用函数 (system, messages) -> str (async)
        on_reply_func: 回调函数 (reply_text, next_player_dict) -> None
        async_summary_func: 异步摘要函数 (room_id, llm_fn) -> None (async)

    返回：
    {
        "reply": 旁白回复文本,
        "next_player": 下一位玩家信息,
        "is_round_end": 是否本轮结束,
        "new_round": 新轮次
    }
    """
    # ── 1. 记录玩家行动 ──
    char = get_player_character(room_id, user_id)
    if not char:
        return {"reply": "⚠️ 你还没有加入游戏，请先 `/报名`", "next_player": None}

    room = get_room(room_id)
    if not room:
        return {"reply": "⚠️ 房间不存在", "next_player": None}

    # ── 1.5 回合校验 + 推进（原子区） ──
    # 竞态防护：router 在 await 前校验"当前玩家 == 发送者"，但校验与 advance_turn
    # 之间隔着 await（占位消息发送）——连发两条 @bot 消息都能通过前置校验。
    # 锁内重新读取回合状态并校验，确保只有真正的当前玩家能推进回合。
    lock = _get_room_action_lock(room_id)
    async with lock:
        turn_info = get_current_turn_info(room_id)
        if turn_info and turn_info["current_player"]["user_id"] != user_id:
            # 回合已被推进（并发消息抢先），本消息不是当前玩家的有效行动
            return {"reply": "⏳ 已经轮到下一位玩家了，请等待轮到你～", "next_player": None}

        # 追加玩家行动
        append_story(room_id, room['round_num'], 0, 'player', user_id,
                     f"{char['character_name']}", action)

        # ── 2. 推进回合 ──
        turn_result = advance_turn(room_id)
        if not turn_result:
            return {"reply": "⚠️ 没有活跃的玩家", "next_player": None}

    is_round_end = turn_result['is_round_end']
    new_round = turn_result['new_round_num']

    # ── 3. 构建上下文 ──
    phase = 'round_end' if is_round_end else 'turn'
    system_prompt, messages = build_llm_context(
        room_id, phase, player_action=action, player_user_id=user_id
    )

    # ── 4. 调用 LLM 生成旁白 ──
    reply = await llm_call_func(system_prompt, messages)

    # 强制字数约束
    reply = _enforce_length(reply)

    # 获取下一位玩家信息
    next_player = turn_result.get('next_player', {})

    # ── 5. 添加 @提示 ──
    if next_player and not is_round_end:
        next_char_name = next_player.get('character_name', '')
        next_nickname = next_player.get('nickname', '')
        reply += f"\n\n@{next_nickname}（{next_char_name}），轮到你行动了。"
    elif next_player and is_round_end:
        next_char_name = next_player.get('character_name', '')
        next_nickname = next_player.get('nickname', '')
        reply += f"\n\n@{next_nickname}（{next_char_name}），第 {new_round} 轮开始，该你了。"

    # ── 6. 记录旁白回复 ──
    append_story(room_id, new_round, 0, 'narrator', None, '旁白', reply,
                 event_tags=['narrator_turn' if not is_round_end else 'round_summary'])

    # ── 7. 记录事件索引 ──
    index_event(room_id, 'player_action', action, [user_id], new_round)

    # ── 8. 触发回调 ──
    if on_reply_func:
        on_reply_func(reply, next_player)

    # ── 9. 异步摘要检查 ──
    if async_summary_func and should_generate_summary(room_id):
        await async_summary_func(room_id, llm_call_func)

    return {
        "reply": reply,
        "next_player": next_player,
        "is_round_end": is_round_end,
        "new_round": new_round,
    }


async def handle_opening(room_id: int, llm_call_func, on_reply_func=None) -> dict:
    """
    处理游戏开场（旁白生成第一幕，async）。

    参数：
        llm_call_func: LLM 调用函数 (async)
        on_reply_func: 回调 (reply_text) -> None

    返回：
        {"reply": str, "next_player": dict}
    """
    # ── 1. 构建上下文 ──
    system_prompt, messages = build_llm_context(room_id, 'opening')

    # ── 2. 调用 LLM ──
    reply = await llm_call_func(system_prompt, messages)
    reply = _enforce_length(reply)

    # ── 3. 获取第一位玩家 ──
    turn_info = get_current_turn_info(room_id)
    next_player = turn_info['current_player'] if turn_info else {}

    # ── 4. 添加 @提示 ──
    if next_player:
        reply += f"\n\n@{next_player['nickname']}（{next_player['character_name']}），你最先醒来，该你了。"

    # ── 5. 记录 ──
    room = get_room(room_id)
    append_story(room_id, room['round_num'], 0, 'narrator', None, '旁白', reply,
                 event_tags=['opening'])

    # ── 6. 更新场景状态 ──
    scene = get_scene_state(room_id)
    bg = room.get('background', '{}')
    try:
        bg_parsed = json.loads(bg) if isinstance(bg, str) and bg.startswith('{') else {"背景": bg}
    except (json.JSONDecodeError, TypeError):
        bg_parsed = {"背景": bg}

    update_scene_state(
        room_id, room['round_num'],
        scene_description=reply[:200],
        location=bg_parsed.get("地点", bg_parsed.get("location", "未指定")),
        time_desc=bg_parsed.get("时间", bg_parsed.get("time", "未指定")),
        status_notes="游戏开始"
    )

    # ── 7. 回调 ──
    if on_reply_func:
        on_reply_func(reply)

    return {
        "reply": reply,
        "next_player": next_player,
    }


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _parse_bg_params(text: str) -> dict:
    """
    解析背景参数：
    支持格式：
    - "背景:XXX 地点:YYY 时间:ZZZ"
    - "背景:XXX\n地点:YYY\n时间:ZZZ"
    - "背景：XXX 地点：YYY 时间：ZZZ"（中文冒号）
    - 纯文本（无参数键）
    """
    result = {}

    # 统一中文冒号
    text_normalized = text.replace('：', ':')

    # 尝试解析键值对
    keys = ['background', 'location', 'time']
    cn_keys = ['背景', '地点', '时间']

    for key, cn_key in zip(keys, cn_keys):
        # 搜索 key: 或 中文key:
        pattern = rf'(?:{re.escape(key)}|{re.escape(cn_key)}):(.+?)(?:\s+(?:{re.escape(keys[0])}|{re.escape(cn_keys[0])}|{re.escape(keys[1])}|{re.escape(cn_keys[1])}|{re.escape(keys[2])}|{re.escape(cn_keys[2])}):|$)'
        match = re.search(pattern, text_normalized, re.IGNORECASE)
        if match:
            result[key] = match.group(1).strip()
            result[cn_key] = result[key]  # 同时存中文 key

    # 如果没有解析到任何键值对，整个文本作为背景
    if not result or 'background' not in result:
        result['background'] = text.strip()
        result['背景'] = text.strip()

    return result


def is_player_message(group_id: int, user_id: int) -> Optional[dict]:
    """
    检查发送者是否为游戏中的活跃玩家。

    返回：
        None → 不是玩家
        dict → 房间信息 + 玩家信息
    """
    room = get_active_room(group_id)
    if not room or room['state'] != 'playing':
        return None

    char = get_player_character(room['room_id'], user_id)
    if not char:
        return None

    return {"room": room, "character": char}


# ─── Mock LLM（测试用）───────────────────────────────────────────────────────

MOCK_NARRATOR_RESPONSES = [
    """灰色的晨光刺破浓雾，照在这片被遗忘的废墟城市上。断壁残垣之间，野草已经长到膝盖高。远处传来丧尸的低吼声。

你们几个人在一座废弃加油站的地下避难所里醒来。空气中弥漫着霉味和铁锈味。角落里有一台还能用的无线电，偶尔发出沙沙的电流声。""",

    """行动产生了预期之外的结果。周围的空气突然安静下来，远处传来金属碰撞的声音——可能是其他幸存者，也可能是更可怕的东西。

你注意到地面在微微震动，有什么东西正在靠近。""",

    """你的行动引起了连锁反应。原本安静的废墟突然变得喧闹起来。

远处的丧尸群似乎被声响吸引，正在向你们的方向移动。时间不多了。""",

    """【本轮总结】

这一轮中，你们做出了几个关键决策。局势正在发生变化。

夜幕正在降临，你们只有不到两个小时的天光。前方的路充满了未知。""",
]

_mock_index = 0


async def mock_llm_call(system: str, messages: list) -> str:
    """模拟 LLM 调用（测试用，async）"""
    global _mock_index
    response = MOCK_NARRATOR_RESPONSES[_mock_index % len(MOCK_NARRATOR_RESPONSES)]
    _mock_index += 1
    return response


async def mock_worldgen_llm(system: str, messages: list) -> str:
    """模拟世界观生成 LLM（测试用，async）"""
    return json.dumps({
        "background": "末日废土世界，三年前的一场未知灾难摧毁了现代文明。城市变成废墟，幸存者分散在各处。丧尸游荡在街头，但真正致命的威胁来自其他幸存者之间的争夺。",
        "location": "废墟城市 - 加油站避难所",
        "time": "灾难后第三年，初秋",
        "world_rules": [
            "丧尸对声音和光线敏感，但对静态目标迟钝",
            "干净的水源极其稀缺",
            "弹药是不可再生的战略物资",
            "信任比武器更珍贵，但也更脆弱"
        ],
        "factions": [
            {"name": "废墟市场", "description": "由商人老陈控制的地下交易网络", "attitude": "中立，只认物资"},
            {"name": "守望者", "description": "前军方残余，占据城市北部的哨塔", "attitude": "敌对，视流浪者为威胁"},
            {"name": "医疗站", "description": "由前医生管理的避难所，收留难民", "attitude": "友善，但资源有限"}
        ],
        "initial_npcs": [
            {
                "name": "老陈",
                "role": "废土商人",
                "personality": ["精明", "贪婪", "有底线"],
                "motivation": "赚取物资，暗中收集武器",
                "location": "废墟市场",
                "secret": "曾是军方后勤官",
                "relationships": {},
                "inventory": ["抗生素x3", "子弹x50", "地图"],
                "reaction_rules": {"面对威胁": "先评估实力，弱势则逃跑，强势则交易"}
            },
            {
                "name": "林医生",
                "role": "前医院医生",
                "personality": ["冷静", "慈悲", "疲惫"],
                "motivation": "救治伤者，维持医疗站运转",
                "location": "医疗站",
                "secret": "自己已经感染了但尚未发作",
                "relationships": {},
                "inventory": ["手术刀", "绷带x10", "抗生素x5"],
                "reaction_rules": {"面对伤者": "优先救治，不问身份"}
            }
        ],
        "initial_items": [
            {"name": "猎刀", "description": "一把锋利的猎刀，刃口完好", "location": "加油站避难所"},
            {"name": "无线电", "description": "一台老式无线电，偶尔能收到信号", "location": "加油站避难所"},
            {"name": "弹药x10", "description": "10 发手枪子弹", "location": "加油站避难所"},
            {"name": "水壶", "description": "空的军用水壶", "location": "加油站避难所"},
            {"name": "急救包", "description": "基础急救物资", "location": "加油站避难所"},
        ],
        "initial_conflicts": [
            "丧尸群正在向城区中心聚集，原因不明",
            "守望者哨塔最近开始搜捕流浪者",
            "医疗站的药品即将耗尽",
            "废墟市场传言有人发现了地下水源"
        ],
        "atmosphere": "压抑、危险、资源稀缺、信任危机"
    })


if __name__ == "__main__":
    _ensure_db()
    print("✅ 群体角色扮演模块初始化完成")
    print(f"   数据库: {DB_PATH}")
