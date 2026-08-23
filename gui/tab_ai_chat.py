"""
tab_ai_chat.py — AI 聊天页
==========================
布局（stretch 填满视口，无外层滚动条）：
  左列（固定 520px，两行）：
    上：群列表（群号 + 用户数）
    下：用户列表（QQ 号 / 昵称[220px] / bot 好感度 / bot 人设）
  右列（拉伸）：所选用户与该 bot 的聊天记录
    QQ/微信私聊式气泡（08-21）：每条消息一个气泡，
    用户消息靠右（微信绿 #95ec69），bot 回复靠左（白底描边），
    气泡下方小字时间戳，最新在底部（默认滚到底部）
    群聊 → session_key = group_{gid}_user_{uid}
数据：GUI 直读 SQLite（只读，WAL 并发安全，tab_messages 同款模式），
不新增控制 API 路由。bot 是各表写者，GUI 只读无冲突。
群/用户列表数据源（08-22 方案 A，用户拍板）：chat_history.db 的
chat_messages（AI 对话记忆单元，session_key=group_{gid}_user_{uid}）——
原来读 personas.db.user_personas（人设表，仅每日 0 点定时任务给已注册
集群群批量写入），刚开聊的群当天不出现（如群 900000011 问题）。
改后：群一开聊当天即上榜；过滤=群号>1e8（挡 111 类测试群号）+
message_archive≥10 行（挡 999999 等无真实存档的测试会话）。
人设/好感度列仍从 user_personas/bot_favorability 关联取，无数据显示灰色 —。
bot 人设（08-21 修正）：不是用户自己人设，而是「bot 跟该用户聊天时演谁」——
按 user_id 查 personas.db.bot_personalities（与 core/router.py 的
get_personality(user_id) 同一语义）；该用户未设置 → 列表显示灰色 —（默认
女高中生人设不占列表空间，仅 tooltip 说明）。
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QScrollArea, QFrame, QSizePolicy,
    QPushButton,
)

import api_client
from worker import Worker

_PAGE_SIZE = 100          # 聊天记录每页条数
# 群列表/用户列表数据源（08-22 方案 A）：chat_messages 的 session_key 解析群号
# （session_key = group_{gid}_user_{uid}；取 'group_' 之后 '_user_' 之前的段）
_SESSION_GROUP_EXPR = (
    "CAST(substr(session_key, 7, instr(substr(session_key, 7), '_user_') - 1) "
    "AS INTEGER)")
# message_archive 存档行数门槛：挡测试残留（999999/111222333/900000012/
# 900000013 无存档，111 仅 1 行）；真实群最小 67 行，新聊的群 15 行即过
_ARCHIVE_MIN_ROWS = 10
_FAV_TIERS = {           # 好感度五档 → 徽章色（与 bot 侧阈值一致）
    "仇人": "#cf222e",
    "陌生人": "#6e7781",
    "普通朋友": "#0969da",
    "好朋友": "#1a7f37",
    "情侣": "#bf3989",
}
_PERSONA_PREVIEW_LEN = 60

# 私聊式气泡样式：用户=微信绿（靠右），bot=白底描边（靠左）
_BUBBLE_USER_QSS = ("background: #95ec69; border: 1px solid #95ec69;"
                    " border-radius: 10px; padding: 8px 12px;"
                    " color: #1f2328; font-size: 13px;")
_BUBBLE_BOT_QSS = ("background: #ffffff; border: 1px solid #d0d7de;"
                   " border-radius: 10px; padding: 8px 12px;"
                   " color: #1f2328; font-size: 13px;")
def _ts(t) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(t)))
    except Exception:
        return str(t)


def _persona_preview(text: str) -> str:
    """bot 人设文本 → 单行预览（压空白、超长截断），全量放 tooltip。"""
    s = " ".join((text or "").split())
    return s[:_PERSONA_PREVIEW_LEN] + ("…" if len(s) > _PERSONA_PREVIEW_LEN else "")


class _TableNoEdit(QTableWidget):
    def __init__(self, rows, cols, *a, **kw):
        super().__init__(rows, cols, *a, **kw)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)


class _Card(QFrame):
    """白卡片容器：标题 16px bold + 内容区。

    actions：标题行右侧的工具按钮列表（用户偏好：按钮放标题行右侧同行，
    不单独占行）。
    """

    def __init__(self, title: str, parent=None, actions=None):
        super().__init__(parent)
        self.setObjectName("ai_chat_card")
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
        self._body = v
        # 暴露给调用方往卡片里加东西
        self.body = v

    def add(self, w, stretch=0):
        self.body.addWidget(w, stretch)


class _BubbleRow(QWidget):
    """一条消息 = 一行：[时间戳][气泡][对侧留白]。

    role='user'：气泡靠右（微信绿）；role='assistant'：气泡靠左（白底描边）。
    时间戳在气泡同侧上方小字（私聊样式惯例）。
    """

    _MAX_W = 520          # 气泡默认最大宽（可被 ai_chat.bubble_max_width 覆盖）

    def __init__(self, role: str, text: str, ts: str, parent=None,
                 max_w: int = _MAX_W):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 2, 12, 2)
        v.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(6)
        stamp = QLabel(ts)
        stamp.setStyleSheet("color: #6e7781; font-size: 11px;")
        head.addWidget(stamp)
        v.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(6)
        body.setContentsMargins(0, 0, 0, 0)
        bubble = QLabel(text)
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum,
                             QSizePolicy.Policy.Preferred)
        bubble.setMaximumWidth(max_w)
        qss = _BUBBLE_USER_QSS if role == "user" else _BUBBLE_BOT_QSS
        bubble.setStyleSheet(qss)

        if role == "user":
            body.addStretch(1)          # 用户消息：左侧留白
            body.addWidget(bubble)      # 气泡靠右
        else:
            body.addWidget(bubble)      # bot 消息：气泡靠左
            body.addStretch(1)          # 右侧留白
        v.addLayout(body)


class TabAiChat(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._silent = False
        self._group_rows = []       # [{group_id, n}]
        self._user_rows = []        # [{user_id, nickname, fav, fav_rel, persona}]
        self._all_users = []
        self._cur_group = None      # 选中的群号（int）
        self._cur_user = None       # 选中的用户行 dict
        self._chat_loaded = 0       # 已加载聊天条数
        self._chat_total = 0
        self._chat_key = None       # 当前渲染的 (scope, user_id) 缓存键
        self._build()
        self._load_groups()

    # ------------------------------------------------------------
    #  构建
    # ------------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        # ---------------- 左列：群列表（上）+ 用户列表（下） ----------------
        left = QWidget()
        left.setFixedWidth(520)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)

        # 设置按钮（群列表卡片标题行右上角，2026-08-21 用户要求；
        # 不单独占行、文字完整自适应宽度）
        self.btn_cfg_params = QPushButton("⚙️ 参数")
        self.btn_cfg_params.setToolTip(
            "AI 聊天参数：显示参数（每页条数/气泡宽度）+ LLM 调用参数"
            "（max_tokens/温度/thinking/json_mode/超时，作用于 bot 回复链路）")
        self.btn_cfg_prompts = QPushButton("📝 提示词")
        self.btn_cfg_prompts.setToolTip(
            "默认人设（女高中生）/ 角色模板（{personality} 扮演规则），保存后热重载生效")
        self.btn_cfg_params.clicked.connect(self._open_params_dialog)
        self.btn_cfg_prompts.clicked.connect(self._open_prompts_dialog)

        self.card_group = _Card("👥 群列表", actions=[self.btn_cfg_params,
                                                      self.btn_cfg_prompts])
        self.tbl_group = _TableNoEdit(0, 2)
        self.tbl_group.setHorizontalHeaderLabels(["群号", "用户数"])
        self.tbl_group.verticalHeader().setVisible(False)
        self.tbl_group.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.tbl_group.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.card_group.add(self.tbl_group, 1)
        self.tbl_group.itemSelectionChanged.connect(self._on_group_select)
        # 群列表占左列上半（矮一些，选 2 行高即可）
        self.tbl_group.setMinimumHeight(120)
        lv.addWidget(self.card_group, 2)

        self.card_user = _Card("👤 用户列表")
        self.tbl_user = _TableNoEdit(0, 4)
        self.tbl_user.setHorizontalHeaderLabels(["QQ 号", "昵称", "好感度", "bot 人设"])
        self.tbl_user.verticalHeader().setVisible(False)
        hh = self.tbl_user.horizontalHeader()
        # QQ 号/好感度 自适应内容；昵称固定 220（加宽，长昵称完整显示）；
        # bot 人设 剩余空间（Interactive 可手动拖）
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_user.setColumnWidth(1, 220)
        self.card_user.add(self.tbl_user, 1)
        self.tbl_user.itemSelectionChanged.connect(self._on_user_select)
        lv.addWidget(self.card_user, 3)

        row.addWidget(left)

        # ---------------- 右列：聊天记录 ----------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        bar = QHBoxLayout()
        self.lbl_chat_title = QLabel("💬 聊天记录")
        ft = QFont()
        ft.setPointSize(int(ft.pointSize() * 1.25))
        ft.setBold(True)
        self.lbl_chat_title.setFont(ft)
        bar.addWidget(self.lbl_chat_title)
        bar.addStretch(1)
        self.lbl_chat_state = QLabel("先在左侧选一个用户")
        self.lbl_chat_state.setStyleSheet("color: #656d76;")
        bar.addWidget(self.lbl_chat_state)
        rv.addLayout(bar)

        self.chat_area = QScrollArea()
        self.chat_area.setWidgetResizable(True)
        self.chat_area.setFrameShape(QScrollArea.NoFrame)
        self.chat_host = QWidget()          # 气泡行容器（滚动内容）
        self.chat_host.setStyleSheet("background: transparent;")
        self.chat_v = QVBoxLayout(self.chat_host)
        self.chat_v.setContentsMargins(4, 4, 4, 4)
        self.chat_v.setSpacing(4)
        self.chat_v.addStretch(1)           # 尾部 stretch：内容不足时气泡靠顶
        self.chat_area.setWidget(self.chat_host)
        rv.addWidget(self.chat_area, 1)

        # 查询/分析设置底栏（2026-08-22）：右对齐 3 个 🔍 按钮
        # （与左上角 ⚙️ 参数/📝 提示词 区分；🔍 前缀防同名混淆）
        fm = QFontMetrics(self.font())
        qa_bar = QHBoxLayout()
        qa_bar.setSpacing(8)
        qa_bar.addStretch(1)
        qa_btn_specs = [
            ("🔍 查询参数",
             "查询/分析 6 命令 · 时间窗/分块/截断 10 项"
             "（/查询 /分析 /群像 /活跃度 /总结 /评选），保存后热重载生效",
             self._open_qa_params_dialog),
            ("🔍 查询LLM",
             "查询/分析 6 命令 · LLM 参数 29 项"
             "（max_tokens / thinking / 重试 / json_mode），保存后热重载生效",
             self._open_qa_llm_dialog),
            ("🔍 查询提示词",
             "查询/分析 6 命令 · 提示词 25 段 6 Tab"
             "（查询4/分析7/群像5/总结4/评选4/定时1），保存后热重载生效",
             self._open_qa_prompts_dialog),
        ]
        for text, tip, slot in qa_btn_specs:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setMinimumHeight(28)
            # emoji 会让 sizeHint 算窄切字：按文字实际宽度 + 余量兜底
            b.setMinimumWidth(fm.horizontalAdvance(text) + 32)
            b.clicked.connect(slot)
            qa_bar.addWidget(b)
        rv.addLayout(qa_bar)

        row.addWidget(right, 1)
        v.addLayout(row, 1)

        self.setStyleSheet(
            "#ai_chat_card { background: #ffffff; border: 1px solid #d0d7de;"
            " border-radius: 8px; }")

    # ------------------------------------------------------------
    #  数据加载
    # ------------------------------------------------------------
    def _load_groups(self):
        """群列表：chat_messages 对话记忆单元（08-22 方案 A，原 user_personas）。

        语义=「和 bot 聊过天的群」：一开聊当天即上榜（原人设表要等每日 0 点
        定时任务给已注册集群群写入，新群当天不出现）。
        过滤：群号>1e8（挡 111 类测试群号）+ message_archive≥10 行
        （挡 999999 等无真实存档的测试会话）。
        计数列=对话用户数（COUNT DISTINCT user_id）。
        """
        def _do():
            return api_client.query(
                self.mw.cfg, "chat",
                f"SELECT {_SESSION_GROUP_EXPR} AS group_id, "
                f"COUNT(DISTINCT user_id) AS n "
                f"FROM chat_messages "
                f"WHERE session_key LIKE 'group_%_user_%' AND role = 'user' "
                f"GROUP BY group_id "
                f"HAVING group_id > 100000000 "
                f"  AND group_id IN (SELECT target_id FROM message_archive "
                f"                    GROUP BY target_id "
                f"                    HAVING COUNT(*) >= {_ARCHIVE_MIN_ROWS}) "
                f"ORDER BY n DESC, group_id")

        w = Worker(_do)

        def _ok(rows):
            self._group_rows = rows
            self._fill_group_table()
            self.mw.statusBar().showMessage(f"AI 聊天：{len(rows)} 个群")
            # 默认选中第一个群
            if self._group_rows and self._cur_group is None:
                self._select_row(self.tbl_group, 0)

        w.finished_ok.connect(_ok)
        w.finished_err.connect(
            lambda e: self.mw.statusBar().showMessage(f"AI 聊天群列表加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_group_table(self):
        self._silent = True
        self.tbl_group.setRowCount(len(self._group_rows))
        for i, r in enumerate(self._group_rows):
            self.tbl_group.setItem(i, 0, QTableWidgetItem(str(r["group_id"])))
            self.tbl_group.setItem(i, 1, QTableWidgetItem(str(r["n"])))
        self._silent = False

    def _select_row(self, tbl: QTableWidget, row: int):
        if 0 <= row < tbl.rowCount():
            tbl.selectRow(row)

    def _on_group_select(self):
        if self._silent:
            return
        i = self.tbl_group.currentRow()
        if i < 0 or i >= len(self._group_rows):
            return
        gid = self._group_rows[i]["group_id"]
        if gid == self._cur_group:
            return
        self._cur_group = gid
        self._cur_user = None
        self._user_rows = []   # 08-22：清空旧群残留（新群用户列表加载前防旧数据被误读）
        self._clear_chat()
        # 08-22：清表格选中——setRowCount 不清 selectionModel，旧群选中行残留
        # （行数相同时 currentRow 仍指向 0）→ 后续 selectRow(同索引) 不触发
        # itemSelectionChanged，聊天加载被静默跳过
        self.tbl_user.clearSelection()
        self.tbl_group.clearSelection()
        self.lbl_chat_title.setText("💬 聊天记录")
        self.lbl_chat_state.setText("先在左侧选一个用户")
        self._load_users()

    def _load_users(self):
        gid = self._cur_group

        def _do():
            # 用户列表：该群 chat_messages 的对话用户（08-22 方案 A，原
            # user_personas）；昵称取该用户最新一条非空昵称（对话中昵称可能
            # 变化），按对话消息数降序。
            ps = api_client.query(
                self.mw.cfg, "chat",
                f"SELECT user_id, "
                f"(SELECT nickname FROM chat_messages c2 "
                f" WHERE c2.session_key = chat_messages.session_key "
                f"   AND c2.user_id = chat_messages.user_id "
                f"   AND c2.nickname != '' "
                f" ORDER BY c2.id DESC LIMIT 1) AS nickname, "
                f"COUNT(*) AS msg_n "
                f"FROM chat_messages "
                f"WHERE session_key LIKE 'group_{gid}_user_%' "
                f"  AND role = 'user' "
                f"GROUP BY user_id "
                f"ORDER BY msg_n DESC, user_id")
            # bot 人设：按对话对象 user_id 查（与 router 的 get_personality 同语义）
            bp = api_client.query(
                self.mw.cfg, "personas",
                "SELECT user_id, personality FROM bot_personalities")
            bp_map = {r["user_id"]: r["personality"] for r in bp}
            fav = api_client.query(
                self.mw.cfg, "settings",
                "SELECT user_id, favorability, relationship FROM bot_favorability "
                "WHERE group_id = ?", (gid,))
            fav_map = {r["user_id"]: r for r in fav}
            rows = []
            for p in ps:
                f = fav_map.get(p["user_id"])
                rows.append({
                    "user_id": p["user_id"],
                    "nickname": p["nickname"] or "",
                    "fav": f["favorability"] if f else None,
                    "fav_rel": f["relationship"] if f else None,
                    "bot_persona": bp_map.get(p["user_id"], ""),
                })
            return rows

        w = Worker(_do)

        def _ok(rows):
            self._all_users = rows
            self._user_rows = rows
            self._fill_user_table()
            self.mw.statusBar().showMessage(
                f"群 {gid}：{len(rows)} 人")
            if rows and self._cur_user is None:
                self._select_row(self.tbl_user, 0)

        w.finished_ok.connect(_ok)
        w.finished_err.connect(
            lambda e: self.mw.statusBar().showMessage(f"AI 聊天用户加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fill_user_table(self):
        self._silent = True
        self.tbl_user.setRowCount(len(self._user_rows))
        for i, r in enumerate(self._user_rows):
            it_id = QTableWidgetItem(str(r["user_id"]))
            it_id.setData(Qt.UserRole, r["user_id"])
            self.tbl_user.setItem(i, 0, it_id)
            it_nick = QTableWidgetItem(r["nickname"])
            it_nick.setToolTip(r["nickname"] or "")
            self.tbl_user.setItem(i, 1, it_nick)
            if r["fav"] is None:
                it_fav = QTableWidgetItem("—")
                it_fav.setForeground(QColor("#656d76"))
            else:
                it_fav = QTableWidgetItem(f"{r['fav']}（{r['fav_rel']}）")
                color = _FAV_TIERS.get(r["fav_rel"], "#1f2328")
                it_fav.setForeground(QColor(color))
                it_fav.setToolTip(
                    f"好感度 {r['fav']}/100 · {r['fav_rel']}（bot 每 8 小时衰减一次）")
            self.tbl_user.setItem(i, 2, it_fav)
            if r["bot_persona"]:
                preview = _persona_preview(r["bot_persona"])
                it_p = QTableWidgetItem(preview)
                it_p.setToolTip(
                    f"bot 与该用户聊天时的人设（/人设 单独设置）：\n{r['bot_persona']}")
            else:
                # 未单独设置 → 列表留空（默认人设不占列表空间）
                it_p = QTableWidgetItem("—")
                it_p.setForeground(QColor("#656d76"))
                it_p.setToolTip(
                    "该用户未单独设置 bot 人设，使用程序默认人设（女高中生）")
            self.tbl_user.setItem(i, 3, it_p)
        self._silent = False

    # ------------------------------------------------------------
    #  设置弹窗（显示参数 / 提示词，2026-08-21 新增）
    #  延迟 import：加快 GUI 启动 + 避免启动期依赖（与 tab_personas 同款）
    # ------------------------------------------------------------
    def _open_params_dialog(self):
        from ai_chat_settings_dialogs import ParamsDialog
        ParamsDialog(self.mw).exec()

    def _open_prompts_dialog(self):
        from ai_chat_settings_dialogs import PromptsDialog
        PromptsDialog(self.mw).exec()

    # ---- 查询/分析 3 弹窗（🔍 底栏，2026-08-22；延迟 import 同款）----
    def _open_qa_params_dialog(self):
        from qa_settings_dialogs import QAParamsDialog
        QAParamsDialog(self.mw).exec()

    def _open_qa_llm_dialog(self):
        from qa_settings_dialogs import QALLMDialog
        QALLMDialog(self.mw).exec()

    def _open_qa_prompts_dialog(self):
        from qa_settings_dialogs import QAPromptsDialog
        QAPromptsDialog(self.mw).exec()

    # ---------- AI 聊天显示参数（AI_CHAT_CFG，弹窗保存后热重载刷新）----------
    def ai_cfg(self) -> dict:
        from core.config import DEFAULTS
        merged = dict(DEFAULTS.get("ai_chat") or {})
        cfg = (self.mw.cfg.get("AI_CHAT_CFG") or {})
        merged.update(cfg if isinstance(cfg, dict) else {})
        return merged

    def page_size(self) -> int:
        return int(self.ai_cfg().get("page_size", _PAGE_SIZE))

    def bubble_max_width(self) -> int:
        return int(self.ai_cfg().get("bubble_max_width", _BubbleRow._MAX_W))

    # ------------------------------------------------------------
    #  聊天记录
    # ------------------------------------------------------------
    def _on_user_select(self):
        if self._silent:
            return
        i = self.tbl_user.currentRow()
        if i < 0 or i >= len(self._user_rows):
            return
        user = self._user_rows[i]
        if (self._cur_user is not None
                and self._cur_user["user_id"] == user["user_id"]
                and self._cur_group is not None
                and self._cur_user.get("_gid") == self._cur_group):
            return
        self._cur_user = {**user, "_gid": self._cur_group}
        self._load_chat()

    def _session_key(self):
        """群聊上下文：group_{gid}_user_{uid}（bot 的记忆单元）。"""
        u = self._cur_user
        return f"group_{u['_gid']}_user_{u['user_id']}"

    def _load_chat(self, append: bool = False):
        u = self._cur_user
        gid, uid = u["_gid"], u["user_id"]
        key = f"g{gid}u{uid}"
        if not append and self._chat_key == key:
            return  # 重复选中，不重复拉
        if append and self._chat_key != key:
            return
        self._chat_key = key
        self._chat_loaded = 0
        self._chat_total = 0
        self._clear_chat()

        title = f"💬 {u['nickname'] or uid}（{uid}）"
        self.lbl_chat_title.setText(title)
        self.lbl_chat_state.setText("加载中…")

        def _do():
            # 主来源：bot 群聊上下文（该用户与该 bot 在群里的对话记忆）
            rows = api_client.query(
                self.mw.cfg, "chat",
                "SELECT role, content, nickname, created_at FROM chat_messages "
                "WHERE session_key = ? AND role IN ('user','assistant') "
                "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (self._session_key(), self.page_size(), 0))
            # 该用户在该群的全部发言（存档，含 bot 未回应的）
            user_msgs = api_client.query(
                self.mw.cfg, "chat",
                "SELECT nickname, content, created_at FROM group_chat_cache "
                "WHERE group_id = ? AND user_id = ? "
                "ORDER BY created_at ASC",
                (gid, uid))
            total = api_client.query(
                self.mw.cfg, "chat",
                "SELECT COUNT(*) AS n FROM chat_messages WHERE session_key = ?",
                (self._session_key(),))[0]["n"]
            return rows, user_msgs, total

        w = Worker(_do)

        def _ok(res):
            if self._chat_key != key:
                return  # 用户已切走，丢弃
            rows, user_msgs, total = res
            self._chat_total = total
            self._chat_loaded = len(rows)
            self._render_bubbles(rows, user_msgs)
            self.lbl_chat_state.setText(
                f"已加载 {self._chat_loaded}/{total} 条"
                + (f"｜该用户在群发言 {len(user_msgs)} 条" if user_msgs else ""))

        w.finished_ok.connect(_ok)
        w.finished_err.connect(
            lambda e: self.lbl_chat_state.setText(f"加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _clear_chat(self):
        """清空气泡行（保留尾部 stretch）。setParent(None) 立即脱离渲染，
        deleteLater 延迟销毁 C++ 对象（布局内直接 delete 会触发 Qt 断言）。"""
        while self.chat_v.count() > 1:
            item = self.chat_v.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _add_bubble(self, role: str, text: str, ts: str):
        row = _BubbleRow(role, text, ts, max_w=self.bubble_max_width())
        # 插在尾部 stretch 之前
        self.chat_v.insertWidget(self.chat_v.count() - 1, row)

    def _add_separator(self, text: str):
        lab = QLabel(text)
        lab.setStyleSheet("color: #6e7781; font-size: 12px; padding: 4px 12px;")
        lab.setAlignment(Qt.AlignCenter)
        self.chat_v.insertWidget(self.chat_v.count() - 1, lab)

    def _render_bubbles(self, ctx_rows, user_msgs):
        """私聊式气泡渲染：每条消息一个气泡（用户右/bot 左），
        末尾附该用户在群里的发言存档（最近 10 条，同为用户侧气泡）。"""
        u = self._cur_user
        self._add_separator(
            f"群 {u['_gid']} · {u['nickname'] or u['user_id']} 与 bot 的对话"
            f"（共 {self._chat_total} 条，显示前 {len(ctx_rows)} 条）")
        for r in ctx_rows:
            self._add_bubble(r["role"], r["content"], _ts(r["created_at"]))
        if not ctx_rows:
            self._add_separator("该用户与 bot 暂无群聊对话记录")

        if user_msgs:
            self._add_separator(
                f"该用户在群里的发言（存档 {len(user_msgs)} 条，最近 10 条）")
            for m in user_msgs[-10:]:
                self._add_bubble("user", m["content"], _ts(m["created_at"]))

        # 最新消息在底部 → 滚到底
        self.chat_area.verticalScrollBar().setValue(
            self.chat_area.verticalScrollBar().maximum())
