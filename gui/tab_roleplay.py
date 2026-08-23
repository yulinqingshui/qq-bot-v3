"""
tab_roleplay.py — 角色扮演页（2026-08-22 新增）
================================================
群体角色扮演（群 RP）的管理页：房间数据查看 + 规则/LLM/提示词设置。
数据源 group_roleplay.db（7 表；bot 实时读写，GUI 只读，唯一写操作=
清理已结束房间，走控制 API POST /roleplay/cleanup 在 bot 进程内执行
级联删除——库未开 WAL，GUI 直写会与 bot 实时写抢锁）。

布局（仿游戏管理页，stretch 填满视口，无外层滚动条）：
  左列（固定 260px）：
    🏠 房间列表（双行式行控件：群号+状态 / 轮数·玩家·NPC·剧情）
    ℹ️ 指令说明（复用 help_menu.CATEGORIES["角色扮演"] 单一事实源）
  右列（上下双卡）：
    🏠 房间总览：状态/轮次/创建时间 + 世界观全文（只读 QPlainTextEdit）
               + 紧凑 Tab ×3（🎭 玩家 / 👥 NPC / 📦 物品）
    📜 剧情记录：rp_story 表（轮次/说话人/内容预览，双击全文）
               + 清理已结束房间（单房/全部，confirm 二次确认）
               （原"当前场景/最新摘要"只读框 08-22 已删，高度让给剧情表格）

设置按钮（页面级顶栏右上角，与页面标题同行——左列 260px 卡片标题行
实测放不下 4 按钮）：
  ⚙️ RP规则（4 项）/ 🤖 LLM参数（5 项）/ 📝 提示词（6 项）
  → roleplay_settings_dialogs.py，落 config.yaml roleplay 段，
    POST /config 热重载生效（RP_CFG 整段 + RP_PROMPT_<KEY> 白名单）。
"""

import json
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QFrame, QPlainTextEdit,
    QPushButton, QTabWidget, QSizePolicy, QAbstractItemView,
    QDialog, QDialogButtonBox, QTextBrowser, QScrollArea,
)

import api_client
from worker import Worker


# ------------------------------------------------------------
#  常量
# ------------------------------------------------------------
STATE_LABEL = {"waiting": "待报名", "playing": "进行中", "ended": "已结束"}
STATE_COLOR = {"waiting": "#9a6700", "playing": "#1a7f37", "ended": "#6e7781"}

# 世界观全文展示（GUI 只读，含隐藏伏笔——管理页给 bot 主人看的，
# 与群里 format_world_for_display 的"对玩家隐藏伏笔"语义不同）
_WORLD_SECTIONS = [
    ("background", "📖 背景故事"),
    ("location", "📍 主要场景"),
    ("time", "🕐 时间设定"),
]


def _ts(v) -> str:
    """REAL 时间戳 → 本地时间（str 透传）。"""
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(float(v)))
        except Exception:
            return str(v)
    s = str(v)
    return s[5:16] if len(s) >= 16 and s[4] == "-" else s


def _fmt_list(v) -> str:
    """list/str → 顿号拼接展示；空值显示 —（空单元格易被误读）。

    DB 文本列（personality / reveal_order / tension_sources / atmosphere 等）
    存的是 JSON 字符串（如 "[]" / '["温柔","体贴"]'）——先按 JSON 解析再拼接，
    否则空性格会显示成字面 "[]"。world_state 里的同名字段已是 list，直接走 list 分支。
    """
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                v = json.loads(s)
            except Exception:
                pass  # 非 JSON 的普通字符串，原样返回
    if isinstance(v, list):
        s = "、".join(str(x) for x in v if str(x) not in ("", "None"))
        return s or "—"
    if v in (None, "", "None"):
        return "—"
    return str(v)


def world_to_text(world) -> str:
    """world_state dict → 可读全文（含伏笔/物品/钩子，管理视图）。"""
    if not isinstance(world, dict) or not world:
        return "（无世界观数据）"
    lines = []
    for key, title in _WORLD_SECTIONS:
        v = world.get(key)
        if v:
            lines.append(f"{title}：\n{v}")
    def add(title, items, fmt):
        if items:
            lines.append(f"\n{title}：")
            for it in items:
                lines.append("  • " + fmt(it))
    add("📜 世界规则", world.get("world_rules"),
        lambda r: r if isinstance(r, str) else str(r))
    add("⚔️ 势力", world.get("factions"),
        lambda f: (f"{f.get('name', '?')}：{f.get('description', '')}"
                   f"（态度：{f.get('attitude', '中立')}，领袖：{f.get('leader', '?')}，"
                   f"实力：{f.get('strength', '?')}）") if isinstance(f, dict) else str(f))
    add("👥 NPC（含秘密）", world.get("initial_npcs"),
        lambda n: (f"{n.get('name', '?')}（{n.get('role', '')}）"
                   f" 性格：{_fmt_list(n.get('personality'))}；"
                   f"动机：{n.get('motivation', '')}；位置：{n.get('position', '')}；"
                   f"说话风格：{n.get('voice_style', '')}；"
                   f"秘密：{n.get('secret', '')}") if isinstance(n, dict) else str(n))
    add("📦 物品", world.get("initial_items"),
        lambda i: (f"{i.get('name', '?')}：{i.get('description', '')}"
                   f"（位置：{i.get('location', '')}，持有：{i.get('owner', '')}，"
                   f"意义：{i.get('significance', '')}）") if isinstance(i, dict) else str(i))
    add("🔥 初始冲突", world.get("initial_conflicts"),
        lambda c: (f"{c.get('description', '')}（{c.get('parties', '')}，"
                   f"紧急度：{c.get('urgency', '')}，卷入方式：{c.get('player_hook', '')}）"
                   if isinstance(c, dict) else str(c)))
    add("🎣 开局钩子", world.get("opening_hooks"),
        lambda h: (f"{h.get('hook', '')}（紧急度：{h.get('urgency', '')}，"
                   f"忽略后果：{h.get('consequence', '')}）"
                   if isinstance(h, dict) else str(h)))
    add("🕳️ 待揭示伏笔", world.get("hidden_plots"),
        lambda p: (f"{p.get('plot', '')}（触发：{p.get('trigger', '')}，"
                   f"影响：{p.get('impact', '')}）"
                   if isinstance(p, dict) else str(p)))
    if isinstance(world.get("narrator_guidance"), dict):
        ng = world["narrator_guidance"]
        lines.append("\n🎬 旁白指引：")
        lines.append(f"  • 开场基调：{ng.get('opening_tone', '')}")
        lines.append(f"  • 节奏：{ng.get('pacing', '')}")
        lines.append(f"  • 揭示顺序：{_fmt_list(ng.get('reveal_order'))}")
        lines.append(f"  • 持续张力：{_fmt_list(ng.get('tension_sources'))}")
    if world.get("atmosphere"):
        lines.append(f"\n🌫️ 氛围关键词：{_fmt_list(world.get('atmosphere'))}")
    return "\n".join(lines)


class _Card(QFrame):
    """白卡片（与 tab_games._Card 同款：自设 QSS + sizeHint 覆盖防外层滚动条）。"""

    def __init__(self, title: str, parent=None, actions=None):
        super().__init__(parent)
        self.setObjectName("roleplay_card")
        self.setStyleSheet(
            "#roleplay_card { background: #ffffff; border: 1px solid #d0d7de;"
            " border-radius: 8px; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)
        t = QLabel(title)
        f = QFont()
        f.setPointSize(int(f.pointSize() * 1.25))
        f.setBold(True)
        t.setFont(f)
        if actions:
            trow = QHBoxLayout()
            trow.setSpacing(6)
            trow.addWidget(t)
            trow.addStretch(1)
            for b in actions:
                b.setMinimumHeight(26)
                trow.addWidget(b)
            v.addLayout(trow)
        else:
            v.addWidget(t)
        self.body = v
        # sizeHint 恒等于 minimumSizeHint：外层滚动区只看最小值，
        # 卡片实际高度由父布局 stretch 拉伸到视口（tab_games 同款根治方案）
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def sizeHint(self):
        return self.minimumSizeHint()

    def add(self, w, stretch=0):
        self.body.addWidget(w, stretch)


def _compact_tabs(tabs: QTabWidget):
    """紧凑页签样式（tab_games 同款：widget 级作用域，不碰 theme.py）。"""
    tabs.setObjectName("games_compact_tabs")
    tabs.setStyleSheet(
        "QTabBar::tab { padding: 4px 10px 5px 10px; margin-right: 3px; "
        "font-size: 12px; font-weight: normal; }")


class _StretchyWidget(QWidget):
    """sizeHint 恒返回 minimumSizeHint 的容器（_Card 同款配方，容器级）。

    用途：页面左右列容器。QWidget 默认 sizeHint 按子控件累加——左列 7 个
    房间行 ~380px 会经布局链推高 QScrollArea 的 widget 高度（实测 804 >
    800 视口 → 外层滚动条 4px）；钉成 minimumSizeHint 后实际高度由布局
    stretch 拉伸到视口。⚠️ 必须类方法覆盖（C++ 虚方法分派不走实例属性）。
    """

    def sizeHint(self):
        return self.minimumSizeHint()


class _TableNoEdit(QTableWidget):
    def __init__(self, rows=0, cols=1, *a, **kw):
        super().__init__(rows, cols, *a, **kw)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(26)
        # (Ignored, Expanding)：表格 sizeHint 按行数算会撑爆页面，
        # 忽略 sizeHint、吃卡片分到的空间（内部滚动）
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)


class _RoomListWidget(QWidget):
    """左列房间列表（双行式行控件，仿 tab_games._GameListWidget）。

    每房一个 _RoomRow：13px 粗体群号+状态 + 11px 灰统计，点选/悬停高亮。
    ⚠️ 不用 QListWidgetItem + HTML——item 不渲染富文本（08-21 实测踩坑）。
    """

    class _RoomRow(QWidget):
        def __init__(self, name: str, on_click, parent=None):
            super().__init__(parent)
            self._on_click = on_click
            v = QVBoxLayout(self)
            v.setContentsMargins(8, 6, 8, 6)
            v.setSpacing(2)
            self.name_lbl = QLabel(name)
            fn = QFont()
            fn.setPointSize(max(int(fn.pointSize() * 1.12), 11))
            fn.setBold(True)
            self.name_lbl.setFont(fn)
            self.name_lbl.setWordWrap(True)
            self.stat_lbl = QLabel()
            self.stat_lbl.setStyleSheet("color: #6a737d; font-size: 11px;")
            v.addWidget(self.name_lbl)
            v.addWidget(self.stat_lbl)
            self._selected = False
            self._restyle()

        def set_state_color(self, state: str):
            self.name_lbl.setStyleSheet(
                f"color: {STATE_COLOR.get(state, '#24292f')};")

        def set_selected(self, on: bool):
            self._selected = on
            self._restyle()

        def _restyle(self):
            if self._selected:
                self.setStyleSheet(
                    "background: rgba(9, 105, 218, 0.10);"
                    " border: 1px solid rgba(9, 105, 218, 0.35); border-radius: 7px;")
            else:
                self.setStyleSheet(
                    "background: rgba(9, 105, 218, 0.04);"
                    " border: 1px solid transparent; border-radius: 7px;")

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton and self._on_click:
                self._on_click()

        def enterEvent(self, e):
            if not self._selected:
                self.setStyleSheet(
                    "background: rgba(9, 105, 218, 0.07);"
                    " border: 1px solid transparent; border-radius: 7px;")

        def leaveEvent(self, e):
            self._restyle()

    clicked = Signal(int)  # 类级信号（PySide6 要求）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []
        self._selected_idx = -1
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.setSpacing(6)
        self._layout.addStretch(0)

    def _clear_rows(self):
        for r in self._rows:
            self._layout.removeWidget(r)
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        self._selected_idx = -1

    def add_room(self, name: str, state: str, stat: str):
        idx = len(self._rows)
        r = self._RoomRow(name, lambda i=idx: self.clicked.emit(i), self)
        r.set_state_color(state)
        r.stat_lbl.setText(stat)
        self._layout.insertWidget(self._layout.count() - 1, r)
        self._rows.append(r)

    def select(self, i: int):
        self._selected_idx = i
        for j, r in enumerate(self._rows):
            r.set_selected(j == i)

    def selected(self):
        return self._selected_idx

    def count(self):
        return len(self._rows)


class TabRoleplay(QWidget):
    """角色扮演页：左列房间列表+说明 / 右列总览+剧情双卡。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._rooms: list[dict] = []      # 当前房间快照（list_rooms 结果）
        self._room_sel = -1               # 选中房间下标
        self._room_id = None              # 选中 room_id（Worker 回调守卫用）
        self._build()
        self._refresh_rooms()
        # 显式钉最小尺寸（08-22 实测）：QWidget.setLayout 自动把 layout 的
        # minimumSize 强加给父控件（此处算出 804px > 800 视口 → 外层滚动条 4px），
        # 按 _Card 同款配方钉成 minimumSizeHint（650px，实际高度由 stretch 拉伸到视口）
        self.setMinimumSize(self.minimumSizeHint())

    def sizeHint(self):
        """恒返回 minimumSizeHint（_Card 同款根治配方）：外层 QScrollArea
        （main_window._scrollify）只看 sizeHint——内容 sizeHint 842px 会推高
        外层滚动条（4px 超出 800 视口）；最小值 650 内，实际高度由布局
        stretch 拉伸到视口填满。"""
        return self.minimumSizeHint()

    def hasHeightForWidth(self):
        """False（08-22 功能巡检修复）：本页含两个内层 QScrollArea + 多处
        wordWrap QLabel，layout.hasHeightForWidth 为 True，而
        layout.heightForWidth(视口宽) 会返回内层滚动区完整内容高度
        （实测 888px，远超 sizeHint 654）——外层 _scrollify 的 QScrollArea
        （widgetResizable）优先用 heightForWidth 定高 → page 被撑到
        视口+110px 且 resize 后弹回（Qt C++ 内部写入，Python hook 拦不到）
        → 外层垂直滚动条恒出现（违反「不出现外层滚动条」偏好）。
        返回 False 后外层改用 sizeHint()（钉死 654），高度由 stretch 填充。
        实测 11 场景全绿（往返×3/窗口 900~760/宽 1400/1000/房间加载后）。"""
        return False

    def heightForWidth(self, w):
        """钉死为 minimumSizeHint 高（与 hasHeightForWidth=False 双保险：
        个别 Qt 调用路径仍会问 heightForWidth，不能让它拿到 888）。"""
        return self.minimumSizeHint().height()

    # ============================================================
    #  构建
    # ============================================================
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        # ---------------- 页面级顶栏（仿真心话页 top 行：标题左 + 按钮右，同行）----------------
        # 08-21 偏好「工具栏按钮放标题行右侧同行、不单独占行」：此处"标题行"=页面标题行。
        # 卡片标题行放不下 4 按钮（260/320px 左列实测溢出 41/29px），按钮上移页面顶栏。
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        trow = QHBoxLayout()
        trow.setSpacing(8)
        self.lbl_title = QLabel("🎭 角色扮演")
        self.lbl_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        trow.addWidget(self.lbl_title)
        trow.addStretch(1)
        self.btn_cfg_rules = QPushButton("⚙️ RP规则")
        self.btn_cfg_rules.setToolTip("RP 规则：摘要间隔/短期窗口/旁白字数上下限")
        self.btn_cfg_llm = QPushButton("🤖 LLM参数")
        self.btn_cfg_llm.setToolTip("LLM 调用参数：max_tokens/温度/thinking/json_mode/超时（5 个调用点共用）")
        self.btn_cfg_prompts = QPushButton("📝 提示词")
        self.btn_cfg_prompts.setToolTip("提示词：世界观生成/旁白/摘要/阶段指令 6 项全开放，每项可恢复默认")
        for b in (self.btn_cfg_rules, self.btn_cfg_llm, self.btn_cfg_prompts):
            b.setMinimumHeight(28)
            # 宽度自适应：style sizeHint 会把 emoji 宽度算窄导致切字，
            # 按实际文字像素宽度 + 充足内边距设最小宽度（08-21 用户反馈文字显示不全）
            b.setMinimumWidth(b.fontMetrics().horizontalAdvance(b.text()) + 44)
        btn_refresh = QPushButton("⟳ 刷新")
        btn_refresh.setMinimumHeight(28)
        btn_refresh.setMinimumWidth(btn_refresh.fontMetrics().horizontalAdvance(btn_refresh.text()) + 44)
        btn_refresh.setToolTip("刷新房间列表")
        for b in (btn_refresh, self.btn_cfg_rules, self.btn_cfg_llm, self.btn_cfg_prompts):
            trow.addWidget(b)
        self.btn_cfg_rules.clicked.connect(self._open_rules_dialog)
        self.btn_cfg_llm.clicked.connect(self._open_llm_dialog)
        self.btn_cfg_prompts.clicked.connect(self._open_prompts_dialog)
        btn_refresh.clicked.connect(self._refresh_rooms)
        tl.addLayout(trow)
        v.addWidget(top)

        # ---------------- 左列 ----------------
        self.card_rooms = _Card("🏠 房间列表")
        self.lst_rooms = _RoomListWidget()
        self.lst_rooms.clicked.connect(self._on_room_click)
        # 08-22 功能巡检修复：min 240 > 左列可用宽 220（left_wrap 240 固定 − _Card
        # margins 20）→ lst_rooms 水平溢出 → scroll_rooms 出内部横向滚动条。
        # 房间行控件全是 wordWrap QLabel（可任意窄），min 160 足够且不再溢出。
        self.lst_rooms.setMinimumWidth(160)
        # 内部滚动（房间数增长不撑外层）：房间行是 Expanding 自定义行控件，
        # 不套 QScrollArea 的话 7 房 ≈ 380px 会推高左列 minimumSizeHint
        self.scroll_rooms = QScrollArea()
        self.scroll_rooms.setWidgetResizable(True)
        self.scroll_rooms.setWidget(self.lst_rooms)
        self.scroll_rooms.setFrameShape(QScrollArea.NoFrame)
        self.card_rooms.add(self.scroll_rooms, 0)

        self.card_info = _Card("ℹ️ 指令说明")
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_info.setStyleSheet("color: #4a5568; font-size: 12px; line-height: 1.6;")
        self._fill_info()
        # 说明文字内部滚动：wordWrap QLabel 的 minimumSizeHint 按最窄宽度算高
        # （实测 ~600px），不套滚动区会把左列 minimumSizeHint 撑爆 → 外层滚动条
        self.scroll_info = QScrollArea()
        self.scroll_info.setWidgetResizable(True)
        self.scroll_info.setWidget(self.lbl_info)
        self.scroll_info.setFrameShape(QScrollArea.NoFrame)
        self.card_info.add(self.scroll_info, 1)

        left_wrap = _StretchyWidget()
        lv = QVBoxLayout(left_wrap)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        # 08-22 用户反馈「房间列表高度大一些，分一点说明的高度」：
        # 原 stretch 0:1 → 列表卡按内容收缩仅 ~116px（7 房需 ~432px，显示 2 行
        # + 内部滚动），说明卡独吞 ~624px（文案实际渲染宽 224px 下需 ~462px）。
        # 改 5:2：列表 ~528px → 7 房全显示无内部滚动；说明 ~212px → 显示
        # ~9 行指令，其余内部滚动（说明卡内部滚动是原设计兜底，文案/房间
        # 增长后都靠卡内滚动，不撑外层）。
        lv.addWidget(self.card_rooms, 5)
        lv.addWidget(self.card_info, 2)
        # 260px：设置按钮已上移页面顶栏，左列只放房间列表+说明
        left_wrap.setFixedWidth(260)
        row.addWidget(left_wrap)

        # ---------------- 右列 ----------------
        right = _StretchyWidget()
        self.right_v = QVBoxLayout(right)
        self.right_v.setContentsMargins(0, 0, 0, 0)
        self.right_v.setSpacing(8)

        # 房间总览卡
        self.card_overview = _Card("🏠 房间总览")
        self.lbl_meta = QLabel("← 选择左侧房间")
        self.lbl_meta.setStyleSheet("color: #656d76; font-size: 13px;")
        self.lbl_meta.setWordWrap(True)
        self.ed_world = QPlainTextEdit()
        self.ed_world.setReadOnly(True)
        self.ed_world.setPlaceholderText("（选择房间后显示世界观全文）")
        f = QFont()
        f.setPointSize(11)
        self.ed_world.setFont(f)
        # 80px：160 会让右列 minimumSizeHint 760px 超过 786 可用视口
        # （垂直滚动条常驻占 14px）→ 外层滚动条 4px；内容超高由内部滚动承担
        self.ed_world.setMinimumHeight(80)
        self.tabs_rp = QTabWidget()
        _compact_tabs(self.tabs_rp)
        self.tbl_chars = self._make_table(["QQ", "昵称", "角色名", "角色描述", "性格", "状态"])
        self.tbl_npcs = self._make_table(["姓名", "身份", "性格", "动机", "位置", "秘密"])
        self.tbl_items = self._make_table(["名称", "描述", "位置", "持有者", "状态"])
        self.tabs_rp.addTab(self.tbl_chars, "🎭 玩家")
        self.tabs_rp.addTab(self.tbl_npcs, "👥 NPC")
        self.tabs_rp.addTab(self.tbl_items, "📦 物品")
        self.card_overview.add(self.lbl_meta, 0)
        self.card_overview.add(self.ed_world, 1)
        self.card_overview.add(self.tabs_rp, 1)
        # 08-22 用户要求「增加剧情记录表格高度」：原 2:3 分配下剧情卡仅 246px
        # （表格 172px≈5 行）；改 1:1 → 剧情卡 ~366px（表格 ~300px≈10 行）。
        # 世界观框有内部滚动，缩高不影响看全文。
        self.right_v.addWidget(self.card_overview, 1)

        # 剧情记录卡
        btn_clean_one = QPushButton("🧹 清理此房间")
        btn_clean_one.setMinimumHeight(26)
        btn_clean_one.setToolTip("级联删除此房间全部数据（仅已结束房间可用）")
        btn_clean_all = QPushButton("🧹 清理全部已结束")
        btn_clean_all.setMinimumHeight(26)
        btn_clean_all.setToolTip("级联删除所有已结束房间")
        btn_clean_one.clicked.connect(lambda: self._cleanup_room())
        btn_clean_all.clicked.connect(lambda: self._cleanup_all_ended())
        self.card_story = _Card("📜 剧情记录", actions=[btn_clean_one, btn_clean_all])
        self.tbl_story = self._make_table(["轮次", "说话人", "内容", "时间"])
        self.tbl_story.doubleClicked.connect(self._on_story_double)
        # 08-22 用户要求「增加剧情记录表格高度」：表格 sizePolicy 是 Ignored
        # （layout 只认 minimum，sizeHint 无效），且右列 stretch 分配对内容重
        # 的总览卡拉不动（实测 2:3→1:1 高度不变）→ 给表格设最小高度硬底线
        # 260px（≈9 行；原 70px≈2.5 行）。世界观框有内部滚动，总览卡被压
        # 不影响看全文。页面 minimumSizeHint 650→~660 < 786 视口，无外层滚动条
        self.tbl_story.setMinimumHeight(260)
        self.lbl_story_hint = QLabel("双击行查看完整内容；剧情全量保留用于检索。")
        self.lbl_story_hint.setStyleSheet("color: #98a2b3; font-size: 11px;")
        # （原"当前场景+最新摘要"只读框 2026-08-22 应用户要求删除，
        #  腾出的高度让给剧情表格——表格 stretch=1 自动占满）
        self.card_story.add(self.tbl_story, 1)
        self.card_story.add(self.lbl_story_hint, 0)
        self.right_v.addWidget(self.card_story, 3)

        row.addWidget(right, 1)
        v.addLayout(row, 1)

    def _make_table(self, headers) -> QTableWidget:
        t = _TableNoEdit(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        return t

    # ============================================================
    #  左列数据
    # ============================================================
    def _fill_info(self):
        """指令说明：复用 help_menu 的单一事实源（群里 /帮助 同源）。"""
        try:
            import core.help_menu as help_menu
            cmds = help_menu.CATEGORIES.get("角色扮演", {}).get("commands", [])
        except Exception:
            cmds = []
        lines = [c["cmd"] + " —— " + c["desc"] for c in cmds]
        text = "\n".join(f"· {l}" for l in lines)
        text += ("\n\n流程：/开始扮演 建房 → 大家 /报名 角色名:描述 → 满 2 人 /开演。"
                 "\n开演后，报名玩家在群内 @bot 的发言即视为角色行动，旁白实时推进剧情。"
                 "\n\n本页为只读管理视图（bot 实时读写数据）；"
                 "唯一写操作=清理已结束房间（走 bot 进程级联删除）。")
        self.lbl_info.setText(text)

    def _load_rooms(self) -> list[dict]:
        """Worker 线程：直读 group_roleplay.db（只读）。"""
        sql = """
            SELECT r.room_id, r.group_id, r.creator_id, r.state, r.round_num,
                   r.created_at, r.updated_at, r.world_state,
                   (SELECT COUNT(*) FROM rp_characters c WHERE c.room_id = r.room_id AND c.active = 1) AS char_count,
                   (SELECT COUNT(*) FROM rp_npcs n WHERE n.room_id = r.room_id AND n.active = 1) AS npc_count,
                   (SELECT COUNT(*) FROM rp_story s WHERE s.room_id = r.room_id) AS story_count
            FROM rp_rooms r
            ORDER BY r.updated_at DESC
        """
        rows = api_client.query(self.mw.cfg, "roleplay", sql)
        import json as _json
        out = []
        for r in rows:
            try:
                ws = _json.loads(r.get("world_state") or "{}")
            except Exception:
                ws = {}
            # NPC 未导入 DB 表（/开演 前）→ 回退 world_state.initial_npcs
            if r["npc_count"] == 0 and isinstance(ws, dict):
                r["npc_count"] = len(ws.get("initial_npcs") or [])
            r["world_state"] = ws
            out.append(r)
        return out

    def _refresh_rooms(self):
        """刷新房间列表（保留选中：同 room_id 仍在则重选）。"""
        w = Worker(self._load_rooms)
        w.finished_ok.connect(self._on_rooms_ok)
        w.finished_err.connect(
            lambda e: self.mw.statusBar().showMessage(f"⚠️ 房间列表加载失败：{e}", 5000))
        w.start()
        self.mw._track(w)

    def _on_rooms_ok(self, rooms):
        self._rooms = rooms or []
        old_sel = self._room_id
        self.lst_rooms._clear_rows()
        for r in self._rooms:
            state = r["state"]
            name = f"群 {r['group_id']}　[{STATE_LABEL.get(state, state)}]"
            stat = (f"轮 {r['round_num']} · 玩家 {r['char_count']} · "
                    f"NPC {r['npc_count']} · 剧情 {r['story_count']}")
            self.lst_rooms.add_room(name, state, stat)
        # 恢复选中（room_id 仍在）
        if old_sel is not None:
            for i, r in enumerate(self._rooms):
                if r["room_id"] == old_sel:
                    self.lst_rooms.select(i)
                    self._room_sel = i
                    self._fill_right(r)
                    break
            else:
                self._room_sel = -1
                self._room_id = None
                self._clear_right()
        elif self._rooms:
            # 默认选第一行（进行中优先，其余按 updated_at）
            idx = 0
            for i, r in enumerate(self._rooms):
                if r["state"] == "playing":
                    idx = i
                    break
            self.lst_rooms.select(idx)
            self._room_sel = idx
            self._fill_right(self._rooms[idx])

    def _on_room_click(self, i):
        if 0 <= i < len(self._rooms):
            self.lst_rooms.select(i)
            self._room_sel = i
            self._fill_right(self._rooms[i])

    # ============================================================
    #  右列数据
    # ============================================================
    def _load_room_detail(self, room_id: int) -> dict:
        """Worker 线程：房间详情（角色/NPC/物品/剧情/场景状态，只读）。"""
        cfg = self.mw.cfg
        detail = {"room_id": room_id}

        def q(sql, params=()):
            return api_client.query(cfg, "roleplay", sql, params)

        detail["characters"] = q(
            "SELECT user_id, nickname, character_name, character_desc, personality, status_json "
            "FROM rp_characters WHERE room_id = ? AND active = 1 ORDER BY turn_order",
            (room_id,))
        detail["npcs"] = q(
            "SELECT name, role, personality, motivation, location, secret, relationships "
            "FROM rp_npcs WHERE room_id = ? AND active = 1 ORDER BY id",
            (room_id,))
        detail["items"] = q(
            "SELECT name, description, location, owner_user_id, owner_npc_id, state "
            "FROM rp_items WHERE room_id = ? ORDER BY id",
            (room_id,))
        detail["story"] = q(
            "SELECT round_num, speaker_type, speaker_name, content, created_at "
            "FROM rp_story WHERE room_id = ? ORDER BY round_num DESC, sequence DESC LIMIT 500",
            (room_id,))
        return detail

    def _fill_right(self, room: dict):
        """填充右列（Worker 加载详情，回调按 room_id 守卫防切房竞态）。"""
        rid = room["room_id"]
        self._room_id = rid
        state = room["state"]
        self.lbl_meta.setText(
            f"群 {room['group_id']}｜{STATE_LABEL.get(state, state)}｜"
            f"第 {room['round_num']} 轮｜创建者 {room['creator_id']}｜"
            f"建于 {_ts(room['created_at'])}｜更新于 {_ts(room['updated_at'])}")
        self.ed_world.setPlainText(world_to_text(room.get("world_state")))
        # 先清空表格再异步填（避免旧房数据残留误导）
        for t in (self.tbl_chars, self.tbl_npcs, self.tbl_items, self.tbl_story):
            t.setRowCount(0)

        w = Worker(self._load_room_detail, rid)
        w.finished_ok.connect(lambda d, _rid=rid: self._on_detail_ok(_rid, d))
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(
            f"⚠️ 房间详情加载失败：{e}", 5000))
        w.start()
        self.mw._track(w)

    def _on_detail_ok(self, rid: int, d: dict):
        # 守卫：切房后旧回调丢弃（控件内容可能已被新房覆盖流程接管）
        if rid != self._room_id:
            return
        import json as _json

        # 玩家表
        chars = d.get("characters") or []
        self.tbl_chars.setRowCount(len(chars))
        for i, c in enumerate(chars):
            try:
                status = _json.loads(c.get("status_json") or "{}")
                status_s = " ".join(f"{k}:{v}" for k, v in status.items()) or "—"
            except Exception:
                status_s = "—"
            vals = [str(c["user_id"]), c["nickname"] or "", c["character_name"] or "",
                    c.get("character_desc") or "", _fmt_list(c.get("personality")), status_s]
            self._fill_row(self.tbl_chars, i, vals)

        # NPC 表（DB 表优先，空则回退 world_state.initial_npcs）
        npcs = d.get("npcs") or []
        if not npcs:
            room = next((r for r in self._rooms if r["room_id"] == rid), None)
            ws = (room or {}).get("world_state") or {}
            npcs = [{"name": n.get("name", "?"), "role": n.get("role", ""),
                     "personality": n.get("personality", []),
                     "motivation": n.get("motivation", ""),
                     "location": n.get("position", ""), "secret": n.get("secret", ""),
                     "_from_ws": True} for n in (ws.get("initial_npcs") or [])
                    if isinstance(n, dict)]
        self.tbl_npcs.setRowCount(len(npcs))
        for i, n in enumerate(npcs):
            vals = [n.get("name", "?"), n.get("role") or "", _fmt_list(n.get("personality")),
                    n.get("motivation") or "", n.get("location") or "", n.get("secret") or ""]
            self._fill_row(self.tbl_npcs, i, vals)

        # 物品表（持有者：user_id/npc_id → 名字）
        user_names = {c["user_id"]: c["nickname"] for c in chars}
        npc_names = {n.get("name"): n.get("name") for n in npcs}
        items = d.get("items") or []
        if not items:
            ws = ((next((r for r in self._rooms if r["room_id"] == rid), None)) or {}).get("world_state") or {}
            items = [{"name": i.get("name", "?"), "description": i.get("description", ""),
                      "location": i.get("location", ""), "owner_user_id": None,
                      "owner_npc_id": None, "state": "initial", "_owner_ws": i.get("owner", "")}
                     for i in (ws.get("initial_items") or []) if isinstance(i, dict)]
        self.tbl_items.setRowCount(len(items))
        for i, it in enumerate(items):
            if it.get("_owner_ws"):
                owner = it["_owner_ws"]
            elif it.get("owner_user_id"):
                owner = user_names.get(it["owner_user_id"], f"玩家 {it['owner_user_id']}")
            elif it.get("owner_npc_id"):
                owner = f"NPC#{it['owner_npc_id']}"
            else:
                owner = "散落"
            vals = [it.get("name", "?"), it.get("description") or "", it.get("location") or "",
                    owner, it.get("state") or ""]
            self._fill_row(self.tbl_items, i, vals)

        # 剧情表（新→旧展示；双击看全文）
        story = d.get("story") or []
        self.tbl_story.setRowCount(len(story))
        for i, s in enumerate(story):
            speaker = {"narrator": "旁白", "player": s.get("speaker_name") or "玩家",
                       "system": "系统"}.get(s["speaker_type"], s["speaker_type"])
            content = s.get("content") or ""
            preview = content if len(content) <= 80 else content[:80] + "…"
            self._fill_row(self.tbl_story, i,
                           [str(s["round_num"]), speaker, preview, _ts(s.get("created_at"))])
            it = self.tbl_story.item(i, 2)
            it.setData(Qt.UserRole, content)  # 全文存 item，双击读取

    @staticmethod
    def _fill_row(table: QTableWidget, row: int, vals: list):
        for c, v in enumerate(vals):
            item = QTableWidgetItem(str(v))
            item.setToolTip(str(v))
            table.setItem(row, c, item)

    def _clear_right(self):
        self.lbl_meta.setText("← 选择左侧房间")
        self.ed_world.setPlainText("")
        for t in (self.tbl_chars, self.tbl_npcs, self.tbl_items, self.tbl_story):
            t.setRowCount(0)

    def _on_story_double(self, idx):
        item = self.tbl_story.item(idx.row(), 2)
        if item is None:
            return
        full = item.data(Qt.UserRole) or item.text()
        dlg = QDialog(self)
        dlg.setWindowTitle("剧情全文")
        dlg.resize(700, 420)
        lv = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(False)
        tb.setPlainText(full)
        lv.addWidget(tb)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.close)
        btns.clicked.connect(dlg.close)
        lv.addWidget(btns)
        dlg.exec()

    # ============================================================
    #  清理（唯一写操作：走控制 API，bot 进程内级联删除）
    # ============================================================
    def _cleanup_room(self):
        if self._room_id is None:
            self.mw.statusBar().showMessage("未选中房间", 4000)
            return
        room = next((r for r in self._rooms if r["room_id"] == self._room_id), None)
        if room is None:
            return
        if room["state"] != "ended":
            self.mw.statusBar().showMessage(
                f"房间状态为「{STATE_LABEL.get(room['state'], room['state'])}」，仅已结束房间可清理", 6000)
            return
        if not self.mw.confirm(
                "清理房间",
                f"将级联删除 群 {room['group_id']} 房间的全部数据"
                f"（玩家 {room['char_count']} / NPC {room['npc_count']} / "
                f"剧情 {room['story_count']} 条），不可恢复。确定？",
                "清理"):
            return
        self._do_cleanup(room["room_id"])

    def _cleanup_all_ended(self):
        ended = [r for r in self._rooms if r["state"] == "ended"]
        if not ended:
            self.mw.statusBar().showMessage("没有已结束的房间", 4000)
            return
        if not self.mw.confirm(
                "清理全部已结束房间",
                f"将级联删除 {len(ended)} 个已结束房间的全部数据"
                f"（进行中/待报名房间不受影响），不可恢复。确定？",
                "清理"):
            return
        self._do_cleanup(None)

    def _do_cleanup(self, room_id):
        def _post():
            import urllib.request, urllib.error, json as _json3
            url = (f"http://{self.mw.cfg.get('CONTROL_API_HOST', '127.0.0.1')}:"
                   f"{self.mw.cfg.get('CONTROL_API_PORT', 8697)}/roleplay/cleanup")
            req = urllib.request.Request(
                url, data=_json3.dumps({"room_id": room_id}).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _json3.loads(resp.read())

        w = Worker(_post)

        def _ok(r):
            if r.get("ok"):
                self.mw.statusBar().showMessage(
                    f"✅ 已清理 {r.get('removed', 0)} 个房间（剧情 {r.get('story_removed', 0)} 条）", 8000)
                self._refresh_rooms()
            else:
                self.mw.statusBar().showMessage(f"⚠️ 清理失败：{r.get('error', '')}", 8000)

        w.finished_ok.connect(_ok)
        w.finished_err.connect(
            lambda e: self.mw.statusBar().showMessage(
                f"⚠️ bot 未运行或清理请求失败（{e}）——bot 运行时重试", 8000))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  设置弹窗
    # ============================================================
    def _open_rules_dialog(self):
        from roleplay_settings_dialogs import RulesDialog
        d = RulesDialog(self.mw)
        d.exec()

    def _open_llm_dialog(self):
        from roleplay_settings_dialogs import LLMParamsDialog
        d = LLMParamsDialog(self.mw)
        d.exec()

    def _open_prompts_dialog(self):
        from roleplay_settings_dialogs import PromptsDialog
        d = PromptsDialog(self.mw)
        d.exec()
