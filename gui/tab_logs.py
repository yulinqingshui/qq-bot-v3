"""
tab_logs.py — 日志页
===================
- 读取 data/bot.log（bot 进程自己写；GUI 子进程模式另有 stdout 管道补充）
- 级别过滤（DEBUG/INFO/WARNING/ERROR）+ 关键词过滤
- 每 2s 增量刷新（只追加新行，不重绘全表）
- 复制/导出
"""

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QComboBox, QLabel, QPlainTextEdit, QCheckBox,
)

import api_client


MAX_LINES = 20000  # 内存上限


class TabLogs(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._offset = 0          # 已读字节偏移
        self._follow = True       # 自动滚动
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(2000)
        self._load_initial()

    def _log_path(self) -> str:
        return os.path.join(os.path.dirname(self.mw.cfg.get("DB_PATH", "data/chat_history.db")), "bot.log")

    def _build(self):
        v = QVBoxLayout(self)

        row = QHBoxLayout()
        self.cmb_level = QComboBox()
        self.cmb_level.addItems(["全部", "INFO", "WARNING", "ERROR"])
        self.ed_kw = QLineEdit()
        self.ed_kw.setPlaceholderText("关键词过滤（留空=全部）")
        self.ed_kw.setFixedWidth(260)
        self.chk_follow = QCheckBox("自动滚动")
        self.chk_follow.setChecked(True)
        self.btn_clear = QPushButton("🧹 清空显示")
        self.btn_copy = QPushButton("📋 复制全部")
        row.addWidget(QLabel("级别:"))
        row.addWidget(self.cmb_level)
        row.addWidget(self.ed_kw)
        row.addWidget(self.chk_follow)
        row.addStretch(1)
        row.addWidget(self.btn_clear)
        row.addWidget(self.btn_copy)
        v.addLayout(row)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setStyleSheet("font-family: monospace; font-size: 12px;")
        v.addWidget(self.view)

        self.lbl_meta = QLabel("")
        v.addWidget(self.lbl_meta)

        self.chk_follow.stateChanged.connect(lambda *_: setattr(self, "_follow", self.chk_follow.isChecked()))
        self.btn_clear.clicked.connect(lambda: self.view.clear())
        self.btn_copy.clicked.connect(self._copy_all)
        self.cmb_level.currentTextChanged.connect(lambda *_: None)  # 过滤在 _render 时应用

    # ------------------------------------------------------------
    def _load_initial(self):
        path = self._log_path()
        if not os.path.exists(path):
            self.append_line(f"[GUI] 日志文件尚未生成: {path}（bot 启动后自动创建）")
            return
        size = os.path.getsize(path)
        # 只读最后 1MB（防大日志卡顿）
        start = max(0, size - 1024 * 1024)
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(start)
            chunk = f.read()
        lines = [l for l in chunk.split("\n") if l.strip()]
        if start > 0:
            self.append_line(f"[GUI] ——— 以下为最近 {len(lines)} 行（文件较大，仅显示尾部）———")
        self._apply_filter_append(lines)
        self._offset = size
        self.lbl_meta.setText(f"{path} · {size // 1024}KB")

    def _tick(self):
        """增量读取新内容"""
        path = self._log_path()
        if not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < self._offset:
            # 文件被截断/轮转
            self._offset = 0
            self.append_line("[GUI] 日志文件已轮转，从头读取")
        if size == self._offset:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset = size
        lines = [l for l in chunk.split("\n") if l.strip()]
        if lines:
            self._apply_filter_append(lines)
        self.lbl_meta.setText(f"{path} · {size // 1024}KB · 已显示 {self.view.blockCount()} 行")

    # ------------------------------------------------------------
    def append_line(self, line: str):
        """子进程 stdout 管道入口（process_manager 信号）"""
        self._apply_filter_append([line])

    def _apply_filter_append(self, lines):
        level = self.cmb_level.currentText()
        kw = self.ed_kw.text().strip()
        out = []
        for l in lines:
            if level != "全部":
                # 日志行格式: YYYY-MM-DD HH:MM:SS [LEVEL] msg
                if f"[{level}]" not in l:
                    continue
            if kw and kw not in l:
                continue
            out.append(l)
        if not out:
            return
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText("\n".join(out) + "\n")
        if self._follow:
            self.view.moveCursor(QTextCursor.End)

    def _copy_all(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.view.toPlainText())
        self.mw.statusBar().showMessage(f"已复制 {self.view.blockCount()} 行日志")
