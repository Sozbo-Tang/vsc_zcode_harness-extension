"""多厂商 LLM Provider 层：统一抽象 + anthropic / openai(兼容) / gemini / fake 适配器。

内部事件形态与厂商差异说明见各模块 docstring 与 docs/research/llm-apis.md。
"""

from .base import (
    InternalEvent,
    LLMRequest,
    Provider,
    ProviderError,
    TextBlock,
    ThinkingBlock,
    RedactedThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    LLMUserMessage,
    LLMAssistantMessage,
    ThinkingDelta,
    TextDelta,
    SignatureUpdate,
    ToolCallDelta,
    UsageUpdate,
    TurnEnd,
    classify_http_error,
    classify_exception,
    merge_usage,
    ping,
)
from .anthropic import AnthropicProvider
from .openai import OpenAIProvider
from .gemini import GeminiProvider
from .fake import FakeProvider, Wait

PROTOCOL_ADAPTERS: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def default_provider_factory(provider_entry: dict, api_key: str | None) -> Provider:
    """按 provider 条目（providers.json）构造 Provider 实例。

    条目字段：id / protocol / baseUrl / auth("none" 可免 Key，本地端点) /
    maxTokensParam("max_completion_tokens")。
    """
    protocol = (provider_entry.get("protocol") or "").lower()
    provider_id = provider_entry.get("id") or "provider"
    base_url = provider_entry.get("baseUrl")
    try:
        if protocol == "anthropic":
            return AnthropicProvider(provider_id, base_url=base_url, api_key=api_key)
        if protocol == "openai":
            return OpenAIProvider(
                provider_id,
                base_url=base_url,
                api_key=api_key,
                api_key_optional=provider_entry.get("auth") == "none",
                max_tokens_field=provider_entry.get("maxTokensParam") or "max_tokens",
            )
        if protocol == "gemini":
            return GeminiProvider(provider_id, base_url=base_url, api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        raise ProviderError("config", f"cannot build provider {provider_id!r}: {exc}") from exc
    raise ProviderError(
        "config",
        f"unsupported protocol {protocol!r} for provider {provider_id!r} "
        "(expected anthropic | openai | gemini)",
    )


__all__ = [
    "InternalEvent",
    "LLMRequest",
    "Provider",
    "ProviderError",
    "TextBlock",
    "ThinkingBlock",
    "RedactedThinkingBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "LLMUserMessage",
    "LLMAssistantMessage",
    "ThinkingDelta",
    "TextDelta",
    "SignatureUpdate",
    "ToolCallDelta",
    "UsageUpdate",
    "TurnEnd",
    "classify_http_error",
    "classify_exception",
    "merge_usage",
    "ping",
    "AnthropicProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "FakeProvider",
    "Wait",
    "PROTOCOL_ADAPTERS",
    "default_provider_factory",
]
