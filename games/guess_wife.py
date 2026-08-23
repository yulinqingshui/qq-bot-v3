#!/usr/bin/env python3
"""
猜老婆游戏模块 - 从 cosplay 图包中随机选取图片，裁剪 1/8 面积后让大家猜角色。
集成到 bot.py 的群聊消息流中使用。
"""

import base64
import io
import math
import os
import random
import sqlite3
import time
import logging
import threading
from contextlib import contextmanager
from PIL import Image
from typing import Optional

logger = logging.getLogger("qq-bot")

# ============================================================
#  配置（v2：cosplay.db 为外部资产，GUI 可配 assets.cosplay_db，热生效）
# ============================================================
def _cosplay_db_path() -> str:
    """返回 cosplay.db 路径；未配置返回空串（调用方需检查并给友好提示）。"""
    from core.config import CONFIG
    return CONFIG.get("ASSET_COSPLAY_DB") or ""


def _check_cosplay_db() -> str:
    """校验 cosplay.db 路径可用，否则抛带提示的异常（调用方已有 try/except 友好兜底）"""
    db_path = _cosplay_db_path()
    if not db_path:
        raise RuntimeError("cosplay 图库未配置：请在 GUI「配置」页填写 assets.cosplay_db 路径")
    if not os.path.exists(db_path):
        raise RuntimeError(f"cosplay 图库不存在: {db_path}（请在 GUI「配置」页检查 assets.cosplay_db）")
    return db_path

# 图片压缩目标大小（KB）
IMAGE_MAX_SIZE_KB = 200

# 游戏超时时间（秒）
QUESTION_TIMEOUT_SECONDS = 180  # 3 分钟

# 答题记录数据库
_RECORDS_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "guess_wife_records.db"
)

# ============================================================
#  cosplay 数据库连接
# ============================================================
@contextmanager
def _get_cosplay_conn():
    """Cosplay 数据库连接上下文管理器"""
    conn = sqlite3.connect(_check_cosplay_db())
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _get_wife_db():
    """答题记录数据库上下文管理器"""
    os.makedirs(os.path.dirname(_RECORDS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_RECORDS_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fetch_valid_files():
    """
    获取所有同时有 dir_character 和 dir_source 的文件记录。
    返回 [(id, filepath, dir_character, dir_source), ...]

    M20 修复：原实现全表加载 15127 条候选 + ORDER BY RANDOM() 全量排序（0.5~2s 用户等待）。
    改为 WHERE 过滤后 ORDER BY RANDOM() LIMIT 200——候选池完整（题材不减少），
    只返回 200 条供加权选择，排序开销降低约 2 个数量级。
    """
    try:
        with _get_cosplay_conn() as conn:
            rows = conn.execute(
                """SELECT id, filepath, dir_character, dir_source
                   FROM files
                   WHERE dir_character IS NOT NULL AND dir_character != ''
                   AND dir_source IS NOT NULL AND dir_source != ''
                   AND (extension LIKE '.jpg%' OR extension LIKE '.png' OR extension LIKE '.webp' OR extension LIKE '.bmp')
                   ORDER BY RANDOM() LIMIT 200"""
            ).fetchall()
        return rows
    except Exception as e:
        logger.error(f"读取 cosplay 数据库失败: {e}")
        return []


def _fetch_all_pairs():
    """
    获取所有不重复的 (作品名, 角色名) 对。
    返回 [(dir_character, dir_source), ...]
    """
    try:
        with _get_cosplay_conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT dir_character, dir_source
                   FROM files
                   WHERE dir_character IS NOT NULL AND dir_character != ''
                   AND dir_source IS NOT NULL AND dir_source != ''"""
            ).fetchall()
        return rows
    except Exception as e:
        logger.error(f"获取角色-作品对失败: {e}")
        return []


# ============================================================
#  答题记录数据库
# ============================================================
_db_lock = threading.Lock()

_get_nickname_fn: Optional[callable] = None  # type: ignore


def _init_records_db():
    os.makedirs(os.path.dirname(_RECORDS_DB_PATH), exist_ok=True)
    with _db_lock:
        with _get_wife_db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    character TEXT NOT NULL,
                    source TEXT NOT NULL,
                    is_correct INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user ON records(user_id);
                CREATE INDEX IF NOT EXISTS idx_group ON records(group_id);
            """
            )


_init_records_db()


def register_get_nickname(func: callable):  # type: ignore
    global _get_nickname_fn
    _get_nickname_fn = func


def _get_nickname(user_id: int, group_id: int) -> str:
    if _get_nickname_fn is not None:
        try:
            nickname = _get_nickname_fn(user_id, group_id)
            if nickname:
                return nickname
        except Exception:
            pass
    return str(user_id)


def _record_answer(user_id: int, group_id: int, character: str, source: str, is_correct: bool):
    try:
        with _db_lock:
            with _get_wife_db() as conn:
                conn.execute(
                    "INSERT INTO records (user_id, group_id, character, source, is_correct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, group_id, character, source, 1 if is_correct else 0, time.time()),
                )
    except Exception as e:
        logger.error(f"记录答题失败: {e}")


def _get_leaderboard(group_id: int, limit: int = 5) -> list[dict]:
    try:
        with _db_lock:
            with _get_wife_db() as conn:
                conn.row_factory = sqlite3.Row
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
                    (group_id, limit),
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


def _format_leaderboard(group_id: int) -> str:
    leaderboard = _get_leaderboard(group_id)
    if not leaderboard:
        return ""
    lines = ["\n🏆 猜老婆排行榜 TOP5", "──────────"]
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(leaderboard):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {entry['nickname']} — 猜对{entry['correct']}次/{entry['total']}次 (准确率{entry['accuracy']}%)")
    return "\n".join(lines)


# ============================================================
#  游戏状态管理
# ============================================================
_GAMES: dict[int, dict] = {}

# 全局轮次计数器
_ROUND_COUNTER: int = 0

# 历史记录：{file_id: round_number}
_QUESTION_HISTORY: dict[int, int] = {}

_DEFAULT_WEIGHT = 200
_HISTORY_WINDOW = 100


def _get_game(group_id: int) -> dict | None:
    return _GAMES.get(group_id)


def _set_game(group_id: int, game: dict):
    _GAMES[group_id] = game


def _end_game(group_id: int):
    if group_id in _GAMES:
        game = _GAMES[group_id]
        task = game.get("timeout_task")
        if task and not task.done():
            task.cancel()
        del _GAMES[group_id]


def is_active(group_id: int) -> bool:
    game = _get_game(group_id)
    return game is not None and game.get("active", False)


# ============================================================
#  图片裁剪
# ============================================================
def _random_crop_one_eighth(image_path: str) -> Image.Image | None:
    """
    随机裁剪原图 1/8 面积的区域。
    裁剪区域保持与原图相同的宽高比，边长为原图的 1/sqrt(8) ≈ 0.353 倍。
    """
    try:
        with Image.open(image_path).convert("RGB") as img:
            orig_w, orig_h = img.size
            # 1/8 面积 -> 边长缩放因子 sqrt(1/8) ≈ 0.35355
            scale = 1.0 / math.sqrt(8)
            crop_w = max(1, int(orig_w * scale))
            crop_h = max(1, int(orig_h * scale))

            # 随机选择裁剪区域的左上角
            max_x = max(0, orig_w - crop_w)
            max_y = max(0, orig_h - crop_h)
            left = random.randint(0, max_x)
            top = random.randint(0, max_y)

            cropped = img.crop((left, top, left + crop_w, top + crop_h))
            return cropped
    except Exception as e:
        logger.error(f"裁剪图片失败 {image_path}: {e}")
        return None


def _image_to_base64(img: Image.Image, max_size_kb: int = IMAGE_MAX_SIZE_KB) -> str:
    """将 PIL Image 对象压缩为 base64 编码"""
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

        if quality > 10:
            quality -= 10
        else:
            scale *= 0.8
            quality = 85

    # 极限压缩后仍超限：返回最后一次结果（尽力而为，宁发小图不空）
    if buf is not None:
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    return ""


def _full_image_to_base64(image_path: str, max_size_kb: int = IMAGE_MAX_SIZE_KB) -> str:
    """读取完整图片并压缩为 base64"""
    try:
        with Image.open(image_path).convert("RGB") as img:
            return _image_to_base64(img, max_size_kb)
    except Exception as e:
        logger.error(f"压缩完整图片失败 {image_path}: {e}")
        return ""


# ============================================================
#  出题功能
# ============================================================
def _weighted_pick_file() -> Optional[tuple]:
    """
    加权随机选题文件：
    - 正常文件权重 = _DEFAULT_WEIGHT
    - 最近 N 轮用过的文件权重 = N
    - 超过 _HISTORY_WINDOW 轮的文件权重恢复为默认值
    """
    global _ROUND_COUNTER
    current_round = _ROUND_COUNTER

    files = _fetch_valid_files()
    if not files:
        return None

    if len(files) <= 6:
        # 文件太少，直接随机
        return random.choice(files)

    weights = []
    for f in files:
        fid = f[0]
        last_round = _QUESTION_HISTORY.get(fid, 0)
        if last_round == 0:
            weights.append(_DEFAULT_WEIGHT)
        else:
            rounds_ago = current_round - last_round
            if rounds_ago <= _HISTORY_WINDOW:
                weights.append(rounds_ago)
            else:
                weights.append(_DEFAULT_WEIGHT)

    chosen = random.choices(files, weights=weights, k=1)[0]

    # 验证文件是否实际存在，如果不存在则重试
    max_retries = 3
    for _ in range(max_retries):
        if os.path.exists(chosen[1]):
            return chosen
        chosen = random.choice(files)

    # 全部重试完，返回最后一个（至少文本可用）
    return chosen


def draw_question(group_id: int, creator_id: int = 0) -> Optional[dict]:
    """
    出一道新题。
    返回游戏数据字典，如果素材不足则返回 None。
    """
    global _ROUND_COUNTER
    _ROUND_COUNTER += 1

    # 选取正确文件
    file_row = _weighted_pick_file()
    if not file_row:
        return None

    file_id, filepath, character, source = file_row

    # 获取所有角色-作品对，排除当前正确答案
    all_pairs = _fetch_all_pairs()
    correct_pair = (character, source)
    other_pairs = [p for p in all_pairs if p != correct_pair]

    if len(other_pairs) < 5:
        return None

    # 随机选 5 个错误选项
    wrong_pairs = random.sample(other_pairs, 5)

    # 构建 6 个选项（打乱顺序）
    options = [correct_pair] + wrong_pairs
    random.shuffle(options)

    correct_index = options.index(correct_pair)  # A=0, B=1, C=2, D=3, E=4, F=5

    # L16 修复：_QUESTION_HISTORY 记录移到出题确认成功后——
    # 原实现 388 行先记录、399 行才检查 other_pairs<5，素材不足时
    # 文件被标记"本轮用过"（权重=1），实际没出成题，重试时该文件被雪藏
    _QUESTION_HISTORY[file_id] = _ROUND_COUNTER
    # M16 修复：清理超出历史窗口的旧条目，防止 dict 无限增长
    stale_ids = [fid for fid, rnd in _QUESTION_HISTORY.items() if rnd <= _ROUND_COUNTER - _HISTORY_WINDOW]
    for fid in stale_ids:
        _QUESTION_HISTORY.pop(fid, None)

    game = {
        "file_id": file_id,
        "filepath": filepath,
        "character": character,
        "source": source,
        "options": options,  # [(character, source), ...] 4 个
        "correct_index": correct_index,  # 正确答案的索引 (0-3)
        "active": True,
        "creator_id": creator_id,
        "user_attempts": {},  # {user_id: attempt_count}
        "start_time": time.time(),
    }

    _set_game(group_id, game)
    logger.info(
        f"猜老婆出题: {source} - {character} (选项{chr(65 + correct_index)})"
    )
    return game


# ============================================================
#  消息构建
# ============================================================
def build_question_segments(game: dict) -> list[dict]:
    """
    构建出题消息：裁剪后的图片 + 四个选项文本。
    如果图片无法加载，仅发送文字选项。
    """
    filepath = game["filepath"]
    options = game["options"]

    segments: list[dict] = []

    # 裁剪图片
    cropped = _random_crop_one_eighth(filepath)
    if cropped:
        b64 = _image_to_base64(cropped)
        segments.append({
            "type": "image",
            "data": {"file": f"base64://{b64}"},
        })
        cropped.close()
        logger.info(f"猜老婆图片已裁剪: {filepath}")
    else:
        # 裁剪失败，尝试用完整图
        b64 = _full_image_to_base64(filepath)
        if b64:
            segments.append({
                "type": "image",
                "data": {"file": f"base64://{b64}"},
            })
            logger.warning(f"猜老婆裁剪失败，使用完整图: {filepath}")
        else:
            logger.warning(f"猜老婆图片无法加载，仅发送文字选项: {filepath}")

    # 构建选项文本
    letters = ["A", "B", "C", "D", "E", "F"]
    option_lines = ["🔍 猜猜这是哪个角色？", ""]
    for i, (char, src) in enumerate(options):
        option_lines.append(f"  {letters[i]}. {src} — {char}")

    option_lines.append("")
    option_lines.append("回复 A-F 来回答~")

    segments.append({"type": "text", "data": {"text": "\n".join(option_lines)}})

    return segments
def build_answer_reveal_segments(group_id: int, game: dict) -> list[dict]:
    """
    构建公布答案的消息：完整图片 + 答案文本 + 排行榜。
    返回消息段列表，供 bot.py 直接发送。
    """
    filepath = game["filepath"]
    character = game["character"]
    source = game["source"]
    correct_index = game["correct_index"]

    segments: list[dict] = []

    # 发送完整原图
    try:
        with Image.open(filepath).convert("RGB") as img:
            b64 = _image_to_base64(img, IMAGE_MAX_SIZE_KB)
        segments.append({
            "type": "image",
            "data": {"file": f"base64://{b64}"},
        })
        logger.info(f"猜老婆答案公布 - 发送完整图片: {filepath}")
    except Exception as e:
        logger.warning(f"猜老婆答案公布 - 加载完整图片失败: {filepath}: {e}")

    # 构建答案文本
    letters = ["A", "B", "C", "D", "E", "F"]
    lines = [
        "🎉 公布答案！",
        f"正确答案是 {letters[correct_index]}：{source} — {character}",
        "",
    ]

    # 排行榜
    lb = _format_leaderboard(group_id)
    if lb:
        lines.append(lb)
        lines.append("")

    lines.append("发送 /猜老婆 开始下一题~")

    segments.append({"type": "text", "data": {"text": "\n".join(lines)}})

    return segments


# ============================================================
#  答题功能
# ============================================================
def build_correct_answer_segments(group_id: int, game: dict, user_id: int = 0) -> list[dict]:
    """
    构建答对时的消息：完整图片 + 恭喜文本 + 排行榜。
    """
    filepath = game["filepath"]
    character = game["character"]
    source = game["source"]
    correct_index = game["correct_index"]
    nickname = _get_nickname(user_id, group_id)

    segments: list[dict] = []

    # 发送完整原图
    try:
        with Image.open(filepath).convert("RGB") as img:
            b64 = _image_to_base64(img, IMAGE_MAX_SIZE_KB)
        segments.append({
            "type": "image",
            "data": {"file": f"base64://{b64}"},
        })
        logger.info(f"猜老婆答对 - 发送完整图片: {filepath}")
    except Exception as e:
        logger.warning(f"猜老婆答对 - 加载完整图片失败: {filepath}: {e}")

    # 构建恭喜文本
    letters = ["A", "B", "C", "D", "E", "F"]
    lines = [
        f"✅ {nickname} 答对了！",
        f"作品：{source}",
        f"角色：{character}",
        "",
    ]

    # 排行榜
    lb = _format_leaderboard(group_id)
    if lb:
        lines.append(lb)
        lines.append("")

    lines.append("发送 /猜老婆 开始下一题~")

    segments.append({"type": "text", "data": {"text": "\n".join(lines)}})

    return segments


def check_answer(group_id: int, user_answer: str, user_id: int = 0) -> Optional[dict]:
    """
    检查用户答案。
    期望用户回答 A-F。

    返回格式：
    - None: 没有正在进行的猜老婆游戏
    - {"type": "reply", "message": str}: 普通文字回复（答错/格式不对）
    - {"type": "correct", "segments": list[dict]}: 答对，需要发送带完整图片的消息段
    - {"type": "timeout", "message": str}: 已超时
    - {"type": "already_answered", "message": str}: 已经猜过了
    """
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    # 检查超时
    elapsed = time.time() - game.get("start_time", time.time())
    if elapsed >= QUESTION_TIMEOUT_SECONDS:
        # BUG 修复（2026-08-03）：原实现调用 reveal_answer 后丢弃返回值，
        # 只回文字 → 答案（含完整图片）永远不显示。改为构建带图片的答案段
        # 交给 router 发送。
        revealed = reveal_answer(group_id)
        if revealed:
            try:
                segments = build_answer_reveal_segments(group_id, revealed)
                end_reveal_game(group_id)
                return {"type": "timeout", "segments": segments,
                        "message": "⏰ 答题时间到！答案已公布~"}
            except Exception:
                end_reveal_game(group_id)
                return {"type": "timeout", "message": "⏰ 答题时间到！答案已公布~"}
        return {"type": "timeout", "message": "⏰ 答题时间到！答案已公布~"}

    # 每人只有一次猜测机会
    user_attempts = game.get("user_attempts", {})
    if user_id in user_attempts:
        return {"type": "already_answered", "message": "🙅 你已经猜过了，每人只有一次机会哦~"}

    # 解析答案
    answer = user_answer.strip().upper()
    if answer not in ("A", "B", "C", "D", "E", "F"):
        # 格式不对不算消耗机会
        return {"type": "reply", "message": "🤔 请回复 A-F 来回答~"}

    selected_index = ord(answer) - ord("A")
    correct_index = game["correct_index"]

    character = game["character"]
    source = game["source"]

    if selected_index == correct_index:
        # 答对
        _record_answer(user_id, group_id, character, source, True)
        _end_game(group_id)
        segments = build_correct_answer_segments(group_id, game, user_id)
        return {"type": "correct", "segments": segments}
    else:
        # 答错 — 记录消耗机会
        user_attempts[user_id] = 1  # 标记已猜过
        game["user_attempts"] = user_attempts

        _record_answer(user_id, group_id, character, source, False)

        letters = ["A", "B", "C", "D", "E", "F"]
        wrong_char = game["options"][selected_index]
        wrong_str = f"{wrong_char[1]} — {wrong_char[0]}"
        return {"type": "reply", "message": f"❌ 不对哦~ {letters[selected_index]} 是 {wrong_str}"}


def reveal_answer(group_id: int) -> dict | None:
    """
    公布当前题目的答案。
    返回 game dict（供调用方构建带图片的消息段），如果没有活跃游戏则返回 None。
    调用方应在构建完消息后自行结束游戏，或调用 end_reveal_game(group_id)。
    """
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None

    # 取消超时定时器
    task = game.get("timeout_task")
    if task and not task.done():
        task.cancel()

    # 标记为非活跃（但不删除，让调用方拿到 game 后自行清理）
    game["active"] = False
    return game


def end_reveal_game(group_id: int):
    """reveal_answer 后的清理：从 _GAMES 中删除游戏"""
    _end_game(group_id)


def get_current_question(group_id: int) -> dict | None:
    game = _get_game(group_id)
    if not game or not game.get("active"):
        return None
    return game
