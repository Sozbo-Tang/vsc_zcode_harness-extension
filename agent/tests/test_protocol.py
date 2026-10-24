"""协议 v1 往返测试：锁定 TS/Python 字段一致（PLAN §5、§8）。"""

from __future__ import annotations

import pytest

from zcode_py import protocol as P


def _samples() -> list:
    ctx = P.MessageContext(selection="print(1)", fileRefs=["src/a.py"], terminalSel="err", problems="P1")
    return [
        P.Init(workspaceRoots=["/a", "/b"], settings={"x": 1}, apiKeyEnv={"glm": "ZCODE_PY_API_KEY_GLM"}),
        P.Init(),
        P.UserMessage(text="hi"),
        P.UserMessage(text="hi", context=ctx),
        P.UserMessage(text="hi", context=P.MessageContext()),
        P.PermissionResponse(id="p1", decision="allow"),
        P.DiffReview(id="d1", accepted=True),
        P.DiffReview(id="d2", accepted=False),
        P.Control(command="new_session"),
        P.Control(command="resume", sessionId="s1"),
        P.Control(command="telegraph", level="high"),
        P.Control(command="switch_model", providerId="openai", modelId="gpt-5.2"),
        # M4：fork/pin/export/set_root 及其扩展参数（None 省略 / False 保留）
        P.Control(command="fork", sessionId="s1", atMessageId="um_1"),
        P.Control(command="pin", sessionId="s1", pinned=True),
        P.Control(command="pin", sessionId="s1", pinned=False),
        P.Control(command="export", sessionId="s1"),
        P.Control(command="set_root", rootIndex=1),
        P.Control(command="set_root", rootPath="/w/b"),
        # M9：后台任务 / 定时任务 / 工作流载荷（taskCommand 承载 shell 命令串）
        P.Control(command="background_start", taskCommand="python train.py", name="train"),
        P.Control(command="background_start", taskCommand="sleep 5", timeoutSec=60.5),
        P.Control(command="task_stop", taskId="bg_ab12"),
        P.Control(command="cron_add", schedule="*/5 * * * *",
                  action={"type": "prompt", "text": "总结进展"}),
        P.Control(command="cron_add", schedule="0 9 * * 1-5",
                  action={"type": "task", "command": "make report"}, enabled=False),
        P.Control(command="cron_remove", cronId="cron_ab12"),
        P.Control(command="cron_toggle", cronId="cron_ab12", enabled=True),
        P.Control(command="workflow_run", name="daily-report"),
        P.Control(command="workflows_list"),
        # R17/R22：git 流载荷（git_preview 的 repoRoot/scope、git_pr 的
        # title/body；GitCommit.message 用户编辑后的完整提交信息）
        P.Control(command="git_preview", repoRoot="/w/proj", scope="staged"),
        P.Control(command="git_preview", repoRoot="/w/proj", scope="all"),
        P.Control(command="git_pr", repoRoot="/w/proj"),
        P.Control(command="git_pr", repoRoot="/w/proj", title="feat: x", body="正文\n两行"),
        P.Rollback(checkpointId="ck1"),
        P.GitCommit(repoRoot="/r", scope="staged", push=False),
        P.GitCommit(repoRoot="/r", scope="all", push=True, message="feat: x\n\n- M a.py"),
        P.SessionStarted(sessionId="s1"),
        P.ThinkingDelta(text="思考"),
        P.StreamDelta(text="正文"),
        P.MessageComplete(),
        P.MessageComplete(stopReason="end_turn", text="答案", inputTokens=10, outputTokens=5),
        P.ToolCall(callId="t1", name="read"),
        P.ToolCall(callId="t1", name="read", args={"path": "a"}, state="finished", result={"ok": 1}, elapsedMs=3),
        P.PermissionRequest(id="p1", tool="bash", args={"command": "ls"}),
        P.DiffProposed(id="d1", file="a.py", oldText="a", newText="b"),
        P.VisionRelay(model="gpt-5.2-vision", preset="ocr", description="描述"),
        P.TelegraphState(levels=[{"id": "off", "label": "关闭", "param": None, "ok": True}], current="off", paramTranslation={}),
        P.Perf(phase="streaming", ttftMs=120.5, tokPerSec=41.2),
        P.Perf(phase="done", lastTurn={"ttftMs": 120.5, "avgTokPerSec": 38.0}),
        # R19 会话统计：perf 帧带 model 分组键（None 编码省略 / 非空保留）
        P.Perf(phase="streaming", ttftMs=90.0, tokPerSec=55.0, model="glm-5.3"),
        P.Perf(phase="done", lastTurn={"ttftMs": 90.0, "avgTokPerSec": 50.0}, model="gpt-5.2"),
        P.EnvInfo(hardware={"cpu": "M4"}, workspaceLangs=["python"]),
        P.Compaction(layer="L2", report={"removed": 3}),
        P.Checkpoint(id="ck1", sha="abc", atMessageId="m1"),
        P.RollbackResult(ok=True),
        P.SessionsList(sessions=[{"sessionId": "s1", "title": "t"}]),
        P.Usage(tokens=42, byModel={"glm-5.3": {"input": 30, "output": 12}}),
        P.PingResult(ok=True, latencyMs=412),
        P.PingResult(ok=False, errorKind="auth", message="bad key"),
        P.GitResult(commitSha="a1", remoteUrl="https://x", commitUrl="https://y", prUrl=None, error=None),
        P.GitResult(prUrl="https://github.com/o/r/pull/7", hint="已设置 upstream 并推送"),
        P.ImageArtifact(path="/tmp/a.png"),
        # M9（R3）：后台任务帧
        P.TaskStarted(taskId="bg_1", name="train", command="python train.py"),
        P.TaskOutput(taskId="bg_1", tail="epoch 34/100"),
        P.TaskFinished(taskId="bg_1", exitCode=0),
        P.TaskFinished(taskId="bg_2", exitCode=-1),
        P.Error(kind="auth", message="x"),
        P.Result(ok=True, command="telegraph", detail="downshifted"),
        # M4：export 回执携带 Markdown 全文（保存落盘由 UI 决定）
        P.Result(ok=True, command="export", markdown="# 会话\n\n## 用户\n"),
        # M2：隐私护栏 warning 标记（None 编码省略 / 非 None 往返保留）
        P.ToolCall(callId="t3", name="read", state="finished",
                   result={"content": "..."}, warning="privacy: '*.pem' matches sensitive file pattern"),
    ]


def test_round_trip_all_message_types():
    for msg in _samples():
        encoded = P.encode_message(msg)
        assert encoded["type"]  # type 总在
        decoded = P.decode_message(encoded)
        assert decoded == msg, f"round trip mismatch for {type(msg).__name__}"


def test_line_round_trip():
    msg = P.UserMessage(text="héllo", context=P.MessageContext(fileRefs=["a"]))
    assert P.decode_line(P.encode_line(msg)) == msg


def test_encode_omits_none_keeps_false():
    encoded = P.encode_message(P.DiffReview(id="d", accepted=False))
    assert encoded == {"type": "diff_review", "id": "d", "accepted": False}
    encoded = P.encode_message(P.MessageComplete())
    assert encoded == {"type": "message_complete"}


def test_decode_rejects_unknown_type():
    with pytest.raises(P.ProtocolError, match="unknown message type"):
        P.decode_message({"type": "nope"})


def test_decode_rejects_missing_required_field():
    with pytest.raises(P.ProtocolError, match="missing required field 'text'"):
        P.decode_message({"type": "user_message"})


def test_decode_rejects_bad_type_value():
    with pytest.raises(P.ProtocolError, match="must be"):
        P.decode_message({"type": "user_message", "text": 123})


def test_decode_rejects_invalid_enums():
    with pytest.raises(P.ProtocolError, match="command"):
        P.decode_message({"type": "control", "command": "explode"})
    with pytest.raises(P.ProtocolError, match="decision"):
        P.decode_message({"type": "permission_response", "id": "p", "decision": "maybe"})
    with pytest.raises(P.ProtocolError, match="state"):
        P.decode_message({"type": "tool_call", "callId": "c", "name": "n", "state": "done"})
    with pytest.raises(P.ProtocolError, match="phase"):
        P.decode_message({"type": "perf", "phase": "idle"})


def test_decode_ignores_unknown_fields():
    decoded = P.decode_message({"type": "user_message", "text": "hi", "futureField": [1, 2]})
    assert decoded == P.UserMessage(text="hi")


def test_control_and_decision_enums_round_trip():
    """受限枚举全集逐一可解码（command / decision 各自声明的每个取值）。"""
    for cmd in P.CONTROL_COMMANDS:
        decoded = P.decode_message({"type": "control", "command": cmd})
        assert decoded == P.Control(command=cmd)
    for decision in P.PERMISSION_DECISIONS:
        decoded = P.decode_message({"type": "permission_response", "id": "p", "decision": decision})
        assert decoded == P.PermissionResponse(id="p", decision=decision)


def test_decode_line_rejects_malformed_json():
    with pytest.raises(P.ProtocolError, match="invalid JSON"):
        P.decode_line("{not json")


def test_context_nested_round_trip():
    payload = {
        "type": "user_message",
        "text": "q",
        "context": {"selection": "x=y", "fileRefs": ["a", "b"]},
    }
    decoded = P.decode_message(payload)
    assert decoded.context == P.MessageContext(selection="x=y", fileRefs=["a", "b"])


def test_registry_covers_plan_section5():
    expected = {
        "init", "user_message", "permission_response", "diff_review", "control",
        "rollback", "git_commit", "session_started", "thinking_delta", "stream_delta",
        "message_complete", "tool_call", "permission_request", "diff_proposed",
        "vision_relay", "telegraph_state", "perf", "env_info", "compaction",
        "checkpoint", "rollback_result", "sessions_list", "usage", "ping_result",
        "git_result", "image_artifact", "task_started", "task_output",
        "task_finished", "error", "result",
    }
    assert set(P._REGISTRY) == expected
