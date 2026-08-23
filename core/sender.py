#!/usr/bin/env python3
"""
消息发送模块 — WebSocket 注册、回复发送（同步/异步/全局）。
"""
import asyncio
import json
import re  # 表情段转换（2026-08-13）
import time
import uuid
import logging
from typing import Optional

import websockets

logger = logging.getLogger("qq-bot")

# 内容审查 — 延迟导入避免循环依赖
_content_filter = None


def _get_content_filter():
    global _content_filter
    if _content_filter is None:
        from core.content_filter import censor_text
        _content_filter = censor_text
    return _content_filter


def _censor(text: str) -> str:
    """对文本进行敏感词审查，替换为拼音。"""
    return _get_content_filter()(text)

# 全局 websocket 注册，供其他模块后台发送消息
_active_websocket: Optional[websockets.legacy.server.WebSocketServerProtocol] = None
_main_event_loop: Optional[asyncio.AbstractEventLoop] = None

# 待响应的 action 请求（echo -> Future），用于 get_login_info 等查询类调用
_pending_requests: dict[str, "asyncio.Future[dict]"] = {}

# ---- WS 连接池（2026-08-21 多账号抢连修复）----
# ws -> (uin, nickname)。主账号判定需要"按连接"查账号（而非全局槽），
# 并支持主动关闭非主账号连接（不等它 3 秒重连再被顶掉）。
_ws_connections: dict = {}


def get_ws_identity(ws) -> tuple:
    """查询指定连接的账号身份 (uin, nickname)；未识别返回 ("", "")"""
    ident = _ws_connections.get(ws)
    return ident if ident else ("", "")


def update_ws_identity(ws, uin: str, nickname: str = "") -> None:
    """记录连接的账号身份（get_login_info 成功后调用）"""
    _ws_connections[ws] = (str(uin), nickname)


def count_ws_connections() -> int:
    """当前 WS 连接池大小（含尚未识别身份的连接）"""
    return len(_ws_connections)


def kick_non_primary_websockets(keep_ws) -> int:
    """主动关闭连接池中除 keep_ws 外的所有连接。返回关闭数量。

    主账号切换时调用：旧账号连接被立即关掉（code=4001），
    从源头断掉"旧账号 3 秒重连风暴"（配合配置收敛后不再重连）。
    """
    kicked = 0
    for ws in list(_ws_connections.keys()):
        if ws is keep_ws:
            continue
        try:
            asyncio.create_task(
                ws.close(code=4001, reason="主账号已切换，旧连接已关闭")
            )
            kicked += 1
        except Exception:
            pass
    return kicked


async def request_action(action: str, params: Optional[dict] = None,
                         timeout: float = 10.0, ws=None) -> dict:
    """向 NapCat 发 OneBot action 并等待响应（需在主事件循环内调用）。

    ws 参数（2026-08-21）：指定目标连接；默认 None = 全局活跃槽。
    主账号判定时新连接尚未注册（槽里可能还是旧连接），必须按连接发。

    返回响应 dict（含 status/retcode/data）。未连接或超时抛 RuntimeError。
    响应消息由 websocket_handler 的 _consume_ws_event() 拦截分发。
    """
    global _active_websocket
    target = ws if ws is not None else _active_websocket
    if target is None:
        raise RuntimeError("NapCat 未连接")
    echo = f"req-{uuid.uuid4().hex}"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_requests[echo] = fut
    await target.send(json.dumps(
        {"action": action, "params": params or {}, "echo": echo}))
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        _pending_requests.pop(echo, None)


def _consume_ws_event(data: dict) -> bool:
    """检查消息是否为待响应 action 的响应。是则分发并返回 True。

    在 websocket_handler 的消息循环里、handle_message 之前调用。
    """
    echo = data.get("echo")
    if echo and echo in _pending_requests:
        fut = _pending_requests[echo]
        if not fut.done():
            fut.set_result(data)
        return True
    return False


def get_login_info_sync(timeout: float = 10.0) -> Optional[dict]:
    """线程安全版 get_login_info（GUI/后台线程调用）。

    返回 {"user_id": ..., "nickname": ...}，失败返回 None。
    """
    if _active_websocket is None or _main_event_loop is None:
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(
            request_action("get_login_info", timeout=timeout), _main_event_loop)
        resp = fut.result(timeout=timeout + 3)
        if resp.get("status") == "ok" and resp.get("data"):
            d = resp["data"]
            return {"user_id": d.get("user_id"), "nickname": d.get("nickname")}
    except Exception as e:
        logger.warning(f"get_login_info 失败: {e}")
    return None


def register_websocket(ws: websockets.legacy.server.WebSocketServerProtocol) -> None:
    """注册 NapCat WebSocket 连接。

    2026-08-21 多账号抢连修复：
    - 后注册者占槽（保持旧行为——主账号判定在 bot.py handler 里先于本函数
      完成，能走到这里的都是该占槽的连接）
    - 所有连接进 _ws_connections 池（主账号判定按连接查身份用）
    """
    global _active_websocket, _main_event_loop
    _active_websocket = ws
    _main_event_loop = asyncio.get_running_loop()
    _ws_connections[ws] = ("", "")
    logger.info(f"🔗 WebSocket 已注册: {ws.remote_address if hasattr(ws, 'remote_address') else ws}"
                f"（当前连接数: {len(_ws_connections)}）")


def unregister_websocket(ws=None) -> None:
    """注销 NapCat WebSocket 连接。

    2026-08-21 修复（竞态）：ws 传入时只有槽里当前就是这条连接才清槽——
    切主时旧连接的清理不会再抹掉已注册的新主连接
    （旧实现无条件置 None：旧连接晚清理一步，槽被抹空，发消息全丢）。
    ws=None 保持旧行为（无条件清槽，兼容无参调用方）。
    """
    global _active_websocket, _main_event_loop
    if ws is not None:
        _ws_connections.pop(ws, None)
    else:
        _ws_connections.clear()
    if ws is None or ws is _active_websocket:
        _active_websocket = None
        _main_event_loop = None


# ---- 主账号 QQ（bot 运行时身份，2026-08-22 从连接派生）----
# bot.qq 原为静态配置（GUI 配置页"机器人"组框管理，改动需重启）。
# 08-22 动态化：主账号 uin 由 NapCat 连接 get_login_info 派生——
# bot.py 主账号判定后调 set_bot_uin() 更新；所有运行时读取点（@bot 判定、
# 忽略自己的消息、统计排除 bot 等）改读 get_bot_uin()。
# 兜底链：_bot_uin（连接派生）→ CONFIG["BOT_QQ"]（yaml 配置）→ ""。
# yaml 值保留作"未连接/识别失败"时的 fallback，GUI 不再暴露该字段
# （值改不动运行时身份，留 GUI 输入框会误导）。
_bot_uin: str = ""


def set_bot_uin(uin: str) -> None:
    """记录主账号 QQ（bot.py 主账号判定后调用）。空值不覆盖已有身份。"""
    global _bot_uin
    uin = str(uin or "").strip()
    if uin:
        _bot_uin = uin


def get_bot_uin() -> str:
    """当前 bot 主账号 QQ：连接派生优先，CONFIG 兜底（连接前/识别失败时）。"""
    if _bot_uin:
        return _bot_uin
    try:
        from .config import CONFIG as _CFG
        return str(_CFG.get("BOT_QQ", "") or "")
    except Exception:
        return ""


def _ws_alive(ws) -> bool:
    """WS 连接存活检查（兼容 websockets 新旧 API，2026-08-22）。

    根因：websockets 16.x 的 asyncio ServerConnection 没有 .open 属性
    （旧版 legacy WebSocketServerProtocol 有 bool .open）——代码用
    `getattr(ws, "open", False)` 判存活在新版上恒为 False，导致
    get_login_info_for 永远返回 None → set_bot_uin 永不执行 →
    @判定回落到 config 兜底错号 → @bot 静默无回复。

    兼容策略：
      - 有 bool .open（旧版 legacy）→ 直接返回
      - 有 .state（新版 asyncio，State.OPEN.value == 1）→ 按 state 判
      - 两者皆无（测试 fake 等）→ 视为存活，真死活交给 send/recv 异常
    """
    if ws is None:
        return False
    open_attr = getattr(ws, "open", None)
    if isinstance(open_attr, bool):
        return open_attr
    state = getattr(ws, "state", None)
    if state is not None:
        try:
            return int(getattr(state, "value", state)) == 1
        except (TypeError, ValueError):
            pass
    return True


async def get_login_info_for(ws) -> Optional[dict]:
    """查询指定连接的登录账号（不走全局活跃槽）。

    主账号判定时新连接可能尚未注册（槽里还是旧连接），
    必须按连接发 get_login_info。返回 {"user_id", "nickname"}，失败 None。
    """
    if not _ws_alive(ws):
        return None
    try:
        resp = await request_action("get_login_info", timeout=8, ws=ws)
        if resp.get("status") == "ok" and resp.get("data"):
            d = resp["data"]
            uid = str(d.get("user_id") or d.get("info", {}).get("user_id") or "").strip()
            if uid:
                return {"user_id": uid,
                        "nickname": str(d.get("nickname") or d.get("info", {}).get("nickname") or "").strip()}
        logger.warning(f"get_login_info(按连接) 无有效账号: {str(resp)[:100]}")
        return None
    except Exception as e:
        logger.warning(f"get_login_info(按连接) 异常: {e}")
        return None


def _send_gate_ok(message_type: str, target_id: int, summary: str = "") -> bool:
    """消息管理：发送门控统一入口（2026-08-23 方案A）。

    msg.send_enabled 总开关 + msg.send_scope 范围门控，所有对外发送路径
    （文本回复 / 图片 / 卡片 / 游戏消息）必须经此判定，被拦截静默丢弃。
    """
    from .config import CONFIG as _CFG
    if not _CFG.get("MSG_SEND_ENABLED", True):
        logger.debug(f"🚫 发送门控: 发送总开关已关闭（target={target_id} {summary}）")
        return False
    _scope = str(_CFG.get("MSG_SEND_SCOPE", "all")).lower()
    if _scope == "group" and message_type != "group":
        logger.debug(f"🚫 发送门控: 发送范围=仅群消息，跳过私聊发送（target={target_id} {summary}）")
        return False
    if _scope == "private" and message_type != "private":
        logger.debug(f"🚫 发送门控: 发送范围=仅私聊，跳过群发送（target={target_id} {summary}）")
        return False
    return True


async def send_segments(
    websocket,
    message_type: str,
    target_id: int,
    segments: list[dict],
    echo: Optional[str] = None,
    wait_response: bool = False,
    timeout: float = 10.0,
) -> Optional[dict]:
    """统一消息发送出口（2026-08-23 方案A：所有对外发送的唯一出口）。

    发送门控（总开关 + 范围）只在此处判定一次：被拦截时静默丢弃并
    返回 None（不抛异常，调用方无需处理）。

    Parameters:
        segments: ArrayMessage 段列表（text/image/face/contact/at/reply）
        echo: 自定义 echo（不传则自动生成 str(time.time())）
        wait_response: True = 等待 NapCat 响应（需 message_id 的图片发送、
            需失败计数的群邀请）；False = fire-and-forget（游戏消息等）
    Returns:
        wait_response=True: 响应 data dict 或 None（超时/被拦截/失败）
        wait_response=False: None
    """
    if not _send_gate_ok(message_type, target_id, f"type={len(segments)}seg"):
        return None

    action = "send_group_msg" if message_type == "group" else "send_private_msg"
    param_key = "group_id" if message_type == "group" else "user_id"

    # wait_response 模式：先注册响应槽再发送——响应可能在 send 返回前就到达
    # （NapCat 响应快/测试 fake 同步分发），注册在后会有丢失窗口
    echo_id = echo if echo is not None else str(time.time())
    if wait_response:
        from .router import _api_responses  # 延迟导入避免循环依赖
        event = asyncio.Event()
        _api_responses[echo_id] = {"event": event, "data": None}
    else:
        event = None

    msg = {
        "action": action,
        "params": {param_key: target_id, "message": segments},
        "echo": echo_id,
    }
    await websocket.send(json.dumps(msg))

    if not wait_response:
        return None

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _api_responses.pop(echo_id, {}).get("data")
    except asyncio.TimeoutError:
        _api_responses.pop(echo_id, None)
        logger.warning(f"send_segments 等待响应超时: {action} target={target_id}")
        return None


async def send_reply(
    websocket,
    message_type: str,
    target_id: int,
    text: str,
    user_id: Optional[int] = None,
    reply_id: Optional[int] = None,
    at_user_ids: Optional[list[int]] = None,
):
    """
    构造回复消息并通过 WebSocket 发送。
    使用 ArrayMessage 格式，包含 @提及 + 引用回复 + 纯文本。

    消息管理（2026-08-20）：msg.send_enabled 总开关 + msg.send_scope
    范围门控。被拦截的发送静默丢弃（调用方无需处理）。
    2026-08-23（方案A）：发送动作委托 send_segments 统一出口，门控单点判定。

    Parameters:
        at_user_ids: 额外需要 @ 的用户 ID 列表（如真心话大冒险的输家）
    """
    # ---- 消息管理：发送门控（send_segments 内再判一次，双保险）----
    from .config import CONFIG as _CFG
    if not _CFG.get("MSG_SEND_ENABLED", True):
        logger.debug(f"🚫 发送门控: 发送总开关已关闭（target={target_id}, len={len(text)}）")
        return
    _scope = str(_CFG.get("MSG_SEND_SCOPE", "all")).lower()
    if _scope == "group" and message_type != "group":
        logger.debug(f"🚫 发送门控: 发送范围=仅群消息，跳过私聊发送（target={target_id}）")
        return
    if _scope == "private" and message_type != "private":
        logger.debug(f"🚫 发送门控: 发送范围=仅私聊，跳过群发送（target={target_id}）")
        return

    segments: list[dict] = []

    # 引用回复
    if reply_id is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})

    # @ 提及发送者（后面加一个空格）
    if user_id is not None:
        segments.append({"type": "at", "data": {"qq": str(user_id)}})
        segments.append({"type": "text", "data": {"text": " "}})

    # 内容审查 — 敏感词替换为拼音
    text = _censor(text)

    # 表情段转换（2026-08-13）：ArrayMessage 的 text 段不解析 CQ 码——
    # 文本里的 [CQ:face,id=N] 需拆分为 face 段才能渲染真表情
    # （否则群里显示字面 "[CQ:face,id=302]"）
    if "[CQ:face" in text:
        for part in re.split(r"(\[CQ:face,id=\d+\])", text):
            if not part:
                continue
            m = re.fullmatch(r"\[CQ:face,id=(\d+)\]", part)
            if m:
                segments.append({"type": "face", "data": {"id": m.group(1)}})
            else:
                segments.append({"type": "text", "data": {"text": part}})
    else:
        # 纯文本
        segments.append({"type": "text", "data": {"text": text}})

    # @ 提及额外用户列表（如真心话大冒险的输家）— 放在消息末尾
    if at_user_ids:
        for uid in at_user_ids:
            segments.append({"type": "at", "data": {"qq": str(uid)}})
            segments.append({"type": "text", "data": {"text": " "}})

    # 方案A（2026-08-23）：发送动作委托统一出口（门控已在上方判定，
    # send_segments 内部双保险再判一次，此处必然通过）
    await send_segments(websocket, message_type, target_id, segments,
                        echo=str(time.time()))


def send_reply_sync(
    message_type: str,
    target_id: int,
    text: str,
    user_id: Optional[int] = None,
    reply_id: Optional[int] = None,
    at_user_ids: Optional[list[int]] = None,
) -> bool:
    """
    从后台线程发送消息。通过 asyncio.run_coroutine_threadsafe 投递到主事件循环执行。
    返回 True 表示发送成功，False 表示 websocket 未连接或投递失败。
    """
    if _active_websocket is None or _main_event_loop is None:
        logger.error(f"[send_reply_sync] websocket 或 event_loop 未就绪, target={target_id}")
        return False
    if _main_event_loop.is_closed():
        logger.error(f"[send_reply_sync] 主事件循环已关闭, target={target_id}")
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_reply(_active_websocket, message_type, target_id, text, user_id=user_id, reply_id=reply_id, at_user_ids=at_user_ids),
            _main_event_loop
        )
        future.result(timeout=10)
        logger.info(f"send_reply_sync 成功: target={target_id}, len={len(text)}")
        return True
    except Exception as e:
        logger.error(f"send_reply_sync 异常: {e}", exc_info=True)
        return False


async def send_reply_global(
    message_type: str,
    target_id: int,
    text: str,
    user_id: Optional[int] = None,
    reply_id: Optional[int] = None,
    at_user_ids: Optional[list[int]] = None,
) -> bool:
    """
    供后台异步任务（同事件循环）使用的全局发送函数。
    当娱乐模块使用 asyncio.create_task 后台生成内容时调用。
    """
    if _active_websocket is None:
        logger.error(f"[send_reply_global] websocket 未就绪, target={target_id}")
        return False
    try:
        await send_reply(_active_websocket, message_type, target_id, text, user_id, reply_id, at_user_ids)
        logger.info(f"send_reply_global 成功: target={target_id}, len={len(text)}")
        return True
    except websockets.exceptions.ConnectionClosed:
        logger.warning(f"[send_reply_global] WebSocket 已断开，消息未发送, target={target_id}")
        return False
    except Exception as e:
        logger.error(f"[send_reply_global] 异常: {e}", exc_info=True)
        return False
