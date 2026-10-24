"""性能指标（PLAN §2.2 R19）：首 token 耗时 TTFT + token/s。

- 首 token 到达瞬间定格 TTFT（首个思考或正文增量都算首 token）；
- streaming 阶段每 sample_interval_s（默认 0.5s，PLAN §6.1）广播一次
  ``perf{phase:"streaming", ttftMs, tokPerSec}``；
- done 阶段广播 ``perf{phase:"done", lastTurn{ttftMs, avgTokPerSec}}``，
  供 UI 在任务结束后显示上一轮数值。

token 计数说明：流中没有逐增量 token 数，采样期用「字符数/4」估算
（estimate_tokens）；done 时若 usage 提供了精确 output_tokens 则用精确值
计算 avgTokPerSec。
"""

from __future__ import annotations

import time
from typing import Callable

from .protocol import Perf

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """粗估 token 数（字符数/4，向上取整，至少 1）。"""
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


class PerfTracker:
    """单回合采样器；每回合 turn_start() 复位，线程安全由调用方保证
    （loop 在回合内独占）。clock 可注入便于测试。"""

    def __init__(
        self,
        *,
        sample_interval_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._interval = sample_interval_s
        self._clock = clock
        self._turn_start: float | None = None
        self._first_token: float | None = None
        self._ttft_ms: float | None = None
        self._tokens: int = 0
        self._last_sample: float | None = None

    def turn_start(self) -> None:
        self._turn_start = self._clock()
        self._first_token = None
        self._ttft_ms = None
        self._tokens = 0
        self._last_sample = None

    @property
    def ttft_ms(self) -> float | None:
        return self._ttft_ms

    def on_token(self, text: str) -> None:
        """记录一个增量（思考或正文都计）。"""
        if self._turn_start is None:
            return
        now = self._clock()
        if self._first_token is None:
            self._first_token = now
            self._ttft_ms = (now - self._turn_start) * 1000.0
        self._tokens += estimate_tokens(text)

    def maybe_sample(self) -> Perf | None:
        """到采样周期才发 streaming 事件；首 token 后立即发一次（TTFT 定格）。"""
        if self._first_token is None:
            return None
        now = self._clock()
        if self._last_sample is not None and (now - self._last_sample) < self._interval:
            return None
        self._last_sample = now
        elapsed = now - self._first_token
        tok_per_sec = self._tokens / elapsed if elapsed > 0 else float(self._tokens)
        return Perf(
            phase="streaming",
            ttftMs=round(self._ttft_ms or 0.0, 1),
            tokPerSec=round(tok_per_sec, 1),
        )

    def turn_end(self, exact_output_tokens: int | None = None) -> Perf:
        """回合结束：done 阶段 + lastTurn（有精确 usage 则用精确值）。"""
        if self._first_token is not None:
            end = self._clock()
            elapsed = max(end - self._first_token, 1e-9)
            tokens = exact_output_tokens if exact_output_tokens is not None else self._tokens
            avg = tokens / elapsed
            last_turn = {"ttftMs": round(self._ttft_ms or 0.0, 1), "avgTokPerSec": round(avg, 1)}
            return Perf(phase="done", ttftMs=round(self._ttft_ms or 0.0, 1), lastTurn=last_turn)
        return Perf(phase="done", lastTurn={"ttftMs": None, "avgTokPerSec": None})
