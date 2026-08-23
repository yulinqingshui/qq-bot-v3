#!/usr/bin/env python3
"""
谁是卧底 游戏模块 - 独立于主程序，提供"谁是卧底"文字推理游戏。
集成到 bot.py 的群聊消息流中使用。

游戏流程：
1. /卧底 创建房间，/卧底加入 加入游戏
2. /卧底开始 发词 + 公布发言顺序
3. 发言阶段：玩家群内发言描述，不可包含词语中的任何字
4. 投票阶段：所有人发过或超时后自动投票，@Bot 视为投票
5. 得票最多者出局，循环直至胜利条件达成

游戏模式：
- 普通模式（默认）：平民 + 卧底
- 白板模式：平民 + 卧底 + 白板（/卧底 白板）
"""

# 延迟注解求值：Player 等类型定义在文件后部（411 行），
# 3.13 及以下立即求值会 NameError；3.14+ 默认延迟（PEP 649）无此问题。
# 显式声明保证所有 Python 版本行为一致。
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
#  LLM 判定配置
# ============================================================
# LLM 后端统一走 core.config（v2）：deepseek/local 可热切 + llm.enabled 总开关，
# 各判定函数内 _resolve_llm_backend(_get_config()) 解析，不再硬编码地址

# LLM 判定开关
_ENABLE_LLM_JUDGE = True

# 判定超时（秒）
_LLM_TIMEOUT = 60

# ============================================================
#  路径配置
# ============================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORDBANK_PATH = os.path.join(BASE_DIR, "data", "question_bank", "spy_wordbank.txt")
_HISTORY_DB_PATH = os.path.join(BASE_DIR, "data", "spy_history.db")

# ============================================================
#  历史记录数据库
# ============================================================


@contextmanager
def _spy_get_db():
    """卧底游戏历史记录数据库上下文管理器"""
    os.makedirs(os.path.dirname(_HISTORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_HISTORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_history_db():
    """初始化历史记录数据库"""
    with _spy_get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS used_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_civilian TEXT NOT NULL,
            word_spy TEXT NOT NULL,
            word_blank TEXT DEFAULT '',
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_words ON used_words(word_civilian, word_spy)")

        # 玩家战绩统计
        conn.execute("""
        CREATE TABLE IF NOT EXISTS spy_stats (
            user_id INTEGER NOT NULL,
            nickname TEXT,
            wins INTEGER DEFAULT 0,
            total_games INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id)
        )
        """)


def _record_used_word(word_civilian: str, word_spy: str, word_blank: str = ""):
    """记录已使用的词语"""
    with _spy_get_db() as conn:
        conn.execute(
            "INSERT INTO used_words (word_civilian, word_spy, word_blank) VALUES (?, ?, ?)",
            (word_civilian, word_spy, word_blank)
        )
    logger.info(f"📝 记录已使用词语: {word_civilian} | {word_spy} | {word_blank}")


def _get_used_words() -> set:
    """获取所有已使用的词语对（包含互换方向）"""
    used = set()
    with _spy_get_db() as conn:
        cursor = conn.execute("SELECT word_civilian, word_spy FROM used_words")
        for row in cursor:
            # 记录两种顺序，因为平民/卧底词可能互换
            used.add((row[0], row[1]))
            used.add((row[1], row[0]))
    return used


def _clear_used_words() -> int:
    """清空历史记录，返回清空的数量"""
    with _spy_get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM used_words")
        count = cursor.fetchone()[0]
        conn.execute("DELETE FROM used_words")
    logger.info(f"🗑️ 清空 {count} 条历史记录")
    return count


def _get_used_count() -> int:
    """获取已使用词语的数量"""
    with _spy_get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM used_words")
        count = cursor.fetchone()[0]
    return count


def _record_game_stats(players: list[Player], winners: list[Player]) -> None:
    """记录本场游戏战绩"""
    winner_uids = {p.user_id for p in winners}
    with _spy_get_db() as conn:
        for p in players:
            win = 1 if p.user_id in winner_uids else 0
            conn.execute("""
                INSERT INTO spy_stats (user_id, nickname, wins, total_games, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    wins = spy_stats.wins + excluded.wins,
                    total_games = spy_stats.total_games + excluded.total_games,
                    updated_at = CURRENT_TIMESTAMP
            """, (p.user_id, p.nickname, win, 1))


def _get_leaderboard() -> str:
    """获取玩家排行榜前 5 名（按获胜次数排名）"""
    with _spy_get_db() as conn:
        cursor = conn.execute("""
            SELECT user_id, nickname, wins, total_games
            FROM spy_stats
            WHERE wins > 0
            ORDER BY wins DESC, total_games ASC
            LIMIT 5
        """)
        rows = cursor.fetchall()

    if not rows:
        return ""

    lines = ["\n🏅 **玩家排行榜（Top 5）**"]
    medal = ["🥇", "🥈", "🥉"]
    for i, (uid, nick, wins, total) in enumerate(rows):
        rate = wins / total * 100 if total > 0 else 0
        prefix = medal[i] if i < 3 else f"  {i+1}."
        lines.append(f"  {prefix} {nick} — {wins}胜/{total}局 ({rate:.0f}%)")

    return "\n".join(lines)


# 初始化数据库
_init_history_db()

# ============================================================
#  题库加载
# ============================================================


def _load_wordbank() -> list[tuple[str, str, str]]:
    """
    加载题库，返回 [(平民词, 卧底词, 白板词), ...] 列表。
    - 两词格式 "A|B" → 白板词 = ""
    - 三词格式 "A|B|C" → 白板词 = C
    """
    entries: list[tuple[str, str, str]] = []
    try:
        with open(WORDBANK_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) == 2:
                    entries.append((parts[0], parts[1], ""))
                elif len(parts) == 3:
                    entries.append((parts[0], parts[1], parts[2]))
    except FileNotFoundError:
        logger.error(f"卧底题库未找到: {WORDBANK_PATH}")
    except Exception as e:
        logger.error(f"卧底题库加载失败: {e}")
    return entries


_WORD_BANK = _load_wordbank()
logger.info(f"🕵️ 卧底题库加载完成，共 {len(_WORD_BANK)} 组词")


def reload_wordbank() -> int:
    """重新加载题库（热更新）"""
    global _WORD_BANK
    _WORD_BANK = _load_wordbank()
    return len(_WORD_BANK)


# ============================================================
#  游戏状态管理
# ============================================================
# {group_id: game_state}
_SPY_GAMES: dict[int, dict] = {}

# 发言超时任务 {group_id: asyncio.Task}
_SPEAK_TIMEOUT_TASKS: dict[int, asyncio.Task] = {}

# 白板猜词超时任务 {group_id: asyncio.Task}
_BLANK_GUESS_TIMEOUT_TASKS: dict[int, asyncio.Task] = {}

# 投票超时任务 {group_id: asyncio.Task}
_VOTE_TIMEOUT_TASKS: dict[int, asyncio.Task] = {}

# PK 发言/投票超时任务 {group_id: asyncio.Task}（BUG 修复 2026-08-03：PK 阶段原本无任何超时）
_PK_SPEAK_TIMEOUT_TASKS: dict[int, asyncio.Task] = {}
_PK_VOTE_TIMEOUT_TASKS: dict[int, asyncio.Task] = {}

# 群 websocket 引用 {group_id: websocket}（供定时器回调使用）
_WEBSOCKETS: dict[int, any] = {}

# LLM 异步判定任务 {group_id: list[asyncio.Task]}
_LLM_JUDGE_TASKS: dict[int, list[asyncio.Task]] = {}


def _cancel_task_safe(task: Optional[asyncio.Task]) -> None:
    """安全取消 asyncio 任务。

    BUG 修复（2026-08-03 二次审查）：定时器回调（_blank_guess_timeout / _vote_timeout /
    _timeout_handler）在自身运行期间会 pop 出【正在运行的自己】并 cancel()，
    导致下一个 await 点抛出 CancelledError，超时公告消息全部丢失。
    这里跳过当前运行的任务——自身即将结束，无需取消。
    """
    if task is None:
        return
    try:
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
    except RuntimeError:
        # 无运行事件循环（同步上下文调用）时 current_task() 抛 RuntimeError
        if not task.done():
            task.cancel()


async def _blank_guess_timeout(group_id: int):
    """白板猜词倒计时结束，继续游戏"""
    await asyncio.sleep(60)
    game = _get_game(group_id)
    if not game or game["phase"] != "blank_guessing":
        return
    task = _BLANK_GUESS_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(task)

    # 取消白板出局检查，继续游戏
    result = _check_and_end_game(group_id)
    if result:
        msg = result
    else:
        # 继续下一轮
        game["round"] += 1
        remaining = sum(1 for p in game["players"].values() if p.is_alive())
        msg = f"⏰ 白板猜词超时，继续游戏！准备第 {game['round']} 轮发言..."
        _start_next_round(group_id)
        # 公布下一轮发言顺序
        msg += "\n\n📢 下一轮发言顺序：\n"
        order = game["speaker_order"]
        for i, uid in enumerate(order, 1):
            p = game["players"].get(uid)
            if p and p.is_alive():
                msg += f"  {i}. {p.nickname}\n"
        first_uid = order[0] if order else None
        first_player = game["players"].get(first_uid) if first_uid else None
        if first_player and first_player.is_alive():
            msg += f"\n🎤 请 {first_player.nickname} 开始发言！\n"

    # 通知群
    await _send_group_message(group_id, msg.rstrip())


def _start_blank_guess_timeout(group_id: int, blank_user_id: int):
    """启动白板猜词倒计时"""
    task = asyncio.create_task(_blank_guess_timeout(group_id))
    _BLANK_GUESS_TIMEOUT_TASKS[group_id] = task


async def _vote_timeout(group_id: int):
    """投票倒计时结束，自动结算当前票数"""
    await asyncio.sleep(120)  # 投票超时 120 秒
    game = _get_game(group_id)
    if not game or game["phase"] != "voting":
        return
    task = _VOTE_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(task)

    # 缺陷 2 修复：投票超时 0 票时 → 平安夜
    if not game["voters"]:
        msg = "⏰ 投票超时！无人投票，本轮平安夜\n"
        peaceful_msg = _handle_peaceful_night(group_id, "无人投票")
        if peaceful_msg:
            msg += peaceful_msg
        await _send_group_message(group_id, msg)
        return

    # 有投票 → 正常结算
    msg = "⏰ 投票超时！自动结算当前票数...\n"
    vote_result = _resolve_votes(group_id)
    msg += vote_result
    await _send_group_message(group_id, msg)


# ============================================================
#  PK 阶段超时（BUG 修复 2026-08-03：原实现 PK 阶段无任何超时，
#  候选人挂机/投票者潜水会永久卡死游戏）
# ============================================================
def _start_pk_speak_timeout(group_id: int):
    """启动 PK 发言超时（默认 120 秒），超时后自动进入 PK 投票"""
    game = _get_game(group_id)
    if not game or game.get("phase") != "pk_speaking":
        return

    old_task = _PK_SPEAK_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(old_task)

    timeout = game.get("pk_speak_timeout", 120)

    async def _timeout_handler():
        await asyncio.sleep(timeout)
        g = _get_game(group_id)
        if not g or g.get("phase") != "pk_speaking":
            return
        _PK_SPEAK_TIMEOUT_TASKS.pop(group_id, None)
        # 未发言者视为放弃发言，直接进入 PK 投票
        msg = "⏰ PK 发言超时！未发言者视为放弃，进入二次投票...\n"
        vote_msg = _enter_pk_voting(group_id)
        if vote_msg:
            msg += vote_msg
        await _send_group_message(group_id, msg)

    task = asyncio.create_task(_timeout_handler())
    _PK_SPEAK_TIMEOUT_TASKS[group_id] = task
    logger.info(f"⏱️ PK 发言定时器已启动，{timeout}秒后触发")


def _start_pk_vote_timeout(group_id: int):
    """启动 PK 投票超时（默认 120 秒），超时后自动结算"""
    game = _get_game(group_id)
    if not game or game.get("phase") != "pk_voting":
        return

    old_task = _PK_VOTE_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(old_task)

    timeout = game.get("pk_vote_timeout", 120)

    async def _timeout_handler():
        await asyncio.sleep(timeout)
        g = _get_game(group_id)
        if not g or g.get("phase") != "pk_voting":
            return
        _PK_VOTE_TIMEOUT_TASKS.pop(group_id, None)
        msg = "⏰ PK 投票超时！自动结算当前票数...\n"
        result = _resolve_pk_vote(group_id)
        msg += result
        await _send_group_message(group_id, msg)

    task = asyncio.create_task(_timeout_handler())
    _PK_VOTE_TIMEOUT_TASKS[group_id] = task
    logger.info(f"⏱️ PK 投票定时器已启动，{timeout}秒后触发")


def _start_vote_timeout(group_id: int):
    """启动投票倒计时"""
    # L3 修复：先取消并清除旧任务——重开投票（如 PK 后重新计票）时
    # 旧超时任务未 pop，若旧任务晚触发会误结算新投票
    old = _VOTE_TIMEOUT_TASKS.pop(group_id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(_vote_timeout(group_id))
    _VOTE_TIMEOUT_TASKS[group_id] = task


# 默认发言时间（秒）
DEFAULT_SPEAK_TIMEOUT = 180  # 3 分钟


class Player:
    """玩家信息"""

    def __init__(self, user_id: int, nickname: str):
        self.user_id = user_id
        self.nickname = nickname
        self.role: str = ""          # "civilian" | "spy" | "blank"
        self.word: str = ""
        self.word_chars: set[str] = set()  # 词语中所有字符（用于违规判定）
        self.alive = True
        self.has_spoken = False
        self.description: str = ""   # 本轮发言内容
        self.vote_count: int = 0     # 本轮被投票数
        self.pk_vote_count: int = 0  # PK二次投票被投票数

    def is_alive(self) -> bool:
        return self.alive


# ============================================================
#  公开接口
# ============================================================
def _get_game(group_id: int) -> Optional[dict]:
    return _SPY_GAMES.get(group_id)


def is_active(group_id: int) -> bool:
    game = _get_game(group_id)
    return game is not None and game.get("active", False)


def get_phase(group_id: int) -> str:
    game = _get_game(group_id)
    return game.get("phase", "idle") if game else "idle"


# ============================================================
#  游戏创建与加入
# ============================================================
def create_game(group_id: int, mode: str = "normal",
                blank_mode: str = "guess") -> Optional[dict]:
    """
    创建新游戏。
    mode: "normal" (平民+卧底) | "blank" (平民+卧底+白板)
    blank_mode: "guess" (模式A-猜词流，默认) | "attach" (模式B-依附流)
    返回游戏状态或 None（已有进行中的游戏）
    """
    if group_id in _SPY_GAMES and _SPY_GAMES[group_id].get("active"):
        return None  # 已有进行中的游戏

    # 取消旧定时器
    old_task = _SPEAK_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(old_task)

    game: dict = {
        "group_id": group_id,
        "mode": mode,  # "normal" | "blank"
        # lobby | speaking | voting | pk_speaking | pk_voting | blank_guessing
        # | ended
        "phase": "lobby",
        "active": True,
        "players": {},  # {user_id: Player}
        "speaker_order": [],  # 本轮发言顺序 [user_id, ...]
        "current_speaker_idx": 0,
        "round": 1,
        # 问题 1 修复：唯一 game_id，防止 LLM 跨局误杀
        "game_id": str(uuid.uuid4())[:8],
        "word_civilian": "",
        "word_spy": "",
        "word_blank": "",
        "speak_timeout": DEFAULT_SPEAK_TIMEOUT,
        "speak_start_time": 0,
        "voters": set(),  # 本轮已投票玩家 user_id 集合
        "creator_id": 0,
        # PK 平票相关
        "pk_candidates": [],  # PK候选人 user_id 列表
        "pk_speeches": {},    # {user_id: speech_text}
        "pk_voters": set(),   # PK二次投票已投票玩家集合
        # 僵局检测（连续平票轮次）
        "stale_rounds": 0,
        # 白板模式配置
        # "guess" (模式A-猜词流) | "attach" (模式B-依附流)，默认模式A
        "blank_mode": blank_mode,
        "blank_guessing_player": None,  # 正在猜词的白板玩家 user_id
        "blank_eliminated_word": "",  # 白板出局时平民词（用于猜词比对）
    }
    _SPY_GAMES[group_id] = game
    return game


def join_game(group_id: int, user_id: int, nickname: str) -> tuple[bool, str]:
    """
    玩家加入游戏。
    返回 (success, message)
    """
    game = _get_game(group_id)
    if not game:
        return False, "⚠️ 当前没有可加入的卧底游戏，发送 /卧底 创建一局"

    phase = game.get("phase", "idle")
    if phase not in ("lobby", "idle"):
        return False, f"⚠️ 游戏已在{phase_map(phase)}中，无法加入"

    if user_id in game["players"]:
        return False, "✅ 你已经在游戏中了"

    player = Player(user_id, nickname)
    game["players"][user_id] = player
    count = len(game["players"])

    mode_desc = "（白板模式）" if game["mode"] == "blank" else ""
    return True, f"✅ {nickname} 加入游戏！当前 {count} 人{mode_desc}"


def leave_game(group_id: int, user_id: int) -> str:
    """玩家退出游戏"""
    game = _get_game(group_id)
    if not game:
        return "⚠️ 当前没有卧底游戏"

    if user_id not in game["players"]:
        return "⚠️ 你不在游戏中"

    nickname = game["players"][user_id].nickname

    # 如果发言/投票阶段有人退出
    if game["phase"] in ("speaking", "voting"):
        # 从发言顺序移除
        if user_id in game["speaker_order"]:
            # Bug #6 修复：先记录退出玩家的索引位置
            exit_idx = game["speaker_order"].index(user_id)
            current_idx = game["current_speaker_idx"]

            game["speaker_order"].remove(user_id)

            # 修正当前发言者索引：
            # - 如果退出者在当前索引之前，索引减 1（因为后面的元素前移了）
            # - 如果退出者就是当前发言者或之后，索引不变
            # - 如果索引超出范围，归零
            if exit_idx < current_idx:
                game["current_speaker_idx"] = current_idx - 1
            elif game["current_speaker_idx"] >= len(game["speaker_order"]):
                game["current_speaker_idx"] = 0
        # 从投票者移除
        game["voters"].discard(user_id)

    # BUG 修复（2026-08-03）：PK 阶段退出未处理 → 候选人挂机/退出永久卡死
    elif game["phase"] in ("pk_speaking", "pk_voting"):
        if user_id in game.get("pk_candidates", []):
            # 候选人退出 → 取消 PK 超时，对手自动安全，进入下一轮
            game["pk_candidates"].remove(user_id)
            _cancel_task_safe(_PK_SPEAK_TIMEOUT_TASKS.pop(group_id, None))
            _cancel_task_safe(_PK_VOTE_TIMEOUT_TASKS.pop(group_id, None))
            del game["players"][user_id]
            count = len(game["players"])
            game["round"] += 1
            survivor_lines = [f"🛡️ PK 候选人 {nickname} 退出了游戏，对手自动安全"]
            result = _check_and_end_game(group_id)
            if result:
                survivor_lines.append(result)
                _end_game(group_id)
                return "\n".join(survivor_lines)
            _start_next_round(group_id)
            survivor_lines.append(f"👥 剩余 {count} 人，准备第 {game['round']} 轮发言...")
            return "\n".join(survivor_lines)
        else:
            # 普通玩家退出 → 从 PK 投票者移除
            game.get("pk_voters", set()).discard(user_id)

    del game["players"][user_id]

    count = len(game["players"])
    if count < _min_players(game["mode"]):
        _end_game(group_id)
        return f"😢 {nickname} 退出了游戏\n⚠️ 剩余 {count} 人，不足 {_min_players(game['mode'])} 人，游戏解散"

    if not game["players"]:
        _end_game(group_id)
        return f"✅ {nickname} 退出游戏，房间已解散"

    return f"😢 {nickname} 退出了游戏\n👥 剩余 {count} 人"


def get_game_status(group_id: int) -> str:
    """获取游戏状态文本"""
    game = _get_game(group_id)
    if not game:
        return "⚠️ 当前没有卧底游戏"

    lines = []
    mode_desc = "白板模式" if game["mode"] == "blank" else "普通模式"
    lines.append(f"🕵️ 卧底游戏 - 第 {game['round']} 轮 [{mode_desc}]")

    if game["phase"] == "lobby":
        lines.append(f"\n👥 玩家（{len(game['players'])} 人）：")
        for pid, p in game["players"].items():
            lines.append(f"  • {p.nickname}")
        lines.append("\n💡 发送 /卧底开始 开始游戏")
    elif game["phase"] == "speaking":
        alive = [p for p in game["players"].values() if p.is_alive()]
        spoken = sum(1 for p in alive if p.has_spoken)
        lines.append(f"\n🗣️ 发言阶段 | 已发言 {spoken}/{len(alive)}")
        elapsed = int(time.time() - game["speak_start_time"])
        remaining = max(0, game["speak_timeout"] - elapsed)
        lines.append(f"⏱️ 剩余 {remaining} 秒")
        if game["speaker_order"]:
            next_idx = game["current_speaker_idx"] % len(game["speaker_order"])
            next_uid = game["speaker_order"][next_idx]
            if next_uid in game["players"] and game["players"][next_uid].is_alive():
                lines.append(f"🎤 下一位：{game['players'][next_uid].nickname}")
    elif game["phase"] == "voting":
        alive = [p for p in game["players"].values() if p.is_alive()]
        voted = len(game["voters"])
        lines.append(f"\n🗳️ 投票阶段 | 已投票 {voted}/{len(alive)}")
        lines.append("💡 格式：发送 /投票 @玩家昵称 或 /投票 玩家昵称（必须指定投票对象）")
    elif game["phase"] == "pk_speaking":
        pk_names = [
            game["players"][uid].nickname for uid in game.get(
                "pk_candidates", []) if uid in game["players"]]
        speeches = len(game.get("pk_speeches", {}))
        lines.append(f"\n🔥 PK 发言阶段 | {speeches}/{len(pk_names)} 人已发言")
        lines.append(f"  PK 候选人：{', '.join(pk_names)}")
    elif game["phase"] == "pk_voting":
        pk_names = [
            game["players"][uid].nickname for uid in game.get(
                "pk_candidates", []) if uid in game["players"]]
        pk_voted = len(game.get("pk_voters", set()))
        eligible = sum(1 for p in game["players"].values()
                       if p.is_alive() and p.user_id not in game.get("pk_candidates", []))
        lines.append(f"\n🗳️ PK 二次投票 | 已投票 {pk_voted}/{eligible}")
        lines.append(f"  请在 {', '.join(pk_names)} 中投票（发送昵称）")
    elif game["phase"] == "ended":
        lines.append("\n🏁 游戏已结束")

    return "\n".join(lines)


def set_speak_timeout(group_id: int, seconds: int) -> str:
    """设置发言时间"""
    game = _get_game(group_id)
    if not game:
        return "⚠️ 当前没有卧底游戏"
    if seconds < 30 or seconds > 600:
        return "⚠️ 发言时间必须在 30-600 秒之间"
    game["speak_timeout"] = seconds
    minutes = seconds // 60
    secs = seconds % 60
    time_str = f"{minutes}分{secs}秒" if minutes else f"{secs}秒"
    return f"⏱️ 发言时间已设置为 {time_str}"


# ============================================================
#  游戏开始 - 分配词语
# ============================================================
def _min_players(mode: str) -> int:
    return 4  # 最小 4 人


# 人数比例配置表（普通模式：仅 civilian + spy）
_ROLE_RATIOS = {
    4: {"civilian": 3, "spy": 1},
    5: {"civilian": 4, "spy": 1},
    6: {"civilian": 5, "spy": 1},
    7: {"civilian": 6, "spy": 1},
    8: {"civilian": 6, "spy": 2},
    9: {"civilian": 7, "spy": 2},
    10: {"civilian": 8, "spy": 2},
}

# 白板模式人数比例配置表（civilian + spy + blank）
_ROLE_RATIOS_BLANK = {
    4: {"civilian": 2, "spy": 1, "blank": 1},  # 白板模式 4 人：2 平民 + 1 卧底 + 1 白板
    5: {"civilian": 3, "spy": 1, "blank": 1},
    6: {"civilian": 4, "spy": 1, "blank": 1},
    7: {"civilian": 5, "spy": 1, "blank": 1},
    8: {"civilian": 5, "spy": 2, "blank": 1},
    9: {"civilian": 6, "spy": 2, "blank": 1},
    10: {"civilian": 6, "spy": 2, "blank": 2},
}


def start_game(group_id: int, user_id: int = 0) -> tuple[bool, str]:
    """
    开始游戏：分配词语 + 公布发言顺序。
    返回 (success, message_for_group)
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "lobby":
        return False, f"⚠️ 游戏不在准备阶段（当前：{phase_map(game.get('phase', 'idle'))}）"

    # 取消旧定时器
    old_task = _SPEAK_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(old_task)

    players = list(game["players"].values())
    n = len(players)

    if n < 4:
        return False, "⚠️ 至少需要 4 人才能开始"

    if n > 10:
        return False, f"⚠️ 人数过多（{n} 人），最多支持 10 人"

    # 抽取词语 — 排除已使用的词语对
    if not _WORD_BANK:
        return False, "😅 题库暂无可用词语"

    # 获取已使用的词语对
    used_words = _get_used_words()

    # 筛选未使用的词语
    available = [entry for entry in _WORD_BANK
                 if (entry[0], entry[1]) not in used_words]

    # 如果所有词语都用过，清空历史记录
    if not available:
        cleared = _clear_used_words()
        available = list(_WORD_BANK)
        reset_hint = f"（已清空 {cleared} 条历史记录）"
    else:
        reset_hint = ""

    word_entry = random.choice(available)
    word_civilian, word_spy = word_entry[0], word_entry[1]

    # 词语互换：50% 概率交换平民词和卧底词
    if random.random() < 0.5:
        word_civilian, word_spy = word_spy, word_civilian

    # 白板词：如果题库条目有第三个词则使用，否则空
    word_blank = word_entry[2] if len(word_entry) > 2 else ""

    # 记录到历史数据库
    _record_used_word(word_civilian, word_spy, word_blank)

    # 分配角色
    _assign_roles(players, game["mode"], word_civilian, word_spy, word_blank)

    # 随机打乱发言顺序
    speaker_ids = [p.user_id for p in players]
    random.shuffle(speaker_ids)

    game["word_civilian"] = word_civilian
    game["word_spy"] = word_spy
    game["word_blank"] = word_blank
    game["speaker_order"] = speaker_ids
    game["current_speaker_idx"] = 0
    game["round"] = game.get("round", 1)
    game["phase"] = "speaking"
    game["voters"] = set()
    game["speak_start_time"] = time.time()

    # 重置 PK 状态
    game["pk_candidates"] = []
    game["pk_speeches"] = {}
    game["pk_voters"] = set()

    # 重置玩家状态
    for p in players:
        p.has_spoken = False
        p.description = ""
        p.vote_count = 0
        p.pk_vote_count = 0
        p.alive = True

    # 构建群内消息
    # 计算角色数量
    if game["mode"] == "blank":
        ratio = _ROLE_RATIOS_BLANK.get(n, {"civilian": n - 2, "spy": 1, "blank": 1})
        role_counts = [
            f"平民 {ratio['civilian']} 人",
            f"卧底 {ratio['spy']} 人",
            f"白板 {ratio['blank']} 人",
        ]
    else:
        ratio = _ROLE_RATIOS.get(n, {"civilian": n - 1, "spy": 1})
        role_counts = [
            f"平民 {ratio['civilian']} 人",
            f"卧底 {ratio['spy']} 人",
        ]

    # 获胜条件
    if game["mode"] == "blank":
        win_conditions = (
            "🏆 获胜条件：\n"
            "  • 平民+卧底：投票淘汰所有对方阵营成员\n"
            "  • 白板：存活到最后即获胜\n"
            "  • 任一角色全部出局，其余方胜利"
        )
    else:
        win_conditions = (
            "🏆 获胜条件：\n"
            "  • 平民胜利：投票淘汰所有卧底\n"
            "  • 卧底胜利：卧底人数 ≥ 平民人数"
        )

    lines = [
        f"🕵️ 【谁是卧底】第 {game['round']} 轮 正式开始！",
        f"👥 共 {n} 人参与",
        f"📋 游戏模式：{'平民 + 卧底 + 白板' if game['mode'] == 'blank' else '平民 + 卧底'}",
        f"🎭 角色分布：{'，'.join(role_counts)}",
        f"⏱️ 发言限时：{game['speak_timeout']} 秒",
        "",
        "📜 游戏规则：",
        "  ① 按顺序发言描述你的词语（私聊已发）",
        "  ② 发言中⚠️ 不准出现词语中的任何字 ⚠️，否则直接淘汰",
        "  ③ 发言需描述词语特征，由 LLM 异步审查是否符合",
        "  ④ 所有人发言完毕后进入投票环节（投票淘汰可疑者）",
        "  ⑤ 投票平票则进入 PK 发言→二次投票",
        "",
        win_conditions,
        "",
        "📢 发言顺序：",
    ]

    for i, uid in enumerate(speaker_ids, 1):
        p = game["players"][uid]
        lines.append(f"  {i}. {p.nickname}")

    lines.append("")
    lines.append("💡 请按照顺序发言描述你的词语！")
    lines.append("⚠️ 发言中不能包含词语中的任何一个字，否则直接出局！")
    lines.append("⏰ 所有人发完或超时后自动进入投票环节")

    return True, "\n".join(lines)


def _assign_roles(
    players: list[Player],
    mode: str,
    word_civilian: str,
    word_spy: str,
    word_blank: str):
    """
    根据人数比例表分配角色和词语。

    人数比例规则：
        4 人: 平民3 : 卧底1（普通）/ 平民2 : 卧底1 : 白板1（白板）
        5 人: 平民3 : 卧底1 : 白板1
        6 人: 平民4 : 卧底1 : 白板1
        7 人: 平民5 : 卧底1 : 白板1
        8 人: 平民5 : 卧底2 : 白板1
        9 人: 平民6 : 卧底2 : 白板1
    10 人: 平民6 : 卧底2 : 白板2

    白板兼容：任意一对词语都可玩白板模式（白板词为空时，白板玩家无词）
    词语互换：在 start_game 中已随机交换平民/卧底词
    """
    n = len(players)

    # 获取该人数的角色比例（普通/白板分开取表）
    if mode == "blank":
        ratio = _ROLE_RATIOS_BLANK.get(n, {"civilian": n - 2, "spy": 1, "blank": 1})
    else:
        ratio = _ROLE_RATIOS.get(n, {"civilian": n - 1, "spy": 1})

    # 普通模式：只分配平民和卧底
    if mode == "normal":
        num_civilians = ratio.get("civilian", n - 1)
        num_spies = ratio.get("spy", 1)
        roles = ["spy"] * num_spies + ["civilian"] * num_civilians
    else:
        # 白板模式
        # 问题 9 修复：4 人白板模式支持白板（2 平民 + 1 卧底 + 1 白板）
        if n == 4:
            roles = ["spy", "blank", "civilian", "civilian"]
        else:
            num_civilians = ratio.get("civilian", n - 2)
            num_spies = ratio.get("spy", 1)
            num_blanks = ratio.get("blank", 1)
            roles = ["spy"] * num_spies + ["blank"] * num_blanks + ["civilian"] * num_civilians

    # 确保角色数等于玩家数
    while len(roles) < n:
        roles.append("civilian")
    roles = roles[:n]

    random.shuffle(roles)

    for i, p in enumerate(players):
        p.role = roles[i]
        if roles[i] == "civilian":
            p.word = word_civilian
        elif roles[i] == "spy":
            p.word = word_spy
        else:
            # 白板：如果有独立白板词则使用，否则无词
            p.word = word_blank if word_blank else ""
        p.word_chars = set(p.word)


def send_words(group_id: int, websocket, message_type: str):
    """
    通过私聊向每个玩家发送词语。
    需要 websocket 引用。
    """
    game = _get_game(group_id)
    if not game:
        return

    for p in game["players"].values():
        if not p.is_alive():
            continue

        # 构建私聊消息（不告知身份，只发词语）
        if p.role == "blank":
            word_text = "（你的词是空白，请自由发挥！）"
        else:
            word_text = p.word

        msg = (
            f"🕵️ 【谁是卧底】\n\n"
            f"📝 你的词语：{word_text}\n\n"
            f"⚠️ 发言时描述你的词语，但绝对不能说出词语中的任何一个字！\n"
            f"（此消息仅供参考，游戏进行中以群内消息为准）"
        )

        _send_private_msg(websocket, p.user_id, msg)


def _send_private_msg(websocket, user_id: int, text: str):
    """发送私聊消息"""
    import json
    from core.content_filter import censor_text
    text = censor_text(text)
    try:
        # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
        # send_segments 是 async 且被门控拦截时静默返回 None，故用 create_task 投递
        from core.sender import send_segments
        asyncio.create_task(
            send_segments(websocket, "private", user_id,
                          [{"type": "text", "data": {"text": text}}],
                          echo=f"spy_private_{user_id}_{int(time.time())}")
        )
    except Exception as e:
        logger.error(f"私聊发送失败 ({user_id}): {e}")


def _get_websocket(group_id: int):
    """获取群游戏的 websocket 引用（从存储中获取）"""
    return _WEBSOCKETS.get(group_id)


async def _send_group_message(group_id: int, text: str):
    """发送群消息（供定时器回调等异步上下文使用）"""
    from core.content_filter import censor_text
    text = censor_text(text)
    ws = _get_websocket(group_id)
    if not ws:
        logger.warning(f"群消息发送失败: 未找到 websocket ({group_id})")
        return
    import json
    try:
        # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
        from core.sender import send_segments
        await send_segments(ws, "group", group_id,
                            [{"type": "text", "data": {"text": text}}],
                            echo=f"spy_group_{int(time.time())}")
    except Exception as e:
        logger.error(f"群消息发送失败 ({group_id}): {e}")


def start_speak_timer(
    group_id: int,
    websocket=None,
    message_type: str = "group",
    user_id: int = 0,
    reply_id: int = 0):
    """
    启动发言倒计时定时器。
    在 start_game 成功后调用，或由 _start_next_round 在每轮发言开始时调用。

    BUG 修复（2026-08-03 二次审查）：原实现只在第一轮 start_game 后调用，
    从第二轮起（_start_next_round）没有重启发言定时器 → 玩家挂机时游戏永久
    卡死在 speaking 阶段。现改为 websocket 可选：_start_next_round 场景
    从 _WEBSOCKETS 缓存获取连接（定时器回调走 _send_group_message 也不依赖
    传入的 websocket）。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "speaking":
        return

    # 缓存 websocket 引用（供 _send_group_message 等异步回调使用）
    # BUG 修复（2026-08-03）：原实现"仅在缺失时写入"导致 NapCat 重连后缓存
    # 永久保留旧连接，定时器公告全部发到死连接。改为传入新连接时更新缓存。
    if websocket is not None:
        _WEBSOCKETS[group_id] = websocket

    # 取消旧定时器
    old_task = _SPEAK_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(old_task)

    timeout = game.get("speak_timeout", DEFAULT_SPEAK_TIMEOUT)

    async def _timeout_handler():
        await asyncio.sleep(timeout)
        # 检查游戏是否还在发言阶段
        g = _get_game(group_id)
        if not g or g.get("phase") != "speaking":
            return

        voting_msg = handle_speak_timeout(group_id)
        if voting_msg:
            # 问题 3 修复：从全局字典获取最新 websocket，避免闭包捕获过期连接
            await _send_group_message(group_id, voting_msg)

    task = asyncio.create_task(_timeout_handler())
    _SPEAK_TIMEOUT_TASKS[group_id] = task
    logger.info(f"⏱️ 卧底发言定时器已启动，{timeout}秒后触发")


# ============================================================
#  发言阶段 - 消息处理
# ============================================================
def handle_speaking_message(
    group_id: int,
    user_id: int,
    text: str) -> Optional[str]:
    """
    处理发言阶段的玩家消息（同步版本，仅做字符违规判定）。
    返回群内回复文本，None 表示无需回复。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "speaking":
        return None

    if user_id not in game["players"]:
        return None  # 非玩家消息，忽略

    player = game["players"].get(user_id)
    if not player:
        return None

    # BUG 3 修复：强制发言顺序 - 只有当前轮到发言的玩家才能发言
    # 问题 1 修复：跳过出局玩家时加迭代上限，防止死循环
    order = game["speaker_order"]
    n = len(order)
    if n == 0:
        return None
    next_idx = game["current_speaker_idx"] % n
    next_uid = order[next_idx]

    if user_id != next_uid:
        if player.has_spoken:
            return f"ℹ️ {player.nickname} 已经发言过了"
        next_player = game["players"].get(next_uid)
        # 问题 1 修复：下一位存活且**未发言**才拦截，已发言的玩家需要跳过
        if next_player and next_player.is_alive() and not next_player.has_spoken:
            return f"🎤 请 {next_player.nickname} 发言"
        # 下一位已出局或已发言，跳过到下一位存活且未发言的玩家
        # 问题 1 修复：限制最多 n 次迭代 + 同时检查 is_alive 和 has_spoken
        for _ in range(n):
            if (next_uid in game["players"]
                    and game["players"][next_uid].is_alive()
                    and not game["players"][next_uid].has_spoken):
                break
            game["current_speaker_idx"] += 1
            next_idx = game["current_speaker_idx"] % n
            next_uid = order[next_idx]
        else:
            # 所有玩家均已出局或已发言，直接返回
            return "⚠️ 本轮所有玩家均已出局或已发言，等待游戏结束..."
        # Bug #2 修复：校准 current_speaker_idx 后，检查当前发消息者是否就是目标
        game["current_speaker_idx"] = next_idx
        if user_id == next_uid:
            # 当前发消息者正好是校准后的发言者，继续下方处理其发言
            pass
        else:
            next_player = game["players"].get(next_uid)
            if next_player:
                return f"🎤 请 {next_player.nickname} 发言"

    # 已发言 / 不在游戏
    if player.has_spoken:
        return f"ℹ️ {player.nickname} 已经发言过了"

    if not player.is_alive():
        return None

    forbidden_found = []
    for char in text:
        if char in player.word_chars:
            forbidden_found.append(char)

    if forbidden_found:
        # 违规出局
        player.alive = False
        player.has_spoken = True
        unique_forbidden = "".join(sorted(set(forbidden_found)))

        msg = (
            f"💥 {player.nickname} 发言违规！\n"
            f"📝 发言内容：{text}\n"
            f"🚫 包含词语中的字：{unique_forbidden}\n"
            f"👋 直接出局！"
        )

        # 检查是否还有存活玩家 / 是否触发胜利
        result = _check_and_end_game(group_id)
        if result:
            return msg + "\n\n" + result

        # 检查是否所有人都已发言（违规出局也算发过了）
        alive_players = [p for p in game["players"].values() if p.is_alive()]
        all_spoken = all(p.has_spoken for p in alive_players)
        if all_spoken:
            voting_msg = _transition_to_voting(group_id)
            if voting_msg:
                return msg + "\n\n" + voting_msg

        return msg

    # 记录发言
    player.has_spoken = True
    player.description = text

    # 检查是否所有人都已发言
    alive_players = [p for p in game["players"].values() if p.is_alive()]
    all_spoken = all(p.has_spoken for p in alive_players)

    if all_spoken:
        # 所有存活玩家都已发言，进入投票
        voting_msg = _transition_to_voting(group_id)
        if voting_msg:
            return voting_msg
        return None

    # 提示下一位
    next_player = _get_next_speaker(group_id)
    if next_player:
        return f"👉 下一位请发言：{next_player.nickname}"

    return None


async def handle_speaking_message_async(
    group_id: int,
    user_id: int,
    text: str) -> Optional[str]:
    """
    处理发言阶段的玩家消息（异步版本，含 LLM 判定）。
    返回群内回复文本，None 表示无需回复。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "speaking":
        return None

    if user_id not in game["players"]:
        return None  # 非玩家消息，忽略

    player = game["players"].get(user_id)
    if not player:
        return None

    # BUG 3 修复：强制发言顺序 - 只有当前轮到发言的玩家才能发言
    # 问题 1 修复：跳过出局玩家时加迭代上限，防止死循环
    order = game["speaker_order"]
    n = len(order)
    if n == 0:
        return None
    next_idx = game["current_speaker_idx"] % n
    next_uid = order[next_idx]

    if user_id != next_uid:
        if player.has_spoken:
            return f"ℹ️ {player.nickname} 已经发言过了"
        next_player = game["players"].get(next_uid)
        # 问题 1 修复：下一位存活且**未发言**才拦截，已发言的玩家需要跳过
        if next_player and next_player.is_alive() and not next_player.has_spoken:
            return f"🎤 请 {next_player.nickname} 发言"
        # 下一位已出局或已发言，跳过到下一位存活且未发言的玩家
        # 问题 1 修复：限制最多 n 次迭代 + 同时检查 is_alive 和 has_spoken
        for _ in range(n):
            if (next_uid in game["players"]
                    and game["players"][next_uid].is_alive()
                    and not game["players"][next_uid].has_spoken):
                break
            game["current_speaker_idx"] += 1
            next_idx = game["current_speaker_idx"] % n
            next_uid = order[next_idx]
        else:
            # 所有玩家均已出局或已发言，直接返回
            return "⚠️ 本轮所有玩家均已出局或已发言，等待游戏结束..."
        # Bug #2 修复：校准 current_speaker_idx 后，检查当前发消息者是否就是目标
        game["current_speaker_idx"] = next_idx
        if user_id == next_uid:
            # 当前发消息者正好是校准后的发言者，继续下方处理其发言
            pass
        else:
            next_player = game["players"].get(next_uid)
            if next_player:
                return f"🎤 请 {next_player.nickname} 发言"

    # 已发言 / 不在游戏
    if player.has_spoken:
        return f"ℹ️ {player.nickname} 已经发言过了"

    if not player.is_alive():
        return None

    forbidden_found = []
    for char in text:
        if char in player.word_chars:
            forbidden_found.append(char)

    if forbidden_found:
        # 违规出局
        player.alive = False
        player.has_spoken = True
        unique_forbidden = "".join(sorted(set(forbidden_found)))

        msg = (
            f"💥 {player.nickname} 发言违规！\n"
            f"📝 发言内容：{text}\n"
            f"🚫 包含词语中的字：{unique_forbidden}\n"
            f"👋 直接出局！"
        )

        result = _check_and_end_game(group_id)
        if result:
            return msg + "\n\n" + result

        alive_players = [p for p in game["players"].values() if p.is_alive()]
        all_spoken = all(p.has_spoken for p in alive_players)
        if all_spoken:
            voting_msg = _transition_to_voting(group_id)
            if voting_msg:
                return msg + "\n\n" + voting_msg

        return msg

    # ---- 记录发言 + 启动异步 LLM 审查（不阻塞流程） ----
    player.has_spoken = True
    player.description = text

    # 后台异步 LLM 审查
    # 问题 1 修复：传入 game_id + round，防止跨局/跨轮回调误杀
    current_round = game["round"]
    current_game_id = game["game_id"]
    from core.llm import llm_enabled
    if _ENABLE_LLM_JUDGE and llm_enabled():
        if player.word:
            task = asyncio.create_task(
                _async_llm_judge_speech(group_id, user_id, player.word, text, current_game_id, current_round))
        else:
            # 白板玩家
            task = asyncio.create_task(
                _async_llm_judge_blank_speech(group_id, user_id, text, current_game_id, current_round))
        tasks = _LLM_JUDGE_TASKS.setdefault(group_id, [])
        tasks.append(task)
        # 自动清理已完成的任务
        _cleanup_finished_tasks(group_id)

    return _post_speech(group_id)


def _cleanup_finished_tasks(group_id: int):
    """清理已完成的 LLM 判定任务，避免内存泄漏"""
    tasks = _LLM_JUDGE_TASKS.get(group_id)
    if tasks:
        _LLM_JUDGE_TASKS[group_id] = [t for t in tasks if not t.done()]
        if not _LLM_JUDGE_TASKS[group_id]:
            del _LLM_JUDGE_TASKS[group_id]


def _schedule_game_cleanup(group_id: int, game_id: int):
    """
    问题 7 修复：异步延迟清理游戏数据，替代 threading.Thread + time.sleep。
    兼容同步/异步调用场景。

    Bug #1 修复：传入 game_id，清理时校验 game_id 是否匹配，
    避免 60 秒内新建游戏被误删。
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop:
        async def delayed_cleanup():
            await asyncio.sleep(60)
            game = _SPY_GAMES.get(group_id)
            if game and game.get("game_id") == game_id:
                _SPY_GAMES.pop(group_id, None)
        asyncio.create_task(delayed_cleanup())
    else:
        # 降级方案：没有运行中的事件循环时使用线程
        import threading
        def delayed_cleanup():
            time.sleep(60)
            game = _SPY_GAMES.get(group_id)
            if game and game.get("game_id") == game_id:
                _SPY_GAMES.pop(group_id, None)
        threading.Thread(target=delayed_cleanup, daemon=True).start()


def _cancel_llm_tasks(group_id: int):
    """取消所有待处理的 LLM 判定任务"""
    tasks = _LLM_JUDGE_TASKS.pop(group_id, [])
    for t in tasks:
        if not t.done():
            t.cancel()


def _post_speech(group_id: int) -> Optional[str]:
    """
    发言后通用逻辑：检查是否全部发言完毕 → 投票 / 提示下一位。
    """
    game = _get_game(group_id)
    if not game:
        return None

    alive_players = [p for p in game["players"].values() if p.is_alive()]
    all_spoken = all(p.has_spoken for p in alive_players)

    if all_spoken:
        voting_msg = _transition_to_voting(group_id)
        if voting_msg:
            return voting_msg
        return None

    next_player = _get_next_speaker(group_id)
    if next_player:
        return f"👉 下一位请发言：{next_player.nickname}"

    return None


async def _judge_description_with_llm(word: str, description: str) -> dict:
    """
    调用 LLM 判定描述是否符合词语。
    返回 {"valid": bool, "reason": str}
    v2：走统一 LLM 后端（core.config）；llm.enabled 关闭时默认通过。
    """
    import httpx
    from core.llm import llm_enabled, _resolve_llm_backend, _get_config

    if not llm_enabled():
        return {"valid": True, "reason": "LLM 总开关关闭，跳过判定（默认通过）"}
    api_url, model, headers, _cap = _resolve_llm_backend(_get_config())

    system_prompt = (
        "你是「谁是卧底」游戏的裁判。判断玩家描述是否合理指向目标词语。\n"
        "规则：\n"
        "1. 描述是否与词语有关联？（特征、用途、场景、属性等）\n"
        "2. 描述是否过于泛泛而谈？（如'很好''很大'等无信息量的描述）\n"
        "3. 描述是否完全偏离词语？\n"
        "4. 白板玩家描述需要有具体内容和可讨论性\n\n"
        "严格按 JSON 格式回复，不要包含其他文字：\n"
        '{"valid": true/false, "reason": "一句话理由"}'
    )

    user_prompt = f"词语：{word}\n描述：{description}\n判断："

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT, trust_env=False) as client:
            resp = await client.post(
                f"{api_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 8192,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()

            # 推理模型：content 可能为空，从 reasoning_content 中提取
            if not content:
                reasoning = (msg.get("reasoning_content") or "").strip()
                # 从 reasoning_content 末尾提取 JSON
                json_match = re.search(
                    r'\{.*"valid"\s*:\s*(true|false).*"reason"\s*:\s*"([^"]*)".*\}',
                    reasoning,
                    re.DOTALL)
                if json_match:
                    valid_str = json_match.group(1).lower()
                    reason = json_match.group(2)
                    return {"valid": valid_str == "true", "reason": reason}
                logger.warning(f"LLM reasoning_content 解析失败: {reasoning[:100]}")
                return {"valid": True, "reason": "判定不明确，默认通过"}

        # 尝试解析 JSON
        json_match = re.search(
            r'\{.*"valid"\s*:\s*(true|false).*"reason"\s*:\s*"([^"]*)".*\}', content)
        if json_match:
            valid_str = json_match.group(1).lower()
            reason = json_match.group(2)
            return {"valid": valid_str == "true", "reason": reason}

        # 解析失败，默认通过
        logger.warning(f"LLM 判定解析失败: {content[:100]}")
        return {"valid": True, "reason": "判定不明确，默认通过"}

    except httpx.TimeoutException:
        logger.warning(f"LLM 判定超时 ({_LLM_TIMEOUT}s)")
        return {"valid": True, "reason": f"判定超时（{_LLM_TIMEOUT}s），默认通过"}
    except Exception as e:
        logger.error(f"LLM 判定异常: {e}")
        return {"valid": True, "reason": f"判定异常（{str(e)[:30]}），默认通过"}


async def _judge_blank_description_with_llm(
        group_id: int, description: str) -> bool:
    """
    白板发言 LLM 质量判定。
    检查发言是否有足够信息量（非废话、非单字、非纯表情）。
    """
    # 问题 1 修复：移除 _llm_api_key() 调用（函数未定义），改用长度和规则判断
    if len(description) < 2 or len(description) > 200:
        return len(description) >= 3

    # 极简规则：单字、纯表情、纯数字、常见废话直接拒绝
    import re
    if len(description.strip()) < 3:
        return False
    if re.match(r'^[\d\s\.,。]+$', description):
        return False
    if re.match(r'^[\U0001F300-\U0001F9FF\s]+$', description):
        return False
    trivial = ["随便说", "不知道", "没想法", "嗯", "啊", "哦", "好的", "行", "可以", "不知道说什么"]
    if description.strip() in trivial:
        return False

    # 问题 10 修复：使用专门的白板判定 prompt，不传空词
    system_prompt = (
        "你是「谁是卧底」游戏的裁判。请判断白板的发言是否有足够的信息量。\n"
        "判断标准（满足任一即可通过）：\n"
        "1. 包含具体名词或概念\n"
        "2. 有描述性内容\n"
        "3. 有场景暗示或可讨论的信息\n"
        "只要不是纯废话、纯表情、单字回复即可通过。\n"
        "请尽量宽容，白板没有词语，发言难度本来就高。\n\n"
        "严格按 JSON 格式回复：\n"
        '{"valid": true/false, "reason": "一句话理由"}'
    )

    user_prompt = f"请判断以下白板发言是否有足够的信息量：\n「{description}」\n\n返回 JSON。"

    # 缺陷 5 修复：使用统一 LLM 后端（v2：core.config，含 llm.enabled 总开关）
    from core.llm import llm_enabled, _resolve_llm_backend, _get_config
    if not llm_enabled():
        return True  # LLM 总开关关闭 → 跳过判定，默认通过
    api_url, model, headers, _cap = _resolve_llm_backend(_get_config())
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT, trust_env=False) as client:
            response = await client.post(
                f"{api_url}/chat/completions",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                },
            )
            resp_content = response.json()["choices"][0]["message"]["content"]

            # 兼容 reasoning_content 字段
            if not resp_content and "reasoning_content" in response.json()[
                "choices"][0]["message"]:
                resp_content = response.json()["choices"][0]["message"]["reasoning_content"]

            json_match = re.search(
                r'\{.*"valid"\s*:\s*(true|false).*"reason"\s*:\s*"([^"]*)".*\}',
                resp_content)
        if json_match:
            valid_str = json_match.group(1).lower()
            return valid_str == "true"

        logger.warning(f"白板 LLM 判定解析失败: {resp_content[:100]}")
        return True  # 解析失败默认通过
    except Exception as e:
        logger.error(f"白板 LLM 判定异常: {e}")
    return True  # 异常默认通过


async def _async_llm_judge_speech(
    group_id: int, user_id: int, word: str, description: str,
    saved_game_id: str, saved_round: int):
    """
    异步后台 LLM 判定发言是否符合词语。
    不阻塞正常游戏流程 — 判定不通过时再发送出局消息。

    问题 1 修复：使用 saved_game_id 防止跨局误杀（新局 round=1 但 game_id 不同）。
    问题 2 修复：仅在 speaking 阶段执行出局操作，voting 阶段跳过以避免污染投票池。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") not in ("speaking", "voting"):
        return

    # 问题 1 修复：LLM 判定跨局回调时，检查 game_id 是否仍然匹配
    if game.get("game_id") != saved_game_id:
        logger.info(f"⏭️ LLM 判定已过期（旧局 {saved_game_id}，当前 {game.get('game_id')}），跳过")
        return

    # 问题 2 修复：voting 阶段不再执行出局，避免污染投票池
    if game.get("phase") != "speaking":
        logger.info("⏭️ LLM 判定到达时已进入投票阶段，跳过出局操作")
        return

    player = game["players"].get(user_id)
    if not player or not player.is_alive():
        return

    llm_result = await _judge_description_with_llm(word, description)
    if llm_result["valid"]:
        return  # 通过，无需通知

    # 判定不通过 — 玩家出局（再次检查 game_id，防止竞态）
    if game.get("game_id") != saved_game_id:
        return
    player.alive = False
    role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
    role_label = role_display.get(player.role, player.role)

    msg = (
        f"\n🤖【LLM 审查结果】\n"
        f"💥 {player.nickname} 发言与词语不符！\n"
        f"📝 发言内容：{description}\n"
        f"🔍 判定理由：{llm_result['reason']}\n"
        f"👋 直接出局！"
    )

    await _send_group_message(group_id, msg)

    # 检查游戏是否结束
    result = _check_and_end_game(group_id)
    if result:
        await _send_group_message(group_id, result)


async def _async_llm_judge_blank_speech(
    group_id: int, user_id: int, description: str,
    saved_game_id: str, saved_round: int):
    """
    异步后台 LLM 判定白板发言质量。

    问题 1 修复：使用 saved_game_id 防止跨局误杀。
    问题 2 修复：仅在 speaking 阶段执行出局操作，voting 阶段跳过以避免污染投票池。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") not in ("speaking", "voting"):
        return

    # 问题 1 修复：LLM 判定跨局回调时，检查 game_id 是否仍然匹配
    if game.get("game_id") != saved_game_id:
        logger.info(f"⏭️ 白板 LLM 判定已过期（旧局 {saved_game_id}，当前 {game.get('game_id')}），跳过")
        return

    # 问题 2 修复：voting 阶段不再执行出局，避免污染投票池
    if game.get("phase") != "speaking":
        logger.info("⏭️ 白板 LLM 判定到达时已进入投票阶段，跳过出局操作")
        return

    player = game["players"].get(user_id)
    if not player or not player.is_alive():
        return

    blank_quality = await _judge_blank_description_with_llm(group_id, description)
    if blank_quality:
        return  # 通过

    # 质量不达标 — 出局（再次检查 game_id，防止竞态）
    if game.get("game_id") != saved_game_id:
        return
    player.alive = False
    role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
    role_label = role_display.get(player.role, player.role)

    msg = (
        f"\n🤖【LLM 审查结果】\n"
        f"💥 {player.nickname} 白板发言信息量不足！\n"
        f"📝 发言内容：{description}\n"
        f"👋 直接出局！"
    )

    await _send_group_message(group_id, msg)

    result = _check_and_end_game(group_id)
    if result:
        await _send_group_message(group_id, result)


def _get_next_speaker(group_id: int) -> Optional[Player]:
    """获取下一个需要发言的存活玩家"""
    game = _get_game(group_id)
    if not game:
        return None

    order = game["speaker_order"]
    n = len(order)
    if n == 0:
        return None

    # 从当前位置开始查找
    for _ in range(n):
        idx = game["current_speaker_idx"] % n
        uid = order[idx]
        if uid in game["players"] and game["players"][uid].is_alive(
        ) and not game["players"][uid].has_spoken:
            # Bug #1 修复：指向当前找到的玩家，而不是他的下一个
            game["current_speaker_idx"] = idx
            return game["players"][uid]
        game["current_speaker_idx"] = (idx + 1) % n

    return None


def handle_speak_timeout(group_id: int) -> Optional[str]:
    """
    发言时间到，进入投票阶段。
    由定时器调用。
    返回投票公告消息。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "speaking":
        return None

    # 标记未发言的存活玩家为跳过了
    for p in game["players"].values():
        if p.is_alive() and not p.has_spoken:
            p.has_spoken = True
            p.description = "（超时未发言）"

    return _transition_to_voting(group_id)


# ============================================================
#  投票阶段
# ============================================================
def _transition_to_voting(group_id: int) -> Optional[str]:
    """从发言阶段过渡到投票阶段"""
    game = _get_game(group_id)
    if not game:
        return None

    # Bug #3 修复：防止重复触发（定时器 + 消息同时触发）
    if game["phase"] == "voting":
        return None

    # 取消发言定时器
    task = _SPEAK_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(task)

    game["phase"] = "voting"
    game["voters"] = set()

    # 重置投票计数
    for p in game["players"].values():
        p.vote_count = 0
        p.pk_vote_count = 0

    # 构建投票公告
    alive = [p for p in game["players"].values() if p.is_alive()]
    spoken = [p for p in alive if p.description]

    lines = [
        f"🗳️ 第 {game['round']} 轮发言结束！进入投票环节！",
        "",
        "📋 发言回顾：",
    ]

    for p in spoken:
        # 问题 7 修复：投票环节不泄露身份
        lines.append(f"  • {p.nickname}：{p.description}")

    lines.append("")
    lines.append(f"👥 存活 {len(alive)} 人，请投票找出卧底！")
    # BUG #4 修复：提示投票格式，列出可投票玩家
    voter_nicks = [p.nickname for p in alive]
    lines.append(f"💡 投票格式: @Bot 玩家昵称（例: @Bot {voter_nicks[0]}）")
    lines.append(f"   可投票给: {', '.join(voter_nicks)}")

    # 问题 4 修复：启动投票倒计时
    _start_vote_timeout(group_id)

    return "\n".join(lines)


def handle_vote(group_id: int, user_id: int, text: str = "") -> Optional[str]:
    """
    处理投票。
    格式: @Bot @目标玩家 或 @Bot 玩家昵称
    要求：必须明确指定投票对象（@提及或昵称），不再自动兜底给上一位发言者。
    返回群内回复文本，None 表示无需回复。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "voting":
        return None

    if user_id not in game["players"]:
        return None

    player = game["players"][user_id]
    if not player.is_alive():
        return None

    if user_id in game["voters"]:
        return f"ℹ️ {player.nickname} 已经投过票了"

    # 解析投票目标：必须 @用户或输入昵称，不兜底
    target = _resolve_vote_target(group_id, user_id, text)
    if not target:
        # 列出可选对象（允许自投）
        alive_others = [p for p in game["players"].values()
                        if p.is_alive()]
        names = ", ".join(p.nickname for p in alive_others)
        return f"ℹ️ 请投票给以下玩家之一: {names}（可以投自己）"

    # 记录投票
    game["voters"].add(user_id)
    target.vote_count += 1

    # 新规则：某玩家票数超过在场人数的半数，直接出局
    alive = [p for p in game["players"].values() if p.is_alive()]
    needed_votes = len(alive)
    half_votes = needed_votes // 2  # 超过半数（如 5 人需 3 票，4 人需 3 票）

    if target.vote_count > half_votes:
        # 票数过半，直接结算出局
        return _resolve_votes(group_id)

    # 检查是否所有人都投完了
    if len(game["voters"]) >= needed_votes:
        return _resolve_votes(group_id)

    remaining = needed_votes - len(game["voters"])
    return f"✅ {player.nickname} 投票给 {target.nickname} | 剩余 {remaining} 人未投"


def _find_last_speaker(group_id: int, voter_id: int) -> Optional[Player]:
    """
    找到投票者的上一位发言者（最近的、存活的、非自己的玩家）。
    """
    game = _get_game(group_id)
    if not game:
        return None

    # 按照发言顺序，投票者在顺序中的前一个
    order = game["speaker_order"]
    if voter_id in order:
        voter_idx = order.index(voter_id)
        # 向前找（发言顺序的反向）
        for i in range(1, len(order)):
            prev_idx = (voter_idx - i) % len(order)
            uid = order[prev_idx]
            if uid in game["players"] and game["players"][uid].is_alive():
                return game["players"][uid]

    # 兜底：找最后一个发言的存活玩家
    for p in game["players"].values():
        if p.is_alive() and p.user_id != voter_id:
            return p

    return None


def _is_negated(text: str, nick: str) -> bool:
    """
    检查昵称是否出现在否定语境中（否定词在昵称之前）。

    问题 2 修复：放弃 \\w 边界匹配（Python3 中 \\w 包含中文），
    改用字符串索引判断——查找昵称位置，检查其前面是否有否定词。

    Bug #4 修复：支持否定词 + 助词（了/一下/个/一放等）后再接昵称，
    如"保一下小明"、"救一下小红"、"放了小明"等不再误判。

    Examples:
        "我保小明" → True (保 = 保护/不投)
        "我保一下小明" → True (保 + 一下)
        "别投小明" → True
        "放了小明" → True (放 + 了)
        "我投小明" → False
        "除了小明都行" → True
    """
    idx = text.find(nick)
    if idx <= 0:
        return False
    # 取昵称前面最多 10 个字符作为上下文（覆盖长否定词 + 助词组合）
    prefix = text[max(0, idx - 10):idx]
    # 否定词列表（长词在前，避免"不"匹配"不投"的前缀）
    neg_words = [
        "不投", "别投", "不要投", "没投",
        "不要", "不是", "并非", "除了", "放过",
        "别", "没", "除", "保", "救", "放",
    ]
    # 固定搭配（常见否定语境短语）
    fixed_patterns = ["保一下", "救一下", "放一下", "留一下"]

    # 策略 1：prefix 以否定词结尾（紧邻昵称）
    if any(prefix.endswith(w) for w in neg_words):
        return True
    # 策略 2：prefix 以否定词 + 助词结尾（如"保了"、"保一下"、"放了"）
    helper_suffixes = ["了", "一下", "一放", "一个", "一留"]
    for w in neg_words:
        for h in helper_suffixes:
            if prefix.endswith(w + h):
                return True
    # 策略 3：prefix 以固定搭配结尾
    if any(prefix.endswith(p) for p in fixed_patterns):
        return True
    # 策略 4（兜底）：否定词出现在 prefix 中间（如"保一放下小明"）
    for w in neg_words:
        if w in prefix and len(w) >= 1:
            # 确认否定词不在昵称之前很远（排除"他保过小明"这种模糊语境）
            w_idx = prefix.find(w)
            # 否定词在 prefix 后半部分（距离昵称 4 字符内）→ 判定为否定
            if len(prefix) - w_idx <= 4:
                return True
    return False


def _resolve_vote_target(
    group_id: int,
    voter_id: int,
    text: str) -> Optional[Player]:
    """
    解析投票目标。
    优先匹配 @user_id（@QQ号），再匹配昵称+投票意图。
    未匹配到任何目标时返回 None。
    """
    game = _get_game(group_id)
    if not game:
        return None

    text_stripped = text.strip()

    # 排除非玩家投票（允许自投）
    alive_others = [p for p in game["players"].values()
                    if p.is_alive()]
    if not alive_others:
        return None

    # Bug #4 修复：优先匹配 @user_id 格式（如 @12345678）
    for p in alive_others:
        at_id = f"@{p.user_id}"
        if at_id in text_stripped:
            return p

    # Bug #5 修复：投票意图校验 + 语义边界检查
    # Bug #2 修复：按昵称长度降序排序，优先匹配长昵称
    sorted_others = sorted(
        alive_others, key=lambda p: len(
        p.nickname), reverse=True)

    for p in sorted_others:
        nick = p.nickname

        # 问题 2 修复：使用字符串索引判断否定语境（替代 \w 正则）
        if _is_negated(text_stripped, nick):
            continue  # 否定句中的昵称不算投票目标

        # Bug #2 修复：投票意图词必须在昵称之前或紧邻（不在昵称之后很远）
        # 检查"投票意图词 + 昵称"的相对位置
        nick_idx = text_stripped.find(nick)
        if nick_idx < 0:
            continue

        # 策略 1：昵称前存在投票意图词 → 匹配
        prefix = text_stripped[:nick_idx]
        has_vote_in_prefix = bool(re.search(r"投|票|选", prefix))

        # 策略 2：文本是纯昵称
        is_pure_nickname = (text_stripped == nick)

        # 策略 3：昵称后有投票意图词，但昵称前也有 → 优先昵称前的语境
        # 例如："我觉得张三是卧底，所以我投李四"
        # 对"张三"：prefix="我觉得"，无投票词 → 不匹配
        # 对"李四"：prefix="我觉得张三是卧底，所以我投"，有"投" → 匹配

        if has_vote_in_prefix or is_pure_nickname:
            return p

    # 未匹配到任何目标 — 不兜底，返回 None
    return None


def _resolve_votes(group_id: int) -> str:
    """
    结算投票，淘汰得票最多者。
    返回结算消息。

    平票规则：
    - 唯一最高票：直接出局
    - 2人平票：进入 PK 发言 → 二次投票 → 仍平票则【平安夜】
    - ≥3人平票：直接【平安夜】
    """
    game = _get_game(group_id)
    if not game:
        return ""

    # 取消投票超时任务
    vote_task = _VOTE_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(vote_task)

    alive = [p for p in game["players"].values() if p.is_alive()]

    # 统计票数
    max_votes = max(p.vote_count for p in alive)
    max_voted = [p for p in alive if p.vote_count == max_votes]

    # 平票处理
    if len(max_voted) > 1:
        if len(max_voted) == 2:
            # 2人平票 → 进入 PK 发言阶段
            return _enter_pk_speaking(group_id, max_voted)
        else:
            # ≥3人平票 → 直接【平安夜】
            return _handle_peaceful_night(group_id, "多人平票")
    else:
        eliminated = max_voted[0]

    # 标记出局
    eliminated.alive = False

    # Bug #3 修复：有人真正出局时才重置僵局计数器
    game["stale_rounds"] = 0

    role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
    role_label = role_display.get(eliminated.role, eliminated.role)

    # 构建投票结果
    lines = [
        "🗳️ 投票结果：",
    ]
    for p in alive:
        vote_info = f"（{p.vote_count} 票）" if p.vote_count > 0 else ""
        lines.append(f"  • {p.nickname}{vote_info}")

    lines.append("")
    lines.append(
        f"💀 {eliminated.nickname} 得票最高（{eliminated.vote_count} 票），出局！")
    # BUG 2 修复：白板出局时隐藏词语，等猜词环节结束后再揭示
    if eliminated.role == "blank" and game["mode"] == "blank" and game.get("blank_mode") == "guess":
        lines.append("🎭 身份揭晓：保密（即将进入猜词环节）")
    else:
        lines.append("🎭 身份揭晓：保密")

    # BUG #3 修复：白板出局时，模式A（猜词流）触发猜词环节
    if eliminated.role == "blank" and game["mode"] == "blank" and game.get(
            "blank_mode") == "guess":
        lines.append("")
        lines.append(f"🔮 {eliminated.nickname} 作为白板，有机会猜平民词！")
        lines.append("💡 请发送词语内容猜平民词，猜对则白板单独获胜！")
        lines.append("⏰ 限时 60 秒，超时则继续游戏")
        game["phase"] = "blank_guessing"
        game["blank_guessing_player"] = eliminated.user_id
        game["blank_eliminated_word"] = game["word_civilian"]
        # 启动猜词倒计时
        _start_blank_guess_timeout(group_id, eliminated.user_id)
        return "\n".join(lines)

    # 检查胜利条件
    result = _check_and_end_game(group_id)
    if result:
        lines.append("")
        lines.append(result)
        return "\n".join(lines)

    # 继续下一轮
    game["round"] += 1
    remaining = sum(1 for p in game["players"].values() if p.is_alive())
    lines.append("")
    lines.append(f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...")

    # 重置状态，进入下一轮发言
    _start_next_round(group_id)

    # 公布下一轮发言顺序
    lines.append("")
    lines.append("📢 下一轮发言顺序：")
    order = game["speaker_order"]
    for i, uid in enumerate(order, 1):
        p = game["players"].get(uid)
        if p and p.is_alive():
            lines.append(f"  {i}. {p.nickname}")

    first_uid = order[0] if order else None
    first_player = game["players"].get(first_uid) if first_uid else None
    if first_player and first_player.is_alive():
        lines.append("")
        lines.append(f"🎤 请 {first_player.nickname} 开始发言！")

    return "\n".join(lines)


def _enter_pk_speaking(group_id: int, candidates: list[Player]) -> str:
    """
    2人平票 → 进入 PK 发言阶段。
    两名平票玩家依次进行辩护发言。
    """
    game = _get_game(group_id)
    if not game:
        return ""

    game["pk_candidates"] = [c.user_id for c in candidates]
    game["pk_speeches"] = {}
    game["pk_voters"] = set()

    # 重置 PK 投票计数
    for c in candidates:
        c.pk_vote_count = 0

    lines = [
    f"⚖️ 投票出现平票！{
        candidates[0].nickname} 与 {
            candidates[1].nickname} 均为 {
                candidates[0].vote_count} 票",
                "",
                "🔥 PK 发言阶段开始！",
                f"  请 {
                    candidates[0].nickname} 和 {
                        candidates[1].nickname} 依次进行辩护发言",
                        "  （直接群内发送消息即可，两人发完自动进入二次投票）",
                         ]

    game["phase"] = "pk_speaking"
    _start_pk_speak_timeout(group_id)  # BUG 修复：PK 发言无超时 → 挂机卡死
    return "\n".join(lines)


def _handle_pk_speech(group_id: int, user_id: int, text: str) -> Optional[str]:
    """
    处理 PK 发言。
    两名候选人各发一条消息，两人发完自动进入 PK 投票。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "pk_speaking":
        return None

    if user_id not in game["pk_candidates"]:
        return None  # 非 PK 候选人发言，忽略

    # 检查是否已经发过 PK 发言
    if user_id in game["pk_speeches"]:
        player = game["players"][user_id]
        return f"ℹ️ {player.nickname} 已经发过 PK 辩护了"

    # 记录 PK 发言
    game["pk_speeches"][user_id] = text
    player = game["players"][user_id]

    # 检查是否两个候选人都已发言
    if len(game["pk_speeches"]) == len(game["pk_candidates"]):
        return _enter_pk_voting(group_id)

    # 提示下一个候选人
    next_candidate = None
    for uid in game["pk_candidates"]:
        if uid not in game["pk_speeches"]:
            next_candidate = game["players"][uid]
            break

    if next_candidate:
        return f"👉 请 {next_candidate.nickname} 进行辩护发言"

    return None


def _enter_pk_voting(group_id: int) -> str:
    """
    PK 发言结束 → 进入 PK 二次投票阶段。
    除平票者外的其余存活玩家，仅限在这2人中重新投票。
    """
    game = _get_game(group_id)
    if not game:
        return ""

    # 检查候选人是否还存活（PK发言期间可能有人违规出局）
    alive_candidates = []
    for uid in game["pk_candidates"]:
        if uid in game["players"] and game["players"][uid].is_alive():
            alive_candidates.append(game["players"][uid])

    if len(alive_candidates) == 0:
        # 两人都出局了（极端情况）→ 平安夜
        return _handle_peaceful_night(group_id, "PK候选人已全部出局")

    if len(alive_candidates) == 1:
        # 只剩一个候选人 → 另一人出局了，存活者自动安全
        survivor = alive_candidates[0]
        lines = [
            f"🛡️ PK 候选人 {survivor.nickname} 的对手已出局",
            f"✅ {survivor.nickname} 本轮安全",
        ]
        # 问题 3 修复：先进入下一轮
        game["round"] += 1
        # 检查胜利条件
        result = _check_and_end_game(group_id)
        if result:
            lines.append(result)
            return "\n".join(lines)
        _start_next_round(group_id)
        remaining = sum(1 for p in game["players"].values() if p.is_alive())
        lines.append(f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...")
        # 公布下一轮发言顺序
        lines.append("")
        lines.append("📢 下一轮发言顺序：")
        order = game["speaker_order"]
        for i, uid in enumerate(order, 1):
            p = game["players"].get(uid)
            if p and p.is_alive():
                lines.append(f"  {i}. {p.nickname}")
        first_uid = order[0] if order else None
        first_player = game["players"].get(first_uid) if first_uid else None
        if first_player and first_player.is_alive():
            lines.append("")
            lines.append(f"🎤 请 {first_player.nickname} 开始发言！")
        return "\n".join(lines)

    # 两个候选人都存活 → 进入 PK 投票
    pk_a = alive_candidates[0]
    pk_b = alive_candidates[1]

    # 找出可以投票的玩家（存活且非 PK 候选人）
    voters = [p for p in game["players"].values()
              if p.is_alive() and p.user_id not in game["pk_candidates"]]

    # 重置 PK 投票计数
    pk_a.pk_vote_count = 0
    pk_b.pk_vote_count = 0

    game["pk_voters"] = set()

    # L4 修复：无 eligible voter → 自动 0:0 平票 → _resolve_pk_vote 中 0:0 分支为
    # 随机淘汰一人（防死锁），非平安夜（平安夜是双方都有票但票数相同的分支）
    if not voters:
        return _resolve_pk_vote(group_id)

    # 构建 PK 投票消息
    lines = [
        "🗣️ PK 发言回顾：",
        f"  • {pk_a.nickname}：{game['pk_speeches'].get(pk_a.user_id, '')}",
        f"  • {pk_b.nickname}：{game['pk_speeches'].get(pk_b.user_id, '')}",
        "",
        "🗳️ PK 二次投票开始！",
        f"  请在 {pk_a.nickname} 与 {pk_b.nickname} 之间投票",
        f"  发送 \"{pk_a.nickname}\" 或 \"{pk_b.nickname}\" 即为投票",
        f"  （可投票者：{len(voters)} 人，不含 PK 双方）",
        "",
        "  ⚠️ 若二次投票仍平票，则判定为【平安夜】（无人出局）",
        f"  💡 投票格式: @Bot 玩家昵称（例: @Bot {pk_a.nickname}）",
    ]

    game["phase"] = "pk_voting"
    _start_pk_vote_timeout(group_id)  # BUG 修复：PK 投票无超时 → 挂机卡死
    return "\n".join(lines)


def _handle_pk_vote(group_id: int, user_id: int, text: str) -> Optional[str]:
    """
    处理 PK 二次投票。
    存活非 PK 候选人在两名 PK 候选人之间投票。
    玩家发送候选人的昵称即为投票。
    """
    game = _get_game(group_id)
    if not game or game.get("phase") != "pk_voting":
        return None

    player = game["players"].get(user_id)
    if not player or not player.is_alive():
        return None

    # PK 候选人不能投票
    if user_id in game["pk_candidates"]:
        return f"ℹ️ {player.nickname} 是 PK 候选人，本轮不投票"

    # 已经投过票
    if user_id in game["pk_voters"]:
        return f"ℹ️ {player.nickname} 已经投过 PK 票了"

    # 找出可以投票的玩家（存活且非 PK 候选人）
    eligible_voters = [p for p in game["players"].values(
    ) if p.is_alive() and p.user_id not in game["pk_candidates"]]

    # 解析投票目标 - Bug #4 修复：优先匹配 @user_id
    target = None

    # 先尝试 @user_id 格式
    for uid in game["pk_candidates"]:
        at_id = f"@{uid}"
        if at_id in text:
            target = game["players"][uid]
            break

    # 再尝试昵称匹配（带投票意图校验 - Bug #5 修复）
    if not target:
        has_vote_intent = bool(re.search(r"投|票|选|@", text))
        is_pure = any(text.strip() == game["players"][uid].nickname
                      for uid in game["pk_candidates"] if uid in game["players"])
        if has_vote_intent or is_pure:
            for uid in game["pk_candidates"]:
                if uid in game["players"]:
                    candidate = game["players"][uid]
                    nick = candidate.nickname
                    # 问题 3 修复：PK 投票也检查否定语境
                    if nick in text and not _is_negated(text, nick):
                        target = candidate
                        break

    if not target:
        # 显示可选对象
        candidate_names = ", ".join(
            game["players"][uid].nickname for uid in game["pk_candidates"]
            if uid in game["players"] and game["players"][uid].is_alive()
        )
        return f"ℹ️ 请发送 \"{candidate_names}\" 中的一人进行 PK 投票"

    # 记录投票
    game["pk_voters"].add(user_id)
    target.pk_vote_count += 1

    # 检查是否所有人都投完了
    if len(game["pk_voters"]) >= len(eligible_voters):
        return _resolve_pk_vote(group_id)

    remaining = len(eligible_voters) - len(game["pk_voters"])
    return f"✅ {player.nickname} 投出 PK 票 | 剩余 {remaining} 人未投"


def _resolve_pk_vote(group_id: int) -> str:
    """
    结算 PK 二次投票。
    """
    game = _get_game(group_id)
    if not game:
        return ""

    candidates = [game["players"][uid] for uid in game["pk_candidates"]
                  if uid in game["players"] and game["players"][uid].is_alive()]

    if len(candidates) != 2:
        # 异常情况
        return _handle_peaceful_night(group_id, "PK候选人数量异常")

    pk_a = candidates[0]
    pk_b = candidates[1]

    lines = [
        "🗳️ PK 二次投票结果：",
        f"  • {pk_a.nickname}：{pk_a.pk_vote_count} 票",
        f"  • {pk_b.nickname}：{pk_b.pk_vote_count} 票",
        "",
    ]

    if pk_a.pk_vote_count > pk_b.pk_vote_count:
        # pk_a 出局
        eliminated = pk_a
    elif pk_b.pk_vote_count > pk_a.pk_vote_count:
        # pk_b 出局
        eliminated = pk_b
    else:
        # 平票处理
            # 问题 8 修复：双方均为 0 票 → 无人投票 → 随机淘汰一人，避免死锁
            if pk_a.pk_vote_count == 0 and pk_b.pk_vote_count == 0:
                import random
                eliminated = random.choice([pk_a, pk_b])
                lines.append(
                    "⚖️ PK 二次投票结果：双方均未获得任何投票")
                lines.append(
                    f"🎲 随机决定：{eliminated.nickname} 出局")
            else:
                # 仍平票 → 平安夜
                # 问题 4 修复：PK 平票也计入 stale_rounds 防死锁计数器
                game["stale_rounds"] = game.get("stale_rounds", 0) + 1
                lines.append(
                    f"🌙 【平安夜】PK 二次投票仍平票（{pk_a.pk_vote_count} 票 vs {pk_b.pk_vote_count} 票）")
                lines.append("  无人出局，直接进入下一轮发言")
                game["round"] += 1
                _start_next_round(group_id)
                remaining = sum(1 for p in game["players"].values() if p.is_alive())
                lines.append(f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...")
                # 公布下一轮发言顺序
                lines.append("")
                lines.append("📢 下一轮发言顺序：")
                order = game["speaker_order"]
                for i, uid in enumerate(order, 1):
                    p = game["players"].get(uid)
                    if p and p.is_alive():
                        lines.append(f"  {i}. {p.nickname}")
                first_uid = order[0] if order else None
                first_player = game["players"].get(first_uid) if first_uid else None
                if first_player and first_player.is_alive():
                    lines.append("")
                    lines.append(f"🎤 请 {first_player.nickname} 开始发言！")
                return "\n".join(lines)

    # 标记出局
    eliminated.alive = False
    # Bug #3 修复：有人真正出局时重置僵局计数器
    game["stale_rounds"] = 0
    role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
    role_label = role_display.get(eliminated.role, eliminated.role)

    lines.append(
        f"💀 {eliminated.nickname} PK 票数更高（{eliminated.pk_vote_count} 票），出局！")
    # BUG 2 修复：白板出局时隐藏词语，等猜词环节结束后再揭示
    if eliminated.role == "blank" and game["mode"] == "blank" and game.get(
            "blank_mode") == "guess":
        lines.append("🎭 身份揭晓：保密（即将进入猜词环节）")
    else:
        lines.append("🎭 身份揭晓：保密")

    # BUG #3 修复：白板出局时，模式A（猜词流）触发猜词环节
    if eliminated.role == "blank" and game["mode"] == "blank" and game.get(
            "blank_mode") == "guess":
        lines.append("")
        lines.append(f"🔮 {eliminated.nickname} 作为白板，有机会猜平民词！")
        lines.append("💡 请发送词语内容猜平民词，猜对则白板单独获胜！")
        lines.append("⏰ 限时 60 秒，超时则继续游戏")
        game["phase"] = "blank_guessing"
        game["blank_guessing_player"] = eliminated.user_id
        game["blank_eliminated_word"] = game["word_civilian"]
        _start_blank_guess_timeout(group_id, eliminated.user_id)
        return "\n".join(lines)

    # 检查胜利条件
    result = _check_and_end_game(group_id)
    if result:
        lines.append("")
        lines.append(result)
        return "\n".join(lines)

    # 继续下一轮
    game["round"] += 1
    remaining = sum(1 for p in game["players"].values() if p.is_alive())
    lines.append("")
    lines.append(f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...")

    _start_next_round(group_id)

    # 公布下一轮发言顺序
    lines.append("")
    lines.append("📢 下一轮发言顺序：")
    order = game["speaker_order"]
    for i, uid in enumerate(order, 1):
        p = game["players"].get(uid)
        if p and p.is_alive():
            lines.append(f"  {i}. {p.nickname}")

    first_uid = order[0] if order else None
    first_player = game["players"].get(first_uid) if first_uid else None
    if first_player and first_player.is_alive():
        lines.append("")
        lines.append(f"🎤 请 {first_player.nickname} 开始发言！")

    return "\n".join(lines)


def _handle_peaceful_night(group_id: int, reason: str) -> str:
    """
    处理【平安夜】——无人出局，直接进入下一轮发言。
    """
    game = _get_game(group_id)
    if not game:
        return ""

    lines = [
        f"🌙 【平安夜】{reason}，本轮无人出局！",
        "",
    ]

    # BUG #7 修复：连续平安夜计数器
    game["stale_rounds"] = game.get("stale_rounds", 0) + 1
    if game["stale_rounds"] >= 3:
        # 连续 3 轮平安夜，强制随机淘汰一名非当前发言玩家
        alive = [p for p in game["players"].values() if p.is_alive()]
        if len(alive) > 1:
            import random
            victim = random.choice(alive)
            victim.alive = False
            victim.has_spoken = True
            # Bug #3 修复：强制淘汰后重置僵局计数器
            # L1 修复：先保存原僵局轮数再重置——原代码先置 0，公告显示"连续 0 轮"（应为 3）
            stale_rounds_before = game["stale_rounds"]
            game["stale_rounds"] = 0
            remaining = len(alive) - 1
            role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
            result = _check_and_end_game(group_id)
            if result:
                return (
                    f"⚖️ 连续 {stale_rounds_before} 轮平安夜，触发强制随机淘汰！\n"
                    f"💀 {
            victim.nickname}（{
                role_display.get(
                    victim.role,
                    '未知')}）被随机淘汰\n\n" f"{result}" )
            _start_next_round(group_id)
            msg = (
                f"⚖️ 连续 {stale_rounds_before} 轮平安夜，触发强制随机淘汰！\n"
                f"💀 {victim.nickname} 被随机淘汰\n"
                f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...\n"
            )
            # 公布下一轮发言顺序
            msg += "\n📢 下一轮发言顺序：\n"
            order = game["speaker_order"]
            for i, uid in enumerate(order, 1):
                p = game["players"].get(uid)
                if p and p.is_alive():
                    msg += f"  {i}. {p.nickname}\n"
            first_uid = order[0] if order else None
            first_player = game["players"].get(first_uid) if first_uid else None
            if first_player and first_player.is_alive():
                msg += f"\n🎤 请 {first_player.nickname} 开始发言！\n"
            return msg.rstrip()
        # 只剩 1 人，直接结束
        game["phase"] = "ended"
        game["active"] = False
        _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
        return f"⚖️ 连续 {game['stale_rounds']} 轮平安夜，仅剩 1 人存活，游戏结束"

    # 检查胜利条件（平安夜后也可能触发）
    result = _check_and_end_game(group_id)
    if result:
        lines.append(result)
        return "\n".join(lines)

    # 继续下一轮
    game["round"] += 1
    remaining = sum(1 for p in game["players"].values() if p.is_alive())
    lines.append(f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...")

    _start_next_round(group_id)

    # 公布下一轮发言顺序
    lines.append("")
    lines.append("📢 下一轮发言顺序：")
    order = game["speaker_order"]
    for i, uid in enumerate(order, 1):
        p = game["players"].get(uid)
        if p and p.is_alive():
            lines.append(f"  {i}. {p.nickname}")

    first_uid = order[0] if order else None
    first_player = game["players"].get(first_uid) if first_uid else None
    if first_player and first_player.is_alive():
        lines.append("")
        lines.append(f"🎤 请 {first_player.nickname} 开始发言！")

    return "\n".join(lines)


def _start_next_round(group_id: int):
    """开始下一轮发言"""
    game = _get_game(group_id)
    if not game:
        return

    # 更新发言顺序（只保留存活玩家）
    alive_ids = [uid for uid in game["speaker_order"]
                 if uid in game["players"] and game["players"][uid].is_alive()]
    random.shuffle(alive_ids)

    game["speaker_order"] = alive_ids
    game["current_speaker_idx"] = 0
    game["phase"] = "speaking"
    game["voters"] = set()
    game["speak_start_time"] = time.time()

    # 重置玩家本轮状态
    for p in game["players"].values():
        if p.is_alive():
            p.has_spoken = False
            p.description = ""
            p.vote_count = 0
            p.pk_vote_count = 0

    # 重置 PK 状态
    game["pk_candidates"] = []
    game["pk_speeches"] = {}
    game["pk_voters"] = set()

    # BUG 修复（2026-08-03）：第二轮起没有重启发言定时器 → 玩家挂机永久卡死。
    # 这里统一在每轮发言开始时启动定时器（websocket 从 _WEBSOCKETS 缓存获取）。
    start_speak_timer(group_id)


# ============================================================
#  胜利条件判定
# ============================================================
def _check_and_end_game(group_id: int) -> str:
    """
    检查胜利条件，返回结果消息或空字符串（游戏继续）。

    判定顺序（白板模式下）：
    1. 卧底全出局 → 平民胜利（即使白板存活，仍判平民胜）
    2. 卧底人数 ≥ 平民人数 → 卧底胜利（不计算白板人数）
    - 模式B（依附流）：白板也随卧底同胜
    3. 白板模式A（猜词流）：白板猜对平民词 → 白板单独获胜

    普通模式下：
    1. 卧底全出局 → 平民胜利
    2. 卧底人数 ≥ 平民人数 → 卧底胜利
    """
    game = _get_game(group_id)
    if not game:
        return ""

    alive = [p for p in game["players"].values() if p.is_alive()]

    # 统计存活角色
    civilians = [p for p in alive if p.role == "civilian"]
    spies = [p for p in alive if p.role == "spy"]
    blanks = [p for p in alive if p.role == "blank"]

    mode = game["mode"]

    if mode == "normal":
        # 普通模式
        if not spies:
            # 卧底全出局 → 平民胜利
            game["phase"] = "ended"
            game["active"] = False
            _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
            return _build_win_message(group_id, civilians, "平民", "卧底已全部找出，平民胜利！")

        if len(spies) >= len(civilians):
            # 卧底人数 ≥ 平民 → 卧底胜利
            game["phase"] = "ended"
            game["active"] = False
            _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
            return _build_win_message(group_id, spies, "卧底", "卧底已掌控局面，卧底胜利！")

    elif mode == "blank":
        # 白板模式
        # BUG #1 修复：卧底全出局 → 先判平民胜利（即使白板存活）
        if not spies:
            game["phase"] = "ended"
            game["active"] = False
            _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
            blank_mode = game.get("blank_mode", "guess")
            # 白板已出局时，平民胜利
            if not blanks:
                return _build_win_message(
                    group_id, civilians, "平民", "卧底已全部找出，平民胜利！")
            # 模式A（猜词流）：白板存活时，白板与平民同胜
            if blank_mode == "guess":
                return _build_win_message(
                    group_id,
                    civilians + blanks,
                    "平民",
                    "卧底已全部找出，平民胜利！（即使白板存活，仍判平民胜）")
            # 模式B（依附流）：白板依附卧底，卧底全出局 → 白板也败，只判平民胜利
            return _build_win_message(
                group_id,
                civilians,
                "平民",
                "卧底已全部找出，平民胜利！（白板依附卧底，卧底出局白板同败）")

        # BUG #2 修复：卧底人数 ≥ 平民人数 → 卧底胜利（不计算白板人数）
        if len(spies) >= len(civilians):
            game["phase"] = "ended"
            game["active"] = False
            _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
            blank_mode = game.get("blank_mode", "guess")
            # BUG #4 修复：模式B（依附流）白板随卧底同胜
            if blank_mode == "attach" and blanks:
                return _build_win_message(
                    group_id, spies + blanks, "卧底", "卧底已掌控局面，卧底胜利！（白板依附卧底同胜）")
            return _build_win_message(group_id, spies, "卧底", "卧底已掌控局面，卧底胜利！")

    # BUG #8 修复：人数不足判定（仅在双方均无获胜可能时触发）
    # 若 4 人局剩 2 平 1 卧，卧底再杀 1 平即可获胜，过早判负会剥夺合理胜利路径
    # 问题 2 修复：使用存活卧底数而非初始卧底数
    civilian_alive = sum(1 for p in alive if p.role == "civilian")
    spy_alive = sum(1 for p in alive if p.role == "spy")

    # BUG 3 修复：原版条件 len(alive) <= spy_alive 永不成立（数学矛盾）
    # 改为：白板模式下平民和卧底全部灭，只剩白板 → 白板胜利
    blank_alive = sum(1 for p in alive if p.role == "blank")
    if civilian_alive == 0 and spy_alive == 0 and blank_alive > 0:
        survivors = [p.nickname for p in alive]
        game["phase"] = "ended"
        game["active"] = False
        _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
        # 记录战绩 + 排行榜
        _record_game_stats(list(game["players"].values()), alive)
        msg = f"🎉 平民与卧底全部出局，白板玩家 {', '.join(survivors)} 孤身获胜！"
        lb = _get_leaderboard()
        return msg + lb if lb else msg

    # 游戏继续
    return ""


def _build_win_message(
    group_id: int,
    winners: list[Player],
    role_name: str,
    victory_text: str) -> str:
    """构建胜利消息"""
    game = _get_game(group_id)
    lines = [f"🏆 {victory_text}"]
    lines.append(f"🎉 获胜方：{role_name}")
    lines.append(f"👑 获胜者：{', '.join(p.nickname for p in winners)}")

    # 全员身份揭晓
    role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
    lines.append("")
    lines.append("📋 全员身份揭晓：")
    if game:
        for p in game["players"].values():
            label = role_display.get(p.role, p.role)
            word_info = f"（词语：{p.word}）" if p.word else "（空白）"
            alive_tag = "✅" if p.alive else "💀"
            lines.append(f"  {alive_tag} {p.nickname} — {label} {word_info}")
    # 记录获胜者信息，供 /卧底惩罚 使用
    if game:
        game["_winners"] = [(p.user_id, p.nickname) for p in winners]

    # 记录本场战绩
    if game:
        all_players = list(game["players"].values())
        _record_game_stats(all_players, winners)

    # 追加排行榜
    leaderboard = _get_leaderboard()
    if leaderboard:
        lines.append(leaderboard)

    return "\n".join(lines)


def _handle_spy_punishment(group_id: int) -> tuple[str, list[int]]:
    """
    卧底游戏惩罚：从真心话大冒险题库中随机抽三道题。

    返回 (消息文本, 需要 @ 的输家 user_id 列表)
    """
    game = _get_game(group_id)
    if not game:
        return "⚠️ 没有找到游戏记录", []

    if game["phase"] != "ended":
        return "⚠️ 卧底游戏还没有结束，请结束后再使用 /卧底惩罚", []

    # 获取输家
    winners_uids = set(uid for uid, _ in game.get("_winners", []))
    losers = []
    for p in game["players"].values():
        if p.user_id not in winners_uids:
            losers.append(p)

    if not losers:
        return "🤔 没有找到输家（全员胜利？）", []

    loser_names = "、".join(p.nickname for p in losers)
    loser_uids = [p.user_id for p in losers]

    # 调用真心话大冒险题库抽题
    try:
        import games.entertainment as entertainment
        questions = entertainment.pick_spy_punishment(loser_uids, count=3)
    except Exception as e:
        return f"⚠️ 抽题失败：{e}", []

    if not questions:
        return "😅 题库暂无可用题目", []

    # 构建惩罚消息
    type_emoji = {"truth": "🔵", "dare": "🔴"}
    type_cn = {"truth": "真心话", "dare": "大冒险"}

    lines = [
        "⚔️ 卧底游戏惩罚挑战",
        f"🎯 受罚者：{loser_names}",
        "",
    ]

    for i, q in enumerate(questions, 1):
        emoji = type_emoji.get(q["type"], "🔵")
        cn = type_cn.get(q["type"], "真心话")
        lines.append(f"第{i}题 {emoji} {cn}：")
        lines.append(f"「{q['question']}」")
        if i < len(questions):
            lines.append("")

    lines.append("")
    lines.append("💡 输家可以选择回答真心话或完成大冒险挑战！")

    return "\n".join(lines), loser_uids


# ============================================================
#  游戏结束与清理
# ============================================================
def end_game(group_id: int) -> str:
    """手动结束游戏"""
    game = _get_game(group_id)
    if not game:
        return "⚠️ 当前没有卧底游戏"

    # 取消定时器
    task = _SPEAK_TIMEOUT_TASKS.pop(group_id, None)
    _cancel_task_safe(task)

    _end_game(group_id)
    return "🏁 游戏已结束，发送 /卧底 重新开始"


def _end_game(group_id: int):
    """内部清理函数"""
    # 取消发言/投票/白板猜词/PK 定时器
    for task_dict in [_SPEAK_TIMEOUT_TASKS, _VOTE_TIMEOUT_TASKS,
                      _BLANK_GUESS_TIMEOUT_TASKS, _PK_SPEAK_TIMEOUT_TASKS,
                      _PK_VOTE_TIMEOUT_TASKS]:
        task = task_dict.pop(group_id, None)
        _cancel_task_safe(task)

    # 取消所有待处理的 LLM 判定任务
    _cancel_llm_tasks(group_id)

    if group_id in _SPY_GAMES:
        _SPY_GAMES[group_id]["active"] = False
        _SPY_GAMES[group_id]["phase"] = "ended"
        # 保留 60 秒后才完全删除，防止竞态
        # 问题 7 修复：改为 asyncio 异步延迟清理，不再使用 threading.Thread
        # Bug #1 修复：传入 game_id 避免误删新游戏
        _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))


def phase_map(phase: str) -> str:
    """阶段名映射"""
    return {
        "lobby": "准备阶段",
        "speaking": "发言",
        "voting": "投票",
        "pk_speaking": "PK发言",
        "pk_voting": "PK投票",
        "blank_guessing": "白板猜词",
        "ended": "已结束"}.get(
        phase,
        phase)


# ============================================================
#  指令检查（bot.py 调用）
# ============================================================
def _spy_help_text() -> str:
    """返回卧底游戏帮助文本"""
    return (
        "🕵️ **谁是卧底 - 游戏规则**\n\n"
        "每人获得一个词语，其中大部分人的词语相同（平民），\n"
        "有 1 人获得不同的词语（卧底）。"
        "在白板模式下还有 1 人获得空白词语（白板）。\n\n"
        "🎯 **目标**\n"
        "平民：找出卧底（和白板）\n"
        "卧底：隐藏身份，存活到最后\n"
        "白板：猜出自己的词语并存活\n\n"
        "📝 **流程**\n"
        "1. 每人发言描述自己的词语（不能包含词语中的字！）\n"
        "2. 发言完毕后进入投票阶段，@Bot 即为投票\n"
        "3. 得票最高者出局（平票时 2 人 PK 发言 + 二次投票，≥3 人平票则【平安夜】）\n"
        "4. 卧底全出局 → 平民胜；卧底存活 ≥ 平民 → 卧底胜\n\n"
        "🎭 **白板模式**（需 4 人）\n"
        "- 模式A（猜词流）：白板被投票出局时，可猜平民词，猜对则单独获胜（默认）\n"
        "- 模式B（依附流）：白板与卧底绑定，卧底胜利则白板同胜\n\n"
        "⚡ **指令**\n"
        "/卧底          创建游戏房间\n"
        "/卧底 白板     创建白板模式（默认模式A-猜词流）\n"
        "/卧底 白板 猜词  创建白板模式（模式A-猜词流）\n"
        "/卧底 白板 依附  创建白板模式（模式B-依附流）\n"
        "/卧底加入      加入游戏\n"
        "/卧底开始      开始游戏\n"
        "/卧底状态      查看游戏状态\n"
        "/卧底时间 X分   设置发言限时\n"
        "/卧底结束      结束游戏\n\n"
        "⚠️ 发言时绝对不可包含自己词语中的任何一个字！"
    )


def check_command(
    text: str,
    group_id: int = None,
    user_id: int = None,
    nickname: str = None) -> Optional[str]:
    """
    检查是否匹配卧底游戏指令。
    返回回复文本，None 表示不匹配。
    """
    # 卧底帮助（私聊可用）
    if text in ("/卧底帮助", "/卧底help", "/卧底 帮助"):
        return _spy_help_text()
    if not group_id:
        return None

    text = text.strip()

    # /卧底 创建游戏
    if text == "/卧底" or text == "/谁是卧底":
        game = create_game(group_id, "normal")
        if game is None:
            existing = _SPY_GAMES.get(group_id)
            if existing and existing.get("active"):
                return get_game_status(group_id)
            return "⚠️ 创建游戏失败"
        game["creator_id"] = user_id
        mode_desc = "（白板模式）" if game["mode"] == "blank" else ""
        return (
            f"🕵️ 卧底游戏房间已创建！{mode_desc}\n"
            f"💡 发送 /卧底加入 加入游戏\n"
            f"💡 至少 4 人后发送 /卧底开始 开始游戏\n"
            f"💡 发送 /卧底 白板 可创建含白板的模式"
        )

    # /卧底 白板 创建白板模式（支持模式A和模式B）
    elif text == "/卧底 白板" or text == "/谁是卧底 白板":
        game = create_game(group_id, "blank", "guess")
        if game is None:
            existing = _SPY_GAMES.get(group_id)
            if existing and existing.get("active"):
                return get_game_status(group_id)
            return "⚠️ 创建游戏失败"
        game["creator_id"] = user_id
        return (
            "🕵️ 卧底游戏房间已创建！（白板模式-模式A猜词流）\n"
            f"💡 模式A：白板被投票出局时，可猜平民词，猜对则单独获胜\n"
            f"💡 发送 /卧底加入 加入游戏\n"
            f"💡 发送 /卧底 白板 依附 可创建模式B（依附流）\n"
            f"💡 至少 4 人后发送 /卧底开始 开始游戏"
        )

    # /卧底 白板 猜词 创建白板模式（模式A-猜词流，显式）
    elif text == "/卧底 白板 猜词" or text == "/谁是卧底 白板 猜词":
        game = create_game(group_id, "blank", "guess")
        if game is None:
            existing = _SPY_GAMES.get(group_id)
            if existing and existing.get("active"):
                return get_game_status(group_id)
            return "⚠️ 创建游戏失败"
        game["creator_id"] = user_id
        return (
            "🕵️ 卧底游戏房间已创建！（白板模式-模式A猜词流）\n"
            f"💡 模式A：白板被投票出局时，可猜平民词，猜对则单独获胜\n"
            f"💡 发送 /卧底加入 加入游戏\n"
            f"💡 至少 4 人后发送 /卧底开始 开始游戏"
        )

    # /卧底 白板 依附 创建白板模式（模式B-依附流）
    elif text == "/卧底 白板 依附" or text == "/谁是卧底 白板 依附":
        game = create_game(group_id, "blank", "attach")
        if game is None:
            existing = _SPY_GAMES.get(group_id)
            if existing and existing.get("active"):
                return get_game_status(group_id)
            return "⚠️ 创建游戏失败"
        game["creator_id"] = user_id
        return (
            "🕵️ 卧底游戏房间已创建！（白板模式-模式B依附流）\n"
            f"💡 模式B：白板与卧底绑定，卧底胜利则白板同胜\n"
            f"💡 发送 /卧底加入 加入游戏\n"
            f"💡 至少 4 人后发送 /卧底开始 开始游戏"
        )

    # /卧底加入
    elif text == "/卧底加入":
        success, msg = join_game(group_id, user_id, nickname)
        return msg

    # /卧底退出
    elif text == "/卧底退出":
        return leave_game(group_id, user_id)

    # /卧底开始 — 返回特殊标记，由 bot.py 统一调用 start_game + send_words + start_speak_timer
    elif text == "/卧底开始":
        return "__SPY_START_MARKER__"

    # /卧底状态
    elif text == "/卧底状态":
        return get_game_status(group_id)

    # /卧底结束 / /结束（仅卧底活跃时拦截，否则放行给后续模块）
    elif text == "/卧底结束" or text == "/结束":
        if not is_active(group_id):
            return None  # 无活跃卧底游戏，放行给海龟汤等后续模块
        return end_game(group_id)

    # /卧底惩罚 - 从真心话大冒险题库中随机抽三道题作为输家惩罚
    elif text == "/卧底惩罚":
        return _handle_spy_punishment(group_id)

    # /卧底时间 X秒/分
    elif text.startswith("/卧底时间 "):
        time_str = text[6:].strip()
        # 解析时间
        import re
        match = re.match(r"(\d+)(秒|分)?", time_str)
        if match:
            value = int(match.group(1))
            unit = match.group(2) or "秒"
            if unit == "分":
                value *= 60
            if value < 30 or value > 600:
                return "⚠️ 发言时间必须在 30-600 秒之间"
            game = _get_game(group_id)
            if game:
                game["speak_timeout"] = value
                minutes = value // 60
                secs = value % 60
                time_display = f"{minutes}分{secs}秒" if minutes else f"{secs}秒"
                return f"⏱️ 发言时间已设置为 {time_display}"
        return "⚠️ 用法：/卧底时间 3分 或 /卧底时间 180秒"

    # /卧底帮助
    elif text == "/卧底帮助":
        return (
            "🕵️ 【谁是卧底】帮助\n\n"
            "📋 指令：\n"
            "  /卧底          创建游戏房间\n"
            "  /卧底 白板     创建白板模式（需 4 人）\n"
            "  /卧底加入      加入游戏\n"
            "  /卧底退出      退出游戏\n"
            "  /卧底开始      开始游戏\n"
            "  /卧底状态      查看游戏状态\n"
            "  /卧底结束      结束游戏\n"
            "  /卧底惩罚       从真心话大冒险题库随机抽3题作为输家惩罚\n"
            "  /卧底时间 X分   设置发言限时（如 /卧底时间 5分）\n"
            "  /卧底判定 开    开启 LLM 语义判定（默认开启）\n"
            "  /卧底判定 关    关闭 LLM 语义判定\n"
            "  /卧底统计       查看历史使用记录\n"
            "  /卧底重置题库   清空历史使用记录\n\n"
            "🎮 玩法：\n"
            "  1. 创建房间后，玩家发送 /卧底加入\n"
            "  2. 满 4 人后 /卧底开始 开始游戏\n"
            "  3. 每个人会通过私聊收到自己的词语\n"
            "  4. 按顺序发言描述词语，不能包含词语中的任何字\n"
            "  5. 发言会被 AI 裁判判定是否符合词语\n"
            "  6. 发言结束后投票，得票最多者出局\n"
            "  7. 平民找出卧底获胜，卧底隐藏成功则卧底获胜\n"
            "  9. 白板模式中，白板不知道自己的词，需要猜阵营\n\n"
            "📚 题库机制：\n"
            "  - 已使用的词语不会重复抽取\n"
            "  - 全部用完后自动重置历史记录\n"
            "  - 发送 /卧底统计 查看使用情况"
        )

    # /卧底判定 开/关
    elif text.startswith("/卧底判定 "):
        mode = text[6:].strip()
        if mode in ("开", "关闭", "关"):
            global _ENABLE_LLM_JUDGE
            _ENABLE_LLM_JUDGE = mode == "开"
            status = "已开启" if _ENABLE_LLM_JUDGE else "已关闭"
            return f"🤖 LLM 语义判定{status}。{'描述需符合词语，否则出局' if _ENABLE_LLM_JUDGE else '仅检测是否包含词语中的字'}"
        return "⚠️ 用法：/卧底判定 开 或 /卧底判定 关"

    # /卧底统计 - 查看历史记录统计
    elif text == "/卧底统计":
        used = _get_used_count()
        total = len(_WORD_BANK)
        remaining = total - used
        return (
            f"📊 卧底游戏历史统计\n"
            f"📚 题库总量：{total} 组\n"
            f"✅ 已使用：{used} 组\n"
            f"🆕 剩余可用：{remaining} 组\n"
            f"💡 已使用的词语不会重复抽取，全部用完后自动重置\n"
            f"💡 发送 /卧底重置题库 可手动清空历史记录"
        )

    # /卧底重置题库 - 清空历史记录
    elif text == "/卧底重置题库" or text == "/卧底重置":
        cleared = _clear_used_words()
        return f"🗑️ 已清空 {cleared} 条历史记录，所有词语将重新可用"

    return None


# ============================================================
#  发言/投票阶段的被动消息处理
# ============================================================
def _handle_blank_guess(game: dict, user_id: int, guess: str) -> Optional[str]:
    """
    处理白板猜词（供同步和异步 handler 共用）。
    """
    group_id = game.get("group_id")
    # 防御：白板玩家在猜词阶段 /卧底退出 后，players 中已无该用户，
    # 但 blank_guessing_player 仍指向他 → 直接 KeyError 会吞掉整条群消息
    if user_id not in game["players"]:
        return None
    eliminated = game["players"][user_id]
    civilian_word = game["blank_eliminated_word"]

    # BUG 5 修复：白板猜词时检查白板是否已出局（可能被二次投票/异常）
    if eliminated.is_alive():
        return "⚠️ 你还没有出局，不能猜词"

    # Bug #5 修复：白板猜词改为精确匹配（去除常见前后缀后）
    # 避免"苹果手机"匹配"苹果"这种过于宽松的子串匹配
    guess_cleaned = guess.strip()
    # 去除常见前缀
    for prefix in ["我猜是", "答案是", "我猜", "猜", "我觉得是", "应该是", "是", "这个词是", "这个词应该是"]:
        if guess_cleaned.startswith(prefix):
            guess_cleaned = guess_cleaned[len(prefix):].strip()
            break
    # 去除常见后缀
    for suffix in ["吧", "啊", "呢", "哦", "吧！", "啊！", "！", "。", "？"]:
        if guess_cleaned.endswith(suffix):
            guess_cleaned = guess_cleaned[:-len(suffix)].strip()
            break
    # 精确匹配
    if guess_cleaned == civilian_word:
        # 白板猜对，单独获胜
        game["phase"] = "ended"
        game["active"] = False
        _schedule_game_cleanup(group_id, _SPY_GAMES[group_id].get("game_id", 0))
        task = _BLANK_GUESS_TIMEOUT_TASKS.pop(group_id, None)
        _cancel_task_safe(task)
        # 记录战绩
        _record_game_stats(list(game["players"].values()), [eliminated])
        lines = [
            f"🎯 {eliminated.nickname} 猜对了！平民词是「{civilian_word}」",
            f"🏆 白板 {eliminated.nickname} 单独获胜！",
            "",
            "📋 全员身份揭晓：",
        ]
        role_display = {"civilian": "平民", "spy": "卧底", "blank": "白板"}
        for p in game["players"].values():
            label = role_display.get(p.role, p.role)
            word_info = f"（词语：{p.word}）" if p.word else "（空白）"
            alive_tag = "✅" if p.alive else "💀"
            lines.append(f"  {alive_tag} {p.nickname} — {label} {word_info}")
        # 追加排行榜
        lb = _get_leaderboard()
        if lb:
            lines.append(lb)
        return "\n".join(lines)
    else:
        # 白板猜错
        # 取消超时任务
        task = _BLANK_GUESS_TIMEOUT_TASKS.pop(group_id, None)
        _cancel_task_safe(task)
        # 检查胜利条件
        result = _check_and_end_game(group_id)
        if result:
            return result
        # 继续游戏
        game["round"] += 1
        remaining = sum(1 for p in game["players"].values() if p.is_alive())
        _start_next_round(group_id)
        msg = f"❌ {eliminated.nickname} 猜错了！平民词保密，继续游戏。\n"
        msg += f"👥 剩余 {remaining} 人，准备第 {game['round']} 轮发言...\n"
        # 公布下一轮发言顺序
        msg += "\n📢 下一轮发言顺序：\n"
        order = game["speaker_order"]
        for i, uid in enumerate(order, 1):
            p = game["players"].get(uid)
            if p and p.is_alive():
                msg += f"  {i}. {p.nickname}\n"
        first_uid = order[0] if order else None
        first_player = game["players"].get(first_uid) if first_uid else None
        if first_player and first_player.is_alive():
            msg += f"\n🎤 请 {first_player.nickname} 开始发言！\n"
        return msg.rstrip()


def handle_game_message(
    group_id: int,
    user_id: int,
    text: str,
    is_at_bot: bool = False) -> Optional[str]:
    """
    在游戏进行中（发言/投票阶段）被动处理玩家消息（同步版本）。
    这是核心消息钩子，bot.py 需要在群消息处理中调用。

    返回群内回复文本，None 表示无需回复。
    """
    game = _get_game(group_id)
    if not game:
        return None

    # 白板猜词阶段
    if game["phase"] == "blank_guessing":
        if user_id == game["blank_guessing_player"]:
            return _handle_blank_guess(game, user_id, text.strip())
        return None  # 非白板玩家发言忽略

    phase = game.get("phase")

    if phase == "speaking":
        # 发言阶段：检查是否是玩家消息
        if user_id in game["players"]:
            player = game["players"][user_id]
            if player.is_alive() and not player.has_spoken:
                return handle_speaking_message(group_id, user_id, text)
    elif phase == "voting":
        # 投票阶段：存活玩家消息中含 @ 或玩家昵称即视为投票（无需 @Bot）
        if user_id in game["players"]:
            player = game["players"][user_id]
            if not player.is_alive():
                return None
            # 检查消息是否包含 @ 或其他玩家昵称
            other_nicknames = [
                p.nickname for p in game["players"].values()
                if p.is_alive() and p.user_id != user_id
            ]
            is_vote = "@" in text or any(
                nick in text for nick in other_nicknames
            )
            if is_vote:
                return handle_vote(group_id, user_id, text)
    elif phase == "pk_speaking":
        # PK 发言阶段：PK 候选人直接发送消息
        if user_id in game.get("pk_candidates", []):
            return _handle_pk_speech(group_id, user_id, text)
    elif phase == "pk_voting":
        # PK 二次投票阶段：发送候选人昵称即为投票
        if user_id in game["players"]:
            return _handle_pk_vote(group_id, user_id, text)

    return None


async def handle_game_message_async(
    group_id: int,
    user_id: int,
    text: str,
    is_at_bot: bool = False) -> Optional[str]:
    """
    在游戏进行中（发言/投票阶段）被动处理玩家消息（异步版本，含 LLM 判定）。
    这是核心消息钩子，bot.py 需要在群消息处理中调用。

    返回群内回复文本，None 表示无需回复。
    """
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    phase = game.get("phase")

    # 白板猜词阶段
    if phase == "blank_guessing":
        if user_id == game["blank_guessing_player"]:
            return _handle_blank_guess(game, user_id, text.strip())
        return None

    if phase == "speaking":
        # 发言阶段：检查是否是玩家消息
        if user_id in game["players"]:
            player = game["players"][user_id]
            if player.is_alive() and not player.has_spoken:
                return await handle_speaking_message_async(group_id, user_id, text)
    elif phase == "voting":
        # 投票阶段：存活玩家消息中含 @ 或玩家昵称即视为投票（无需 @Bot）
        if user_id in game["players"]:
            player = game["players"][user_id]
            if not player.is_alive():
                return None
            # 检查消息是否包含 @ 或其他玩家昵称
            other_nicknames = [
                p.nickname for p in game["players"].values()
                if p.is_alive() and p.user_id != user_id
            ]
            is_vote = "@" in text or any(
                nick in text for nick in other_nicknames
            )
            if is_vote:
                return handle_vote(group_id, user_id, text)
    elif phase == "pk_speaking":
        # PK 发言阶段
        if user_id in game.get("pk_candidates", []):
            return _handle_pk_speech(group_id, user_id, text)
    elif phase == "pk_voting":
        # PK 二次投票阶段
        if user_id in game["players"]:
            return _handle_pk_vote(group_id, user_id, text)

    return None
