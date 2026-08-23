"""
qa_settings_dialogs.py — 查询/分析命令 3 个设置弹窗
====================================================
AI 聊天页右列底栏 🔍 按钮行（2026-08-22 查询/分析命令配置化）：
- 🔍 查询参数（QAParamsDialog）：qa.params 10 项时间窗/分块/截断参数，
  800 宽两列均衡（5:5），数值框无箭头禁滚轮（全局偏好）。
- 🔍 查询LLM（QALLMDialog）：qa.llm 29 项——全局 temperature/timeout +
  query/analysis/group_persona/summary/evaluation/scheduled 六作用域的
  max_tokens/thinking 档位（analysis 另含 merge 三级，scheduled 另含
  retries/json_mode）。两列均衡（左 14 / 右 15）。
- 🔍 查询提示词（QAPromptsDialog）：25 段提示词 6 Tab（查询4/分析7/
  群像5/总结4/评选4/定时1），1000×760 minH700，每 Tab QScrollArea
  内部滚动（/分析 Tab 7 段会超高）；每 Tab 顶「恢复本组默认」+ 底部
  「全部恢复默认」。

保存流程（与 ai_chat / persona / truth_dare 弹窗一致）：改 mw.yaml_cfg
的 qa 段（只写非默认值，等于默认的键 pop 保持 yaml 干净）→
api_client.save_yaml 写盘 → POST /config 通知 bot 热重载 → GUI 侧刷新
QA_CFG / QA_PROMPT_* 键。bot 侧调用点调用时实时读 CONFIG → 热生效
（无需重启）。

边界：yaml 已有顶层 analysis 段（max_rows，消息管理页用）与本页无关；
本页只管 qa 段。
"""

import copy
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget,
    QVBoxLayout, QWidget, QMessageBox,
)

import api_client
from worker import Worker
from widgets import (
    NoArrowSpinBox, NoArrowDoubleSpinBox, no_wheel_spin,
    int_spin, float_spin, flash_button,
)

from core.config import DEFAULTS
from core import qa_prompts as QP

_QA_DEFAULTS = DEFAULTS["qa"]
_THINKING_OPTS = ["on", "off", "low", "max"]

# ============================================================
#  ① 参数 10 项：(key, 显示名, 说明, lo, hi)
#  两列各 5 项（均衡偏好）；说明字段级一行内
# ============================================================
_PARAMS_LEFT = [
    ("query_default_hours", "/查询 默认小时",
     "不带参数 /查询 时回溯多久", 1, 168),
    ("query_hours_max", "/查询 小时上限",
     "/查询n 的 n 拦截上限", 1, 168),
    ("analysis_default_days", "/分析 默认天数",
     "不带天数 /分析 时回溯多久", 1, 365),
    ("analysis_days_max", "/分析 天数上限",
     "/分析n 的 n 拦截上限", 1, 365),
    ("analysis_context_window", "/分析 上下文条数",
     "每条相关消息前后各带 N 条", 0, 20),
]
_PARAMS_RIGHT = [
    ("group_persona_map_threshold", "/群像 Map 阈值（字）",
     "人设合并文本超此值走 Map+Reduce", 2000, 200000),
    ("activity_default_days", "/活跃度 默认天数",
     "不带参数 /活跃度 时统计多久", 1, 365),
    ("map_batch_chars", "Map 分块大小（字）",
     "各命令 Map 阶段每批字符上限", 8000, 200000),
    ("msg_truncate_chars", "消息截断（字）",
     "聊天记录每条消息截取长度", 20, 1000),
    ("report_window_hours", "/总结 /评选 时间窗（小时）",
     "手动指令回溯多久（定时报告不受影响）", 1, 168),
]


def _param_defaults() -> dict:
    return dict(_QA_DEFAULTS["params"])


# ============================================================
#  ② LLM 29 项：(scope, key, 显示名, 说明, kind, lo, hi)
#  scope=_common 为全局公共项；其余六作用域
#  左列 14 项（全局2+查询4+群像4+定时4）/ 右列 15 项（分析7+总结4+评选4）
# ============================================================
_LLM_LEFT = [
    ("_common", "temperature", "全局 temperature",
     "6 命令共用采样温度", "float", 0.0, 2.0),
    ("_common", "timeout", "全局 超时（秒）",
     "单次 LLM 调用 HTTP 超时", "int", 30, 3600),
    ("query", "map_max_tokens", "Map max_tokens",
     "思考+正文共享预算", "int", 256, 262144),
    ("query", "map_thinking", "Map thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("query", "reduce_max_tokens", "Reduce max_tokens",
     "汇总回答预算", "int", 256, 262144),
    ("query", "reduce_thinking", "Reduce thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("group_persona", "map_max_tokens", "Map max_tokens",
     "人设数据分批提取预算", "int", 256, 262144),
    ("group_persona", "map_thinking", "Map thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("group_persona", "reduce_max_tokens", "Reduce max_tokens",
     "汇总回答预算", "int", 256, 262144),
    ("group_persona", "reduce_thinking", "Reduce thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("scheduled", "max_tokens", "max_tokens",
     "合并提取（JSON 双产出）预算", "int", 256, 262144),
    ("scheduled", "thinking", "thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("scheduled", "retries", "重试次数",
     "无效返回重试（含首次）", "int", 1, 20),
    ("scheduled", "json_mode", "json_mode",
     "强制 JSON 输出（默认关）", "check", None, None),
]
_LLM_RIGHT = [
    ("analysis", "map_max_tokens", "Map max_tokens",
     "用户发言分批提取预算", "int", 256, 262144),
    ("analysis", "map_thinking", "Map thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("analysis", "reduce_max_tokens", "Reduce max_tokens",
     "最终回答预算", "int", 256, 262144),
    ("analysis", "reduce_thinking", "Reduce thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("analysis", "merge_max_tokens", "合并 max_tokens",
     "线索超长时多级收敛预算", "int", 256, 262144),
    ("analysis", "merge_thinking", "合并 thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("analysis", "merge_retries", "合并重试次数",
     "收敛层网络重试（含首次）", "int", 1, 20),
    ("summary", "map_max_tokens", "Map max_tokens",
     "分批摘要预算", "int", 256, 262144),
    ("summary", "map_thinking", "Map thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("summary", "reduce_max_tokens", "Reduce max_tokens",
     "⚠️ 现状 131072（异类，忠实保留）", "int", 256, 262144),
    ("summary", "reduce_thinking", "Reduce thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("evaluation", "map_max_tokens", "Map max_tokens",
     "五维度候选提取预算", "int", 256, 262144),
    ("evaluation", "map_thinking", "Map thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
    ("evaluation", "reduce_max_tokens", "Reduce max_tokens",
     "最终评选预算", "int", 256, 262144),
    ("evaluation", "reduce_thinking", "Reduce thinking",
     "on 默认 / off 关 / low·max 强度", "combo", None, None),
]
_LLM_SCOPES = ["query", "analysis", "group_persona",
               "summary", "evaluation", "scheduled"]


def _llm_default(scope: str, key: str):
    """qa.llm 默认值（_common=顶层公共项）。"""
    d = _QA_DEFAULTS["llm"]
    if scope == "_common":
        return d.get(key)
    return d.get(scope, {}).get(key)


# ============================================================
#  公共基类：qa 段读写 + 保存落盘 + 热重载
# ============================================================
class _QABaseDialog(QDialog):
    def __init__(self, mw, title: str, width: int = 800):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle(title)
        self.setFixedWidth(width)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)
        self._layout = v

    # ---------- qa 段读写 ----------
    def qa_cfg(self, scope: str | None = None) -> dict:
        """当前值 = 代码默认 与 yaml qa 段子段级合并（缺项回退默认）。

        scope=None → params 段；scope=llm 作用域名 → llm.<scope> 子段；
        scope="_common" → llm 顶层公共项（temperature/timeout）。
        yaml 只存非默认值（浅层），这里按子段合并补全，保证任意 key 可读。
        """
        y = (self.mw.yaml_cfg or {}).get("qa") or {}
        if scope is None:
            return {**_QA_DEFAULTS["params"], **(y.get("params") or {})}
        llm_y = y.get("llm") or {}
        if scope == "_common":
            return {
                **_QA_DEFAULTS["llm"],
                **{k: v for k, v in llm_y.items() if not isinstance(v, dict)},
            }
        return {**_QA_DEFAULTS["llm"][scope], **(llm_y.get(scope) or {})}

    def _yaml_qa(self) -> dict:
        """mw.yaml_cfg 中实际落盘的 qa 段（保存只写用户改过的键）。"""
        y = self.mw.yaml_cfg
        if not isinstance(y.get("qa"), dict):
            y["qa"] = {}
        return y["qa"]

    def prompt_effective(self, key: str) -> str:
        """当前值：yaml qa.prompts 定制 > 代码默认。"""
        v = ((self.mw.yaml_cfg or {}).get("qa") or {}).get("prompts", {}).get(key)
        if isinstance(v, str) and v.strip():
            return v
        return QP.default_prompt(key)

    # ---------- 保存 ----------
    def _commit(self, what: str):
        """写 yaml → POST /config 热重载 → 刷新 GUI 侧 QA_CFG / QA_PROMPT_* 键。"""
        try:
            api_client.save_yaml(self.mw.yaml_cfg)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"config.yaml 写入失败：{e}")
            return
        # GUI 侧同步（刷新 qa 相关键）
        try:
            from core.config import flatten_yaml_tree
            fresh = flatten_yaml_tree(api_client.load_yaml())
        except Exception:
            fresh = {}
        for k, v in fresh.items():
            if k == "QA_CFG" or k.startswith("QA_PROMPT_"):
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

    # ---------- 落盘公共逻辑：只写非默认值（yaml 干净）----------
    def _write_qa_params(self, qa: dict, values: dict):
        p = {}
        for k, v in values.items():
            if v != _param_defaults().get(k):
                p[k] = v
        if p:
            qa["params"] = p
        else:
            qa.pop("params", None)

    def _write_qa_llm(self, qa: dict, common_values: dict,
                      scope_values: dict[str, dict]):
        """llm 段落盘：先复制旧值（保留未管理键）再覆写，等于默认的键 pop。"""
        llm = dict(qa.get("llm") or {})
        for k, v in common_values.items():
            if v != _llm_default("_common", k):
                llm[k] = v
            else:
                llm.pop(k, None)
        for scope in _LLM_SCOPES:
            sub = dict(llm.get(scope) or {})
            for k, v in scope_values.get(scope, {}).items():
                if v != _llm_default(scope, k):
                    sub[k] = v
                else:
                    sub.pop(k, None)
            if sub:
                llm[scope] = sub
            else:
                llm.pop(scope, None)
        if llm:
            qa["llm"] = llm
        else:
            qa.pop("llm", None)

    def _write_qa_prompts(self, qa: dict, values: dict[str, str]):
        prompts = {}
        for k, v in values.items():
            if v and v != QP.default_prompt(k):
                prompts[k] = v
        if prompts:
            qa["prompts"] = prompts
        else:
            qa.pop("prompts", None)


# ============================================================
#  ① 🔍 查询参数（10 项，两列 5:5）
# ============================================================
class QAParamsDialog(_QABaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🔍 查询参数（6 命令 · 10 项）", 800)
        self._build()

    def _build(self):
        self.spins = {}

        def _col(title: str, keys: list) -> QGroupBox:
            gb = QGroupBox(title)
            form = QFormLayout(gb)
            form.setSpacing(12)
            for key, label, hint, lo, hi in keys:
                row = QHBoxLayout()
                row.setSpacing(8)
                spin = int_spin(self.qa_cfg().get(key, _param_defaults()[key]),
                                lo, hi)
                row.addWidget(spin)
                hl = QLabel(hint)
                hl.setWordWrap(True)
                row.addWidget(hl)
                row.addStretch(1)
                form.addRow(QLabel(label), row)
                self.spins[key] = spin
            return gb

        cols = QHBoxLayout()
        cols.setSpacing(10)
        cols.addWidget(_col("查询 / 分析（5 项）", _PARAMS_LEFT), 1)
        cols.addWidget(_col("群像 / 活跃度 / 分块（5 项）", _PARAMS_RIGHT), 1)
        self._layout.addLayout(cols)

        hint = QLabel(
            "说明：时间窗参数同时作用于解析拦截与 /帮助 文案（热重载后自动同步）；"
            "消息截断与 Map 分块作用于 /查询 /分析 /群像 /总结 /评选 及"
            "消息管理页·消息分析共用链路。默认值 = 当前行为。"
            "thinking：on=后端默认（DeepSeek 默认 max）/ off=关思考 / low·max=思考强度。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
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
        for key, _l, _h, _lo, _hi in _PARAMS_LEFT + _PARAMS_RIGHT:
            self.spins[key].setValue(int(_param_defaults()[key]))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        values = {key: int(self.spins[key].value())
                  for key, _l, _h, _lo, _hi in _PARAMS_LEFT + _PARAMS_RIGHT}
        y = self._yaml_qa()
        self._write_qa_params(y, values)
        self._commit("查询参数")


# ============================================================
#  ② 🔍 查询LLM（29 项，两列 14:15）
# ============================================================
class QALLMDialog(_QABaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🔍 查询LLM（6 命令 · 29 项）", 900)
        self._build()

    def _build(self):
        self.widgets = {}  # (scope, key) -> widget

        def _row(scope, key, label, hint, kind, lo, hi):
            row = QHBoxLayout()
            row.setSpacing(8)
            dflt = _llm_default(scope, key)
            cur = self.qa_cfg(scope).get(key)
            cur = dflt if cur is None else cur  # yaml 值缺失/None 均回退默认
            if kind == "int":
                w = NoArrowSpinBox()
                w.setRange(int(lo), int(hi))
                w.setValue(int(cur))
                w = no_wheel_spin(w)
            elif kind == "float":
                w = NoArrowDoubleSpinBox()
                w.setRange(float(lo), float(hi))
                w.setDecimals(2)
                w.setSingleStep(0.05)
                w.setValue(float(cur))
                w = no_wheel_spin(w)
            elif kind == "combo":
                w = QComboBox()
                w.addItems(_THINKING_OPTS)
                w.setCurrentText(str(cur or "on"))
            else:  # check
                w = QCheckBox()
                w.setChecked(bool(cur))
            row.addWidget(w)
            self.widgets[(scope, key)] = w
            hl = QLabel(hint)
            hl.setWordWrap(True)
            row.addWidget(hl)
            row.addStretch(1)
            return row

        def _col(title: str, items: list) -> QGroupBox:
            gb = QGroupBox(title)
            form = QFormLayout(gb)
            form.setSpacing(10)
            for scope, key, label, hint, kind, lo, hi in items:
                form.addRow(QLabel(label), _row(scope, key, label, hint,
                                                kind, lo, hi))
            return gb

        cols = QHBoxLayout()
        cols.setSpacing(10)
        cols.addWidget(_col("全局 + 查询 + 群像 + 定时（14 项）", _LLM_LEFT), 1)
        cols.addWidget(_col("分析 + 总结 + 评选（15 项）", _LLM_RIGHT), 1)
        self._layout.addLayout(cols)

        hint = QLabel(
            "说明：thinking 档位复用人设链路映射（on=后端默认开思考 / "
            "off=关思考 / low·max=思考强度）。max_tokens 是思考链+正文共享预算。"
            "⚠️ Reduce 16384 + 开思考：大输入时思考链可能吃光预算导致空回复——"
            "遇此情况优先关 thinking 或调大 max_tokens。"
            "/总结 Reduce 现状 131072（比其他命令 Reduce 大一个量级，默认忠实保留）。"
            "本段 max_tokens 是 qa 作用域副本，不碰全局 MAX_TOKENS_LONG/SHORT。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
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

    def _widget_value(self, scope: str, key: str):
        w = self.widgets[(scope, key)]
        if isinstance(w, QCheckBox):
            return bool(w.isChecked())
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, NoArrowDoubleSpinBox):
            return float(w.value())
        return int(w.value())

    def _reset_all(self):
        for scope, key, _label, _hint, _kind, _lo, _hi in _LLM_LEFT + _LLM_RIGHT:
            dflt = _llm_default(scope, key)
            w = self.widgets[(scope, key)]
            if isinstance(w, QCheckBox):
                w.setChecked(bool(dflt))
            elif isinstance(w, QComboBox):
                w.setCurrentText(str(dflt or "on"))
            else:
                w.setValue(float(dflt) if isinstance(w, NoArrowDoubleSpinBox)
                           else int(dflt))
        self.mw.statusBar().showMessage("已恢复默认值（点击「保存并生效」落盘）", 5000)

    def _save(self):
        common = {}
        for scope, key, _l, _h, kind, _lo, _hi in _LLM_LEFT + _LLM_RIGHT:
            if scope == "_common":
                common[key] = self._widget_value(scope, key)
        scopes = {}
        for scope, key, _l, _h, kind, _lo, _hi in _LLM_LEFT + _LLM_RIGHT:
            if scope == "_common":
                continue
            scopes.setdefault(scope, {})[key] = self._widget_value(scope, key)
        y = self._yaml_qa()
        self._write_qa_llm(y, common, scopes)
        self._commit("查询LLM")


# ============================================================
#  ③ 🔍 查询提示词（25 段，6 Tab）
# ============================================================
_EDITOR_H = 340


class QAPromptsDialog(_QABaseDialog):
    def __init__(self, mw):
        super().__init__(mw, "🔍 查询提示词（6 命令 · 25 段）", 1000)
        self.setMinimumHeight(700)
        self._build()

    def _build(self):
        self.editors = {}      # key -> QPlainTextEdit
        self.group_keys = {}   # group -> [key...]
        self.lbl_ph = {}       # key -> 占位符提示 QLabel

        self.tabs = QTabWidget()
        for g in QP.prompt_groups():
            metas = [m for m in QP.prompt_meta() if m["group"] == g]
            self.group_keys[g] = [m["key"] for m in metas]

            page = QWidget()
            pv = QVBoxLayout(page)
            pv.setContentsMargins(4, 4, 4, 4)
            pv.setSpacing(6)

            # 本组恢复默认（Tab 顶）
            gbar = QHBoxLayout()
            gbar.addStretch(1)
            gbtn = QPushButton(f"↺ 恢复「{g}」组默认")
            gbtn.clicked.connect(lambda _c, gg=g: self._reset_group(gg))
            gbar.addWidget(gbtn)
            pv.addLayout(gbar)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            host = QWidget()
            hv = QVBoxLayout(host)
            hv.setSpacing(8)
            for m in metas:
                gb = QGroupBox(f"{m['name']}（{m['key']}）")
                gb_v = QVBoxLayout(gb)
                gb_v.setContentsMargins(8, 6, 8, 8)
                gb_v.setSpacing(4)
                desc = QLabel(m["desc"])
                desc.setWordWrap(True)
                desc.setStyleSheet("color: #656d76; font-size: 12px;")
                gb_v.addWidget(desc)
                ed = QPlainTextEdit()
                f = QFont("Monospace")
                f.setStyleHint(QFont.Monospace)
                f.setPointSize(11)
                ed.setFont(f)
                ed.setFixedHeight(_EDITOR_H)
                ed.setPlainText(self.prompt_effective(m["key"]))
                ed.textChanged.connect(self._on_text_changed)
                gb_v.addWidget(ed)
                self.editors[m["key"]] = ed
                # 占位符提示 + 作用域脚注
                phs = sorted(set(re.findall(r"\{([a-z_0-9]+)\}",
                                            QP.default_prompt(m["key"]))))
                ph = QLabel(
                    ("占位符（渲染时替换，请勿删除）：" +
                     "、".join("{" + p + "}" for p in phs)) if phs else "无占位符")
                ph.setWordWrap(True)
                ph.setStyleSheet("color: #98a2b3; font-size: 11px;")
                gb_v.addWidget(ph)
                self.lbl_ph[m["key"]] = ph
                scope_lbl = QLabel("作用域：" + m["scope"])
                scope_lbl.setStyleSheet("color: #98a2b3; font-size: 11px;")
                scope_lbl.setWordWrap(True)
                gb_v.addWidget(scope_lbl)
                hv.addWidget(gb)
            hv.addStretch(1)
            scroll.setWidget(host)
            pv.addWidget(scroll, 1)

            self.tabs.addTab(page, f"{g}（{len(metas)} 段）")
        self._layout.addWidget(self.tabs, 1)

        hint = QLabel(
            "说明：提示词用字符串替换渲染（非 format）；缺占位符时对应位置原样保留"
            "（保存时警告不阻止）。/查询 组同时作用于「消息管理页·消息分析」"
            "（单一来源）；定时组仅作用于半日报告（11:30/22:30），"
            "其 Reduce 复用总结/评选提示词。保存后热重载即时生效。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #667085;")
        self._layout.addWidget(hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
        btn_reset_all = QPushButton("↺ 全部恢复默认")
        btn_reset_all.clicked.connect(self._reset_all)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        btn_save = QPushButton("💾 保存并生效")
        btn_save.setMinimumWidth(110)
        btn_save.clicked.connect(self._save)
        bar.addWidget(btn_reset_all)
        bar.addWidget(btn_cancel)
        bar.addWidget(btn_save)
        self._layout.addLayout(bar)

        self._session_edits: dict[str, str] = {}

    def _on_text_changed(self):
        # 找到触发的编辑器（信号不带控件名，遍历比对）
        for key, ed in self.editors.items():
            if ed is self.sender():
                self._session_edits[key] = ed.toPlainText()
                break

    def _effective(self, key: str) -> str:
        if key in self._session_edits:
            return self._session_edits[key]
        return self.prompt_effective(key)

    def _reset_group(self, group: str):
        for key in self.group_keys.get(group, []):
            self._session_edits.pop(key, None)
            dflt = QP.default_prompt(key)
            self.editors[key].setPlainText(dflt)
            self._yaml_qa().setdefault("prompts", {}).pop(key, None)
        self.mw.statusBar().showMessage(f"「{group}」组已恢复默认（保存后生效）", 5000)

    def _reset_all(self):
        for group in list(self.group_keys):
            self._reset_group(group)

    def _save(self):
        # 校验：非空 + 占位符完整（警告不阻止）
        meta_map = {m["key"]: m for m in QP.prompt_meta()}
        for key in QP.prompt_keys():
            text = self._effective(key)
            name = meta_map[key]["name"]
            if not text.strip():
                QMessageBox.warning(self, "提示",
                                    f"「{name}」不能为空（可点「恢复默认」）。")
                return
            missing = [p for p in sorted(set(
                re.findall(r"\{([a-z_0-9]+)\}", QP.default_prompt(key))))
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
        values = {key: self._effective(key) for key in QP.prompt_keys()}
        y = self._yaml_qa()
        self._write_qa_prompts(y, values)
        self._session_edits.clear()
        self._commit("查询提示词")
