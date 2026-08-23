"""
tab_personas.py — 人设画像页（08-20 重做 v2：人设字段级编辑）
===========================
布局（内容用 stretch 填满视口高度，不出现外层滚动条）：
  功能栏：搜索(QQ/昵称) | 搜索 | 刷新 | 复制内容 | 状态提示
  主体三列：
    左列（固定宽）：群列表（上，矮）+ 用户列表（下，高）
        选择顺序不分先后——选用户会自动定位到群
    中列（固定宽）：所选用户的人设（JSON 各小标题）+ 画像（【】各分节）标题表
    右列（拉伸）：  人设小节 → 字段级可编辑网格（每个 dict key / list item / 字符串
                    一个文本框，可单独编辑/删除，保存时从 Python 对象重建 JSON，
                    绝不拼接字符串 → 结构安全）
                    画像小节 → 只读文本框
"""

import copy
import json
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QGroupBox, QPlainTextEdit,
    QStackedWidget, QScrollArea, QFrame, QSizePolicy,
)

import api_client
from worker import Worker


# 人设 JSON 顶层 key → 中文标题（保序；未收录的 key 追加在后）
PERSONA_KEYS = [
    ("identity", "身份"),
    ("interests", "兴趣"),
    ("personality", "性格"),
    ("relationships", "关系"),
    ("weaknesses_taboos", "弱点/雷点"),
    ("group_role", "群内角色"),
    ("catchphrases", "口头禅"),
    ("sexual_experience", "性经历"),
    ("sexual_preferences", "性偏好"),
]


def _split_profile_spans(text: str):
    """画像文本按行首【标题】切分 → [(标题, body, body_start, body_end)]
    start/end 是 body 在原文中的字符偏移（保存时按偏移替换，其余内容逐字节保留）。
    body 含分节间的空白（如末尾 \\n\\n），显示/保存时 strip。
    无标题的纯文本 → 整篇一节（标题='整体'）。
    """
    text = text or ""
    matches = list(re.finditer(r"(?m)^(【[^】]+】)\s*$", text))
    if not matches:
        return [("整体", text, 0, len(text))]
    out = []
    if matches[0].start() > 0:
        pre = text[:matches[0].start()].strip()
        if pre:
            out.append(("（开头）", pre, 0, matches[0].start()))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1), text[start:end], start, end))
    return out


class _TableNoEdit(QTableWidget):
    """不可编辑表格"""

    def __init__(self, rows, cols, *a, **kw):
        super().__init__(rows, cols, *a, **kw)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setAlternatingRowColors(True)


class _Field(QWidget):
    """一个可编辑字段行：[标签][文本框][✕删除]"""

    def __init__(self, label: str, text: str, deletable: bool = True, parent=None):
        super().__init__(parent)
        self.orig_text = text      # 渲染时的文本（未修改判定基准）
        self.orig_value = None     # 渲染时的原始 JSON 值（未修改则原样保留，防类型漂移）
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        lab = QLabel(label)
        lab.setFixedWidth(120)          # 08-21：限制标签宽度（关系里的昵称 key 很长）
        lab.setWordWrap(True)           # 超出换行，不再挤压右侧文本框
        lab.setStyleSheet("color: #656d76;")
        self.ed = QPlainTextEdit()
        self.ed.setPlainText(text)
        self.ed.setFixedHeight(58)
        self.ed.setStyleSheet("font-size: 13px;")
        self.btn_del = QPushButton("🗑")
        self.btn_del.setFixedWidth(44)   # 08-21：34→44，emoji 图标显示不全
        self.btn_del.setToolTip("删除该字段")
        if not deletable:
            self.btn_del.setEnabled(False)
        row.addWidget(lab)
        row.addWidget(self.ed, 1)
        row.addWidget(self.btn_del)


class TabPersonas(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._silent = False            # 程序化选中表格时屏蔽联动
        self._group_rows = []           # [{group_id, n}]
        self._user_rows = []            # [{user_id, group_id, nickname, persona, profile}]
        self._all_users = []            # 未过滤的当前群全部用户
        self._section_rows = []         # [(类型, 标题, 原始值)]
        self._cur_user = None
        self._cur_section_idx = -1
        self._fields: list[_Field] = []  # 当前人设小节的字段行
        self._build()
        self._load_groups()

    # ------------------------------------------------------------
    #  构建
    # ------------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        # ---------------- 功能栏 ----------------
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索：用户QQ 或 昵称")
        self.ed_search.setFixedWidth(220)
        self.btn_search = QPushButton("🔍 搜索")
        self.btn_search.setFixedWidth(80)
        self.btn_refresh = QPushButton("🔄 刷新")
        self.btn_refresh.setFixedWidth(80)
        self.btn_copy = QPushButton("📋 复制内容")
        self.btn_copy.setFixedWidth(96)
        bar.addWidget(self.ed_search)
        bar.addWidget(self.btn_search)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_copy)
        bar.addStretch(1)
        # ---------------- 设置按钮（4 个配置弹窗，2026-08-21 新增） ----------------
        self.btn_cfg_preprocess = QPushButton("⚙️ 预处理")
        self.btn_cfg_preprocess.setFixedWidth(86)
        self.btn_cfg_preprocess.setToolTip("数据预处理：触发阈值/批次大小/上下文窗口/session 间隔/并发")
        self.btn_cfg_llm = QPushButton("🤖 参数")
        self.btn_cfg_llm.setFixedWidth(86)
        self.btn_cfg_llm.setToolTip("LLM 调用参数：7 阶段的 max_tokens/温度/thinking/json_mode/超时")
        self.btn_cfg_rules = QPushButton("📏 规则")
        self.btn_cfg_rules.setFixedWidth(86)
        self.btn_cfg_rules.setToolTip("人设画像规则：字段限制/字数区间/压缩循环")
        self.btn_cfg_prompts = QPushButton("📝 提示词")
        self.btn_cfg_prompts.setFixedWidth(92)
        self.btn_cfg_prompts.setToolTip("LLM 处理提示词：全部提示词可编辑，每项可恢复默认")
        for b in (self.btn_cfg_preprocess, self.btn_cfg_llm, self.btn_cfg_rules, self.btn_cfg_prompts):
            b.setMinimumHeight(28)
            bar.addWidget(b)
        self.btn_cfg_preprocess.clicked.connect(self._open_preprocess_dialog)
        self.btn_cfg_llm.clicked.connect(self._open_llm_dialog)
        self.btn_cfg_rules.clicked.connect(self._open_rules_dialog)
        self.btn_cfg_prompts.clicked.connect(self._open_prompts_dialog)
        self.lbl_bar_state = QLabel("")
        bar.addWidget(self.lbl_bar_state)
        v.addLayout(bar)

        # ---------------- 三列主体 ----------------
        cols = QHBoxLayout()
        cols.setSpacing(8)

        # --- 左列：群列表（矮）+ 用户列表（高） ---
        left = QVBoxLayout()
        left.setSpacing(8)

        gb_group = QGroupBox("群列表")
        gl = QVBoxLayout(gb_group)
        gl.setContentsMargins(6, 6, 6, 6)
        self.tbl_group = _TableNoEdit(0, 2)
        self.tbl_group.setHorizontalHeaderLabels(["群号", "人数"])
        self.tbl_group.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_group.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_group.verticalHeader().setVisible(False)
        # 08-21：QTableWidget 的 minimumSizeHint=列宽总和，会把固定宽容器撑爆
        # （setFixedWidth 的 max 被 layout min 顶穿）。Ignored=布局忽略其 sizeHint。
        self.tbl_group.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.tbl_group.setFixedHeight(124)   # 比下方用户列表矮
        self.tbl_group.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        gl.addWidget(self.tbl_group)
        left.addWidget(gb_group)

        gb_user = QGroupBox("用户列表")
        ul = QVBoxLayout(gb_user)
        ul.setContentsMargins(6, 6, 6, 6)
        self.tbl_user = _TableNoEdit(0, 4)
        self.tbl_user.setHorizontalHeaderLabels(["用户QQ", "昵称", "人设", "画像"])
        # 列宽：去掉群号列（与群列表重复）；08-21 左列 470→370→320（省出的宽度让给右列内容）。
        # QQ号定宽 96，昵称 Stretch 独享剩余宽度，人设/画像固定 2×42 →
        # 总宽恒等于表格宽，无横向滚动条（超长昵称截断 + tooltip 看全名）。
        self.tbl_user.horizontalHeader().setMinimumSectionSize(24)
        self.tbl_user.setColumnWidth(0, 96)   # QQ号定宽（10位数字）
        self.tbl_user.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)  # 昵称独享剩余宽度
        self.tbl_user.setColumnWidth(2, 42)   # 08-21：30→42
        self.tbl_user.setColumnWidth(3, 42)   # 08-21：30→42
        self.tbl_user.verticalHeader().setVisible(False)
        # 08-21：同上——忽略表格 sizeHint，防止列宽总和撑爆左列 320 容器
        self.tbl_user.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.tbl_user.setWordWrap(False)
        # 横向滚动条关死：内容列截断+tooltip 看全名，两 Stretch 列自动均分剩余宽度
        # （223 行时纵向滚动条占 12px，AsNeeded 会被顶出多余的横向滚条）
        self.tbl_user.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ul.addWidget(self.tbl_user)
        left.addWidget(gb_user, 1)

        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(320)   # 08-21：370→320，宽度让给右列内容区
        cols.addWidget(left_w)

        # --- 中列：人设/画像分节标题 ---
        gb_sec = QGroupBox("人设/画像 分节")
        sl = QVBoxLayout(gb_sec)
        sl.setContentsMargins(6, 6, 6, 6)
        self.tbl_sec = _TableNoEdit(0, 2)
        self.tbl_sec.setHorizontalHeaderLabels(["类型", "标题"])
        self.tbl_sec.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_sec.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_sec.verticalHeader().setVisible(False)
        # 08-21：同左列表格——忽略 sizeHint 防撑爆固定宽容器
        self.tbl_sec.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.tbl_sec.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sl.addWidget(self.tbl_sec)
        gb_sec_w = QWidget()
        gb_sec_w.setLayout(sl)
        gb_sec_w.setFixedWidth(220)
        cols.addWidget(gb_sec_w)

        # --- 右列：内容（人设=可编辑字段网格 / 画像=只读文本） ---
        gb_content = QGroupBox("内容")
        cl = QVBoxLayout(gb_content)
        cl.setContentsMargins(6, 6, 6, 6)

        # 小节操作栏（人设模式下显示）
        self.sec_bar_w = QWidget()
        self.sec_bar = QHBoxLayout(self.sec_bar_w)
        self.sec_bar.setContentsMargins(0, 0, 0, 0)
        self.sec_bar.setSpacing(6)
        self.lbl_sec_title = QLabel("")
        self.lbl_sec_title.setStyleSheet("font-weight: bold;")
        self.btn_save = QPushButton("💾 保存人设修改")
        self.btn_save.setFixedWidth(150)
        self.btn_add_item = QPushButton("➕ 追加条目")
        self.btn_add_item.setFixedWidth(110)
        self.btn_add_item.setToolTip("列表型小节（兴趣/口头禅等）追加一个新条目")
        self.sec_bar.addWidget(self.lbl_sec_title)
        self.sec_bar.addStretch(1)
        self.sec_bar.addWidget(self.btn_add_item)
        self.sec_bar.addWidget(self.btn_save)
        cl.addWidget(self.sec_bar_w)

        # 内容堆栈：0=字段编辑区 1=只读文本
        self.stack = QStackedWidget()

        self._field_scroll = QScrollArea()
        self._field_scroll.setWidgetResizable(True)
        self._field_scroll.setFrameShape(QFrame.NoFrame)
        self._field_host = QWidget()
        self._field_layout = QVBoxLayout(self._field_host)
        self._field_layout.setContentsMargins(2, 2, 2, 2)
        self._field_layout.setSpacing(6)
        self._field_layout.addStretch(1)
        self._field_scroll.setWidget(self._field_host)
        self.stack.addWidget(self._field_scroll)

        self.txt_content = QPlainTextEdit()
        self.txt_content.setReadOnly(True)
        self.txt_content.setPlaceholderText("在中间列选择一个分节标题查看内容…")
        # 08-21：theme 的 QPlainTextEdit 全局规则是 12px（日志等宽专用），
        # 内联钉回 13px 与界面正文/字段编辑框一致
        self.txt_content.setStyleSheet("font-size: 13px;")
        self.stack.addWidget(self.txt_content)
        cl.addWidget(self.stack, 1)

        cols.addWidget(gb_content, 1)

        v.addLayout(cols, 1)

        # ---------------- 信号 ----------------
        self.ed_search.returnPressed.connect(self._apply_search)
        self.btn_search.clicked.connect(self._apply_search)
        self.btn_refresh.clicked.connect(self._load_groups)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_save.clicked.connect(self._save_persona)
        self.btn_add_item.clicked.connect(self._add_list_item)
        self.tbl_group.itemSelectionChanged.connect(self._on_group_select)
        self.tbl_user.itemSelectionChanged.connect(self._on_user_select)
        self.tbl_sec.itemSelectionChanged.connect(self._on_section_select)

    # ------------------------------------------------------------
    #  设置弹窗（数据预处理 / LLM 参数 / 规则 / 提示词，2026-08-21 新增）
    #  延迟 import 弹窗模块：加快 GUI 启动 + 避免启动期依赖
    # ------------------------------------------------------------
    def _open_preprocess_dialog(self):
        from persona_settings_dialogs import PreprocessDialog
        d = PreprocessDialog(self.mw)
        d.exec()

    def _open_llm_dialog(self):
        from persona_settings_dialogs import LLMParamsDialog
        d = LLMParamsDialog(self.mw)
        d.exec()

    def _open_rules_dialog(self):
        from persona_settings_dialogs import RulesDialog
        d = RulesDialog(self.mw)
        d.exec()

    def _open_prompts_dialog(self):
        from persona_settings_dialogs import PromptsDialog
        d = PromptsDialog(self.mw)
        d.exec()

    # ------------------------------------------------------------
    #  数据加载
    # ------------------------------------------------------------
    def _load_groups(self):
        w = Worker(api_client.query, self.mw.cfg, "personas",
                   "SELECT group_id, COUNT(*) AS n FROM ("
                   " SELECT DISTINCT user_id, group_id FROM user_personas "
                   " UNION SELECT DISTINCT user_id, group_id FROM user_profiles"
                   ") GROUP BY group_id ORDER BY group_id")

        def _ok(rows):
            self._group_rows = rows
            self._fill_group_table()
            self.mw.statusBar().showMessage(f"群：{len(rows)} 个")
            self._load_users(None)   # 默认展示全部用户

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"群列表加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_group_table(self):
        self._silent = True
        self.tbl_group.setRowCount(len(self._group_rows))
        for i, r in enumerate(self._group_rows):
            self.tbl_group.setItem(i, 0, QTableWidgetItem(str(r["group_id"])))
            self.tbl_group.setItem(i, 1, QTableWidgetItem(str(r["n"])))
        self._silent = False

    def _load_users(self, group_id):
        where = " WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()

        def _do():
            ps = api_client.query(self.mw.cfg, "personas",
                                  f"SELECT user_id, group_id, nickname, persona FROM user_personas{where}",
                                  params)
            fs = api_client.query(self.mw.cfg, "personas",
                                  f"SELECT user_id, group_id, nickname, profile FROM user_profiles{where}",
                                  params)
            merged = {}
            for r in list(ps) + list(fs):
                key = (r["user_id"], r["group_id"])
                m = merged.setdefault(key, {"user_id": r["user_id"], "group_id": r["group_id"],
                                            "nickname": r["nickname"], "persona": None, "profile": None})
                persona = r.get("persona") if "persona" in r.keys() else None
                profile = r.get("profile") if "profile" in r.keys() else None
                if persona is not None:
                    m["persona"] = persona
                    m["nickname"] = r["nickname"] or m["nickname"]
                if profile is not None:
                    m["profile"] = profile
            return list(merged.values())

        w = Worker(_do)

        def _ok(rows):
            self._all_users = rows
            self._apply_search()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"用户加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _apply_search(self):
        kw = self.ed_search.text().strip()
        rows = self._all_users
        if kw:
            if kw.isdigit():
                rows = [r for r in rows if str(r["user_id"]) == kw or str(r["group_id"]) == kw]
            else:
                rows = [r for r in rows if kw.lower() in (r["nickname"] or "").lower()]
        self._user_rows = rows
        self._fill_user_table()
        self.lbl_bar_state.setText(f"{len(rows)} 人")

    def _fill_user_table(self):
        self._silent = True
        self.tbl_user.setRowCount(len(self._user_rows))
        for i, r in enumerate(self._user_rows):
            self.tbl_user.setItem(i, 0, QTableWidgetItem(str(r["user_id"])))
            it = QTableWidgetItem(r["nickname"] or "")
            it.setToolTip(r["nickname"] or "")   # 截断时悬浮看全名
            self.tbl_user.setItem(i, 1, it)
            self.tbl_user.setItem(i, 2, QTableWidgetItem("✓" if r["persona"] else "—"))
            self.tbl_user.setItem(i, 3, QTableWidgetItem("✓" if r["profile"] else "—"))
        self._silent = False
        if not self._user_rows:
            self.lbl_bar_state.setText("0 人（无匹配）")

    # ------------------------------------------------------------
    #  选择联动
    # ------------------------------------------------------------
    def _on_group_select(self):
        if self._silent:
            return
        row = self.tbl_group.currentRow()
        if row < 0 or not self._group_rows:
            return
        gid = self._group_rows[row]["group_id"]
        self._clear_user()
        self._load_users(gid)

    def _on_user_select(self):
        if self._silent:
            return
        row = self.tbl_user.currentRow()
        if row < 0 or not self._user_rows:
            return
        u = self._user_rows[row]
        # 自动定位群（顺序不分先后的关键）
        self._select_group_row(u["group_id"])
        self._load_sections(u)

    def _select_group_row(self, group_id):
        for i, g in enumerate(self._group_rows):
            if g["group_id"] == group_id:
                self._silent = True
                self.tbl_group.selectRow(i)
                self._silent = False
                return

    def _clear_user(self):
        self._cur_user = None
        self._section_rows = []
        self._cur_section_idx = -1
        self._fields = []
        self._clear_field_layout()
        self._silent = True
        self.tbl_user.clearSelection()
        self.tbl_sec.setRowCount(0)
        self._silent = False
        self.txt_content.clear()
        self.txt_content.setPlaceholderText("在左侧用户列表选择用户…")

    def _load_sections(self, u):
        self._cur_user = u
        self._cur_section_idx = -1
        self._fields = []
        sections = []   # [(类型, 标题, 值, body_start, body_end)]
        if u["persona"]:
            try:
                p = json.loads(u["persona"])
                if isinstance(p, dict):
                    known = {k for k, _ in PERSONA_KEYS}
                    for k, label in PERSONA_KEYS:
                        if k in p:
                            sections.append(("人设", label, p[k], None, None))
                    for k, val in p.items():
                        if k not in known:
                            sections.append(("人设", k, val, None, None))
                else:
                    sections.append(("人设", "（非标准结构）", p, None, None))
            except Exception:
                sections.append(("人设", "（JSON解析失败）", u["persona"], None, None))
        if u["profile"]:
            for title, body, s, e in _split_profile_spans(u["profile"]):
                sections.append(("画像", title, body, s, e))
        self._section_rows = sections

        self._silent = True
        self.tbl_sec.setRowCount(len(sections))
        for i, (typ, title, _val, _s, _e) in enumerate(sections):
            self.tbl_sec.setItem(i, 0, QTableWidgetItem(typ))
            self.tbl_sec.setItem(i, 1, QTableWidgetItem(title))
        self._silent = False
        if sections:
            self.mw.statusBar().showMessage(f"{u['nickname'] or u['user_id']}：{len(sections)} 个分节")
        if self.tbl_sec.rowCount() > 0:
            self._silent = True
            self.tbl_sec.selectRow(0)
            self._silent = False
            self._show_section(0)

    def _on_section_select(self):
        if self._silent:
            return
        row = self.tbl_sec.currentRow()
        if row < 0 or not self._section_rows:
            return
        self._show_section(row)

    # ------------------------------------------------------------
    #  内容区渲染
    # ------------------------------------------------------------
    def _clear_field_layout(self):
        # 08-21 修重叠 bug：取出 widget 后必须 deleteLater，
        # 否则旧字段行残留在视口下继续绘制 → 切换分节时内容叠加
        # 08-21 修重复提示：hint 加在末尾 stretch 之后，原先只清到剩 1 个 item
        # （保留 stretch）→ 旧 hint 残留，每切一个分节多一条。现全清后重建 stretch。
        while self._field_layout.count() > 0:
            it = self._field_layout.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._field_layout.addStretch(1)
        self._fields = []

    def _show_section(self, row):
        if not (0 <= row < len(self._section_rows)):
            return
        self._cur_section_idx = row
        typ, title, val, _s, _e = self._section_rows[row]
        self.lbl_sec_title.setText(f"{typ} · {title}")
        if typ == "人设" and not isinstance(val, str):
            # dict/list → 字段级网格
            self._render_persona_fields(val)
            self.stack.setCurrentIndex(0)
            self._field_scroll.verticalScrollBar().setValue(0)   # 切换分节回顶部
            self.sec_bar_w.setVisible(True)
        else:
            # 人设 str 节（性格/群内角色等）与画像节 → 大段可编辑文本
            self._render_text(title, val, editable=True)
            self.stack.setCurrentIndex(1)
            self.sec_bar_w.setVisible(True)

    def _render_text(self, title, val, editable: bool = False):
        self._fields = []
        self._clear_field_layout()
        if isinstance(val, str):
            body = val
        else:
            body = json.dumps(val, ensure_ascii=False, indent=1)
        # 显示不带【标题】前缀（操作栏已显示当前分节名），保存时按类型重组
        self.txt_content.setReadOnly(not editable)
        self.txt_content.setPlainText(body.strip())
        self.btn_save.setVisible(editable)
        self.btn_add_item.setVisible(False)

    def _render_persona_fields(self, val):
        """人设小节 → 字段级网格。
        dict → 每个 key 一个文本框；list → 每个 item 一个文本框；str → 单个文本框。
        _fields 存 [(kind, key_or_idx, _Field)]，kind ∈ dict|list|str。
        """
        self._fields = []
        self._clear_field_layout()
        self.btn_add_item.setVisible(isinstance(val, list))

        if isinstance(val, dict):
            for k, x in val.items():
                if isinstance(x, str):
                    txt, orig = x, x
                else:
                    txt, orig = ("" if x is None else json.dumps(x, ensure_ascii=False)), x
                f = _Field(k, txt, deletable=True)
                f.orig_text, f.orig_value = txt, orig
                f.btn_del.clicked.connect(lambda _=False, kk=k, ff=f: self._del_field("dict", kk, ff))
                self._add_field_row(f, "dict", k)
        elif isinstance(val, list):
            for i, x in enumerate(val):
                if isinstance(x, str):
                    txt, orig = x, x
                else:
                    txt, orig = json.dumps(x, ensure_ascii=False), x
                f = _Field(f"条目 {i + 1}", txt, deletable=True)
                f.orig_text, f.orig_value = txt, orig
                f.btn_del.clicked.connect(lambda _=False, idx=i, ff=f: self._del_field("list", idx, ff))
                self._add_field_row(f, "list", i)
        else:   # str / 其他（兜底按字符串）
            f = _Field("内容", val if isinstance(val, str) else str(val), deletable=False)
            self._add_field_row(f, "str", 0)
        # 提示行
        hint = QLabel("🗑 删除字段/条目；修改后点「保存人设修改」生效（保存时重建 JSON 并校验，结构安全）")
        hint.setStyleSheet("color: #9aa4b2; font-size: 11px;")
        self._field_layout.addWidget(hint, 0, Qt.AlignBottom)

    def _add_field_row(self, f: _Field, kind, key):
        self._field_layout.insertWidget(self._field_layout.count() - 1, f)
        self._fields.append((kind, key, f))

    def _del_field(self, kind, key, f: _Field):
        """移除字段行（保存时才真正改 JSON；此处仅从界面移除）"""
        self._fields = [t for t in self._fields if t[2] is not f]
        f.setParent(None)
        f.deleteLater()

    def _add_list_item(self):
        if self._cur_section_idx < 0:
            return
        typ, _title, val, _s, _e = self._section_rows[self._cur_section_idx]
        if typ != "人设" or not isinstance(val, list):
            self.mw.statusBar().showMessage("仅列表型小节（兴趣/口头禅等）可追加条目")
            return
        f = _Field(f"条目 {len(val) + 1}", "", deletable=True)
        # 追加的条目 key = len(val)（占位；保存时按当前 _fields 顺序重建 list）
        f.btn_del.clicked.connect(lambda _=False, ff=f: self._del_field("list", len(val), ff))
        self._add_field_row(f, "list", len(val))
        f.ed.setFocus()

    # ------------------------------------------------------------
    #  保存（JSON 结构安全核心）
    # ------------------------------------------------------------
    def _collect_edits(self):
        """从 _fields 重建当前小节的值。
        dict：保留原 key 集合，应用编辑；被删除的 key 移除；
        list：按 _fields 顺序重建（删除即移除，追加即新增）；
        str：整体替换。
        返回值：(new_val, changed: bool)
        """
        if self._cur_section_idx < 0:
            return None, False
        typ, _title, val, _s, _e = self._section_rows[self._cur_section_idx]
        if typ != "人设" or isinstance(val, str):
            return None, False
        if isinstance(val, dict):
            new_val = copy.deepcopy(val)
            changed = False
            kept = set()
            for kind, key, f in self._fields:
                if kind != "dict":
                    continue
                kept.add(key)
                text = f.ed.toPlainText()
                if text == f.orig_text:
                    continue   # 未修改：原样保留（防非字符串值类型漂移）
                new_val[key] = text
                changed = True
            # 被删除的 key
            for key in list(new_val.keys()):
                if key not in kept:
                    del new_val[key]
                    changed = True
            return new_val, changed
        if isinstance(val, list):
            list_fields = [f for kind, _k, f in self._fields if kind == "list"]
            new_val = [f.ed.toPlainText() for f in list_fields]
            orig_list = [f.orig_text for f in list_fields]
            # 变更 = 条目数变了，或对应位置内容变了（追加的新字段 orig_text 为空串，
            # 写入内容后 zip 对比也会命中）
            changed = (len(new_val) != len(val)) or any(
                n != o for n, o in zip(new_val, orig_list))
            return new_val, changed
        return None, False

    def _save_persona(self):
        """统一保存入口：按当前分节类型走 人设 JSON / 画像偏移替换 两条路径。"""
        u = self._cur_user
        if not u or self._cur_section_idx < 0:
            self.mw.statusBar().showMessage("无已加载的小节可保存")
            return
        typ, title, val, s, e = self._section_rows[self._cur_section_idx]
        text = self.txt_content.toPlainText().strip()

        if typ == "人设":
            if isinstance(val, str):
                if text == val.strip():
                    self.mw.statusBar().showMessage("无变更")
                    return
                try:
                    persona = json.loads(u["persona"])
                    if not isinstance(persona, dict):
                        raise ValueError("persona 非 dict")
                except Exception as ex:
                    self.mw.statusBar().showMessage(f"当前人设 JSON 无法解析，拒绝保存：{ex}")
                    return
                target_key = self._find_persona_key(persona, title)
                if target_key is None:
                    self.mw.statusBar().showMessage(f"找不到小节「{title}」对应的 JSON key，拒绝保存")
                    return
                if not self.mw.confirm("保存人设修改",
                                       f"用户 {u['nickname'] or u['user_id']} 的「{title}」将更新（{len(text)} 字）？"):
                    return
                new_persona = copy.deepcopy(persona)
                new_persona[target_key] = text
                # round-trip 校验
                try:
                    payload = json.dumps(new_persona, ensure_ascii=False)
                    check = json.loads(payload)
                    if not isinstance(check, dict) or set(check.keys()) != set(persona.keys()):
                        raise ValueError("round-trip 后结构异常")
                except Exception as ex:
                    self.mw.statusBar().showMessage(f"JSON 校验失败，拒绝保存：{ex}")
                    return

                def _do():
                    import time as _t
                    return api_client.query(self.mw.cfg, "personas",
                                            "UPDATE user_personas SET persona = ?, last_updated_at = ? "
                                            "WHERE user_id = ? AND group_id = ?",
                                            (payload, _t.time(), u["user_id"], u["group_id"]), write=True)
                w = Worker(_do)

                def _ok(*_):
                    self.mw.statusBar().showMessage(f"人设已保存（{title}）")
                    u["persona"] = payload
                    self._load_sections(u)

                w.finished_ok.connect(_ok)
                w.finished_err.connect(lambda ex: self.mw.statusBar().showMessage(f"保存失败: {ex}"))
                w.start()
                self.mw._track(w)
            else:
                # dict/list 字段网格
                self._save_persona_fields()
        elif typ == "画像":
            self._save_profile(u, title, val, s, e, text)

    def _save_profile(self, u, title, val, s, e, text):
        """画像分节保存：按字符偏移只替换该节 body，其余内容逐字节保留（格式无损）。"""
        if not u["profile"]:
            self.mw.statusBar().showMessage("画像原文缺失，拒绝保存")
            return
        if s is None or e is None or not (0 <= s <= e <= len(u["profile"])):
            self.mw.statusBar().showMessage("画像分节偏移异常，拒绝保存")
            return
        orig_body = u["profile"][s:e]
        if text == orig_body.strip():
            self.mw.statusBar().showMessage("无变更")
            return
        # 重组：保留原 body 的前导/尾随空白（分节间 \n\n 等），只换内容
        lead = orig_body[:len(orig_body) - len(orig_body.lstrip())]
        trail = orig_body[len(orig_body.rstrip()):]
        if not u["profile"][:s].endswith("\n") and lead == "":
            # 标题行后无换行的异常格式，补一个换行避免标题与正文粘连
            lead = "\n"
        if not self.mw.confirm("保存画像修改",
                               f"用户 {u['nickname'] or u['user_id']} 的画像「{title}」将更新（{len(text)} 字）？"):
            return
        new_profile = u["profile"][:s] + lead + text + trail + u["profile"][e:]
        # 校验：重组后其余分节标题必须全部保留
        old_titles = [m.group(1) for m in re.finditer(r"(?m)^(【[^】]+】)\s*$", u["profile"])]
        new_titles = [m.group(1) for m in re.finditer(r"(?m)^(【[^】]+】)\s*$", new_profile)]
        if old_titles != new_titles:
            self.mw.statusBar().showMessage(f"分节标题校验失败（{old_titles} → {new_titles}），拒绝保存")
            return

        def _do():
            import time as _t
            return api_client.query(self.mw.cfg, "personas",
                                    "UPDATE user_profiles SET profile = ?, last_updated_at = ? "
                                    "WHERE user_id = ? AND group_id = ?",
                                    (new_profile, _t.time(), u["user_id"], u["group_id"]), write=True)
        w = Worker(_do)

        def _ok(*_):
            self.mw.statusBar().showMessage(f"画像已保存（{title}）")
            u["profile"] = new_profile
            self._load_sections(u)

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda ex: self.mw.statusBar().showMessage(f"保存失败: {ex}"))
        w.start()
        self.mw._track(w)

    def _save_persona_fields(self):
        u = self._cur_user
        if not u or not u["persona"]:
            self.mw.statusBar().showMessage("无已加载的人设小节可保存")
            return
        try:
            persona = json.loads(u["persona"])
            if not isinstance(persona, dict):
                raise ValueError("persona 非 dict")
        except Exception as ex:
            self.mw.statusBar().showMessage(f"当前人设 JSON 无法解析，拒绝保存：{ex}")
            return

        new_val, changed = self._collect_edits()
        if new_val is None:
            self.mw.statusBar().showMessage("当前小节不可编辑")
            return
        if not changed:
            self.mw.statusBar().showMessage("无变更")
            return

        typ, title, _val, _s, _e = self._section_rows[self._cur_section_idx]
        target_key = self._find_persona_key(persona, title)
        if target_key is None:
            self.mw.statusBar().showMessage(f"找不到小节「{title}」对应的 JSON key，拒绝保存")
            return
        if not self.mw.confirm("保存人设修改",
                               f"用户 {u['nickname'] or u['user_id']} 的「{title}」将更新（{len(json.dumps(new_val, ensure_ascii=False))} 字）？"):
            return

        new_persona = copy.deepcopy(persona)
        new_persona[target_key] = new_val
        # 结构安全校验：重建后的 JSON 必须可 round-trip 且顶层仍是 dict
        try:
            payload = json.dumps(new_persona, ensure_ascii=False)
            check = json.loads(payload)
            if not isinstance(check, dict):
                raise ValueError("round-trip 后不是 dict")
            if set(check.keys()) != set(persona.keys()):
                # key 集合变化仅允许小节值内部变化，顶层 key 集合应不变（删的是小节内的 key）
                self.mw.statusBar().showMessage(f"顶层 key 集合异常变化，拒绝保存：{sorted(check.keys())}")
                return
        except Exception as ex:
            self.mw.statusBar().showMessage(f"JSON 校验失败，拒绝保存：{ex}")
            return

        def _do():
            import time as _t
            return api_client.query(self.mw.cfg, "personas",
                                    "UPDATE user_personas SET persona = ?, last_updated_at = ? "
                                    "WHERE user_id = ? AND group_id = ?",
                                    (payload, _t.time(), u["user_id"], u["group_id"]), write=True)
        w = Worker(_do)

        def _ok(*_):
            self.mw.statusBar().showMessage(f"人设已保存（{title}）")
            # 同步内存数据后刷新当前用户视图
            u["persona"] = payload
            self._load_sections(u)

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda ex: self.mw.statusBar().showMessage(f"保存失败: {ex}"))
        w.start()
        self.mw._track(w)

    def _find_persona_key(self, persona: dict, title: str):
        """中文标题 → 顶层 key（PERSONA_KEYS 反查；未收录 key 按原样匹配）"""
        for k, label in PERSONA_KEYS:
            if label == title and k in persona:
                return k
        if title in persona:
            return title
        return None

    def _copy(self):
        if self.stack.currentIndex() == 0:
            # 人设字段模式：拼接导出
            if not self._fields:
                self.mw.statusBar().showMessage("没有可复制的内容")
                return
            lines = []
            for kind, key, f in self._fields:
                lines.append(f"{key}: {f.ed.toPlainText()}")
            text = "\n".join(lines)
        else:
            text = self.txt_content.toPlainText()
        if not text:
            self.mw.statusBar().showMessage("没有可复制的内容")
            return
        QGuiApplication.clipboard().setText(text)
        self.mw.statusBar().showMessage("内容已复制到剪贴板")
