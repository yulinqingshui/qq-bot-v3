"""
roleplay_settings_dialogs.py — 角色扮演页 3 个设置弹窗（2026-08-22）
=================================================================
- ⚙️ RP 规则：rules 段 4 项（摘要间隔/短期窗口/旁白字数下限/上限）
- 🤖 LLM 参数：5 个调用点共用 {max_tokens, temperature, thinking, json_mode, timeout}
  （json_mode 仅世界观生成生效——旁白/摘要纯文本不吃 JSON 模式）
- 📝 提示词：6 组提示词编辑器（默认值来自 core/roleplay_prompts.py，
  用户定制存 config.yaml 的 roleplay.prompts，每项可单独恢复默认）

保存流程（与 truth_dare/persona 弹窗一致）：改 mw.yaml_cfg →
api_client.save_yaml 写盘 → POST /config 通知 bot 热重载 → GUI 侧
同步刷新 RP_CFG / RP_PROMPT_* 键（load_config 原地替换，调用点实时读）。
恢复默认：前两窗有总「恢复默认设置」按钮；提示词窗每项可恢复默认。
"""

import copy
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSpinBox, QDoubleSpinBox,
    QVBoxLayout, QAbstractItemView, QMessageBox,
)

import api_client
from worker import Worker
from widgets import NoArrowSpinBox, NoArrowDoubleSpinBox, no_wheel_spin, int_spin, float_spin


# ------------------------------------------------------------
#  默认值 / 结构常量
# ------------------------------------------------------------
from core.config import DEFAULTS
import core.roleplay_prompts as rpp

_RP_DEFAULTS = DEFAULTS["roleplay"]

# RP 规则 4 项：(key, 显示名, 说明, 范围)
_RULE_KEYS = [
    ("summary_interval", "剧情摘要间隔（轮）",
     "每 N 轮生成一次长期记忆摘要（调小=记忆更准、token 更高）", 1, 50),
    ("short_window_size", "短期窗口大小（条）",
     "旁白每次参考的最近原始消息条数（调大=对话风格更连贯、token 更高）", 1, 50),
    ("narrator_min_chars", "旁白每幕字数下限",
     "prompt 约束非硬截断（LLM 偶发不达标）；低于下限=信息密度不足", 50, 4000),
    ("narrator_max_chars", "旁白每幕字数上限",
     "prompt 约束非硬截断；超过上限=阅读负担过重", 100, 8000),
]

# LLM 参数 5 项：(key, 显示名, 说明)
_LLM_KEYS = [
    ("max_tokens", "max_tokens",
     "思考链+正文共享预算，过小会截断（原 32768，让模型充分思考后输出正文）"),
    ("temperature", "温度",
     "创意与稳定性平衡（原 0.7）"),
    ("thinking", "thinking 档位",
     "on=后端默认（DeepSeek 默认 max）/ off=关思考 / low·max=reasoning_effort"),
    ("json_mode", "json_mode（仅世界观生成生效）",
     "强制 JSON 输出，提高世界观解析成功率；旁白/摘要不吃（JSON 化会毁掉叙事）"),
    ("timeout", "超时（秒）",
     "单次 LLM 调用超时（原 1800，长思考场景别调太低）"),
]
_THINKING_OPTS = ["on", "off", "low", "max"]


class _BaseDialog(QDialog):
    """三个弹窗的公共基类：读 roleplay 段、保存落盘、热重载、刷新 GUI cfg。"""

    def __init__(self, mw, title: str, width: int = 620):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        self._layout = v

    # ---------- roleplay 段读写 ----------
    def rp_cfg(self) -> dict:
        """yaml 当前 roleplay 段（与 DEFAULTS 深合并，缺项回退默认）。"""
        merged = copy.deepcopy(_RP_DEFAULTS)
        y = (self.mw.yaml_cfg or {}).get("roleplay") or {}
        for k, v in y.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        return merged

    def _yaml_rp(self) -> dict:
        """mw.yaml_cfg 中实际落盘的 roleplay 段（保存只写用户改过的键）。"""
        y = self.mw.yaml_cfg
        if not isinstance(y.get("roleplay"), dict):
            y["roleplay"] = {}
        return y["roleplay"]

    # ---------- 保存 ----------
    def _commit(self, what: str):
        """写 yaml → POST /config 热重载 → 刷新 GUI 侧 RP* 键。"""
        try:
            api_client.save_yaml(self.mw.yaml_cfg)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"config.yaml 写入失败：{e}")
            return
        # GUI 侧同步（只刷新 RP_* 键）
        try:
            from core.config import flatten_yaml_tree
            fresh = flatten_yaml_tree(api_client.load_yaml())
        except Exception:
            fresh = {}
        for k in [k for k in list(self.mw.cfg) if k.startswith("RP_PROMPT_")]:
            self.mw.cfg.pop(k)
        self.mw.cfg["RP_CFG"] = fresh.get("RP_CFG", self.rp_cfg())
        for k, v in fresh.items():
            if k.startswith("RP_PROMPT_"):
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
#  ① RP 规则
# ============================================================
class RulesDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "⚙️ 角色扮演 · RP 规则", 640)
        self._build()

    def _build(self):
        tc = self.rp_cfg()
        rules = tc.get("rules", {})
        form = QFormLayout()
        form.setSpacing(12)
        self.spins = {}
        for key, label, hint, lo, hi in _RULE_KEYS:
            row = QHBoxLayout()
            row.setSpacing(8)
            spin = int_spin(rules.get(key, _RP_DEFAULTS["rules"][key]), lo, hi)
            row.addWidget(spin)
            # 行内 hint 必须 wordWrap：无 wrap 时被列宽截断成半句（pitfall 14）
            hint_lbl = QLabel(hint)
            hint_lbl.setWordWrap(True)
            hint_lbl.setStyleSheet("color: #667085; font-size: 12px;")
            row.addWidget(hint_lbl)
            row.addStretch(1)
            form.addRow(QLabel(label), row)
            self.spins[key] = spin
        self._layout.addLayout(form)

        hint = QLabel("说明：旁白字数上下限是写进旁白 prompt 的硬约束文案"
                      "（「每幕严格控制在 N-M 字」），不是输出截断——LLM 偶发超标属正常。"
                      "摘要间隔/短期窗口影响 token 预算：调小间隔、调大窗口 = 记忆更准但更费 token。")
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
        for key, _l, _h, _lo, _hi in _RULE_KEYS:
            self.spins[key].setValue(int(_RP_DEFAULTS["rules"][key]))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        # 校验：下限必须 ≤ 上限
        lo = self.spins["narrator_min_chars"].value()
        hi = self.spins["narrator_max_chars"].value()
        if lo > hi:
            QMessageBox.warning(self, "参数校验", "字数下限不能大于上限。")
            return
        y = self._yaml_rp()
        r = {}
        for key, _l, _h, _lo, _hi in _RULE_KEYS:
            v = self.spins[key].value()
            if v != _RP_DEFAULTS["rules"][key]:
                r[key] = v
        if r:
            merged = dict(y.get("rules") or {})
            merged.update(r)
            for key in list(merged):
                if key not in r and merged[key] == _RP_DEFAULTS["rules"].get(key):
                    merged.pop(key)
            y["rules"] = merged
        else:
            y.pop("rules", None)
        self._commit("RP 规则")


# ============================================================
#  ② LLM 参数
# ============================================================
class LLMParamsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🤖 角色扮演 · LLM 调用参数", 700)
        self._build()

    def _build(self):
        tc = self.rp_cfg()
        llm = tc.get("llm", {})

        box = QGroupBox("LLM 参数（5 个调用点共用：世界观生成 / 开场旁白 / 行动旁白 / 轮末总结 / 剧情摘要）")
        box.setFont(QFont())
        box.font().setPointSize(int(box.font().pointSize() * 1.25))
        box.font().setBold(True)
        form = QFormLayout(box)
        form.setSpacing(12)
        self.ctrls = {}
        for key, label, hint in _LLM_KEYS:
            d = _RP_DEFAULTS["llm"][key]
            v = llm.get(key, d)
            if key == "max_tokens":
                w = NoArrowSpinBox(); w.setRange(256, 262144)
                w.setValue(int(v)); w = no_wheel_spin(w)
            elif key == "temperature":
                w = NoArrowDoubleSpinBox(); w.setRange(0.0, 2.0)
                w.setDecimals(2); w.setSingleStep(0.05)
                w.setValue(float(v)); w = no_wheel_spin(w)
            elif key == "thinking":
                w = QComboBox(); w.addItems(_THINKING_OPTS)
                w.setCurrentText(str(v))
            elif key == "json_mode":
                w = QCheckBox()
                w.setChecked(bool(v))
            else:  # timeout
                w = NoArrowSpinBox(); w.setRange(30, 3600)
                w.setValue(int(v)); w = no_wheel_spin(w)
            w.setMinimumWidth(130)
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(w)
            hint_lbl = QLabel(hint)
            hint_lbl.setWordWrap(True)
            hint_lbl.setStyleSheet("color: #667085; font-size: 12px;")
            row.addWidget(hint_lbl, 1)
            form.addRow(QLabel(label), row)
            self.ctrls[key] = w
        self._layout.addWidget(box)

        hint = QLabel("说明：max_tokens 是思考链+正文共享预算（DeepSeek），过小会截断；"
                      "json_mode 只对世界观生成生效（_parse_world_json 依赖 JSON 解析），"
                      "旁白/摘要是纯文本输出，即使勾选也不受影响。")
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
        for key, w in self.ctrls.items():
            v = _RP_DEFAULTS["llm"][key]
            if isinstance(w, QComboBox):
                w.setCurrentText(str(v))
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            else:
                w.setValue(v)
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_rp()
        llm = {}
        for key, _l, _h in _LLM_KEYS:
            w = self.ctrls[key]
            d = _RP_DEFAULTS["llm"][key]
            if isinstance(w, QComboBox):
                v = w.currentText()
            elif isinstance(w, QCheckBox):
                v = w.isChecked()
            else:
                v = w.value()
                if key == "temperature":
                    v = float(v)
                else:
                    v = int(v)
            if v != d:
                llm[key] = v
        if llm:
            merged = dict(y.get("llm") or {})
            merged.update(llm)
            for key in list(merged):
                if key not in llm and merged[key] == _RP_DEFAULTS["llm"].get(key):
                    merged.pop(key)
            y["llm"] = merged
        else:
            y.pop("llm", None)
        self._commit("LLM 调用参数")


# ============================================================
#  ③ 提示词（编辑器）
# ============================================================
class PromptsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "📝 角色扮演 · 提示词", 860)
        self.setMinimumHeight(700)  # 用户偏好：提示词弹窗要够高
        self._build()

    def _build(self):
        self.metas = rpp.prompt_meta()
        self.prompts_cfg = (self.mw.yaml_cfg or {}).get("roleplay", {}).get("prompts") or {}

        # 顶部：分组
        self.cmb_group = QComboBox()
        self.cmb_group.addItem("全部分组", "")
        for g in rpp.prompt_groups():
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
        self._session_edits: dict[str, str] = {}
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
        return rpp.default_prompt(key)

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
        phs = sorted(set(re.findall(r"\{([a-z_0-9]+)\}", rpp.default_prompt(key))))
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
                self.ed.setPlainText(rpp.default_prompt(key))
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
        missing = [p for p in sorted(set(re.findall(r"\{([a-z_0-9]+)\}", rpp.default_prompt(key))))
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
        cfg = self._yaml_rp().setdefault("prompts", {})
        for m in self.metas:
            k = m["key"]
            v = self._effective(k)
            if not v:
                cfg.pop(k, None)
                continue
            if v == rpp.default_prompt(k):
                cfg.pop(k, None)  # 等于默认 → 不写盘
            else:
                cfg[k] = v
                self.prompts_cfg[k] = v
        if not cfg:
            self._yaml_rp().pop("prompts", None)
        self._session_edits.clear()
        self._commit("提示词")
