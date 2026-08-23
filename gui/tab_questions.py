"""
tab_questions.py — 🎲 真心话大冒险页签（题库管理，2026-08-21）
==============================================================
三列布局（仿人设画像页）：
  左列 fixedWidth 300：
    - 群列表（fixedHeight 120，首行固定"📦 通用"，其余为有人设题库的群）
    - 用户列表（Expanding 占剩余高度，选群后显示该群有题库的玩家）
  中列 fixedWidth 200：14 项 = 7 色度档位 × (真心话/大冒险)，显示当前选中
    (通用/用户) 各档位题量
  右列 stretch：题目列表（未做在上、做过在下置灰）+ 底部编辑框
    工具条：➕ 添加 / 🗑 删除 / 🤖 LLM 重新生成 / 💾 保存修改

数据链路：
  读/改/删：GUI 直连 SQLite（api_client.query kind='truth_dare'）
  LLM 重新生成：POST /questions/regenerate（bot 内嵌路由，后台线程 + 防重入），
  3s 轮询 GET /questions/regen_status 至完成后刷新。

题库表 auto_question_pool（data/truth_dare.db）：
  通用题 source='generic' + user_id=0 + group_id=0
  人设题 source='persona' + user_id>0 + group_id=群号
  spiciness 0-6（7 档），question_type truth/dare，used 0/1
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QPlainTextEdit, QSizePolicy, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import api_client
from worker import Worker

# 色度档位名（与 games/question_pool.py _SPICE_LEVEL_PROMPTS 权威定义一致）
SPICE_NAMES = {
    0: "纯清水", 1: "轻松日常", 2: "轻度私密", 3: "大胆私密",
    4: "直球私密", 5: "深水私密", 6: "深渊私密",
}
_QTYPES = [("truth", "真心话"), ("dare", "大冒险")]
_GRAY = QColor(160, 166, 173)
_OK = QColor(39, 174, 96)


class _TableNoEdit(QTableWidget):
    """不可编辑表格（行选择）"""

    def __init__(self, rows, cols, *a, **kw):
        super().__init__(rows, cols, *a, **kw)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)


class TabQuestions(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._silent = False
        # 当前选中：source('generic'/'persona') + group_id + user_id
        self._sel = {"source": "generic", "group_id": 0, "user_id": 0}
        self._group_rows: list[dict] = []
        self._user_rows: list[dict] = []
        self._mid_rows: list[dict] = []          # [(spiciness, qtype, count)]
        self._sel_mid: tuple | None = None       # (spiciness, qtype)
        self._qrows: list[dict] = []             # 题目列表行
        self._edits: dict[int, str] = {}         # 未保存的编辑 {id: 新文本}
        self._poll_timer: QTimer | None = None
        self._programmatic = False   # 程序化 setPlainText 时不触发 _on_edit_changed
        self._q_seq = 0              # 题目查询序号（防旧 worker 覆盖新选择）
        self._build()
        self._load_groups()

    # ============================================================
    #  构建 UI
    # ============================================================
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # 顶部功能栏
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)

        # 第一行：标题（左）+ 4 个设置按钮（右上角同行，2026-08-21 布局调整：
        # 不再单独占行；宽度走 sizeHint 自适应，保证按钮文字完整显示）
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.lbl_scope = QLabel("📦 通用题库 · 全群共享")
        self.lbl_scope.setStyleSheet("font-size: 14px; font-weight: bold;")
        row1.addWidget(self.lbl_scope)
        row1.addStretch(1)
        self.btn_cfg_pool = QPushButton("⚙️ 题库规则")
        self.btn_cfg_pool.setToolTip("题库规则：补充阈值/批次数/防重历史/人设截断")
        self.btn_cfg_llm = QPushButton("🤖 LLM 参数")
        self.btn_cfg_llm.setToolTip("LLM 调用参数：3 阶段出题的 max_tokens/温度/thinking/json_mode/超时")
        self.btn_cfg_game = QPushButton("🎮 自动模式")
        self.btn_cfg_game.setToolTip("自动模式行为：大冒险概率/自动踢人/出题延迟/默认色度")
        self.btn_cfg_prompts = QPushButton("📝 出题提示词")
        self.btn_cfg_prompts.setToolTip("出题提示词：32 项全部可编辑，每项可恢复默认")
        for b in (self.btn_cfg_pool, self.btn_cfg_llm, self.btn_cfg_game, self.btn_cfg_prompts):
            b.setMinimumHeight(28)
            # 宽度自适应：style sizeHint 会把 emoji 宽度算窄导致切字，
            # 按实际文字像素宽度 + 充足内边距设最小宽度（2026-08-21 用户反馈文字显示不全）
            _fm = b.fontMetrics()
            b.setMinimumWidth(_fm.horizontalAdvance(b.text()) + 44)
            row1.addWidget(b)
        tl.addLayout(row1)

        self.lbl_hint = QLabel("左列选群/用户 → 中列选色度档位与题型 → 右列管理题目（未做在上，做过置灰在下）")
        self.lbl_hint.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        tl.addWidget(self.lbl_hint)
        self.btn_cfg_pool.clicked.connect(self._open_pool_dialog)
        self.btn_cfg_llm.clicked.connect(self._open_llm_dialog)
        self.btn_cfg_game.clicked.connect(self._open_game_dialog)
        self.btn_cfg_prompts.clicked.connect(self._open_prompts_dialog)

        v.addWidget(top)

        self.splitter = QSplitter(Qt.Horizontal)

        # ---------- 左列：群列表 + 用户列表 ----------
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        lab_g = QLabel("群（通用 = 全群共享题库）")
        lab_g.setStyleSheet("font-size: 12px; color: #7f8c8d;")
        ll.addWidget(lab_g)
        self.tbl_group = _TableNoEdit(0, 2)
        self.tbl_group.setHorizontalHeaderLabels(["群", "题数"])
        self.tbl_group.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_group.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_group.setFixedHeight(120)
        ll.addWidget(self.tbl_group)
        lab_u = QLabel("用户（该群有题库的玩家）")
        lab_u.setStyleSheet("font-size: 12px; color: #7f8c8d;")
        ll.addWidget(lab_u)
        self.tbl_user = _TableNoEdit(0, 2)
        self.tbl_user.setHorizontalHeaderLabels(["昵称", "题数"])
        self.tbl_user.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_user.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_user.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        ll.addWidget(self.tbl_user, 1)
        self.tbl_group.cellClicked.connect(self._on_group_select)
        self.tbl_user.cellClicked.connect(self._on_user_select)
        self.splitter.addWidget(left)

        # ---------- 中列：7 色度 × 2 题型 = 14 项 ----------
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        lab_m = QLabel("色度档位 × 题型（题数）")
        lab_m.setStyleSheet("font-size: 12px; color: #7f8c8d;")
        ml.addWidget(lab_m)
        self.tbl_mid = _TableNoEdit(14, 2)
        self.tbl_mid.setHorizontalHeaderLabels(["档位 · 题型", "题数"])
        self.tbl_mid.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_mid.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_mid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        ml.addWidget(self.tbl_mid, 1)
        self.tbl_mid.cellClicked.connect(self._on_mid_select)
        self.splitter.addWidget(mid)

        # ---------- 右列：题目列表 + 编辑 ----------
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        # 工具条
        bar = QWidget()
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        self.lbl_qtitle = QLabel("—")
        self.lbl_qtitle.setStyleSheet("font-size: 13px; font-weight: bold;")
        bl.addWidget(self.lbl_qtitle)
        btnrow = self._btn_row()
        bl.addLayout(btnrow)
        rl.addWidget(bar)
        # 题目列表
        self.tbl_q = _TableNoEdit(0, 2)
        self.tbl_q.setHorizontalHeaderLabels(["题目", "状态"])
        self.tbl_q.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_q.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_q.verticalHeader().setVisible(False)
        self.tbl_q.itemSelectionChanged.connect(self._on_q_select)
        rl.addWidget(self.tbl_q, 1)
        # 编辑框
        self.ed_q = QPlainTextEdit()
        self.ed_q.setFixedHeight(92)
        self.ed_q.setPlaceholderText("选中一道题可在此编辑（仅单选生效）；修改后点「💾 保存修改」落盘")
        self.ed_q.setLineWrapMode(QPlainTextEdit.NoWrap)
        rl.addWidget(self.ed_q)
        self.ed_q.textChanged.connect(self._on_edit_changed)
        self.splitter.addWidget(right)

        self.splitter.setSizes([300, 200, 700])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        v.addWidget(self.splitter, 1)

    def _btn_row(self):
        h = QHBoxLayout()
        h.setSpacing(6)
        self.btn_add = QPushButton("➕ 添加")
        self.btn_del = QPushButton("🗑 删除")
        self.btn_regen = QPushButton("🤖 LLM 重新生成")
        self.btn_save = QPushButton("💾 保存修改")
        for b in (self.btn_add, self.btn_del, self.btn_regen, self.btn_save):
            b.setFixedHeight(30)
            h.addWidget(b)
        self.btn_save.setEnabled(False)
        h.addStretch(1)
        self.btn_regen.clicked.connect(self._on_regen)
        self.btn_save.clicked.connect(self._on_save_edits)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_del.clicked.connect(self._on_delete)
        return h

    # ============================================================
    #  左列：群 / 用户
    # ============================================================
    def _where(self, extra_params: tuple = ()) -> tuple[str, tuple]:
        """当前选中的 SQL WHERE 条件（source 维度）。"""
        if self._sel["source"] == "generic":
            return "source='generic'", ()
        return ("source='persona' AND group_id=? AND user_id=?",
                (self._sel["group_id"], self._sel["user_id"]))

    # ------------------------------------------------------------
    #  设置弹窗（题库规则 / LLM 参数 / 自动模式 / 出题提示词，2026-08-21 新增）
    #  延迟 import 弹窗模块：加快 GUI 启动 + 避免启动期依赖
    # ------------------------------------------------------------
    def _open_pool_dialog(self):
        from truth_dare_settings_dialogs import PoolRulesDialog
        d = PoolRulesDialog(self.mw)
        d.exec()

    def _open_llm_dialog(self):
        from truth_dare_settings_dialogs import LLMParamsDialog
        d = LLMParamsDialog(self.mw)
        d.exec()

    def _open_game_dialog(self):
        from truth_dare_settings_dialogs import GameRulesDialog
        d = GameRulesDialog(self.mw)
        d.exec()

    def _open_prompts_dialog(self):
        from truth_dare_settings_dialogs import PromptsDialog
        d = PromptsDialog(self.mw)
        d.exec()

    def _scope_title(self) -> str:
        if self._sel["source"] == "generic":
            return "📦 通用题库 · 全群共享"
        nick = next((u["nickname"] for u in self._user_rows
                     if u["user_id"] == self._sel["user_id"]),
                    str(self._sel["user_id"]))
        return f"👤 {nick}（群 {self._sel['group_id']}）"

    def _load_groups(self):
        def _do():
            rows = api_client.query(
                self.mw.cfg, "truth_dare",
                "SELECT group_id, COUNT(*) AS n FROM auto_question_pool "
                "WHERE source='persona' GROUP BY group_id ORDER BY group_id")
            gen = api_client.query(
                self.mw.cfg, "truth_dare",
                "SELECT COUNT(*) AS n FROM auto_question_pool WHERE source='generic'")
            return rows, (gen[0]["n"] if gen else 0)

        def _ok(res):
            rows, gen_n = res
            self._group_rows = [{"source": "generic", "group_id": 0, "user_id": 0, "n": gen_n}]
            self._group_rows += [{"source": "persona", "group_id": r["group_id"],
                                  "user_id": 0, "n": r["n"]} for r in rows]
            self._fill_group_table()
            self.mw.statusBar().showMessage(f"题库群：{len(rows)} 个")
            self._select_group_row(0)  # 默认选通用

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"题库群加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_group_table(self):
        self._silent = True
        self.tbl_group.setRowCount(len(self._group_rows))
        for i, r in enumerate(self._group_rows):
            if r["source"] == "generic":
                item = QTableWidgetItem("📦 通用（全群）")
            else:
                item = QTableWidgetItem(str(r["group_id"]))
            item.setData(Qt.UserRole, i)
            self.tbl_group.setItem(i, 0, item)
            self.tbl_group.setItem(i, 1, QTableWidgetItem(str(r["n"])))
        self._silent = False

    def _select_group_row(self, group_id: int):
        for i, g in enumerate(self._group_rows):
            if g["group_id"] == group_id:
                self._silent = True
                self.tbl_group.selectRow(i)
                self._silent = False
                self._on_group_select()
                return
        self.mw.statusBar().showMessage("未找到该群")

    def _on_group_select(self):
        if self._silent:
            return
        row = self.tbl_group.currentRow()
        if row < 0 or row >= len(self._group_rows):
            return
        g = self._group_rows[row]
        self._sel = {"source": g["source"], "group_id": g["group_id"], "user_id": 0}
        self._edits = {}
        self.btn_save.setEnabled(False)
        self.lbl_scope.setText(self._scope_title())
        self._load_users()

    def _load_users(self):
        """按当前群加载用户列表（通用时清空）。"""
        self._user_rows = []
        if self._sel["source"] == "generic":
            self._fill_user_table()
            self._load_mid()
            return

        gid = self._sel["group_id"]

        def _do():
            pool = api_client.query(
                self.mw.cfg, "truth_dare",
                "SELECT user_id, COUNT(*) AS n FROM auto_question_pool "
                "WHERE source='persona' AND group_id=? GROUP BY user_id", (gid,))
            uids = [r["user_id"] for r in pool]
            nicks = {}
            if uids:
                marks = ",".join("?" * len(uids))
                for r in api_client.query(
                        self.mw.cfg, "personas",
                        f"SELECT user_id, nickname FROM user_personas "
                        f"WHERE user_id IN ({marks})", tuple(uids)):
                    nicks[r["user_id"]] = r["nickname"] or ""
            out = [{"user_id": r["user_id"], "n": r["n"],
                    "nickname": nicks.get(r["user_id"], f"用户{r['user_id']}")}
                   for r in pool]
            out.sort(key=lambda u: u["user_id"])
            return out

        def _ok(rows):
            self._user_rows = rows
            self._fill_user_table()
            if rows:
                # 默认选第一个用户
                self._silent = True
                self.tbl_user.selectRow(0)
                self._silent = False
                self._on_user_select()
            else:
                self._load_mid()
            self.mw.statusBar().showMessage(f"该群 {len(rows)} 个玩家有题库")

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"用户加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_user_table(self):
        self._silent = True
        self.tbl_user.setRowCount(len(self._user_rows))
        for i, r in enumerate(self._user_rows):
            self.tbl_user.setItem(i, 0, QTableWidgetItem(r["nickname"]))
            self.tbl_user.setItem(i, 1, QTableWidgetItem(str(r["n"])))
        self._silent = False

    def _on_user_select(self):
        if self._silent:
            return
        row = self.tbl_user.currentRow()
        if row < 0 or row >= len(self._user_rows):
            return
        u = self._user_rows[row]
        self._sel = {"source": "persona", "group_id": self._sel["group_id"],
                     "user_id": u["user_id"]}
        self._edits = {}
        self.btn_save.setEnabled(False)
        self.lbl_scope.setText(self._scope_title())
        self._load_mid()

    # ============================================================
    #  中列：14 项（7 色度 × 2 题型）
    # ============================================================
    def _load_mid(self):
        w = Worker(self._do_query_mid)

        def _ok(rows):
            self._mid_rows = rows
            self._fill_mid_table()
            if self._sel_mid:
                # 保持原选中档位
                sp, qt = self._sel_mid
                for i, r in enumerate(self._mid_rows):
                    if r["spiciness"] == sp and r["question_type"] == qt:
                        self._silent = True
                        self.tbl_mid.selectRow(i)
                        self._silent = False
                        self._on_mid_select()
                        return
            # 默认选中 4 档真心话
            for i, r in enumerate(self._mid_rows):
                if r["spiciness"] == 4 and r["question_type"] == "truth":
                    self._silent = True
                    self.tbl_mid.selectRow(i)
                    self._silent = False
                    self._on_mid_select()
                    return
            if self._mid_rows:
                self._silent = True
                self.tbl_mid.selectRow(0)
                self._silent = False
                self._on_mid_select()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"档位加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _do_query_mid(self) -> list[dict]:
        where, params = self._where()
        rows = api_client.query(
            self.mw.cfg, "truth_dare",
            f"SELECT question_type, spiciness, COUNT(*) AS n FROM auto_question_pool "
            f"WHERE {where} GROUP BY question_type, spiciness", params)
        cnt = {(r["question_type"], r["spiciness"]): r["n"] for r in rows}
        out = []
        for sp in range(7):
            for qt, _label in _QTYPES:
                out.append({"spiciness": sp, "question_type": qt,
                            "n": cnt.get((qt, sp), 0)})
        return out

    def _fill_mid_table(self):
        self._silent = True
        self.tbl_mid.setRowCount(len(self._mid_rows))
        for i, r in enumerate(self._mid_rows):
            label = f"{r['spiciness']} · {SPICE_NAMES[r['spiciness']]}"
            item = QTableWidgetItem(f"{label} — {'真心话' if r['question_type'] == 'truth' else '大冒险'}")
            item.setData(Qt.UserRole, i)
            self.tbl_mid.setItem(i, 0, item)
            n_item = QTableWidgetItem(str(r["n"]))
            n_item.setForeground(_OK if r["n"] > 0 else _GRAY)
            self.tbl_mid.setItem(i, 1, n_item)
        self._silent = False

    def _on_mid_select(self):
        if self._silent:
            return
        row = self.tbl_mid.currentRow()
        if row < 0 or row >= len(self._mid_rows):
            return
        r = self._mid_rows[row]
        self._sel_mid = (r["spiciness"], r["question_type"])
        self._edits = {}
        self.btn_save.setEnabled(False)
        self._load_questions()

    # ============================================================
    #  右列：题目列表 + 编辑
    # ============================================================
    def _load_questions(self):
        self._q_seq += 1
        seq = self._q_seq
        w = Worker(self._do_query_questions)

        def _ok(rows):
            if seq != self._q_seq:   # 期间用户切了档位/用户 → 丢弃旧结果
                return
            self._qrows = rows
            self._fill_q_table()
            sp, qt = self._sel_mid
            total = sum(1 for x in rows)
            self.lbl_qtitle.setText(
                f"{self._scope_title().split('（')[0]} · "
                f"{sp}档{SPICE_NAMES[sp]} · {'真心话' if qt == 'truth' else '大冒险'}"
                f"（{total} 道）")

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"题目加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _do_query_questions(self) -> list[dict]:
        sp, qt = self._sel_mid
        where, params = self._where()
        rows = api_client.query(
            self.mw.cfg, "truth_dare",
            f"SELECT id, question_text, used, used_at, created_at FROM auto_question_pool "
            f"WHERE {where} AND question_type=? AND spiciness=? "
            f"ORDER BY used ASC, created_at DESC, id DESC",
            params + (qt, sp))
        return [dict(r) for r in rows]

    def _fill_q_table(self):
        self._silent = True
        self.tbl_q.setRowCount(len(self._qrows))
        for i, r in enumerate(self._qrows):
            it = QTableWidgetItem(r["question_text"])
            it.setData(Qt.UserRole, i)
            it.setFlags(it.flags() | Qt.ItemIsSelectable)
            self.tbl_q.setItem(i, 0, it)
            st = QTableWidgetItem("⏳ 做过" if r["used"] else "✅ 未做")
            if r["used"]:
                st.setForeground(_GRAY)
                it.setForeground(_GRAY)
                if r.get("used_at"):
                    st.setToolTip(f"用过：{r['used_at']}")
            else:
                st.setForeground(_OK)
            self.tbl_q.setItem(i, 1, st)
        self._silent = False
        self._on_q_select()

    def _selected_q_ids(self) -> list[int]:
        ids = []
        for idx in self.tbl_q.selectionModel().selectedRows():
            i = idx.row()
            if 0 <= i < len(self._qrows):
                ids.append(self._qrows[i]["id"])
        return ids

    def _on_q_select(self):
        """单选 → 编辑框填入原题；多选/无选 → 清空编辑框。"""
        ids = self._selected_q_ids()
        if len(ids) == 1:
            row = next((r for r in self._qrows if r["id"] == ids[0]), None)
            if row:
                new_text = self._edits.get(row["id"])
                text = new_text if new_text is not None else row["question_text"]
                self._programmatic = True
                self.ed_q.setPlainText(text)
                self._programmatic = False
                if new_text is not None and new_text != row["question_text"]:
                    self.lbl_qtitle.setText(self.lbl_qtitle.text() + "  ✏️ 已改")
        else:
            self._programmatic = True
            self.ed_q.setPlainText("")
            self._programmatic = False

    def _on_edit_changed(self):
        if self._programmatic:
            return
        ids = self._selected_q_ids()
        if len(ids) != 1:
            return
        row = next((r for r in self._qrows if r["id"] == ids[0]), None)
        if not row:
            return
        text = self.ed_q.toPlainText().strip()
        if text == row["question_text"]:
            self._edits.pop(row["id"], None)
        else:
            self._edits[row["id"]] = text
        self.btn_save.setEnabled(bool(self._edits))

    # ============================================================
    #  操作：保存 / 添加 / 删除 / LLM 重新生成
    # ============================================================
    def _on_save_edits(self):
        if not self._edits:
            return
        edits = self._edits
        self._edits = {}
        self.btn_save.setEnabled(False)

        def _do():
            n = 0
            for qid, text in edits.items():
                api_client.query(
                    self.mw.cfg, "truth_dare",
                    "UPDATE auto_question_pool SET question_text=? WHERE id=?",
                    (text, qid), write=True)
                n += 1
            return n

        def _ok(n):
            self.mw.statusBar().showMessage(f"✅ 已保存 {n} 道修改", 3000)
            self._load_mid()
            self._load_questions()

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"保存失败: {e}"))
        w.start()
        self.mw._track(w)

    def _on_add(self):
        if not self._sel_mid:
            self.mw.statusBar().showMessage("请先在中列选择档位与题型")
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "添加题目",
            "每行一道题（可一次粘贴多道）：\n", "")
        if not ok:
            return
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return
        sp, qt = self._sel_mid

        def _do():
            where, params = self._where()
            if self._sel["source"] == "generic":
                uid, gid, source = 0, 0, "generic"
            else:
                uid, gid, source = self._sel["user_id"], self._sel["group_id"], "persona"
            n = 0
            for q in lines:
                # 查重（与 bot 入库同口径）
                exists = api_client.query(
                    self.mw.cfg, "truth_dare",
                    "SELECT id FROM auto_question_pool WHERE question_text=? AND question_type=? "
                    "AND spiciness=? AND source=? AND group_id=?",
                    (q, qt, sp, source, gid))
                if exists:
                    continue
                api_client.query(
                    self.mw.cfg, "truth_dare",
                    "INSERT INTO auto_question_pool "
                    "(user_id, group_id, question_text, question_type, spiciness, source) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (uid, gid, q, qt, sp, source), write=True)
                n += 1
            return n

        def _ok(n):
            self.mw.statusBar().showMessage(f"✅ 新增 {n} 道（重复已跳过 {len(lines) - n} 道）", 4000)
            self._load_mid()
            self._load_questions()

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"添加失败: {e}"))
        w.start()
        self.mw._track(w)

    def _on_delete(self):
        ids = self._selected_q_ids()
        if not ids:
            self.mw.statusBar().showMessage("先在右列选中要删的题目（可多选）")
            return
        if not self.mw.confirm(
                "删除题目",
                f"确定删除选中的 {len(ids)} 道题目？\n（删除后不可恢复，bot 出题时也不会再取到）",
                "删除"):
            return
        marks = ",".join("?" * len(ids))

        def _do():
            api_client.query(
                self.mw.cfg, "truth_dare",
                f"DELETE FROM auto_question_pool WHERE id IN ({marks})", tuple(ids),
                write=True)
            return len(ids)

        def _ok(n):
            self._edits = {}
            self.btn_save.setEnabled(False)
            self.mw.statusBar().showMessage(f"🗑 已删除 {n} 道", 4000)
            self._load_mid()
            self._load_questions()

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"删除失败: {e}"))
        w.start()
        self.mw._track(w)

    def _on_regen(self):
        if not self._sel_mid:
            self.mw.statusBar().showMessage("请先在中列选择档位与题型")
            return
        sp, qt = self._sel_mid
        payload = {
            "source": self._sel["source"],
            "group_id": self._sel["group_id"],
            "user_id": self._sel["user_id"],
            "question_type": qt,
            "spiciness": sp,
        }

        def _do():
            return api_client._request(self.mw.cfg, "POST",
                                       "/questions/regenerate", payload, timeout=15)

        def _ok(r):
            if not r.get("ok"):
                self.mw.statusBar().showMessage(f"启动生成失败: {r.get('error', '')}", 6000)
                return
            self.btn_regen.setEnabled(False)
            self.mw.statusBar().showMessage(
                f"🤖 LLM 生成中：{sp}档 · {'真心话' if qt == 'truth' else '大冒险'}"
                f"（分钟级，完成后自动刷新）", 0)
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_regen_status)
            self._poll_timer.start(3000)

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"生成请求失败: {e}"))
        w.start()
        self.mw._track(w)

    def _poll_regen_status(self):
        if not self._poll_timer:
            return
        sp, qt = self._sel_mid or (4, "truth")

        def _do():
            return api_client._request(self.mw.cfg, "GET", "/questions/regen_status",
                                       None, timeout=10)

        def _ok(r):
            if not self._poll_timer:
                return
            mine = None
            for t in r.get("tasks", []):
                if (t.get("source") == self._sel["source"]
                        and t.get("group_id") == self._sel["group_id"]
                        and t.get("user_id") == self._sel["user_id"]
                        and t.get("question_type") == qt
                        and t.get("spiciness") == sp):
                    mine = t
                    break
            if mine and mine.get("status") in ("done", "error"):
                self._poll_timer.stop()
                self._poll_timer.deleteLater()
                self._poll_timer = None
                self.btn_regen.setEnabled(True)
                if mine["status"] == "done":
                    self.mw.statusBar().showMessage(
                        f"✅ LLM 生成完成 +{mine.get('added', 0)} 道，已刷新", 5000)
                else:
                    self.mw.statusBar().showMessage(
                        f"❌ LLM 生成失败: {mine.get('error', '')[:120]}", 8000)
                self._load_mid()
                self._load_questions()

        def _err(e):
            # 轮询失败（bot 未运行等）不停轮询，等下次
            pass

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"题目加载失败: {e}"))
        w.start()
        self.mw._track(w)
