"""L2 机械淘汰（PLAN §3.4）：旧工具结果换占位符，配对不断裂。

实现约束（Anthropic 回放约束，PLAN §8 compaction 单测专测项）：

- **只替换 content，不删任何块**：assistant 消息里的每个 tool_use 依旧能
  在其后的 user 消息里找到配对的 tool_result（tool_use_id 一一对应）；
- **不动消息边界**：user/assistant 交替、消息条数原样；
- **幂等**：已是占位符的结果不重复替换（重复触发 L2 无副作用）；
- 保留最近 ``keep_recent`` 条工具结果消息不动（当前回合刚用过的输出
  仍有价值），更早的全换占位符。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..llm.base import (
    LLMAssistantMessage,
    LLMUserMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

EVICT_PLACEHOLDER_TEMPLATE = "[evicted: 原 {chars} 字符工具输出]"


def placeholder_for(chars: int) -> str:
    return EVICT_PLACEHOLDER_TEMPLATE.format(chars=chars)


def is_placeholder(content) -> bool:
    """content 是否已是 L2 占位符（幂等判据）。"""
    return isinstance(content, str) and content.startswith("[evicted:")


def content_chars(content) -> int:
    """结果的字符量（str 直数；dict/其他 JSON 序列化后数）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(content))


def estimate_tokens(text_or_content) -> int:
    """粗略 token 估计（chars/4）——无分词器依赖，阈值与报告用同一口径。"""
    return content_chars(text_or_content) // 4


def estimate_messages_tokens(messages: list) -> int:
    total = 0
    for msg in messages:
        for block in getattr(msg, "blocks", []):
            total += estimate_tokens(getattr(block, "text", None))
            if hasattr(block, "content"):
                total += estimate_tokens(block.content)
            total += estimate_tokens(getattr(block, "input", None))
    return total


@dataclass
class EvictionReport:
    evicted_results: int = 0
    evicted_chars: int = 0
    result_messages_total: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    def to_dict(self) -> dict:
        return {
            "evictedResults": self.evicted_results,
            "evictedChars": self.evicted_chars,
            "resultMessages": self.result_messages_total,
            "tokensBefore": self.tokens_before,
            "tokensAfter": self.tokens_after,
        }


def tool_result_message_indices(messages: list) -> list[int]:
    """含 ToolResultBlock 的 user 消息下标（按出现顺序）。"""
    return [
        i
        for i, msg in enumerate(messages)
        if isinstance(msg, LLMUserMessage)
        and any(isinstance(b, ToolResultBlock) for b in msg.blocks)
    ]


def evict_tool_results(
    messages: list, *, keep_recent: int = 1, already_counted: bool = False
) -> EvictionReport:
    """就地替换较早轮次的 tool_result content 为占位符。

    ``keep_recent``：最近 N 条工具结果消息保持原样（默认 1）。已是占位符
    的跳过（幂等）。返回报告（evicted=0 表示无事可做，调用方不必广播）。
    """
    report = EvictionReport(
        tokens_before=estimate_messages_tokens(messages),
        result_messages_total=len(tool_result_message_indices(messages)),
    )
    indices = tool_result_message_indices(messages)
    if len(indices) <= keep_recent:
        report.tokens_after = report.tokens_before
        return report
    for i in indices[: len(indices) - keep_recent]:
        for block in messages[i].blocks:
            if not isinstance(block, ToolResultBlock) or is_placeholder(block.content):
                continue
            chars = content_chars(block.content)
            block.content = placeholder_for(chars)
            report.evicted_results += 1
            report.evicted_chars += chars
    report.tokens_after = estimate_messages_tokens(messages)
    return report


# ---------------------------------------------------------------------------
# 配对完整性校验（专测的判定函数，也供 manager 压缩后自检）
# ---------------------------------------------------------------------------


def pairing_intact(messages: list) -> bool:
    """Anthropic 回放约束自检：

    1. 每条 assistant 消息里的每个 tool_use 恰好有一个配对的 tool_result
       （tool_use_id 相等）出现在**其后**的 user 消息里；
    2. 不存在孤儿 tool_result（引用了从未出现的 tool_use_id）；
    3. tool_use id 不重复；
    4. user/assistant 严格交替，首条必须是 user（空列表也合法）。
    """
    from ..llm.base import ToolUseBlock

    if not messages:
        return True
    seen_uses: set[str] = set()
    used_results: set[str] = set()
    prev_role = None
    for msg in messages:
        if isinstance(msg, LLMUserMessage):
            role = "user"
        elif isinstance(msg, LLMAssistantMessage):
            role = "assistant"
        else:
            return False
        if prev_role is not None and role == prev_role:
            return False
        prev_role = role
        for block in msg.blocks:
            if isinstance(block, ToolUseBlock):
                if not isinstance(msg, LLMAssistantMessage):
                    return False  # tool_use 只能出现在 assistant 消息
                if block.id in seen_uses:
                    return False
                seen_uses.add(block.id)
            elif isinstance(block, ToolResultBlock):
                if not isinstance(msg, LLMUserMessage):
                    return False  # tool_result 只能出现在 user 消息
                if block.tool_use_id not in seen_uses or block.tool_use_id in used_results:
                    return False
                used_results.add(block.tool_use_id)
    return used_results == seen_uses


def summarize_sources(messages: list, *, head_limit: int = 60_000) -> str:
    """把消息列表渲染成供摘要模型阅读的纯文本语料（截断到 head_limit 字符）。"""
    lines: list[str] = []
    for msg in messages:
        role = "user" if isinstance(msg, LLMUserMessage) else "assistant"
        parts: list[str] = []
        for block in msg.blocks:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                parts.append(
                    f"<tool_use id={block.id} name={block.name}>\n"
                    f"{json.dumps(block.input, ensure_ascii=False)}\n</tool_use>"
                )
            elif isinstance(block, ToolResultBlock):
                parts.append(
                    f"<tool_result id={block.tool_use_id}>\n{block.content}\n</tool_result>"
                )
        joined = "\n".join(p for p in parts if p)
        if joined:
            lines.append(f"### {role}\n{joined}")
    text = "\n\n".join(lines)
    if len(text) > head_limit:
        omitted = len(text) - head_limit
        text = text[:head_limit] + f"\n... [compaction corpus truncated: {omitted} chars omitted] ..."
    return text


__all__ = [
    "EVICT_PLACEHOLDER_TEMPLATE",
    "EvictionReport",
    "content_chars",
    "estimate_messages_tokens",
    "estimate_tokens",
    "evict_tool_results",
    "is_placeholder",
    "pairing_intact",
    "placeholder_for",
    "summarize_sources",
    "tool_result_message_indices",
]
