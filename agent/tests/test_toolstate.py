"""R3 工具开关 + 默认权限（tools.json 持久化）——service 级往返。"""

from __future__ import annotations

import json
import os
import tempfile

from zcode_py.llm.fake import FakeProvider


def _make_harness(provider=None, tmp_home=None):
    """就地建 Harness（复用 test_service 的构造方式；避免跨测试模块导入）。"""
    import io

    from zcode_py.main import StdioEmitter, ZcodeService
    from zcode_py.sessions import SessionStore

    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: provider or FakeProvider())
    return out, emitter, service


class Harness:
    def __init__(self, provider=None, tmp_home=None):
        self._tmp_home = tmp_home
        self.out_ref, self.emitter, self.service = _make_harness(provider, tmp_home)

    def send(self, payload: dict) -> None:
        import json as _json
        self.service.handle_line(_json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out_ref.getvalue().splitlines()]


def _tools(harness) -> dict:
    harness.send({"type": "control", "command": "tools_list"})
    frames = [
        f for f in harness.frames()
        if f.get("type") == "result" and f.get("command") == "tools_list"
    ]
    assert frames, "no tools_list result"
    return {t["name"]: t for t in json.loads(frames[-1]["detail"])}


def test_tools_list_shape(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    tools = _tools(harness)
    assert {"read", "bash", "edit", "notebook_read", "memory"} <= set(tools)
    entry = tools["read"]
    assert entry["enabled"] is True and entry["alwaysAllowed"] is False
    assert entry["tier"] == "readonly"


def test_tool_toggle_disables_schema_and_persists(tmp_home):
    provider = FakeProvider()
    harness = Harness(provider)
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send({"type": "control", "command": "tool_toggle", "name": "bash", "enabled": False})
    ack = [f for f in harness.frames() if f.get("command") == "tool_toggle"][-1]
    assert ack["ok"] is True

    # 禁用即从 schema 消失（模型不可见）+ registry.get 返回 None
    assert harness.service._registry.get("bash") is None
    names = {t["function"]["name"] for t in harness.service._registry.schemas()}
    assert "bash" not in names and "read" in names

    # 持久化：新 service 实例（同 HOME）仍是禁用态
    harness2 = Harness(FakeProvider())
    harness2.send({"type": "init", "workspaceRoots": []})
    assert harness2.service._registry.get("bash") is None
    assert _tools(harness2)["bash"]["enabled"] is False

    # 重新启用恢复
    harness.send({"type": "control", "command": "tool_toggle", "name": "bash", "enabled": True})
    assert harness.service._registry.get("bash") is not None


def test_tool_toggle_unknown_tool(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "tool_toggle", "name": "nope", "enabled": True})
    errs = [f for f in harness.frames() if f["type"] == "error"]
    assert errs and errs[-1]["kind"] == "not_found"


def test_tool_permission_always_and_ask(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send({"type": "control", "command": "tool_permission", "name": "bash", "mode": "always"})
    ack = [f for f in harness.frames() if f.get("command") == "tool_permission"][-1]
    assert ack["ok"] is True
    check = harness.service._permissions_engine.check("bash", {"command": "git status"})
    assert check.outcome == "allow" and not check.danger
    # §9.3 不变量：always 规则绝不放行黑名单命令
    danger = harness.service._permissions_engine.check("bash", {"command": "rm -rf /"})
    assert danger.danger is True

    harness.send({"type": "control", "command": "tool_permission", "name": "bash", "mode": "ask"})
    check2 = harness.service._permissions_engine.check("bash", {"command": "git status"})
    assert check2.outcome == "ask"
    # 持久化到 tools.json
    state = json.loads((tmp_home / "config" / "tools.json").read_text(encoding="utf-8"))
    assert state["alwaysAllow"] == []


def test_tool_permission_invalid_mode(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "tool_permission", "name": "bash", "mode": "yolo!"})
    errs = [f for f in harness.frames() if f["type"] == "error"]
    assert errs and errs[-1]["kind"] == "protocol"
