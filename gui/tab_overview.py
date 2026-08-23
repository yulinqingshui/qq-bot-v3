"""
tab_overview.py — 总览页
布局（08-20 精简版）：
  顶部：状态灯 + 启停按钮
  下方：2 行板块（高度随内容压缩，不拉伸）
    行1：[1] NapCat（二维码/账号信息）  [2][3] LLM 后端（跨两列：配置/密钥/测试/用量）
    行2：[1] 消息管理（收发总开关/类型子开关/存档/保留期）  [2] 配置面板（总闸/其他设置）  [3] 任务列表（只读，2026-08-22 替代预留占位）
  已删除板块：bot 进程、ComfyUI、数据概览（08-20 用户要求精简）
"""

import base64
import os
import sys
import time

from PySide6.QtCore import Qt, QTimer, QSize, Signal
from PySide6.QtGui import QPixmap, QImage, QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QGridLayout, QSizePolicy, QComboBox, QLineEdit,
    QStackedWidget, QSpinBox, QFormLayout, QMessageBox,
    QDialog, QPlainTextEdit,
    QListWidget, QListWidgetItem,
    QApplication,
)

import api_client
from status_light import StatusLightRow
from widgets import QSwitch, NoWheelSpinBox, flash_button
from worker import Worker


# 板块统一样式
# 注意（Qt 坑，实测）：
# - QGroupBox::title 子控件的 font-size / font-weight 在 QSS 里都不生效，
#   title 字体跟随 QGroupBox 控件字体 → 标题字号/加粗靠 widget setFont，
#   QSS 里 font-size 只用于兜底。
# - 面板内正文控件钉回 13px + normal，防止跟随标题的 16px bold 被连带放大。
_PANEL_STYLE = """
QGroupBox {
    border: 1px solid #d0d7de;
    border-radius: 6px;
    margin-top: 24px;
    padding: 10px 12px 12px 12px;
    font-size: 16px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    font-weight: bold;
}
QGroupBox QLabel, QGroupBox QLineEdit, QGroupBox QComboBox,
QGroupBox QPushButton, QGroupBox QCheckBox {
    font-size: 13px;
    font-weight: normal;
}
"""
_OK_COLOR = "color: #27ae60;"
_WARN_COLOR = "color: #e67e22;"
_BAD_COLOR = "color: #e74c3c;"


class _ClickableLabel(QLabel):
    """可点击 QLabel（QLabel 无 clicked 信号，mousePressEvent 补；08-22）。

    用法：clicked = Signal()；mousePressEvent 左键→ emit + accept。
    保留 TextSelectableByMouse 文本选择能力（选中拖动不触发 clicked——
    按下+抬起位移 >2px 视为拖选）。
    """
    clicked = Signal()

    def mousePressEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.LeftButton and self.isEnabled():
            self._press_pos = ev.position()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):  # noqa: N802
        if (ev.button() == Qt.LeftButton and hasattr(self, "_press_pos")
                and self.isEnabled()
                and (ev.position() - self._press_pos).manhattanLength() <= 2):
            self.clicked.emit()
            del self._press_pos
        super().mouseReleaseEvent(ev)


class _TaskList(QListWidget):
    """任务列表面板专用 QListWidget：sizeHint 压小（默认 QListWidget ≈10 行高）。

    08-22：默认 sizeHint 比同排消息管理/配置面板都高 → 主导第二行网格行高，
    另两卡被 Expanding 拉伸后各自内部余量摊法不同 → 底部按钮行错位 9px。
    压小后行高由消息管理/配置面板主导，三卡等高对齐。
    实际渲染高度不受影响：stretch=1 + 默认 Expanding 策略照旧拉伸填满行高。
    （Qt sizeHint 是 C++ 虚方法：子类重写生效，实例属性赋值无效——
      同 roleplay 页 _StretchyWidget 配方）
    """

    def sizeHint(self):  # noqa: N802
        w = super().sizeHint().width()
        h = 4 * self.fontMetrics().height() + 20  # ≈4 行高，足够装下少量任务
        return QSize(w, h)


class TabOverview(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._last_status = None
        self._last_napcat_fetch = 0.0   # 自动拉二维码防抖
        # 2026-08-23：本地请求在途标志（update_status 恢复按钮可用性时
        # 需排除在途操作，防止把「刷新中/注销中」的禁用态抢掉）
        self._logout_busy = False
        self._qr_refresh_busy = False
        self._build()

    # ------------------------------------------------------------
    # 外层纵向滚动条根治（08-23，RP 页 08-22 同款配方）：
    # 页面内嵌 wordWrap QLabel（NapCat 状态行/详情行）→ layout
    # hasHeightForWidth=True，QScrollArea(widgetResizable) 定页高优先用
    # heightForWidth 而非 sizeHint → 内容自然高（~625px，NapCat 启动后
    # 状态行折行+任务列表+LLM 用量行再涨）超过视口即出外层滚动条。
    # 双保险：sizeHint 恒返回 minimumSizeHint（外层按视口定页高）+
    # hasHeightForWidth/heightForWidth 钉死（Qt C++ 布局路径绕过 sizeHint
    # 时也不回落到 heightForWidth）。页面填满视口后多出的余量由
    # _build 里 grid stretch=1 摊进两行卡片（各卡内部按钮行 stretch 钉底，
    # 不沉大白块）。极端小窗（min>视口）时滚动条保留=正常兜底。
    # ------------------------------------------------------------
    def sizeHint(self):  # noqa: N802
        return self.minimumSizeHint()

    def hasHeightForWidth(self):  # noqa: N802
        return False

    def heightForWidth(self, w):  # noqa: N802
        return self.minimumSizeHint().height()

    # ============================================================
    #  构建 UI
    # ============================================================
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        # 08-22：spacing 10→8——顶栏按钮与板块标题间隙略缩（17→15px 视觉距离）；
        # 主布局仅 toprow/grid 两项，此 spacing 只作用于顶栏与板块网格之间
        v.setSpacing(8)

        # ---------- 顶部：操作按钮（左上）+ 状态灯（右上）----------
        # 08-20：用户要求按钮放左上角、状态灯放右上角（原为灯左按钮右）
        toprow = QHBoxLayout()
        toprow.setSpacing(8)
        self.btn_start = QPushButton("🚀 启动 bot")
        self.btn_start.setProperty("primary", True)
        self.btn_stop = QPushButton("🛑 停止 bot")
        self.btn_stop.setProperty("danger", True)
        self.btn_restart = QPushButton("🔁 重启 bot")
        self.btn_open_logdir = QPushButton("📂 打开数据目录")
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_open_logdir):
            b.setMinimumHeight(30)
            toprow.addWidget(b)
        toprow.addStretch(1)
        sep = QLabel("|")
        sep.setStyleSheet("color: #bdc3c7; font-size: 14px;")
        toprow.addWidget(sep)
        self.lights = StatusLightRow()
        toprow.addWidget(self.lights)
        v.addLayout(toprow)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_restart.clicked.connect(self._restart)
        self.btn_open_logdir.clicked.connect(self._open_logdir)

        # ---------- 2 行板块网格 ----------
        # 行1：NapCat + LLM（跨2列）；行2：消息管理（跨整行）
        # 08-20：删除 bot 进程/ComfyUI/数据概览板块；行不拉伸（高度随内容压缩）
        grid = QGridLayout()
        grid.setSpacing(8)  # 列间距
        grid.setVerticalSpacing(6)  # 08-20：行间 8→6，收小第一行卡片底部空隙

        # 板块：NapCat（第一行第一列）
        # 08-20：stretchy=True + 无 AlignTop → 填满网格行，底部边框与 LLM 卡对齐
        #       （内部无底部 stretch，多出的 8px 摊进行距，无沉底空隙）
        self.napcat_box = self._make_panel("📱 NapCat", stretchy=True)
        self._build_napcat_panel()
        grid.addWidget(self.napcat_box, 0, 0)

        # 板块：LLM 后端（第一行第二、三列——LLM 配置全部在此管理）
        # 08-20：改回 stretchy=True（等高拉伸）→ 底部边框与 NapCat 对齐；
        #       末尾 addStretch 已移除，多余高度摊进行距（~7px/行，不显空行）
        # 08-22：bottom_pad=10（默认 12）——底部用量小字与边框间隙 12→10px
        self.llm_box = self._make_panel("🧠 LLM 后端", stretchy=True, bottom_pad=10)
        self._build_llm_panel()
        # 08-20：不加 AlignTop——网格单元内填满拉伸，底部边框与 NapCat 对齐
        #       （AlignTop 会禁止拉伸，卡按内容高度放顶部→底部错开 75px）
        grid.addWidget(self.llm_box, 0, 1, 1, 2)

        # 板块：消息管理（第二行第一列——收发总开关/类型子开关/存档/保留期）
        self.msg_box = self._make_panel("📥 消息管理")
        self._build_msg_panel()
        grid.addWidget(self.msg_box, 1, 0)

        # 板块：配置面板（第二行第二列——ComfyUI/赛博模仿/定时任务全局总闸，
        # 2026-08-22 替代原预留占位；群级开关仍在「群组集群」页细调）
        self.cfg_panel_box = self._make_panel("⚙️ 配置面板")
        self._build_cfg_panel()
        grid.addWidget(self.cfg_panel_box, 1, 1)

        # 第二行第三列：任务列表（只读，2026-08-22 替代预留占位）
        # 显示当前正在进行/排队中的后台任务（人设画像更新/群指令/定时任务/题库维护），
        # 数据来自 bot /status 的 tasks 字段（复用 2 秒轮询，update_status 刷新）
        self.task_box = self._make_panel("📋 任务列表")
        self._build_task_panel()
        grid.addWidget(self.task_box, 1, 2)

        for c in (0, 1, 2):
            grid.setColumnStretch(c, 1)
        # 08-23：grid stretch=1 + 删底部 addStretch——页面被外层滚动区拉满
        # 视口高后，网格填满剩余高度（两行均摊余量，卡片 Expanding 拉伸），
        # 不再"内容贴顶+底部大片留白"，配合类级 sizeHint override 根治
        # 外层纵向滚动条（08-23 用户报告：NapCat 启动后滚动条明显）。
        v.addLayout(grid, 1)

        # ---------- 定时器 ----------
        # 登录确认后收起二维码区（过渡提示）
        self._login_ok_timer = QTimer(self)
        self._login_ok_timer.setSingleShot(True)
        self._login_ok_timer.timeout.connect(self._on_login_ok_delayed)
        # 刷新二维码后延迟拉取（NapCat 重启后约 10 秒才出码）
        self._qr_timer = QTimer(self)
        self._qr_timer.setSingleShot(True)
        self._qr_timer.timeout.connect(lambda: self._fetch_napcat(force=True))

    def _make_panel(self, title: str, stretchy: bool = True, bottom_pad: int = 12) -> QGroupBox:
        box = QGroupBox(title)
        # 底部 padding 可配（08-22：LLM 卡 12→10——底部小字提示与边框间隙略缩，
        # 不影响同排 NapCat 卡；布局 margin 已清零，此 padding 即底部留白）
        style = _PANEL_STYLE.replace(
            "padding: 10px 12px 12px 12px;",
            f"padding: 10px 12px {bottom_pad}px 12px;")
        box.setStyleSheet(style)
        # 标题字体：QSS 对 title 的 font-weight/font-size 不生效，
        # 用 widget 字体（16px bold）——title 跟随，正文控件已被 QSS 钉回 13px
        tf = QFont()
        tf.setPixelSize(16)
        tf.setBold(True)
        box.setFont(tf)
        # 纵向策略：stretchy=Expanding（被网格等高拉伸，底部 stretch 沉底→底部空隙）
        #           stretchy=False=Maximum（高度随内容，配 AlignTop 放板块，底部边框紧贴内容）
        v_pol = QSizePolicy.Expanding if stretchy else QSizePolicy.Maximum
        box.setSizePolicy(QSizePolicy.Expanding, v_pol)
        lay = QVBoxLayout(box)
        # 08-20：清空布局默认 margin（~9px）——底部空隙只剩样式表 12px padding
        #       （原 9+12=21px，等高拉伸后卡片底部显得空）；顶部让给标题区
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        box._vlayout = lay  # 挂在 box 上，避免多面板共享属性串位
        return box

    # ---------------------------------------------------------- 任务列表（2026-08-22）
    def _build_task_panel(self):
        """任务列表面板：只读 QListWidget，⚡正在进行 / ⏳排队中 两区。

        数据源：bot /status 的 tasks 字段（core/task_registry.py 内存态注册表），
        由 update_status() 复用 2 秒轮询刷新。
        """
        lay = self.task_box._vlayout
        lay.setContentsMargins(0, 8, 0, 0)

        # 列表（只读、不可选中输入）
        # 08-22：_TaskList 压小 sizeHint，防主导第二行行高致按钮行错位
        self.task_list = _TaskList()
        self.task_list.setEditTriggers(QListWidget.NoEditTriggers)
        self.task_list.setDragEnabled(False)
        self.task_list.setSelectionMode(QListWidget.NoSelection)
        self.task_list.setFocusPolicy(Qt.NoFocus)
        self.task_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; }"
            "QListWidget::item { padding: 4px 6px; border-radius: 5px;"
            " margin: 1px 2px; }"
            "QListWidget::item:selected { background: transparent; }"
        )
        self.task_list.itemClicked.connect(lambda _it: None)  # 点击无副作用（只读）
        lay.addWidget(self.task_list, 1)

        # 底部状态行（2026-08-22：汇总标签 + 暂停/继续按钮，按钮靠右不单独占行）
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        self.task_count_label = QLabel("当前无任务")
        self.task_count_label.setStyleSheet("color: #9aa4b2;")
        self.task_count_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bottom.addWidget(self.task_count_label, 1)
        # 按钮文字必须完整（emoji 会让 sizeHint 算窄切字）；宽度随文字自适应
        self.btn_task_pause = QPushButton("⏸ 暂停")
        self.btn_task_pause.setCursor(Qt.PointingHandCursor)
        self.btn_task_pause.setToolTip(
            "暂停任务序列：排队中任务等待继续（⏸ 前缀），执行中任务跑完当前步。\n"
            "范围=全部（含 AI 聊天回复）；无限等待，点「继续」恢复。")
        self.btn_task_pause.clicked.connect(self._on_task_pause)
        self.btn_task_resume = QPushButton("▶ 继续")
        self.btn_task_resume.setCursor(Qt.PointingHandCursor)
        self.btn_task_resume.setToolTip("继续任务序列：全部排队任务按原顺序恢复执行")
        self.btn_task_resume.clicked.connect(self._on_task_resume)
        bottom.addWidget(self.btn_task_pause)
        bottom.addWidget(self.btn_task_resume)
        lay.addLayout(bottom)

    def _update_task_btns(self, paused: bool):
        """暂停/继续按钮态（互斥 enabled + 视觉强调）。"""
        self.btn_task_pause.setEnabled(not paused)
        self.btn_task_resume.setEnabled(paused)
        self.btn_task_pause.setStyleSheet(
            "QPushButton { background: #fdf6ec; color: #9c640c; border: 1px solid #f5cba7;"
            " border-radius: 5px; padding: 3px 10px; }" if not paused else
            "QPushButton { background: #f4f6f8; color: #b6bec9; border: 1px solid #e3e8ee;"
            " border-radius: 5px; padding: 3px 10px; }")
        self.btn_task_resume.setStyleSheet(
            "QPushButton { background: #eafaf1; color: #1e8449; border: 1px solid #a9dfbf;"
            " border-radius: 5px; padding: 3px 10px; font-weight: bold; }" if paused else
            "QPushButton { background: #f4f6f8; color: #b6bec9; border: 1px solid #e3e8ee;"
            " border-radius: 5px; padding: 3px 10px; }")

    def _on_task_pause(self):
        """暂停：异步调控制 API（Worker 不阻塞 UI），完成后立即刷新面板。"""
        if self.btn_task_pause.isEnabled() is False:
            return

        def _do():
            return api_client.tasks_pause(self.mw.cfg)

        def _ok(res):
            # 立即刷新按钮态（不等 2s 轮询）
            self._update_task_btns(True)
            self.task_count_label.setText("⏸ 已暂停 · 等待继续")

        def _err(e):
            # bot 未运行时静默（下轮再试）
            pass

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _on_task_resume(self):
        """继续：异步调控制 API，完成后立即刷新面板。"""
        if self.btn_task_resume.isEnabled() is False:
            return

        def _do():
            return api_client.tasks_resume(self.mw.cfg)

        def _ok(res):
            self._update_task_btns(False)

        def _err(e):
            pass

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _task_item_text(self, t: dict) -> str:
        """单条任务的显示文本：状态图标 + 标签 + 耗时。"""
        icon = "⚡" if t.get("status") == "running" else "⏳"
        label = t.get("label") or "(未命名)"
        el = int(t.get("elapsed") or 0)
        if el >= 3600:
            dur = f"{el // 3600}h{(el % 3600) // 60}m"
        elif el >= 60:
            dur = f"{el // 60}m"
        else:
            dur = f"{el}s"
        return f"{icon} {label}（已等待 {dur}）"

    def update_task_panel(self, tasks: dict):
        """刷新任务列表（update_status 每 2 秒调一次）。"""
        running = tasks.get("running") or []
        queued = tasks.get("queued") or []
        paused = bool(tasks.get("paused"))
        # 按钮态与 bot 侧暂停态同步（2s 轮询兜底；点按钮时已即时刷新）
        self._update_task_btns(paused)
        self.task_list.blockSignals(True)
        self.task_list.clear()
        try:
            if not running and not queued:
                it = QListWidgetItem("（当前无任务）")
                it.setForeground(QColor("#9aa4b2"))
                self.task_list.addItem(it)
            else:
                if running:
                    h = QListWidgetItem("⚡ 正在进行")
                    f = QFont()
                    f.setBold(True)
                    h.setFont(f)
                    h.setForeground(QColor("#27ae60"))
                    h.setFlags(Qt.NoItemFlags)  # 不可选中
                    self.task_list.addItem(h)
                    for t in running:
                        it = QListWidgetItem(self._task_item_text(t))
                        it.setBackground(QColor("#eafaf1"))
                        it.setForeground(QColor("#1e8449"))
                        it.setToolTip(f"分类: {t.get('category', '')}\n"
                                      f"群: {t.get('group_id') or '-'}  发起: {t.get('user_id') or '-'}")
                        self.task_list.addItem(it)
                if queued:
                    # 2026-08-22：暂停中排队条目带 ⏸ 前缀（等待继续）
                    prefix = "⏸ " if paused else ""
                    h = QListWidgetItem(f"{prefix}⏳ 排队中（{len(queued)}）")
                    f = QFont()
                    f.setBold(True)
                    h.setFont(f)
                    h.setForeground(QColor("#e67e22"))
                    h.setFlags(Qt.NoItemFlags)
                    self.task_list.addItem(h)
                    for t in queued:
                        it = QListWidgetItem(self._task_item_text(t))
                        it.setBackground(QColor("#fdf6ec"))
                        it.setForeground(QColor("#9c640c"))
                        it.setToolTip(f"分类: {t.get('category', '')}\n"
                                      f"群: {t.get('group_id') or '-'}  发起: {t.get('user_id') or '-'}"
                                      + ("\n⏸ 任务序列已暂停，等待继续" if paused else ""))
                        self.task_list.addItem(it)
        finally:
            self.task_list.blockSignals(False)
        n = len(running) + len(queued)
        if paused:
            self.task_count_label.setText(
                f"⏸ 已暂停 · 等待中 {len(queued)}（正在进行 {len(running)}）" if n else "⏸ 已暂停")
        else:
            self.task_count_label.setText(
                f"正在进行 {len(running)} · 排队 {len(queued)}" if n else "当前无任务")

    def _add_panel_row(self, box: QGroupBox, label: str, value: str) -> QLabel:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(f"{label}：")
        lab.setStyleSheet("color: #7f8c8d;")
        lab.setMinimumWidth(56)
        val = QLabel(value)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val.setWordWrap(True)
        val.setStyleSheet("font-size: 13px;")
        row.addWidget(lab, 0)
        row.addWidget(val, 1)
        box._vlayout.addLayout(row)
        return val

    # ============================================================
    #  LLM 板块（第一行第二列：总开关 + 后端配置 + 状态）
    # ============================================================
    def _build_llm_panel(self):
        """LLM 配置全部在此板块（配置页已移除）：
        总开关 + 后端选择 + 远程/本地字段组（地址/模型/获取模型/max_tokens/并发/密钥）
        + 连接测试 + 用量统计。保存写 config.yaml + .env，可热生效。"""
        lay = self.llm_box._vlayout
        # 08-20：行距 6→10——卡被等高拉伸后，多出的 ~8px 由行距摊分吸收，
        # 底部内边距从 25px 收到 ~12px（原 6px 行距摊分后行距 13px 偏松 + 底部留白偏大）
        # 08-22：10→8——第一行板块高度过大（行高 303），5 个间隙共省 10px；
        # 12px 行距仍宽松，不影响美观（NapCat 卡 281px，LLM 降后 291 仍为主导行高）
        lay.setSpacing(8)

        # --- 顶行：总开关 + 后端选择 ---
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("总开关："))
        self.chk_llm_enabled = QPushButton("✅ 开启")
        self.chk_llm_enabled.setMinimumHeight(30)
        self.chk_llm_enabled.setCheckable(True)
        self.chk_llm_enabled.setChecked(True)
        self.chk_llm_enabled.setCursor(Qt.PointingHandCursor)
        self.chk_llm_enabled.setStyleSheet(
            "QPushButton:checked { background: #eafaf1; color: #27ae60; "
            "border: 1px solid #27ae60; border-radius: 4px; font-weight: bold; }"
            "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; }")
        self.chk_llm_enabled.toggled.connect(self._on_llm_enabled_toggled)
        self.chk_llm_enabled.setFixedWidth(100)  # 限宽：不再被拉伸占满
        top.addWidget(self.chk_llm_enabled)
        top.addSpacing(16)
        top.addWidget(QLabel("后端："))
        self.cmb_llm_backend = QComboBox()
        self.cmb_llm_backend.addItems(["remote（远程 API）", "local（本地 LLM）"])
        self.cmb_llm_backend.setToolTip(
            "remote=远程 OpenAI 兼容 API（DeepSeek/OpenAI/网关等）；\n"
            "local=本地 OpenAI 兼容服务（Ollama/vLLM 等）。下方两组字段均可编辑，保存时一并写入")
        self.cmb_llm_backend.currentIndexChanged.connect(self._on_backend_changed)
        top.addWidget(self.cmb_llm_backend, 1)  # 后端下拉占剩余宽度
        top.addStretch(1)
        lay.addLayout(top)

        # --- 字段区：远程/本地两页（后端选择只决定哪组"生效"，两组都能改）---
        self.llm_stack = QStackedWidget()

        # 页 0：远程
        page_r = QWidget()
        fr = QFormLayout(page_r)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setVerticalSpacing(5)  # 08-22：6→5，配合主行距 10→8 压缩第一行高度
        fr.setHorizontalSpacing(10)
        # 08-20：删除远程页提示文字（压缩第一行板块高度）
        # 行1：API 地址 + 并发上限（08-20 重排）
        self.ed_r_api = QLineEdit()
        self.ed_r_api.setPlaceholderText("如 https://api.deepseek.com/v1")
        self.ed_r_api.textEdited.connect(lambda *_: self._mark_llm_dirty())
        self.sp_r_parallel = NoWheelSpinBox()  # 禁滚轮改值（用户要求）
        self.sp_r_parallel.setRange(1, 1000)
        self.sp_r_parallel.setButtonSymbols(QSpinBox.NoButtons)
        self.sp_r_parallel.setFixedWidth(80)  # 08-20：88→80，消横向溢出（最大 1000 显示无碍）
        self.sp_r_parallel.valueChanged.connect(lambda *_: self._mark_llm_dirty())
        row_ra = QHBoxLayout()
        row_ra.setSpacing(6)
        row_ra.addWidget(self.ed_r_api, 1)
        lab_par = QLabel("并发：")
        lab_par.setStyleSheet("color: #7f8c8d;")
        row_ra.addWidget(lab_par)
        row_ra.addWidget(self.sp_r_parallel)
        fr.addRow("API 地址 / 并发上限", row_ra)
        # 行2：模型 + 获取模型按钮 + max_tokens（08-20 重排）
        self.ed_r_model = QLineEdit()
        self.ed_r_model.setPlaceholderText("模型名（如 deepseek-chat）")
        self.ed_r_model.setMinimumWidth(120)  # 08-20：150→120，消横向溢出（连带消竖直滚动条）
        self.ed_r_model.textEdited.connect(lambda *_: self._mark_llm_dirty())
        btn_fr = QPushButton("⬇ 获取模型")
        btn_fr.setFixedWidth(100)  # 加宽：避免文字被遮挡
        btn_fr.setToolTip("从该 API 的 /models 接口拉取可用模型列表（网关支持时可用）")
        btn_fr.clicked.connect(lambda: self._fetch_models(self.ed_r_api, self.ed_r_model))
        self.sp_r_tokens = NoWheelSpinBox()  # 禁滚轮改值（用户要求）
        self.sp_r_tokens.setRange(1024, 1000000)
        self.sp_r_tokens.setButtonSymbols(QSpinBox.NoButtons)  # 去掉上下箭头，直接输入
        self.sp_r_tokens.setFixedWidth(100)  # 08-20：110→100，消横向溢出（最大 1000000 显示无碍）
        self.sp_r_tokens.valueChanged.connect(lambda *_: self._mark_llm_dirty())
        row_rm = QHBoxLayout()
        row_rm.setSpacing(6)
        row_rm.addWidget(self.ed_r_model, 1)
        row_rm.addWidget(btn_fr)
        lab_tok = QLabel("max_tokens：")
        lab_tok.setStyleSheet("color: #7f8c8d;")
        row_rm.addWidget(lab_tok)
        row_rm.addWidget(self.sp_r_tokens)
        fr.addRow("模型 / max_tokens", row_rm)
        self.ed_r_key = QLineEdit()
        self.ed_r_key.setEchoMode(QLineEdit.Normal)  # 用户要求：GUI 中明文显示
        self.ed_r_key.setToolTip("远程 API 的 Bearer 密钥（.env 的 REMOTE_API_KEY）")
        self.ed_r_key.textEdited.connect(lambda *_: self._mark_llm_dirty())
        fr.addRow("REMOTE_API_KEY（密钥）", self.ed_r_key)
        self.llm_stack.addWidget(page_r)

        # 页 1：本地
        page_l = QWidget()
        fl = QFormLayout(page_l)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setVerticalSpacing(5)  # 08-22：6→5，与远程页一致
        fl.setHorizontalSpacing(10)
        # 08-20：删除本地页提示文字（压缩第一行板块高度）
        self.ed_l_api = QLineEdit()
        self.ed_l_api.setPlaceholderText("如 http://127.0.0.1:8000/v1")
        self.ed_l_api.textEdited.connect(lambda *_: self._mark_llm_dirty())
        fl.addRow("API 地址", self.ed_l_api)
        self.ed_l_model = QLineEdit()
        self.ed_l_model.setPlaceholderText("模型 ID（可点右侧按钮自动拉取）")
        self.ed_l_model.textEdited.connect(lambda *_: self._mark_llm_dirty())
        row_lm = QHBoxLayout()
        row_lm.setSpacing(6)
        row_lm.addWidget(self.ed_l_model, 1)
        btn_fl = QPushButton("⬇ 获取模型")
        btn_fl.setFixedWidth(100)  # 加宽：避免文字被遮挡
        btn_fl.setToolTip("从本地服务的 /models 接口拉取可用模型列表")
        btn_fl.clicked.connect(lambda: self._fetch_models(self.ed_l_api, self.ed_l_model))
        row_lm.addWidget(btn_fl)
        fl.addRow("模型", row_lm)
        self.ed_l_key = QLineEdit()
        self.ed_l_key.setEchoMode(QLineEdit.Normal)  # 用户要求：GUI 中明文显示
        self.ed_l_key.setToolTip("本地 LLM 的密钥（多数本地服务无需 key，可留空；.env 的 LLM_API_KEY）")
        self.ed_l_key.textEdited.connect(lambda *_: self._mark_llm_dirty())
        fl.addRow("LLM_API_KEY（密钥）", self.ed_l_key)
        self.llm_stack.addWidget(page_l)

        lay.addWidget(self.llm_stack)

        # --- 按钮行 ---
        btns = QHBoxLayout()
        self.btn_llm_test = QPushButton("🔌 连接测试")
        self.btn_llm_test.setMinimumHeight(30)
        self.btn_llm_test.clicked.connect(self._test_llm_from_overview)
        self.btn_llm_save = QPushButton("💾 保存配置（热生效）")
        self.btn_llm_save.setProperty("primary", True)
        self.btn_llm_save.setMinimumHeight(30)
        self.btn_llm_save.clicked.connect(self._save_llm)
        btns.addWidget(self.btn_llm_test, 1)
        btns.addWidget(self.btn_llm_save, 1)
        lay.addLayout(btns)

        # --- 状态行（轮询刷新：后端配置摘要）---
        self.lbl_llm_state = QLabel("—")
        self.lbl_llm_state.setWordWrap(True)
        self.lbl_llm_state.setStyleSheet("font-size: 12px; color: #7f8c8d;")
        lay.addWidget(self.lbl_llm_state)

        # --- 测试结果行（独立于状态行：轮询不覆盖，持久显示时间戳+耗时）---
        # 08-20 扩展：也显示其他 LLM 请求（bot 回复/人设更新等）——单行 + 超长省略号
        # 08-22：_ClickableLabel（QLabel 无 clicked 信号）——点击弹窗看完整输出
        self.lbl_llm_test_result = _ClickableLabel("")
        self.lbl_llm_test_result.setWordWrap(False)  # 强制单行（超长 elide，tooltip 看全文）
        self.lbl_llm_test_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_llm_test_result.setStyleSheet("font-size: 12px;")
        # 08-22：点击弹窗看完整输出（全文随写行存入 _llm_result_full）
        self.lbl_llm_test_result.setCursor(Qt.PointingHandCursor)
        self.lbl_llm_test_result.setToolTip(
            "点击弹出窗口查看完整 LLM 输出内容")
        self.lbl_llm_test_result.clicked.connect(self._open_llm_result_dialog)
        lay.addWidget(self.lbl_llm_test_result)
        # 结果行全文/元信息（写行时同步存，弹窗直接读——失败行不会误取
        # 上次成功请求的 full）
        self._llm_result_full = ""
        self._llm_result_meta = {}

        # --- 用量行（15s 自动刷新）---
        self.lbl_llm_usage = QLabel("")
        self.lbl_llm_usage.setWordWrap(True)
        self.lbl_llm_usage.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_llm_usage.setStyleSheet("font-size: 12px; color: #2980b9;")
        lay.addWidget(self.lbl_llm_usage)

        self._llm_dirty = False
        self._llm_saving = False
        self._init_llm_widgets()

        # 08-20：卡片被网格行高（左侧 NapCat 二维码卡很高）拉伸后，外层垂直布局
        # 08-20：移除原底部 addStretch(1)——等高拉伸后多余高度摊进行距，
        # 底部边框与 NapCat 对齐（之前 stretch 会把空隙沉底，两卡底部错开 75px）。
        # 行距 6px + 摊分 ~7px = 13px/行，无"空行"观感。

        # 用量自动刷新
        QTimer.singleShot(500, self._refresh_usage)
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(15000)
        self._usage_timer.timeout.connect(self._refresh_usage)
        self._usage_timer.start()

        # 最近 LLM 请求行（5s 轮询：bot 回复/人设更新等请求实时显示在测试结果行）
        self._shown_req_seq = 0          # 已显示请求的 seq（防轮询重复写/覆盖更新）
        self._suppress_recent_until = 0.0  # 连接测试刚完成时抑制覆盖（给用户看测试反馈）
        self._recent_timer = QTimer(self)
        self._recent_timer.setInterval(5000)
        self._recent_timer.timeout.connect(self._refresh_recent_request)
        self._recent_timer.start()

    def _set_test_line(self, text: str, color: str = "", full: str = "",
                       meta: dict | None = None) -> None:
        """写测试结果行：强制单行（超长 elide 省略号），tooltip 存全文。

        full/meta（08-22）：完整 LLM 输出 + 元信息随写入 _llm_result_full/_meta，
        点击结果行弹窗展示（失败行传 full="" 清空，防误取上次成功请求全文）。
        """
        self.lbl_llm_test_result.setToolTip(text)
        self._llm_result_full = full
        self._llm_result_meta = meta or {}
        w = max(20, self.lbl_llm_test_result.width() - 8)
        fm = self.lbl_llm_test_result.fontMetrics()
        shown = fm.elidedText(text, Qt.ElideRight, w) if fm.horizontalAdvance(text) > w else text
        self.lbl_llm_test_result.setText(shown)
        if color:
            self.lbl_llm_test_result.setStyleSheet(f"font-size: 12px; color: {color};")

    def _open_llm_result_dialog(self):
        """点击测试结果行：弹窗展示完整 LLM 输出（08-22）。

        数据源=_llm_result_full（写行时同步存）；行内无全文（老数据/失败行）
        时显示行内容占位提示，不报错。
        """
        full = self._llm_result_full or self.lbl_llm_test_result.text()
        meta = self._llm_result_meta or {}
        dlg = QDialog(self)
        dlg.setWindowTitle("LLM 输出内容")
        dlg.resize(760, 520)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(8)
        # 元信息行（时间/来源/模型，有才显示）
        bits = [meta.get(k, "") for k in ("time", "source", "model") if meta.get(k)]
        if bits:
            lab = QLabel(" · ".join(bits))
            lab.setStyleSheet("color: #7f8c8d; font-size: 12px;")
            v.addWidget(lab)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(full)
        body.setStyleSheet("font-size: 13px;")
        body.setPlaceholderText("（该行无完整输出内容——bot 旧版未记录全文，或请求失败）")
        v.addWidget(body, 1)
        btn_copy = QPushButton("📋 复制全文")
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(full))
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(btn_copy)
        brow.addWidget(btn_close)
        v.addLayout(brow)
        dlg.exec()

    def _sync_shown_req_seq(self):
        """连接测试刚完成：把 bot 侧当前 seq 标记为已显示，
        防止 5s 轮询用 📨 行覆盖刚显示的 ✅/❌ 测试反馈。"""
        try:
            d = api_client.get_recent_request(self.mw.cfg)
            if isinstance(d, dict) and d.get("seq"):
                self._shown_req_seq = max(self._shown_req_seq, int(d.get("seq", 0)))
        except Exception:
            # bot 不可达等：给 10s 抑制窗口兜底
            self._suppress_recent_until = time.time() + 10

    def _refresh_recent_request(self):
        """5s 轮询 bot 侧最近一次 LLM 请求，显示到测试结果行。

        规则：
        - seq 未变 → 跳过（不重复写）
        - 连接测试刚完成（10s 抑制期）→ 跳过（给用户看测试反馈）
        - 显示格式同测试消息：时间 · 来源 · 模型 · 回复预览（单行，超长省略号）
        """
        if time.time() < self._suppress_recent_until:
            return

        def _do():
            return api_client.get_recent_request(self.mw.cfg)

        def _ok(d):
            if not isinstance(d, dict) or not d.get("seq"):
                return
            seq = int(d.get("seq", 0))
            if seq <= self._shown_req_seq:
                return  # 没有更新的请求（或已显示过）
            self._shown_req_seq = seq
            model = d.get("model", "") or "?"
            if len(model) > 26:
                model = model[:25] + "…"
            line = (f"📨 {d.get('time', '?')} {d.get('source', 'LLM')} · "
                    f"{model} · {d.get('preview', '')[:50]}")
            # 08-22：全文随写入（点击结果行弹窗看完整输出）
            # finish_reason=length → 生成被 max_tokens 截断，弹窗顶部提示
            full_text = str(d.get("full") or d.get("preview") or "")
            fr = str(d.get("finish_reason") or "")
            if fr == "length":
                full_text = (f"⚠️ 本次生成不完整：输出达到 max_tokens 上限被截断"
                             f"（finish_reason=length）。以下为已生成的部分，"
                             f"如需完整内容请调大 LLM 板块的 max tokens 后重试。\n\n"
                             + full_text)
            self._set_test_line(
                line, "#2c3e50",
                full=full_text,
                meta={"time": d.get("time", ""), "source": d.get("source", ""),
                      "model": d.get("model", "")})

        def _err(e):
            pass  # bot 未运行时静默（下轮再试）

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _init_llm_widgets(self):
        """从 mw.yaml_cfg / mw.env_cfg 初始化 LLM 控件（不触发 dirty）。"""
        y = getattr(self.mw, "yaml_cfg", None) or {}
        llm = y.get("llm", {})
        enabled = llm.get("enabled", True)
        # 后端名兼容旧值 deepseek → remote
        backend = str(llm.get("backend", "remote")).lower()
        backend = "remote" if backend in ("deepseek", "remote") else backend
        self.chk_llm_enabled.blockSignals(True)
        self.chk_llm_enabled.setChecked(bool(enabled))
        self.chk_llm_enabled.blockSignals(False)
        self.chk_llm_enabled.setText("✅ 开启" if enabled else "⛔ 关闭")
        # 远程字段（新键优先，兼容旧键 deepseek_*）
        self.ed_r_api.setText(str(llm.get("remote_api") or llm.get("deepseek_api", "")))
        self.ed_r_model.setText(str(llm.get("remote_model") or llm.get("deepseek_model", "")))
        self.sp_r_tokens.setValue(int(llm.get("remote_max_tokens")
                                     or llm.get("deepseek_max_tokens", 131072)))
        self.sp_r_parallel.setValue(int(llm.get("remote_max_parallel")
                                        or llm.get("deepseek_max_parallel", 10)))
        # 本地字段
        self.ed_l_api.setText(str(llm.get("local_api", "")))
        self.ed_l_model.setText(str(llm.get("local_model", "")))
        # 密钥（.env，兼容旧键名）
        env = getattr(self.mw, "env_cfg", None) or {}
        self.ed_r_key.setText(env.get("REMOTE_API_KEY") or env.get("DEEPSEEK_API_KEY", ""))
        self.ed_l_key.setText(env.get("LLM_API_KEY", ""))
        # 后端选择 → 对应字段页
        idx = 0 if backend == "remote" else 1
        self.cmb_llm_backend.blockSignals(True)
        self.cmb_llm_backend.setCurrentIndex(idx)
        self.cmb_llm_backend.blockSignals(False)
        self.llm_stack.setCurrentIndex(idx)
        self._llm_dirty = False
        self._refresh_llm_state_label()

    def _on_backend_changed(self):
        """切后端：只显示对应字段页（两组字段独立保存，互不覆盖）。"""
        self.llm_stack.setCurrentIndex(self.cmb_llm_backend.currentIndex())
        self._mark_llm_dirty()

    def _refresh_llm_state_label(self):
        st = self._last_status or {}
        llm = st.get("llm", {})
        if not llm:
            return
        if not llm.get("enabled", True):
            self.lbl_llm_state.setText("🔕 LLM 调用已关闭（聊天/画像/游戏判定等降级）")
            self.lbl_llm_state.setStyleSheet("font-size: 12px; color: #e67e22;")
            return
        bits = [f"✅ 已配置 · {llm.get('backend', '?')}"]
        if llm.get("api"):
            bits.append(llm["api"])
        if llm.get("model"):
            bits.append(llm["model"])
        self.lbl_llm_state.setText(" · ".join(bits))
        self.lbl_llm_state.setStyleSheet("font-size: 12px; color: #27ae60;")

    # ============================================================
    #  配置面板（第二行第二列，2026-08-22）
    #  ComfyUI（从配置页搬入）+ 赛博模仿全局开关/概率 + 三个定时任务
    #  全局总闸。开关语义：全局 master——关=所有群不跑，开=仍按各群
    #  群级开关（群组集群页）判定。保存写 config.yaml scheduler 段 +
    #  comfyui 段 → POST /config 热重载（SCHED_* / COMFYUI_URL 即时生效）。
    # ============================================================
    def _build_cfg_panel(self):
        lay = self.cfg_panel_box._vlayout
        lay.setSpacing(4)  # 4（非 8）：面板内容 sizeHint≈360px 须 ≤ 网格行高 363，
        # 8 时 395px 超出 → 底部保存按钮/任务行被截（08-22 渲染实测）

        # --- ComfyUI（AI 画图）：地址 + 连接测试（08-22 自「配置」页搬入）---
        row_comfy = QHBoxLayout()
        row_comfy.setContentsMargins(0, 0, 0, 0)
        comfy_lab = QLabel("ComfyUI 地址")
        comfy_lab.setStyleSheet("color: #7f8c8d;")
        comfy_lab.setMinimumWidth(90)
        row_comfy.addWidget(comfy_lab, 0)
        self.ed_comfy_url = QLineEdit()
        self.ed_comfy_url.setPlaceholderText("如 http://127.0.0.1:8188")
        self.ed_comfy_url.textEdited.connect(lambda *_: self._mark_cfgpanel_dirty())
        row_comfy.addWidget(self.ed_comfy_url, 1)
        self.btn_test_comfy = QPushButton("🔌 测试")
        # 宽度按文字像素宽 + 内边距（08-22 用户反馈按钮显示不全）：
        # fixedWidth(64) 装不下 emoji+文字（style sizeHint 把 emoji 算窄，
        # 直接 sizeHint 也不准）→ 同顶栏按钮配方：文字宽 + 44
        self.btn_test_comfy.setMinimumWidth(
            self.btn_test_comfy.fontMetrics().horizontalAdvance(self.btn_test_comfy.text()) + 44)
        self.btn_test_comfy.setMinimumHeight(28)
        self.btn_test_comfy.setToolTip("探活 ComfyUI /system_stats（用当前表单地址，不写盘）")
        self.btn_test_comfy.clicked.connect(self._test_comfy_from_panel)
        row_comfy.addWidget(self.btn_test_comfy)
        lay.addLayout(row_comfy)

        # --- 赛博模仿：全局总开关 + 概率 % ---
        row_mimic = QHBoxLayout()
        row_mimic.setContentsMargins(0, 0, 0, 0)
        self.sw_mimic = QSwitch("赛博模仿")
        self.sw_mimic.setToolTip("全局总闸：关闭=所有群都不触发（群级开关不受影响）；"
                                  "开启后仍按各群群级开关（群组集群页）判定")
        self.sw_mimic.toggled.connect(lambda *_: self._mark_cfgpanel_dirty())
        row_mimic.addWidget(self.sw_mimic)
        row_mimic.addSpacing(12)
        prob_lab = QLabel("触发概率 %")
        prob_lab.setStyleSheet("color: #7f8c8d;")
        row_mimic.addWidget(prob_lab)
        self.sp_mimic_prob = NoWheelSpinBox()
        self.sp_mimic_prob.setRange(0, 100)
        self.sp_mimic_prob.setButtonSymbols(QSpinBox.NoButtons)
        self.sp_mimic_prob.setFixedWidth(70)
        self.sp_mimic_prob.setToolTip("全局触发概率（0=停用）。仅赛博模仿开关+群级开关都开启时生效")
        self.sp_mimic_prob.valueChanged.connect(lambda *_: self._mark_cfgpanel_dirty())
        row_mimic.addWidget(self.sp_mimic_prob)
        row_mimic.addStretch(1)
        lay.addLayout(row_mimic)

        # --- 三个定时任务全局总闸（QSwitch 各一行；08-22 删小字提示/分隔线，
        #     面板高度过大瘦身；开关名已含执行时间，语义见各开关 tooltip）---
        # 08-22 对齐：QSwitch 默认 Fixed 宽度（=文字宽+轨道），轨道锚定控件右缘 →
        # 三行文字长短不一，轨道左右错位 33px（16:xx 渲染实测）。
        # 改水平 Expanding：控件拉满行宽，文字左对齐（x=0 绘制）、轨道统一贴
        # 面板内容右缘 → 三个轨道同列对齐，垂直间距由面板 spacing(4) 控制。
        self.sw_daily_report = QSwitch("📋 每日总结 / 评选（11:30 / 22:30）")
        self.sw_daily_report.setToolTip("全局总闸：关闭=所有群不生成半日总结/评选报告"
                                         "（可用 /总结 /评选 手动补发）；开启后按各群群级开关判定")
        self.sw_daily_report.toggled.connect(lambda *_: self._mark_cfgpanel_dirty())
        self.sw_question_refill = QSwitch("🎲 题库自动补充（每日 0 点）")
        self.sw_question_refill.setToolTip("全局总闸：关闭=不自动预填充真心话大冒险题库；"
                                            "开启后按各群群级开关判定")
        self.sw_question_refill.toggled.connect(lambda *_: self._mark_cfgpanel_dirty())
        self.sw_persona_update = QSwitch("🎭 人设 / 画像更新（每日 0 点）")
        self.sw_persona_update.setToolTip("全局总闸：关闭=不自动联合更新人设+画像；"
                                           "开启后按各群群级开关判定")
        self.sw_persona_update.toggled.connect(lambda *_: self._mark_cfgpanel_dirty())
        for _sw in (self.sw_daily_report, self.sw_question_refill, self.sw_persona_update):
            _sw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            lay.addWidget(_sw)

        # --- 底部按钮行（08-22：「其他设置」与保存同行，高度零增加）---
        # 配置标签页已删除，其内容（listen/control_api/assets/debug/paths 五段）
        # 迁入「⚙️ 其他设置」弹窗（gui/config_settings_dialogs.py）
        # 08-23：按钮行前加 stretch=1 钉底——面板被拉伸时按钮行贴面板底边，
        # 与消息管理面板底部保存按钮对齐（原实现对余量摊法不同，窗口较高时
        # 按钮行错位 ~20% 余量，用户报"没对齐"）。
        lay.addStretch(1)
        btns_row = QHBoxLayout()
        btns_row.setSpacing(8)
        self.btn_cfgpanel_other = QPushButton("⚙️ 其他设置")
        self.btn_cfgpanel_other.setMinimumHeight(30)
        # 动态宽度（emoji 会让 sizeHint 算窄切字）
        self.btn_cfgpanel_other.setMinimumWidth(
            self.btn_cfgpanel_other.fontMetrics().horizontalAdvance(self.btn_cfgpanel_other.text()) + 44)
        self.btn_cfgpanel_other.setToolTip(
            "监听/控制 API/文件资产/调试/数据目录（原「⚙️ 配置」标签页内容，08-22 迁入）")
        self.btn_cfgpanel_other.clicked.connect(self._open_cfg_dialog)
        btns_row.addWidget(self.btn_cfgpanel_other, 1)
        self.btn_cfgpanel_save = QPushButton("💾 保存配置（热生效）")
        self.btn_cfgpanel_save.setProperty("primary", True)
        self.btn_cfgpanel_save.setMinimumHeight(30)
        self.btn_cfgpanel_save.setMinimumWidth(
            self.btn_cfgpanel_save.fontMetrics().horizontalAdvance(self.btn_cfgpanel_save.text()) + 44)
        self.btn_cfgpanel_save.clicked.connect(self._save_cfg_panel)
        btns_row.addWidget(self.btn_cfgpanel_save, 1)
        lay.addLayout(btns_row)

        self._cfgpanel_dirty = False
        self._cfgpanel_saving = False
        self._init_cfg_panel_widgets()

    def _init_cfg_panel_widgets(self):
        """从 mw.yaml_cfg 初始化配置面板控件（不触发 dirty）。"""
        y = getattr(self.mw, "yaml_cfg", None) or {}
        self.ed_comfy_url.setText(str((y.get("comfyui") or {}).get("url", "")))
        s = y.get("scheduler") or {}
        # 缺省回退 DEFAULTS（与 bot 侧 flatten 同逻辑：yaml 缺项=默认）
        from core.config import DEFAULTS as _D
        sd = _D.get("scheduler", {})
        self.sw_mimic.blockSignals(True)
        self.sw_mimic.setChecked(bool(s.get("mimic_enabled", sd.get("mimic_enabled", False))))
        self.sw_mimic.blockSignals(False)
        self.sp_mimic_prob.blockSignals(True)
        self.sp_mimic_prob.setValue(int(s.get("mimic_probability", sd.get("mimic_probability", 0))))
        self.sp_mimic_prob.blockSignals(False)
        self.sw_daily_report.blockSignals(True)
        self.sw_daily_report.setChecked(bool(s.get("daily_report", sd.get("daily_report", True))))
        self.sw_daily_report.blockSignals(False)
        self.sw_question_refill.blockSignals(True)
        self.sw_question_refill.setChecked(bool(s.get("question_refill", sd.get("question_refill", True))))
        self.sw_question_refill.blockSignals(False)
        self.sw_persona_update.blockSignals(True)
        self.sw_persona_update.setChecked(bool(s.get("persona_update", sd.get("persona_update", True))))
        self.sw_persona_update.blockSignals(False)
        self._cfgpanel_dirty = False

    def _mark_cfgpanel_dirty(self):
        self._cfgpanel_dirty = True

    def _save_cfg_panel(self):
        """保存配置面板：comfyui.url + scheduler 段 → 热重载。"""
        if self._cfgpanel_saving:
            return
        y = self.mw.yaml_cfg
        y.setdefault("comfyui", {})["url"] = self.ed_comfy_url.text().strip()
        sched = y.setdefault("scheduler", {})
        sched["mimic_enabled"] = bool(self.sw_mimic.isChecked())
        sched["mimic_probability"] = int(self.sp_mimic_prob.value())
        sched["daily_report"] = bool(self.sw_daily_report.isChecked())
        sched["question_refill"] = bool(self.sw_question_refill.isChecked())
        sched["persona_update"] = bool(self.sw_persona_update.isChecked())

        self._cfgpanel_saving = True
        self.btn_cfgpanel_save.setEnabled(False)

        def _do():
            api_client.save_yaml(y)
            report = api_client.reload_config(self.mw.cfg)
            # 同步 GUI 侧扁平键（SCHED_* / COMFYUI_URL），与 bot 侧同值
            try:
                from core.config import flatten_yaml_tree
                fresh = flatten_yaml_tree(api_client.load_yaml())
            except Exception:
                fresh = {}
            for k in ("SCHED_DAILY_REPORT", "SCHED_QUESTION_REFILL",
                      "SCHED_PERSONA_UPDATE", "SCHED_MIMIC_ENABLED",
                      "SCHED_MIMIC_PROBABILITY", "COMFYUI_URL"):
                if k in fresh:
                    self.mw.cfg[k] = fresh[k]
            self.mw.yaml_cfg = api_client.load_yaml()
            return report

        def _ok(report):
            self._cfgpanel_saving = False
            self._cfgpanel_dirty = False
            self.btn_cfgpanel_save.setEnabled(True)
            flash_button(self.btn_cfgpanel_save)
            n = len(report.get("applied") or [])
            self.mw.statusBar().showMessage(f"配置面板已保存并热重载（{n} 项变更）", 5000)

        def _err(e):
            self._cfgpanel_saving = False
            self.btn_cfgpanel_save.setEnabled(True)
            self.mw.statusBar().showMessage(
                f"⚠️ 配置已写入 config.yaml，但 bot 未运行或热重载失败（{str(e)[:60]}）——启动后生效", 8000)

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _open_cfg_dialog(self):
        """打开「其他设置」弹窗（原「⚙️ 配置」标签页内容，08-22 迁入）。

        每次点按钮新建弹窗（同 AI 聊天参数弹窗模式）：值从 mw.yaml_cfg 现读现填，
        无共享状态、无脏数据。保存后弹窗内 _refresh_mw_cfg 已同步 mw.cfg/yaml_cfg。
        """
        from config_settings_dialogs import ConfigSettingsDialog
        dlg = ConfigSettingsDialog(self.mw)
        dlg.exec()

    def _test_comfy_from_panel(self):
        """ComfyUI 连接测试：用当前表单地址（不写盘），探活 /system_stats。
        08-22：结果改弹窗输出（原面板内 lbl_comfy_result 预留位删除，面板瘦身）。"""
        url = self.ed_comfy_url.text().strip()
        if not url:
            QMessageBox.warning(self, "ComfyUI 测试", "未填 ComfyUI 地址")
            return
        self.btn_test_comfy.setEnabled(False)
        self.btn_test_comfy.setText("⏳ 测试中…")

        def _do():
            # 用表单地址构造临时 cfg（不动 mw.cfg，不写盘）
            import copy
            tmp = dict(self.mw.cfg)
            tmp["COMFYUI_URL"] = url
            return api_client.test_comfyui(tmp, url=url)

        def _show_ok(r):
            self.btn_test_comfy.setEnabled(True)
            self.btn_test_comfy.setText("🔌 测试")
            if r.get("ok"):
                extra = ""
                ver = r.get("version") or (r.get("data") or {}).get("version")
                if ver:
                    extra = f"（版本 {ver}）"
                QMessageBox.information(self, "ComfyUI 测试", f"✅ 可达{extra}\n{url}")
            else:
                QMessageBox.critical(self, "ComfyUI 测试",
                                     f"❌ {r.get('error', '?')}\n{url}")

        def _show_err(e):
            self.btn_test_comfy.setEnabled(True)
            self.btn_test_comfy.setText("🔌 测试")
            QMessageBox.critical(self, "ComfyUI 测试", f"❌ {e}")

        w = Worker(_do)
        w.finished_ok.connect(_show_ok)
        w.finished_err.connect(_show_err)
        w.start()
        self.mw._track(w)

    def _mark_llm_dirty(self):
        self._llm_dirty = True

    def _llm_backend_key(self) -> str:
        return "remote" if self.cmb_llm_backend.currentIndex() == 0 else "local"

    def _on_llm_enabled_toggled(self, checked: bool):
        """总开关切换：即时写 yaml + 通知 bot 热重载（llm.enabled 可热生效）。"""
        if self._llm_saving:
            return
        y = self.mw.yaml_cfg
        y.setdefault("llm", {})["enabled"] = bool(checked)
        self.chk_llm_enabled.setText("✅ 开启" if checked else "⛔ 关闭")

        def _do():
            api_client.save_yaml(y)
            return api_client.reload_config(self.mw.cfg)

        def _ok(report):
            self.mw.statusBar().showMessage(
                f"LLM 总开关已{'开启' if checked else '关闭'}（热生效）", 4000)
            self._refresh_llm_state_label()

        def _err(e):
            self.mw.statusBar().showMessage(f"LLM 总开关切换失败: {e}", 4000)

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _save_llm(self):
        """保存全部 LLM 配置（两组字段 + 密钥）：写 yaml + .env → 热重载。"""
        y = self.mw.yaml_cfg
        llm = y.setdefault("llm", {})
        bk = self._llm_backend_key()
        # 远程字段
        llm["remote_api"] = self.ed_r_api.text().strip()
        llm["remote_model"] = self.ed_r_model.text().strip()
        llm["remote_max_tokens"] = self.sp_r_tokens.value()
        llm["remote_max_parallel"] = self.sp_r_parallel.value()
        # 本地字段
        llm["local_api"] = self.ed_l_api.text().strip()
        llm["local_model"] = self.ed_l_model.text().strip()
        # 后端选择 + 总开关
        llm["backend"] = bk
        llm["enabled"] = bool(self.chk_llm_enabled.isChecked())
        # 密钥（合并现有 .env，不丢其它字段）
        env = dict(getattr(self.mw, "env_cfg", None) or {})
        env["REMOTE_API_KEY"] = self.ed_r_key.text().strip()
        env["LLM_API_KEY"] = self.ed_l_key.text().strip()

        if bk == "remote" and not env["REMOTE_API_KEY"]:
            if not self.mw.confirm("密钥缺失", "llm.backend=remote 但未填 REMOTE_API_KEY，\n将回退本地 LLM。继续保存？"):
                return
        if bk == "local" and not llm["local_api"]:
            if not self.mw.confirm("配置不完整", "llm.backend=local 但未填本地 API 地址。继续保存？"):
                return

        self._llm_saving = True
        self.btn_llm_save.setEnabled(False)

        def _do():
            api_client.save_yaml(y)
            api_client.save_env(env)
            report = api_client.reload_config(self.mw.cfg)
            # 同步 mw.cfg / mw.env_cfg
            from core.config import flatten_yaml_tree
            new_cfg = flatten_yaml_tree(y)
            new_cfg["REMOTE_API_KEY"] = env.get("REMOTE_API_KEY", "")
            new_cfg["LLM_API_KEY"] = env.get("LLM_API_KEY", "")
            self.mw.cfg.clear()
            self.mw.cfg.update(new_cfg)
            self.mw.yaml_cfg = y
            self.mw.env_cfg = env
            return report

        def _ok(report):
            self._llm_saving = False
            self._llm_dirty = False
            self.btn_llm_save.setEnabled(True)
            flash_button(self.btn_llm_save)  # 点击反馈：按钮文字短暂变"✅ 已保存"
            restart = report.get("restart_required", [])
            self.mw.statusBar().showMessage(
                f"LLM 配置已保存（热生效）{'；需重启: ' + ','.join(restart) if restart else ''}",
                5000)
            self._refresh_llm_state_label()

        def _err(e):
            self._llm_saving = False
            self.btn_llm_save.setEnabled(True)
            self.mw.statusBar().showMessage(f"LLM 配置保存失败: {e}", 5000)

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _test_llm_from_overview(self):
        """连接测试：默认用 bot 当前生效配置；表单有未保存改动时先询问是否保存。

        修复（08-20）：原逻辑 dirty 时静默保存——用户切换下拉框看看 remote
        页再点测试，backend 就被写盘成 remote（用户没点保存，状态却变了，
        下次打开软件"自动切到 remote"）。现在测试前明确询问。"""
        if self._llm_dirty:
            if not self.mw.confirm(
                    "有未保存的改动",
                    "LLM 表单有未保存的改动（含后端选择）。\n\n"
                    "是 = 先保存再用新配置测试\n"
                    "否 = 不保存，用 bot 当前生效配置测试"):
                self._do_test_llm()  # 直接测当前生效配置
            else:
                self._save_llm()
                self.mw.statusBar().showMessage("已保存，2 秒后连接测试…", 3000)
                QTimer.singleShot(2000, self._do_test_llm)
        else:
            self._do_test_llm()

    def _do_test_llm(self):
        import time as _time
        self.btn_llm_test.setEnabled(False)
        self.lbl_llm_state.setText("⏳ 连接测试中…")
        self.lbl_llm_test_result.setText("")

        def _ok(r):
            self.btn_llm_test.setEnabled(True)
            ts = _time.strftime("%H:%M:%S")
            el = r.get("elapsed")
            el_s = f" · {el}s" if el is not None else ""
            if r.get("ok"):
                # 结果写入独立行（单行 elide，tooltip 全文；轮询不覆盖）
                reply_full = r.get("detail") or r.get("reply") or ""
                text = (f"✅ {ts} 连接正常{el_s} · 模型 {r.get('model', '?')} · "
                        f"回复: {reply_full[:50]}")
                # 08-22：全文随写入（点击结果行弹窗看完整输出）
                self._set_test_line(
                    text, "#27ae60",
                    full=str(reply_full)[:4000],
                    meta={"time": ts, "source": "连接测试",
                          "model": r.get("model", "")})
                # 同步 bot 侧 seq（测试本身也记了最近请求，防 5s 轮询用 📨 行覆盖 ✅ 反馈）
                self._sync_shown_req_seq()
            else:
                text = f"❌ {ts} 连接失败{el_s} · {(r.get('error', '未知') or '')[:60]}"
                self._set_test_line(text, "#e74c3c")
            # 状态行恢复为配置摘要
            self._refresh_llm_state_label()

        def _err(e):
            self.btn_llm_test.setEnabled(True)
            self._set_test_line(f"❌ {_time.strftime('%H:%M:%S')} 连接测试异常: {str(e)[:50]}", "#e74c3c")
            self._refresh_llm_state_label()

        w = Worker(lambda: api_client.test_llm(self.mw.cfg))
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    # ============================================================
    #  消息管理板块（第二行第一列：接收/发送/存档/保留期）
    # ============================================================
    _RETENTION_ITEMS = ["永久保留", "7 天", "30 天", "90 天", "180 天", "365 天"]
    _RETENTION_DAYS = [0, 7, 30, 90, 180, 365]

    def _build_msg_panel(self):
        """消息管理：接收/发送总开关 + 范围 + 类型子开关 + 撤回存档
        + 保留期（文本/媒体分别设置）+ 存档路径。保存热生效。"""
        lay = self.msg_box._vlayout

        # --- 第一行：接收/发送总开关 ---
        row_sw = QHBoxLayout()
        row_sw.setContentsMargins(0, 0, 0, 0)
        self.chk_recv = QPushButton("✅ 接收开启")
        self.chk_recv.setCheckable(True)
        self.chk_recv.setChecked(True)
        self.chk_recv.setFixedWidth(96)
        self.chk_recv.setCursor(Qt.PointingHandCursor)
        self.chk_recv.setStyleSheet(
            "QPushButton:checked { background: #eafaf1; color: #27ae60; "
            "border: 1px solid #27ae60; border-radius: 4px; font-weight: bold; }"
            "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; }")
        self.chk_recv.toggled.connect(self._on_msg_toggled)
        row_sw.addWidget(self.chk_recv)
        row_sw.addSpacing(10)
        self.chk_send = QPushButton("✅ 发送开启")
        self.chk_send.setCheckable(True)
        self.chk_send.setChecked(True)
        self.chk_send.setFixedWidth(96)
        self.chk_send.setCursor(Qt.PointingHandCursor)
        self.chk_send.setStyleSheet(
            "QPushButton:checked { background: #eafaf1; color: #27ae60; "
            "border: 1px solid #27ae60; border-radius: 4px; font-weight: bold; }"
            "QPushButton { border: 1px solid #bdc3c7; border-radius: 4px; }")
        self.chk_send.toggled.connect(self._on_msg_toggled)
        row_sw.addWidget(self.chk_send)
        hint_sw = QLabel("总开关：控制 bot 是否允许接收/发送 QQ 消息")
        hint_sw.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        row_sw.addWidget(hint_sw, 1)
        lay.addLayout(row_sw)

        # --- 接收消息类型子开关（08-21：文字/图片/语音/视频/消息记录；
        #     文件开关移至撤回行，避免一行 6 个太挤）---
        row_types = QHBoxLayout()
        row_types.setContentsMargins(0, 0, 0, 0)
        self.chk_recv_text = QSwitch("文字")
        self.chk_recv_img = QSwitch("图片")
        self.chk_recv_voice = QSwitch("语音")
        self.chk_recv_video = QSwitch("视频")
        self.chk_recv_fwd = QSwitch("消息记录")
        for c in (self.chk_recv_text, self.chk_recv_img, self.chk_recv_voice,
                  self.chk_recv_video, self.chk_recv_fwd):
            c.setChecked(True)
            c.toggled.connect(lambda *_: self._mark_msg_dirty())
            row_types.addWidget(c)
        row_types.addStretch(1)
        lay.addLayout(row_types)
        # 08-21：开关=接收门控（关闭=该类型消息不接收不存档）；
        # 混合消息中关闭的类型仍存档 URL（skipped 行），事后可补下载
        self.chk_recv_file = QSwitch("文件")  # 08-21 移到撤回行（见下）
        self.chk_recv_file.setChecked(True)
        self.chk_recv_file.toggled.connect(lambda *_: self._mark_msg_dirty())

        # --- 第四行：撤回存档子开关（08-21：文件接收开关移到"保存撤回消息"左边，
        #     与类型开关行分家，两行都不挤）---
        row_recall = QHBoxLayout()
        row_recall.setContentsMargins(0, 0, 0, 0)
        row_recall.addWidget(self.chk_recv_file)
        row_recall.addSpacing(20)
        self.chk_recall_msg = QSwitch("保存撤回消息")
        self.chk_recall_msg.setChecked(True)
        self.chk_recall_msg.toggled.connect(self._on_recall_toggled)
        row_recall.addWidget(self.chk_recall_msg)
        self.chk_recall_media = QSwitch("保存多媒体撤回")
        self.chk_recall_media.setChecked(True)
        self.chk_recall_media.toggled.connect(self._on_recall_toggled)
        row_recall.addWidget(self.chk_recall_media)
        row_recall.addStretch(1)
        lay.addLayout(row_recall)

        # --- 第五行：保留期（文本/媒体分别设置，0=永久）---
        row_ret = QHBoxLayout()
        row_ret.setContentsMargins(0, 0, 0, 0)
        row_ret.addWidget(QLabel("文本保留"))
        self.cmb_text_ret = QComboBox()
        self.cmb_text_ret.addItems(self._RETENTION_ITEMS)
        self.cmb_text_ret.currentIndexChanged.connect(lambda *_: self._mark_msg_dirty())
        row_ret.addWidget(self.cmb_text_ret, 1)
        row_ret.addSpacing(10)
        row_ret.addWidget(QLabel("媒体保留"))
        self.cmb_media_ret = QComboBox()
        self.cmb_media_ret.addItems(self._RETENTION_ITEMS)
        self.cmb_media_ret.currentIndexChanged.connect(lambda *_: self._mark_msg_dirty())
        row_ret.addWidget(self.cmb_media_ret, 1)
        lay.addLayout(row_ret)
        # 08-20：删除"每天 3:00 自动清理超期数据"小字提示（用户要求）

        # --- 第六行：存档路径 ---
        row_path = QHBoxLayout()
        row_path.setContentsMargins(0, 0, 0, 0)
        self.ed_archive_dir = QLineEdit()
        self.ed_archive_dir.setPlaceholderText("默认 data/archive")
        self.ed_archive_dir.setToolTip("媒体存档目录（相对程序目录或绝对路径），热生效")
        self.ed_archive_dir.textEdited.connect(lambda *_: self._mark_msg_dirty())
        row_path.addWidget(self.ed_archive_dir, 1)
        btn_restore = QPushButton("↺ 恢复默认")
        btn_restore.setFixedWidth(96)  # 加宽：避免文字显示不全
        btn_restore.setToolTip("恢复为默认存档目录 data/archive")
        btn_restore.clicked.connect(self._restore_default_dir)
        row_path.addWidget(btn_restore)
        lay.addLayout(row_path)

        # --- 保存按钮 ---
        # 08-23：按钮行前加 stretch=1 钉底——面板被网格拉伸到自然高度以上时，
        # 按钮行贴面板底边（与配置面板底部按钮行对齐；不加 stretch 时两面板
        # 对余量摊法不同，配置面板按钮行会悬在中间，错位 ~20% 余量）。
        # 自然高度（行高=sizeHint）时 stretch 余量为 0，布局不变。
        lay.addStretch(1)
        self.btn_msg_save = QPushButton("💾 保存消息设置（热生效）")
        self.btn_msg_save.setProperty("primary", True)
        self.btn_msg_save.setMinimumHeight(30)
        self.btn_msg_save.clicked.connect(self._save_msg)
        lay.addWidget(self.btn_msg_save)

        # 08-20：删除底部免责声明（压缩第二行板块高度）

        self._msg_dirty = False
        self._msg_saving = False
        self._init_msg_widgets()

    def _init_msg_widgets(self):
        """从 mw.yaml_cfg 初始化消息管理控件（不触发 dirty）。"""
        y = getattr(self.mw, "yaml_cfg", None) or {}
        msg = y.get("msg", {})
        ar = y.get("archive", {})
        # 撤回开关（msg 新键优先，回退 archive 旧键）
        recall_msg = msg.get("save_recall_messages", ar.get("save_recall_messages", True))
        recall_media = msg.get("save_recall_images", ar.get("save_recall_images", True))
        def _set_combo(cmb, days):
            idx = self._RETENTION_DAYS.index(days) if days in self._RETENTION_DAYS else 0
            cmb.blockSignals(True)
            cmb.setCurrentIndex(idx)
            cmb.blockSignals(False)
        self.chk_recv.blockSignals(True)
        self.chk_recv.setChecked(bool(msg.get("receive_enabled", True)))
        self.chk_recv.blockSignals(False)
        self.chk_send.blockSignals(True)
        self.chk_send.setChecked(bool(msg.get("send_enabled", True)))
        self.chk_send.blockSignals(False)
        self._update_msg_switch_text()
        # 接收/发送范围已移除（08-20，默认 all），不再读 yaml
        self.chk_recv_text.blockSignals(True)
        self.chk_recv_text.setChecked(bool(msg.get("receive_text", True)))
        self.chk_recv_text.blockSignals(False)
        self.chk_recv_img.blockSignals(True)
        self.chk_recv_img.setChecked(bool(msg.get("receive_image", True)))
        self.chk_recv_img.blockSignals(False)
        self.chk_recv_voice.blockSignals(True)
        self.chk_recv_voice.setChecked(bool(msg.get("receive_voice", True)))
        self.chk_recv_voice.blockSignals(False)
        self.chk_recv_video.blockSignals(True)
        self.chk_recv_video.setChecked(bool(msg.get("receive_video", True)))
        self.chk_recv_video.blockSignals(False)
        self.chk_recv_file.blockSignals(True)
        self.chk_recv_file.setChecked(bool(msg.get("receive_file", True)))
        self.chk_recv_file.blockSignals(False)
        self.chk_recv_fwd.blockSignals(True)
        self.chk_recv_fwd.setChecked(bool(msg.get("receive_forward", True)))
        self.chk_recv_fwd.blockSignals(False)
        self.chk_recall_msg.blockSignals(True)
        self.chk_recall_msg.setChecked(bool(recall_msg))
        self.chk_recall_msg.blockSignals(False)
        self.chk_recall_media.blockSignals(True)
        self.chk_recall_media.setChecked(bool(recall_media))
        self.chk_recall_media.blockSignals(False)
        _set_combo(self.cmb_text_ret, int(ar.get("text_retention_days", 0)))
        _set_combo(self.cmb_media_ret, int(ar.get("media_retention_days", 0)))
        self.ed_archive_dir.setText(str(ar.get("base_dir", "data/archive")))
        self._msg_dirty = False

    def _mark_msg_dirty(self):
        self._msg_dirty = True

    def _restore_default_dir(self):
        self.ed_archive_dir.setText("data/archive")
        self._mark_msg_dirty()

    def _update_msg_switch_text(self):
        """接收/发送按钮文案随状态切换（08-23 用户报告：关闭后文案不变
        仍显示「接收开启/发送开启」，误导用户以为还是开的）。
        开=✅ 接收开启（绿底粗体，:checked 样式）/ 关=⛔ 接收关闭（默认灰边）。"""
        self.chk_recv.setText("✅ 接收开启" if self.chk_recv.isChecked() else "⛔ 接收关闭")
        self.chk_send.setText("✅ 发送开启" if self.chk_send.isChecked() else "⛔ 发送关闭")

    def _on_msg_toggled(self, *_):
        """接收/发送总开关切换：更新按钮文案 + 即时保存（单键，无需等保存按钮）。"""
        self._update_msg_switch_text()
        self._save_msg(immediate=True)

    def _on_recall_toggled(self, *_):
        """撤回存档开关打开时弹免责声明（不用于收集隐私）。"""
        if self.chk_recall_msg.isChecked() or self.chk_recall_media.isChecked():
            if not self.mw.confirm(
                    "存档免责声明",
                    "开启存档功能。\n\n"
                    "本程序存档的消息/媒体仅用于本人娱乐与取证目的，\n"
                    "不用于收集隐私、不用于对外公开传播。\n\n确认开启？"
                    "（选「否」将保持关闭状态）"):
                # 回退到关闭
                if self.chk_recall_msg.isChecked():
                    self.chk_recall_msg.setChecked(False)
                if self.chk_recall_media.isChecked():
                    self.chk_recall_media.setChecked(False)
                return
        self._mark_msg_dirty()

    def _save_msg(self, immediate: bool = False):
        """保存消息管理配置：写 yaml（msg + archive）→ 热重载。"""
        y = self.mw.yaml_cfg
        msg = y.setdefault("msg", {})
        ar = y.setdefault("archive", {})
        msg["receive_enabled"] = bool(self.chk_recv.isChecked())
        msg["send_enabled"] = bool(self.chk_send.isChecked())
        # 接收/发送范围固定 all（08-20 移除 GUI 选项，清理旧 yaml 里的残留值）
        msg["receive_scope"] = "all"
        msg["send_scope"] = "all"
        msg["receive_text"] = bool(self.chk_recv_text.isChecked())
        msg["receive_image"] = bool(self.chk_recv_img.isChecked())
        msg["receive_voice"] = bool(self.chk_recv_voice.isChecked())
        msg["receive_video"] = bool(self.chk_recv_video.isChecked())
        msg["receive_file"] = bool(self.chk_recv_file.isChecked())
        msg["receive_forward"] = bool(self.chk_recv_fwd.isChecked())
        msg["save_recall_messages"] = bool(self.chk_recall_msg.isChecked())
        msg["save_recall_images"] = bool(self.chk_recall_media.isChecked())
        ar["text_retention_days"] = self._RETENTION_DAYS[self.cmb_text_ret.currentIndex()]
        ar["media_retention_days"] = self._RETENTION_DAYS[self.cmb_media_ret.currentIndex()]
        ar["base_dir"] = self.ed_archive_dir.text().strip() or "data/archive"

        # 同步 archive 旧键（保持兼容：配置页/旧代码读 archive.save_recall_* 时值一致）
        ar["save_recall_messages"] = msg["save_recall_messages"]
        ar["save_recall_images"] = msg["save_recall_images"]

        if self._msg_saving:
            return
        self._msg_saving = True
        if not immediate:
            self.btn_msg_save.setEnabled(False)

        def _do():
            api_client.save_yaml(y)
            report = api_client.reload_config(self.mw.cfg)
            # 同步 mw.cfg
            from core.config import flatten_yaml_tree
            new_cfg = flatten_yaml_tree(y)
            self.mw.cfg.clear()
            self.mw.cfg.update(new_cfg)
            self.mw.yaml_cfg = y
            return report

        def _ok(report):
            self._msg_saving = False
            self._msg_dirty = False
            if not immediate:
                self.btn_msg_save.setEnabled(True)
            flash_button(self.btn_msg_save)  # 点击反馈：按钮文字短暂变"✅ 已保存"
            self.mw.statusBar().showMessage(
                f"{'消息设置' if not immediate else '开关'}已保存（热生效）", 4000)

        def _err(e):
            self._msg_saving = False
            if not immediate:
                self.btn_msg_save.setEnabled(True)
            self.mw.statusBar().showMessage(f"消息设置保存失败: {e}", 4000)

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    # ------------------------------------------------------------
    #  获取模型（/models 拉取）
    # ------------------------------------------------------------
    # 部分网关 /models 会带模型输出上限字段（OpenAI 系 max_output_tokens、
    # OpenRouter 等 context_length/max_tokens）；标准 OpenAI 协议不保证有。
    _MODEL_MAX_FIELDS = ("max_output_tokens", "max_tokens", "max_completion_tokens",
                         "context_length", "max_model_len", "max_input_tokens")

    def _fetch_models(self, api_ed: QLineEdit, model_ed: QLineEdit):
        """从 LLM 服务的 /models 接口拉取模型列表（本地 Ollama/vLLM 通用）。
        远程 OpenAI 官方 API 的 /v1/models 需要 key：带当前表单里的密钥尝试。
        若服务端在模型条目里带了输出上限字段，选中后自动填入 max_tokens。
        """
        base = api_ed.text().strip().rstrip("/")
        if not base:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "缺少地址", "先填 API 地址，再点「获取模型」")
            return
        key = ""
        if api_ed is self.ed_r_api:
            key = self.ed_r_key.text().strip()
        elif api_ed is self.ed_l_api:
            key = self.ed_l_key.text().strip()

        def _do():
            import json, urllib.request
            req = urllib.request.Request(f"{base}/models")
            # 部分网关 WAF 会拦截 Python-urllib 默认 UA（部分 API 网关返回 403），
            # 带浏览器 UA 规避
            req.add_header("User-Agent",
                           "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
            if key:
                req.add_header("Authorization", f"Bearer {key}")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            raw = data.get("data") or data.get("models") or []
            models, maxmap = [], {}
            for m in raw:
                mid = m.get("id") or m.get("name") or ""
                if not mid:
                    continue
                models.append(mid)
                # 提取服务端提供的输出上限（若字段存在且为正整数）
                for f in self._MODEL_MAX_FIELDS:
                    v = m.get(f)
                    if isinstance(v, int) and v > 0:
                        maxmap[mid] = v
                        break
            return models, maxmap

        w = Worker(_do)

        def _ok(res):
            models, maxmap = res
            if not models:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(self, "获取模型", "该服务 /models 接口未返回模型列表")
                return
            if len(models) == 1:
                chosen = models[0]
            else:
                from PySide6.QtWidgets import QInputDialog
                chosen, okay = QInputDialog.getItem(
                    self, "选择模型", f"该服务有 {len(models)} 个可用模型：", models, 0, False)
                if not (okay and chosen):
                    return
            model_ed.setText(chosen)
            self._mark_llm_dirty()
            # 远程页：服务端带输出上限字段则自动填入 max_tokens（夹回范围）
            if api_ed is self.ed_r_api:
                cap = maxmap.get(chosen)
                if cap:
                    v = max(self.sp_r_tokens.minimum(), min(cap, self.sp_r_tokens.maximum()))
                    self.sp_r_tokens.setValue(v)
                    self.mw.statusBar().showMessage(
                        f"已拉取 {len(models)} 个模型 · max_tokens 已从服务端自动填入 {cap}"
                        f"（记得点保存）", 6000)
                else:
                    self.mw.statusBar().showMessage(
                        f"已拉取 {len(models)} 个模型 · 服务端未提供 max_tokens 字段，"
                        f"请手动填写（记得点保存）", 6000)
            else:
                self.mw.statusBar().showMessage(
                    f"已拉取 {len(models)} 个模型（记得点保存）", 4000)

        def _err(msg):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "获取模型失败", f"无法访问 {base}/models\n{msg[:160]}")

        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    # ------------------------------------------------------------
    #  用量统计（15s 自动刷新 + 测试后刷新）
    # ------------------------------------------------------------
    def _refresh_usage(self):
        def _do():
            return api_client.get_usage(self.mw.cfg)

        def _fmt(n):
            n = int(n or 0)
            if n >= 1000000:
                return f"{n / 1000000:.1f}M"
            if n >= 1000:
                return f"{n / 1000:.1f}K"
            return str(n)

        def _ok(d):
            t = d.get("total", {})
            if not t or t.get("calls", 0) == 0:
                self.lbl_llm_usage.setText("📈 LLM 用量：暂无记录（调用后自动累计）")
                return
            day = d.get("by_day") or {}
            today = day.get(time.strftime("%Y-%m-%d"), {})
            # 08-20：压缩为一行，不显示各模型明细（明细在 data/llm_usage.json）
            # 08-21：去掉合计（输入/输出已能自算）；今日补输入/输出用量
            self.lbl_llm_usage.setText(
                f"📈 LLM 用量：累计 {t['calls']} 次 · "
                f"输入 {_fmt(t.get('prompt_tokens', 0))} / 输出 {_fmt(t.get('completion_tokens', 0))} tokens · "
                f"今日 {today.get('calls', 0)} 次 · "
                f"输入 {_fmt(today.get('prompt_tokens', 0))} / 输出 {_fmt(today.get('completion_tokens', 0))}")

        def _err(msg):
            self.lbl_llm_usage.setText(f"📈 LLM 用量：（bot 未运行，无法读取）")

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    # ============================================================
    #  NapCat 板块（二维码 / 账号信息 双态）
    # ============================================================
    def _build_napcat_panel(self):
        lay = self.napcat_box._vlayout

        # --- 状态行（账号 or 提示）---
        self.lbl_nc_state = QLabel("—")
        self.lbl_nc_state.setWordWrap(True)
        self.lbl_nc_state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_nc_state)

        # 注销按钮（08-23 放在卡片最上层、二维码/登录两区之外：
        # 两个状态都可见——待扫码态（login_area 隐藏）和已登录态
        # （qr_area 隐藏）都能直接点，登错号/残留登录态时不用绕路）
        self.btn_nc_logout = QPushButton("🚪 注销登录（清空凭证）")
        self.btn_nc_logout.setMinimumHeight(28)
        self.btn_nc_logout.setStyleSheet(
            "background: #fff3f3; border: 1px solid #e74c3c; color: #c0392b; "
            "border-radius: 4px; font-size: 12px;")
        self.btn_nc_logout.clicked.connect(self._logout_napcat)
        lay.addWidget(self.btn_nc_logout)

        # --- 未登录：二维码（居中）+ 按钮行 + 说明 ---
        self.qr_area = QWidget()
        qv = QVBoxLayout(self.qr_area)
        qv.setSpacing(6)  # 08-20：8→6，缩减第一行板块高度
        self.lbl_qr = QLabel("（未获取到二维码）")
        self.lbl_qr.setAlignment(Qt.AlignCenter)
        self.lbl_qr.setMinimumSize(130, 130)  # 08-20：170→130，压缩第一行板块高度
        self.lbl_qr.setMaximumSize(150, 150)
        self.lbl_qr.setStyleSheet(
            "border: 1px solid #ccc; border-radius: 4px; background: white; "
            "font-size: 13px; font-weight: normal;")
        # 08-20：二维码垂直居中（上下对称 stretch；此前贴按钮行偏下）
        qv.addStretch(1)
        qrow_qr = QHBoxLayout()
        qrow_qr.addStretch(1)
        qrow_qr.addWidget(self.lbl_qr)
        qrow_qr.addStretch(1)
        qv.addLayout(qrow_qr)
        qv.addStretch(1)

        # 按钮行（二维码下方并排，不再右侧悬空留白）
        self.btn_qr_refresh = QPushButton("🔄 刷新二维码")
        self.btn_qr_refresh.setMinimumHeight(28)  # 08-20：30→28，压缩第一行板块高度
        self.btn_qr_refresh.clicked.connect(self._refresh_qr)
        self.btn_napcat_console = QPushButton("🖥 NapCat 控制台")
        self.btn_napcat_console.setMinimumHeight(28)  # 08-20：30→28，压缩第一行板块高度
        self.btn_napcat_console.clicked.connect(self._open_napcat_console)
        qrow_btns = QHBoxLayout()
        qrow_btns.setSpacing(8)
        qrow_btns.addWidget(self.btn_qr_refresh, 1)
        qrow_btns.addWidget(self.btn_napcat_console, 1)
        qv.addLayout(qrow_btns)

        # 说明文字（卡片整宽，避免窄列折行孤行）
        self.lbl_qr_hint = QLabel("")
        self.lbl_qr_hint.setWordWrap(False)  # 08-20：强制单行（文案已精简），省一行高度
        self.lbl_qr_hint.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        qv.addWidget(self.lbl_qr_hint)
        # 08-20：去掉 qv 底部 stretch——内容贴顶，多余高度不再沉底撑空隙
        lay.addWidget(self.qr_area)

        # --- 已登录：账号信息区（头像+昵称 / QQ号 / 连接信息 / 版本）---
        self.login_area = QWidget()
        lv = QVBoxLayout(self.login_area)
        lv.setSpacing(5)
        # 头像行（08-22 C 方案：头像 56px 圆 + 昵称；下载失败降级首字色块）
        self.lbl_nc_avatar = QLabel()
        self.lbl_nc_avatar.setFixedSize(56, 56)
        self.lbl_nc_avatar.setAlignment(Qt.AlignCenter)
        self.lbl_nc_avatar.setToolTip("点击复制 QQ 号")
        # 昵称/QQ 号（头像右侧纵向两行）
        self.lbl_nc_account = QLabel("—")
        # 昵称用深色（状态行已占绿色语义，昵称做主体需更强对比）
        self.lbl_nc_account.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #2c3e50;")
        self.lbl_nc_account.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_nc_qq = QLabel("")
        self.lbl_nc_qq.setStyleSheet("font-size: 13px; color: #555;")
        self.lbl_nc_qq.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v_acct = QVBoxLayout()
        v_acct.setSpacing(1)
        v_acct.addWidget(self.lbl_nc_account)
        v_acct.addWidget(self.lbl_nc_qq)
        h_acct = QHBoxLayout()
        h_acct.setSpacing(10)
        h_acct.addWidget(self.lbl_nc_avatar, 0, Qt.AlignVCenter)
        h_acct.addLayout(v_acct, 1)
        lv.addLayout(h_acct)
        self.lbl_nc_detail = QLabel("")
        self.lbl_nc_detail.setWordWrap(True)
        self.lbl_nc_detail.setStyleSheet("color: #7f8c8d; font-size: 12px;")
        self.lbl_nc_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_nc_version = QLabel("")
        self.lbl_nc_version.setWordWrap(True)
        self.lbl_nc_version.setStyleSheet("color: #95a5a6; font-size: 11px;")
        self.lbl_nc_version.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lv.addWidget(self.lbl_nc_detail)
        lv.addWidget(self.lbl_nc_version)
        # 头像下载状态（08-22）：当前 uin / 进行中的 Worker
        self._avatar_uin = ""
        self._avatar_worker = None
        # 08-20：去掉 lv 底部 stretch（内容贴顶）
        lay.addWidget(self.login_area)

        self._show_qr_area(True)

    def _show_qr_area(self, show: bool):
        self.qr_area.setVisible(show)
        self.login_area.setVisible(not show)

    # ============================================================
    #  状态轮询入口（main_window 每 2s 调）
    # ============================================================
    def update_status(self, status: dict):
        self._last_status = status

        # --- bot.qq 兜底值防覆盖（2026-08-23）---
        # bot 确认真实登录号后已把 config.yaml 的 bot.qq 自动回写为实际号
        # （core.bot._confirm_account → sync_bot_qq_to_yaml）。GUI 的保存流程
        # 是「启动快照 mw.yaml_cfg 整树 dump 写盘」——不跟着刷新的话，用户
        # 下次保存任意设置会把内存快照里的旧号又写回去（bot 刚回写的新号
        # 被覆盖 → 下次启动兜底又是错号）。这里每轮把快照同步成 /status
        # 报的真实号（仅内存，不写盘——写盘由 bot 侧负责，值相同不动文件）。
        _st_qq = str(status.get("bot_qq") or "").strip()
        if _st_qq and _st_qq.isdigit():
            _y = getattr(self.mw, "yaml_cfg", None)
            if isinstance(_y, dict):
                _ybot = _y.get("bot")
                if not isinstance(_ybot, dict):
                    _ybot = {}
                    _y["bot"] = _ybot
                if str(_ybot.get("qq") or "") != _st_qq:
                    _ybot["qq"] = _st_qq

        # --- 任务列表（08-22：bot /status 的 tasks 字段 → 总览页面板）---
        self.update_task_panel(status.get("tasks") or {"running": [], "queued": []})

        # --- LLM 板块（总开关 + 状态联动）---
        llm = status.get("llm", {})
        enabled = bool(llm.get("enabled", True))
        cur = self.chk_llm_enabled.isChecked()
        if cur != enabled:
            self.chk_llm_enabled.blockSignals(True)
            self.chk_llm_enabled.setChecked(enabled)
            self.chk_llm_enabled.blockSignals(False)
        self.chk_llm_enabled.setText("✅ 开启" if enabled else "⛔ 关闭")
        # bot 内存当前后端（/status 返回的是 bot 运行态，不是磁盘值）
        st_backend = str(llm.get("backend", "")).lower()
        if not self._llm_dirty:
            # 08-20 策略（用户指定）：LLM 后端**以 bot 内存为准**。
            # 下拉框只跟随 bot 实际运行态显示，不读磁盘 config.yaml，
            # 也不做任何"磁盘→内存"的自动同步（不自动拉回磁盘值）。
            # 手动切换后点"保存配置"才写盘 + 热重载；手动改 config.yaml
            # 需要点"重启 bot"才生效（bot 启动时读盘一次）。
            if st_backend:
                want = 0 if st_backend in ("remote", "deepseek") else 1
                if self.cmb_llm_backend.currentIndex() != want:
                    self.cmb_llm_backend.blockSignals(True)
                    self.cmb_llm_backend.setCurrentIndex(want)
                    self.cmb_llm_backend.blockSignals(False)
                    self.llm_stack.setCurrentIndex(want)
            self._refresh_llm_state_label()

        # --- 存档已并入「消息管理」板块（控件实时反映，不再单独轮询）---

        # --- NapCat 状态联动（未连接时自动拉二维码）---
        nap = status.get("napcat", {})
        # 2026-08-23 注销进行中：禁用「刷新二维码」+「注销」按钮
        # （logout 窗口内重启容器 = 扫码后登录态被收尾抹掉，14:24 竞态）。
        # /status 每 2s 轮询 → 进程重启/漏掉 Worker 回调时也能恢复禁用态。
        logout_busy = bool(nap.get("logout_in_progress"))
        if logout_busy:
            self.btn_qr_refresh.setEnabled(False)
            self.btn_nc_logout.setEnabled(False)
            self.lbl_qr_hint.setText("注销进行中（清空凭证约 1 分钟），请勿操作…")
        elif self._logout_busy:
            # 本地请求在途（Worker 未回）：保持禁用直到回调恢复
            self.btn_qr_refresh.setEnabled(False)
            self.btn_nc_logout.setEnabled(False)
        if not nap.get("connected"):
            if time.time() - self._last_napcat_fetch > 30:
                self._fetch_napcat()
            # 2026-08-23：待扫码状态（QQ 登录态失效挂二维码）——状态行橙色
            # 提示"扫码"而非误导性的"断连"（watchdog 已停止无效重启）
            if nap.get("scan_pending"):
                self.lbl_nc_state.setStyleSheet(_WARN_COLOR)
                self.lbl_nc_state.setText("⚠️ 登录态失效 · 待扫码（点右侧刷新二维码 → 手Q 扫）")
        else:
            # 已连接：状态行 + 账号信息区（昵称/QQ号/连接信息/版本）
            self.lbl_nc_state.setStyleSheet(_OK_COLOR)
            self.lbl_nc_state.setText("✅ 已登录并连接")
            self._refresh_napcat_login_view(nap)
            self._show_qr_area(False)
        # 注销完成（/status 字段回落 False）：恢复按钮可用性
        # （Worker 回调也会恢复，此处兜底进程重启/回调丢失的场景）
        if not logout_busy and not self._logout_busy \
                and not self._qr_refresh_busy:
            self.btn_qr_refresh.setEnabled(True)
            self.btn_nc_logout.setEnabled(True)

    def _refresh_napcat_login_view(self, nap: dict):
        """已登录视图填充（昵称/QQ号/连接信息/版本行）。

        account 字段格式 "昵称 (QQ号)"（bot.py get_login_info 写入）；
        解析失败时昵称行直接显示原值。版本号来自 WebUI（/status 合入，
        1 小时缓存），WebUI 未起时版本行自动省略。
        """
        import re
        account = nap.get("account", "") or ""
        m = re.match(r"^(.*?)\s*\((\d+)\)\s*$", account)
        if m:
            self.lbl_nc_account.setText(m.group(1))
            self.lbl_nc_qq.setText(f"QQ {m.group(2)}")
            self.lbl_nc_qq.setVisible(True)
            self._load_avatar(m.group(2), m.group(1))
        elif account:
            self.lbl_nc_account.setText(account)
            self.lbl_nc_qq.setVisible(False)
            self._avatar_uin = ""
            self._avatar_fallback("Q")
        else:
            self.lbl_nc_account.setText("（账号信息获取中…）")
            self.lbl_nc_qq.setVisible(False)
            self._avatar_uin = ""
            self._avatar_fallback("Q")
        # 连接信息行：在线时长 + 时间 + 来源地址（08-22 连接状态行增强）
        # detail 里 remote 两种格式：
        #   旧（bot 未重启前写入）: remote=('192.168.0.3', 58922)
        #   新（bot.py 修复后）:     remote=192.168.0.3:58922
        bits = []
        t_s = nap.get("time")
        if t_s:
            try:
                from datetime import datetime
                t0 = datetime.strptime(t_s, "%Y-%m-%d %H:%M:%S")
                secs = max(0, int(time.time() - t0.timestamp()))
                if secs >= 86400:
                    bits.append(f"已在线 {secs // 86400} 天 {(secs % 86400) // 3600} 小时")
                elif secs >= 3600:
                    bits.append(f"已在线 {secs // 3600} 小时 {(secs % 3600) // 60} 分")
                elif secs >= 60:
                    bits.append(f"已在线 {secs // 60} 分")
                else:
                    bits.append("刚刚连接")
                bits.append(f"于 {t_s}")
            except Exception:
                bits.append(f"连接于 {t_s}")
        else:
            bits.append("—")
        d = str(nap.get("detail", ""))
        remote = ""
        rm = re.search(r"remote=\(?'?([\d.]+)'?,\s*(\d+)", d)
        if rm:
            remote = f"{rm.group(1)}:{rm.group(2)}"
        else:
            rm2 = re.search(r"remote=([\d.]+:\d+)", d)
            if rm2:
                remote = rm2.group(1)
        if remote:
            bits.append(f"来源 {remote}")
        self.lbl_nc_detail.setText(" · ".join(bits) or "—")
        # 版本行：NapCat x.y.z · QQ 协议 3.x.x（取不到则隐藏，不留空行）
        vbits = []
        if nap.get("napcat_version"):
            vbits.append(f"NapCat {nap['napcat_version']}")
        if nap.get("qq_version"):
            vbits.append(f"QQ 协议 {nap['qq_version']}")
        self.lbl_nc_version.setText(" · ".join(vbits))
        self.lbl_nc_version.setVisible(bool(vbits))

    # ============================================================
    #  NapCat 头像（2026-08-22 C 方案）
    # ============================================================
    @staticmethod
    def _avatar_cache_path(uin: str) -> str:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        return os.path.join(data_dir, "avatar_cache", f"avatar_{uin}.png")

    @staticmethod
    def _avatar_url(uin: str) -> str:
        """QQ 官方头像 CDN（实测 q1.qlogo.cn/g?b=qq&nk={uin}&s=640 可用）。"""
        return f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"

    def _avatar_fallback(self, letter: str):
        """降级：首字色块头像（下载失败/未下载时）。"""
        import hashlib
        # 按昵称首字哈希选底色（同昵称颜色稳定，不同昵称颜色不同）
        h = int(hashlib.md5(letter.encode("utf-8", "ignore")).hexdigest()[:6], 16)
        hue = h % 360
        pixmap = QPixmap(56, 56)
        pixmap.fill(Qt.transparent)
        from PySide6.QtGui import QPainter, QBrush, QPen
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor.fromHsl(hue, 90, 52)))
        painter.setPen(QPen(Qt.NoPen))
        painter.drawRoundedRect(0, 0, 56, 56, 10, 10)
        f = QFont()
        f.setPixelSize(26)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, letter)
        painter.end()
        self.lbl_nc_avatar.setPixmap(pixmap)

    def _load_avatar(self, uin: str, nickname: str):
        """加载头像：本地缓存 → CDN 下载（QThread 异步）→ 首字色块降级。

        同一 uin 不重复下载；24h 缓存过期重取。下载失败保留首字色块。
        """
        if not uin or uin == self._avatar_uin:
            return
        self._avatar_uin = uin
        letter = (nickname or "Q").strip()[:1] or "Q"
        # 先显示首字色块（占位，下载完成后替换）
        self._avatar_fallback(letter)

        cache_path = self._avatar_cache_path(uin)
        # 本地缓存 24h 内直接加载
        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < 86400:
                pm = QPixmap(cache_path)
                if not pm.isNull():
                    self.lbl_nc_avatar.setPixmap(
                        pm.scaled(56, 56, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                        .copy(
                            (pm.width() - 56) // 2, (pm.height() - 56) // 2, 56, 56))
                    return
            else:
                # 过期 → 后台重取（旧的先留着显示，成功后替换）
                pass

        # 后台下载（QThread，不阻塞 UI）
        if self._avatar_worker is not None and self._avatar_worker.isRunning():
            return  # 已有下载在跑，不叠加
        self._avatar_worker = Worker(self._download_avatar, uin, cache_path,
                                     parent=self)
        self._avatar_worker.finished_ok.connect(self._on_avatar_ok)
        self._avatar_worker.finished_err.connect(self._on_avatar_err)
        self._avatar_worker.start()

    def _download_avatar(self, uin: str, cache_path: str) -> bool:
        """QThread 内下载头像到缓存（同步 urllib）。失败抛异常。"""
        import urllib.request
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        req = urllib.request.Request(
            self._avatar_url(uin),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        if len(data) < 100:
            raise RuntimeError(f"头像数据异常（{len(data)} bytes）")
        with open(cache_path, "wb") as f:
            f.write(data)
        return True

    def _on_avatar_ok(self, result):
        if not result:
            return
        # 期间账号切换过则丢弃（只加载当前 uin 的缓存）
        cache_path = self._avatar_cache_path(self._avatar_uin)
        pm = QPixmap(cache_path)
        if pm.isNull():
            return
        # 裁成方形 56×56（头像原图可能非正方形）
        side = min(pm.width(), pm.height())
        x = (pm.width() - side) // 2
        y = (pm.height() - side) // 2
        self.lbl_nc_avatar.setPixmap(
            pm.copy(x, y, side, side)
            .scaled(56, 56, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def _on_avatar_err(self, msg: str):
        # 下载失败：保留首字色块（已是占位），不弹窗（本地 GUI 静默降级）
        pass

    # ============================================================
    #  NapCat：拉取 / 刷新 / 应用
    # ============================================================
    def _apply_napcat(self, res: dict):
        """应用 /napcat 结果（UI 线程）。"""
        if res is None:
            return
        if "_error" in res or not isinstance(res, dict):
            self.lbl_qr_hint.setText(f"获取失败: {res.get('_error') if isinstance(res, dict) else res}")
            return

        if res.get("logged_in"):
            # 已登录 → 切账号信息态（完整视图由 /status 2s 轮询补全版本行）
            self.lbl_nc_state.setStyleSheet(_OK_COLOR)
            self.lbl_nc_state.setText("✅ 已登录并连接")
            if not res.get("account"):
                self.lbl_nc_account.setText("（账号信息获取中…）")
            self._refresh_napcat_login_view(res)
            self._show_qr_area(False)
            self._login_ok_timer.start(5000)
            return

        # 未登录 → 二维码态
        self.lbl_nc_state.setStyleSheet(_WARN_COLOR)
        self.lbl_nc_state.setText("⚠️ 未登录，请扫码")
        b64 = res.get("qrcode_b64", "")
        if b64:
            try:
                data = base64.b64decode(b64)
                # PySide6 6.x: QImage(bytes) 会把 bytes 当文件名 → 必须 fromData
                img = QImage.fromData(data)
                if not img.isNull():
                    pm = QPixmap.fromImage(img)
                    # 08-20 修复：label 内容区最大 148×148（max 150 − 2px 边框），
                    # 旧代码放大到 190px 超出 label，Qt 直接裁切二维码外圈
                    # （角上定位图案受损）→ 手机扫不出。改缩放到 144px 完整显示，
                    # 留 2px 白边（保持 FastTransformation 像素锐利）
                    pm = pm.scaled(144, 144, Qt.KeepAspectRatio, Qt.FastTransformation)
                    self.lbl_qr.setPixmap(pm)
            except Exception as e:
                self.lbl_qr_hint.setText(f"二维码解析失败: {e}")
        hint = res.get("hint", "")
        mtime = res.get("qrcode_mtime", 0)
        age = int(time.time() - mtime) if mtime else 0
        if mtime:
            hint = f"{hint}（{age} 秒前生成）"  # 08-20：去重复"过期点刷新"，压一行
        self.lbl_qr_hint.setText(hint)
        if not b64:
            self.lbl_qr.setText(hint or "（未获取到二维码，点右侧刷新）")
        self._show_qr_area(True)

    def _fetch_napcat(self, force: bool = False):
        """拉取 NapCat 登录态 + 二维码（GET /napcat）。"""
        now = time.time()
        if not force and now - self._last_napcat_fetch < 10:
            return
        self._last_napcat_fetch = now

        def _do():
            try:
                return api_client.get_napcat(self.mw.cfg)
            except Exception as e:
                return {"_error": str(e)}

        def _err(e):
            self.lbl_qr_hint.setText(f"获取失败: {e}")

        w = Worker(_do)
        w.finished_ok.connect(self._apply_napcat)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _refresh_qr(self):
        """点「刷新二维码」：已登录→仅重拉；未登录→重启 NapCat 出新码。"""
        if self._last_status and self._last_status.get("napcat", {}).get("connected"):
            self._fetch_napcat(force=True)
            return
        self.btn_qr_refresh.setEnabled(False)
        self._qr_refresh_busy = True  # 08-23：在途标志，防 update_status 抢恢复
        self.lbl_qr_hint.setText("正在重启 NapCat 刷新二维码（约 15 秒）…")
        self.mw.statusBar().showMessage("NapCat 刷新中…", 5000)

        def _do():
            try:
                r = api_client.napcat_restart(self.mw.cfg)
                return r if isinstance(r, dict) else {"_error": str(r)}
            except Exception as e:
                return {"_error": str(e)}

        def _ok(r):
            self._qr_refresh_busy = False
            self.btn_qr_refresh.setEnabled(True)
            if r.get("ok"):
                self.lbl_qr_hint.setText("NapCat 已重启，12 秒后自动拉取新二维码…")
                self._qr_timer.start(12000)
            else:
                self.lbl_qr_hint.setText(f"刷新失败: {r.get('error') or r.get('_error') or '未知错误'}")

        def _err(e):
            self._qr_refresh_busy = False
            self.btn_qr_refresh.setEnabled(True)
            self.lbl_qr_hint.setText(f"刷新失败: {e}")

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _on_login_ok_delayed(self):
        """登录确认后收起二维码区（update_status 会维持已登录态）。"""
        if self._last_status and self._last_status.get("napcat", {}).get("connected"):
            self._show_qr_area(False)

    def _logout_napcat(self):
        """点「注销登录」：确认后调 /napcat/logout（全清凭证），出新二维码可扫任意账号。

        2026-08-23 起 logout 为全清流程（清 QQ 数据 volume 登录态 + passkey
        + 快速登录账号 + bot 侧主账号/收敛配置）——原账号也需重新扫码，
        弹窗文案必须讲清楚（避免用户以为只需重扫一次就能回旧号）。
        """
        if QMessageBox.question(
                self, "注销 NapCat 登录（清空全部凭证）",
                "将清空所有 QQ 登录凭证（原账号登录态一并清除）：\n"
                "· 之后每次扫码都是全新登录，可扫任意账号\n"
                "· 原账号也需要重新扫码\n"
                "· 不点注销时不受影响：重启程序照旧免扫自动登录\n\n"
                "清空过程约 1 分钟（备份 + 清数据 + 重启容器），确定吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.btn_nc_logout.setEnabled(False)
        self._logout_busy = True  # 08-23：在途标志，update_status 据此保持禁用
        self.mw.statusBar().showMessage("NapCat 注销中（清空全部凭证，约 1 分钟）…", 0)

        def _do():
            try:
                r = api_client.napcat_logout(self.mw.cfg)
                return r if isinstance(r, dict) else {"_error": str(r)}
            except Exception as e:
                return {"_error": str(e)}

        def _ok(r):
            self._logout_busy = False
            self.btn_nc_logout.setEnabled(True)
            self.mw.statusBar().clearMessage()
            if r.get("ok"):
                self.mw.statusBar().showMessage("已注销并清空全部凭证，等待新二维码…", 8000)
                self._show_qr_area(True)
                self.lbl_qr_hint.setText("已清空全部凭证，15 秒后自动拉取新二维码（可扫任意账号，原账号也需重扫）…")
                self._qr_timer.start(15000)
            else:
                self.lbl_qr_hint.setText(f"注销失败: {r.get('error') or r.get('_error') or '未知错误'}")

        def _err(e):
            self._logout_busy = False
            self.btn_nc_logout.setEnabled(True)
            self.mw.statusBar().clearMessage()
            self.lbl_qr_hint.setText(f"注销失败: {e}")

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    # NapCat WebUI 自动登录注入脚本（2026-08-20 逆向）：
    # POST /api/auth/login {hash: sha256(token + '.napcat')} → data.Credential，
    # 存 localStorage['token'] 后刷新页面 = 免手输 token。
    _WEBUI_LOGIN_JS = r"""
    (async () => {
      try {
        if (localStorage.getItem('token')) return 'already-logged-in';
        const token = '__TOKEN__';
        if (!token) return 'no-token';
        // 内嵌 SHA-256（public domain，避免依赖页面 bundle 的私有模块）
        async function sha256hex(plain) {
          const buf = await crypto.subtle.digest(
            'SHA-256', new TextEncoder().encode(plain));
          return Array.from(new Uint8Array(buf))
            .map(b => b.toString(16).padStart(2, '0')).join('');
        }
        const hash = await sha256hex(token + '.napcat');
        const resp = await fetch('/api/auth/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({hash}),
        });
        const data = await resp.json();
        if (data.code === 0 && data.data && data.data.Credential) {
          localStorage.setItem('token', JSON.stringify(data.data.Credential));
          location.reload();
          return 'logged-in';
        }
        return 'login-failed: ' + (data.message || 'unknown');
      } catch (e) {
        return 'error: ' + e;
      }
    })()
    """

    def _open_napcat_console(self):
        """内嵌窗口打开 NapCat WebUI 控制台（自动填充 token 登录态）。

        URL + webui_token 由 bot 的 /napcat 接口返回。页面加载后自动执行
        登录注入（sha256(token+'.napcat') → /api/auth/login → 存 localStorage），
        用户无需知道/输入 token。WebEngine 不可用时回退系统浏览器。
        """
        def _do():
            url, token = None, ""
            try:
                res = api_client.get_napcat(self.mw.cfg)
                if isinstance(res, dict):
                    url = res.get("console_url")
                    token = res.get("webui_token", "")
            except Exception:
                pass
            if not url:
                port = self.mw.cfg.get("NAPCAT_CONSOLE_PORT", 6099)
                url = f"http://127.0.0.1:{port}/webui"
            # 控制台绑定宿主局域网 IP；GUI 同机访问时换 127.0.0.1 更稳
            if "127.0.0.1" not in url and "localhost" not in url:
                import re as _re
                url = _re.sub(r"(?<=://)[^:/]+", "127.0.0.1", url, count=1)
            return {"url": url, "token": token}

        def _ok(r):
            url, token = r["url"], r.get("token", "")
            # 08-24 新增：打开控制台时自动复制 token 到剪贴板（内嵌/外部浏览器共用）
            if token:
                try:
                    QApplication.clipboard().setText(token)
                    self.mw.statusBar().showMessage(
                        f"NapCat token 已复制到剪贴板（{len(token)} 字符）", 5000)
                except Exception as _e:
                    self.mw.statusBar().showMessage(
                        f"token 复制失败: {_e}", 4000)
            try:
                from PySide6.QtWidgets import QDialog
                from PySide6.QtWebEngineWidgets import QWebEngineView
                from PySide6.QtCore import QUrl as _QUrl
            except ImportError:
                return self._open_console_external(url)
            dlg = QDialog(self.mw)
            dlg.setWindowTitle("NapCat 控制台")
            dlg.resize(1100, 760)
            view = QWebEngineView(dlg)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(view)
            # 注入登录（token 已验证存在时才注入；无 token 则用户手动登录）
            if token:
                js = self._WEBUI_LOGIN_JS.replace("__TOKEN__", token)

                def _loaded(ok):
                    if ok:
                        view.page().runJavaScript(js, 0, lambda res:
                            self.mw.statusBar().showMessage(
                                f"控制台登录注入: {res}", 5000))
                view.loadFinished.connect(_loaded)
            view.setUrl(_QUrl(url))
            dlg.show()
            self.mw.statusBar().showMessage(f"已打开 NapCat 控制台: {url}", 5000)
            # 保留引用防 GC
            self._console_dlg = dlg

        def _err(e):
            self.mw.statusBar().showMessage(f"打开控制台失败: {e}", 4000)

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _open_console_external(self, url: str):
        """回退：系统浏览器打开（WebEngine 不可用）。失败则 URL 入剪贴板。"""
        try:
            if sys.platform == "win32":
                os.startfile(url)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{url}"')
            else:
                os.system(f'xdg-open "{url}" >/dev/null 2>&1 &')
            self.mw.statusBar().showMessage(f"已打开 NapCat 控制台: {url}", 5000)
            return
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(url)
        except Exception:
            pass
        QMessageBox.information(
            self.mw, "NapCat 控制台",
            f"无法自动打开浏览器（可能没有图形浏览器）。\n\n"
            f"控制台地址已复制到剪贴板，在浏览器打开即可：\n\n{url}")

    # ============================================================
    #  操作（启停/重启/打开目录）
    # ============================================================
    def _start(self):
        def _do():
            return self.mw.pm.start_bot()

        def _ok(err):
            from PySide6.QtWidgets import QMessageBox as MB
            if err:
                MB.critical(self.mw, "启动失败", err)
            else:
                self.mw.statusBar().showMessage("bot 启动中…")

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"启动失败: {e}"))
        w.start()
        self.mw._track(w)

    def _stop(self):
        if not self.mw.confirm("停止 bot", "确认停止 bot？（数据库已自动持久化，无数据丢失风险）"):
            return

        def _do():
            return self.mw.pm.stop_bot()

        w = Worker(_do)
        w.finished_ok.connect(lambda err: self.mw.statusBar().showMessage(err or "bot 已停止"))
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"停止失败: {e}"))
        w.start()
        self.mw._track(w)

    def _restart(self):
        if not self.mw.confirm("重启 bot", "确认重启 bot？（优雅退出后重新拉起，约 5-10 秒）"):
            return

        def _do():
            err = self.mw.pm.stop_bot(graceful_timeout=10)
            if err:
                return err
            import time
            time.sleep(1)
            return self.mw.pm.start_bot()

        w = Worker(_do)
        w.finished_ok.connect(lambda err: self.mw.statusBar().showMessage(err or "重启完成"))
        w.start()
        self.mw._track(w)

    def _open_logdir(self):
        import subprocess
        data_dir = os.path.dirname(self.mw.cfg.get("DB_PATH", "data/chat_history.db"))
        if not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(data_dir)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", data_dir])
            else:
                subprocess.Popen(["xdg-open", data_dir])
        except Exception as e:
            self.mw.statusBar().showMessage(f"无法打开目录: {e}")
