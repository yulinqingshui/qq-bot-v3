#!/usr/bin/env python3
"""
llm_usage.py — LLM 用量统计（token 消耗追踪 + 持久化）
=====================================================
- 每次 LLM 调用成功返回后，从 response.usage 累加
  prompt_tokens / completion_tokens / total_tokens
- 按日期（本地时区）分桶 + 按模型分桶
- 持久化到 <data_dir>/llm_usage.json（bot 重启不丢）
- 控制 API GET /llm/usage 读取，GUI 配置页底部展示

线程/事件循环安全：所有写操作在 bot 的 asyncio 事件循环内
（_post_llm_chat 是 async，同一线程），用 asyncio.Lock 防并发写文件。
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime

logger = logging.getLogger("qq-bot")

# 全局累加器（内存热数据；_persist 时落盘）
_usage_state = {
    "total": {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    },
    "by_day": {},    # "2026-08-19" -> {calls, prompt_tokens, completion_tokens, total_tokens}
    "by_model": {},  # model -> {calls, prompt_tokens, completion_tokens, total_tokens}
}

# 最近 LLM 请求（环形缓冲，内存热数据，不落盘——仅用于 GUI 实时展示最近一次请求）
# 每条: {seq, time, model, source, preview}
#   source: bot 调用来源标记（回复/人设/出题/画像/测试连接…），测试连接走 control_api 单独打 tag
_recent_requests: deque = deque(maxlen=20)
_request_seq = 0  # 单调递增序号（GUI 轮询防重复/防覆盖：只显示 seq 更新的请求）
_lock = asyncio.Lock()
_persist_path = None  # 延迟初始化（需要 CONFIG 的 data_dir）


def _usage_file() -> str:
    """持久化文件路径：<data_dir>/llm_usage.json（首次调用时初始化）。

    2026-08-23 修复：原实现 `CONFIG.get("DATA_DIR", "data")` 的 DATA_DIR
    键在 CONFIG 里**根本不存在**（config.py flatten 从未产出该键，数据目录
    藏在 _abs(paths.data_dir) 里只用于拼 DB_PATH 等）→ 一直落回相对路径
    "data"，纯靠 bot 进程 cwd 恰好是项目根才没出事。改为从 DB_PATH 取
    dirname（= _abs(paths.data_dir)，恒为绝对路径，与 GUI db_path 同约定）。
    """
    global _persist_path
    if _persist_path is None:
        try:
            from .config import CONFIG
            _persist_path = os.path.join(
                os.path.dirname(CONFIG.get("DB_PATH", "data/chat_history.db")),
                "llm_usage.json")
        except Exception:
            _persist_path = os.path.join("data", "llm_usage.json")
    return _persist_path


def _empty_bucket() -> dict:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _add_bucket(bucket: dict, usage: dict) -> None:
    bucket["calls"] += 1
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    tt = int(usage.get("total_tokens", 0) or (pt + ct))
    bucket["prompt_tokens"] += pt
    bucket["completion_tokens"] += ct
    bucket["total_tokens"] += tt


async def record_usage(usage: dict, model: str = "") -> None:
    """记录一次调用的 usage（OpenAI 格式 usage 字段）。

    失败/无 usage 时静默跳过——统计是旁路功能，不能影响主链路。
    """
    if not usage:
        return
    try:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        model_key = model or "(unknown)"
        async with _lock:
            _add_bucket(_usage_state["total"], usage)
            _add_bucket(_usage_state["by_day"].setdefault(day, _empty_bucket()), usage)
            _add_bucket(_usage_state["by_model"].setdefault(model_key, _empty_bucket()), usage)
            await _persist()
    except Exception as e:
        logger.warning("llm_usage.record 失败（不影响主链路）: %s", e)


def record_request(model: str, reply: str, source: str = "llm",
                   finish_reason: str = "") -> None:
    """记录一次 LLM 请求的摘要（GUI 总览页"最近请求"行展示用）。

    同步函数（无磁盘 IO，仅写内存环形缓冲）——可在 async 上下文直接调用。
    preview 截断到 80 字符、去换行（GUI 单行显示）；full 保留全文
    （08-22：GUI 结果行点击弹窗看完整输出，环形缓冲 20 条内存开销可忽略）；
    source 是调用来源标记。
    08-22：去 4000 字硬截断（长输出人设/画像点开弹窗不完整）——改为 100k
    防御上限（20 条 × 100k ≈ 2MB 极端内存，防病态超长；正常输出 <10k 字）；
    新增 finish_reason（"length"=生成被 max_tokens 截断，GUI 弹窗顶部提示
    区分"显示截断"vs"生成就断了"）。
    """
    global _request_seq
    try:
        _request_seq += 1
        reply_text = reply or ""
        preview = " ".join(reply_text.split())[:80]
        _recent_requests.append({
            "seq": _request_seq,
            "time": time.strftime("%H:%M:%S"),
            "model": model or "(unknown)",
            "source": source or "llm",
            "preview": preview,
            "full": reply_text[:100000],  # 100k 防御上限（非显示截断）
            "finish_reason": finish_reason or "",
        })
    except Exception:
        pass  # 旁路功能，绝不抛错影响主链路


def get_recent_request() -> dict:
    """读取最近一次 LLM 请求（GUI 控制 API / 轮询用）。无记录返回 {}。"""
    return dict(_recent_requests[-1]) if _recent_requests else {}


async def _persist() -> None:
    """落盘（调用方已持锁）"""
    try:
        path = _usage_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **_usage_state,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("llm_usage 落盘失败: %s", e)


def get_usage() -> dict:
    """读取当前用量（GUI 控制 API 用）。

    首次调用时从磁盘加载历史（bot 重启后恢复），之后读内存热数据。
    """
    global _persist_path
    # 懒加载：内存为空且磁盘有历史 → 恢复
    if _usage_state["total"]["calls"] == 0 and not _loaded_from_disk:
        _load_from_disk()
    path = _usage_file()
    return {
        "file": path,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": dict(_usage_state["total"]),
        # 最近 7 天（升序）
        "by_day": dict(sorted(_usage_state["by_day"].items())[-7:]),
        # token 最多的前 5 个模型
        "by_model": dict(
            sorted(_usage_state["by_model"].items(),
                   key=lambda kv: kv[1]["total_tokens"], reverse=True)[:5]
        ),
    }


_loaded_from_disk = False


def _load_from_disk() -> None:
    global _loaded_from_disk
    _loaded_from_disk = True
    try:
        path = _usage_file()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        total = data.get("total", {})
        for k in _usage_state["total"]:
            _usage_state["total"][k] = int(total.get(k, 0) or 0)
        for day, b in (data.get("by_day") or {}).items():
            _usage_state["by_day"][day] = {
                k: int(b.get(k, 0) or 0) for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
            }
        for m, b in (data.get("by_model") or {}).items():
            _usage_state["by_model"][m] = {
                k: int(b.get(k, 0) or 0) for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
            }
        logger.info("llm_usage: 已恢复历史用量（%s 次调用）", _usage_state["total"]["calls"])
    except Exception as e:
        logger.warning("llm_usage 加载历史失败: %s", e)


def _fmt_tokens(n: int) -> str:
    if n >= 1000000:
        return f"{n / 1000000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def format_summary() -> str:
    """单行摘要（GUI 状态栏/日志用）"""
    t = _usage_state["total"]
    if t["calls"] == 0:
        return "LLM 用量: 暂无记录"
    return (f"LLM 用量: {t['calls']} 次调用 · "
            f"输入 { _fmt_tokens(t['prompt_tokens']) } / "
            f"输出 { _fmt_tokens(t['completion_tokens']) } / "
            f"合计 { _fmt_tokens(t['total_tokens']) } tokens")
