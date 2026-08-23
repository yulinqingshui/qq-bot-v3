"""
persona_settings_dialogs.py — 人设画像页 4 个设置弹窗
=====================================================
- ⚙️ 数据预处理：min_incremental_messages / batch_chars / direct_threshold /
  context_window / session_gap_seconds / map_concurrency
- 🤖 LLM 调用参数：7 阶段 × {max_tokens, temperature, thinking, json_mode, timeout}
  + llm_retries / net_retries
- 📏 人设画像规则：persona_limits（字段限制/字数/压缩区间）+ profile_limits（画像字数）
- 📝 LLM 处理提示词：41 组提示词编辑器（默认值来自 core/persona_prompts.py，
  用户定制存 config.yaml 的 persona.prompts，每项可单独恢复默认）

保存流程（与配置页一致）：改 mw.yaml_cfg → api_client.save_yaml 写盘
→ POST /config 通知 bot 热重载 → GUI 侧同步刷新 PERSONA_CFG / PERSONA_PROMPT_* 键。
恢复默认：预处理/参数/规则三窗有总「恢复默认设置」按钮；提示词窗每项可恢复默认。
"""

import copy
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QVBoxLayout, QAbstractItemView, QMessageBox,
)

import api_client
from worker import Worker


# ------------------------------------------------------------
#  默认值 / 结构常量
# ------------------------------------------------------------
from core.config import DEFAULTS
import core.persona_prompts as pp

_PERSONA_DEFAULTS = DEFAULTS["persona"]

# 预处理 6 项（直接位于 persona 段下的平铺键）
_PREPROCESS_KEYS = [
    ("min_incremental_messages", "增量触发阈值（条）",
     "新增消息 ≥ N 条才触发该用户的增量更新", 1, 1000000),
    ("batch_chars", "Map 批次大小（字符）",
     "消息分批提取时，每批最多 N 个字符", 1000, 200000),
    ("direct_threshold", "直接分析阈值（字符）",
     "聊天文本 < N 字符时跳过 Map→Reduce，直接单次 LLM 调用", 1000, 200000),
    ("context_window", "上下文窗口（±条）",
     "目标用户每条发言前后保留的群消息条数", 0, 100),
    ("session_gap_seconds", "Session 间隔（秒）",
     "相邻消息间隔超过 N 秒视为新会话", 60, 86400),
    ("map_concurrency", "Map 并发批次",
     "Map 阶段同时进行的 LLM 调用上限", 1, 50),
]

# LLM 阶段（表格行）：(key, 显示名)
_LLM_STAGES = [
    ("map", "消息提取（Map）"),
    ("persona_reduce", "人设终合并（Reduce）"),
    ("profile_reduce", "画像终稿（Reduce）"),
    ("merge", "多级中间合并"),
    ("compress", "画像机械压缩"),
    ("persona_compress_loop", "人设压缩循环"),
    ("verify", "一致性审核"),
]
_THINKING_OPTS = ["on", "off", "low", "max"]

# 规则：人设 JSON 限制——单字段（9 项，左列）
_PERSONA_FIELD_ITEMS = [
    ("identity_sub", "identity 各子键（字）", 0, 500),
    ("personality", "personality（字）", 0, 500),
    ("group_role", "group_role（字）", 0, 500),
    ("sexual_sub", "sexual 经历/身体（字）", 0, 500),
    ("interests", "兴趣（项）", 0, 50),
    ("weaknesses_taboos", "雷点（项）", 0, 50),
    ("catchphrases", "口癖（项）", 0, 50),
    ("relationships", "关系（对）", 0, 50),
    ("sexual_preferences", "性偏好（项）", 0, 50),
]
# 规则：人设 JSON 限制——总长与压缩（6 项，右列）
_PERSONA_TOTAL_ITEMS = [
    ("total_min", "总长目标下限（字）", 0, 100000),
    ("total_max", "总长目标上限（字）", 0, 100000),
    ("total_hard_max", "总长硬上限（字，超限触发压缩）", 0, 100000),
    ("compress_rounds", "压缩循环轮数上限", 1, 10),
    ("compress_fix_min", "过头修正恢复下限（字）", 0, 100000),
    ("compress_fix_max", "过头修正恢复上限（字）", 0, 100000),
]
# 兼容旧引用（全量 15 项）
_PERSONA_LIMIT_ITEMS = _PERSONA_FIELD_ITEMS + _PERSONA_TOTAL_ITEMS
# 规则：画像字数
_PROFILE_LIMIT_ITEMS = [
    ("total_min", "画像总字数下限", 0, 10000),
    ("total_max", "画像总字数上限", 0, 10000),
    ("compress_trigger", "强制压缩模式触发线（旧画像>N字）", 0, 10000),
    ("compress_rounds", "压缩循环轮数上限", 1, 10),
    ("compress_fix_min", "过头修正恢复下限（字）", 0, 10000),
    ("compress_fix_max", "过头修正恢复上限（字）", 0, 10000),
]


# 数值框控件 2026-08-21 抽公共到 widgets.py（NoArrowSpinBox 家族 + int_spin/float_spin），
# 本文件保留 _ 前缀兼容别名（文件内既有调用点零改动）
from widgets import (
    NoArrowSpinBox as _NoWheelSpinBox,
    NoArrowDoubleSpinBox as _NoWheelDoubleSpinBox,
    no_wheel_spin as _no_wheel_spin,
    int_spin as _int_spin,
    float_spin as _float_spin,
)


class _BaseDialog(QDialog):
    """四个弹窗的公共基类：读 persona 段、保存落盘、热重载、刷新 GUI cfg。"""

    def __init__(self, mw, title: str, width: int = 620):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        self._layout = v

    # ---------- persona 段读写 ----------
    def persona_cfg(self) -> dict:
        """yaml 当前 persona 段（与 DEFAULTS 深合并，缺项回退默认）。"""
        merged = copy.deepcopy(_PERSONA_DEFAULTS)
        y = (self.mw.yaml_cfg or {}).get("persona") or {}
        for k, v in y.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged

    def _yaml_persona(self) -> dict:
        """mw.yaml_cfg 中实际落盘的 persona 段（不合并默认；保存只写用户改过的键）。"""
        y = self.mw.yaml_cfg
        if not isinstance(y.get("persona"), dict):
            y["persona"] = {}
        return y["persona"]

    # ---------- 保存 ----------
    def _commit(self, what: str):
        """写 yaml → POST /config 热重载 → 刷新 GUI 侧 PERSONA* 键。"""
        try:
            api_client.save_yaml(self.mw.yaml_cfg)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"config.yaml 写入失败：{e}")
            return
        # GUI 侧同步（不碰密钥等其他键，只刷新 PERSONA_*）
        try:
            from core.config import flatten_yaml_tree
            fresh = flatten_yaml_tree(api_client.load_yaml())
        except Exception:
            fresh = {}
        for k in [k for k in list(self.mw.cfg) if k.startswith("PERSONA_PROMPT_")]:
            self.mw.cfg.pop(k)
        self.mw.cfg["PERSONA_CFG"] = fresh.get("PERSONA_CFG", self.persona_cfg())
        for k, v in fresh.items():
            if k.startswith("PERSONA_PROMPT_"):
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
#  ① 数据预处理
# ============================================================
class PreprocessDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "⚙️ 人设画像 · 数据预处理", 560)
        self._build()

    def _build(self):
        pc = self.persona_cfg()
        form = QFormLayout()
        form.setSpacing(12)
        self.spins = {}
        for key, label, hint, lo, hi in _PREPROCESS_KEYS:
            row = QHBoxLayout()
            row.setSpacing(8)
            spin = _int_spin(pc.get(key, _PERSONA_DEFAULTS.get(key)), lo, hi)
            row.addWidget(spin)
            hint_lbl = QLabel(hint)
            hint_lbl.setWordWrap(True)  # 防长 hint 贴边硬切（08-22 巡检发现）
            row.addWidget(hint_lbl)
            row.addStretch(1)
            form.addRow(QLabel(label), row)
            self.spins[key] = spin
        self._layout.addLayout(form)

        hint = QLabel("说明：这些参数决定人设/画像「何时生成、取多少消息、怎么分批」。"
                      "改完保存后，下一次触发的生成任务即使用新值。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        # 底部：恢复默认 + 取消/保存
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
        for key, _l, _h, lo, hi in _PREPROCESS_KEYS:
            self.spins[key].setValue(int(_PERSONA_DEFAULTS.get(key)))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_persona()
        for key, _l, _h, lo, hi in _PREPROCESS_KEYS:
            v = self.spins[key].value()
            if v != _PERSONA_DEFAULTS.get(key):
                y[key] = v
            else:
                y.pop(key, None)  # 与默认相同 → 不写盘，保持 yaml 干净
        self._commit("数据预处理")


# ============================================================
#  ② LLM 调用参数
# ============================================================
class LLMParamsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🤖 人设画像 · LLM 调用参数", 760)
        self._build()

    def _build(self):
        pc = self.persona_cfg()
        stages = pc.get("llm", {}) or {}

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
            d = stages.get(key) or _PERSONA_DEFAULTS["llm"][key]
            tbl.setItem(r, 0, QTableWidgetItem(name))
            tbl.item(r, 0).setFlags(Qt.NoItemFlags)
            # max_tokens
            mt = _NoWheelSpinBox(); mt.setRange(256, 262144)
            mt.setValue(int(d["max_tokens"]))
            # temperature
            tp = _NoWheelDoubleSpinBox(); tp.setRange(0.0, 2.0); tp.setDecimals(2); tp.setSingleStep(0.05)
            tp.setValue(float(d["temperature"]))
            # thinking
            tk = QComboBox(); tk.addItems(_THINKING_OPTS)
            tk.setCurrentText(str(d["thinking"]))
            # json_mode
            jm = QCheckBox()
            jm.setChecked(bool(d["json_mode"]))
            # timeout
            to = _NoWheelSpinBox(); to.setRange(30, 3600)
            to.setValue(int(d.get("timeout", 900)))
            for c, w in [(1, mt), (2, tp), (3, tk), (4, jm), (5, to)]:
                if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                    w = _no_wheel_spin(w)
                tbl.setCellWidget(r, c, w)
                w.setMinimumWidth(90)
            self.cells[key] = (mt, tp, tk, jm, to)
        self.tbl = tbl
        self._layout.addWidget(tbl)

        # 全局重试
        form = QFormLayout()
        self.spin_retries = _int_spin(pc.get("llm_retries", 5), 1, 20)
        form.addRow("单批次业务层重试次数", self.spin_retries)
        self.spin_net = _int_spin(pc.get("net_retries", 3), 0, 20)
        form.addRow("网络异常额外重试次数", self.spin_net)
        self._layout.addLayout(form)

        hint = QLabel("thinking：on=开思考 / off=关思考 / low·max=DeepSeek reasoning_effort。"
                      "注意：temperature 对 DeepSeek 推理模型仅影响最终输出；"
                      "max_tokens 是思考链+正文共享预算，过小会截断。")
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
        d = _PERSONA_DEFAULTS
        for key, (mt, tp, tk, jm, to) in self.cells.items():
            v = d["llm"][key]
            mt.setValue(int(v["max_tokens"])); tp.setValue(float(v["temperature"]))
            tk.setCurrentText(str(v["thinking"])); jm.setChecked(bool(v["json_mode"]))
            to.setValue(int(v.get("timeout", 900)))
        self.spin_retries.setValue(int(d.get("llm_retries", 5)))
        self.spin_net.setValue(int(d.get("net_retries", 3)))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_persona()
        llm = {}
        for key, (mt, tp, tk, jm, to) in self.cells.items():
            d = {
                "max_tokens": mt.value(),
                "temperature": tp.value(),
                "thinking": tk.currentText(),
                "json_mode": jm.isChecked(),
                "timeout": to.value(),
            }
            if d == _PERSONA_DEFAULTS["llm"][key]:
                continue  # 与默认相同不写
            llm[key] = d
        if llm:
            # 合并已有 llm 段（保留未在本表出现的键，如有）
            merged = dict((y.get("llm") or {}))
            merged.update(llm)
            # 清掉恢复为默认的阶段（不写盘）
            for key in list(merged):
                if key not in llm and merged[key] == _PERSONA_DEFAULTS["llm"].get(key):
                    merged.pop(key)
            y["llm"] = merged if merged else None
        else:
            # 全部恢复默认 → 整段清除
            y.pop("llm", None)
        if y.get("llm") is None:
            y.pop("llm", None)
        if self.spin_retries.value() != _PERSONA_DEFAULTS.get("llm_retries", 5):
            y["llm_retries"] = self.spin_retries.value()
        else:
            y.pop("llm_retries", None)
        if self.spin_net.value() != _PERSONA_DEFAULTS.get("net_retries", 3):
            y["net_retries"] = self.spin_net.value()
        else:
            y.pop("net_retries", None)
        self._commit("LLM 调用参数")


# ============================================================
#  ③ 人设画像规则
# ============================================================
class RulesDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "📏 人设画像 · 生成规则", 820)
        self._build()

    def _build(self):
        pc = self.persona_cfg()
        plim = pc.get("persona_limits") or {}
        flim = pc.get("profile_limits") or {}
        pd = _PERSONA_DEFAULTS["persona_limits"]
        fd = _PERSONA_DEFAULTS["profile_limits"]

        # ── 两列布局（2026-08-21 用户要求：左右内容平衡，各带明确标题）──
        cols = QHBoxLayout()
        cols.setSpacing(14)

        # 左列：人设单字段限制（9 项）
        g1 = QGroupBox("人设 JSON · 单字段限制（9 项）")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)
        self.plim_spins = {}
        for key, label, lo, hi in _PERSONA_FIELD_ITEMS:
            s = _int_spin(plim.get(key, pd[key]), lo, hi)
            f1.addRow(QLabel(label), s)
            self.plim_spins[key] = s
        cols.addWidget(g1, 1)

        # 右列：人设总长与压缩（6 项）+ 画像字数规则（6 项），各带明确标题
        right = QVBoxLayout()
        right.setSpacing(10)
        g3 = QGroupBox("人设 JSON · 总长与压缩（6 项）")
        f3 = QFormLayout(g3)
        f3.setSpacing(8)
        for key, label, lo, hi in _PERSONA_TOTAL_ITEMS:
            s = _int_spin(plim.get(key, pd[key]), lo, hi)
            f3.addRow(QLabel(label), s)
            self.plim_spins[key] = s
        right.addWidget(g3)

        g2 = QGroupBox("画像字数规则（6 项）")
        f2 = QFormLayout(g2)
        f2.setSpacing(8)
        self.flim_spins = {}
        for key, label, lo, hi in _PROFILE_LIMIT_ITEMS:
            s = _int_spin(flim.get(key, fd[key]), lo, hi)
            f2.addRow(QLabel(label), s)
            self.flim_spins[key] = s
        right.addWidget(g2)
        right.addStretch(1)
        cols.addLayout(right, 1)
        self._layout.addLayout(cols)

        # 底部通栏说明（左右均衡后不再占列）
        hint = QLabel("说明：总长/字数指 LLM 输出的目标区间；「超限触发压缩」是硬上限，超过会自动进入压缩循环。"
                      "改小上限可让人设/画像更精炼。")
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
        d = _PERSONA_DEFAULTS
        for key in self.plim_spins:
            self.plim_spins[key].setValue(int(d["persona_limits"][key]))
        for key in self.flim_spins:
            self.flim_spins[key].setValue(int(d["profile_limits"][key]))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_persona()
        d = _PERSONA_DEFAULTS
        p = {}
        for key in self.plim_spins:
            v = self.plim_spins[key].value()
            if v != d["persona_limits"][key]:
                p[key] = v
        f = {}
        for key in self.flim_spins:
            v = self.flim_spins[key].value()
            if v != d["profile_limits"][key]:
                f[key] = v
        if p:
            merged = dict((y.get("persona_limits") or {}))
            merged.update(p)
            for key in list(merged):
                if key not in p and merged[key] == d["persona_limits"].get(key):
                    merged.pop(key)
            y["persona_limits"] = merged
        else:
            y.pop("persona_limits", None)
        if f:
            merged = dict((y.get("profile_limits") or {}))
            merged.update(f)
            for key in list(merged):
                if key not in f and merged[key] == d["profile_limits"].get(key):
                    merged.pop(key)
            y["profile_limits"] = merged
        else:
            y.pop("profile_limits", None)
        self._commit("人设画像规则")


# ============================================================
#  ④ LLM 处理提示词（编辑器）
# ============================================================
class PromptsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "📝 人设画像 · LLM 处理提示词", 860)
        self.setMinimumHeight(700)  # 2026-08-21 用户要求：高度增加一些
        self._build()

    def _build(self):
        self.metas = pp.prompt_meta()
        self.prompts_cfg = (self.mw.yaml_cfg or {}).get("persona", {}).get("prompts") or {}

        # 顶部：分组
        self.cmb_group = QComboBox()
        self.cmb_group.addItem("全部分组", "")
        for g in pp.prompt_groups():
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
        return pp.default_prompt(key)

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
        phs = sorted(set(re.findall(r"\{([a-z_0-9]+)\}", pp.default_prompt(key))))
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
                self.ed.setPlainText(pp.default_prompt(key))
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
        missing = [p for p in sorted(set(re.findall(r"\{([a-z_0-9]+)\}", pp.default_prompt(key))))
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
        cfg = self._yaml_persona().setdefault("prompts", {})
        for m in self.metas:
            k = m["key"]
            v = self._effective(k)
            if not v:
                cfg.pop(k, None)
                continue
            if v == pp.default_prompt(k):
                cfg.pop(k, None)  # 等于默认 → 不写盘
            else:
                cfg[k] = v
                self.prompts_cfg[k] = v
        if not cfg:
            self._yaml_persona().pop("prompts", None)
        self._session_edits.clear()
        self._commit("提示词")
