"""
status_light.py — 状态指示灯组件
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QHBoxLayout, QWidget

# 颜色
GREEN = "#2ecc71"
RED = "#e74c3c"
YELLOW = "#f39c12"
GRAY = "#95a5a6"


class StatusLight(QLabel):
    """圆形状态灯 + 文字标签（自身画圆，文字放右侧）"""

    def __init__(self, default_text: str = "—", parent=None):
        super().__init__(parent)
        self._label = QLabel(default_text)
        self._label.setStyleSheet("font-size: 13px;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(6)
        # 圆点用一个内部 QLabel 画（避免把自己加进自己的布局）
        self._dot = QLabel(self)
        self._dot.setFixedSize(14, 14)
        lay.addWidget(self._dot)
        lay.addWidget(self._label)
        self.setFixedWidth(170)  # 08-20：250→170，与操作按钮合并一行（文字超长自动省略）
        self._set_color(GRAY)

    def _set_color(self, color: str):
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 7px;")

    def set_state(self, state: str, text: str):
        """state: 'ok' | 'warn' | 'off' | 'idle'"""
        color = {"ok": GREEN, "warn": YELLOW, "off": RED, "idle": GRAY}.get(state, GRAY)
        self._set_color(color)
        self._label.setText(text)


class StatusLightRow(QWidget):
    """一行状态灯容器（总览页顶部）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)  # 08-20：16→10，与按钮合并一行后省宽
        self.lights = {}
        for key, name in [
            ("bot", "bot 进程"),
            ("napcat", "NapCat 连接"),
            ("llm", "LLM 后端"),
        ]:
            light = StatusLight()
            light.set_state("idle", name)
            self.lights[key] = light
            lay.addWidget(light)
        lay.addStretch(1)
