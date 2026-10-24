"""llm 适配器：SSE 解析 / 三厂商事件映射与请求体构造 / 错误分类 / ping。

全部离线：直接喂调研笔记记录的事件 dict 形态（docs/research/llm-apis.md）。
"""

from __future__ import annotations

import httpx
import pytest

from zcode_py.llm import anthropic as A
from zcode_py.llm import gemini as G
from zcode_py.llm import openai as O
from zcode_py.llm.base import (
    LLMRequest,
    ProviderError,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    LLMUserMessage,
    LLMAssistantMessage,
    classify_exception,
    classify_http_error,
    ping,
)
from zcode_py.llm.fake import FakeProvider
from zcode_py.llm.sse import iter_sse_json


# --------------------------------------------------------------------------- SSE


def test_sse_skips_comments_and_done():
    lines = [
        ": OPENROUTER PROCESSING",
        'data: {"choices":[{"delta":{"content":"a"}}]}',
        "",
        "event: message",
        'data: {"choices":[{"delta":{"content":"b"}}]}',
        "data: [DONE]",
        'data: {"never":true}',
    ]
    objs = list(iter_sse_json(lines))
    assert len(objs) == 2
    assert objs[0]["choices"][0]["delta"]["content"] == "a"


def test_sse_skips_malformed_json():
    assert list(iter_sse_json(["data: not json", 'data: {"ok":1}'])) == [{"ok": 1}]


# ----------------------------------------------------------------------- Anthropic


def _anthropic_stream_events() -> list[dict]:
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先想"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "答"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"file": '}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"a.py"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "ping"},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"},
    ]


def test_anthropic_event_mapping():
    events = list(A.map_anthropic_events(_anthropic_stream_events()))
    kinds = [(type(e).__name__, getattr(e, "text", None) or getattr(e, "args_delta", None) or getattr(e, "signature", None)) for e in events]
    assert ("ThinkingDelta", "先想") in kinds
    assert ("SignatureUpdate", "sig==") in kinds
    assert ("TextDelta", "答") in kinds
    tool_deltas = [e for e in events if type(e).__name__ == "ToolCallDelta"]
    assert tool_deltas[0].call_id == "toolu_1" and tool_deltas[0].name == "read"
    assert "".join(e.args_delta for e in tool_deltas) == '{"file": "a.py"}'
    usages = [e for e in events if type(e).__name__ == "UsageUpdate"]
    assert usages[0].input_tokens == 25
    assert usages[-1].output_tokens == 40  # 累计覆盖语义
    assert events[-1].stop_reason == "tool_use"


def test_anthropic_stream_error_event_raises():
    stream = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    ]
    with pytest.raises(ProviderError) as ei:
        list(A.map_anthropic_events(stream))
    assert ei.value.kind == "network"


def test_anthropic_body_thinking_clamp():
    req = LLMRequest(
        model="claude-sonnet-4-5",
        messages=[LLMUserMessage([TextBlock("hi")])],
        thinking={"thinking": {"type": "enabled", "budget_tokens": 65536}},
        max_tokens=4096,
    )
    body = A.build_anthropic_body(req)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4095}  # budget < max_tokens


def test_anthropic_body_drops_tiny_budget():
    req = LLMRequest(
        model="m",
        messages=[LLMUserMessage([TextBlock("hi")])],
        thinking={"thinking": {"type": "enabled", "budget_tokens": 512}},  # < 1024
        max_tokens=4096,
    )
    assert "thinking" not in A.build_anthropic_body(req)


def test_anthropic_body_disabled_and_default_max_tokens():
    req = LLMRequest(
        model="m",
        messages=[LLMUserMessage([TextBlock("hi")])],
        thinking={"thinking": {"type": "disabled"}},
    )
    body = A.build_anthropic_body(req)
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 4096
    assert body["stream"] is True


def test_anthropic_message_replay_preserves_signature_and_redacted():
    messages = [
        LLMAssistantMessage(
            [
                ThinkingBlock(text="想", signature="sig=="),
                RedactedThinkingBlock(data="enc"),
                ToolUseBlock(id="t1", name="read", input={"file": "a"}),
            ]
        ),
        LLMUserMessage([ToolResultBlock(tool_use_id="t1", content="内容", is_error=False)]),
    ]
    encoded = A.encode_anthropic_messages(messages)
    assert encoded[0]["content"][0] == {"type": "thinking", "thinking": "想", "signature": "sig=="}
    assert encoded[0]["content"][1] == {"type": "redacted_thinking", "data": "enc"}
    assert encoded[0]["content"][2] == {"type": "tool_use", "id": "t1", "name": "read", "input": {"file": "a"}}
    assert encoded[1]["content"] == [{"type": "tool_result", "tool_use_id": "t1", "content": "内容"}]


def test_anthropic_tool_result_dict_becomes_json_text():
    encoded = A.encode_anthropic_messages(
        [LLMUserMessage([ToolResultBlock(tool_use_id="t", content={"ok": 1})])]
    )
    block = encoded[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"][0]["text"] == '{"ok": 1}'


# -------------------------------------------------------------------------- OpenAI


def test_openai_chunk_mapping_with_reasoning_and_tools():
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "思考"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "答"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read", "arguments": ""}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"file":"a.py"}'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        # OpenRouter 偏离 spec 的 usage chunk：choices 非空 + 重复 finish_reason
        {"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 9, "completion_tokens": 12}},
        "not-a-dict",
    ]
    events = list(O.map_openai_chunks(chunks))
    deltas = [(type(e).__name__, e) for e in events]
    assert ("ThinkingDelta", events[0]) and events[0].text == "思考"
    assert ("TextDelta", events[1]) and events[1].text == "答"
    tools = [e for _, e in deltas if _ == "ToolCallDelta"]
    assert tools[0].call_id == "call_1" and tools[0].name == "read"
    assert tools[1].args_delta == '{"file":"a.py"}'
    usage = [e for e in events if type(e).__name__ == "UsageUpdate"]
    assert usage[0].input_tokens == 9 and usage[0].output_tokens == 12
    assert events[-1].stop_reason == "tool_use"


def test_openai_midstream_error_chunk_raises():
    chunks = [
        {"error": {"message": "Provider returned error", "metadata": {"error_type": "provider_unavailable"}}}
    ]
    with pytest.raises(ProviderError) as ei:
        list(O.map_openai_chunks(chunks))
    assert ei.value.kind == "network"


def test_openai_body_merges_thinking_and_encodes_tool_flow():
    messages = [
        LLMAssistantMessage([ToolUseBlock(id="call_1", name="read", input={"file": "a.py"})]),
        LLMUserMessage([ToolResultBlock(tool_use_id="call_1", content={"lines": 3})]),
    ]
    req = LLMRequest(
        model="glm-5.3",
        messages=messages,
        system="SYS",
        thinking={"thinking": {"type": "enabled"}},
        max_tokens=1024,
        tools=[{"type": "function", "function": {"name": "read", "description": "d", "parameters": {"type": "object"}}}],
    )
    body = O.build_openai_body(req)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["thinking"] == {"type": "enabled"}
    assert body["max_tokens"] == 1024
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "read"
    assert body["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": '{"lines": 3}'}
    assert body["tools"][0]["function"]["name"] == "read"


def test_openai_reasoning_effort_passthrough():
    body = O.build_openai_body(
        LLMRequest(model="gpt-5.2", messages=[], thinking={"reasoning_effort": "high"})
    )
    assert body["reasoning_effort"] == "high"
    assert "max_tokens" not in body  # 未给 max_output 则不发


# -------------------------------------------------------------------------- Gemini


def test_gemini_mapping_thoughts_and_function_call():
    responses = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "想", "thought": True},
                            {"text": "答"},
                            {"functionCall": {"name": "read", "args": {"file": "a.py"}}},
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7, "thoughtsTokenCount": 3},
        },
        {"candidates": [{"finishReason": "STOP"}]},
    ]
    events = list(G.map_gemini_responses(responses))
    assert events[0].text == "想" and type(events[0]).__name__ == "ThinkingDelta"
    assert events[1].text == "答" and type(events[1]).__name__ == "TextDelta"
    fc = events[2]
    assert fc.name == "read"
    assert fc.args_delta == '{"file": "a.py"}'
    assert events[-1].stop_reason == "tool_use"  # 有 functionCall 即 tool_use
    usage = [e for e in events if type(e).__name__ == "UsageUpdate"][0]
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (5, 7, 3)


def test_gemini_body_and_function_response_name_resolution():
    messages = [
        LLMUserMessage([TextBlock("hi")]),
        LLMAssistantMessage([ToolUseBlock(id="fc_1_read", name="read", input={"file": "a.py"})]),
        LLMUserMessage([ToolResultBlock(tool_use_id="fc_1_read", content="content of a")]),
    ]
    req = LLMRequest(
        model="gemini-2.5-flash",
        messages=messages,
        system="SYS",
        thinking={"thinkingConfig": {"thinkingBudget": 2048, "includeThoughts": True}},
        max_tokens=8192,
    )
    body = G.build_gemini_body(req)
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["generationConfig"] == {
        "thinkingConfig": {"thinkingBudget": 2048, "includeThoughts": True},
        "maxOutputTokens": 8192,
    }
    contents = body["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "hi"}]}
    assert contents[1] == {"role": "model", "parts": [{"functionCall": {"name": "read", "args": {"file": "a.py"}}}]}
    # functionResponse 用函数名（从历史 ToolUseBlock 解析），不是 call_id
    assert contents[2]["parts"][0]["functionResponse"] == {
        "name": "read",
        "response": {"result": "content of a"},
    }


def test_gemini_finish_max_tokens():
    events = list(G.map_gemini_responses([{"candidates": [{"finishReason": "MAX_TOKENS"}]}]))
    assert events[-1].stop_reason == "max_tokens"


# --------------------------------------------------------------------- 错误分类 / ping


def test_classify_http_error():
    assert classify_http_error(401) == "auth"
    assert classify_http_error(403, "denied") == "auth"
    assert classify_http_error(404, "no model") == "model"
    assert classify_http_error(429) == "quota"
    assert classify_http_error(500) == "network"
    assert classify_http_error(400, "thinking disabled not allowed for this model") == "model"
    assert classify_http_error(400, "invalid api key") == "auth"
    assert classify_http_error(418) == "unknown"


def test_classify_exception():
    assert classify_exception(httpx.ConnectError("refused")) == "network"
    assert classify_exception(httpx.ReadTimeout("t")) == "network"
    assert classify_exception(ValueError("x")) == "unknown"
    assert classify_exception(ProviderError("quota", "q")) == "quota"


def test_ping_success_uses_tiny_request():
    fake = FakeProvider(turns=[[TextBlock("pong")]])
    outcome = ping(fake, "glm-5.3")
    assert outcome["ok"] is True
    assert outcome["latencyMs"] >= 0
    assert outcome["errorKind"] is None
    sent = fake.requests[0]
    assert sent.max_tokens == 16
    assert sent.thinking is None  # ping 不带思考参数（GLM 强制思考模型 omit 即可）


def test_ping_classifies_provider_error():
    fake = FakeProvider(turns=[[ProviderError("auth", "bad key")]])
    outcome = ping(fake, "glm-5.3")
    assert outcome == {"ok": False, "latencyMs": outcome["latencyMs"], "errorKind": "auth", "message": "bad key"}


def test_ping_classifies_unexpected_exception():
    fake = FakeProvider(turns=[[RuntimeError("boom")]])
    outcome = ping(fake, "m")
    assert outcome["ok"] is False
    assert outcome["errorKind"] == "unknown"
    assert "RuntimeError" in outcome["message"]


@pytest.mark.parametrize(
    "kind", ["auth", "network", "model", "quota", "unknown", "config"]
)
def test_ping_classifies_every_provider_error_kind(kind):
    """ProviderError.kind 全枚举原样上报（ping 错误分类的主路径）。"""
    fake = FakeProvider(turns=[[ProviderError(kind, f"{kind} failure")]])
    outcome = ping(fake, "m")
    assert outcome["ok"] is False
    assert outcome["errorKind"] == kind
    assert outcome["message"] == f"{kind} failure"


def _install_fake_transport(monkeypatch, handler):
    """假 transport：把 base._sse_post 里的 httpx.Client 换成注入
    httpx.MockTransport 的工厂——真适配器全链路（构造请求体 → HTTP →
    分类）无网可测。"""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


# 哑 Key（逐段拼装，仅满足适配器「Key 非空」校验；不含任何凭据形态字面量，
# PLAN §9.2：测试中不得出现可用凭据字面量）
_DUMMY_KEY = "".join(("d", "u", "m", "m", "y"))


def _transport_provider() -> A.AnthropicProvider:
    return A.AnthropicProvider("fake-anthropic", api_key=_DUMMY_KEY)


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, '{"error":{"type":"authentication_error"}}', "auth"),
        (403, "forbidden", "auth"),
        (404, "model not found", "model"),
        (429, "rate limited", "quota"),
        (500, "server error", "network"),
        (400, "thinking disabled not allowed for this model", "model"),
        (400, "invalid api key", "auth"),
        (418, "teapot", "unknown"),
    ],
)
def test_ping_http_error_classification_via_fake_transport(
    monkeypatch, status, body, expected
):
    def handler(request):
        return httpx.Response(status, text=body, request=request)

    _install_fake_transport(monkeypatch, handler)
    outcome = ping(_transport_provider(), "claude-sonnet-4-5")
    assert outcome["ok"] is False
    assert outcome["errorKind"] == expected
    assert outcome["latencyMs"] is not None
    assert str(status) in outcome["message"]


def test_ping_connect_error_via_fake_transport(monkeypatch):
    """传输层连接失败（非 HTTP 状态）→ network 分类。"""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _install_fake_transport(monkeypatch, handler)
    outcome = ping(_transport_provider(), "m")
    assert outcome["ok"] is False
    assert outcome["errorKind"] == "network"


def test_ping_success_via_fake_transport_sse(monkeypatch):
    """假 transport 也可发正常 SSE：首个事件到达即 ok。"""

    def handler(request):
        sse = "\n".join(
            [
                'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"pong"}}',
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
                'data: {"type":"message_stop"}',
            ]
        )
        return httpx.Response(200, text=sse, request=request)

    _install_fake_transport(monkeypatch, handler)
    outcome = ping(_transport_provider(), "claude-sonnet-4-5")
    assert outcome["ok"] is True
    assert outcome["latencyMs"] >= 0
    assert outcome["errorKind"] is None
