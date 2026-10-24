"""Ollama 原生 /api/chat 适配器（NDJSON 流式，docs.ollama.com/api/chat）。

与 OpenAI 兼容透传（llm/openai.py 配 ``http://localhost:11434/v1``）的区别：
原生端点拿到 tools 原生块、``think`` 参数与逐块 ``message.thinking`` 增量
（OpenAI 兼容层不透出思考），故按 ROADMAP M1 独立成适配器。

请求形状（2026-10 官方 docs 核对）：
- ``POST {base}/api/chat``，body ``{model, messages, tools, stream: true,
  think?, options: {num_predict}}``；tools 即 OpenAI function 风格直通；
  max_tokens → ``options.num_predict``；思考参数（档案声明 ``{"think": true}``
  等）经 thinking 载荷直接合并进 body（仅思考型模型接受，非思考模型会 400，
  故参考档案按无思考型处理、默认不发）。
- 消息：user 文本 + 图片（base64 字符串数组）；tool 结果 ``{role: "tool",
  content, tool_name}``（按函数名回传，无 id 概念——从历史 assistant
  ToolUseBlock 建 id→name 映射，同 gemini 适配器做法）；assistant 的
  tool_calls 以完整对象回放（arguments 为 JSON 对象）。

流式响应为 NDJSON（一行一个 JSON 对象，非 SSE）：
- 每块 ``{model, created_at, message: {role, content, thinking, tool_calls},
  done}``；``thinking`` 字段版本支持时逐块出现 → ThinkingDelta；
- tool_calls 整块下发（无 id 字段，arguments 是对象）→ 合成 call_id
  ``oc_<序号>_<name>``，args 整体序列化为一个 args_delta（loop 聚合后
  json.loads 还原）；部分模型只在 done 块才带 tool_calls，故 done 块同样解析；
- 末块 ``done: true`` 带 ``done_reason``（stop | length | tool_calls | load |
  unload）与计数 ``prompt_eval_count`` / ``eval_count`` → UsageUpdate。

安全（PLAN §9.1）：本适配器默认 ``http://localhost:11434`` 属**用户显式配置
的本地端点例外**——netguard 的出网校验只作用于 agent 主动发起的
webfetch/websearch/browser 请求，providers.json 里的 provider baseUrl 是
用户亲手配置的 LLM 端点，不做环回/私网拒绝（与 anthropic/gemini 等远程
适配器同一条路径，不因协议不同而区别对待）。Key 可选：未配 Key 不发
Authorization（本地默认无需认证）；配了则发 Bearer（Ollama 服务端
OLLAMA_AUTH 场景）。
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator

import httpx

from .base import (
    ImageBlock,
    InternalEvent,
    LLMRequest,
    Provider,
    ProviderError,
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

DEFAULT_BASE_URL = "http://localhost:11434"

_DONE_REASON_MAP = {
    "stop": STOP_END_TURN,
    "load": STOP_END_TURN,   # 模型加载空响应（messages 为空场景），视为正常收尾
    "unload": STOP_END_TURN,  # keep_alive=0 卸载请求的收尾，同上
    "length": STOP_MAX_TOKENS,
    "tool_calls": STOP_TOOL_USE,
}


# ---------------------------------------------------------------------------
# 请求体构造（纯函数，供单测）
# ---------------------------------------------------------------------------


def _tool_name_map(messages: Iterable) -> dict[str, str]:
    """历史 assistant ToolUseBlock 的 id → name 映射（tool 结果按名回传）。"""
    mapping: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, LLMAssistantMessage):
            for blk in msg.blocks:
                if isinstance(blk, ToolUseBlock):
                    mapping[blk.id] = blk.name
    return mapping


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def encode_ollama_messages(messages: Iterable, system: str | None = None) -> list[dict]:
    name_map = _tool_name_map(messages)
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        if isinstance(msg, LLMUserMessage):
            text = "\n\n".join(b.text for b in msg.blocks if isinstance(b, TextBlock))
            images = [
                b.data
                for b in msg.blocks
                if isinstance(b, ImageBlock) and b.source_type == "base64"
            ]
            if text or images:
                entry: dict = {"role": "user", "content": text}
                if images:
                    entry["images"] = images  # Ollama：base64 裸字符串数组
                out.append(entry)
            for blk in msg.blocks:
                if isinstance(blk, ToolResultBlock):
                    out.append(
                        {
                            "role": "tool",
                            "content": _result_text(blk.content),
                            "tool_name": name_map.get(blk.tool_use_id, blk.tool_use_id),
                        }
                    )
        elif isinstance(msg, LLMAssistantMessage):
            text = "\n\n".join(b.text for b in msg.blocks if isinstance(b, TextBlock))
            tool_uses = [b for b in msg.blocks if isinstance(b, ToolUseBlock)]
            # 思考内容不回传（Ollama 不接受 thinking 重放，与 openai 适配器一致）
            if text or tool_uses:
                entry = {"role": "assistant", "content": text}
                if tool_uses:
                    entry["tool_calls"] = [
                        {
                            "function": {
                                "name": b.name,
                                "arguments": b.input,  # 原生形态：JSON 对象
                            }
                        }
                        for b in tool_uses
                    ]
                out.append(entry)
    return out


def build_ollama_body(request: LLMRequest) -> dict:
    body: dict = {
        "model": request.model,
        "messages": encode_ollama_messages(request.messages, system=request.system),
        "stream": True,
    }
    if request.max_tokens and request.max_tokens > 0:
        body["options"] = {"num_predict": request.max_tokens}
    if request.tools:
        body["tools"] = request.tools  # 内部即 OpenAI function 风格，原生直通
    if isinstance(request.thinking, dict):
        body.update(request.thinking)  # 如 {"think": true} / {"think": "low"}
    return body


# ---------------------------------------------------------------------------
# NDJSON 流事件映射（纯生成器，供单测）
# ---------------------------------------------------------------------------


def _ollama_error_kind(message: str) -> str:
    """Ollama 错误文本启发分类（流内 {"error": "..."}，NDJSON 中途错误）。"""
    lowered = message.lower()
    if "model" in lowered and ("not found" in lowered or "does not exist" in lowered):
        return "model"
    if any(k in lowered for k in ("auth", "unauthorized", "forbidden", "api key")):
        return "auth"
    if any(k in lowered for k in ("timeout", "connection", "refused", "unavailable")):
        return "network"
    return "unknown"


def map_ollama_chunks(raw: Iterable[dict]) -> Iterator[InternalEvent]:
    """NDJSON 块流 → 内部事件。done 块同样解析 tool_calls（部分模型只在
    done 帧才给解析完成的工具调用），有工具调用即 tool_use 收尾（不信
    done_reason 单一信号）。"""
    finish: str | None = None
    saw_tool_call = False
    counter = 0

    for chunk in raw:
        if not isinstance(chunk, dict):
            continue
        error = chunk.get("error")
        if error:
            text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False, default=str)
            raise ProviderError(_ollama_error_kind(text), f"stream error: {text[:300]}")
        message = chunk.get("message") or {}
        thinking = message.get("thinking")
        if isinstance(thinking, str) and thinking:
            yield ThinkingDelta(thinking)
        content = message.get("content")
        if isinstance(content, str) and content:
            yield TextDelta(content)
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name") or "call"
            counter += 1
            saw_tool_call = True
            args = fn.get("arguments")
            args_delta = (
                args if isinstance(args, str) and args
                else json.dumps(args if args is not None else {}, ensure_ascii=False, default=str)
            )
            yield ToolCallDelta(call_id=f"oc_{counter}_{name}", name=name, args_delta=args_delta)
        if chunk.get("done"):
            finish = chunk.get("done_reason") or "stop"
            # 末块计数：prompt_eval_count/eval_count（无 cache 维度，None 不覆盖）
            if chunk.get("prompt_eval_count") is not None or chunk.get("eval_count") is not None:
                yield UsageUpdate(
                    input_tokens=chunk.get("prompt_eval_count"),
                    output_tokens=chunk.get("eval_count"),
                )

    if saw_tool_call:
        yield TurnEnd(STOP_TOOL_USE)
    else:
        yield TurnEnd(_DONE_REASON_MAP.get(finish or "", STOP_END_TURN))


def iter_ndjson_json(lines: Iterable[str]) -> Iterator[dict]:
    """NDJSON 行流 → JSON 对象流（跳过空行与损坏行，与 sse.py 同宽容度）。"""
    for raw in lines:
        line = raw.rstrip("\r\n").strip("\ufeff").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OllamaProvider(Provider):
    """Ollama 原生 /api/chat（本地端点，免 Key）。"""

    protocol = "ollama"

    def stream(self, request: LLMRequest) -> Iterator[InternalEvent]:
        body = build_ollama_body(request)
        headers = {"content-type": "application/json"}
        if self.api_key:
            # 可选认证：Ollama 服务端配置 OLLAMA_AUTH 时才校验，本地默认忽略
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = (self.base_url or DEFAULT_BASE_URL).rstrip("/") + "/api/chat"
        yield from map_ollama_chunks(self._ndjson_post(url, headers=headers, body=body))

    def _ndjson_post(
        self,
        url: str,
        *,
        headers: dict,
        body: dict,
        timeout: httpx.Timeout | float | None = None,
    ) -> Iterator[dict]:
        """POST + NDJSON 行流。非 2xx 抛 ProviderError（与 base._sse_post 对齐，
        但 Ollama 是 NDJSON 不是 SSE，单列而非塞进基类）。"""
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
                yield from iter_ndjson_json(resp.iter_lines())
