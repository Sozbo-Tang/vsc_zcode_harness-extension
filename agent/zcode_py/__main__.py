"""M0 skeleton entrypoint: stdio JSONL echo.

Reads JSONL lines from stdin; for each line writes exactly one JSON line
``{"type": "echo", "line": <原文>}`` to stdout (flushed immediately), so the
TS shell can spawn this process and verify the full round-trip (PLAN §7 M0).
Real protocol handling starts in M1+; do not add features here.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO


def echo_line(line: str) -> str:
    """Return the JSONL echo frame for one input line, preserving it verbatim."""
    return json.dumps({"type": "echo", "line": line}, ensure_ascii=False)


def main(stream_in: TextIO | None = None, stream_out: TextIO | None = None) -> int:
    stream_in = stream_in if stream_in is not None else sys.stdin
    stream_out = stream_out if stream_out is not None else sys.stdout
    for raw in stream_in:
        stream_out.write(echo_line(raw.rstrip("\r\n")) + "\n")
        stream_out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
