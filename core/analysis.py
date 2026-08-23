"""
analysis.py — 聊天记录 Map-Reduce 分析核心（2026-08-21 从 router.handle_query 抽出）
=============================================================================
/查询 指令（群聊）与 GUI 消息分析（control_api /analysis/query）共用：

  prepare_chat_rows(rows)      行 → 人物映射(U1/U2) + 格式化消息行（单一实现）
  run_query_analysis(...)      Map 并行提取 → U 引用归一化 → Reduce 汇总 → 落库审计

prompt 只在本文件定义一处——/查询 与 GUI 分析行为永远一致，改 prompt 只改这里。
handle_query 保留：取数（group_chat_cache 近 N 小时）+ 群内"请稍候"反馈。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from .persona import _normalize_u_refs
from .llm import call_llm, llm_enabled
from .scheduler import chunk_messages_by_token
from .database import save_query_batch
from .qa_prompts import (render_prompt, qa_params, qa_llm,
                         qa_llm_scope, thinking_kwargs)

logger = logging.getLogger("qq-bot")


class AnalysisError(Exception):
    """分析失败（LLM 全批次失败 / 调用异常）。"""


def prepare_chat_rows(rows: list) -> tuple:
    """行列表 → (batch_lines, nick_map_header, short_map, total)。

    rows：[{user_id, nickname, content, created_at}, ...]（已按时间 ASC 排好）。
    与 2026-08-05 前的 handle_query 内联实现逐字一致：
      - 用户映射 U1/U2...（按首次出现顺序）
      - 人物表：U1=昵称(QQ号)（QQ 号锚点防同名混淆，Pitfall 87）
      - 消息行：#序号 HH:MM Ux: 内容（300 字截断、真实换行转 \\n 保持一行一消息）
    """
    uid_to_short: dict = {}
    _counter = 1
    for row in rows:
        uid = row["user_id"]
        if uid not in uid_to_short:
            uid_to_short[uid] = f"U{_counter}"
            _counter += 1

    short_map: dict = {}
    nick_map_lines = []
    for uid, short in uid_to_short.items():
        nick = ""
        for row in rows:
            if row["user_id"] == uid:
                nick = row["nickname"] or f"用户{uid}"
                break
        short_map[short] = {"nickname": nick, "qq": str(uid)}
        nick_map_lines.append(f"{short}={nick}({uid})")
    nick_map_header = "人物:\n" + "\n".join(nick_map_lines) + "\n\n"

    batch_lines: list = []
    _trunc = int(qa_params().get("msg_truncate_chars", 300))  # 消息截断字数（qa 段，热生效）
    for i, row in enumerate(rows, 1):
        ts = datetime.fromtimestamp(row["created_at"]).strftime("%H:%M") if row["created_at"] else ""
        content = (row["content"] or "")[:_trunc]
        # 消息内真实换行转义为字面 \\n，保证"一条消息一行"契约
        content = content.replace("\n", "\\n")
        uid = row["user_id"]
        batch_lines.append(f"#{i} {ts} {uid_to_short[uid]}: {content}")

    return batch_lines, nick_map_header, short_map, len(rows)


async def run_query_analysis(
    question: str,
    rows: list,
    scope_desc: str,
    source: str = "cmd",
    group_id: int = 0,
    hours: int = 0,
    progress_cb: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    run_id: Optional[int] = None,
) -> tuple[str, int]:
    """对 rows 执行 Map-Reduce 分析，返回 (最终答案, 批次数)。

    Args:
        question:   用户问题（自然语言）
        rows:       聊天记录行（ASC 时间序；{user_id,nickname,content,created_at}）
        scope_desc: 输入来源描述（拼进 prompt"以下是{scope_desc}的批次 x/y"）。
                    /查询 传 f"近 {hours} 小时群聊记录"（与原版逐字一致）；
                    GUI 传"上方筛选出的聊天记录"等。
        source:     审计来源标记（query_batch_results.source 列）
        group_id:   审计用（GUI 多群筛选时传 0）
        hours:      审计用（GUI 无时间窗概念时传 0）
        progress_cb: 可选 async 回调 (done, total, stage)，Map 每批完成/
                    Reduce 开始时调用（GUI 进度展示用）
        run_id:     审计 run_id。GUI 路径传 control_api 任务表的 run_id（保证
                    任务 run_id == 审计 run_id，进度兜底可查）；/查询 传 None
                    由本函数自生成（保持原行为）。

    Raises:
        AnalysisError: 全部 Map 批次无有效结果（LLM 全挂 / 无相关信息）
    """
    if run_id is None:
        run_id = int(time.time() * 1000)

    # LLM 总开关早退（2026-08-21 审计）：/查询 与 GUI 消息分析共用本核心，
    # 关闭时直接抛 AnalysisError——不发起任何 LLM 调用、不写 query_batch_results、
    # 调用方按"未找到"路径提示（router 群内 / control_api 任务表均安全）
    if not llm_enabled():
        raise AnalysisError("LLM 总开关关闭，暂时无法分析（GUI 总览页 LLM 板块可开启）")

    batch_lines, nick_map_header, short_map, total = prepare_chat_rows(rows)

    # 分块（按累计字符≈token 数，目标 map_batch_chars/batch；qa 段，热生效）
    _batch_chars = int(qa_params().get("map_batch_chars", 40000))
    chunks = chunk_messages_by_token(batch_lines, target_tokens=_batch_chars)
    if chunks:
        first_batch_text = "\n".join(chunks[0])
        chunks[0] = [nick_map_header + first_batch_text]
    total_batches = len(chunks)

    all_analysis_results: list = []
    _done_count = 0   # Map 完成计数（asyncio 单线程内非原子操作安全）

    async def _report(stage: str):
        nonlocal _done_count
        if progress_cb is not None:
            try:
                await progress_cb(_done_count, total_batches, stage)
            except Exception:
                pass  # 进度回调失败不影响分析

    # ---------------- Map 阶段（并行） ----------------
    async def _process_query_batch(batch_num: int, chunk: list) -> str:
        nonlocal _done_count
        batch_text = "\n".join(chunk)
        logger.info(f"📡 查询批次 {batch_num}/{total_batches} (source={source})...")

        # 提示词（qa_prompts 单一来源；用户定制经 CONFIG 热生效）
        query_system_prompt = render_prompt("query_map_system")
        user_prompt = render_prompt("query_map_user", {
            "question": question,
            "scope_desc": scope_desc,
            "batch_num": batch_num,
            "total_batches": total_batches,
            "batch_text": batch_text,
        })

        # LLM 参数（qa.llm.query 段，默认=原硬编码行为）
        _q = qa_llm_scope("query")
        _common = qa_llm()
        reply = await call_llm(
            [{"role": "system", "content": query_system_prompt},
             {"role": "user", "content": user_prompt}],
            max_tokens=int(_q.get("map_max_tokens", 131072)),
            parallel=True,
            source=f"消息分析({source})",
            temperature=float(_common.get("temperature", 0.7)),
            timeout=int(_common.get("timeout", 1800)),
            **thinking_kwargs(_q.get("map_thinking", "on")),
        )
        reply = reply.strip()

        done = batch_num  # Map 批次按号完成，进度取 max
        if reply.startswith(("😵", "🔕")):
            logger.warning(f"⚠️ 查询批次 {batch_num} LLM 调用失败")
            save_query_batch(group_id, run_id, question, hours,
                             batch_num, total_batches, len(batch_text), "map",
                             reply, is_valid=0, source=source)
            _done_count += 1
            await _report("map")
            return ""
        if reply == "无相关信息":
            save_query_batch(group_id, run_id, question, hours,
                             batch_num, total_batches, len(batch_text), "map", reply,
                             source=source)
            _done_count += 1
            await _report("map")
            return ""
        # U 编号引用归一化为 昵称(qq号)
        normalized = _normalize_u_refs(reply, short_map)
        save_query_batch(group_id, run_id, question, hours,
                         batch_num, total_batches, len(batch_text), "map", normalized,
                         source=source)
        _done_count += 1
        await _report("map")
        return normalized

    results = await asyncio.gather(
        *[_process_query_batch(i + 1, chunk) for i, chunk in enumerate(chunks)])
    all_analysis_results = [r for r in results if r]

    # ---------------- Reduce 阶段 ----------------
    if not all_analysis_results:
        raise AnalysisError(
            f"在{scope_desc}中未找到与「{question}」相关的信息"
            if source == "cmd" else
            f"未找到与「{question}」相关的信息（或 LLM 全部批次调用失败）")

    if progress_cb is not None:
        try:
            await progress_cb(total_batches, total_batches, "reduce")
        except Exception:
            pass

    # 统一走 Reduce 阶段，确保用户看到的是自然语言回答
    combined = "\n---\n".join(all_analysis_results)
    summary_user_prompt = render_prompt("query_reduce_user", {
        "question": question,
        "combined": combined,
    })

    _q = qa_llm_scope("query")
    _common = qa_llm()
    summary = await call_llm(
        [{"role": "system", "content": render_prompt("query_reduce_system")},
         {"role": "user", "content": summary_user_prompt}],
        max_tokens=int(_q.get("reduce_max_tokens", 16384)),
        source=f"消息分析({source})",
        temperature=float(_common.get("temperature", 0.7)),
        timeout=int(_common.get("timeout", 1800)),
        **thinking_kwargs(_q.get("reduce_thinking", "on")),
    )
    summary = summary.strip()
    # Reduce 输出兜底归一化（防 LLM 输出 U 引用）
    summary = _normalize_u_refs(summary, short_map)

    # 最终答案落库（batch_index=0 标识 reduce 结果）
    save_query_batch(group_id, run_id, question, hours,
                     0, total_batches, len(combined), "reduce", summary,
                     source=source)
    logger.info(f"✅ 分析完成 ({total} 条记录, {total_batches} 批次, source={source})")
    return summary, total_batches
