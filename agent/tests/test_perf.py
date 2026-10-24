"""perf：TTFT 定格 / 0.5s 采样门控 / done 阶段 lastTurn（PLAN §2.2 R19）。"""

from __future__ import annotations

import pytest

from zcode_py.llm.base import TextDelta, ThinkingDelta
from zcode_py.llm.fake import FakeProvider
from zcode_py.loop import AgentLoop
from zcode_py.perf import PerfTracker, estimate_tokens


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_estimate_tokens():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_ttft_fixed_at_first_token():
    clock = FakeClock()
    tracker = PerfTracker(clock=clock)
    tracker.turn_start()
    clock.advance(0.842)
    tracker.on_token("你好世界你好世界")  # 8 字符 → 2 token
    assert tracker.ttft_ms == pytest.approx(842.0)
    clock.advance(0.158)
    tracker.on_token("x" * 8)
    assert tracker.ttft_ms == pytest.approx(842.0)  # 定格


def test_sample_gated_by_interval():
    clock = FakeClock()
    tracker = PerfTracker(sample_interval_s=0.5, clock=clock)
    tracker.turn_start()
    assert tracker.maybe_sample() is None  # 尚无 token
    tracker.on_token("aaaa")
    first = tracker.maybe_sample()  # 首 token 后立即发一次
    assert first is not None and first.phase == "streaming"
    assert first.ttftMs == 0.0
    clock.advance(0.2)
    assert tracker.maybe_sample() is None  # 未到周期
    tracker.on_token("bbbb")  # 第二个 token
    clock.advance(0.4)
    second = tracker.maybe_sample()
    assert second is not None
    assert second.tokPerSec == pytest.approx(2 / 0.6, abs=0.1)  # 2 token / 0.6s


def test_turn_done_last_turn_exact_tokens():
    clock = FakeClock()
    tracker = PerfTracker(sample_interval_s=0.5, clock=clock)
    tracker.turn_start()
    clock.advance(0.5)  # TTFT
    tracker.on_token("a" * 4)  # 1 token（估算）
    clock.advance(0.5)  # 生成耗时
    done = tracker.turn_end(exact_output_tokens=10)  # usage 精确值优先
    assert done.phase == "done"
    # avg = 精确 token / 生成时长（首 token 起算 0.5s）
    assert done.lastTurn == {"ttftMs": pytest.approx(500.0), "avgTokPerSec": pytest.approx(20.0)}


def test_turn_done_without_tokens():
    tracker = PerfTracker(clock=FakeClock)
    tracker.turn_start()
    done = tracker.turn_end()
    assert done.phase == "done"
    assert done.lastTurn == {"ttftMs": None, "avgTokPerSec": None}


def test_turn_start_resets():
    clock = FakeClock()
    tracker = PerfTracker(clock=clock)
    tracker.turn_start()
    tracker.on_token("x")
    tracker.turn_start()
    assert tracker.ttft_ms is None
    assert tracker.maybe_sample() is None


def test_streaming_samples_time_monotonic():
    """耗时单调：同一回合内 TTFT 定格不变；由 tokPerSec 反推的已历时
    （tokens / tokPerSec）随单调时钟推进只增不减。"""
    clock = FakeClock()
    tracker = PerfTracker(sample_interval_s=0.5, clock=clock)
    tracker.turn_start()
    clock.advance(0.3)
    tracker.on_token("aaaa")  # t=0.3 首 token → TTFT 300ms
    samples = [tracker.maybe_sample()]  # 首 token 立即发一次
    clock.advance(0.5)
    tracker.on_token("bbbb")  # 第二个 token
    samples.append(tracker.maybe_sample())
    clock.advance(1.0)
    samples.append(tracker.maybe_sample())
    samples = [s for s in samples if s is not None]
    assert len(samples) == 3  # 事件存在性：三次采样全部发出
    assert {s.ttftMs for s in samples} == {300.0}  # 定格
    assert all(s.phase == "streaming" and s.tokPerSec > 0 for s in samples)
    derived_elapsed = [2 / s.tokPerSec for s in samples[1:]]  # tokens=2
    assert derived_elapsed == sorted(derived_elapsed)  # 单调不回退


def test_ttft_across_turns_not_leaked():
    """跨回合不泄漏：turn_start 复位后 TTFT 按新回合计时（不沿用旧值）。"""
    clock = FakeClock()
    tracker = PerfTracker(sample_interval_s=0.5, clock=clock)
    tracker.turn_start()
    clock.advance(0.2)
    tracker.on_token("a")
    first = tracker.ttft_ms
    tracker.turn_start()
    clock.advance(0.5)
    tracker.on_token("b")
    assert first == pytest.approx(200.0)
    assert tracker.ttft_ms == pytest.approx(500.0)


def test_loop_emits_streaming_then_done_perf_events():
    """perf 事件存在性（loop 集成）：回合内至少一个 streaming 事件、恰好一个
    done 收尾；TTFT 在 streaming 与 done 之间一致（同一定格值）。"""
    provider = FakeProvider(
        turns=[[ThinkingDelta("t"), TextDelta("a"), TextDelta("b")]]
    )
    emitted = []
    loop = AgentLoop(provider=provider, model="m", emit=emitted.append, perf=PerfTracker())
    loop.run_user_text("q")

    perfs = [m for m in emitted if type(m).__name__ == "Perf"]
    streaming = [p for p in perfs if p.phase == "streaming"]
    done = [p for p in perfs if p.phase == "done"]
    assert len(streaming) >= 1
    assert len(done) == 1
    assert streaming[0].ttftMs >= 0
    assert done[0].lastTurn["ttftMs"] == streaming[0].ttftMs  # 定格值一致
    assert done[0].lastTurn["avgTokPerSec"] >= 0
