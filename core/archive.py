#!/usr/bin/env python3
"""
消息存档模块 — 文本存档、图片/语音下载与归档、撤回记录。
"""
import os
import hashlib
import time
import asyncio
import logging
import threading
import httpx
import requests
from typing import Optional

logger = logging.getLogger("qq-bot")

from .database import get_db

# ============================================================
#  存档目录（v2：从 config.yaml 读取，GUI 可配 + 热生效）
# ============================================================
def _archive_images_dir() -> str:
    from .config import CONFIG
    return CONFIG["ARCHIVE_IMAGES_DIR"]


def _recall_images_dir() -> str:
    from .config import CONFIG
    return CONFIG["ARCHIVE_RECALL_DIR"]


def _archive_voices_dir() -> str:
    from .config import CONFIG
    return CONFIG["ARCHIVE_VOICES_DIR"]


def _save_images_enabled() -> bool:
    """是否保存媒体（图片+语音）。默认开启；08-21 起 yaml 不再暴露
    archive.save_images 配置项（如需关闭可直接在 yaml 写该键）。"""
    from .config import CONFIG
    return bool(CONFIG.get("SAVE_IMAGES", True))


def _save_recall_messages_enabled() -> bool:
    """是否保存撤回消息记录，config.yaml: archive.save_recall_messages"""
    from .config import CONFIG
    return bool(CONFIG.get("SAVE_RECALL_MESSAGES", True))


def _save_recall_images_enabled() -> bool:
    """是否保存撤回图片，config.yaml: archive.save_recall_images"""
    from .config import CONFIG
    return bool(CONFIG.get("SAVE_RECALL_IMAGES", True))


# ============================================================
#  消息存档（文本）
# ============================================================
def archive_message(message_id: int, message_type: str, target_id: int,
                    user_id: int, nickname: str, content: str, raw_message: str = "",
                    has_image: bool = False, has_voice: bool = False,
                    msg_kind: str = "text",
                    created_at: Optional[float] = None):
    """
    将消息存入永久存档表（不清理）。
    同时存入 group_chat_cache 以保持 /评选 功能可用。

    created_at: 自定义时间戳（默认 time.time()）。bot 回复存档用——
                锚定用户 @bot 消息时间 + 0.01s，保证提取时对话轮次相邻（2026-08-08）。
    msg_kind: 消息类型（08-21 统一列），由调用方从 raw_message 的 CQ 标记派生
              （derive_msg_kind），与下载开关无关——反映"消息是什么"而非"下载了什么"。
    """
    ts = created_at if created_at is not None else time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO message_archive "
            "(message_id, message_type, target_id, user_id, nickname, content, raw_message, has_image, has_voice, msg_kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, message_type, target_id, user_id, nickname, content, raw_message, int(has_image), int(has_voice), msg_kind, ts),
        )
        if message_type == "group":
            conn.execute(
                "INSERT INTO group_chat_cache (message_id, group_id, user_id, nickname, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, target_id, user_id, nickname, content, ts),
            )


# ============================================================
#  图片下载
# ============================================================
def _download_image_sync(image_url: str, target_dir: Optional[str] = None, target_id: int = 0) -> tuple[str, str, int]:
    """下载图片到本地（同步版本，用于 archive_recall 等同步上下文）"""
    save_dir = target_dir or _archive_images_dir()
    if target_id:
        save_dir = os.path.join(save_dir, str(target_id))
    os.makedirs(save_dir, exist_ok=True)
    tmp_path = os.path.join(save_dir, f"tmp_{int(time.time())}_{id(threading.current_thread())}.tmp")
    try:
        resp = requests.get(image_url, timeout=15)
        if resp.status_code != 200:
            return "", "", 0
        content = resp.content
        md5 = hashlib.md5(content).hexdigest()
        ext = ".jpg"
        url_path = image_url.lower().split("?")[0]
        if url_path.endswith(".png"):
            ext = ".png"
        elif url_path.endswith(".gif"):
            ext = ".gif"
        elif url_path.endswith(".webp"):
            ext = ".webp"
        final_path = os.path.join(save_dir, f"{md5}{ext}")
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, final_path)
        return final_path, md5, len(content)
    except Exception as e:
        logger.warning(f"同步下载图片失败: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return "", "", 0


async def _download_image(image_url: str, target_dir: Optional[str] = None, target_id: int = 0) -> tuple[str, str, int]:
    """下载图片到本地，返回 (file_path, md5_hash, file_size)（异步）"""
    save_dir = target_dir or _archive_images_dir()
    if target_id:
        save_dir = os.path.join(save_dir, str(target_id))
    os.makedirs(save_dir, exist_ok=True)
    tmp_path = os.path.join(save_dir, f"tmp_{int(time.time())}_{id(asyncio.current_task())}.tmp")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(image_url)
            if resp.status_code != 200:
                return "", "", 0
            content = resp.content
        md5 = hashlib.md5(content).hexdigest()
        ext = ".jpg"
        if image_url.lower().split("?")[0].endswith(".png"):
            ext = ".png"
        elif image_url.lower().split("?")[0].endswith(".gif"):
            ext = ".gif"
        elif image_url.lower().split("?")[0].endswith(".webp"):
            ext = ".webp"
        final_path = os.path.join(save_dir, f"{md5}{ext}")
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, final_path)
        return final_path, md5, len(content)
    except Exception as e:
        logger.warning(f"图片下载失败: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return "", "", 0


# ============================================================
#  图片存档
# ============================================================
async def archive_image(message_id: int, message_type: str, target_id: int,
                        user_id: int, nickname: str, image_url: str,
                        allowed: bool = True):
    """记录并下载图片（异步，MD5 去重，相同内容只下载/存储一份）

    v2: archive.save_images=false 时跳过媒体下载（DB 记录仍保留，file_path 为空）。
    08-21 status 列（下载存档状态，四值）：
      - allowed=False（接收门控关闭该类型）：写 skipped 行（有 URL 无文件），
        保留 URL 供事后开关打开时重新下载
      - allowed=True：下载，status=ok（文件落盘）/ failed（下载失败，保留 URL）
    """
    ts = time.time()
    if not _save_images_enabled():
        logger.debug(f"📷 图片存档已禁用（archive.save_images=false），跳过下载: {image_url[:60]}")
        return

    with get_db() as conn:
        existing = conn.execute(
            "SELECT file_path, file_size, md5_hash FROM image_archive WHERE image_url = ? LIMIT 1",
            (image_url,),
        ).fetchone()

        file_path = ""
        file_size = 0
        md5_hash = ""
        status = "failed"

        if existing and existing["file_path"] and os.path.exists(existing["file_path"]):
            # 之前已下载成功，复用文件，不重复下载
            file_path = existing["file_path"]
            file_size = existing["file_size"]
            md5_hash = existing["md5_hash"]
            status = "ok"
        elif not allowed:
            # 接收门控关闭该类型：只记 URL 不下载（skipped，事后可补下）
            status = "skipped"
            logger.debug(f"📷 图片跳过下载（接收门控关闭）: {image_url[:60]}")
        else:
            result_path, result_md5, result_size = await _download_image(image_url, target_id=target_id)
            if result_path:
                if result_md5:
                    dup = conn.execute(
                        "SELECT file_path, file_size FROM image_archive WHERE md5_hash = ? LIMIT 1",
                        (result_md5,),
                    ).fetchone()
                    if dup and dup["file_path"] and os.path.exists(dup["file_path"]):
                        file_path = dup["file_path"]
                        file_size = dup["file_size"]
                        md5_hash = result_md5
                        try:
                            os.remove(result_path)
                        except OSError:
                            pass
                    else:
                        file_path = result_path
                        file_size = result_size
                        md5_hash = result_md5
                else:
                    file_path = result_path
                    file_size = result_size
                status = "ok"
            else:
                logger.warning(f"📷 图片下载失败（status=failed，保留 URL）: {image_url[:60]}")
                status = "failed"

        conn.execute(
            "INSERT INTO image_archive (message_id, message_type, target_id, user_id, nickname, "
            "image_url, md5_hash, file_path, file_size, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, message_type, target_id, user_id, nickname, image_url, md5_hash, file_path, file_size, status, ts),
        )
    logger.info(f"📷 图片存档[{status}]: {nickname}({user_id}) -> {file_path or image_url}")


# ============================================================
#  语音下载与存档
# ============================================================
async def _download_voice(voice_url: str) -> tuple[str, str, int]:
    """下载语音文件到本地，返回 (file_path, md5_hash, file_size)（异步）"""
    os.makedirs(_archive_voices_dir(), exist_ok=True)
    tmp_path = os.path.join(_archive_voices_dir(), f"tmp_{int(time.time())}_{id(asyncio.current_task())}.tmp")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(voice_url)
            if resp.status_code != 200:
                return "", "", 0
            content = resp.content
        md5 = hashlib.md5(content).hexdigest()
        ext = ".amr"
        if voice_url.lower().split("?")[0].endswith((".mp3", ".m4a", ".ogg")):
            ext = voice_url.lower().split("?")[0].split(".")[-1]
            ext = f".{ext}"
        final_path = os.path.join(_archive_voices_dir(), f"{md5}{ext}")
        if os.path.exists(final_path):
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return final_path, md5, os.path.getsize(final_path)
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, final_path)
        return final_path, md5, len(content)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        logger.error(f"🎤 语音下载失败: {voice_url} - {e}")
        return "", "", 0


async def archive_voice(message_id: int, message_type: str, target_id: int,
                        user_id: int, nickname: str, voice_url: str,
                        allowed: bool = True):
    """归档语音消息：下载 + 入库（含 MD5 去重）

    v2: archive.save_images=false 时跳过媒体下载（与图片同一开关控制媒体存档）。
    08-21 status 列：
      - allowed=False（接收门控关闭）：写 skipped 行（有 URL 无文件，事后可补下）
      - allowed=True：下载，status=ok/failed（失败也写行保留 URL，08-21 前失败不写行）
    """
    if not _save_images_enabled():
        logger.debug("🎤 媒体存档已禁用（archive.save_images=false），跳过语音下载")
        return
    if not allowed:
        # 接收门控关闭该类型：只记 URL 不下载
        with get_db() as conn:
            conn.execute(
                "INSERT INTO voice_archive "
                "(message_id, message_type, target_id, user_id, nickname, voice_url, md5_hash, file_path, file_size, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '', '', 0, 'skipped', ?)",
                (message_id, message_type, target_id, user_id, nickname, voice_url, time.time()),
            )
        logger.debug(f"🎤 语音跳过下载（接收门控关闭）: {voice_url[:60]}")
        return
    file_path, md5, file_size = await _download_voice(voice_url)
    status = "ok" if file_path else "failed"
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM voice_archive WHERE md5_hash = ? AND md5_hash != ''",
            (md5,),
        ).fetchone()
        if existing and file_path:
            logger.info(f"🎤 语音已存在（MD5 重复），仅记录引用: {md5[:8]}")
        else:
            conn.execute(
                "INSERT INTO voice_archive "
                "(message_id, message_type, target_id, user_id, nickname, voice_url, md5_hash, file_path, file_size, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, message_type, target_id, user_id, nickname, voice_url, md5, file_path, file_size, status, time.time()),
            )
            if status == "ok":
                logger.info(f"🎤 语音已存档: {nickname}({user_id}), {file_size} bytes, {md5[:8]}")
            else:
                logger.warning(f"🎤 语音下载失败（status=failed，保留 URL）: {voice_url[:60]}")


# ============================================================
#  聊天记录转发存档（forward）
# ============================================================
def extract_forward_ids(message_segments: list[dict]) -> list[dict]:
    """从 ArrayMessage 中提取所有 forward 段，返回 [{'id': str, 'content': list|None}]"""
    results = []
    if isinstance(message_segments, list):
        for seg in message_segments:
            if seg.get("type") == "forward":
                data = seg.get("data", {})
                fwd_id = str(data.get("id", ""))
                if fwd_id:
                    results.append({"id": fwd_id, "content": data.get("content")})
    return results


def extract_video_urls(message_segments: list[dict]) -> list[str]:
    """从 ArrayMessage 中提取所有视频 URL"""
    urls = []
    if isinstance(message_segments, list):
        for seg in message_segments:
            if seg.get("type") == "video":
                url = seg.get("data", {}).get("url", "")
                if url:
                    urls.append(url)
    return urls


def _seg_to_marker(seg: dict) -> str:
    """将单个消息段转为可读标记（text 原文 / 其他类型标记）"""
    seg_type = seg.get("type", "")
    data = seg.get("data", {})
    if seg_type == "text":
        return data.get("text", "")
    if seg_type == "image":
        return "[图片]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "face":
        return f"[表情{data.get('id', '')}]"
    if seg_type == "record" or seg_type == "voice":
        return "[语音]"
    if seg_type == "forward":
        return f"[聊天记录转发 {data.get('id', '')}]"
    if seg_type == "reply":
        return "[回复]"
    if seg_type == "at":
        return f"@{data.get('qq', '')}"
    if seg_type == "json":
        # 小程序/卡片：尝试提取可读标题
        import json as _json
        try:
            raw = data.get("data", "")
            parsed = _json.loads(raw) if raw else {}
            prompt = parsed.get("prompt", "") or parsed.get("meta", {}).get("detail_1", "")
            if prompt:
                return f"[卡片]{prompt}"
        except Exception:
            pass
        return "[卡片]"
    if seg_type == "file":
        return f"[文件]{data.get('name', '')}"
    return f"[{seg_type}]"


# 嵌套转发最大展开深度（防循环引用）
_MAX_FORWARD_DEPTH = 10


async def _ensure_napcat_for_fetch(forward_id: str) -> None:
    """转发拉取前预检（08-22 半死态复盘）。

    NapCat HTTP 服务挂死时直接拉取必失败（"Server disconnected"），
    且静默丢数据。预检逻辑：
      - 服务健康 → 直接返回
      - 不健康 + auto_restart 开 → 触发统一自动重启（request_restart，
        与 watchdog 共享冷却）+ 等待恢复（上限 120s，转发是实时操作
        不宜等太久），恢复后继续拉取
      - 不健康 + auto_restart 关 → 记日志后照常尝试（大概率仍失败
        标 failed，与旧行为一致，但日志明确指向原因）
    本函数永不抛异常——预检本身失败不影响主流程。
    """
    from . import napcat_watchdog
    from .config import CONFIG
    healthy, detail = await napcat_watchdog.check_http_healthy()
    if healthy:
        return
    auto = CONFIG.get("NAPCAT_WATCHDOG_AUTO_RESTART", False)
    if not auto:
        logger.info(f"📎 转发预检: NapCat HTTP 服务不健康（{detail}），"
                    f"未开自动重启，直接尝试拉取")
        return
    logger.warning(f"📎 转发预检: NapCat HTTP 服务不健康（{detail}），"
                   f"触发自动重启…")
    r = await napcat_watchdog.request_restart()
    if not r.get("restarted"):
        logger.info(f"📎 转发预检: 未执行重启（{r.get('error')}），直接尝试拉取")
        return
    recovered, elapsed = await napcat_watchdog.wait_healthy(timeout=120)
    if recovered:
        logger.info(f"✅ 转发预检: NapCat 服务已恢复（{elapsed}s），继续拉取")
    else:
        logger.warning(f"📎 转发预检: 重启后 {elapsed}s 未恢复，仍尝试拉取")


def _parse_forward_messages(messages: list, pending_map: Optional[dict] = None) -> tuple[list[dict], list[str], list[str]]:
    """递归解析 get_forward_msg 返回的消息列表。

    返回 (结构列表, 可读文本行列表, 未展开的嵌套 forward_id 列表)。
    嵌套 forward 展开逻辑：
      - 消息段自带 content -> 直接递归展开
      - pending_map 中已有该 forward_id 的解析结果 -> 展开并挂载到段上
      - 否则记入 pending_ids，由调用方递归拉取后再次解析
    图片段保留 url 字段，供后续下载归档。
    """
    nodes: list[dict] = []
    lines: list[str] = []
    pending_ids: list[str] = []

    def walk(msgs, depth: int = 0):
        for m in msgs or []:
            user_id = m.get("user_id", 0)
            sender = m.get("sender", {}) or {}
            nick = sender.get("nickname", "") or sender.get("card", "") or str(user_id)
            ts = m.get("time", 0)
            msg = m.get("message", "")
            node = {
                "user_id": user_id,
                "nickname": nick,
                "time": ts,
                "segments": [],
            }
            if isinstance(msg, list):
                for seg in msg:
                    seg_type = seg.get("type", "")
                    data = seg.get("data", {})
                    marker = _seg_to_marker(seg)
                    seg_info = {"type": seg_type, "text": marker}
                    if seg_type == "image":
                        # 保留图片 URL，供 _archive_forward_images 下载归档
                        seg_info["url"] = data.get("url", "") or ""
                    node["segments"].append(seg_info)
                    if seg_type == "forward":
                        sub = data.get("content")
                        fid = str(data.get("id", ""))
                        if isinstance(sub, list) and sub:
                            walk(sub, depth + 1)
                        elif pending_map and fid and fid in pending_map:
                            sub_nodes, sub_lines = pending_map[fid]
                            seg_info["expanded"] = sub_nodes
                            if sub_lines:
                                for sl in sub_lines:
                                    lines.append("  " * (depth + 1) + sl)
                        elif fid:
                            if fid not in pending_ids:
                                pending_ids.append(fid)
            else:
                node["segments"].append({"type": "raw", "text": str(msg)})
            nodes.append(node)
            # 可读文本行
            texts = [s["text"] for s in node["segments"] if s.get("text")]
            line_text = " ".join(t for t in texts if t).strip()
            if line_text:
                time_str = ""
                if ts:
                    import datetime
                    time_str = datetime.datetime.fromtimestamp(ts + 8 * 3600).strftime("%m-%d %H:%M")
                lines.append(f"[{time_str}] {nick}: {line_text}")

    walk(messages)
    return nodes, lines, pending_ids


def _collect_forward_images(nodes: list[dict]) -> list[dict]:
    """递归收集转发内容中的所有图片段（含嵌套展开的图片）。

    返回 [{'user_id', 'nickname', 'url'}]
    """
    images = []
    seen = set()

    def walk(ns):
        for node in ns or []:
            uid = node.get("user_id", 0)
            nick = node.get("nickname", "") or str(uid)
            for seg in node.get("segments", []) or []:
                if seg.get("type") == "image" and seg.get("url"):
                    url = seg["url"]
                    if url and url not in seen:
                        seen.add(url)
                        images.append({"user_id": uid, "nickname": nick, "url": url})
                if seg.get("type") == "forward" and seg.get("expanded"):
                    walk(seg["expanded"])

    walk(nodes)
    return images


async def _archive_forward_images(nodes: list[dict], outer_message_id: int,
                                  message_type: str, target_id: int) -> int:
    """归档转发内容中的所有图片（下载 + image_archive 入库）。

    返回归档的图片数量。图片段无 URL（NapCat 未返回）时跳过。
    """
    images = _collect_forward_images(nodes)
    if not images:
        return 0
    saved = 0
    for img in images:
        try:
            await archive_image(
                outer_message_id, message_type, target_id,
                img["user_id"], img["nickname"], img["url"],
            )
            saved += 1
        except Exception as e:
            logger.warning(f"转发图片归档失败: {e}")
    logger.info(f"📷 转发内图片归档: {saved}/{len(images)} 张 (外层消息 {outer_message_id})")
    return saved


async def fetch_forward_content(forward_id: str, _depth: int = 0,
                                _cache: Optional[dict] = None) -> tuple[list[dict], list[str], int]:
    """调用 NapCat HTTP API 拉取转发聊天记录内容（递归展开嵌套转发）。

    返回 (nodes, lines, msg_count)。失败时 nodes/lines 为空。
    嵌套 forward 通过 pending_map 递归拉取并展开；同一次调用内相同
    forward_id 只拉取一次（_cache 去重，防循环引用）。
    """
    if _depth > _MAX_FORWARD_DEPTH:
        logger.warning(f"forward {forward_id} 超过最大嵌套深度 {_MAX_FORWARD_DEPTH}，截断")
        return [], [], 0
    if _cache is None:
        _cache = {}
    from .config import CONFIG
    napcat_http = CONFIG.get("NAPCAT_HTTP", "http://127.0.0.1:3000")
    url = f"{napcat_http}/get_forward_msg"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params={"id": forward_id})
            if resp.status_code != 200:
                logger.warning(f"forward {forward_id} 拉取失败 HTTP {resp.status_code}")
                return [], [], 0
            data = resp.json()
        if data.get("status") != "ok":
            logger.warning(f"forward {forward_id} API 返回非 ok: {data.get('retcode')}")
            return [], [], 0
        messages = (data.get("data") or {}).get("messages", [])
        if not messages:
            return [], [], 0

        # 第一轮：解析出未展开的嵌套 forward_id
        nodes, lines, pending = _parse_forward_messages(messages)
        if pending:
            # 递归拉取所有嵌套转发（缓存去重）
            pending_map = {}
            for fid in pending:
                if fid in _cache:
                    sub_nodes, sub_lines, _ = _cache[fid]
                else:
                    sub_nodes, sub_lines, _ = await fetch_forward_content(fid, _depth + 1, _cache)
                    _cache[fid] = (sub_nodes, sub_lines, len(sub_nodes))
                pending_map[fid] = (sub_nodes, sub_lines)
            # 第二轮：带 pending_map 重新解析，展开嵌套内容
            nodes, lines, _ = _parse_forward_messages(messages, pending_map)
        return nodes, lines, len(nodes)
    except Exception as e:
        logger.warning(f"forward {forward_id} 拉取异常: {e}")
        return [], [], 0


async def archive_forward(message_id: int, message_type: str, target_id: int,
                          user_id: int, nickname: str, forward_id: str,
                          created_at: Optional[float] = None,
                          embedded_content: Optional[list] = None):
    """拉取并归档一条聊天记录转发。异步执行，不阻塞消息处理。

    embedded_content: NapCat 有时直接在消息段里带 content（免二次请求）。
    """
    ts = created_at if created_at is not None else time.time()

    # 先去重：同一 forward_id 已成功入库则跳过（嵌套转发会重复出现）
    with get_db() as conn:
        done = conn.execute(
            "SELECT id FROM forward_archive WHERE forward_id = ? AND status = 'ok' LIMIT 1",
            (forward_id,),
        ).fetchone()
    if done:
        logger.info(f"📎 forward {forward_id} 已存档，跳过")
        return

    # 08-22 半死态预检：NapCat HTTP 服务挂死时拉取必失败且静默丢数据。
    # 服务不健康且开了自动重启 → 重启+等恢复后再拉；否则记日志照常尝试。
    # 预检永不抛异常，不影响主流程。
    await _ensure_napcat_for_fetch(forward_id)

    nodes, lines, msg_count = [], [], 0
    if embedded_content and isinstance(embedded_content, list) and embedded_content:
        # 消息段自带 content（对象数组）——直接解析（可能仍有未展开的嵌套）
        nodes, lines, pending = _parse_forward_messages(embedded_content)
        if pending:
            pending_map = {}
            for fid in pending:
                sub_nodes, sub_lines, _ = await fetch_forward_content(fid)
                pending_map[fid] = (sub_nodes, sub_lines)
            nodes, lines, _ = _parse_forward_messages(embedded_content, pending_map)
        msg_count = len(nodes)
    if not nodes:
        nodes, lines, msg_count = await fetch_forward_content(forward_id)

    import json as _json
    status = "ok" if nodes else "failed"
    content_json = _json.dumps(nodes, ensure_ascii=False, default=str) if nodes else ""
    content_text = "\n".join(lines) if lines else ""

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM forward_archive WHERE forward_id = ? AND status = 'ok' LIMIT 1",
            (forward_id,),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO forward_archive "
            "(forward_id, message_id, message_type, target_id, user_id, nickname, "
            "content_json, content_text, status, msg_count, created_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (forward_id, message_id, message_type, target_id, user_id, nickname,
             content_json, content_text, status, msg_count, ts, time.time()),
        )
    logger.info(f"📎 转发存档: {nickname}({user_id}) forward={forward_id} msgs={msg_count} status={status}")

    # 归档转发内容中的所有图片（下载 + image_archive 入库）
    if nodes:
        try:
            await _archive_forward_images(nodes, message_id, message_type, target_id)
        except Exception as e:
            logger.warning(f"转发图片归档异常: {e}")


async def archive_video(message_id: int, message_type: str, target_id: int,
                        user_id: int, nickname: str, video_url: str):
    """归档视频消息：记录 URL（不下载，视频文件太大且 qq 多媒体 URL 易过期）。"""
    ts = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO video_archive "
            "(message_id, message_type, target_id, user_id, nickname, video_url, file_path, file_size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '', 0, ?)",
            (message_id, message_type, target_id, user_id, nickname, video_url, ts),
        )
    logger.info(f"🎬 视频记录: {nickname}({user_id}) -> {video_url[:60]}...")


# ============================================================
#  撤回消息记录
# ============================================================
def extract_image_urls_from_raw(raw: str) -> list[str]:
    """从 CQ 码字符串中直接提取图片 URL（用于撤回时异步 image_archive 尚未写入的场景）"""
    import re
    urls = re.findall(r'\[CQ:image[^\]]*url=(.+?)\]', raw, re.DOTALL)
    return [re.sub(r',file_size=\d+$', '', u).replace('&amp;', '&').strip() for u in urls]


# ============================================================
#  消息类型派生（08-21 统一标志位方案）
# ============================================================
# msg_kind 六值，优先级（防未来一条消息多类型；当前数据多类型共现=0）：
#   forward > video > voice > image > file > text
# 真相源是 raw_message 的 CQ 标记（100% 随消息落库，不被下载开关污染）。
_MSG_KIND_RULES: list[tuple[str, str]] = [
    ("forward", "[CQ:forward"),
    ("video",   "[CQ:video,"),
    ("voice",   "[CQ:record,"),   # NapCat 语音统一为 CQ:record
    ("image",   "[CQ:image,"),
    ("file",    "[CQ:file,"),
]
# 兼容 CQ:voice 旧写法（历史数据/其他 NapCat 版本）
_VOICE_MARKER_ALT = "[CQ:voice,"


def derive_msg_kind(raw_message: str) -> str:
    """从 raw_message 的 CQ 标记派生消息类型（唯一真相源，08-21）。

    返回六值之一：forward/video/voice/image/file/text。
    纯函数、幂等——写入链路和回填脚本共用此实现，杜绝规则漂移。
    """
    if not raw_message:
        return "text"
    for kind, marker in _MSG_KIND_RULES:
        if kind == "voice":
            if marker in raw_message or _VOICE_MARKER_ALT in raw_message:
                return "voice"
        elif marker in raw_message:
            return kind
    return "text"


def archive_recall(message_id: int, operator_id: int, message_type: str,
                   target_id: int, user_id: int, nickname: str = "", content: str = ""):
    """记录一条撤回消息（同步，不阻塞）

    v2: archive.save_recall_messages=false 时不记录撤回消息。
    """
    if not _save_recall_messages_enabled():
        logger.debug(f"↩️ 撤回消息存档已禁用（archive.save_recall_messages=false），跳过: {message_id}")
        return
    ts = time.time()
    with get_db() as conn:
        row = conn.execute(
            "SELECT content, nickname, has_image, raw_message, msg_kind FROM message_archive "
            "WHERE message_id = ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
            (message_id, target_id),
        ).fetchone()

        if row:
            if not content:
                content = row["content"]
            if not nickname:
                nickname = row["nickname"]

        has_image = bool(row and row["has_image"]) if row else 0
        # 08-21：从原消息继承 msg_kind（原消息未存档时兜底从 content/raw 派生）
        if row and row["msg_kind"]:
            msg_kind = row["msg_kind"]
        elif row and row["raw_message"]:
            msg_kind = derive_msg_kind(row["raw_message"])
        else:
            msg_kind = "image" if has_image else "text"

        cursor = conn.execute(
            "INSERT INTO message_recalls "
            "(message_id, operator_id, message_type, target_id, user_id, nickname, content, has_image, msg_kind, recalled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, operator_id, message_type, target_id, user_id, nickname, content, int(has_image), msg_kind, ts),
        )
        recall_id = cursor.lastrowid

        if has_image and row and row["raw_message"] and _save_recall_images_enabled():
            raw = row["raw_message"]
            for url in extract_image_urls_from_raw(raw):
                existing = conn.execute(
                    "SELECT file_path FROM image_archive WHERE message_id = ? AND target_id = ? AND image_url = ? LIMIT 1",
                    (message_id, target_id, url),
                ).fetchone()

                if existing and existing["file_path"] and os.path.exists(existing["file_path"]):
                    recall_dir = os.path.join(_recall_images_dir(), str(target_id))
                    os.makedirs(recall_dir, exist_ok=True)
                    src_path = existing["file_path"]
                    ext = os.path.splitext(src_path)[1] or ".jpg"
                    file_size = os.path.getsize(src_path)
                    with open(src_path, "rb") as f:
                        md5 = hashlib.md5(f.read()).hexdigest()
                    recall_path = os.path.join(recall_dir, f"{md5}{ext}")
                    if not os.path.exists(recall_path):
                        import shutil
                        shutil.copy2(src_path, recall_path)
                    conn.execute(
                        "INSERT INTO recall_image "
                        "(recall_id, message_id, message_type, target_id, user_id, nickname, image_url, file_path, file_size, recalled_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (recall_id, message_id, message_type, target_id, user_id, nickname, url, recall_path, file_size, ts),
                    )
                else:
                    file_path, md5, file_size = _download_image_sync(url, target_dir=_recall_images_dir(), target_id=target_id)
                    if file_path:
                        conn.execute(
                            "INSERT INTO image_archive "
                            "(message_id, message_type, target_id, user_id, nickname, image_url, file_path, md5_hash, file_size, status, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
                            (message_id, message_type, target_id, user_id, nickname, url, file_path, md5, file_size, ts),
                        )
                        conn.execute(
                            "INSERT INTO recall_image "
                            "(recall_id, message_id, message_type, target_id, user_id, nickname, image_url, file_path, file_size, recalled_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (recall_id, message_id, message_type, target_id, user_id, nickname, url, file_path, file_size, ts),
                        )
                        logger.info(f"↩️ 撤回图片同步下载: {nickname} -> {os.path.basename(file_path)}")
                    else:
                        conn.execute(
                            "INSERT INTO recall_image "
                            "(recall_id, message_id, message_type, target_id, user_id, nickname, image_url, recalled_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (recall_id, message_id, message_type, target_id, user_id, nickname, url, ts),
                        )
                        logger.warning(f"↩️ 撤回图片下载失败（可能 URL 已过期）: {nickname}")

        if message_type == "group":
            conn.execute(
                "DELETE FROM group_chat_cache WHERE message_id = ? AND group_id = ?",
                (message_id, target_id),
            )

        logger.info(f"↩️ 撤回记录: {nickname}({user_id}) 在 {message_type} {target_id}, 操作者 {operator_id}, 含图片={has_image}")


async def _archive_recall_images(message_id: int, target_id: int, message_type: str, user_id: int):
    """下载撤回消息中的图片到独立存档目录（异步后台）

    v2: archive.save_recall_images=false 时直接跳过。
    """
    if not _save_recall_images_enabled():
        return
    try:
        with get_db() as conn:
            recall_row = conn.execute(
                "SELECT id, nickname FROM message_recalls WHERE message_id = ? AND target_id = ? ORDER BY recalled_at DESC LIMIT 1",
                (message_id, target_id),
            ).fetchone()
            if not recall_row:
                return
            recall_id = recall_row["id"]
            nickname = recall_row["nickname"]

            imgs = conn.execute(
                "SELECT id, image_url, file_path, file_size FROM recall_image WHERE recall_id = ?",
                (recall_id,),
            ).fetchall()

        for img in imgs:
            img_id = img["id"]
            img_url = img["image_url"]
            existing_file = img["file_path"]

            with get_db() as conn:
                done = conn.execute(
                    "SELECT file_path FROM recall_image WHERE recall_id = ? AND file_path != '' AND file_path IS NOT NULL AND file_path LIKE '%/recalls/%'",
                    (recall_id,),
                ).fetchone()
                if done and os.path.exists(done["file_path"]):
                    continue

            if existing_file and os.path.exists(existing_file):
                import shutil
                save_dir = os.path.join(_recall_images_dir(), str(target_id))
                os.makedirs(save_dir, exist_ok=True)
                filename = os.path.basename(existing_file)
                recall_path = os.path.join(save_dir, filename)
                if not os.path.exists(recall_path):
                    shutil.copy2(existing_file, recall_path)
                with get_db() as conn:
                    conn.execute(
                        "UPDATE recall_image SET file_path = ?, file_size = ? WHERE id = ?",
                        (recall_path, os.path.getsize(recall_path), img_id),
                    )
                logger.info(f"↩️ 撤回图片已复制到 recalls: {nickname}({user_id}) -> {recall_path}")
                continue

            if not existing_file or not os.path.exists(existing_file):
                file_path, md5_hash, file_size = await _download_image(img_url, _recall_images_dir(), target_id)
                if file_path:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE recall_image SET file_path = ?, file_size = ? WHERE id = ?",
                            (file_path, file_size, img_id),
                        )
                    logger.info(f"↩️ 撤回图片已保存: {nickname}({user_id}) -> {file_path}")
    except Exception as e:
        logger.warning(f"撤回图片归档失败: {e}")


# ============================================================
#  兼容别名（bot.py 启动时建目录用；运行时新代码走 _xxx_dir() 函数）
# ============================================================
_ARCHIVE_IMAGES_DIR = _archive_images_dir()
_RECALL_IMAGES_DIR = _recall_images_dir()
_ARCHIVE_VOICES_DIR = _archive_voices_dir()
