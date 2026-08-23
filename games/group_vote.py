#!/usr/bin/env python3
"""
QQ 群投票功能
==============
- /投票 A B C ... 开启投票（至少 2 个选项）
- 群成员发送包含选项文字的消息即视为投票
- 每人只能投一票
- 60 秒后自动公布结果
"""

import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# {group_id: vote_state}
_active_votes: dict[int, dict] = {}

# 投票超时任务 {group_id: asyncio.Task}
_vote_tasks: dict[int, asyncio.Task] = {}

VOTE_DURATION = 120  # 投票持续时间（秒）


# ============================================================
#  投票状态管理
# ============================================================

def _get_vote(group_id: int) -> Optional[dict]:
    """获取当前群活跃的投票状态"""
    return _active_votes.get(group_id)


def is_active(group_id: int) -> bool:
    """该群是否有活跃的投票"""
    return group_id in _active_votes


def has_voted(group_id: int, user_id: int) -> bool:
    """用户是否已经投过票"""
    vote = _get_vote(group_id)
    if not vote:
        return False
    return user_id in vote["voters"]


def get_options(group_id: int) -> list[str]:
    """获取投票选项列表"""
    vote = _get_vote(group_id)
    return vote["options"] if vote else []


# ============================================================
#  创建投票
# ============================================================

def start_vote(group_id: int, options: list[str], creator_id: int,
                websocket=None, message_type: str = "group") -> tuple[bool, str]:
    """
    创建新投票。

    返回 (成功, 消息)
    """
    if len(options) < 2:
        return False, "⚠️ 投票至少需要 2 个选项\n用法：/投票 选项A 选项B 选项C ..."

    if len(options) > 10:
        return False, "⚠️ 投票选项不能超过 10 个"

    if is_active(group_id):
        return False, "⚠️ 当前已经有投票正在进行中，请等结束后再发起新的投票"

    # 构建选项显示
    option_lines = []
    for i, opt in enumerate(options, 1):
        option_lines.append(f"  {i}. {opt}")

    options_text = "\n".join(option_lines)

    vote_state = {
        "options": options,
        "voters": {},  # {user_id: option_text}
        "creator_id": creator_id,
        "start_time": time.time(),
        "end_time": time.time() + VOTE_DURATION,
    }

    _active_votes[group_id] = vote_state

    msg = (
        f"🗳️ 投票开始！\n\n"
        f"{options_text}\n\n"
        f"⏰ 投票将在 {VOTE_DURATION} 秒后结束\n"
        f"💡 发送选项文字或序号即可投票（每人一票，投完不影响聊天）"
    )

    # 启动超时任务
    task = asyncio.create_task(_vote_timeout(group_id, websocket, message_type))
    _vote_tasks[group_id] = task

    return True, msg


# ============================================================
#  投票
# ============================================================

def cast_vote(group_id: int, user_id: int, nickname: str, message: str) -> Optional[str]:
    """
    尝试投票。如果消息匹配某个选项（文字或序号）且用户尚未投票，则记录投票。

    返回回复消息，如果没有匹配或已经投过则返回 None。
    """
    vote = _get_vote(group_id)
    if not vote:
        return None

    # M17 修复：投票已过期（超时任务被 cancel/延迟时 end_time 已过仍可投票）
    # → 直接视为无投票，返回 None（等待超时结算或手动结束）
    if time.time() > vote.get("end_time", time.time() + VOTE_DURATION):
        return None

    # 检查是否已投票
    if user_id in vote["voters"]:
        return None  # 已投票，静默忽略

    msg_clean = message.strip()
    matched_option = None

    # 1. 数字序号匹配（"1"、"2"、"1。" 等 → 对应选项）
    # 去除常见后缀后判断是否为纯数字
    num_text = msg_clean.rstrip("。！？.,!?））")
    if num_text.isdigit():
        idx = int(num_text)
        if 1 <= idx <= len(vote["options"]):
            matched_option = vote["options"][idx - 1]

    # 2. 文字包含匹配（消息包含选项文字或选项包含消息）
    if matched_option is None:
        msg_lower = msg_clean.lower()
        for opt in vote["options"]:
            if opt.lower() in msg_lower or msg_lower in opt.lower():
                matched_option = opt
                break

    if matched_option is None:
        return None  # 不匹配任何选项

    # 记录投票
    vote["voters"][user_id] = matched_option

    return f"✅ {nickname} 投给了「{matched_option}」"


# ============================================================
#  超时结算
# ============================================================

async def _vote_timeout(group_id: int, websocket=None, message_type: str = "group"):
    """投票倒计时结束，自动公布结果"""
    await asyncio.sleep(VOTE_DURATION)
    from core.content_filter import censor_text
    result = censor_text(publish_result(group_id))
    if result and websocket:
        # 方案A（2026-08-23）：统一发送出口（发送门控单点判定）
        from core.sender import send_segments
        try:
            await send_segments(websocket, message_type, group_id,
                                [{"type": "text", "data": {"text": result}}])
            logger.info(f"✅ 投票超时自动公布结果: group_{group_id}")
        except Exception as e:
            logger.error(f"❌ 投票结果发送失败: {e}")


def publish_result(group_id: int) -> str:
    """
    公布投票结果并清理状态。

    返回结果消息文本。
    """
    vote = _active_votes.pop(group_id, None)
    if not vote:
        return ""

    # 取消超时任务
    # BUG 修复（2026-08-03）：原实现只 pop 不 cancel → 手动结束后旧超时任务
    # 仍会醒来，把新发起的投票误弹出并公布。改为 cancel。
    task = _vote_tasks.pop(group_id, None)
    if task and not task.done():
        task.cancel()

    options = vote["options"]
    voters = vote["voters"]
    total_votes = len(voters)

    # 统计各选项票数
    counts: dict[str, int] = {opt: 0 for opt in options}
    for opt in voters.values():
        counts[opt] = counts.get(opt, 0) + 1

    # 排序
    sorted_options = sorted(counts.items(), key=lambda x: -x[1])

    # 构建结果消息
    lines = ["📊 投票结果公布：", ""]

    for rank, (opt, count) in enumerate(sorted_options, 1):
        bar_len = count  # 每个票一个█
        bar = "█" * bar_len if count > 0 else ""
        emoji = "🥇" if rank == 1 and count > 0 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else "  "))
        lines.append(f"{emoji} {opt}: {count} 票 {bar}")

    lines.append("")
    lines.append(f"📝 共 {total_votes} 人参与投票")

    if total_votes == 0:
        lines.append("😅 无人参与，下次早点投票哦～")

    return "\n".join(lines)


def get_vote_status(group_id: int) -> Optional[str]:
    """获取当前投票状态（剩余时间、已投票数）"""
    vote = _get_vote(group_id)
    if not vote:
        return None

    remaining = max(0, int(vote["end_time"] - time.time()))
    total_votes = len(vote["voters"])
    total_options = len(vote["options"])

    return (
        f"🗳️ 投票进行中 | 剩余 {remaining}s | 已投票 {total_votes} 人\n"
        f"选项：{' / '.join(vote['options'])}"
    )


def end_vote(group_id: int) -> Optional[str]:
    """手动结束投票（提前公布结果）"""
    if not is_active(group_id):
        return None
    return publish_result(group_id)
