"""M0 smoke test: stdin JSONL -> stdout echo round-trip."""

from __future__ import annotations

import io
import json

from zcode_py.__main__ import main


def test_echo_round_trip() -> None:
    frames = [
        json.dumps({"type": "user_message", "text": "hello"}),
        json.dumps({"type": "init", "workspaceRoots": ["/tmp"]}),
    ]
    stdin = io.StringIO("\n".join(frames) + "\n")
    stdout = io.StringIO()

    assert main(stdin, stdout) == 0

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 2
    for original, echoed in zip(frames, lines):
        assert json.loads(echoed) == {"type": "echo", "line": original}
