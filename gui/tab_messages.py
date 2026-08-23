"""
tab_messages.py — 消息管理页
===========================
- 历史消息：按群号/用户/关键词/时间范围搜索 message_archive，全量分页
  （08-21：每页 500 条，右下角标准页码条 + 跳转框，懒加载；
   类型下拉含「撤回消息」：查独立表 message_recalls（时间列
   recalled_at），覆盖文本/图片/语音/视频/转发/文件全部撤回记录；
   时间范围为同行动作文本框，格式化解析：7-9 / 7-9 08:30 /
   2026-07-09 14:30:05，不写年份默认今年，只写日期含当天全天，留空=不限）
- 会话上下文清除：08-21 已移除（bot /清除人设 指令仍可清除单会话记忆）
- 管理员 & 黑名单：08-20 已移至群组集群页
"""

import json
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QComboBox, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QGroupBox, QTextBrowser, QSpinBox,
    QAbstractSpinBox,
)

import api_client
import media_viewer
from worker import Worker

_PAGE_SIZE = 500
_ANALYSIS_MAX_DEFAULT = 10000   # 单次分析条数上限默认值（config.yaml analysis.max_rows）


def _parse_time(s: str, is_end: bool):
    """解析时间范围输入（08-21），返回 (timestamp, err)。

    支持格式（不写年份默认今年）：
      7-9 / 07-9 / 7/9 / 7.9 / 2026-7-9 / 2026-07-09
      7-9 14:30 / 2026-07-09 14:30 / 2026-07-09 14:30:00
    规则：
      - 只写日期时，起始=当日 00:00:00，结束=当日 23:59:59.999（含当天全天）
      - 写了时间则精确到该时刻
      - 空串 = 不限
    """
    s = s.strip().replace("/", "-").replace(".", "-")
    if not s:
        return None, None
    from datetime import datetime
    fmts = (["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"] +
            ["%m-%d %H:%M:%S", "%m-%d %H:%M", "%m-%d"])
    year = datetime.now().year
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=year)
        has_time = dt.second != 0 or dt.minute != 0 or dt.hour != 0
        if not has_time and is_end:
            # 只写日期的结束边界 = 当天最后一毫秒
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
        return dt.timestamp(), None
    return None, f"无法解析时间「{s}」（示例：7-9 或 7-9 14:30 或 2026-07-09）"

# 页码按钮统一样式（08-21 分页）
_PG_BTN_QSS = """
QPushButton {
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    color: #1f2328;
    font-size: 13px;
    min-width: 28px;
    padding: 2px 4px;
}
QPushButton:hover { border-color: #0969da; color: #0969da; }
QPushButton:disabled { color: #b6bcc4; border-color: #e6eaee; background: #fafbfc; }
QPushButton[on="true"] { background: #0969da; color: #ffffff; border-color: #0969da; }
"""


def _ts(t) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(t)))
    except Exception:
        return str(t)


class _NoArrowSpinBox(QSpinBox):
    """无上下箭头 + 禁滚轮的数值框（用户偏好：直接输入，防滚轮误改）。"""

    def wheelEvent(self, ev):  # noqa: N802
        ev.ignore()


class _AnalysisWorker(QThread):
    """消息分析 Worker（主线程轮询控制 API 状态，不阻塞 UI）。

    流程：POST /analysis/query → 每 2s GET /analysis/query/status 直到
    state != running → 发 done(dict)。中途 GUI 关标签页则发 aborted。
    """

    done = Signal(object)

    def __init__(self, mw, payload: dict):
        super().__init__()
        self.mw = mw
        self.payload = payload

    def run(self):
        try:
            import urllib.request
            base = (f"http://{self.mw.cfg.get('CONTROL_API_HOST', '127.0.0.1')}:"
                    f"{self.mw.cfg.get('CONTROL_API_PORT', 8697)}")
            # 启动任务
            data = json.dumps(self.payload).encode()
            req = urllib.request.Request(
                base + "/analysis/query", data=data, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                r = json.loads(resp.read())
            if not r.get("ok"):
                self.done.emit({"state": "error", "error": r.get("error", "启动失败")})
                return
            run_id = r["run_id"]
            # 轮询状态
            while True:
                time.sleep(2)
                req = urllib.request.Request(
                    f"{base}/analysis/query/status?run_id={run_id}")
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        st = json.loads(resp.read())
                except Exception as e:
                    # 瞬时网络错误：重试 3 次再放弃（bot 重启/网络抖动）
                    for _ in range(3):
                        time.sleep(2)
                        try:
                            with urllib.request.urlopen(req, timeout=15) as resp:
                                st = json.loads(resp.read())
                            break
                        except Exception:
                            continue
                    else:
                        self.done.emit({"state": "error",
                                        "error": f"查询任务状态失败（bot 可能未运行/已重启）: {e}"})
                        return
                if st.get("state") != "running":
                    st["run_id"] = run_id
                    self.done.emit(st)
                    return
        except Exception as e:
            self.done.emit({"state": "error",
                            "error": f"{type(e).__name__}: {e}"})


def _analysis_btn_qss():
    return """
QPushButton {
    background: #0969da; border: 1px solid #0969da; border-radius: 4px;
    color: #ffffff; font-size: 13px; padding: 3px 14px;
}
QPushButton:hover { background: #0860c4; }
QPushButton:disabled { color: #b6bcc4; border-color: #e6eaee; background: #fafbfc; }
"""


class TabMessages(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._where = "1=1"          # 缓存当前搜索条件（分页复用）
        self._params: tuple = ()
        self._table = "message_archive"   # 当前查询表（撤回消息切 message_recalls）
        self._time_col = "created_at"     # 时间列（message_recalls 为 recalled_at）
        self._total = 0
        self._page = 1
        self._req_id = 0             # 防乱序：旧请求的结果不覆盖新页
        # 消息分析状态（08-21）
        self._ana_running = False
        self._ana_run_id = None
        self._ana_answer = ""
        self._ana_worker = None
        self._build()
        self._init_analysis_max()

    def _build(self):
        v = QVBoxLayout(self)

        # ---------------- 历史消息 ----------------
        self.gb_hist = gb_hist = QGroupBox("历史消息")
        v_box = QVBoxLayout(gb_hist)
        row1 = QHBoxLayout()
        self.ed_group = QLineEdit()
        self.ed_group.setPlaceholderText("群号（留空=全部）")
        self.ed_user = QLineEdit()
        self.ed_user.setPlaceholderText("用户QQ（留空=全部）")
        self.ed_kw = QLineEdit()
        self.ed_kw.setPlaceholderText("关键词（留空=全部）")
        self.cmb_type = QComboBox()
        # 08-21：统一 msg_kind 筛选（全部/文本/图片/语音/视频/消息记录/文件），
        # WHERE msg_kind=? 走 idx_archive_kind 索引；与媒体列徽标同源。
        # 「撤回消息」独立走 message_recalls 表（时间列 recalled_at），
        # 覆盖全部类型的撤回记录（文本/图片/语音/视频/转发/文件）
        self.cmb_type.addItems(["全部", "撤回消息", "文本", "图片", "语音", "视频", "消息记录", "文件"])
        row1.addWidget(self.ed_group)
        row1.addWidget(self.ed_user)
        row1.addWidget(self.ed_kw)
        row1.addWidget(self.cmb_type)

        # 时间范围（08-21：一行内文本框，留空=不限；
        #  支持 7-9 / 7-9 08:30 / 2026-07-09 等格式，只写日期含当天全天。
        #  提示语拆两段：起始框=可输时间示例，结束框=留空规则
        #  （不重复格式提示），各配最小宽度防截断）
        self.ed_t0 = QLineEdit()
        self.ed_t0.setPlaceholderText("起始（如 7-9 或 7-9 08:00）")
        self.ed_t1 = QLineEdit()
        self.ed_t1.setPlaceholderText("结束（留空=不限，日期=全天）")
        # 完整格式说明放 tooltip（悬停可见），提示语只放半句
        _time_tip = ("时间范围（留空=不限）\n"
                     "支持格式：7-9 / 7/9 / 07-9 / 2026-7-9 / 2026-07-09\n"
                     "可精确到时刻：7-9 08:00 / 2026-07-09 14:30:05\n"
                     "不写年份默认今年；只写日期=含当天全天（00:00~23:59:59）")
        self.ed_t0.setToolTip(_time_tip)
        self.ed_t1.setToolTip(_time_tip)
        # 两框等宽（08-21），宽度按较长提示语（结束框）留足余量
        self.ed_t0.setMinimumWidth(230)
        self.ed_t0.setMaximumWidth(230)
        self.ed_t1.setMinimumWidth(230)
        self.ed_t1.setMaximumWidth(230)
        row1.addWidget(self.ed_t0)
        row1.addWidget(QLabel("—"))
        row1.addWidget(self.ed_t1)

        self.btn_search = QPushButton("🔍 查询")
        row1.addWidget(self.btn_search)
        v_box.addLayout(row1)

        self.tbl_hist = QTableWidget(0, 7)
        self.tbl_hist.setHorizontalHeaderLabels(
            ["时间", "类型", "群/私聊", "用户", "昵称", "内容", "媒体"])
        self.tbl_hist.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tbl_hist.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_hist.setAlternatingRowColors(True)
        v_box.addWidget(self.tbl_hist)
        v_box.addWidget(QLabel(
            "提示：点击内容列可选中复制；内容过长截断显示；"
            "双击图片/语音/视频/消息记录行可弹窗查看，双击文件行打开所在文件夹"))

        # ---------------- 分页条（右下角，08-21）----------------
        pbar = QHBoxLayout()
        self.lb_pageinfo = QLabel("未查询")
        self.lb_pageinfo.setStyleSheet("color: #656d76;")
        pbar.addWidget(self.lb_pageinfo)
        pbar.addStretch(1)

        def _pg_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFixedHeight(26)
            b.setMinimumWidth(30)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_PG_BTN_QSS)
            return b

        self.btn_first = _pg_btn("«")
        self.btn_prev = _pg_btn("‹")
        self._num_btns: list[QPushButton] = []   # 动态页码按钮
        self.btn_next = _pg_btn("›")
        self.btn_last = _pg_btn("»")
        self.ed_jump = QLineEdit()
        self.ed_jump.setPlaceholderText("页码")
        self.ed_jump.setFixedWidth(54)
        self.ed_jump.setAlignment(Qt.AlignCenter)
        self.btn_jump = QPushButton("跳转")
        self.btn_jump.setFixedHeight(26)

        self.btn_first.clicked.connect(lambda: self._goto_page(1))
        self.btn_prev.clicked.connect(lambda: self._goto_page(self._page - 1))
        self.btn_next.clicked.connect(lambda: self._goto_page(self._page + 1))
        self.btn_last.clicked.connect(lambda: self._goto_page(self._total_pages()))
        self.btn_jump.clicked.connect(self._jump)
        self.ed_jump.returnPressed.connect(self._jump)

        for b in (self.btn_first, self.btn_prev):
            pbar.addWidget(b)
        self._num_container = QHBoxLayout()     # 页码按钮动态容器
        pbar.addLayout(self._num_container)
        for b in (self.btn_next, self.btn_last):
            pbar.addWidget(b)
        pbar.addWidget(QLabel("跳转"))
        pbar.addWidget(self.ed_jump)
        pbar.addWidget(self.btn_jump)
        v_box.addLayout(pbar)
        v.addWidget(gb_hist, 1)   # 历史消息区占满剩余高度，分析区贴底

        # ---------------- 消息分析（AI，08-21 新增）----------------
        # 把上方筛选出的全部记录（非当前页）喂给 LLM，走 /查询 同款
        # Map-Reduce（bot 控制 API /analysis/query 后台跑，prompt 单一来源）。
        # 上限可编辑（config.yaml analysis.max_rows，超出取最近 N 条并弹确认）。
        self.gb_ana = gb_ana = QGroupBox("📊 消息分析（AI）")
        a_v = QVBoxLayout(gb_ana)
        a_v.setContentsMargins(10, 10, 10, 10)
        a_v.setSpacing(6)
        # 行1：问题输入 + 上限 + 按钮
        a_row = QHBoxLayout()
        a_row.setSpacing(8)
        self.ed_q = QLineEdit()
        self.ed_q.setPlaceholderText("分析问题，例如：大家在讨论什么？谁最近发言最多？")
        self.ed_q.setToolTip("对上方筛选出的全部记录提问（不是当前页，是筛选命中的全部）")
        a_row.addWidget(self.ed_q, 1)
        a_row.addWidget(QLabel("上限"))
        self.sp_max = _NoArrowSpinBox()
        self.sp_max.setRange(100, 1000000)
        self.sp_max.setButtonSymbols(QAbstractSpinBox.NoButtons)   # 无上下箭头（Qt.NoButton 不存在，e2e 抓到）
        self.sp_max.setFixedWidth(96)
        self.sp_max.setToolTip("单次分析条数上限。筛选结果超过上限时弹确认，确认后只取时间上最近的 N 条。修改会写入 config.yaml")
        a_row.addWidget(self.sp_max)
        self.btn_ana = QPushButton("📊 开始分析")
        self.btn_ana.setFixedHeight(28)
        self.btn_ana.setStyleSheet(_analysis_btn_qss())
        self.btn_ana.setEnabled(False)
        self.btn_ana.setToolTip("请先在上方筛选出消息")
        a_row.addWidget(self.btn_ana)
        a_v.addLayout(a_row)
        # 行2：状态 + 结果区（内滚）+ 操作
        self.lb_ana = QLabel("未分析（先在上方筛选出消息，再输入问题点「开始分析」）")
        self.lb_ana.setStyleSheet("color: #656d76; font-size: 13px;")
        a_v.addWidget(self.lb_ana)
        self.ana_view = QTextBrowser()
        self.ana_view.setOpenExternalLinks(False)
        self.ana_view.setStyleSheet("font-size: 13px;")
        self.ana_view.setMinimumHeight(80)    # 08-21 用户要求增高消息列表：结果区压缩（内滚仍可看全文）
        self.ana_view.setMaximumHeight(150)
        a_v.addWidget(self.ana_view)
        a_bar = QHBoxLayout()
        a_bar.addStretch(1)
        self.btn_ana_copy = QPushButton("复制答案")
        self.btn_ana_copy.setEnabled(False)
        self.btn_ana_clear = QPushButton("清空")
        self.btn_ana_clear.setEnabled(False)
        a_bar.addWidget(self.btn_ana_copy)
        a_bar.addWidget(self.btn_ana_clear)
        a_v.addLayout(a_bar)
        v.addWidget(gb_ana)

        # 绑定
        self.btn_search.clicked.connect(self._search)
        self.btn_ana.clicked.connect(self._start_analysis)
        self.btn_ana_copy.clicked.connect(self._copy_answer)
        self.btn_ana_clear.clicked.connect(self._clear_analysis)
        self.sp_max.valueChanged.connect(self._save_analysis_max)
        # 08-21：双击行查看媒体（image/voice/video/forward 弹窗，file 打开所在
        # 文件夹，text 无动作）
        self.tbl_hist.cellDoubleClicked.connect(self._on_row_double_clicked)

    # ------------------------------------------------------------
    #  历史消息（分页懒加载）
    # ------------------------------------------------------------
    def _total_pages(self) -> int:
        return max(1, (self._total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _search(self):
        group = self.ed_group.text().strip()
        user = self.ed_user.text().strip()
        kw = self.ed_kw.text().strip()
        mtype = self.cmb_type.currentText()

        # 08-21：「撤回消息」查独立表 message_recalls（时间列 recalled_at，
        # 含全部类型撤回记录）；其余类型查 message_archive
        if mtype == "撤回消息":
            self._table = "message_recalls"
            self._time_col = "recalled_at"
        else:
            self._table = "message_archive"
            self._time_col = "created_at"

        sql_where = "1=1"
        params = []
        if group:
            sql_where += " AND target_id = ?"
            params.append(int(group))
        if user:
            sql_where += " AND user_id = ?"
            params.append(int(user))
        # 08-21：统一 msg_kind 筛选（与媒体列徽标同源，索引查询）
        _KIND_SQL = {
            "文本": "text", "图片": "image", "语音": "voice",
            "视频": "video", "消息记录": "forward", "文件": "file",
        }
        if mtype in _KIND_SQL:
            sql_where += " AND msg_kind = ?"
            params.append(_KIND_SQL[mtype])
        if kw:
            sql_where += " AND content LIKE ?"
            params.append(f"%{kw}%")

        # 时间范围（08-21：文本框，留空=不限；格式化解析，见 _parse_time）
        t0, err = _parse_time(self.ed_t0.text().strip(), is_end=False)
        if err:
            self.mw.statusBar().showMessage(err, 5000)
            return
        t1, err = _parse_time(self.ed_t1.text().strip(), is_end=True)
        if err:
            self.mw.statusBar().showMessage(err, 5000)
            return
        if t0 is not None and t1 is not None and t0 > t1:
            self.mw.statusBar().showMessage("起始时间晚于结束时间", 5000)
            return
        if t0 is not None:
            sql_where += f" AND {self._time_col} >= ?"
            params.append(t0)
        if t1 is not None:
            sql_where += f" AND {self._time_col} <= ?"
            params.append(t1)

        self._where = sql_where
        self._params = tuple(params)
        # 新搜索回到第 1 页。注意不能走 _goto_page——它的 _total==0 守卫
        # 会把首次查询（此时还没数据）挡掉（08-21 实测踩坑），直接启动加载
        self._page = 1
        self.ed_jump.clear()
        self._start_page_load(1)

    def _goto_page(self, page: int):
        if self._total == 0:
            return
        page = max(1, min(page, self._total_pages()))
        self._page = page
        self.ed_jump.setText(str(page))
        self._start_page_load(page)

    def _start_page_load(self, page: int):
        self._req_id += 1
        req = self._req_id
        w = Worker(self._load_page, page, req)
        w.finished_ok.connect(lambda res: self._on_page_loaded(res, req))
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"分页查询失败: {e}"))
        w.start()
        self.mw._track(w)
        self.mw.statusBar().showMessage(f"加载第 {page} 页…")

    def _load_page(self, page: int, req: int):
        """后台线程：查总数 + 当前页数据（参数化 SQL；OFFSET 为受控 int，安全）

        08-21：撤回消息查 message_recalls（时间列 recalled_at，别名对齐
        created_at 供渲染层复用；无 has_voice 列，徽标走 msg_kind 兜底）
        """
        off = (page - 1) * _PAGE_SIZE
        table = self._table
        if table == "message_recalls":
            # message_recalls 无 raw_message 列 → 空串兜底（file 类型双击时
            # 无文件名/URL，走"未知文件名"分支）
            cols = ("message_id, message_type, target_id, user_id, nickname, "
                    "content, has_image, msg_kind, recalled_at AS created_at, "
                    "1 AS is_recall, '' AS raw_message")
            order_by = "recalled_at DESC, id DESC"
        else:
            cols = ("message_id, message_type, target_id, user_id, nickname, "
                    "content, has_image, has_voice, msg_kind, created_at, "
                    "0 AS is_recall, raw_message")
            order_by = "created_at DESC, message_id DESC"
        total = api_client.query(
            self.mw.cfg, "chat",
            f"SELECT COUNT(*) AS n FROM {table} WHERE {self._where}",
            self._params)[0]["n"]
        rows = api_client.query(
            self.mw.cfg, "chat",
            (f"SELECT {cols} FROM {table} "
             f"WHERE {self._where} ORDER BY {order_by} "
             f"LIMIT {_PAGE_SIZE} OFFSET {off}"),
            self._params)
        return total, rows

    def _on_page_loaded(self, res, req: int):
        if req != self._req_id:
            return  # 已有更新的翻页请求，丢弃过期结果
        total, rows = res
        self._total = total
        self._fill_hist(rows)
        self._update_pagebar()
        self._update_ana_state()
        pages = self._total_pages()
        self.mw.statusBar().showMessage(f"第 {self._page}/{pages} 页 · 共 {total} 条")

    def _jump(self):
        t = self.ed_jump.text().strip()
        if not t.isdigit():
            self.mw.statusBar().showMessage("请输入页码数字")
            return
        self._goto_page(int(t))

    # ------------------------------------------------------------
    #  渲染
    # ------------------------------------------------------------
    # msg_kind → 媒体列徽标（08-21：与筛选下拉同源；text 不显示）
    _KIND_BADGE = {
        "image": "图", "voice": "音", "video": "影",
        "forward": "转", "file": "档",
    }

    def _fill_hist(self, rows):
        self.tbl_hist.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # 媒体列优先按 msg_kind 统一徽标；旧行兜底 has_image/has_voice
            badge = self._KIND_BADGE.get(r.get("msg_kind") or "text")
            if badge is None:
                legacy = []
                if r.get("has_image"):
                    legacy.append("图")
                if r.get("has_voice"):
                    legacy.append("音")
                badge = "/".join(legacy) or ""
            # 08-21：撤回行内容加「↩️ 撤回：」前缀（时间列是撤回时刻，
            # 不是发送时刻，前缀+tooltip 双重说明避免歧义）
            is_recall = bool(r.get("is_recall"))
            content = r["content"] or ""
            shown = ("↩️ 撤回：" + content) if is_recall else content
            vals = [
                _ts(r["created_at"]),
                r["message_type"],
                str(r["target_id"]),
                str(r["user_id"]),
                r["nickname"],
                shown[:300],
                badge or "-",
            ]
            for j, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if j == 5:
                    # tooltip 给原文（撤回行加时刻说明），内容列截断显示
                    tip = content
                    if is_recall:
                        tip = f"[撤回于 {vals[0]}]\n{content}"
                    item.setToolTip(tip)
                if j == 0:
                    # 08-21：缓存整行数据供双击查看媒体（msg_kind/is_recall/
                    # message_id/target_id/raw_message 等），不额外查库
                    item.setData(Qt.UserRole, r)
                self.tbl_hist.setItem(i, j, item)

    # ------------------------------------------------------------
    #  双击行 → 媒体查看器（08-21）
    # ------------------------------------------------------------
    def _on_row_double_clicked(self, row: int, col: int):
        item = self.tbl_hist.item(row, 0)
        if item is None:
            return
        r = item.data(Qt.UserRole)
        if not isinstance(r, dict):
            return
        kind = r.get("msg_kind") or "text"
        # 纯文本无动作
        if kind == "text":
            return
        if kind == "file":
            # file：打开所在文件夹并高亮定位（未落盘→打开存档目录+信息弹窗）
            media_viewer._open_file_row(self.mw, r, r.get("raw_message") or "")
            return
        # image/voice/video/forward：弹窗查看
        media_viewer.open_media(self.mw, r, r.get("raw_message") or "")

    def _update_pagebar(self):
        pages = self._total_pages()
        self.lb_pageinfo.setText(f"共 {self._total} 条 · {pages} 页")
        self.btn_first.setEnabled(self._page > 1)
        self.btn_prev.setEnabled(self._page > 1)
        self.btn_next.setEnabled(self._page < pages)
        self.btn_last.setEnabled(self._page < pages)

        # 页码窗口：总页数 <= 9 全显示，否则显示 首/末/当前±2 + 省略号
        seq = self._page_seq(self._page, pages)
        # 清空旧按钮
        while self._num_btns:
            b = self._num_btns.pop()
            self._num_container.removeWidget(b)
            b.deleteLater()
        for n in seq:
            b = QPushButton("…" if n is None else str(n))
            b.setFixedHeight(26)
            b.setMinimumWidth(30)
            b.setStyleSheet(_PG_BTN_QSS)
            if n is None:
                b.setEnabled(False)
                b.setCursor(Qt.ArrowCursor)
            else:
                b.setProperty("on", "true" if n == self._page else "false")
                b.setCursor(Qt.PointingHandCursor)
                target = n
                b.clicked.connect(lambda _=False, t=target: self._goto_page(t))
            self._num_container.addWidget(b)
            self._num_btns.append(b)

    @staticmethod
    def _page_seq(current: int, pages: int):
        """生成页码序列（None = 省略号），常规排布：首、末、当前±2。"""
        if pages <= 9:
            return list(range(1, pages + 1))
        want = {1, 2, pages - 1, pages,
                current - 2, current - 1, current, current + 1, current + 2}
        seq = []
        prev = 0
        for n in sorted(n for n in want if 1 <= n <= pages):
            if n - prev > 1:
                seq.append(None)
            seq.append(n)
            prev = n
        return seq

    # ------------------------------------------------------------
    #  消息分析（AI，08-21 新增）
    # ------------------------------------------------------------
    def _init_analysis_max(self):
        """从 config.yaml 读 analysis.max_rows（读不到用默认值），填入上限框。"""
        val = _ANALYSIS_MAX_DEFAULT
        try:
            y = api_client.load_yaml()
            v = (y.get("analysis") or {}).get("max_rows")
            if isinstance(v, int) and v > 0:
                val = v
        except Exception:
            pass
        # 防 valueChanged 触发写回（初始值与文件一致时无副作用，但显式断开更安全）
        self.sp_max.blockSignals(True)
        self.sp_max.setValue(min(max(val, 100), 1000000))
        self.sp_max.blockSignals(False)

    def _save_analysis_max(self, v: int):
        """上限框改值 → 写回 config.yaml（analysis.max_rows）。"""
        try:
            y = api_client.load_yaml()
            y.setdefault("analysis", {})["max_rows"] = int(v)
            api_client.save_yaml(y)
            self.mw.statusBar().showMessage(f"分析上限已保存：{v} 条", 3000)
        except Exception as e:
            self.mw.statusBar().showMessage(f"保存上限失败: {e}", 5000)

    def _update_ana_state(self):
        """按当前筛选结果/运行态刷新分析按钮可用性。"""
        if self._ana_running:
            self.btn_ana.setEnabled(False)
            self.btn_ana.setText("⏳ 分析中…")
            return
        self.btn_ana.setText("📊 开始分析")
        self.btn_ana.setEnabled(self._total > 0)
        self.btn_ana.setToolTip(
            "" if self._total > 0 else "请先在上方筛选出消息")

    def _build_ana_scope_desc(self) -> str:
        """输入源描述（拼进 Map prompt 的 scope_desc）。"""
        mtype = self.cmb_type.currentText()
        if mtype == "撤回消息":
            return "上方筛选出的已撤回聊天记录（以下消息均已被撤回，时间为撤回时刻）"
        return "上方筛选出的聊天记录"

    def _start_analysis(self):
        question = self.ed_q.text().strip()
        if not question:
            self.mw.statusBar().showMessage("请输入分析问题", 3000)
            return
        if self._total <= 0:
            self.mw.statusBar().showMessage("没有可分析的记录（请先筛选）", 3000)
            return
        max_rows = self.sp_max.value()

        # 超上限 → 弹确认取最近 N 条（用户 08-21 定稿）
        if self._total > max_rows:
            if not self.mw.confirm(
                    "分析条数超上限",
                    f"筛选出 {self._total} 条，超过上限 {max_rows} 条。\n"
                    f"确认后将只取时间上最近的 {max_rows} 条进行分析。"):
                return

        # 取数（后台线程，防大筛选集阻塞 UI）。max_rows 主线程先读好传进去
        # （QSpinBox 跨线程访问不安全）
        self._ana_running = True
        self._ana_run_id = None
        self._update_ana_state()
        self.lb_ana.setText("正在读取筛选结果…")
        w = Worker(self._fetch_analysis_rows, question, max_rows)
        w.finished_ok.connect(self._on_rows_fetched)
        w.finished_err.connect(lambda e: self._on_ana_error(
            f"读取筛选结果失败: {e}"))
        w.start()
        self.mw._track(w)

    def _fetch_analysis_rows(self, question: str, max_rows: int):
        """后台线程：按当前筛选条件全量取数（ASC，尾部=最近）+ 超上限截尾。

        返回 (rows_asc, used, total, max_rows)。rows 只取 LLM 需要的 4 列
        （user_id/nickname/content/created_at），控制 POST 体积。
        """
        table = self._table
        where = self._where
        params = self._params
        total = api_client.query(
            self.mw.cfg, "chat",
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where}",
            params)[0]["n"]
        # 超上限取最近 N 条（ASC 尾部）：内层按时间倒序 LIMIT N 再外层翻回 ASC
        if total > max_rows:
            rows = api_client.query(
                self.mw.cfg, "chat",
                (f"SELECT user_id, nickname, content, {self._time_col} AS created_at "
                 f"FROM (SELECT user_id, nickname, content, {self._time_col} "
                 f"FROM {table} WHERE {where} "
                 f"ORDER BY {self._time_col} DESC, id DESC LIMIT {max_rows}) "
                 f"ORDER BY created_at ASC"),
                params)
        else:
            rows = api_client.query(
                self.mw.cfg, "chat",
                (f"SELECT user_id, nickname, content, {self._time_col} AS created_at "
                 f"FROM {table} WHERE {where} ORDER BY {self._time_col} ASC, id ASC"),
                params)
        return rows, min(total, max_rows), total, max_rows

    def _on_rows_fetched(self, res):
        rows, used, total, max_rows = res
        question = self.ed_q.text().strip()
        payload = {
            "question": question,
            "rows": rows,
            "max_rows": max_rows,
            "meta": {
                "scope_desc": self._build_ana_scope_desc(),
                "used": used, "total": total,
                "table": self._table,
                "where": self._where,
                "params": list(self._params),
            },
        }
        self._ana_worker = _AnalysisWorker(self.mw, payload)
        self._ana_worker.done.connect(self._on_ana_done)
        self.mw._track(self._ana_worker)
        self.lb_ana.setText(
            f"分析中…（{used}/{total} 条"
            + (f"，超出上限取最近 {used} 条" if total > max_rows else "")
            + "，Map-Reduce 分批处理，通常需要数分钟）")
        self.mw.statusBar().showMessage(f"消息分析已启动（{used} 条）")
        self._ana_worker.start()

    def _on_ana_done(self, st: dict):
        self._ana_running = False
        self._update_ana_state()
        state = st.get("state")
        if state == "done":
            answer = (st.get("answer") or "").strip()
            self._ana_run_id = st.get("run_id")
            if answer:
                self._ana_answer = answer
                # 实际分析条数：不截断=筛选总数，截断=上限值
                # （超上限提示用 GUI 侧已知量 self._total vs 上限框当前值）
                truncated = self._total > self.sp_max.value()
                used = self.sp_max.value() if truncated else self._total
                # 不截断时两个数相同，括号冗余 → 只留截断场景的说明
                header = (f"<div style='color:#656d76; font-size:12px; "
                          f"margin-bottom:8px;'>分析完成 · 输入 {used} 条"
                          + (f"（筛选共 {self._total} 条，超上限取最近 {self.sp_max.value()} 条）"
                             if truncated else "")
                          + "</div>")
                self.ana_view.setHtml(
                    header + self._ana_html(answer))
                self.btn_ana_copy.setEnabled(True)
                self.btn_ana_clear.setEnabled(True)
                self.lb_ana.setText(f"✅ 分析完成（{self._total} 条"
                                    + (f"，取最近 {self.sp_max.value()}" if truncated else "") + "）")
                self.mw.statusBar().showMessage("消息分析完成", 5000)
            else:
                # done 但无答案 = "未找到相关信息"（AnalysisError 走 done）
                err = st.get("error") or "未找到相关信息"
                self.lb_ana.setText(f"⚠️ {err}")
                self.ana_view.setHtml(
                    f"<div style='color:#9a6700; font-size:13px; padding:16px;'>"
                    f"⚠️ {self._esc(err)}</div>")
                self.btn_ana_copy.setEnabled(False)
        elif state == "error":
            self._on_ana_error(st.get("error", "分析失败"))

    def _on_ana_error(self, msg: str):
        self._ana_running = False
        self._update_ana_state()
        self.lb_ana.setText(f"❌ {msg}")
        self.ana_view.setHtml(
            f"<div style='color:#cf222e; font-size:13px; padding:16px;'>"
            f"❌ {self._esc(msg)}</div>")
        self.btn_ana_copy.setEnabled(False)
        self.mw.statusBar().showMessage(msg, 6000)

    def _copy_answer(self):
        if not self._ana_answer:
            return
        from PySide6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        if cb:
            cb.setText(self._ana_answer)
            self.mw.statusBar().showMessage("已复制分析答案到剪贴板", 3000)

    def _clear_analysis(self):
        self._ana_answer = ""
        self.ana_view.clear()
        self.lb_ana.setText("未分析")
        self.btn_ana_copy.setEnabled(False)
        self.btn_ana_clear.setEnabled(False)

    @staticmethod
    def _esc(s: str) -> str:
        import html
        return html.escape(str(s))

    @staticmethod
    def _ana_html(answer: str) -> str:
        import html
        return (f"<div style='white-space:pre-wrap; font-size:13px; "
                f"color:#1f2328; line-height:1.6;'>"
                f"{html.escape(answer)}</div>")
