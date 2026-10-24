"""OpenRouter 适配器：OpenAI 兼容 Chat Completions + 站点可选归因头 + 模型列表。

与 openai 协议透传的差异（2026-10 官方 docs 核对）：
- 站点要求的**可选**归因头：``HTTP-Referer``（应用站点 URL）与
  ``X-Title``（应用名）——用于 openrouter.ai 排名页归因，官方标注 optional，
  但个别 Key 策略下未带会异常，故 provider 条目可配 ``httpReferer`` /
  ``appTitle``（providers.json 字段，缺省不发）；
- SSE 心跳注释行（``: OPENROUTER PROCESSING``）由 llm/sse.py 统一跳过，
  末尾 usage chunk choices 非空的偏离与流中 ``error.metadata.error_type``
  中途错误均已由 openai 适配器的 map_openai_chunks / _openrouter_error_kind
  覆盖——本适配器继承 OpenAIProvider 复用整条事件映射路径；
- 模型列表端点 ``GET {base}/models``（公开、免 Key）：返回 ``{data: [...]}``，
  每条含 id/name/context_length/architecture/supported_parameters 等；**定价
  字段（pricing）一律忽略**（PLAN §6.3 模型档案只存上下文/能力标记）。

思考参数：OpenRouter 透传 ``{"reasoning": {...}}`` / ``reasoning_effort``
等扩展参数（模型支持子集各异），thinking 载荷仍直接合并进请求体；参考
档案按无思考型处理（telegraph.reference_telegraph），模型档案声明后即通。
"""

from __future__ import annotations

from typing import Iterator

import httpx

from ..config import env_var_for
from .base import InternalEvent, LLMRequest, ProviderError, classify_exception
from .openai import OpenAIProvider

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# 模型列表条目里可用于推断能力标记的 supported_parameters 值（忽略其余）
_REASONING_PARAMS = ("reasoning", "include_reasoning", "reasoning_effort")


def normalize_openrouter_model(entry: dict) -> dict:
    """OpenRouter /models 条目 → providers.json 模型档案形态。

    只取 id/名称/上下文长度/能力标记；pricing 等计费字段全部丢弃。
    能力推断：tools 看 supported_parameters；thinking 看 reasoning 系参数
    或顶层 reasoning 元数据；vision 看 architecture.input_modalities 含 image。
    """
    if not isinstance(entry, dict) or not entry.get("id"):
        return {}
    params = entry.get("supported_parameters") or []
    architecture = entry.get("architecture") or {}
    input_modalities = architecture.get("input_modalities") or []
    reasoning_meta = entry.get("reasoning") or {}
    return {
        "id": entry["id"],
        "label": entry.get("name") or entry["id"],
        "contextWindow": entry.get("context_length"),
        "capabilities": {
            "vision": "image" in input_modalities,
            "tools": "tools" in params,
            "thinking": bool(
                reasoning_meta.get("supported_efforts")
                or any(p in params for p in _REASONING_PARAMS)
            ),
        },
    }


def fetch_models(
    base_url: str | None = None,
    *,
    api_key: str | None = None,
    timeout: httpx.Timeout | float | None = None,
) -> list[dict]:
    """GET {base}/models → 规整后的模型档案列表（定价字段丢弃）。

    端点公开免 Key；带 Key 可提高限流配额。失败抛 ProviderError（分类与
    chat 路径一致），供宿主 provider.list_models RPC 将来回执错误分类。
    """
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    headers: dict = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = httpx.get(f"{base}/models", headers=headers, timeout=timeout or 15.0)
    except Exception as exc:  # noqa: BLE001 —— 网络异常统一分类上报
        raise ProviderError(
            classify_exception(exc), f"list models failed: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code >= 400:
        text = resp.text[:500] if resp.text else ""
        raise ProviderError(
            "auth" if resp.status_code in (401, 403) else "network",
            f"HTTP {resp.status_code}: {text}",
            status=resp.status_code,
        )
    try:
        data = (resp.json() or {}).get("data")
    except ValueError as exc:
        raise ProviderError("unknown", f"list models: invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ProviderError("unknown", "list models: response has no 'data' array")
    models = [normalize_openrouter_model(e) for e in data if isinstance(e, dict)]
    return [m for m in models if m]


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter：OpenAI 兼容 + HTTP-Referer / X-Title 可选归因头。

    Key 必填（远程端点）；归因头经 OpenAIProvider.extra_headers 进
    Authorization 之外的请求头，事件映射完全复用父类。
    """

    protocol = "openrouter"

    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout=None,
        http_referer: str | None = None,
        app_title: str | None = None,
    ):
        # 站点归因头（官方标注 optional）：条目未配置则一个都不发
        extra: dict = {}
        if http_referer:
            extra["HTTP-Referer"] = http_referer
        if app_title:
            extra["X-Title"] = app_title
        super().__init__(
            provider_id,
            base_url=base_url or DEFAULT_BASE_URL,
            api_key=api_key,
            timeout=timeout,
            extra_headers=extra,
        )

    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        if not self.api_key:
            raise ProviderError(
                "auth",
                f"missing API key for provider {self.provider_id!r}: "
                f"set environment variable {env_var_for(self.provider_id)}",
            )
        return super().stream(request)

    def list_models(
        self, *, timeout: httpx.Timeout | float | None = None
    ) -> list[dict]:
        """实例方法形态（带实例 base_url/Key），供宿主 RPC 直接调用。"""
        return fetch_models(self.base_url, api_key=self.api_key, timeout=timeout)


__all__ = [
    "DEFAULT_BASE_URL",
    "OpenRouterProvider",
    "fetch_models",
    "normalize_openrouter_model",
]
