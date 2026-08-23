#!/usr/bin/env python3
"""
Cosplay 图包自然语言搜索模块 — 独立程序文件
通过 /找图 xxx 指令从 cosplay 数据库中搜索并随机返回一张符合要求的图片。

v3 搜索策略：
  1. 默认：jieba 分词 → 关键词提取 → 全字段 LIKE 模糊搜索（快速 ~0.3s）
  2. 降级：关键词搜不到 → LLM 从自然语言提取关键词 → 全字段 LIKE 搜索
"""

import base64
import io
import json
import os
import random
import re
import sqlite3
import time
import logging
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

# 图片压缩目标大小（KB）— QQ 消息限制约 5MB，保守控制在 300KB
IMAGE_MAX_SIZE_KB = 300

# 单次查询最多扫描的结果数（用于随机选取）
MAX_CANDIDATES = 20

# 搜索冷却时间（秒）— 防止同一用户频繁搜索
SEARCH_COOLDOWN = 10

# LLM API 配置：v2 起不再硬编码，统一走 core.config 的 LLM 后端
# （deepseek/local 可热切换 + llm.enabled 总开关），见 _call_llm_sync

# ============================================================
#  LLM 调用（轻量级，用于关键词提取）
# ============================================================
def _call_llm_sync(messages: list[dict], max_tokens: int = 8192) -> str:
    """
    同步调用 LLM（blocking，适合在 async 上下文中用 asyncio.to_thread 包装）。
    返回纯净内容字符串。v2：走统一 LLM 后端（core.llm._resolve_llm_backend），
    llm.enabled 关闭时返回空串（调用方降级为纯关键词搜索）。
    """
    import httpx
    from core.llm import llm_enabled, _resolve_llm_backend, _get_config

    if not llm_enabled():
        logger.info("LLM 总开关关闭，跳过关键词提取")
        return ""
    api_url, model, headers, _cap = _resolve_llm_backend(_get_config())

    try:
        with httpx.Client(timeout=300, trust_env=False) as client:
            resp = client.post(
                f"{api_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            # 清理 thinking 标签
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return content
    except Exception as e:
        logger.warning(f"LLM 调用失败: {e}")
        return ""


# ============================================================
#  cosplay 数据库连接
# ============================================================
def _check_cosplay_db() -> str:
    """校验 cosplay.db 路径可用，否则抛带提示的异常（调用方已有 try/except 友好兜底）"""
    db_path = _cosplay_db_path()
    if not db_path:
        raise RuntimeError("cosplay 图库未配置：请在 GUI「配置」页填写 assets.cosplay_db 路径")
    if not os.path.exists(db_path):
        raise RuntimeError(f"cosplay 图库不存在: {db_path}（请在 GUI「配置」页检查 assets.cosplay_db）")
    return db_path


@contextmanager
def _get_cosplay_conn():
    """Cosplay 数据库连接上下文管理器"""
    conn = sqlite3.connect(_check_cosplay_db())
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


# ============================================================
#  LLM 提取关键词（当关键词搜索无结果时）
# ============================================================
def _extract_keywords_with_llm(query_text: str) -> Optional[list[str]]:
    """
    调用 LLM 从自然语言中提取搜索关键词列表。
    返回关键词列表，如 ["银发", "女仆"]
    如果 LLM 失败，返回 None。
    """
    prompt = f"""你是 cosplay 图片搜索关键词提取器。
请从用户描述中提取 3-5 个搜索关键词，返回 JSON 数组。
关键词要简洁（1-4个字），保留有意义的信息。

用户：「{query_text}」"""

    messages = [
        {"role": "system", "content": "你是关键词提取器。只输出 JSON 数组，如 [\"银发\", \"女仆\"]，不要其他文字。"},
        {"role": "user", "content": prompt},
    ]

    result = _call_llm_sync(messages, max_tokens=8192)
    if not result:
        return None

    # 提取 JSON 数组
    result = re.sub(r"^```json\s*", "", result, flags=re.MULTILINE).strip()
    result = re.sub(r"\s*```$", "", result, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(result)
        if isinstance(parsed, list):
            # 过滤空值和无效项
            keywords = [str(k).strip() for k in parsed if k and str(k).strip()]
            return keywords if keywords else None
    except json.JSONDecodeError:
        pass

    # 尝试从文本中提取中括号内的内容
    match = re.search(r'\[([^\]]+)\]', result)
    if match:
        try:
            items = [item.strip().strip('"\'') for item in match.group(1).split(',')]
            items = [i for i in items if i]
            return items if items else None
        except Exception:
            pass

    logger.warning(f"LLM 关键词提取解析失败: {result[:200]}")
    return None


# ============================================================
#  关键词降级搜索（当 LLM 失败或简单查询时）
# ============================================================
_STOPWORDS = {
    "的", "是", "有", "和", "与", "或", "一个", "一张", "那种", "这种",
    "那个", "这个", "来", "要", "想要", "没有", "在", "吗", "呢",
    "请", "帮", "找", "搜", "看看", "给我", "我想要", "能", "可以",
    "a", "the", "an", "and", "or", "了", "着", "过", "把", "被",
    "对", "对于", "关于", "以及", "然后", "所以", "因为", "但是",
    "穿", "穿着", "戴着", "拿着", "站在", "坐着", "躺在",
    "图片", "照片", "图", "图包",
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "谁", "什么", "哪", "怎么", "怎样", "如何",
    "试试", "搜搜", "找找", "推荐", "介绍",
    "角色", "人物", "人",
    # 新增：口语化前缀/后缀
    "有没有", "来个", "来个", "想要", "有没有",
    "给我看看", "给我来", "来张", "来个", "来一张",
    "有没有那种", "有没有穿", "来",
    "坐在", "站着", "躺着", "蹲着", "靠着", "趴着",
    "最好", "那种", "有", "那种",
    "cos", "cosplay", "的cos", "的cosplay",
    "有没有", "有没有", "有没有那种",
    "穿", "穿着", "戴", "戴着", "拿", "拿着", "站", "站着",
    "裙子", "裙", "服", "装", "衣服", "衣服",
    "好看", "漂亮", "美", "帅", "酷", "萌", "甜", "飒",
}

# 尝试加载 jieba（中文分词库）
_JIEBA_AVAILABLE = False
try:
    import jieba
    _JIEBA_AVAILABLE = True

    # 添加 cosplay 相关词汇到 jieba 词典（Coser 名 + 特征词 + 服装 + 发型 + 配饰 + 场景 + 风格）
    _JIEBA_CUSTOM_WORDS = [
        # Coser 名
        "蜜汁猫裘", "不呆猫", "喵了个咪", "兔玩映画", "周叽是可爱兔兔",
        # 角色名
        "初音未来", "春日野穹", "雷姆", "拉姆", " Saber", "远坂凛",
        "亚丝娜", "莉法", "明日香", "绫波丽", "零二", "广",
        "博丽灵梦", "雾雨魔理沙", "东方", "Fate", "型月",
        "Re0", "命运石之门", "缘之空", "樱花庄", "我的妹妹",
        # 发色/发型特征
        "银发", "红瞳", "黑发", "金发", "棕发", "粉发", "紫发", "蓝发", "绿发", "白毛",
        "双马尾", "单马尾", "丸子头", "长直发", "短发", "卷发",
        # 服装
        "女仆装", "水手服", "泳装", "体操服", "校服", "连衣裙", "jk", "洛丽塔",
        "吊带袜", "过膝袜", "白丝", "黑丝", "腿环",
        # 配饰/道具
        "眼镜娘", "戴眼镜", "兽耳", "猫耳", "兔耳", "翅膀", "耳机", "法杖",
        # 场景
        "教室", "天台", "天台上", "花田", "海滩", "泳池", "室内", "室外",
        # 风格/表情
        "可爱", "清纯", "性感", "御姐", "萝莉", "正太", "傲娇", "三无", "少女",
        "微笑", "wink", "摸鱼", "自拍",
        # 其他高频词
        "比基尼", "制服", "saber",
    ]
    for word in _JIEBA_CUSTOM_WORDS:
        jieba.add_word(word)
except ImportError:
    pass


def _extract_search_terms(query_text: str) -> list[str]:
    """从自然语言查询中提取搜索关键词"""
    # 如果 jieba 可用，使用 jieba 分词
    if _JIEBA_AVAILABLE:
        raw_terms = jieba.lcut(query_text.strip())
    else:
        # 降级：按标点符号和空格分割
        raw_terms = re.split(r"[，,、\s]+", query_text.strip())

    terms = []
    for t in raw_terms:
        t = t.strip()
        # 过滤：停用词、单字（太短容易误匹配）、纯数字/标点
        if (t and t not in _STOPWORDS and len(t) >= 2
                and not re.match(r'^[\d\s\W_]+$', t)):
            terms.append(t)

    # 如果没有提取到有效关键词，返回原始查询
    return terms if terms else [query_text.strip()]


def _build_keyword_query(terms: list[str], use_and: bool = False) -> tuple[str, list]:
    """根据搜索关键词构建 SQL 查询（全字段模糊搜索）

    Args:
        terms: 关键词列表
        use_and: True=所有关键词必须全部命中(AND), False=命中任意一个即可(OR)
    """
    conditions = []
    params = []

    all_fields = [
        "keywords", "analysis", "costume", "pose",
        "body_focus", "scene", "vibe", "style_aura",
        "hair_color", "legwear", "accessory",
        "expression", "quality", "flags", "rating",
        "framing", "angle", "body_shape", "props",
        "lighting", "hairstyle", "eye_color",
        "dir_character", "dir_source", "coser", "series", "filename",
    ]

    for term in terms:
        # L14 修复：转义 LIKE 通配符（%/_）——原实现直接把用户输入拼进
        # %term%，搜"100%"会匹配任意包含 100 的记录、搜"a_b"会匹配 axb，
        # 搜索结果被语义污染。ESCAPE '\' 在下方 SQL 中声明。
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        field_conditions = []
        for field in all_fields:
            field_conditions.append(f"{field} LIKE ? ESCAPE '\\'")
            params.append(like_pattern)

        term_condition = " OR ".join(field_conditions)
        conditions.append(f"({term_condition})")

    # 多关键词用 AND，单关键词用 OR
    connector = " AND " if use_and and len(terms) >= 2 else " OR "
    where_clause = connector.join(conditions)

    ext_condition = """(extension LIKE '.jpg%' OR extension LIKE '.jpeg%'
                       OR extension LIKE '.png' OR extension LIKE '.webp' OR extension LIKE '.bmp')"""

    safe_query = any(
        word in " ".join(terms)
        for word in ["清纯", "可爱", "清新", "日常", "校园"]
    )
    safe_filter = " AND rating NOT LIKE '%R-18%'" if safe_query else ""

    sql = f"""
        SELECT id, filepath, filename, coser, series, dir_character, dir_source,
               costume, accessory, legwear, hair_color, keywords, rating, analysis,
               expression, scene, vibe, pose
        FROM files
        WHERE ({where_clause})
          AND {ext_condition}
        {safe_filter}
        ORDER BY RANDOM()
        LIMIT ?
    """
    params.append(MAX_CANDIDATES)

    return sql, params


# ============================================================
#  搜索主逻辑
# ============================================================
def search_cosplay(query_text: str) -> Optional[dict]:
    """
    执行搜索，返回随机一张匹配结果的详细信息。

    搜索策略：
    1. 默认：jieba 分词 → 关键词 AND 精确匹配（所有关键词必须命中）
    2. 降级：AND 无结果 → 同一批关键词 OR 宽松匹配
    3. 终极降级：OR 也搜不到 → LLM 提取关键词 → 再次尝试搜索
    """
    if not query_text or len(query_text.strip()) < 1:
        return None

    # ---- 策略 1: 关键词 AND 精确匹配 ----
    terms = _extract_search_terms(query_text)
    logger.info(f"🔍 AND 精确搜索: {query_text} → {terms}")
    sql, params = _build_keyword_query(terms, use_and=True)

    try:
        with _get_cosplay_conn() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        if rows:
            logger.info(f"✅ AND 精确匹配成功，命中 {len(rows)} 条")
            row = random.choice(rows)
            return _row_to_dict(row)
    except Exception as e:
        logger.error(f"AND 精确搜索失败: {e}")

    # ---- 策略 2: 关键词 OR 宽松匹配 ----
    logger.info(f"🔍 AND 无结果，降级为 OR 宽松搜索: {terms}")
    sql, params = _build_keyword_query(terms, use_and=False)

    try:
        with _get_cosplay_conn() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

        if rows:
            logger.info(f"✅ OR 宽松匹配成功，命中 {len(rows)} 条")
            row = random.choice(rows)
            return _row_to_dict(row)
    except Exception as e:
        logger.error(f"OR 宽松搜索失败: {e}")

    # ---- 策略 3: LLM 提取关键词（终极降级）----
    logger.info(f"🧠 关键词无结果，尝试 LLM 提取关键词: {query_text}")
    llm_keywords = _extract_keywords_with_llm(query_text)

    if llm_keywords:
        logger.info(f"🧠 LLM 提取关键词: {llm_keywords}")
        # 先尝试 AND
        sql, params = _build_keyword_query(llm_keywords, use_and=True)

        try:
            with _get_cosplay_conn() as conn:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()

            if rows:
                logger.info(f"✅ LLM AND 匹配成功，命中 {len(rows)} 条")
                row = random.choice(rows)
                return _row_to_dict(row)
        except Exception as e:
            logger.error(f"LLM AND 搜索失败: {e}")

        # LLM OR 最后尝试
        sql, params = _build_keyword_query(llm_keywords, use_and=False)
        try:
            with _get_cosplay_conn() as conn:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()

            if rows:
                logger.info(f"✅ LLM OR 匹配成功，命中 {len(rows)} 条")
                row = random.choice(rows)
                return _row_to_dict(row)
        except Exception as e:
            logger.error(f"LLM OR 搜索失败: {e}")

    return None


def _row_to_dict(row: tuple) -> dict:
    """将 SQL 查询结果行转换为字典"""
    return {
        "filepath": row[1],
        "filename": row[2] or "",
        "coser": row[3],
        "series": row[4],
        "character": row[5],
        "source": row[6],
        "costume": row[7],
        "accessory": row[8],
        "legwear": row[9],
        "hair_color": row[10],
        "keywords": row[11],
        "rating": row[12],
        "analysis": row[13],
        "expression": row[14],
        "scene": row[15],
        "vibe": row[16],
        "pose": row[17],
    }


# ============================================================
#  图片压缩
# ============================================================
def _image_to_base64(img: Image.Image, max_size_kb: int = IMAGE_MAX_SIZE_KB) -> str:
    """将 PIL Image 对象压缩为 base64 编码"""
    max_bytes = max_size_kb * 1024
    quality = 85
    scale = 1.0
    buf = None

    # M19 修复：原实现 quality 序列 85→...→35→25 时即重置（quality<=30 分支是死代码），
    # 高熵大图在 scale≈0.107 退出时仍可能超限。改为显式收敛：
    # - quality 一直降到 10 才缩小 scale（更多压缩档位）
    # - scale 降到 0.05 才退出
    # - 每个档位都检查 size，命中即返回
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
        logger.warning(f"⚠️ 图片压缩到极限仍超限: {len(buf.getvalue()) // 1024}KB > {max_size_kb}KB")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    return ""


def _compress_image_to_base64(image_path: str, max_size_kb: int = IMAGE_MAX_SIZE_KB) -> str:
    """读取图片文件并压缩为 base64"""
    try:
        with Image.open(image_path).convert("RGB") as img:
            return _image_to_base64(img, max_size_kb)
    except Exception as e:
        logger.error(f"压缩图片失败 {image_path}: {e}")
        return ""


# ============================================================
#  消息构建
# ============================================================
def build_result_segments(result: dict, query_text: str) -> list[dict]:
    """
    构建搜索结果的消息段列表（图片 + 文字信息）。
    返回 list[dict]，可直接通过 websocket 发送。
    """
    segments: list[dict] = []

    # 压缩图片并添加
    b64 = _compress_image_to_base64(result["filepath"])
    if b64:
        segments.append({
            "type": "image",
            "data": {"file": f"base64://{b64}"},
        })
        logger.info(f"找图 - 图片压缩成功: {result['filepath']}")
    else:
        logger.warning(f"找图 - 图片压缩失败: {result['filepath']}")

    # 构建信息文本
    lines = [f"🔍 找到符合「{query_text}」的图片："]

    if result.get("coser"):
        lines.append(f"  Coser：{result['coser']}")
    if result.get("series"):
        lines.append(f"  系列：{result['series']}")
    if result.get("character"):
        lines.append(f"  角色：{result['character']}")
    if result.get("source"):
        lines.append(f"  作品：{result['source']}")
    if result.get("costume"):
        lines.append(f"  服装：{result['costume']}")
    if result.get("accessory"):
        lines.append(f"  配饰：{result['accessory']}")
    if result.get("legwear"):
        lines.append(f"  腿部：{result['legwear']}")
    if result.get("hair_color"):
        lines.append(f"  发色：{result['hair_color']}")
    if result.get("pose"):
        lines.append(f"  姿势：{result['pose']}")
    if result.get("scene"):
        lines.append(f"  场景：{result['scene']}")
    if result.get("vibe"):
        lines.append(f"  氛围：{result['vibe']}")
    if result.get("expression"):
        lines.append(f"  表情：{result['expression']}")
    if result.get("rating"):
        lines.append(f"  分级：{result['rating']}")
    if result.get("keywords"):
        lines.append(f"  标签：{result['keywords']}")

    lines.append("")
    lines.append("💡 再搜试试：/找图 你的描述")

    text = "\n".join(lines)
    segments.append({"type": "text", "data": {"text": text}})

    return segments


def build_not_found_segments(query_text: str) -> list[dict]:
    """构建未找到结果的消息段"""
    tips = [
        "试试更具体的描述",
        "试试 Coser 名字",
        "试试角色名或作品名",
        "试试服装风格描述（如：女仆装、水手服）",
        "试试关键词（如：银发、红瞳、泳装）",
    ]
    tip = random.choice(tips)
    # L15 修复：动态查询图片总数——原硬编码 204070 会随数据库更新失真
    total = 0
    try:
        conn = sqlite3.connect(_check_cosplay_db())
        try:
            row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            total = row[0] if row else 0
        finally:
            conn.close()
    except Exception:
        total = 0
    total_text = f"数据库目前有 {total} 张图片" if total else "数据库暂时不可用"
    text = f"😅 没有找到符合「{query_text}」的图片\n\n💡 建议：{tip}\n{total_text}"
    return [{"type": "text", "data": {"text": text}}]


def build_error_segments() -> list[dict]:
    """构建搜索出错的消息段"""
    return [{"type": "text", "data": {"text": "😵 搜索时出错了，请稍后再试试~"}}]


# ============================================================
#  冷却控制
# ============================================================
_user_cooldowns: dict[int, float] = {}


def _is_on_cooldown(user_id: int) -> bool:
    """检查用户是否处于冷却期"""
    now = time.time()
    last = _user_cooldowns.get(user_id, 0)
    if now - last < SEARCH_COOLDOWN:
        return True
    return False


def _set_cooldown(user_id: int):
    """设置用户冷却

    M15 修复：写入时顺带清理已过冷却窗口的旧条目，防止 dict 无限增长
    """
    _user_cooldowns[user_id] = time.time()
    cutoff = time.time() - SEARCH_COOLDOWN
    expired = [uid for uid, ts in _user_cooldowns.items() if ts < cutoff]
    for uid in expired:
        _user_cooldowns.pop(uid, None)


def build_cooldown_segments(user_id: int) -> list[dict]:
    """构建冷却提示消息段"""
    remaining = int(SEARCH_COOLDOWN - (time.time() - _user_cooldowns.get(user_id, 0)))
    if remaining <= 0:
        return []
    return [{"type": "text", "data": {"text": f"⏳ 搜索冷却中，请 {remaining} 秒后再试试~"}}]