"""
gui_launcher.py — QQ Bot 控制台 GUI 入口
========================================
用法:
    python3 gui_launcher.py            # 启动控制台（bot 可手动/自动启动）
    python3 gui_launcher.py --start    # 启动控制台并自动拉起 bot
"""

import os
import sys

# 项目根入 sys.path（支持直接 python3 gui_launcher.py）
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# gui 目录入 sys.path（gui 内部模块平铺 import：import api_client / process_manager …）
_GUI_DIR = os.path.join(_PROJECT_ROOT, "gui")
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from gui.main_window import MainWindow
from gui.theme import load_theme


def main():
    # 挂 VNC（Xvfb）场景修复：进程环境可能残留 WAYLAND_DISPLAY（继承自
    # GDM 桌面会话），Qt6 会优先 qtwayland 后端把窗口建到 Wayland 桌面
    # 而非 Xvfb → VNC 里看不到窗口。强制 xcb 走 X11 连 :99。
    # 仅「Linux + 有 DISPLAY + 有 WAYLAND_DISPLAY + 未显式指定平台」时生效：
    #   Windows(win32) 不触发（无 DISPLAY）；纯 X11 桌面不触发（无 WAYLAND_DISPLAY）。
    if sys.platform.startswith("linux") and os.environ.get("DISPLAY") \
            and os.environ.get("WAYLAND_DISPLAY") \
            and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    # 高 DPI 支持（Windows 缩放）
    from PySide6.QtCore import Qt
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("QQ Bot 控制台")
    app.setApplicationVersion("3.0.0")
    # 全局主题（GitHub Light 风格；各页内联样式优先级更高，状态色不受影响）
    app.setStyleSheet(load_theme())
    # 中文字体回退
    font = QFont()
    font.setFamily("Noto Sans CJK SC")
    font.setPointSize(10)
    app.setFont(font)

    win = MainWindow()
    win.show()

    if "--start" in sys.argv:
        win.tab_overview._start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
