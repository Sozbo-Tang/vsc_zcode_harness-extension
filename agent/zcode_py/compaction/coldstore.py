"""L4 冷存检索（PLAN §3.4）：search_history 工具。

冷存储本体就是 ``transcript.jsonl``（PLAN §3.2 决策：转录即冷存，天然可
grep，不引入 SQLite/RAG）。本模块提供：

- :func:`search_transcript`：大小写不敏感子串检索转录行，命中返回信封
  摘要（ts/dir/事件类型/文本片段），上限 20 条（任务书：命中行上下文
  ≤ 20 条）；
- ``search_history`` 工具（readonly 档）：模型在 L2/L3 淘汰了原文后仍能
  凭关键词找回完整历史。转录路径经 ``ctx.state["history_reader"]``
  注入（服务层提供当前会话转录路径的回调——会话切换后自动跟新）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..tools import ToolContext, ToolOutcome, ToolSpec, error_outcome

MAX_HITS = 20
SNIPPET_CHARS = 400


def search_transcript(path: Path | str | None, query: str, *, max_hits: int = MAX_HITS) -> list[dict]:
    """检索一个 transcript.jsonl；返回命中摘要列表（≤ max_hits 条）。

    匹配对象是整行 JSON 文本（信封 + 事件全文）——工具参数、工具结果、
    用户/助手文本都在语料里。文件不存在/不可读返回空列表（只读工具，
    不抛异常）。
    """
    if not query.strip():
        return []
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    needle = query.strip().lower()
    hits: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or needle not in line.lower():
            continue
        hits.append(_hit(lineno, line))
        if len(hits) >= max_hits:
            break
    return hits


def _hit(lineno: int, line: str) -> dict:
    """单条命中摘要：行号 + 信封元数据 + 上下文片段（人物可读事件直取 text）。"""
    entry: dict = {"line": lineno}
    try:
        envelope = json.loads(line)
    except json.JSONDecodeError:
        entry["snippet"] = line[:SNIPPET_CHARS]
        return entry
    event = envelope.get("event") or {}
    entry["ts"] = envelope.get("ts")
    entry["dir"] = envelope.get("dir")
    entry["type"] = event.get("type")
    text = event.get("text")
    if isinstance(text, str) and text.strip():
        entry["text"] = _snippet(text)
    else:
        entry["snippet"] = _snippet(line)
    return entry


def _snippet(text: str) -> str:
    text = " ".join(text.split())  # 折叠换行，命中摘要保持单行可扫读
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


def _handle_search_history(args: dict, ctx: ToolContext) -> ToolOutcome:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return error_outcome("search_history requires a non-empty string 'query'")
    reader = ctx.state.get("history_reader")
    path = reader() if callable(reader) else None
    if path is None:
        return ToolOutcome(
            result={
                "error": "no_transcript",
                "detail": "本会话没有可检索的转录（init 之前或会话未落盘）",
            },
            is_error=True,
        )
    hits = search_transcript(path, query)
    return ToolOutcome(
        result={
            "query": query,
            "hits": hits,
            "count": len(hits),
            "truncated": len(hits) >= MAX_HITS,
            "source": str(path),
        }
    )


SEARCH_HISTORY_SPEC = ToolSpec(
    name="search_history",
    description=(
        "Full-text search over this session's persisted transcript (cold store). "
        "Use it to recover details that were evicted from the live context by "
        "compaction. Returns up to 20 matching entries with line numbers and "
        "snippets."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "case-insensitive substring"}
        },
        "required": ["query"],
    },
    tier="readonly",
    handler=_handle_search_history,
)


__all__ = ["MAX_HITS", "SEARCH_HISTORY_SPEC", "search_transcript"]
