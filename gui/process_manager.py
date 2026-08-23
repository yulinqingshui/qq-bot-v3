"""
process_manager.py — bot 子进程生命周期管理（QThread 轮询）
===========================================================
- start(): subprocess 启动 python -m core.bot（stdout 管道 → 日志面板 + data/bot.log 已由 bot 自身写）
- stop(): 先 POST /restart 优雅退出，超时后 kill（附着模式按控制 API 端口定位外部进程强杀）
- attach(): 端口已被外部 bot 占用 → 附着模式（只监控不管理）
"""

import os
import signal
import subprocess
import sys
import threading
import urllib.request
import urllib.error
import time

from PySide6.QtCore import QThread, Signal

import api_client


class BotProcessManager(QThread):
    """管理 bot 子进程。信号在 QThread 中 emit，主线程通过槽接收。"""

    # (running: bool, attached: bool, pid: int, detail: str)
    state_changed = Signal(bool, bool, int, str)
    # 子进程 stdout 行（实时日志面板）
    stdout_line = Signal(str)
    # 进程退出 (exit_code, detail)
    exited = Signal(int, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.attached = False  # 附着模式：端口被外部进程占用
        self._stop_requested = False
        self._reader_thread: threading.Thread | None = None

    # ------------------------------------------------------------
    #  探测端口是否被占用
    # ------------------------------------------------------------
    def port_in_use(self) -> bool:
        import socket
        host = self.cfg.get("LISTEN_HOST", "0.0.0.0")
        port = int(self.cfg.get("LISTEN_PORT", 8696))
        # 探测回环地址上的监听
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            return False

    def control_api_alive(self) -> bool:
        """控制 API 是否可连（判断 8696 上的进程是不是我们的 bot）"""
        try:
            api_client.get_status(self.cfg)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------
    def start_bot(self) -> str:
        """启动 bot 子进程。返回错误信息（空串=成功）。"""
        if self.proc is not None and self.proc.poll() is None:
            return "bot 已在运行"
        if self.port_in_use() and self.control_api_alive():
            self.attached = True
            self.state_changed.emit(True, True, 0, "附着模式：检测到外部 bot 进程")
            return ""
        self.attached = False
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", os.path.join(project_root, "core", "bot.py")],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=project_root,
                env=env,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            return f"启动失败: {e}"
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        self.state_changed.emit(True, False, self.proc.pid, "已启动")
        return ""

    def _read_stdout(self):
        """读子进程 stdout → emit 信号（线程安全：Qt 跨线程信号自动排队）。"""
        if self.proc is None or self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            self.stdout_line.emit(line.rstrip("\n"))
        # stdout 结束 = 进程退出
        code = self.proc.wait()
        self.exited.emit(code, "bot 进程退出")

    def find_external_pid(self) -> int:
        """附着模式：找监听控制 API 端口的外部 bot 进程 pid。失败返回 0。"""
        port = int(self.cfg.get("CONTROL_API_PORT", 8697))
        if sys.platform == "win32":
            import subprocess
            try:
                out = subprocess.check_output(["netstat", "-ano"], text=True, timeout=5)
                for line in out.splitlines():
                    parts = line.split()
                    if (len(parts) >= 5 and parts[1].endswith(f":{port}")
                            and parts[3] == "LISTENING"):
                        return int(parts[4])
            except Exception:
                pass
            return 0
        # Linux：/proc/net/tcp 拿 inode → 扫 /proc/<pid>/fd 反查
        try:
            inodes = set()
            with open("/proc/net/tcp") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":  # 0A = LISTEN
                        continue
                    if int(parts[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(int(parts[9]))
            if not inodes:
                return 0
            for d in os.listdir("/proc"):
                if not d.isdigit():
                    continue
                fd_dir = f"/proc/{d}/fd"
                try:
                    for fd in os.listdir(fd_dir):
                        try:
                            if os.readlink(f"{fd_dir}/{fd}") in \
                                    {f"socket:[{i}]" for i in inodes}:
                                return int(d)
                        except OSError:
                            continue
                except (OSError, PermissionError):
                    continue
        except Exception:
            pass
        return 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def stop_bot(self, graceful_timeout: int = 10) -> str:
        """停止 bot：先优雅（POST /restart），超时强杀。
        08-20：附着模式（service.sh 等外部方式启动的 bot）同样生效——
        优雅退出超时后按控制 API 端口定位外部进程强杀。"""
        own_running = self.proc is not None and self.proc.poll() is None
        if not own_running and not (self.attached and self.port_in_use()):
            self.proc = None
            self.attached = False
            self.state_changed.emit(False, False, 0, "未运行")
            return ""
        # 优雅退出（bot 主循环 0.5s 轮询 /restart 标志后自行退出）
        try:
            api_client.request_restart(self.cfg)
        except Exception:
            pass  # 控制 API 挂了就直接杀
        deadline = time.time() + graceful_timeout
        while time.time() < deadline:
            own_running = self.proc is not None and self.proc.poll() is None
            if not own_running and not self.port_in_use():
                self.proc = None
                self.attached = False
                self.state_changed.emit(False, False, 0, "已停止")
                return ""
            time.sleep(0.2)
        # 超时强杀：自有子进程 → terminate/kill；附着 → 按端口定位外部进程
        killed = False
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                time.sleep(2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
            killed = True
        else:
            pid = self.find_external_pid()
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(2)
                    if self._pid_alive(pid):
                        os.kill(pid, signal.SIGKILL)
                    killed = True
                except Exception:
                    pass
        self.proc = None
        self.attached = False
        self.state_changed.emit(False, False, 0, "已强制停止" if killed else "已停止")
        return ""

    def is_running(self) -> bool:
        if self.attached:
            return self.port_in_use()
        return self.proc is not None and self.proc.poll() is None

    def running_detail(self) -> tuple[bool, bool, int]:
        """(running, attached, pid)"""
        if self.attached:
            return (self.port_in_use(), True, 0)
        if self.proc is not None and self.proc.poll() is None:
            return (True, False, self.proc.pid)
        return (False, False, 0)

    # QThread 约定：run() 里放阻塞循环；这里轮询进程状态供外部查询
    def run(self):
        while True:
            running, attached, pid = self.running_detail()
            self.state_changed.emit(running, attached, pid, "轮询")
            if not running and self._stop_requested:
                break
            self.msleep(1500)

    def shutdown(self):
        """GUI 退出时调用：确保子进程被清理。"""
        self._stop_requested = True
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
