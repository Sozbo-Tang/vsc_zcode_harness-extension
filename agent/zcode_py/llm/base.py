"""Provider 抽象：把各厂商流式事件统一为内部事件形态（docs/PLAN.md §3.1 llm/）。

内部统一三路内容 + 元事件（调研笔记 docs/research/llm-apis.md §7）：
- 思考增量：Anthropic thinking_delta / GLM·DeepSeek delta.reasoning_content /
  Gemini part.thought → :class:`ThinkingDelta`
- 正文增量：text_delta / delta.content / part.text → :class:`TextDelta`
- 工具调用增量：input_json_delta（按 content block index）/ delta.tool_calls
  （按 index 聚合，id/name 仅首块）/ functionCall（整块）→ :class:`ToolCallDelta`
- usage 为累计覆盖语义（字段 None = 无信息不覆盖）；stop_reason 归一为
  end_turn | tool_use | max_tokens | content_filter | cancelled。

HTTP 统一走 httpx 手写 SSE（适配 OpenRouter 注释行 / [DONE] 差异，见
llm/sse.py），不依赖各厂商 SDK 的流实现。
"""

from __future__ import annotations

import base64
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

import httpx

from .sse import iter_sse_json

ErrorKind = Literal["auth", "network", "model", "quota", "unknown", "config"]

STOP_END_TURN = "end_turn"
STOP_TOOL_USE = "tool_use"
STOP_MAX_TOKENS = "max_tokens"
STOP_CONTENT_FILTER = "content_filter"
STOP_CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# 与厂商无关的消息 / 内容块模型
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    """思考块。Anthropic 协议下必须连同 signature 原样回传（调研 §1.3），
    故签名随块保存，由 anthropic 适配器回放。"""

    text: str = ""
    signature: str = ""


@dataclass
class RedactedThinkingBlock:
    """Anthropic redacted_thinking 块：必须原样回传，内容不可读。"""

    data: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool = False


_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass
class ImageBlock:
    """图片内容块（M5 视觉，R9）：``source_type`` 目前仅 ``"base64"``（读文件
    编码），``path`` 保留审计/显示用途。主模型支持视觉时随用户消息进入
    LLM 请求；不支持时由 vision_relay 转述成文字、原图不进主请求。"""

    source_type: str = "base64"
    data: str = ""
    media_type: str = "image/png"
    path: str | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> "ImageBlock":
        p = Path(path).expanduser()
        raw = p.read_bytes()
        return cls(
            source_type="base64",
            data=base64.b64encode(raw).decode("ascii"),
            media_type=_MEDIA_TYPES.get(p.suffix.lower(), "image/png"),
            path=str(p),
        )


@dataclass
class LLMUserMessage:
    blocks: list = field(default_factory=list)


@dataclass
class LLMAssistantMessage:
    blocks: list = field(default_factory=list)


LLMMessage = LLMUserMessage | LLMAssistantMessage


# ---------------------------------------------------------------------------
# 内部流事件（Provider.stream 的产出）
# ---------------------------------------------------------------------------


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class TextDelta:
    text: str


@dataclass
class SignatureUpdate:
    """当前思考块的签名增量（Anthropic signature_delta，在块 stop 前下发）。"""

    signature: str


@dataclass
class ToolCallDelta:
    """工具调用增量。name 仅在首个分片出现（OpenAI），args_delta 为部分 JSON
    字符串（Anthropic input_json_delta / OpenAI arguments / Gemini 整块序列化）。"""

    call_id: str
    name: str | None = None
    args_delta: str = ""


@dataclass
class UsageUpdate:
    """累计值、整体覆盖；None 字段表示该次事件无该信息、不应覆盖已有值。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass
class TurnEnd:
    stop_reason: str = STOP_END_TURN


InternalEvent = (
    ThinkingDelta | TextDelta | SignatureUpdate | ToolCallDelta | UsageUpdate | TurnEnd
)


def merge_usage(current: UsageUpdate | None, update: UsageUpdate) -> UsageUpdate:
    """累计覆盖语义：非 None 字段覆盖，None 字段保留旧值（调研 §1.2）。"""
    if current is None:
        return UsageUpdate(
            input_tokens=update.input_tokens,
            output_tokens=update.output_tokens,
            cache_read_tokens=update.cache_read_tokens,
            cache_write_tokens=update.cache_write_tokens,
            reasoning_tokens=update.reasoning_tokens,
        )
    return UsageUpdate(
        input_tokens=(
            update.input_tokens
            if update.input_tokens is not None
            else current.input_tokens
        ),
        output_tokens=(
            update.output_tokens
            if update.output_tokens is not None
            else current.output_tokens
        ),
        cache_read_tokens=(
            update.cache_read_tokens
            if update.cache_read_tokens is not None
            else current.cache_read_tokens
        ),
        cache_write_tokens=(
            update.cache_write_tokens
            if update.cache_write_tokens is not None
            else current.cache_write_tokens
        ),
        reasoning_tokens=(
            update.reasoning_tokens
            if update.reasoning_tokens is not None
            else current.reasoning_tokens
        ),
    )


# ---------------------------------------------------------------------------
# 请求对象
# ---------------------------------------------------------------------------


@dataclass
class LLMRequest:
    model: str
    messages: list = field(default_factory=list)
    system: str | None = None
    """内部工具定义采用 OpenAI function 风格
    ``{"type":"function","function":{"name","description","parameters"}}``，
    各适配器自行转换为本协议格式。"""
    tools: list | None = None
    """思考参数：telegraph.py 按模型档案解析出的「直接合并进请求体」的键值
    （如 anthropic ``{"thinking": {...}}``、openai ``{"reasoning_effort": ...}``、
    gemini ``{"thinkingConfig": {...}}``）。None = 不发送（用模型默认）。"""
    thinking: dict | None = None
    max_tokens: int | None = None


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Provider 层错误，kind ∈ auth|network|model|quota|unknown|config。"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


def classify_http_error(status: int, body: str = "") -> str:
    """HTTP 状态码 + 响应体启发 → 错误分类。"""
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "model"
    if status == 429:
        return "quota"
    body_l = body.lower()
    if status == 400 and ("model" in body_l or "thinking" in body_l or "effort" in body_l):
        # 未知模型名 / 思考参数不支持（如 GLM-5.3 thinking disabled、Gemini
        # thinkingBudget 越界）都表现为 400 + 模型/参数名（调研 §7.4）。
        return "model"
    if status >= 500 or status == 408:
        return "network"
    if status == 400 and ("key" in body_l or "auth" in body_l or "token" in body_l):
        return "auth"
    return "unknown"


def classify_exception(exc: BaseException) -> str:
    """非 HTTP 异常（连接失败 / 超时 / 协议解析错）分类。"""
    if isinstance(exc, ProviderError):
        return exc.kind
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, ConnectionError)):
        return "network"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    return "unknown"


# ---------------------------------------------------------------------------
# Provider 基类
# ---------------------------------------------------------------------------


class Provider(ABC):
    """流式 Provider 抽象。stream() 是生成器：消费方提前 break 后应 close()
    （loop.py 的 finally 会做），以释放底层 HTTP 连接并支持取消。"""

    protocol: str = "abstract"

    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: httpx.Timeout | float | None = None,
    ):
        self.provider_id = provider_id
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    @abstractmethod
    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        """执行一次流式请求，产出内部事件。"""

    def _sse_post(
        self,
        url: str,
        *,
        headers: dict,
        body: dict,
        timeout: httpx.Timeout | float | None = None,
    ) -> Iterator[dict]:
        """POST + SSE，产出解析后的 JSON 对象流。非 2xx 抛 ProviderError。"""
        effective = timeout or self.timeout or httpx.Timeout(10.0, read=180.0)
        with httpx.Client(timeout=effective) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    text = resp.read().decode("utf-8", "replace")[:500]
                    raise ProviderError(
                        classify_http_error(resp.status_code, text),
                        f"HTTP {resp.status_code}: {text}",
                        status=resp.status_code,
                    )
                yield from iter_sse_json(resp.iter_lines())


# ---------------------------------------------------------------------------
# Ping：极小请求测连通与延迟（PLAN §2.2 R5 测试连接）
# ---------------------------------------------------------------------------


def ping(provider: Provider, model: str, *, read_timeout: float = 12.0) -> dict:
    """发一条极小消息（max_tokens=16，不带思考参数），到首个事件即停。

    返回 dict 供 PingResult 事件直接使用：
    ``{ok, latencyMs, errorKind, message}``。
    """
    started = time.perf_counter()
    request = LLMRequest(
        model=model,
        messages=[LLMUserMessage([TextBlock("ping")])],
        system=None,
        tools=None,
        thinking=None,
        max_tokens=16,
    )
    stream = provider.stream(request)
    try:
        for _event in stream:
            return {
                "ok": True,
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "errorKind": None,
                "message": None,
            }
        return {
            "ok": True,
            "latencyMs": int((time.perf_counter() - started) * 1000),
            "errorKind": None,
            "message": "empty stream",
        }
    except ProviderError as exc:
        return {
            "ok": False,
            "latencyMs": int((time.perf_counter() - started) * 1000),
            "errorKind": exc.kind,
            "message": exc.message,
        }
    except Exception as exc:  # noqa: BLE001 —— ping 需要把一切失败分类后上报
        return {
            "ok": False,
            "latencyMs": None,
            "errorKind": classify_exception(exc),
            "message": f"{type(exc).__name__}: {exc}",
        }
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
