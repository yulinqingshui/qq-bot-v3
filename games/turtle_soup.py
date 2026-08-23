#!/usr/bin/env python3
"""
海龟汤游戏模块 - 独立于主程序，提供"海龟汤"情境推理游戏。
集成到 bot.py 的群聊消息流中使用。

游戏流程：
1. /海龟汤 → 端上汤面（谜面）
2. 玩家 @Bot 提问（是/否问题）→ LLM 根据汤底判定
3. /整理线索 → 汇总已确认的"是"线索
4. /提示 → 给出边缘线索
5. 玩家猜中汤底 或 /答案 揭锅
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
import threading
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("qq-bot")

# ============================================================
#  路径配置
# ============================================================
BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOUP_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "question_bank", "turtle_soup.json")
_HISTORY_DB_PATH = os.path.join(BASE_DIR, "data", "turtle_soup_history.db")

# ============================================================
#  LLM 调用（同步）
# ============================================================
from core.llm import call_llm, llm_enabled

# ============================================================
#  题库加载
# ============================================================
_SOUPS: list[dict] = []


def _load_soup_bank():
    """加载海龟汤题库"""
    global _SOUPS
    try:
        with open(SOUP_DATA_PATH, encoding="utf-8") as f:
            _SOUPS = json.load(f)
        logger.info(f"🐢 海龟汤题库加载完成，共 {len(_SOUPS)} 题")
    except FileNotFoundError:
        logger.error(f"海龟汤题库未找到: {SOUP_DATA_PATH}")
    except Exception as e:
        logger.error(f"海龟汤题库加载失败: {e}")


_load_soup_bank()


def reload_soup_bank():
    """重新加载题库"""
    _load_soup_bank()
    return len(_SOUPS)


# ============================================================
#  历史记录数据库（记录已用过的题目，避免重复）
# ============================================================
_db_lock = threading.Lock()


@contextmanager
def _soup_get_db():
    """历史数据库上下文管理器"""
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
    with _db_lock:
        with _soup_get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS used_soups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    soup_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    used_at REAL NOT NULL,
                    solved INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS player_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    nickname TEXT NOT NULL,
                    solved_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id)
                )
            """)


_init_history_db()


def _record_used_soup(soup_id: int, group_id: int, solved: bool):
    """记录已使用的汤"""
    with _db_lock:
        with _soup_get_db() as conn:
            conn.execute(
                "INSERT INTO used_soups (soup_id, group_id, used_at, solved) VALUES (?, ?, ?, ?)",
                (soup_id, group_id, time.time(), 1 if solved else 0),
            )


def _record_player_solve(user_id: int, nickname: str):
    """记录用户猜中成绩"""
    with _db_lock:
        with _soup_get_db() as conn:
            conn.execute(
                "INSERT INTO player_stats (user_id, nickname, solved_count, updated_at) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "solved_count = solved_count + 1, "
                "nickname = excluded.nickname, "
                "updated_at = excluded.updated_at",
                (user_id, nickname, time.time()),
            )


def _get_leaderboard(group_id: int | None = None, top_n: int = 5) -> str:
    """获取排行榜文本"""
    with _db_lock:
        with _soup_get_db() as conn:
            rows = conn.execute(
                "SELECT nickname, solved_count FROM player_stats "
                "ORDER BY solved_count DESC, updated_at ASC LIMIT ?",
                (top_n,),
            ).fetchall()
    if not rows:
        return ""
    lines = ["", "🏆 猜中排行榜："]
    for i, (nick, count) in enumerate(rows, 1):
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(f"  {medal} {nick} ({count}题)")
    return "\n".join(lines)


def _get_recently_used(group_id: int, window_hours: int = 72) -> set:
    """获取最近 N 小时内用过的汤 ID（避免重复出题）"""
    used = set()
    cutoff = time.time() - window_hours * 3600
    with _db_lock:
        with _soup_get_db() as conn:
            rows = conn.execute(
                "SELECT soup_id FROM used_soups WHERE group_id = ? AND used_at >= ?",
                (group_id, cutoff),
            ).fetchall()
    for row in rows:
        used.add(row[0])
    return used


# ============================================================
#  游戏状态管理
# ============================================================
# {group_id: game_state}
_SOUP_GAMES: dict[int, dict] = {}

# 外部函数：获取用户昵称（由 bot.py 注册）
_get_nickname_fn: Optional[callable] = None  # type: ignore


def register_get_nickname(func: callable):  # type: ignore
    """注册获取用户昵称的回调函数"""
    global _get_nickname_fn
    _get_nickname_fn = func


def _get_nickname(user_id: int, group_id: int) -> str:
    """获取用户昵称"""
    if _get_nickname_fn is not None:
        try:
            nickname = _get_nickname_fn(user_id, group_id)
            if nickname:
                return nickname
        except Exception:
            pass
    return f"用户{user_id}"


def _get_game(group_id: int) -> dict | None:
    return _SOUP_GAMES.get(group_id)


def _set_game(group_id: int, game: dict):
    _SOUP_GAMES[group_id] = game


def _end_game(group_id: int):
    if group_id in _SOUP_GAMES:
        game = _SOUP_GAMES[group_id]
        soup = game.get("soup", {})
        solved = game.get("solved", False)
        # 记录已使用的汤底（防止重复出题）
        _record_used_soup(soup.get("id", 0), group_id, solved)
        del _SOUP_GAMES[group_id]


def is_active(group_id: int) -> bool:
    """检查该群是否有活跃的海龟汤游戏"""
    game = _get_game(group_id)
    return game is not None and game.get("active", False)


# ============================================================
#  出题
# ============================================================
def _choose_soup(group_id: int) -> dict | None:
    """选择一题海龟汤（永久不重复，全部出过才重置）"""
    if not _SOUPS:
        return None

    # 获取该群历史上所有用过的汤ID
    all_used = set()
    with _db_lock:
        with _soup_get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT soup_id FROM used_soups WHERE group_id = ?",
                (group_id,),
            ).fetchall()
    for row in rows:
        all_used.add(row[0])

    # 过滤掉出过的
    candidates = [s for s in _SOUPS if s["id"] not in all_used]
    if not candidates:
        # 全部出过一轮，重置记录重新开始
        with _db_lock:
            with _soup_get_db() as conn:
                conn.execute(
                    "DELETE FROM used_soups WHERE group_id = ?",
                    (group_id,),
                )
        candidates = _SOUPS

    return random.choice(candidates)


def start_game(group_id: int, creator_id: int = 0) -> str | None:
    """
    开始一局海龟汤游戏。
    返回汤面文本，失败返回 None。
    """
    if is_active(group_id):
        return "🐢 当前正在进行海龟汤游戏哦～猜完这局再开新的吧！"

    soup = _choose_soup(group_id)
    if not soup:
        return "😵 题库为空，无法出题"

    nickname = _get_nickname(creator_id, group_id)

    game = {
        "soup": soup,
        "active": True,
        "creator_id": creator_id,
        "creator_nickname": nickname,
        "questions": [],  # [{"user_id", "nickname", "question", "answer", "timestamp"}]
        "hints_given": 0,
        "guess_history": [],  # 猜测历史记录
        "solved": False,
        "solver_id": None,
        "solver_nickname": None,
        "start_time": time.time(),
        "explanation_enabled": False,  # 解释开关，默认关闭
    }
    _set_game(group_id, game)

    # 构建汤面消息
    surface_text = _format_surface(soup, nickname)
    return surface_text


def _format_surface(soup: dict, creator: str) -> str:
    """格式化汤面（谜面）"""
    lines = [
        "🐢═══════════════════🐢",
        "  海 龟 汤 游 戏 开 始",
        "🐢═══════════════════🐢",
        "",
        "📖 汤面：",
        f"  {soup.get('surface', '（无汤面）')}",
        "",
        "💡 请大家 @我 进行提问",
        "   只能问可以用「是」或「否」回答的问题哦！",
        "",
        "📌 指令速查：",
        "   /整理线索 — 查看已确认的线索",
        "   /提示 — 获得一条提示",
        "   /结束 — 手动结束当前游戏",
        "   /答案 — 公布汤底（投降）",
        "   /海龟汤 — 开始新的一局",
        "   /提交答案 [你的推理] — 尝试猜中汤底",
        "   /开启解释 — 开启回答后的解释说明",
        "   /关闭解释 — 关闭回答后的解释说明",
        "",
        f"— 由 {creator} 发起 —",
    ]
    return "\n".join(lines)


# ============================================================
#  LLM 判定引擎
# ============================================================
def _build_judge_prompt(soup: dict, question: str) -> str:
    """构建 LLM 判定 prompt"""
    key_facts_text = "\n".join(f"- {fact}" for fact in soup.get("key_facts", []))
    return (
        "你是一个严谨、理性的海龟汤游戏主持人。你掌握故事的完整真相（汤底）。\n"
        "你的任务是判断玩家的【是/否问题】与汤底真相的契合度。\n\n"
        "【汤底真相】\n"
        f"{soup.get('truth', '（无汤底）')}\n\n"
        "【关键事实清单】\n"
        f"{key_facts_text}\n\n"
        f"【玩家问题】\n{question}\n\n"
        "【判定规则】\n"
        "1. 是：问题的核心假设在汤底中为真。\n"
        "2. 不是：问题的核心假设在汤底中为假，或前提错误、违背常识。\n"
        "   - ⚠️ 生死常识：如果汤面/汤底明确某人" + '"已死"' + "，玩家问" + '"他活着吗/他是活人吗/死者是活的吗"' + "，必须回答" + '"不是"' + "！绝不允许用" + '"他死前是活的"' + "这种诡辩来回答" + '"是"' + "！\n"
        "   - ⚠️ 字面判定：不要试图帮玩家" + '"合理化"' + "语病或逻辑矛盾的问题。只要字面意思违背当前事实，直接判" + '"不是"' + "。\n"
        "3. 无关：问题涉及的元素在汤底中完全未提及且对核心诡计毫无影响（如问天气、路人甲）。\n"
        "4. 宽容原则：玩家表述可能口语化，只要核心指向汤底中的某个事实，就判为" + "'是'" + "或" + "'不是'" + "。\n\n"
        "【输出要求】\n"
        "必须且只能输出一个合法的 JSON 对象，绝对不要包含任何 Markdown 标记（如 ```json）、解释性文字或前言后语。\n"
        "JSON 格式如下：\n"
        "{\n"
        '  "answer": "是", // 只能填 "是"、"不是" 或 "无关"\n'
        '  "explanation": "不超过15个字的侧面解释。如果是' + '"不是"' + '，直接指出矛盾点，绝对禁止剧透！"\n'
        "}\n"
    )


async def _judge_question(soup: dict, question: str) -> tuple[str, str]:
    """
    调用 LLM 判定玩家问题。
    返回 (answer, explanation) 其中 answer 为 '是'/'不是'/'无关'
    """
    prompt = _build_judge_prompt(soup, question)

    try:
        response = await call_llm(
            messages=[
                {"role": "system", "content": "你是一个严谨、理性的海龟汤游戏主持人。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=16384,
            use_lock=True,
            lock_type="chat",
            timeout=120,  # 交互路径短超时：消息循环串行，长超时会让全 bot 静默阻塞
        )

        # 调试：打印 LLM 原始返回
        logger.info(f"🐢 LLM 原始返回 ({len(response)} chars): {response[:300]}")

        # 首尾括号匹配法 — 比非贪婪正则更稳健
        response = response.strip()
        start_idx = response.find("{")
        end_idx = response.rfind("}")
        logger.info(f"🐢 JSON 提取: start={start_idx}, end={end_idx}")
        answer: str = "无关"
        explanation: str = ""

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = response[start_idx:end_idx + 1]
            try:
                data = json.loads(json_str)
                answer = data.get("answer", "无关")
                explanation = data.get("explanation", "")
                # 归一化 answer 字段
                answer_lower = str(answer).lower().strip()
                if answer_lower in ("是", "是的", "对", "正确", "yes"):
                    answer = "是"
                elif answer_lower in ("不是", "否", "不对", "错误", "no"):
                    answer = "不是"
                else:
                    answer = "无关"
            except json.JSONDecodeError:
                pass

        if answer == "无关" and not explanation:
            # 兜底：根据关键词判断
            # M6 修复：必须先判"不是"再判"是"——"不是"包含"是"，
            # 原顺序会把"不是。因为……"的否定判定翻转为肯定
            lower = response.lower()
            if "不是" in lower[:10] or "否" in lower[:10]:
                answer = "不是"
            elif "是" in lower[:10]:
                answer = "是"

        return answer, explanation

    except Exception as e:
        logger.error(f"🐢 LLM 判定失败: {e}")
        return "无关", "（判定超时，请换个角度提问）"


# ============================================================
#  回答问题（玩家 @Bot 提问）
# ============================================================
async def handle_question(group_id: int, user_id: int, question: str, reply_id: Optional[int] = None) -> tuple[str, Optional[int]] | None:
    """
    处理玩家的是/否提问。
    返回 (回复文本, reply_id) 或 None（非活跃游戏）
    """
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    if game.get("solved"):
        return None

    soup = game["soup"]
    nickname = _get_nickname(user_id, group_id)

    # LLM 总开关早退（2026-08-21 审计）：LLM 关闭时判定引擎不可用，
    # 直接明确告知（避免降级串解析失败后把每个问题都答成"无关"误导玩家）
    if not llm_enabled():
        return (f"🔕 @{nickname} LLM 总开关关闭，暂时无法判定问题（GUI 总览页 LLM 板块可开启）。", reply_id)

    # 调用 LLM 判定
    answer, explanation = await _judge_question(soup, question)

    # 记录问题
    q_record = {
        "user_id": user_id,
        "nickname": nickname,
        "question": question,
        "answer": answer,
        "timestamp": time.time(),
    }
    game["questions"].append(q_record)

    # 构建回复
    reply = _format_answer(answer, explanation, question, game.get("explanation_enabled", False))

    return reply, reply_id


def _format_answer(answer: str, explanation: str, question: str, explanation_enabled: bool = False) -> str:
    """格式化回答 — 严谨风格，固定用语"""
    if answer == "是":
        prefix = "是"
    elif answer == "不是":
        prefix = "不是"
    else:
        prefix = "无关"

    if explanation_enabled and explanation:
        return f"问题：{question} 回答：{prefix}。{explanation}"
    return f"问题：{question} 回答：{prefix}。"


# ============================================================
#  猜中判断
# ============================================================
async def handle_guess(group_id: int, user_id: int, nickname: str, guess_text: str, reply_id: Optional[int] = None) -> tuple[str, Optional[int]] | None:
    """
    处理玩家猜测汤底。
    判定分级：完全正确 / 接近（还需更多线索） / 偏差较大
    每位玩家只有 2 次提交机会，用完则不再接受。
    """
    game = _get_game(group_id)
    if not game or not game.get("active") or game.get("solved"):
        return None

    # 每位玩家只有 2 次提交汤底的机会
    guess_history = game.get("guess_history", [])
    user_guesses = [g for g in guess_history if g["user_id"] == user_id]
    if len(user_guesses) >= 2:
        return (f"🐢 @{nickname} 你已经用完了 2 次提交机会，不能再提交汤底了。可以多看别人的推理，或者下一局再试！", None)

    soup = game["soup"]

    # LLM 总开关早退（2026-08-21 审计）：猜中判定依赖 LLM，关闭时明确告知
    # （避免降级串解析失败后所有猜测都被判成"方向偏差"误导玩家）
    if not llm_enabled():
        return (f"🔕 @{nickname} LLM 总开关关闭，暂时无法判定猜测（GUI 总览页 LLM 板块可开启）。", None)

    # 调用 LLM 判断猜测是否命中核心真相
    key_facts_text = "\n".join(f"- {fact}" for fact in soup.get("key_facts", []))
    total_facts = len(soup.get("key_facts", []))
    guess_prompt = (
        "你是一个海龟汤游戏主持人。玩家正在尝试还原故事的完整真相（汤底）。\n\n"
        "【汤底真相】\n"
        f"{soup['truth']}\n\n"
        "【关键事实清单】（共 {total_facts} 条）\n"
        f"{key_facts_text}\n\n"
        f"【玩家猜测】\n{guess_text}\n\n"
        "【判定标准】（严格遵守！）\n"
        "1. correct（通关）：\n"
        "   - ⚠️ 核心诡计一票通过制：只要玩家点破了故事的【核心诡计】、【最大反转点】或【根本动机】，即使遗漏了次要细节（如人名、时间、非关键物品），或表述口语化，必须判为 correct！\n"
        "   - 绝不要求玩家逐字还原所有关键事实。\n"
        "2. near（接近真相）：\n"
        "   - 玩家猜对了大方向，但【完全没有触及核心诡计/反转点】。\n"
        "   - ⚠️ 严禁使用“另有隐情”、“还差一点”、“水很深”等谜语人废话！必须指出玩家忽略了哪个维度的线索（如：注意死者的职业/注意那碗汤的成分）。\n"
        "3. wrong（偏差较大）：\n"
        "   - 玩家的推理与核心真相南辕北辙，或仅仅是表面现象的罗列。\n\n"
        "【输出要求】\n"
        "必须且只能输出一个合法的 JSON 对象，绝对不要包含 Markdown 标记（如 ```json）或任何额外文字。\n"
        "JSON 格式如下：\n"
        "{\n"
        '  "level": "correct", // 只能填 "correct", "near" 或 "wrong"\n'
        '  "reason": "不超过20个字的评价。如果是 near 或 wrong，请给出方向性引导，绝对禁止直接剧透真相！",\n'
        f'  "matched": {total_facts} // 0到{total_facts}之间的整数，表示玩家猜测覆盖了多少条关键事实\n'
        "}"
    )

    try:
        response = await call_llm(
            messages=[
                {"role": "system", "content": "你是一个海龟汤游戏主持人。"},
                {"role": "user", "content": guess_prompt},
            ],
            max_tokens=16384,
            use_lock=True,
            lock_type="chat",
            timeout=120,  # 交互路径短超时：消息循环串行，长超时会让全 bot 静默阻塞
        )

        # 首尾括号匹配法 — 比非贪婪正则更稳健
        response = response.strip()
        start_idx = response.find("{")
        end_idx = response.rfind("}")
        level: str = "wrong"
        reason: str = ""
        matched: int = 0

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = response[start_idx:end_idx + 1]
            try:
                data = json.loads(json_str)
                level = data.get("level", "wrong")
                reason = data.get("reason", "")
                matched = data.get("matched", 0)
                # 归一化 level 字段
                level_lower = str(level).lower().strip()
                if level_lower in ("correct", "正确", "对", "yes", "猜中"):
                    level = "correct"
                elif level_lower in ("near", "接近", "差不多", "almost"):
                    level = "near"
                else:
                    level = "wrong"
            except json.JSONDecodeError:
                pass

    except Exception as e:
        logger.error(f"🐢 猜中判定失败: {e}")
        level = "wrong"
        reason = ""
        matched = 0

    # 记录猜测历史（保留记录，不限制次数）
    if "guess_history" not in game:
        game["guess_history"] = []
    game["guess_history"].append({
        "user_id": user_id,
        "nickname": nickname,
        "guess": guess_text,
        "level": level,
        "matched": matched,
        "timestamp": time.time(),
    })

    # 判定结果处理
    if level == "correct":
        # 完全猜中！
        game["solved"] = True
        game["solver_id"] = user_id
        game["solver_nickname"] = nickname
        # 记录玩家成绩
        _record_player_solve(user_id, nickname)
        solved_msg = _format_solved(nickname, soup)
        # 清除游戏状态（_end_game 内部会记录已用汤底）
        _end_game(group_id)
        return solved_msg, reply_id

    elif level == "near":
        # 接近了，给鼓励提示
        progress = f"（已命中 {matched}/{len(soup.get('key_facts', []))} 条关键线索，再想想！）"
        return f"🐢 @{nickname} 很接近了！{reason if reason else '方向对了'} {progress}", reply_id

    else:
        # 偏差较大
        return f"🐢 @{nickname} 方向偏差有点大～{reason if reason else '换个角度想想'}", reply_id


def _format_solved(nickname: str, soup: dict) -> str:
    """格式化猜中消息"""
    lines = [
        "🎊═══════════════════🎊",
        "  🎉 猜 中 了 ！🎉",
        "🎊═══════════════════🎊",
        "",
        f"🏆 恭喜 @{nickname} 还原了故事的真相！",
        "",
        "📖 完整汤底：",
        f"  {soup['truth']}",
        "",
        "👏 精彩推理！",
    ]
    # 追加排行榜
    lb = _get_leaderboard()
    if lb:
        lines.append(lb)
    lines.extend(["", "发送 /海龟汤 开始新的一局～"])
    return "\n".join(lines)


# ============================================================
#  整理线索
# ============================================================
async def get_clues(group_id: int) -> str | None:
    """
    整理并返回所有已确认为"是"的线索。
    返回格式化文本，无活跃游戏返回 None。
    """
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    yes_questions = [
        q for q in game["questions"] if q["answer"] == "是"
    ]

    if not yes_questions:
        return "🐢 目前还没有确认的线索，大家继续提问吧～"

    # LLM 总开关早退（2026-08-21 审计）：LLM 关闭时直接列出已确认问题，
    # 不走 LLM 整理（避免把降级串当线索文本发群）
    if not llm_enabled():
        lines = ["🔍 目前确认的线索（LLM 关闭，未整理分类）："]
        for q in yes_questions:
            lines.append(f"  ✓ {q['question']}（@{q['nickname']}）")
        lines.append("")
        lines.append(f"共 {len(yes_questions)} 条已确认线索")
        return "\n".join(lines)

    # 调用 LLM 整理线索为流畅的文本
    questions_text = "\n".join(
        f"- {q['question']}（@{q['nickname']}）"
        for q in yes_questions
    )

    organize_prompt = (
        "你是海龟汤游戏主持人。请将以下玩家提问中回答为'是'的记录，整理成一份清晰的线索板。\n\n"
        "【已确认的问题】\n"
        f"{questions_text}\n\n"
        "【整理规则】\n"
        "1. 将疑问句转换为肯定的陈述句（例如：'他是盲人吗？' -> '主角是盲人'）。\n"
        "2. 对线索进行逻辑分类（如：人物身份、关键物品、核心行为、环境背景等）。\n"
        "3. 严禁脑补！严禁添加任何【已确认的问题】中未提及的信息！严禁剧透汤底！\n"
        "4. 语言要简洁、悬疑，符合海龟汤游戏的氛围。\n\n"
        "【输出格式】（直接输出最终文本，无需 JSON）\n"
        "🔍 已确认线索：\n"
        "【分类名称】\n"
        "1. ...\n"
        "2. ...\n"
    )

    try:
        response = await call_llm(
            messages=[
                {"role": "system", "content": "你是一个海龟汤游戏主持人，正在帮助玩家整理已确认的线索。"},
                {"role": "user", "content": organize_prompt},
            ],
            max_tokens=16384,
            use_lock=True,
            lock_type="chat",
            timeout=120,  # 交互路径短超时：消息循环串行，长超时会让全 bot 静默阻塞
        )

        # 清理响应
        response = response.strip()
        if response.startswith("🔍"):
            return response
        return f"🔍 已确认线索：\n{response}"

    except Exception as e:
        logger.error(f"🐢 整理线索失败: {e}")
        # 降级：直接列出问题
        lines = ["🔍 目前确认的线索："]
        for q in yes_questions:
            lines.append(f"  ✓ {q['question']}（@{q['nickname']}）")
        lines.append("")
        lines.append(f"共 {len(yes_questions)} 条已确认线索")
        return "\n".join(lines)


# ============================================================
#  提示
# ============================================================
def get_hint(group_id: int) -> str | None:
    """
    给出一条提示。
    返回提示文本，无活跃游戏返回 None。
    """
    game = _get_game(group_id)
    if not game or not game.get("active") or game.get("solved"):
        return None

    soup = game["soup"]
    hints = soup.get("hints", [])

    if not hints:
        return "🐢 这局暂无额外提示，大家自由发挥吧～"

    # 给出一条还没给过的提示
    available = hints[game["hints_given"]:]
    if not available:
        return "🐢 所有提示都用完了，再努力想想吧～"

    hint = available[0]
    game["hints_given"] += 1

    return f"💡 主持人提示：\n{hint}\n\n（剩余 {len(hints) - game['hints_given']} 条提示）"


# ============================================================
#  公布答案
# ============================================================
def reveal_answer(group_id: int) -> str | None:
    """
    公布汤底（投降模式）。
    返回汤底文本，无活跃游戏返回 None。
    """
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    soup = game["soup"]

    # 游戏结束（_end_game 内部会自动记录已用汤底）
    _end_game(group_id)

    lines = [
        "🥣═══════════════════🥣",
        "  揭 锅 时 间",
        "🥣═══════════════════🥣",
        "",
        "📖 完整汤底：",
        f"  {soup['truth']}",
        "",
        "🐢 游戏结束～",
        "",
        "发送 /海龟汤 开始新的一局！",
    ]
    return "\n".join(lines)


# ============================================================
#  游戏状态查询
# ============================================================
def get_game_status(group_id: int) -> str | None:
    """查询当前游戏状态"""
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return "🐢 当前没有进行中的海龟汤游戏\n发送 /海龟汤 开始一局吧～"

    soup = game["soup"]
    elapsed = int(time.time() - game["start_time"])
    minutes = elapsed // 60
    questions_count = len(game["questions"])
    yes_count = sum(1 for q in game["questions"] if q["answer"] == "是")
    no_count = sum(1 for q in game["questions"] if q["answer"] == "不是")
    irrelevant_count = sum(1 for q in game["questions"] if q["answer"] == "无关")
    hints_remaining = len(soup.get("hints", [])) - game["hints_given"]

    lines = [
        f"🐢 当前海龟汤：{soup['title']}",
        f"⏱️ 已进行 {minutes} 分钟",
        f"❓ 共提问 {questions_count} 次（是{yes_count} / 不是{no_count} / 无关{irrelevant_count}）",
        f"💡 剩余 {hints_remaining} 条提示",
        f"👤 发起者：{game['creator_nickname']}",
        "",
        "发送 /整理线索 查看已确认线索",
        "发送 /提示 获得提示",
        "发送 /结束 手动结束游戏",
        "发送 /答案 公布汤底",
    ]
    return "\n".join(lines)




async def check_command(text: str, group_id: int = 0, user_id: int = 0, nickname: str = "") -> str | None:
    """
    检查是否是海龟汤指令。
    是则返回回复文本，不是返回 None。
    """
    text = text.strip()

    # /海龟汤 — 开始新游戏
    if text == "/海龟汤":
        result = start_game(group_id, user_id)
        return result

    # /海龟汤状态
    if text == "/海龟汤状态":
        return get_game_status(group_id)

    # /整理线索
    if text == "/整理线索":
        if not is_active(group_id):
            return "🐢 当前没有正在进行的海龟汤游戏，先发送 /海龟汤 开始一局吧～"
        result = await get_clues(group_id)
        return result

    # /提示
    if text == "/提示":
        if not is_active(group_id):
            return "🐢 当前没有正在进行的海龟汤游戏，先发送 /海龟汤 开始一局吧～"
        result = get_hint(group_id)
        return result

    # /答案
    if text == "/答案":
        if is_active(group_id):
            return reveal_answer(group_id)

    # /结束 — 手动结束当前游戏
    if text == "/结束":
        if not is_active(group_id):
            return None  # 放行给 router 兜底
        game = _get_game(group_id)  # type: ignore[misc]
        soup = game["soup"]
        _end_game(group_id)
        return (
            f"🐢 海龟汤游戏已结束！\n"
            f"📖 汤面：{soup['title']}\n"
            f"💡 汤底：{soup['truth']}"
        )

    # /提交答案 [推理内容]
    if text.startswith("/提交答案"):
        guess_text = text[len("/提交答案"):].strip()
        if not guess_text:
            return "🐢 请在 /提交答案 后面写上你的推理哦～"
        if not is_active(group_id):
            return "🐢 请先发送 /海龟汤 开始游戏。"
        result = await handle_guess(group_id, user_id, nickname, guess_text)
        if result:
            return result[0]
        return "🐢 回答被吞了，再试一次？"

    # /开启解释
    if text == "/开启解释":
        if not is_active(group_id):
            return "🐢 当前没有正在进行的海龟汤游戏，先发送 /海龟汤 开始一局吧～"
        game = _get_game(group_id)
        game["explanation_enabled"] = True
        return "🐢 已开启解释模式。回答将附带简短解释。"

    # /关闭解释
    if text == "/关闭解释":
        if not is_active(group_id):
            return "🐢 当前没有正在进行的海龟汤游戏，先发送 /海龟汤 开始一局吧～"
        game = _get_game(group_id)
        game["explanation_enabled"] = False
        return "🐢 已关闭解释模式。回答将只显示判定结果。"

    return None
