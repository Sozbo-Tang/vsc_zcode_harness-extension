"""OpenAI Chat Completions 兼容适配器（调研笔记 docs/research/llm-apis.md §2–§5）。

覆盖一类端点（差异全部在配置层消化，代码同一条路径）：

- **OpenAI 官方**：base_url ``https://api.openai.com/v1``，``Authorization: Bearer``；
  思考参数 ``{"reasoning_effort": "minimal|low|medium|high|xhigh|max"}``（模型支持
  子集各异，400 时按错误分类上报）；官方 Chat Completions 不透出推理文本。
- **智谱 GLM OpenAI 兼容**：base_url ``https://open.bigmodel.cn/api/paas/v4``；
  思考参数 ``{"thinking": {"type": "enabled"}}``（GLM-5.3 传 disabled 直接报错，
  应在模型档案把 off 档 ok 置 false）；思考增量走非标 ``delta.reasoning_content``
  （DeepSeek 约定），本适配器已映射为 ThinkingDelta。
- **OpenRouter**：base_url ``https://openrouter.ai/api/v1``；SSE 注释行心跳由
  llm/sse.py 跳过；末尾 usage chunk 的 choices 非空（偏离 spec）不影响映射
  （choices 内 delta 为空即无事发生）；中途错误是 chunk 顶层 ``error`` 对象，
  ``error.metadata.error_type`` 用于分类；私有参数 ``{"reasoning": {...}}``
  同样经 thinking 载荷直接合并进请求体。
- **Ollama**：base_url ``http://localhost:11434/v1``，api_key 任意非空被忽略
  （provider 条目配 ``"auth": "none"`` 可免配 Key）；支持
  stream/stream_options.include_usage/tools/reasoning_effort（调研 §5.1）。
- **自定义 / DeepSeek 等其他 OpenAI 兼容端点**：只改 baseUrl + Key。

max_tokens 字段：默认发 ``max_tokens``（兼容面最广，GLM/Ollama/多数兼容端点）；
个别新模型要求 ``max_completion_tokens`` 的，provider 条目可配
``"maxTokensParam": "max_completion_tokens"``。
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
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"

_FINISH_MAP = {
    "stop": STOP_END_TURN,
    "tool_calls": STOP_TOOL_USE,
    "function_call": STOP_TOOL_USE,
    "length": STOP_MAX_TOKENS,
    "content_filter": STOP_CONTENT_FILTER,
}


# ---------------------------------------------------------------------------
# 请求体构造（纯函数，供单测）
# ---------------------------------------------------------------------------


def _tool_result_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def encode_openai_messages(messages: Iterable, system: str | None = None) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        if isinstance(msg, LLMUserMessage):
            text_parts = [b.text for b in msg.blocks if isinstance(b, TextBlock)]
            image_blocks = [b for b in msg.blocks if isinstance(b, ImageBlock)]
            tool_msgs = [
                {
                    "role": "tool",
                    "tool_call_id": b.tool_use_id,
                    "content": _tool_result_to_text(b.content),
                }
                for b in msg.blocks
                if isinstance(b, ToolResultBlock)
            ]
            out.extend(tool_msgs)
            if text_parts and not image_blocks:
                out.append({"role": "user", "content": "\n\n".join(text_parts)})
            elif text_parts or image_blocks:
                # M5 视觉：多模态 content 数组（text + image_url data URL）
                content: list = [{"type": "text", "text": t} for t in text_parts]
                for blk in image_blocks:
                    if blk.source_type == "base64":
                        url = f"data:{blk.media_type};base64,{blk.data}"
                    else:  # source_type 为 URL 的形态（当前生成路径未用，直通）
                        url = blk.data
                    content.append({"type": "image_url", "image_url": {"url": url}})
                out.append({"role": "user", "content": content})
        elif isinstance(msg, LLMAssistantMessage):
            entry: dict = {"role": "assistant"}
            text = "\n\n".join(b.text for b in msg.blocks if isinstance(b, TextBlock))
            tool_uses = [b for b in msg.blocks if isinstance(b, ToolUseBlock)]
            # 注意：assistant 思考内容不回传（GLM clear_thinking 默认 true 的行为，
            # 调研 §3.3；保留式思考属后续里程碑的上下文管理职责）。
            if text:
                entry["content"] = text
            if tool_uses:
                entry["tool_calls"] = [
                    {
                        "id": b.id,
                        "type": "function",
                        "function": {
                            "name": b.name,
                            "arguments": json.dumps(
                                b.input, ensure_ascii=False, default=str
                            ),
                        },
                    }
                    for b in tool_uses
                ]
            if len(entry) > 1:
                out.append(entry)
    return out


def build_openai_body(
    request: LLMRequest, *, max_tokens_field: str = "max_tokens"
) -> dict:
    body: dict = {
        "model": request.model,
        "messages": encode_openai_messages(request.messages, system=request.system),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.max_tokens and request.max_tokens > 0:
        body[max_tokens_field] = request.max_tokens
    if request.tools:
        body["tools"] = request.tools  # 内部即 OpenAI function 风格，直通
    if isinstance(request.thinking, dict):
        body.update(request.thinking)  # reasoning_effort / thinking / reasoning ...
    return body


# ---------------------------------------------------------------------------
# 流事件映射（纯生成器，供单测）
# ---------------------------------------------------------------------------


def _openrouter_error_kind(error: dict) -> str:
    meta = error.get("metadata") or {}
    token = " ".join(
        str(x) for x in (meta.get("error_type"), error.get("code"), error.get("message")) if x
    ).lower()
    if any(k in token for k in ("auth", "401", "403", "api key")):
        return "auth"
    if any(k in token for k in ("quota", "rate", "429", "credit")):
        return "quota"
    if any(k in token for k in ("model", "404", "not found")):
        return "model"
    if any(
        k in token
        for k in ("provider", "network", "timeout", "unavailable", "overloaded", "502", "503")
    ):
        return "network"
    return "unknown"


def map_openai_chunks(raw: Iterable[dict]) -> Iterator[InternalEvent]:
    index_to_id: dict[int, str] = {}
    finish: str | None = None

    for chunk in raw:
        if not isinstance(chunk, dict):
            continue
        error = chunk.get("error")
        if error:
            raise ProviderError(
                _openrouter_error_kind(error),
                f"stream error: {json.dumps(error, ensure_ascii=False, default=str)[:300]}",
            )
        usage = chunk.get("usage")
        if usage:
            details = usage.get("completion_tokens_details") or {}
            yield UsageUpdate(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                reasoning_tokens=details.get("reasoning_tokens"),
            )
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield TextDelta(content)
            reasoning = delta.get("reasoning_content")  # GLM/DeepSeek 非标字段
            if isinstance(reasoning, str) and reasoning:
                yield ThinkingDelta(reasoning)
            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                call_id = tc.get("id")
                if call_id:
                    index_to_id.setdefault(index, call_id)
                resolved = index_to_id.get(index) or f"call_{index}"
                fn = tc.get("function") or {}
                name = fn.get("name")
                args_delta = fn.get("arguments") or ""
                if name is None and not args_delta:
                    continue
                yield ToolCallDelta(
                    call_id=resolved, name=name, args_delta=args_delta
                )
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

    yield TurnEnd(_FINISH_MAP.get(finish or "", STOP_END_TURN))


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAIProvider(Provider):
    """OpenAI 及一切 Chat Completions 兼容端点（GLM/OpenRouter/Ollama/自定义）。"""

    protocol = "openai"

    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout=None,
        api_key_optional: bool = False,
        max_tokens_field: str = "max_tokens",
        extra_headers: dict | None = None,
    ):
        super().__init__(
            provider_id, base_url=base_url, api_key=api_key, timeout=timeout
        )
        self.api_key_optional = api_key_optional
        self.max_tokens_field = max_tokens_field
        self.extra_headers = extra_headers or {}

    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        key = self.api_key
        if not key and self.api_key_optional:
            key = "unused"  # Ollama 等本地端点忽略 Key 但需要非空头
        if not key:
            raise ProviderError(
                "auth",
                f"missing API key for provider {self.provider_id!r}: "
                f"set environment variable {env_var_for(self.provider_id)}",
            )
        body = build_openai_body(request, max_tokens_field=self.max_tokens_field)
        headers = {
            "Authorization": f"Bearer {key}",
            "content-type": "application/json",
            **self.extra_headers,
        }
        url = (self.base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
        yield from map_openai_chunks(self._sse_post(url, headers=headers, body=body))
