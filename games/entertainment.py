#!/usr/bin/env python3
"""
QQ 群机器人 - 真心话大冒险核心模块
包含：TD 状态管理、核心游戏逻辑、命令路由
"""

import random
import time
import re
import sqlite3
import asyncio
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from .question_pool import (
    TRUTH_QUESTIONS, DARE_QUESTIONS,
    TD_HISTORY_DB, TRUTH_FILE, DARE_FILE, _get_db,
    _clear_history, _get_history_count, _get_done_questions, _record_question,
    _append_question, _background_refill_all, _load_questions,
    _init_td_history_db, _init_auto_questions_db, _init_self_select_db,
    list_self_select, handle_suggest_question, handle_confirm_suggestion,
    handle_spiciness,
    _parse_self_select_questions, _save_self_select,
    _record_auto_question,
    _ensure_player_pool, _get_or_pop_question, _get_persona_nickname,
    _td_get, _td_dare_probability,
)
from .mini_games import (
    handle_dice, handle_rps, handle_card, handle_fortune,
    handle_riddle, handle_answer_riddle, handle_song, handle_help,
    COMMANDS, ALIASES,
)

# 模块初始化
_init_td_history_db()
_init_auto_questions_db()
_init_self_select_db()

# ============ 自动模式行为参数（2026-08-21 起读 config.yaml truth_dare.game 段）============
# 单一事实源：core/config.py DEFAULTS["truth_dare"]；各调用点每次实时取值 → 热加载生效。
# GUI「🎮 自动模式」弹窗管理。

def _td_default_spiciness() -> int:
    """新游戏开局默认色度档位（0-6，越界自动钳制）"""
    try:
        return max(0, min(6, int(_td_get("game", "default_spiciness", 4))))
    except Exception:
        return 4


def _td_kick_threshold() -> int:
    """自动踢人阈值：连续 N 轮被抽到未回答则请出游戏"""
    try:
        return max(1, int(_td_get("game", "auto_kick_threshold", 2)))
    except Exception:
        return 2


def _td_bg_delay() -> float:
    """/下一轮 后延迟 N 秒发 AI 出题消息（等骰子消息先被服务器处理）"""
    try:
        return max(0.0, float(_td_get("game", "bg_delay_seconds", 1)))
    except Exception:
        return 1.0

# 真心话大冒险游戏状态
_TRUTH_DARE_GAMES: dict[int, dict] = {}

# 骰子表情映射
DICE_EMOJI = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}

def handle_auto_mode(group_id: int, user_id: int) -> tuple[Optional[str], list[int]]:
    """切换到自动模式"""
    return (set_display_mode("auto", group_id), [])

def _roll_dice(dice_count: int, max_face: int = 6) -> tuple[int, list[int]]:
    """投指定数量骰子，返回 (总点数, 各骰子点数列表)"""
    rolls = [random.randint(1, max_face) for _ in range(dice_count)]
    return sum(rolls), rolls

def _get_auto_dice_count(player_count: int) -> int:
    """根据人数自动计算骰子数量"""
    if player_count >= 10:
        return 1
    if player_count >= 8:
        return 2
    if player_count >= 6:
        return 3
    if player_count >= 4:
        return 4
    return 5

def is_td_active(group_id: int) -> bool:
    """真心话大冒险游戏是否正在该群进行（供拍一拍触发 /下一轮 等外部调用）"""
    return _TRUTH_DARE_GAMES.get(group_id) is not None


def start_truth_dare(game_type: str, user_id: int, nickname: str, group_id: int) -> Optional[str]:
    """开始真心话/大冒险/真心话大冒险游戏"""
    if game_type == "truth":
        game_type_cn = "真心话"
    elif game_type == "dare":
        game_type_cn = "大冒险"
    else:
        game_type_cn = "真心话大冒险"

    if group_id in _TRUTH_DARE_GAMES:
        return f"🎮 本群正在进行 {game_type_cn} 游戏，请使用 /加入 加入或 /结束 结束当前游戏"

    _TRUTH_DARE_GAMES[group_id] = {
        "type": game_type,
        "players": [{"id": user_id, "name": nickname}],
        "all_player_ids": {user_id},
        "rolls": {},
        "round": 1,
        "started_at": time.time(),
        "ask_counts": {},
        "answer_counts": {},
        "recently_asked": {},
        "dice_count": _get_auto_dice_count(1),
        "manual_dice_count": False,  # True = 用户手动设定，不自动调整
        "display_mode": "auto",  # "auto" = 自动模式(AI出题), "full" = 完整模式, "simplified" = 简化模式, "custom" = 自选模式
        "manual_display_mode": False,  # True = 用户手动切换过模式，不随人数自动切换
        "unanswered_counts": {},  # {user_id: count} 被抽到但未回答的次数
        "current_targets": [],  # 当前轮次被抽到的玩家 ID 列表
        "dice_max": {user_id: 6},  # {user_id: max_face} 每个用户骰子的最大点数（默认6）
        "_group_id": group_id,  # 绑定群号，供后台线程使用
    }

    if game_type == "mixed":
        return (
            f"🎮 {game_type_cn} 游戏开始！（第 1 轮）\n"
            f"👑 发起人：{nickname}\n"
            f"👥 当前玩家：1 人\n"
            f"🤖 默认自动模式：AI 根据输家画像出题\n"
            f"🎲 每次抽题有 {int((1-_td_dare_probability())*100)}% 概率是真心话，{int(_td_dare_probability()*100)}% 概率是大冒险\n\n"
            f"📋 核心指令：\n"
            f"  /加入 — 加入游戏\n"
            f"  /下一轮 — 投骰子 + AI 自动出题\n"
            f"  /色色程度 [0-6] — 调整 AI 出题尺度\n"
            f"  /退出 — 退出游戏  |  /结束 — 结束整局\n\n"
            f"💡 发送「/帮助」查看所有指令"
        )
    return (
        f"🎮 {game_type_cn} 游戏开始！（第 1 轮）\n"
        f"👑 发起人：{nickname}\n"
        f"👥 当前玩家：1 人\n"
        f"🤖 默认自动模式：AI 根据输家画像出题\n\n"
        f"📋 核心指令：\n"
        f"  /加入 — 加入游戏\n"
        f"  /下一轮 — 投骰子 + AI 自动出题\n"
        f"  /色色程度 [0-6] — 调整 AI 出题尺度\n"
        f"  /退出 — 退出游戏  |  /结束 — 结束整局\n\n"
        f"💡 发送「/帮助」查看所有指令"
    )

def join_truth_dare(user_id: int, nickname: str, group_id: int) -> Optional[str]:
    """加入真心话大冒险游戏"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏，发送 /真心话 或 /大冒险 开始游戏"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # 检查是否已加入（用 id 判断）
    if any(p["id"] == user_id for p in game["players"]):
        return f"✅ 你已经加入 {game_type_cn} 游戏了，等待其他人加入即可"

    # 自选模式人数上限 7 人（从其他模式切换到自选模式时允许已存在超过 7 人，但不再接受新加入）
    if game.get("display_mode") == "custom" and len(game["players"]) >= 7:
        return f"🚫 自选模式人数已达上限（7 人），当前 {len(game['players'])} 人，暂不接受新加入"

    game["players"].append({"id": user_id, "name": nickname})
    game["all_player_ids"].add(user_id)
    # 新玩家默认骰子最大点数为 6
    dice_max = game.get("dice_max", {})
    if user_id not in dice_max:
        dice_max[user_id] = 6
        game["dice_max"] = dice_max

    # 后台静默为该玩家预填充题目池（真心话+大冒险）— 异步线程，不阻塞加入响应
    from core.persona import get_active_persona, persona_to_text
    active_persona = get_active_persona(user_id, group_id)
    persona_text = persona_to_text(active_persona) if active_persona else ""
    gender = active_persona.get("gender") if active_persona else None
    spiciness = game.get("spiciness", _td_default_spiciness())
    threading.Thread(
        target=_ensure_player_pool,
        args=(user_id, group_id, nickname, persona_text, spiciness, gender),
        daemon=True,
    ).start()

    player_count = len(game["players"])

    # 人数变化时自动调整骰子数量（除非用户手动设定过）
    if not game.get("manual_dice_count", False):
        auto_dice = _get_auto_dice_count(player_count)
        if auto_dice != game.get("dice_count", 5):
            game["dice_count"] = auto_dice

    # 人数超过 5 人时自动切换简化模式（用户手动设定过则不自动切换，auto 模式也不切换）
    mode_changed = False
    if player_count > 5 and not game.get("manual_display_mode", False) and game.get("display_mode") not in ("simplified", "auto"):
        game["display_mode"] = "simplified"
        mode_changed = True

    # 满 2 人后不自动投骰，提示主持人手动触发
    if player_count >= 2:
        mode_hint = ""
        if mode_changed:
            mode_hint = "\n🔇 人数较多，已自动切换简化模式（/完整模式 可切回）"
        return (
            f"✅ {nickname} 加入了 {game_type_cn} 游戏！\n"
            f"👥 当前玩家：{player_count} 人\n"
            f"🎲 骰子数量：{game.get('dice_count', 5)} 个\n"
            f"💡 已满足人数，发送「/骰」投骰子开始游戏，或「/下一轮」自动投骰 + 抽题{mode_hint}"
        )
    else:
        return f"✅ {nickname} 加入了 {game_type_cn} 游戏！\n👥 当前玩家：{player_count} 人（还需要至少 1 人即可开始）"

def leave_truth_dare(user_id: int, nickname: str, group_id: int) -> Optional[str]:
    """退出真心话大冒险游戏（仅自己退出）"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # 检查是否在玩家列表中
    player = next((p for p in game["players"] if p["id"] == user_id), None)
    if not player:
        return f"✅ 你还没有加入 {game_type_cn} 游戏"

    game["players"].remove(player)
    # all_player_ids 保留所有参与过的玩家（包括退出的），供 end_game 统计使用

    # 如果该玩家是当前 max/min，清除
    game.pop("max_players", None)
    game.pop("min_players", None)
    game.pop("all_same_score", None)

    # 清除该玩家的投骰记录
    game["rolls"].pop(user_id, None)

    # 清除该玩家的未回答计数
    game["unanswered_counts"].pop(user_id, None)
    # 从当前轮次目标中移除
    if user_id in game.get("current_targets", []):
        game["current_targets"].remove(user_id)

    # 清除该玩家的骰子最大点数设置
    game.get("dice_max", {}).pop(user_id, None)

    player_count = len(game["players"])

    # 人数变化时自动调整骰子数量（除非用户手动设定过）
    if not game.get("manual_dice_count", False):
        auto_dice = _get_auto_dice_count(player_count)
        if auto_dice != game.get("dice_count", 5):
            game["dice_count"] = auto_dice

    # 人数减少到 6 人以下时自动切换完整模式（用户手动设定过则不自动切换）
    if player_count <= 5 and not game.get("manual_display_mode", False) and game.get("display_mode") == "simplified":
        game["display_mode"] = "full"

    # 只剩 1 人或 0 人时，自动结束游戏
    if player_count < 2:
        end_msg = end_game(group_id)
        return f"🚪 {nickname} 退出了游戏\n{end_msg}"

    return (
        f"🚪 {nickname} 退出了 {game_type_cn} 游戏\n"
        f"👥 剩余玩家：{player_count} 人\n"
        f"🎲 骰子数量：{game.get('dice_count', 5)} 个"
    )

def kick_player(arg: str, group_id: int, kicker_id: int, kicker_name: str) -> tuple[Optional[str], list[int]]:
    """
    踢出玩家（手动指令 /踢人 <昵称或QQ号>）。
    返回 (result_str, at_user_ids)
    """
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏", []

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")
    arg = arg.strip()

    if not arg:
        return (
            "💡 用法：/踢人 <昵称或QQ号>\n"
            f"   例如：/踢人 小明\n"
            f"   例如：/踢人 123456789",
            [],
        )

    # 尝试按 QQ 号精确匹配
    kicked: dict | None = None
    if arg.isdigit():
        qq_num = int(arg)
        for p in game["players"]:
            if p["id"] == qq_num:
                kicked = p
                break

    # 如果 QQ 号没找到，按昵称模糊匹配
    if not kicked:
        for p in game["players"]:
            if arg in p["name"] or p["name"] in arg:
                kicked = p
                break

    if not kicked:
        return f"❌ 没有找到玩家「{arg}」，请确认昵称或QQ号是否正确", []

    # 不能踢自己
    if kicked["id"] == kicker_id:
        return "❌ 不能踢自己～", []

    kicked_name = kicked["name"]
    kicked_id = kicked["id"]

    # 执行踢人
    game["players"].remove(kicked)
    game["rolls"].pop(kicked_id, None)
    game["unanswered_counts"].pop(kicked_id, None)
    if kicked_id in game.get("current_targets", []):
        game["current_targets"].remove(kicked_id)
    # 清除被踢玩家的骰子最大点数设置
    game.get("dice_max", {}).pop(kicked_id, None)

    # 清除 max/min 缓存
    game.pop("max_players", None)
    game.pop("min_players", None)
    game.pop("all_same_score", None)

    # 人数变化时自动调整骰子数量
    player_count = len(game["players"])
    if not game.get("manual_dice_count", False):
        auto_dice = _get_auto_dice_count(player_count)
        if auto_dice != game.get("dice_count", 5):
            game["dice_count"] = auto_dice

    if player_count <= 5 and not game.get("manual_display_mode", False) and game.get("display_mode") == "simplified":
        game["display_mode"] = "full"

    if player_count < 2:
        end_msg = end_game(group_id)
        return f"👢 {kicked_name} 被 {kicker_name} 踢出了游戏\n{end_msg}", [kicked_id]

    return (
        f"👢 {kicked_name} 被 {kicker_name} 踢出了 {game_type_cn} 游戏\n"
        f"👥 剩余玩家：{player_count} 人\n"
        f"🎲 骰子数量：{game.get('dice_count', 5)} 个",
        [kicked_id],
    )

def _get_player_name(game: dict, user_id: int) -> str:
    """获取玩家显示名称（优先群昵称）"""
    for p in game["players"]:
        if p["id"] == user_id:
            return p.get("name", str(user_id))
    # 已退出/被踢的玩家（仍在 ask_counts 等统计中）：查群消息缓存昵称，
    # 避免榜单显示 QQ 号（2026-08-07 修复）；查不到才回退 QQ 号
    try:
        from core.database import _get_user_nickname
        group_id = game.get("_group_id", 0)
        if group_id:
            nickname = _get_user_nickname(user_id, group_id)
            if nickname and str(nickname) != str(user_id):
                return str(nickname)
    except Exception:
        pass
    return str(user_id)

def acknowledge_answer(user_id: int, group_id: int, message_text: str = "") -> tuple[Optional[str], list[int]]:
    """
    玩家发送了任何消息（包括普通发言），视为回答了当前被抽到的问题。
    如果该玩家是当前轮次的被抽到的目标，清除未回答计数。

    自选模式下：记录赢家（max_players）的所有消息，用于后续 LLM 识别提问。

    返回 (reply_text, at_user_ids)，如果有回复（如 /提问建议 数字确认），否则返回 (None, [])。
    """
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return (None, [])

    current_targets = game.get("current_targets", [])
    if user_id in current_targets:
        unanswered = game.get("unanswered_counts", {})
        unanswered.pop(user_id, None)

    # ---- 自选模式：处理 /提问建议 的数字确认（不 @bot 也能触发）----
    # 条件：有 pending_suggestions + 纯数字 1-4 + 是发起建议的赢家 + 是当前轮次赢家
    if (game.get("display_mode") == "custom" and game.get("pending_suggestions")
            and message_text.strip() in ("1", "2", "3", "4")):
        choice = int(message_text.strip())
        winner_uid = game.get("suggestion_winner_uid")
        # 双重守卫：必须是发起建议的赢家 AND 是当前轮次赢家
        max_players = game.get("max_players", [])
        if winner_uid == user_id and user_id in max_players:
            # BUG 修复（2026-08-03）：原实现委托给 question_pool.handle_confirm_suggestion
            # 的 _handle_confirm_impl stub，永远返回占位文本、无任何实际效果。
            # 这里直接实现：取选中的建议 → 保存到自选提问库 → 清理状态。
            suggestions = game.get("pending_suggestions", [])
            if 1 <= choice <= len(suggestions):
                chosen = suggestions[choice - 1]
                min_players = game.get("min_players", [])
                target_uid = min_players[0] if min_players else None
                target_name = _get_player_name(game, target_uid) if target_uid else "—"
                winner_name = _get_player_name(game, user_id)
                try:
                    _save_self_select(
                        group_id=group_id,
                        winner_id=user_id,
                        winner_name=winner_name,
                        target_id=target_uid,
                        target_name=target_name,
                        original_msgs=[chosen],
                        extracted=[chosen],
                        game_type=game.get("type", "mixed"),
                    )
                except Exception as e:
                    logger.error(f"保存自选提问失败: {e}")
                game.pop("pending_suggestions", None)
                game.pop("suggestion_winner_uid", None)
                reply = (
                    f"✅ 已确认提问建议！\n"
                    f"📝 问题：{chosen}\n"
                    f"🎯 请 {target_name} 回答！"
                )
                at_ids = [target_uid] if target_uid else []
                return (reply, at_ids)
            # 建议列表为空/越界 → 清理并提示
            game.pop("pending_suggestions", None)
            game.pop("suggestion_winner_uid", None)
            return ("⚠️ 建议列表已失效，请重新发送 /提问建议", [])

    # ---- 自选模式：收集赢家发言 ----
    if game.get("display_mode") == "custom" and message_text.strip():
        max_players = game.get("max_players", [])
        if user_id in max_players and "max_players" in game:
            # 确保 winner_messages 存在
            if "winner_messages" not in game:
                game["winner_messages"] = {}
            if user_id not in game["winner_messages"]:
                game["winner_messages"][user_id] = []
            game["winner_messages"][user_id].append(message_text.strip())

    return (None, [])

def _get_players_names(game: dict, uids: list[int]) -> str:
    """获取多个玩家的显示名称，用顿号连接"""
    return "、".join(_get_player_name(game, uid) for uid in uids)

def _weighted_pick(questions: list[str], game: dict, round_number: int, target_user_ids: list[int] | None = None, question_type: str = "") -> str:
    """
    加权随机抽取题目。

    分层过滤策略（多人并列时避免重复）：
      第 0 层：所有输家都没做过的题目（最优）
      第 1 层：只有 1 个输家做过的题目
      第 2 层：只有 2 个输家做过的题目
      … 逐级放宽，直到最后一层：所有输家都做过的题目

    最近 30 轮出现过的题目权重降低：
        - 30 轮前（或未出现过）: 权重 100（最高）
        - 1 轮前（刚出现过）:   权重 1（最低）
        - 线性递增：2 轮前=2, 3 轮前=3, …, 29 轮前=29
    """
    # 分层过滤：按"做过该题的输家人数"分组
    if target_user_ids and question_type:
        per_user_done: dict[int, set[str]] = {}
        for uid in target_user_ids:
            per_user_done[uid] = _get_done_questions([uid], question_type)

        num_loser = len(target_user_ids)
        layers: dict[int, list[str]] = {i: [] for i in range(num_loser + 1)}
        for q in questions:
            count = sum(1 for uid in target_user_ids if q in per_user_done.get(uid, set()))
            layers[count].append(q)

        # 从第 0 层（没人做过）开始逐级查找，优先使用做过人数最少的层
        available = []
        for i in range(num_loser + 1):
            if layers[i]:
                available = layers[i]
                break

        # 理论上每道题至少分在一层，所以 available 不应该为空
        # 但防御性处理：若确实为空则清空历史重来
        if not available:
            for uid in target_user_ids:
                _clear_history(uid, None)
            available = list(questions)
    else:
        available = list(questions)

    # 加权随机（基于最近 N 轮的去重逻辑）
    recently_asked = game.get("recently_asked", {})
    weights = []
    for q in available:
        last_round = recently_asked.get(q, 0)
        rounds_ago = round_number - last_round
        if rounds_ago <= 0:
            w = 1
        elif rounds_ago >= 30:
            w = 100
        else:
            w = rounds_ago
        weights.append(w)
    
    question = random.choices(available, weights=weights, k=1)[0]
    
    # 记录该题被抽取的轮次（内存去重）
    recently_asked[question] = round_number
    
    # 记录用户做过该题（持久化）
    if target_user_ids and question_type:
        _record_question(target_user_ids, question, question_type)
    
    return question

def pick_spy_punishment(target_user_ids: list[int], count: int = 3) -> list[dict]:
    """
    为卧底游戏输家抽取惩罚题目（真心话大冒险题库）。

    分层过滤策略：尽可能让尽可能少的输家抽到回答过的题。
    - 第 0 层：所有输家都没做过的题目（最优）
    - 第 1 层：只有 1 个输家做过的题目
    - … 逐级放宽

    Parameters:
        target_user_ids: 输家 QQ 号列表
        count: 抽取题目数量

    Returns:
        题目列表，每个元素为 {"question": str, "type": "truth"/"dare"}
    """
    results: list[dict] = []
    if not target_user_ids:
        return results

    # 合并两个题库（混合模式）
    all_questions = list(TRUTH_QUESTIONS) + list(DARE_QUESTIONS)
    if not all_questions:
        return results

    # 获取每个输家做过的题目
    per_user_done: dict[int, set[str]] = {}
    for uid in target_user_ids:
        # 合并真心话和大冒险的历史
        truth_done = _get_done_questions([uid], "truth")
        dare_done = _get_done_questions([uid], "dare")
        per_user_done[uid] = truth_done | dare_done

    num_loser = len(target_user_ids)

    # 已抽取的题目（避免同一轮重复）
    picked: set[str] = set()

    for _ in range(count):
        # 分层过滤：按"做过该题的输家人数"分组
        available = [q for q in all_questions if q not in picked]
        if not available:
            break

        layers: dict[int, list[str]] = {i: [] for i in range(num_loser + 1)}
        for q in available:
            count_done = sum(1 for uid in target_user_ids if q in per_user_done.get(uid, set()))
            layers[count_done].append(q)

        # 从第 0 层（没人做过）开始逐级查找
        candidate_pool: list[str] = []
        for i in range(num_loser + 1):
            if layers[i]:
                candidate_pool = layers[i]
                break

        if not candidate_pool:
            candidate_pool = available

        # 随机选一道
        question = random.choice(candidate_pool)

        # 决定类型（根据题目来源）
        if question in TRUTH_QUESTIONS:
            qtype = "truth"
        elif question in DARE_QUESTIONS:
            qtype = "dare"
        else:
            qtype = "truth"

        results.append({"question": question, "type": qtype})
        picked.add(question)

        # 更新历史：把这道题标记为输家们"做过"，影响下一轮的层级计算
        for uid in target_user_ids:
            per_user_done.setdefault(uid, set()).add(question)

        # 持久化记录
        _record_question(target_user_ids, question, qtype)

    return results

def _auto_pick_question(game: dict) -> str:
    """自动从题库中抽取一道题目"""
    # 混合模式下，每次随机决定真心话还是大冒险（使用游戏级别概率）
    if game["type"] == "mixed":
        dare_prob = game.get("dare_probability", _td_dare_probability())
        is_dare = random.random() < dare_prob
        round_type = "dare" if is_dare else "truth"
    else:
        round_type = game["type"]

    # 保存本轮类型供简化模式读取
    game["round_type"] = round_type

    if round_type == "truth":
        round_type_cn = "真心话"
        questions = TRUTH_QUESTIONS
        action = "提问"
    else:
        round_type_cn = "大冒险"
        questions = DARE_QUESTIONS
        action = "挑战"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # 抽题目标：点数最小者（需要避免抽到他们做过的题）
    target_uids = game.get("min_players", [])

    question = _weighted_pick(questions, game, game["round"], target_uids, round_type)

    if game["type"] == "mixed":
        # 混合模式显示本轮类型
        type_tag = f"🔵 {round_type_cn}" if round_type == "truth" else f"🔴 {round_type_cn}"
        if "max_players" in game and "min_players" in game:
            max_names = _get_players_names(game, game["max_players"])
            min_names = _get_players_names(game, game["min_players"])
            if game.get("all_same_score"):
                return (
                    f"🃏 自动抽题 {type_tag}（{game_type_cn}）：\n\n"
                    f"👑🎯 全员同分，{max_names} 互相{action}：\n"
                    f"「{question}」\n"
                    f"💡 发送「/抽题」重新抽取一道题目\n"
                    f"   发送「/下一轮」重置骰子，开始新一轮"
                )
            return (
                f"🃏 自动抽题 {type_tag}（{game_type_cn}）：\n\n"
                f"👑 {max_names} 向 {min_names} {action}：\n"
                f"「{question}」\n"
                f"💡 发送「/抽题」重新抽取一道题目\n"
                f"   发送「/下一轮」重置骰子，开始新一轮"
            )
        else:
            return f"🃏 自动抽题 {type_tag}（{game_type_cn}）：\n\n「{question}」"
    else:
        if "max_players" in game and "min_players" in game:
            max_names = _get_players_names(game, game["max_players"])
            min_names = _get_players_names(game, game["min_players"])
            if game.get("all_same_score"):
                return (
                    f"🃏 自动抽题（{game_type_cn}）：\n\n"
                    f"👑🎯 全员同分，{max_names} 互相{action}：\n"
                    f"「{question}」\n"
                    f"💡 发送「/抽题」重新抽取一道 {game_type_cn} 题目\n"
                    f"   发送「/下一轮」重置骰子，开始新一轮"
                )
            return (
                f"🃏 自动抽题（{game_type_cn}）：\n\n"
                f"👑 {max_names} 向 {min_names} {action}：\n"
                f"「{question}」\n"
                f"💡 发送「/抽题」重新抽取一道 {game_type_cn} 题目\n"
                f"   发送「/下一轮」重置骰子，开始新一轮"
            )
        else:
            return f"🃏 自动抽题（{game_type_cn}）：\n\n「{question}」"

def _auto_roll(game: dict, simplified: bool = False, custom: bool = False) -> tuple[str, list[int]]:
    """自动投骰子并返回结果 (result_str, min_player_uids)"""
    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # 每个玩家投骰子（使用游戏设置的骰子数量 + 个人最大点数）
    dice_count = game.get("dice_count", 5)
    dice_max = game.get("dice_max", {})
    for p in game["players"]:
        max_face = dice_max.get(p["id"], 6)
        total, individual = _roll_dice(dice_count, max_face)
        game["rolls"][p["id"]] = (total, individual)

    # 按总点数排序
    sorted_players = sorted(game["rolls"].items(), key=lambda x: x[1][0], reverse=True)
    max_val = sorted_players[0][1][0]
    min_val = sorted_players[-1][1][0]

    # 找出所有并列最大/最小的玩家（支持多人同分）
    max_uids = [uid for uid, (total, _) in sorted_players if total == max_val]
    min_uids = [uid for uid, (total, _) in sorted_players if total == min_val]

    # 保存所有胜者/败者（支持多人并列）
    game["max_players"] = max_uids
    game["min_players"] = min_uids
    # 标记是否全员同分（max_uids 和 min_uids 指向同一组人）
    game["all_same_score"] = (max_val == min_val)

    # 记录当前轮次被抽到的玩家（用于自动踢人追踪）
    game["current_targets"] = list(min_uids)

     # 更新提问和被提问次数统计（所有并列者都计入）
    for uid in max_uids:
        game["ask_counts"][uid] = game["ask_counts"].get(uid, 0) + 1
    for uid in min_uids:
        game["answer_counts"][uid] = game["answer_counts"].get(uid, 0) + 1

    # 构建骰子结果展示（供完整模式使用）
    roll_lines = []
    for uid, (total, individual) in sorted_players:
        name = _get_player_name(game, uid)
        emoji_str = " ".join(DICE_EMOJI.get(r, f"[{r}]") for r in individual)
        marker = ""
        if uid in max_uids and uid in min_uids:
            marker = " 👑🎯"
        elif uid in max_uids:
            marker = " 👑"
        elif uid in min_uids:
            marker = " 🎯"
        roll_lines.append(f"  {name}: {emoji_str} = {total} 点{marker}")

    # 处理并列情况 — 先构建名称变量
    if len(max_uids) > 1:
        max_names = "、".join(_get_player_name(game, uid) for uid in max_uids)
        max_line = f"👑 点数最大（并列 {len(max_uids)} 人）：{max_names}（{max_val} 点）"
    else:
        max_names = _get_player_name(game, max_uids[0])
        max_line = f"👑 点数最大：{max_names}（{max_val} 点）"

    if len(min_uids) > 1:
        min_names = "、".join(_get_player_name(game, uid) for uid in min_uids)
        min_line = f"🎯 点数最小（并列 {len(min_uids)} 人）：{min_names}（{min_val} 点）"
    else:
        min_names = _get_player_name(game, min_uids[0])
        min_line = f"🎯 点数最小：{min_names}（{min_val} 点）"

    # ---- 简化模式（真心话/大冒险/混合） ----
    if simplified:
        question = _auto_pick_question(game)
        # L10 修复：原实现 split("「")[1] 依赖"必须含「且格式规整"的题库文本，
        # 换成 partition 三态处理：无「→原文；有「无」→取「后全部；有「有」→取中间
        if "「" in question:
            _, _, inner = question.partition("「")
            question_text = inner.partition("」")[0].strip() if "」" in inner else inner.strip()
        else:
            question_text = question.strip()

        return (
            f"🎯 {min_names} 请接受惩罚：\n"
            f"「{question_text}」"
        ), min_uids

    # ---- 自选模式（不抽题，由点数最大者向最小者自由提问/挑战） ----
    if custom:
        result = (
            f"🎲 投骰子结果（{dice_count} 个骰子比大小）：\n"
            + "\n".join(roll_lines) + "\n\n"
            f"{max_line}\n"
            f"{min_line}\n\n"
        )

        # 混合模式下判断本轮类型（真心话/大冒险）
        if game["type"] == "mixed":
            dare_prob = game.get("dare_probability", _td_dare_probability())
            is_dare = random.random() < dare_prob
        else:
            is_dare = (game["type"] == "dare")

        type_tag = "🔴 大冒险" if is_dare else "🔵 真心话"
        action = "发起大冒险" if is_dare else "提问"
        action_hint = "大冒险或提问" if is_dare else "自由提问"

        # 多人并列时添加提示
        if len(max_uids) > 1 or len(min_uids) > 1:
            if max_val == min_val:
                result += f"💡 全员同分！{max_names} 互相{action_hint}！\n\n"
            else:
                if len(max_uids) > 1:
                    result += f"💡 多人并列最大，{max_names} 都需要向输方{action}！\n"
                if len(min_uids) > 1:
                    result += f"💡 多人并列最小，{min_names} 都需要回答/接受挑战！\n"
                result += "\n"

        # 自选模式：提示最大者向最小者提问/发起大冒险
        if max_val == min_val:
            result += f"{type_tag} 🎤 {max_names} 互相{action_hint}！\n"
        else:
            result += f"{type_tag} 🎤 {max_names} 向 {min_names} {action_hint}！\n"
        result += "💡 发送「/提问建议 <描述>」让 AI 帮你生成问题\n"
        result += "   发送「/抽题」改为从题库抽题\n"
        result += "   发送「/下一轮」重置骰子，开始新一轮"

        return result, list(set(max_uids + min_uids))

    # ---- 自动模式（AI 根据输家画像出题） ----
    if game.get("display_mode") == "auto":
        # 确定本轮类型
        if game["type"] == "mixed":
            dare_prob = game.get("dare_probability", _td_dare_probability())
            is_dare = random.random() < dare_prob
        else:
            is_dare = (game["type"] == "dare")

        round_type = "dare" if is_dare else "truth"
        round_type_cn = "大冒险" if is_dare else "真心话"
        type_tag = "🔴 大冒险" if is_dare else "🔵 真心话"
        action = "接受大冒险" if is_dare else "回答"

        # 获取 group_id
        gid = game.get("_group_id", 0)

        # 后台静默检查所有有答题记录的玩家，补充不足阈值的题目池
        # 注意：必须放线程，否则会阻塞消息处理（LLM 请求 timeout=180s）
        threading.Thread(
            target=_background_refill_all, args=(gid,), daemon=True, name="bg-refill"
        ).start()

        def _bg_generate_questions():
            """后台线程：为每个输家生成问题并追加发送"""
            import logging
            logger = logging.getLogger("qq-bot")
            from core.persona import get_user_profile
            from core.sender import send_reply_sync
            # 等 N 秒让骰子消息先被服务器处理，避免消息顺序颠倒（N 走配置 bg_delay_seconds）
            time.sleep(_td_bg_delay())
            try:
                loser_questions = {}  # uid -> [(winner_name, question), ...]
                for uid in min_uids:
                    name = _get_player_name(game, uid)
                    from core.persona import get_active_persona, persona_to_text
                    active_persona = get_active_persona(uid, gid)
                    persona_text = persona_to_text(active_persona) if active_persona else ""

                    spiciness = game.get("spiciness", _td_default_spiciness())

                    loser_questions[uid] = []
                    # 多人赢单人输：每个赢家各出一道题；多人赢多人输：输家只收1道（赢家随机）
                    if len(min_uids) == 1 and len(max_uids) > 1:
                        winner_pool = max_uids
                    else:
                        winner_pool = [random.choice(max_uids)]

                    for wuid in winner_pool:
                        wname = _get_player_name(game, wuid)
                        _, _, gender = _get_persona_nickname(uid, gid)
                        # 优先从题目池中取，池空则实时生成
                        question = _get_or_pop_question(
                            user_id=uid,
                            group_id=gid,
                            question_type=round_type,
                            profile_text=persona_text,
                            spiciness=spiciness,
                            nickname=name,
                            gender=gender,
                        )
                        logger.info(f"auto 模式出题（@{wname} → @{name}）: {question[:30]}...")

                        question = question.replace("选一个回答就行～", "").strip()
                        parts = [p.strip() for p in question.split("\n\n") if p.strip()]
                        final_question = parts[0] if parts else question

                        _record_auto_question(uid, gid, final_question, round_type, persona_text)
                        loser_questions[uid].append((wname, final_question))

                # 构建问题消息并追加发送
                questions_msg = ""
                for uid, q_list in loser_questions.items():
                    lname = _get_player_name(game, uid)
                    questions_msg += f"{type_tag} 🤖 AI 出题 → @{lname}：\n"
                    for wname, lquestion in q_list:
                        if len(q_list) > 1:
                            # 多人提问一人：提问者放题目后面，避免玩家误以为被点名回答（2026-08-07）
                            questions_msg += f"  「{lquestion}」（{wname} 提问）\n"
                        else:
                            questions_msg += f"「{lquestion}」\n"
                    questions_msg += "\n"
                questions_msg += f"🎯 {min_names} 请{action}！\n"
                questions_msg += "💡 发送「/色色程度 [0-6]」调整 AI 出题尺度\n"
                questions_msg += "   发送「/下一轮」开始新一轮"

                success = send_reply_sync("group", gid, questions_msg, at_user_ids=min_uids)
                logger.info(f"auto 模式追加发送题目: success={success}, group={gid}")

            except Exception as e:
                import logging
                logger = logging.getLogger("qq-bot")
                logger.error(f"auto 模式后台出题异常: {e}", exc_info=True)
                error_msg = "⚠️ AI 出题出现异常，已降级为手动模式\n💡 请发送「/抽题」从题库随机抽题，或发送「/下一轮」重新开始"
                send_reply_sync("group", gid, error_msg, at_user_ids=min_uids)

        # 启动后台线程生成问题（不阻塞主循环）
        threading.Thread(target=_bg_generate_questions, daemon=True, name="auto-gen-questions").start()

        # 自动模式：不返回骰子结果（返回空串），由后台出题线程直接发送题目
        # 骰子结果不发，避免刷屏——只保留 min_uids 供调用方记录
        return "", min_uids

    # ---- 完整模式（真心话/大冒险/混合） ----
    # 投骰结果 + 自动抽题
    result = (
        f"🎲 投骰子结果（{dice_count} 个骰子比大小）：\n"
        + "\n".join(roll_lines) + "\n\n"
        f"{max_line}\n"
        f"{min_line}\n\n"
    )

    # 多人并列时添加提示
    if len(max_uids) > 1 or len(min_uids) > 1:
        if max_val == min_val:
            result += f"💡 全员同分！{max_names} 互相提问/挑战！\n\n"
        else:
            if len(max_uids) > 1:
                result += f"💡 多人并列最大，{max_names} 都需要向输方提问/挑战！\n"
            if len(min_uids) > 1:
                result += f"💡 多人并列最小，{min_names} 都需要回答/接受挑战！\n"
            result += "\n"

    # 自动抽题
    result += _auto_pick_question(game)

    # 纯文本中保留 @ 提示（bot.py 会额外插入 at 消息段实现真正的 @ 通知）
    result += f"\n\n🎯 {min_names} 请接受惩罚！"

    return result, min_uids

def pick_card(group_id: int) -> Optional[str]:
    """从题库中抽取题目"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏"

    # 混合模式下，随机决定真心话还是大冒险（使用游戏级别概率）
    if game["type"] == "mixed":
        dare_prob = game.get("dare_probability", _td_dare_probability())
        is_dare = random.random() < dare_prob
        round_type = "dare" if is_dare else "truth"
    else:
        round_type = game["type"]

    if round_type == "truth":
        round_type_cn = "真心话"
        questions = TRUTH_QUESTIONS
        action = "提问"
    else:
        round_type_cn = "大冒险"
        questions = DARE_QUESTIONS
        action = "挑战"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # 抽题目标：点数最小者
    target_uids = game.get("min_players", [])

    question = _weighted_pick(questions, game, game["round"], target_uids, round_type)

    if game["type"] == "mixed":
        type_tag = f"🔵 {round_type_cn}" if round_type == "truth" else f"🔴 {round_type_cn}"
        if "max_players" in game and "min_players" in game:
            max_names = _get_players_names(game, game["max_players"])
            min_names = _get_players_names(game, game["min_players"])
            if game.get("all_same_score"):
                return (
                    f"🃏 抽题结果 {type_tag}（{game_type_cn}）：\n\n"
                    f"👑🎯 全员同分，{max_names} 互相{action}：\n"
                    f"「{question}」"
                )
            return (
                f"🃏 抽题结果 {type_tag}（{game_type_cn}）：\n\n"
                f"👑 {max_names} 向 {min_names} {action}：\n"
                f"「{question}」"
            )
        else:
            return f"🃏 抽题结果 {type_tag}（{game_type_cn}）：\n\n「{question}」"
    else:
        if "max_players" in game and "min_players" in game:
            max_names = _get_players_names(game, game["max_players"])
            min_names = _get_players_names(game, game["min_players"])
            if game.get("all_same_score"):
                return (
                    f"🃏 抽题结果（{game_type_cn}）：\n\n"
                    f"👑🎯 全员同分，{max_names} 互相{action}：\n"
                    f"「{question}」"
                )
            return (
                f"🃏 抽题结果（{game_type_cn}）：\n\n"
                f"👑 {max_names} 向 {min_names} {action}：\n"
                f"「{question}」"
            )
        else:
            return f"🃏 抽题结果（{game_type_cn}）：\n\n「{question}」"

def next_round(group_id: int) -> tuple[Optional[str], list[int]]:
    """进入下一轮，返回 (result_str, min_player_uids)"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏", []

    # 10 秒冷却：防止多人同时触发 /下一轮
    last_time = game.get("_last_next_round", 0)
    cooldown = 10
    remaining = cooldown - (time.time() - last_time)
    if remaining > 0:
        return f"⏳ 请等 {int(remaining) + 1} 秒后再发送「/下一轮」～", []

    game["_last_next_round"] = time.time()

    # 下一轮时触发题目池补充检查（放后台线程，避免阻塞消息处理）
    gid = game.get("_group_id", group_id)
    threading.Thread(
        target=_background_refill_all, args=(gid,), daemon=True, name="bg-refill-next"
    ).start()

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # ---- 保存赢家消息用于 LLM 异步总结（先清出，避免阻塞投骰输出）----
    winner_msgs_for_llm = None
    if game.get("display_mode") == "custom" and game.get("winner_messages"):
        winner_msgs_for_llm = game.pop("winner_messages", {})

    # ---- 自动踢人逻辑：检查上一轮被抽到的人是否仍未回答 ----
    previous_targets = game.get("current_targets", [])
    auto_kick_msgs = ""
    auto_kick_uids = []
    if previous_targets:
        unanswered = game.get("unanswered_counts", {})
        kick_threshold = _td_kick_threshold()
        for uid in previous_targets:
            # 上一轮被抽到的人 +1 计数（如果本轮开始时还未回答）
            unanswered[uid] = unanswered.get(uid, 0) + 1
            if unanswered[uid] >= kick_threshold:
                auto_kick_uids.append(uid)
                # M2 修复：@_:{uid} 是无效占位符，sender 的 @ 通知依赖返回值 auto_kick_uids，
                # 文本里直接用昵称文字描述（返回的 auto_kick_uids 会真正 @ 被踢玩家）
                auto_kick_msgs += f"🚪 玩家 {uid} 连续 {kick_threshold} 轮未回答，已被请出游戏\n"

    player_count = len(game["players"]) - len(auto_kick_uids)

    # 如果只剩 1 人，游戏结束
    if player_count < 2:
        # 先执行踢人（与下方 1005-1014 相同的清理逻辑），再清理游戏状态
        # 防止游戏永久残留成"僵尸游戏"：/下一轮 死循环、/真心话 永远提示进行中
        for uid in auto_kick_uids:
            player = next((p for p in game["players"] if p["id"] == uid), None)
            if player:
                game["players"].remove(player)
            game["rolls"].pop(uid, None)
            game["unanswered_counts"].pop(uid, None)
            if uid in game.get("current_targets", []):
                game["current_targets"].remove(uid)
            game.get("dice_max", {}).pop(uid, None)
        _TRUTH_DARE_GAMES.pop(group_id, None)
        if auto_kick_msgs:
            return auto_kick_msgs + f"🏁 场上只剩 {player_count} 人，游戏结束！", auto_kick_uids
        if game.get("display_mode") in ("simplified", "custom", "auto"):
            return f"🏁 场上只剩 {player_count} 人，游戏结束！", []
        return f"🏁 场上只剩 {player_count} 人，游戏结束！", []

    # ---- 踢人执行 ----
    for uid in auto_kick_uids:
        player = next((p for p in game["players"] if p["id"] == uid), None)
        if player:
            game["players"].remove(player)
        # 清理被踢玩家的相关状态
        game["rolls"].pop(uid, None)
        game["unanswered_counts"].pop(uid, None)
        if uid in game.get("current_targets", []):
            game["current_targets"].remove(uid)
        game.get("dice_max", {}).pop(uid, None)
    # 清除 max/min 缓存
    game.pop("max_players", None)
    game.pop("min_players", None)
    game.pop("all_same_score", None)

    # ---- 新一轮开始 ----
    game["round"] += 1
    game["rolls"] = {}

    # ---- 自动模式：投骰+AI根据输家画像出题 ----
    if game.get("display_mode") == "auto":
        game["_group_id"] = group_id
        roll_result, min_uids = _auto_roll(game)
        # roll_result 为空串（由后台出题线程直接发消息），只返回踢人消息
        # 注意：返回空字符串 "" 而非 None，让路由器正确 return，防止触发 AI 聊天
        # ⚠️ at 列表只含 auto_kick_uids（被踢者）——新输家 min_uids 的 @ 由后台
        #    出题线程（_bg_generate_questions）负责，混入会 @ 错人（2026-08-12 修复）
        if auto_kick_msgs:
            return auto_kick_msgs, auto_kick_uids
        return "", auto_kick_uids

    # ---- 自选模式：先投骰+输出，再异步调用 LLM 总结问题 ----
    if game.get("display_mode") == "custom":
        roll_result, min_uids = _auto_roll(game, custom=True)
        result_msg = auto_kick_msgs + roll_result

        # 在后台异步调用 LLM 总结上一轮赢家发言（不阻塞返回）
        if winner_msgs_for_llm:
            def _async_summarize():
                min_players = game.get("min_players", [])
                game_type = game["type"]
                for winner_uid, messages in winner_msgs_for_llm.items():
                    if not messages:
                        continue
                    winner_name = _get_player_name(game, winner_uid)
                    target_uid = min_players[0] if min_players else None
                    target_name = _get_player_name(game, target_uid) if target_uid else "—"
                    extracted = _parse_self_select_questions(messages, game_type)
                    if extracted:
                        _save_self_select(
                            group_id=group_id,
                            winner_id=winner_uid,
                            winner_name=winner_name,
                            target_id=target_uid,
                            target_name=target_name,
                            original_msgs=messages,
                            extracted=extracted,
                            game_type=game_type,
                        )
            threading.Thread(target=_async_summarize, daemon=True).start()

        return result_msg, auto_kick_uids + min_uids
    # M1 修复：1021 行已统一 game["round"] += 1 + game["rolls"] = {}，
    # 这里不能再重复自增（原代码 simplified/full 模式每轮轮次 +2，导致
    # end_game 统计虚高 + _weighted_pick 的 recently_asked 权重系统性偏移）
    if player_count >= 2:

        # 简化模式：只显示问题和 @ 输家
        if game.get("display_mode") == "simplified":
            roll_result, min_uids = _auto_roll(game, simplified=True)
            return auto_kick_msgs + roll_result, auto_kick_uids + min_uids
        # 自动模式：AI 根据输家画像出题（⚠️ 实际在 1062 分支已提前 return——此处为
        # 兜底分支，at 只含被踢者；新输家 @ 由后台出题线程负责，2026-08-12）
        elif game.get("display_mode") == "auto":
            game["_group_id"] = group_id
            roll_result, min_uids = _auto_roll(game)
            return auto_kick_msgs + roll_result, auto_kick_uids
        # 自选模式：投骰子+排名，不抽题
        elif game.get("display_mode") == "custom":
            roll_result, min_uids = _auto_roll(game, custom=True)
            return auto_kick_msgs + roll_result, auto_kick_uids + min_uids
        else:
            header = (
                f"🎮 {game_type_cn} 第 {game['round']} 轮开始！\n"
                f"👥 当前玩家：{player_count} 人\n"
                f"🎲 系统自动投骰子中...\n"
            )
            roll_result, min_uids = _auto_roll(game)
            return auto_kick_msgs + header + roll_result, auto_kick_uids + min_uids
    else:
        if auto_kick_msgs:
            if game.get("display_mode") in ("simplified", "custom", "auto"):
                return auto_kick_msgs + f"👥 当前玩家：{player_count} 人（还需要至少 1 人即可开始）", auto_kick_uids
            return auto_kick_msgs + f"👥 当前玩家：{player_count} 人（还需要至少 1 人即可开始）", auto_kick_uids
        if game.get("display_mode") in ("simplified", "custom", "auto"):
            return f"👥 当前玩家：{player_count} 人（还需要至少 1 人即可开始）", []
        return (
            f"🎮 {game_type_cn} 第 {game['round']} 轮开始！\n"
            f"👥 当前玩家：{player_count} 人（还需要至少 1 人即可开始）"
        ), []

def end_game(group_id: int) -> Optional[str]:
    """结束游戏"""
    game = _TRUTH_DARE_GAMES.pop(group_id, None)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")
    rounds = game["round"]
    player_count = len(game.get("all_player_ids", set()))

    # 统计提问次数最多的玩家
    ask_counts = game.get("ask_counts", {})
    answer_counts = game.get("answer_counts", {})

    # 按提问次数排序，取前两名
    sorted_askers = sorted(ask_counts.items(), key=lambda x: x[1], reverse=True)
    # 按被提问次数排序，取前两名
    sorted_answervers = sorted(answer_counts.items(), key=lambda x: x[1], reverse=True)

    stats = ""
    if sorted_askers:
        top_askers = sorted_askers[:2]
        asker_lines = "\n".join(f"  {i+1}. {_get_player_name(game, uid)}: {count} 次" for i, (uid, count) in enumerate(top_askers))
        stats += f"\n🎤 提问次数最多：\n{asker_lines}"

    if sorted_answervers:
        top_answerers = sorted_answervers[:2]
        answerer_lines = "\n".join(f"  {i+1}. {_get_player_name(game, uid)}: {count} 次" for i, (uid, count) in enumerate(top_answerers))
        stats += f"\n🎯 被提问次数最多：\n{answerer_lines}"

    return (
        f"🏁 {game_type_cn} 游戏结束！\n"
        f"📊 共进行了 {rounds} 轮\n"
        f"👥 共有 {player_count} 人参与\n"
        f"{stats}\n"
        f"🎉 感谢大家的参与！"
    )

def get_game_status(group_id: int) -> Optional[str]:
    """获取游戏状态"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")
    player_count = len(game["players"])
    rounds = game["round"]

    # 列出所有玩家昵称
    player_names = ", ".join(p["name"] for p in game["players"])

    status = (
        f"📊 {game_type_cn} 游戏状态\n"
        f"👥 玩家（{player_count} 人）：{player_names}\n"
        f"📝 当前轮次：第 {rounds} 轮\n"
        f"🎲 骰子数量：{game.get('dice_count', 5)} 个{'（手动锁定）' if game.get('manual_dice_count', False) else '（自动调整）'}\n"
    )

    if "max_players" in game:
        max_names = _get_players_names(game, game["max_players"])
        min_names = _get_players_names(game, game["min_players"])
        if game.get("all_same_score"):
            status += (
                f"👑🎯 全员同分：{max_names}"
            )
        else:
            status += (
                f"👑 上一轮点数最大：{max_names}\n"
                f"🎯 上一轮点数最小：{min_names}"
            )
    else:
        status += "🎲 等待投骰子..."

    return status

# ============ 真心话大冒险命令映射 ============
TD_COMMANDS = {
    "真心话": "truth",
    "大冒险": "dare",
    "真心话大冒险": "mixed",
    "加入": "join",
    "退出": "leave",
    "踢人": "kick",
    "抽题": "pick",
    "骰": "roll",
    "下一轮": "next",
    "概率": "probability",
    "骰数": "dice_count",
    "点数": "dice_max",
    "简化模式": "simple_mode",
    "自选模式": "custom_mode",
    "完整模式": "full_mode",
    "自动模式": "auto_mode",
    "色色程度": "spiciness",
    "添加真心话": "add_truth",
    "添加大冒险": "add_dare",
    "题库": "questions",
    "结束": "end",
    "游戏状态": "status",
    "做过": "view_history",
    "清空做过": "clear_history",
    "自选记录": "self_select",
    "提问建议": "suggest_question",
    "取消建议": "cancel_suggestion",
}

# 追加到别名
TD_ALIASES = {
    "td": "真心话",
    "参加": "加入",
    "参与": "加入",
    "退出游戏": "退出",
    "历史记录": "做过",
    "重置": "清空做过",
    "投骰": "骰",
    "骰子": "骰数",
}

# 合并 TD 命令到全局命令表
COMMANDS.update({k: lambda t, a=v: a for k, v in TD_COMMANDS.items()})
ALIASES.update(TD_ALIASES)

# ============ 功能函数 ============

def check_command(
    text: str,
    group_id: int | None = None,
    user_id: int = 0,
    nickname: str = "",
) -> tuple[Optional[str], list[int]]:
    """
    检查是否是娱乐命令，如果是则返回 (回复文本, at_user_ids)，否则返回 (None, [])
    """
    text_lower = text.strip().lower()

    # 检查 /命令 格式
    if text_lower.startswith("/"):
        parts = text_lower.split(maxsplit=1)
        cmd = parts[0][1:]  # 去掉 /
        arg = parts[1] if len(parts) > 1 else ""

        # 兼容无空格写法：/点数4 → cmd=点数, arg=4（/概率50 同理）
        # 仅当整段命令名查不到时尝试拆分，避免误伤
        if cmd not in ALIASES and cmd not in TD_COMMANDS and cmd not in COMMANDS:
            m = re.match(r"^([^\d]+?)(\d.*)$", cmd)
            if m and (m.group(1) in ALIASES or m.group(1) in TD_COMMANDS or m.group(1) in COMMANDS):
                cmd = m.group(1)
                arg = m.group(2) + ((" " + arg) if arg else "")

        # 先通过别名映射解析（合并 TD_ALIASES + 小游戏 ALIASES）
        resolved = ALIASES.get(cmd, cmd)

        # 真心话大冒险命令（返回 (str, list[int]) 元组）
        if resolved in TD_COMMANDS:
            return _dispatch_td(TD_COMMANDS[resolved], arg, group_id, user_id, nickname)

        if resolved in COMMANDS:
            handler = COMMANDS[resolved]
            if resolved in ("riddle", "answer", "help"):
                if resolved in ("riddle", "answer"):
                    return (handler(group_id or 0, user_id or 0), [])
                return (handler(), [])
            return (handler(arg), [])
        return (None, [])

    # 检查中文命令（无前缀）— 按长度降序排列，确保长命令优先匹配
    # 先检查 TD_COMMANDS（真心话大冒险系列命令）
    # ⚠️ 必须精确匹配（== 或 "命令 " 前缀）：startswith 无边界匹配会让
    #    "结束话题吧"/"退出群聊" 等闲聊误触发 结束/退出/清空做过 等破坏性命令
    for td_cmd in sorted(TD_COMMANDS.keys(), key=lambda x: -len(x)):
        if text_lower == td_cmd or text_lower.startswith(td_cmd + " "):
            return _dispatch_td(TD_COMMANDS[td_cmd], "", group_id, user_id, nickname)

    # 处理数字回复 — 当有 pending_suggestions 时，匹配 1-4 选择
    # 双重守卫：必须是发起建议的赢家 AND 是当前轮次赢家
    # M7 修复：原实现委托 question_pool.handle_confirm_suggestion stub（永远返回占位文本
    # 且不清除 pending_suggestions），导致建议状态残留、之后每次发数字都重复命中。
    # 这里与 acknowledge_answer 的内联确认逻辑保持一致（去掉 display_mode 限制，
    # 使非 custom 模式下已产生的 pending_suggestions 也能被正常确认）。
    if text_lower in ("1", "2", "3", "4"):
        game = _TRUTH_DARE_GAMES.get(group_id or 0, {})
        if game and game.get("pending_suggestions"):
            winner_uid = game.get("suggestion_winner_uid")
            max_players = game.get("max_players", [])
            if winner_uid == user_id and user_id in max_players:
                choice = int(text_lower)
                suggestions = game.get("pending_suggestions", [])
                if 1 <= choice <= len(suggestions):
                    chosen = suggestions[choice - 1]
                    min_players = game.get("min_players", [])
                    target_uid = min_players[0] if min_players else None
                    target_name = _get_player_name(game, target_uid) if target_uid else "—"
                    winner_name = _get_player_name(game, user_id)
                    try:
                        _save_self_select(
                            group_id=group_id or 0,
                            winner_id=user_id,
                            winner_name=winner_name,
                            target_id=target_uid,
                            target_name=target_name,
                            original_msgs=[chosen],
                            extracted=[chosen],
                            game_type=game.get("type", "mixed"),
                        )
                    except Exception as e:
                        logger.error(f"保存自选提问失败: {e}")
                    game.pop("pending_suggestions", None)
                    game.pop("suggestion_winner_uid", None)
                    reply = (
                        f"✅ 已确认提问建议！\n"
                        f"📝 问题：{chosen}\n"
                        f"🎯 请 {target_name} 回答！"
                    )
                    at_ids = [target_uid] if target_uid else []
                    return (reply, at_ids)
                # 建议列表为空/越界 → 清理并提示
                game.pop("pending_suggestions", None)
                game.pop("suggestion_winner_uid", None)
                return ("⚠️ 建议列表已失效，请重新发送 /提问建议", [])

    # 再检查别名映射
    # ⚠️ 同样要求精确匹配（== 或 "别名 " 前缀），避免"参与讨论"误触发"参与"→"加入"等
    for alias, cmd in sorted(ALIASES.items(), key=lambda x: -len(x[0])):
        if text_lower == alias or text_lower.startswith(alias + " "):
            handler = COMMANDS.get(cmd)
            if handler:
                remaining = text[len(alias):].strip()
                if cmd in ("riddle", "answer", "help"):
                    if cmd in ("riddle", "answer"):
                        return (handler(group_id or 0, user_id or 0), [])
                    return (handler(), [])
                return (handler(remaining), [])

    return (None, [])

def add_truth_question(arg: str) -> Optional[str]:
    """添加真心话题目到题库（同时写入文件，实时生效）"""
    content = arg.strip()
    if not content:
        return "💡 用法：/添加真心话 <题目内容>\n   例如：/添加真心话 你最后一次哭是因为什么？"

    # 长度限制
    if len(content) > 200:
        return "❌ 题目过长（最多 200 字），请精简后再试"

    # 查重
    if content in TRUTH_QUESTIONS:
        return f"⚠️ 这道题目已经存在于题库中\n\n「{content}」"

    _append_question(TRUTH_FILE, TRUTH_QUESTIONS, content)
    return f"✅ 已添加真心话题目（题库共 {len(TRUTH_QUESTIONS)} 题）\n\n「{content}」"

def add_dare_question(arg: str) -> Optional[str]:
    """添加大冒险题目到题库（同时写入文件，实时生效）"""
    content = arg.strip()
    if not content:
        return "💡 用法：/添加大冒险 <挑战内容>\n   例如：/添加大冒险 发一段 30 秒的语音说绕口令"

    # 长度限制
    if len(content) > 200:
        return "❌ 挑战过长（最多 200 字），请精简后再试"

    # 查重
    if content in DARE_QUESTIONS:
        return f"⚠️ 这项挑战已经存在于题库中\n\n「{content}」"

    _append_question(DARE_FILE, DARE_QUESTIONS, content)
    return f"✅ 已添加大冒险挑战（题库共 {len(DARE_QUESTIONS)} 题）\n\n「{content}」"

def clear_history(user_id: int) -> Optional[str]:
    """清空自己的做过题目历史"""
    count = _clear_history(user_id, None)
    if count == 0:
        return "📭 你还没有做过任何真心话/大冒险题目"
    return f"🗑️ 已清空你的题目历史记录（共 {count} 条）\n\n现在所有题目都可以重新被抽到了~"

def view_history(user_id: int) -> Optional[str]:
    """查看自己的做过题目历史"""
    count = _get_history_count(user_id)
    if count == 0:
        return "📭 你还没有做过任何真心话/大冒险题目"

    # 分别统计真心话和大冒险
    with _get_db(TD_HISTORY_DB) as conn:
        truth_count = conn.execute(
            "SELECT COUNT(*) FROM user_question_history WHERE user_id = ? AND question_type = 'truth'",
            (user_id,),
        ).fetchone()[0]
        dare_count = conn.execute(
            "SELECT COUNT(*) FROM user_question_history WHERE user_id = ? AND question_type = 'dare'",
            (user_id,),
        ).fetchone()[0]

    truth_remaining = max(0, len(TRUTH_QUESTIONS) - truth_count)
    dare_remaining = max(0, len(DARE_QUESTIONS) - dare_count)

    return (
        f"📊 你的真心话大冒险记录：\n\n"
        f"🔵 真心话：已做 {truth_count} 题 / 剩余 {truth_remaining} 题（共 {len(TRUTH_QUESTIONS)} 题）\n"
        f"🔴 大冒险：已做 {dare_count} 题 / 剩余 {dare_remaining} 题（共 {len(DARE_QUESTIONS)} 题）\n\n"
        f"💡 发送「/清空做过」可重置所有记录"
    )

def set_probability(arg: str, group_id: int) -> Optional[str]:
   """修改真心话大冒险混合模式的大冒险概率"""
   game = _TRUTH_DARE_GAMES.get(group_id)
   if not game or game["type"] != "mixed":
       return "🎮 当前没有正在进行的混合模式游戏\n\n💡 用法：/概率 [0-100] 设置大冒险概率（百分比）\n   例如：/概率 30 表示 30% 概率大冒险，70% 概率真心话"

   if not arg.strip():
       dare_pct = game.get("dare_probability", _td_dare_probability()) * 100
       truth_pct = 100 - dare_pct
       return f"📊 当前概率：真心话 {int(truth_pct)}% / 大冒险 {int(dare_pct)}%\n\n💡 用法：/概率 [0-100] 设置大冒险概率（百分比）"

   try:
       new_prob = int(arg.strip())
   except ValueError:
       return "❌ 请输入 0-100 之间的整数，例如 /概率 30"

   if new_prob < 0 or new_prob > 100:
       return "❌ 概率范围：0-100，请输入有效数值"

   # 游戏级别独立设置，不影响全局
   game["dare_probability"] = new_prob / 100
   truth_pct = 100 - new_prob
   return (
       f"✅ 概率已更新！\n\n"
       f"🔵 真心话：{truth_pct}%\n"
       f"🔴 大冒险：{new_prob}%\n\n"
       f"（本次游戏有效，未单独设置时恢复默认 {int(_td_dare_probability()*100)}%）"
   )

def manual_roll(group_id: int, user_id: int) -> tuple[Optional[str], list[int]]:
   """手动投骰子（主持人触发），返回 (result_str, min_player_uids)"""
   game = _TRUTH_DARE_GAMES.get(group_id)
   if not game:
       return "🎮 当前没有正在进行的真心话大冒险游戏", []

   game["_group_id"] = group_id  # 确保后台线程能拿到正确的群号

   player_count = len(game["players"])
   if player_count < 2:
       return f"👥 当前只有 {player_count} 人，还需至少 1 人才能投骰子\n\n💡 其他人发送「/加入」参与游戏", []

   # 初始化 ask_counts/answer_counts（如果还没投过）
   if "ask_counts" not in game:
       game["ask_counts"] = {}
   if "answer_counts" not in game:
       game["answer_counts"] = {}

   if game.get("display_mode") == "simplified":
       return _auto_roll(game, simplified=True)
   elif game.get("display_mode") == "custom":
       return _auto_roll(game, custom=True)
   else:
       return _auto_roll(game)

def set_dice_count(arg: str, group_id: int, user_id: int) -> Optional[str]:
   """修改骰子数量"""
   game = _TRUTH_DARE_GAMES.get(group_id)
   if not game:
       return "🎮 当前没有正在进行的真心话大冒险游戏\n\n💡 用法：/骰数 [1-12] 设置骰子数量（默认随人数自动调整）\n   例如：/骰数 3 改为 3 个骰子，/骰数 自动 恢复自动调整"

   if not arg.strip():
       current = game.get("dice_count", 5)
       manual = game.get("manual_dice_count", False)
       mode_str = "手动锁定" if manual else "自动调整"
       return f"🎲 当前骰子数量：{current} 个（{mode_str}）\n\n💡 用法：/骰数 [1-12] 设置骰子数量（锁定，不再自动调整）\n   /骰数 自动 恢复自动调整\n   例如：/骰数 3 改为 3 个骰子"

   # 支持「自动」关键字恢复自动调整
   if arg.strip().lower() in ("自动", "auto"):
       game["manual_dice_count"] = False
       auto_dice = _get_auto_dice_count(len(game["players"]))
       game["dice_count"] = auto_dice
       game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")
       return (
           f"✅ 骰子数量已恢复自动调整（当前 {auto_dice} 个）！\n\n"
           f"（人数变化时将自动调整，使用 /骰数 [1-12] 可手动锁定）\n"
           f"（本次 {game_type_cn} 游戏有效，重启后恢复默认）"
       )

   try:
       new_count = int(arg.strip())
   except ValueError:
       return "❌ 请输入 1-12 之间的整数或「自动」，例如 /骰数 3 或 /骰数 自动"

   if new_count < 1 or new_count > 12:
       return "❌ 骰子数量范围：1-12，请输入有效数值"

   game["dice_count"] = new_count
   game["manual_dice_count"] = True
   game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")
   return (
        f"✅ 骰子数量已更新为 {new_count} 个！（已锁定，不再随人数变化自动调整）\n\n"
        f"💡 如需恢复自动调整，发送「/骰数 自动」\n"
        f"（本次 {game_type_cn} 游戏有效，重启后恢复默认）"
    )

def set_display_mode(mode: str, group_id: int) -> Optional[str]:
    """切换显示模式（简化/完整/自选）"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏"

    current = game.get("display_mode", "full")
    player_count = len(game["players"])
    if mode == "auto":
        mode_cn = "自动"
    else:
        mode_cn = "简化" if mode == "simplified" else ("自选" if mode == "custom" else "完整")

    # 检查是否已经是该模式
    if current == mode:
        return f"🎮 当前已经是 {mode_cn} 模式"

    game["display_mode"] = mode
    # 用户手动切换模式后，不再随人数自动切换
    game["manual_display_mode"] = True

    mode_desc = {
        "simplified": "→ 仅显示问题和 @ 输家",
        "custom": "→ 显示骰子结果、排名和模式标签（真心话/大冒险），由点数最大者向最小者自由提问或发起大冒险",
        "auto": "→ 显示骰子结果、排名，AI 根据输家画像自动生成问题（使用 /色色程度 控制尺度）",
        "full": "→ 显示骰子结果、排名、问题和 @ 输家（完整信息）",
    }

    return (
        f"✅ 已切换到 {mode_cn} 模式！（当前 {player_count} 人）\n\n"
        f"📌 {mode_cn} 模式下「/下一轮」回复内容：\n"
        f"  {mode_desc[mode]}"
    )

def get_questions(arg: str) -> Optional[str]:
    """查看当前题库内容（/题库 真心话 / 题库 大冒险 / 题库 全部）"""
    mode = arg.strip().lower()

    if mode in ("真心话", "truth"):
        if not TRUTH_QUESTIONS:
            return "🔵 真心话题库为空"
        lines = [f"  {i+1}. {q}" for i, q in enumerate(TRUTH_QUESTIONS)]
        return f"🔵 真心话题库（共 {len(TRUTH_QUESTIONS)} 题）：\n\n" + "\n".join(lines)
    elif mode in ("大冒险", "dare"):
        if not DARE_QUESTIONS:
            return "🔴 大冒险题库为空"
        lines = [f"  {i+1}. {q}" for i, q in enumerate(DARE_QUESTIONS)]
        return f"🔴 大冒险题库（共 {len(DARE_QUESTIONS)} 题）：\n\n" + "\n".join(lines)
    else:
        parts = []
        if TRUTH_QUESTIONS:
            truth_lines = [f"  {i+1}. {q}" for i, q in enumerate(TRUTH_QUESTIONS)]
            parts.append(f"🔵 真心话（{len(TRUTH_QUESTIONS)} 题）：\n" + "\n".join(truth_lines))
        if DARE_QUESTIONS:
            dare_lines = [f"  {i+1}. {q}" for i, q in enumerate(DARE_QUESTIONS)]
            parts.append(f"🔴 大冒险（{len(DARE_QUESTIONS)} 题）：\n" + "\n".join(dare_lines))

        if not parts:
            return "📚 题库为空，使用 /添加真心话 或 /添加大冒险 添加题目"

        return (
            f"📚 真心话大冒险题库\n"
            f"🔵 真心话 {len(TRUTH_QUESTIONS)} 题 / 🔴 大冒险 {len(DARE_QUESTIONS)} 题\n\n"
            + "\n\n".join(parts)
            + "\n\n💡 /题库 真心话 或 /题库 大冒险 查看分类"
        )

def set_dice_max(arg: str, group_id: int, user_id: int, nickname: str) -> Optional[str]:
    """设置用户骰子最大点数（6-12）"""
    game = _TRUTH_DARE_GAMES.get(group_id)
    if not game:
        return "🎮 当前没有正在进行的真心话大冒险游戏"

    game_type_cn = "真心话" if game["type"] == "truth" else ("大冒险" if game["type"] == "dare" else "真心话大冒险")

    # 检查是否在玩家列表中
    if not any(p["id"] == user_id for p in game["players"]):
        return f"✅ 你还没有加入 {game_type_cn} 游戏"

    if not arg:
        current = game.get("dice_max", {}).get(user_id, 6)
        return f"🎲 你的骰子最大点数为 {current}（默认 6），发送「/点数 [6-12]」修改"

    try:
        new_max = int(arg)
    except ValueError:
        current = game.get("dice_max", {}).get(user_id, 6)
        return f"❌ 请输入数字（6-12），当前点数：{current}"

    if new_max < 6 or new_max > 12:
        current = game.get("dice_max", {}).get(user_id, 6)
        return f"❌ 骰子最大点数范围为 6-12，当前点数：{current}"

    dice_max = game.get("dice_max", {})
    dice_max[user_id] = new_max
    game["dice_max"] = dice_max

    return f"🎲 你的骰子最大点数已调整为 {new_max}（下次投骰生效）"

def _dispatch_td(cmd: str, arg: str, group_id: int | None, user_id: int, nickname: str) -> tuple[Optional[str], list[int]]:
    """真心话大冒险命令分发，返回 (result_str, at_user_ids)"""
    # 这些命令不需要群聊上下文
    if cmd == "add_truth":
        return (add_truth_question(arg), [])
    elif cmd == "add_dare":
        return (add_dare_question(arg), [])
    elif cmd == "questions":
        return (get_questions(arg), [])
    elif cmd == "view_history":
        return (view_history(user_id), [])
    elif cmd == "clear_history":
        return (clear_history(user_id), [])

    if group_id is None:
        return ("🎮 真心话大冒险需要在群聊中使用", [])

    if cmd == "truth":
        return (start_truth_dare("truth", user_id, nickname, group_id), [])
    elif cmd == "dare":
        return (start_truth_dare("dare", user_id, nickname, group_id), [])
    elif cmd == "mixed":
        return (start_truth_dare("mixed", user_id, nickname, group_id), [])
    elif cmd == "join":
        return (join_truth_dare(user_id, nickname, group_id), [])
    elif cmd == "leave":
        return (leave_truth_dare(user_id, nickname, group_id), [])
    elif cmd == "pick":
        return (pick_card(group_id), [])
    elif cmd == "roll":
        return manual_roll(group_id, user_id)
    elif cmd == "next":
        return next_round(group_id)
    elif cmd == "kick":
        return kick_player(arg, group_id, user_id, nickname)
    elif cmd == "end":
        # 没有真心话游戏时返回 None，让路由器继续传递给海龟汤/卧底等模块
        if group_id not in _TRUTH_DARE_GAMES:
            return (None, [])
        return (end_game(group_id), [])
    elif cmd == "status":
        return (get_game_status(group_id), [])
    elif cmd == "probability":
        return (set_probability(arg, group_id), [])
    elif cmd == "dice_count":
        return (set_dice_count(arg, group_id, user_id), [])
    elif cmd == "dice_max":
        return (set_dice_max(arg, group_id, user_id, nickname), [])
    elif cmd == "simple_mode":
        return (set_display_mode("simplified", group_id), [])
    elif cmd == "custom_mode":
        return (set_display_mode("custom", group_id), [])
    elif cmd == "full_mode":
        return (set_display_mode("full", group_id), [])
    elif cmd == "self_select":
        return (list_self_select(group_id), [])
    elif cmd == "suggest_question":
        game = _TRUTH_DARE_GAMES.get(group_id)
        # BUG 修复：nickname 用函数入参（此前错误地用 int QQ 号当昵称），
        # user_desc 传 arg（此前硬编码空字符串导致 /提问建议 <描述> 描述被丢弃）
        return handle_suggest_question(group_id, user_id, nickname, arg, game)
    elif cmd == "cancel_suggestion":
        game = _TRUTH_DARE_GAMES.get(group_id)
        if game:
            game.pop("pending_suggestions", None)
            game.pop("suggestion_winner_uid", None)
            return ("✅ 已取消本次提问建议", [])
        return ("🎮 没有待取消的建议", [])
    elif cmd == "auto_mode":
        return handle_auto_mode(group_id, user_id)
    elif cmd == "spiciness":
        game = _TRUTH_DARE_GAMES.get(group_id)
        if not game:
            return "🎮 当前没有正在进行的真心话大冒险游戏，先发送 /真心话 开始游戏", []
        if arg.strip():
            # 修改色色程度：仅管理员可用
            from core.database import is_admin
            if not is_admin(user_id):
                return "🔒 只有管理员才能修改色色程度，当前可查看：/色色程度", []
        return (handle_spiciness(game, arg), [])
    return (None, [])
