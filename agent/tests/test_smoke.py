"""M1 stdio 全链路冒烟：init → session_started → 流式回复（FakeProvider 无网）。

本文件原为 M0 的 stdin→stdout echo 往返测试；M1 按任务要求把骨架改造成真实
协议处理（echo 是 M0 占位行为，PLAN §7 M1 起由协议分发取代），断言随之更新
为协议行为——stdin JSONL 进、stdout JSONL 出的全链路覆盖保持不变。
"""

from __future__ import annotations

import io
import json

from zcode_py.llm import FakeProvider
from zcode_py.llm.base import TextDelta
from zcode_py.main import main


def test_stdio_round_trip(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("你好")]])
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "init", "workspaceRoots": ["/tmp/proj"]}),
                json.dumps({"type": "user_message", "text": "hello"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    assert main(stdin, stdout, provider_factory=lambda entry, key: fake) == 0

    frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
    types = [f["type"] for f in frames]

    assert types[0] == "session_started"
    assert "telegraph_state" in types
    assert "sessions_list" in types
    assert "stream_delta" in types
    assert "message_complete" in types
    assert "perf" in types
    assert "usage" in types

    session = frames[0]
    assert isinstance(session["sessionId"], str) and session["sessionId"]

    stream = next(f for f in frames if f["type"] == "stream_delta")
    assert stream["text"] == "你好"

    complete = next(f for f in frames if f["type"] == "message_complete")
    assert complete["stopReason"] == "end_turn"
    assert complete["text"] == "你好"

    perf = [f for f in frames if f["type"] == "perf"]
    assert perf[-1]["phase"] == "done"
    assert perf[-1]["lastTurn"]["ttftMs"] is not None
