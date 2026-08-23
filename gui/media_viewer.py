"""
media_viewer.py — 消息管理页 · 媒体详情查看器（08-21 新增）
==========================================================
历史消息表格双击行入口（tab_messages._on_row_double_clicked 按 msg_kind 分发，
纯文本 text 无动作，不进这里）：

  - image   → ImageViewer   多图（单条消息最多 114 张）前后导航 + 缩放；
                            GIF 走 QMovie 循环播放；撤回图查 recall_image
  - voice   → AudioViewer   QMediaPlayer 直接播 AMR（PySide6 内置 FFmpeg
                            后端，08-21 实测直接播 .amr 成功，无需预转码）
  - video   → VideoViewer   QMediaPlayer + QVideoWidget（当前 0 个本地文件，
                            显示"未存档"提示 + URL，架子先搭好）
  - forward → ForwardViewer forward_archive 子消息文本（content_text，
                            最多 600 条）；拉取失败/无记录显示对应提示
  - file    → _open_file_row 打开所在文件夹并高亮定位（nautilus <文件>）；
                            文件未落盘 → 打开存档目录 + 信息弹窗
                            （文件名/大小/下载 URL + 复制）

数据一律 SQLite 只读（api_client.query）；文件缺失统一显示"文件未找到"
（存档下载失败 / 保留期清理 / 历史路径迁移——40% 图片路径指向已迁移的
<旧迁移路径> 旧路径，不可恢复，属数据现状非 bug）。
"""

import html
import os
import re
import subprocess
import time

from PySide6.QtCore import Qt, QUrl, QSize
from PySide6.QtGui import QPixmap, QMovie, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QScrollArea, QTextBrowser, QSizePolicy, QWidget,
)
from PySide6.QtMultimedia import QMediaPlayer

import api_client
from worker import Worker

_KIND_TITLE = {"image": "图片", "voice": "语音", "video": "视频", "forward": "消息记录"}

_BTN_QSS = """
QPushButton {
    background: #ffffff; border: 1px solid #d0d7de; border-radius: 6px;
    color: #1f2328; font-size: 13px; font-weight: 500;
    padding: 4px 12px;
}
QPushButton:hover { border-color: #0969da; color: #0969da; }
QPushButton:disabled { color: #afb8c1; background: #f6f8fa; border-color: #d0d7de; }
"""


def _ts_str(t) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except Exception:
        return str(t)


def _fmt_ms(ms) -> str:
    try:
        ms = max(0, int(ms))
    except (TypeError, ValueError):
        return "00:00"
    return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"


def _fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


def _copy_text(text: str):
    cb = QGuiApplication.clipboard()
    if cb:
        cb.setText(text)


def _parent(mw):
    """取弹窗 parent：mw 本身是 QWidget（生产 MainWindow）就用它，
    否则退回 mw._win（e2e MockMW 持有 QMainWindow）。"""
    from PySide6.QtWidgets import QWidget
    if isinstance(mw, QWidget):
        return mw
    return getattr(mw, "_win", None)


def _open_in_file_manager(target: str):
    """打开文件管理器：nautilus 优先（传文件路径=打开所在文件夹并高亮定位），
    xdg-open 兜底。detached 启动，GUI 关闭不受影响。返回 (ok, detail)。"""
    if not os.path.exists(target):
        # 文件不存在时 nautilus <文件> 会弹错误框 → 退化为打开所在目录
        d = os.path.dirname(target)
        if d and os.path.exists(d):
            target = d
        else:
            return False, "路径不存在"
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":99")
    for cmd in (["nautilus", target], ["xdg-open", target]):
        try:
            subprocess.Popen(cmd, env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, cmd[0]
        except FileNotFoundError:
            continue
    return False, "无可用文件管理器（nautilus/xdg-open 均未找到）"


# ============================================================
#  入口
# ============================================================
def open_media(mw, row: dict, raw_message: str = ""):
    """双击行分发（tab_messages 调用）。row 为表格缓存行 dict（含 msg_kind/is_recall）。

    text=纯文本无动作；其余类型按媒体表查询后弹窗。查询均为毫秒级
    （target_id 前缀索引 + 表规模小），主线程直查，无需 Worker。
    """
    kind = row.get("msg_kind") or "text"
    if kind == "text":
        return
    if kind == "image":
        ImageViewer(mw, row).exec()
    elif kind == "voice":
        AudioViewer(mw, row).exec()
    elif kind == "video":
        VideoViewer(mw, row).exec()
    elif kind == "forward":
        ForwardViewer(mw, row).exec()
    elif kind == "file":
        _open_file_row(mw, row, raw_message)


def _fetch_images(cfg, row):
    """该消息的图片记录：普通消息→image_archive；撤回消息→recall_image。"""
    mid, tid = row["message_id"], row["target_id"]
    if row.get("is_recall"):
        return api_client.query(cfg, "chat",
            "SELECT image_url, file_path, file_size FROM recall_image "
            "WHERE message_id=? AND target_id=? ORDER BY id", (mid, tid))
    return api_client.query(cfg, "chat",
        "SELECT image_url, file_path, file_size FROM image_archive "
        "WHERE message_id=? AND target_id=? ORDER BY id", (mid, tid))


# ============================================================
#  公共基类（标题信息条 + 尺寸）
# ============================================================
class _BaseViewer(QDialog):
    def __init__(self, mw, row: dict, title: str, w: int = 900, h: int = 680):
        super().__init__(_parent(mw))
        self.mw = mw
        self.row = row
        self.setWindowTitle(title)
        self.resize(w, h)
        self.setMinimumSize(520, 400)
        self.setModal(True)
        self._workers: list = []

    def _track(self, w):
        self._workers.append(w)
        if hasattr(self.mw, "_track"):
            self.mw._track(w)

    def _info_bar(self, sub: str) -> QLabel:
        bar = QLabel(sub)
        bar.setStyleSheet("color: #656d76; font-size: 13px; padding: 6px 2px;")
        bar.setTextFormat(Qt.PlainText)
        return bar

    def closeEvent(self, ev):  # noqa: N802
        # 释放播放资源，防关闭后继续出声/占内存
        for attr in ("_player",):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
        mv = getattr(self, "_movie", None)
        if mv is not None:
            try:
                mv.stop()
            except Exception:
                pass
        super().closeEvent(ev)


# ============================================================
#  图片查看器
# ============================================================
class ImageViewer(_BaseViewer):
    """单条消息的全部图片：前后导航（循环）+ 缩放 + 在文件夹中显示。

    大图解码走 Worker 线程（防 UI 卡顿）；GIF 用 QMovie 循环播放；
    文件缺失（40% 历史图片路径已失效）显示占位 + 原 URL 复制。
    """

    _MISSING = "文件未找到\n（存档时下载失败 / 保留期清理 / 历史路径迁移）"

    def __init__(self, mw, row: dict):
        super().__init__(mw, row,
            f"图片 — {row['nickname'] or '未知用户'} {_ts_str(row['created_at'])}")
        self._items = _fetch_images(self.mw.cfg, row)
        self._idx = 0
        self._zoom = 1.0
        self._req = 0
        self._orig: QPixmap = QPixmap()
        self._movie: QMovie | None = None
        self._movie_size = (0, 0)      # GIF 原始尺寸（QMovie 无 frameWidth/Height）
        self._current_path = ""
        self._build()
        if not self._items:
            self._show_none()
        else:
            self._load_current()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(8)
        v.addWidget(self._info_bar("双击图片行查看 · ←/→ 方向键可切换 · 滚轮缩放"))

        # 中央显示区（QScrollArea 包 QLabel，widgetResizable 保持居中）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setBackgroundRole(QPalette.NoRole)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("background: #f6f8fa; border-radius: 6px;")
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._scroll.setWidget(self._label)
        v.addWidget(self._scroll, 1)

        # 底部工具栏
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_prev = QPushButton("‹ 上一张")
        self.btn_next = QPushButton("下一张 ›")
        self.lb_pos = QLabel("0/0")
        self.lb_pos.setMinimumWidth(64)
        self.lb_pos.setAlignment(Qt.AlignCenter)
        self.lb_pos.setStyleSheet("font-size: 13px; color: #656d76;")
        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_fit = QPushButton("适配窗口")
        self.lb_zoom = QLabel("100%")
        self.lb_zoom.setMinimumWidth(48)
        self.lb_zoom.setAlignment(Qt.AlignCenter)
        self.lb_zoom.setStyleSheet("font-size: 13px; color: #656d76;")
        self.btn_folder = QPushButton("📁 在文件夹中显示")
        self.btn_copy_url = QPushButton("复制原图 URL")
        for b in (self.btn_prev, self.btn_next, self.btn_zoom_out,
                  self.btn_zoom_in, self.btn_zoom_fit, self.btn_folder, self.btn_copy_url):
            b.setStyleSheet(_BTN_QSS)
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.btn_zoom_out.clicked.connect(lambda: self._zoom_by(1 / 1.25))
        self.btn_zoom_in.clicked.connect(lambda: self._zoom_by(1.25))
        self.btn_zoom_fit.clicked.connect(lambda: self._set_zoom(1.0))
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_copy_url.clicked.connect(self._copy_url)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.lb_pos)
        bar.addWidget(self.btn_next)
        bar.addStretch(1)
        bar.addWidget(self.btn_zoom_out)
        bar.addWidget(self.lb_zoom)
        bar.addWidget(self.btn_zoom_in)
        bar.addWidget(self.btn_zoom_fit)
        bar.addStretch(1)
        bar.addWidget(self.btn_copy_url)
        bar.addWidget(self.btn_folder)
        v.addLayout(bar)

    # ------------------------------------------------------------
    def _item(self) -> dict | None:
        if 0 <= self._idx < len(self._items):
            return self._items[self._idx]
        return None

    def _show_none(self):
        self._label.setText("该消息没有图片存档记录\n（存档时下载开关关闭 / 记录缺失）")
        self._label.setStyleSheet("background: #f6f8fa; border-radius: 6px; color: #656d76; font-size: 14px;")
        self.lb_pos.setText("0/0")
        self._update_btns()

    def _load_current(self):
        self._req += 1
        req = self._req
        it = self._item()
        if it is None:
            return
        path = it.get("file_path") or ""
        self._current_path = path
        self.lb_pos.setText(f"{self._idx + 1}/{len(self._items)}")
        self._update_btns()
        self._set_zoom(1.0)
        if not path or not os.path.exists(path):
            self._show_missing(it)
            return
        if path.lower().endswith(".gif"):
            mv = QMovie()
            # 08-21：PySide6 6.11 QMovie 无 setSource/frameWidth，用 setFileName
            # + QImageReader 取首帧尺寸（QMovie.scaledSize 需显式给原始尺寸）
            from PySide6.QtGui import QImageReader
            rd = QImageReader(path)
            size = rd.size()
            mv.setFileName(path)
            if not mv.isValid():
                self._show_missing(it)
                return
            self._movie = mv
            self._movie_size = (size.width(), size.height())
            self._label.setMovie(mv)
            self._movie.setScaledSize(self._display_size(size.width(), size.height()))
            self._movie.start()
            self._label.setText("")
            return
        # 静态图：Worker 解码
        # 08-21：_decode 只接 path 一个参数——req 不能塞进 Worker args
        # （Worker(func, *args) 会把全部 args 传给 func，多传 req 直接
        #  TypeError → 误走"文件未找到"分支，e2e B1 实测抓到）
        self._label.setText(f"加载图片 {self._idx + 1}/{len(self._items)} …")
        self._label.setStyleSheet("background: #f6f8fa; border-radius: 6px; color: #656d76; font-size: 14px;")
        w = Worker(self._decode, path)
        w.finished_ok.connect(lambda data, _q=req: self._on_decoded(data, _q))
        w.finished_err.connect(lambda e, _q=req: self._on_missing_err(e, _q))
        w.start()
        self._track(w)

    @staticmethod
    def _decode(path: str) -> bytes:
        # 08-21：Worker 线程只读字节（跨线程安全）；QPixmap 是 GUI 类，
        # 必须在主线程构造（跨线程 QPixmap(path) 未定义行为，实测返回空图）
        with open(path, "rb") as f:
            return f.read()

    def _on_decoded(self, data: bytes, req: int):
        if req != self._req:
            return
        pm = QPixmap()
        pm.loadFromData(data)
        if pm.isNull():
            self._show_missing(self._item())
            return
        self._orig = pm
        self._movie = None
        self._movie_size = (0, 0)
        self._label.setPixmap(self._render())
        self._label.setStyleSheet("background: #f6f8fa; border-radius: 6px;")

    def _on_missing_err(self, e: str, req: int):
        if req != self._req:
            return
        self._show_missing(self._item())

    def _show_missing(self, it: dict | None):
        self._orig = QPixmap()
        self._movie = None
        self._movie_size = (0, 0)
        url = (it or {}).get("image_url") or ""
        tip = self._MISSING
        if url:
            tip += f"\n原图 URL（已失效概率高，可尝试复制）:\n{url[:160]}"
        self._label.setText(tip)
        self._label.setStyleSheet("background: #f6f8fa; border-radius: 6px; color: #656d76; font-size: 14px;")
        self.btn_copy_url.setEnabled(bool(url))

    def _update_btns(self):
        n = len(self._items)
        self.btn_prev.setEnabled(n > 1)
        self.btn_next.setEnabled(n > 1)
        it = self._item()
        path = (it or {}).get("file_path") or ""
        self.btn_folder.setEnabled(bool(path and os.path.exists(path)))
        if it is not None and not (path and os.path.exists(path)):
            url = it.get("image_url") or ""
            self.btn_copy_url.setEnabled(bool(url))
        if it is None:
            self.btn_copy_url.setEnabled(False)

    def _step(self, d: int):
        if not self._items:
            return
        self._idx = (self._idx + d) % len(self._items)
        self._load_current()

    def _display_size(self, w: int, h: int) -> QSize:
        avail = self._scroll.viewport().size()
        if w <= 0 or h <= 0 or avail.width() <= 0 or avail.height() <= 0:
            return QSize(w, h)
        s = min(avail.width() / w, avail.height() / h)
        if s > 1:
            s = 1.0          # 适配窗口不放大原图（小图保持原始尺寸居中）
        s *= self._zoom
        return QSize(max(1, int(w * s)), max(1, int(h * s)))

    def _render(self) -> QPixmap:
        if self._orig.isNull():
            return self._orig
        sz = self._display_size(self._orig.width(), self._orig.height())
        return self._orig.scaled(sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _set_zoom(self, z: float):
        self._zoom = min(8.0, max(0.25, z))
        self.lb_zoom.setText(f"{int(self._zoom * 100)}%")
        self._apply_display()

    def _zoom_by(self, f: float):
        self._set_zoom(self._zoom * f)

    def _apply_display(self):
        """按当前缩放重绘（静态图/QMovie/空态安全）"""
        if self._movie is not None and self._movie_size[0] > 0:
            self._movie.setScaledSize(self._display_size(*self._movie_size))
        elif not self._orig.isNull():
            self._label.setPixmap(self._render())

    def resizeEvent(self, ev):  # noqa: N802
        super().resizeEvent(ev)
        self._apply_display()

    def wheelEvent(self, ev):  # noqa: N802
        if self._label.underMouse() or self._scroll.underMouse():
            self._zoom_by(1.25 if ev.angleDelta().y() > 0 else 1 / 1.25)
            ev.accept()
            return
        super().wheelEvent(ev)

    def keyPressEvent(self, ev):  # noqa: N802
        if ev.key() == Qt.Key_Left:
            self._step(-1)
        elif ev.key() == Qt.Key_Right:
            self._step(1)
        else:
            super().keyPressEvent(ev)

    def _open_folder(self):
        if self._current_path and os.path.exists(self._current_path):
            ok, detail = _open_in_file_manager(self._current_path)
            if self.mw and hasattr(self.mw, "statusBar"):
                self.mw.statusBar().showMessage(
                    f"已打开文件管理器（{detail}）" if ok else f"打开失败：{detail}", 4000)

    def _copy_url(self):
        it = self._item()
        url = (it or {}).get("image_url") or ""
        if url:
            _copy_text(url)
            if self.mw and hasattr(self.mw, "statusBar"):
                self.mw.statusBar().showMessage("已复制图片 URL 到剪贴板", 3000)


# ============================================================
#  音频/视频播放器
# ============================================================
def _fetch_media_row(cfg, row, table: str, urlcol: str) -> dict | None:
    """按 message_id+target_id 取媒体表记录（取 file_path 存在的第一条，
    否则第一条兜底）。

    08-21：video_archive 无 status 列（建表时就没加），按表选列。
    """
    mid, tid = row["message_id"], row["target_id"]
    status_col = "status" if table in ("image_archive", "voice_archive") else "'' AS status"
    rows = api_client.query(cfg, "chat",
        f"SELECT {urlcol} AS url, file_path, file_size, {status_col} FROM {table} "
        "WHERE message_id=? AND target_id=? ORDER BY id", (mid, tid))
    if not rows:
        return None
    for r in rows:
        if r.get("file_path") and os.path.exists(r["file_path"]):
            return r
    return rows[0]


class _PlayerViewer(_BaseViewer):
    """语音/视频公共播放器骨架：播放/暂停 + 进度 + 音量 + 信息条。"""

    def __init__(self, mw, row: dict, kind: str, table: str, urlcol: str):
        title = f"{_KIND_TITLE.get(kind, kind)} — {row['nickname'] or '未知用户'} {_ts_str(row['created_at'])}"
        # 08-21：语音弹窗矮+窄（640x420）——音频无画面，宽大会空旷稀疏
        super().__init__(mw, row, title,
                         w=640 if kind == "voice" else 900,
                         h=420 if kind == "voice" else 680)
        self._kind = kind
        self.media = _fetch_media_row(self.mw.cfg, row, table, urlcol)
        self._player = QMediaPlayer()
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.positionChanged.connect(self._on_pos)
        self._player.durationChanged.connect(self._on_dur)
        self._player.errorOccurred.connect(self._on_err)
        self._build()
        self._load_source()

    # ---- 子类实现：中央区 ----
    def _build_center(self, v: QVBoxLayout) -> None:
        raise NotImplementedError

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(8)
        self._build_center(v)

        # 控制条
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_play = QPushButton("▶ 播放")
        self.btn_play.setStyleSheet(_BTN_QSS)
        self.btn_play.setFixedWidth(88)
        self.btn_play.clicked.connect(self._toggle_play)
        self.lb_time = QLabel("00:00 / 00:00")
        self.lb_time.setMinimumWidth(96)
        self.lb_time.setAlignment(Qt.AlignCenter)
        self.lb_time.setStyleSheet("font-size: 13px; color: #656d76;")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self._seek)
        self.slider.valueChanged.connect(lambda _v: None)
        self.lb_vol = QLabel("🔊")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(100)
        self.vol.setFixedWidth(90)
        self.vol.valueChanged.connect(lambda x: self._player.setVolume(x))
        self._info = QLabel("")
        self._info.setStyleSheet("color: #656d76; font-size: 13px;")
        self._info.setWordWrap(True)
        # 08-21：URL 复制按钮（长 URL 不直接显示——无空格长串 wordWrap 不
        # 断行，实测溢出右边缘/被裁切；改"状态提示+复制按钮"绕开）
        self.btn_copy_url = QPushButton("📋 复制 URL")
        self.btn_copy_url.setStyleSheet(_BTN_QSS)
        self.btn_copy_url.clicked.connect(self._copy_url)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.lb_time)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.lb_vol)
        bar.addWidget(self.vol)
        bar.addStretch(1)
        bar.addWidget(self.btn_copy_url)
        v.addLayout(bar)
        v.addWidget(self._info)

    # ---- 数据/播放 ----
    def _load_source(self):
        m = self.media
        if m is None:
            self._info.setText("该消息没有媒体存档记录")
            self._set_playable(False)
            self._media_url = ""
            self.btn_copy_url.setEnabled(False)   # 无记录=无 URL 可复制
            return
        path = m.get("file_path") or ""
        url = m.get("url") or ""
        size = _fmt_size(m.get("file_size"))
        status = m.get("status") or ""
        base = f"{os.path.basename(path) if path else ''}"
        if path:
            self._info.setText(
                f"{base} · {size}" + (f" · 状态 {status}" if status else ""))
        else:
            self._info.setText(
                f"未存档（无本地文件）" + (f" · status={status}" if status else "") +
                " · 完整下载 URL 见右侧复制按钮（QQ 链接有时效性）")
        self._media_url = url
        self.btn_copy_url.setEnabled(bool(url))
        if path and os.path.exists(path):
            self._player.setSource(QUrl.fromLocalFile(path))
            self._set_playable(True)
            self._player.play()
        else:
            self._set_playable(False)

    def _copy_url(self):
        url = (getattr(self, "_media_url", "") or "").strip()
        if not url:
            return
        _copy_text(url)
        if hasattr(self.mw, "statusBar"):
            self.mw.statusBar().showMessage("已复制媒体 URL 到剪贴板", 3000)

    def _set_playable(self, ok: bool):
        self.btn_play.setEnabled(ok)
        self.slider.setEnabled(ok)

    def _toggle_play(self):
        from PySide6.QtMultimedia import QMediaPlayer as _PM
        st = self._player.playbackState()
        if st == _PM.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state(self, st):
        from PySide6.QtMultimedia import QMediaPlayer as _PM
        if st == _PM.PlaybackState.PlayingState:
            self.btn_play.setText("⏸ 暂停")
        elif st == _PM.PlaybackState.PausedState:
            self.btn_play.setText("▶ 继续")
        else:
            self.btn_play.setText("▶ 播放")

    def _on_dur(self, d: int):
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, int(d)))
        self.slider.blockSignals(False)
        self._upd_time()

    def _on_pos(self, p: int):
        self._upd_time()

    def _upd_time(self):
        d = self._player.duration()
        if d > 0 and self.slider.maximum() != d:
            self.slider.blockSignals(True)
            self.slider.setMaximum(d)
            self.slider.blockSignals(False)
        if not self._seeking:
            self.slider.blockSignals(True)
            self.slider.setValue(self._player.position())
            self.slider.blockSignals(False)
        self.lb_time.setText(f"{_fmt_ms(self._player.position())} / {_fmt_ms(d)}")

    def _seek(self, v: int):
        self._seeking = True
        self._player.setPosition(v)
        self.lb_time.setText(f"{_fmt_ms(v)} / {_fmt_ms(self._player.duration())}")

    def _on_err(self, err, msg):
        self._info.setText(f"播放失败：{msg or str(err)}\n（格式/文件问题，可尝试手动用播放器打开）")


class AudioViewer(_PlayerViewer):
    """语音查看器：QMediaPlayer 直播 AMR（内置 FFmpeg 后端，08-21 实测可用）。"""

    def __init__(self, mw, row: dict):
        super().__init__(mw, row, "voice", "voice_archive", "voice_url")
        self._seeking = False

    def _build_center(self, v: QVBoxLayout):
        # 08-21：中央区做"音频展示卡"（大喇叭 + 副标题垂直居中）——
        # 纯 emoji 小图标在 Expanding 区里观感空旷，加副标题+大字号更有存在感
        card = QWidget()
        card.setStyleSheet("background: #f6f8fa; border-radius: 8px;")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 0, 0, 0)
        self._deco = QLabel("🔊")
        self._deco.setAlignment(Qt.AlignCenter)
        self._deco.setStyleSheet("font-size: 88px; background: transparent;")
        sub = QLabel("语音消息（自动播放，底部控制条可暂停/拖动）")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("font-size: 15px; color: #656d76; background: transparent;")
        cv.addStretch(1)
        cv.addWidget(self._deco)
        cv.addWidget(sub)
        cv.addStretch(1)
        v.addWidget(card, 1)


class VideoViewer(_PlayerViewer):
    """视频查看器：QVideoWidget 显示画面（当前生产库 0 本地文件，
    文件缺失时显示提示 + URL，视频存档开启后自动可用）。"""

    def __init__(self, mw, row: dict):
        super().__init__(mw, row, "video", "video_archive", "video_url")
        self._seeking = False

    def _build_center(self, v: QVBoxLayout):
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self._video = QVideoWidget()
        self._video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._player.setVideoOutput(self._video)
        v.addWidget(self._video, 1)

    def _on_err(self, err, msg):
        super()._on_err(err, msg)
        # 无本地文件时不报错（_load_source 未 setSource），仅文件损坏才提示


# ============================================================
#  消息记录（转发）查看器
# ============================================================
class ForwardViewer(_BaseViewer):
    """转发消息查看器：渲染 forward_archive.content_text（逐条 [时间] 昵称: 内容）。

    status: ok=展开成功 / pending=拉取中 / failed=当时拉取失败(URL过期) / empty=空。
    子消息内的图片为占位符 [图片]（子图未单独落库）。
    """

    _STATUS_TIP = {
        "pending": "转发内容正在后台拉取，稍后再试",
        "failed": "转发内容当时拉取失败（QQ 转发 URL 有时效性，过期后不可恢复）",
        "empty": "转发内容为空",
    }

    def __init__(self, mw, row: dict):
        super().__init__(mw, row,
            f"消息记录 — {row['nickname'] or '未知用户'} {_ts_str(row['created_at'])}", h=640)
        self._build()
        self._load()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(8)
        self.lb_head = QLabel("")
        self.lb_head.setStyleSheet("color: #656d76; font-size: 13px;")
        v.addWidget(self.lb_head)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        v.addWidget(self.browser, 1)
        bar = QHBoxLayout()
        self.btn_copy = QPushButton("复制全部")
        self.btn_copy.setStyleSheet(_BTN_QSS)
        self.btn_copy.clicked.connect(self._copy_all)
        bar.addStretch(1)
        bar.addWidget(self.btn_copy)
        v.addLayout(bar)

    def _load(self):
        mid, tid = self.row["message_id"], self.row["target_id"]
        rows = api_client.query(self.mw.cfg, "chat",
            "SELECT content_text, content_json, status, msg_count, fetched_at "
            "FROM forward_archive WHERE message_id=? AND target_id=? "
            "ORDER BY fetched_at DESC LIMIT 1", (mid, tid))
        if not rows:
            self.lb_head.setText("无转发存档记录（存档功能上线前 / 记录缺失）")
            self.browser.setHtml(self._empty_html("该转发消息没有存档记录"))
            self.btn_copy.setEnabled(False)
            return
        r = rows[0]
        status = r.get("status") or "unknown"
        if status == "ok" and (r.get("content_text") or "").strip():
            n = r.get("msg_count") or 0
            self.lb_head.setText(
                f"共 {n} 条子消息 · 拉取于 {_ts_str(r.get('fetched_at'))} · 子消息内图片为 [图片] 占位")
            body = html.escape(r["content_text"])
            self.browser.setHtml(
                '<div style="white-space:pre-wrap; font-family:\'JetBrains Mono\','
                '\'DejaVu Sans Mono\',monospace; font-size:13px; color:#1f2328; line-height:1.55;">'
                f"{body}</div>")
            self._all_text = r["content_text"]
            self.btn_copy.setEnabled(True)
        else:
            tip = self._STATUS_TIP.get(status, f"未知状态：{status}")
            self.lb_head.setText(f"状态：{status}")
            self.browser.setHtml(self._empty_html(tip))
            self.btn_copy.setEnabled(False)

    @staticmethod
    def _empty_html(tip: str) -> str:
        return (f'<div style="color:#9a6700; font-size:14px; padding:20px;">'
                f"⚠️ {html.escape(tip)}</div>")

    def _copy_all(self):
        _copy_text(getattr(self, "_all_text", ""))


# ============================================================
#  文件类型（CQ:file）
# ============================================================
_CQ_FILE_RE = re.compile(
    r"\[CQ:file,file=([^,]+),file_id=([^,]+),file_size=(\d+),url=(.*?)\]")


def _open_file_row(mw, row: dict, raw_message: str = ""):
    """文件消息双击：打开所在文件夹并高亮定位。

    当前实现：文件消息不落盘（仅记录下载 URL，08-21 实测 65 条全部无本地文件），
    所以走兜底——打开存档目录 + 信息弹窗（文件名/大小/URL/复制）。
    逻辑写成通用：若将来文件落盘（file_path 存在），自动 nautilus <文件> 高亮。
    """
    m = _CQ_FILE_RE.search(raw_message or "")
    if m:
        name, _file_id, size, url = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    else:
        name, size, url = "", 0, ""
    # 文件落盘位置（当前未实现下载，统一看存档根目录）
    base_dir = mw.cfg.get("ARCHIVE_BASE_DIR") or os.path.join(os.getcwd(), "data", "archive")
    if name:
        candidate = os.path.join(base_dir, name)
        target = candidate if os.path.exists(candidate) else base_dir
    else:
        target = base_dir
    ok, detail = _open_in_file_manager(target)
    if hasattr(mw, "statusBar"):
        mw.statusBar().showMessage(
            f"已打开文件管理器（{detail}）" if ok else f"打开失败：{detail}", 4000)
    # 信息弹窗
    dlg = QDialog(_parent(mw))
    dlg.setWindowTitle("文件消息")
    dlg.setModal(True)
    dlg.resize(460, 330)
    v = QVBoxLayout(dlg)
    v.setContentsMargins(16, 14, 16, 14)
    v.setSpacing(8)
    title = QLabel("📎 文件消息")
    title.setStyleSheet("font-size: 16px; font-weight: bold; color: #1f2328;")
    v.addWidget(title)
    info = QLabel(
        f"文件名：{name or '（撤回记录，未知文件名）'}\n"
        f"大小：{_fmt_size(size) if size else '—'}\n"
        f"存档时间：{_ts_str(row['created_at'])}"
        f"{'（撤回于该时刻）' if row.get('is_recall') else ''}")
    info.setStyleSheet("color: #1f2328; font-size: 13px;")
    info.setTextFormat(Qt.PlainText)
    v.addWidget(info)
    note = QLabel("该类文件未落盘存档（仅记录下载 URL，QQ 下载链接有时效性，"
                  "过期后不可恢复）。已在文件管理器中打开存档目录。")
    note.setStyleSheet("color: #9a6700; font-size: 12px;")
    note.setWordWrap(True)
    v.addWidget(note)
    if url:
        # 08-21：长 URL 截断预览（无空格长串 wordWrap 不折行，全文显示会溢出
        # 被裁切）；完整链接走"复制 URL"按钮
        url_lb = QLabel(f"下载 URL（前 80 字符，完整链接点右侧复制按钮）：\n{url[:80]}…")
        url_lb.setStyleSheet("color: #656d76; font-size: 11px;")
        url_lb.setTextFormat(Qt.PlainText)
        url_lb.setWordWrap(True)
        v.addWidget(url_lb)
    bar = QHBoxLayout()
    if url:
        btn_copy = QPushButton("复制 URL")
        btn_copy.setStyleSheet(_BTN_QSS)
        btn_copy.clicked.connect(lambda: _copy_text(url))
        bar.addWidget(btn_copy)
    bar.addStretch(1)
    btn_close = QPushButton("关闭")
    btn_close.setStyleSheet(_BTN_QSS)
    btn_close.clicked.connect(dlg.accept)
    bar.addWidget(btn_close)
    v.addLayout(bar)
    dlg.exec()
