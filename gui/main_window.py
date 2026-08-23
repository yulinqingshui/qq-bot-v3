"""
main_window.py — QQ Bot 控制台主窗口
====================================
- 9 个标签页：总览 / 消息管理 / 人设画像 / AI 聊天 / 真心话大冒险 / 群组集群 / 游戏 / 角色扮演 / 日志
  （配置标签页 2026-08-22 删除，内容迁入总览页配置面板「⚙️ 其他设置」弹窗）
  （画图页 2026-08-22 删除；ComfyUI 地址/连接测试在总览页「⚙️ 配置面板」）
- 状态栏每 2s 轮询控制 API /status
- 危险操作统一走 confirm() 确认框
"""

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QApplication, QTabWidget, QMessageBox, QStatusBar, QWidget,
    QVBoxLayout, QLabel, QScrollArea,
)

import api_client
from process_manager import BotProcessManager
from tab_overview import TabOverview
from tab_messages import TabMessages
from tab_personas import TabPersonas
from tab_ai_chat import TabAiChat
from tab_questions import TabQuestions
from tab_groups import TabGroups
from tab_games import TabGames
from tab_roleplay import TabRoleplay
from tab_logs import TabLogs
from status_light import StatusLightRow

VERSION = "3.0.0"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"QQ Bot 控制台 v{VERSION}")
        # 窗口尺寸自适应屏幕（VNC/Xvfb 无窗口管理器：不约束尺寸，窗口会按
        # 内容自然高度撑开、超出屏幕导致底部被切）。约束到屏幕内并居中。
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.resize(min(1180, geo.width() - 20), min(760, geo.height() - 20))
            self.move(max(geo.x(), (geo.width() - self.width()) // 2),
                      max(geo.y(), (geo.height() - self.height()) // 2))
        else:
            self.resize(1180, 760)

        # 配置（本地读取；bot 运行时以 /config 为准）
        self.yaml_cfg = api_client.load_yaml()
        self.env_cfg = api_client.load_env()
        # 扁平 cfg（控制 API 基址等）——优先从 yaml 算
        from core.config import flatten_yaml_tree
        # .env 覆盖密钥
        self.cfg = flatten_yaml_tree(self.yaml_cfg)
        # .env 密钥（REMOTE_API_KEY 为主，兼容旧键 DEEPSEEK_API_KEY）
        self.cfg["REMOTE_API_KEY"] = (self.env_cfg.get("REMOTE_API_KEY")
                                      or self.env_cfg.get("DEEPSEEK_API_KEY")
                                      or os.environ.get("REMOTE_API_KEY", ""))
        self.cfg["LLM_API_KEY"] = self.env_cfg.get("LLM_API_KEY", os.environ.get("LLM_API_KEY", ""))
        # DB 路径（08-21 修复：flatten_yaml_tree 不含 DB 键，数据页查询会
        # KeyError 'DB_PATH'——与 core.config.load_config 同逻辑补齐）
        from core.config import _abs
        _data_dir = _abs(self.yaml_cfg.get("paths", {}).get("data_dir", "data"))
        self.cfg["DB_PATH"] = os.path.join(_data_dir, "chat_history.db")
        self.cfg["BOT_SETTINGS_DB_PATH"] = os.path.join(_data_dir, "bot_settings.db")
        self.cfg["PERSONAS_DB_PATH"] = os.path.join(_data_dir, "personas.db")
        self.cfg["DAILY_REPORTS_DB_PATH"] = os.path.join(_data_dir, "daily_reports.db")
        self.cfg["TRUTH_DARE_DB_PATH"] = os.path.join(_data_dir, "truth_dare.db")

        # 进程管理
        self.pm = BotProcessManager(self.cfg, self)
        self.pm.state_changed.connect(self._on_pm_state)
        self.pm.exited.connect(self._on_pm_exited)
        self.pm.start()
        self._workers: list = []  # 后台 Worker 引用（防 GC）

        # 标签页
        self.tabs = QTabWidget()
        self.tab_overview = TabOverview(self)
        # 08-22：配置标签页删除（内容迁入总览页配置面板「⚙️ 其他设置」弹窗）
        self.tab_messages = TabMessages(self)
        self.tab_personas = TabPersonas(self)
        self.tab_ai_chat = TabAiChat(self)
        self.tab_questions = TabQuestions(self)
        self.tab_groups = TabGroups(self)
        self.tab_games = TabGames(self)
        self.tab_roleplay = TabRoleplay(self)
        self.tab_logs = TabLogs(self)

        for tab, title in [
            (self.tab_overview, "📊 总览"),
            (self.tab_groups, "👥 群组集群"),
            (self.tab_messages, "💬 消息管理"),
            (self.tab_ai_chat, "🤖 AI 聊天"),
            (self.tab_personas, "🎭 人设画像"),
            (self.tab_games, "🎮 游戏管理"),
            (self.tab_roleplay, "🎭 角色扮演"),
            (self.tab_questions, "🎲 真心话大冒险"),
            (self.tab_logs, "📜 日志"),
        ]:
            # 每页套滚动区：VNC 无窗口管理器时窗口尺寸固定为屏幕内，
            # 内容超高可纵向滚动，不会被切掉
            self.tabs.addTab(self._scrollify(tab), title)

        # 子进程 stdout → 日志面板
        self.pm.stdout_line.connect(self.tab_logs.append_line)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(8, 8, 8, 4)
        v.addWidget(self.tabs)
        self.setCentralWidget(central)

        # 状态栏
        self.statusBar().showMessage("就绪")

        # 状态轮询（2s）
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_status)
        self._poll_timer.start(2000)
        self._poll_status()

        # Worker 引用清扫（30s，防 QThread 对象累积）
        self._gc_timer = QTimer(self)
        self._gc_timer.timeout.connect(self._gc_workers)
        self._gc_timer.start(30000)

        # 附着模式检测：已有 bot 在跑 → 自动附着
        self._auto_attach_check()

    # ------------------------------------------------------------
    #  确认框（危险操作统一入口）
    # ------------------------------------------------------------
    @staticmethod
    def _scrollify(tab: QWidget) -> QScrollArea:
        """把 tab 套进纵向滚动区（窗口固定屏幕内、内容超高可滚）。"""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(tab)
        sa.setFrameShape(QScrollArea.NoFrame)
        return sa

    def confirm(self, title: str, text: str, yes_text: str = "确认") -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText(yes_text)
        box.button(QMessageBox.No).setText("取消")
        return box.exec() == QMessageBox.Yes

    def confirm_danger(self, title: str, text: str) -> bool:
        """高危操作确认：要求输入 YES"""
        from PySide6.QtWidgets import QInputDialog
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setTextFormat(Qt.PlainText)
        box.setInformativeText("输入 YES 确认执行：")
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.button(QMessageBox.Ok).setText("执行")
        r = box.exec()
        if r != QMessageBox.Ok:
            return False
        text_in, ok = QInputDialog.getText(self, "确认", "输入 YES：")
        return ok and text_in.strip().upper() == "YES"

    # ------------------------------------------------------------
    #  状态轮询
    # ------------------------------------------------------------
    # ------------------------------------------------------------
    #  Worker 追踪（防 QThread 泄漏：30s 定时清扫已结束的 worker 引用）
    #  注意：不连 worker.finished 信号——PySide6 下在信号发射链中移除
    #  引用会触发递归/abort，定时清扫最稳。
    # ------------------------------------------------------------
    def _track(self, w):
        self._workers.append(w)

    def _gc_workers(self):
        """清扫已结束 worker（主线程定时器调用，isRunning 查询安全）。"""
        alive = []
        for w in self._workers:
            try:
                if w.isRunning():
                    alive.append(w)
            except RuntimeError:
                continue  # C++ 对象已销毁
        self._workers = alive

    def _poll_status(self):
        """轮询控制 API（bot 未运行时静默跳过，状态灯置灰）。"""
        from worker import Worker
        if getattr(self, "_polling", False):
            return
        self._polling = True

        def _do():
            try:
                return api_client.get_status(self.cfg)
            except Exception:
                return None

        def _ok(status):
            self._polling = False
            if status is None:
                # bot 未运行：状态灯置灰
                for key, light in self.tab_overview.lights.lights.items():
                    light.set_state("idle", light._label.text())
                self.statusBar().showMessage("bot 未运行")
                return
            # 状态灯
            lights = self.tab_overview.lights.lights
            lights["bot"].set_state("ok", "bot 运行中 (pid %d)" % status.get("pid", 0))
            if status.get("napcat", {}).get("connected"):
                lights["napcat"].set_state("ok", "NapCat 已连接")
            else:
                lights["napcat"].set_state("warn", "NapCat 未连接")
            # LLM 状态灯（08-21 修复：原逻辑写死 ok 恒绿，现在读 /status 的
            # health 字段——最近一次真实调用/连接测试的结果：
            #   idle=灰(未测试) ok=绿(最近成功) fail=红(最近失败)）
            llm = status.get("llm", {})
            backend = llm.get("backend", "?")
            if not llm.get("enabled", True):
                lights["llm"].set_state("off", f"LLM: {backend} 已关闭")
            elif llm.get("health") == "ok":
                ts = llm.get("health_ts")
                tstr = (time.strftime("%H:%M", time.localtime(ts))
                        if ts else "")
                lights["llm"].set_state("ok", f"LLM: {backend} ✓{tstr}")
                lights["llm"].setToolTip(
                    f"最近一次成功（{llm.get('health_source', '?')}）{tstr and ('· ' + tstr) or ''}")
            elif llm.get("health") == "fail":
                lights["llm"].set_state(
                    "off", f"LLM: {backend} ✗{time.strftime('%H:%M', time.localtime(llm['health_ts'])) if llm.get('health_ts') else ''}")
                # tooltip 看失败原因
                lights["llm"].setToolTip(
                    f"最近一次 LLM 调用失败（{llm.get('health_source', '?')}）\n{llm.get('health_error', '')}")
            else:
                lights["llm"].set_state("idle", f"LLM: {backend} 未测试")
                lights["llm"].setToolTip("LLM 尚无调用记录——点「🔌 连接测试」或等一次真实调用")
            # 总览页数据
            self.tab_overview.update_status(status)
            # 各数据页可刷新（轻量）
            self.statusBar().showMessage(
                f"运行 {self._fmt_uptime(status.get('uptime_seconds', 0))} | "
                f"db {self._fmt_size(status.get('db_size_bytes', 0))}"
            )

        def _err(msg):
            self._polling = False

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self._track(w)

    def _auto_attach_check(self):
        if self.pm.port_in_use() and self.pm.control_api_alive():
            self.pm.attached = True
            self.pm.state_changed.emit(True, True, 0, "附着模式")
            self.statusBar().showMessage("附着模式：检测到已在运行的 bot")

    # ------------------------------------------------------------
    #  进程状态
    # ------------------------------------------------------------
    def _on_pm_state(self, running, attached, pid, detail):
        pass  # 轮询已覆盖

    def _on_pm_exited(self, code, detail):
        self.tab_logs.append_line(f"[GUI] {detail} (exit={code})")
        self.statusBar().showMessage(f"bot 已停止 (exit={code})")

    # ------------------------------------------------------------
    #  工具
    # ------------------------------------------------------------
    def _fmt_uptime(self, seconds: int) -> str:
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _fmt_size(self, n: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if n < 1024:
                return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"

    def closeEvent(self, event):
        # 退出 GUI：bot 子进程默认保留运行（后台服务），确认框说明
        if self.pm.proc is not None and self.pm.proc.poll() is None:
            box = QMessageBox(self)
            box.setWindowTitle("退出")
            box.setText("退出控制台，bot 继续后台运行？")
            box.setInformativeText("选「停止 bot」则先停 bot 再退出")
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            box.button(QMessageBox.Yes).setText("保留运行")
            box.button(QMessageBox.No).setText("停止 bot")
            box.button(QMessageBox.Cancel).setText("取消退出")
            r = box.exec()
            if r == QMessageBox.Cancel:
                event.ignore()
                return
            if r == QMessageBox.No:
                # 停止 bot：控制 API 优雅停 + 兜底杀（含 NapCat，bot 退出收尾清理）
                self.pm.stop_bot(graceful_timeout=8)
                self.pm.shutdown(kill_bot=True)
                event.accept()
                return
            # 保留运行：只停轮询线程，bot 进程留在后台（08-24 修复：
            # 原来误杀 bot 且 NapCat 残留孤儿）
            self.pm.shutdown(kill_bot=False)
            event.accept()
            return
        # bot 不在运行：直接收尾
        self.pm.shutdown(kill_bot=True)
        event.accept()
