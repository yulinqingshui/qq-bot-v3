"""gui/widgets.py — 通用控件：苹果风格拨动开关（QSwitch）+ 数值框家族。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QBrush
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QDoubleSpinBox, QSizePolicy, QSpinBox,
)


class QSwitch(QCheckBox):
    """苹果风格拨动开关（iOS toggle），替代普通 QCheckBox 打勾项。

    用法与 QCheckBox 基本一致：setChecked / isChecked / setEnabled /
    setToolTip / setChecked(False)；信号用 toggled(bool)（QCheckBox 也有
    该信号，直接 connect 即可）。
    渲染：左侧文字标签 + 右侧 track（on=绿/off=灰）+ 白色 knob 圆。
    """

    _W = 44   # track 宽
    _H = 24   # track 高
    _GAP = 10  # 文字与开关间距

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def mousePressEvent(self, ev):  # noqa: N802
        # 2026-08-21 修复：QCheckBox 父类的点击命中区只覆盖样式认为的
        # indicator+文字（左侧），右侧自绘的开关轨道后半段点不到——
        # 用户点轨道没反应（"开关点不动"）。整个控件改为均可点击切换。
        if ev.button() == Qt.LeftButton and self.isEnabled():
            self.toggle()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 文字标签（左侧，垂直居中）
        color = QColor(24, 28, 32) if self.isEnabled() else QColor(160, 166, 173)
        p.setPen(QPen(color, 0))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self.text()) if self.text() else 0
        ty = (self.height() - fm.height()) // 2 + fm.ascent()
        if self.text():
            p.drawText(0, ty, self.text())

        # 开关画在控件右侧
        on = self.isChecked()
        enabled = self.isEnabled()
        x = self.width() - self._W
        y = (self.height() - self._H) // 2
        track = QColor(0x34, 0xC7, 0x59) if on else QColor(0xE5, 0xE5, 0xEA)
        if not enabled:
            track = QColor(0xB8, 0xE6, 0xC2) if on else QColor(0xF0, 0xF0, 0xF2)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(int(x), int(y), self._W, self._H, self._H / 2, self._H / 2)

        # knob 白色圆
        d = self._H - 4
        kx = x + (self._W - d) if on else x + 2
        ky = y + 2
        # 轻微阴影
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 28)))
        p.drawEllipse(int(kx), int(ky) + 1, d, d)
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.drawEllipse(int(kx), int(ky), d, d)
        p.end()

    def sizeHint(self):  # noqa: N802
        from PySide6.QtWidgets import QApplication
        fm = QApplication.fontMetrics()
        tw = fm.horizontalAdvance(self.text()) if self.text() else 0
        return QSize(tw + self._GAP + self._W, self._H + 6)


def flash_button(btn, text: str = "✅ 已保存", ms: int = 1400):
    """按钮点击反馈：短时间把文字换成 text（默认"✅ 已保存"），ms 毫秒后还原。

    重复点击时重启动画（不叠加）。用于保存类按钮的"已点上了"确认反馈。
    """
    from PySide6.QtCore import QTimer

    if not hasattr(btn, "_flash_orig"):
        btn._flash_orig = btn.text()
        btn._flash_timer = QTimer(btn)
        btn._flash_timer.setSingleShot(True)
        btn._flash_timer.timeout.connect(
            lambda: (btn.setText(btn._flash_orig), btn.setEnabled(True)))
    btn._flash_timer.stop()
    btn.setText(text)
    btn._flash_timer.start(ms)


class NoWheelSpinBox(QSpinBox):
    """禁用滚轮改值的 QSpinBox（Qt6 无 setWheelEnabled，需重写 wheelEvent）。

    用户要求：LLM 的 max_tokens / 并发数值不能被鼠标滚轮误改，
    只能点箭头或直接键入。
    """

    def wheelEvent(self, ev):  # noqa: N802
        # 不转发给父类 → 滚轮在此控件上无效
        ev.ignore()


# ============================================================
#  无箭头数值框家族（2026-08-21 从 persona_settings_dialogs.py 抽公共，
#  人设画像弹窗与真心话大冒险弹窗共用；用户偏好：数值框无上下箭头、禁滚轮）
# ============================================================
class NoArrowSpinBox(QSpinBox):
    """QSpinBox：无箭头按钮 + 禁用滚轮。改值只能直接输入/拖选。"""

    def wheelEvent(self, e):  # noqa: N802 — Qt 回调签名
        e.ignore()


class NoArrowDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox：同上。"""

    def wheelEvent(self, e):  # noqa: N802
        e.ignore()


def no_wheel_spin(s):
    """数值框统一交互：取消上下箭头按钮 + 取消鼠标滚轮调节。"""
    s.setButtonSymbols(QAbstractSpinBox.NoButtons)
    try:
        s.setWheelEnabled(False)  # 有该方法的新版本直接禁用
    except AttributeError:
        pass  # 无该方法的版本由子类 wheelEvent 兜底
    return s


def int_spin(value: int, lo: int, hi: int) -> NoArrowSpinBox:
    """整型数值框（130px 定宽，无箭头，禁滚轮）。"""
    s = NoArrowSpinBox()
    s.setRange(lo, hi)
    s.setValue(int(value))
    s.setFixedWidth(130)
    return no_wheel_spin(s)


def float_spin(value: float, lo: float, hi: float, decimals: int = 2) -> NoArrowDoubleSpinBox:
    """浮点数值框（130px 定宽，无箭头，禁滚轮，步长 0.1）。"""
    s = NoArrowDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(0.1)
    s.setValue(float(value))
    s.setFixedWidth(130)
    return no_wheel_spin(s)
