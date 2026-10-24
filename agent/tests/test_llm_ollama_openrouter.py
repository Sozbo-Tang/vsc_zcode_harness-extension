"""ollama / openrouter 独立适配器测试（ROADMAP M1 未勾项）。

全部离线：事件映射直接喂官方 docs 记录的 NDJSON / chunk 形态；全链路走
httpx.MockTransport 假流式（真适配器 构造请求体 → HTTP → 分类），
无网可测。真实厂商联测无 Key，记入 ROADMAP deferred。
"""

from __future__ import annotations

import json

import httpx
import pytest

from zcode_py import telegraph
from zcode_py.llm import ollama as OL
from zcode_py.llm import openrouter as OR
from zcode_py.llm import default_provider_factory
from zcode_py.llm.base import (
    LLMRequest,
    ProviderError,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    LLMUserMessage,
    LLMAssistantMessage,
    ping,
)


# 哑 Key（逐段拼装，仅满足适配器「Key 非空」校验；不含任何凭据形态字面量）
_DUMMY_KEY = "".join(("d", "u", "m", "m", "y"))


# --------------------------------------------------------------------------- Ollama
# 事件形态来源：docs.ollama.com/api/chat 与 ollama.com/blog/streaming-tool
# （2026-10 核对：NDJSON 一行一 JSON；末块 done/done_reason + 计数）。


def test_ollama_chunk_mapping_thinking_tools_usage():
    chunks = [
        {"model": "qwen3", "created_at": "t0", "message": {"role": "assistant", "thinking": "先想"}, "done": False},
        {"model": "qwen3", "created_at": "t1", "message": {"role": "assistant", "content": "答"}, "done": False},
        {
            "model": "qwen3",
            "created_at": "t2",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_weather", "arguments": {"city": "Tokyo", "format": "celsius"}}}
                ],
            },
            "done": False,
        },
        {
            "model": "qwen3",
            "created_at": "t3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "tool_calls",
            "total_duration": 1,
            "prompt_eval_count": 12,
            "eval_count": 34,
        },
    ]
    events = list(OL.map_ollama_chunks(chunks))
    assert type(events[0]).__name__ == "ThinkingDelta" and events[0].text == "先想"
    assert type(events[1]).__name__ == "TextDelta" and events[1].text == "答"
    tool = events[2]
    assert tool.call_id == "oc_1_get_weather"  # 无 id 字段 → 合成
    assert tool.name == "get_weather"
    assert json.loads(tool.args_delta) == {"city": "Tokyo", "format": "celsius"}
    usage = [e for e in events if type(e).__name__ == "UsageUpdate"][0]
    assert (usage.input_tokens, usage.output_tokens) == (12, 34)
    assert events[-1].stop_reason == "tool_use"


def test_ollama_tool_calls_only_in_final_done_chunk():
    """部分模型只在 done 帧才给解析完成的 tool_calls（调研：new-api #7252）。"""
    chunks = [
        {"model": "m", "message": {"role": "assistant", "content": "看下天气"}, "done": False},
        {
            "model": "m",
            "message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read", "arguments": {"file": "a.py"}}}]},
            "done": True,
            "done_reason": "stop",  # done_reason 不可单信
            "eval_count": 5,
        },
    ]
    events = list(OL.map_ollama_chunks(chunks))
    tools = [e for e in events if type(e).__name__ == "ToolCallDelta"]
    assert tools and tools[0].name == "read"
    assert events[-1].stop_reason == "tool_use"  # 有工具调用即 tool_use 收尾


def test_ollama_done_reason_mapping():
    stop = list(OL.map_ollama_chunks([{"message": {"content": "x"}, "done": True, "done_reason": "stop"}]))
    assert stop[-1].stop_reason == "end_turn"
    length = list(OL.map_ollama_chunks([{"message": {}, "done": True, "done_reason": "length"}]))
    assert length[-1].stop_reason == "max_tokens"
    unload = list(OL.map_ollama_chunks([{"message": {}, "done": True, "done_reason": "unload"}]))
    assert unload[-1].stop_reason == "end_turn"
    unknown = list(OL.map_ollama_chunks([{"message": {}, "done": True, "done_reason": "weird"}]))
    assert unknown[-1].stop_reason == "end_turn"  # 未知 done_reason 归 end_turn


def test_ollama_midstream_error_chunk_classifies_model_not_found():
    with pytest.raises(ProviderError) as ei:
        list(OL.map_ollama_chunks([{"error": "model 'nope' not found"}]))
    assert ei.value.kind == "model"


def test_ollama_ndjson_iter_skips_blank_and_corrupt_lines():
    lines = [
        "",
        json.dumps({"message": {"content": "a"}, "done": False}),
        "not json",
        json.dumps({"message": {"content": "b"}, "done": True, "done_reason": "stop"}),
    ]
    events = list(OL.map_ollama_chunks(OL.iter_ndjson_json(lines)))
    texts = [e.text for e in events if type(e).__name__ == "TextDelta"]
    assert texts == ["a", "b"]


def test_ollama_body_encodes_tool_flow_and_images():
    messages = [
        LLMAssistantMessage([ToolUseBlock(id="oc_1_get_weather", name="get_weather", input={"city": "Tokyo"})]),
        LLMUserMessage([ToolResultBlock(tool_use_id="oc_1_get_weather", content="11 degrees")]),
    ]
    from zcode_py.llm.base import ImageBlock

    req = LLMRequest(
        model="qwen3",
        messages=messages,
        system="SYS",
        thinking={"think": True},
        max_tokens=512,
        tools=[{"type": "function", "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object"}}}],
    )
    body = OL.build_ollama_body(req)
    assert body["model"] == "qwen3"
    assert body["stream"] is True
    assert body["options"] == {"num_predict": 512}
    assert body["think"] is True
    assert body["tools"][0]["function"]["name"] == "get_weather"  # OpenAI function 风格直通
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    # assistant tool_calls 原生回放：arguments 为 JSON 对象（非字符串）
    assert msgs[1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Tokyo"}}}],
    }
    # tool 结果按函数名回传（tool_name，从历史 ToolUseBlock 解析）
    assert msgs[2] == {"role": "tool", "content": "11 degrees", "tool_name": "get_weather"}
    # 图片：base64 裸字符串数组（Ollama 原生形态，非 data URL）
    img_req = LLMRequest(
        model="llava",
        messages=[LLMUserMessage([TextBlock("这是什么"), ImageBlock(data="aGk=", media_type="image/png")])],
    )
    img_body = OL.build_ollama_body(img_req)
    assert img_body["messages"][0]["images"] == ["aGk="]
    assert "options" not in img_body  # 未给 max_tokens 不发 options


def test_ollama_body_without_thinking_sends_nothink():
    body = OL.build_ollama_body(LLMRequest(model="llama3.2", messages=[LLMUserMessage([TextBlock("hi")])]))
    assert "think" not in body  # 无思考型档案缺省不发（非思考模型会 400）


# ------------------------------------------------------------------------ OpenRouter


def test_openrouter_normalizes_model_entry_and_drops_pricing():
    entry = {
        "id": "moonshotai/kimi-k3.1-pro",
        "name": "MoonshotAI: Kimi K3.1 Pro",
        "context_length": 1048576,
        "architecture": {"input_modalities": ["text", "image", "video"]},
        "pricing": {"prompt": "0.000002", "completion": "0.00001"},  # 必须被忽略
        "supported_parameters": ["max_tokens", "reasoning", "tools"],
    }
    m = OR.normalize_openrouter_model(entry)
    assert m["id"] == "moonshotai/kimi-k3.1-pro"
    assert m["label"] == "MoonshotAI: Kimi K3.1 Pro"
    assert m["contextWindow"] == 1048576
    assert m["capabilities"] == {"vision": True, "tools": True, "thinking": True}
    assert "pricing" not in json.dumps(m)  # 定价字段彻底不落档案


def test_openrouter_normalize_skips_entry_without_id():
    assert OR.normalize_openrouter_model({"name": "no id"}) == {}


def test_openrouter_provider_requires_key():
    provider = OR.OpenRouterProvider("openrouter")  # 无 Key
    with pytest.raises(ProviderError) as ei:
        provider.stream(LLMRequest(model="a/b", messages=[]))
    assert ei.value.kind == "auth"
    assert "ZCODE_PY_API_KEY_OPENROUTER" in ei.value.message


def test_openrouter_defaults_to_site_base_url():
    provider = OR.OpenRouterProvider("openrouter", api_key=_DUMMY_KEY)
    assert provider.base_url == OR.DEFAULT_BASE_URL == "https://openrouter.ai/api/v1"


# ------------------------------------------------------------------- 注册表 / 档案


def test_factory_builds_ollama_and_openrouter():
    ollama = default_provider_factory(
        {"id": "local", "protocol": "ollama", "baseUrl": "http://127.0.0.1:11434"},
        None,  # 本地端点免 Key
    )
    assert isinstance(ollama, OL.OllamaProvider)
    assert ollama.protocol == "ollama"
    router = default_provider_factory(
        {"id": "or", "protocol": "openrouter", "httpReferer": "https://example.com", "appTitle": "Zcode-Py"},
        _DUMMY_KEY,
    )
    assert isinstance(router, OR.OpenRouterProvider)
    assert router.extra_headers == {"HTTP-Referer": "https://example.com", "X-Title": "Zcode-Py"}


def test_factory_still_rejects_unknown_protocol():
    with pytest.raises(ProviderError) as ei:
        default_provider_factory({"id": "x", "protocol": "nope"}, None)
    assert ei.value.kind == "config"
    assert "ollama | openrouter" in ei.value.message


def test_protocol_adapters_registry_contains_new_protocols():
    from zcode_py.llm import PROTOCOL_ADAPTERS

    assert PROTOCOL_ADAPTERS["ollama"] is OL.OllamaProvider
    assert PROTOCOL_ADAPTERS["openrouter"] is OR.OpenRouterProvider
    # 既有适配器不受影响（不动 anthropic/gemini/custom 的任务红线）
    assert PROTOCOL_ADAPTERS["anthropic"].protocol == "anthropic"
    assert PROTOCOL_ADAPTERS["openai"].protocol == "openai"
    assert PROTOCOL_ADAPTERS["gemini"].protocol == "gemini"


def test_telegraph_reference_profiles_are_none_mode():
    """无思考能力按无思考型档案处理：整体置灰，缺省 off。"""
    for proto in ("ollama", "openrouter"):
        ref = telegraph.reference_telegraph(proto)
        assert ref["mode"] == "none"
        profile = telegraph.parse_profile({"telegraph": ref})
        assert profile.mode == "none"
        assert all(not lv.ok for lv in profile.levels)
        assert telegraph.translate(profile, "high") is None  # 不发思考参数
        assert telegraph.downshift(profile, "high") == "off"


# ------------------------------------------------------- MockTransport 全链路假流式


def _install_fake_transport(monkeypatch, handler):
    """与 test_llm_mapping.py 同法：注入 httpx.MockTransport，无网可测。"""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _ollama_ndjson(chunks: list[dict]) -> str:
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks)


def test_ollama_full_path_stream_via_mock_transport(monkeypatch):
    """全链路：build body → POST /api/chat → NDJSON 解析 → 事件映射。

    本地端点（127.0.0.1）无需任何 Key；同时校验请求侧形状（url/body/头）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        ndjson = _ollama_ndjson(
            [
                {"message": {"role": "assistant", "thinking": "思考"}, "done": False},
                {"message": {"role": "assistant", "content": "答"}, "done": False},
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "read", "arguments": {"file": "a.py"}}}],
                    },
                    "done": False,
                },
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "tool_calls", "prompt_eval_count": 9, "eval_count": 7},
            ]
        )
        return httpx.Response(200, text=ndjson, request=request)

    _install_fake_transport(monkeypatch, handler)
    provider = OL.OllamaProvider("local-ollama", base_url="http://127.0.0.1:11434")
    events = list(
        provider.stream(
            LLMRequest(
                model="qwen3",
                messages=[LLMUserMessage([TextBlock("读 a.py")])],
                tools=[{"type": "function", "function": {"name": "read", "description": "d", "parameters": {"type": "object"}}}],
                max_tokens=256,
            )
        )
    )
    # 请求侧
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["auth"] is None  # 免 Key 端点不发 Authorization
    assert captured["body"]["model"] == "qwen3"
    assert captured["body"]["stream"] is True
    assert captured["body"]["options"] == {"num_predict": 256}
    assert captured["body"]["tools"][0]["function"]["name"] == "read"
    # 事件侧
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ThinkingDelta", "TextDelta", "ToolCallDelta", "UsageUpdate", "TurnEnd"]
    assert events[-1].stop_reason == "tool_use"


def test_ollama_full_path_http_error_classification(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model 'nope' not found, try pulling it first", request=request)

    _install_fake_transport(monkeypatch, handler)
    provider = OL.OllamaProvider("local-ollama")
    with pytest.raises(ProviderError) as ei:
        list(provider.stream(LLMRequest(model="nope", messages=[LLMUserMessage([TextBlock("hi")])])))
    assert ei.value.kind == "model"  # 404 → model


def test_ollama_ping_via_mock_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        ndjson = _ollama_ndjson(
            [
                {"message": {"role": "assistant", "content": "pong"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1},
            ]
        )
        return httpx.Response(200, text=ndjson, request=request)

    _install_fake_transport(monkeypatch, handler)
    outcome = ping(OL.OllamaProvider("local-ollama"), "qwen3")
    assert outcome["ok"] is True
    assert outcome["errorKind"] is None


def test_openrouter_full_path_stream_via_mock_transport(monkeypatch):
    """全链路：headers（Bearer + HTTP-Referer/X-Title）→ SSE（含站点心跳注释行
    与 usage chunk）→ 事件映射（复用 openai 路径）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["referer"] = request.headers.get("HTTP-Referer")
        captured["title"] = request.headers.get("X-Title")
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        sse = "\n".join(
            [
                ": OPENROUTER PROCESSING",
                'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}',
                'data: {"choices":[{"delta":{"content":"答"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":12}}',
                "data: [DONE]",
            ]
        )
        return httpx.Response(200, text=sse, request=request)

    _install_fake_transport(monkeypatch, handler)
    provider = OR.OpenRouterProvider(
        "openrouter",
        api_key=_DUMMY_KEY,
        http_referer="https://example.com",
        app_title="Zcode-Py",
    )
    events = list(
        provider.stream(
            LLMRequest(
                model="moonshotai/kimi-k3.1-pro",
                messages=[LLMUserMessage([TextBlock("hi")])],
                thinking={"reasoning": {"effort": "high"}},
            )
        )
    )
    # 请求侧：站点归因头 + Bearer + 思考取参直通
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["referer"] == "https://example.com"
    assert captured["title"] == "Zcode-Py"
    assert captured["auth"] == f"Bearer {_DUMMY_KEY}"
    assert captured["body"]["reasoning"] == {"effort": "high"}
    # 事件侧：心跳行被跳过、reasoning_content → ThinkingDelta、usage 收齐
    kinds = [type(e).__name__ for e in events]
    assert "ThinkingDelta" in kinds and "TextDelta" in kinds
    usage = [e for e in events if type(e).__name__ == "UsageUpdate"][0]
    assert (usage.input_tokens, usage.output_tokens) == (9, 12)
    assert events[-1].stop_reason == "end_turn"


def test_openrouter_fetch_models_via_mock_transport(monkeypatch):
    """模型列表端点：GET /models 公开免 Key；定价字段丢弃；条目规整。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        payload = {
            "data": [
                {
                    "id": "openai/gpt-5.2",
                    "name": "OpenAI: GPT-5.2",
                    "context_length": 400000,
                    "architecture": {"input_modalities": ["text", "image"]},
                    "pricing": {"prompt": "0.000001"},
                    "supported_parameters": ["max_tokens", "tools"],
                },
                {"id": "x/none"},
            ]
        }
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_get(handler, *a, **k))
    models = OR.fetch_models("https://openrouter.ai/api/v1", api_key=None)
    assert captured["method"] == "GET"
    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert captured["auth"] is None
    assert len(models) == 2
    m = models[0]
    assert m["id"] == "openai/gpt-5.2"
    assert m["capabilities"] == {"vision": True, "tools": True, "thinking": False}
    assert "pricing" not in json.dumps(models)
    assert models[1] == {
        "id": "x/none",
        "label": "x/none",
        "contextWindow": None,
        "capabilities": {"vision": False, "tools": False, "thinking": False},
    }


def _mock_get(handler, url, **kwargs):
    """httpx.get 的 MockTransport 替身（fetch_models 用同步 GET，不走
    httpx.Client，故单独注入）。"""
    request = httpx.Request("GET", url, headers=kwargs.get("headers") or {})
    return handler(request)


def test_openrouter_fetch_models_classifies_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid key", request=request)

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _mock_get(handler, *a, **k))
    with pytest.raises(ProviderError) as ei:
        OR.fetch_models(api_key=_DUMMY_KEY)
    assert ei.value.kind == "auth"


def test_openrouter_fetch_models_classifies_network_error(monkeypatch):
    def fake_get(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(ProviderError) as ei:
        OR.fetch_models()
    assert ei.value.kind == "network"


# --------------------------------------------- tool_use 块解析往返（loop 聚合语义）


def test_ollama_tool_call_roundtrip_through_loop_aggregation():
    """合成 call_id + args 整体序列化 → loop 的 ToolCallDelta 聚合能还原
    ToolUseBlock，且 encode_ollama_messages 能把它按原生形态回放。"""
    chunks = [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read", "arguments": {"file": "a.py"}}},
                    {"function": {"name": "grep", "arguments": {"pattern": "def "}}},
                ],
            },
            "done": False,
        },
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "tool_calls", "prompt_eval_count": 3, "eval_count": 4},
    ]
    deltas = [e for e in OL.map_ollama_chunks(chunks) if type(e).__name__ == "ToolCallDelta"]
    # loop.py 聚合语义的最小复刻（tool_aggs/tool_order 逻辑见 loop.py:289-324）
    aggs: dict = {}
    order: list[str] = []
    for d in deltas:
        agg = aggs.setdefault(d.call_id, {"name": None, "buf": []})
        if d.call_id not in order:
            order.append(d.call_id)
        if d.name:
            agg["name"] = d.name
        if d.args_delta:
            agg["buf"].append(d.args_delta)
    blocks = [
        ToolUseBlock(
            id=cid,
            name=aggs[cid]["name"] or cid,
            input=json.loads("".join(aggs[cid]["buf"])),
        )
        for cid in order
    ]
    assert [b.name for b in blocks] == ["read", "grep"]
    assert blocks[0].input == {"file": "a.py"}
    assert blocks[1].input == {"pattern": "def "}
    # 回放：assistant tool_calls 原生对象形态
    encoded = OL.encode_ollama_messages([LLMAssistantMessage(blocks)])
    assert encoded[0]["tool_calls"] == [
        {"function": {"name": "read", "arguments": {"file": "a.py"}}},
        {"function": {"name": "grep", "arguments": {"pattern": "def "}}},
    ]
    # tool 结果按名回传：id→name 映射要求 assistant 历史与结果同批编码
    # （_tool_name_map 从本次传入的消息建表，与 gemini 适配器同一约定）
    followup = OL.encode_ollama_messages(
        [
            LLMAssistantMessage(blocks),
            LLMUserMessage([ToolResultBlock(tool_use_id=blocks[0].id, content="内容")]),
        ]
    )
    assert followup[1] == {"role": "tool", "content": "内容", "tool_name": "read"}
