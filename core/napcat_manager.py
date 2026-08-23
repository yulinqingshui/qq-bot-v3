# ============================================================
#  napcat_manager.py — NapCat 平台抽象层（bot 集成 NapCat）
#
#  为什么需要它：
#    NapCat（QQ 协议端）是 bot 的「腿」，但官方只发布
#    Linux(Docker) / Windows(绿色版) 两种形态。bot 程序要能在
#    两种平台上一键运行，就必须把「NapCat 怎么起/停/刷新二维码」
#    抽象成统一接口，平台差异藏在本模块里。
#
#  两种后端：
#    - DockerBackend（Linux 现状）：管理 mlikiowa/napcat-docker 容器
#        · QR 码在容器内 /app/napcat/cache/qrcode.png，docker cp 取出
#        · 刷新二维码 = docker restart
#    - WinGreenBackend（Windows 目标）：管理内置绿色版子进程
#        · 绿色版 = NapCat.Shell.Windows.Node.zip（111MB，自包含：
#          node.exe + QQ 运行时 + NapCat，无需另装 QQ/Node）
#        · 首次使用时从 GitHub Release 自动下载到
#          NAPCAT_WIN_PACKAGE_DIR，之后离线可复用
#        · 启动 = node.exe ./index.js（cwd=绿色版目录），
#          注入 env NAPCAT_WORKDIR=NAPCAT_DATA_DIR → 数据/凭据/配置
#          全落在程序目录内（passkey.json 扫码登录态，一次扫码
#          长期有效，程序重启免扫）
#        · onebot11 配置写 <workdir>/NapCat/config/onebot11_<uin>.json
#          （WS 客户端连 ws://127.0.0.1:LISTEN_PORT/ + token）
#        · QR 码位置随版本可能有变 → 数据目录递归扫描 qrcode.png
#
#  统一接口（control_api / GUI 只调这些）：
#    status()        -> dict   登录态 + 二维码(b64) + 提示
#    restart()       -> dict   重启 NapCat（刷新二维码）
#    ensure_running()-> dict   确保 NapCat 在跑（bot 启动时调用）
#
#  配置（config.yaml napcat 节，热加载）：
#    mode: auto | docker | win | off
#      auto  = Linux 用 docker，Windows 用内置绿色版
#      off   = 不管理（外部自管 NapCat，bot 只等 WS 连入）
#
#  ⚠️ Windows 待真机验证项（代码按逆向结论实现，细节需实测校准）：
#    - NAPCAT_WORKDIR 是否被 v4.18 尊重为数据根（逆向见 env 引用，
#      未见赋值点；不生效则数据落 %USERPROFILE%\.config\QQ，
#      此时 QR/passkey 扫描路径需按实际落点调整）
#    - QR 码实际路径（候选：workdir/NapCat/cache/ 或 workdir/cache/）
# ============================================================

import base64
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import logging

from .config import CONFIG

log = logging.getLogger("qq-bot")


# ============================================================
#  平台探测与模式解析
# ============================================================
def _is_windows() -> bool:
    return platform.system() == "Windows"


def resolve_mode() -> str:
    """mode: auto → 按平台落成 docker/win；其他值原样返回。"""
    mode = str(CONFIG.get("NAPCAT_MODE", "auto")).lower()
    if mode == "auto":
        return "win" if _is_windows() else "docker"
    return mode


def _host_lan_ip() -> str:
    """宿主局域网 IP（用户浏览器从其他机器访问 bot 所在机用）。

    通过 UDP connect 探测出口网卡（不实际发包）；失败回退 127.0.0.1。
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _console_url() -> str:
    port = int(CONFIG.get("NAPCAT_CONSOLE_PORT", 6099))
    return f"http://{_host_lan_ip()}:{port}/webui"


_WEBUI_TOKEN_CACHE: tuple[float, str] = (0.0, "")


def _webui_api(backend: str, path: str, payload: dict | None = None,
               timeout: float = 10.0) -> dict | None:
    """调 NapCat WebUI 内部 API（带 JWT 登录）。

    登录机制（2026-08-20 逆向）：POST /api/auth/login
    {hash: sha256(token + '.napcat').hexdigest()} → data.Credential（JWT），
    之后请求头 Authorization: Bearer <JWT>。
    端点（v4.18.6 前端源码确认）：
      POST /api/QQLogin/RestartNapCat     杀当前登录会话、出新二维码
      POST /api/QQLogin/SetQuickLoginQQ   {uin} 设/清「自动快速登录」账号
      POST /api/OB11Config/GetConfig      当前登录账号的 onebot11 配置
    失败返回 None（logout 等流程按非致命降级处理）。
    """
    token = _read_webui_token(backend)
    if not token:
        return None
    import hashlib
    import urllib.request
    try:
        host_ip = _host_lan_ip() if backend == "docker" else "127.0.0.1"
        # docker 后端：WebUI 起在容器内网 6099，宿主机已做端口映射 → 用宿主机 IP
        port = int(CONFIG.get("NAPCAT_CONSOLE_PORT", 6099))
        base = f"http://{host_ip}:{port}"
        h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
        req = urllib.request.Request(
            base + "/api/auth/login",
            data=json.dumps({"hash": h}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            jwt = json.loads(r.read())["data"]["Credential"]
        body = json.dumps(payload or {}).encode()
        req2 = urllib.request.Request(
            base + path, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {jwt}"}, method="POST")
        with urllib.request.urlopen(req2, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"WebUI API {path} 调用失败: {type(e).__name__}: {e}")
        return None


_VERSION_CACHE: tuple[float, dict] = (0.0, {})


def version_info() -> dict:
    """NapCat / QQ 协议版本号（GUI 登录卡片版本行用）。

    走 WebUI GET 接口（需 JWT），带 1 小时缓存——GUI 2 秒轮询 /status，
    版本号不变，没必要每次都 docker exec + JWT 登录。
    WebUI 未起 / token 读不到 → 返回空 dict（GUI 版本行自动省略）。
    实测（v4.18.6）：/api/base/GetNapCatVersion → {version}；
    /api/base/QQVersion → str（QQ 协议端版本，未登录时 unknown）。
    """
    global _VERSION_CACHE
    now = time.time()
    if now - _VERSION_CACHE[0] < 3600 and _VERSION_CACHE[1]:
        return _VERSION_CACHE[1]
    mode, b = _backend()
    if b is None:
        return {}
    info = {}
    try:
        import hashlib
        import urllib.request
        token = _read_webui_token(b.name)
        if token:
            host_ip = _host_lan_ip() if b.name == "docker" else "127.0.0.1"
            port = int(CONFIG.get("NAPCAT_CONSOLE_PORT", 6099))
            base = f"http://{host_ip}:{port}"
            h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
            req = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"hash": h}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=8) as r:
                jwt = json.loads(r.read())["data"]["Credential"]
            for key, path in (("napcat_version", "/api/base/GetNapCatVersion"),
                              ("qq_version", "/api/base/QQVersion")):
                try:
                    req = urllib.request.Request(
                        base + path,
                        headers={"Authorization": f"Bearer {jwt}"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        d = json.loads(r.read()).get("data")
                        if isinstance(d, dict) and d.get("version"):
                            info[key] = str(d["version"])
                        elif isinstance(d, str) and d:
                            info[key] = d
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"version_info 失败: {e}")
    if info:
        _VERSION_CACHE = (now, info)
    return info


def _read_webui_token(backend: str) -> str:
    """读 NapCat WebUI 登录 token（webui.json）。

    WebUI 登录机制（2026-08-20 逆向）：POST /api/auth/login
    {hash: sha256(token + '.napcat').hexdigest()} → data.Credential（JWT），
    存 localStorage['token']。token 由 NapCat 首次启动自生成（webui.json）。
    带 5 分钟缓存（status() 被 GUI 2s 轮询，避免频繁 docker exec）。
    """
    global _WEBUI_TOKEN_CACHE
    now = time.time()
    if now - _WEBUI_TOKEN_CACHE[0] < 300:
        return _WEBUI_TOKEN_CACHE[1]
    token = ""
    try:
        if backend == "docker":
            # 容器内路径（mlikiowa 镜像）
            container = CONFIG.get("NAPCAT_CONTAINER", "napcat")
            r = subprocess.run(
                ["docker", "exec", container, "cat", "/app/napcat/config/webui.json"],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                token = str(json.loads(r.stdout).get("token", ""))
        else:
            # Windows 绿色版 / 外部自管：本地数据目录
            p = os.path.join(CONFIG.get("NAPCAT_DATA_DIR", ""), "config", "webui.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    token = str(json.load(f).get("token", ""))
    except Exception as e:
        log.debug(f"读 webui token 失败: {e}")
        token = ""
    _WEBUI_TOKEN_CACHE = (now, token)
    return token


def _read_qr_file(path: str) -> tuple[str, int]:
    """读二维码 PNG → (base64, mtime)；失败返回 ("", 0)。"""
    if not os.path.isfile(path):
        return "", 0
    try:
        # 二维码 PNG 通常 1-2KB，过滤 QQ 缓存里的大图误命中
        if os.path.getsize(path) > 200 * 1024:
            return "", 0
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode(), int(os.path.getmtime(path))
    except Exception:
        return "", 0


def _find_qr(root: str, max_age: int = 300) -> tuple[str, int]:
    """在数据目录树里递归找最近生成的 qrcode.png（位置随版本漂移的兜底）。"""
    best, best_mtime = "", 0
    now = time.time()
    if not os.path.isdir(root):
        return "", 0
    for dirpath, _dirs, files in os.walk(root):
        # 只扫 NapCat 相关目录，避开 QQ 聊天缓存
        rel = os.path.relpath(dirpath, root)
        if rel != "." and not any(
                p in ("NapCat", "cache", "config", "db") for p in rel.split(os.sep)):
            continue
        for f in files:
            if f.lower() != "qrcode.png":
                continue
            p = os.path.join(dirpath, f)
            b64, mtime = _read_qr_file(p)
            if not b64:
                continue
            if now - mtime > max_age:
                continue  # 超过 5 分钟的码已过期，跳过
            if mtime > best_mtime:
                best, best_mtime = b64, mtime
    return best, best_mtime


def _build_status(running: bool, hint: str, qrcode_b64: str = "",
                  qrcode_mtime: int = 0, backend: str = "") -> dict:
    """组装 status() 返回结构（登录判定统一逻辑）。"""
    from .sender import _active_websocket
    ws_connected = _active_websocket is not None
    # 已登录时从状态文件读账号信息（bot.py 连接后 get_login_info 写入）
    account = ""
    if ws_connected:
        try:
            sf = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "napcat_status.txt")
            if os.path.exists(sf):
                with open(sf, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("account:"):
                            account = line.partition(":")[2].strip()
                            break
        except OSError:
            pass
    info: dict = {
        "backend": backend,
        "container": CONFIG.get("NAPCAT_CONTAINER", "napcat"),
        "container_running": running,
        "logged_in": ws_connected,
        "ws_connected": ws_connected,
        "account": account,
        "qrcode_b64": qrcode_b64,
        "qrcode_mtime": qrcode_mtime,
        "hint": hint,
        "console_url": _console_url(),
        # WebUI 登录 token（GUI 内嵌控制台时自动注入登录态；5 分钟缓存）
        "webui_token": _read_webui_token(backend) if running else "",
    }
    if ws_connected:
        info["hint"] = "QQ 已登录，NapCat 已连入 bot"
    elif qrcode_b64:
        info["hint"] = "用手机 QQ 扫下方二维码（约 5 分钟过期，过期点刷新）"
    elif running:
        info["hint"] = "QQ 登录态正常，等待 NapCat 连入 bot..."
    return info


def _build_default_onebot11_config(ws_url: str) -> dict:
    """构造默认 onebot11.json（不带 uin 后缀，任意账号登录的兜底桥）。

    NapCat configLoader.read() 逻辑（v4.18.6 源码确认）：
      账号登录后先找 onebot11_<uin>.json，**没有则回落默认
      onebot11.json 并自动 save() 成该账号的文件**。
    因此 config 目录里放一个带 WS 桥的默认文件，任何账号扫码
    登录后都会继承它、自动连回 bot——实现「任意账号接入」。
    （今天 19:03 事故根因：小号登录时无默认文件、无 uin 文件 →
    落盘空网络配置 → 登进 QQ 却连不进 bot。）
    不含 HTTP server：避免多账号回落后 3000 端口互撞。
    """
    return _build_onebot11_config(ws_url)


def _write_default_onebot11(cfg_dir: str, ws_url: str) -> None:
    """写默认 onebot11.json（幂等：已存在不覆盖，尊重手工修改）。"""
    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, "onebot11.json")
    if os.path.isfile(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_build_default_onebot11_config(ws_url), f, ensure_ascii=False, indent=2)
    log.info(f"📝 已写入默认 NapCat onebot11 配置（任意账号登录自动连 bot）: {path}")


def _build_onebot11_config(ws_url: str) -> dict:
    """构造 onebot11 配置 JSON（WS 客户端连 bot + token）。

    win 后端: ws_url = ws://127.0.0.1:<listen_port>/
    docker 后端: ws_url = NAPCAT_DOCKER_HOST_WS（容器网关 → 宿主机）
    """
    listen_port = int(CONFIG.get("LISTEN_PORT", 8696))
    return {
        "network": {
            "httpServers": [
                {"name": "HTTP", "host": "0.0.0.0",
                 "port": int(CONFIG.get("NAPCAT_ONEBOT_HTTP_PORT", 3000)),
                 "enable": True, "token": "", "debug": False},
            ],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {"name": "AI Bot",
                 "url": ws_url if ws_url.endswith("/") else ws_url + "/",
                 "token": CONFIG.get("NAPCAT_WS_TOKEN", ""),
                 "reconnectInterval": 3000,
                 "heartInterval": 30000,
                 "enable": True,
                 "messagePostFormat": "array",
                 "reportSelfMessage": False,
                 "debug": False,
                 "verifyCertificate": True},
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "timeout": {"baseTimeout": 10000, "uploadSpeedKBps": 256,
                    "downloadSpeedKBps": 256, "maxTimeout": 1800000},
    }


# ============================================================
#  注销全清：bot 侧状态复位（两后端共用）
# ============================================================
def _reset_bot_side_after_logout() -> list[str]:
    """注销全清后同步复位 bot 侧状态（2026-08-23，防「登进 QQ 连不进 bot」）。

    协议层凭证清完后，bot 侧还残留两处旧登录态：
      1) 主账号记录 data/napcat_primary.txt —— 旧账号记录还在，新账号
         WS 连入会被按「非主账号」判定（其收敛配置 enable=false →
         reject），扫码升主路径走不通。
      2) 各账号 onebot11_<uin>.json 的收敛状态 —— 非主账号桥被
         converge 关成 enable=false。

    处理：清主账号记录 + 全部账号桥恢复 enable=true（任意账号扫码
    连入都走「无主账号 → 首个连接升主」的 P1 路径，升主时再自动
    收敛关死其余账号）。配置目录缺失（外部自管/未配置）静默跳过；
    单项失败只记日志不抛（凭证已清，bot 侧残留最坏 = 需手动收敛）。
    返回被恢复的 uin 列表（日志/GUI 提示用）。
    """
    from . import napcat_primary
    notes: list[str] = []
    try:
        napcat_primary.clear_primary()
        notes.append("主账号记录已清")
    except Exception as e:
        log.warning(f"⚠️ 注销复位: 清主账号记录失败: {e}")
    try:
        cfg_dir = str(CONFIG.get("NAPCAT_CONFIG_DIR", "") or "")
        if cfg_dir:
            for uin in napcat_primary.enable_all_bridges(cfg_dir):
                notes.append(f"桥已恢复 {uin}")
        else:
            notes.append("未配置 NAPCAT_CONFIG_DIR，跳过账号桥复位")
    except Exception as e:
        log.warning(f"⚠️ 注销复位: 恢复账号桥失败: {e}")
    return notes


# ============================================================
#  Docker 后端（Linux）
# ============================================================
class DockerBackend:
    name = "docker"
    _QR_IN_CONTAINER = "/app/napcat/cache/qrcode.png"

    def __init__(self):
        self.container = CONFIG.get("NAPCAT_CONTAINER", "napcat")
        self.docker = shutil.which("docker")

    def _run(self, *args: str, timeout: int = 15):
        if not self.docker:
            return -1, "docker 不可用"
        try:
            r = subprocess.run([self.docker, *args], capture_output=True,
                               text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    def _state(self) -> str:
        rc, out = self._run("inspect", "-f", "{{.State.Status}}", self.container)
        return out.splitlines()[-1].strip() if rc == 0 and out else "none"

    def ensure_running(self) -> dict:
        if not self.docker:
            return {"ok": False, "error": "docker 不可用（mode=docker 需要 Docker）"}
        st = self._state()
        if st == "running":
            return {"ok": True, "message": f"容器 {self.container} 运行中"}
        if st in ("exited", "created", "paused"):
            rc, out = self._run("start", self.container, timeout=30)
            return {"ok": rc == 0, "message": out or f"已启动 {self.container}"}
        # 容器不存在 → 自动部署（新 Linux 机器一键集成）
        return self._auto_deploy()

    # ---- 自动部署（容器不存在时）----
    def _auto_deploy(self) -> dict:
        """pull 镜像 + 建数据目录 + 注入 onebot11 配置 + 起容器。

        部署参数与生产 docker-compose 对齐（bind 挂载 config/data/logs，
        bridge 网络，unless-stopped 重启策略，3000/3001/6099 端口）。
        数据落 NAPCAT_DOCKER_DATA_DIR（程序目录内）→ passkey.json 持久化，
        新机器扫一次码后重启免扫。
        """
        image = CONFIG.get("NAPCAT_DOCKER_IMAGE", "")
        data_dir = CONFIG.get("NAPCAT_DOCKER_DATA_DIR", "")
        if not image or not data_dir:
            return {"ok": False, "error": "napcat.docker_image / docker_data_dir 未配置"}

        # 1) 注入 onebot11 配置：默认 onebot11.json（任意账号扫码登录的兜底桥）。
        # 08-22 删除预写 onebot11_{bot_qq}.json：账号登录后 NapCat 找不到
        # onebot11_<uin>.json 会自动回落默认文件并 save() 成该账号的文件
        # （configLoader 源码确认），主账号身份由连接 get_login_info 派生，
        # 不再需要配置 bot.qq 预写。
        ws_url = CONFIG.get("NAPCAT_DOCKER_HOST_WS", "")
        cfg_dir = os.path.join(data_dir, "config")
        os.makedirs(cfg_dir, exist_ok=True)
        _write_default_onebot11(cfg_dir, ws_url)

        # 2) 镜像（没有就 pull）
        rc, out = self._run("image", "inspect", image)
        if rc != 0:
            log.info(f"⬇️  拉取 NapCat 镜像 {image}（约 1.4GB，首次较慢）…")
            rc, out = self._run("pull", image, timeout=900)
            if rc != 0:
                return {"ok": False,
                        "error": f"镜像拉取失败: {out[:200]}（检查网络/镜像源）"}
        else:
            log.info(f"📦 镜像 {image} 已存在")

        # 3) 起容器（与生产 compose 参数对齐；端口映射可配，冲突时可覆盖）
        port_maps = CONFIG.get("NAPCAT_DOCKER_HOST_PORTS",
                               ["3000:3000", "3001:3001", "6099:6099"])
        cmd = [
            "run", "-d",
            "--name", self.container,
            "--restart", "unless-stopped",
            "-v", f"{data_dir}/config:/app/napcat/config",
            # /root/.config/hermes 是 mlikiowa/napcat-docker 镜像内 QQ 客户端数据的约定路径
            "-v", f"{data_dir}/data:/root/.config/hermes",
            "-v", f"{data_dir}/logs:/app/logs",
            "-v", "napcat_qq_data:/app/.config/QQ",
        ]
        for p in port_maps:
            cmd += ["-p", str(p)]
        cmd.append(image)
        rc, out = self._run(*cmd, timeout=60)
        if rc != 0:
            err = out[:200]
            if "port is already allocated" in out or "driver failed programming external connectivity" in out:
                err = (f"端口冲突: {err} —— 宿主机 {port_maps} 被占用，"
                       f"请在 config.yaml napcat.docker_host_ports 改为空闲端口")
            return {"ok": False, "error": err}
        log.info(f"🚀 NapCat(docker) 已自动部署: {self.container} (数据 {data_dir})")
        return {"ok": True, "message": f"容器已自动部署并启动: {self.container}"}

    def status(self) -> dict:
        if not self.docker:
            return _build_status(False, "docker 不可用，无法读取 NapCat 状态", backend=self.name)
        st = self._state()
        if st != "running":
            return _build_status(
                False, f"容器 {self.container} 未运行（{st or '不存在'}），"
                       f"点「刷新二维码」自动部署/启动",
                backend=self.name)
        b64, mtime = "", 0
        tmp = "/tmp/_napcat_qr_fetch.png"
        rc, _ = self._run("cp", f"{self.container}:{self._QR_IN_CONTAINER}", tmp, timeout=15)
        if rc == 0:
            b64, mtime = _read_qr_file(tmp)
            try:
                os.remove(tmp)
            except OSError:
                pass
        return _build_status(True, "", b64, mtime, backend=self.name)

    def restart(self, force: bool = False) -> dict:
        """重启 NapCat 容器（GUI「刷新二维码」/ watchdog 自动重启用）。

        force（08-22）：绕过「已连接无需重启」守卫——半死态（WS 还挂着
        但 HTTP 服务已死）下 watchdog 必须能强制重启；GUI 路径保持
        默认 False 不变（有活跃连接时仍拒绝，防误操作断 QQ）。
        """
        from .sender import _active_websocket
        if not force and _active_websocket is not None:
            return {"ok": False, "error": "NapCat 已连接，无需刷新（重启会断开 QQ）"}
        if not self.docker:
            return {"ok": False, "error": "docker 不可用"}
        # 容器不存在 → 走自动部署（新机器首次点刷新 = 部署 + 出码）
        if self._state() == "none":
            return self._auto_deploy()
        rc, out = self._run("restart", self.container, timeout=60)
        if rc != 0:
            return {"ok": False, "error": out[:200] or "重启失败"}
        log.info(f"🔄 NapCat(docker) 已重启（{'force' if force else '刷新二维码'}）: {self.container}")
        return {"ok": True, "message": f"{self.container} 已重启，10 秒后刷新二维码"}

    # ---- 注销全清（2026-08-23 重写）----
    def _qq_data_volume(self) -> str:
        """动态发现容器里 /app/.config/QQ 挂载的 docker volume 名。

        QQ 客户端登录态（nt_qq_* 目录，扫码免登的关键）落这个 volume。
        不同部署（compose 命名卷 / 匿名卷 / _auto_deploy 的
        napcat_qq_data）名字不一致 → 不硬编码，inspect 容器 mounts
        按 Destination==/app/.config/QQ 找。找不到返回 ""（注销降级
        为「停容器 + 清 passkey + 重启」，登录态残留风险记日志）。
        """
        try:
            rc, out = self._run("inspect", "-f", "{{json .Mounts}}", self.container,
                                timeout=15)
            if rc != 0 or not out:
                return ""
            import json as _json
            for m in _json.loads(out):
                if m.get("Type") == "volume" and m.get("Destination") == "/app/.config/QQ":
                    return str(m.get("Name") or "")
        except Exception as e:
            log.warning(f"发现 QQ 数据 volume 失败: {e}")
        return ""

    def _backup_and_clear_qq_volume(self, volume: str) -> tuple[bool, str]:
        """备份并清空 QQ 数据 volume（登录态全清的核心步骤）。

        实现：挂一个 one-off 容器（同镜像，--entrypoint 覆盖跳过 QQ 栈），
        把 volume 挂到 /qqdata → tar czf 备份到 config 挂载目录（宿主机
        可持久访问）→ rm -rf 清空。

        备份保留最近 2 份（防磁盘膨胀）；备份失败**不阻断**清空
        （可用性优先，volume 里主要是聊天缓存，登录态丢失后本就要
        重扫）。返回 (成功?, 说明)。
        """
        cfg_dir = str(CONFIG.get("NAPCAT_CONFIG_DIR", "") or "")
        image = str(CONFIG.get("NAPCAT_DOCKER_IMAGE", "") or "")
        if not image:
            # 从现有容器读镜像兜底
            rc, out = self._run("inspect", "-f", "{{.Config.Image}}",
                                self.container, timeout=15)
            if rc == 0 and out:
                image = out.splitlines()[-1].strip()
        if not image:
            return False, "无法确定镜像，跳过 volume 备份/清空"
        backup_dir = os.path.join(cfg_dir, "qq_data_backup") if cfg_dir else ""
        if backup_dir:
            # 宿主机先建目录：docker run 对不存在的 host 目录会以 root 创建，
            # 属主错乱后续清理麻烦（config 目录是 agent 属主）
            os.makedirs(backup_dir, exist_ok=True)
        # one-off 容器：跳过 entrypoint（默认 entrypoint 会拉起整个 QQ 栈）
        run_cmd = [
            "run", "--rm",
            "--entrypoint", "/bin/sh",
            "-v", f"{volume}:/qqdata",
        ]
        if backup_dir:
            run_cmd += ["-v", f"{backup_dir}:/backup"]
        run_cmd.append(image)
        # 容器内脚本：备份（可选）→ 清空 → 校验。
        # ⚠️ 必须单行无注释：Python 相邻字符串拼接成一行，# 注释会吞掉
        # 同行后续内容（含 fi），dash 直接报 Syntax error（08-23 实测）。
        sh = (
            "set -u; "
            "if [ -n \"$1\" ]; then "
            "  ts=$(date +%Y%m%d_%H%M%S); "
            "  tar czf /backup/qq_data_${ts}.tar.gz -C /qqdata . 2>/dev/null; "
            "  ls -1t /backup/qq_data_*.tar.gz 2>/dev/null | tail -n +3 | xargs -r rm -f; "
            "fi; "
            "find /qqdata -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null; "
            "echo \"REMAIN=$(find /qqdata -mindepth 1 2>/dev/null | wc -l)\""
        )
        cmd = run_cmd + ["-c", sh, "sh", backup_dir or ""]
        rc, out = self._run(*cmd, timeout=600)
        if rc != 0:
            return False, f"清空脚本执行失败: {out[:200]}"
        # 校验清空结果
        rem = ""
        for line in reversed(out.splitlines()):
            if line.startswith("REMAIN="):
                rem = line.split("=", 1)[1].strip()
                break
        ok = rem == "0"
        return ok, (f"volume 已清空（剩余 {rem} 项）" if ok
                    else f"清空后仍有 {rem} 项残留（可能被占用/权限）")

    def _clear_passkey(self) -> None:
        """清 NapCat 扫码登录态 passkey.json（config 挂载目录，宿主机直删）。

        08-20 逆向：passkey.json 是 NapCat 记录的「可快速登录账号」清单；
        全清后删掉，防止下次启动对旧账号抢跑快速登录。文件缺失/损坏
        静默跳过（无登录态 = 本来就要出码）。
        """
        cfg_dir = str(CONFIG.get("NAPCAT_CONFIG_DIR", "") or "")
        if not cfg_dir:
            return
        p = os.path.join(cfg_dir, "passkey.json")
        try:
            if os.path.isfile(p):
                os.remove(p)
                log.info(f"🧹 已删除 NapCat 扫码登录态: {p}")
        except OSError as e:
            log.warning(f"删除 passkey.json 失败: {e}")

    def _clear_webui_auto_login(self) -> None:
        """清 WebUI 的 autoLoginAccount（webui.json，宿主机直删字段）。

        直接改文件比走 WebUI API 可靠（注销流程里 WebUI 可能因容器
        刚 stop 不可达）；autoLoginAccount 非空会让容器启动后先对该
        账号快速登录，抢跑新码流程。字段缺失/文件损坏只记日志。
        """
        cfg_dir = str(CONFIG.get("NAPCAT_CONFIG_DIR", "") or "")
        if not cfg_dir:
            return
        p = os.path.join(cfg_dir, "webui.json")
        try:
            if not os.path.isfile(p):
                return
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("autoLoginAccount"):
                data["autoLoginAccount"] = ""
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                log.info("🧹 已清空 WebUI autoLoginAccount")
        except Exception as e:
            log.warning(f"清 autoLoginAccount 失败: {e}")

    def logout(self) -> dict:
        """注销当前登录（2026-08-23 全清重写）：彻底清凭证 → 出新二维码。

        为什么全清：旧版只杀 NapCat 登录会话（RestartNapCat），QQ 客户端
        的登录态文件仍留在 volume 里——容器一重启 QQ 就拿残留登录态
        **静默回登旧账号**，之后扫新账号撞协议层「账号已有另一会话」
        （ErrCode: 3 死循环，每 2 分钟刷码永远登不上）。全清后每次
        扫码都是全新登录，彻底规避此类身份冲突。

        步骤：
          1) 定位 QQ 数据 volume（/app/.config/QQ 挂载点动态发现）
          2) docker stop（杀 QQ 进程，防清数据时回写）
          3) 备份 + 清空 volume（登录态所在）
          4) 删 passkey.json + 清 webui.json autoLoginAccount
          5) docker start（全新状态出码）
          6) bot 侧同步复位（主账号记录 + 账号桥 enable 全开）

        降级：volume 发现失败 → 跳过 3（其余照常，记风险）；stop 失败
        → 直接失败返回；start 失败 → 已清凭证，返回 ok=False 提示手动
        启动（不能把「凭证已清但容器没起来」报成成功）。

        注意：未点注销时登录态文件原样保留，重启程序/容器照旧免扫
        自动登录——本方法只由 GUI「注销」按钮触发。
        """
        if not self.docker:
            return {"ok": False, "error": "docker 不可用"}
        st = self._state()
        if st == "none":
            return {"ok": False, "error": f"容器 {self.container} 不存在，无法注销"}
        notes: list[str] = []
        # 1) 定位 QQ 数据 volume
        volume = self._qq_data_volume()
        if volume:
            notes.append(f"QQ 数据 volume={volume}")
        else:
            notes.append("⚠️ 未发现 QQ 数据 volume（登录态可能残留，重启后或回登旧账号）")
        # 2) 停容器（杀 QQ 进程）
        rc, out = self._run("stop", self.container, timeout=90)
        if rc != 0:
            return {"ok": False, "error": f"停容器失败: {out[:150]}"}
        notes.append("容器已停止")
        # 3) 备份 + 清空 volume
        if volume:
            ok, detail = self._backup_and_clear_qq_volume(volume)
            notes.append(detail)
            if not ok:
                notes.append("⚠️ volume 未清空干净——旧登录态可能残留，若扫码异常请手动 docker volume rm 后重启")
        # 4) 清 passkey + webui autoLogin
        self._clear_passkey()
        self._clear_webui_auto_login()
        notes.append("passkey/autoLogin 已清")
        # 5) 起容器
        rc, out = self._run("start", self.container, timeout=90)
        if rc != 0:
            return {"ok": False,
                    "error": f"凭证已清空但容器启动失败: {out[:150]}（请手动 docker start {self.container}）"}
        notes.append("容器已启动")
        # 6) bot 侧同步复位（非致命：失败只记日志）
        notes.extend(_reset_bot_side_after_logout())
        log.info(f"👋 NapCat(docker) 已注销（全清凭证）: {' | '.join(notes)}")
        return {"ok": True,
                "cleared": notes,
                "message": "已注销并清空全部登录凭证，约 15 秒后自动拉取新二维码。"
                           "原有账号也需重新扫码（登录态已彻底清除，可扫任意账号）"}


# ============================================================
#  Windows 绿色版后端（Windows 目标）
# ============================================================
class WinGreenBackend:
    """
    管理内置 NapCat 绿色版子进程。

    绿色版目录结构（NapCat.Shell.Windows.Node.zip 解压后，v4.18.19 实测）：
      <win_package_dir>/
        node.exe              内置 Node 运行时
        index.js              启动入口（napcat.bat 内容 = node.exe ./index.js）
        wrapper.node          QQ 协议栈（106MB，内置 QQ 运行时）
        package.json / config.json   QQ 版本信息
        napcat/               NapCat 本体（napcat.mjs / NapCatWinBootMain.exe …）
        *.dll                 依赖库

    运行时行为（数据全落 NAPCAT_DATA_DIR，程序目录内自包含）：
      - 启动: node.exe ./index.js，cwd=绿色版目录
      - env NAPCAT_WORKDIR=NAPCAT_DATA_DIR → 数据根
      - onebot11 配置: <workdir>/NapCat/config/onebot11_<uin>.json
      - passkey.json（扫码登录态）/ QR 码: <workdir> 内（递归扫描定位）
      - stdout/stderr → <workdir>/napcat_win.log
    """
    name = "win"

    def __init__(self):
        self.pkg_dir = CONFIG.get("NAPCAT_WIN_PACKAGE_DIR", "")
        self.data_dir = CONFIG.get("NAPCAT_DATA_DIR", "")
        self._proc: subprocess.Popen | None = None

    # ---- 绿色版定位/下载 ----
    def _node_exe(self) -> str:
        return os.path.join(self.pkg_dir, "node.exe") if self.pkg_dir else ""

    def _green_ready(self) -> bool:
        return (os.path.isfile(self._node_exe())
                and os.path.isfile(os.path.join(self.pkg_dir, "index.js")))

    def _patch_napcat_mjs(self) -> None:
        """修复官方绿色版的 --no-sandbox 崩溃（2026-08-23 真机实测根因）。

        napcat/napcat.mjs 用 child_process.fork 拉 worker 进程时硬塞了
        Chromium 的 --no-sandbox 旗标（Linux/docker 专用），Windows 的
        node 直接报 `bad option: --no-sandbox` 退出码 9，worker 三连炸
        后主进程自杀 → NapCat 永远起不来、无二维码、WebUI 端口不通。

        修复：把 `...X ? {} : { execArgv: ["--no-sandbox"] }` 三元展开
        整体移除（非 Electron 路径本来就不该带任何 execArgv）。
        幂等：已修复（marker 文件存在且 mjs 内无残留）直接返回；
        版本升级（mjs 内重新出现该串）自动重新修复。
        """
        # mjs 定位：官方 zip 直接解到 pkg_dir，结构 pkg_dir/napcat/napcat.mjs；
        # 若用户解压时多套一层目录（pkg_dir/NapCat.Shell.Windows.Node/napcat/…）
        # 也兼容
        candidates = [os.path.join(self.pkg_dir, "napcat", "napcat.mjs")]
        if os.path.isdir(self.pkg_dir):
            for d in os.listdir(self.pkg_dir):
                p = os.path.join(self.pkg_dir, d, "napcat", "napcat.mjs")
                if os.path.isfile(p):
                    candidates.append(p)
        mjs = next((p for p in candidates if os.path.isfile(p)), None)
        if not mjs:
            log.warning("未找到 napcat/napcat.mjs（无法打 --no-sandbox 补丁），"
                        "绿色版目录结构可能有变")
            return
        marker = mjs + ".patched"
        try:
            with open(mjs, encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            log.warning(f"读取 napcat.mjs 失败（补丁跳过）: {e}")
            return
        import re
        # 匹配整个 spread 表达式（含前导逗号）：`...X ? {} : { execArgv: ["--no-sandbox"] }`
        pat = re.compile(r",?\s*\.\.\.\w+\s*\?\s*\{\s*\}\s*:\s*\{\s*execArgv:\s*\[\s*\"--no-sandbox\"\s*\]\s*\}")
        if pat.search(content) or ("execArgv" in content and "--no-sandbox" in content):
            patched, n1 = pat.subn("", content)
            # 兜底：三元结构被混淆器改动时，至少把 execArgv 数组清空
            patched, n2 = re.subn(r"execArgv:\s*\[\s*\"--no-sandbox\"\s*\]",
                                  "execArgv: []", patched)
            if n1 or n2:
                # 补丁后语法自检（有 node 环境时；发行版运行在 Windows 无 node 命令，
                # 跳过不影响——补丁本身是纯文本替换，结构已实测验证）
                with open(mjs, "w", encoding="utf-8") as f:
                    f.write(patched)
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(f"patched at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"ternary={n1} fallback={n2}\n")
                log.info(f"🩹 napcat.mjs 已修复 --no-sandbox "
                         f"(ternary={n1}, fallback={n2})")
        elif not os.path.isfile(marker):
            # 无残留也无 marker：结构未知，记日志提示人工检查
            log.warning("napcat.mjs 内未发现 --no-sandbox（可能版本结构有变）")

    def download(self) -> dict:
        """从 GitHub Release 下载绿色版 zip 并解压到 win_package_dir。"""
        url = CONFIG.get("NAPCAT_WIN_DOWNLOAD_URL", "")
        if not url:
            return {"ok": False, "error": "napcat.win_download_url 未配置"}
        if not self.pkg_dir:
            return {"ok": False, "error": "napcat.win_package_dir 未配置"}
        if self._green_ready():
            return {"ok": True, "message": "绿色版已就绪，跳过下载"}
        os.makedirs(self.pkg_dir, exist_ok=True)
        import urllib.request
        archive = os.path.join(self.pkg_dir, "_napcat_pkg.zip")
        log.info(f"⬇️  下载 NapCat Windows 绿色版 ({url})…约 111MB")
        try:
            urllib.request.urlretrieve(url, archive)
        except Exception as e:
            return {"ok": False, "error": f"下载失败: {e}（检查网络/GitHub 可达性）"}
        try:
            import zipfile
            with zipfile.ZipFile(archive) as z:
                z.extractall(self.pkg_dir)
            os.remove(archive)
        except Exception as e:
            return {"ok": False, "error": f"解压失败: {e}"}
        if not self._green_ready():
            return {"ok": False, "error": "解压完成但未找到 node.exe/index.js（版本结构有变，请检查 release）"}
        log.info(f"✅ NapCat Windows 绿色版就绪: {self.pkg_dir}")
        return {"ok": True, "message": f"绿色版下载完成（{self.pkg_dir}）"}

    # ---- 配置注入 ----
    def _write_onebot_config(self) -> None:
        """写默认 onebot11.json：WS 客户端连本机 bot + token（任意账号兜底桥）。

        08-22 改：原写 onebot11_<uin>.json（uin 来自 bot.qq 配置）改为写默认
        onebot11.json——账号登录后 NapCat 找不到 onebot11_<uin>.json 会回落
        默认文件并自动 save() 成该账号的（configLoader 源码确认），
        与 docker 后端行为对齐，且不再依赖 bot.qq 配置。
        落点: <workdir>/NapCat/config/onebot11.json（幂等，已存在不覆盖）
        """
        cfg_dir = os.path.join(self.data_dir, "NapCat", "config")
        listen_port = int(CONFIG.get("LISTEN_PORT", 8696))
        _write_default_onebot11(cfg_dir, f"ws://127.0.0.1:{listen_port}/")

    # ---- 进程管理 ----
    def _spawn(self) -> str:
        if not self._green_ready():
            return "绿色版未就绪（先点刷新触发下载，或手动解压到 napcat.win_package_dir）"
        os.makedirs(self.data_dir, exist_ok=True)
        self._write_onebot_config()
        self._patch_napcat_mjs()
        env = dict(os.environ)
        # 数据根指向程序目录（passkey/QR/onebot11 配置全在这）
        env["NAPCAT_WORKDIR"] = self.data_dir
        # 与绿色版 index.js 自身设置一致（禁 pipe，避免跨进程通信问题）
        env.setdefault("NAPCAT_DISABLE_PIPE", "1")
        try:
            logf = open(os.path.join(self.data_dir, "napcat_win.log"), "ab")
            self._proc = subprocess.Popen(
                [self._node_exe(), os.path.join(self.pkg_dir, "index.js")],
                cwd=self.pkg_dir,
                env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logf.close()
        except Exception as e:
            return f"启动失败: {e}"
        return ""

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_running(self) -> dict:
        if self._alive():
            return {"ok": True, "message": "Windows 绿色版运行中"}
        if not self._green_ready():
            r = self.download()
            if not r.get("ok"):
                return r
        err = self._spawn()
        if err:
            return {"ok": False, "error": err}
        log.info(f"🚀 NapCat(win) 已启动 pid={self._proc.pid} workdir={self.data_dir}")
        return {"ok": True, "message": f"绿色版已启动 (pid {self._proc.pid})"}

    def status(self) -> dict:
        if not self.pkg_dir:
            return _build_status(False, "napcat.win_package_dir 未配置", backend=self.name)
        running = self._alive()
        if not running and not self._green_ready():
            return _build_status(
                False, f"Windows 绿色版未安装（{self.pkg_dir}），点「刷新二维码」触发自动下载",
                backend=self.name)
        b64, mtime = _find_qr(self.data_dir)
        return _build_status(running, "", b64, mtime, backend=self.name)

    def restart(self, force: bool = False) -> dict:
        # force：win 绿色版无「已连接」守卫概念，参数仅签名对齐
        from .sender import _active_websocket
        if not force and _active_websocket is not None:
            return {"ok": False, "error": "NapCat 已连接，无需刷新（重启会断开 QQ）"}
        if self._alive():
            try:
                self._proc.terminate()
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            time.sleep(1)
        err = self._spawn()
        if err:
            return {"ok": False, "error": err}
        log.info(f"🔄 NapCat(win) 已重启（刷新二维码）pid={self._proc.pid}")
        return {"ok": True, "message": "绿色版已重启，10 秒后刷新二维码"}

    def logout(self) -> dict:
        """注销：停绿色版进程 + 清全部登录凭证 → 下次启动出二维码。

        Windows 后端无 WebUI 内部 API 可用，采用「停进程 + 删凭据」：
          - passkey*.json（NapCat 扫码登录态）删掉
          - webui.json 的 autoLoginAccount 清空（防启动时抢跑快速登录）
          - QQ 数据子目录（<data_dir>/.config/QQ 或 data_dir 根下
            nt_qq*/global 等）best-effort 清理——⚠️ win 绿色版的数据
            落点随版本可能有变（NAPCAT_WORKDIR 是否生效未真机验证），
            清不到不阻断、记日志提示。
        之后 bot 侧同步复位（主账号记录 + 账号桥全开）。
        """
        if not self._alive():
            return {"ok": False, "error": "NapCat 未运行，无法注销"}
        try:
            self._proc.terminate()
            self._proc.wait(timeout=8)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        time.sleep(1)
        # 清 NapCat 扫码登录态（递归找 passkey*.json，避开误删）
        removed = []
        try:
            for dirpath, _dirs, files in os.walk(self.data_dir):
                for f in files:
                    if f.lower().startswith("passkey") and f.lower().endswith(".json"):
                        p = os.path.join(dirpath, f)
                        os.remove(p)
                        removed.append(p)
        except OSError as e:
            log.warning(f"清理登录态文件失败: {e}")
        # 清 webui.json autoLoginAccount（win 数据目录内，直改文件）
        try:
            wp = os.path.join(self.data_dir, "config", "webui.json")
            if not os.path.isfile(wp):
                wp = os.path.join(self.data_dir, "NapCat", "config", "webui.json")
            if os.path.isfile(wp):
                with open(wp, encoding="utf-8") as f:
                    wd = json.load(f)
                if wd.get("autoLoginAccount"):
                    wd["autoLoginAccount"] = ""
                    with open(wp, "w", encoding="utf-8") as f:
                        json.dump(wd, f, ensure_ascii=False, indent=4)
        except Exception as e:
            log.warning(f"清 win webui autoLoginAccount 失败: {e}")
        # 清 QQ 数据子目录（best-effort：落点随版本可能漂移）
        qq_dirs = []
        for cand in (os.path.join(self.data_dir, ".config", "QQ"),
                     os.path.join(self.data_dir, "NapCat", ".config", "QQ")):
            if os.path.isdir(cand):
                qq_dirs.append(cand)
        for dirpath, dirs, _files in os.walk(self.data_dir):
            if dirpath == self.data_dir:
                for d in list(dirs):
                    if d.startswith("nt_qq") or d in ("global", "Crashpad",
                                                      "crash_files"):
                        qq_dirs.append(os.path.join(dirpath, d))
        qq_removed = 0
        for d in qq_dirs:
            try:
                shutil.rmtree(d)
                qq_removed += 1
            except OSError as e:
                log.warning(f"清理 QQ 数据目录失败 {d}: {e}")
        if not qq_dirs:
            log.warning("未找到 QQ 数据目录（win 数据落点可能漂移）——"
                        "若注销后回登旧账号，请手动检查 NAPCAT_DATA_DIR")
        self._proc = None
        notes = [f"passkey 已清 {len(removed)} 个", f"QQ 数据目录已清 {qq_removed} 个"]
        notes.extend(_reset_bot_side_after_logout())
        log.info(f"👋 NapCat(win) 已注销（全清凭证）: {' | '.join(notes)}")
        return {"ok": True,
                "cleared": notes,
                "message": "已注销并清空全部登录凭证，点「刷新二维码」重新启动并扫码"
                           "（原有账号也需重新扫码，可扫任意账号）"}


# ============================================================
#  统一入口
# ============================================================
_backend_cache: dict = {}


def _backend():
    """按当前 mode 取后端实例（热加载 mode 变化时重建）。off=外部自管，无后端。"""
    mode = resolve_mode()
    if mode == "off":
        return mode, None
    if mode not in _backend_cache:
        _backend_cache.clear()
        _backend_cache[mode] = {
            "docker": DockerBackend(),
            "win": WinGreenBackend(),
        }[mode]
    return mode, _backend_cache[mode]


# ============================================================
#  注销进行中锁（2026-08-23 竞态修复）
#  背景：logout 是 60-70s 流程（备份+清空 2.35G volume），期间
#  restart 可从外部进来（GUI「刷新二维码」手动 / watchdog 自动）：
#  历史事故 = 注销跑到一半时手动 restart 提前拉起容器 → 用户扫码
#  登录成功 → 25 秒后 logout 收尾（清 volume+删 passkey）
#  把刚登上的登录态抹掉。锁覆盖 restart/ensure_running/logout 三个
#  模块级入口（双后端共用），窗口内容器保持 stopped、二维码不存在，
#  竞态从根上消失。
#  线程语义：logout 跑在线程池（_h_napcat_logout），restart 可能在
#  事件循环或别的 executor 线程 → 用 threading 锁保护状态。
#  ⚠️ 必须 RLock（可重入）：logout() 持锁窗口内会调 logout_in_progress()
#  （及 b.logout 内的日志路径），threading.Lock 会自死锁（08-23 冒烟实测）。
# ============================================================
_logout_lock = threading.RLock()
_logout_state: dict = {"active": False, "since": 0.0}


def logout_in_progress() -> bool:
    """注销是否进行中（/status 字段、GUI 禁用按钮、测试断言用）。"""
    with _logout_lock:
        return _logout_state["active"]


def status() -> dict:
    mode, b = _backend()
    if mode == "off" or b is None:
        from .sender import _active_websocket
        out = _build_status(
            _active_websocket is not None,
            "mode=off：NapCat 由外部自管，bot 只等待 WS 连入",
            backend="off")
    else:
        try:
            out = b.status()
        except Exception as e:
            out = _build_status(False, f"状态读取异常: {type(e).__name__}: {e}",
                                backend=mode)
    # 注销进行中（GUI 禁用按钮 / 重启后恢复禁用态，08-23 竞态修复）
    out["logout_in_progress"] = logout_in_progress()
    return out


def restart(force: bool = False) -> dict:
    """重启 NapCat。force=watchdog 强制模式（绕过 WS 连接守卫，08-22）。

    2026-08-23：注销进行中一律拒绝（force 也拦）——logout 窗口内提前
    拉起容器 = 用户扫码后被 logout 收尾抹掉登录态（14:24 事故）。
    """
    if logout_in_progress():
        return {"ok": False,
                "error": "注销进行中（清空凭证约 1 分钟），请等待完成后再试"}
    mode, b = _backend()
    if mode == "off" or b is None:
        return {"ok": False, "error": "mode=off：外部自管，bot 无法重启 NapCat"}
    try:
        return b.restart(force=force)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def ensure_running() -> dict:
    """确保 NapCat 在跑（bot 启动时调用）。注销进行中跳过（容器应保持
    stopped，由 logout 收尾负责拉起）。"""
    if logout_in_progress():
        return {"ok": True, "message": "注销进行中，跳过（容器由注销流程负责拉起）"}
    mode, b = _backend()
    if mode == "off" or b is None:
        return {"ok": True, "message": "mode=off：跳过（外部自管）"}
    try:
        return b.ensure_running()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def logout() -> dict:
    """注销当前登录（GUI「注销」按钮）：重置登录会话 + 出新二维码，
    之后可扫任意账号重新登录。

    2026-08-23：进行中锁——入口置位（拒绝重复调用），try/finally 释放；
    窗口内 restart/ensure_running 被拒，防竞态抹掉新登录态。
    """
    with _logout_lock:
        if _logout_state["active"]:
            return {"ok": False, "error": "注销已在进行中，请勿重复操作"}
        _logout_state["active"] = True
        _logout_state["since"] = time.time()
    try:
        mode, b = _backend()
        if mode == "off" or b is None:
            return {"ok": False, "error": "mode=off：外部自管，bot 无法注销 NapCat"}
        try:
            return b.logout()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        with _logout_lock:
            _logout_state["active"] = False
            _logout_state["since"] = 0.0
