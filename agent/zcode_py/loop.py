"""Agent 主循环（PLAN §3.1 loop.py）。

M2 职责：消费 Provider 流式 → 广播 thinking_delta / stream_delta →
message_complete；维护多轮上下文；perf 采样；取消；**工具循环**——
tool_use 聚合 → 权限判定（ToolSession 内经 ask_permission 回调广播
permission_request 并阻塞等待 permission_response）→ 执行（L1 截断）→
tool_result(is_error) 回灌 → 继续下一轮模型调用。

``tool_session``（tools.ToolSession）缺省时回退 M1 的 ``tool_executor``
（StubToolExecutor 返回结构化错误）——M1 全部行为与测试保持不变。

结构参照 OpenHands SDK 双层 + SWE-agent 有界收场（docs/research/
python-harness-patterns.md §1）：外层有界多轮（max_turns → stopReason
"max_turns"），内层单次模型流；取消（stopReason "cancelled"）在每个事件处
检查。取消发生在工具批次中间时，剩余调用快速补 "cancelled" 结果——
assistant 消息里每个 tool_use 必须有配对的 tool_result（Anthropic 回放
约束），哪怕回合已经放弃。回合异常收尾的另两种断裂（终态非 tool_use 却带
tool_call、工具执行前取消）由 ``_close_dangling_tool_uses`` 合成 not_executed
结果兜底；其后下一轮 user 文本并入 tool_result user 消息，维持角色交替。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .llm.base import (
    ImageBlock,
    LLMRequest,
    ProviderError,
    RedactedThinkingBlock,
    SignatureUpdate,
    STOP_CANCELLED,
    STOP_END_TURN,
    STOP_TOOL_USE,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolCallDelta,
    ToolResultBlock,
    ToolUseBlock,
    TurnEnd,
    UsageUpdate,
    LLMUserMessage,
    LLMAssistantMessage,
    merge_usage,
)
from .perf import PerfTracker
from .protocol import MessageComplete, StreamDelta, ThinkingDelta as ThinkingDeltaMsg, ToolCall
from .tools import ToolOutcome

DEFAULT_SYSTEM_PROMPT = (
    "You are Zcode-Py, a coding agent running inside a VSCode sidebar. "
    "Answer concisely in the user's language. You have tools: read/glob/grep "
    "for exploring files, todo for task tracking, write/edit for modifying "
    "files (edits are shown to the user as diffs for review), bash for shell "
    "commands (output truncated at 30k chars), and webfetch/websearch. "
    "Prefer tools over guessing; read a file before editing it."
)

# 有界收场（非模型 stop_reason，UI/转录语义）：轮数上限
STOP_MAX_TURNS = "max_turns"


class ToolExecutor(Protocol):
    """M2 工具执行器接口：执行一个工具调用，返回 (result, elapsedMs)。"""

    def run_tool(self, name: str, args: dict, call_id: str) -> tuple[Any, int]: ...


class StubToolExecutor:
    """M1 占位执行器：不执行，返回结构化错误（模型可据此改口）。"""

    def run_tool(self, name: str, args: dict, call_id: str) -> tuple[Any, int]:
        return (
            {
                "error": "tool_execution_unavailable",
                "detail": "Tool execution lands in M2 (docs/PLAN.md §7). "
                "Answer without tools for now.",
            },
            0,
        )


@dataclass
class _ToolAgg:
    name: str | None = None
    buf: list[str] = field(default_factory=list)


@dataclass
class TurnOutcome:
    stop_reason: str
    text: str
    tool_calls: list = field(default_factory=list)
    usage: UsageUpdate | None = None


class AgentLoop:
    def __init__(
        self,
        *,
        provider,
        model: str,
        emit: Callable[[Any], None],
        perf: PerfTracker,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_turns: int = 8,
        max_output_tokens: int | None = None,
        thinking_resolver: Callable[[], dict | None] | None = None,
        tool_executor: ToolExecutor | None = None,
        tool_session=None,
        cancel_event: threading.Event | None = None,
        on_usage: Callable[[str, UsageUpdate], None] | None = None,
        on_model_usage: Callable[[UsageUpdate], None] | None = None,
    ):
        self.provider = provider
        self.model = model
        self.emit = emit
        self.perf = perf
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        self.thinking_resolver = thinking_resolver
        self.tool_executor = tool_executor or StubToolExecutor()
        self.tool_session = tool_session
        self.cancel_event = cancel_event or threading.Event()
        self.on_usage = on_usage
        # M8 压缩钩子：每个模型回合结束（工具批次执行前）收到累计 usage，
        # CompactionManager 据此在两次模型调用之间做 L2/L3（不打断流）
        self.on_model_usage = on_model_usage
        self.messages: list = []
        self._last_outcome: TurnOutcome | None = None
        self._last_stop: str = STOP_END_TURN

    # -- 上下文 --------------------------------------------------------------

    def reset_context(self) -> None:
        self.messages = []

    def load_context(self, messages: list) -> None:
        self.messages = list(messages)

    # -- 主入口 --------------------------------------------------------------

    def run_user_text(self, text: str, images: list[ImageBlock] | None = None) -> None:
        """一轮用户输入 →（可能有多次模型-工具往返）→ message_complete。

        ``images``（M5 视觉）：主模型支持视觉时传 ImageBlock 列表，随本轮
        user 消息进入 LLM 请求；主模型无视觉时服务层已把中继转述段落并入
        ``text``，此处不传图（原图不进主请求，R9）。

        Provider 异常向上传播（由服务层捕获并广播 ``error{kind}``）；传播前
        回退本轮 user 文本，保持 user/assistant 交替——与取消路径同一语义，
        否则出错后的下一次请求会以连续 user 角色破坏 Anthropic 回放约束。

        上一回合以 tool_result（含合成的未执行结果，见
        ``_close_dangling_tool_uses``）收尾时，新文本**并入**那条 user 消息
        而不是再追加一条——连续 user 消息同样破坏交替。
        """
        image_blocks = [b for b in (images or [])]
        base_len = len(self.messages)
        merged: LLMUserMessage | None = None
        appended = 0  # 并入路径下本轮追加的块数（文本 + 图片），回退凭此摘除
        if self.messages and isinstance(self.messages[-1], LLMUserMessage):
            merged = self.messages[-1]
            merged.blocks.append(TextBlock(text))
            merged.blocks.extend(image_blocks)
            appended = 1 + len(image_blocks)
        else:
            self.messages.append(LLMUserMessage([TextBlock(text), *image_blocks]))
        self.perf.turn_start()
        try:
            self._run_bounded_turns()
        except BaseException:
            self._rollback_user_text(base_len, merged, appended)
            raise
        final = self._last_outcome
        # 本回合没有产生任何 assistant 消息（首事件前取消 / 空回合）：回退
        # 本轮 user 文本（与 context_from_frames 丢弃空回合的语义一致）
        self._rollback_user_text(base_len, merged, appended)
        self.emit(
            MessageComplete(
                stopReason=self._last_stop,
                text=final.text if final else None,
                inputTokens=(final.usage.input_tokens if final and final.usage else None),
                outputTokens=(final.usage.output_tokens if final and final.usage else None),
            )
        )
        perf_done = self.perf.turn_end(
            exact_output_tokens=(
                final.usage.output_tokens if final and final.usage else None
            )
        )
        self.emit(perf_done)
        if final is not None and final.usage is not None and self.on_usage is not None:
            self.on_usage(self.model, final.usage)

    def _run_bounded_turns(self) -> None:
        """有界多轮循环；结果落在 self._last_outcome / self._last_stop。"""
        outcome: TurnOutcome | None = None
        stop = STOP_END_TURN
        for _ in range(self.max_turns):
            if self._cancelled():
                stop = STOP_CANCELLED
                break
            outcome = self._model_turn()
            # M8：两次模型调用之间给压缩管理器看一次累计用量（L2/L3 时机）；
            # 回调内重建 self.messages 是安全窗口——本轮 outcome 的 tool_calls
            # 尚未执行，重建保留的尾部必然包含刚产出的 assistant 消息
            if (
                outcome.usage is not None
                and self.on_model_usage is not None
                and not self._cancelled()
            ):
                try:
                    self.on_model_usage(outcome.usage)
                except Exception:  # noqa: BLE001 —— 压缩失败不炸回合
                    pass
            stop = outcome.stop_reason
            if self._cancelled():
                stop = STOP_CANCELLED
                break
            if outcome.stop_reason == STOP_TOOL_USE and outcome.tool_calls:
                self._run_tools(outcome.tool_calls)
                continue
            break
        else:
            stop = STOP_MAX_TURNS
        # 配对兜底：模型吐了 tool_call 但终态非 tool_use（适配器把缺失/stop
        # 的 finish_reason 映射成 end_turn）、或回合在工具执行前被取消——
        # 悬空的 tool_use 会让下一次请求被 Anthropic 端点 400 拒绝
        if stop == STOP_CANCELLED:
            close_reason = "turn cancelled before tool execution"
        elif stop == STOP_MAX_TURNS:
            close_reason = "max turns reached before tool execution"
        else:
            close_reason = "turn ended without tool_use stop"
        self._close_dangling_tool_uses(close_reason)
        self._last_outcome = outcome
        self._last_stop = stop

    # -- 单次模型回合 ---------------------------------------------------------

    def _model_turn(self) -> TurnOutcome:
        thinking = self.thinking_resolver() if self.thinking_resolver else None
        request = LLMRequest(
            model=self.model,
            messages=list(self.messages),
            system=self.system_prompt,
            tools=(self.tool_session.schemas() if self.tool_session is not None else None),
            thinking=thinking,
            max_tokens=self.max_output_tokens,
        )
        blocks: list = []
        tool_aggs: dict[str, _ToolAgg] = {}
        tool_order: list[str] = []
        usage: UsageUpdate | None = None
        stop_reason: str | None = None
        cancelled = False

        stream = self.provider.stream(request)
        try:
            for event in stream:
                if self._cancelled():
                    cancelled = True
                    break
                if isinstance(event, ThinkingDelta):
                    if event.text:
                        self.emit(ThinkingDeltaMsg(event.text))
                        self._append_thinking(blocks, event.text)
                        self.perf.on_token(event.text)
                        self._maybe_perf()
                elif isinstance(event, TextDelta):
                    if event.text:
                        self.emit(StreamDelta(event.text))
                        self._append_text(blocks, event.text)
                        self.perf.on_token(event.text)
                        self._maybe_perf()
                elif isinstance(event, SignatureUpdate):
                    if event.signature:
                        self._attach_signature(blocks, event.signature)
                elif isinstance(event, ToolCallDelta):
                    agg = tool_aggs.get(event.call_id)
                    if agg is None:
                        agg = _ToolAgg()
                        tool_aggs[event.call_id] = agg
                        tool_order.append(event.call_id)
                    if event.name:
                        agg.name = event.name
                    if event.args_delta:
                        agg.buf.append(event.args_delta)
                elif isinstance(event, UsageUpdate):
                    usage = merge_usage(usage, event)
                elif isinstance(event, TurnEnd):
                    stop_reason = event.stop_reason
                    break
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()

        if cancelled:
            stop_reason = STOP_CANCELLED
        elif stop_reason is None:
            stop_reason = STOP_END_TURN

        tool_calls = []
        for call_id in tool_order:
            agg = tool_aggs[call_id]
            raw = "".join(agg.buf)
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"_unparsed": raw[:200]}
            if not isinstance(parsed, dict):
                parsed = {"_value": parsed}
            tool_calls.append(ToolUseBlock(id=call_id, name=agg.name or call_id, input=parsed))

        if blocks or tool_calls:
            blocks.extend(tool_calls)
            self.messages.append(LLMAssistantMessage(blocks))

        text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
        return TurnOutcome(stop_reason=stop_reason, text=text, tool_calls=tool_calls, usage=usage)

    # -- 工具执行（M2：ToolSession 权限→执行→截断；M1：stub 回退） ----------------

    def _run_tools(self, tool_calls: list) -> None:
        result_blocks: list = []
        for call in tool_calls:
            self.emit(
                ToolCall(callId=call.id, name=call.name, args=call.input, state="started")
            )
            started = time.perf_counter()
            outcome = self._execute_tool(call)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.emit(
                ToolCall(
                    callId=call.id,
                    name=call.name,
                    args=call.input,
                    state="finished",
                    result=outcome.result,
                    elapsedMs=elapsed_ms,
                    warning=outcome.warning,
                )
            )
            result_blocks.append(
                ToolResultBlock(
                    tool_use_id=call.id, content=outcome.result, is_error=outcome.is_error
                )
            )
        if result_blocks:
            self.messages.append(LLMUserMessage(result_blocks))

    def _execute_tool(self, call) -> ToolOutcome:
        if self.tool_session is not None:
            return self.tool_session.run(call.name, dict(call.input), call.id)
        if self._cancelled():
            return ToolOutcome(
                result={"error": "cancelled",
                        "detail": "turn cancelled before execution"},
                is_error=True,
            )
        result, _elapsed = self.tool_executor.run_tool(call.name, dict(call.input), call.id)
        return ToolOutcome(
            result=result,
            is_error=isinstance(result, dict) and "error" in result,
        )

    # -- 小工具 --------------------------------------------------------------

    def _rollback_user_text(
        self, base_len: int, merged: LLMUserMessage | None, appended: int = 1
    ) -> None:
        """回退本轮 user 文本——仅当回合没有产生任何 assistant 消息时。

        merged 非空表示本轮文本（及图片块）并入了上一回合的 tool_result
        user 消息：回退就是摘掉追加的 ``appended`` 个块；否则 pop 本轮追加
        的整条消息。
        """
        if merged is not None:
            n = appended if appended > 0 else 1
            if len(self.messages) == base_len and len(merged.blocks) >= n:
                del merged.blocks[-n:]
        elif len(self.messages) == base_len + 1:
            self.messages.pop()

    def _close_dangling_tool_uses(self, reason: str) -> None:
        """为未执行而悬空的 tool_use 合成 is_error 的 tool_result。

        只看最后一条消息：正常回合结束时它要么是 assistant（end_turn + 意外
        tool_call，或取消），要么是 user（tool_result 已配对，无事可做）。
        """
        if not self.messages or not isinstance(self.messages[-1], LLMAssistantMessage):
            return
        dangling = [b for b in self.messages[-1].blocks if isinstance(b, ToolUseBlock)]
        if not dangling:
            return
        self.messages.append(
            LLMUserMessage(
                [
                    ToolResultBlock(
                        tool_use_id=b.id,
                        content={"error": "not_executed", "detail": reason},
                        is_error=True,
                    )
                    for b in dangling
                ]
            )
        )

    def _cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def _maybe_perf(self) -> None:
        msg = self.perf.maybe_sample()
        if msg is not None:
            self.emit(msg)

    @staticmethod
    def _append_text(blocks: list, text: str) -> None:
        if blocks and isinstance(blocks[-1], TextBlock):
            blocks[-1].text += text
        else:
            blocks.append(TextBlock(text))

    @staticmethod
    def _append_thinking(blocks: list, text: str) -> None:
        if blocks and isinstance(blocks[-1], ThinkingBlock):
            blocks[-1].text += text
        else:
            blocks.append(ThinkingBlock(text=text))

    @staticmethod
    def _attach_signature(blocks: list, signature: str) -> None:
        for block in reversed(blocks):
            if isinstance(block, ThinkingBlock):
                block.signature += signature
                return


# ---------------------------------------------------------------------------
# 会话恢复：从转录帧重建 LLM 上下文（M1 纯文本重建；工具链路 M2 补）
# ---------------------------------------------------------------------------


def context_from_frames(frames) -> list:
    """user_message(in) 与 message_complete.text(out) 交替重建消息列表。

    无正文的完成（取消 / 出错 / 空回复）整轮丢弃——Anthropic 协议要求
    user/assistant 严格交替，悬空的 user 消息会直接破坏回放合法性。
    """
    messages: list = []
    pending_user: str | None = None
    for envelope in frames:
        event = envelope.get("event") or {}
        etype = event.get("type")
        direction = envelope.get("dir")
        if direction == "in" and etype == "user_message":
            pending_user = event.get("text") or ""
        elif direction == "out" and etype == "message_complete":
            text = event.get("text")
            if isinstance(text, str) and text and pending_user is not None:
                messages.append(LLMUserMessage([TextBlock(pending_user)]))
                messages.append(LLMAssistantMessage([TextBlock(text)]))
                pending_user = None
    return messages


__all__ = [
    "AgentLoop",
    "ToolExecutor",
    "StubToolExecutor",
    "TurnOutcome",
    "DEFAULT_SYSTEM_PROMPT",
    "context_from_frames",
    "ProviderError",
]
