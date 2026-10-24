"""M8 计划模式：build | plan | yolo 三档表驱动 + DANGER 黑名单压过 yolo
（PLAN §9.3 不变量）+ service control 接线。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zcode_py.llm.base import TextDelta, ToolCallDelta, TurnEnd
from zcode_py.llm.fake import FakeProvider
from zcode_py.permissions import (
    ALLOW,
    ASK,
    DENY,
    PLAN_MODE_DENY_REASON,
    PermissionEngine,
    mode_aware_engine,
)
from zcode_py.protocol import PermissionRequest, decode_message
from zcode_py.tools import ToolOutcome, ToolRegistry, ToolSession, ToolSpec

# 档位查询表：read=readonly，write/bash/edit/mcp__*=write
TIERS = {
    "read": "readonly",
    "grep": "readonly",
    "write": "write",
    "edit": "write",
    "bash": "write",
    "notebook_write": "write",
    "mcp__fs__write_file": "write",
}


def engine_for(mode: str, allow_rules: list | None = None):
    return mode_aware_engine(
        PermissionEngine(allow_rules=allow_rules or []),
        tier_lookup=lambda name: TIERS.get(name, "write"),
        mode_getter=lambda: mode,
    )


# ---------------------------------------------------------------------------
# 表驱动：三档 × 工具类型
# ---------------------------------------------------------------------------

CASES = [
    # (mode, tool, args, expected_outcome, expected_ruleId)
    ("build", "write", {"path": "a.py"}, ASK, "default_ask"),
    ("build", "read", {"path": "a.py"}, ALLOW, "readonly"),
    ("plan", "write", {"path": "a.py"}, DENY, "plan_mode"),
    ("plan", "edit", {"path": "a.py"}, DENY, "plan_mode"),
    ("plan", "bash", {"command": "ls"}, DENY, "plan_mode"),
    ("plan", "mcp__fs__write_file", {"p": "x"}, DENY, "plan_mode"),
    ("plan", "read", {"path": "a.py"}, ALLOW, "readonly"),
    ("yolo", "write", {"path": "a.py"}, ALLOW, "yolo"),
    ("yolo", "bash", {"command": "ls -la"}, ALLOW, "yolo"),
    ("yolo", "mcp__fs__write_file", {"p": "x"}, ALLOW, "yolo"),
    ("yolo", "read", {"path": "a.py"}, ALLOW, "readonly"),
    # 黑名单压过 yolo（§9.3 不变量）
    ("yolo", "bash", {"command": "rm -rf /"}, ASK, "danger.rm_recursive_force"),
    ("yolo", "bash", {"command": "git push --force origin main"}, ASK, "danger.git_push_force"),
    ("yolo", "bash", {"command": "sudo rm x"}, ASK, "danger.sudo"),
    # 黑名单同样压过白名单（build 档下）
    ("build", "bash", {"command": "rm -rf /"}, ASK, "danger.rm_recursive_force"),
]


@pytest.mark.parametrize("mode,tool,args,outcome,rule_id", CASES)
def test_permission_mode_matrix(mode, tool, args, outcome, rule_id):
    check = engine_for(mode, allow_rules=["bash: rm *"]).check(tool, dict(args))
    assert check.outcome == outcome
    assert check.ruleId == rule_id


def test_yolo_with_allowlist_still_forces_danger_ask():
    engine = engine_for("yolo", allow_rules=["bash: *"])  # 全量白名单
    check = engine.check("bash", {"command": "dd if=x of=/dev/sda"})
    assert check.outcome == ASK and check.danger is True  # 黑名单最高优先级
    safe = engine.check("bash", {"command": "git status"})
    assert safe.outcome == ALLOW


def test_invalid_mode_falls_back_to_build():
    engine = mode_aware_engine(
        PermissionEngine(), tier_lookup=lambda n: TIERS.get(n, "write"), mode_getter=lambda: "bogus"
    )
    assert engine.check("write", {"path": "a"}).outcome == ASK  # build 语义


def test_always_memory_forwarded_to_inner():
    engine = engine_for("build")
    engine.remember_always("write", {"path": "a.py"})
    assert engine.check("write", {"path": "a.py"}).outcome == ALLOW


# ---------------------------------------------------------------------------
# ToolSession 层：plan 拒绝不弹卡 / edit 也被拒 / yolo 免卡但危险仍弹卡
# ---------------------------------------------------------------------------


def spec(name: str, tier: str, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        parameters={},
        tier=tier,
        handler=handler or (lambda args, ctx: ToolOutcome(result={"ok": True})),
    )


def make_session(engine, *, asks, executed, extra_specs=()):
    registry = ToolRegistry()
    for s in extra_specs:
        registry.register(s)
    session = ToolSession(
        registry=registry,
        permissions=engine,
        cwd=Path("."),
        emit=lambda m: None,
        ask_permission=lambda tool, args: asks.append(tool) or "deny",
    )
    session.ctx.state["record"] = executed
    return session


def test_plan_mode_denies_write_tools_without_permission_card():
    asks: list = []
    executed: list = []

    def guarded(args, ctx):
        executed.append(args)
        from zcode_py.tools import ToolOutcome

        return ToolOutcome(result={"ok": True})

    session = make_session(
        engine_for("plan"), asks=asks, executed=executed,
        extra_specs=[spec("write", "write", guarded)],
    )
    outcome = session.run("write", {"path": "a.py", "content": "x"}, "c1")
    assert executed == []  # handler 没跑
    assert asks == []  # 不弹卡
    assert outcome.is_error and outcome.result["error"] == "permission_denied"
    assert outcome.result["ruleId"] == "plan_mode"
    assert outcome.result["detail"] == PLAN_MODE_DENY_REASON


def test_plan_mode_denies_edit_and_mcp_tools():
    asks: list = []
    session = make_session(
        engine_for("plan"), asks=asks, executed=[],
        extra_specs=[spec("edit", "write"), spec("mcp__s__t", "write")],
    )
    for name, args in (("edit", {"path": "a", "oldText": "x", "newText": "y"}),
                       ("mcp__s__t", {})):
        outcome = session.run(name, args, "c")
        assert outcome.is_error and outcome.result["ruleId"] == "plan_mode"
        assert asks == []  # edit 也不走审阅卡（计划模式直接结构化拒绝）


def test_yolo_runs_write_without_asking_but_danger_asks():
    asks: list = []
    calls: list = []

    def bash_handler(args, ctx):
        calls.append(args["command"])
        return ToolOutcome(result={"exitCode": 0})

    # ask 回调恒 deny：若引擎误走弹卡路径，结果一定是 permission_denied
    session = make_session(
        engine_for("yolo"), asks=asks, executed=[],
        extra_specs=[spec("bash", "write", bash_handler)],
    )
    ok = session.run("bash", {"command": "git status"}, "c1")
    assert not ok.is_error and calls == ["git status"]
    assert asks == []  # yolo：普通命令免卡直接执行

    denied = session.run("bash", {"command": "sudo rm -rf /"}, "c2")
    assert asks == ["bash"]  # 危险命令：强制弹卡（yolo 不豁免黑名单）
    assert denied.is_error and denied.result["error"] == "permission_denied"
    assert calls == ["git status"]  # handler 未执行


# ---------------------------------------------------------------------------
# service 接线：control set_permission_mode + init.settings.permissionMode
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self, provider: FakeProvider, settings: dict | None = None):
        import io

        from zcode_py.main import StdioEmitter, ZcodeService
        from zcode_py.sessions import SessionStore

        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter, provider_factory=lambda entry, key: provider
        )
        self.settings = settings or {}

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def frames_of_type(self, type_name: str) -> list:
        return [f for f in self.frames() if f.get("type") == type_name]


def test_control_set_permission_mode_roundtrip(tmp_home, tmp_path):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": [str(tmp_path)]})
    harness.send({"type": "control", "command": "set_permission_mode", "mode": "plan"})
    results = [f for f in harness.frames() if f["type"] == "result"]
    assert results[-1]["command"] == "set_permission_mode" and results[-1]["ok"] is True

    harness.send({"type": "control", "command": "set_permission_mode", "mode": "wat"})
    errors = harness.frames_of_type("error")
    assert errors[-1]["kind"] == "protocol"


def test_plan_mode_in_full_loop_returns_structured_refusal(tmp_home, tmp_path):
    """整 loop：plan 模式下 write 工具被结构化拒绝、无权限卡、回合正常收场。"""
    fake = FakeProvider(
        turns=[
            [ToolCallDelta(call_id="c1", name="write", args_delta=json.dumps(
                {"path": str(tmp_path / "a.txt"), "content": "x"})), TurnEnd("tool_use")],
            [TextDelta("计划模式：我将只提出方案")],
        ]
    )
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": [str(tmp_path)]})
    harness.send({"type": "control", "command": "set_permission_mode", "mode": "plan"})
    harness.send({"type": "user_message", "text": "改一下 a.txt"})
    # 等回合结束
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if harness.frames_of_type("message_complete"):
            break
        time.sleep(0.01)

    assert harness.frames_of_type("message_complete"), "回合应正常完成"
    assert harness.frames_of_type("permission_request") == []  # 不弹卡
    finished = [
        f for f in harness.frames_of_type("tool_call") if f["state"] == "finished"
    ]
    assert finished and finished[0]["result"]["error"] == "permission_denied"
    assert finished[0]["result"]["ruleId"] == "plan_mode"
    assert not (tmp_path / "a.txt").exists()  # 文件没被写


def test_init_settings_permission_mode_yolo(tmp_home, tmp_path):
    fake = FakeProvider(
        turns=[
            [ToolCallDelta(call_id="c1", name="write", args_delta=json.dumps(
                {"path": str(tmp_path / "y.txt"), "content": "hi"})), TurnEnd("tool_use")],
            [TextDelta("done")],
        ]
    )
    harness = Harness(fake, settings={"permissionMode": "yolo"})
    harness.send({"type": "init", "workspaceRoots": [str(tmp_path)], "settings": harness.settings})
    harness.send({"type": "user_message", "text": "写 y.txt"})
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if harness.frames_of_type("message_complete"):
            break
        time.sleep(0.01)

    assert harness.frames_of_type("permission_request") == []  # yolo：write 免询问
    assert (tmp_path / "y.txt").exists()  # 真的写了


def test_control_decode_accepts_new_fields():
    msg = decode_message(
        {"type": "control", "command": "mcp_add", "name": "fs", "server": {"command": ["a"]}}
    )
    assert msg.name == "fs" and msg.server == {"command": ["a"]}
    with pytest.raises(Exception):
        decode_message({"type": "control", "command": "mcp_explode"})
