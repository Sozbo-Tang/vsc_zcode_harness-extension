"""FakeProvider 本体（PLAN §8「M1 建立，开发命脉」）：脚本化编排假流式序列
（thinking/text/tool_use 块）、可注入延迟（Delay）与错误（异常实例）、
Wait 门控、回合耗尽重复、请求记录。"""

from __future__ import annotations

import threading
import time

import pytest

from zcode_py.llm.base import (
    LLMRequest,
    LLMUserMessage,
    ProviderError,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    TurnEnd,
)
from zcode_py.llm.fake import Delay, FakeProvider, Wait, text_turn


def _req(n: int = 0) -> LLMRequest:
    return LLMRequest(model="fake-model", messages=[LLMUserMessage([TextBlock(f"q{n}")])])


def test_scripted_blocks_stream_in_order():
    provider = FakeProvider(
        turns=[[
            ThinkingDelta("想"),
            TextDelta("答"),
            ToolCallDelta(call_id="t1", name="read", args_delta='{"f": 1}'),
            TurnEnd("tool_use"),
        ]]
    )
    events = list(provider.stream(_req()))
    assert [type(e).__name__ for e in events] == [
        "ThinkingDelta", "TextDelta", "ToolCallDelta", "TurnEnd",
    ]
    assert events[-1].stop_reason == "tool_use"


def test_turns_consumed_in_order_then_repeat_last():
    provider = FakeProvider(turns=[[TextDelta("一")], [TextDelta("二")]])
    texts = []
    for _ in range(4):
        stream = provider.stream(_req())
        texts.append([e.text for e in stream if isinstance(e, TextDelta)])
    assert texts[0] == ["一"]
    assert texts[1] == ["二"]
    assert texts[2] == ["二"]  # 耗尽后重复最后一回合
    assert texts[3] == ["二"]
    assert len(provider.requests) == 4  # 每次调用都被记录


def test_requests_recorded_with_payload():
    provider = FakeProvider(turns=[text_turn("hi")])
    req = LLMRequest(
        model="glm-5.3",
        messages=[LLMUserMessage([TextBlock("q")])],
        system="SYS",
        thinking={"thinking": {"type": "enabled"}},
        max_tokens=128,
    )
    list(provider.stream(req))
    assert provider.requests == [req]
    assert provider.requests[0].thinking == {"thinking": {"type": "enabled"}}
    assert provider.requests[0].max_tokens == 128


def test_delay_injection_blocks_between_deltas():
    provider = FakeProvider(turns=[[TextDelta("a"), Delay(0.08), TextDelta("b")]])
    started = time.monotonic()
    events = list(provider.stream(_req()))
    elapsed = time.monotonic() - started
    assert elapsed >= 0.08
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts[0] == "a" and texts[-1] == "b"
    assert texts.count("") >= 1  # 阻塞期间周期性 keepalive 空增量


def test_wait_released_by_event():
    gate = threading.Event()
    provider = FakeProvider(turns=[[Wait(gate, timeout=2.0), TextDelta("go")]])
    threading.Timer(0.05, gate.set).start()
    texts = [e.text for e in provider.stream(_req()) if isinstance(e, TextDelta) and e.text]
    assert texts == ["go"]


def test_wait_timeout_raises_network_error():
    gate = threading.Event()
    provider = FakeProvider(turns=[[Wait(gate, timeout=0.05)]])
    with pytest.raises(ProviderError) as ei:
        list(provider.stream(_req()))
    assert ei.value.kind == "network"


def test_error_injection_raises_scripted_exception():
    provider = FakeProvider(turns=[[TextDelta("part"), ProviderError("quota", "over limit")]])
    stream = provider.stream(_req())
    assert next(stream).text == "part"
    with pytest.raises(ProviderError) as ei:
        next(stream)
    assert ei.value.kind == "quota" and ei.value.message == "over limit"


def test_error_injection_supports_arbitrary_exceptions():
    provider = FakeProvider(turns=[[RuntimeError("boom")]])
    with pytest.raises(RuntimeError, match="boom"):
        list(provider.stream(_req()))


def test_turn_end_auto_appended_when_missing():
    provider = FakeProvider(turns=[[TextDelta("x")]])
    events = list(provider.stream(_req()))
    assert isinstance(events[-1], TurnEnd) and events[-1].stop_reason == "end_turn"


def test_empty_script_yields_bare_end_turn():
    provider = FakeProvider()
    assert list(provider.stream(_req())) == [TurnEnd("end_turn")]


def test_text_turn_helper():
    script = text_turn("答案", thinking="思考")
    assert [type(i).__name__ for i in script] == ["ThinkingDelta", "TextDelta"]
    assert script[0].text == "思考" and script[1].text == "答案"
