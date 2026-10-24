"""stdio JSONL 服务入口：``python -m zcode_py``。

每行一个 JSON 帧进 stdin，协议帧与事件逐行写 stdout（立即 flush）。
协议定义见 zcode_py/protocol.py（PLAN §5）；分发逻辑见 zcode_py/main.py。
"""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
