#!/usr/bin/env python3
"""
谐音梗游戏模块 - 独立于主程序，提供"看图猜谐音梗"游戏功能。
集成到 bot.py 的群聊消息流中使用。
"""

import base64
import csv
import io
import os
import re
import random
import time
import asyncio
import logging
from PIL import Image
from typing import Optional
from contextlib import contextmanager
import sqlite3
import threading

logger = logging.getLogger(__name__)

# ============================================================
#  路径配置：GUI 可配 assets.pun_dir，热生效；未配置时回退程序目录下 pun_bank/
# ============================================================
_DEFAULT_PUN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pun_bank")


def _pun_base_dir() -> str:
    from core.config import CONFIG
    d = CONFIG.get("ASSET_PUN_DIR") or _DEFAULT_PUN_DIR
    return d


def _pun_csv_path() -> str:
    return os.path.join(_pun_base_dir(), "文字题库.csv")


def _pun_image_dir() -> str:
    return os.path.join(_pun_base_dir(), "图片题库")

# 图片质量压缩目标（KB）
IMAGE_MAX_SIZE_KB = 150

# ============================================================
#  拼音字典加载
# ============================================================
_PINYIN_MAP: dict[str, str] = {}


def _load_pinyin_map():
    """加载 pinyin.txt -> {字: 拼音带声调}"""
    pinyin_file = os.path.join(_pun_base_dir(), "pinyin.txt")
    try:
        with open(pinyin_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    char, py = line.split("=", 1)
                    _PINYIN_MAP[char] = py
        logger.info(f"拼音字典加载完成，共 {len(_PINYIN_MAP)} 条")
    except FileNotFoundError:
        logger.warning(f"拼音字典未找到: {pinyin_file}，谐音梗半对检测将不可用")
    except Exception as e:
        logger.error(f"拼音字典加载失败: {e}")


def _strip_tone(py: str) -> str:
    """去掉拼音声调数字，保留字母部分（如 'hao3' -> 'hao'）"""
    return re.sub(r"\d+$", "", py)


def _get_pinyin(char: str) -> str:
    """获取单个字的拼音（带声调），找不到返回空串"""
    return _PINYIN_MAP.get(char, "")


_load_pinyin_map()

# ============================================================
#  题库加载
# ============================================================
def _load_question_bank():
    """加载文字题库（GBK 编码），过滤掉图片序号为空的题目"""
    questions = []
    try:
        with open(_pun_csv_path(), encoding="gbk") as f:
            reader = csv.reader(f)
            next(reader, None)  # 跳过表头行
            for row in reader:
                if len(row) >= 6:
                    image_seq = row[5].strip()
                    if image_seq:
                        questions.append({
                            "word": row[0].strip(),
                            "pic1_desc": row[1].strip(),
                            "pun": row[2].strip(),
                            "pinyin": row[3].strip(),
                            "pun_type": row[4].strip(),
                            "image_seq": image_seq,
                        })
    except FileNotFoundError:
        logger.error(f"谐音梗题库未找到: {_pun_csv_path()}（请在 GUI 配置 assets.pun_dir 指向题库目录）")
    except Exception as e:
        logger.error(f"谐音梗题库加载失败: {e}")
    return questions


_QUESTION_BANK = _load_question_bank()
logger.info(f"谐音梗题库加载完成，共 {len(_QUESTION_BANK)} 道有效题目")


def reload_question_bank():
    global _QUESTION_BANK
    _QUESTION_BANK = _load_question_bank()
    return len(_QUESTION_BANK)


# ============================================================
#  答题记录数据库
# ============================================================
import sqlite3
import threading

# 数据库路径（与 chat_history.db 同一位置）
_RECORDS_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pun_game_records.db")

_db_lock = threading.Lock()


@contextmanager
def _pun_get_db():
    """答题记录数据库上下文管理器"""
    os.makedirs(os.path.dirname(_RECORDS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_RECORDS_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 外部函数：获取用户昵称（由 bot.py 注册）
_get_nickname_fn: Optional[callable] = None  # type: ignore


def _init_db():
    """初始化答题记录数据库"""
    os.makedirs(os.path.dirname(_RECORDS_DB_PATH), exist_ok=True)
    with _db_lock:
        with _pun_get_db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    question_seq TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    is_correct INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user ON records(user_id);
                CREATE INDEX IF NOT EXISTS idx_group ON records(group_id);

                -- 题目出题次数记录（用于加权选题）
                CREATE TABLE IF NOT EXISTS question_ask_count (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_seq TEXT NOT NULL UNIQUE,
                    ask_count INTEGER NOT NULL DEFAULT 0
                );
            """)


_init_db()


def register_get_nickname(func: callable):  # type: ignore
    """注册获取用户昵称的回调函数（bot.py 调用此函数注册）"""
    global _get_nickname_fn
    _get_nickname_fn = func


def _get_nickname(user_id: int, group_id: int) -> str:
    """获取用户昵称（优先用回调，否则用 user_id）"""
    if _get_nickname_fn is not None:
        try:
            nickname = _get_nickname_fn(user_id, group_id)
            if nickname:
                return nickname
        except Exception:
            pass
    return str(user_id)


def _record_answer(user_id: int, group_id: int, question_seq: str, answer: str, is_correct: bool):
    """记录一次答题"""
    try:
        with _db_lock:
            with _pun_get_db() as conn:
                conn.execute(
                    "INSERT INTO records (user_id, group_id, question_seq, answer, is_correct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, group_id, question_seq, answer, 1 if is_correct else 0, time.time())
                )
    except Exception as e:
        logger.error(f"记录答题失败: {e}")


def _get_leaderboard(group_id: int = 0, limit: int = 5) -> list[dict]:
    """
    获取排行榜
    返回：[{user_id, nickname, correct, total, accuracy}, ...]
    """
    try:
        with _db_lock:
            with _pun_get_db() as conn:
                conn.row_factory = sqlite3.Row
                if group_id:
                    rows = conn.execute(
                        """SELECT user_id,
                                  SUM(is_correct) as correct,
                                  COUNT(*) as total,
                                  ROUND(CAST(SUM(is_correct) * 100.0 / COUNT(*) AS REAL), 1) as accuracy
                           FROM records WHERE group_id = ?
                           GROUP BY user_id
                           HAVING correct > 0
                           ORDER BY correct DESC, accuracy DESC
                           LIMIT ?""",
                        (group_id, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT user_id,
                                  SUM(is_correct) as correct,
                                  COUNT(*) as total,
                                  ROUND(CAST(SUM(is_correct) * 100.0 / COUNT(*) AS REAL), 1) as accuracy
                           FROM records
                           GROUP BY user_id
                           HAVING correct > 0
                           ORDER BY correct DESC, accuracy DESC
                           LIMIT ?""",
                        (limit,)
                    ).fetchall()

            result = []
            for row in rows:
                user_id = row["user_id"]
                nickname = _get_nickname(user_id, group_id)
                result.append({
                    "user_id": user_id,
                    "nickname": nickname,
                    "correct": row["correct"],
                    "total": row["total"],
                    "accuracy": row["accuracy"],
                })
            return result
    except Exception as e:
        logger.error(f"查询排行榜失败: {e}")
        return []


def _increment_ask_count(question_seq: str):
    """题目出题次数 +1（持久化到数据库）"""
    try:
        with _db_lock:
            with _pun_get_db() as conn:
                conn.execute(
                    """INSERT INTO question_ask_count (question_seq, ask_count)
                       VALUES (?, 1)
                       ON CONFLICT(question_seq) DO UPDATE SET ask_count = ask_count + 1""",
                    (question_seq,)
                )
    except Exception as e:
        logger.error(f"更新出题次数失败: {e}")


def _get_ask_counts() -> dict[str, int]:
    """从数据库加载所有题目的出题次数 {question_seq: count}"""
    counts: dict[str, int] = {}
    try:
        with _db_lock:
            with _pun_get_db() as conn:
                rows = conn.execute(
                    "SELECT question_seq, ask_count FROM question_ask_count"
                ).fetchall()
        for seq, count in rows:
            counts[seq] = count
    except Exception as e:
        logger.error(f"查询出题次数失败: {e}")
    return counts


# 题目出题权重衰减系数
_WEIGHT_DECAY = 0.8
_WEIGHT_BASE = 200


def _format_leaderboard(group_id: int) -> str:
    """格式化排行榜文本"""
    leaderboard = _get_leaderboard(group_id)
    if not leaderboard:
        return ""

    lines = ["\n🏆 排行榜 TOP5", "──────────"]
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(leaderboard):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {entry['nickname']} — 做对{entry['correct']}题/{entry['total']}次 (准确率{entry['accuracy']}%)")
    return "\n".join(lines)


# ============================================================
#  游戏状态管理
# ============================================================
_PUN_GAMES: dict[int, dict] = {}

# 全局轮次计数器（每次出题 +1）
_ROUND_COUNTER: int = 0

# 题目历史记录：{image_seq: round_number}
_QUESTION_HISTORY: dict[str, int] = {}

# 历史记录窗口
_HISTORY_WINDOW = 100


def _get_game(group_id: int) -> dict | None:
    return _PUN_GAMES.get(group_id)


def _set_game(group_id: int, game: dict):
    _PUN_GAMES[group_id] = game


def _end_game(group_id: int):
    if group_id in _PUN_GAMES:
        # 取消定时器任务，防止旧定时器醒来后误操作
        game = _PUN_GAMES[group_id]
        task = game.get("timeout_task")
        if task and not task.done():
            task.cancel()
        del _PUN_GAMES[group_id]


def is_active(group_id: int) -> bool:
    game = _get_game(group_id)
    return game is not None and game.get("active", False)


# ============================================================
#  出题功能
# ============================================================
QUESTION_TIMEOUT_SECONDS = 120  # 2 分钟超时


def _weighted_choice() -> dict:
    """
    加权随机选题：
    - 权重 = _WEIGHT_BASE × _WEIGHT_DECAY^count（持久化出题次数）
      例如做过 1 次 → 160，做过 5 次 → 82，做过 10 次 → 33
    - 最近 N 轮做过的题目额外施加冷却系数
    - 超过 _HISTORY_WINDOW 轮的题目冷却系数恢复为 1
    """
    global _ROUND_COUNTER
    current_round = _ROUND_COUNTER

    ask_counts = _get_ask_counts()

    weights = []
    for q in _QUESTION_BANK:
        seq = q["image_seq"]

        # 基础权重：根据历史出题次数衰减
        count = ask_counts.get(seq, 0)
        weight = _WEIGHT_BASE * (_WEIGHT_DECAY ** count)

        # 冷却惩罚：最近做过，额外降低权重
        last_round = _QUESTION_HISTORY.get(seq, 0)
        if last_round != 0:
            rounds_ago = current_round - last_round
            if rounds_ago <= _HISTORY_WINDOW:
                weight *= (rounds_ago / _HISTORY_WINDOW)

        weights.append(weight)

    # random.choices 支持加权随机
    return random.choices(_QUESTION_BANK, weights=weights, k=1)[0]


def draw_question(group_id: int, creator_id: int = 0) -> dict | None:
    if not _QUESTION_BANK:
        return None
    global _ROUND_COUNTER
    _ROUND_COUNTER += 1

    question = _weighted_choice()
    # 记录这道题的出题轮次（内存冷却）
    _QUESTION_HISTORY[question["image_seq"]] = _ROUND_COUNTER
    # M16 修复：清理超出历史窗口的旧条目，防止 dict 无限增长
    stale_seqs = [seq for seq, rnd in _QUESTION_HISTORY.items() if rnd <= _ROUND_COUNTER - _HISTORY_WINDOW]
    for seq in stale_seqs:
        _QUESTION_HISTORY.pop(seq, None)
    # 持久化出题次数（数据库权重衰减）
    _increment_ask_count(question["image_seq"])

    game = {
        "question": question,
        "active": True,
        "creator_id": creator_id,
        "user_attempts": {},  # {user_id: attempt_count}
        "start_time": time.time(),  # 出题时间戳
    }
    _set_game(group_id, game)
    return question


# ============================================================
#  消息构建
# ============================================================
def _compress_image_to_base64(image_path: str, max_size_kb: int = IMAGE_MAX_SIZE_KB) -> str:
    """
    读取图片并压缩为 JPEG，返回 base64 编码字符串。
    目标大小：max_size_kb KB
    """
    with Image.open(image_path).convert("RGB") as img:
        # 计算目标尺寸（保持宽高比）
        max_bytes = max_size_kb * 1024
        quality = 85
        scale = 1.0
        buf = None

        # M19 修复：原实现 quality<=30 分支是死代码（35→25 即重置），高熵大图在
        # scale≈0.107 退出时仍可能超限。改为显式收敛：quality 降到 10 才缩 scale，
        # scale 降到 0.05 才退出，每个档位都检查 size。
        while scale > 0.05:
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            resized = img.resize((new_w, new_h), Image.LANCZOS)

            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality)
            size = len(buf.getvalue())

            if size <= max_bytes:
                return base64.b64encode(buf.getvalue()).decode("utf-8")

            # 缩小质量或尺寸
            if quality > 10:
                quality -= 10
            else:
                scale *= 0.8
                quality = 85

        # 极限压缩后仍超限：返回最后一次结果（尽力而为，宁发小图不空）
        if buf is not None:
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        return ""


def build_question_segments(question: dict) -> list[dict]:
    """
    构建出题消息的 ArrayMessage 消息段列表。
    发送两张图片 + 文字提示。
    """
    word = question["word"]
    pic1_desc = question["pic1_desc"]
    pun_type = question["pun_type"]
    image_seq = question["image_seq"]

    # 下划线个数 = 正确词语字数的两倍
    blanks = "_" * (len(word) * 2)
    hint = f"这是{pic1_desc}，这是{blanks}（{len(word)}字{pun_type}短语）"

    segments: list[dict] = []

    # 添加两张图片
    img1_path = os.path.join(_pun_image_dir(), f"w{image_seq}-1.png")
    img2_path = os.path.join(_pun_image_dir(), f"w{image_seq}-2.png")

    for img_path in [img1_path, img2_path]:
        if os.path.exists(img_path):
            b64 = _compress_image_to_base64(img_path)
            segments.append({
                "type": "image",
                "data": {"file": f"base64://{b64}"},
            })
            logger.info(f"谐音梗图片已添加: {img_path}")
        else:
            logger.warning(f"谐音梗图片不存在: {img_path}")

    # 添加文字提示
    segments.append({"type": "text", "data": {"text": hint}})

    logger.info(f"谐音梗出题: {hint}")
    return segments


def build_answer_reveal(question: dict) -> str:
    word = question["word"]
    pun = question["pun"]
    pinyin = question["pinyin"]
    pun_type = question["pun_type"]
    lines = [
        "🎉 公布答案！",
        f"正确词语：{word}",
        f"谐音梗：{pun}（{pinyin}）",
        f"类型：{pun_type}",
        "",
        "发送新答案或 /谐音梗 继续游戏~",
    ]
    return "\n".join(lines)


# ============================================================
#  答题功能
# ============================================================
def _compare_answer(correct_word: str, user_answer: str, correct_pinyin: str) -> list[str]:
    """
    逐字对比正确答案和用户回答，返回每个位置的标记列表。
    - √ : 字完全正确
    - ⍻ : 字不对但拼音相同（忽略声调）
    - x : 字不对且拼音不同
    - ? : 用户未填/缺字
    - + : 用户多填了字
    """
    # 获取正确答案每个字的拼音
    correct_pinyin_list = correct_pinyin.split()
    result: list[str] = []
    min_len = min(len(correct_word), len(user_answer))

    for i in range(min_len):
        c_correct = correct_word[i]
        c_user = user_answer[i]
        if c_correct == c_user:
            result.append("√")
        else:
            # 比较拼音（忽略声调）
            py_correct = _strip_tone(correct_pinyin_list[i]) if i < len(correct_pinyin_list) else ""
            py_user = _strip_tone(_get_pinyin(c_user))
            if py_correct and py_user and py_correct == py_user:
                result.append("⍻")  # 拼音对但字不对
            else:
                result.append("x")

    # 用户回答短于正确答案 -> 缺字
    if len(user_answer) < len(correct_word):
        result.extend(["?"] * (len(correct_word) - len(user_answer)))
    # 用户回答长于正确答案 -> 多字
    elif len(user_answer) > len(correct_word):
        result.extend(["+"] * (len(user_answer) - len(correct_word)))

    return result


def _build_feedback(correct_word: str, user_answer: str, correct_pinyin: str,
                    question: dict, remaining: int, total: int) -> str:
    """构建逐字对比的反馈信息
    返回两行或三行：
      第1行：用户回答 + 逐字标记（每个字中间加空格）
      第2行：剩余尝试次数 / 总尝试次数
      第3行（可选）：如果有半对号，提示"⍻表示仅拼音正确"
    """
    marks = _compare_answer(correct_word, user_answer, correct_pinyin)

    # 构建逐字对比行：用户回答 + 标记，每个字中间加空格
    parts: list[str] = []
    for i, char in enumerate(user_answer):
        parts.append(char + marks[i])
    # 缺字部分
    for i in range(len(user_answer), len(marks)):
        if marks[i] == "?":
            parts.append("？？")
        elif marks[i] == "+":
            parts.append("+")

    comparison = " ".join(parts)

    lines = [comparison, f"剩余尝试：{remaining}/{total}"]

    # 如果有半对号，第三行加提示
    if "⍻" in marks:
        lines.append("⍻表示仅拼音正确")

    return "\n".join(lines)


def check_answer(group_id: int, user_answer: str, user_id: int = 0) -> str | None:
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    # 检查超时（2分钟自动公布答案）
    elapsed = time.time() - game.get("start_time", time.time())
    if elapsed >= QUESTION_TIMEOUT_SECONDS:
        # BUG 修复（2026-08-03）：原实现调用 reveal_answer 后丢弃返回值，
        # 且 reveal 内部 _end_game 会 cancel 掉 router 的自动公布定时器 →
        # 答案永远不显示。改为直接返回 reveal 的答案文本。
        reveal_text = reveal_answer(group_id)
        if reveal_text:
            return reveal_text
        return "⏰ 答题时间到！答案已公布~"

    question = game["question"]
    correct_word = question["word"]
    correct_pinyin = question["pinyin"]
    user_attempts = game.get("user_attempts", {})

    def normalize(s: str) -> str:
        return "".join(c for c in s if c.isalnum() or c in "··")

    normalized_answer = normalize(user_answer)
    normalized_correct = normalize(correct_word)

    if normalized_answer == normalized_correct:
        # 记录答题成功
        _record_answer(user_id, group_id, question["image_seq"], normalized_answer, True)
        _end_game(group_id)
        lines = [
            "✅ 答对了！",
            f"答案：{correct_word}",
            f"谐音梗：{question['pun']}（{question['pinyin']}）",
            f"类型：{question['pun_type']}",
        ]
        # 追加排行榜
        lb = _format_leaderboard(group_id)
        if lb:
            lines.append(lb)
        lines.append("发送 /谐音梗 开始下一题~")
        return "\n".join(lines)
    else:
        # 更新该用户的尝试次数
        current_attempts = user_attempts.get(user_id, 0) + 1
        user_attempts[user_id] = current_attempts
        game["user_attempts"] = user_attempts

        # 记录答题失败
        _record_answer(user_id, group_id, question["image_seq"], normalized_answer, False)

        max_attempts = 5
        remaining = max_attempts - current_attempts

        # 超过5次：机会已用完，不再判断对错
        if current_attempts > max_attempts:
            return f"😔 你的 {max_attempts} 次尝试机会已用完"

        # 返回逐字对比反馈（第1-5次答错都给反馈）
        return _build_feedback(correct_word, normalized_answer, correct_pinyin, question, remaining, max_attempts)


def check_answer_correct(group_id: int, user_answer: str) -> bool:
    """只读检查：用户答案是否正确，不修改任何游戏状态（不消耗尝试次数）"""
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return False

    # 检查超时
    elapsed = time.time() - game.get("start_time", time.time())
    if elapsed >= QUESTION_TIMEOUT_SECONDS:
        return False

    question = game["question"]
    correct_word = question["word"]

    def normalize(s: str) -> str:
        return "".join(c for c in s if c.isalnum() or c in "··")

    return normalize(user_answer) == normalize(correct_word)


def reveal_answer(group_id: int) -> str | None:
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None
    question = game["question"]
    _end_game(group_id)

    lines = [
        "🎉 公布答案！",
        f"正确词语：{question['word']}",
        f"谐音梗：{question['pun']}（{question['pinyin']}）",
        f"类型：{question['pun_type']}",
    ]

    # 追加排行榜
    lb = _format_leaderboard(group_id)
    if lb:
        lines.append(lb)

    lines.append("发送新答案或 /谐音梗 继续游戏~")
    return "\n".join(lines)


def get_current_question(group_id: int) -> dict | None:
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None
    return game["question"]
