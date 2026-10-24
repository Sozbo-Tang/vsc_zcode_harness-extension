"""sessions：transcript.jsonl 追加式 + meta.json + 上下文重建（PLAN §3.2）。"""

from __future__ import annotations

import json

import pytest

from zcode_py.loop import context_from_frames
from zcode_py.llm.base import TextBlock, LLMUserMessage, LLMAssistantMessage
from zcode_py.sessions import SessionError, SessionStore


def test_create_writes_meta(tmp_home):
    store = SessionStore()
    meta = store.create(workspace_roots=["/w"], provider_id="glm", model_id="glm-5.3", title="t")
    meta_path = tmp_home / "sessions" / meta["sessionId"] / "meta.json"
    assert meta_path.exists()
    on_disk = json.loads(meta_path.read_text(encoding="utf-8"))
    assert on_disk["providerId"] == "glm"
    assert on_disk["modelId"] == "glm-5.3"
    assert on_disk["workspaceRoots"] == ["/w"]


def test_append_and_read_frames_round_trip(tmp_home):
    store = SessionStore()
    meta = store.create()
    store.append(meta["sessionId"], {"type": "session_started", "sessionId": meta["sessionId"]})
    store.append(meta["sessionId"], {"type": "user_message", "text": "hi"}, direction="in")
    store.append(meta["sessionId"], {"type": "stream_delta", "text": "答"})
    frames = store.read_frames(meta["sessionId"])
    assert [f["dir"] for f in frames] == ["out", "in", "out"]
    assert frames[1]["event"] == {"type": "user_message", "text": "hi"}
    assert all("ts" in f for f in frames)
    # 追加式：transcript.jsonl 是三行
    lines = (tmp_home / "sessions" / meta["sessionId"] / "transcript.jsonl").read_text().splitlines()
    assert len(lines) == 3


def test_update_meta_and_list_sessions(tmp_home):
    store = SessionStore()
    first = store.create(title="first")
    second = store.create(title="second")
    # update_meta 会刷新 updatedAt：改过 first 后它就是「最近更新」
    store.update_meta(first["sessionId"], title="renamed")
    assert store.get_meta(first["sessionId"])["title"] == "renamed"
    listing = store.list_sessions()
    assert [m["sessionId"] for m in listing] == [first["sessionId"], second["sessionId"]]


def test_missing_session_raises(tmp_home):
    store = SessionStore()
    with pytest.raises(SessionError):
        store.get_meta("nope")
    with pytest.raises(SessionError):
        store.read_frames("nope")


def test_context_from_frames_rebuilds_pairs():
    frames = [
        {"dir": "out", "event": {"type": "session_started", "sessionId": "s"}},
        {"dir": "in", "event": {"type": "user_message", "text": "q1"}},
        {"dir": "out", "event": {"type": "stream_delta", "text": "部分"}},
        {"dir": "out", "event": {"type": "message_complete", "stopReason": "end_turn", "text": "a1"}},
        {"dir": "in", "event": {"type": "user_message", "text": "q2"}},
        {"dir": "out", "event": {"type": "message_complete", "stopReason": "cancelled", "text": None}},
        {"dir": "in", "event": {"type": "user_message", "text": "q3"}},
        {"dir": "out", "event": {"type": "message_complete", "stopReason": "end_turn", "text": "a3"}},
    ]
    messages = context_from_frames(frames)
    assert len(messages) == 4  # q2 那轮无 assistant 文本：整轮丢弃，保持配对
    assert isinstance(messages[0], LLMUserMessage)
    assert messages[0].blocks == [TextBlock("q1")]
    assert isinstance(messages[1], LLMAssistantMessage)
    assert messages[1].blocks == [TextBlock("a1")]
    assert messages[2].blocks == [TextBlock("q3")]
    assert messages[3].blocks == [TextBlock("a3")]


def test_read_frames_skips_torn_tail_line(tmp_home):
    """进程崩溃留下的断尾 JSON 行不阻塞回放（transcript 即 L4 冷存储，
    必须半行可读）。"""
    store = SessionStore()
    meta = store.create()
    store.append(meta["sessionId"], {"type": "session_started", "sessionId": "s"})
    transcript = tmp_home / "sessions" / meta["sessionId"] / "transcript.jsonl"
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('{"ts":"t","dir":"out","event":{"type":"str')  # 断尾
    store.append(meta["sessionId"], {"type": "stream_delta", "text": "ok"})
    frames = store.read_frames(meta["sessionId"])
    assert [f["event"]["type"] for f in frames] == ["session_started", "stream_delta"]


def test_replay_with_fresh_store_instance(tmp_home):
    """落盘后可回放：新进程（新 SessionStore 实例）读同一目录恢复元数据、
    帧序与 LLM 上下文。"""
    store = SessionStore()
    meta = store.create(provider_id="glm", model_id="glm-5.3")
    session_id = meta["sessionId"]
    store.append(session_id, {"type": "user_message", "text": "q1"}, direction="in")
    store.append(
        session_id,
        {"type": "message_complete", "stopReason": "end_turn", "text": "a1"},
    )

    revived = SessionStore()  # 同一 ZCODE_PY_HOME，全新实例（模拟重启）
    assert revived.get_meta(session_id)["modelId"] == "glm-5.3"
    assert [m["sessionId"] for m in revived.list_sessions()] == [session_id]
    messages = context_from_frames(revived.read_frames(session_id))
    assert [b.text for m in messages for b in m.blocks] == ["q1", "a1"]
