"""共享 fixture：每个测试隔离的 ZCODE_PY_HOME（配置与会话都落在 tmp_path）。"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCODE_PY_HOME", str(tmp_path))
    return tmp_path


def parse_frames(raw: str) -> list[dict]:
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@pytest.fixture
def frames_of():
    return parse_frames
