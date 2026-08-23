"""
worker.py — 通用后台任务线程（GUI 所有耗时操作都走这里，不阻塞 UI）
"""

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    """
    用法:
        w = Worker(some_func, arg1, kw={"a": 1})
        w.finished_ok.connect(on_ok)   # 参数 = 返回值
        w.finished_err.connect(on_err) # 参数 = 错误信息
        w.start()
    """

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, func, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
            self.finished_ok.emit(result)
        except Exception as e:
            self.finished_err.emit(f"{type(e).__name__}: {e}")
