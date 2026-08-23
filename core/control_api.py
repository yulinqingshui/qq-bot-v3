#!/usr/bin/env python3
"""
control_api.py — bot 内嵌控制 API（GUI 专用通道）
====================================================
- 只绑 127.0.0.1（CONFIG: control_api），不对局域网暴露
- 与 bot 同进程运行（共享事件循环），GUI 通过它读写状态/配置/触发重载
- 接口一览：
    GET  /status               运行状态（NapCat/LLM/存档开关/版本/运行时长）
    GET  /config               当前配置（密钥打码）
    POST /config               热重载 config.yaml + .env，返回 {applied, restart_required, errors}
    POST /reload               重载指定资源: sensitive_words / pun_question_bank / all
    POST /test/llm             用当前配置发一次最小 LLM 调用（GUI「测试连接」）
    POST /test/comfyui         探活 ComfyUI /system_stats（可选 body {url} 用指定地址探活，08-22）
    POST /restart              请求优雅重启（GUI 确认后调用）
    GET  /napcat               NapCat 登录态 + 扫码二维码（base64 PNG），GUI 登录卡片用
    POST /analysis/query       消息分析（GUI 消息管理页）：{question, rows[], meta}
    GET  /analysis/query/status?run_id= 分析任务进度/结果
    POST /roleplay/cleanup     角色扮演房间清理（{room_id?}，仅 ended 房间；级联 7 表）
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional

from aiohttp import web


# ============================================================
#  2026-08-23：GUI 轮询降噪（用户拍板：200 轮询不落盘，只记非 200 和写操作）
#  背景：GUI 总览页 2s/5s/10s 轮询 /status /llm/recent /llm/usage，
#  aiohttp 默认 access log 把每条 200 心跳都落盘（实测 1.75 行/秒，
#  15 万行/天），真正的业务日志被淹。过滤规则（精确、保守）：
#  - GET /status、/llm/recent、/llm/usage、/napcat 且返回 200 → 不落盘
#    （/napcat 2026-08-23 加入：GUI NapCat 卡片 5s 轮询，同属心跳噪音）
#  - 非 200（4xx/5xx 等异常响应）→ 照记（排查依据，不能丢）
#  - 写操作（POST/PUT/DELETE/...）→ 照记（用户操作痕迹，不能丢）
#  实现：aiohttp access log 走 logging.Logger("aiohttp.access")，
#  给它挂 Filter 即可，不动 aiohttp 本体。
# ============================================================

_POLL_200_PATHS = frozenset(
    ("/status", "/llm/recent", "/llm/usage", "/napcat"))
_READ_METHODS = frozenset(("GET", "HEAD"))


class _AccessPollFilter(logging.Filter):
    """只吞掉 200 的 GUI 轮询 GET；其余全部放行。

    aiohttp 3.13 默认 access log 格式（logging 模板 %s - - [%s] "%s" %d %s "%s" "%s"）：
        127.0.0.1 [23/Aug/2026:06:30:00 +0000] "GET /status HTTP/1.1" 200 322 "-" "Mozilla/5.0"
    单一正则解析（method + path + status），解析失败一律放行（宁可多记不能漏记）。
    """

    _LINE_RE = re.compile(
        r'^\S+\s+\[[^\]]*\]\s+"([A-Z]+)\s+(\S+)\s+HTTP/[\d.]+"\s+(\d{3})\s')

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            m = self._LINE_RE.match(record.getMessage())
            if not m:
                return True
            method, path, status = m.group(1), m.group(2), m.group(3)
            if (method in _READ_METHODS and path in _POLL_200_PATHS
                    and status == "200"):
                return False  # 200 轮询 → 不落盘
            return True
        except Exception:
            return True  # 解析异常一律放行（宁可多记不能漏记）


def _setup_access_log_filter():
    """给 aiohttp access logger 挂轮询过滤（幂等，重复调用安全）。"""
    logger = logging.getLogger("aiohttp.access")
    for f in list(logger.filters):
        if isinstance(f, _AccessPollFilter):
            return  # 已挂过
    logger.addFilter(_AccessPollFilter())

from .config import CONFIG, load_config
from . import __version__

def _get_logger():
    import logging
    return logging.getLogger("qq-bot")


# ============================================================
#  密钥打码
# ============================================================
_SECRET_KEYS = {"REMOTE_API_KEY", "DEEPSEEK_API_KEY", "LLM_API_KEY"}


def _mask(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def _redact_config() -> dict:
    out = {}
    for k, v in CONFIG.items():
        if k in _SECRET_KEYS:
            v = _mask(str(v))
        out[k] = v
    return out


# ============================================================
#  状态快照
# ============================================================
def _napcat_status_detail() -> dict:
    """读取 data/napcat_status.txt（bot.py 写入的状态文件）。

    08-24 编码修复：bot.py 写文件已强制 UTF-8；历史文件可能是
    Windows 默认 GBK 写入的，UTF-8 解码失败时回退 GBK 读取。
    """
    try:
        status_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "napcat_status.txt")
        if os.path.exists(status_file):
            data = {}
            raw = open(status_file, "rb").read()
            for enc in ("utf-8", "gbk"):
                try:
                    text = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    data[k.strip()] = v.strip()
            return data
    except Exception:
        pass
    return {}


def llm_health_snapshot() -> dict:
    """LLM 健康状态快照（/status 合入，GUI 状态灯用）。

    只反映最近一次真实 LLM 调用或手动连接测试的结果（08-21）：
    idle=无活动（bot 启动后还没调过 LLM），ok=最近一次成功，fail=最近一次失败。
    不做后台心跳探测（不烧 token）。
    """
    from .llm import LLM_HEALTH
    return {
        "health": LLM_HEALTH["status"],
        "health_ts": LLM_HEALTH["ts"],
        "health_error": LLM_HEALTH["error"],
        "health_source": LLM_HEALTH["source"],
    }


def _get_task_snapshot() -> dict:
    """任务注册表快照（2026-08-22）。

    延迟 import task_registry：control_api 在 bot 进程内 import，
    但 get_status 也可能在测试/独立进程里被调（task_registry 无副作用，
    延迟 import 避免 import 顺序问题）。
    """
    try:
        from .task_registry import TASK_REGISTRY
        return TASK_REGISTRY.snapshot()
    except Exception:
        # 注册表异常时返回空结构（面板显示"当前无任务"），不影响 /status
        return {"running": [], "queued": [], "count": 0, "paused": False}


# ============================================================
#  任务列表 暂停/继续（2026-08-22：GUI 任务面板按钮，范围=全部、无限等待）
# ============================================================
async def _h_tasks_pause(request):
    """暂停任务序列：卡"新任务开始"，不打断执行中任务。幂等。"""
    try:
        from .task_registry import TASK_REGISTRY
        changed = TASK_REGISTRY.pause()
        _get_logger().info("⏸️ 控制API: 任务序列已暂停" + ("" if changed else "（原本已暂停）"))
        return web.json_response({"ok": True, "paused": True, "changed": changed})
    except Exception as e:
        _get_logger().error(f"⏸️ 控制API: 暂停任务失败: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def _h_tasks_resume(request):
    """继续任务序列：放行全部等待中的任务（按原 FIFO 顺序）。幂等。"""
    try:
        from .task_registry import TASK_REGISTRY
        changed = TASK_REGISTRY.resume()
        _get_logger().info("▶️ 控制API: 任务序列已继续" + ("" if changed else "（原本未暂停）"))
        return web.json_response({"ok": True, "paused": False, "changed": changed})
    except Exception as e:
        _get_logger().error(f"▶️ 控制API: 继续任务失败: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


def get_status() -> dict:
    from .sender import _active_websocket, get_bot_uin
    from .llm import _resolve_llm_backend
    from . import napcat_manager

    api_url, model, headers, cap = _resolve_llm_backend(CONFIG)

    try:
        db_path = CONFIG["DB_PATH"]
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    except Exception:
        db_size = 0

    return {
        "version": __version__,
        "started_at": _STARTED_AT,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "pid": os.getpid(),
        "listen": f"{CONFIG['LISTEN_HOST']}:{CONFIG['LISTEN_PORT']}",
        "bot_qq": get_bot_uin(),  # 08-22：从连接派生（CONFIG 兜底）
        "napcat": {
            "connected": _active_websocket is not None,
            **_napcat_status_detail(),
            # 版本信息（napcat_manager 内 1 小时缓存，GUI 版本行用）
            **napcat_manager.version_info(),
            # 注销进行中（08-23 竞态修复：GUI 禁用刷新/注销按钮，
            # 进程重启后靠 2s 轮询恢复禁用态）
            "logout_in_progress": napcat_manager.logout_in_progress(),
        },
        "llm": {
            "enabled": bool(CONFIG.get("LLM_ENABLED", True)),
            "backend": "remote" if (str(CONFIG.get("LLM_BACKEND", "remote")).lower() == "remote" and CONFIG.get("REMOTE_API_KEY")) else "local",
            "api": api_url,
            "model": model,
            "max_tokens_cap": cap,
            # 健康状态：最近一次真实调用/连接测试的结果（GUI 状态灯，08-21）
            **llm_health_snapshot(),
        },
        "comfyui_url": CONFIG.get("COMFYUI_URL", ""),
        "archive": {
            "base_dir": CONFIG["ARCHIVE_BASE_DIR"],
            "save_recall_messages": CONFIG["SAVE_RECALL_MESSAGES"],
            "save_recall_images": CONFIG["SAVE_RECALL_IMAGES"],
        },
        "db_size_bytes": db_size,
        "data_dir": os.path.dirname(CONFIG["DB_PATH"]),
        # 2026-08-22 任务列表：当前正在进行/排队中的后台任务（GUI 总览页面板）
        "tasks": _get_task_snapshot(),
    }


# ============================================================
#  重启请求
# ============================================================
_restart_requested = False


def request_restart():
    global _restart_requested
    _restart_requested = True


def is_restart_requested() -> bool:
    return _restart_requested


# ============================================================
#  测试连接
# ============================================================
async def test_llm() -> dict:
    """用当前生效配置发一次最小 LLM 调用。"""
    from .llm import _post_llm_chat, _resolve_llm_backend, update_health
    import httpx
    import time

    api_url, model, headers, cap = _resolve_llm_backend(CONFIG)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await _post_llm_chat(
                client, api_url, model, headers,
                [{"role": "user", "content": "请用一句简短的中文回复，确认你现在可以正常工作。"}],
                temperature=0.1, max_tokens=64,
            )
        reply = (res.get("content") or res.get("reasoning") or "").strip() or "(空回复)"
        # 健康状态：连接测试成功（GUI 状态灯，08-21）
        update_health("ok", source="连接测试")
        # 测试调用也计入用量 + 最近请求（真实消耗了 token，GUI 点测试后立即可见）
        try:
            from . import llm_usage
            if res.get("usage"):
                await llm_usage.record_usage(res["usage"], res.get("model") or model)
            llm_usage.record_request(
                res.get("model") or model, reply, source="连接测试",
                finish_reason=res.get("finish_reason", ""))
        except Exception:
            pass
        return {"ok": True, "model": res.get("model") or model, "reply": reply[:200],
                "elapsed": round(time.time() - t0, 1),
                "usage": res.get("usage") or {}}
    except Exception as e:
        update_health("fail", source="连接测试",
                      error=f"{type(e).__name__}: {e}")
        return {"ok": False, "model": model, "error": f"{type(e).__name__}: {e}",
                "elapsed": round(time.time() - t0, 1)}


async def test_comfyui(url: Optional[str] = None) -> dict:
    import urllib.request

    # 2026-08-22：可选 url 覆盖（GUI 总览页配置面板「🔌 测试」用当前表单地址探活，
    # 不写盘、不动 bot 内存配置）；不传则用 bot 内存的 COMFYUI_URL
    if url:
        url = url.strip()
    else:
        url = CONFIG.get("COMFYUI_URL", "")
    if not url:
        return {"ok": False, "error": "comfyui.url 未配置"}
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/system_stats")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return {"ok": True, "url": url, "stats_keys": list(data.keys())[:10]}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}


# ============================================================
#  NapCat 登录态（GUI 登录卡片：二维码 + 刷新 + 控制台链接）
#  实现下沉到 core/napcat_manager.py（平台抽象层：docker / win / off）
# ============================================================

def napcat_status() -> dict:
    from . import napcat_manager
    return napcat_manager.status()


async def _h_napcat(request):
    return web.json_response(napcat_status())


async def _h_napcat_restart(request):
    """重启 NapCat 刷新二维码（仅未登录时允许，避免打断扫码/token 写入）。"""
    from . import napcat_manager
    r = napcat_manager.restart()
    if r.get("ok"):
        _get_logger().info(f"🔄 控制API: {r.get('message', '')}，约 10 秒后可拉取新码")
    return web.json_response(r)


async def _h_napcat_logout(request):
    """注销 NapCat 登录（GUI「注销」按钮）：全清凭证 + 出新二维码。

    2026-08-23：logout 是同步阻塞流程（stop 容器 + 备份/清 2.7G 数据 +
    start 容器，约 60s）——直接同步调会卡死整个事件循环（WS 收发消息、
    定时任务、GUI 轮询全停），必须丢线程池跑。
    """
    from . import napcat_manager
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, napcat_manager.logout)
    _get_logger().info(f"👋 控制API: NapCat 注销 {'成功' if r.get('ok') else '失败: ' + str(r.get('error'))}")
    return web.json_response(r)


# ============================================================
#  热重载
# ============================================================
def reload_resources(what: str) -> dict:
    """重载文件型资源（GUI 改 assets.* 路径 / 游戏题库后调用）。

    2026-08-21：新增 truth_dare / spy / turtle_soup 三项（游戏管理页
    题库落盘后热重载，bot 未运行时 GUI 侧文件照样写盘、启动后生效）。
    """
    result = {}
    if what in ("sensitive_words", "all"):
        from . import content_filter
        n = content_filter.reload_custom_words()
        result["sensitive_words"] = f"已重载，自定义词 {n} 条"
    if what in ("pun_question_bank", "all"):
        import games.pun_game as pun_game
        n = pun_game.reload_question_bank()
        result["pun_question_bank"] = f"已重载，有效题目 {n} 道"
    if what in ("truth_dare", "all"):
        from games import question_pool
        n = question_pool.reload_question_bank()
        result["truth_dare"] = f"已重载，真心话 {n['truth']} / 大冒险 {n['dare']}"
    if what in ("spy", "all"):
        import games.game_spy as game_spy
        n = game_spy.reload_wordbank()
        result["spy"] = f"已重载，词对 {n} 组"
    if what in ("turtle_soup", "all"):
        import games.turtle_soup as turtle_soup
        n = turtle_soup.reload_soup_bank()
        result["turtle_soup"] = f"已重载，海龟汤 {n} 题"
    return result


# ============================================================
#  Handler 注册（bot.py 在事件循环内调用 start_control_api()）
# ============================================================
_STARTED_AT = time.time()


async def _h_status(request):
    data = get_status()
    # 2026-08-23：待扫码状态（QQ 登录态失效挂二维码）——未连接时才检测
    # （10 秒缓存，GUI 2s 轮询不打爆 docker logs subprocess）
    nap = data.get("napcat") or {}
    if not nap.get("connected"):
        try:
            from . import napcat_watchdog
            nap["scan_pending"] = await napcat_watchdog.awaiting_login_scan()
        except Exception:
            nap["scan_pending"] = False
    return web.json_response(data)


async def _h_config_get(request):
    return web.json_response(_redact_config())


async def _h_config_post(request):
    report = load_config(verbose=True)
    _get_logger().info(f"📝 控制API: 配置重载完成 applied={len(report['applied'])} restart={report['restart_required']}")
    return web.json_response(report)


async def _h_reload(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    what = data.get("what", "all")
    result = reload_resources(what)
    return web.json_response({"ok": True, "result": result})


async def _h_test_llm(request):
    result = await test_llm()
    return web.json_response(result)


async def _h_test_comfyui(request):
    # 可选 body {url}：GUI 配置面板用表单地址探活（08-22）
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = await test_comfyui(url=body.get("url"))
    return web.json_response(result)


async def _h_llm_usage(request):
    """GET /llm/usage — LLM 用量统计（token 消耗，按日/模型分桶）。"""
    from . import llm_usage
    return web.json_response(llm_usage.get_usage())


async def _h_llm_recent(request):
    """GET /llm/recent — LLM 用量统计（最近一次请求摘要，GUI 总览页"最近请求"行）。"""
    from . import llm_usage
    return web.json_response(llm_usage.get_recent_request())


async def _h_forward_refetch(request):
    """POST /forward/refetch — 重新拉取一条转发存档（GUI「重试拉取」按钮，08-23）。

    背景：HTTP 通道挂死/未启用时，当时拉取失败的转发会留 failed 行
    （content_json 为空）。forward_id 在 QQ 服务器长期有效，通道恢复
    （或走 WS 反向兜底）后随时可救回——历史 failed 记录不再是终态。

    body {forward_id, message_id, message_type, target_id, user_id, nickname}
    走 archive.archive_forward（自带 ok 行去重；failed 行会重新拉取并
    写新行，GUI 按 fetched_at DESC 取最新）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    fwd_id = str(body.get("forward_id", ""))
    if not fwd_id:
        return web.json_response({"ok": False, "error": "缺少 forward_id"},
                                 status=400)
    from . import archive
    try:
        await archive.archive_forward(
            message_id=int(body.get("message_id") or 0),
            message_type=str(body.get("message_type") or "group"),
            target_id=int(body.get("target_id") or 0),
            user_id=int(body.get("user_id") or 0),
            nickname=str(body.get("nickname") or ""),
            forward_id=fwd_id,
        )
    except Exception as e:
        return web.json_response({"ok": False,
                                  "error": f"{type(e).__name__}: {str(e)[:200]}"})
    # 拉取结果以库里最新行为准（ok/failed 由 archive_forward 落库）
    try:
        from .config import CONFIG as _CFG
        from core.database import get_db as _get_db
        with _get_db() as conn:
            row = conn.execute(
                "SELECT status, msg_count, fetched_at FROM forward_archive "
                "WHERE forward_id = ? ORDER BY fetched_at DESC LIMIT 1",
                (fwd_id,)).fetchone()
        return web.json_response({
            "ok": True,
            "status": row[0] if row else "unknown",
            "msg_count": row[1] if row else 0,
            "fetched_at": row[2] if row else 0,
        })
    except Exception as e:
        return web.json_response({"ok": True, "status": "unknown",
                                  "error": f"状态回读失败: {e}"})


async def _h_test_forward(request):
    """POST /test/forward — WS 反向通道拉取转发（诊断用，08-23 转正）。

    body {id}: forward_id。直接走 WS 反向 action get_forward_msg
    （不经过 NapCat HTTP）。返回子消息数 + 前 10 条预览。
    与 /forward/refetch 的区别：本端点只读不落库（诊断），
    refetch 走完整 archive 链路（落库 + 图片归档）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    fwd_id = str(body.get("id", ""))
    if not fwd_id:
        return web.json_response({"ok": False, "error": "缺少 id"})
    from . import sender
    try:
        resp = await sender.request_action("get_forward_msg", {"id": fwd_id},
                                           timeout=30)
    except Exception as e:
        return web.json_response({"ok": False,
                                  "error": f"{type(e).__name__}: {str(e)[:200]}"})
    if resp.get("status") != "ok":
        return web.json_response({"ok": False, "retcode": resp.get("retcode"),
                                  "msg": str(resp.get("msg", ""))[:200]})
    messages = (resp.get("data") or {}).get("messages", [])
    preview = []
    for m in messages[:10]:
        s = m.get("sender", {}) or {}
        msg = m.get("message", "")
        if isinstance(msg, list):
            text = "".join(seg.get("data", {}).get("text", "")
                           for seg in msg if seg.get("type") == "text")
        else:
            text = str(msg)
        # 08-23：WS 反向返回的 nickname 是 "QQ用户" 占位 → 回落 QQ 号
        nick = s.get("nickname", "")
        nick = nick if nick not in ("", "QQ用户") else str(m.get("user_id", ""))
        preview.append({"nickname": nick, "user_id": m.get("user_id"),
                        "time": m.get("time"), "text": text[:100]})
    return web.json_response({"ok": True, "msg_count": len(messages),
                              "preview": preview})


# ============================================================
#  题库 LLM 重新生成（GUI 真心话大冒险页，2026-08-21）
#  题库读/改/删由 GUI 直连 SQLite（api_client.query kind='truth_dare'），
#  仅 LLM 生成走此路由（需 bot 内嵌的 LLM 队列/人设数据）。
# ============================================================
async def _h_questions_regen(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    from games import question_pool
    try:
        result = question_pool.start_regen(
            user_id=int(data.get("user_id", 0)),
            group_id=int(data.get("group_id", 0)),
            question_type=str(data.get("question_type", "truth")),
            spiciness=int(data.get("spiciness", 4)),
            source="generic" if data.get("source") == "generic" else "persona",
        )
    except Exception as e:
        result = {"ok": False, "error": str(e)[:300]}
    return web.json_response(result)


async def _h_questions_regen_status(request):
    from games import question_pool
    return web.json_response({"tasks": question_pool.get_regen_status()})


# ============================================================
#  消息分析（GUI 消息管理页，2026-08-21）
# ============================================================
# 任务表（bot 进程内存）：run_id → {state, progress, answer, error, started_at}
# 任务在 bot 事件循环后台跑，GUI 重启/关标签页不影响（不做续查：
# 任务跑完答案在 query_batch_results 审计表里可追溯，GUI 侧状态丢失只影响展示）
_analysis_tasks: dict = {}


async def _run_analysis_task(run_id: int, data: dict):
    """后台任务：执行 Map-Reduce 分析，写任务表状态。"""
    from .analysis import run_query_analysis, AnalysisError

    question = data["question"]
    rows = data["rows"]
    meta = data.get("meta") or {}
    scope_desc = meta.get("scope_desc") or "上方筛选出的聊天记录"

    def _set(**kw):
        t = _analysis_tasks.get(run_id)
        if t is not None:
            t.update(kw)

    try:
        async def _progress(done: int, total: int, stage: str):
            _set(progress=f"{stage}:{done}/{total}")

        summary, total_batches = await run_query_analysis(
            question, rows,
            scope_desc=scope_desc,
            source="gui",
            group_id=int(meta.get("group_id") or 0),
            hours=int(meta.get("hours") or 0),
            progress_cb=_progress,
            run_id=run_id,   # 任务 run_id == 审计 run_id（进度兜底可查）
        )
        _set(state="done", answer=summary, total_batches=total_batches,
             progress=f"reduce:{total_batches}/{total_batches}")
    except AnalysisError as e:
        # "未找到相关信息/LLM 全失败"算正常完成（非异常路径）
        _set(state="done", answer="", error=str(e), total_batches=0)
    except Exception as e:
        _get_logger().error(f"消息分析任务失败: {e}", exc_info=True)
        _set(state="error", error=f"{type(e).__name__}: {e}")


def _analysis_progress(run_id: int) -> str:
    """进度文案：优先任务表实时进度；任务表无（bot 刚重启）时按审计表已落库
    Map 批次数兜底。返回 'stage:done/total'。"""
    t = _analysis_tasks.get(run_id)
    if t is not None:
        return t.get("progress") or "map:0/0"
    try:
        from .database import get_daily_reports_db
        with get_daily_reports_db() as db:
            r = db.execute(
                "SELECT COUNT(*) AS done, MAX(total_batches) AS total "
                "FROM query_batch_results WHERE run_id=? AND stage='map' AND source='gui'",
                (run_id,)).fetchone()
            if r and r["total"]:
                return f"map:{r['done']}/{r['total']}"
    except Exception:
        pass
    return "map:0/0"


async def _h_analysis_query(request):
    try:
        return await _do_analysis_query(request)
    except Exception as e:
        _get_logger().error(f"消息分析请求处理失败: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": f"服务器内部错误: {e}"},
                                 status=500)


async def _do_analysis_query(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    question = str(data.get("question") or "").strip()
    rows = data.get("rows") or []
    if not question:
        return web.json_response({"ok": False, "error": "问题不能为空"})
    if not rows:
        return web.json_response({"ok": False, "error": "没有可分析的聊天记录"})
    # 行结构校验 + 规范化（防 GUI 传脏数据进 LLM）。非 dict 行/字段类型错
    # 的行直接跳过（08-21 e2e 抓到：裸 int 混入时 .get 抛 AttributeError → 500）
    norm_rows = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            norm_rows.append({
                "user_id": int(r.get("user_id") or 0),
                "nickname": str(r.get("nickname") or ""),
                "content": str(r.get("content") or ""),
                "created_at": float(r.get("created_at") or 0),
            })
        except (TypeError, ValueError):
            continue
    if not norm_rows:
        return web.json_response({"ok": False, "error": "聊天记录格式无效"})
    # 上限裁剪（GUI 已确认取最近 N 条，这里兜底再裁一次防绕过）
    max_rows = int(data.get("max_rows") or 0)
    if max_rows > 0 and len(norm_rows) > max_rows:
        norm_rows = norm_rows[-max_rows:]   # 输入已 ASC，尾部=最近
    # 并发限制：最多 1 个 running 任务
    if any(v["state"] == "running" for v in _analysis_tasks.values()):
        return web.json_response({"ok": False, "error": "已有分析任务进行中，请稍候"})
    run_id = int(time.time() * 1000)
    _analysis_tasks[run_id] = {
        "state": "running", "progress": "map:0/0", "answer": "", "error": "",
        "started_at": time.time(), "total": len(norm_rows), "question": question,
    }
    # 任务表瘦身：保留最近 10 个
    if len(_analysis_tasks) > 10:
        for k in sorted(_analysis_tasks, reverse=True)[10:]:
            _analysis_tasks.pop(k, None)
    asyncio.create_task(_run_analysis_task(run_id, {"question": question,
                                                     "rows": norm_rows,
                                                     "meta": data.get("meta") or {}}))
    _get_logger().info(f"📊 消息分析任务启动 run_id={run_id} 共 {len(norm_rows)} 条")
    return web.json_response({"ok": True, "run_id": run_id, "total": len(norm_rows)})


async def _h_analysis_query_status(request):
    # run_id 在任务表是 int，query 参数是 str——必须转换（08-21 e2e 抓到：
    # 不转换则 dict.get 永远 None，所有状态查询都报"任务不存在"）
    try:
        run_id = int(request.query.get("run_id", ""))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "run_id 无效"})
    t = _analysis_tasks.get(run_id)
    if t is None:
        return web.json_response({"ok": False, "error": "任务不存在（bot 可能已重启）"})
    resp = {
        "ok": True, "state": t["state"], "error": t.get("error", ""),
        "answer": t.get("answer", ""), "total_batches": t.get("total_batches", 0),
        "total": t.get("total", 0),
    }
    if t["state"] == "running":
        resp["progress"] = _analysis_progress(run_id)
    return web.json_response(resp)


# ============================================================
#  角色扮演房间清理（GUI 角色扮演页，2026-08-22）
#  房间数据由 bot 实时读写（group_roleplay.db 未开 WAL），
#  GUI 写操作必须走此路由在 bot 进程内执行级联删除，
#  避免 GUI 直写与 bot 实时写抢锁。仅允许清理 state='ended'
#  的房间（进行中/待报名房间 bot 可能正在写）。
# ============================================================
async def _h_roleplay_cleanup(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    from games import group_roleplay
    room_id = data.get("room_id")
    try:
        room_id = int(room_id) if room_id is not None else None
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "room_id 无效"})
    try:
        if room_id is None:
            # 清理全部已结束房间
            with group_roleplay.get_rp_db() as conn:
                ids = [r[0] for r in conn.execute(
                    "SELECT room_id FROM rp_rooms WHERE state = 'ended'").fetchall()]
        else:
            room = group_roleplay.get_room(room_id)
            if room is None:
                return web.json_response({"ok": False, "error": "房间不存在"})
            if room.get("state") != "ended":
                return web.json_response(
                    {"ok": False,
                     "error": f"房间状态为 {room.get('state')}（仅已结束房间可清理）"})
            ids = [room_id]
        removed = 0
        total_story = 0
        for rid in ids:
            total_story += group_roleplay.cleanup_room(rid)
            removed += 1
        _get_logger().info(f"🧹 控制API: 角色扮演清理 {removed} 个房间（剧情 {total_story} 条）")
        return web.json_response({"ok": True, "removed": removed, "story_removed": total_story})
    except Exception as e:
        _get_logger().error(f"角色扮演清理失败: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                 status=500)


async def _h_restart(request):
    request_restart()
    _get_logger().warning("🛑 控制API: 收到重启请求，将在当前消息处理后退出")
    return web.json_response({"ok": True, "message": "重启请求已受理"})


async def start_control_api():
    """启动控制 API（在 bot 主事件循环内 await 此协程即可；返回 app 供测试）。"""
    global _runner
    # 2026-08-23：GUI 轮询降噪——200 轮询不落盘，只记非 200 和写操作
    _setup_access_log_filter()
    app = web.Application()
    app.router.add_get("/status", _h_status)
    app.router.add_get("/config", _h_config_get)
    app.router.add_post("/config", _h_config_post)
    app.router.add_post("/reload", _h_reload)
    app.router.add_post("/test/llm", _h_test_llm)
    app.router.add_post("/test/comfyui", _h_test_comfyui)
    app.router.add_get("/llm/usage", _h_llm_usage)
    app.router.add_get("/llm/recent", _h_llm_recent)
    app.router.add_post("/questions/regenerate", _h_questions_regen)
    app.router.add_get("/questions/regen_status", _h_questions_regen_status)
    app.router.add_post("/analysis/query", _h_analysis_query)
    app.router.add_get("/analysis/query/status", _h_analysis_query_status)
    app.router.add_post("/roleplay/cleanup", _h_roleplay_cleanup)
    app.router.add_post("/tasks/pause", _h_tasks_pause)
    app.router.add_post("/tasks/resume", _h_tasks_resume)
    app.router.add_get("/napcat", _h_napcat)
    app.router.add_post("/napcat/restart", _h_napcat_restart)
    app.router.add_post("/napcat/logout", _h_napcat_logout)
    app.router.add_post("/test/forward", _h_test_forward)
    app.router.add_post("/forward/refetch", _h_forward_refetch)
    app.router.add_post("/restart", _h_restart)
    runner = web.AppRunner(app)
    await runner.setup()
    host = CONFIG.get("CONTROL_API_HOST", "127.0.0.1")
    port = int(CONFIG.get("CONTROL_API_PORT", 8697))
    site = web.TCPSite(runner, host, port)
    await site.start()
    _get_logger().info(f"🎛️ 控制 API 已启动: http://{host}:{port} （仅本机回环，GUI 专用）")
    _runner = runner
    return app


_runner = None


async def stop_control_api():
    """关闭控制 API（bot 退出时调用）。"""
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
        _get_logger().info("🎛️ 控制 API 已停止")
