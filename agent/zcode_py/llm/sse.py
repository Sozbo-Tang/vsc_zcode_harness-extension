"""手写 SSE 解析（调研笔记 docs/research/llm-apis.md §4.2 / §7.3）。

健壮性要求：
- 跳过 ``:`` 开头的注释行（OpenRouter 的 ``: OPENROUTER PROCESSING`` 心跳）；
- 跳过空行与 ``event:`` / ``id:`` / ``retry:`` 等非 data 行；
- ``data: [DONE]`` 终止符（OpenAI 系）结束流，其他厂商不出现时自然耗尽；
- 单行损坏 JSON 跳过不致命（流中偶发心跳/杂散行）。
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """SSE 行流 → data 载荷字符串流（不含 ``data: `` 前缀）。"""
    for raw in lines:
        line = raw.rstrip("\r\n").strip("\ufeff")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[5:].strip()
        # event:/id:/retry: 等行按 SSE 规范忽略


def iter_sse_json(lines: Iterable[str]) -> Iterator[dict]:
    """SSE 行流 → 解析后的 JSON 对象流。"""
    for payload in iter_sse_data(lines):
        if payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj
