#!/usr/bin/env python3
"""
内容审查模块 — 敏感词检测 + 拼音替换（DFA 算法 + pypinyin）。

特点：
- 基于 DFA（确定性有限状态自动机）算法，O(n) 时间复杂度
- 内置常用中文敏感词库
- 支持自定义敏感词
- 零外部依赖（除 pypinyin 外）
"""

import os
import threading
from pypinyin import pinyin, Style

# ============================================================
#  内置敏感词库（QQ 群场景常用）
#  注意：避免单字词和高频普通词（如"日"、"操"、"北京"、"那个"等），
#  否则 DFA 子串匹配会把日常对话误替换成拼音。
# ============================================================
_BUILTIN_WORDS = {
    # 政治类
    "共产党", "毛泽东", "邓小平", "江泽民", "胡锦涛", "习近平",
    "中南海", "国务院",
    # 色情类
    "色情", "裸体", "裸奔", "性交", "口交", "肛交", "乳交",
    "阴茎", "阴道", "肛门", "乳房", "乳头",
    "春药", "自慰", "手淫", "打飞机", "撸管",
    "性爱", "做爱",
    "高潮", "射精", "淫水", "淫叫",
    # QQ 风控高危词（2026-08-12 补：群昵称含这些词导致整条消息被风控拦截/折叠，
    # 典型案例：活跃度排行因"贫乳/萝莉控/人妻"昵称整条消息客户端不可见）
    "贫乳", "萝莉控", "人妻", "勾引", "母性", "萝莉",
    # 脏话类
    "他妈", "傻逼", "傻比", "智障", "脑残", "白痴", "弱智",
    "畜生", "禽兽", "妓女",
    # 广告类（多字词，避免误伤日常用语）
    "微商", "刷单", "加微信", "加QQ", "加Q",
}


# ============================================================
#  DFA 自动机
# ============================================================
class _DFAFilter:
    """基于 DFA 的敏感词过滤器（线程安全）"""

    def __init__(self) -> None:
        self._root: dict = {}
        self._lock = threading.Lock()

    def _add_word(self, word: str) -> None:
        """添加单个敏感词到 DFA 树"""
        node = self._root
        for ch in word:
            if ch not in node:
                node[ch] = {}
            node = node[ch]
        node["#"] = True  # 标记词尾

    def add_words(self, words: set[str]) -> None:
        """批量添加敏感词（线程安全）"""
        with self._lock:
            for word in words:
                self._add_word(word)

    def check_and_replace(self, text: str) -> tuple[str, bool]:
        """
        检测敏感词并替换为拼音。
        返回 (替换后的文本, 是否替换)。
        
        使用 offset 映射机制：result 列表会被拼音替换拉长，
        通过跟踪 result_offset（result 与原文之间的索引偏移量）
        来确保后续替换位置正确。
        """
        if not text:
            return text, False

        result = list(text)
        found = False
        result_offset = 0  # result 相对于原文的索引偏移量

        i = 0
        while i < len(text):
            node = self._root
            start = i
            j = i
            # 向前匹配
            while j < len(text) and text[j] in node:
                node = node[text[j]]
                if "#" in node:
                    # 找到敏感词：text[start:j+1]
                    found = True
                    word = text[start : j + 1]
                    py = "".join(
                        [item[0] for item in pinyin(word, style=Style.NORMAL)]
                    )
                    # 使用 offset 校正后的索引进行替换
                    r_start = start + result_offset
                    r_end = j + 1 + result_offset
                    result[r_start : r_end] = list(py)
                    # 更新偏移量
                    result_offset += len(py) - (j + 1 - start)
                    i = j + 1
                    break
                j += 1
            else:
                i += 1

        return "".join(result), found


# ============================================================
#  全局单例
# ============================================================
_filter = _DFAFilter()
_initialized = False
_init_lock = threading.Lock()

# 审查全局开关（默认关闭）
_enabled = False
_enabled_lock = threading.Lock()

# 自定义敏感词文件路径（v2：GUI 可配 assets.sensitive_words，热生效；未配置时回退 data/ 下默认文件）
_DEFAULT_CUSTOM_WORDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "custom_sensitive_words.txt"
)


def _custom_words_file() -> str:
    from .config import CONFIG
    return CONFIG.get("ASSET_SENSITIVE_WORDS") or _DEFAULT_CUSTOM_WORDS_FILE


def _ensure_init() -> None:
    """懒加载初始化 DFA 自动机（线程安全）"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        # 加载内置词库
        _filter.add_words(_BUILTIN_WORDS)

        # 加载自定义词库
        _words_file = _custom_words_file()
        if os.path.exists(_words_file):
            with open(_words_file, encoding="utf-8") as f:
                custom = {
                    line.strip()
                    for line in f
                    if line.strip() and not line.startswith("#")
                }
                _filter.add_words(custom)

        _initialized = True


def censor_text(text: str) -> str:
    """
    对文本进行敏感词审查，将检测到的敏感词替换为拼音。

    Args:
        text: 待审查的文本

    Returns:
        审查后的文本（敏感词被替换为拼音）
    """
    if not text or not _enabled:
        return text

    _ensure_init()
    result, _ = _filter.check_and_replace(text)
    return result


def censor_text_forced(text: str) -> str:
    """强制审查（无视全局开关，2026-08-12）。

    用于批量群友昵称/排行类消息（活跃度、群像等）——这类消息包含大量
    群友昵称，昵称含 QQ 风控高危词（贫乳/萝莉控/人妻等）会导致整条消息
    被服务端拦截/折叠（客户端不可见），必须强制净化昵称再发送。
    """
    if not text:
        return text
    _ensure_init()
    result, _ = _filter.check_and_replace(text)
    return result


def is_enabled() -> bool:
    """检查审查是否开启"""
    with _enabled_lock:
        return _enabled


def set_enabled(enabled: bool) -> None:
    """设置审查开关"""
    global _enabled
    with _enabled_lock:
        _enabled = enabled


def add_sensitive_words(words: set[str]) -> None:
    """
    动态添加敏感词（运行时热更新）。

    Args:
        words: 敏感词集合
    """
    _filter.add_words(words)


def reload_custom_words() -> int:
    """热加载自定义敏感词（GUI 改 assets.sensitive_words 后调用），返回自定义词数。"""
    global _initialized
    with _init_lock:
        # 重置 DFA 树（add_words 内部自己加锁，此处不可重入）
        with _filter._lock:
            _filter._root = {}
        _filter.add_words(_BUILTIN_WORDS)
        _words_file = _custom_words_file()
        n = 0
        if os.path.exists(_words_file):
            with open(_words_file, encoding="utf-8") as f:
                custom = {
                    line.strip()
                    for line in f
                    if line.strip() and not line.startswith("#")
                }
            _filter.add_words(custom)
            n = len(custom)
        _initialized = True
    return n
