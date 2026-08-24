#!/usr/bin/env python3
"""
群友聊天模拟模块（/模仿 指令）2026-08-12
========================================
以指定群友的聊天习惯和内容风格在群里回复。

数据源（原型验证：采样+画像双通道优于纯采样）：
  1. 分层采样最近 200 条有效消息（跨多天、去语音/图片/指令/@bot）
  2. 人设画像（过滤 NSFW 字段；catchphrases 口癖高权重）
  3. 上下文：群最近 50 条 + 目标用户最近立场

安全：
  - 仅管理员可用（router 层检查）
  - 目标用户数据 < 30 条拒绝（没素材模仿不像）
  - 输出纪律（禁止思考过程泄漏）
"""
from typing import Optional
import re
import time
import json
import sqlite3
import logging
from collections import Counter
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("qq-bot")

# 模仿黑名单：黑名单用户不进行模仿（/模仿 指令与赛博模仿均生效）。
# 公开版默认空集合——bot 自身由 get_bot_uin() 运行时排除，无需写死。
# 如需追加，直接填 QQ 号。
MIMIC_BLACKLIST = set()

_MAX_SAMPLES = 200          # 采样上限
_MIN_SAMPLES = 30           # 低于此数拒绝模仿
_GROUP_CONTEXT = 5          # 群上下文条数（2026-08-13 用户配置：过滤媒体后最近 5 条有效文本）
_DAILY_CAP = 30             # 每天最多采样条数（分层采样）

# ============================================================
#  数据提取
# ============================================================
def _strip_cq(text: str) -> str:
    """剥离无信息 CQ 码（图片/语音/视频/文件等）——保留表情 CQ:face（2026-08-13：
    表情 ID 是语义信息——让模仿学 TA 的表情习惯；图片等模型看不到内容故剥离）"""
    t = text or ""
    placeholders = []

    def _save(m):
        placeholders.append(m.group(0))
        return f"\x00FACE{len(placeholders) - 1}\x00"

    t = re.sub(r"\[CQ:face,[^\]]*\]", _save, t)
    t = re.sub(r"\[CQ:[^\]]*\]", "", t)  # 剥其他 CQ 码
    t = re.sub(r"\x00FACE(\d+)\x00", lambda m: placeholders[int(m.group(1))], t)
    # 文本化表情还原（2026-08-13：message_archive 里 NapCat 将表情存档为
    # [表情302] 文本——还原成 CQ:face 才能被模型学习/发送渲染）
    t = re.sub(r"\[表情(\d+)\]", r"[CQ:face,id=\1]", t)
    return t.strip()


def _is_face_only(text: str) -> bool:
    """是否为纯表情消息（只含 CQ:face 表情码——2026-08-13 保留为表情习惯样本）"""
    t = _strip_cq(text)
    if not t:
        return False
    return bool(re.fullmatch(r"(?:\[CQ:face,[^\]]*\]\s*)+", t))


# 纯媒体占位文本（NapCat 文本化后的表情包/图片等——2026-08-12：
# 大多数图片是表情包无实质信息，占位符保留会让模型误以为有图可看/困惑）
_MEDIA_PLACEHOLDERS = (
    "[图片]", "[表情]", "[动画表情]", "[视频]", "[语音]", "[音乐]",
    "[文件]", "[小程序]", "[分享]", "[转账]", "[红包]",
)


def _is_media_only(text: str) -> bool:
    """是否为纯媒体消息（无实质文本）"""
    t = _strip_cq(text)
    if not t or t in _MEDIA_PLACEHOLDERS:
        return True
    # 带数字的媒体占位（NapCat 文本化：[表情318]/[图片123] 等——2026-08-13
    # 修复：原精确匹配漏掉带数字变体，[表情318] 进入采样导致 LLM 学样输出 [表情5]）
    if re.match(r"^\[(表情|动画表情|图片|视频|语音|音乐|文件|小程序|分享|转账|红包)\d*\]$", t):
        return True
    # @某人 + 纯媒体（如 "@100000006 [图片]"）——也视为媒体消息（2026-08-12）
    t2 = re.sub(r"@\d+", "", t).strip()
    return not t2 or t2 in _MEDIA_PLACEHOLDERS or bool(re.match(r"^\[(表情|动画表情|图片|视频|语音|音乐|文件|小程序|分享|转账|红包)\d*\]$", t2))


def _get_user_messages(user_id: int, group_id: int) -> list[dict]:
    """分层采样：按天分组取消息（跨多天保证话题多样）"""
    from core.database import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT content, created_at FROM message_archive "
            "WHERE user_id=? AND target_id=? AND content IS NOT NULL AND content != '' "
            "ORDER BY created_at DESC LIMIT 3000",
            (user_id, group_id),
        ).fetchall()
    msgs = []
    face_cnt = 0
    for r in rows:
        text = _strip_cq(r["content"])
        if not text or _is_media_only(text):
            continue
        if text.startswith("/") or "@bot" in text or "@机器人" in text:
            continue
        # 2026-08-12：剥离 @QQ号（@其他群友的号码对模型无意义，且会导致模仿时虚构 @）
        text = re.sub(r"@\d+", "", text).strip()
        if not text:
            continue
        # 纯表情消息限流（2026-08-13：表情习惯样本保留——但比例 ≤20%，防表情刷屏
        # 挤占文字样本；保底保留 2 条）
        if _is_face_only(text):
            if face_cnt >= 2 and face_cnt * 5 > len(msgs):
                continue
            face_cnt += 1
        msgs.append({"text": text, "ts": r["created_at"]})
    by_day: dict[str, list] = {}
    for m in msgs:
        day = datetime.fromtimestamp(m["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(m)
    sampled = []
    for day in sorted(by_day, reverse=True):
        if len(sampled) >= _MAX_SAMPLES:
            break
        sampled.extend(by_day[day][:_DAILY_CAP])
    return sampled[:_MAX_SAMPLES]


def _style_features(msgs: list[dict]) -> str:
    """风格特征统计（口癖/长度/emoji）"""
    texts = [m["text"] for m in msgs]
    avg_len = sum(len(t) for t in texts) / max(1, len(texts))
    words = []
    for t in texts:
        for tok in re.findall(r"[\u4e00-\u9fff]{1,6}|[a-zA-Z]{2,}|[^\u4e00-\u9fff\w\s]", t):
            if tok.strip():
                words.append(tok.strip())
    stop = set("的了是我在你有他这那吗呢啊吧呀嘛哈嗯哦哈哈www捏草好不都就也一个真喜欢喵汪".replace(" ", ""))
    freq = Counter(w for w in words if w not in stop and len(w) > 1)
    top = freq.most_common(10)
    emoji_cnt = sum(1 for t in texts for c in t if ord(c) > 0x1F000)
    return (
        f"平均消息长度: {avg_len:.0f} 字\n"
        f"高频词/口癖: {'、'.join(f'{w}({n}次)' for w, n in top)}\n"
        f"emoji 使用: {emoji_cnt}/{len(texts)} 条消息"
    )


def _get_persona_text(user_id: int, group_id: int) -> str:
    """人设画像（过滤 NSFW；catchphrases/relationships 高价值字段保留；
    2026-08-12 完整注入不截断——原 500 字截断导致 relationships 截半）"""
    try:
        with sqlite3.connect('data/personas.db') as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT persona FROM user_personas WHERE user_id=? AND group_id=?",
                (user_id, group_id),
            ).fetchone()
            if not row:
                return ""
            p = json.loads(row["persona"])
        keep = {}
        for k in ("identity", "interests", "personality", "group_role", "catchphrases", "relationships"):
            if k in p and p[k]:
                keep[k] = p[k]
        if "relationships" in keep and isinstance(keep["relationships"], dict):
            keep["relationships"] = {str(k): v for k, v in list(keep["relationships"].items())[:5]}
        return json.dumps(keep, ensure_ascii=False)
    except Exception:
        return ""


def _get_profile_text(user_id: int, group_id: int) -> str:
    """用户画像（user_profiles.profile——【现实底牌】自由文本，2026-08-12 新增注入）"""
    try:
        with sqlite3.connect('data/personas.db') as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT profile FROM user_profiles WHERE user_id=? AND group_id=?",
                (user_id, group_id),
            ).fetchone()
            return (row["profile"] or "").strip() if row else ""
    except Exception:
        return ""


def _recent_group_msgs(group_id: int, n: int = _GROUP_CONTEXT) -> list[str]:
    """群最近 n 条【有效文本】消息（上下文）。

    2026-08-13 用户配置：n=5——去除图片/语音等纯媒体后仍保留的最近 5 条，
    避免模型被旧话题干扰（50 条时模型倾向接 34 分钟前的旧话题）。

    其他特性（保留）：
    - 每条带时间标志 [MM-DD HH:MM:SS]（东八区，秒级）
    - @QQ号 替换为 @昵称
    """
    from core.database import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, nickname, content, created_at FROM message_archive "
            "WHERE target_id=? AND content IS NOT NULL AND content != '' "
            "ORDER BY created_at DESC LIMIT 500",  # 大窗口保证过滤后有 n 条
            (group_id,),
        ).fetchall()
    # 过滤纯媒体/指令/@bot，取最近 n 条有效文本（表情消息限流 10 条——2026-08-13）
    valid = []
    face_ctx = 0
    for r in rows:
        text = _strip_cq(r["content"])
        if not text or text.startswith("/") or _is_media_only(text):
            continue
        if _is_face_only(text):
            face_ctx += 1
            if face_ctx > 10:
                continue
        valid.append(r)
        if len(valid) >= n:
            break
    # 构建 user_id → 昵称映射（@QQ号 转昵称用）
    nick_map: dict[int, str] = {}
    for r in valid:
        if r["user_id"] and r["nickname"]:
            nick_map[r["user_id"]] = r["nickname"]

    def _resolve_nick(uid: int) -> str:
        """@QQ号 → 昵称：窗口映射优先；未命中补查数据库最新昵称（2026-08-12）"""
        if uid in nick_map:
            return nick_map[uid]
        try:
            from core.database import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT nickname FROM message_archive WHERE user_id=? AND nickname != '' "
                    "ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
                return row["nickname"] if row else str(uid)
        except Exception:
            return str(uid)

    lines = []
    for r in reversed(valid):  # 时间升序（旧→新）
        text = _strip_cq(r["content"])
        # @QQ号 → @昵称（找不到的保留原数字）
        text = re.sub(r"@(\d+)", lambda m: f"@{_resolve_nick(int(m.group(1)))}", text)
        ts = datetime.fromtimestamp(r["created_at"], tz=timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
        lines.append(f"[{ts}] {r['nickname'] or '?'}: {text[:80]}")
    return lines


def _recent_user_stance(user_id: int, group_id: int) -> str:
    """目标用户最近 1 分钟内的实质发言（立场延续——模仿不能与其观点相反）。

    2026-08-13 用户配置：只取 1 分钟内的发言——1 分钟内没有有效发言则返回空
    （调用方不加立场段）——防旧消息干扰（原"最近 2 条实质发言"可能跨越数十分钟，
    含旧话题带偏最近接话，如 27 分钟前的"被发现了"干扰音响话题）。
    """
    # 无信息量收尾/情绪词（出现即视为该条无立场价值）
    _NOISE = ("晚安", "早", "哈哈", "笑死", "哦", "嗯", "好", "？", "！", "好的", "睡了", "我去", "神了", "666", "在吗", "在的", "可以", "正确的", "好哦")
    cutoff = time.time() - 60  # 1 分钟窗口
    from core.database import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT content, created_at FROM message_archive "
            "WHERE user_id=? AND target_id=? AND content IS NOT NULL AND content != '' "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 20",
            (user_id, group_id, cutoff),
        ).fetchall()
    lines = []
    for r in rows:
        t = _strip_cq(r["content"])
        if not t or t.startswith("/") or _is_media_only(t) or _is_face_only(t):
            continue  # 表情消息无立场价值（2026-08-13）
        t = re.sub(r"@\d+", "", t).strip()
        if not t:
            continue
        if t in _NOISE or len(t) <= 2:
            continue  # 收尾语/无信息量
        ts = datetime.fromtimestamp(r["created_at"], tz=timezone(timedelta(hours=8))).strftime("%H:%M")
        lines.append(f"[{ts}] {t[:80]}")
        if len(lines) >= 2:
            break
    return "\n".join(reversed(lines))  # 时间升序（旧→新）——与群上下文一致；空=1分钟内无发言


# ============================================================
#  Prompt 构建 + 生成
# ============================================================
def _build_prompt(nickname: str, msgs: list[dict], persona_text: str,
                  group_msgs: list[str], stance: str, question: str) -> list[dict]:
    # 2026-08-13 用户配置：删【风格特征统计】段 + 删【他最近说过】立场段
    # 保留：样本40条 + 人设画像 + 要求7条 + 群上下文
    # 2026-08-13 表情样本置顶：纯表情消息优先入样本（最多 5 条——表情习惯
    # 是稀有信号，按序切片时容易被文字挤掉）
    _face_msgs = [m for m in msgs if _is_face_only(m["text"])]
    _text_msgs = [m for m in msgs if not _is_face_only(m["text"])]
    _sample_list = _face_msgs[:5] + _text_msgs[: max(0, 40 - len(_face_msgs[:5]))]
    samples = "\n".join(f"- {m['text'][:80]}" for m in _sample_list)
    system = (
        f"你现在要模仿群友「{nickname}」在群里聊天。\n\n"
        "【风格样本】（他的真实聊天记录节选）：\n" + samples + "\n\n"
        "要求：\n"
        f"1. 完全用{nickname} 的口吻、用词习惯、表情风格（照抄他的口癖）\n"
        "2. 内容符合他的话题偏好\n"
        "3. 消息长度与他平时相近\n"
        "4. 绝口不提自己是 AI 或模仿\n"
        "5. 不要拼凑样本原句，像他一样自然回应\n"
        "6. ⚠️ 直接输出回复内容本身！禁止输出任何思考过程、分析、"
        "「我回应应该…」「可能回：」「他的风格是…」等过程文字\n"
        "7. 输出格式：JSON {\"reply\": \"你的回复内容\"}——reply 字段必须是可直接发送的发言"
    )
    if persona_text:
        system += (
            "\n\n【他的人设画像】（仅供参考方向，口癖以采样为准）：\n" + persona_text +
            "\n注意：画像中的人际关系不要直接复述，只是理解他对群友的态度。"
        )
    user = (
        "【群里最近在聊】\n" + "\n".join(group_msgs) + "\n\n"
        + (f"【他最近说过】\n{stance}\n\n" if stance else "")
        + f"请以{nickname} 的身份根据群聊上下文自然接话，"
        f"输出 JSON 格式：{{\"reply\": \"回复内容\"}}（reply 必须是可直接发送的发言，禁止思考过程）："
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def generate_mimic_reply(nickname: str, user_id: int, group_id: int,
                               question: str, priority: int = 0) -> str:
    """生成一条模仿发言。失败返回空字符串。"""
    from core.persona import _call_llm_net

    # bot 自身防御：不应被模仿（运行时从连接派生，不写死）
    try:
        from core.sender import get_bot_uin
        if int(user_id) == int(get_bot_uin() or 0):
            logger.warning(f"[模仿] 目标为 bot 自身，拒绝模仿")
            return ""
    except Exception:
        pass
    # 黑名单检查：黑名单用户不进行模仿
    if user_id in MIMIC_BLACKLIST:
        logger.warning(f"[模仿] {nickname}({user_id}) 在模仿黑名单中，拒绝模仿")
        return ""

    msgs = _get_user_messages(user_id, group_id)
    if len(msgs) < _MIN_SAMPLES:
        logger.warning(f"[模仿] {nickname} 有效消息仅 {len(msgs)} 条（<{_MIN_SAMPLES}），拒绝模仿")
        return ""

    persona_text = _get_persona_text(user_id, group_id)
    group_msgs = _recent_group_msgs(group_id)
    stance = _recent_user_stance(user_id, group_id)

    prompt = _build_prompt(nickname, msgs, persona_text, group_msgs, stance, question)
    # 2026-08-24：LLM 参数跟随 AI 聊天链路（与 _handle_ai_reply 同源：
    # CONFIG["AI_CHAT_CFG"]["llm"]，GUI「AI 聊天·显示参数」弹窗可配、热重载即时生效）。
    # 原硬编码 max_tokens=8192/temperature=0.85/thinking=max 被本地后端 Jinja 模板
    # 拒绝（Qwen3 系模板只接受 none/low/medium/high，max → HTTP 500
    # "Unexpected reasoning effort max" → 赛博模仿整链路报"模型出了点小问题"）。
    from core.config import CONFIG as _CFG
    _ai_llm = ((_CFG.get("AI_CHAT_CFG") or {}).get("llm") or {})
    _kw = {
        "max_tokens": int(_ai_llm.get("max_tokens", 65536)),
        "temperature": float(_ai_llm.get("temperature", 0.7)),
        "json_mode": bool(_ai_llm.get("json_mode", False)),
        "timeout": int(_ai_llm.get("timeout", 1800)),
        "priority": priority,
    }
    _th = str(_ai_llm.get("thinking", "on")).lower()
    if _th == "off":
        _kw["disable_thinking"] = True
    elif _th in ("low", "max"):
        _kw["reasoning_effort"] = _th
    # _th == "on" → 不传（后端默认思考）
    reply = await _call_llm_net(prompt, **_kw)
    text = (reply or "").strip()
    # JSON 解析（json_mode 输出 {"reply": "..."}）
    try:
        _parsed = json.loads(text)
        if isinstance(_parsed, dict) and _parsed.get("reply"):
            text = str(_parsed["reply"])
    except Exception:
        # 解析失败：提取所有 {..} 块（非嵌套），逐个尝试——2026-08-13 修复：
        # LLM 偶发输出 {"reply":"x1"}{"reply":"x2"} 多段拼接——取第一个合法 JSON
        for _m in re.finditer(r"\{[^{}]*\}", text):
            try:
                _parsed = json.loads(_m.group(0))
                if isinstance(_parsed, dict) and _parsed.get("reply"):
                    text = str(_parsed["reply"])
                    break
            except Exception:
                continue
        else:
            # 未闭合 JSON 修复（2026-08-14）：LLM 输出 {"reply": "内容 但缺结尾 "}——
            # 如 {"reply": "夜袭可还行hhh（害怕了喵（无右括号）——提取 reply 内容
            if re.match(r'^\s*\{\s*"reply"\s*:\s*"', text):
                _m2 = re.match(r'^\s*\{\s*"reply"\s*:\s*"(.*)', text, re.S)
                if _m2:
                    text = _m2.group(1).rstrip().rstrip('"}').strip()
    # 清理思考残留（兜底——json 解析失败时）
    for marker in ("我回应应该", "可能回：", "他的风格是", "作为", "模仿",
                   "Short messages", "I should", "Given it's", "The last message",
                   "response could", "- Playful", "Maybe something", "Looking at",
                   "From the style", "The user", "I need to", "Let me think",
                   "Or something", "something like", "response to", "Given 渡月初",
                   "he's already", "it's 1am", "Given it", "His style"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    # 表情转换（2026-08-13）：[表情N] → [CQ:face,id=N]——NapCat 文本化格式
    # 转回 CQ 码才能渲染成真表情（原样发送会显示字面"[表情5]"）
    text = re.sub(r"\[表情(\d+)\]", r"[CQ:face,id=\1]", text)
    return text.strip()[:200]


def check_mimic_command(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """解析 /模仿 指令 → (昵称, 内容, 错误信息)"""
    if not text.startswith("/模仿"):
        return None, None, None
    rest = text[len("/模仿"):].strip()
    if not rest:
        return None, None, "📝 用法：/模仿 <昵称> <要说的内容>\n例：/模仿 渡月初 在吗"
    # 昵称 = 第一个词（可能含括号如 渡月初（day 6）——取到冒号/空格/换行前）
    m = re.match(r"^(.+?)[:：\s]+(.+)$", rest, re.S)
    if not m:
        return None, None, "📝 用法：/模仿 <昵称> <要说的内容>\n例：/模仿 渡月初 在吗"
    return m.group(1).strip(), m.group(2).strip(), None
