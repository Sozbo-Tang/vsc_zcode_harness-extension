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


# ---------------------------------------------------------------------------
# M4：列表元信息（title/messageCount/pinned/rootPath）、搜索、分叉、导出、输入历史
# ---------------------------------------------------------------------------


def test_record_user_input_title_history_count(tmp_home):
    """title 由首条非空用户消息定（前 30 字符只定一次）；messageCount 每条 +1。"""
    store = SessionStore()
    sid = store.create()["sessionId"]
    store.record_user_input(sid, "把登录模块重构成类形式并补齐测试")
    first = store.get_meta(sid)
    assert first["title"] == "把登录模块重构成类形式并补齐测试"  # <30 字符整条
    assert first["messageCount"] == 1

    long_text = "甲" * 35
    other = store.create()["sessionId"]
    store.record_user_input(other, long_text)
    assert store.get_meta(other)["title"] == "甲" * 30  # 恰 30 字符截断

    # 第二条消息不改 title，只累计轮次
    store.record_user_input(sid, "再来一条")
    again = store.get_meta(sid)
    assert again["title"] == "把登录模块重构成类形式并补齐测试"
    assert again["messageCount"] == 2


def test_input_history_cap_and_consecutive_dedupe(tmp_home):
    store = SessionStore()
    sid = store.create()["sessionId"]
    store.record_user_input(sid, "same")
    store.record_user_input(sid, "same")  # 连续重复不占 ↑ 位
    assert store.get_meta(sid)["inputHistory"] == ["same"]
    for i in range(105):
        store.record_user_input(sid, f"q{i:03d}")
    history = store.get_meta(sid)["inputHistory"]
    assert len(history) == 100  # R27 上限
    assert history[0] == "q005"  # 最旧的被挤出
    assert history[-1] == "q104"


def test_create_defaults_and_pin_toggle(tmp_home):
    store = SessionStore()
    meta = store.create(root_path="/w/a")
    sid = meta["sessionId"]
    on_disk = store.get_meta(sid)
    assert on_disk["pinned"] is False
    assert on_disk["rootPath"] == "/w/a"
    assert on_disk["messageCount"] == 0

    store.set_pinned(sid, True)
    assert store.get_meta(sid)["pinned"] is True
    store.set_pinned(sid, False)
    assert store.get_meta(sid)["pinned"] is False


def test_search_by_title_and_transcript_text(tmp_home):
    store = SessionStore()
    s1 = store.create()["sessionId"]
    store.record_user_input(s1, "重构登录模块")
    store.append(s1, {"type": "message_complete", "stopReason": "end_turn", "text": "AuthClass 已就位"})
    s2 = store.create()["sessionId"]
    store.record_user_input(s2, "数据库连接排查")

    assert [m["sessionId"] for m in store.search_sessions("登录")] == [s1]  # title 命中
    assert [m["sessionId"] for m in store.search_sessions("authclass")] == [s1]  # 助手文本、大小写不敏感
    assert [m["sessionId"] for m in store.search_sessions("数据库")] == [s2]  # 用户文本
    assert store.search_sessions("不存在的关键词") == []
    assert len(store.search_sessions("   ")) == 2  # 空关键词退化为全量
    # 工具参数不是搜索语料：查工具路径命不中
    store.append(s2, {"type": "tool_call", "callId": "t", "name": "read",
                      "args": {"path": "zzz_secret.py"}, "state": "finished"})
    assert store.search_sessions("zzz_secret") == []


def _fill_two_turns(store: SessionStore, sid: str) -> None:
    store.append(sid, {"type": "session_started", "sessionId": sid})
    store.append(sid, {"type": "telegraph_state", "current": "max"})
    store.append(sid, {"type": "user_message", "text": "q1", "messageId": "um_a"}, direction="in")
    store.append(sid, {"type": "tool_call", "callId": "t1", "name": "read", "args": {"path": "a.py"},
                       "state": "finished", "result": {"content": "x"}, "elapsedMs": 3})
    store.append(sid, {"type": "message_complete", "stopReason": "end_turn", "text": "a1"})
    store.append(sid, {"type": "user_message", "text": "q2", "messageId": "um_b"}, direction="in")
    store.append(sid, {"type": "message_complete", "stopReason": "end_turn", "text": "a2"})


def test_fork_copies_prefix_before_fork_point(tmp_home):
    """分叉语义：新会话 = 分叉点用户消息**之前**的完整轮次；分叉点那轮不带走。"""
    store = SessionStore()
    meta = store.create(workspace_roots=["/w"], root_path="/w",
                        provider_id="glm", model_id="glm-5.3", title="话题")
    sid = meta["sessionId"]
    _fill_two_turns(store, sid)

    forked = store.fork(sid, "um_b")
    new_id = forked["sessionId"]
    assert new_id != sid
    assert forked["parentSessionId"] == sid
    assert forked["title"] == "话题"
    assert forked["rootPath"] == "/w"
    frames = store.read_frames(new_id)
    assert [(f["dir"], f["event"]["type"]) for f in frames] == [
        ("out", "session_started"),   # 新 id 首帧
        ("out", "telegraph_state"),
        ("in", "user_message"),       # q1 轮完整（含工具卡）
        ("out", "tool_call"),
        ("out", "message_complete"),
    ]
    assert frames[0]["event"]["sessionId"] == new_id  # 旧 session_started 已剔除
    assert frames[2]["event"]["messageId"] == "um_a"
    # 分叉点那轮不带走：q2/a2 不在新转录，上下文重建也只有 q1/a1
    assert all(f["event"].get("text") != "q2" for f in frames)
    assert forked["messageCount"] == 1
    assert forked["inputHistory"] == ["q1"]
    messages = context_from_frames(frames)
    assert [b.text for m in messages for b in m.blocks] == ["q1", "a1"]

    # 原会话不受影响
    assert len(store.read_frames(sid)) == 7


def test_fork_at_first_message_yields_empty_prefix(tmp_home):
    store = SessionStore()
    sid = store.create()["sessionId"]
    _fill_two_turns(store, sid)
    forked = store.fork(sid, "um_a")
    frames = store.read_frames(forked["sessionId"])
    # 前缀里没有轮次，只剩新 session_started + 首条消息前的状态帧（车钟等）
    assert [(f["dir"], f["event"]["type"]) for f in frames] == [
        ("out", "session_started"),
        ("out", "telegraph_state"),
    ]
    assert frames[0]["event"]["sessionId"] == forked["sessionId"]
    assert forked["messageCount"] == 0
    assert forked["inputHistory"] == []


def test_fork_unknown_point_raises(tmp_home):
    store = SessionStore()
    sid = store.create()["sessionId"]
    _fill_two_turns(store, sid)
    with pytest.raises(SessionError, match="fork point"):
        store.fork(sid, "um_ghost")
    with pytest.raises(SessionError):
        store.fork("nope", "um_a")


def test_export_markdown_structure(tmp_home):
    store = SessionStore()
    sid = store.create(provider_id="glm", model_id="glm-5.3", root_path="/w")["sessionId"]
    store.record_user_input(sid, "看看这个报错")
    store.append(sid, {"type": "user_message", "text": "看看这个报错", "messageId": "um_a"}, direction="in")
    store.append(sid, {"type": "tool_call", "callId": "t1", "name": "bash",
                       "args": {"command": "pytest -q"}, "state": "finished",
                       "result": {"output": "..."}, "elapsedMs": 12})
    store.append(sid, {"type": "tool_call", "callId": "t2", "name": "read",
                       "args": {"path": "a.py"}, "state": "finished",
                       "result": {"error": "not_found", "detail": "gone"}})
    store.append(sid, {"type": "message_complete", "stopReason": "end_turn", "text": "修好了"})
    store.append(sid, {"type": "user_message", "text": "再跑一次", "messageId": "um_b"}, direction="in")
    store.append(sid, {"type": "message_complete", "stopReason": "cancelled", "text": None})

    md = store.export_markdown(sid)
    assert md.startswith("# 看看这个报错\n")
    assert "session `" + sid + "`" in md
    assert "glm/glm-5.3" in md
    assert "root `/w`" in md
    # 轮次结构：用户 → 本轮工具卡摘要 → 助手
    assert md.index("## 用户") < md.index("`bash` pytest -q (ok, 12ms)")
    assert md.index("`bash`") < md.index("`read` a.py (error)")
    assert md.index("`read`") < md.index("## 助手")
    assert "修好了" in md
    assert "_（无文本输出）_" in md  # 取消轮显式标注，不静默丢轮
