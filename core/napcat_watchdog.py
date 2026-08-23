#!/usr/bin/env python3
"""
napcat_watchdog.py — NapCat HTTP 服务健康守护（2026-08-22）
============================================================
背景（08-22 故障复盘）：NapCat 存在「半死」状态——QQ 客户端进程还活着
（消息照常收），但 OneBot HTTP 服务组件已死（3000 端口由 docker-proxy
挂着，TCP 连接建立后立即断开，所有 API 调用
"Server disconnected without sending a response"）。容器无 crash、无
OOM、无告警，静默丢失转发存档/成员获取等依赖 HTTP 服务的数据。

本模块在 bot 主事件循环内跑一个轻量协程：
  - 每 NAPCAT_WATCHDOG_INTERVAL（默认 60s）GET NapCat HTTP 的
    /get_login_info（最便宜的存活探针：200 + status=ok 才算健康）
  - 连续 NAPCAT_WATCHDOG_THRESHOLD（默认 3）次失败 → WARNING 告警
  - NAPCAT_WATCHDOG_AUTO_RESTART=True（默认 False）→ docker restart
    NapCat 容器（走 napcat_manager.restart(force=True)，绕过
    「已连接无需重启」守卫——半死时 WS 可能还挂着但 HTTP 已死），
    并轮询等待恢复（上限 600s）；恢复/失败各记 INFO/ERROR
  - 重启冷却 NAPCAT_WATCHDOG_COOLDOWN（默认 1800s）：防止「重启后
    仍半死」造成无限重启循环
  - 每次 tick 实时读 CONFIG → /config 热重载立即生效，无需重启 bot

配置键（config.yaml napcat.* 段，flatten 后）：
  NAPCAT_WATCHDOG_INTERVAL / _THRESHOLD / _COOLDOWN / _AUTO_RESTART
  在 GUI「其他设置」弹窗「NapCat 守护（3 项）」管理（auto_restart 开关
  + 间隔 + 阈值）。

设计取舍：
  - 只探 HTTP（3000），不探 QQ 客户端：半死态正是「QQ 活 + HTTP 死」，
    探 QQ 客户端（无独立端点）反而探不出
  - 失败计数不区分「容器未运行」和「服务挂死」：容器没跑时 3000
    同样连接即断/拒绝，统一走告警（+可选重启）是正确行为
  - 手动关闭 NapCat（docker stop）的场景：auto_restart 默认关，
    只告警不重启；开了自动重启的话，手动关闭后 watchdog 会在一个
    阈值周期后把它拉起来——这是该开关的明确语义（守护=保活）
"""
import asyncio
import logging
import os
import time

logger = logging.getLogger("qq-bot")

# 默认值（config.py DEFAULTS 同源，此处为读取兜底）
_DEFAULT_INTERVAL = 60        # 探活间隔（秒）
_DEFAULT_THRESHOLD = 3        # 连续失败几次判定不健康
_DEFAULT_COOLDOWN = 1800      # 自动重启冷却（秒）
_DEFAULT_AUTO_RESTART = False  # 自动重启开关（默认关：只告警不擅自动手）
_RECOVER_WAIT = 600           # 重启后等待恢复上限（秒）


def _cfg(key: str, default):
    from .config import CONFIG
    return CONFIG.get(key, default)


async def check_http_healthy() -> tuple[bool, str]:
    """探活 NapCat HTTP 服务（/get_login_info）。

    返回 (healthy, detail)。healthy=True 仅当 200 + JSON status=ok。
    本函数独立可复用（archive 预检也用）。
    """
    import httpx
    from .config import CONFIG
    napcat_http = CONFIG.get("NAPCAT_HTTP") or \
        f"http://127.0.0.1:{int(CONFIG.get('NAPCAT_ONEBOT_HTTP_PORT', 3000))}"
    url = f"{napcat_http}/get_login_info"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        data = resp.json()
        if data.get("status") != "ok":
            return False, f"status={data.get('status')} retcode={data.get('retcode')}"
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


async def wait_healthy(timeout: float = _RECOVER_WAIT,
                       poll: float = 10.0) -> tuple[bool, float]:
    """轮询等待 HTTP 服务恢复（重启后调用）。返回 (恢复?, 耗时秒)。"""
    start = time.time()
    while time.time() - start < timeout:
        healthy, _ = await check_http_healthy()
        if healthy:
            return True, round(time.time() - start, 1)
        await asyncio.sleep(poll)
    return False, round(time.time() - start, 1)


_last_restart_at = 0.0  # 模块级共享冷却状态（watchdog 循环 + archive 预检共用）

# 2026-08-23：状态落盘（bot 重启不清零）
#   14:51 事故链：14:26 watchdog 杀容器 → 14:48 bot 重启 →
#   _last_restart_at 归零 → 冷却记忆丢失 → 14:51 再次下手误杀。
#   冷却/上次重启时间写 data/napcat_watchdog_state.json（与 DB 同目录，
#   绝对路径），启动时恢复、每次重启后原子写回。
_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "napcat_watchdog_state.json")


def _load_state() -> None:
    global _last_restart_at
    try:
        import json as _json
        with open(_STATE_FILE, encoding="utf-8") as f:
            _last_restart_at = float(_json.load(f).get("last_restart_at", 0.0))
    except Exception:
        pass  # 文件缺失/损坏 = 首次运行，保持 0


def _save_state() -> None:
    try:
        import json as _json
        d = os.path.dirname(_STATE_FILE)
        os.makedirs(d, exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"last_restart_at": _last_restart_at}, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        logger.debug(f"watchdog 状态落盘失败（不影响守护）: {e}")


_load_state()

# 2026-08-23：待扫码状态检测（08-23 故障复盘）
#   根因：QQ 踢登录态后 NapCat 挂二维码等人工扫，此时 HTTP 探活同样失败，
#   watchdog 误判"HTTP 半死"→ 反复 docker restart → 每次重启刷新二维码
#   （更难扫）+ 切断 WS——帮倒忙的死循环。修复：检测到"待扫码"时
#   自动重启无效（重启救不了登录态），只告警引导扫码。
_SCAN_MARKERS = ("请扫描下面的二维码", "登录态已失效")
_scan_cache = (0.0, False)  # (检测时刻, 结果) 10 秒缓存（GUI 轮询 + watchdog 共用）
_scan_why = ""  # 最近一次真实检测的命中依据（判定日志取证用，08-23）


async def awaiting_login_scan() -> bool:
    """检测 NapCat 是否处于「待扫码」状态（QQ 登录态失效，等人工扫码）。

    2026-08-23 加固（14:51 误杀复盘：marker 在窗口内却判了 False，
    走了静默异常路径/缓存污染，且无任何日志可取证）：
      1) 双信号 OR——容器日志 marker **或** 二维码文件 mtime 新鲜
         （docker exec stat /app/napcat/cache/qrcode.png，10s 超时）；
         码文件由 NapCat 每次刷码时生成，比日志行数更稳
      2) 失败保守化——两路信号都**读不到**（docker 不可用/超时）→
         返回 True（宁可漏杀一次，不可误杀一次：误杀直接作废用户
         正在扫的码），并打 WARNING（此前静默 False 导致事后无据）
         读到日志/码文件但**无信号**（容器活着且已登录/无码）→ False
      3) 日志窗口 --tail 200 → --since 5m（实测启动期 dbus/EGL 噪音
         可把 marker 挤出行数窗口；5 分钟时间窗盖住 2 分钟刷码周期）
    带 10 秒缓存：本函数在 request_restart 热路径 + /status 构建里被
    调用（GUI 2s 轮询），docker subprocess 不能每 2 秒跑一次。
    """
    global _scan_cache, _scan_why
    from .config import CONFIG
    now = time.time()
    if now - _scan_cache[0] < 10:
        return _scan_cache[1]
    container = CONFIG.get("NAPCAT_CONTAINER") or "napcat"
    try:
        loop = asyncio.get_running_loop()
        rc, out = await loop.run_in_executor(
            None,
            lambda: _run_docker_logs(container))
        logs_readable = (rc == 0)
        marker_hit = logs_readable and any(m in out for m in _SCAN_MARKERS)
    except Exception:
        logs_readable = False
        marker_hit = False
    # 信号 2：二维码文件 mtime（独立 docker exec，失败只降级不判 False）
    qr_fresh = False
    try:
        loop = asyncio.get_running_loop()
        qr_fresh = await loop.run_in_executor(
            None, lambda: _qrcode_fresh(container))
    except Exception:
        qr_fresh = False
    if marker_hit or qr_fresh:
        hit = True
        why = "日志marker" if marker_hit else "码文件新鲜"
    elif not logs_readable and not qr_fresh:
        # 两路都读不到 = docker 本身异常：保守放行（不拦截重启），
        # 但打日志（此前静默 False 是 14:51 误杀无法取证的根因）
        hit = True
        why = "检测失败(保守放行)"
        logger.warning(f"⚠️ NapCat 待扫码检测失败（docker 日志/码文件均不可读）"
                       f"——保守视为待扫码，跳过自动重启判定")
    else:
        hit = False
        why = "无信号"
    _scan_cache = (now, hit)
    _scan_why = why
    if hit:
        logger.debug(f"🐱 待扫码检测命中（{why}）")
    return hit


def _run_docker_logs(container: str) -> tuple[int, str]:
    """docker logs <container> --since 5m（同步，供 run_in_executor 调用）。

    08-23 由 --tail 200 改 --since 5m：行数窗口会被 QQ 客户端启动期的
    dbus/EGL 噪音（实测 1 分钟 ~45 行）挤掉 marker；时间窗口不受行数影响，
    且盖住 2 分钟刷码周期。保留 6000 字符尾部截取（防意外巨量输出）。
    """
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "logs", container, "--since", "5m"],
            capture_output=True, text=True, timeout=15)
        # NapCat 日志打 stdout（2026-08-23 实测：stderr 恒空），两者都看
        return (r.returncode, ((r.stdout or "") + "\n" + (r.stderr or ""))[-6000:])
    except Exception as e:
        return 1, f"{type(e).__name__}: {str(e)[:200]}"


def _qrcode_fresh(container: str, max_age: int = 300) -> bool:
    """容器内二维码文件 mtime 是否 < max_age 秒（待扫码第二信号）。

    NapCat 未登录时每 ~2 分钟刷码一次，刷码 = 写
    /app/napcat/cache/qrcode.png（status() 拉码用的同一路径）。
    已登录时不刷码 → mtime 超龄 → 不误判。docker 不可用/文件不存在
    → False（只降级，不误判；主信号是日志 marker）。
    """
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             "stat -c %Y /app/napcat/cache/qrcode.png 2>/dev/null"],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False
        mtime = float((r.stdout or "").strip())
        return (time.time() - mtime) < max_age
    except Exception:
        return False


# 启动宽限期（08-23：14:51 误杀时容器刚被手动刷新拉起 175s，QQ 栈 +
# HTTP 服务还在启动中，探活失败被累计成"挂死"）。刚重启的容器（无论
# 谁发起的）在此窗口内探活失败不累计、fail_streak 清零——启动失败
# 最坏情况 = 宽限期后按正常阈值走。
_START_GRACE = 180
_uptime_cache = (0.0, -1.0)  # (检测时刻, uptime 秒；-1=读不到)


def _container_uptime_sync() -> float:
    """容器启动时长（秒）；读不到 → -1（不应用宽限期）。"""
    import subprocess
    from datetime import datetime
    from .config import CONFIG
    container = CONFIG.get("NAPCAT_CONTAINER") or "napcat"
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return -1.0
        s = (r.stdout or "").strip()
        started = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return time.time() - started.timestamp()
    except Exception:
        return -1.0


async def _container_uptime() -> float:
    """带 10 秒缓存（watchdog 每 tick 最多一次 docker inspect）。"""
    global _uptime_cache
    now = time.time()
    if now - _uptime_cache[0] < 10:
        return _uptime_cache[1]
    loop = asyncio.get_running_loop()
    val = await loop.run_in_executor(None, _container_uptime_sync)
    _uptime_cache = (now, val)
    return val


async def trigger_restart() -> dict:
    """触发 NapCat 容器重启（force：绕过 WS 连接守卫）。

    在独立线程跑（docker CLI 是同步 subprocess），不阻塞事件循环。
    """
    from . import napcat_manager
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: napcat_manager.restart(force=True))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def request_restart() -> dict:
    """统一自动重启入口（共享冷却，防 watchdog/archive 预检同时重启）。

    返回 {"ok": bool, "restarted": bool, "error"/"message": str}。
    冷却期内调用不执行重启（restarted=False）。

    2026-08-23：待扫码守卫——QQ 登录态失效（挂二维码等人工扫）时
    docker restart 救不了登录态，只会刷新二维码（更难扫）+ 切断 WS，
    属帮倒忙。此时拦截自动重启，改走扫码引导（GUI 状态/日志提示）。
    手动路径（GUI「刷新二维码」走 napcat_manager.restart(force=False)）
    不受本守卫影响——用户显式操作优先。
    """
    global _last_restart_at
    # 待扫码 → 自动重启无效，拦截（仅拦截自动路径，手动不受限）
    if await awaiting_login_scan():
        return {"ok": False, "restarted": False,
                "error": "登录态失效待扫码（自动重启无效，请在 GUI 扫码）"}
    cooldown = _cfg("NAPCAT_WATCHDOG_COOLDOWN", _DEFAULT_COOLDOWN)
    now = time.time()
    if now - _last_restart_at < cooldown:
        return {"ok": False, "restarted": False, "error": "处于重启冷却期"}
    r = await trigger_restart()
    if r.get("ok"):
        _last_restart_at = now
        _save_state()  # 08-23：bot 重启不清零冷却记忆（14:51 事故）
        return {"ok": True, "restarted": True, "message": r.get("message", "")}
    return {"ok": False, "restarted": False, "error": r.get("error", "")}


async def watchdog_loop() -> None:
    """主守护协程（bot 主循环内 create_task）。永不主动退出。

    失败计数：每次探活失败 +1，成功清零。连续达到 threshold 且处于
    冷却期外（request_restart 内部判定）→ 告警（+可选自动重启）。
    """
    interval = _cfg("NAPCAT_WATCHDOG_INTERVAL", _DEFAULT_INTERVAL)
    threshold = _cfg("NAPCAT_WATCHDOG_THRESHOLD", _DEFAULT_THRESHOLD)
    logger.info(
        f"🐱 NapCat 守护已启动: 间隔 {interval}s / 阈值 {threshold} 次 / "
        f"自动重启={'开' if _cfg('NAPCAT_WATCHDOG_AUTO_RESTART', _DEFAULT_AUTO_RESTART) else '关'}"
        f" / 宽限期 {_START_GRACE}s")
    fail_streak = 0
    while True:
        await asyncio.sleep(interval)
        healthy, detail = await check_http_healthy()
        if healthy:
            if fail_streak > 0:
                logger.info(f"🐱 NapCat 守护: HTTP 服务已恢复（此前失败 {fail_streak} 次）")
            fail_streak = 0
            continue
        # 08-23 启动宽限期：刚重启的容器（QQ 栈/HTTP 服务还在拉起中）
        # 探活失败不累计、streak 清零——14:51 误杀时容器才启动 175s
        # （180s 内），无宽限期则 3 次失败直接判"挂死"杀掉正在出码的容器
        uptime = await _container_uptime()
        if uptime >= 0 and uptime < _START_GRACE:
            if fail_streak > 0:
                logger.info(f"🐱 NapCat 守护: 容器启动宽限期内（{int(uptime)}s < "
                            f"{_START_GRACE}s），探活失败不累计（{detail}）")
            fail_streak = 0
            continue
        fail_streak += 1
        if fail_streak < threshold:
            logger.debug(f"🐱 NapCat 守护: HTTP 探活失败 {fail_streak}/{threshold} "
                         f"（{detail}）")
            continue
        # 达到阈值：区分「待扫码」「服务真挂死」「持续恶化」
        # 2026-08-23：待扫码时告警文案完全不同（引导扫码而非"HTTP 挂死"）
        scan_pending = await awaiting_login_scan()
        if fail_streak == threshold:
            if scan_pending:
                logger.warning(
                    f"🚨 NapCat 守护: QQ 登录态失效，正在等待人工扫码"
                    f"（连续 {fail_streak} 次探活失败属预期）。"
                    f"处理: 打开 GUI 总览页 NapCat 卡片「刷新二维码」→ 手Q 扫码。"
                    f"自动重启对登录态失效无效，已自动跳过。")
            else:
                # 08-23 判定依据日志（14:51 误杀无据可查的教训）：
                # 明确记录 scan_pending 判定结果 + 命中依据 + 探活详情，
                # 以后再出误判可直接取证
                logger.warning(
                    f"🚨 NapCat 守护: HTTP 服务连续 {fail_streak} 次探活失败"
                    f"（{detail}）——OneBot HTTP 服务可能已挂死（QQ 客户端可能仍在线，"
                    f"转发存档/成员获取等功能不可用）"
                    f"[判定依据: scan_pending={scan_pending}({_scan_why})]")
        elif fail_streak % (threshold * 4) == 0:
            if scan_pending:
                logger.warning(
                    f"⏳ NapCat 守护: 仍待扫码（已 {fail_streak} 次探活失败）——"
                    f"请尽快 GUI 扫码，bot 当前无法收发消息")
            else:
                logger.warning(f"🚨 NapCat 守护: HTTP 服务仍不健康（已失败 {fail_streak} 次）")
        # 自动重启判定（2026-08-23：待扫码时直接跳过——重启救不了登录态，
        # 此前每个 tick 刷一对「触发自动重启…/未执行重启（待扫码）」噪音日志）
        auto = _cfg("NAPCAT_WATCHDOG_AUTO_RESTART", _DEFAULT_AUTO_RESTART)
        if not auto or scan_pending:
            continue
        logger.warning("🐱 NapCat 守护: 触发自动重启（docker restart）…")
        r = await request_restart()
        if not r.get("restarted"):
            logger.info(f"🐱 NapCat 守护: 未执行重启（{r.get('error')}）")
            continue
        recovered, elapsed = await wait_healthy()
        if recovered:
            logger.info(f"✅ NapCat 守护: 服务已恢复（重启后 {elapsed}s）")
            fail_streak = 0
        else:
            logger.error(
                f"🐱 NapCat 守护: 重启后 {elapsed}s 内仍未恢复——"
                f"可能需要人工介入（检查 QQ 登录态：GUI 刷新二维码）")
            # 不重置 fail_streak：持续失败，进入冷却期后停止重启
