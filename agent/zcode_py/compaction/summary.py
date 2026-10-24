"""L3 自动摘要（PLAN §3.4）：结构化压缩 + 保留尾部 ~15% 原文轮次。

流程（compact_with_summary）：

1. 解析压缩专用模型（``compactionModel``，沿用 vision_relay 的
   ``providerId/modelId`` 解析模式：provider 条目字段优先，
   ``init.settings.compactionModel`` 兜底，未配置回落当前主模型）；
2. 选尾部保留段：从后向前在**安全切点**（user 消息开头、其前一条
   assistant 无悬空 tool_use、该 user 无 tool_result——保证尾部自身配对
   完整、且与新上下文拼接后 user/assistant 交替成立）累积到 ~15% token
   估计量，至少保留最后一个安全段；
3. 头部语料渲染成纯文本交给压缩模型，产出**七要素结构化摘要**
   （任务目标/关键决策/文件及状态/错误与解法/环境事实/待办/下一步）；
4. 重建：``[摘要块并入尾部首条 user 消息开头] + 尾部原文轮次``——摘要以
   TextBlock 形式**并入**首条保留 user 消息（再追加一条独立 user 消息会
   制造连续 user，破坏交替）。

ProviderError 向上抛（由 manager 决定是否降级为 L2）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..config import get_provider_entry
from ..llm.base import (
    LLMAssistantMessage,
    LLMRequest,
    LLMUserMessage,
    Provider,
    ProviderError,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    TurnEnd,
)
from .eviction import estimate_messages_tokens, estimate_tokens, summarize_sources

# 七要素（PLAN §3.4 L3 任务书原文）
SUMMARY_SECTIONS = (
    "任务目标",
    "关键决策",
    "文件及状态",
    "错误与解法",
    "环境事实",
    "待办",
    "下一步",
)

SUMMARY_SYSTEM_PROMPT = (
    "你是编码 Agent 的上下文压缩器。把输入的历史对话压缩成结构化摘要，"
    "供同一个 Agent 无损接续工作。必须包含且仅包含以下七个小节"
    "（用 Markdown 二级标题，顺序固定）：\n"
    + "\n".join(f"## {name}" for name in SUMMARY_SECTIONS)
    + "\n\n要求：只保留对继续任务有信息量的事实；文件及状态要给出路径与最后"
    "已知状态；错误与解法要成对记录；环境事实记录工具链/平台等硬事实；"
    "待办列出未完成事项；下一步给出场内最合理的下一个动作。不要寒暄、"
    "不要复述原文、不要遗漏已做出的决定。"
)

SUMMARY_MAX_OUTPUT_TOKENS = 8192
# 尾部保留比例（token 估计口径）
TAIL_KEEP_RATIO = 0.15

_UNSET = "(compaction model unset; falling back to the main model)"


def resolve_compaction_target(
    config: dict,
    provider_entry: dict | None,
    settings: dict | None = None,
) -> tuple[dict | None, str | None, str]:
    """解析压缩专用模型 → (provider 条目, model id, 说明)。

    优先级：provider 条目 ``compactionModel`` → ``init.settings.compactionModel``
    → 当前主模型（条目 + 其 defaultModel）。格式同 visionRelayModel：
    ``"providerId/modelId"`` 跨 provider / 纯 ``"modelId"`` 当前 provider 内。
    找不到跨 provider 条目时返回错误说明。
    """
    spec = (provider_entry or {}).get("compactionModel")
    if (not isinstance(spec, str) or not spec.strip()) and settings:
        candidate = settings.get("compactionModel")
        if isinstance(candidate, str) and candidate.strip():
            spec = candidate
    if not isinstance(spec, str) or not spec.strip():
        entry = provider_entry
        if entry is None:
            return None, None, "no provider configured (compaction falls back unavailable)"
        return entry, str(entry.get("defaultModel") or ""), _UNSET
    spec = spec.strip()
    if "/" in spec:
        pid, _, mid = spec.partition("/")
        entry = get_provider_entry(config or {}, pid)
        if entry is None:
            return None, None, (
                f"压缩专用 provider {pid!r} 不在 providers.json 中；"
                "请修正 compactionModel（格式 providerId/modelId）"
            )
        model_id = mid.strip() or entry.get("defaultModel")
        if not model_id:
            return None, None, f"压缩专用 provider {pid!r} 未声明模型（defaultModel 缺失）"
        return entry, str(model_id), ""
    entry = provider_entry
    if entry is None:
        return None, None, "no current provider to resolve a bare compaction model id"
    return entry, spec, ""


@dataclass
class SummaryOutcome:
    ok: bool = False
    summary: str | None = None
    model: str | None = None  # providerId/modelId
    error: str | None = None  # ok=False 时的降级原因
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    summary_tokens: int = 0
    kept_tail_messages: int = 0
    notes: list = field(default_factory=list)
    new_messages: list = field(default_factory=list)  # 重建结果（ok=True 时非空）；不进 report

    def report(self) -> dict:
        return {
            "model": self.model,
            "summaryTokens": self.summary_tokens,
            "messagesBefore": self.messages_before,
            "messagesAfter": self.messages_after,
            "keptTailMessages": self.kept_tail_messages,
            "tokensBefore": self.tokens_before,
            "tokensAfter": self.tokens_after,
            **({"error": self.error} if self.error else {}),
            **({"notes": self.notes} if self.notes else {}),
        }


def summarize_messages(
    provider: Provider,
    model_id: str,
    messages: list,
    *,
    cancel_event: threading.Event | None = None,
) -> str | ProviderError:
    """调用压缩模型产出七要素摘要（流式拼接；取消/空回复按 ProviderError 处理）。"""
    corpus = summarize_sources(messages)
    request = LLMRequest(
        model=model_id,
        messages=[LLMUserMessage([TextBlock(corpus)])],
        system=SUMMARY_SYSTEM_PROMPT,
        tools=None,
        thinking=None,
        max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
    )
    chunks: list[str] = []
    stream = provider.stream(request)
    try:
        for event in stream:
            if cancel_event is not None and cancel_event.is_set():
                return ProviderError("unknown", "compaction cancelled")
            if isinstance(event, TextDelta):
                if event.text:
                    chunks.append(event.text)
            elif isinstance(event, TurnEnd):
                break
    except ProviderError as exc:
        return exc
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    text = "".join(chunks).strip()
    if not text:
        return ProviderError("model", "compaction model returned an empty summary")
    return text


# ---------------------------------------------------------------------------
# 尾部保留段选择 + 上下文重建
# ---------------------------------------------------------------------------


def _safe_cut_points(messages: list) -> list[int]:
    """安全切点下标 i：messages[i] 是「纯 user 消息」（无 tool_result），
    且 messages[i-1]（若存在）是 assistant 且无 tool_use（无悬空调用）。
    在 i 处切开，前后两段各自满足交替与配对约束。"""
    points: list[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, LLMUserMessage):
            continue
        if any(isinstance(b, ToolResultBlock) for b in msg.blocks):
            continue
        if i > 0:
            prev = messages[i - 1]
            if not isinstance(prev, LLMAssistantMessage):
                continue
            if any(isinstance(b, ToolUseBlock) for b in prev.blocks):
                continue
        points.append(i)
    return points


def split_head_tail(messages: list, *, keep_ratio: float = TAIL_KEEP_RATIO) -> tuple[list, list]:
    """(头部待摘要语料, 尾部保留轮次)。

    尾部从最后一个安全切点开始按比例向前扩展到 ~keep_ratio（至少保留
    最后一个安全段；没有任何安全切点时尾部=全部、头部=空——调用方应放弃
    本轮压缩）。
    """
    points = _safe_cut_points(messages)
    if not points:
        return [], list(messages)
    total = max(1, estimate_messages_tokens(messages))
    budget = max(1, int(total * keep_ratio))
    tail_start = points[-1]
    for cut in reversed(points):
        if estimate_messages_tokens(messages[cut:]) <= budget:
            tail_start = cut
        else:
            break
    return list(messages[:tail_start]), list(messages[tail_start:])


def rebuild_with_summary(summary_text: str, tail: list) -> list:
    """摘要并入尾部首条 user 消息开头 + 其余尾部原样。

    尾部为空（不可能，split_head_tail 保证至少一个安全段）防御性返回单条
    摘要 user 消息。
    """
    preamble = (
        "## 本会话此前上下文已压缩（L3）\n"
        "以下是七要素结构化摘要，替代更早的原始对话：\n\n" + summary_text
    )
    if not tail:
        return [LLMUserMessage([TextBlock(preamble)])]
    new_messages: list = []
    first = tail[0]
    if isinstance(first, LLMUserMessage):
        merged = LLMUserMessage([TextBlock(preamble), *first.blocks])
        new_messages.append(merged)
        new_messages.extend(tail[1:])
    else:  # 防御：安全切点保证首条是 user，这里兜底不破坏交替
        new_messages.append(LLMUserMessage([TextBlock(preamble)]))
        new_messages.extend(tail)
    return new_messages


def compact_with_summary(
    messages: list,
    *,
    provider: Provider,
    model_id: str,
    provider_id: str = "",
    cancel_event: threading.Event | None = None,
    keep_ratio: float = TAIL_KEEP_RATIO,
) -> SummaryOutcome:
    """L3 主入口：消息列表 → 摘要 + 尾部重建。**不**就地修改入参。"""
    outcome = SummaryOutcome(
        model=f"{provider_id}/{model_id}" if provider_id else model_id,
        messages_before=len(messages),
        tokens_before=estimate_messages_tokens(messages),
    )
    if len(messages) < 2:
        outcome.error = "context too small to compact"
        return outcome
    head, tail = split_head_tail(messages, keep_ratio=keep_ratio)
    if not head:
        outcome.error = "no safe cut point; nothing to summarize"
        return outcome
    summary = summarize_messages(provider, model_id, head, cancel_event=cancel_event)
    if isinstance(summary, ProviderError):
        outcome.error = f"compaction model failed: {summary.message}"
        return outcome
    new_messages = rebuild_with_summary(summary, tail)
    outcome.ok = True
    outcome.summary = summary
    outcome.new_messages = new_messages
    outcome.messages_after = len(new_messages)
    outcome.kept_tail_messages = len(tail)
    outcome.summary_tokens = estimate_tokens(summary)
    outcome.tokens_after = estimate_messages_tokens(new_messages)
    return outcome


__all__ = [
    "SUMMARY_MAX_OUTPUT_TOKENS",
    "SUMMARY_SECTIONS",
    "SUMMARY_SYSTEM_PROMPT",
    "TAIL_KEEP_RATIO",
    "SummaryOutcome",
    "compact_with_summary",
    "rebuild_with_summary",
    "resolve_compaction_target",
    "split_head_tail",
    "summarize_messages",
]
