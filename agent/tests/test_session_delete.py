"""session_delete（M4 补完）：

- 删除非活跃会话：整目录消失 + result ok + sessions_list 收敛
- 拒绝当前活跃会话（Result ok=False，提示先切换——Error 帧会等 15s 兜底
  才解锁 UI，可预期拒绝必须走 result）
- 不存在 / 缺 sessionId 的回执形态
- SessionStore.delete 的 id 白名单：路径逃逸（../、分隔符、合法形态外的
  任意串）一律 SessionError，sessions 根目录外零副作用
"""

from __future__ import annotations

import io
import json

import pytest

from zcode_py.llm.fake import FakeProvider
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.sessions import SessionError, SessionStore


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


def test_delete_non_active_session_removes_dir_and_broadcasts(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    first = harness.frames()[0]["sessionId"]

    # 换到新会话，first 变为非活跃的盘上会话
    harness.send({"type": "control", "command": "new_session"})
    second = next(
        f["sessionId"] for f in harness.frames_of_type("session_started") if f["sessionId"] != first
    )
    assert (tmp_home / "sessions" / first).is_dir()

    harness.send({"type": "control", "command": "session_delete", "sessionId": first})
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "session_delete" and result["ok"] is True
    assert result["detail"] == first

    # 目录消失；随后的 sessions_list 不再含该会话（record=False，仅 UI 广播）
    assert not (tmp_home / "sessions" / first).exists()
    lists = harness.frames_of_type("sessions_list")
    ids = [s["sessionId"] for s in lists[-1]["sessions"]]
    assert first not in ids and second in ids


def test_delete_active_session_rejected_via_result(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    active = harness.frames()[0]["sessionId"]

    harness.send({"type": "control", "command": "session_delete", "sessionId": active})
    result = harness.frames_of_type("result")[-1]
    assert result["command"] == "session_delete" and result["ok"] is False
    assert "活跃" in result["detail"]
    # 目录完好
    assert (tmp_home / "sessions" / active / "meta.json").exists()


def test_delete_missing_session_and_missing_session_id(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    # 合法形态但不存在 → result ok=False（不是 Error 帧）
    harness.send(
        {"type": "control", "command": "session_delete", "sessionId": "20260101-000000-00000000"}
    )
    result = harness.frames_of_type("result")[-1]
    assert result["ok"] is False and "not found" in result["detail"]

    # 缺 sessionId → 协议误用的 Error 帧
    harness.send({"type": "control", "command": "session_delete"})
    error = harness.frames_of_type("error")[-1]
    assert error["kind"] == "protocol" and "sessionId" in error["message"]


def test_store_delete_id_whitelist_blocks_path_escape(tmp_path):
    store = SessionStore(base_dir=tmp_path)
    meta = store.create(root_path="/w")

    for evil in ("../evil", "..", "20260101-000000-0000000x", "a/b", "20260101-000000-00000000\n"):
        with pytest.raises(SessionError, match="invalid session id"):
            store.delete(evil)

    # 白名单外的父目录零副作用；合法 id 正常删除
    assert (tmp_path / meta["sessionId"]).is_dir()
    assert not (tmp_path.parent / "evil").exists()
    store.delete(meta["sessionId"])
    assert not (tmp_path / meta["sessionId"]).exists()

    # 再删一次 → not found（目录已不在）
    with pytest.raises(SessionError, match="session not found"):
        store.delete(meta["sessionId"])
