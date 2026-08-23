"""
tab_games.py — 游戏管理页（2026-08-21 重写）
=================================================
管理四款群游戏的题库与答题记录（用户拍板：原「命令速查/谐音梗/敏感词/
cosplay 资源状态/模仿黑名单」内容全部移除——黑名单与群组集群页重复，
不单独出现）：

  🎯 真心话大冒险  题库=truth/dare_questions.txt（一行一题）
                   记录=truth_dare.db.user_question_history（做过题）
  🕵️  谁是卧底     题库=spy_wordbank.txt（平民词|卧底词[|白板]）
                   记录=spy_history.db（spy_stats 战绩 + used_words 已用词）
  🥣 海龟汤        题库=turtle_soup.json（结构化：id/标题/汤面/汤底/hints）
                   记录=turtle_soup_history.db（used_soups + player_stats）

布局（stretch 填满视口，无外层滚动条）：
  左列（固定 240px）：游戏列表（名称 + 题库条数 / 记录条数）
  右列（拉伸，上下两卡）：
    📚 题库管理（标题行右侧工具按钮：增/删/排序/保存/热重载 + 搜索框）
       行式 txt 游戏=行内编辑表格；海龟汤=表格 + 底部三字段编辑器
    📊 答题记录（标题行右侧：删除选中 / 清空全部）

写操作安全：
  - 题库落盘前 .bak 时间戳备份（data/question_backup/）
  - 记录删除/清空均 confirm 二次确认
  - 行式 txt 保存保留文件头部注释块；文件中间的分节注释行保存后合并
  - 所有 DB/文件 IO 走 Worker 线程，不阻塞 UI
热重载：题库保存后点「♻️ 通知 bot 重载」→ POST /reload
  truth_dare / spy / turtle_soup（2026-08-21 control_api 新增三项，
  bot 未运行时文件照样落盘、启动后生效）。
"""

import json
import os
import time
import shutil

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QFrame, QGroupBox, QLineEdit,
    QPushButton, QPlainTextEdit, QTabWidget, QSizePolicy,
)

import api_client
from worker import Worker

# ------------------------------------------------------------
#  游戏定义
# ------------------------------------------------------------
GAMES = [
    # (game_id, emoji, 名称, 题库文件(相对 data_dir), 记录描述)
    ("truth_dare", "🎯", "真心话大冒险",
     ["question_bank/truth_questions.txt", "question_bank/dare_questions.txt"],
     "user_question_history"),
    ("spy", "🕵️", "谁是卧底",
     ["question_bank/spy_wordbank.txt"], "spy_history"),
    ("turtle_soup", "🥣", "海龟汤",
     ["question_bank/turtle_soup.json"], "turtle_soup_history"),
]

RELOAD_WHAT = {"truth_dare": "truth_dare", "spy": "spy",
               "turtle_soup": "turtle_soup"}


def _compact_tabs(tabs: QTabWidget):
    """小标签样式（08-21 用户要求统一：以海龟汤答题记录「使用记录/玩家统计」为基准）。

    widget 级作用域 QSS 覆盖全局 16px 粗体页签 → 12px 常规 + 紧凑 padding，
    不碰 theme.py（主窗口其他页签不受影响）。
    """
    tabs.setObjectName("games_compact_tabs")
    tabs.setStyleSheet(
        "QTabBar::tab { padding: 4px 10px 5px 10px; margin-right: 3px; "
        "font-size: 12px; font-weight: normal; }")


class _SoupEditBox(QGroupBox):
    """海龟汤编辑器分组框（08-21 修复过度压缩）。

    两全：
    - sizeHint 恒等于 minimumSizeHint → 不会像普通 QGroupBox 那样按子控件
      sizeHint 算出 600+px 把页面撑出外层滚动条；
    - 垂直策略 Expanding → 卡片有多余空间时能跟汤表一起分（普通 Ignored 策略
      永远只拿最小高度，3 个编辑器各卡 48px，用户实际无法操作）。
    """

    def sizeHint(self):
        return self.minimumSizeHint()

# 左列「说明」卡文案（08-21 排版优化：列表卡按内容收缩后，剩余空间放说明）
INFO_TEXT = {
    "truth_dare": (
        "题库：完整模式（骰子+排名）与简化模式共用同一固定题库"
        "（truth_questions / dare_questions 两个文件，自选模式不抽题、"
        "自动模式由 LLM 出题）。\n\n"
        "记录：user_question_history 存每个用户做过的题，"
        "抽题时自动避开做过的（/做过 查看、/清空做过 重置）。\n\n"
        "改题流程：行内编辑 → 💾 保存（自动 .bak 备份）→ "
        "♻️ 通知 bot 重载（bot 未运行时启动后生效）。"
    ),
    "spy": (
        "题库：词对格式 平民词|卧底词|白板词，一行一对，"
        "# 开头为注释。白板词可选（留空=无白板）。\n\n"
        "记录：spy_stats 存每人战绩（胜场/总场），"
        "used_words 存用过的词对（避免重复抽到）。\n\n"
        "清空战绩=重置胜负统计；删除已用词对=该词对可再被抽到。"
    ),
    "turtle_soup": (
        "题库：turtle_soup.json 结构化存储（标题/汤面/汤底/提示 hints/"
        "关键事实 key_facts），key_facts 只读（展示于汤底 tooltip）。\n\n"
        "记录：used_soups 存每局使用的汤与是否猜中，"
        "player_stats 存每人累计猜中数。\n\n"
        "编辑器每行一条提示；新汤 id 自动递增。"
    ),
}

_BAK_DIR = os.path.join("question_backup")


def _ts(v) -> str:
    """时间显示统一（str 直接透传；REAL 时间戳转本地时间）。"""
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(float(v)))
        except Exception:
            return str(v)
    s = str(v)
    return s[5:16] if len(s) >= 16 and s[4] == "-" else s


class _TableNoEdit(QTableWidget):
    def __init__(self, rows=0, cols=1, *a, **kw):
        super().__init__(rows, cols, *a, **kw)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        # 08-21 关键：QTableWidget 的 sizeHint 按全部行数算（133 行 ≈ 3500px），
        # 默认 Preferred 会把卡片撑高 → 页面出现外层滚动条。
        # (Ignored, Expanding) = 忽略 sizeHint 参与分配、吃卡片分到的空间
        # （内部滚动），与 tab_personas 既有惯例一致。
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)


class _TableEditable(QTableWidget):
    """行内编辑表格（双击编辑），只读列通过 setCellWidget 占位实现。"""

    def __init__(self, rows=0, cols=1, *a, **kw):
        super().__init__(rows, cols, *a, **kw)
        self.setEditTriggers(QTableWidget.DoubleClicked)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(26)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)


class _Card(QFrame):
    """白卡片：标题 16px bold + 标题行右侧工具按钮 + 内容区。"""

    def __init__(self, title: str, parent=None, actions=None):
        super().__init__(parent)
        self.setObjectName("game_mng_card")
        # 与 AI 聊天页 ai_chat_card 同款外观（08-21 补：漏设 QSS 卡片无白底/边框）
        self.setStyleSheet(
            "#game_mng_card { background: #ffffff; border: 1px solid #d0d7de;"
            " border-radius: 8px; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)
        if actions:
            trow = QHBoxLayout()
            trow.setSpacing(6)
            t = QLabel(title)
            f = QFont()
            f.setPointSize(int(f.pointSize() * 1.25))
            f.setBold(True)
            t.setFont(f)
            trow.addWidget(t)
            trow.addStretch(1)
            for b in actions:
                b.setMinimumHeight(26)
                trow.addWidget(b)
            v.addLayout(trow)
        else:
            t = QLabel(title)
            f = QFont()
            f.setPointSize(int(f.pointSize() * 1.25))
            f.setBold(True)
            t.setFont(f)
            v.addWidget(t)
        self.body = v
        # 08-21 关键：QFrame 的 sizeHint 按子控件 sizeHint 累加（soup 卡 547 +
        # 记录卡 299 = 846 > 800 视口）→ 外层 QScrollArea 出现纵向滚动条。
        # 覆盖 sizeHint 恒等于 minimumSizeHint：外层滚动区只看最小值（<800 无滚动条），
        # 卡片实际高度由 right_v 的 stretch 因子（2/3）拉伸到视口，内部表格/编辑器
        # （Expanding）随之拿到大空间。与 _SoupEditBox 同思路，在卡片层一次性解决。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def sizeHint(self):
        return self.minimumSizeHint()

    def add(self, w, stretch=0):
        self.body.addWidget(w, stretch)


# ============================================================
#  题库文件 IO（GUI 侧直读直写，.bak 备份）
# ============================================================
def _read_line_bank(path: str) -> tuple[list[str], list[str]]:
    """行式题库 → (数据行, 头部注释行)。头部注释 = 文件开头到第一行数据前。"""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    header, data = [], []
    started = False
    for ln in lines:
        s = ln.strip()
        if not started:
            if not s or s.startswith("#"):
                header.append(ln)
                continue
            started = True
        if s and not s.startswith("#"):
            data.append(s)
    return data, header


def _write_line_bank(path: str, data: list[str], header: list[str]) -> str:
    """行式题库落盘（保留头部注释 + 数据行）。返回备份路径。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = _backup_file(path)
    body = "\n".join(h for h in header if h.strip())
    if body:
        body += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + "\n".join(data) + "\n")
    return bak


def _backup_file(path: str) -> str:
    """时间戳备份到 data/question_backup/，返回备份路径（失败不抛）。"""
    try:
        bak_dir = os.path.join(os.path.dirname(path), os.pardir, _BAK_DIR)
        os.makedirs(bak_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(bak_dir, f"{os.path.basename(path)}.{ts}.bak")
        shutil.copy2(path, dst)
        return dst
    except Exception:
        return ""


def _read_soup_bank(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_soup_bank(path: str, soups: list[dict]) -> str:
    bak = _backup_file(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(soups, f, ensure_ascii=False, indent=2)
    return bak


class _GameListWidget(QWidget):
    """左列游戏列表（08-21 用户要求：单行小字太小、留白太多 → 双行大字）。

    每个游戏一个 _GameRow：14px 粗体游戏名 + 11px 灰色统计，鼠标点选、
    选中/悬停高亮。
    ⚠️ 不能用 QListWidgetItem + setText(HTML)——item 不渲染富文本，
    标签会原样显示成字符串（08-21 实测踩坑）。
    """

    class _GameRow(QWidget):
        def __init__(self, name: str, on_click, parent=None):
            super().__init__(parent)
            self._on_click = on_click
            v = QVBoxLayout(self)
            v.setContentsMargins(8, 7, 8, 7)
            v.setSpacing(2)
            self.name_lbl = QLabel(name)
            fn = QFont()
            fn.setPointSize(max(int(fn.pointSize() * 1.15), 11))
            fn.setBold(True)
            self.name_lbl.setFont(fn)
            self.stat_lbl = QLabel()
            self.stat_lbl.setStyleSheet("color: #6a737d; font-size: 11px;")
            v.addWidget(self.name_lbl)
            v.addWidget(self.stat_lbl)
            self._selected = False
            self._restyle()

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

    def add_game(self, name: str):
        idx = len(self._rows)
        r = self._GameRow(name, lambda i=idx: self.clicked.emit(i), self)
        self._layout.insertWidget(self._layout.count() - 1, r)
        self._rows.append(r)

    def set_stat(self, i: int, text: str):
        if 0 <= i < len(self._rows):
            self._rows[i].stat_lbl.setText(text)

    def count(self):
        return len(self._rows)

    def select(self, i: int):
        self._selected_idx = i
        for j, r in enumerate(self._rows):
            r.set_selected(j == i)

    def selected(self):
        return self._selected_idx

    def setCurrentRow(self, i: int):
        """与 QListWidget 同名的编程式选中（触发选中态，不触发 clicked 信号）。"""
        if 0 <= i < len(self._rows):
            self.select(i)

    def name_text(self, i: int) -> str:
        return self._rows[i].name_lbl.text()

    def stat_text(self, i: int) -> str:
        return self._rows[i].stat_lbl.text()


class TabGames(QWidget):
    """游戏管理页：左列游戏列表 + 右列题库/答题记录双卡。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._game = None            # 当前选中 game_id
        self._summary = {}           # game_id -> (bank_n, record_n)
        self._soup = []              # 海龟汤 json 全量（选中时加载）
        self._soup_sel = -1          # 海龟汤选中行（json 下标）
        self._build()
        self._load_summary()
        # 08-22 功能巡检修复：默认选中第一个游戏（与 AI 聊天页默认选中首行一致）——
        # 原行为进页面只有「← 选择左侧游戏」空态且左侧无高亮，UX 不一致
        if GAMES:
            self._on_game_click(0)

    # ------------------------------------------------------------
    #  构建
    # ------------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        # ---------------- 左列：游戏列表（内容自适应高）+ 说明卡（填满剩余） ----------------
        # 08-21 用户反馈：旧版单行小字 + 卡片全高留白太多 → 双行大字行（_GameListWidget，
        # 14px 粗名 + 11px 统计），列表卡按内容收缩，下方「说明」卡 stretch 填满并
        # 随选中游戏更新。
        self.card_game = _Card("🎮 游戏列表")
        self.lst_game = _GameListWidget()
        for gid, emoji, name, _files, _rec in GAMES:
            self.lst_game.add_game(f"{emoji} {name}")
        self.lst_game.clicked.connect(self._on_game_click)
        self.card_game.add(self.lst_game, 0)
        self.lst_game.setMinimumWidth(200)

        self.card_info = _Card("ℹ️ 说明")
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_info.setStyleSheet("color: #4a5568; font-size: 12px; line-height: 1.6;")
        self.card_info.add(self.lbl_info, 1)

        left_wrap = QWidget()
        lv = QVBoxLayout(left_wrap)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        lv.addWidget(self.card_game, 0)
        lv.addWidget(self.card_info, 1)
        left_wrap.setFixedWidth(240)
        row.addWidget(left_wrap)

        # ---------------- 右列：动态容器 ----------------
        right = QWidget()
        self.right_v = QVBoxLayout(right)
        self.right_v.setContentsMargins(0, 0, 0, 0)
        self.right_v.setSpacing(8)
        self.lbl_right_hint = QLabel("← 选择左侧游戏")
        self.lbl_right_hint.setAlignment(Qt.AlignCenter)
        self.lbl_right_hint.setStyleSheet("color: #656d76; font-size: 15px;")
        self.right_v.addWidget(self.lbl_right_hint, 1)
        self.lbl_info.setText("点击左侧游戏查看题库与答题记录管理。")
        row.addWidget(right, 1)

        v.addLayout(row, 1)

    # ------------------------------------------------------------
    #  左列：游戏概览
    # ------------------------------------------------------------
    def _data_dir(self) -> str:
        return os.path.dirname(self.mw.cfg.get("DB_PATH", "data/chat_history.db"))

    def _bank_paths(self, game_id: str) -> list[str]:
        for g in GAMES:
            if g[0] == game_id:
                return [os.path.join(self._data_dir(), p) for p in g[3]]
        return []

    def _load_summary(self):
        def _scan():
            out = {}
            dd = self._data_dir()
            # 题库条数（文件）
            try:
                n_truth = len(_read_line_bank(os.path.join(dd, "question_bank", "truth_questions.txt"))[0])
                n_dare = len(_read_line_bank(os.path.join(dd, "question_bank", "dare_questions.txt"))[0])
                n_spy = len(_read_line_bank(os.path.join(dd, "question_bank", "spy_wordbank.txt"))[0])
                soup_n = len(_read_soup_bank(os.path.join(dd, "question_bank", "turtle_soup.json")))
            except Exception:
                n_truth = n_dare = n_spy = soup_n = -1
            out["truth_dare"] = n_truth + n_dare if (n_truth >= 0 and n_dare >= 0) else -1
            out["spy"] = n_spy
            out["turtle_soup"] = soup_n
            # 记录条数（DB）
            try:
                r = api_client.query(self.mw.cfg, "truth_dare", "SELECT COUNT(*) n FROM user_question_history")
                out["rec_truth_dare"] = r[0]["n"] if r else 0
            except Exception:
                out["rec_truth_dare"] = -1
            try:
                r = api_client.query(self.mw.cfg, "spy", "SELECT COUNT(*) n FROM spy_stats")
                out["rec_spy"] = r[0]["n"] if r else 0
            except Exception:
                out["rec_spy"] = -1
            try:
                r = api_client.query(self.mw.cfg, "turtle_soup", "SELECT COUNT(*) n FROM used_soups")
                out["rec_turtle_soup"] = r[0]["n"] if r else 0
            except Exception:
                out["rec_turtle_soup"] = -1
            return out

        w = Worker(_scan)

        def _ok(res):
            self._summary = res
            self._refresh_list()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"概览加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _refresh_list(self):
        for i, (gid, emoji, name, _files, _rec) in enumerate(GAMES):
            n = self._summary.get(gid, -1)
            rn = self._summary.get(f"rec_{gid}", -1)
            self.lst_game.set_stat(i, f"题库 {n if n >= 0 else '?'} · 记录 {rn if rn >= 0 else '?'}")

    def _on_game_click(self, idx: int):
        self.lst_game.select(idx)
        gid = GAMES[idx][0]
        self.lbl_info.setText(INFO_TEXT.get(gid, ""))
        if gid == self._game:
            return
        self._game = gid
        self._build_right()

    # ------------------------------------------------------------
    #  右列：构建
    # ------------------------------------------------------------
    def _build_right(self):
        # 清空（setParent(None) 立即脱离渲染，deleteLater 延迟销毁 C++ 对象——
        # 布局内直接 delete 会触发 Qt 断言；与 AI 聊天页 _clear_chat 同款）
        while self.right_v.count():
            item = self.right_v.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.lbl_bank_title = QLabel("")
        self.lbl_bank_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #1f2328;")
        self.right_v.addWidget(self.lbl_bank_title)

        if self._game == "truth_dare":
            self._build_td_bank()
            self._build_td_record()
        elif self._game == "spy":
            self._build_spy_bank()
            self._build_spy_record()
        elif self._game == "turtle_soup":
            self._build_soup_bank()
            self._build_soup_record()
        # 底部 stretch（卡片 stretch 分配，剩余空间归 stretch）
        self.right_v.addStretch(0)

    def _card_actions(self, *btns):
        out = []
        for b in btns:
            out.append(b)
        return out

    # ============================================================
    #  题库卡：真心话大冒险（双 Tab 行内编辑）
    # ============================================================
    def _build_td_bank(self):
        self.lbl_bank_title.setText("📚 题库管理 · 真心话 / 大冒险")
        self.tbl_bank = _TableEditable(0, 1)
        self.tbl_bank.setHorizontalHeaderLabels(["题目文本（双击编辑）"])
        self.tbl_bank.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_bank.verticalHeader().setVisible(False)
        self.tbl_bank.setMinimumHeight(180)  # 底线小，高度由布局分配（表格 Expanding 吃剩余空间）

        self.tabs_bank = QTabWidget()
        self.tabs_bank.addTab(self.tbl_bank, "真心话")
        # 大冒险 Tab 直接建（08-21 修复：原懒加载 hasattr 判断在切游戏重建右列后
        # 失效——_build_right 只销毁控件不清属性，hasattr 恒真导致 addTab 不再执行，
        # 只剩 1 个 Tab）
        self._td_tab2 = _TableEditable(0, 1)
        self._td_tab2.setHorizontalHeaderLabels(["题目文本（双击编辑）"])
        self._td_tab2.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._td_tab2.verticalHeader().setVisible(False)
        self._td_tab2.setMinimumHeight(180)  # 与真心话 Tab 一致
        self.tabs_bank.addTab(self._td_tab2, "大冒险")
        _compact_tabs(self.tabs_bank)  # 小标签样式（08-21 用户要求统一）

        # 搜索
        self.ed_bank_search = QLineEdit()
        self.ed_bank_search.setPlaceholderText("🔍 搜索题目…")
        self.ed_bank_search.setClearButtonEnabled(True)
        self.ed_bank_search.textChanged.connect(self._filter_bank)
        self._bank_rows_all: list[str] = []
        self._bank_dirty = False

        def _load():
            rows = {}
            for i, p in enumerate(self._bank_paths("truth_dare")):
                data, header = _read_line_bank(p)
                rows[i] = data
            return rows

        w = Worker(_load)

        def _ok(res):
            if self._game != "truth_dare":      # 切游戏竞态：控件已销毁，丢弃结果
                return
            self._fill_td_tab(0, res.get(0, []))
            self._fill_td_tab(1, res.get(1, []))

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"题库加载失败: {e}"))
        w.start()
        self.mw._track(w)

        # 按钮
        self.btn_bank_add = QPushButton("＋ 添加")
        self.btn_bank_del = QPushButton("🗑 删除")
        self.btn_bank_up = QPushButton("↑ 上移")
        self.btn_bank_down = QPushButton("↓ 下移")
        self.btn_bank_save = QPushButton("💾 保存")
        self.btn_bank_reload = QPushButton("♻️ 通知 bot 重载")

        def _add():
            self._bank_append(self.tabs_bank.currentIndex())
        self.btn_bank_add.clicked.connect(_add)
        self.btn_bank_del.clicked.connect(self._bank_remove)
        self.btn_bank_up.clicked.connect(lambda: self._bank_move(-1))
        self.btn_bank_down.clicked.connect(lambda: self._bank_move(1))
        self.btn_bank_save.clicked.connect(self._bank_save_td)
        self.btn_bank_reload.clicked.connect(lambda: self._bank_reload("truth_dare"))
        self.btn_bank_add.setToolTip("在末尾添加一行（进入编辑态）")
        self.btn_bank_del.setToolTip("删除选中行（保存后生效）")
        self.btn_bank_save.setToolTip("写入文件（自动备份 .bak）；保存后点「重载」让运行中的 bot 生效")
        self.btn_bank_reload.setToolTip("POST /reload truth_dare——bot 未运行时仅落盘，启动后生效")

        card = _Card("📚 题库管理",
                     actions=[self.ed_bank_search, self.btn_bank_add, self.btn_bank_del,
                              self.btn_bank_up, self.btn_bank_down, self.btn_bank_save,
                              self.btn_bank_reload])
        self.ed_bank_search.setMaximumWidth(180)
        card.add(self.tabs_bank, 2)
        hint = QLabel("行内双击编辑 · 保存前改动只在界面（💾 落盘）· 保存保留文件头注释")
        hint.setStyleSheet("color: #667085; font-size: 12px;")
        card.add(hint)
        self.right_v.addWidget(card, 2)

    def _fill_td_tab(self, idx, rows):
        """把数据填进第 idx 个 Tab 的表格（0=真心话 1=大冒险）。"""
        tbl = self.tbl_bank if idx == 0 else self._td_tab2
        tbl.blockSignals(True)
        tbl.setRowCount(len(rows))
        for i, txt in enumerate(rows):
            it = QTableWidgetItem(txt)
            tbl.setItem(i, 0, it)
        tbl.blockSignals(False)
        tbl._all_rows = list(rows)  # 过滤用
        self._apply_filter(tbl)

    def _cur_bank_tbl(self):
        return self.tbl_bank if self.tabs_bank.currentIndex() == 0 else self._td_tab2

    def _bank_append(self, idx):
        tbl = self.tbl_bank if idx == 0 else self._td_tab2
        tbl._all_rows = list(getattr(tbl, "_all_rows", []))
        tbl._all_rows.append("")
        tbl.setRowCount(len(tbl._all_rows))
        it = QTableWidgetItem("")
        tbl.setItem(tbl.rowCount() - 1, 0, it)
        self._apply_filter(tbl)
        tbl.setCurrentCell(tbl.rowCount() - 1, 0)
        tbl.editItem(it)

    def _bank_remove(self):
        tbl = self._cur_bank_tbl()
        r = tbl.currentRow()
        if r < 0:
            return
        rows = list(getattr(tbl, "_all_rows", []))
        if r < len(rows):
            rows.pop(r)
        tbl._all_rows = rows
        tbl.setRowCount(len(rows))
        for i, txt in enumerate(rows):
            tbl.setItem(i, 0, QTableWidgetItem(txt))
        self._apply_filter(tbl)
        self.mw.statusBar().showMessage("已删除选中行（💾 保存后生效）", 3000)

    def _bank_move(self, direction):
        tbl = self._cur_bank_tbl()
        r = tbl.currentRow()
        if r < 0:
            return
        rows = list(getattr(tbl, "_all_rows", []))
        j = r + direction
        if not (0 <= j < len(rows)):
            return
        rows[r], rows[j] = rows[j], rows[r]
        tbl._all_rows = rows
        tbl.setRowCount(len(rows))
        for i, txt in enumerate(rows):
            tbl.setItem(i, 0, QTableWidgetItem(txt))
        self._apply_filter(tbl)
        tbl.selectRow(j)

    def _filter_bank(self, _text=""):
        for tbl in (self.tbl_bank, getattr(self, "_td_tab2", None)):
            if tbl is not None:
                self._apply_filter(tbl)

    def _apply_filter(self, tbl):
        kw = self.ed_bank_search.text().strip() if hasattr(self, "ed_bank_search") else ""
        rows = getattr(tbl, "_all_rows", [])
        for i in range(tbl.rowCount()):
            tbl.setRowHidden(i, bool(kw) and kw not in rows[i])

    def _bank_save_td(self):
        """真心话/大冒险题库落盘（两个 Tab 都写各自文件）。"""
        paths = self._bank_paths("truth_dare")
        if not paths:
            return
        tabs = [self.tbl_bank, getattr(self, "_td_tab2", None)]
        if len(tabs) < 2 or tabs[1] is None:
            return
        if not self.mw.confirm("保存题库", "把界面题库写入文件？\n（自动备份原文件；保存后建议点「♻️ 重载」）"):
            return

        def _do():
            baks = []
            for i, tbl in enumerate(tabs):
                rows = [it.text().strip() if it else ""
                        for it in (tbl.item(r, 0) for r in range(tbl.rowCount()))]
                rows = [r for r in rows if r]
                data, header = _read_line_bank(paths[i])  # 取原头部注释
                bak = _write_line_bank(paths[i], rows, header)
                baks.append(bak)
            return baks

        w = Worker(_do)

        def _ok(res):
            bak = res[0]
            self.mw.statusBar().showMessage(
                f"✅ 题库已保存（备份 {os.path.basename(bak) if bak else '—'}）——点「♻️ 重载」让 bot 生效", 8000)
            self._load_summary()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"保存失败: {e}", 8000))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  题库卡：谁是卧底（三列行内编辑）
    # ============================================================
    def _build_spy_bank(self):
        self.lbl_bank_title.setText("📚 题库管理 · 谁是卧底词对")
        self.tbl_spy = _TableEditable(0, 3)
        self.tbl_spy.setHorizontalHeaderLabels(["平民词", "卧底词", "白板词（可空）"])
        hh = self.tbl_spy.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_spy.verticalHeader().setVisible(False)

        self.ed_spy_search = QLineEdit()
        self.ed_spy_search.setPlaceholderText("🔍 搜索词…")
        self.ed_spy_search.setClearButtonEnabled(True)
        self.ed_spy_search.textChanged.connect(self._filter_spy)

        self.btn_spy_add = QPushButton("＋ 添加")
        self.btn_spy_del = QPushButton("🗑 删除")
        self.btn_spy_save = QPushButton("💾 保存")
        self.btn_spy_reload = QPushButton("♻️ 通知 bot 重载")
        self.btn_spy_add.clicked.connect(self._spy_append)
        self.btn_spy_del.clicked.connect(self._spy_remove)
        self.btn_spy_save.clicked.connect(self._spy_save)
        self.btn_spy_reload.clicked.connect(lambda: self._bank_reload("spy"))
        self.btn_spy_save.setToolTip("写入 spy_wordbank.txt（自动备份）")
        self.btn_spy_reload.setToolTip("POST /reload spy——bot 未运行时仅落盘")

        card = _Card("📚 题库管理",
                     actions=[self.ed_spy_search, self.btn_spy_add, self.btn_spy_del,
                              self.btn_spy_save, self.btn_spy_reload])
        self.ed_spy_search.setMaximumWidth(180)
        card.add(self.tbl_spy, 1)
        hint = QLabel("格式 平民词|卧底词|白板（白板可空）· 双击编辑 · 💾 保存保留文件头注释")
        hint.setStyleSheet("color: #667085; font-size: 12px;")
        card.add(hint)
        self.right_v.addWidget(card, 2)

        def _load():
            data, header = _read_line_bank(self._bank_paths("spy")[0])
            rows = []
            for ln in data:
                parts = ln.split("|")
                if len(parts) == 2:
                    rows.append([parts[0].strip(), parts[1].strip(), ""])
                elif len(parts) >= 3:
                    rows.append([p.strip() for p in parts[:3]])
                else:
                    rows.append([ln, "", ""])
            return rows, header

        w = Worker(_load)

        def _ok(res):
            if self._game != "spy":
                return
            rows, header = res
            self._spy_header = header
            self.tbl_spy._all_rows = [list(r) for r in rows]
            self._fill_spy_rows()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"题库加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_spy_rows(self):
        rows = getattr(self.tbl_spy, "_all_rows", [])
        self.tbl_spy.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j in range(3):
                it = QTableWidgetItem(r[j] if j < len(r) else "")
                self.tbl_spy.setItem(i, j, it)
        self._apply_spy_filter()

    def _spy_append(self):
        rows = list(getattr(self.tbl_spy, "_all_rows", []))
        rows.append(["", "", ""])
        self.tbl_spy._all_rows = rows
        self._fill_spy_rows()
        self.tbl_spy.setCurrentCell(len(rows) - 1, 0)
        it = self.tbl_spy.item(len(rows) - 1, 0)
        if it:
            self.tbl_spy.editItem(it)

    def _spy_remove(self):
        r = self.tbl_spy.currentRow()
        if r < 0:
            return
        rows = list(getattr(self.tbl_spy, "_all_rows", []))
        if r < len(rows):
            rows.pop(r)
        self.tbl_spy._all_rows = rows
        self._fill_spy_rows()

    def _filter_spy(self, _text=""):
        self._apply_spy_filter()

    def _apply_spy_filter(self):
        kw = self.ed_spy_search.text().strip()
        rows = getattr(self.tbl_spy, "_all_rows", [])
        for i in range(self.tbl_spy.rowCount()):
            hit = " ".join(rows[i]) if i < len(rows) else ""
            self.tbl_spy.setRowHidden(i, bool(kw) and kw not in hit)

    def _spy_save(self):
        path = self._bank_paths("spy")[0]
        rows = getattr(self.tbl_spy, "_all_rows", [])
        # 界面内容 → 行（读表格实时值，覆盖 _all_rows 的旧快照）
        live = []
        for i in range(self.tbl_spy.rowCount()):
            vals = []
            for j in range(3):
                it = self.tbl_spy.item(i, j)
                vals.append(it.text().strip() if it else "")
            if not any(vals):
                continue
            line = "|".join(vals[:2]) + (f"|{vals[2]}" if vals[2] else "")
            live.append(line)
        if not self.mw.confirm("保存题库", f"写入 spy_wordbank.txt（{len(live)} 组词对）？\n（自动备份原文件）"):
            return

        def _do():
            header = getattr(self, "_spy_header", [])
            bak = _write_line_bank(path, live, header)
            return bak, len(live)

        w = Worker(_do)

        def _ok(res):
            bak, n = res
            self.mw.statusBar().showMessage(
                f"✅ 卧底词库已保存 {n} 组（备份 {os.path.basename(bak) if bak else '—'}）", 8000)
            self._load_summary()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"保存失败: {e}", 8000))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  题库卡：海龟汤（表格 + 底部编辑器）
    # ============================================================
    def _build_soup_bank(self):
        self.lbl_bank_title.setText("📚 题库管理 · 海龟汤")
        self.tbl_soup = _TableNoEdit(0, 3)
        self.tbl_soup.setHorizontalHeaderLabels(["id", "标题", "汤面预览"])
        self.tbl_soup.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_soup.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_soup.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_soup.verticalHeader().setVisible(False)
        self.tbl_soup.setColumnWidth(2, 320)
        self.tbl_soup.setMinimumHeight(120)
        self.tbl_soup.itemSelectionChanged.connect(self._soup_select)

        self.ed_soup_title = QLineEdit()
        self.ed_soup_title.setPlaceholderText("汤标题")
        self.ed_soup_surface = QPlainTextEdit()
        self.ed_soup_surface.setPlaceholderText("汤面（开局谜面）")
        self.ed_soup_truth = QPlainTextEdit()
        self.ed_soup_truth.setPlaceholderText("汤底（真相）")
        self.ed_soup_hints = QPlainTextEdit()
        self.ed_soup_hints.setPlaceholderText("提示 hints（每行一条）")
        # QPlainTextEdit 的 sizeHint 按文档内容算（长汤文会撑高页面 → 外层滚动条），
        # 同样 Ignored+Expanding（08-21，与表格同因）
        for _ed in (self.ed_soup_surface, self.ed_soup_truth, self.ed_soup_hints):
            _ed.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

        self.btn_soup_new = QPushButton("＋ 新汤")
        self.btn_soup_del = QPushButton("🗑 删除本汤")
        self.btn_soup_save = QPushButton("💾 保存")
        self.btn_soup_reload = QPushButton("♻️ 通知 bot 重载")
        self.btn_soup_new.clicked.connect(self._soup_new)
        self.btn_soup_del.clicked.connect(self._soup_delete)
        self.btn_soup_save.clicked.connect(self._soup_save)
        self.btn_soup_reload.clicked.connect(lambda: self._bank_reload("turtle_soup"))
        self.btn_soup_save.setToolTip("写入 turtle_soup.json（自动备份）")

        card = _Card("📚 题库管理",
                     actions=[self.btn_soup_new, self.btn_soup_del,
                              self.btn_soup_save, self.btn_soup_reload])
        card.add(self.tbl_soup, 3)
        # 编辑器区（08-21 修复过度压缩）：
        # 原 ed_box(QGroupBox) 设 (Ignored, Ignored) 后永远只拿最小高度（3 编辑器
        # 各 48px ≈ 3 行），吃不到卡片剩余空间 → 用户实际无法操作。
        # _SoupEditBox：sizeHint 恒=最小值（不撑爆页面）+ 垂直 Expanding（可分剩余空间），
        # 编辑器 minH 提到 90px（≈5 行）保证可操作，卡内 stretch 汤表 3 : 编辑器 4。
        ed_box = _SoupEditBox("选中汤编辑（key_facts 只读展示于 tooltip）")
        ed_box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        ev = QVBoxLayout(ed_box)
        ev.setSpacing(6)
        ev.addWidget(self.ed_soup_title)
        # 编辑器 minH 90px（≈5 行）保证可操作——卡片 sizeHint 已被覆盖，
        # 不再需要压小最小高度去挤视口
        self.ed_soup_surface.setMinimumHeight(90)
        self.ed_soup_truth.setMinimumHeight(90)
        self.ed_soup_hints.setMinimumHeight(90)
        ev.addWidget(self.ed_soup_surface)
        ev.addWidget(self.ed_soup_truth)
        ev.addWidget(self.ed_soup_hints)
        card.add(ed_box, 4)
        self.right_v.addWidget(card, 3)

        def _load():
            return _read_soup_bank(self._bank_paths("turtle_soup")[0])

        w = Worker(_load)

        def _ok(res):
            if self._game != "turtle_soup":
                return
            self._soup = list(res or [])
            self._soup_sel = -1
            self._fill_soup_table()
            self._clear_soup_editor()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"题库加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_soup_table(self):
        self.tbl_soup.setRowCount(len(self._soup))
        for i, s in enumerate(self._soup):
            self.tbl_soup.setItem(i, 0, QTableWidgetItem(str(s.get("id", i + 1))))
            self.tbl_soup.setItem(i, 1, QTableWidgetItem(s.get("title", "")))
            prev = " ".join(str(s.get("surface", "")).split())
            self.tbl_soup.setItem(i, 2, QTableWidgetItem(prev[:60] + ("…" if len(prev) > 60 else "")))

    def _clear_soup_editor(self):
        self.ed_soup_title.clear()
        self.ed_soup_surface.clear()
        self.ed_soup_truth.clear()
        self.ed_soup_hints.clear()

    def _soup_select(self):
        r = self.tbl_soup.currentRow()
        if r < 0 or r >= len(self._soup):
            return
        self._soup_sel = r
        s = self._soup[r]
        self.ed_soup_title.setText(str(s.get("title", "")))
        self.ed_soup_surface.setPlainText(str(s.get("surface", "")))
        self.ed_soup_truth.setPlainText(str(s.get("truth", "")))
        self.ed_soup_hints.setPlainText("\n".join(str(h) for h in s.get("hints", [])))
        kf = s.get("key_facts", [])
        if kf:
            self.ed_soup_truth.setToolTip("key_facts（只读）:\n" + "\n".join(str(k) for k in kf))

    def _soup_new(self):
        new_id = max((s.get("id", 0) for s in self._soup), default=0) + 1
        self._soup.append({"id": new_id, "title": "", "surface": "", "truth": "",
                           "hints": [], "key_facts": []})
        self._soup_sel = len(self._soup) - 1
        self._fill_soup_table()
        self.tbl_soup.selectRow(self._soup_sel)
        self.ed_soup_title.setFocus()

    def _soup_delete(self):
        if self._soup_sel < 0:
            return
        s = self._soup[self._soup_sel]
        if not self.mw.confirm("删除海龟汤", f"删除「{s.get('title', s.get('id'))}」？\n（💾 保存后生效）"):
            return
        self._soup.pop(self._soup_sel)
        self._soup_sel = min(self._soup_sel, len(self._soup) - 1)
        self._fill_soup_table()
        if self._soup_sel >= 0:
            self.tbl_soup.selectRow(self._soup_sel)
        else:
            self._clear_soup_editor()

    def _soup_save(self):
        # 编辑器内容回填选中汤
        if self._soup_sel >= 0 and self._soup_sel < len(self._soup):
            s = self._soup[self._soup_sel]
            s["title"] = self.ed_soup_title.text().strip()
            s["surface"] = self.ed_soup_surface.toPlainText().strip()
            s["truth"] = self.ed_soup_truth.toPlainText().strip()
            s["hints"] = [h.strip() for h in self.ed_soup_hints.toPlainText().splitlines() if h.strip()]
        if not self._soup:
            self.mw.statusBar().showMessage("空题库，无可保存", 5000)
            return
        if not self.mw.confirm("保存题库", f"写入 turtle_soup.json（{len(self._soup)} 汤）？\n（自动备份原文件）"):
            return
        path = self._bank_paths("turtle_soup")[0]
        data = [dict(s) for s in self._soup]

        def _do():
            bak = _write_soup_bank(path, data)
            return bak, len(data)

        w = Worker(_do)

        def _ok(res):
            bak, n = res
            self.mw.statusBar().showMessage(
                f"✅ 海龟汤已保存 {n} 汤（备份 {os.path.basename(bak) if bak else '—'}）", 8000)
            self._load_summary()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"保存失败: {e}", 8000))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  热重载（bot 侧）
    # ============================================================
    def _bank_reload(self, game_id: str):
        what = RELOAD_WHAT[game_id]
        if not self.mw.confirm("重载题库", f"通知 bot 重载 {what} 题库？\n（bot 未运行时仅落盘，启动后生效）"):
            return
        w = Worker(api_client.reload_resources, self.mw.cfg, what)

        def _ok(r):
            res = r.get("result", r) if isinstance(r, dict) else {}
            self.mw.statusBar().showMessage(f"♻️ 重载完成: {res}", 8000)

        w.finished_ok.connect(_ok)
        w.finished_err.connect(
            lambda e: self.mw.statusBar().showMessage(f"bot 未响应（{e}）——已落盘，bot 启动/重启后生效", 8000))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  记录卡：真心话大冒险（做过题）
    # ============================================================
    def _build_td_record(self):
        self.btn_rec_del = QPushButton("🗑 删除选中")
        self.btn_rec_clear = QPushButton("🧹 清空全部")
        self.btn_rec_del.clicked.connect(lambda: self._rec_delete("truth_dare"))
        self.btn_rec_clear.clicked.connect(lambda: self._rec_clear_all("truth_dare"))
        self.btn_rec_del.setToolTip("删除选中记录（该题可再被抽到）")
        self.btn_rec_clear.setToolTip("清空全部做过记录（/清空做过 同效）")

        self.tbl_rec = _TableNoEdit(0, 6)
        self.tbl_rec.setHorizontalHeaderLabels(["QQ 号", "昵称", "题目", "类型", "群号", "时间"])
        hh = self.tbl_rec.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.tbl_rec.verticalHeader().setVisible(False)
        self.tbl_rec.setMinimumHeight(170)

        card = _Card("📊 答题记录 · 做过的题目", actions=[self.btn_rec_del, self.btn_rec_clear])
        card.add(self.tbl_rec, 1)
        hint = QLabel("删除记录 = 允许该用户再被抽到同一题（与游戏内 /做过 /清空做过 同源）")
        hint.setStyleSheet("color: #667085; font-size: 12px;")
        card.add(hint)
        self.right_v.addWidget(card, 3)

        self._load_records()

    def _load_records(self):
        game = self._game
        if game == "truth_dare":
            self._load_td_records()
        elif game == "spy":
            self._load_spy_records()
        elif game == "turtle_soup":
            self._load_soup_records()

    def _load_td_records(self):
        def _do():
            rows = api_client.query(
                self.mw.cfg, "truth_dare",
                "SELECT id, user_id, question_text, question_type, group_id, answered_at "
                "FROM user_question_history ORDER BY answered_at DESC LIMIT 500")
            # 昵称：message_archive 最近一条
            uids = sorted({r["user_id"] for r in rows if r["user_id"]})
            nicks = {}
            for uid in uids:
                try:
                    n = api_client.query(
                        self.mw.cfg, "chat",
                        "SELECT nickname FROM message_archive WHERE user_id = ? "
                        "ORDER BY created_at DESC LIMIT 1", (uid,))
                    if n:
                        nicks[uid] = n[0]["nickname"]
                except Exception:
                    pass
            return rows, nicks

        w = Worker(_do)

        def _ok(res):
            if self._game != "truth_dare":
                return
            rows, nicks = res
            self.tbl_rec.setRowCount(len(rows))
            for i, r in enumerate(rows):
                vals = [str(r["user_id"]), nicks.get(r["user_id"], ""),
                        r["question_text"], "真心话" if r["question_type"] == "truth" else "大冒险",
                        str(r["group_id"] or "—"), _ts(r["answered_at"])]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if j == 2:
                        it.setToolTip(v)
                    self.tbl_rec.setItem(i, j, it)
            self.tbl_rec._row_ids = [r["id"] for r in rows]

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"记录加载失败: {e}"))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  记录卡：谁是卧底（战绩 + 已用词对 双 Tab）
    # ============================================================
    def _build_spy_record(self):
        self.btn_rec_del = QPushButton("🗑 删除选中")
        self.btn_rec_clear = QPushButton("🧹 清空本表")
        self.btn_rec_del.clicked.connect(lambda: self._rec_delete("spy"))
        self.btn_rec_clear.clicked.connect(lambda: self._rec_clear_all("spy"))
        self.btn_rec_del.setToolTip("删除当前 Tab 选中的记录行")
        self.btn_rec_clear.setToolTip("清空当前 Tab 对应表（战绩/已用词）")

        self.tabs_rec = QTabWidget()

        self.tbl_spy_stats = _TableNoEdit(0, 5)
        self.tbl_spy_stats.setHorizontalHeaderLabels(["QQ 号", "昵称", "胜场", "总场次", "胜率"])
        hh = self.tbl_spy_stats.horizontalHeader()
        for j in range(5):
            hh.setSectionResizeMode(j, QHeaderView.Stretch)
        self.tbl_spy_stats.verticalHeader().setVisible(False)
        self.tbl_spy_stats.itemSelectionChanged.connect(lambda: None)

        self.tbl_spy_words = _TableNoEdit(0, 4)
        self.tbl_spy_words.setHorizontalHeaderLabels(["平民词", "卧底词", "白板词", "使用时间"])
        self.tbl_spy_words.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_spy_words.verticalHeader().setVisible(False)

        self.tabs_rec.addTab(self.tbl_spy_stats, "战绩")
        self.tabs_rec.addTab(self.tbl_spy_words, "已用词对")
        _compact_tabs(self.tabs_rec)  # 小标签样式（08-21 用户要求统一）

        card = _Card("📊 答题记录 · 谁是卧底", actions=[self.btn_rec_del, self.btn_rec_clear])
        card.add(self.tabs_rec, 1)
        hint = QLabel("删除已用词对 = 该词对可再次被抽到；清空战绩 = 重置胜负统计")
        hint.setStyleSheet("color: #667085; font-size: 12px;")
        card.add(hint)
        self.right_v.addWidget(card, 3)

        self._load_spy_records()

    def _load_spy_records(self):
        def _do():
            stats = api_client.query(
                self.mw.cfg, "spy",
                "SELECT user_id, nickname, wins, total_games, updated_at "
                "FROM spy_stats ORDER BY wins DESC, total_games DESC")
            words = api_client.query(
                self.mw.cfg, "spy",
                "SELECT id, word_civilian, word_spy, word_blank, used_at "
                "FROM used_words ORDER BY used_at DESC LIMIT 300")
            return stats, words

        w = Worker(_do)

        def _ok(res):
            if self._game != "spy":
                return
            stats, words = res
            self.tbl_spy_stats.setRowCount(len(stats))
            for i, r in enumerate(stats):
                rate = f"{r['wins'] / r['total_games'] * 100:.0f}%" if r["total_games"] else "—"
                vals = [str(r["user_id"]), r["nickname"] or "", str(r["wins"]),
                        str(r["total_games"]), rate]
                for j, v in enumerate(vals):
                    self.tbl_spy_stats.setItem(i, j, QTableWidgetItem(v))
            self.tbl_spy_stats._row_ids = [r["user_id"] for r in stats]
            self.tbl_spy_words.setRowCount(len(words))
            for i, r in enumerate(words):
                vals = [r["word_civilian"], r["word_spy"], r["word_blank"] or "—", _ts(r["used_at"])]
                for j, v in enumerate(vals):
                    self.tbl_spy_words.setItem(i, j, QTableWidgetItem(v))
            self.tbl_spy_words._row_ids = [r["id"] for r in words]

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"记录加载失败: {e}"))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  记录卡：海龟汤（使用记录 + 玩家统计 双 Tab）
    # ============================================================
    def _build_soup_record(self):
        self.btn_rec_del = QPushButton("🗑 删除选中")
        self.btn_rec_clear = QPushButton("🧹 清空本表")
        self.btn_rec_del.clicked.connect(lambda: self._rec_delete("turtle_soup"))
        self.btn_rec_clear.clicked.connect(lambda: self._rec_clear_all("turtle_soup"))
        self.btn_rec_del.setToolTip("删除当前 Tab 选中的记录行")
        self.btn_rec_clear.setToolTip("清空当前 Tab 对应表（使用记录/玩家统计）")

        self.tabs_rec = QTabWidget()
        _compact_tabs(self.tabs_rec)  # 小标签样式（基准样式）

        self.tbl_soup_used = _TableNoEdit(0, 4)
        self.tbl_soup_used.setHorizontalHeaderLabels(["汤", "群号", "时间", "猜中"])
        hh = self.tbl_soup_used.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for j in (1, 2, 3):
            hh.setSectionResizeMode(j, QHeaderView.ResizeToContents)
        self.tbl_soup_used.verticalHeader().setVisible(False)
        self.tbl_soup_used.setMinimumHeight(85)

        self.tbl_soup_players = _TableNoEdit(0, 4)
        self.tbl_soup_players.setHorizontalHeaderLabels(["QQ 号", "昵称", "猜中数", "更新时间"])
        self.tbl_soup_players.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_soup_players.verticalHeader().setVisible(False)
        self.tbl_soup_players.setMinimumHeight(85)

        self.tabs_rec.addTab(self.tbl_soup_used, "使用记录")
        self.tabs_rec.addTab(self.tbl_soup_players, "玩家统计")

        card = _Card("📊 答题记录 · 海龟汤", actions=[self.btn_rec_del, self.btn_rec_clear])
        card.add(self.tabs_rec, 1)
        hint = QLabel("删除使用记录 = 该汤可再被抽到；清空玩家统计 = 重置猜中计数")
        hint.setStyleSheet("color: #667085; font-size: 12px;")
        card.add(hint)
        self.right_v.addWidget(card, 3)

        self._load_soup_records()

    def _load_soup_records(self):
        def _do():
            used = api_client.query(
                self.mw.cfg, "turtle_soup",
                "SELECT id, soup_id, group_id, used_at, solved FROM used_soups "
                "ORDER BY used_at DESC LIMIT 300")
            players = api_client.query(
                self.mw.cfg, "turtle_soup",
                "SELECT user_id, nickname, solved_count, updated_at FROM player_stats "
                "ORDER BY solved_count DESC")
            # 汤标题
            soups = _read_soup_bank(self._bank_paths("turtle_soup")[0])
            titles = {s.get("id"): s.get("title", str(s.get("id"))) for s in soups}
            return used, players, titles

        w = Worker(_do)

        def _ok(res):
            if self._game != "turtle_soup":
                return
            used, players, titles = res
            self.tbl_soup_used.setRowCount(len(used))
            for i, r in enumerate(used):
                vals = [titles.get(r["soup_id"], f"#{r['soup_id']}"),
                        str(r["group_id"]), _ts(r["used_at"]),
                        "✅ 是" if r["solved"] else "—"]
                for j, v in enumerate(vals):
                    self.tbl_soup_used.setItem(i, j, QTableWidgetItem(v))
            self.tbl_soup_used._row_ids = [r["id"] for r in used]
            self.tbl_soup_players.setRowCount(len(players))
            for i, r in enumerate(players):
                vals = [str(r["user_id"]), r["nickname"], str(r["solved_count"]), _ts(r["updated_at"])]
                for j, v in enumerate(vals):
                    self.tbl_soup_players.setItem(i, j, QTableWidgetItem(v))
            self.tbl_soup_players._row_ids = [r["user_id"] for r in players]

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"记录加载失败: {e}"))
        w.start()
        self.mw._track(w)

    # ============================================================
    #  记录删除/清空（按当前游戏+Tab 路由）
    # ============================================================
    def _rec_target(self) -> tuple[str, str, str, list, object]:
        """返回 (kind, table_name, pk_col, row_ids, 当前选中 pk 或 None)。

        各表主键不同（spy_stats 主键=user_id，其余=id），DELETE 用对应列。
        """
        game = self._game
        if game == "truth_dare":
            ids = getattr(self.tbl_rec, "_row_ids", [])
            return "truth_dare", "user_question_history", "id", ids, \
                (ids[self.tbl_rec.currentRow()]
                 if 0 <= self.tbl_rec.currentRow() < len(ids) else None)
        if game == "spy":
            if self.tabs_rec.currentIndex() == 0:
                ids = getattr(self.tbl_spy_stats, "_row_ids", [])
                return "spy", "spy_stats", "user_id", ids, \
                    (ids[self.tbl_spy_stats.currentRow()]
                     if 0 <= self.tbl_spy_stats.currentRow() < len(ids) else None)
            ids = getattr(self.tbl_spy_words, "_row_ids", [])
            return "spy", "used_words", "id", ids, \
                (ids[self.tbl_spy_words.currentRow()]
                 if 0 <= self.tbl_spy_words.currentRow() < len(ids) else None)
        if game == "turtle_soup":
            if self.tabs_rec.currentIndex() == 0:
                ids = getattr(self.tbl_soup_used, "_row_ids", [])
                return "turtle_soup", "used_soups", "id", ids, \
                    (ids[self.tbl_soup_used.currentRow()]
                     if 0 <= self.tbl_soup_used.currentRow() < len(ids) else None)
            ids = getattr(self.tbl_soup_players, "_row_ids", [])
            return "turtle_soup", "player_stats", "user_id", ids, \
                (ids[self.tbl_soup_players.currentRow()]
                 if 0 <= self.tbl_soup_players.currentRow() < len(ids) else None)
        return "", "", "id", [], None

    def _rec_delete(self, game_id: str):
        kind, table, pk, _ids, sel = self._rec_target()
        if sel is None:
            self.mw.statusBar().showMessage("先选中一行", 3000)
            return
        if not self.mw.confirm("删除记录", f"从 {table} 删除记录 {pk}={sel}？\n（该题/词对可再次被抽到）"):
            return

        def _do():
            api_client.query(self.mw.cfg, kind,
                             f"DELETE FROM {table} WHERE {pk} = ?", (sel,), write=True)
            return sel

        w = Worker(_do)

        def _ok(res):
            self.mw.statusBar().showMessage(f"🗑 已删除记录 {pk}={res}", 5000)
            self._load_records()
            self._load_summary()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"删除失败: {e}", 6000))
        w.start()
        self.mw._track(w)

    def _rec_clear_all(self, game_id: str):
        kind, table, _pk, ids, _sel = self._rec_target()
        n = len(ids)
        if n == 0:
            self.mw.statusBar().showMessage("当前表为空", 3000)
            return
        if not self.mw.confirm("清空记录", f"清空 {table} 全部 {n} 条记录？\n（与游戏内 /清空做过 同效，不可恢复）"):
            return

        def _do():
            api_client.query(self.mw.cfg, kind, f"DELETE FROM {table}", write=True)
            return n

        w = Worker(_do)

        def _ok(res):
            self.mw.statusBar().showMessage(f"🧹 已清空 {table}（{res} 条）", 5000)
            self._load_records()
            self._load_summary()

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"清空失败: {e}", 6000))
        w.start()
        self.mw._track(w)
