"""
ai_chat_settings_dialogs.py — AI 聊天页 2 个设置弹窗
=====================================================
- ⚙️ 参数（显示参数 + bot 回复链路参数 合并单窗，2026-08-21 用户要求）：
    显示参数 2 项（ai_chat 段顶层键：每页条数 / 气泡最大宽度）
    bot 回复链路参数 7 项：上下文条数（bot.max_history）+ 冷却秒数
    （bot.cooldown_seconds，bot 回复后同用户再次触发的冷却窗口）
    ——2026-08-22 从「配置」页移入；LLM 调用参数 5 项（ai_chat.llm 子段：
    max_tokens / temperature / thinking / json_mode / timeout）——作用域
    = bot 对话回复链路（core/router.py _handle_ai_reply 的 call_llm），
    默认值 = 原函数默认（行为不变）；热重载后即时生效。
    ⚠️ 不碰全局 llm 段（backend/model/并发等基础设施参数）——
    那属于配置页范畴，避免 AI 聊天弹窗误伤其他链路。
- 📝 提示词：2 组提示词编辑器——
    默认人设（system_prompt，bot 未单独设置人设时扮演的女高中生）
    角色模板（personality_template，{personality} 占位符的扮演规则）
  默认值来自 core/config.py DEFAULTS；用户定制存 config.yaml 顶层键
  system_prompt / personality_template（与 bot 运行时 CONFIG["SYSTEM_PROMPT"] /
  CONFIG["PERSONALITY_TEMPLATE"] 同一事实源，保存后热重载即生效）。

保存流程（与 persona / truth_dare 弹窗一致）：改 mw.yaml_cfg →
api_client.save_yaml 写盘 → POST /config 通知 bot 热重载 → GUI 侧刷新
AI_CHAT_CFG / SYSTEM_PROMPT / PERSONALITY_TEMPLATE 键。
恢复默认：参数窗有总「恢复默认设置」（显示+LLM 一起）；提示词窗每项可恢复默认。
"""

import copy
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QSpinBox, QDoubleSpinBox, QVBoxLayout, QAbstractItemView, QMessageBox,
)

import api_client
from worker import Worker
from widgets import (
    NoArrowSpinBox, NoArrowDoubleSpinBox, no_wheel_spin,
    int_spin, float_spin,
)

from core.config import DEFAULTS

_AI_CHAT_DEFAULTS = DEFAULTS["ai_chat"]
_BOT_DEFAULTS = DEFAULTS["bot"]  # 上下文条数默认值（bot.max_history）

# 显示参数 2 项：(key, 显示名, 说明, 范围)
# 说明文字保持字段级一行内（弹窗列宽有限，过长会被截断）
_PARAM_KEYS = [
    ("page_size", "聊天记录每页条数",
     "选中用户后一次加载多少条", 10, 500),
    ("bubble_max_width", "气泡最大宽度（px）",
     "气泡超此宽度自动换行", 160, 900),
]

# LLM 调用参数 5 项（ai_chat.llm 子段，bot 对话回复链路专用）
_LLM_KEYS = [
    ("max_tokens", "max_tokens",
     "思考+正文共享预算", "int", 256, 262144),
    ("temperature", "temperature",
     "0 最稳 / 1 最放飞", "float", 0.0, 2.0),
    ("thinking", "thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("json_mode", "json_mode",
     "强制 JSON 输出", "check", None, None),
    ("timeout", "超时（秒）",
     "单次调用 HTTP 超时", "int", 30, 3600),
]
_THINKING_OPTS = ["on", "off", "low", "max"]

# 提示词 2 项：(key, 显示名, 说明, 默认值来源)
_PROMPT_KEYS = [
    ("system_prompt", "默认人设（女高中生）",
     "用户未单独设置 bot 人设（/人设）时，bot 扮演的角色全文。"
     "AI 聊天页用户列表「bot 人设」列的未设置提示、"
     "以及 bot 实际回复所依据的基础人设都用它。"),
    ("personality_template", "角色模板（扮演规则）",
     "用户设置了单独人设后，bot 用该人设扮演的规则模板。"
     "渲染时把 {personality} 替换成该用户的单独人设文本。"
     "占位符 {personality} 必须保留。"),
]


class _BaseDialog(QDialog):
    """两个弹窗的公共基类：读 ai_chat 段 / 顶层提示词键、保存落盘、热重载。"""

    def __init__(self, mw, title: str, width: int = 620):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        self._layout = v

    # ---------- ai_chat 段读写 ----------
    def ai_cfg(self) -> dict:
        """yaml 当前 ai_chat 段（与 DEFAULTS 合并，缺项回退默认）。"""
        merged = copy.deepcopy(_AI_CHAT_DEFAULTS)
        y = (self.mw.yaml_cfg or {}).get("ai_chat") or {}
        for k, v in y.items():
            merged[k] = v
        return merged

    def _yaml_ai(self) -> dict:
        """mw.yaml_cfg 中实际落盘的 ai_chat 段（保存只写用户改过的键）。"""
        y = self.mw.yaml_cfg
        if not isinstance(y.get("ai_chat"), dict):
            y["ai_chat"] = {}
        return y["ai_chat"]

    # ---------- 提示词顶层键读写 ----------
    def prompt_default(self, key: str) -> str:
        return DEFAULTS.get(key, "")

    # ---------- bot 段读写（上下文条数 bot.max_history） ----------
    def _bot_cfg(self) -> dict:
        """yaml 当前 bot 段（与 DEFAULTS 合并，缺项回退默认）。"""
        merged = copy.deepcopy(_BOT_DEFAULTS)
        y = (self.mw.yaml_cfg or {}).get("bot")
        if isinstance(y, dict):
            for k, v in y.items():
                merged[k] = v
        return merged

    def _yaml_bot(self) -> dict:
        """mw.yaml_cfg 中实际落盘的 bot 段（只改 max_history 键）。"""
        y = self.mw.yaml_cfg
        if not isinstance(y.get("bot"), dict):
            y["bot"] = {}
        return y["bot"]

    def prompt_effective(self, key: str) -> str:
        """当前值：yaml 定制 > 代码默认（顶层键，扁平存储）。"""
        v = (self.mw.yaml_cfg or {}).get(key)
        if isinstance(v, str) and v.strip():
            return v
        return self.prompt_default(key)

    # ---------- 保存 ----------
    def _commit(self, what: str):
        """写 yaml → POST /config 热重载 → 刷新 GUI 侧 AI_CHAT_* / 提示词键。"""
        try:
            api_client.save_yaml(self.mw.yaml_cfg)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"config.yaml 写入失败：{e}")
            return
        # GUI 侧同步（只刷新本页相关键）
        try:
            from core.config import flatten_yaml_tree
            fresh = flatten_yaml_tree(api_client.load_yaml())
        except Exception:
            fresh = {}
        for k in ("AI_CHAT_CFG", "SYSTEM_PROMPT", "PERSONALITY_TEMPLATE"):
            if k in fresh:
                self.mw.cfg[k] = fresh[k]
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

    def _bottom_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addStretch(1)
        return bar


# ============================================================
#  ① 参数（显示参数 + LLM 调用参数，合并单窗）
# ============================================================
class ParamsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "⚙️ AI 聊天 · 参数", 800)
        self._build()

    def _llm_defaults(self) -> dict:
        return _AI_CHAT_DEFAULTS.get("llm", {})

    def _build(self):
        cfg = self.ai_cfg()

        # ---- 显示参数（左列） ----
        gb_display = QGroupBox("显示参数（2 项）")
        dform = QFormLayout(gb_display)
        dform.setSpacing(12)
        self.spins = {}
        for key, label, hint, lo, hi in _PARAM_KEYS:
            row = QHBoxLayout()
            row.setSpacing(8)
            spin = int_spin(cfg.get(key, _AI_CHAT_DEFAULTS[key]), lo, hi)
            row.addWidget(spin)
            hl = QLabel(hint)
            hl.setWordWrap(True)
            row.addWidget(hl)
            row.addStretch(1)
            dform.addRow(QLabel(label), row)
            self.spins[key] = spin

        # ---- bot 回复链路参数（右列）：上下文条数 + 冷却 + LLM 调用参数 5 项 ----
        gb_llm = QGroupBox("bot 回复链路参数（7 项）")
        lform = QFormLayout(gb_llm)
        lform.setSpacing(12)
        bot_cur = self._bot_cfg()
        # 上下文条数（bot.max_history，2026-08-22 从「配置」页移入）
        row = QHBoxLayout()
        row.setSpacing(8)
        self.sp_max_history = int_spin(
            bot_cur.get("max_history", _BOT_DEFAULTS["max_history"]), 10, 2000)
        row.addWidget(self.sp_max_history)
        hl = QLabel("bot 对话记忆上下文窗口")
        hl.setWordWrap(True)
        row.addWidget(hl)
        row.addStretch(1)
        lform.addRow(QLabel("上下文条数"), row)
        # 冷却秒数（bot.cooldown_seconds，2026-08-22 从「配置」页移入）
        row = QHBoxLayout()
        row.setSpacing(8)
        self.sp_cooldown = int_spin(
            bot_cur.get("cooldown_seconds", _BOT_DEFAULTS["cooldown_seconds"]), 0, 3600)
        row.addWidget(self.sp_cooldown)
        hl = QLabel("同用户触发冷却窗口")
        hl.setWordWrap(True)
        row.addWidget(hl)
        row.addStretch(1)
        lform.addRow(QLabel("冷却（秒）"), row)
        llm_cur = cfg.get("llm", {}) or self._llm_defaults()
        llm_def = self._llm_defaults()
        self.llm_widgets = {}
        for key, label, hint, kind, lo, hi in _LLM_KEYS:
            row = QHBoxLayout()
            row.setSpacing(8)
            if kind == "int":
                assert lo is not None and hi is not None
                w = NoArrowSpinBox()
                w.setRange(int(lo), int(hi))
                w.setValue(int(llm_cur.get(key, llm_def.get(key))))
                w = no_wheel_spin(w)
            elif kind == "float":
                assert lo is not None and hi is not None
                w = NoArrowDoubleSpinBox()
                w.setRange(float(lo), float(hi))
                w.setDecimals(2)
                w.setSingleStep(0.05)
                w.setValue(float(llm_cur.get(key, llm_def.get(key))))
                w = no_wheel_spin(w)
            elif kind == "combo":
                w = QComboBox()
                w.addItems(_THINKING_OPTS)
                w.setCurrentText(str(llm_cur.get(key, llm_def.get(key, "on"))))
            else:  # check
                w = QCheckBox()
                w.setChecked(bool(llm_cur.get(key, llm_def.get(key))))
            row.addWidget(w)
            hl = QLabel(hint)
            hl.setWordWrap(True)
            row.addWidget(hl)
            row.addStretch(1)
            lform.addRow(QLabel(label), row)
            self.llm_widgets[key] = w

        cols = QHBoxLayout()
        cols.setSpacing(10)
        cols.addWidget(gb_display, 1)
        cols.addWidget(gb_llm, 1)
        self._layout.addLayout(cols)

        hint = QLabel(
            "说明：显示参数即时生效于 GUI 渲染（保存后重新选中用户即生效）；"
            "右列参数作用于 bot 对话回复链路（热重载后生效），默认值 = 原行为。"
            "上下文条数 = bot 群聊上下文记忆窗口；冷却 = bot 回复后同用户再次触发的间隔。"
            "两项原在「配置」页，2026-08-22 移入此处。"
            "thinking：on=后端默认（DeepSeek 默认 max）/ off=关思考 / low·max=思考强度。"
            "max_tokens 是思考链+正文共享预算，过小会截断回复。"
            "⚠️ 此处不管理全局 LLM 后端/模型参数（那在「配置」页）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = self._bottom_bar()
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
        for key, _l, _h, _lo, _hi in _PARAM_KEYS:
            self.spins[key].setValue(int(_AI_CHAT_DEFAULTS[key]))
        self.sp_max_history.setValue(int(_BOT_DEFAULTS["max_history"]))
        self.sp_cooldown.setValue(int(_BOT_DEFAULTS["cooldown_seconds"]))
        ld = self._llm_defaults()
        for key, _label, _hint, kind, _lo, _hi in _LLM_KEYS:
            w = self.llm_widgets[key]
            dv = ld.get(key)
            if kind == "int":
                w.setValue(int(dv))
            elif kind == "float":
                w.setValue(float(dv))
            elif kind == "combo":
                w.setCurrentText(str(dv))
            else:
                w.setChecked(bool(dv))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        y = self._yaml_ai()
        # 显示参数
        for key, _l, _h, _lo, _hi in _PARAM_KEYS:
            v = self.spins[key].value()
            if v != _AI_CHAT_DEFAULTS[key]:
                y[key] = v
            else:
                y.pop(key, None)  # 与默认相同 → 不写盘，保持 yaml 干净
        # 上下文条数 + 冷却（bot 段；与默认相同 → 不写盘）
        bh = self._yaml_bot()
        mh = int(self.sp_max_history.value())
        if mh != _BOT_DEFAULTS["max_history"]:
            bh["max_history"] = mh
        else:
            bh.pop("max_history", None)
        cd = int(self.sp_cooldown.value())
        if cd != _BOT_DEFAULTS["cooldown_seconds"]:
            bh["cooldown_seconds"] = cd
        else:
            bh.pop("cooldown_seconds", None)
        # LLM 调用参数（llm 子段）
        ld = self._llm_defaults()
        llm = {}
        for key, _label, _hint, kind, _lo, _hi in _LLM_KEYS:
            w = self.llm_widgets[key]
            if kind == "int":
                v = int(w.value())
            elif kind == "float":
                v = float(w.value())
            elif kind == "combo":
                v = w.currentText()
            else:
                v = bool(w.isChecked())
            if v != ld.get(key):
                llm[key] = v
        if llm:
            merged = dict((y.get("llm") or {}))
            merged.update(llm)
            for key in list(merged):
                if key not in llm and merged[key] == ld.get(key):
                    merged.pop(key)
            y["llm"] = merged
        else:
            y.pop("llm", None)
        self._commit("参数")


# ============================================================
#  ② 提示词（默认人设 + 角色模板）
# ============================================================
class PromptsDialog(_BaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "📝 AI 聊天 · 提示词（默认人设 / 角色模板）", 860)
        self.setMinimumHeight(700)  # 用户偏好：提示词弹窗要够高
        self._build()

    def _build(self):
        # 左：条目列表（2 项）
        mid = QHBoxLayout()
        mid.setSpacing(10)

        self.lst = QListWidget()
        self.lst.setFixedWidth(250)
        self.lst.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lst.itemSelectionChanged.connect(self._on_select)
        for key, name, _desc, in _PROMPT_KEYS:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, key)
            if (self.mw.yaml_cfg or {}).get(key):
                item.setText(name + " ✏️")  # 已定制标记
            self.lst.addItem(item)
        mid.addWidget(self.lst)

        # 右：说明 + 编辑器 + 占位符提示
        right = QVBoxLayout()
        self.lbl_name = QLabel("—")
        self.lbl_name.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.lbl_name.setWordWrap(True)
        self.lbl_desc = QLabel("")
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setStyleSheet("color: #656d76;")
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
        bar = self._bottom_bar()
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

        self._session_edits: dict[str, str] = {}
        self.ed.textChanged.connect(self._on_text_changed)
        if self.lst.count():
            self.lst.setCurrentRow(0)

    def _on_text_changed(self):
        if getattr(self, "_programmatic_edit", False):
            return
        key = self._cur_key()
        if key:
            self._session_edits[key] = self.ed.toPlainText()

    def _effective(self, key: str) -> str:
        """当前条目应显示的内容：会话编辑 > yaml 定制 > 代码默认。"""
        if key in self._session_edits:
            return self._session_edits[key]
        return self.prompt_effective(key)

    def _cur_key(self):
        items = self.lst.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def _on_select(self):
        key = self._cur_key()
        if not key:
            return
        _k, name, desc, = next(x for x in _PROMPT_KEYS if x[0] == key)
        self.lbl_name.setText(f"{name}（{key}）")
        self.lbl_desc.setText(desc)
        self._programmatic_edit = True
        try:
            self.ed.setPlainText(self._effective(key))
        finally:
            self._programmatic_edit = False
        phs = sorted(set(re.findall(r"\{([a-z_0-9]+)\}",
                                    self.prompt_default(key))))
        self.lbl_ph.setText("占位符（渲染时替换，请勿删除）：" +
                            ("、".join("{" + p + "}" for p in phs) if phs else "无"))

    def _reset_item(self):
        key = self._cur_key()
        if not key:
            return
        was_custom = bool((self.mw.yaml_cfg or {}).get(key)) or key in self._session_edits
        if key in self._session_edits:
            self._session_edits.pop(key, None)
        if was_custom:
            self._programmatic_edit = True
            try:
                self.ed.setPlainText(self.prompt_default(key))
            finally:
                self._programmatic_edit = False
            # 刷新列表定制标记
            for i in range(self.lst.count()):
                if self.lst.item(i).data(Qt.UserRole) == key:
                    _k, name, _d = next(x for x in _PROMPT_KEYS if x[0] == key)
                    self.lst.item(i).setText(name)
                    break
            self._on_select()
            self.mw.statusBar().showMessage(
                f"{key} 已恢复默认（保存后生效）", 5000)
        else:
            self.mw.statusBar().showMessage(f"{key} 本来就是默认值", 4000)

    def _save(self):
        # 校验所有条目：非空 + 占位符完整
        for key, name, _desc in _PROMPT_KEYS:
            text = self._effective(key)
            if not text.strip():
                QMessageBox.warning(self, "提示",
                                    f"「{name}」不能为空（可点「恢复此条默认」）。")
                return
            missing = [p for p in sorted(set(
                re.findall(r"\{([a-z_0-9]+)\}", self.prompt_default(key))))
                if "{" + p + "}" not in text]
            if missing:
                r = QMessageBox.question(
                    self, "占位符检查",
                    f"「{name}」缺少以下占位符：\n"
                    + "、".join("{" + p + "}" for p in missing) +
                    "\n\n渲染时这些位置将原样保留，可能导致 LLM 收到残缺提示词。\n仍要保存吗？",
                    QMessageBox.Yes | QMessageBox.No)
                if r != QMessageBox.Yes:
                    return
        # 落盘：与默认相同 → 不写顶层键（保持 yaml 干净）
        for key, _name, _desc in _PROMPT_KEYS:
            v = self._effective(key)
            if v == self.prompt_default(key):
                self.mw.yaml_cfg.pop(key, None)
            else:
                self.mw.yaml_cfg[key] = v
        self._session_edits.clear()
        self._commit("提示词")
