"""M8 hooks：配置解析、pre_tool 阻断、post_tool 附加、超时、缝缺位禁用
（不阻断工具）、service 装配接线。runner 全打桩，零真实子进程。"""

from __future__ import annotations

import json

import pytest

from zcode_py.hooks import (
    HookRule,
    HooksConfigError,
    HooksRunner,
    hooks_path,
    load_hooks,
    parse_hooks,
)
from zcode_py.permissions import PermissionEngine
from zcode_py.protocol import Error
from zcode_py.tools import ToolOutcome, ToolRegistry, ToolSession, ToolSpec

try:  # 装配缝守门（与 test_mcp 同款）
    from zcode_py.mcp import proc_backend  # noqa: F401

    PROC_BACKEND_ASSEMBLED = True
except ImportError:
    PROC_BACKEND_ASSEMBLED = False


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def test_parse_hooks_top_level_array_and_wrapped():
    raw = [
        {"event": "pre_tool", "tool": "bash", "command": ["python", "guard.py"]},
        {"event": "post_tool", "command": ["python", "log.py"]},
    ]
    rules = parse_hooks(raw)
    assert rules == [
        HookRule(event="pre_tool", tool="bash", command=["python", "guard.py"]),
        HookRule(event="post_tool", tool="*", command=["python", "log.py"]),
    ]
    assert parse_hooks({"hooks": raw}) == rules  # 包裹形态兼容
    assert parse_hooks({}) == []  # hooks 键缺省 = 空


def test_parse_hooks_rejects_bad_entries():
    with pytest.raises(HooksConfigError):
        parse_hooks([{"event": "mid_tool", "command": ["x"]}])
    with pytest.raises(HooksConfigError):
        parse_hooks([{"event": "pre_tool", "command": "sh -c boom"}])  # 必须是 argv 列表
    with pytest.raises(HooksConfigError):
        parse_hooks([{"event": "pre_tool", "command": []}])


def test_load_hooks_missing_file_and_bad_file(tmp_path):
    assert load_hooks(tmp_path / "absent.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(HooksConfigError):
        load_hooks(bad)
    assert hooks_path().name == "hooks.json"


# ---------------------------------------------------------------------------
# runner：pre 阻断 / post 附加 / 超时 / 崩溃（打桩）
# ---------------------------------------------------------------------------


def scripted_runner(results: list, calls: list):
    """按序返回预设结果，记录 (command, payload, timeout)。"""

    def runner(command, *, stdin=None, timeout=10.0):
        calls.append({"command": list(command), "payload": json.loads(stdin), "timeout": timeout})
        return results.pop(0)

    return runner


def make_rules():
    return [
        HookRule(event="pre_tool", tool="bash", command=["guard.py"]),
        HookRule(event="post_tool", tool="*", command=["log.py"]),
    ]


def test_pre_tool_block_passes_reason_and_skips_handler():
    calls: list = []
    runner = scripted_runner([(1, "危险命令：不允许 rm", "", False)], calls)
    hooks = HooksRunner(make_rules(), runner=runner)
    executed = []

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="bash", description="", parameters={}, tier="write",
        handler=lambda args, ctx: executed.append(args) or ToolOutcome(result={"ok": True}),
    ))
    session = ToolSession(
        registry=registry, permissions=PermissionEngine(), cwd=__import__("pathlib").Path("."),
        emit=lambda m: None, ask_permission=lambda *a: "allow",
    )
    session.ctx.state["hooks"] = hooks
    outcome = session.run("bash", {"command": "rm temp"}, "c1")

    assert executed == []  # 阻断：handler 未执行
    assert outcome.is_error and outcome.result["error"] == "hook_blocked"
    assert "危险命令" in outcome.result["detail"]
    # stdin 载荷是 {tool, args} JSON
    assert calls[0]["payload"] == {"event": "pre_tool", "tool": "bash", "args": {"command": "rm temp"}}


def test_pre_tool_exit_zero_allows():
    calls: list = []
    runner = scripted_runner([(0, "", "", False)], calls)
    hooks = HooksRunner([HookRule("pre_tool", "*", ["g"])], runner=runner)
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="bash", description="", parameters={}, tier="write",
        handler=lambda args, ctx: ToolOutcome(result={"ok": True}),
    ))
    session = ToolSession(
        registry=registry, permissions=PermissionEngine(), cwd=__import__("pathlib").Path("."),
        emit=lambda m: None, ask_permission=lambda *a: "allow",
    )
    session.ctx.state["hooks"] = hooks
    outcome = session.run("bash", {"command": "ls"}, "c1")
    assert not outcome.is_error and outcome.result == {"ok": True}


def test_post_tool_stdout_appended_to_result():
    calls: list = []
    runner = scripted_runner([(0, "", "", False), (0, "logged by hook", "", False)], calls)
    hooks = HooksRunner(make_rules(), runner=runner)
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="bash", description="", parameters={}, tier="write",
        handler=lambda args, ctx: ToolOutcome(result={"ok": True}),
    ))
    session = ToolSession(
        registry=registry, permissions=PermissionEngine(), cwd=__import__("pathlib").Path("."),
        emit=lambda m: None, ask_permission=lambda *a: "allow",
    )
    session.ctx.state["hooks"] = hooks
    outcome = session.run("bash", {"command": "ls"}, "c1")
    assert outcome.result["postHook"] == "logged by hook"


def test_pre_tool_timeout_blocks_with_reason():
    calls: list = []
    runner = scripted_runner([(-1, "", "", True)], calls)
    hooks = HooksRunner([HookRule("pre_tool", "*", ["g"], timeout_s=0.2)], runner=runner)
    blocked, reason = hooks.pre("bash", {"command": "ls"})
    assert blocked and "timed out" in reason


def test_hook_crash_does_not_block():
    def crashing_runner(command, *, stdin=None, timeout=10.0):
        raise RuntimeError("hook bug")

    hooks = HooksRunner([HookRule("pre_tool", "*", ["g"])], runner=crashing_runner)
    blocked, reason = hooks.pre("bash", {})
    assert blocked is False and reason is None  # hooks 是旁路，不是门禁


def test_glob_matching_scopes_rules():
    calls: list = []
    runner = scripted_runner([(0, "hit", "", False)], calls)
    hooks = HooksRunner([HookRule("post_tool", "notebook_*", ["n"])], runner=runner)
    assert hooks.post("bash", {}) is None  # 工具不匹配：不执行
    assert calls == []
    assert hooks.post("notebook_read", {}) == "hit"  # 命中：stdout 附加
    assert calls[0]["command"] == ["n"]


# ---------------------------------------------------------------------------
# 缝缺位：广播一次 config error 后禁用（不阻断工具）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(PROC_BACKEND_ASSEMBLED, reason="proc_backend 已装配，缺位分支不可达")
def test_seam_missing_broadcasts_once_and_disables(tmp_path):
    (tmp_path / "hooks.json").write_text(
        json.dumps([{"event": "pre_tool", "tool": "bash", "command": ["g"]}]), encoding="utf-8"
    )
    rules = load_hooks(tmp_path / "hooks.json")
    emitted: list = []
    hooks = HooksRunner(rules, emit=emitted.append)
    hooks.startup_check()

    assert len(emitted) == 1 and isinstance(emitted[0], Error)
    assert emitted[0].kind == "config" and "MCP_BACKEND_SPEC" in emitted[0].message
    # 禁用后：pre 直通、不再广播
    assert hooks.pre("bash", {"command": "ls"}) == (False, None)
    hooks.startup_check()
    assert len(emitted) == 1


@pytest.mark.skipif(PROC_BACKEND_ASSEMBLED, reason="proc_backend 已装配，缺位分支不可达")
def test_service_wires_hooks_without_blocking_tools(tmp_home):
    """hooks.json 存在 + 缝缺位：init 广播一次 config error；工具照常执行。"""
    import io

    from zcode_py.llm.fake import FakeProvider
    from zcode_py.main import StdioEmitter, ZcodeService
    from zcode_py.sessions import SessionStore

    (tmp_home / "hooks.json").write_text(
        json.dumps([{"event": "pre_tool", "command": ["g"]}]), encoding="utf-8"
    )
    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: FakeProvider())
    service.handle_line(json.dumps({"type": "init", "workspaceRoots": [str(tmp_home)]}))

    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    config_errors = [
        f for f in frames
        if f["type"] == "error" and f["kind"] == "config" and "hooks" in f["message"]
    ]
    assert len(config_errors) == 1  # 恰一次
    # 工具未被阻断：search_history（readonly）正常执行（转录已有帧 → 有命中）
    outcome = service._loop.tool_session.run(
        "search_history", {"query": "hooks"}, "c1"
    )
    assert not outcome.is_error and "hits" in outcome.result


def test_broken_hooks_config_disables_hooks(tmp_home):
    import io

    from zcode_py.llm.fake import FakeProvider
    from zcode_py.main import StdioEmitter, ZcodeService
    from zcode_py.sessions import SessionStore

    (tmp_home / "hooks.json").write_text("{broken", encoding="utf-8")
    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: FakeProvider())
    service.handle_line(json.dumps({"type": "init", "workspaceRoots": []}))

    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    assert any(
        f["type"] == "error" and f["kind"] == "config" and "hooks disabled" in f["message"]
        for f in frames
    )
    assert service._hooks is None  # 无规则可用：session 不接 hooks
