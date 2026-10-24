"""CompactionManager：四层防御中 L2/L3 的阈值编排（M8）。

由服务层构造并注入 AgentLoop（``on_model_usage`` 回调——每次模型回合
结束、工具批次执行**之前**拿到累计 usage；压缩发生在两次模型调用之间，
绝不打断流）。判定口径：``provider usage.input_tokens / 模型档案
contextWindow``（usage 缺失则不动作）。

- 用量 ≥ ``evict_threshold``（默认 0.5）→ L2 机械淘汰（旧 tool_result 换
  占位符，配对不断裂；已是占位符的跳过，幂等）；无事可做不广播；
- 用量 ≥ ``summarize_threshold``（默认 0.85）→ L3 结构化摘要（压缩专用
  模型可配置，回落主模型；模型失败降级为一次 L2 并把失败写进报告）；
- ``control compact`` 手动触发 = 强制 L3（同语义）。

每次实际压缩广播 ``compaction{layer, report}``（帧已注册，PLAN §5），
并就地重建 ``loop.messages``（``list[:]`` 原位替换，保持 loop 持有的
列表对象不变）。压缩后跑 :func:`pairing_intact` 自检——L3 重建若破坏
配对（理论不可达，安全切点保证）则放弃替换并广播错误报告。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Protocol

from ..config import api_key_for
from ..llm.base import Provider, UsageUpdate
from ..protocol import Compaction
from .eviction import evict_tool_results, pairing_intact
from .summary import compact_with_summary, resolve_compaction_target

DEFAULT_EVICT_THRESHOLD = 0.5
DEFAULT_SUMMARIZE_THRESHOLD = 0.85
DEFAULT_CONTEXT_WINDOW = 200_000


def default_context_window(model_entry: dict | None) -> int:
    """模型档案 contextWindow（缺失回退 200k——主流编码模型下限量级）。"""
    value = (model_entry or {}).get("contextWindow")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return DEFAULT_CONTEXT_WINDOW


class AgentLoopLike(Protocol):
    """结构契约（避免循环 import）：manager 只需要 loop.messages。"""

    messages: list


class CompactionManager:
    def __init__(
        self,
        *,
        emit: Callable[[Any], None],
        loop_supplier: Callable[[], AgentLoopLike | None],
        provider_supplier: Callable[[], tuple[Provider | None, str | None, dict | None, dict | None]],
        config_supplier: Callable[[], dict],
        settings_supplier: Callable[[], dict],
        provider_factory: Callable[[dict, str | None], Provider],
        api_key_env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        evict_threshold: float = DEFAULT_EVICT_THRESHOLD,
        summarize_threshold: float = DEFAULT_SUMMARIZE_THRESHOLD,
    ):
        self.emit = emit
        self.loop_supplier = loop_supplier
        self.provider_supplier = provider_supplier
        self.config_supplier = config_supplier
        self.settings_supplier = settings_supplier
        self.provider_factory = provider_factory
        self.api_key_env = api_key_env or {}
        self.cancel_event = cancel_event
        self.evict_threshold = evict_threshold
        self.summarize_threshold = summarize_threshold
        self._lock = threading.Lock()  # 回调在回合线程串行；加锁兜底手动 compact 并发
        self._last_input_tokens: int | None = None

    # ------------------------------------------------------------------ 入口

    def on_model_usage(self, usage: UsageUpdate) -> None:
        """AgentLoop 每个模型回合后调用（累计 usage，input_tokens 为口径）。"""
        if usage.input_tokens is None:
            return
        self._last_input_tokens = int(usage.input_tokens)
        loop = self.loop_supplier()
        if loop is None:
            return
        _provider, _model_id, _provider_entry, model_entry = self.provider_supplier()
        window = default_context_window(model_entry)
        if window <= 0:
            return
        ratio = self._last_input_tokens / window
        if ratio >= self.summarize_threshold:
            self.compact_now(
                loop, reason=f"auto: usage {ratio:.0%} >= {self.summarize_threshold:.0%}"
            )
        elif ratio >= self.evict_threshold:
            self._run_eviction(loop, reason=f"auto: usage {ratio:.0%} >= {self.evict_threshold:.0%}")

    def compact_now(self, loop: AgentLoopLike, *, reason: str = "manual") -> dict:
        """L3（control compact 手动 / 85% 自动）。返回报告 dict（测试断言用）。"""
        with self._lock:
            return self._summarize(loop, reason=reason)

    # ------------------------------------------------------------------ L2

    def _run_eviction(self, loop: AgentLoopLike, *, reason: str) -> dict | None:
        report = evict_tool_results(loop.messages)
        if report.evicted_results == 0:
            return None
        if not pairing_intact(loop.messages):
            # 理论不可达（只换 content）；自检失败立即广播，绝不带着坏上下文继续
            self.emit(
                Compaction(
                    layer="L2",
                    report={"error": "pairing_broken_after_eviction", "reason": reason},
                )
            )
            return report.to_dict()
        payload = {"reason": reason, **report.to_dict()}
        self.emit(Compaction(layer="L2", report=payload))
        return payload

    # ------------------------------------------------------------------ L3

    def _resolve_summary_provider(self) -> tuple[Provider, str, str, list]:
        """(压缩 provider, model id, providerId, notes)。专用模型回落主模型。"""
        provider, model_id, provider_entry, _model_entry = self.provider_supplier()
        notes: list[str] = []
        if provider is None or not model_id:
            raise _NoProvider()
        entry, comp_model_id, note = resolve_compaction_target(
            self.config_supplier(), provider_entry, self.settings_supplier()
        )
        if note and not note.startswith("(compaction model unset"):
            notes.append(note)
        if entry is None or not comp_model_id or (
            entry is provider_entry and comp_model_id == model_id
        ):
            return provider, model_id, (provider_entry or {}).get("id") or "", notes
        # 跨 provider / 同 provider 换模型：重建 provider 实例
        key = api_key_for(entry.get("id") or "", self.api_key_env)
        try:
            return self.provider_factory(entry, key), comp_model_id, entry.get("id") or "", notes
        except Exception as exc:  # noqa: BLE001 —— 构建失败回落主模型
            notes.append(f"compaction provider build failed ({exc}); using main model")
            return provider, model_id, (provider_entry or {}).get("id") or "", notes

    def _summarize(self, loop: AgentLoopLike, *, reason: str) -> dict:
        try:
            target_provider, comp_model_id, provider_id, notes = self._resolve_summary_provider()
        except _NoProvider:
            report = {"reason": reason, "error": "no_provider"}
            self.emit(Compaction(layer="L3", report=report))
            return report
        outcome = compact_with_summary(
            loop.messages,
            provider=target_provider,
            model_id=comp_model_id,
            provider_id=provider_id,
            cancel_event=self.cancel_event,
        )
        outcome.notes.extend(notes)
        if not outcome.ok:
            # 摘要失败降级：先做一次 L2（能省则省），报告携带失败原因
            eviction = evict_tool_results(loop.messages)
            report = {
                "reason": reason,
                "error": outcome.error,
                "fallback": "L2",
                "model": outcome.model,
                **eviction.to_dict(),
            }
            self.emit(Compaction(layer="L3", report=report))
            return report
        if not pairing_intact(outcome.new_messages):
            report = {
                "reason": reason,
                "error": "pairing_broken_after_rebuild",
                **outcome.report(),
            }
            self.emit(Compaction(layer="L3", report=report))
            return report
        loop.messages[:] = outcome.new_messages
        report = {"reason": reason, **outcome.report()}
        self.emit(Compaction(layer="L3", report=report))
        return report


class _NoProvider(Exception):
    """主模型缺席，无法压缩。"""


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_EVICT_THRESHOLD",
    "DEFAULT_SUMMARIZE_THRESHOLD",
    "CompactionManager",
    "default_context_window",
]
