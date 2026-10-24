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
        P.Rollback(checkpointId="ck1"),
        P.GitCommit(repoRoot="/r", scope="staged", push=False),
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
        P.EnvInfo(hardware={"cpu": "M4"}, workspaceLangs=["python"]),
        P.Compaction(layer="L2", report={"removed": 3}),
        P.Checkpoint(id="ck1", sha="abc", atMessageId="m1"),
        P.RollbackResult(ok=True),
        P.SessionsList(sessions=[{"sessionId": "s1", "title": "t"}]),
        P.Usage(tokens=42, byModel={"glm-5.3": {"input": 30, "output": 12}}),
        P.PingResult(ok=True, latencyMs=412),
        P.PingResult(ok=False, errorKind="auth", message="bad key"),
        P.GitResult(commitSha="a1", remoteUrl="https://x", commitUrl="https://y", prUrl=None, error=None),
        P.ImageArtifact(path="/tmp/a.png"),
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
        "git_result", "image_artifact", "error", "result",
    }
    assert set(P._REGISTRY) == expected
