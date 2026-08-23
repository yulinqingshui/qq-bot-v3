"""
truth_dare_settings_dialogs.py — 真心话大冒险页 4 个设置弹窗
=========================================================
- ⚙️ 题库规则：pool 段 7 项（补充阈值/批次数/防重历史/人设截断）
- 🤖 LLM 调用参数：3 阶段 × {max_tokens, temperature, thinking, json_mode, timeout}
  + llm_retries + priority（只读展示，全局队列参数）
- 🎮 自动模式行为：game 段 4 项（大冒险概率/自动踢人/出题延迟/默认色度）
- 📝 出题提示词：32 组提示词编辑器（默认值来自 core/truth_dare_prompts.py，
  用户定制存 config.yaml 的 truth_dare.prompts，每项可单独恢复默认）

保存流程（与 persona 弹窗一致）：改 mw.yaml_cfg → api_client.save_yaml 写盘
→ POST /config 通知 bot 热重载 → GUI 侧同步刷新 TD_CFG / TD_PROMPT_* 键。
恢复默认：前三窗有总「恢复默认设置」按钮；提示词窗每项可恢复默认。
"""

import copy
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSpinBox, QDoubleSpinBox,
    QTableWidget, QTableWidgetItem,
    QHeaderView, QVBoxLayout, QAbstractItemView, QMessageBox,
)

import api_client
from worker import Worker
from widgets import (
    NoArrowSpinBox, NoArrowDoubleSpinBox, no_wheel_spin, int_spin, float_spin,
)


# ------------------------------------------------------------
#  默认值 / 结构常量
# ------------------------------------------------------------
from core.config import DEFAULTS
import core.truth_dare_prompts as tdpp

_TD_DEFAULTS = DEFAULTS["truth_dare"]

# 题库规则 7 项：(key, 显示名, 说明, 范围)
_POOL_KEYS = [
    ("persona_threshold", "人设库补充阈值（道）",
     "单玩家单档位题库低于 N 道时触发后台补充", 1, 200),
    ("persona_batch_size", "人设库每批道数",
     "人设题库每次批量生成 N 道（循环补充直到超过阈值）", 1, 50),
    ("generic_threshold", "通用库补充阈值（道）",
     "通用题库单档位低于 N 道时触发后台补充", 1, 500),
    ("generic_batch_size", "通用库每批道数",
     "通用题库每次批量生成 N 道（循环补充直到超过阈值）", 1, 100),
    ("anti_dup_history", "防重历史上限（道）",
     "出题时注入的已有题目条数（DB 抓取上限，通用题库同此值）", 0, 500),
    ("prompt_history", "人设出题历史注入（道）",
     "人设题库出题（批量+现场降级）prompt 注入的历史条数", 0, 500),
    ("persona_text_max_chars", "人设文本截断（字）",
     "出题/入库时人设文本超过 N 字截断", 0, 10000),
]

# LLM 阶段（表格行）：(key, 显示名)
_LLM_STAGES = [
    ("batch_persona", "人设题库批量出题"),
    ("batch_generic", "通用题库批量出题"),
    ("live", "现场降级出题"),
]
_THINKING_OPTS = ["on", "off", "low", "max"]

# 自动模式行为 4 项：(key, 显示名, 说明)
_GAME_KEYS = [
    ("dare_probability", "大冒险概率（%）",
     "混合模式下 AI 出题时大冒险的占比（群内 /概率 可单游戏覆盖，此处为默认值）"),
    ("auto_kick_threshold", "自动踢人阈值（轮）",
     "连续 N 轮被抽到未回答自动请出游戏"),
    ("bg_delay_seconds", "出题延迟（秒）",
     "/下一轮 后延迟 N 秒发 AI 出题消息（等骰子消息先处理）"),
    ("default_spiciness", "新游戏默认色度",
     "新开局未手动设置色度时的默认档位（0-6；群内 /色色程度 可单游戏覆盖）"),
]


class _BaseDialog(QDialog):
    """四个弹窗的公共基类：读 truth_dare 段、保存落盘、热重载、刷新 GUI cfg。"""

    def __init__(self, mw, title: str, width: int = 620):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        self._layout = v

    # ---------- truth_dare 段读写 ----------
    def td_cfg(self) -> dict:
        """yaml 当前 truth_dare 段（与 DEFAULTS 深合并，缺项回退默认）。"""
        merged = copy.deepcopy(_TD_DEFAULTS)
        y = (self.mw.yaml_cfg or {}).get("truth_dare") or {}
        for k, v in y.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged

    def _yaml_td(self) -> dict:
        """mw.yaml_cfg 中实际落盘的 truth_dare 段（不合并默认；保存只写用户改过的键）。"""
        y = self.mw.yaml_cfg
        if not isinstance(y.get("truth_dare"), dict):
            y["truth_dare"] = {}
        return y["truth_dare"]

    # ---------- 保存 ----------
    def _commit(self, what: str):
        """写 yaml → POST /config 热重载 → 刷新 GUI 侧 TD* 键。"""
        try:
            api_client.save_yaml(self.mw.yaml_cfg)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"config.yaml 写入失败：{e}")
            return
        # GUI 侧同步（不碰密钥等其他键，只刷新 TD_*）
        try:
            from core.config import flatten_yaml_tree
            fresh = flatten_yaml_tree(api_client.load_yaml())
        except Exception:
            fresh = {}
        for k in [k for k in list(self.mw.cfg) if k.startswith("TD_PROMPT_")]:
            self.mw.cfg.pop(k)
        self.mw.cfg["TD_CFG"] = fresh.get("TD_CFG", self.td_cfg())
        for k, v in fresh.items():
            if k.startswith("TD_PROMPT_"):
                self.mw.cfg[k] = v
        self.mw.yaml_cfg = api_client.load_yaml()

        def _do():
            return api_client.reload_config(self.mw.cfg)

        w = Worker(_do)
        w.finished_ok.connect(lambda r: self._on_reload_ok(r, what))
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(
            f"⚠️ bot 未运行或热重载失败（{e}）：配置已写入 config.yaml，下次启动生效"))
        w.start()
        self.mw._track(w)
        self.close()

    def _on_reload_ok(self, report: dict, what: str):
        n = len(report.get("applied") or [])
        self.mw.statusBar().showMessage(f"✅ {what}已保存并热重载生效（{n} 项）", 8000)

    def _bottom_bar(self, on_save) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addStretch(1)
        return bar


# ============================================================
#  ① 题库规则
# ============================================================
class PoolRulesDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "⚙️ 真心话大冒险 · 题库规则", 600)
        self._build()

    def _build(self):
        tc = self.td_cfg()
        pool = tc.get("pool", {})
        form = QFormLayout()
        form.setSpacing(12)
        self.spins = {}
        for key, label, hint, lo, hi in _POOL_KEYS:
            row = QHBoxLayout()
            row.setSpacing(8)
            spin = int_spin(pool.get(key, _TD_DEFAULTS["pool"][key]), lo, hi)
            row.addWidget(spin)
            hint_lbl = QLabel(hint)
            hint_lbl.setWordWrap(True)  # 防长 hint 贴边硬切（08-22 巡检发现）
            row.addWidget(hint_lbl)
            row.addStretch(1)
            form.addRow(QLabel(label), row)
            self.spins[key] = spin
        self._layout.addLayout(form)

        hint = QLabel("说明：阈值决定「题库低于多少道时触发后台补充」，批次数决定每次让 LLM 出多少道"
                      "（循环补充直到超过阈值）。改完保存后，下一次触发的补充任务即使用新值。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = self._bottom_bar(None)
        btn_reset = QPushButton("↺ 恢复默认设置")
        btn_reset.clicked.connect(self._reset_all)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        btn_save = QPushButton("💾 保存并生效")
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(self._save)
        bar.addWidget(btn_reset)
        bar.addWidget(btn_cancel)
        bar.addWidget(btn_save)
        self._layout.addLayout(bar)

    def _reset_all(self):
        for key, _l, _h, _lo, _hi in _POOL_KEYS:
            self.spins[key].setValue(int(_TD_DEFAULTS["pool"][key]))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_td()
        p = {}
        for key, _l, _h, _lo, _hi in _POOL_KEYS:
            v = self.spins[key].value()
            if v != _TD_DEFAULTS["pool"][key]:
                p[key] = v
        if p:
            merged = dict((y.get("pool") or {}))
            merged.update(p)
            for key in list(merged):
                if key not in p and merged[key] == _TD_DEFAULTS["pool"].get(key):
                    merged.pop(key)
            y["pool"] = merged
        else:
            y.pop("pool", None)
        self._commit("题库规则")


# ============================================================
#  ② LLM 调用参数
# ============================================================
class LLMParamsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🤖 真心话大冒险 · LLM 调用参数", 780)
        self._build()

    def _build(self):
        tc = self.td_cfg()
        stages = tc.get("llm", {}) or {}

        tbl = QTableWidget(len(_LLM_STAGES), 6)
        tbl.setHorizontalHeaderLabels(["阶段", "max_tokens", "温度", "thinking", "json_mode", "超时(s)"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 6):
            tbl.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setMinimumSectionSize(90)
        tbl.verticalHeader().setVisible(False)
        tbl.setFixedHeight(len(_LLM_STAGES) * 36 + 24)
        self.cells = {}
        for r, (key, name) in enumerate(_LLM_STAGES):
            d = stages.get(key) or _TD_DEFAULTS["llm"][key]
            tbl.setItem(r, 0, QTableWidgetItem(name))
            tbl.item(r, 0).setFlags(Qt.NoItemFlags)
            # max_tokens
            mt = NoArrowSpinBox(); mt.setRange(256, 262144)
            mt.setValue(int(d["max_tokens"]))
            # temperature
            tp = NoArrowDoubleSpinBox(); tp.setRange(0.0, 2.0); tp.setDecimals(2); tp.setSingleStep(0.05)
            tp.setValue(float(d["temperature"]))
            # thinking
            tk = QComboBox(); tk.addItems(_THINKING_OPTS)
            tk.setCurrentText(str(d["thinking"]))
            # json_mode
            jm = QCheckBox()
            jm.setChecked(bool(d["json_mode"]))
            # timeout
            to = NoArrowSpinBox(); to.setRange(30, 3600)
            to.setValue(int(d.get("timeout", 1800)))
            for c, w in [(1, mt), (2, tp), (3, tk), (4, jm), (5, to)]:
                if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                    w = no_wheel_spin(w)
                tbl.setCellWidget(r, c, w)
                w.setMinimumWidth(90)
            self.cells[key] = (mt, tp, tk, jm, to)
        self.tbl = tbl
        self._layout.addWidget(tbl)

        # 全局项：重试 + 优先级（只读）
        form = QFormLayout()
        self.spin_retries = int_spin(tc.get("llm_retries", 1), 0, 20)
        form.addRow("LLM 解析失败重试次数", self.spin_retries)
        lbl_prio = QLabel(f"-1（最高，高于用户指令 0 / 定时任务 1）")
        lbl_prio.setStyleSheet("color: #667085;")
        form.addRow("LLM 队列优先级（只读）", lbl_prio)
        self._layout.addLayout(form)

        hint = QLabel("thinking：on=后端默认（DeepSeek 默认 max）/ off=关思考 / low·max=reasoning_effort。"
                      "max_tokens 是思考链+正文共享预算，过小会截断；"
                      "出题建议保持 json_mode 开启（从机制上杜绝评审/序号文本混入）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = self._bottom_bar(None)
        btn_reset = QPushButton("↺ 恢复默认设置")
        btn_reset.clicked.connect(self._reset_all)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        btn_save = QPushButton("💾 保存并生效")
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(self._save)
        bar.addWidget(btn_reset)
        bar.addWidget(btn_cancel)
        bar.addWidget(btn_save)
        self._layout.addLayout(bar)

    def _reset_all(self):
        d = _TD_DEFAULTS
        for key, (mt, tp, tk, jm, to) in self.cells.items():
            v = d["llm"][key]
            mt.setValue(int(v["max_tokens"])); tp.setValue(float(v["temperature"]))
            tk.setCurrentText(str(v["thinking"])); jm.setChecked(bool(v["json_mode"]))
            to.setValue(int(v.get("timeout", 1800)))
        self.spin_retries.setValue(int(d.get("llm_retries", 1)))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_td()
        llm = {}
        for key, (mt, tp, tk, jm, to) in self.cells.items():
            d = {
                "max_tokens": mt.value(),
                "temperature": tp.value(),
                "thinking": tk.currentText(),
                "json_mode": jm.isChecked(),
                "timeout": to.value(),
            }
            if d == _TD_DEFAULTS["llm"][key]:
                continue  # 与默认相同不写
            llm[key] = d
        if llm:
            merged = dict((y.get("llm") or {}))
            merged.update(llm)
            for key in list(merged):
                if key not in llm and merged[key] == _TD_DEFAULTS["llm"].get(key):
                    merged.pop(key)
            y["llm"] = merged if merged else None
        else:
            y.pop("llm", None)
        if y.get("llm") is None:
            y.pop("llm", None)
        if self.spin_retries.value() != _TD_DEFAULTS.get("llm_retries", 1):
            y["llm_retries"] = self.spin_retries.value()
        else:
            y.pop("llm_retries", None)
        self._commit("LLM 调用参数")


# ============================================================
#  ③ 自动模式行为
# ============================================================
class GameRulesDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🎮 真心话大冒险 · 自动模式行为", 600)
        self._build()

    def _build(self):
        tc = self.td_cfg()
        game = tc.get("game", {})
        form = QFormLayout()
        form.setSpacing(14)
        self.spins = {}
        # dare_probability: 0-100 int
        s = int_spin(game.get("dare_probability", _TD_DEFAULTS["game"]["dare_probability"]), 0, 100)
        row = QHBoxLayout(); row.setSpacing(8)
        h = QLabel(_GAME_KEYS[0][2]); h.setWordWrap(True)
        row.addWidget(s); row.addWidget(h); row.addStretch(1)
        form.addRow(QLabel(_GAME_KEYS[0][1]), row)
        self.spins["dare_probability"] = s
        # auto_kick_threshold: >=1 int
        s = int_spin(game.get("auto_kick_threshold", _TD_DEFAULTS["game"]["auto_kick_threshold"]), 1, 50)
        row = QHBoxLayout(); row.setSpacing(8)
        h = QLabel(_GAME_KEYS[1][2]); h.setWordWrap(True)
        row.addWidget(s); row.addWidget(h); row.addStretch(1)
        form.addRow(QLabel(_GAME_KEYS[1][1]), row)
        self.spins["auto_kick_threshold"] = s
        # bg_delay_seconds: 0-60 float
        s = float_spin(game.get("bg_delay_seconds", _TD_DEFAULTS["game"]["bg_delay_seconds"]), 0.0, 60.0)
        row = QHBoxLayout(); row.setSpacing(8)
        h = QLabel(_GAME_KEYS[2][2]); h.setWordWrap(True)
        row.addWidget(s); row.addWidget(h); row.addStretch(1)
        form.addRow(QLabel(_GAME_KEYS[2][1]), row)
        self.spins["bg_delay_seconds"] = s
        # default_spiciness: 0-6 int
        s = int_spin(game.get("default_spiciness", _TD_DEFAULTS["game"]["default_spiciness"]), 0, 6)
        row = QHBoxLayout(); row.setSpacing(8)
        h = QLabel(_GAME_KEYS[3][2]); h.setWordWrap(True)
        row.addWidget(s); row.addWidget(h); row.addStretch(1)
        form.addRow(QLabel(_GAME_KEYS[3][1]), row)
        self.spins["default_spiciness"] = s
        self._layout.addLayout(form)

        hint = QLabel("说明：大冒险概率与默认色度都是「开局默认值」——进行中的游戏仍可用群内指令覆盖"
                      "（/概率 改概率、/色色程度 改色度，仅管理员）。自动踢人阈值 = 玩家拥有的回答机会数"
                      "（阈值 2 = 第 2 次被抽到且仍未回答时踢出）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = self._bottom_bar(None)
        btn_reset = QPushButton("↺ 恢复默认设置")
        btn_reset.clicked.connect(self._reset_all)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        btn_save = QPushButton("💾 保存并生效")
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(self._save)
        bar.addWidget(btn_reset)
        bar.addWidget(btn_cancel)
        bar.addWidget(btn_save)
        self._layout.addLayout(bar)

    def _reset_all(self):
        d = _TD_DEFAULTS["game"]
        for key in self.spins:
            self.spins[key].setValue(d[key])
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_td()
        g = {}
        for key in self.spins:
            s = self.spins[key]
            v = s.value()
            if key == "bg_delay_seconds":
                v = float(v)
            if v != _TD_DEFAULTS["game"][key]:
                g[key] = v
        if g:
            merged = dict((y.get("game") or {}))
            merged.update(g)
            for key in list(merged):
                if key not in g and merged[key] == _TD_DEFAULTS["game"].get(key):
                    merged.pop(key)
            y["game"] = merged
        else:
            y.pop("game", None)
        self._commit("自动模式行为")


# ============================================================
#  ④ 出题提示词（编辑器）
# ============================================================
class PromptsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "📝 真心话大冒险 · 出题提示词", 860)
        self.setMinimumHeight(700)  # 用户偏好：提示词弹窗要够高
        self._build()

    def _build(self):
        self.metas = tdpp.prompt_meta()
        self.prompts_cfg = (self.mw.yaml_cfg or {}).get("truth_dare", {}).get("prompts") or {}

        # 顶部：分组
        self.cmb_group = QComboBox()
        self.cmb_group.addItem("全部分组", "")
        for g in tdpp.prompt_groups():
            self.cmb_group.addItem(g, g)
        self.cmb_group.currentIndexChanged.connect(self._filter_list)
        top = QHBoxLayout()
        top.addWidget(QLabel("分组"))
        top.addWidget(self.cmb_group, 1)
        self._layout.addLayout(top)

        # 左右：列表 + 编辑器
        mid = QHBoxLayout()
        mid.setSpacing(10)

        self.lst = QListWidget()
        self.lst.setFixedWidth(250)
        self.lst.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lst.itemSelectionChanged.connect(self._on_select)
        self._fill_list("")
        mid.addWidget(self.lst)

        right = QVBoxLayout()
        self.lbl_name = QLabel("—")
        self.lbl_name.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.lbl_name.setWordWrap(True)
        self.lbl_desc = QLabel("")
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setStyleSheet("color: #667085;")
        right.addWidget(self.lbl_name)
        right.addWidget(self.lbl_desc)
        self.ed = QPlainTextEdit()
        f = QFont("Monospace")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(11)
        self.ed.setFont(f)
        right.addWidget(self.ed, 1)
        self.lbl_ph = QLabel("")
        self.lbl_ph.setWordWrap(True)
        self.lbl_ph.setStyleSheet("color: #98a2b3;")
        right.addWidget(self.lbl_ph)
        mid.addLayout(right, 1)
        self._layout.addLayout(mid, 1)

        # 底部：恢复此条默认 + 取消/保存
        bar = self._bottom_bar(None)
        btn_reset_item = QPushButton("↺ 恢复此条默认")
        btn_reset_item.clicked.connect(self._reset_item)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        btn_save = QPushButton("💾 保存并生效")
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(self._save)
        bar.addWidget(btn_reset_item)
        bar.addWidget(btn_cancel)
        bar.addWidget(btn_save)
        self._layout.addLayout(bar)

        self._dirty = False
        self._session_edits: dict[str, str] = {}  # 本次打开期间编辑过但可能未保存的内容
        self.ed.textChanged.connect(self._on_text_changed)

    def _on_text_changed(self):
        self._dirty = True
        if getattr(self, "_programmatic_edit", False):
            return  # 程序化 setPlainText 不进会话缓存
        key = self._cur_key()
        if key:
            self._session_edits[key] = self.ed.toPlainText()

    def _effective(self, key: str) -> str:
        """当前条目应显示的内容：会话编辑 > yaml 定制 > 代码默认。"""
        if key in self._session_edits:
            return self._session_edits[key]
        if key in self.prompts_cfg:
            return self.prompts_cfg[key]
        return tdpp.default_prompt(key)

    def _fill_list(self, group: str):
        self.lst.clear()
        for i, m in enumerate(self.metas):
            if group and m["group"] != group:
                continue
            item = QListWidgetItem(f"[{m['group']}] {m['name']}")
            item.setData(Qt.UserRole, m["key"])
            if m["key"] in self.prompts_cfg:
                item.setText(item.text() + " ✏️")  # 已定制标记
            self.lst.addItem(item)

    def _filter_list(self, _idx):
        self._fill_list(self.cmb_group.currentData() or "")

    def _cur_key(self):
        items = self.lst.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def _on_select(self):
        key = self._cur_key()
        if not key:
            return
        m = next(x for x in self.metas if x["key"] == key)
        self.lbl_name.setText(f"{m['name']}（{key}）")
        self.lbl_desc.setText(m["desc"])
        self._programmatic_edit = True
        try:
            self.ed.setPlainText(self._effective(key))
        finally:
            self._programmatic_edit = False
        # 占位符提示：默认值中出现的 {xxx}
        phs = sorted(set(re.findall(r"\{([a-z_0-9]+)\}", tdpp.default_prompt(key))))
        self.lbl_ph.setText("占位符（渲染时替换，请勿删除）：" +
                            ("、".join("{" + p + "}" for p in phs) if phs else "无"))

    def _reset_item(self):
        key = self._cur_key()
        if not key:
            return
        was_custom = key in self.prompts_cfg or key in self._session_edits
        if key in self.prompts_cfg:
            self.prompts_cfg.pop(key)
        self._session_edits.pop(key, None)
        if was_custom:
            self._programmatic_edit = True
            try:
                self.ed.setPlainText(tdpp.default_prompt(key))
            finally:
                self._programmatic_edit = False
            self._fill_list(self.cmb_group.currentData() or "")
            # 重新选中当前条目（_fill_list 清空了选中态）
            for i in range(self.lst.count()):
                if self.lst.item(i).data(Qt.UserRole) == key:
                    self.lst.setCurrentRow(i)
                    break
            self._on_select()
            self.mw.statusBar().showMessage(f"{key} 已恢复默认（保存后生效）", 5000)
        else:
            self.mw.statusBar().showMessage(f"{key} 本来就是默认值", 4000)

    def _save(self):
        key = self._cur_key()
        if not key:
            return
        text = self.ed.toPlainText()
        if not text.strip():
            QMessageBox.warning(self, "提示", "提示词不能为空（可点「恢复此条默认」）。")
            return
        # 占位符检查：默认值里的占位符缺失 → 警告但不阻止
        missing = [p for p in sorted(set(re.findall(r"\{([a-z_0-9]+)\}", tdpp.default_prompt(key))))
                   if "{" + p + "}" not in text]
        if missing:
            r = QMessageBox.question(
                self, "占位符检查",
                "编辑内容缺少以下占位符：\n" + "、".join("{" + p + "}" for p in missing) +
                "\n\n渲染时这些位置将原样保留，可能导致 LLM 收到残缺提示词。\n仍要保存吗？",
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        # 全量保存：会话内所有编辑过的项 + yaml 已有定制项（_effective 统一取当前值）
        cfg = self._yaml_td().setdefault("prompts", {})
        for m in self.metas:
            k = m["key"]
            v = self._effective(k)
            if not v:
                cfg.pop(k, None)
                continue
            if v == tdpp.default_prompt(k):
                cfg.pop(k, None)  # 等于默认 → 不写盘
            else:
                cfg[k] = v
                self.prompts_cfg[k] = v
        if not cfg:
            self._yaml_td().pop("prompts", None)
        self._session_edits.clear()
        self._commit("出题提示词")
