"""
theme.py — GUI 全局样式表（QSS）
================================
GitHub Light 风格：白卡片 + 浅灰底 + 蓝色强调 + 圆润控件。
应用方式：gui_launcher.py 中 app.setStyleSheet(load_theme())
各页面已有的内联 setStyleSheet 优先级更高，状态色（绿/橙/红）不受影响。
"""

# 色板（GitHub Light）
_BG = "#f6f8fa"        # 窗口/页面浅灰底
_CARD = "#ffffff"      # 卡片白
_BORDER = "#d0d7de"    # 边框
_TEXT = "#1f2328"      # 主文字
_MUTED = "#656d76"     # 次要文字
_ACCENT = "#0969da"    # 强调蓝
_ACCENT_HOVER = "#0860c4"
_OK = "#1a7f37"
_WARN = "#9a6700"
_BAD = "#cf222e"

_QSS = f"""
/* ============ 基础 ============ */
QMainWindow, QDialog {{
    background: {_BG};
    color: {_TEXT};
}}
QWidget {{
    color: {_TEXT};
    font-size: 13px;
}}
QLabel {{
    background: transparent;
}}

/* ============ 标签页 ============ */
QTabWidget::pane {{
    border: 1px solid {_BORDER};
    border-radius: 8px;
    background: {_CARD};
    top: -1px;
}}
QTabBar {{
    qproperty-drawBase: 0;
}}
/* 08-20：标签页文字加粗。
   08-20 实测修正：QSS 字号/字重写在 QTabBar::tab 上【生效】
   （此前注释误记为不生效，系测试污染：选中态被 :selected 规则覆盖）；
   widget setFont 方案反而会被 QSS 字号完全压制，弃用。
   08-21：17px→16px（用户要求标题减小一号）
   08-22：16px→15px（用户要求再小一号）。
   注意：只影响主窗口标签栏；内层小标签（游戏/角色扮演页）走
   _compact_tabs widget 级 12px 样式，不受此处影响。 */
QTabBar::tab {{
    background: transparent;
    color: {_MUTED};
    padding: 9px 16px 11px 16px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-size: 15px;
    font-weight: bold;
}}
QTabBar::tab:hover {{
    background: rgba(9, 105, 218, 0.06);
    color: {_TEXT};
}}
QTabBar::tab:selected {{
    background: {_CARD};
    color: {_ACCENT};
    border: 1px solid {_BORDER};
    border-bottom: 2px solid {_ACCENT};
}}

/* ============ 按钮 ============ */
QPushButton {{
    background: {_CARD};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    color: {_TEXT};
    font-weight: 500;
}}
QPushButton:hover {{
    background: #f3f4f6;
    border-color: #8c959f;
}}
QPushButton:pressed {{
    background: #ebecf0;
}}
QPushButton:disabled {{
    color: #afb8c1;
    background: {_BG};
    border-color: {_BORDER};
}}

/* 主操作按钮（蓝色）——用 property 标记，页面可 opt-in */
QPushButton[primary="true"] {{
    background: {_ACCENT};
    border: 1px solid {_ACCENT};
    color: white;
}}
QPushButton[primary="true"]:hover {{
    background: {_ACCENT_HOVER};
}}
QPushButton[primary="true"]:pressed {{
    background: #0757b0;
}}

/* 危险按钮（红色描边） */
QPushButton[danger="true"] {{
    background: {_CARD};
    border: 1px solid {_BAD};
    color: {_BAD};
}}
QPushButton[danger="true"]:hover {{
    background: #fff5f5;
}}

/* ============ 输入控件 ============ */
QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox {{
    background: {_CARD};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {_ACCENT};
    selection-color: white;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {_ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: {_BG};
    color: {_MUTED};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border: none;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {_MUTED};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {_CARD};
    border: 1px solid {_BORDER};
    selection-background-color: rgba(9, 105, 218, 0.12);
    selection-color: {_TEXT};
    outline: none;
}}

QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid {_BORDER};
    border-bottom: 1px solid {_BORDER};
    border-bottom-right-radius: 6px;
    background: #f6f8fa;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border-left: 1px solid {_BORDER};
    border-top: 1px solid {_BORDER};
    border-bottom-left-radius...[truncated]
    border: 1px solid {_BORDER};
    border-radius: 5px;
    padding: 4px 10px;
    color: {_MUTED};
}}
QStatusBar::item {{
    border: none;
}}

/* ============ 滚动条（细、圆） ============ */
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #c9d1d9;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #8c959f;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #c9d1d9;
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #8c959f;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ============ 表格 / 列表 / 树 ============ */
QTableWidget, QTableView, QTreeView, QListView, QListView::item {{
    background: {_CARD};
    alternate-background-color: #fafbfc;
    border: 1px solid {_BORDER};
    border-radius: 6px;
    gridline-color: #eaeef2;
    selection-background-color: rgba(9, 105, 218, 0.12);
    selection-color: {_TEXT};
}}
QHeaderView::section {{
    background: #f6f8fa;
    border: none;
    border-bottom: 1px solid {_BORDER};
    border-right: 1px solid #eaeef2;
    padding: 6px 8px;
    font-weight: bold;
    color: {_MUTED};
}}
QTableCornerButton::section {{
    background: #f6f8fa;
    border: none;
}}

/* ============ 文本编辑（日志等） ============ */
QTextEdit, QPlainTextEdit {{
    background: #fbfcfd;
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 6px;
    font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}

/* ============ 菜单 / 提示 ============ */
QMenu {{
    background: {_CARD};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 20px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: rgba(9, 105, 218, 0.1);
}}
QToolTip {{
    background: #24292f;
    color: #f6f8fa;
    border: none;
    border-radius: 5px;
    padding: 5px 8px;
}}
QMessageBox, QInputDialog {{
    background: {_CARD};
}}
"""


def load_theme() -> str:
    return _QSS
