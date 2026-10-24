"""FakeProvider：pytest 脚本化假流式（docs/PLAN.md §8 —— M1 建立，开发命脉）。

用法：按「模型回合」给脚本，每个回合是内部事件（或 Wait / Delay / 异常实例）
列表；回合耗尽后重复最后一回合（便于多轮对话测试）。所有请求被记录在
``requests`` 供断言。

``Wait``：阻塞等待一个 threading.Event，阻塞期间每 20ms 产出空 keepalive
增量——让 AgentLoop 在两次事件之间有机会检查取消标志（模拟真实网络流的
高频到包节奏）。

``Delay``：真实阻塞指定秒数后继续（同样周期性产出 keepalive 空增量），
用于注入可测的时间延迟（perf 采样 / TTFT / 取消响应性）。

异常实例（ProviderError / RuntimeError / httpx.ConnectError …）：消费到该
位置即抛出，用于注入各类错误。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator

from .base import (
    InternalEvent,
    LLMRequest,
    Provider,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    TurnEnd,
)


class Wait:
    """脚本占位：阻塞直到 event 被 set（或超时抛 network 错）。"""

    def __init__(self, event: threading.Event, timeout: float = 5.0):
        self.event = event
        self.timeout = timeout


class Delay:
    """脚本占位：真实阻塞指定秒数（期间每 20ms 产出空 keepalive 增量，
    让 AgentLoop 有机会检查取消标志）。"""

    def __init__(self, seconds: float):
        self.seconds = seconds


def text_turn(text: str, *, thinking: str | None = None) -> list[Any]:
    """便捷构造：一个纯文本（可带思考）模型回合。"""
    events: list[Any] = []
    if thinking is not None:
        events.append(ThinkingDelta(thinking))
    events.append(TextDelta(text))
    return events


class FakeProvider(Provider):
    protocol = "fake"

    def __init__(self, turns: list[list[Any]] | None = None, provider_id: str = "fake"):
        super().__init__(provider_id)
        self.turns: list[list[Any]] = [list(t) for t in (turns or [])]
        self.turn_index = 0
        self.requests: list[LLMRequest] = []

    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        self.requests.append(request)
        if not self.turns:
            yield TurnEnd("end_turn")
            return
        idx = min(self.turn_index, len(self.turns) - 1)
        self.turn_index += 1
        saw_end = False
        for item in self.turns[idx]:
            if isinstance(item, Delay):
                deadline = time.monotonic() + item.seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.02, remaining))
                    yield TextDelta("")  # keepalive：给 loop 检查取消的机会
                continue
            if isinstance(item, Wait):
                deadline = time.monotonic() + item.timeout
                while not item.event.wait(0.02):
                    yield TextDelta("")  # keepalive：给 loop 检查取消的机会
                    if time.monotonic() >= deadline:
                        raise ProviderError("network", "fake wait timed out")
                continue
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, TurnEnd):
                saw_end = True
            yield item
        if not saw_end:
            yield TurnEnd("end_turn")


__all__ = ["FakeProvider", "Wait", "Delay", "text_turn"]
