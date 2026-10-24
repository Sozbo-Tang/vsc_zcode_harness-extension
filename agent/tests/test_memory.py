"""memory：MEMORY.md 读写、system prompt 注入（R24）、memory 工具与分档权限。"""

from __future__ import annotations

import io
import json
import time

from zcode_py.llm.base import TextDelta
from zcode_py.llm.fake import FakeProvider
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.memory import (
    MEMORY_INJECTION_LIMIT,
    MEMORY_SPEC,
    MemoryPermissionEngine,
    append_memory,
    memory_injection,
    memory_path,
    read_memory,
    register_memory_tool,
    replace_memory,
)
from zcode_py.permissions import ALLOW, ASK
from zcode_py.sessions import SessionStore
from zcode_py.tools import ToolContext, ToolRegistry


def _wait_requests(fake: FakeProvider, count: int = 1, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(fake.requests) >= count:
            return
        time.sleep(0.01)


def test_missing_or_empty_memory_no_injection(tmp_home):
    assert memory_injection() is None  # 无文件
    memory_path().write_text("", encoding="utf-8")
    assert memory_injection() is None  # 空文件也不占 token
    assert read_memory() == ""


def test_injection_contains_header_and_content(tmp_home):
    memory_path().write_text("- 用户用 uv 管理依赖\n", encoding="utf-8")
    injection = memory_injection()
    assert injection is not None
    assert "MEMORY.md" in injection
    assert "用户用 uv 管理依赖" in injection


def test_injection_truncated_at_limit(tmp_home):
    memory_path().write_text("x" * (MEMORY_INJECTION_LIMIT + 500), encoding="utf-8")
    injection = memory_injection()
    assert "truncated" in injection and "500 chars omitted" in injection
    assert "x" * 100 in injection  # 头部内容保留
    # 有界：头部说明 + 截断内容 + 尾注，不随文件无限膨胀
    assert len(injection) < MEMORY_INJECTION_LIMIT + 500


def test_append_ensures_line_boundary(tmp_home):
    append_memory("first fact")  # 空文件起笔
    append_memory("second fact")
    assert read_memory() == "first fact\nsecond fact\n"


def test_replace_rewrites_whole_file(tmp_home):
    append_memory("old-1")
    append_memory("old-2")
    replace_memory("only this\n")
    assert read_memory() == "only this\n"


def test_memory_tool_handler_read_append_replace(tmp_home):
    ctx = ToolContext()
    assert MEMORY_SPEC.handler({"mode": "read"}, ctx).result == {"content": ""}
    MEMORY_SPEC.handler({"mode": "append", "text": "fact A"}, ctx)
    assert read_memory() == "fact A\n"
    MEMORY_SPEC.handler({"mode": "replace", "text": "fact B\n"}, ctx)
    assert read_memory() == "fact B\n"
    # 非法入参结构化报错（不炸回合）
    assert MEMORY_SPEC.handler({"mode": "append"}, ctx).is_error  # 缺 text
    assert MEMORY_SPEC.handler({"mode": "explode"}, ctx).is_error


def test_memory_permission_split_by_mode():
    """read 免批（与只读三件套同待遇）；append/replace 与 write 同档默认询问。"""
    engine = MemoryPermissionEngine()
    assert engine.check("memory", {"mode": "read"}).outcome == ALLOW
    assert engine.check("memory", {}).outcome == ALLOW  # mode 缺省视作 read
    assert engine.check("memory", {"mode": "append", "text": "x"}).outcome == ASK
    assert engine.check("memory", {"mode": "replace", "text": "x"}).outcome == ASK
    # 其余工具的分档不受影响
    assert engine.check("write", {"path": "a.md"}).outcome == ASK
    assert engine.check("read", {"path": "a.md"}).outcome == ALLOW


def test_memory_tool_registration_and_tier(tmp_home):
    registry = ToolRegistry()
    register_memory_tool(registry)
    assert "memory" in registry.names()
    assert registry.get("memory").tier == "write"  # 与 write 同档（read 分流在引擎）


def test_service_injects_memory_into_system_prompt(tmp_home):
    """会话启动注入：有 MEMORY.md 时请求 system 尾部带记忆块；无文件时不带。"""
    memory_path().write_text("- 偏好中文回复\n", encoding="utf-8")
    fake = FakeProvider(turns=[[TextDelta("ok")]])
    out = io.StringIO()
    service = ZcodeService(
        StdioEmitter(out, store=SessionStore()),
        provider_factory=lambda entry, key: fake,
    )
    service.handle_line(json.dumps({"type": "init", "workspaceRoots": []}))
    service.handle_line(json.dumps({"type": "user_message", "text": "q"}))
    _wait_requests(fake)
    system = fake.requests[0].system or ""
    assert "MEMORY.md" in system and "偏好中文回复" in system

    # 同一 home 下删掉 MEMORY.md：新会话请求 system 无记忆块（不占 token）
    memory_path().unlink()
    fake2 = FakeProvider(turns=[[TextDelta("ok")]])
    out2 = io.StringIO()
    service2 = ZcodeService(
        StdioEmitter(out2, store=SessionStore()),
        provider_factory=lambda entry, key: fake2,
    )
    service2.handle_line(json.dumps({"type": "init", "workspaceRoots": []}))
    service2.handle_line(json.dumps({"type": "user_message", "text": "q"}))
    _wait_requests(fake2)
    assert "MEMORY.md" not in (fake2.requests[0].system or "")
