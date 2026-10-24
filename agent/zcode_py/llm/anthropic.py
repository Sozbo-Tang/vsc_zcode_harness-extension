"""Anthropic Messages 协议适配器（SSE，调研笔记 docs/research/llm-apis.md §1）。

事件序列 message_start → (content_block_start → content_block_delta×N →
content_block_stop)×块 → message_delta（累计 usage + stop_reason）→ message_stop，
可穿插 ping / error。

thinking 参数（telegraph 解析后形如 ``{"thinking": {"type": "enabled",
"budget_tokens": N}}``）：
- budget_tokens 必须 ≥1024 且 < max_tokens（调研 §1.3），这里做防御性钳制：
  ``min(budget, max_tokens - 1)``；若钳制后 <1024（max_tokens 太小）则干脆
  不发送 thinking 块（用模型默认）。
- thinking 块与 redacted_thinking 块回传时必须连同 signature/data 原样带回，
  encode 侧已保证（见 encode_anthropic_messages）。

GLM 的 Anthropic 兼容端点（base_url https://open.bigmodel.cn/api/anthropic，
x-api-key 认证）零改动复用本适配器（调研 §3.1）。
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator

from ..config import env_var_for
from .base import (
    InternalEvent,
    LLMRequest,
    Provider,
    ProviderError,
    RedactedThinkingBlock,
    STOP_CONTENT_FILTER,
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_TOOL_USE,
    SignatureUpdate,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolCallDelta,
    ToolResultBlock,
    ToolUseBlock,
    LLMUserMessage,
    LLMAssistantMessage,
    TurnEnd,
    UsageUpdate,
    classify_http_error,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MIN_THINKING_BUDGET = 1024

_STOP_REASON_MAP = {
    "end_turn": STOP_END_TURN,
    "stop_sequence": STOP_END_TURN,
    "tool_use": STOP_TOOL_USE,
    "max_tokens": STOP_MAX_TOKENS,
    "refusal": STOP_CONTENT_FILTER,
}

_ERROR_KIND_MAP = {
    "authentication_error": "auth",
    "permission_error": "auth",
    "rate_limit_error": "quota",
    "not_found_error": "model",
    "overloaded_error": "network",
    "api_error": "unknown",
    "invalid_request_error": "unknown",  # message 里含 model 再细分
}


# ---------------------------------------------------------------------------
# 请求体构造（纯函数，供单测）
# ---------------------------------------------------------------------------


def convert_tool(tool: dict) -> dict:
    """内部 OpenAI function 风格 → anthropic tools 条目。"""
    if "function" not in tool and "name" in tool:
        return tool  # 已是 anthropic 形态
    fn = tool.get("function") or {}
    return {
        "name": fn.get("name") or tool.get("name") or "",
        "description": fn.get("description") or "",
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def encode_anthropic_messages(messages: Iterable) -> list[dict]:
    out = []
    for msg in messages:
        if isinstance(msg, LLMUserMessage):
            content = []
            for blk in msg.blocks:
                if isinstance(blk, TextBlock):
                    content.append({"type": "text", "text": blk.text})
                elif isinstance(blk, ToolResultBlock):
                    if isinstance(blk.content, str):
                        result_content = blk.content
                    else:
                        result_content = [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    blk.content, ensure_ascii=False, default=str
                                ),
                            }
                        ]
                    entry = {
                        "type": "tool_result",
                        "tool_use_id": blk.tool_use_id,
                        "content": result_content,
                    }
                    if blk.is_error:
                        entry["is_error"] = True
                    content.append(entry)
            if content:
                out.append({"role": "user", "content": content})
        elif isinstance(msg, LLMAssistantMessage):
            content = []
            for blk in msg.blocks:
                if isinstance(blk, TextBlock):
                    content.append({"type": "text", "text": blk.text})
                elif isinstance(blk, ThinkingBlock):
                    content.append(
                        {
                            "type": "thinking",
                            "thinking": blk.text,
                            "signature": blk.signature,
                        }
                    )
                elif isinstance(blk, RedactedThinkingBlock):
                    content.append({"type": "redacted_thinking", "data": blk.data})
                elif isinstance(blk, ToolUseBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": blk.id,
                            "name": blk.name,
                            "input": blk.input,
                        }
                    )
            if content:
                out.append({"role": "assistant", "content": content})
    return out


def build_anthropic_body(request: LLMRequest) -> dict:
    max_tokens = request.max_tokens if request.max_tokens and request.max_tokens > 0 else 4096
    body: dict = {
        "model": request.model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": encode_anthropic_messages(request.messages),
    }
    if request.system:
        body["system"] = request.system
    if request.tools:
        body["tools"] = [convert_tool(t) for t in request.tools]
    if isinstance(request.thinking, dict):
        th = request.thinking.get("thinking")
        if isinstance(th, dict):
            if th.get("type") == "enabled":
                try:
                    budget = int(th.get("budget_tokens", 0) or 0)
                except (TypeError, ValueError):
                    budget = 0
                clamped = min(budget, max_tokens - 1)
                if clamped >= MIN_THINKING_BUDGET:
                    body["thinking"] = {"type": "enabled", "budget_tokens": clamped}
                # 钳制后不足 1024：不发 thinking（预算型思考与极小 max_tokens 不兼容）
            elif th.get("type") == "disabled":
                body["thinking"] = {"type": "disabled"}
    return body


# ---------------------------------------------------------------------------
# 流事件映射（纯生成器，供单测直接喂 dict 序列）
# ---------------------------------------------------------------------------


def _anthropic_error_kind(error: dict) -> str:
    etype = error.get("type") or ""
    kind = _ERROR_KIND_MAP.get(etype, "unknown")
    if kind == "unknown" and "model" in str(error.get("message", "")).lower():
        return "model"
    return kind


def map_anthropic_events(raw: Iterable[dict]) -> Iterator[InternalEvent]:
    tool_index: dict[int, tuple[str, str]] = {}
    stop_reason: str | None = None
    saw_tool_use = False
    emitted_end = False

    for obj in raw:
        etype = obj.get("type")
        if etype == "error":
            error = obj.get("error") or {}
            raise ProviderError(
                _anthropic_error_kind(error),
                f"stream error: {json.dumps(error, ensure_ascii=False, default=str)[:300]}",
            )
        if etype == "message_start":
            usage = (obj.get("message") or {}).get("usage") or {}
            yield UsageUpdate(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_input_tokens"),
                cache_write_tokens=usage.get("cache_creation_input_tokens"),
            )
        elif etype == "content_block_start":
            index = obj.get("index", 0)
            block = obj.get("content_block") or {}
            if block.get("type") == "tool_use":
                call_id = block.get("id") or f"toolu_{index}"
                name = block.get("name") or ""
                tool_index[index] = (call_id, name)
                saw_tool_use = True
                yield ToolCallDelta(call_id=call_id, name=name, args_delta="")
            # text/thinking/redacted_thinking 的起始块无增量载荷，忽略
        elif etype == "content_block_delta":
            index = obj.get("index", 0)
            delta = obj.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                if delta.get("text"):
                    yield TextDelta(delta["text"])
            elif dtype == "thinking_delta":
                if delta.get("thinking"):
                    yield ThinkingDelta(delta["thinking"])
            elif dtype == "signature_delta":
                if delta.get("signature"):
                    yield SignatureUpdate(delta["signature"])
            elif dtype == "input_json_delta":
                if index in tool_index and delta.get("partial_json"):
                    yield ToolCallDelta(
                        call_id=tool_index[index][0],
                        args_delta=delta["partial_json"],
                    )
        elif etype == "message_delta":
            inner = obj.get("delta") or {}
            if inner.get("stop_reason"):
                stop_reason = inner["stop_reason"]
            usage = obj.get("usage") or {}
            if usage:
                yield UsageUpdate(
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_tokens=usage.get("cache_read_input_tokens"),
                    cache_write_tokens=usage.get("cache_creation_input_tokens"),
                )
        elif etype == "message_stop":
            emitted_end = True
            yield TurnEnd(
                _STOP_REASON_MAP.get(stop_reason, STOP_END_TURN)
                if stop_reason
                else (STOP_TOOL_USE if saw_tool_use else STOP_END_TURN)
            )
            return
        # ping / content_block_stop：忽略

    if not emitted_end:
        # 流异常耗尽（无 message_stop）：按已知信息收尾
        yield TurnEnd(
            _STOP_REASON_MAP.get(stop_reason, STOP_END_TURN)
            if stop_reason
            else (STOP_TOOL_USE if saw_tool_use else STOP_END_TURN)
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AnthropicProvider(Provider):
    """api.anthropic.com 及一切 Anthropic 兼容端点（GLM 等）。"""

    protocol = "anthropic"

    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        if not self.api_key:
            raise ProviderError(
                "auth",
                f"missing API key for provider {self.provider_id!r}: "
                f"set environment variable {env_var_for(self.provider_id)}",
            )
        body = build_anthropic_body(request)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        url = (self.base_url or DEFAULT_BASE_URL).rstrip("/") + "/v1/messages"
        raw = self._sse_post(url, headers=headers, body=body)
        # 非流式错误（连接失败等）在首个 next() 时抛出
        yield from map_anthropic_events(raw)
