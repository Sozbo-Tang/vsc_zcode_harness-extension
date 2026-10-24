"""AgentLoop：流式广播 / 多轮上下文 / 工具回灌接口 / 取消 / 有界收场。"""

from __future__ import annotations

import threading

import pytest

from zcode_py.llm.base import (
    ProviderError,
    SignatureUpdate,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolCallDelta,
    ToolResultBlock,
    TurnEnd,
    UsageUpdate,
    LLMUserMessage,
    LLMAssistantMessage,
)
from zcode_py.llm.fake import FakeProvider, Wait
from zcode_py.loop import AgentLoop, StubToolExecutor
from zcode_py.perf import PerfTracker


def make_loop(provider, **kwargs):
    emitted = []
    loop = AgentLoop(
        provider=provider,
        model="test-model",
        emit=emitted.append,
        perf=PerfTracker(),
        **kwargs,
    )
    return loop, emitted


def types_of(emitted):
    return [type(m).__name__ for m in emitted]


def test_streaming_broadcast_and_message_complete():
    provider = FakeProvider(
        turns=[[
            ThinkingDelta("想"),
            TextDelta("你"),
            TextDelta("好"),
            UsageUpdate(input_tokens=5, output_tokens=2),
            TurnEnd("end_turn"),
        ]]
    )
    loop, emitted = make_loop(provider)
    loop.run_user_text("hi")

    names = types_of(emitted)
    assert names[0] == "ThinkingDelta"
    # 首 token 到达瞬间先定格一次 perf（PLAN §6.1），随后是流式增量
    assert names[1] == "Perf"
    assert names.count("StreamDelta") == 2
    complete = next(m for m in emitted if type(m).__name__ == "MessageComplete")
    assert complete.stopReason == "end_turn"
    assert complete.text == "你好"
    assert complete.inputTokens == 5
    assert complete.outputTokens == 2
    done = next(m for m in emitted if type(m).__name__ == "Perf" and m.phase == "done")
    assert done.lastTurn["ttftMs"] is not None


def test_multi_turn_context_maintained():
    provider = FakeProvider(turns=[[TextDelta("r1")], [TextDelta("r2")]])
    loop, _ = make_loop(provider)
    loop.run_user_text("q1")
    loop.run_user_text("q2")

    assert len(provider.requests) == 2
    second = provider.requests[1].messages
    assert isinstance(second[0], LLMUserMessage)
    assert second[0].blocks == [TextBlock("q1")]
    assert isinstance(second[1], LLMAssistantMessage)
    assert second[1].blocks == [TextBlock("r1")]
    assert isinstance(second[2], LLMUserMessage)
    assert second[2].blocks == [TextBlock("q2")]


def test_thinking_signature_attached_for_replay():
    provider = FakeProvider(
        turns=[[
            ThinkingDelta("想"),
            SignatureUpdate("sig=="),
            TextDelta("答"),
            TurnEnd("end_turn"),
        ]]
    )
    loop, _ = make_loop(provider)
    loop.run_user_text("q")
    assistant = loop.messages[-1]
    thinking = assistant.blocks[0]
    assert isinstance(thinking, ThinkingBlock)
    assert thinking.text == "想"
    assert thinking.signature == "sig=="  # 多轮回传必须带（调研 §1.3）


def test_tool_use_aggregation_and_stub_executor_requote():
    provider = FakeProvider(
        turns=[
            [
                ToolCallDelta(call_id="toolu_1", name="read", args_delta=""),
                ToolCallDelta(call_id="toolu_1", args_delta='{"file": '),
                ToolCallDelta(call_id="toolu_1", args_delta='"a.py"}'),
                UsageUpdate(input_tokens=10, output_tokens=6),
                TurnEnd("tool_use"),
            ],
            [TextDelta("done without tool")],
        ]
    )
    loop, emitted = make_loop(provider)
    loop.run_user_text("读一下 a.py")

    tool_events = [m for m in emitted if type(m).__name__ == "ToolCall"]
    assert [t.state for t in tool_events] == ["started", "finished"]
    assert tool_events[0].args == {"file": "a.py"}
    assert tool_events[1].result == StubToolExecutor().run_tool("read", {}, "x")[0]
    assert tool_events[1].elapsedMs == 0

    # 第二次模型请求：assistant 带 tool_use，随后 user 带 tool_result（回灌链）
    second_request = provider.requests[1]
    assistant = second_request.messages[-2]
    assert assistant.blocks[-1].id == "toolu_1"
    tool_result_msg = second_request.messages[-1]
    assert isinstance(tool_result_msg.blocks[0], ToolResultBlock)
    assert tool_result_msg.blocks[0].tool_use_id == "toolu_1"

    complete = next(m for m in emitted if type(m).__name__ == "MessageComplete")
    assert complete.stopReason == "end_turn"
    assert complete.text == "done without tool"


def test_unparsable_tool_args_kept_raw():
    provider = FakeProvider(
        turns=[[
            ToolCallDelta(call_id="t1", name="bash", args_delta="not json {"),
            TurnEnd("tool_use"),
        ]]
    )
    loop, emitted = make_loop(provider, max_turns=1)
    loop.run_user_text("q")
    started = next(m for m in emitted if type(m).__name__ == "ToolCall")
    assert started.args == {"_unparsed": "not json {"}


def test_max_turns_bounded():
    provider = FakeProvider(
        turns=[[
            ToolCallDelta(call_id="t", name="loop_tool", args_delta="{}"),
            TurnEnd("tool_use"),
        ]]
    )
    loop, emitted = make_loop(provider, max_turns=2)
    loop.run_user_text("q")
    complete = next(m for m in emitted if type(m).__name__ == "MessageComplete")
    assert complete.stopReason == "max_turns"
    assert len(provider.requests) == 2  # 有界：恰好 max_turns 次模型调用


def test_cancel_before_turn():
    provider = FakeProvider(turns=[[TextDelta("x")]])
    cancel = threading.Event()
    loop, emitted = make_loop(provider, cancel_event=cancel)
    cancel.set()
    loop.run_user_text("q")
    complete = next(m for m in emitted if type(m).__name__ == "MessageComplete")
    assert complete.stopReason == "cancelled"
    assert provider.requests == []  # 未发起任何请求
    assert loop.messages == []  # 悬空 user 消息已回退（保持交替配对）


def test_cancel_mid_stream_skips_rest():
    gate = threading.Event()
    provider = FakeProvider(turns=[[TextDelta("a"), Wait(gate, timeout=3.0), TextDelta("b")]])
    cancel = threading.Event()
    loop, emitted = make_loop(provider, cancel_event=cancel)
    cancel.set()  # 首个事件后即取消
    loop.run_user_text("q")
    texts = [m.text for m in emitted if type(m).__name__ == "StreamDelta"]
    assert texts == []  # 取消后所有增量都被跳过
    complete = next(m for m in emitted if type(m).__name__ == "MessageComplete")
    assert complete.stopReason == "cancelled"
    gate.set()  # 释放等待（若生成器仍阻塞）


def test_empty_tool_call_args_defaults_to_empty_dict():
    provider = FakeProvider(
        turns=[[
            ToolCallDelta(call_id="t1", name="todo", args_delta=""),
            TurnEnd("tool_use"),
        ]]
    )
    loop, emitted = make_loop(provider)
    loop.run_user_text("q")
    started = next(m for m in emitted if type(m).__name__ == "ToolCall")
    assert started.args == {}


def test_provider_error_propagates_and_rolls_back_user_message():
    """Provider 出错：loop 不吞异常（由服务层广播 error）；悬空 user 消息
    回退，下一轮请求保持 user/assistant 交替（Anthropic 回放约束）。"""
    provider = FakeProvider(turns=[[TextDelta("x"), ProviderError("quota", "limit hit")]])
    loop, emitted = make_loop(provider)
    with pytest.raises(ProviderError) as ei:
        loop.run_user_text("q")
    assert ei.value.kind == "quota"
    assert loop.messages == []  # 悬空 user 已回退
    assert "StreamDelta" in types_of(emitted)  # 出错前的增量已广播
    assert "MessageComplete" not in types_of(emitted)  # 错误回合没有完成事件

    # 错误后可恢复：下一轮请求不带悬空重复的 user 消息
    provider.turns.append([TextDelta("恢复")])
    loop.run_user_text("q2")
    second = provider.requests[-1].messages
    assert [type(m).__name__ for m in second] == ["LLMUserMessage"]
    assert second[0].blocks == [TextBlock("q2")]
    # 回合完成后上下文恢复 user/assistant 交替
    assert [type(m).__name__ for m in loop.messages] == ["LLMUserMessage", "LLMAssistantMessage"]
