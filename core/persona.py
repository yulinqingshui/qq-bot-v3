"""
persona.py — 用户画像与人设管理模块
负责：画像更新、人设更新、JSON合并/解析/序列化、消息提取、后台任务队列
"""
import asyncio
import json
import logging
import re as _re
import time
from typing import Optional, Any

import httpx

from .database import get_db, get_persona_db, get_settings_db, set_cooldown, _session_key, get_cluster_master_group, get_cluster_id, get_cluster_groups
from .llm import call_llm, MAX_TOKENS_LONG, MAX_TOKENS_SHORT
from .sender import send_reply, _active_websocket, get_bot_uin
from .config import CONFIG, DEFAULTS
from . import persona_prompts

_SESSION_GAP_SECONDS = CONFIG.get("_SESSION_GAP_SECONDS", 1800)

# ────────────────────────────────────────────────────────────
#  人设/画像生成配置访问层（2026-08-21 设置功能）
#  全部读 CONFIG["PERSONA_CFG"]（热加载后即时生效），缺失回退 DEFAULTS。
# ────────────────────────────────────────────────────────────

def _pcfg() -> dict:
    return CONFIG.get("PERSONA_CFG") or DEFAULTS.get("persona", {})


def _pcfg_section(name: str) -> dict:
    """persona 段的嵌套子表（llm / persona_limits / profile_limits），逐键回退默认。"""
    p = _pcfg()
    cur = {k: v for k, v in DEFAULTS["persona"][name].items()}
    for k, v in (p.get(name) or {}).items():
        cur[k] = v
    return cur


def _llm_stage(stage: str) -> dict:
    """LLM 阶段调用参数：{max_tokens, temperature, thinking, json_mode, timeout}。"""
    return _pcfg_section("llm").get(stage, {})


def _llm_kwargs(stage: str) -> dict:
    """阶段参数 → call_llm 关键字参数（thinking: on/off/low/max 映射）。"""
    s = _llm_stage(stage)
    kw: dict = {
        "max_tokens": int(s.get("max_tokens", 16384)),
        "temperature": float(s.get("temperature", 0.7)),
        "json_mode": bool(s.get("json_mode", False)),
        "timeout": int(s.get("timeout", 300)),
    }
    th = str(s.get("thinking", "on")).lower()
    if th == "off":
        kw["disable_thinking"] = True
    elif th in ("low", "max"):
        kw["reasoning_effort"] = th
    # th == "on" → 不传（DeepSeek 后端默认 reasoning_effort=max）
    return kw


def _prompt(key: str, **ctx) -> str:
    """渲染提示词（CONFIG 用户定制优先 → 代码默认兜底，占位符字符串替换）。"""
    return persona_prompts.render_prompt(key, ctx)


def _persona_limits() -> dict:
    return _pcfg_section("persona_limits")


def _profile_limits() -> dict:
    return _pcfg_section("profile_limits")


def _p_total_range() -> tuple:
    l = _persona_limits()
    return int(l["total_min"]), int(l["total_max"])


def _pr_total_range() -> tuple:
    l = _profile_limits()
    return int(l["total_min"]), int(l["total_max"])


def _batch_chars() -> int:
    return int(_pcfg().get("batch_chars", 40000)) or 40000


def _direct_threshold() -> int:
    return int(_pcfg().get("direct_threshold", 36000)) or 36000


def _prompt_limit_ctx() -> dict:
    """人设 JSON 字段限制占位符上下文（提示词模板内 {identity_sub}/{interests_limit} 等）。"""
    l = _persona_limits()
    return {
        "identity_sub": int(l["identity_sub"]),
        "personality_limit": int(l["personality"]),
        "group_role_limit": int(l["group_role"]),
        "sexual_sub_limit": int(l["sexual_sub"]),
        "interests_limit": int(l["interests"]),
        "weaknesses_limit": int(l["weaknesses_taboos"]),
        "catchphrases_limit": int(l["catchphrases"]),
        "relationships_limit": int(l["relationships"]),
        "sexual_pref_limit": int(l["sexual_preferences"]),
        "persona_total_min": int(l["total_min"]),
        "persona_total_max": int(l["total_max"]),
        "persona_total_hard_max": int(l["total_hard_max"]),
    }

# Map 批次 gather 并发信号量：限制 asyncio.gather 同时运行的批次数量，
# 防止 batch 数量过大时一次性并发涌出（即使 DEEPSEEK_MAX_PARALLEL 配置错误也兜底）。
# 容量与 DEEPSEEK_MAX_PARALLEL 一致（默认 10），不改变既有并发语义。
_MAP_GATHER_CONCURRENCY = int(CONFIG.get("DEEPSEEK_MAX_PARALLEL", 10))
_MAP_GATHER_SEMAPHORE = asyncio.Semaphore(_MAP_GATHER_CONCURRENCY)


async def _map_gather_run(coro):
    """在 Map 批次信号量内运行协程，限制 gather 同时执行的批次数量。"""
    async with _MAP_GATHER_SEMAPHORE:
        return await coro

logger = logging.getLogger('qq-bot')


def _resolve_persona_group(group_id: int) -> int:
    """
    解析人设/画像的目标 group_id。

    集群场景：如果群在集群中，返回主群（group_clusters.master_group_id），
    实现同一用户共享人设/画像。
    非集群场景：返回原 group_id（各群独立）。
    """
    if group_id == 0:
        return group_id
    try:
        master = get_cluster_master_group(group_id)
        if master is not None:
            return master
    except Exception:
        pass
    return group_id


def _is_privileged_group(group_id: int) -> bool:
    """该群是否为最高权限群（标识 is_privileged_group=1，2026-08-12）。

    最高权限群 = 人设/画像【查看范围】特权：可回退查看其他所有群的数据；
    与机器人管理权限（is_admin）完全无关。
    """
    if not group_id:
        return False
    try:
        from .database import get_settings_db
        with get_settings_db() as conn:
            row = conn.execute(
                "SELECT is_privileged_group FROM group_cluster_members WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            return bool(row and row[0])
    except Exception:
        return False


def _persona_fallback_groups(group_id: int) -> list[int]:
    """人设/画像查询回退链（按优先级从高到低，2026-08-12）。

    最高权限群：本群 → 其他所有有数据的群（按群号倒序——新群优先，够用即可）。
    普通集群群：本群 → 主群（原逻辑）。
    非集群群：仅本群。
    调用方依次查询，找到即返回（_source_group 标注实际来源）。
    """
    if not group_id:
        return [0]
    if _is_privileged_group(group_id):
        try:
            # 查"其他群"必须用 personas.db（get_persona_db）——2026-08-12 修复：
            # 原误用 get_settings_db（bot_settings.db 无 user_personas 表，异常被吞→回退链只有本群）
            from .database import get_persona_db
            with get_persona_db() as conn:
                others = [r[0] for r in conn.execute(
                    "SELECT DISTINCT group_id FROM user_personas WHERE group_id != ? AND group_id != 0 "
                    "UNION SELECT DISTINCT group_id FROM user_profiles WHERE group_id != ? AND group_id != 0",
                    (group_id, group_id),
                ).fetchall()]
            if others:
                return [group_id] + sorted(others, reverse=True)
        except Exception:
            pass
        return [group_id]
    master = _resolve_persona_group(group_id)
    return [group_id, master] if master != group_id else [group_id]


def _get_persona_target_groups(group_id: int) -> list[int]:
    """
    获取人设/画像消息提取的目标群列表。

    集群场景：返回集群内所有群号（合并所有群消息）。
    非集群场景：返回 [group_id]（仅当前群）。
    """
    if group_id == 0:
        return []
    try:
        cid = get_cluster_id(group_id)
        if cid:
            members = get_cluster_groups(cid)
            if members:
                return [m["group_id"] for m in members]
    except Exception:
        pass
    return [group_id]

# 最大 LLM 重试次数（防止 while not 无限循环导致数据库疯狂读写）
_MAX_LLM_RETRIES = 5

# ---- 网络异常盲区补丁（2026-08-06）：超时/连接错误不再直接跳过 ----
# 背景：call_llm 只对 429/503 做 HTTP 层重试，TransportError（超时/连接错误）
# 会直接上抛，业务层重试循环无 try/except → 该用户本轮被跳过。此处统一补上。
_NET_RETRY_LIMIT = 3    # 网络异常额外重试次数（退避 2s/4s/8s，共 4 次尝试）
_NET_RETRY_BASE = 2.0   # 指数退避基数


class LLMNetworkExhausted(Exception):
    """LLM 网络异常（超时/连接错误等 TransportError）重试耗尽。"""


async def _call_llm_net(messages, *, net_retries=None, **kwargs) -> str:
    """call_llm 封装：补网络异常盲区。

    - 429/503 限流：call_llm 内部已重试（3 次，退避 2s/4s），此处不重复处理
    - TransportError（超时/连接错误/网络中断）：指数退避重试 net_retries 次
      （默认 3 次 → 共 4 次尝试），全部失败抛 LLMNetworkExhausted
    - 其余异常原样上抛（业务 bug 不吞）
    调用方业务循环捕获 LLMNetworkExhausted 后按失败处理（跳过本轮，等下一轮）。
    """
    if net_retries is None:
        net_retries = int(_pcfg().get('net_retries', 3))
    for attempt in range(1, net_retries + 2):
        try:
            return await call_llm(messages, **kwargs)
        except httpx.TransportError as e:
            if attempt <= net_retries:
                backoff = _NET_RETRY_BASE * (2 ** (attempt - 1))
                logger.warning(
                    f"🔄 LLM 网络异常({type(e).__name__})，{backoff:.0f}s 后重试 "
                    f"({attempt}/{net_retries + 1})"
                )
                await asyncio.sleep(backoff)
                continue
            logger.error(f"❌ LLM 网络异常重试耗尽（{net_retries + 1} 次尝试）")
            raise LLMNetworkExhausted(
                f"LLM 网络异常重试耗尽: {type(e).__name__}: {e}") from e
    # 不可达（循环内必 return 或 raise），仅满足类型检查
    raise LLMNetworkExhausted("LLM 网络异常重试耗尽")

# 批次处理大小：Map→Reduce 分批的字符数上限（~近似 token 数）
# 所有画像/人设/联合更新路径共用的统一批次大小，修改此值全局生效。
# 2026-08-25 用户配置：输入消息按 40000 token（~字符）分段。
BATCH_CHARS = int(CONFIG.get("PERSONA_CFG", {}).get("batch_chars", 40000)) or 40000
# ↑ 模块常量保留（router.py import 用）；本模块内部使用点已改动态读 _batch_chars()

# 直接调用阈值：聊天文本小于该值时不走 Map→Reduce，直接单次 LLM 调用。
# 跟随 _batch_chars() 按 0.9 倍缩放（原 5000 批次对应 4500）。
DIRECT_THRESHOLD = int(CONFIG.get("PERSONA_CFG", {}).get("direct_threshold", 36000)) or 36000
# ↑ 模块常量保留（兼容）；本模块内部使用点已改动态读 _direct_threshold()

# LLM 返回的错误消息前缀（call_llm 异常时返回的友好提示）
# 2026-08-21 事故补充："🔕"（llm.enabled=false 时 call_llm 的降级返回串
# "🔕 LLM 已关闭（总览页 LLM 板块可开启）"——曾漏过本检查，被画像 Reduce
# 当有效输出写库，覆盖了真实画像；同时把降级串当"1 道题"解析，引发
# 题库补充死循环 15h/3.5GB）
_LLM_ERROR_PREFIXES = (
    "😵", "⏳", "🤔", "🔕",
    "模型在想什么呢", "思考时间太长啦", "模型那边出了点小问题",
)


def _is_llm_error(text: str) -> bool:
    """检测 LLM 是否返回了错误提示而非有效结果"""
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in _LLM_ERROR_PREFIXES)


# ============================================================
#  消息提取与格式化辅助函数
# ============================================================

def _extract_relevant_messages(profile_user_id: int, target_id: int = 0, last_message_id: int = 0, last_scan_at: float = 0) -> list[dict]:
    """
    提取与目标用户相关的消息，用于画像分析。

    策略：
    - 主动发言：目标用户发送的所有消息
    - 被动互动：他人 @目标用户 或回复目标用户的消息
    - 上下文窗口：保留目标用户发言前后 8 条群消息（2026-08-25 用户配置）
    - 集群合并：如果群在集群中，合并所有群的消息按时间顺序提取

    返回按 created_at 排序的去重消息列表。
    每个元素：{id, message_id, user_id, nickname, content, created_at}
    """
    # 获取目标群列表：集群内所有群，非集群则只有当前群
    target_group_ids = _get_persona_target_groups(target_id)

    # 构建 user_msgs 查询：目标用户的所有消息
    base_where = "WHERE user_id = ?"
    base_params = [profile_user_id]
    if target_group_ids:
        placeholders = ",".join(["?"] * len(target_group_ids))
        base_where += f" AND target_id IN ({placeholders})"
        base_params.extend(target_group_ids)

    with get_db() as conn:
        if last_scan_at > 0:
            q = f"SELECT id, message_id, user_id, nickname, content, raw_message, created_at " \
                f"FROM message_archive {base_where} AND created_at > ? ORDER BY created_at ASC"
            params = base_params + [last_scan_at]
            user_msgs = conn.execute(q, params).fetchall()
        elif last_message_id > 0:
            q = f"SELECT id, message_id, user_id, nickname, content, raw_message, created_at " \
                f"FROM message_archive {base_where} AND message_id > ? ORDER BY created_at ASC"
            params = base_params + [last_message_id]
            user_msgs = conn.execute(q, params).fetchall()
        else:
            q = f"SELECT id, message_id, user_id, nickname, content, raw_message, created_at " \
                f"FROM message_archive {base_where} ORDER BY created_at DESC LIMIT 1000000"
            user_msgs = conn.execute(q, base_params).fetchall()
            user_msgs = list(reversed(user_msgs))

    if not user_msgs:
        return []

    target_message_ids = set(str(row["message_id"]) for row in user_msgs)

    first_ts = user_msgs[0]["created_at"]
    last_ts = user_msgs[-1]["created_at"]

    at_pattern = f"[CQ:at,qq={profile_user_id}]"

    # 构建上下文查询：目标用户消息前后的群消息
    # 窗口 OR 条件（2026-08-08 修复）：
    #   1. 目标用户消息 ±60s 上下文窗口（原逻辑）
    #   2. @目标用户 的消息按断点独立提取（目标用户沉默期别人 @TA 的线索不丢）
    #   3. 回复目标用户的消息（reply CQ 码）在 [first_ts, last_ts+1h] 独立提取
    #      （回复发生在目标用户消息之后，1 小时覆盖绝大多数回复；具体是否回复了
    #       目标用户由 relevant_ids 的 reply id 匹配判定）
    # instr() 避免 LIKE 特殊字符转义（[CQ:at,...] 的 [ 是 SQLite LIKE 字符类）
    context_where = (
        "WHERE ((created_at >= ? AND created_at <= ?) "
        "OR (instr(raw_message, ?) > 0 AND created_at >= ?) "
        "OR (instr(raw_message, '[CQ:reply,id=') > 0 "
        "    AND created_at >= ? AND created_at <= ?))"
    )
    context_params = [
        first_ts - 60, last_ts + 60,
        at_pattern, last_scan_at,
        first_ts, last_ts + 3600,
    ]
    if target_group_ids:
        placeholders = ", ".join(["?"] * len(target_group_ids))
        context_where += f" AND target_id IN ({placeholders})"
        context_params.extend(target_group_ids)

    with get_db() as conn:
        all_msgs = conn.execute(
            f"SELECT id, message_id, user_id, nickname, content, raw_message, created_at "
            f"FROM message_archive {context_where} ORDER BY created_at ASC",
            context_params,
        ).fetchall()

    if not all_msgs:
        return []

    at_pattern = f"[CQ:at,qq={profile_user_id}]"
    reply_re = _re.compile(r"\[CQ:reply,id=(\d+)\]")
    bot_qq_ext = int(get_bot_uin() or 0)  # 08-22：从连接派生

    relevant_ids = set()
    for msg in all_msgs:
        if msg["user_id"] == profile_user_id:
            relevant_ids.add(msg["id"])
        elif at_pattern in msg["raw_message"]:
            relevant_ids.add(msg["id"])
        else:
            m = reply_re.search(msg["raw_message"])
            if m and m.group(1) in target_message_ids:
                relevant_ids.add(msg["id"])

    id_to_index = {msg["id"]: i for i, msg in enumerate(all_msgs)}
    # 上下文窗口时间上限：与 _split_into_sessions 的 _SESSION_GAP_SECONDS 一致（30 分钟）。
    # 稀疏时段按条数取"前 8 条"会回溯数小时、夹带与目标用户无关的他人独白（跨对话批次），
    # 故对窗口内每条消息校验与目标用户相关消息的时间差，超限则不纳入。相关消息本身
    # （目标用户发言 / @目标用户 / 回复目标用户）已通过 relevant_ids 独立保留，不受限。
    context_ids = set()
    for rel_id in relevant_ids:
        idx = id_to_index.get(rel_id)
        if idx is not None:
            # bot 回复：只保留自身，不展开 ±8 条上下文（2026-08-08）
            # 它紧贴用户 @bot 消息（+0.01s 锚定），语境已由用户消息的窗口覆盖；
            # 展开反而重复提取 + 带入回复后的无关新消息
            if all_msgs[idx]["user_id"] == bot_qq_ext:
                context_ids.add(rel_id)
                continue
            # 上下文窗口：目标用户发言/相关引用消息前后统一保留 8 条群消息（2026-08-25 用户配置）
            window = int(_pcfg().get('context_window', 8))
            rel_ts = all_msgs[idx]["created_at"]
            for offset in range(-window, window + 1):
                target_idx = idx + offset
                if 0 <= target_idx < len(all_msgs):
                    ctx_msg = all_msgs[target_idx]
                    # 时间跨度上限：窗口消息与目标用户相关消息超过 30 分钟视为跨对话批次，
                    # 不作为上下文纳入（防止把数小时前的凌晨独白夹带进当前批次）。
                    if abs(ctx_msg["created_at"] - rel_ts) > _SESSION_GAP_SECONDS:
                        continue
                    if (ctx_msg["user_id"] != profile_user_id and
                        at_pattern not in ctx_msg["raw_message"] and
                        not (reply_re.search(ctx_msg["raw_message"]) and
                             reply_re.search(ctx_msg["raw_message"]).group(1) in target_message_ids)):
                        if abs(offset) <= window:
                            context_ids.add(ctx_msg["id"])
                    else:
                        context_ids.add(ctx_msg["id"])

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


def _clean_cq_codes(text: str, target_user_id: int = 0, nickname_map: dict[int, str] | None = None,
                     uid_to_short: dict[int, str] | None = None) -> str:
    """将 CQ 码转换为人类可读文本，避免 LLM 误解。

    uid_to_short 提供时，@数字 统一映射为 @U短ID（如 @U3），与人物映射表一致；
    @目标用户 同样映射为 @U短ID，不保留特殊标记。
    不提供时保持原行为（@昵称 / @目标用户）。
    """
    if nickname_map is None:
        nickname_map = {}

    text = _re.sub(r'\[CQ:face,id=\d+\]', '[表情]', text)
    text = _re.sub(r'\[CQ:face,type=\d+\]', '[表情]', text)
    text = _re.sub(r'\[CQ:reply,id=\d+\]', '[回复]', text)
    text = _re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', text)
    text = _re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', text)
    text = _re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', text)
    text = _re.sub(r'\[CQ:share,[^\]]*\]', '[链接分享]', text)
    text = _re.sub(r'\[CQ:music,[^\]]*\]', '[音乐分享]', text)

    def _replace_at_cq(m: _re.Match) -> str:
        at_uid = int(m.group(1))
        # uid_to_short 提供时：@目标用户 也映射为 @U短ID，与其他 @ 一致
        if uid_to_short is not None and at_uid in uid_to_short:
            return f'@{uid_to_short[at_uid]}'
        if at_uid == target_user_id and uid_to_short is None:
            return '@目标用户'
        nick = nickname_map.get(at_uid)
        if not nick:
            nick_m = _re.search(r'nickname=([^,\]]+)', m.group(0))
            nick = nick_m.group(1) if nick_m else '某人'
        return f'@{nick}'

    text = _re.sub(r'\[CQ:at,qq=(\d+)(?:,nickname=[^\]]*)?\]', _replace_at_cq, text)
    text = _re.sub(r'\[CQ:\w+,[^\]]*\]', '[CQ码]', text)

    # uid_to_short 提供时，@目标用户 也走统一映射（_replace_at_id 处理）
    if target_user_id > 0 and uid_to_short is None:
        text = _re.sub(rf'@{target_user_id}\b', '@目标用户', text)

    def _replace_at_id(m: _re.Match) -> str:
        at_uid = int(m.group(1))
        if uid_to_short is not None and at_uid in uid_to_short:
            return f'@{uid_to_short[at_uid]}'
        nick = nickname_map.get(at_uid, '某人')
        return f'@{nick}'

    text = _re.sub(r'@(\d{5,})', _replace_at_id, text)
    return text


# ============================================================
# U 短 ID 引用归一化：LLM 输出中的 U 编号 → 昵称(qq号)
# （QQ 号作为稳定锚点：用户改昵称后跨批次/跨轮次仍可对齐）
# ============================================================

def _build_short_map(uid_to_short: dict, nickname_map: dict) -> dict:
    """构建 short → {"nickname": ..., "qq": ...} 映射，供 _normalize_u_refs 使用。"""
    return {
        short: {"nickname": nickname_map.get(uid, str(uid)), "qq": str(uid)}
        for uid, short in uid_to_short.items()
    }


# LLM 输出中可能的 U 引用形式：
#   A. "U8_痴呆"     — U编号+下划线+昵称（主要出现在 JSON 键，由 _normalize_u_ref_key 处理；
#                      文本中出现时无法与正文可靠区分，保持原样不误伤）
#   B. "@U9"         — @ 引用
#   C. "U3、U6、U8"  — 独立 U 编号
_U_REF_AT_RE = _re.compile(r'@U(\d+)(?![A-Za-z0-9_])')
_U_REF_BARE_RE = _re.compile(r'(?<![A-Za-z0-9@_])U(\d+)(?![A-Za-z0-9_])')


def _normalize_text_u_refs(text: str, short_map: dict | None) -> str:
    """把文本中的 U 编号引用替换为 昵称(qq号)。short_map 缺失或无匹配时原样保留。"""
    if not text or not short_map:
        return text
    # 占位符法：先替换为不可见占位符，再统一回填，避免回填内容被二次匹配
    placeholders: dict[str, str] = {}
    counter = 0

    def _ph(m: _re.Match) -> str:
        nonlocal counter
        short = "U" + m.group(1)
        info = short_map.get(short)
        if not info:
            return m.group(0)
        ph = f"\x00U{counter}\x00"
        placeholders[ph] = f"{info['nickname']}({info['qq']})"
        counter += 1
        return ph

    text = _U_REF_AT_RE.sub(lambda m: "@" + _ph(m), text)
    text = _U_REF_BARE_RE.sub(_ph, text)
    for ph, repl in placeholders.items():
        text = text.replace(ph, repl)
    return text


def _normalize_u_ref_key(key: str, short_map: dict | None) -> str:
    """替换 JSON 键中的 U 引用（relationships 键 "U8_痴呆" / "U8" / "@U8"）。"""
    if not short_map:
        return key
    m = _re.match(r'^@?U(\d+)$', key)
    if m and ("U" + m.group(1)) in short_map:
        info = short_map["U" + m.group(1)]
        return f"{info['nickname']}({info['qq']})"
    m = _re.match(r'^U(\d+)_', key)
    if m and ("U" + m.group(1)) in short_map:
        info = short_map["U" + m.group(1)]
        return f"{info['nickname']}({info['qq']})"
    return key


def _normalize_u_refs(obj, short_map: dict | None = None) -> Any:
    """递归替换 dict/list/str 中的 U 编号引用为 昵称(qq号)。"""
    if isinstance(obj, dict):
        result: dict = {}
        for k, v in obj.items():
            nk = _normalize_u_ref_key(k, short_map) if isinstance(k, str) else k
            result[nk] = _normalize_u_refs(v, short_map)
        return result
    if isinstance(obj, list):
        return [_normalize_u_refs(item, short_map) for item in obj]
    if isinstance(obj, str):
        return _normalize_text_u_refs(obj, short_map)
    return obj


def _strip_self_relationships(persona: dict | None, target_user_id: int) -> bool:
    """兜底校验：剔除 relationships 中指向目标用户自身的条目。

    人不可能与自己建立"关系"，若 LLM 把目标用户自己的 QQ 号写进了
    relationships 键（归一化后形如 "昵称(QQ号)"），说明 LLM 认错了人，
    该条目 100% 是错误，直接剔除并告警。
    返回是否剔除了条目（供调用方记录日志）。
    """
    if not isinstance(persona, dict):
        return False
    rel = persona.get("relationships")
    if not isinstance(rel, dict) or not rel:
        return False
    removed = []
    target_str = str(target_user_id)
    for key in list(rel.keys()):
        # 归一化后键格式为 "昵称(QQ号)"；LLM 也可能输出纯昵称（无 QQ 号），
        # 此时无法可靠识别，仅当键中明确包含目标 QQ 号时剔除
        if f"({target_str})" in str(key):
            removed.append(key)
            del rel[key]
    if removed:
        logger.warning(
            f"⚠️ 检测到目标用户 {target_user_id} 被 LLM 写入 relationships（疑似认错人），"
            f"已剔除 {len(removed)} 条: {removed}"
        )
        return True
    return False


def _build_session_header(
    session_msgs: list[dict],
    nickname_map: dict[int, str] | None,
    uid_to_short: dict[int, str],
    target_user_id: int = 0,
) -> str:
    """构建人物映射表头（含 QQ 号与目标用户标记）。

    格式：U1=昵称(QQ号) ← 目标用户
    - QQ 号是全局唯一锚点：昵称可跨群/随时改名，QQ 号不变，避免 LLM 认错人
    - 目标用户行加 '← 目标用户' 标记，LLM 可直接用 U 编号锚定提取对象
    """
    seen_uids: list[int] = []
    for msg in session_msgs:
        if msg["user_id"] not in seen_uids:
            seen_uids.append(msg["user_id"])
    nick_map_lines = []
    for uid in seen_uids:
        short = uid_to_short[uid]
        nick = nickname_map.get(uid, "") if nickname_map else ""
        if not nick:
            # 从消息中取昵称
            for msg in session_msgs:
                if msg["user_id"] == uid:
                    nick = msg["nickname"]
                    break
        marker = " ← 目标用户" if (target_user_id > 0 and uid == target_user_id) else ""
        nick_map_lines.append(f"{short}={nick}({uid}){marker}")
    return "人物:\n" + "\n".join(nick_map_lines) + "\n\n"


def _split_long_session_chunks(
    sess: list[dict],
    target_user_id: int,
    nickname_map: dict[int, str],
    uid_to_short: dict[int, str],
    time_fmt: str = "%H:%M",
) -> list[str]:
    """将超长 Session（> _batch_chars()）切分为多个子块，每个子块自带人物映射表头。

    修复：原实现分块后丢失人物映射（或人设路径直接用昵称），LLM 无法确认
    U 编号对应谁，导致张冠李戴。现在每块开头都带 '人物:' 表头（含 QQ 号与
    '← 目标用户' 标记），行格式与 _format_session_text 对齐为 '#序号 HH:MM U1: 内容'
    （序号为整个 session 的行号，跨子块连续，不重新计数）。
    """
    from datetime import datetime

    header = _build_session_header(sess, nickname_map, uid_to_short, target_user_id)

    lines: list[str] = []
    for i, msg in enumerate(sess, 1):
        dt = datetime.fromtimestamp(msg["created_at"])
        time_str = dt.strftime(time_fmt)
        content = _clean_cq_codes(msg["content"], target_user_id, nickname_map, uid_to_short)
        if len(content) > 300:
            content = content[:300] + "..."
        # 消息内真实换行转义为字面 \n（一条消息一行契约，方案B）
        content = content.replace("\n", "\\n")
        short = uid_to_short[msg["user_id"]]
        lines.append(f"#{i} {time_str} {short}: {content}\n")

    sub_chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line)
        if current_len > 0 and current_len + line_len > _batch_chars():
            sub_chunks.append(header + "".join(current_lines))
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len
    if current_lines:
        sub_chunks.append(header + "".join(current_lines))
    return sub_chunks


def _format_session_text(session_msgs: list[dict], target_user_id: int = 0, nickname_map: dict[int, str] | None = None, uid_to_short: dict[int, str] | None = None) -> str:
    """
    将一个 Session 的消息格式化为紧凑文本（节省 token）。
    
    格式：14:05 U1: 内容
    - 时间只保留时分
    - 用户名用短 ID（U1, U2...）
    - 合并同一用户连续发言
    - 返回 "人物映射" + 消息体
    - uid_to_short 传入时使用外部全局编号（批次内跨 session 唯一，LLM 输出可归一化）；
      不传时按 session 内出现顺序编号（向后兼容）
    """
    from datetime import datetime
    
    # 构建 user_id → 短 ID 映射（外部传入则复用，否则按本 session 出现顺序编号）
    if uid_to_short is None:
        uid_to_short = {}
        counter = 1
        for msg in session_msgs:
            uid = msg["user_id"]
            if uid not in uid_to_short:
                uid_to_short[uid] = f"U{counter}"
                counter += 1
    
    # 格式化 + 合并连续消息
    formatted: list[tuple[str, int, str]] = []  # (time, uid, content)
    for msg in session_msgs:
        dt = datetime.fromtimestamp(msg["created_at"])
        time_str = dt.strftime("%H:%M")
        content = _clean_cq_codes(msg["content"], target_user_id, nickname_map, uid_to_short)
        if len(content) > 300:
            content = content[:300] + "..."
        # 消息内真实换行转义为字面 \n（一条消息一行契约，方案B）
        content = content.replace("\n", "\\n")
        uid = msg["user_id"]
        # 合并同一用户连续消息
        if formatted and formatted[-1][1] == uid and formatted[-1][0] == time_str:
            prev_content = formatted[-1][2]
            merged = prev_content + content
            if len(merged) <= 300:
                formatted[-1] = (time_str, uid, merged)
            else:
                formatted.append((time_str, uid, content))
        else:
            formatted.append((time_str, uid, content))
    
    # 构建人物映射（只列本 session 出现的用户，编号取传入映射；含 QQ 号与目标标记）
    header = _build_session_header(session_msgs, nickname_map, uid_to_short, target_user_id)
    
    # 构建消息行（带序号）
    lines = []
    for i, (time_str, uid, content) in enumerate(formatted, 1):
        short = uid_to_short[uid]
        lines.append(f"#{i} {time_str} {short}: {content}")
    
    return header + "\n".join(lines)


def _split_into_sessions(messages: list[dict], gap_seconds: int = _SESSION_GAP_SECONDS) -> list[list[dict]]:
    """按时间间隔切分为多个对话 Session（30 分钟无发言视为一次会话结束）"""
    if not messages:
        return []
    sessions = []
    current = [messages[0]]
    for msg in messages[1:]:
        if msg["created_at"] - current[-1]["created_at"] > gap_seconds:
            sessions.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        sessions.append(current)
    return sessions


def chunk_messages_by_token(messages: list[str], target_tokens: Optional[int] = None) -> list[list[str]]:
    """按累计 token 数（~字符数）对消息列表分批。target_tokens 缺省读配置 batch_chars。"""
    if target_tokens is None:
        target_tokens = _batch_chars()
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


# ============================================================
#  公共聊天材料准备（画像/人设/联合更新共用）
# ============================================================

def _prepare_chat_messages(
    user_id: int, group_id: int,
    last_message_id: int = 0,
    last_scan_at: float = 0,
) -> tuple[list[dict], int, float, dict[int, str], str]:
    """
    公共聊天材料准备：提取消息 → 切分 Session → 构建 nickname_map → 格式化 → 分批。
    返回 (messages, last_id, last_scan_at, nickname_map, chat_log_text)
    """
    messages = _extract_relevant_messages(user_id, group_id, last_message_id, last_scan_at)
    if not messages:
        return [], 0, 0, {}, ""

    last_id = messages[-1]["message_id"]
    last_scan_at = messages[-1]["created_at"]

    sessions = _split_into_sessions(messages)

    nickname_map: dict[int, str] = {}
    for msg in messages:
        uid = msg["user_id"]
        # 一直覆盖：messages 按时间升序，最后赋值的 = 最新昵称（用户可能改过名）
        nickname_map[uid] = msg["nickname"]

    session_texts: list[str] = []
    for sess in sessions:
        text = _format_session_text(sess, user_id, nickname_map)
        if len(text) > _batch_chars():
            uid_to_short: dict[int, str] = {}
            _counter = 1
            for m in sess:
                if m["user_id"] not in uid_to_short:
                    uid_to_short[m["user_id"]] = f"U{_counter}"
                    _counter += 1
            session_texts.extend(_split_long_session_chunks(sess, user_id, nickname_map, uid_to_short))
        else:
            session_texts.append(text)

    chat_log_text = "\n\n--- 对话分隔 ---\n\n".join(session_texts)
    return messages, last_id, last_scan_at, nickname_map, chat_log_text


# ============================================================
#  Combined Map 提示词与调用函数
# ============================================================

_COMBINED_MAP_SYSTEM = None
_COMBINED_MAP_USER_TEMPLATE = None
_COMBINED_MAP_LOW_INFO = None

# 人物映射表格式说明（各提取路径提示词共用）：
# 映射表行格式为 "U编号=昵称(QQ号)"，目标用户行带 "← 目标用户" 标记。
# QQ 号是唯一锚点：昵称可能跨群不同/随时改名，绝不能仅凭昵称认人。
_MAPPING_FORMAT_RULE = (
    '📋【人物映射表使用说明】聊天记录头部"人物:"表即人物映射表，每行格式为'
    '"U编号=昵称(QQ号)"，其中标注"← 目标用户"的 U 编号就是本次要提取信息的'
    '目标用户本人（QQ号是唯一锚点，昵称可能跨群不同或随时改名，不要仅凭昵称认人）。'
    '只有该 U 编号的发言才能作为目标用户的证据提取；其他 U 编号（未标注）的发言'
    '仅为上下文参考，禁止提取为目标用户的信息。\n'
    '⚠️ 消息文本中出现"@机器人"表示该条消息是对机器人的发言，归属对象是机器人，'
    '不是任何群友；昵称为"机器人"的 U 编号是机器人的回复，仅作对话上下文理解，'
    '不得把目标用户的发言与之混淆。\n\n'
)

# 发言真实性分级框架：四条提取路径（Combined Map / 画像 Map / 人设 Map / 直接分析）共用。
# 任何身份/现实信息的提取必须先按此分级，只有 L1/L2 可作为证据。
_TRUTH_GRADING_RULES = (
    '【发言真实性分级框架】（提取任何身份/现实信息前，先对目标用户的相关发言分类；只有 L1/L2 可作为身份与现实证据）：\n'
    'L1 自我披露：本人独立、直接、语境严肃的陈述（如认真回答"你多大"→"大二"）。✅ 可直接提取。\n'
    'L2 回应式披露：被正经提问（问题本身语境严肃、非调侃非游戏）后本人的回答；问题本身不是证据，回答是。✅ 可提取。⚠️ 若问题是调侃/玩梗式（"你是不是24岁处男"），或发生在游戏/挑战/真心话等娱乐语境，回答降级为 L3。\n'
    'L3 玩梗/口嗨：夸张自嘲、网络流行梗（"20岁宝爸自学西医"）、接话复述（别人先说"你24岁"你回"24岁，事学生"）、挑战活动里的调侃。❌ 除非同一信息多次独立出现（独立 = 不同时间/不同话题语境；同一场梗局里的连续复读只算一次），否则不提取。\n'
    'L4 扮演/假设：装嫩装萝莉（"我才十几岁"）、扮演设定（临时人设）、假设句（"如果我是…"）、愿望句（"我要一个这样的妹妹"）、身份揭露式玩梗（"其实我是男的，伪女""摊牌了我是小男娘"——以"其实/摊牌/坦白"等戏剧化转折+表演性身份声明的句式是群聊经典玩梗，默认按扮演处理，除非上下文明确为严肃自我披露）、/指令中的设定描述（如"/分析 你是一个20岁…"）。❌ 一律不提取为现实信息（临时人设内容仅可作群内人设参考，不作现实证据）。\n'
    'L5 他人转述/抽象讨论/涉他陈述：群友对目标用户的调侃描述、本人转述他人评价（"我朋友说我是…"）、与本人无关的一般性讨论（"12岁和24岁的人恋爱"是讨论年龄差）、涉及他人而非本人的陈述（"九龙坡记住了"是回应他人在九龙坡）。❌ 一律不提取。\n'
    '【否定处理】本人对某信息的明确否定（"我不在重庆""我不是学生"）按 L1 处理且优先级最高：被本人否定过的值不得写入该字段；若已有旧值，以否定为准（该字段留空或写入否定后的新状态）。\n'
    '【隐私字段加严】性经历/性偏好/身体数据在技巧③基础上再加严：仅 L1 且语境严肃可提取；L2 仅当问题本身严肃；L3/L4/L5 一律不提取。\n'
    '【性别特例】性别可依据本人自称用词（爷/老子/姐等）提取：自称 ≥2 次，或 1 次自称且无反向证据；群友称呼不算。\n'
    '判断技巧：① 看目标消息前 5 条左右，同一关键词先由别人提出 → 大概率接梗；② 看语境：正经讨论或提问后的回答 = 认真，玩梗话题/涩图话题/挑战活动 = 大概率玩梗；③ 身份类字段（年龄/位置/职业/感情/身体）⚠️ 只认直接证据：本人明确的陈述或自称称谓（"我是女的""姐""老子""我现居重庆"）才可定值；间接信号（对他人称呼"兄弟/哥们"、玩梗人设"当皇帝"、说话风格、话题偏好、群友反应）只能提示线索，禁止作为判定依据。满足任一即可提取：1 次 L1 自我披露；或 1 次正经语境下的 L2 回答；或 ≥2 次独立出现且至少 1 次为 L1/L2（纯玩梗不能凑数）。不满足则留空；多个可信出现以时间最新为准；⚠️ 若同一字段存在互相矛盾的本人表达（如既说"我是男的"又说"我是女的"，或自称"老子"又说"老娘"），说明目标用户处于玩梗/表演模式，该字段一律留空、绝不选边；④ /开头的消息是机器人指令，指令中的设定描述按 L4 处理，不是现实证据。\n\n'
)


def _get_combined_map_system(nickname: str) -> str:
    """联合 Map system 提示词（模板见 core/persona_prompts.py: combined_map_system，可 GUI 编辑）。"""
    return _prompt("combined_map_system", nickname=nickname)


def _get_combined_map_user(batch_text: str, nickname: str) -> str:
    """联合 Map user 提示词（模板见 core/persona_prompts.py: combined_map_user）。"""
    return _prompt("combined_map_user", nickname=nickname, batch_text=batch_text)


def _get_combined_map_low_info() -> dict:
    global _COMBINED_MAP_LOW_INFO
    if _COMBINED_MAP_LOW_INFO is None:
        _COMBINED_MAP_LOW_INFO = {
            "persona": {},
            "profile_material": {"reality": "", "group_persona": "", "social": "", "language_style": "", "quotes": []},
            "low_information": True
        }
    return _COMBINED_MAP_LOW_INFO


# ---- batch 端点（断点续跑）开关（08-22）----
# 开（默认，保持现状）：Map 批次结果写 combined_batch_results，中断后下次
#   在已记录批次基础上继续处理（断点恢复：_load_combined_batch_cache 复用 +
#   _recent_combined_batch_failure_count 判失败）。
# 关：不记录批次结果进数据库、不做断点续跑——每次按"最后一次更新人设画像
#   后的全部消息"重新处理。失败计数此时转内存计数器（保留"部分批次失败不推进
#   断点"保护：失败批次的消息不丢，下次全量重跑时自然补上）。
# 内存计数 key=(user_id, total_batches)，每轮运行开始 reset（_do_update 里）。
_batch_endpoint_failures: dict[tuple[int, int], int] = {}


def _bendpoint_enabled() -> bool:
    """batch 端点（断点续跑）开关：DEBUG_BATCH_ENDPOINT，默认开=保持现状。"""
    return bool(CONFIG.get("DEBUG_BATCH_ENDPOINT", True))


def _bendpoint_fail_inc(user_id: int, total_batches: int) -> None:
    """batch 端点关闭时：登记一次"真失败"批次（LLM 调用失败，非低信息量）。"""
    key = (user_id, total_batches)
    _batch_endpoint_failures[key] = _batch_endpoint_failures.get(key, 0) + 1


def _bendpoint_fail_count(user_id: int, total_batches: int) -> int:
    return _batch_endpoint_failures.get((user_id, total_batches), 0)


def _bendpoint_fail_reset(user_id: int) -> None:
    """每轮运行开始时清空该用户的失败计数（与 DB 路径的"最近一轮"窗口语义对齐）。"""
    for k in [k for k in _batch_endpoint_failures if k[0] == user_id]:
        _batch_endpoint_failures.pop(k, None)


def _save_combined_batch(
    user_id: int, group_id: int, nickname: str, batch_num: int, total_batches: int,
    batch_char_count: int, raw_response: str, parsed: Optional[dict],
    is_valid: int,
    batch_text: str = "",
) -> None:
    """保存 Combined Map 批次中间结果（is_valid=0 也保存，保证批次可追溯/断点恢复）。

    batch_text: 该批次实际喂给 LLM 的聊天记录原文（临时调试功能 DEBUG_SAVE_BATCH_TEXT，
    排查 low_information 误判时手动核对用；默认空串不写，避免膨胀）。

    08-22 batch 端点开关：关闭时不记录批次结果进数据库；"真失败"（is_valid=0 且
    parsed is None，=LLM 调用失败，非低信息量）转内存计数（保留"部分失败不推进断点"
    保护——失败批次的消息不丢，下次全量重跑时自然补上）。
    """
    if not _bendpoint_enabled():
        if not is_valid and parsed is None:
            _bendpoint_fail_inc(user_id, total_batches)
        return
    try:
        parsed_json = json.dumps(parsed, ensure_ascii=False) if parsed else ""
        persona = (parsed or {}).get("persona", {})
        profile_material = (parsed or {}).get("profile_material", {})
        debug_text = batch_text if CONFIG.get("DEBUG_SAVE_BATCH_TEXT") else ""
        with get_persona_db() as db:
            db.execute(
                "INSERT INTO combined_batch_results (user_id, group_id, nickname, batch_index, total_batches, batch_char_count, raw_response, parsed_json, persona_json, profile_material_json, is_valid, is_incremental, created_at, batch_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (user_id, group_id, nickname, batch_num, total_batches, batch_char_count,
                 raw_response, parsed_json,
                 json.dumps(persona, ensure_ascii=False),
                 json.dumps(profile_material, ensure_ascii=False),
                 is_valid, time.time(), debug_text)
            )
            db.commit()
    except Exception as e:
        logger.warning(f"保存 Combined Map 中间结果失败: {e}")


def _load_combined_batch_cache(user_id: int, total_batches: int) -> dict[int, dict]:
    """
    断点恢复：读取该用户最近一次同 total_batches 运行的批次中间结果。
    返回 {batch_num: {"parsed": dict, "is_valid": int, "batch_char_count": int}}。
    只有最近一轮（created_at 最大的那批记录）会被加载，避免跨轮复用。
    """
    cache: dict[int, dict] = {}
    # 08-22 batch 端点开关：关闭时不做断点续跑（不读库复用，全部批次重新处理）
    if not _bendpoint_enabled():
        return cache
    try:
        with get_persona_db() as db:
            # 窗口 6 小时：一轮联合更新（含 4h 超时上限 + Reduce）可能跨 2-4 小时，
            # 3600s 会把同轮较早完成的批次错误过滤掉。
            rows = db.execute(
                "SELECT batch_index, parsed_json, is_valid, batch_char_count, created_at FROM combined_batch_results "
                "WHERE user_id = ? AND total_batches = ? "
                "AND created_at >= (SELECT COALESCE(MAX(created_at), 0) FROM combined_batch_results "
                "                    WHERE user_id = ? AND total_batches = ?) - 21600 "
                "ORDER BY created_at ASC",
                (user_id, total_batches, user_id, total_batches)
            ).fetchall()
        for r in rows:
            try:
                parsed = json.loads(r["parsed_json"]) if r["parsed_json"] else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            # ASC 排序下后写入的 = 较新的记录，覆盖旧轮同 batch_index 的结果
            cache[r["batch_index"]] = {
                "parsed": parsed,
                "is_valid": r["is_valid"],
                "batch_char_count": r["batch_char_count"],
            }
    except Exception as e:
        logger.warning(f"加载 Combined Map 批次缓存失败: {e}")
    return cache


def _recent_combined_batch_failure_count(user_id: int, total_batches: int) -> int:
    """
    统计最近一轮同 total_batches 的"失败形态"批次数（is_valid=0 且 parsed 为空 = LLM 调用失败，
    非低信息量——低信息量是 is_valid=0 但 parsed 有值）。用于区分"模型故障"与"真实低信息量"。

    08-22 batch 端点开关：关闭时改读内存计数（不写库则查库恒为 0，会误判"全成功"
    推进断点、丢失失败批次消息）。
    """
    if not _bendpoint_enabled():
        return _bendpoint_fail_count(user_id, total_batches)
    try:
        with get_persona_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM combined_batch_results "
                "WHERE user_id = ? AND total_batches = ? AND is_valid = 0 "
                "AND (parsed_json = '' OR parsed_json IS NULL) "
                "AND created_at >= (SELECT COALESCE(MAX(created_at), 0) FROM combined_batch_results "
                "                    WHERE user_id = ? AND total_batches = ?) - 21600",
                (user_id, total_batches, user_id, total_batches)
            ).fetchone()
            return row["n"] if row else 0
    except Exception as e:
        logger.warning(f"查询 Combined Map 失败批次统计失败: {e}")
        return 0


def _count_useful_lines(batch_text: str, target_short: str) -> int:
    """统计批次文本中目标用户的有效文本发言行数（排除 [图片] 等标记、空行、单字回应）。

    ⚠️ 正则兼容两种行格式：_format_session_text / _split_long_session_chunks / scheduler
    统一输出 `#序号 HH:MM U5: 内容`（带序号，2026-08-05 对齐）；历史旧格式
    `HH:MM U5: 内容`（不带序号）仅存在于旧缓存/旧批次文本中，`(?:#\\d+ )?` 使序号
    可选以兼容重放验证——新生成文本全部带序号。
    """
    useful = 0
    for line in batch_text.split("\n"):
        m = _re.match(r"(?:#\d+ )?\d{2}:\d{2} (U\d+): (.*)", line)
        if not m or m.group(1) != target_short:
            continue
        content = m.group(2).strip()
        if not content or len(content) < 2:
            continue
        if content.startswith("[") and content.endswith("]"):
            continue
        useful += 1
    return useful


async def _combined_map_call(
    batch_text: str,
    nickname: str,
    user_id: int,
    batch_num: int,
    total_batches: int,
    priority: int = 0,
    group_id: int = 0,
    short_map: dict | None = None,
    target_useful_lines: int = 0,
) -> Optional[dict]:
    """
    Combined Map 调用：一次 LLM 调用同时产出 persona 片段和 profile_material。
    返回 {"persona": dict, "profile_material": dict} 或 None（失败/低信息量）。
    所有结果（含低信息量/失败）都会落库，便于断点续跑。
    """
    system_prompt = _get_combined_map_system(nickname)
    user_prompt = _get_combined_map_user(batch_text, nickname)

    result = ""
    parsed = None
    for attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
        try:
            reply = await _call_llm_net([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], priority=priority, source="画像", **_llm_kwargs("map"))
        except LLMNetworkExhausted as _ne:
            logger.error(f"❌ Combined Map 批次 {batch_num} 网络异常，跳过该批次: {_ne}")
            _save_combined_batch(user_id, group_id, nickname, batch_num, total_batches,
                                 len(batch_text), "", None, 0, batch_text)
            return None
        result = reply.strip()
        if not result or _is_llm_error(result):
            logger.warning(f"🔄 Combined Map 批次 {batch_num} 返回无效 content (attempt {attempt}/{_MAX_LLM_RETRIES})，重试中...")
            continue
        # JSON 解析失败也重试（DeepSeek 思考模式下 content 可能为空、只有
        # reasoning，或返回被截断的 JSON——重试可提高拿到完整 JSON 的概率）
        parsed = _parse_combined_map_json(result)
        if parsed is not None:
            # 误判守卫：目标用户有效文本发言 ≥3 行却标 low_information = LLM 误判（Pitfall 78），强制重试
            if parsed.get("low_information") and target_useful_lines >= 3:
                logger.warning(f"⚠️ Combined Map 批次 {batch_num} low_information 疑似误判（目标用户有效发言 {target_useful_lines} 行），强制重试...")
                parsed = None
                continue
            break
        logger.warning(f"⚠️ Combined Map 批次 {batch_num} JSON 解析失败 (attempt {attempt}/{_MAX_LLM_RETRIES})，重试中...")
    else:
        logger.error(f"❌ Combined Map 批次 {batch_num} 达到最大重试次数，跳过该批次")
        _save_combined_batch(user_id, group_id, nickname, batch_num, total_batches,
                             len(batch_text), result or "", None, 0, batch_text)
        return None

    # 低信息量判断（也落库，标记 is_valid=0，便于断点跳过）
    if parsed.get("low_information"):
        logger.info(f"ℹ️ Combined Map 批次 {batch_num} 信息量不足")
        _save_combined_batch(user_id, group_id, nickname, batch_num, total_batches,
                             len(batch_text), result, _normalize_u_refs(parsed, short_map), 0, batch_text)
        return None

    # 验证至少有一个有效字段
    persona = parsed.get("persona", {})
    profile_material = parsed.get("profile_material", {})
    if not persona and not any(v for v in [profile_material.get("reality", ""), profile_material.get("group_persona", "")]):
        _save_combined_batch(user_id, group_id, nickname, batch_num, total_batches,
                             len(batch_text), result, _normalize_u_refs(parsed, short_map), 0, batch_text)
        return None

    # 保存中间结果（U 编号引用归一化为 昵称(qq号)，供断点恢复直接消费）
    parsed_norm = _normalize_u_refs(parsed, short_map)
    # 兜底校验：剔除 relationships 中指向目标用户自身的条目（LLM 认错人防线）
    if _strip_self_relationships(parsed_norm.get("persona"), user_id):
        logger.warning(f"⚠️ Combined Map 批次 {batch_num} 已剔除目标自身关系条目")
    _save_combined_batch(user_id, group_id, nickname, batch_num, total_batches,
                         len(batch_text), result, parsed_norm, 1, batch_text)

    return {"persona": parsed_norm.get("persona", {}), "profile_material": parsed_norm.get("profile_material", {})}


def _parse_combined_map_json(text: str) -> Optional[dict]:
    """解析 Combined Map 的 JSON 输出"""
    if not text:
        return None
    text = text.strip()
    # 去除 Markdown 代码块
    if "```" in text:
        code_match = _re.search(r'```(?:json)?\s*(.*?)\s*```', text, _re.DOTALL)
        if code_match:
            text = code_match.group(1).strip()
    # 提取 JSON 对象
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    try:
        parsed = json.loads(text, strict=False)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _format_chat_messages(chat_log: list[dict], start_idx: int = 0) -> list[str]:
    """
    将 chat_log 格式化为紧凑消息行列表（节省 token）。
    
    格式：HH:MM U1: 内容
    - 时间只保留时分
    - 用户名用短 ID（U1, U2...）
    - 合并同一用户连续发言
    - 返回列表，首行为人物映射表
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
    formatted: list[tuple[str, int, str]] = []
    for entry in chat_log:
        dt = datetime.fromtimestamp(entry["created_at"])
        time_str = dt.strftime("%H:%M")
        # 与画像/人设一致的格式：CQ 码清理 + @数字 → @U短ID（uid_to_short 映射）
        content = _clean_cq_codes(entry.get("content") or "", 0, {}, uid_to_short)
        content = content[:300]
        # 消息内真实换行转义为字面 \n（一条消息一行契约，方案B）
        content = content.replace("\n", "\\n")
        uid = entry.get("user_id", 0)
        short = uid_to_short[uid]
        
        # 合并同一用户连续消息
        if formatted and formatted[-1][1] == uid and formatted[-1][0] == time_str:
            prev_content = formatted[-1][2]
            merged = prev_content + content
            if len(merged) <= 300:
                formatted[-1] = (time_str, uid, merged)
            else:
                formatted.append((time_str, uid, content))
        else:
            formatted.append((time_str, uid, content))
    
    # 构建人物映射表（与画像/人设格式一致：表头后空行；join 时会补一个 \n 形成空行）
    nick_map_lines = [f"{short}={short_to_nick[short]}" for short in short_to_nick]
    header = f"人物:\n{chr(10).join(nick_map_lines)}\n"
    
    # 构建消息行（带序号）
    lines = [header]
    for i, (time_str, uid, content) in enumerate(formatted, 1):
        short = uid_to_short[uid]
        lines.append(f"#{i} {time_str} {short}: {content}")
    
    return lines[start_idx:] if start_idx else lines


# ============================================================
#  画像 Reduce 提示词与调用函数
# ============================================================

def _format_profile_material_text(materials: list[dict]) -> str:
    """
    将多个 profile_material 整理为画像素材文本（供画像 Reduce 使用）。
    格式：【现实底牌】【人设与话题】【社交羁绊】【语言指纹】【原话证据】
    """
    sections = {
        "reality": "现实底牌",
        "group_persona": "人设与话题",
        "social": "社交羁绊",
        "language_style": "语言指纹",
        "quotes": "原话证据",
    }
    merged: dict[str, list[str]] = {k: [] for k in sections}

    for mat in materials:
        for key, _ in sections.items():
            val = mat.get(key)
            if not val:
                continue
            if key == "quotes" and isinstance(val, list):
                # 合并 quotes，去重
                for q in val:
                    if q and q not in merged[key]:
                        merged[key].append(q)
            elif isinstance(val, str) and val.strip():
                merged[key].append(val.strip())

    lines = []
    for key, label in sections.items():
        content = merged[key]
        if not content:
            continue
        lines.append(f"【{label}】")
        if key == "quotes":
            for q in content[:3]:
                lines.append(f"- {q}")
        else:
            # 合并多个来源的描述
            combined = "\n".join(content)
            lines.append(combined)
        lines.append("")

    return "\n".join(lines) if lines else "信息量不足。"


async def _hierarchical_merge_by_len(
    items: list,
    threshold: int,
    log_prefix: str,
    serialize,          # callable(item) -> str
    merge_group,        # async callable(list) -> merged_item or None
    on_group_merged=None,  # optional async callable(batch_idx, total_groups, merged_item)
) -> list:
    """batch_token(threshold) 长度驱动的多级收敛合并骨架。

    items: 待合并元素列表（dict/str 均可，serialize 统一序列化）。
    只要待合并集合序列化总长 > threshold，就按 threshold 等分切成若干组（组内元素完整，
    不切开单个元素），逐组 merge_group 预合并；重复上升层级，直到整组总长 <= threshold
    且一次合并可收敛为单一结果。返回收敛后的片段集合列表（可能 1 个成功合并项，
    也可能因合并失败回退为多个原始元素）——调用方直接使用该列表接续最终融合。

    同一层的所有组合并调用通过 asyncio.gather 并发提交（顺序由 gather 保证与输入一致；
    并发上限由 call_llm 的信号量/队列控制）。on_group_merged 的 batch_idx 为跨层全局
    唯一递增序号（1,2,3...），供调用方落库时取负值区分"第几层第几组"。
    """
    cur = list(items)
    if not cur:
        return cur
    merge_serial = 0  # 跨层全局唯一合并序号（落库 batch_index 取负）

    async def _merge_one(batch_idx, chunk):
        """合并单个组；返回 (result_list, merged_item_or_None)。"""
        if len(chunk) == 1:
            return chunk, None
        merged = await merge_group(chunk)
        if merged is not None:
            if on_group_merged:
                await on_group_merged(batch_idx, len(chunks), merged)
            return [merged], merged
        return chunk, None  # 失败回退原始元素

    for _level in range(1, 30):
        if len(cur) <= 1:
            return cur
        total = sum(len(serialize(it)) for it in cur)
        if total <= threshold:
            # 整组已可一次合并
            merged = await merge_group(cur)
            if merged is not None:
                return [merged]
            # 合并失败，回退为原始片段集（交给最终融合兜底），不再尝试
            return cur
        # 按 threshold 贪心切段：段内元素完整，段总长尽量接近但不超过 threshold
        chunks = []
        c, clen = [], 0
        for it in cur:
            ln = len(serialize(it))
            if c and clen + ln > threshold:
                chunks.append(c)
                c, clen = [], 0
            c.append(it)
            clen += ln
        if c:
            chunks.append(c)
        if len(chunks) <= 1:
            # 单个超长元素无法再切，直接尝试整体合并
            merged = await merge_group(cur)
            return [merged] if merged is not None else cur
        # 若切出的每段都是单元素（无法两两配对），直接整体合并一次收尾
        if all(len(x) == 1 for x in chunks):
            merged = await merge_group(cur)
            return [merged] if merged is not None else cur
        # 同一层所有组 gather 并发合并（顺序与 chunks 一致）
        new_cur = []
        results = await asyncio.gather(*[
            _merge_one(merge_serial + i + 1, chunk) for i, chunk in enumerate(chunks)
        ])
        merge_serial += len(chunks)
        for result_list, merged_item in results:
            new_cur.extend(result_list)
        if not new_cur or len(new_cur) >= len(cur):
            # 无收敛进展，防止死循环
            return cur
        cur = new_cur
    return cur


async def _hierarchical_merge_json(
    fragments: list[dict],
    threshold: int,
    nickname: str,
    priority: int,
    system_template: str,
    log_prefix: str,
    on_group_merged=None,
) -> list[dict]:
    """人设/画像 JSON 片段的多级收敛合并。

    用 batch_token 长度约束替代固定分组(`REDUCE_GROUP_SIZE=9`)与固定两级合并：
    总长 > threshold 时按阈值多段预合并，递归收敛直到合并为单一 JSON。
    返回收敛后的片段列表（1 个合并项，或失败回退的多个原始片段）。
    system_template 需含 {old_data_clause} 占位（中间合并时替换为空串）。
    """
    def _ser(e):
        return json.dumps(e, ensure_ascii=False)

    async def _mg(group):
        group_json = json.dumps(group, ensure_ascii=False, indent=2)
        m_sys = system_template.replace("{old_data_clause}", "")
        m_usr = f"待合并的 JSON 片段：\n{group_json}\n\n请合并为一个 JSON："
        inter = ""
        for _a in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
            try:
                reply = await _call_llm_net(
                    [{"role": "system", "content": m_sys}, {"role": "user", "content": m_usr}],
                    priority=priority, source="画像", **_llm_kwargs("merge"))
            except LLMNetworkExhausted as _ne:
                logger.error(f"❌ {log_prefix} 多级合并网络异常: {_ne}")
                return None
            inter = reply.strip()
            if inter and not _is_llm_error(inter) and _parse_persona_json(inter):
                break
            logger.warning(f"🔄 {log_prefix} 多级合并返回无效 content (attempt {_a}/{_MAX_LLM_RETRIES})，重试中...")
        else:
            return None
        return _parse_persona_json(inter) or None

    return await _hierarchical_merge_by_len(
        fragments, threshold, log_prefix, _ser, _mg, on_group_merged)


# 画像 Reduce·联合路径 system 提示词已迁至 core/persona_prompts.py: profile_reduce_system（2026-08-21）


async def _do_profile_reduce(
    materials: list[dict],
    old_profile: str,
    nickname: str,
    priority: int = 0,
    user_id: int = 0,
    group_id: int = 0,
) -> Optional[str]:
    """
    画像 Reduce：旧画像 + 画像素材 → 最终第一人称画像文本。
    返回画像文本或 None。
    """
    profile_material_text = _format_profile_material_text(materials)

    # 素材文本过长时按 batch_token(_batch_chars()) 长度多级收敛合并
    # （替代固定"每 10 个一组合并"。总长 > _batch_chars() 时按阈值逐段预合并，递归收敛）
    if len(materials) > 1 and len(profile_material_text) > _batch_chars():
        logger.info(f"📋 画像素材文本 {len(profile_material_text)} 字符 > _batch_chars()({_batch_chars()})，启动多级收敛合并...")

        def _profile_ser(mat):
            return _format_profile_material_text([mat]) if isinstance(mat, dict) else mat

        async def _profile_mg(group):
            group_text = "\n---\n".join(
                _format_profile_material_text([m]) if isinstance(m, dict) else m for m in group)
            merge_system = _prompt("profile_material_merge_system", nickname=nickname)
            merge_user = _prompt("profile_material_merge_user", nickname=nickname, group_text=group_text)
            merge_result = ""
            for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
                try:
                    merge_result = await _call_llm_net([
                        {"role": "system", "content": merge_system},
                        {"role": "user", "content": merge_user}
                    ], priority=priority, source="画像", **_llm_kwargs("merge"))
                except LLMNetworkExhausted as _ne:
                    logger.error(f"❌ 画像素材多级合并网络异常: {_ne}")
                    return None
                merge_result = merge_result.strip()
                if merge_result and not _is_llm_error(merge_result):
                    break
                logger.warning(f"🔄 画像素材多级合并 (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
            else:
                return None
            return merge_result

        async def _profile_reduce_on_group_merged(batch_idx, total_groups, merged_item):
            # merged_item 可能是 dict（素材）或 str（合并产物文本），统一序列化落库
            if isinstance(merged_item, dict):
                inter_str = _format_profile_material_text([merged_item])
            else:
                inter_str = merged_item
            try:
                with get_persona_db() as db:
                    db.execute(
                        "INSERT INTO profile_batch_results (user_id, nickname, group_id, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
                        (user_id, nickname, group_id,
                         -batch_idx, total_groups,
                         len(inter_str), inter_str, time.time())
                    )
                    db.commit()
                logger.info(f"💾 画像素材合并中间结果已保存 (batch_index=-{batch_idx}, 组 {batch_idx}/{total_groups} 序号)")
            except Exception as e:
                logger.warning(f"保存画像素材合并中间结果失败: {e}")

        merged_materials = await _hierarchical_merge_by_len(
            materials, _batch_chars(), "画像素材", _profile_ser, _profile_mg,
            on_group_merged=_profile_reduce_on_group_merged)
        # 收敛后可能仍为多个原始素材（合并失败回退），统一格式化为素材文本
        profile_material_text = "\n---\n".join(
            _format_profile_material_text([m]) if isinstance(m, dict) else m for m in merged_materials)
        logger.info(f"✅ 画像素材多级收敛合并完成，{len(profile_material_text)} 字符")

    # 构建提示词
    p_min, p_max = _pr_total_range()
    system_prompt = _prompt("profile_reduce_system", nickname=nickname,
                               profile_min=p_min, profile_max=p_max)

    if old_profile:
        word_limit = _prompt("profile_word_limit", profile_min=p_min, profile_max=p_max)
        # 旧画像过长时强制压缩模式（2026-08-08：多轮增量累积膨胀防线）
        # 压缩是成熟任务：给 LLM 明确的量化目标 + 具体操作方法，而不是模糊的"尽量压缩"
        # 注：不合并维度（用户要求保留【群内人设与羁绊】和【兴趣与作风】独立结构），改为每个维度限句数压缩
        if len(old_profile) > int(_profile_limits()['compress_trigger']):
            word_limit += _prompt("profile_compress_mode",
                                 trigger=int(_profile_limits()['compress_trigger']),
                                 profile_min=p_min, profile_max=p_max)
        profile_hint = _prompt("profile_hint_material", old_profile=old_profile, word_limit=word_limit)
    else:
        profile_hint = _prompt("profile_hint_first", profile_min=p_min, profile_max=p_max)

    user_prompt = _prompt("profile_reduce_user", nickname=nickname,
                        profile_material_text=profile_material_text, profile_hint=profile_hint)

    # 调用 LLM
    result = ""
    for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
        try:
            reply = await _call_llm_net([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], priority=priority, source="画像", **_llm_kwargs("profile_reduce"))
        except LLMNetworkExhausted as _ne:
            logger.error(f"❌ 画像 Reduce 网络异常: {_ne}")
            return None
        result = reply.strip()
        if result and not _is_llm_error(result):
            break
        logger.warning(f"🔄 画像 Reduce 返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
    else:
        logger.error("❌ 画像 Reduce 达到最大重试次数")
        return None

    # 压缩循环（2026-08-08 用户方案：删除代码硬截断）
    # 输出 >600（超出目标上限 500-600）即触发压缩——首建/增量统一，
    # 首次建立时 LLM 输出 800+ 字同样压缩干预（修复首建无兜底缺口）
    # 压缩循环（2026-08-08 用户方案：删除代码硬截断）
    # 输出超目标上限即触发压缩——首建/增量统一，首建时 LLM 输出 800+ 字同样压缩干预
    pl = _profile_limits()
    pr_min, pr_max = int(pl["total_min"]), int(pl["total_max"])
    if len(result) > pr_max:
        original_len = len(result)  # 压缩基准：原始画像字数（压缩比按此计算，不随轮次漂移）
        original_text = result  # 原始画像全文（过头修正轮恢复内容时的依据）
        for _cmp in range(1, int(pl["compress_rounds"]) + 1):  # 轮数可配（2026-08-21）
            if pr_min <= len(result) <= pr_max:
                break
            logger.info(f"🔁 画像压缩循环 {_cmp}/{pl['compress_rounds']}: {len(result)} 字，继续处理")
            if len(result) < pr_min:
                # 过头修正轮：压过头了，对照原始画像恢复内容（2026-08-09 实测 445→528 成功）
                compress_prompt = _prompt("profile_compress_fix_user",
                                         total=len(result), original_text=original_text,
                                         new_profile=result, profile_min=pr_min,
                                         fix_min=int(pl["compress_fix_min"]),
                                         fix_max=int(pl["compress_fix_max"]))
            elif _cmp == 1:
                # 第 1 轮：动态压缩比——把绝对目标换算成原始字数的比例
                ratio_lo = max(35, int(pr_min / original_len * 100))
                ratio_hi = min(95, int(pr_max / original_len * 100))
                compress_prompt = _prompt("profile_compress_user",
                                         total=len(result), original_len=original_len,
                                         ratio_lo=ratio_lo, ratio_hi=ratio_hi,
                                         profile_min=pr_min, profile_max=pr_max,
                                         new_profile=result)
            else:
                # 第 2+ 轮：差距驱动——明确本轮还需减少多少字（2026-08-09 实测避免重复输出卡住）
                compress_prompt = _prompt("profile_compress_gap_user",
                                         total=len(result), excess=len(result) - pr_max,
                                         original_len=original_len,
                                         profile_min=pr_min, profile_max=pr_max,
                                         new_profile=result)
            try:
                reply = await _call_llm_net([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": compress_prompt}
                ], priority=priority, **_llm_kwargs("compress"))
            except LLMNetworkExhausted as _ne:
                logger.error(f"❌ 画像压缩循环网络异常: {_ne}")
                break
            new_result = reply.strip()
            if new_result and not _is_llm_error(new_result):
                result = new_result
            else:
                logger.warning("🔄 画像压缩循环无效输出，保留上次结果")
                break
        logger.info(f"📏 画像压缩完成: {len(result)} 字" if len(result) <= pr_max
                    else f"⚠️ 压缩 {pl['compress_rounds']} 轮后仍 {len(result)} 字（保留）")

    # 清理 Markdown 残留
    result = _re.sub(r'(\*\*|__)(.*?)\1', r'\2', result)
    result = _re.sub(r'(?<!\w)([*_])(.*?)\1(?!\w)', r'\2', result)
    result = _re.sub(r'^#+\s*', '', result, flags=_re.MULTILINE)
    result = _re.sub(r'^[-*]\s+', '', result, flags=_re.MULTILINE)

    return result


# ============================================================
#  画像更新
# ============================================================

async def _do_update_profile(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int],
    profile_user_id: int, profile_nickname: str,
    silent: bool = False,
    priority: int = 0,  # 0=用户手动(高优先), 1=定时任务(低优先)
) -> None:
    """
    画像更新核心逻辑（内部函数，含锁）。
    被 handle_update_profile（前台）和 handle_update_all_profiles（后台）调用。
    持 _profile_update_lock 确保串行执行。
    """
    # BUG2修复：动态获取最新 WebSocket，避免 NapCat 重连后闭包引用过期
    ws = _active_websocket if _active_websocket is not None else websocket
    if ws is None:
        logger.warning("WebSocket 未就绪，跳过本次画像更新")
        return

    async with _profile_update_lock:
        profile = get_user_profile(profile_user_id, target_id)
        last_message_id = profile["last_message_id"] if profile else 0
        last_scan_at = profile["last_scan_at"] if profile else 0
        # 旧断点副本：部分批次失败时不推进断点（下次重扫自动补跑失败批次）
        start_last_id = last_message_id
        start_last_scan = last_scan_at

        messages = _extract_relevant_messages(profile_user_id, target_id, last_message_id, last_scan_at)

        if not messages:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"👤 暂无 {profile_nickname} 的新增聊天记录，画像已是最新状态～", user_id, reply_id)
            return

        total_new = len(messages)

        # ─── 新增量不足 500 条，跳过更新 ───
        MIN_INCREMENTAL_MESSAGES = int(_pcfg().get('min_incremental_messages', 500))
        if total_new < MIN_INCREMENTAL_MESSAGES:
            # 不推进断点，这批消息留到下次积累够了再一起处理
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"📊 {profile_nickname} 仅有 {total_new} 条新消息（阈值 {MIN_INCREMENTAL_MESSAGES}），暂不更新画像～",
                    user_id, reply_id)
            else:
                logger.info(f"📊 {profile_nickname} 仅有 {total_new} 条新消息（阈值 {MIN_INCREMENTAL_MESSAGES}），跳过画像更新")
            return

        last_id = messages[-1]["message_id"]
        last_scan_at = messages[-1]["created_at"]

        sessions = _split_into_sessions(messages)

        nickname_map: dict[int, str] = {}
        for msg in messages:
            uid = msg["user_id"]
            # 一直覆盖：messages 按时间升序，最后赋值的 = 最新昵称（用户可能改过名）
            nickname_map[uid] = msg["nickname"]

        # 目标昵称强制覆盖：跨群合并场景下 nickname_map 可能取到目标用户在
        # 其他群/历史昵称（如"网恋秀大追追…"），与提示词中的当前昵称不一致，
        # 会导致 LLM 认不出目标用户（张冠李戴根因之一）。用调用方传入的当前昵称覆盖。
        nickname_map[profile_user_id] = profile_nickname

        session_texts = []
        # 全局短 ID 编号：批次内跨 session 唯一，LLM 输出可归一化
        global_uid_to_short: dict[int, str] = {}
        _gcounter = 1
        for _m in messages:
            if _m["user_id"] not in global_uid_to_short:
                global_uid_to_short[_m["user_id"]] = f"U{_gcounter}"
                _gcounter += 1
        global_short_map = _build_short_map(global_uid_to_short, nickname_map)

        for sess in sessions:
            text = _format_session_text(sess, profile_user_id, nickname_map, global_uid_to_short)
            if len(text) > _batch_chars():
                session_texts.extend(_split_long_session_chunks(sess, profile_user_id, nickname_map, global_uid_to_short))
            else:
                session_texts.append(text)

        chat_log_text = "\n\n--- 对话分隔 ---\n\n".join(session_texts)

        # 【熔断 1】发言过少
        if total_new < 3 or len(chat_log_text.strip()) < 50:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🤷 {profile_nickname} 的发言太少啦，没有足够信息建立用户画像哦！", user_id, reply_id)
            return

        # 按字符数分批
        token_chunks = chunk_messages_by_token(session_texts, target_tokens=_batch_chars())

        if len(token_chunks) > 1 or len(chat_log_text) > _direct_threshold():
            # Map→Reduce 路径
            set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🔍 正在分析 {profile_nickname} 的 {total_new} 条聊天记录（{len(sessions)} 个对话 Session，分 {len(token_chunks)} 批处理），请稍候...", user_id, reply_id)
            logger.info(f"🔍 开始画像分析: {profile_nickname}({profile_user_id}), {total_new} 条新消息, {len(sessions)} 个 Session, {len(token_chunks)} 批")

            # Map 阶段：分批提取摘要（gather 并发提交，由 call_llm 并行信号量/串行队列统一限流）
            async def _profile_map_batch(i, chunk):
                batch_text = "\n\n--- 对话分隔 ---\n\n".join(chunk)
                batch_num = i + 1
                logger.info(f"📝 画像批次 {batch_num}/{len(token_chunks)}...")

                batch_system = _prompt("profile_map_system", nickname=profile_nickname)

                batch_result = ""
                for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
                    try:
                        reply = await _call_llm_net([
                            {"role": "system", "content": batch_system},
                            {"role": "user", "content": f"以下是【{profile_nickname}】的聊天记录片段：\n\n{batch_text}\n\n请提取该用户的信息，输出摘要："}
                        ], priority=priority, source="画像", **_llm_kwargs("map"))
                    except LLMNetworkExhausted as _ne:
                        logger.error(f"❌ Map 批次 {batch_num} 网络异常: {_ne}")
                        return None
                    batch_result = reply.strip()
                    if batch_result and not _is_llm_error(batch_result):
                        break
                    logger.warning(f"🔄 Map 批次 {batch_num} 返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
                else:
                    logger.error(f"❌ Map 批次 {batch_num} 达到最大重试次数，跳过该批次")
                    return None

                # U 编号引用归一化为 昵称(qq号)（落库与 Reduce 输入均使用归一化文本）
                batch_result = _normalize_u_refs(batch_result, global_short_map)

                # 保存 Map 批次中间结果
                try:
                    debug_text = batch_text if CONFIG.get("DEBUG_SAVE_BATCH_TEXT") else ""
                    with get_persona_db() as db:
                        db.execute(
                            "INSERT INTO profile_batch_results (user_id, nickname, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at, batch_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (profile_user_id, profile_nickname, batch_num, len(token_chunks), len(batch_text), batch_result, 1 if "信息量不足" not in batch_result else 0, 1 if (profile and profile.get("profile")) else 0, time.time(), debug_text)
                        )
                        db.commit()
                except Exception as e:
                    logger.warning(f"保存批次中间结果失败: {e}")

                return batch_result

            map_results = await asyncio.gather(*[_map_gather_run(_profile_map_batch(i, chunk)) for i, chunk in enumerate(token_chunks)])
            batches = [b for b in map_results if b]

            # 过滤掉"信息量不足"的批次
            valid_batches = [b for b in batches if "信息量不足，无有效发言" not in b]

            # 【熔断 2/3】Map 后拦截：全部批次均为无效发言
            if not valid_batches:
                if not batches:
                    # 全部批次 LLM 调用失败（模型不可用）——区别于"全是表情包/复读"
                    logger.error(f"❌ {profile_nickname} 画像 Map 全部批次失败（模型不可用），断点未推进")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"⚠️ 模型暂时不可用（限流或服务异常），{profile_nickname} 的画像更新未能完成，请稍后重试。", user_id, reply_id)
                else:
                    logger.info(f"⚠️ {profile_nickname} 的所有批次均无有效发言，触发 Map 后熔断")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"🤷 {profile_nickname} 最近全是表情包或复读，没有足够信息建立用户画像哦！", user_id, reply_id)
                return

            # 批次文本过长时按 batch_token(_batch_chars()) 长度多级收敛合并
            # （替代固定"每 10 批一次中间总结 + 新增量合并"。总长 > _batch_chars() 时逐段收敛）
            if len(valid_batches) > 1:
                def _batch_ser(b):
                    return b
                _profile_merge_serial = [0]  # 跨层唯一中间总结编号（batch_index 取负）
                async def _batch_mg(group):
                    intermediate_system = _prompt("profile_merge_intermediate_system", nickname=profile_nickname)
                    intermediate_user = _prompt("profile_merge_intermediate_user",
                                                nickname=profile_nickname, group_count=len(group),
                                                group_text='---\n'.join(group))
                    intermediate_reply = ""
                    for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
                        try:
                            intermediate_reply = await _call_llm_net([
                                {"role": "system", "content": intermediate_system},
                                {"role": "user", "content": intermediate_user}
                            ], priority=priority, source="画像", **_llm_kwargs("merge"))
                        except LLMNetworkExhausted as _ne:
                            logger.error(f"❌ 画像多级中间总结网络异常: {_ne}")
                            return None
                        intermediate_reply = intermediate_reply.strip()
                        if intermediate_reply and not _is_llm_error(intermediate_reply):
                            break
                        logger.warning(f"🔄 画像多级中间总结返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
                    else:
                        logger.error("❌ 画像多级中间总结达到最大重试次数，使用空摘要")
                        return None
                    # 保存中间总结到 DB（batch_index 取负且跨层唯一，与 Map 正索引区分）
                    _profile_merge_serial[0] += 1
                    try:
                        with get_persona_db() as db:
                            db.execute(
                                "INSERT INTO profile_batch_results (user_id, nickname, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (profile_user_id, profile_nickname, -_profile_merge_serial[0], 1, len(intermediate_reply), intermediate_reply, 1, 1 if (profile and profile.get("profile")) else 0, time.time())
                            )
                            db.commit()
                    except Exception as e:
                        logger.warning(f"保存画像多级中间总结失败: {e}")
                    return intermediate_reply
                merged_summaries = await _hierarchical_merge_by_len(
                    valid_batches, _batch_chars(), "画像", _batch_ser, _batch_mg)
                summaries_text = "\n===\n".join(merged_summaries)
                logger.info(f"✅ 画像批次多级收敛合并完成，收敛到 {len(merged_summaries)} 段，共 {len(summaries_text)} 字符")
            else:
                summaries_text = valid_batches[0] if valid_batches else "各批次均无有效发言信息。"

            # 【熔断 3/3】Reduce 前拦截：有效摘要过短
            if len(summaries_text) < 150:
                logger.info(f"⚠️ {profile_nickname} 的有效摘要过短（{len(summaries_text)}字符），触发 Reduce 前熔断")
                if not silent:
                    await send_reply(websocket, message_type, target_id,
                        f"🤷 {profile_nickname} 的有效发言太少啦，没有足够信息建立用户画像哦！", user_id, reply_id)
                return

            # Reduce 阶段：画像生成
            old_profile = profile["profile"] if profile else ""
            system_prompt = _prompt("profile_reduce_incremental_system", nickname=profile_nickname)

            if old_profile:
                pr_min, pr_max = _pr_total_range()
                word_limit = _prompt("profile_word_limit", profile_min=pr_min, profile_max=pr_max)
                # 旧画像过长时强制压缩模式（2026-08-08：多轮增量累积膨胀防线）
                if len(old_profile) > int(_profile_limits()['compress_trigger']):
                    word_limit += _prompt("profile_compress_mode",
                                     trigger=int(_profile_limits()['compress_trigger']),
                                     profile_min=pr_min, profile_max=pr_max)
                pr_min, pr_max = _pr_total_range()
                profile_hint = _prompt("profile_hint_summary", old_profile=old_profile, word_limit=word_limit)
            else:
                profile_hint = _prompt("profile_hint_first", profile_min=pr_min, profile_max=pr_max)

            user_prompt = _prompt("profile_reduce_incremental_user", nickname=profile_nickname,
                                summaries_text=summaries_text, profile_hint=profile_hint)

        else:
            # 消息不多，直接分析
            set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
            if not silent:
                await send_reply(websocket, message_type, target_id,
                    f"🔍 正在分析 {profile_nickname} 的 {total_new} 条聊天记录（{len(sessions)} 个对话 Session），请稍候...", user_id, reply_id)
            logger.info(f"🔍 开始画像分析: {profile_nickname}({profile_user_id}), {total_new} 条新消息, {len(sessions)} 个 Session")

            old_profile = profile["profile"] if profile else ""
            system_prompt = _prompt("profile_reduce_direct_system", nickname=profile_nickname)

            if old_profile:
                pr_min, pr_max = _pr_total_range()
                word_limit = _prompt("profile_word_limit", profile_min=pr_min, profile_max=pr_max)
                # 旧画像过长时强制压缩模式（2026-08-08：多轮增量累积膨胀防线）
                if len(old_profile) > int(_profile_limits()['compress_trigger']):
                    word_limit += _prompt("profile_compress_mode",
                                     trigger=int(_profile_limits()['compress_trigger']),
                                     profile_min=pr_min, profile_max=pr_max)
                pr_min, pr_max = _pr_total_range()
                profile_hint = _prompt("profile_hint_chat", old_profile=old_profile, word_limit=word_limit)
            else:
                profile_hint = _prompt("profile_hint_first", profile_min=pr_min, profile_max=pr_max)

            user_prompt = _prompt("profile_reduce_direct_user", nickname=profile_nickname,
                                chat_log_text=chat_log_text, profile_hint=profile_hint)

        # 调用 LLM
        new_profile = ""
        for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
            try:
                reply = await _call_llm_net([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ], priority=priority, source="画像", **_llm_kwargs("profile_reduce"))
            except LLMNetworkExhausted as _ne:
                logger.error(f"❌ 画像 Reduce 网络异常，跳过 {profile_nickname} 的画像更新: {_ne}")
                return
            new_profile = reply.strip()
            if new_profile and not _is_llm_error(new_profile):
                break
            logger.warning(f"🔄 Reduce 阶段返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
        else:
            logger.error(f"❌ 画像 Reduce 达到最大重试次数，跳过 {profile_nickname} 的画像更新")
            return

        # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
        new_profile = _normalize_u_refs(new_profile, global_short_map)

        # 压缩循环（2026-08-08 用户方案：删除代码硬截断，与联合路径一致）
        # 输出 >600 即触发压缩——首建/增量统一（修复首建无兜底缺口）
        # 压缩循环（2026-08-08 用户方案：删除代码硬截断，与联合路径一致）
        # 输出超目标上限即触发压缩——首建/增量统一（修复首建无兜底缺口）
        pl = _profile_limits()
        pr_min, pr_max = int(pl["total_min"]), int(pl["total_max"])
        if len(new_profile) > pr_max:
            original_len = len(new_profile)  # 压缩基准：原始画像字数（压缩比按此计算，不随轮次漂移）
            original_text = new_profile  # 原始画像全文（过头修正轮恢复内容时的依据）
            for _cmp in range(1, int(pl["compress_rounds"]) + 1):  # 轮数可配（2026-08-21）
                if pr_min <= len(new_profile) <= pr_max:
                    break
                logger.info(f"🔁 画像压缩循环 {_cmp}/{pl['compress_rounds']}: {len(new_profile)} 字，继续处理")
                if len(new_profile) < pr_min:
                    # 过头修正轮：压过头了，对照原始画像恢复内容（2026-08-09 实测 445→528 成功）
                    compress_prompt = _prompt("profile_compress_fix_user",
                                             total=len(new_profile), original_text=original_text,
                                             new_profile=new_profile, profile_min=pr_min,
                                             fix_min=int(pl["compress_fix_min"]),
                                             fix_max=int(pl["compress_fix_max"]))
                elif _cmp == 1:
                    # 第 1 轮：动态压缩比——把绝对目标换算成原始字数的比例
                    ratio_lo = max(35, int(pr_min / original_len * 100))
                    ratio_hi = min(95, int(pr_max / original_len * 100))
                    compress_prompt = _prompt("profile_compress_user",
                                             total=len(new_profile), original_len=original_len,
                                             ratio_lo=ratio_lo, ratio_hi=ratio_hi,
                                             profile_min=pr_min, profile_max=pr_max,
                                             new_profile=new_profile)
                else:
                    # 第 2+ 轮：差距驱动——明确本轮还需减少多少字（2026-08-09 实测避免重复输出卡住）
                    compress_prompt = _prompt("profile_compress_gap_user",
                                             total=len(new_profile), excess=len(new_profile) - pr_max,
                                             original_len=original_len,
                                             profile_min=pr_min, profile_max=pr_max,
                                             new_profile=new_profile)
                try:
                    reply = await _call_llm_net([
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": compress_prompt}
                    ], priority=priority, **_llm_kwargs("compress"))
                except LLMNetworkExhausted as _ne:
                    logger.error(f"❌ 画像压缩循环网络异常: {_ne}")
                    break
                new_result = reply.strip()
                if new_result and not _is_llm_error(new_result):
                    new_profile = new_result
                else:
                    logger.warning("🔄 画像压缩循环无效输出，保留上次结果")
                    break
            logger.info(f"📏 画像压缩完成: {len(new_profile)} 字" if len(new_profile) <= pr_max
                        else f"⚠️ 压缩 {pl['compress_rounds']} 轮后仍 {len(new_profile)} 字（保留）")

        # 一致性校验保险（2026-08-10）：增量更新校验新 vs 旧——失败则丢弃新画像（保留旧画像）
        if old_profile and not await _verify_consistency(
                "画像", old_profile, new_profile, profile_nickname, priority):
            logger.error(f"❌ {profile_nickname} 画像一致性校验失败，丢弃新画像保留旧画像")
            if not silent:
                await send_reply(websocket, message_type, target_id,
                    f"⚠️ {profile_nickname} 画像更新校验未通过（与旧画像不一致），已保留旧画像，请稍后重试。",
                    user_id, reply_id)
            return

        # 清理 Markdown 残留
        new_profile = _re.sub(r'(\*\*|__)(.*?)\1', r'\2', new_profile)
        new_profile = _re.sub(r'(?<!\w)([*_])(.*?)\1(?!\w)', r'\2', new_profile)
        new_profile = _re.sub(r'^#+\s*', '', new_profile, flags=_re.MULTILINE)
        new_profile = _re.sub(r'^[-*]\s+', '', new_profile, flags=_re.MULTILINE)

        # 保存画像（按群隔离）
        # 部分批次失败（模型异常）时保存结果但断点不推进：下次重扫自动补跑失败批次
        _chunks = locals().get("token_chunks", [])
        _batches = locals().get("batches", [])
        if len(_chunks) > 1 and len(_batches) < len(_chunks):
            last_id, last_scan_at = start_last_id, start_last_scan
            logger.warning(f"⚠️ {profile_nickname} 部分批次失败 ({len(_batches)}/{len(_chunks)})，保存画像但断点不推进，下次自动补跑")
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"⚠️ {profile_nickname} 部分批次因模型异常未完成，已保存当前画像，剩余部分将在下次更新时自动补全。", user_id, reply_id)
        save_user_profile(profile_user_id, profile_nickname, new_profile, last_id, group_id=target_id, last_scan_at=last_scan_at)

        # 保存 Reduce 最终结果到中间结果表
        try:
            _reduce_batches = locals().get('token_chunks', [None])
            with get_persona_db() as db:
                db.execute(
                    "INSERT INTO profile_batch_results (user_id, nickname, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (profile_user_id, profile_nickname, 0, len(_reduce_batches), len(new_profile), new_profile, 1, 1 if (profile and profile.get("profile")) else 0, time.time())
                )
                db.commit()
        except Exception as e:
            logger.warning(f"保存最终画像结果失败: {e}")

        # 发送结果
        if not silent:
            await send_reply(websocket, message_type, target_id,
                f"👤 {profile_nickname} 的用户画像已更新：\n\n{new_profile}", user_id, reply_id)
        logger.info(f"✅ 画像更新完成: {profile_nickname}({profile_user_id}), {len(new_profile)} 字")


async def handle_update_profile(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int],
    profile_user_id: int, profile_nickname: str
) -> None:
    """
    /更新画像 xxx 指令：LLM 分析该用户的聊天记录，更新/创建用户画像。
    """
    set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
    await send_reply(websocket, message_type, target_id,
        f"⏳ 正在后台分析 {profile_nickname} 的聊天记录，请稍候...（期间可以正常聊天）", user_id, reply_id)

    _task_key = _TASK_REGISTRY.register(
        "人设画像更新", f"更新画像 {profile_nickname}({profile_user_id})",
        group_id=target_id if message_type == "group" else 0,
        user_id=user_id, status="queued")

    async def _update_task():
        await _do_update_profile(
            websocket, message_type, target_id, user_id, reply_id,
            profile_user_id, profile_nickname
        )

    await _enqueue_profile_update(_update_task(), task_key=_task_key)


# ============================================================
#  人设更新
# ============================================================

async def _do_update_persona(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int],
    persona_user_id: int, persona_nickname: str,
    silent: bool = False,
    priority: int = 0,  # 0=用户手动(高优先), 1=定时任务(低优先)
) -> None:
    """
    人设更新核心逻辑（内部函数，含锁）。
    从聊天记录中提取结构化人设 JSON，增量合并到已有数据。
    """
    # BUG2修复：动态获取最新 WebSocket，避免 NapCat 重连后闭包引用过期
    ws = _active_websocket if _active_websocket is not None else websocket
    if ws is None:
        logger.warning("WebSocket 未就绪，跳过本次人设更新")
        return

    async with _profile_update_lock:
        persona_row = get_persona_display(persona_user_id, target_id)
        old_persona = persona_row.get("persona") if persona_row else {}

        last_message_id = persona_row.get("last_persona_message_id", 0) if persona_row else 0
        last_scan_at = persona_row.get("last_persona_scan_at", 0) if persona_row else 0
        # 旧断点副本：部分批次失败时不推进断点（下次重扫自动补跑失败批次）
        start_last_id = last_message_id
        start_last_scan = last_scan_at

        messages = _extract_relevant_messages(persona_user_id, target_id, last_message_id, last_scan_at)

        if not messages:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"👤 暂无 {persona_nickname} 的新增聊天记录，人设已是最新状态～", user_id, reply_id)
            return

        total_new = len(messages)

        # ─── 新增量不足 500 条，跳过更新 ───
        MIN_INCREMENTAL_MESSAGES = int(_pcfg().get('min_incremental_messages', 500))
        if total_new < MIN_INCREMENTAL_MESSAGES:
            # 不推进断点，这批消息留到下次积累够了再一起处理
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"📊 {persona_nickname} 仅有 {total_new} 条新消息（阈值 {MIN_INCREMENTAL_MESSAGES}），暂不更新人设～",
                    user_id, reply_id)
            else:
                logger.info(f"📊 {persona_nickname} 仅有 {total_new} 条新消息（阈值 {MIN_INCREMENTAL_MESSAGES}），跳过人设更新")
            return

        last_id = messages[-1]["message_id"]
        last_scan_at = messages[-1]["created_at"]

        sessions = _split_into_sessions(messages)

        nickname_map: dict[int, str] = {}
        for msg in messages:
            uid = msg["user_id"]
            # 一直覆盖：messages 按时间升序，最后赋值的 = 最新昵称（用户可能改过名）
            nickname_map[uid] = msg["nickname"]

        # 目标昵称强制覆盖：跨群合并场景下 nickname_map 可能取到目标用户在
        # 其他群/历史昵称，与提示词中的当前昵称不一致，会导致 LLM 认不出目标用户
        # （张冠李戴根因之一）。用调用方传入的当前昵称覆盖。
        nickname_map[persona_user_id] = persona_nickname

        session_texts = []
        # 全局短 ID 编号：批次内跨 session 唯一，LLM 输出可归一化
        global_uid_to_short: dict[int, str] = {}
        _gcounter = 1
        for _m in messages:
            if _m["user_id"] not in global_uid_to_short:
                global_uid_to_short[_m["user_id"]] = f"U{_gcounter}"
                _gcounter += 1
        global_short_map = _build_short_map(global_uid_to_short, nickname_map)

        for sess in sessions:
            text = _format_session_text(sess, persona_user_id, nickname_map, global_uid_to_short)
            if len(text) > _batch_chars():
                session_texts.extend(_split_long_session_chunks(sess, persona_user_id, nickname_map, global_uid_to_short))
            else:
                session_texts.append(text)

        chat_log_text = "\n\n--- 对话分隔 ---\n\n".join(session_texts)

        # 【熔断 1】发言过少
        if total_new < 3 or len(chat_log_text.strip()) < 50:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🤷 {persona_nickname} 的发言太少啦，没有足够信息建立用户人设哦！", user_id, reply_id)
            return

        token_chunks = chunk_messages_by_token(session_texts, target_tokens=_batch_chars())

        if len(token_chunks) > 1 or len(chat_log_text) > _direct_threshold():
            # Map→Reduce 路径
            set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🔍 正在分析 {persona_nickname} 的 {total_new} 条聊天记录（{len(sessions)} 个对话 Session，分 {len(token_chunks)} 批处理），请稍候...", user_id, reply_id)
            logger.info(f"🔍 开始人设分析: {persona_nickname}({persona_user_id}), {total_new} 条新消息, {len(sessions)} 个 Session, {len(token_chunks)} 批")

            # Map 阶段：提取结构化人设片段
            extract_prompt = _prompt("persona_map_system", nickname=persona_nickname,
                                      **_prompt_limit_ctx())

            # Map 阶段：分批提取人设（gather 并发提交，由 call_llm 并行信号量/串行队列统一限流）
            async def _persona_map_batch(i, chunk):
                batch_text = "\n\n--- 对话分隔 ---\n\n".join(chunk)
                batch_num = i + 1
                logger.info(f"📝 人设批次 {batch_num}/{len(token_chunks)}...")

                msg = _prompt("persona_map_user", nickname=persona_nickname, batch_text=batch_text)

                batch_result = ""
                parsed = None
                for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
                    try:
                        reply = await _call_llm_net([
                            {"role": "system", "content": extract_prompt},
                            {"role": "user", "content": msg}
                        ], priority=priority, source="人设", **_llm_kwargs("map"))
                    except LLMNetworkExhausted as _ne:
                        logger.error(f"❌ Map 批次 {batch_num} 网络异常: {_ne}")
                        return None
                    batch_result = reply.strip()
                    if not batch_result or _is_llm_error(batch_result):
                        logger.warning(f"🔄 Map 批次 {batch_num} 返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
                        continue
                    # JSON 解析失败也重试（DeepSeek 思考模式下 content 可能为空、
                    # 只有 reasoning，或返回被截断的 JSON；_parse_persona_json
                    # 失败返回 {}，用 truthiness 判断）
                    parsed = _parse_persona_json(batch_result)
                    if parsed:
                        break
                    logger.warning(f"⚠️ Map 批次 {batch_num} JSON 解析失败 (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
                else:
                    logger.error(f"❌ Map 批次 {batch_num} 达到最大重试次数，跳过该批次")
                    return None

                # U 编号引用归一化为 昵称(qq号)（落库与 Reduce 输入均使用归一化结果）
                if parsed:
                    parsed = _normalize_u_refs(parsed, global_short_map)
                    # 兜底校验：剔除 relationships 中指向目标用户自身的条目（LLM 认错人防线）
                    if _strip_self_relationships(parsed.get("persona"), persona_user_id):
                        logger.warning(f"⚠️ 人设 Map 批次 {batch_num} 已剔除目标自身关系条目")

                is_valid = 1 if parsed else 0
                try:
                    debug_text = batch_text if CONFIG.get("DEBUG_SAVE_BATCH_TEXT") else ""
                    with get_persona_db() as db:
                        db.execute(
                            "INSERT INTO persona_batch_results (user_id, nickname, group_id, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at, batch_text) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (persona_user_id, persona_nickname, target_id, batch_num, len(token_chunks),
                             len(batch_text), json.dumps(parsed, ensure_ascii=False) if parsed else batch_result,
                             is_valid, 1, time.time(), debug_text)
                        )
                        db.commit()
                except Exception as e:
                    logger.warning(f"保存人设 Map 批次中间结果失败: {e}")

                return parsed

            map_results = await asyncio.gather(*[_map_gather_run(_persona_map_batch(i, chunk)) for i, chunk in enumerate(token_chunks)])
            batches = [b for b in map_results if b]

            valid_batches = [b for b in batches if any(b.values())]

            if not valid_batches:
                if not batches:
                    # 全部批次 LLM 调用失败（模型不可用）——区别于"全是表情包/复读"
                    logger.error(f"❌ {persona_nickname} 人设 Map 全部批次失败（模型不可用），断点未推进")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"⚠️ 模型暂时不可用（限流或服务异常），{persona_nickname} 的人设更新未能完成，请稍后重试。", user_id, reply_id)
                else:
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"🤷 {persona_nickname} 最近全是表情包或复读，没有足够信息建立用户人设哦！", user_id, reply_id)
                return

            # ─── 共享合并规则 ───
            merge_rules = _prompt("persona_merge_rules",
                                 relationships_limit=int(_persona_limits()["relationships"]))

            reduce_system_template = _prompt("persona_reduce_solo_system",
                                            nickname=persona_nickname, **_prompt_limit_ctx())

            # ─── 多级收敛合并：batch_token(_batch_chars()) 长度驱动，替代固定 REDUCE_GROUP_SIZE=9
            # 分组预合并 + 固定两级。总长 > _batch_chars() 时按阈值逐段预合并，递归收敛为单一 JSON。
            async def _combined_on_group_merged(batch_idx, total_groups, merged_item):
                inter_json_str = json.dumps(merged_item, ensure_ascii=False)
                try:
                    with get_persona_db() as db:
                        db.execute(
                            "INSERT INTO persona_batch_results (user_id, nickname, group_id, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
                            (persona_user_id, persona_nickname, target_id,
                             -batch_idx, total_groups,
                             len(inter_json_str), inter_json_str, time.time())
                        )
                        db.commit()
                    logger.info(f"💾 多级合并中间结果已保存 (batch_index=-{batch_idx}, 组 {batch_idx}/{total_groups} 序号)")
                except Exception as e:
                    logger.warning(f"保存多级合并中间结果失败: {e}")

            valid_batches = await _hierarchical_merge_json(
                valid_batches, _batch_chars(), persona_nickname, priority,
                reduce_system_template, "人设",
                on_group_merged=_combined_on_group_merged)

            # 第二轮 Reduce：旧人设 + 新增量融合
            summaries_json = json.dumps(valid_batches, ensure_ascii=False, indent=2)

            if len(summaries_json) < 100:
                if not silent:
                    await send_reply(ws, message_type, target_id,
                        f"🤷 {persona_nickname} 的有效信息太少啦，没有足够内容更新人设哦！", user_id, reply_id)
                return

            system_prompt = reduce_system_template.replace("{old_data_clause}", "以及已有的人设数据。")

            old_json = json.dumps(old_persona, ensure_ascii=False) if old_persona else "{}"
            user_prompt = _prompt("persona_reduce_user", old_json=old_json,
                                  summaries_json=summaries_json)

        else:
            # 消息不多，直接分析
            set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🔍 正在分析 {persona_nickname} 的 {total_new} 条聊天记录（{len(sessions)} 个对话 Session），请稍候...", user_id, reply_id)
            logger.info(f"🔍 开始人设分析: {persona_nickname}({persona_user_id}), {total_new} 条新消息, {len(sessions)} 个 Session")

            system_prompt = _prompt("persona_direct_system", nickname=persona_nickname,
                                    **_prompt_limit_ctx())
            old_json = json.dumps(old_persona, ensure_ascii=False) if old_persona else "{}"
            user_prompt = _prompt("persona_direct_user", old_json=old_json,
                                  nickname=persona_nickname, chat_log_text=chat_log_text)

        # 调用 LLM
        new_persona_json = ""
        for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
            try:
                reply = await _call_llm_net([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ], priority=priority, source="人设", **_llm_kwargs("persona_reduce"))
            except LLMNetworkExhausted as _ne:
                logger.error(f"❌ 人设 Reduce 网络异常，跳过 {persona_nickname} 的人设更新: {_ne}")
                return
            new_persona_json = reply.strip()
            if new_persona_json and not _is_llm_error(new_persona_json):
                # 字段完整性校验（历史事故：合并偶发只写一个字段）：单更路径无多批次并集概念
                # （直接聊天文本一次提取），保持有值字段 ≥ 4 的门槛，重试而非落库残缺人设
                _pp = _parse_persona_json(new_persona_json)
                if _pp and sum(1 for v in _pp.values() if v) >= 4:
                    break
                if _pp:
                    _fc = sum(1 for v in _pp.values() if v)
                    logger.warning(
                        f"🔄 人设 Reduce 输出字段过少（有值 {_fc}/9，疑似只写单字段），"
                        f"重试中 (attempt {_attempt}/{_MAX_LLM_RETRIES})"
                    )
                    continue
            logger.warning(f"🔄 Reduce 阶段返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
        else:
            logger.error(f"❌ 人设 Reduce 达到最大重试次数，跳过 {persona_nickname} 的人设更新")
            return

        new_persona = _parse_persona_json(new_persona_json)
        if not new_persona:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    "⚠️ LLM 返回格式异常，人设更新失败，请稍后重试。", user_id, reply_id)
            return

        # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
        new_persona = _normalize_u_refs(new_persona, global_short_map)
        # 兜底校验：剔除 relationships 中指向目标用户自身的条目（LLM 认错人防线）
        if _strip_self_relationships(new_persona.get("persona"), persona_user_id):
            logger.warning(f"⚠️ 人设直接分析 Reduce 输出已剔除目标自身关系条目")

        # LLM Reduce 阶段已完成新旧语义融合，直接使用其结果
        # 压缩循环替代硬截断：字段超限/总量超限时 LLM 压缩（尽量不丢信息），
        # _check_persona_limits 仅作为最终监控告警（2026-08-08）
        final_persona = await _compress_persona_loop(new_persona, persona_nickname, priority)
        _check_persona_limits(final_persona, persona_nickname)
        # 一致性校验保险（2026-08-10）：增量更新校验新 vs 旧——失败则丢弃新人设（保留旧人设）
        if old_persona and not await _verify_consistency(
                "人设", json.dumps(old_persona, ensure_ascii=False),
                json.dumps(final_persona, ensure_ascii=False), persona_nickname, priority):
            logger.error(f"❌ {persona_nickname} 人设一致性校验失败，丢弃新人设保留旧人设")
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"⚠️ {persona_nickname} 人设更新校验未通过（与旧人设不一致），已保留旧人设，请稍后重试。",
                    user_id, reply_id)
            return
        # 部分批次失败（模型异常）时保存结果但断点不推进：下次重扫自动补跑失败批次
        _chunks = locals().get("token_chunks", [])
        _batches = locals().get("batches", [])
        if len(_chunks) > 1 and len(_batches) < len(_chunks):
            last_id, last_scan_at = start_last_id, start_last_scan
            logger.warning(f"⚠️ {persona_nickname} 部分批次失败 ({len(_batches)}/{len(_chunks)})，保存人设但断点不推进，下次自动补跑")
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"⚠️ {persona_nickname} 部分批次因模型异常未完成，已保存当前人设，剩余部分将在下次更新时自动补全。", user_id, reply_id)
        save_persona(persona_user_id, final_persona, persona_nickname, target_id,
                     last_persona_message_id=last_id, last_persona_scan_at=last_scan_at)

        # 保存 Reduce 最终结果
        try:
            with get_persona_db() as db:
                db.execute(
                    "INSERT INTO persona_batch_results (user_id, nickname, group_id, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at) "
                    "VALUES (?, ?, ?, 0, ?, ?, ?, 1, ?, ?)",
                    (persona_user_id, persona_nickname, target_id,
                     len(token_chunks) if 'token_chunks' in locals() else 1,
                     len(json.dumps(final_persona, ensure_ascii=False)),
                     json.dumps(final_persona, ensure_ascii=False),
                     1 if old_persona else 0,
                     time.time())
                )
                db.commit()
        except Exception as e:
            logger.warning(f"保存人设 Reduce 最终结果失败: {e}")

        persona_text = persona_to_text(final_persona)
        if not silent:
            await send_reply(websocket, message_type, target_id,
                f"✅ {persona_nickname} 的人设已更新！\n\n{persona_text}", user_id, reply_id)

        logger.info(f"✅ 人设更新完成: {persona_nickname}({persona_user_id})")


async def handle_update_persona(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int],
    persona_user_id: int, persona_nickname: str
) -> None:
    """
    /更新人设 xxx 指令：LLM 分析该用户的聊天记录，提取结构化人设 JSON。
    """
    set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
    await send_reply(websocket, message_type, target_id,
        f"⏳ 正在后台分析 {persona_nickname} 的聊天记录以提取人设，请稍候...（期间可以正常聊天）", user_id, reply_id)

    _task_key = _TASK_REGISTRY.register(
        "人设画像更新", f"更新人设 {persona_nickname}({persona_user_id})",
        group_id=target_id if message_type == "group" else 0,
        user_id=user_id, status="queued")

    async def _update_task():
        await _do_update_persona(
            websocket, message_type, target_id, user_id, reply_id,
            persona_user_id, persona_nickname
        )

    await _enqueue_profile_update(_update_task(), task_key=_task_key)


# ============================================================
#  联合更新：画像 + 人设（Combined Map）
# ============================================================

async def _verify_consistency(kind: str, old_text: str, new_text: str, nickname: str,
                              priority: int = 0) -> bool:
    """增量更新一致性校验（2026-08-10 保险步骤）。

    新人设/画像生成后，与旧版本一起输入 LLM（开思考 max）判断新版本
    是否是旧版本的正确更新：核心事实保留、无编造矛盾、身份稳定、结构完整。
    返回 True = 校验通过；False = 明确判定 invalid（调用方丢弃并重试）。
    LLM 调用自身失败（网络/解析）→ 返回 True（保险不是闸门，不阻塞更新）。
    """
    try:
        if not old_text or not new_text:
            return True
        system_prompt = _prompt("verify_system")
        user_prompt = _prompt("verify_user", kind=kind, old_text=old_text, new_text=new_text)
        reply = await _call_llm_net(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            priority=priority, source="审核", **_llm_kwargs("verify"))
        parsed = _parse_persona_json(reply.strip()) if reply else None
        if not parsed:
            logger.warning(f"🔄 {kind} 一致性校验输出解析失败，按通过处理（{nickname}）")
            return True
        valid = parsed.get("valid")
        reason = str(parsed.get("reason", ""))[:60]
        if valid is False:
            logger.warning(f"❌ {kind} 一致性校验失败 [{nickname}]: {reason}")
            return False
        logger.info(f"✅ {kind} 一致性校验通过 [{nickname}]: {reason}")
        return True
    except Exception as e:
        logger.warning(f"🔄 {kind} 一致性校验异常，按通过处理 [{nickname}]: {e}")
        return True


async def _do_update_profile_and_persona(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int],
    target_user_id: int, target_nickname: str,
    silent: bool = False,
    priority: int = 0,
) -> None:
    """
    联合更新核心逻辑：画像 + 人设一次性更新（Combined Map 方案）。
    每个聊天批次只调一次 LLM，同时产出人设 JSON 片段和画像素材，
    然后分别走人设 Reduce 和画像 Reduce。
    """
    ws = _active_websocket if _active_websocket is not None else websocket
    if ws is None:
        logger.warning("WebSocket 未就绪，跳过本次联合更新")
        return

    async with _profile_update_lock:
        # ─── 读取旧数据 ───
        profile = get_user_profile(target_user_id, target_id)
        persona_row = get_persona_display(target_user_id, target_id)
        old_profile = profile["profile"] if profile else ""
        old_persona = persona_row.get("persona") if persona_row else {}

        # ─── 统一断点：取两者中较早的一个 ───
        profile_last_mid = profile["last_message_id"] if profile else 0
        profile_last_scan = profile["last_scan_at"] if profile else 0
        persona_last_mid = persona_row.get("last_persona_message_id", 0) if persona_row else 0
        persona_last_scan = persona_row.get("last_persona_scan_at", 0) if persona_row else 0

        if profile_last_mid > 0 and persona_last_mid > 0:
            last_message_id = min(profile_last_mid, persona_last_mid)
            last_scan_at = min(profile_last_scan, persona_last_scan)
        elif profile_last_mid > 0:
            last_message_id = profile_last_mid
            last_scan_at = profile_last_scan
        elif persona_last_mid > 0:
            last_message_id = persona_last_mid
            last_scan_at = persona_last_scan
        else:
            last_message_id = 0
            last_scan_at = 0

        # ─── 公共聊天材料准备 ───
        messages, last_id, last_scan_at_ts, nickname_map, chat_log_text = _prepare_chat_messages(
            target_user_id, target_id, last_message_id, last_scan_at
        )
        # 目标昵称强制覆盖：跨群合并场景下 nickname_map 可能取到目标用户在
        # 其他群/历史昵称，与提示词中的当前昵称不一致，会导致 LLM 认不出目标用户
        # （张冠李戴根因之一）。用调用方传入的当前昵称覆盖。
        nickname_map[target_user_id] = target_nickname

        if not messages:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"👤 暂无 {target_nickname} 的新增聊天记录，画像和人设已是最新状态～", user_id, reply_id)
            return

        total_new = len(messages)

        MIN_INCREMENTAL_MESSAGES = int(_pcfg().get('min_incremental_messages', 500))
        if total_new < MIN_INCREMENTAL_MESSAGES:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"📊 {target_nickname} 仅有 {total_new} 条新消息（阈值 {MIN_INCREMENTAL_MESSAGES}），暂不更新～",
                    user_id, reply_id)
            else:
                logger.info(f"📊 {target_nickname} 仅有 {total_new} 条新消息（阈值 {MIN_INCREMENTAL_MESSAGES}），跳过联合更新")
            return

        # 【熔断 1】发言过少
        if total_new < 3 or len(chat_log_text.strip()) < 50:
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🤷 {target_nickname} 的发言太少啦，没有足够信息建立画像和人设哦！", user_id, reply_id)
            return

        # 按字符数分批（全局短 ID 编号：批次内跨 session 唯一，LLM 输出可归一化）
        global_uid_to_short: dict[int, str] = {}
        _gcounter = 1
        for _m in messages:
            if _m["user_id"] not in global_uid_to_short:
                global_uid_to_short[_m["user_id"]] = f"U{_gcounter}"
                _gcounter += 1
        global_short_map = _build_short_map(global_uid_to_short, nickname_map)

        session_texts_for_batch: list[str] = []
        sessions = _split_into_sessions(messages)
        for sess in sessions:
            text = _format_session_text(sess, target_user_id, nickname_map, global_uid_to_short)
            if len(text) > _batch_chars():
                session_texts_for_batch.extend(_split_long_session_chunks(sess, target_user_id, nickname_map, global_uid_to_short))
            else:
                session_texts_for_batch.append(text)

        token_chunks = chunk_messages_by_token(session_texts_for_batch, target_tokens=_batch_chars())

        # 08-22 batch 端点开关：每轮运行开始清空该用户的内存失败计数（与 DB 路径
        # "最近一轮"窗口语义对齐——计数只反映本轮，避免跨轮累积误推进/误不推进）
        _bendpoint_fail_reset(target_user_id)

        # ─── Combined Map 阶段 ───
        if len(token_chunks) > 1 or len(chat_log_text) > _direct_threshold():
            set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🔍 正在联合分析 {target_nickname} 的 {total_new} 条聊天记录（{len(sessions)} 个 Session，分 {len(token_chunks)} 批），请稍候...", user_id, reply_id)
            logger.info(f"🔍 开始联合分析: {target_nickname}({target_user_id}), {total_new} 条新消息, {len(token_chunks)} 批")

            # 断点恢复：加载该用户最近一次同 total_batches 的批次缓存，跳过已完成的批次
            batch_cache = _load_combined_batch_cache(target_user_id, len(token_chunks))
            reused = 0

            async def _combined_map_batch(i, chunk):
                nonlocal reused
                batch_text = "\n\n--- 对话分隔 ---\n\n".join(chunk)
                batch_num = i + 1
                # 统计目标用户有效文本发言行数（low_information 误判守卫用）
                target_short = global_uid_to_short.get(target_user_id, "")
                target_useful_lines = _count_useful_lines(batch_text, target_short) if target_short else 0

                # 已缓存的批次直接复用（避免超时/重启后全量重跑）
                # 校验 batch_char_count 一致，防止消息集变化后批次错位
                cached = batch_cache.get(batch_num)
                if cached is not None and cached.get("batch_char_count") == len(batch_text):
                    if cached["is_valid"] and cached["parsed"]:
                        # 旧缓存可能含 U 编号引用（历史版本），统一归一化为 昵称(qq号)
                        parsed = _normalize_u_refs(cached["parsed"], global_short_map)
                        # 兜底校验：剔除 relationships 中指向目标用户自身的条目（LLM 认错人防线）
                        _strip_self_relationships(parsed.get("persona"), target_user_id)
                        persona_c = parsed.get("persona", {})
                        profile_c = parsed.get("profile_material", {})
                        if persona_c or profile_c:
                            reused += 1
                            return {"persona": persona_c, "profile_material": profile_c}
                    # is_valid=0 且 parsed 有值 = 低信息量（正常结果，跳过合理）
                    # 守卫：目标用户有效发言 ≥3 行 → 旧缓存是误判，不复用，重新调 LLM
                    if cached["parsed"]:
                        if target_useful_lines >= 3:
                            logger.info(f"🔄 Combined Map 批次 {batch_num} 缓存为 low_information 但目标用户有效发言 {target_useful_lines} 行，疑似误判，重新调用 LLM")
                        else:
                            reused += 1
                            logger.info(f"♻️ Combined Map 批次 {batch_num} 复用缓存 (is_valid=0 低信息量)")
                            return None
                    # is_valid=0 且 parsed 为空 = 失败批次（LLM 错误/JSON 解析失败），
                    # 不可复用，重新调用 LLM（否则修复后重跑仍缺该批次）
                    logger.info(f"🔄 Combined Map 批次 {batch_num} 缓存为失败状态(is_valid=0 无解析结果)，重新调用 LLM")

                # 目标用户 0 有效发言：跳过 LLM（省 _batch_chars() 输入 token），直接落库
                # 低信息量——与 LLM 判 low_information 的结果完全一致（is_valid=0 +
                # parsed 有值），缓存复用/断点推进逻辑无缝衔接。LLM 判定标准本来就是
                # "有效文本发言 < 3 条"才可熔断，0 条必然低信息量，无需再花一次调用确认。
                if target_useful_lines == 0:
                    logger.info(f"ℹ️ Combined Map 批次 {batch_num} 目标用户无有效发言，跳过 LLM（低信息量）")
                    low_info = _get_combined_map_low_info()
                    _save_combined_batch(target_user_id, target_id, target_nickname, batch_num, len(token_chunks),
                                         len(batch_text), json.dumps(low_info, ensure_ascii=False), low_info, 0, batch_text)
                    return None

                logger.info(f"📝 Combined Map 批次 {batch_num}/{len(token_chunks)}...")

                return await _combined_map_call(batch_text, target_nickname, target_user_id, batch_num, len(token_chunks), priority, group_id=target_id, short_map=global_short_map, target_useful_lines=target_useful_lines)

            map_results = await asyncio.gather(*[_map_gather_run(_combined_map_batch(i, chunk)) for i, chunk in enumerate(token_chunks)])
            combined_results = [r for r in map_results if r]

            if reused:
                logger.info(f"♻️ {target_nickname} 断点恢复: 复用 {reused}/{len(token_chunks)} 个已处理批次")

            # 过滤有效结果
            valid_results = [r for r in combined_results if r]

            if not valid_results:
                # 区分模型故障与真实低信息量：失败形态批次（is_valid=0 且 parsed 空）覆盖全部批次 = 模型不可用
                if _recent_combined_batch_failure_count(target_user_id, len(token_chunks)) >= len(token_chunks):
                    logger.error(f"❌ {target_nickname} Combined Map 全部批次失败（模型不可用），断点未推进")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"⚠️ 模型暂时不可用（限流或服务异常），{target_nickname} 的联合更新未能完成，请稍后重试。", user_id, reply_id)
                else:
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"🤷 {target_nickname} 最近全是表情包或复读，没有足够信息建立画像和人设哦！", user_id, reply_id)
                return

            # 分离结果
            persona_fragments = [r["persona"] for r in valid_results if r.get("persona")]
            profile_materials = [r["profile_material"] for r in valid_results if r.get("profile_material")]

            # 检查是否有效批次过少
            if len(valid_results) < len(token_chunks) // 2 and len(token_chunks) > 1:
                logger.warning(f"⚠️ {target_nickname} 有效批次过少 ({len(valid_results)}/{len(token_chunks)})")

        else:
            # 短消息路径：一次 Combined Map（用全局编号重建的 session 文本，保证归一化映射一致）
            set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"🔍 正在联合分析 {target_nickname} 的 {total_new} 条聊天记录，请稍候...", user_id, reply_id)
            logger.info(f"🔍 开始联合分析（短消息路径）: {target_nickname}({target_user_id})")

            short_chat_text = "\n\n--- 对话分隔 ---\n\n".join(session_texts_for_batch)
            result = await _combined_map_call(short_chat_text, target_nickname, target_user_id, 1, 1, priority, group_id=target_id, short_map=global_short_map)
            if not result:
                if _recent_combined_batch_failure_count(target_user_id, 1) >= 1:
                    logger.error(f"❌ {target_nickname} Combined Map 调用失败（模型不可用），断点未推进")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"⚠️ 模型暂时不可用（限流或服务异常），{target_nickname} 的联合更新未能完成，请稍后重试。", user_id, reply_id)
                else:
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"🤷 {target_nickname} 发言太少啦，没有足够信息建立画像和人设哦！", user_id, reply_id)
                return

            persona_fragments = [result["persona"]] if result.get("persona") else []
            profile_materials = [result["profile_material"]] if result.get("profile_material") else []
            # short path 单批：无失败批次（失败已在上面 return），供下方断点推进判断使用
            combined_results = [result]

        # ─── 人设 Reduce（含一致性校验保险：2026-08-10 增量更新时校验新 vs 旧，
        #      明确 invalid 则丢弃重新生成，最多重试一次；重试后仍失败保留旧人设）───
        new_persona = None
        if persona_fragments:
            for _v_attempt in range(1, 3):
                new_persona = await _do_persona_reduce(persona_fragments, old_persona, target_nickname, priority,
                                                       user_id=target_user_id, group_id=target_id)
                if new_persona:
                    # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
                    new_persona = _normalize_u_refs(new_persona, global_short_map)
                    # 兜底校验：剔除 relationships 中指向目标用户自身的条目（LLM 认错人防线）
                    if _strip_self_relationships(new_persona.get("persona"), target_user_id):
                        logger.warning(f"⚠️ 联合更新人设 Reduce 输出已剔除目标自身关系条目")
                if not new_persona:
                    logger.warning(f"⚠️ {target_nickname} 人设 Reduce 失败（{target_user_id}），本次联合更新取消")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"⚠️ {target_nickname} 人设 Reduce 失败，本次联合更新取消。", user_id, reply_id)
                    return
                # 一致性校验（仅增量：有旧人设才校验；首次建库跳过）
                if not old_persona:
                    break
                if await _verify_consistency("人设",
                                             json.dumps(old_persona, ensure_ascii=False),
                                             json.dumps(new_persona, ensure_ascii=False),
                                             target_nickname, priority):
                    break
                logger.warning(f"🔄 {target_nickname} 人设一致性校验失败，重新生成（attempt {_v_attempt}/2）")
            else:
                # 2 次校验均失败：丢弃新人设，保留旧人设（保险步骤兜底）
                logger.error(f"❌ {target_nickname} 人设两次一致性校验均失败，丢弃新人设保留旧人设")
                new_persona = None

        # ─── 画像 Reduce（含一致性校验保险：2026-08-10 同人设逻辑）───
        new_profile = None
        if profile_materials:
            for _v_attempt in range(1, 3):
                new_profile = await _do_profile_reduce(profile_materials, old_profile, target_nickname, priority,
                                                       user_id=target_user_id, group_id=target_id)
                if new_profile:
                    new_profile = _normalize_u_refs(new_profile, global_short_map)
                if not new_profile:
                    logger.warning(f"⚠️ {target_nickname} 画像 Reduce 失败（{target_user_id}），本次联合更新取消")
                    if not silent:
                        await send_reply(ws, message_type, target_id,
                            f"⚠️ {target_nickname} 画像 Reduce 失败，本次联合更新取消。", user_id, reply_id)
                    return
                # 一致性校验（仅增量：有旧画像才校验；首次建库跳过）
                if not old_profile:
                    break
                if await _verify_consistency("画像", old_profile, new_profile,
                                             target_nickname, priority):
                    break
                logger.warning(f"🔄 {target_nickname} 画像一致性校验失败，重新生成（attempt {_v_attempt}/2）")
            else:
                logger.error(f"❌ {target_nickname} 画像两次一致性校验均失败，丢弃新画像保留旧画像")
                new_profile = None

        # ─── 原子保存：有失败批次时保存成功结果但断点不推进 ───
        # 部分批次失败（模型异常）时，断点维持本次开始时的位置：下次运行失败批次
        # （缓存 is_valid=0 且 parsed 空，不可复用）自动重扫重跑，避免数据永久缺失。
        # ⚠️ 只统计"真失败"（is_valid=0 且 parsed 空）——低信息量（is_valid=0 但
        # parsed 有值）是正常结果，LLM 判 low_information 是正确的，不能阻止断点推进
        # （否则只要存在低信息量批次，断点永远停在 0，每次全量重扫 + 缓存失效重调 LLM）。
        _real_failures = _recent_combined_batch_failure_count(target_user_id, len(token_chunks))
        if len(token_chunks) > 1 and _real_failures > 0:
            last_id_final = last_message_id
            last_scan_final = last_scan_at
            logger.warning(f"⚠️ {target_nickname} 部分批次失败 ({_real_failures}/{len(token_chunks)})，保存结果但断点不推进，下次自动补跑")
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"⚠️ {target_nickname} 部分批次因模型异常未完成，已保存当前结果，剩余部分将在下次更新时自动补全。", user_id, reply_id)
        else:
            last_id_final = messages[-1]["message_id"]
            last_scan_final = messages[-1]["created_at"]

        try:
            if new_profile:
                save_user_profile(target_user_id, target_nickname, new_profile, last_id_final, group_id=target_id, last_scan_at=last_scan_final)
            if new_persona:
                _check_persona_limits(new_persona, target_nickname)
                save_persona(target_user_id, new_persona, target_nickname, target_id,
                             last_persona_message_id=last_id_final, last_persona_scan_at=last_scan_final)
        except Exception as e:
            logger.error(f"❌ 联合更新保存失败: {e}")
            if not silent:
                await send_reply(ws, message_type, target_id,
                    f"⚠️ {target_nickname} 联合更新保存失败，请稍后重试。", user_id, reply_id)
            return

        # 发送结果
        parts = []
        if new_profile:
            parts.append(f"👤 {target_nickname} 的用户画像已更新：\n\n{new_profile}")
        if new_persona:
            parts.append(f"✅ {target_nickname} 的人设已更新！\n\n{persona_to_text(new_persona)}")

        if not silent:
            await send_reply(ws, message_type, target_id, "\n\n---\n\n".join(parts), user_id, reply_id)

        logger.info(f"✅ 联合更新完成: {target_nickname}({target_user_id}), 画像={len(new_profile) if new_profile else 0}字, 人设={'✓' if new_persona else '✗'}")


async def handle_update_profile_and_persona(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int],
    target_user_id: int, target_nickname: str
) -> None:
    """
    /更新画像和人设 xxx 指令：联合更新画像和人设（Combined Map 方案）。
    """
    set_cooldown(_session_key(target_id if message_type == "group" else 0, user_id))
    await send_reply(websocket, message_type, target_id,
        f"⏳ 正在后台联合分析 {target_nickname} 的聊天记录（画像+人设），请稍候...（期间可以正常聊天）", user_id, reply_id)

    _task_key = _TASK_REGISTRY.register(
        "人设画像更新", f"联合更新 {target_nickname}({target_user_id})",
        group_id=target_id if message_type == "group" else 0,
        user_id=user_id, status="queued")

    async def _update_task():
        await _do_update_profile_and_persona(
            websocket, message_type, target_id, user_id, reply_id,
            target_user_id, target_nickname
        )

    await _enqueue_profile_update(_update_task(), task_key=_task_key)


def _persona_union_threshold(fragments: list[dict]) -> int:
    """字段完整性校验阈值：各批次有值字段的并集 - 1（允许少一个）。
    例：b1 有 ABC、b2 有 ACDE → 并集 {A,B,C,D,E}=5 → 阈值 4。
    下限保护：至少 1（并集过小时任何输出都算达标）。"""
    union: set[str] = set()
    for f in fragments:
        if isinstance(f, dict):
            union.update(k for k, v in f.items() if v)
    return max(1, len(union) - 1)


async def _do_persona_reduce(
    fragments: list[dict],
    old_persona: Optional[dict],
    nickname: str,
    priority: int = 0,
    user_id: int = 0,
    group_id: int = 0,
) -> Optional[dict]:
    """
    人设 Reduce：旧人设 + 人设片段 → 最终 persona JSON。
    保留现有人设 Reduce 逻辑。
    """
    valid_batches = [b for b in fragments if any(b.values())]
    if not valid_batches:
        return None

    # 合并提纯规则已内嵌于 persona_prompts["persona_reduce_system"]（GUI 可编辑）

    reduce_system_template = _prompt("persona_reduce_system",
                                  nickname=nickname, **_prompt_limit_ctx())

    # 多级收敛合并：batch_token(_batch_chars()) 长度驱动，替代固定 REDUCE_GROUP_SIZE=9
    # 分组预合并 + 固定两级。总长 > _batch_chars() 时按阈值逐段预合并，递归收敛为单一 JSON。
    # on_group_merged 回调：把每组合并中间结果落库（batch_index 取负，与 Map 正索引区分，
    # 断点/追溯用；与 _do_update_persona 单独路径的 _combined_on_group_merged 行为一致）
    async def _persona_reduce_on_group_merged(batch_idx, total_groups, merged_item):
        inter_json_str = json.dumps(merged_item, ensure_ascii=False)
        try:
            with get_persona_db() as db:
                db.execute(
                    "INSERT INTO persona_batch_results (user_id, nickname, group_id, batch_index, total_batches, batch_char_count, analysis_result, is_valid, is_incremental, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
                    (user_id, nickname, group_id,
                     -batch_idx, total_groups,
                     len(inter_json_str), inter_json_str, time.time())
                )
                db.commit()
            logger.info(f"💾 人设合并中间结果已保存 (batch_index=-{batch_idx}, 组 {batch_idx}/{total_groups} 序号)")
        except Exception as e:
            logger.warning(f"保存人设合并中间结果失败: {e}")

    valid_batches = await _hierarchical_merge_json(
        valid_batches, _batch_chars(), nickname, priority,
        reduce_system_template, "人设",
        on_group_merged=_persona_reduce_on_group_merged)

    # 第二轮 Reduce：旧人设 + 新增量融合
    summaries_json = json.dumps(valid_batches, ensure_ascii=False, indent=2)

    if len(summaries_json) < 100:
        logger.warning(f"⚠️ 人设 Reduce 素材过少（{len(summaries_json)} 字符），无法合并")
        return None

    system_prompt = reduce_system_template.replace("{old_data_clause}", "以及已有的人设数据。")
    old_json = json.dumps(old_persona, ensure_ascii=False) if old_persona else "{}"
    user_prompt = _prompt("persona_reduce_user", old_json=old_json,
                          summaries_json=summaries_json)

    # 调用 LLM（有效输出 = 非错误文本 且 可解析为 JSON；格式漂移时重试而非静默失败）
    result = ""
    parsed_persona = None
    for _attempt in range(1, int(_pcfg().get('llm_retries', 5)) + 1):
        try:
            reply = await _call_llm_net([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], priority=priority, source="人设", **_llm_kwargs("persona_reduce"))
        except LLMNetworkExhausted as _ne:
            logger.error(f"❌ 人设 Reduce 网络异常: {_ne}")
            return None
        result = reply.strip()
        parsed_persona = _parse_persona_json(result)
        if result and not _is_llm_error(result) and parsed_persona:
            # 字段完整性校验：合并偶发"只写一个字段"，
            # 输出字段数 < 各批次并集-1 视为异常输出，重试而非落库残缺人设
            threshold = _persona_union_threshold(valid_batches)
            filled_count = sum(1 for v in parsed_persona.values() if v)
            if filled_count >= threshold:
                break
            logger.warning(
                f"🔄 人设 Reduce 输出字段过少（有值 {filled_count}/9，疑似只写单字段），"
                f"重试中 (attempt {_attempt}/{_MAX_LLM_RETRIES})"
            )
        else:
            logger.warning(f"🔄 人设 Reduce 返回无效 content (attempt {_attempt}/{_MAX_LLM_RETRIES})，重试中...")
    else:
        logger.error("❌ 人设 Reduce 达到最大重试次数，最后一次输出无法解析为 JSON")
        return None

    new_persona = parsed_persona
    if not new_persona:
        logger.error("❌ 人设 Reduce 输出无法解析为 JSON，已重试仍失败")
        return None

    # 压缩循环替代硬截断：字段超限/总量超限时 LLM 压缩（尽量不丢信息）（2026-08-08）
    new_persona = await _compress_persona_loop(new_persona, nickname, priority)

    return new_persona


from .task_registry import TASK_REGISTRY as _TASK_REGISTRY

_TASK_BATCH_SEQ = 0


def _task_batch_id(tag: str) -> str:
    """批量任务 id：时间戳+自增，防同秒重复。"""
    global _TASK_BATCH_SEQ
    _TASK_BATCH_SEQ += 1
    return f"{tag}-{int(time.time())}-{_TASK_BATCH_SEQ}"


def _task_label_uid(u: dict, prefix: str = "") -> str:
    """任务列表显示标签（2026-08-22）：`前缀+昵称(QQ号)`。

    prefix 区分指令来源：人设更新/画像更新/联合更新（scheduler 用 ⏰ 联合更新）。
    """
    return f"{prefix}{u['nickname']}({u['user_id']})"


async def _handle_update_all_personas(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int]
) -> None:
    """
    /更新全部人设 指令：为群内所有用户批量更新人设。
    优先级：未建立人设的用户 > 已建立人设的用户（增量更新）
    """
    from .database import CONFIG

    group_id = target_id if target_id > 0 else 0
    if not group_id:
        await send_reply(websocket, message_type, target_id,
            "📋 /更新全部人设 需要在群聊中使用", user_id, reply_id)
        return

    with get_db() as conn:
        all_users = conn.execute(
            "SELECT DISTINCT user_id, nickname FROM message_archive WHERE target_id = ? ORDER BY created_at DESC",
            (group_id,)
        ).fetchall()

    if not all_users:
        await send_reply(websocket, message_type, target_id,
            "📋 群内暂无聊天记录，无法更新人设", user_id, reply_id)
        return

    bot_qq = int(get_bot_uin() or 0)  # 08-22：从连接派生
    all_users = [dict(r) for r in all_users if dict(r)["user_id"] != bot_qq]

    # 去重：保留最新昵称（ORDER BY created_at DESC 排序下，同一 user_id 只保留第一行 = 最新）
    seen = {}
    for u in all_users:
        uid = u["user_id"]
        if uid not in seen:
            seen[uid] = u
    all_users = list(seen.values())

    with get_persona_db() as conn:
        personaed = conn.execute(
            "SELECT user_id FROM user_personas WHERE group_id = ?",
            (group_id,)
        ).fetchall()
        personaed_ids = {dict(r)["user_id"] for r in personaed}

    no_persona = [u for u in all_users if u["user_id"] not in personaed_ids]
    has_persona = [u for u in all_users if u["user_id"] in personaed_ids]

    queue = no_persona + has_persona
    total = len(queue)

    await send_reply(websocket, message_type, target_id,
        f"📋 开始批量更新人设，共 {total} 人\n"
        f"  ├─ 未建立人设: {len(no_persona)} 人（优先）\n"
        f"  └─ 已建立人设: {len(has_persona)} 人（增量更新）", user_id, reply_id)

    updated = 0
    failed = 0

    async def update_one(u: dict) -> tuple[str, bool]:
        uid = u["user_id"]
        nickname = u["nickname"]
        prefix = "🔴 未建人设" if uid not in personaed_ids else "🟢 增量更新"
        logger.info(f"{prefix}: {nickname}({uid})")
        try:
            await asyncio.wait_for(
                _do_update_persona(websocket, message_type, target_id, user_id, None, uid, nickname, priority=1),
                timeout=86400
            )
            return (f"{prefix}: {nickname}({uid})", True)
        except asyncio.TimeoutError:
            logger.error(f"⏱️ 更新人设超时: {nickname}({uid})")
            return (f"⏱️ 超时: {nickname}({uid})", False)
        except Exception as e:
            logger.error(f"❌ 更新人设失败 {nickname}({uid}): {e}")
            return (f"❌ 失败: {nickname}({uid}) - {str(e)[:50]}", False)

    results = []
    _batch_id = _task_batch_id("bpersonas")
    _task_keys = _TASK_REGISTRY.begin_batch(
        _batch_id, "人设画像更新", [_task_label_uid(u, "人设更新 ") for u in queue])
    try:
        for i, u in enumerate(queue):
            # 2026-08-22 暂停门：queued→running 转换前等放行（暂停时原地等待，
            # 批量停在当前用户；执行中用户不被打断）
            await _TASK_REGISTRY.wait_if_paused()
            _TASK_REGISTRY.set_status(_task_keys[i], "running")
            try:
                result = await update_one(u)
            finally:
                _TASK_REGISTRY.finish(_task_keys[i])
            results.append(result)
            if result[1]:
                updated += 1
            else:
                failed += 1

            if (i + 1) % 3 == 0 or i == total - 1:
                progress = i + 1
                await send_reply(websocket, message_type, target_id,
                    f"📋 进度更新: {progress}/{total} 已完成\n"
                    f"  ├─ 成功: {updated}\n"
                    f"  └─ 失败: {failed}", user_id, reply_id)
    finally:
        _TASK_REGISTRY.finish_batch(_task_keys)

    await send_reply(websocket, message_type, target_id,
        f"✅ 批量人设更新完成！\n"
        f"  ├─ 总数: {total}\n"
        f"  ├─ 成功: {updated}\n"
        f"  └─ 失败: {failed}", user_id, reply_id)

    logger.info(f"✅ 批量人设更新完成: {updated}/{total} 成功, {failed} 失败")


async def handle_update_all_profiles(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int]
) -> None:
    """
    /更新全部画像 指令：为群内所有用户批量更新画像。
    优先级：未建立画像的用户 > 已建立画像的用户（增量更新）
    """
    from .database import CONFIG

    group_id = target_id if target_id > 0 else 0
    if not group_id:
        await send_reply(websocket, message_type, target_id,
            "📋 /更新全部画像 需要在群聊中使用", user_id, reply_id)
        return

    with get_db() as conn:
        all_users = conn.execute(
            "SELECT DISTINCT user_id, nickname FROM message_archive WHERE target_id = ? ORDER BY created_at DESC",
            (group_id,)
        ).fetchall()

    if not all_users:
        await send_reply(websocket, message_type, target_id,
            "📋 群内暂无聊天记录，无法更新画像", user_id, reply_id)
        return

    bot_qq = int(get_bot_uin() or 0)  # 08-22：从连接派生
    all_users = [dict(r) for r in all_users if dict(r)["user_id"] != bot_qq]

    with get_persona_db() as conn:
        profiled = conn.execute(
            "SELECT user_id FROM user_profiles WHERE group_id = ?",
            (group_id,)
        ).fetchall()
        profiled_ids = {dict(r)["user_id"] for r in profiled}

    no_profile = [u for u in all_users if u["user_id"] not in profiled_ids]
    has_profile = [u for u in all_users if u["user_id"] in profiled_ids]

    queue = no_profile + has_profile
    total = len(queue)

    await send_reply(websocket, message_type, target_id,
        f"📋 开始批量更新画像，共 {total} 人\n"
        f"  ├─ 未建立画像: {len(no_profile)} 人（优先）\n"
        f"  └─ 已建立画像: {len(has_profile)} 人（增量更新）", user_id, reply_id)

    updated = 0
    failed = 0

    async def update_one(u: dict) -> tuple[str, bool]:
        uid = u["user_id"]
        nickname = u["nickname"]
        prefix = "🔴 未建画像" if uid not in profiled_ids else "🟢 增量更新"
        logger.info(f"{prefix}: {nickname}({uid})")
        try:
            await asyncio.wait_for(
                _do_update_profile(websocket, message_type, target_id, user_id, None, uid, nickname, priority=1),
                timeout=86400
            )
            return (f"{prefix}: {nickname}({uid})", True)
        except asyncio.TimeoutError:
            logger.error(f"⏱️ 更新画像超时: {nickname}({uid})")
            return (f"⏱️ 超时: {nickname}({uid})", False)
        except Exception as e:
            logger.error(f"❌ 更新画像失败 {nickname}({uid}): {e}")
            return (f"❌ 失败: {nickname}({uid}) - {str(e)[:50]}", False)

    results = []
    _batch_id = _task_batch_id("bprofiles")
    _task_keys = _TASK_REGISTRY.begin_batch(
        _batch_id, "人设画像更新", [_task_label_uid(u, "画像更新 ") for u in queue])
    try:
        for i, u in enumerate(queue):
            # 2026-08-22 暂停门：queued→running 转换前等放行
            await _TASK_REGISTRY.wait_if_paused()
            _TASK_REGISTRY.set_status(_task_keys[i], "running")
            try:
                result = await update_one(u)
            finally:
                _TASK_REGISTRY.finish(_task_keys[i])
            results.append(result)
            if result[1]:
                updated += 1
            else:
                failed += 1

            if (i + 1) % 3 == 0 or i == total - 1:
                progress = i + 1
                await send_reply(websocket, message_type, target_id,
                    f"📋 进度更新: {progress}/{total} 已完成\n"
                    f"  ├─ 成功: {updated}\n"
                    f"  └─ 失败: {failed}", user_id, reply_id)
    finally:
        _TASK_REGISTRY.finish_batch(_task_keys)

    await send_reply(websocket, message_type, target_id,
        f"✅ 批量画像更新完成！\n"
        f"  ├─ 总数: {total}\n"
        f"  ├─ 成功: {updated}\n"
        f"  └─ 失败: {failed}", user_id, reply_id)

    logger.info(f"✅ 批量画像更新完成: {updated}/{total} 成功, {failed} 失败")


async def handle_update_all_profiles_and_personas(
    websocket, message_type: str, target_id: int, user_id: int, reply_id: Optional[int]
) -> None:
    """
    /更新全部画像和人设 指令：为群内所有用户批量联合更新画像+人设（Combined Map 方案）。
    每个用户只调一次 LLM，同时产出人设 JSON 片段和画像素材。
    优先级：未建立画像/人设的用户 > 已建立的用户（增量更新）
    """
    from .database import CONFIG

    group_id = target_id if target_id > 0 else 0
    if not group_id:
        await send_reply(websocket, message_type, target_id,
            "📋 /更新全部画像和人设 需要在群聊中使用", user_id, reply_id)
        return

    with get_db() as conn:
        all_users = conn.execute(
            "SELECT DISTINCT user_id, nickname FROM message_archive WHERE target_id = ? ORDER BY created_at DESC",
            (group_id,)
        ).fetchall()

    if not all_users:
        await send_reply(websocket, message_type, target_id,
            "📋 群内暂无聊天记录，无法更新画像和人设", user_id, reply_id)
        return

    bot_qq = int(get_bot_uin() or 0)  # 08-22：从连接派生
    all_users = [dict(r) for r in all_users if dict(r)["user_id"] != bot_qq]

    # 去重
    seen = {}
    for u in all_users:
        uid = u["user_id"]
        if uid not in seen:
            seen[uid] = u
    all_users = list(seen.values())

    # 查询已有画像和人设的用户
    with get_persona_db() as conn:
        profiled = conn.execute(
            "SELECT user_id FROM user_profiles WHERE group_id = ?",
            (group_id,)
        ).fetchall()
        profiled_ids = {dict(r)["user_id"] for r in profiled}

        personaed = conn.execute(
            "SELECT user_id FROM user_personas WHERE group_id = ?",
            (group_id,)
        ).fetchall()
        personaed_ids = {dict(r)["user_id"] for r in personaed}

    # 优先级：两者都没有 > 只有画像 > 只有人设 > 两者都有
    def _priority_score(u: dict) -> int:
        has_prof = u["user_id"] in profiled_ids
        has_pers = u["user_id"] in personaed_ids
        if not has_prof and not has_pers:
            return 0  # 最高优先级
        elif not has_prof:
            return 1
        elif not has_pers:
            return 2
        else:
            return 3  # 最低优先级

    all_users.sort(key=_priority_score)

    total = len(all_users)

    await send_reply(websocket, message_type, target_id,
        f"📋 开始批量联合更新画像+人设，共 {total} 人\n"
        f"  ├─ 无画像无人设: {sum(1 for u in all_users if _priority_score(u) == 0)} 人（优先）\n"
        f"  ├─ 仅无人设: {sum(1 for u in all_users if _priority_score(u) == 1)} 人\n"
        f"  ├─ 仅无画像: {sum(1 for u in all_users if _priority_score(u) == 2)} 人\n"
        f"  └─ 两者均有: {sum(1 for u in all_users if _priority_score(u) == 3)} 人（增量更新）", user_id, reply_id)

    updated = 0
    failed = 0

    async def update_one(u: dict) -> tuple[str, bool]:
        uid = u["user_id"]
        nickname = u["nickname"]
        try:
            await asyncio.wait_for(
                _do_update_profile_and_persona(websocket, message_type, target_id, user_id, None, uid, nickname, priority=1),
                timeout=86400
            )
            return (f"✅ {nickname}({uid})", True)
        except asyncio.TimeoutError:
            logger.error(f"⏱️ 联合更新超时: {nickname}({uid})")
            return (f"⏱️ 超时: {nickname}({uid})", False)
        except Exception as e:
            logger.error(f"❌ 联合更新失败 {nickname}({uid}): {e}")
            return (f"❌ 失败: {nickname}({uid}) - {str(e)[:50]}", False)

    results = []
    _batch_id = _task_batch_id("bcombined")
    _task_keys = _TASK_REGISTRY.begin_batch(
        _batch_id, "人设画像更新", [_task_label_uid(u) for u in all_users])
    try:
        for i, u in enumerate(all_users):
            # 2026-08-22 暂停门：queued→running 转换前等放行
            await _TASK_REGISTRY.wait_if_paused()
            _TASK_REGISTRY.set_status(_task_keys[i], "running")
            try:
                result = await update_one(u)
            finally:
                _TASK_REGISTRY.finish(_task_keys[i])
            results.append(result)
            if result[1]:
                updated += 1
            else:
                failed += 1

            if (i + 1) % 3 == 0 or i == total - 1:
                progress = i + 1
                await send_reply(websocket, message_type, target_id,
                    f"📋 进度更新: {progress}/{total} 已完成\n"
                    f"  ├─ 成功: {updated}\n"
                    f"  └─ 失败: {failed}", user_id, reply_id)
    finally:
        _TASK_REGISTRY.finish_batch(_task_keys)

    await send_reply(websocket, message_type, target_id,
        f"✅ 批量联合更新完成！\n"
        f"  ├─ 总数: {total}\n"
        f"  ├─ 成功: {updated}\n"
        f"  └─ 失败: {failed}", user_id, reply_id)

    logger.info(f"✅ 批量联合更新(画像+人设)完成: {updated}/{total} 成功, {failed} 失败")


# ============================================================
#  人设 JSON 解析/合并/序列化
# ============================================================

PERSONA_FIELD_LABELS = {
    "identity": "身份信息",
    "interests": "兴趣爱好",
    "personality": "性格特征",
    "relationships": "与群友关系",
    "weaknesses_taboos": "弱点与禁忌",
    "group_role": "群内角色",
    "catchphrases": "口头禅",
    "sexual_experience": "性经历",
    "sexual_preferences": "性癖好",
    "mood": "当前情绪",
    "situation": "当前情境",
}

PERSONA_SUB_LABELS = {
    "gender": "性别",
    "age_range": "年龄段",
    "body_features": "身体特征",
    "location": "城市",
    "school_work": "学校/工作",
    "experience": "性经历",
    "body": "性器官特征",
}


def _get_persona_diff(old: dict, new: dict, _path: str = "") -> str:
    """对比新旧人设，返回变更字段的差异文本。"""
    changes = []
    all_keys = set(list(old.keys()) + list(new.keys()))

    for key in all_keys:
        old_val = old.get(key)
        new_val = new.get(key)

        if old_val == new_val:
            continue

        if isinstance(old_val, dict) and isinstance(new_val, dict):
            parent_label = PERSONA_FIELD_LABELS.get(key, key)
            sub = _get_persona_diff(old_val, new_val, _path + parent_label + " > ")
            changes.append(sub)
            continue

        sub_label = PERSONA_SUB_LABELS.get(key, key)
        top_label = PERSONA_FIELD_LABELS.get(key, key)
        label = f"{_path}{sub_label}" if _path else f"{top_label}"

        if isinstance(old_val, list) and isinstance(new_val, list):
            added = [item for item in new_val if item not in old_val]
            removed = [item for item in old_val if item not in new_val]
            parts = []
            if added:
                parts.append(f"+ {', '.join(str(a) for a in added)}")
            if removed:
                parts.append(f"- {', '.join(str(r) for r in removed)}")
            if parts:
                changes.append(f"• {label}：{'；'.join(parts)}")
            continue

        if old_val is None or (old_val == "" or old_val == [] or old_val == {}):
            changes.append(f"• {label}：{new_val}")
        elif new_val is None or (new_val == "" or new_val == [] or new_val == {}):
            changes.append(f"• {label}：已清除（原值：{old_val}）")
        else:
            changes.append(f"• {label}：\n  旧：{old_val}\n  新：{new_val}")

    if not changes:
        if not _path:
            return "（本次无人设字段发生变更）"
        return ""

    return "\n".join(changes)


def _parse_persona_json(text: str) -> dict:
    """从 LLM 返回内容或 DB 中解析人设 JSON"""
    if not text or text.strip() == '{}':
        return {}
    text = text.strip()
    if "```" in text:
        code_match = _re.search(r'```(?:json)?\s*(.*?)\s*```', text, _re.DOTALL)
        if code_match:
            text = code_match.group(1).strip()
        else:
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("```"):
                    continue
                text = line
                break

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        # 容错（2026-08-09）：LLM 偶发"顶层对象提前闭合"（如 interests 后误写 } 再继续写字段）
        # → raw_decode 取第一个完整对象，剩余 ", field: ...}" 包成对象合并恢复
        try:
            decoder = json.JSONDecoder()
            obj, idx = decoder.raw_decode(text[start:end + 1])
            if isinstance(obj, dict):
                rest = text[start + idx:end + 1].strip()
                if rest.startswith(","):
                    try:
                        inner = rest[1:].strip().rstrip(",")
                        if inner.endswith("}"):
                            inner = inner[:-1]  # 去掉顶层提前闭合的 }（只去一个，保留嵌套对象的 }）
                        merged = json.loads("{" + inner + "}")
                        if isinstance(merged, dict):
                            obj.update(merged)
                            return obj
                    except json.JSONDecodeError:
                        pass
                return obj
        except json.JSONDecodeError:
            pass

    try:
        parsed = json.loads(text, strict=False)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    return {}


def _merge_persona_json(old: dict, new: dict) -> dict:
    """合并新旧人设 JSON，新数据优先，但保留旧数据中未提及的字段"""
    if not new:
        return old
    if not old:
        return new

    merged = dict(old)

    old_id = old.get("identity", {}) or {}
    new_id = new.get("identity", {}) or {}
    if new_id:
        merged_id = dict(old_id)
        for k, v in new_id.items():
            if v or v == "":
                merged_id[k] = v
        merged["identity"] = merged_id

    old_int = old.get("interests", []) or []
    new_int = new.get("interests", []) or []
    if new_int:
        merged_int = list(old_int)
        for item in new_int:
            if item and item not in merged_int:
                merged_int.append(item)
        merged["interests"] = merged_int
    elif old_int:
        merged["interests"] = old_int

    if new.get("personality"):
        merged["personality"] = new["personality"]
    elif old.get("personality"):
        merged["personality"] = old["personality"]

    old_rel = old.get("relationships", {}) or {}
    new_rel = new.get("relationships", {}) or {}
    if new_rel or old_rel:
        merged_rel = dict(old_rel)
        merged_rel.update(new_rel)
        merged["relationships"] = merged_rel

    old_se = old.get("sexual_experience", {}) or {}
    new_se = new.get("sexual_experience", {}) or {}
    if new_se:
        merged_se = dict(old_se)
        for k, v in new_se.items():
            if v or v == "":
                merged_se[k] = v
        merged["sexual_experience"] = merged_se
    elif old_se:
        merged["sexual_experience"] = old_se

    old_sp = old.get("sexual_preferences", []) or []
    new_sp = new.get("sexual_preferences", []) or []
    if new_sp:
        merged_sp = list(old_sp)
        for item in new_sp:
            if item and item not in merged_sp:
                merged_sp.append(item)
        merged["sexual_preferences"] = merged_sp
    elif old_sp:
        merged["sexual_preferences"] = old_sp

    old_wt = old.get("weaknesses_taboos", []) or []
    new_wt = new.get("weaknesses_taboos", []) or []
    if new_wt:
        merged_wt = list(old_wt)
        for item in new_wt:
            if item and item not in merged_wt:
                merged_wt.append(item)
        merged["weaknesses_taboos"] = merged_wt
    elif old_wt:
        merged["weaknesses_taboos"] = old_wt

    if new.get("group_role"):
        merged["group_role"] = new["group_role"]
    elif old.get("group_role"):
        merged["group_role"] = old["group_role"]

    old_cp = old.get("catchphrases", []) or []
    new_cp = new.get("catchphrases", []) or []
    if new_cp:
        merged_cp = list(old_cp)
        for item in new_cp:
            if item and item not in merged_cp:
                merged_cp.append(item)
        merged["catchphrases"] = merged_cp
    elif old_cp:
        merged["catchphrases"] = old_cp

    # ── 合并后强制限制各字段数量/字数上限 ──
    # 数组字段硬限制
    _PERSONA_ARRAY_LIMITS = {
        "interests": 6,
        "weaknesses_taboos": 6,
        "catchphrases": 6,
        "sexual_preferences": 8,
    }
    for field, limit in _PERSONA_ARRAY_LIMITS.items():
        arr = merged.get(field)
        if isinstance(arr, list) and len(arr) > limit:
            # 保留末尾（最新）的项，丢弃最旧的部分
            merged[field] = arr[-limit:]

    # relationships 限制最多 6 个键值对
    return merged


# ============================================================
#  人设/画像 CRUD 函数
# ============================================================

def save_personality(user_id: int, personality: str):
    """保存用户性格"""
    with get_persona_db() as conn:
        conn.execute(
            "INSERT INTO bot_personalities (user_id, personality, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "personality = excluded.personality, updated_at = excluded.updated_at",
            (user_id, personality, time.time()),
        )
    logger.info(f"🎭 用户 {user_id} 设置性格: {personality[:50]}...")


def get_personality(user_id: int) -> str:
    """获取用户性格"""
    with get_persona_db() as conn:
        row = conn.execute(
            "SELECT personality FROM bot_personalities WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["personality"] if row else ""


def get_user_profile(user_id: int, group_id: int = 0) -> Optional[dict]:
    """获取用户画像。
    集群场景：路由到主群查找（同一用户共享画像）。
    非集群场景：只在当前群查找。
    最高权限群（is_privileged_group=1）：本群优先 → 回退到其他所有群（2026-08-12）。
    返回结果增加 _source_group 字段标注数据来源（供 /用户画像 指令显示）
    """
    # 回退链：本群 → 主群（集群）或 → 其他所有群（特权群）
    for target_group in _persona_fallback_groups(group_id):
        with get_persona_db() as conn:
            row = conn.execute(
                "SELECT user_id, group_id, nickname, profile, last_updated_at, last_message_id, COALESCE(last_scan_at, 0) as last_scan_at FROM user_profiles WHERE user_id = ? AND group_id = ?",
                (user_id, target_group),
            ).fetchone()
        if row:
            result = dict(row)
            result["_source_group"] = result["group_id"]
            if result["group_id"] != group_id:
                if _is_privileged_group(group_id):
                    result["_note"] = f"（来自群{result['group_id']}，本群无该用户数据）"
                else:
                    result["_note"] = f"（来自主群{result['group_id']}，与同集群群共享）"
            return result
    return None


def save_user_profile(user_id: int, nickname: str, profile: str, last_message_id: int = 0, group_id: int = 0, last_scan_at: float = 0):
    """保存用户画像（UPSERT），按 (user_id, group_id) 复合键。
    集群场景：统一存到主群，同一用户在各群共享画像。
    """
    target_group = _resolve_persona_group(group_id)
    with get_persona_db() as conn:
        conn.execute(
            "INSERT INTO user_profiles (user_id, group_id, nickname, profile, last_updated_at, last_message_id, last_scan_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "nickname = excluded.nickname, profile = excluded.profile, "
            "last_updated_at = excluded.last_updated_at, last_message_id = excluded.last_message_id, last_scan_at = excluded.last_scan_at",
            (user_id, target_group, nickname, profile, time.time(), last_message_id, last_scan_at),
        )
    group_label = f"群{target_group}" if target_group else "全局"
    if target_group != group_id:
        group_label += f" (主群，原群{group_id})"
    logger.info(f"👤 用户 {user_id}({nickname}) 画像已更新 [{group_label}] ({len(profile)} 字)")


def get_group_personas(group_id: int) -> tuple[list[dict], bool]:
    """
    获取指定群内所有有正式人设的用户。

    说明：只在当前群（或主群，集群场景由调用方传入主群号）查找，
    不再回退到其他群。

    返回 (results, fallback_used)
      results: [{"user_id": int, "nickname": str, "persona": dict, "source_group": int}, ...]
        source_group 表示该人设数据来源于哪个群（方便用户理解）
      fallback_used: 恒为 False（保留签名兼容）
    """
    results = []
    # 集群场景：路由到主群查询（同集群所有群共享人设数据，2026-08-07 修复）
    target_group = _resolve_persona_group(group_id)
    with get_persona_db() as conn:
        rows = conn.execute(
            "SELECT user_id, nickname, persona FROM user_personas "
            "WHERE group_id = ? AND persona != '{}' AND persona IS NOT NULL",
            (target_group,),
        ).fetchall()
        for row in rows:
            persona = _parse_persona_json(row["persona"])
            if persona:
                results.append({
                    "user_id": row["user_id"],
                    "nickname": row["nickname"] or f"用户{row['user_id']}",
                    "persona": persona,
                    "source_group": target_group,
                })
    
    # 本群有人设数据，直接返回
    if results:
        return results, False

    # 本群无人设，直接返回空（不再回退到其他群）
    return [], False


def get_group_personas_with_profiles(group_id: int) -> tuple[list[dict], bool]:
    """
    获取指定群内所有有正式人设 **或** 用户画像的用户（人设 + 画像合并输入）。

    /群像 使用：同时用人设和画像作为 LLM 输入，覆盖面更全——
    有人设无人像、有人像无人设、两者都有，均纳入。

    返回 (results, fallback_used)
      results: [{
          "user_id": int, "nickname": str,
          "persona": dict | None,   # 有人设时为解析后的 dict，否则 None
          "profile": str | None,    # 有画像时为画像文本（第一人称），否则 None
          "source_group": int,
      }, ...]
      fallback_used: 恒为 False（保留签名兼容）
    """
    # 集群场景：路由到主群查询（同集群所有群共享人设/画像数据，2026-08-07 修复）
    target_group = _resolve_persona_group(group_id)
    with get_persona_db() as conn:
        p_rows = conn.execute(
            "SELECT user_id, nickname, persona FROM user_personas "
            "WHERE group_id = ? AND persona != '{}' AND persona IS NOT NULL",
            (target_group,),
        ).fetchall()
        pf_rows = conn.execute(
            "SELECT user_id, nickname, profile FROM user_profiles "
            "WHERE group_id = ? AND profile IS NOT NULL AND profile != ''",
            (target_group,),
        ).fetchall()

    by_uid: dict[int, dict] = {}
    for row in p_rows:
        persona = _parse_persona_json(row["persona"])
        if persona:
            by_uid[row["user_id"]] = {
                "user_id": row["user_id"],
                "nickname": row["nickname"] or f"用户{row['user_id']}",
                "persona": persona,
                "profile": None,
                "source_group": target_group,
            }
    for row in pf_rows:
        uid = row["user_id"]
        if uid in by_uid:
            by_uid[uid]["profile"] = row["profile"]
        else:
            by_uid[uid] = {
                "user_id": uid,
                "nickname": row["nickname"] or f"用户{uid}",
                "persona": None,
                "profile": row["profile"],
                "source_group": target_group,
            }

    return list(by_uid.values()), False


def get_all_profiles(group_id: int = 0) -> list[dict]:
    """获取用户画像列表。group_id>0 时只返回该群的画像"""
    with get_persona_db() as conn:
        if group_id:
            rows = conn.execute(
                "SELECT user_id, group_id, nickname FROM user_profiles WHERE group_id = ? ORDER BY last_updated_at DESC",
                (group_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id, group_id, nickname FROM user_profiles ORDER BY last_updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def find_user_by_nickname(nickname: str, group_id: int = 0) -> Optional[dict]:
    """根据昵称模糊搜索用户画像，返回最匹配的一条。只在当前群（或主群）查找。
    2026-08-13：支持 QQ 号（纯数字 → user_id 精确匹配）+ 特权群回退链。"""
    for target_group in _persona_fallback_groups(group_id):
        with get_persona_db() as conn:
            if nickname.isdigit():
                row = conn.execute(
                    "SELECT user_id, group_id, nickname FROM user_profiles "
                    "WHERE user_id = ? AND group_id = ? LIMIT 1",
                    (int(nickname), target_group),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT user_id, group_id, nickname FROM user_profiles "
                    "WHERE nickname LIKE ? AND group_id = ? ORDER BY last_updated_at DESC LIMIT 1",
                    (f"%{nickname}%", target_group),
                ).fetchone()
        if row:
            return dict(row)
    return None


def find_persona_by_nickname(nickname: str, group_id: int = 0) -> Optional[dict]:
    """
    根据昵称模糊搜索用户人设，返回最匹配的一条。
    只在当前群（或主群）查找，不再回退到其他群。
    最高权限群（is_privileged_group=1）：本群优先 → 回退到其他所有群（2026-08-12）。
    2026-08-13：支持 QQ 号（纯数字 → user_id 精确匹配）。
    返回格式: {user_id, group_id, nickname, persona, temporary_persona, _source_group}
    """
    for target_group in _persona_fallback_groups(group_id):
        with get_persona_db() as conn:
            if nickname.isdigit():
                row = conn.execute(
                    "SELECT user_id, group_id, nickname, persona, temporary_persona "
                    "FROM user_personas WHERE user_id = ? AND group_id = ? AND persona != '{}' "
                    "LIMIT 1",
                    (int(nickname), target_group),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT user_id, group_id, nickname, persona, temporary_persona "
                    "FROM user_personas WHERE nickname LIKE ? AND group_id = ? AND persona != '{}' "
                    "ORDER BY last_updated_at DESC LIMIT 1",
                    (f"%{nickname}%", target_group),
                ).fetchone()
        if row:
            result = dict(row)
            result["_source_group"] = result["group_id"]
            return result
    return None


def find_profile_by_nickname(nickname: str, group_id: int = 0) -> Optional[dict]:
    """
    根据昵称模糊搜索用户画像，返回完整画像数据。
    只在当前群（或主群）查找，不再回退到其他群。
    最高权限群（is_privileged_group=1）：本群优先 → 回退到其他所有群（2026-08-12）。
    2026-08-13：支持 QQ 号（纯数字 → user_id 精确匹配）。
    返回格式: {user_id, group_id, nickname, profile, last_updated_at, _source_group}
    """
    for target_group in _persona_fallback_groups(group_id):
        with get_persona_db() as conn:
            if nickname.isdigit():
                row = conn.execute(
                    "SELECT user_id, group_id, nickname, profile, last_updated_at "
                    "FROM user_profiles WHERE user_id = ? AND group_id = ? "
                    "LIMIT 1",
                    (int(nickname), target_group),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT user_id, group_id, nickname, profile, last_updated_at "
                    "FROM user_profiles WHERE nickname LIKE ? AND group_id = ? "
                    "ORDER BY last_updated_at DESC LIMIT 1",
                    (f"%{nickname}%", target_group),
                ).fetchone()
        if row:
            result = dict(row)
            result["_source_group"] = result["group_id"]
            return result
    return None


def get_same_persona_chat_context(user_id: int, group_id: int = 0) -> str:
    """
    获取与当前用户设置了相同机器人人设的其他用户的聊天记录。
    逻辑：
    - 如果当前用户设置了 /人设 xxx：找同群内也设置了相同 /人设 的其他用户
    - 如果当前用户没设置 /人设：找同群内所有其他用户（默认人设相同）
    用于让 AI 了解同一角色/人设下的跨用户互动，实现"共享记忆"和"跨用户提醒"。
    """
    three_days_ago = time.time() - 3 * 86400

    # 获取当前用户的机器人人设
    my_personality = get_personality(user_id)

    # 找到相关用户ID
    relevant_user_ids = []
    if my_personality and group_id:
        # 设置了 /人设 且在群聊中：找同群内相同人设的用户
        with get_persona_db() as pconn:
            same_persona_users = pconn.execute(
                "SELECT user_id FROM bot_personalities "
                "WHERE personality = ? AND user_id != ?",
                (my_personality, user_id),
            ).fetchall()
            if same_persona_users:
                candidate_ids = [r["user_id"] for r in same_persona_users]
                # 过滤：只保留同群内有聊天记录的用户
                with get_db() as conn:
                    for uid in candidate_ids:
                        row = conn.execute(
                            "SELECT 1 FROM chat_messages WHERE user_id = ? AND session_key LIKE ? AND created_at > ? LIMIT 1",
                            (uid, f"group_{group_id}%", three_days_ago),
                        ).fetchone()
                        if row:
                            relevant_user_ids.append(uid)
    elif group_id:
        # 没设置 /人设 或在群聊中：找同群内所有其他用户
        with get_db() as conn:
            other_users = conn.execute(
                "SELECT DISTINCT user_id FROM chat_messages "
                "WHERE session_key LIKE ? AND user_id != ? AND role = 'user' "
                "AND created_at > ?",
                (f"group_{group_id}%", user_id, three_days_ago),
            ).fetchall()
            relevant_user_ids = [r["user_id"] for r in other_users]

    if not relevant_user_ids:
        return ""

    # 获取这些用户的聊天记录（优先同群，否则全局）
    with get_db() as conn:
        messages = []
        for uid in relevant_user_ids:
            if group_id:
                # 优先同群的消息
                rows = conn.execute(
                    "SELECT content, nickname, created_at FROM chat_messages "
                    "WHERE user_id = ? AND role = 'user' AND session_key LIKE ? "
                    "AND created_at > ? ORDER BY created_at DESC LIMIT 3",
                    (uid, f"group_{group_id}%", three_days_ago),
                ).fetchall()
            else:
                rows = []
            if not rows:
                # 回退到全局消息（私聊等）
                rows = conn.execute(
                    "SELECT content, nickname, created_at FROM chat_messages "
                    "WHERE user_id = ? AND role = 'user' AND created_at > ? "
                    "ORDER BY created_at DESC LIMIT 3",
                    (uid, three_days_ago),
                ).fetchall()

            for r in rows:
                from datetime import datetime
                ts = datetime.fromtimestamp(r["created_at"])
                time_str = ts.strftime("%m-%d %H:%M")
                messages.append(f"[{time_str}] {r['nickname']}: {r['content'][:200]}")

    if not messages:
        return ""

    return "\n".join(messages[:10])


def get_active_persona(user_id: int, group_id: int = 0) -> dict:
    """获取用户当前生效的人设（优先临时人设，其次正式人设）
    集群场景：路由到主群查找（同一用户共享人设）。
    非集群场景：只在当前群查找。
    最高权限群（is_privileged_group=1）：本群优先 → 回退到其他所有群（2026-08-12）。
    """
    # 回退链：本群 → 主群（集群）或 → 其他所有群（特权群）
    for target_group in _persona_fallback_groups(group_id):
        with get_persona_db() as conn:
            row = conn.execute(
                "SELECT persona, temporary_persona, nickname FROM user_personas WHERE user_id = ? AND group_id = ?",
                (user_id, target_group),
            ).fetchone()
        if not row:
            continue
        if row["temporary_persona"]:
            return _parse_persona_json(row["temporary_persona"])
        return _parse_persona_json(row["persona"])
    return {}


def get_persona_display(user_id: int, group_id: int = 0) -> Optional[dict]:
    """
    获取用户人设的完整信息（供 /用户人设 指令使用）
    集群场景：路由到主群查找（同一用户共享人设）。
    非集群场景：只在当前群查找。
    最高权限群（is_privileged_group=1）：本群优先 → 回退到其他所有群（2026-08-12）。
    返回结果增加 _source_group 字段标注数据来源
    """
    # 回退链：本群 → 主群（集群）或 → 其他所有群（特权群）
    for target_group in _persona_fallback_groups(group_id):
        with get_persona_db() as conn:
            row = conn.execute(
                "SELECT persona, temporary_persona, nickname, last_persona_message_id, last_persona_scan_at, group_id "
                "FROM user_personas WHERE user_id = ? AND group_id = ?",
                (user_id, target_group),
            ).fetchone()
        if not row or not row["persona"] or row["persona"] == '{}':
            continue
        persona = _parse_persona_json(row["persona"])
        temp_persona = _parse_persona_json(row["temporary_persona"]) if row["temporary_persona"] else None
        source_group = row["group_id"]
        result = {
            "persona": persona,
            "temp_persona": temp_persona,
            "nickname": row["nickname"],
            "last_persona_message_id": row["last_persona_message_id"] or 0,
            "last_persona_scan_at": row["last_persona_scan_at"] or 0,
            "_source_group": source_group,
        }
        if source_group != group_id:
            if _is_privileged_group(group_id):
                result["_note"] = f"（来自群{source_group}，本群无该用户数据）"
            else:
                result["_note"] = f"（来自主群{source_group}，与同集群群共享）"
        return result
    return None


def save_persona(user_id: int, persona: dict, nickname: str = "", group_id: int = 0,
                 last_persona_message_id: int = 0, last_persona_scan_at: float = 0):
    """保存/更新用户正式人设（JSON 格式），同时更新独立断点。
    集群场景：统一存到主群，同一用户在各群共享人设。
    """
    # 结构归一化：全链路兜底（任何路径输出的人设在保存前保证结构完整）（2026-08-09）
    persona = _normalize_persona_structure(persona)
    persona_str = json.dumps(persona, ensure_ascii=False)
    target_group = _resolve_persona_group(group_id)
    with get_persona_db() as conn:
        conn.execute(
            "INSERT INTO user_personas (user_id, group_id, nickname, persona, last_updated_at, "
            "last_persona_message_id, last_persona_scan_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "nickname = excluded.nickname, persona = excluded.persona, "
            "last_updated_at = excluded.last_updated_at, "
            "last_persona_message_id = excluded.last_persona_message_id, "
            "last_persona_scan_at = excluded.last_persona_scan_at",
            (user_id, target_group, nickname, persona_str, time.time(),
             last_persona_message_id, last_persona_scan_at),
        )
    logger.info(f"👤 用户 {user_id}({nickname}) 正式人设已更新，断点: message_id={last_persona_message_id}")


def set_temporary_persona(user_id: int, temp_persona: dict, nickname: str = "", group_id: int = 0):
    """设置用户临时人设。集群场景：路由到主群。"""
    persona_str = json.dumps(temp_persona, ensure_ascii=False)
    target_group = _resolve_persona_group(group_id)
    with get_persona_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM user_personas WHERE user_id = ? AND group_id = ?",
            (user_id, target_group),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO user_personas (user_id, group_id, nickname, persona, last_updated_at) "
                "VALUES (?, ?, ?, '{}', ?)",
                (user_id, target_group, nickname, time.time()),
            )
        conn.execute(
            "UPDATE user_personas SET temporary_persona = ?, last_updated_at = ? "
            "WHERE user_id = ? AND group_id = ?",
            (persona_str, time.time(), user_id, target_group),
        )
    logger.info(f"🎭 用户 {user_id}({nickname}) 临时人设已设置")


def reset_temporary_persona(user_id: int, group_id: int = 0) -> bool:
    """清除临时人设，恢复正式人设。集群场景：路由到主群。"""
    target_group = _resolve_persona_group(group_id)
    with get_persona_db() as conn:
        row = conn.execute(
            "SELECT temporary_persona FROM user_personas WHERE user_id = ? AND group_id = ?",
            (user_id, target_group),
        ).fetchone()
        if not row:
            return False
        if row["temporary_persona"]:
            conn.execute(
                "UPDATE user_personas SET temporary_persona = NULL WHERE user_id = ? AND group_id = ?",
                (user_id, target_group),
            )
            logger.info(f"🔄 用户 {user_id} 临时人设已清除，恢复正式人设")
            return True
    return False


PERSONA_SECTIONS = {
    "identity": "身份标签",
    "interests": "兴趣爱好",
    "personality": "性格特征",
    "relationships": "与群友关系",
    "weaknesses_taboos": "弱点与雷区",
    "group_role": "群内地位",
    "catchphrases": "口头禅/常用梗",
    "sexual_experience": "性经历与性器官特征",
    "sexual_preferences": "性癖好",
}


def persona_to_text(persona: dict) -> str:
    """将人设字典格式化为可读文本"""
    if not persona:
        return ""
    lines = []
    for section_key, section_label in PERSONA_SECTIONS.items():
        section = persona.get(section_key)
        if not section:
            continue
        if isinstance(section, dict):
            sub_labels_map = {
                "gender": "性别", "age_range": "年龄段", "body_features": "身体特征",
                "location": "城市坐标", "school_work": "学校/工作状态",
                "experience": "恋爱史/前任与性经历描述", "body": "性器官特征描述",
            }
            sub_items = []
            for key, value in section.items():
                if not value:
                    continue
                label = sub_labels_map.get(key, key)
                sub_items.append(f"  {label}: {value}")
            if not sub_items:
                continue
        elif isinstance(section, list):
            if not any(section):
                continue
        else:
            if not section:
                continue

        lines.append(f"【{section_label}】")
        if isinstance(section, dict):
            lines.extend(sub_items)
        elif isinstance(section, list):
            for item in section:
                lines.append(f"  • {item}")
        else:
            lines.append(f"  {section}")
        lines.append("")

    # 兼容旧格式
    for key in persona:
        if key not in PERSONA_SECTIONS:
            lines.append(f"  {key}: {persona[key]}")
    return "\n".join(lines).strip()


# ============================================================
#  后台任务队列
# ============================================================

_profile_update_lock = asyncio.Semaphore(1)  # 画像+人设共用锁，确保串行执行
_profile_task_queue = asyncio.Queue(maxsize=100)
_profile_worker_started = False
# 保留 worker 任务引用，防止 GC 回收
_profile_worker_task: Optional[asyncio.Task[None]] = None


async def _profile_worker():
    """画像更新后台 Worker：从队列中消费任务，逐个执行画像更新。"""
    while True:
        task = await _profile_task_queue.get()
        try:
            await task
        except Exception as e:
            logger.error(f"画像后台任务异常: {e}", exc_info=True)
        finally:
            _profile_task_queue.task_done()


def _ensure_profile_worker():
    """确保画像后台 Worker 已启动。崩溃后自动重置并重新启动。"""
    global _profile_worker_started, _profile_worker_task
    # 如果 worker 已启动但已 done/cancelled，重置标志位重新启动
    if _profile_worker_started and _profile_worker_task is not None:
        if _profile_worker_task.done() or _profile_worker_task.cancelled():
            logger.warning("画像后台 Worker 已停止，重新启动...")
            _profile_worker_started = False
            _profile_worker_task = None
    if not _profile_worker_started:
        _profile_worker_started = True
        _profile_worker_task = asyncio.create_task(_profile_worker())


async def _enqueue_profile_update(coro, task_key: "Optional[str]" = None):
    """将画像更新任务放入后台队列，立即返回。

    task_key（2026-08-22 任务列表）：提供时 worker 消费（开始执行）才转 running，
    执行完/异常即 finish——排队中面板显示 queued，执行中显示 running。
    """
    _ensure_profile_worker()
    if task_key is not None:
        async def _wrapped():
            # 2026-08-22 暂停门：worker 开始执行（queued→running）前等放行
            await _TASK_REGISTRY.wait_if_paused()
            _TASK_REGISTRY.set_status(task_key, "running")
            try:
                await coro
            finally:
                _TASK_REGISTRY.finish(task_key)
        coro = _wrapped()
    await _profile_task_queue.put(coro)


# ============================================================
#  人设数据库 Schema
# ============================================================
PERSONAS_SCHEMA = """
-- 用户为 Bot 设定的角色身份
CREATE TABLE IF NOT EXISTS bot_personalities (
    user_id INTEGER PRIMARY KEY,
    personality TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- 用户画像表（每个用户不超过500字的性格/行为画像，按群隔离）
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL DEFAULT 0,
    nickname TEXT DEFAULT '',
    profile TEXT NOT NULL,
    last_updated_at REAL NOT NULL,
    last_message_id INTEGER DEFAULT 0,
    last_scan_at REAL DEFAULT 0,
    PRIMARY KEY (user_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_user_profiles_nickname ON user_profiles(nickname);
CREATE INDEX IF NOT EXISTS idx_user_profiles_group ON user_profiles(group_id);

-- 用户人设表（客观信息存储，JSON 格式，按群隔离）
CREATE TABLE IF NOT EXISTS user_personas (
    user_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL DEFAULT 0,
    nickname TEXT DEFAULT '',
    persona TEXT NOT NULL DEFAULT '{}',
    temporary_persona TEXT DEFAULT NULL,
    last_updated_at REAL NOT NULL,
    last_persona_message_id INTEGER DEFAULT 0,
    last_persona_scan_at REAL DEFAULT 0,
    PRIMARY KEY (user_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_user_personas_group ON user_personas(group_id);
CREATE INDEX IF NOT EXISTS idx_user_personas_nickname ON user_personas(nickname);

-- 人设更新批次中间结果表
CREATE TABLE IF NOT EXISTS persona_batch_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    nickname TEXT DEFAULT '',
    group_id INTEGER NOT NULL DEFAULT 0,
    batch_index INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    batch_char_count INTEGER NOT NULL,
    analysis_result TEXT NOT NULL,
    is_valid INTEGER NOT NULL DEFAULT 1,
    is_incremental INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    batch_text TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_persona_batch_user ON persona_batch_results(user_id, group_id, created_at);

-- 画像更新批次中间结果表
CREATE TABLE IF NOT EXISTS profile_batch_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    nickname TEXT DEFAULT '',
    group_id INTEGER NOT NULL DEFAULT 0,
    batch_index INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    batch_char_count INTEGER NOT NULL,
    analysis_result TEXT NOT NULL,
    is_valid INTEGER NOT NULL DEFAULT 1,
    is_incremental INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    batch_text TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_profile_batch_user ON profile_batch_results(user_id, group_id, created_at);

-- 联合更新（Combined Map）批次中间结果表
CREATE TABLE IF NOT EXISTS combined_batch_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL DEFAULT 0,
    nickname TEXT DEFAULT '',
    batch_index INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    batch_char_count INTEGER NOT NULL,
    raw_response TEXT NOT NULL,
    parsed_json TEXT DEFAULT '',
    persona_json TEXT DEFAULT '',
    profile_material_json TEXT DEFAULT '',
    is_valid INTEGER NOT NULL DEFAULT 1,
    is_incremental INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    batch_text TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_combined_batch_user ON combined_batch_results(user_id, group_id, created_at);
"""
def _persona_array_limits() -> dict:
    """人设数组字段项数限制（配置化；_check_persona_limits/_persona_issues 共用）。"""
    l = _persona_limits()
    return {
        "interests": int(l["interests"]),
        "weaknesses_taboos": int(l["weaknesses_taboos"]),
        "catchphrases": int(l["catchphrases"]),
        "relationships": int(l["relationships"]),
        "sexual_preferences": int(l["sexual_preferences"]),
    }


def _persona_str_limits() -> dict:
    """人设字符串字段字数限制（配置化）。"""
    l = _persona_limits()
    return {
        "personality": int(l["personality"]),
        "group_role": int(l["group_role"]),
        "sexual_sub": int(l["sexual_sub"]),
        "identity_sub": int(l["identity_sub"]),
    }


def _check_persona_limits(persona: dict, nickname: str) -> None:
    """检查人设字段是否超出限制（限制值配置化），超出则打告警日志但不截断。"""
    lim = _persona_array_limits()
    for field, limit in lim.items():
        arr = persona.get(field)
        if isinstance(arr, list) and len(arr) > limit:
            logger.warning(
                f"⚠️ 人设字段超标 [{nickname}] {field}: {len(arr)} 项（限制 {limit}）→ {arr}")

    sl = _persona_str_limits()
    for field, char_limit in (("personality", sl["personality"]), ("group_role", sl["group_role"])):
        val = persona.get(field)
        if isinstance(val, str) and len(val) > char_limit:
            logger.warning(
                f"⚠️ 人设字段超标 [{nickname}] {field}: {len(val)} 字（限制 {char_limit}）")

    se = persona.get("sexual_experience")
    if isinstance(se, dict):
        for sub_key in ("experience", "body"):
            val = se.get(sub_key)
            if isinstance(val, str) and len(val) > sl["sexual_sub"]:
                logger.warning(f"⚠️ 人设字段超标 [{nickname}] sexual_experience.{sub_key}: "
                             f"{len(val)} 字（限制 {sl['sexual_sub']}）")

_PERSONA_DEFAULT_STRUCTURE = {
    "identity": {"gender": "", "age_range": "", "body_features": "", "location": "", "school_work": ""},
    "interests": [],
    "personality": "",
    "group_role": "",
    "relationships": {},
    "weaknesses_taboos": [],
    "catchphrases": [],
    "sexual_experience": {"experience": "", "body": ""},
    "sexual_preferences": [],
}


def _normalize_persona_structure(persona: dict) -> dict:
    """人设结构归一化（2026-08-09 三层方案第一层：程序兜底）。

    LLM 压缩/合并在减字数压力下会删空字段（body: "" → 删键、数组空 → 删键），
    导致结构漂移。本函数只补结构、不删内容：
    - 缺失键 → 补默认值（"" / [] / {}）
    - 缺失子键 → 补默认（identity 5 子键、sexual_experience 2 子键）
    - 类型错误 → 保守修正：
      * sexual_experience 为字符串 → {"experience": 原内容, "body": ""}（字符串几乎必然是经历描述）
      * 数组字段为字符串 → 包装成单元素数组（内容保留）
      * 其他类型错乱（list/dict 字段收到异常类型）→ 置默认（罕见，内容无意义）
    - 额外键（mood/situation 等临时字段）→ 保留不删
    - 已有内容留在原键不动（不搬移、不猜测归属）
    """
    out = dict(persona)
    for key, default in _PERSONA_DEFAULT_STRUCTURE.items():
        if key not in out:
            out[key] = default
            continue
        val = out[key]
        if isinstance(default, dict):
            if isinstance(val, str) and key == "sexual_experience":
                val = {"experience": val, "body": ""}
            elif not isinstance(val, dict):
                val = {}
            merged = dict(default)
            merged.update({k: v for k, v in val.items() if v is not None})
            out[key] = merged
        elif isinstance(default, list):
            if isinstance(val, str):
                out[key] = [val]  # 字符串包装为单元素数组（内容保留）
            elif not isinstance(val, list):
                out[key] = []
        elif isinstance(default, str):
            if not isinstance(val, str):
                out[key] = "" if val is None else json.dumps(val, ensure_ascii=False)
    return out


def _persona_issues(persona: dict) -> list[str]:
    """检查人设各字段是否超限（数组个数/字符串字数/总量，限制值配置化），返回问题描述列表，空 = 全部达标。
    2026-08-08：替代硬截断的前置检查——超限才触发 LLM 压缩（尽量不丢信息）。"""
    issues: list[str] = []
    lim = _persona_array_limits()
    for field, limit in lim.items():
        arr = persona.get(field)
        if isinstance(arr, list) and len(arr) > limit:
            issues.append(f"{field}: {len(arr)} 个（限制 {limit} 个）")
    sl = _persona_str_limits()
    for field, char_limit in (("personality", sl["personality"]), ("group_role", sl["group_role"])):
        val = persona.get(field)
        if isinstance(val, str) and len(val) > char_limit:
            issues.append(f"{field}: {len(val)} 字（限制 {char_limit} 字）")
    se = persona.get("sexual_experience")
    if isinstance(se, dict):
        for sub_key in ("experience", "body"):
            val = se.get(sub_key)
            if isinstance(val, str) and len(val) > sl["sexual_sub"]:
                issues.append(f"sexual_experience.{sub_key}: {len(val)} 字（限制 {sl['sexual_sub']} 字）")
    idt = persona.get("identity")
    if isinstance(idt, dict):
        for sub_key in ("gender", "age_range", "body_features", "location", "school_work"):
            val = idt.get(sub_key)
            if isinstance(val, str) and len(val) > sl["identity_sub"]:
                issues.append(f"identity.{sub_key}: {len(val)} 字（限制 {sl['identity_sub']} 字）")
    total = len(json.dumps(persona, ensure_ascii=False))
    p_lim = _persona_limits()
    if total > int(p_lim["total_hard_max"]):
        issues.append(f"JSON 总量: {total} 字（目标 {int(p_lim['total_min'])}-{int(p_lim['total_max'])} 字）")
    # 结构完整性检查（2026-08-09 三层方案第三层：发现结构问题——缺键/类型错）
    for key, default in _PERSONA_DEFAULT_STRUCTURE.items():
        if key not in persona:
            issues.append(f"结构缺失: 缺 {key} 键")
            continue
        val = persona[key]
        if isinstance(default, dict):
            if key == "sexual_experience" and isinstance(val, str):
                issues.append(f"结构错误: sexual_experience 是字符串，应为对象")
            elif not isinstance(val, dict):
                issues.append(f"结构错误: {key} 类型是 {type(val).__name__}，应为对象")
            elif key == "identity":
                for sub in ("gender", "age_range", "body_features", "location", "school_work"):
                    if sub not in val:
                        issues.append(f"结构缺失: identity 缺子键 {sub}")
            elif key == "sexual_experience":
                for sub in ("experience", "body"):
                    if sub not in val:
                        issues.append(f"结构缺失: sexual_experience 缺子键 {sub}")
        elif isinstance(default, list):
            if not isinstance(val, list):
                issues.append(f"结构错误: {key} 类型是 {type(val).__name__}，应为数组")
        elif isinstance(default, str):
            if not isinstance(val, str):
                issues.append(f"结构错误: {key} 类型是 {type(val).__name__}，应为字符串")
    return issues


async def _compress_persona_loop(persona: dict, nickname: str, priority: int = 0) -> dict:
    """人设压缩循环（2026-08-08：替代 enforce_persona_limits 硬截断）。
    字段超限（数组个数/字符串字数/总量）时调用 LLM 压缩：
    - 数组超限：合并同义项 + 精简条目冗余修饰
    - 字符串超限/语言冗余：语义压缩（保留核心事实，不切句子）
    - 总量超限：整体压回 1100-1200（量化幅度 + 字段分布参考 + 示例）
    最多 3 轮；解析失败或无效输出时保留上一轮结果。
    """
    def _field_lens(p: dict) -> list[tuple[str, int]]:
        """字段长度分布（压缩参考）：字符串字段按内容长度、数组按序列化长度。"""
        out: list[tuple[str, int]] = []
        idt = p.get("identity") or {}
        for k in ("gender", "age_range", "body_features", "location", "school_work"):
            v = idt.get(k)
            if isinstance(v, str) and v:
                out.append((f"identity.{k}", len(v)))
        for k in ("personality", "group_role"):
            v = p.get(k)
            if isinstance(v, str) and v:
                out.append((k, len(v)))
        se = p.get("sexual_experience")
        # 类型守卫：LLM 偶发输出字符串而非对象（2026-08-09 江南一白事故），防 .get 崩溃
        if isinstance(se, dict):
            for k in ("experience", "body"):
                v = se.get(k)
                if isinstance(v, str) and v:
                    out.append((f"sexual_experience.{k}", len(v)))
        for k in ("interests", "weaknesses_taboos", "catchphrases", "sexual_preferences"):
            v = p.get(k)
            if isinstance(v, list) and v:
                out.append((f"{k}（{len(v)}项）", len(json.dumps(v, ensure_ascii=False))))
        rel = p.get("relationships")
        if isinstance(rel, dict) and rel:
            out.append((f"relationships（{len(rel)}项）", len(json.dumps(rel, ensure_ascii=False))))
        out.sort(key=lambda x: -x[1])
        return out[:3]

    original_len = len(json.dumps(persona, ensure_ascii=False))  # 压缩基准：原始人设字数
    original_text = json.dumps(persona, ensure_ascii=False)  # 原始人设全文（过头修正轮恢复依据）
    system_prompt = _prompt("persona_compress_system", **_prompt_limit_ctx())
    # fewshot 压缩示范已迁至 persona_prompts["persona_compress_fewshot"]（GUI 可编辑）
    _p_lim = _persona_limits()
    _p_min, _p_max = _p_total_range()
    _p_rounds = int(_p_lim.get("compress_rounds", 3))
    for _round in range(1, _p_rounds + 1):
        issues = _persona_issues(persona)
        total = len(json.dumps(persona, ensure_ascii=False))
        if not issues and total >= _p_min:
            break
        logger.info(f"🔁 人设压缩循环 {_round}/{_p_rounds}: {nickname} {total} 字，超限 {len(issues)} 项 -> {issues}")
        if total < _p_min:
            # 过头修正轮：压过头了，对照原始人设恢复内容（2026-08-09 实测 1088→1146 成功）
            user_prompt = _prompt("persona_compress_fix_user", total=total,
                                    original_text=original_text,
                                    current_json=json.dumps(persona, ensure_ascii=False),
                                    fix_min=int(_p_lim["compress_fix_min"]),
                                    fix_max=int(_p_lim["compress_fix_max"]))
        elif _round == 1:
            # 第 1 轮：动态压缩比——把绝对目标 1100-1200 换算成原始字数的比例
            ratio_lo = max(60, int(_p_min / original_len * 100))
            ratio_hi = min(95, int(_p_max / original_len * 100))
            issue_text = "\n".join(f"- {x}" for x in issues)
            user_prompt = _prompt("persona_compress_user", issue_text=issue_text,
                                   original_len=original_len, ratio_lo=ratio_lo, ratio_hi=ratio_hi,
                                   current_json=json.dumps(persona, ensure_ascii=False),
                                   **_prompt_limit_ctx())
        else:
            # 第 2+ 轮：差距驱动——明确本轮还需减少多少字（2026-08-09 实测避免重复输出卡住）
            user_prompt = _prompt("persona_compress_gap_user", total=total,
                                  excess=total - _p_max, original_len=original_len,
                                  current_json=json.dumps(persona, ensure_ascii=False),
                                  **_prompt_limit_ctx())
        try:
            reply = await _call_llm_net([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], priority=priority, source="人设压缩", **_llm_kwargs("persona_compress_loop"))
        except LLMNetworkExhausted as _ne:
            logger.error(f"❌ 人设压缩循环网络异常: {_ne}")
            break
        new_persona = _parse_persona_json(reply.strip())
        if not new_persona:
            logger.warning("🔄 人设压缩循环输出解析失败，保留上一轮结果")
            break
        # 结构归一化：LLM 压缩可能删空字段（body:""→删键），程序补结构兜底（2026-08-09）
        new_persona = _normalize_persona_structure(new_persona)
        persona = new_persona
    total = len(json.dumps(persona, ensure_ascii=False))
    issues = _persona_issues(persona)
    logger.info(f"📏 人设压缩完成: {nickname} JSON {total} 字" +
                (f"，仍超限: {issues}" if issues else "，全部达标"))
    return persona
