"""M2 工具循环集成测试：loop ↔ ToolSession ↔ 服务层全链路（FakeProvider 离线）。

覆盖：schemas 注入、权限询问-阻塞-应答往返、deny 回灌、readonly 免批、
会话 always 记忆、危险命令压过 always、edit 的 diff_proposed→diff_review
闸门、tool_result(is_error) 回灌配对、服务级 checkpoint/rollback。
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time

from zcode_py.llm.base import (
    TextBlock,
    TextDelta,
    ToolCallDelta,
    ToolResultBlock,
    ToolUseBlock,
    TurnEnd,
    LLMAssistantMessage,
    LLMUserMessage,
)
from zcode_py.llm.fake import Delay, FakeProvider
from zcode_py.loop import AgentLoop
from zcode_py.permissions import PermissionEngine
from zcode_py.perf import PerfTracker
from zcode_py.protocol import DiffProposed, MessageComplete, PermissionRequest
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.sessions import SessionStore
from zcode_py.tools import ToolSession, default_registry


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# AgentLoop 直连层
# ---------------------------------------------------------------------------


def make_session(ws, emitted, *, ask=None, review=None, allow_rules=None, cancel=None):
    return ToolSession(
        registry=default_registry(),
        permissions=PermissionEngine(allow_rules=allow_rules or []),
        cwd=ws,
        emit=emitted.append,
        ask_permission=ask,
        review_diff=review,
        cancel_event=cancel,
    )


def make_loop(provider, emitted, session=None, **kwargs):
    loop = AgentLoop(
        provider=provider,
        model="test-model",
        emit=emitted.append,
        perf=PerfTracker(),
        tool_session=session,
        **kwargs,
    )
    return loop


def tool_turn(call_id, name, args):
    return [
        ToolCallDelta(call_id=call_id, name=name, args_delta=""),
        ToolCallDelta(call_id=call_id, args_delta=json.dumps(args)),
        TurnEnd("tool_use"),
    ]


def test_schemas_injected_into_request(tmp_path):
    provider = FakeProvider(turns=[[TextDelta("ok")]])
    emitted = []
    session = make_session(tmp_path, emitted)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("hi")
    names = [t["function"]["name"] for t in provider.requests[0].tools]
    assert "read" in names and "bash" in names and "edit" in names


def test_readonly_tool_runs_without_asking(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    asks = []
    provider = FakeProvider(turns=[
        tool_turn("t1", "read", {"path": "a.txt"}),
        [TextDelta("done")],
    ])
    emitted = []
    session = make_session(tmp_path, emitted, ask=lambda tool, args: asks.append(tool) or "deny")
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("read it")
    assert asks == []  # readonly 免批
    finished = [m for m in emitted if type(m).__name__ == "ToolCall" and m.state == "finished"]
    assert finished[0].result["content"] == "hello"
    # tool_result 回灌：第二次请求末尾是 user 消息携带结果
    second = provider.requests[1]
    block = second.messages[-1].blocks[0]
    assert isinstance(block, ToolResultBlock) and block.tool_use_id == "t1"
    assert block.is_error is False


def test_write_asks_and_allow_executes(tmp_path):
    decisions = ["allow"]
    provider = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "out.txt", "content": "data"}),
        [TextDelta("written")],
    ])
    emitted = []

    # ask 回调契约（与服务层 _ask_permission 一致）：广播 permission_request
    # 再给决定——发送/等待职责在回调侧，ToolSession 只做判定编排
    def ask(tool, args):
        emitted.append(PermissionRequest(id="perm_1", tool=tool, args=args))
        return decisions.pop(0)

    session = make_session(tmp_path, emitted, ask=ask)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("write it")
    req = next(m for m in emitted if isinstance(m, PermissionRequest))
    assert req.tool == "write" and req.args["path"] == "out.txt"
    assert (tmp_path / "out.txt").read_text() == "data"


def test_tool_use_allow_execute_feed_back_final_reply_chain(tmp_path):
    """端到端全链路（任务书验收口径）：FakeProvider 吐 tool_use → 权限卡
    allow → 工具执行落盘 → tool_result 回灌进第二次模型请求 → 最终回复。"""
    provider = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "e2e.txt", "content": "payload"}),
        [TextDelta("已写入 e2e.txt")],
    ])
    emitted = []

    def ask(tool, args):
        emitted.append(PermissionRequest(id="p1", tool=tool, args=args))
        return "allow"

    session = make_session(tmp_path, emitted, ask=ask)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("帮我写文件")

    # ① FakeProvider 吐了 tool_use → ② 权限卡广播且被 allow
    assert next(m for m in emitted if isinstance(m, PermissionRequest)).tool == "write"
    # ③ 工具真实执行：文件落盘，ToolCall finished 帧带结果
    assert (tmp_path / "e2e.txt").read_text() == "payload"
    finished = [m for m in emitted if type(m).__name__ == "ToolCall" and m.state == "finished"]
    assert finished[0].result["status"] == "written"
    # ④ 回灌：第二次模型请求上下文 = user → assistant(tool_use) → user(tool_result)
    assert len(provider.requests) == 2
    second = provider.requests[1]
    assert isinstance(second.messages[-2].blocks[-1], ToolUseBlock)
    fed = second.messages[-1].blocks[0]
    assert isinstance(fed, ToolResultBlock)
    assert fed.tool_use_id == "t1" and fed.is_error is False
    assert fed.content["status"] == "written"
    # ⑤ 最终回复：message_complete 带第二轮文本，stopReason end_turn
    complete = next(m for m in emitted if isinstance(m, MessageComplete))
    assert complete.text == "已写入 e2e.txt"
    assert complete.stopReason == "end_turn"


def test_permission_deny_returns_error_result(tmp_path):
    provider = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "out.txt", "content": "x"}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(tmp_path, emitted, ask=lambda tool, args: "deny")
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("write")
    assert not (tmp_path / "out.txt").exists()  # 未写盘
    second = provider.requests[1]
    block = second.messages[-1].blocks[0]
    assert block.is_error is True
    assert block.content["error"] == "permission_denied"


def test_missing_ask_callback_fails_closed(tmp_path):
    provider = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "out.txt", "content": "x"}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(tmp_path, emitted, ask=None)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("write")
    assert not (tmp_path / "out.txt").exists()
    second = provider.requests[1]
    assert second.messages[-1].blocks[0].content["error"] == "permission_denied"


def test_session_always_remembered_across_calls(tmp_path):
    decisions = ["always", "allow"]  # 第一次 always，第二次本应再问但记忆生效
    asked = []
    provider = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "a.txt", "content": "1"}),
        tool_turn("t2", "write", {"path": "a.txt", "content": "2"}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(
        tmp_path, emitted,
        ask=lambda tool, args: asked.append(args["path"]) or decisions.pop(0),
    )
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("write twice")
    assert asked == ["a.txt"]  # 第二次同主体未再问
    assert (tmp_path / "a.txt").read_text() == "2"


def test_danger_beats_session_always_in_loop(tmp_path):
    """用户对 bash 命令族选了 always，危险命令仍然弹确认（PLAN §9.3）。"""
    decisions = ["always", "allow"]
    asked = []
    provider = FakeProvider(turns=[
        tool_turn("t1", "bash", {"command": "git status"}),
        tool_turn("t2", "bash", {"command": "rm -rf /"}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(
        tmp_path, emitted,
        ask=lambda tool, args: asked.append(args["command"]) or decisions.pop(0),
    )
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("run")
    # 两次都问了：git status 是 always（但 bash 执行核心缺后端，返回结构化
    # backend_unavailable——不影响权限断言）；rm -rf 压过 always 仍要问
    assert asked == ["git status", "rm -rf /"]


def test_unknown_tool_structured_error(tmp_path):
    provider = FakeProvider(turns=[
        tool_turn("t1", "nonexistent_tool", {}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(tmp_path, emitted)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("x")
    second = provider.requests[1]
    assert second.messages[-1].blocks[0].content["error"] == "unknown_tool"


def test_edit_diff_gate_in_loop(tmp_path):
    """edit：广播 diff_proposed → accepted 才写盘；拒绝则文件不动。"""
    (tmp_path / "f.txt").write_text("aaa\nbbb\n", encoding="utf-8")
    reviews = []
    counter = [0]

    # review 回调契约（与服务层 _review_diff 一致）：广播 diff_proposed 再给结论
    def review(path, old, new):
        counter[0] += 1
        emitted.append(DiffProposed(id=f"diff_{counter[0]}", file=path, oldText=old, newText=new))
        reviews.append((path, old, new))
        return len(reviews) == 1  # 第一次接受，第二次拒绝

    provider = FakeProvider(turns=[
        tool_turn("e1", "edit", {"path": "f.txt", "old_str": "aaa", "new_str": "XXX"}),
        tool_turn("e2", "edit", {"path": "f.txt", "old_str": "XXX", "new_str": "YYY"}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(tmp_path, emitted, review=review)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("edit twice")

    proposals = [m for m in emitted if isinstance(m, DiffProposed)]
    assert len(proposals) == 2
    assert proposals[0].oldText == "aaa" and proposals[0].newText == "XXX"
    text = (tmp_path / "f.txt").read_text()
    assert "XXX" in text and "YYY" not in text  # 第二次被拒：文件停在第一次的状态
    # 拒绝结果以 is_error 回灌
    third_request_blocks = provider.requests[2].messages[-1].blocks
    rejected = [b for b in third_request_blocks if isinstance(b, ToolResultBlock)]
    assert any(b.content.get("error") == "diff_rejected" for b in rejected)


def test_privacy_warning_surfaces_on_tool_event(tmp_path):
    (tmp_path / "id_rsa").write_text("PRIVATE\n", encoding="utf-8")
    provider = FakeProvider(turns=[
        tool_turn("t1", "read", {"path": "id_rsa"}),
        [TextDelta("ok")],
    ])
    emitted = []
    session = make_session(tmp_path, emitted)
    loop = make_loop(provider, emitted, session=session)
    loop.run_user_text("read key")
    finished = [m for m in emitted if type(m).__name__ == "ToolCall" and m.state == "finished"]
    assert finished[0].warning and "id_rsa" in finished[0].warning


def test_cancel_mid_permission_denies_and_pairs_results(tmp_path):
    cancel = threading.Event()
    release = threading.Event()

    def slow_ask(tool, args):
        release.wait(timeout=5)
        return "allow"

    provider = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "x.txt", "content": "1"}),
        tool_turn("t2", "write", {"path": "y.txt", "content": "2"}),
        [TextDelta("ok")],
    ])
    emitted = []

    def canceller():
        cancel.set()
        release.set()

    session = make_session(tmp_path, emitted, ask=slow_ask, cancel=cancel)
    loop = make_loop(provider, emitted, session=session, cancel_event=cancel)
    threading.Thread(target=canceller, daemon=True).start()
    loop.run_user_text("x")
    # 取消回合：每个 tool_use 仍有配对 tool_result（Anthropic 回放约束）
    complete = next(m for m in emitted if type(m).__name__ == "MessageComplete")
    assert complete.stopReason == "cancelled"
    assert not (tmp_path / "x.txt").exists() and not (tmp_path / "y.txt").exists()


# ---------------------------------------------------------------------------
# 服务层全链路（stdio 帧级）：权限往返 / diff 审阅往返 / checkpoint+rollback
# ---------------------------------------------------------------------------


class ServiceHarness:
    def __init__(self, provider, workspace):
        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter, provider_factory=lambda entry, key: provider
        )
        self.workspace = workspace

    def send(self, payload):
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def frames_of_type(self, t):
        return [f for f in self.frames() if f.get("type") == t]

    def wait_frames(self, t, count=1, timeout=5.0):
        return wait_for(lambda: len(self.frames_of_type(t)) >= count, timeout=timeout)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15, check=True
    )


def test_service_permission_roundtrip(tmp_home, tmp_path):
    fake = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "svc.txt", "content": "from service"}),
        [TextDelta("已写入")],
    ])
    harness = ServiceHarness(fake, tmp_path)
    harness.send({"type": "init", "workspaceRoots": [str(tmp_path)],
                  "settings": {"permissionTimeoutSec": 5}})
    harness.send({"type": "user_message", "text": "写文件"})
    assert harness.wait_frames("permission_request")
    req = harness.frames_of_type("permission_request")[-1]
    assert req["tool"] == "write"

    harness.send({"type": "permission_response", "id": req["id"], "decision": "allow"})
    assert harness.wait_frames("message_complete")
    assert (tmp_path / "svc.txt").read_text() == "from service"
    finished = [f for f in harness.frames_of_type("tool_call") if f["state"] == "finished"]
    assert finished[0]["result"]["status"] == "written"
    # 权限往返已入转录（回放可见）
    transcript_dir = tmp_home / "sessions"
    assert any(
        "permission_request" in p.read_text(encoding="utf-8")
        for p in transcript_dir.glob("*/transcript.jsonl")
    )


def test_service_edit_diff_review_roundtrip(tmp_home, tmp_path):
    (tmp_path / "code.py").write_text("value = 1\n", encoding="utf-8")
    fake = FakeProvider(turns=[
        tool_turn("e1", "edit", {"path": "code.py", "old_str": "value = 1",
                                 "new_str": "value = 42"}),
        [TextDelta("改好了")],
    ])
    harness = ServiceHarness(fake, tmp_path)
    harness.send({"type": "init", "workspaceRoots": [str(tmp_path)]})
    harness.send({"type": "user_message", "text": "改一下"})
    assert harness.wait_frames("diff_proposed")
    prop = harness.frames_of_type("diff_proposed")[-1]
    assert prop["file"] == "code.py" and prop["oldText"] == "value = 1"

    # 先拒绝：文件不动
    harness.send({"type": "diff_review", "id": prop["id"], "accepted": False})
    assert harness.wait_frames("message_complete")
    assert (tmp_path / "code.py").read_text() == "value = 1\n"

    # 再来一次，接受：写盘
    fake.turns.append(tool_turn("e2", "edit", {"path": "code.py", "old_str": "value = 1",
                                               "new_str": "value = 42"}))
    fake.turns.append([TextDelta("done")])
    harness.send({"type": "user_message", "text": "再改"})
    assert harness.wait_frames("diff_proposed", count=2)
    prop2 = harness.frames_of_type("diff_proposed")[-1]
    harness.send({"type": "diff_review", "id": prop2["id"], "accepted": True})
    assert harness.wait_frames("message_complete", count=2)
    assert (tmp_path / "code.py").read_text() == "value = 42\n"


def test_service_permission_timeout_denies(tmp_home, tmp_path):
    fake = FakeProvider(turns=[
        tool_turn("t1", "write", {"path": "t.txt", "content": "x"}),
        [TextDelta("ok")],
    ])
    harness = ServiceHarness(fake, tmp_path)
    harness.send({"type": "init", "workspaceRoots": [str(tmp_path)],
                  "settings": {"permissionTimeoutSec": 1}})
    harness.send({"type": "user_message", "text": "写"})
    assert harness.wait_frames("message_complete", timeout=8)
    assert not (tmp_path / "t.txt").exists()  # 超时=拒绝，未写盘
    finished = [f for f in harness.frames_of_type("tool_call") if f["state"] == "finished"]
    assert finished[0]["result"]["error"] == "permission_denied"


def test_service_checkpoint_and_rollback(tmp_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "--allow-empty", "-q", "-m", "init")
    (repo / "app.txt").write_text("good\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "app")

    fake = FakeProvider(turns=[[TextDelta("ok")]])
    harness = ServiceHarness(fake, repo)
    harness.send({"type": "init", "workspaceRoots": [str(repo)]})
    harness.send({"type": "user_message", "text": "第一轮"})
    assert harness.wait_frames("checkpoint")
    assert harness.wait_frames("usage")  # 回合彻底收尾（usage 是 _run_turn 最后的帧）
    assert wait_for(lambda: not harness.service._turn_alive())
    ck = harness.frames_of_type("checkpoint")[-1]
    assert ck["sha"] and ck["atMessageId"].startswith("um_")

    # 模拟 Agent 误改（绕过工具直接写）
    (repo / "app.txt").write_text("ruined\n", encoding="utf-8")
    harness.send({"type": "rollback", "checkpointId": ck["id"]})
    assert harness.wait_frames("rollback_result")
    assert harness.frames_of_type("rollback_result")[-1]["ok"] is True
    assert (repo / "app.txt").read_text() == "good\n"  # 已回滚

    # 快照与回滚事件都进了转录
    transcript_dir = tmp_home / "sessions"
    found = [
        p for p in transcript_dir.glob("*/transcript.jsonl")
        if "checkpoint" in p.read_text(encoding="utf-8")
        and "rollback_result" in p.read_text(encoding="utf-8")
    ]
    assert found


# ---------------------------------------------------------------------------
# 配对兜底：tool_use/tool_result 断裂自愈（独立评审中危 #5 的两个场景）
# ---------------------------------------------------------------------------


def assert_valid_pairing(messages):
    """无连续同角色；每个 assistant tool_use 在紧随的 user 消息里有配对结果。"""
    roles = ["u" if isinstance(m, LLMUserMessage) else "a" for m in messages]
    for i in range(1, len(roles)):
        assert roles[i] != roles[i - 1], f"consecutive role {roles[i]!r} at index {i}"
    for i, msg in enumerate(messages):
        if not isinstance(msg, LLMAssistantMessage):
            continue
        ids = [b.id for b in msg.blocks if isinstance(b, ToolUseBlock)]
        if not ids:
            continue
        assert i + 1 < len(messages), f"dangling tool_use {ids} at last message"
        nxt = messages[i + 1]
        assert isinstance(nxt, LLMUserMessage)
        result_ids = {b.tool_use_id for b in nxt.blocks if isinstance(b, ToolResultBlock)}
        for cid in ids:
            assert cid in result_ids, f"tool_use {cid} has no paired tool_result"


def test_end_turn_with_tool_calls_synthesizes_results(tmp_path):
    """场景A：模型吐 tool_call 但终态 end_turn → 合成 not_executed 结果，
    下一次请求不会被 Anthropic 端点以配对断裂 400 拒绝。"""
    provider = FakeProvider(turns=[
        [
            ToolCallDelta(call_id="c1", name="read",
                          args_delta=json.dumps({"path": "a.txt"})),
            TurnEnd("end_turn"),
        ],
    ])
    emitted = []
    loop = make_loop(provider, emitted)
    loop.run_user_text("hi")
    assert_valid_pairing(loop.messages)
    last = loop.messages[-1]
    assert isinstance(last, LLMUserMessage)
    synth = [b for b in last.blocks if isinstance(b, ToolResultBlock)]
    assert synth and synth[0].is_error
    assert synth[0].content["error"] == "not_executed"
    # 下一轮 user 文本并入 tool_result user 消息（不产生连续 user），配对仍完好
    provider.turns.append([TextDelta("ok again"), TurnEnd("end_turn")])
    loop.run_user_text("next question")
    assert_valid_pairing(loop.messages)
    assert any(
        isinstance(b, TextBlock) and b.text == "next question"
        for m in loop.messages for b in m.blocks
    )


def test_cancel_after_tool_deltas_pairs_results(tmp_path):
    """场景B：工具增量到达后取消 → 同样合成配对结果，会话可继续。"""
    provider = FakeProvider(turns=[
        [ToolCallDelta(call_id="c2", name="grep", args_delta="{}"), Delay(10.0)],
    ])
    emitted = []
    cancel = threading.Event()
    loop = make_loop(provider, emitted, cancel_event=cancel)
    timer = threading.Timer(0.15, cancel.set)
    timer.start()
    try:
        loop.run_user_text("hi")
    finally:
        timer.cancel()
    assert_valid_pairing(loop.messages)
    synth = [b for b in loop.messages[-1].blocks if isinstance(b, ToolResultBlock)]
    assert synth and synth[0].content["error"] == "not_executed"
