"""M9 工作流测试：临时 workflows 目录 + 声明式 STEPS；真实 ChildRuntime +
FakeProvider 逐步执行断言（前步结果作下步前缀）；失败步停止；取消；服务层
workflow_run / workflows_list control 往返。"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from zcode_py.llm.base import ProviderError, TextDelta
from zcode_py.llm.fake import FakeProvider
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.permissions import PermissionEngine
from zcode_py.sessions import SessionStore
from zcode_py.subagents import ChildRuntime
from zcode_py.tools import ToolOutcome, default_registry
from zcode_py.workflows import WorkflowError, WorkflowManager


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def workflows_dir(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    return directory


def write_workflow(directory, name: str, body: str):
    (directory / f"{name}.py").write_text(body, encoding="utf-8")


GOOD = (
    "STEPS = [\n"
    "    {'prompt': '调研仓库结构并总结'},\n"
    "    {'prompt': '基于上文写一份发布说明', 'model': 'p2/cheap-1'},\n"
    "]\n"
)


# ---------------------------------------------------------------------------
# 扫描 / 加载
# ---------------------------------------------------------------------------


def test_list_workflows_scans_and_degrades(workflows_dir):
    write_workflow(workflows_dir, "good", GOOD)
    write_workflow(workflows_dir, "nosteps", "X = 1\n")
    write_workflow(workflows_dir, "badsteps", "STEPS = []\n")
    write_workflow(workflows_dir, "badprompt", "STEPS = [{'prompt': ''}]\n")
    (workflows_dir / "notes.txt").write_text("ignore", encoding="utf-8")

    manager = WorkflowManager(workflows_dir)
    listed = {entry["name"]: entry for entry in manager.list_workflows()}
    assert set(listed) == {"good", "nosteps", "badsteps", "badprompt"}
    assert listed["good"]["stepCount"] == 2
    assert listed["good"]["models"] == ["p2/cheap-1"]
    assert "no module-level STEPS" in listed["nosteps"]["error"]
    assert listed["badsteps"]["error"] and listed["badprompt"]["error"]


def test_load_rejects_traversal_and_missing(workflows_dir):
    manager = WorkflowManager(workflows_dir)
    with pytest.raises(WorkflowError, match="invalid workflow name"):
        manager.load("../etc/passwd")
    with pytest.raises(WorkflowError, match="invalid workflow name"):
        manager.load("a b")
    with pytest.raises(WorkflowError, match="not found"):
        manager.load("missing")


def test_empty_dir_lists_empty(tmp_path):
    assert WorkflowManager(tmp_path / "nope").list_workflows() == []


# ---------------------------------------------------------------------------
# 执行：真实 ChildRuntime + FakeProvider
# ---------------------------------------------------------------------------

CONFIG = {
    "providers": [
        {"id": "p1", "protocol": "openai", "defaultModel": "m1", "models": [{"id": "m1"}]},
        {"id": "p2", "protocol": "openai", "defaultModel": "cheap-1",
         "models": [{"id": "cheap-1"}]},
    ]
}


def make_step_runner(tmp_path, emitted, provider, config=None):
    runtime = ChildRuntime(
        registry=default_registry(),
        permissions=PermissionEngine(allow_rules=[]),
        cwd=tmp_path,
        emit=emitted.append,
        ask_permission=lambda tool, args: "deny",
        review_diff=lambda *a: False,
        provider_supplier=lambda: (provider, "m1", {"id": "p1"}),
        factory=lambda entry, key: provider,
        config_supplier=lambda: config if config is not None else CONFIG,
        api_key_env={},
        main_cancel=threading.Event(),
    )
    return lambda prompt, model: runtime.run({"prompt": prompt, "model": model})


def test_run_steps_sequentially_with_context_prefix(workflows_dir, tmp_path):
    write_workflow(workflows_dir, "report", GOOD)
    fake = FakeProvider(
        turns=[
            [TextDelta("结构总结：三层架构"), TextDelta("")],
            [TextDelta("发布说明 v2"), TextDelta("")],
        ]
    )
    emitted: list = []
    runner = make_step_runner(tmp_path, emitted, fake)
    report = WorkflowManager(workflows_dir).run("report", step_runner=runner)

    assert report["ok"] is True and report["completed"] == 2 and report["total"] == 2
    assert [s["ok"] for s in report["steps"]] == [True, True]
    assert report["steps"][0]["result"] == "结构总结：三层架构"
    assert report["steps"][1]["result"] == "发布说明 v2"
    assert report["steps"][1]["model"] == "p2/cheap-1"
    # 步骤 2 的 prompt 带步骤 1 结果前缀（FakeProvider 记录了两次请求）
    second_prompt = fake.requests[1].messages[0].blocks[0].text
    assert second_prompt.startswith("[Context — result of previous workflow step]")
    assert "结构总结：三层架构" in second_prompt
    assert second_prompt.endswith("基于上文写一份发布说明")
    # 两次请求都真实发出（FakeProvider 逐步消费）
    assert len(fake.requests) == 2


def test_run_failure_step_stops(workflows_dir):
    write_workflow(
        workflows_dir,
        "fails",
        "STEPS = [{'prompt': 'one'}, {'prompt': 'two'}, {'prompt': 'three'}]\n",
    )
    calls: list[str] = []

    def failing_runner(prompt, model):
        calls.append(prompt)
        if len(calls) == 2:
            return ToolOutcome(
                result={"error": "child_failed", "detail": "boom", "result": ""},
                is_error=True,
            )
        return ToolOutcome(result={"result": f"ok {len(calls)}", "stopReason": "end_turn"})

    report = WorkflowManager(workflows_dir).run("fails", step_runner=failing_runner)
    assert report["ok"] is False and report["failed_at"] == 1
    assert report["completed"] == 1 and len(report["steps"]) == 2
    assert report["steps"][1]["error"] == "child_failed: boom"
    assert len(calls) == 2  # 第三步不执行


def test_run_provider_error_step_fails_via_child_runtime(workflows_dir, tmp_path):
    write_workflow(workflows_dir, "authfail", "STEPS = [{'prompt': 'go'}]\n")
    fake = FakeProvider(turns=[[ProviderError("auth", "bad key")]])
    runner = make_step_runner(tmp_path, [], fake)
    report = WorkflowManager(workflows_dir).run("authfail", step_runner=runner)
    assert report["ok"] is False and report["failed_at"] == 0
    assert "child_failed" in report["steps"][0]["error"]


def test_run_cancel_event_stops_before_next_step(workflows_dir):
    write_workflow(workflows_dir, "cancelme", "STEPS = [{'prompt': 'a'}, {'prompt': 'b'}]\n")
    cancel = threading.Event()
    calls: list[str] = []

    def runner(prompt, model):
        calls.append(prompt)
        cancel.set()  # 第一步完成后取消
        return ToolOutcome(result={"result": "done", "stopReason": "end_turn"})

    report = WorkflowManager(workflows_dir).run("cancelme", step_runner=runner, cancel_event=cancel)
    assert report.get("cancelled") is True and report["ok"] is False
    assert report["completed"] == 1 and len(calls) == 1


def test_run_unknown_workflow_returns_error_report(workflows_dir):
    report = WorkflowManager(workflows_dir).run("ghost", step_runner=lambda p, m: ToolOutcome())
    assert report["ok"] is False and "not found" in report["error"]


def test_context_prefix_truncated(workflows_dir):
    write_workflow(workflows_dir, "long", "STEPS = [{'prompt': 'a'}, {'prompt': 'b'}]\n")
    prompts: list[str] = []

    def runner(prompt, model):
        prompts.append(prompt)
        return ToolOutcome(result={"result": "Z" * 20000 if len(prompts) == 1 else "b"})

    WorkflowManager(workflows_dir).run("long", step_runner=runner)
    assert len(prompts) == 2
    assert "Z" * 8000 in prompts[1] and "Z" * 8001 not in prompts[1]


# ---------------------------------------------------------------------------
# 服务层 control 往返
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self, provider):
        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter, provider_factory=lambda entry, key: provider
        )

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def results(self, command: str) -> list:
        return [
            f for f in self.frames()
            if f.get("type") == "result" and f.get("command") == command
        ]


def test_service_workflow_run_and_list(tmp_home):
    workflows = tmp_home / "workflows"
    workflows.mkdir()
    write_workflow(workflows, "daily", "STEPS = [{'prompt': 'step one'}, {'prompt': 'step two'}]\n")
    fake = FakeProvider(turns=[[TextDelta("结果一")], [TextDelta("结果二")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send({"type": "control", "command": "workflows_list"})
    listed = json.loads(harness.results("workflows_list")[-1]["detail"])
    assert listed == [{"name": "daily", "stepCount": 2, "models": []}]

    harness.send({"type": "control", "command": "workflow_run", "name": "daily"})
    assert wait_for(lambda: harness.results("workflow_run"))
    report = json.loads(harness.results("workflow_run")[-1]["detail"])
    assert report["ok"] is True and report["completed"] == 2
    assert [s["result"] for s in report["steps"]] == ["结果一", "结果二"]
    # 前步结果进入第二步 prompt（跨步骤上下文）
    assert "结果一" in fake.requests[1].messages[0].blocks[0].text


def test_service_workflow_run_unknown_name(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "workflow_run", "name": "ghost"})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert errors[-1]["kind"] == "not_found" and "ghost" in errors[-1]["message"]
    harness.send({"type": "control", "command": "workflow_run"})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert errors[-1]["kind"] == "protocol"


def test_service_user_message_busy_during_workflow(tmp_home):
    from zcode_py.llm.fake import Delay

    workflows = tmp_home / "workflows"
    workflows.mkdir()
    write_workflow(workflows, "slow", "STEPS = [{'prompt': 'slow step'}]\n")
    fake = FakeProvider(turns=[[Delay(0.5), TextDelta("late")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "workflow_run", "name": "slow"})

    assert wait_for(lambda: harness.service._workflow_thread is not None
                    and harness.service._workflow_thread.is_alive())
    harness.send({"type": "user_message", "text": "插队"})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert errors[-1]["kind"] == "busy" and "workflow" in errors[-1]["message"]

    assert wait_for(lambda: harness.results("workflow_run"), timeout=5.0)
    report = json.loads(harness.results("workflow_run")[-1]["detail"])
    assert report["ok"] is True
