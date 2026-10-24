"""Google Gemini generateContent REST 适配器（调研笔记 §6）。

端点：
- 流式 ``POST {base}/v1beta/models/{model}:streamGenerateContent?alt=sse``
- 认证 ``x-goog-api-key`` 头；REST 字段 camelCase。

思考参数：thinking 载荷形如
``{"thinkingConfig": {"thinkingBudget": N, "includeThoughts": true}}``（0=禁用、
-1=动态；Gemini 3/4 改用 ``thinkingLevel``，档案数据驱动即可，本适配器只做
直通合并进 generationConfig）。思考 part 为 ``{"text": ..., "thought": true}``。

工具调用：functionCall part 整块下发（非增量），API 不提供稳定 id —— 这里合成
``fc_<序号>_<name>`` 作为 call_id，回传用 functionResponse（按函数名匹配，从
历史 assistant ToolUseBlock 建 id→name 映射）。
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator

from ..config import env_var_for
from .base import (
    ImageBlock,
    InternalEvent,
    LLMRequest,
    Provider,
    ProviderError,
    STOP_CONTENT_FILTER,
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_TOOL_USE,
    TextBlock,
    TextDelta,
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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

_FINISH_MAP = {
    "STOP": STOP_END_TURN,
    "MAX_TOKENS": STOP_MAX_TOKENS,
    "SAFETY": STOP_CONTENT_FILTER,
    "PROHIBITED_CONTENT": STOP_CONTENT_FILTER,
    "BLOCKLIST": STOP_CONTENT_FILTER,
    "RECITATION": STOP_CONTENT_FILTER,
}


# ---------------------------------------------------------------------------
# 请求体构造（纯函数，供单测）
# ---------------------------------------------------------------------------


def convert_tool_gemini(tool: dict) -> dict:
    if "function" not in tool and "name" in tool:
        parameters = tool.get("parameters") or tool.get("input_schema")
        entry = {"name": tool.get("name") or ""}
        if tool.get("description"):
            entry["description"] = tool["description"]
        if parameters is not None:
            entry["parameters"] = parameters
        return entry
    fn = tool.get("function") or {}
    entry = {"name": fn.get("name") or ""}
    if fn.get("description"):
        entry["description"] = fn["description"]
    if fn.get("parameters") is not None:
        entry["parameters"] = fn["parameters"]
    return entry


def _tool_name_map(messages: Iterable) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, LLMAssistantMessage):
            for blk in msg.blocks:
                if isinstance(blk, ToolUseBlock):
                    mapping[blk.id] = blk.name
    return mapping


def encode_gemini_contents(messages: Iterable) -> list[dict]:
    name_map = _tool_name_map(messages)
    contents: list[dict] = []
    for msg in messages:
        if isinstance(msg, LLMUserMessage):
            parts = []
            for blk in msg.blocks:
                if isinstance(blk, TextBlock):
                    parts.append({"text": blk.text})
                elif isinstance(blk, ToolResultBlock):
                    content = blk.content
                    if isinstance(content, dict):
                        response = content
                    else:
                        response = {"result": _as_text(content)}
                    parts.append(
                        {
                            "functionResponse": {
                                "name": name_map.get(blk.tool_use_id, blk.tool_use_id),
                                "response": response,
                            }
                        }
                    )
                elif isinstance(blk, ImageBlock):
                    # M5 视觉：base64 → inlineData（REST camelCase，调研 §6）
                    if blk.source_type == "base64":
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": blk.media_type,
                                    "data": blk.data,
                                }
                            }
                        )
                    else:
                        # URL/file API 引用形态：当前生成路径未用，fileData 直通
                        parts.append(
                            {
                                "fileData": {
                                    "mimeType": blk.media_type,
                                    "fileUri": blk.data,
                                }
                            }
                        )
            if parts:
                contents.append({"role": "user", "parts": parts})
        elif isinstance(msg, LLMAssistantMessage):
            parts = []
            for blk in msg.blocks:
                if isinstance(blk, TextBlock) and blk.text:
                    parts.append({"text": blk.text})
                elif isinstance(blk, ToolUseBlock):
                    parts.append({"functionCall": {"name": blk.name, "args": blk.input}})
                # thinking 块不回传（Gemini 不接受思考重放）
            if parts:
                contents.append({"role": "model", "parts": parts})
    return contents


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def build_gemini_body(request: LLMRequest) -> dict:
    body: dict = {
        "contents": encode_gemini_contents(request.messages),
    }
    if request.system:
        body["systemInstruction"] = {"parts": [{"text": request.system}]}
    generation: dict = dict(request.thinking or {})
    if request.max_tokens and request.max_tokens > 0:
        generation["maxOutputTokens"] = request.max_tokens
    if generation:
        body["generationConfig"] = generation
    if request.tools:
        body["tools"] = [
            {"functionDeclarations": [convert_tool_gemini(t) for t in request.tools]}
        ]
    return body


# ---------------------------------------------------------------------------
# 流事件映射（纯生成器，供单测）
# ---------------------------------------------------------------------------


def map_gemini_responses(raw: Iterable[dict]) -> Iterator[InternalEvent]:
    finish: str | None = None
    saw_function_call = False
    counter = 0

    for obj in raw:
        if not isinstance(obj, dict):
            continue
        error = obj.get("error")
        if error:
            code = error.get("code") or 0
            raise ProviderError(
                classify_http_error(code, json.dumps(error, ensure_ascii=False)[:300]),
                f"stream error: {json.dumps(error, ensure_ascii=False)[:300]}",
                status=code or None,
            )
        candidates = obj.get("candidates") or []
        if candidates:
            cand = candidates[0]
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("thought"):
                    text = part.get("text")
                    if text:
                        yield ThinkingDelta(text)
                    continue
                function_call = part.get("functionCall")
                if function_call:
                    counter += 1
                    saw_function_call = True
                    name = function_call.get("name") or "call"
                    yield ToolCallDelta(
                        call_id=f"fc_{counter}_{name}",
                        name=name,
                        args_delta=json.dumps(
                            function_call.get("args") or {}, ensure_ascii=False
                        ),
                    )
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    yield TextDelta(text)
            if cand.get("finishReason"):
                finish = cand["finishReason"]
        usage = obj.get("usageMetadata")
        if usage:
            yield UsageUpdate(
                input_tokens=usage.get("promptTokenCount"),
                output_tokens=usage.get("candidatesTokenCount"),
                cache_read_tokens=usage.get("cachedContentTokenCount"),
                reasoning_tokens=usage.get("thoughtsTokenCount"),
            )

    if saw_function_call:
        yield TurnEnd(STOP_TOOL_USE)
    else:
        yield TurnEnd(_FINISH_MAP.get(finish or "", STOP_END_TURN))


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class GeminiProvider(Provider):
    protocol = "gemini"

    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        if not self.api_key:
            raise ProviderError(
                "auth",
                f"missing API key for provider {self.provider_id!r}: "
                f"set environment variable {env_var_for(self.provider_id)}",
            )
        body = build_gemini_body(request)
        headers = {
            "x-goog-api-key": self.api_key,
            "content-type": "application/json",
        }
        base = (self.base_url or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/v1beta/models/{request.model}:streamGenerateContent?alt=sse"
        yield from map_gemini_responses(self._sse_post(url, headers=headers, body=body))
