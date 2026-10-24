"""stdio 服务（ZcodeService / main）：init、control 全集、取消、忙碌、恢复、
Ping、转录落盘——全部离线（FakeProvider）。"""

from __future__ import annotations

import io
import json
import threading
import time

from zcode_py.llm.base import ProviderError, TextBlock, TextDelta
from zcode_py.llm.fake import FakeProvider, Wait
from zcode_py.main import StdioEmitter, ZcodeService, main
from zcode_py.sessions import SessionStore


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# user_message.context 渲染（M3 编辑器联动的 Python 侧锁定）
# ---------------------------------------------------------------------------


def test_decorate_user_message_all_context_fields():
    """selection/fileRefs/terminalSel/problems 四类附件按序拼装进提示词。"""
    from zcode_py.main import _decorate_user_message
    from zcode_py.protocol import MessageContext, UserMessage

    msg = UserMessage(
        text="请看上面的问题",
        context=MessageContext(
            selection="print('hi')",
            fileRefs=["src/a.py"],
            terminalSel="Traceback (most recent call last):",
            problems="[error] src/a.py:12: boom",
        ),
    )
    out = _decorate_user_message(msg)
    assert out.index("Referenced files: src/a.py") < out.index("Selected code:")
    assert "```\nprint('hi')\n```" in out
    assert "Terminal selection:" in out and "Traceback" in out
    assert "Problems:" in out and "[error] src/a.py:12" in out
    assert out.endswith("请看上面的问题")


def test_decorate_user_message_empty_context():
    from zcode_py.main import _decorate_user_message
    from zcode_py.protocol import UserMessage

    assert _decorate_user_message(UserMessage(text="你好")) == "你好"


class Harness:
    """直连 ZcodeService（stdout 为 StringIO），帧用 dict 断言。"""

    def __init__(self, provider: FakeProvider):
        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter,
            provider_factory=lambda entry, key: provider,
        )

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def frames_of_type(self, type_name: str) -> list:
        return [f for f in self.frames() if f.get("type") == type_name]

    def wait_frames(self, type_name: str, count=1) -> bool:
        return wait_for(lambda: len(self.frames_of_type(type_name)) >= count)


def test_init_starts_session_and_broadcasts_telegraph(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": ["/w"]})

    frames = harness.frames()
    assert frames[0]["type"] == "session_started"
    session_id = frames[0]["sessionId"]
    telegraph = next(f for f in frames if f["type"] == "telegraph_state")
    assert telegraph["current"] == "max"  # glm-5.3 默认档
    assert telegraph["paramTranslation"] == {"thinking": {"type": "enabled"}}
    ids = [lv["id"] for lv in telegraph["levels"]]
    assert ids == ["off", "min", "low", "medium", "high", "max"]
    assert next(f for f in frames if f["type"] == "sessions_list")["sessions"]

    # meta.json + transcript.jsonl 已落盘且含 out 帧
    transcript = (tmp_home / "sessions" / session_id / "transcript.jsonl").read_text()
    assert "telegraph_state" in transcript
    meta = json.loads((tmp_home / "sessions" / session_id / "meta.json").read_text())
    assert meta["providerId"] == "glm" and meta["modelId"] == "glm-5.3"


def test_user_message_streams_and_persists_inbound(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("回复")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    session_id = harness.frames()[0]["sessionId"]

    harness.send({"type": "user_message", "text": "问题", "context": {"fileRefs": ["a.py"]}})
    assert harness.wait_frames("message_complete")

    complete = harness.frames_of_type("message_complete")[0]
    assert complete["text"] == "回复" and complete["stopReason"] == "end_turn"
    # 请求上下文带 @引用 拼装
    assert "Referenced files: a.py" in fake.requests[0].messages[0].blocks[0].text
    # 入站帧也进了转录（dir=in）
    transcript = (tmp_home / "sessions" / session_id / "transcript.jsonl").read_text().splitlines()
    inbound = [json.loads(line) for line in transcript if json.loads(line)["dir"] == "in"]
    assert inbound[0]["event"]["text"] == "问题"


def test_telegraph_control_downshifts_on_glm(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "telegraph", "level": "off"})

    state = harness.frames_of_type("telegraph_state")[-1]
    assert state["current"] == "max"  # GLM-5.3 off 不可用 → 降档
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "telegraph" and "downshifted" in (result.get("detail") or "")


def test_switch_model_auto_downshift_and_translation(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("ok")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    assert harness.frames_of_type("telegraph_state")[0]["current"] == "max"  # glm 默认档

    # glm(仅 max) → gpt(max 可用)：保持 max
    harness.send({"type": "control", "command": "switch_model", "providerId": "openai", "modelId": "gpt-5.2"})
    state = harness.frames_of_type("telegraph_state")[-1]
    assert state["current"] == "max"
    assert state["paramTranslation"] == {"reasoning_effort": "max"}

    # gpt(max) → claude(无 max 档)：自动降档到 high
    harness.send({"type": "control", "command": "switch_model", "providerId": "anthropic", "modelId": "claude-sonnet-4-5"})
    state = harness.frames_of_type("telegraph_state")[-1]
    assert state["current"] == "high"
    assert state["paramTranslation"] == {"thinking": {"type": "enabled", "budget_tokens": 65536}}

    # 显式降到 medium 后，thinking 参数进入下一次模型请求
    harness.send({"type": "control", "command": "telegraph", "level": "medium"})
    harness.send({"type": "user_message", "text": "q"})
    assert harness.wait_frames("message_complete")
    assert fake.requests[-1].thinking == {"thinking": {"type": "enabled", "budget_tokens": 24576}}


def test_switch_model_unknown_provider(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "switch_model", "providerId": "nope"})
    error = harness.frames_of_type("error")[-1]
    assert error["kind"] == "config" and "unknown provider" in error["message"]


def test_ping_ok_and_error_classification(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("pong")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "ping"})
    ping_frame = harness.frames_of_type("ping_result")[-1]
    assert ping_frame["ok"] is True
    assert ping_frame["latencyMs"] >= 0
    assert ping_frame.get("errorKind") is None  # None 字段编码时省略

    failing = FakeProvider(turns=[[ProviderError("auth", "bad key")]])
    harness2 = Harness(failing)
    harness2.send({"type": "init", "workspaceRoots": []})
    harness2.send({"type": "control", "command": "ping", "providerId": "anthropic", "modelId": "claude-sonnet-4-5"})
    ping_frame = harness2.frames_of_type("ping_result")[-1]
    assert ping_frame["ok"] is False
    assert ping_frame["errorKind"] == "auth"
    assert ping_frame["message"] == "bad key"


def test_new_session_and_resume_replays_transcript(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("第一轮")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    first_session = harness.frames()[0]["sessionId"]
    harness.send({"type": "user_message", "text": "问题一"})
    assert harness.wait_frames("usage")

    harness.send({"type": "control", "command": "new_session"})
    second_session = harness.frames_of_type("session_started")[-1]["sessionId"]
    assert second_session != first_session

    harness.send({"type": "control", "command": "resume", "sessionId": first_session})
    # 重放顺序：init 的 → new_session 的 → resume 回执 → 重放出的原始 session_started
    starts = [f["sessionId"] for f in harness.frames_of_type("session_started")]
    assert starts == [first_session, second_session, first_session, first_session]
    assert any(f["type"] == "message_complete" and f.get("text") == "第一轮" for f in harness.frames())
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "resume"

    # 恢复后继续对话：上下文包含历史问答
    fake.turns.append([TextDelta("第二轮")])
    harness.send({"type": "user_message", "text": "问题二"})
    assert harness.wait_frames("message_complete", count=2)
    last_request = fake.requests[-1]
    texts = [b.text for m in last_request.messages for b in m.blocks if isinstance(b, TextBlock)]
    assert texts == ["问题一", "第一轮", "问题二"]


def test_resume_unknown_session(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "resume", "sessionId": "ghost"})
    assert harness.frames_of_type("error")[-1]["kind"] == "not_found"


def test_cancel_mid_turn(tmp_home):
    release = threading.Event()
    fake = FakeProvider(turns=[[TextDelta("a"), Wait(release, timeout=3.0), TextDelta("b")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "user_message", "text": "q"})
    assert wait_for(lambda: any(f["type"] == "stream_delta" for f in harness.frames()))

    harness.send({"type": "control", "command": "cancel"})
    assert harness.wait_frames("message_complete")
    complete = harness.frames_of_type("message_complete")[0]
    assert complete["stopReason"] == "cancelled"
    # "b" 未被广播
    assert all(f.get("text") != "b" for f in harness.frames_of_type("stream_delta"))
    release.set()


def test_busy_rejects_second_user_message(tmp_home):
    release = threading.Event()
    fake = FakeProvider(turns=[[TextDelta("a"), Wait(release, timeout=5.0), TextDelta("z")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "user_message", "text": "q1"})
    assert wait_for(lambda: any(f["type"] == "stream_delta" for f in harness.frames()))

    harness.send({"type": "user_message", "text": "q2"})
    busy = harness.frames_of_type("error")[-1]
    assert busy["kind"] == "busy"

    release.set()
    assert harness.wait_frames("message_complete")
    assert wait_for(lambda: not harness.service._turn_alive())

    # 取消标志不泄漏：cancel 后的新消息照常运行
    harness.send({"type": "control", "command": "cancel"})
    fake.turns.append([TextDelta("next")])
    harness.send({"type": "user_message", "text": "q3"})
    assert harness.wait_frames("message_complete", count=2)
    assert harness.frames_of_type("message_complete")[-1]["stopReason"] == "end_turn"


def test_malformed_line_and_unknown_type(tmp_home):
    harness = Harness(FakeProvider())
    harness.service.handle_line("{broken json")
    harness.send({"type": "time_travel"})
    errors = harness.frames_of_type("error")
    assert len(errors) == 2
    assert all(e["kind"] == "protocol" for e in errors)


def test_unsupported_and_pre_init_guards(tmp_home):
    # workspaceRoots 指向非 git 目录：检查点走降级路径，行为确定（不依赖
    # pytest 运行目录是否恰好在 git 仓库里）
    ws = tmp_home / "ws"
    ws.mkdir()
    harness = Harness(FakeProvider())
    harness.send({"type": "user_message", "text": "early"})  # init 之前
    harness.send({"type": "init", "workspaceRoots": [str(ws)]})
    assert any(
        f["type"] == "result" and f.get("command") == "checkpoint" and f["ok"] is False
        for f in harness.frames()
    )  # 无 git 仓库：降级说明已广播（R30）
    # M2 起 permission_response / diff_review / rollback 已实装：无挂起请求
    # 时回 not_found；降级下 rollback 回 rollback_result{ok:false}+原因。
    harness.send({"type": "permission_response", "id": "p", "decision": "allow"})
    harness.send({"type": "diff_review", "id": "d", "accepted": True})
    harness.send({"type": "rollback", "checkpointId": "c"})
    harness.send({"type": "control", "command": "compact"})
    harness.send({"type": "git_commit", "repoRoot": "/r", "scope": "all"})
    errors = harness.frames_of_type("error")
    kinds = [e["kind"] for e in errors]
    assert kinds[0] == "protocol"  # init 前 user_message
    assert kinds[1] == "not_found"  # 无挂起权限请求
    assert kinds[2] == "not_found"  # 无挂起 diff 提案
    # rollback：检查点降级（非 git 仓库）→ ok=False + checkpoint 原因帧
    rollback = harness.frames_of_type("rollback_result")
    assert rollback and rollback[0]["ok"] is False
    assert kinds[3] == "checkpoint"
    assert kinds[4] == "unsupported"  # compact 属 M8
    assert kinds[5] == "unsupported"  # git_commit 属后续里程碑


def test_broken_config_falls_back_to_defaults(tmp_home):
    config_dir = tmp_home / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "providers.json").write_text("{broken", encoding="utf-8")
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    errors = harness.frames_of_type("error")
    assert any(e["kind"] == "config" for e in errors)
    # 配置错误先报，随后仍以内置默认档案开起了会话
    assert harness.frames()[0]["type"] == "error"
    assert harness.frames_of_type("session_started")
    telegraph = harness.frames_of_type("telegraph_state")[0]
    assert telegraph["current"] == "max"


def test_provider_error_during_turn_broadcasts_error(tmp_home):
    """Provider 出错时广播 error（PLAN §8 loop 无网端到端）：kind 取自
    ProviderError.kind；出错回合无 message_complete 但 usage 照常收尾；
    服务恢复后下一轮正常完成。"""
    fake = FakeProvider(turns=[[TextDelta("部分"), ProviderError("quota", "quota exceeded")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    session_id = harness.frames()[0]["sessionId"]

    harness.send({"type": "user_message", "text": "q"})
    assert harness.wait_frames("error")
    assert harness.wait_frames("usage")

    err = harness.frames_of_type("error")[-1]
    assert err["kind"] == "quota" and err["message"] == "quota exceeded"
    assert harness.frames_of_type("message_complete") == []  # 出错回合没有完成事件
    assert any(f.get("text") == "部分" for f in harness.frames_of_type("stream_delta"))
    # 错误帧也进了转录（回放可见）
    transcript = (tmp_home / "sessions" / session_id / "transcript.jsonl").read_text()
    assert "quota exceeded" in transcript

    # 服务仍然可用：悬空 user 消息已回退，下一轮正常完成
    fake.turns.append([TextDelta("恢复")])
    harness.send({"type": "user_message", "text": "q2"})
    assert harness.wait_frames("message_complete")
    complete = harness.frames_of_type("message_complete")[-1]
    assert complete["text"] == "恢复" and complete["stopReason"] == "end_turn"
    # 下一轮请求上下文干净：无连续 user 角色悬空
    second_texts = [
        b.text for m in fake.requests[-1].messages for b in m.blocks
        if isinstance(b, TextBlock)
    ]
    assert second_texts == ["q2"]


def test_unexpected_exception_during_turn_broadcasts_unknown(tmp_home):
    fake = FakeProvider(turns=[[RuntimeError("boom")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "user_message", "text": "q"})
    assert harness.wait_frames("error")
    err = harness.frames_of_type("error")[-1]
    assert err["kind"] == "unknown" and "RuntimeError" in err["message"]


def test_main_entrypoint_end_to_end(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("端到端")]])
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "init", "workspaceRoots": ["/w"]}),
                json.dumps({"type": "user_message", "text": "你好"}),
                json.dumps({"type": "control", "command": "ping"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    assert main(stdin, stdout, provider_factory=lambda entry, key: fake) == 0
    types = [json.loads(line)["type"] for line in stdout.getvalue().splitlines()]
    assert types[0] == "session_started"
    assert "message_complete" in types
    assert "ping_result" in types


# ---------------------------------------------------------------------------
# M4：fork / pin / export / set_root 控制命令往返 + sessions_list 新字段
# ---------------------------------------------------------------------------


def _inbound_events(tmp_home, session_id: str) -> list:
    transcript = (tmp_home / "sessions" / session_id / "transcript.jsonl").read_text()
    return [
        json.loads(line)["event"]
        for line in transcript.splitlines()
        if json.loads(line)["dir"] == "in"
    ]


def test_sessions_list_carries_m4_fields(tmp_home):
    ws = tmp_home / "ws"
    ws.mkdir()
    fake = FakeProvider(turns=[[TextDelta("答")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": [str(ws)]})
    session_id = harness.frames()[0]["sessionId"]
    harness.send({"type": "user_message", "text": "重构登录模块"})
    assert harness.wait_frames("message_complete")

    # new_session 触发一次新的 sessions_list 广播，携带首会话的最新元信息
    harness.send({"type": "control", "command": "new_session"})
    listing = harness.frames_of_type("sessions_list")[-1]["sessions"]
    entry = next(s for s in listing if s["sessionId"] == session_id)
    assert entry["title"] == "重构登录模块"
    assert entry["pinned"] is False
    assert entry["messageCount"] == 1
    assert entry["lastActivity"]
    assert entry["rootPath"] == str(ws)


def test_fork_control_round_trip(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("第一答")], [TextDelta("第二答")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    session_id = harness.frames()[0]["sessionId"]
    harness.send({"type": "user_message", "text": "问题一"})
    assert harness.wait_frames("message_complete")
    harness.send({"type": "user_message", "text": "问题二"})
    assert harness.wait_frames("message_complete", count=2)

    # 分叉点 = 转录里第二条用户消息的 messageId（入站帧落盘时已附上）
    fork_point = _inbound_events(tmp_home, session_id)[-1]["messageId"]

    harness.send(
        {"type": "control", "command": "fork", "sessionId": session_id, "atMessageId": fork_point}
    )
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "fork" and result["ok"] is True
    new_id = result["detail"]
    assert new_id != session_id
    # 广播了新会话 session_started + 列表更新（含 parentSessionId）
    assert new_id in [f["sessionId"] for f in harness.frames_of_type("session_started")]
    listing = harness.frames_of_type("sessions_list")[-1]["sessions"]
    assert any(
        s["sessionId"] == new_id and s["parentSessionId"] == session_id for s in listing
    )
    # 新会话转录只含分叉点之前的轮次（问题一那轮）
    assert [e["text"] for e in _inbound_events(tmp_home, new_id)] == ["问题一"]
    # 新会话的 session_started 没被重复记进本会话转录（record=False 语义）
    assert "问题二" not in (tmp_home / "sessions" / new_id / "transcript.jsonl").read_text()

    # 未知分叉点 → not_found
    harness.send({"type": "control", "command": "fork", "sessionId": session_id, "atMessageId": "ghost"})
    assert harness.frames_of_type("error")[-1]["kind"] == "not_found"


def test_pin_toggle_round_trip(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    session_id = harness.frames()[0]["sessionId"]

    harness.send({"type": "control", "command": "pin", "sessionId": session_id, "pinned": True})
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "pin" and result["ok"] is True and result["detail"] == "pinned"
    meta = json.loads((tmp_home / "sessions" / session_id / "meta.json").read_text())
    assert meta["pinned"] is True

    # pinned 缺省 = 取反切换
    harness.send({"type": "control", "command": "pin", "sessionId": session_id})
    result = harness.frames_of_type("result")[-1]
    assert result["detail"] == "unpinned"
    meta = json.loads((tmp_home / "sessions" / session_id / "meta.json").read_text())
    assert meta["pinned"] is False

    harness.send({"type": "control", "command": "pin", "sessionId": "ghost"})
    assert harness.frames_of_type("error")[-1]["kind"] == "not_found"


def test_export_returns_markdown_via_result(tmp_home):
    fake = FakeProvider(turns=[[TextDelta("答复全文")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    session_id = harness.frames()[0]["sessionId"]
    harness.send({"type": "user_message", "text": "导出我"})
    assert harness.wait_frames("message_complete")

    harness.send({"type": "control", "command": "export", "sessionId": session_id})
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "export" and result["ok"] is True
    markdown = result["markdown"]
    assert markdown.startswith("# ")
    assert "## 用户" in markdown and "导出我" in markdown
    assert "## 助手" in markdown and "答复全文" in markdown

    harness.send({"type": "control", "command": "export", "sessionId": "ghost"})
    assert harness.frames_of_type("error")[-1]["kind"] == "not_found"


def test_set_root_switches_tool_cwd(tmp_home):
    ws_a = tmp_home / "ws_a"
    ws_b = tmp_home / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    fake = FakeProvider(turns=[[TextDelta("ok")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": [str(ws_a), str(ws_b)]})
    session_id = harness.frames()[0]["sessionId"]
    assert harness.service._loop.tool_session.ctx.cwd == ws_a  # 默认第一个根

    # 按 rootIndex 切根：工具 ctx cwd、会话 meta.rootPath 同步
    harness.send({"type": "control", "command": "set_root", "rootIndex": 1})
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "set_root" and result["ok"] is True
    assert result["detail"] == str(ws_b)
    assert harness.service._loop.tool_session.ctx.cwd == ws_b
    meta = json.loads((tmp_home / "sessions" / session_id / "meta.json").read_text())
    assert meta["rootPath"] == str(ws_b)

    # 按 rootPath 切回；不带参数回落第一个根
    harness.send({"type": "control", "command": "set_root", "rootPath": str(ws_a)})
    assert harness.service._loop.tool_session.ctx.cwd == ws_a
    harness.send({"type": "control", "command": "set_root", "rootIndex": 1})
    harness.send({"type": "control", "command": "set_root"})
    assert harness.service._loop.tool_session.ctx.cwd == ws_a

    # 越界 / 工作区之外的路径一律拒绝
    harness.send({"type": "control", "command": "set_root", "rootIndex": 9})
    errors = harness.frames_of_type("error")
    assert errors[-1]["kind"] == "protocol" and "out of range" in errors[-1]["message"]
    harness.send({"type": "control", "command": "set_root", "rootPath": "/not/a/root"})
    errors = harness.frames_of_type("error")
    assert errors[-1]["kind"] == "protocol" and "workspaceRoots" in errors[-1]["message"]


def test_new_session_resets_root_and_resume_restores(tmp_home):
    """set_root 的根只属于当前会话：新会话回落第一个根，resume 回到记录的根。"""
    ws_a = tmp_home / "ws_a"
    ws_b = tmp_home / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    fake = FakeProvider(turns=[[TextDelta("ok")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": [str(ws_a), str(ws_b)]})
    first = harness.frames()[0]["sessionId"]
    harness.send({"type": "control", "command": "set_root", "rootIndex": 1})
    assert harness.service._loop.tool_session.ctx.cwd == ws_b

    harness.send({"type": "control", "command": "new_session"})
    assert harness.service._loop.tool_session.ctx.cwd == ws_a  # 新会话默认第一个根

    harness.send({"type": "control", "command": "resume", "sessionId": first})
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "resume" and result["ok"] is True
    assert harness.service._loop.tool_session.ctx.cwd == ws_b  # 恢复记录的根
