"""
task_registry.py — 进程内任务注册表（2026-08-22，总览页「任务列表」面板数据源）
=============================================================================
用途：
    追踪当前**正在进行 + 排队中**的后台任务，供 GUI 总览页第二行第三列
    「📋 任务列表」面板显示（含暂停/继续控制）。数据经
    control_api.get_status() 的 "tasks" 字段返回，GUI 复用 2 秒轮询刷新。

设计：
    - 纯内存态（bot 重启即清空——正好符合"当前正在进行/排队"的语义；
      暂停态同样重启清空，与任务列表语义一致）
    - asyncio 单线程事件循环内操作，无锁无竞态
    - 旁路登记：埋点只调本模块 API，不改变任何任务执行逻辑；
      本模块任何异常都由埋点方 try/except 吞掉（登记失败最多面板少一行，
      绝不影响任务本身）
    - 条目生命周期：queued/running → finish 即移除（完成态不保留，
      面板只显示"正在做"和"排队等"）

暂停/继续（2026-08-22 用户拍板：范围=全部任务、时长=无限等待）：
    - 软暂停：卡"新任务开始"，不打断"执行中任务"（进行中的 LLM 调用不
      cancel——防 DB 半提交/进度消息发一半/费用已发生）
    - 实现：13 个 queued→running 转换点在转 running 前
      `await TASK_REGISTRY.wait_if_paused()` 等门；暂停时 Event 未 set，
      任务原地等；resume 时 set() 放行全部等待者（按原 FIFO 顺序继续）
    - pause/resume 幂等；snapshot 带 "paused" 字段供 GUI 按钮态

任务来源（5 类埋点）：
    1. persona 批量更新 ×3（/更新全部人设 / 画像 / 画像和人设）
       —— 循环前全量 queued，循环内逐用户 running，面板显示
          "当前正在更新的用户 + 后面所有排队的用户"
    2. persona 单人更新（/更新画像和人设 xxx）
    3. scheduler 定时联合更新（用户层循环）+ 题库补充（集群层单条）
    4. router _safe_command（手动群指令：/查询 /分析 /总结 /评选 /补充题库 /群像）
       —— 等 FIFO 锁时 queued，获锁转 running
    5. router _execute_ai_command（AI 执行标记直调路径：query/analysis/group_persona
       ——不走 FIFO 锁，单独埋；label 带 🤖 前缀区分来源）
    6. llm.call_llm chat 锁（AI 聊天回复：等 chat 锁 queued、持锁 running）
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskEntry:
    """一条任务记录（snapshot 时转 dict 给 GUI）。"""
    key: str
    category: str            # "人设画像更新" / "群指令" / "定时任务" / "题库维护"
    label: str               # 显示文本（含昵称/QQ号；定时带 ⏰、AI 触发带 🤖）
    status: str              # "queued" | "running"
    group_id: int = 0
    user_id: int = 0
    queued_at: float = field(default_factory=time.time)


class TaskRegistry:
    """内存态任务注册表。单例见模块底部 `TASK_REGISTRY`。"""

    def __init__(self) -> None:
        self._entries: dict[str, TaskEntry] = {}
        # 单调递增计数器：同用户同指令并发时 key 唯一
        self._seq = itertools.count(1)
        # 2026-08-22 软暂停门（懒绑定事件循环）
        self._paused: bool = False
        self._resume_event: Optional[asyncio.Event] = None

    # ------------------------------------------------------------ 登记
    def register(self, category: str, label: str, *,
                 group_id: int = 0, user_id: int = 0,
                 status: str = "running", key: Optional[str] = None) -> str:
        """登记一条任务，返回 key。key 缺省时自动生成（category+seq）。"""
        if key is None:
            key = f"{category}:{next(self._seq)}"
        if key in self._entries:
            # 同 key 重复登记：覆盖（防御埋点重入，不抛错）
            self._entries[key] = TaskEntry(
                key=key, category=category, label=label,
                status=status, group_id=group_id, user_id=user_id)
            return key
        self._entries[key] = TaskEntry(
            key=key, category=category, label=label,
            status=status, group_id=group_id, user_id=user_id)
        return key

    # ------------------------------------------------------------ 批量
    def begin_batch(self, batch_id: str, category: str,
                    items: list) -> list[str]:
        """批量任务登记：循环前把全部条目注册为 queued。

        items 两种格式（兼容）：
          - [label, ...]                    —— 纯字符串列表（group/user 用 0）
          - [(label, gid, uid), ...]        —— 三元组列表
        返回与 items 对齐的 key 列表（供循环内逐条 start/finish）。
        """
        keys: list[str] = []
        for i, item in enumerate(items):
            if isinstance(item, tuple):
                label, gid, uid = item
            else:
                label, gid, uid = item, 0, 0
            keys.append(self.register(
                category, label, group_id=gid, user_id=uid,
                status="queued", key=f"{batch_id}:{i}"))
        return keys

    def set_status(self, key: str, status: str) -> None:
        """queued → running 转换（或任意状态修正）。不存在则忽略。"""
        e = self._entries.get(key)
        if e is not None:
            e.status = status

    # ------------------------------------------------------------ 暂停/继续
    def _ensure_event(self) -> asyncio.Event:
        """懒创建 resume 门（绑当前事件循环；未暂停=已 set）。"""
        if self._resume_event is None:
            self._resume_event = asyncio.Event()
            if not self._paused:
                self._resume_event.set()
        return self._resume_event

    def pause(self) -> bool:
        """暂停：禁止新任务开始（执行中任务跑完当前步）。幂等。

        返回 True=状态发生了变化（非暂停→暂停）。
        """
        if self._paused:
            return False
        self._paused = True
        # 事件须先建再 clear（pause 可能在无运行循环的上下文被调——防御）
        try:
            self._ensure_event().clear()
        except RuntimeError:
            # 无事件循环（不该发生：控制 API 在 bot 主循环内）——
            # _paused 已置位，后续 wait_if_paused 会新建未 set 的 Event
            self._resume_event = None
        return True

    def resume(self) -> bool:
        """继续：放行全部等待中的任务。幂等。返回 True=状态发生了变化。"""
        if not self._paused:
            return False
        self._paused = False
        ev = self._resume_event
        if ev is not None:
            try:
                ev.set()
            except RuntimeError:
                pass  # 事件绑定的循环已死；新 wait 会建新事件（未暂停直接放行）
        return True

    async def wait_if_paused(self) -> None:
        """13 个转换点的统一门：未暂停立即返回；暂停时等 resume。

        无限等待（用户拍板，无超时）——等期间任务保持 queued 态显示在
        面板上（⏸ 前缀由 GUI 渲染）。
        """
        if not self._paused:
            return
        ev = self._ensure_event()
        # _ensure_event 懒建时若 _paused=True 则 Event 未 set（初始未 set），
        # 直接等；若中途被 resume，_ensure_event 前 _paused 已 False 早退了
        await ev.wait()

    # ------------------------------------------------------------ 移除
    def finish(self, key: str) -> None:
        """任务结束（成功/失败/超时都调）：移除条目。不存在则忽略。"""
        self._entries.pop(key, None)

    def finish_batch(self, keys: list[str]) -> None:
        for k in keys:
            self._entries.pop(k, None)

    # ------------------------------------------------------------ 快照
    def snapshot(self) -> dict:
        """返回 GUI 用快照（深拷贝语义：新 dict，条目转 dict）。

        running 在前、queued 在后，各自按入队时间升序；paused 供 GUI 按钮态。
        """
        now = time.time()
        running = [e for e in self._entries.values() if e.status == "running"]
        queued = [e for e in self._entries.values() if e.status == "queued"]
        running.sort(key=lambda e: e.queued_at)
        queued.sort(key=lambda e: e.queued_at)

        def _d(e: TaskEntry) -> dict:
            return {
                "key": e.key,
                "category": e.category,
                "label": e.label,
                "status": e.status,
                "group_id": e.group_id,
                "user_id": e.user_id,
                "elapsed": int(now - e.queued_at),
            }

        return {
            "running": [_d(e) for e in running],
            "queued": [_d(e) for e in queued],
            "count": len(running) + len(queued),
            "paused": self._paused,
        }

    def __len__(self) -> int:
        return len(self._entries)


# 全局单例（bot 进程内唯一；GUI 进程不 import 本模块）
TASK_REGISTRY = TaskRegistry()
