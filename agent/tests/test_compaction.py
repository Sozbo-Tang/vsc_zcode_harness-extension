"""M8 上下文压缩（PLAN §3.4 R14）：L2 配对完整性、阈值触发、L3 摘要调用
（FakeProvider）、control compact、search_history（L4）。全离线。"""

from __future__ import annotations

import json
import threading

from zcode_py.compaction import (
    CompactionManager,
    evict_tool_results,
    is_placeholder,
    pairing_intact,
    resolve_compaction_target,
    search_transcript,
)
from zcode_py.compaction.coldstore import SEARCH_HISTORY_SPEC
from zcode_py.compaction.summary import split_head_tail
from zcode_py.llm.base import (
    LLMAssistantMessage,
    LLMUserMessage,
    ProviderError,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    UsageUpdate,
)
from zcode_py.llm.fake import FakeProvider
from zcode_py.loop import AgentLoop
from zcode_py.perf import PerfTracker
from zcode_py.permissions import PermissionEngine
from zcode_py.protocol import Compaction
from zcode_py.tools import ToolContext, ToolRegistry, ToolSession


# ---------------------------------------------------------------------------
# 消息构造（合法上下文：严格交替 + tool_use/tool_result 成对）
# ---------------------------------------------------------------------------


def user_text(text: str) -> LLMUserMessage:
    return LLMUserMessage([TextBlock(text)])


def assistant_text(text: str) -> LLMAssistantMessage:
    return LLMAssistantMessage([TextBlock(text)])


def tool_turn(text: str, call_id: str, result_content) -> tuple[LLMAssistantMessage, LLMUserMessage]:
    """一对 (assistant[text+tool_use], user[tool_result])。"""
    assistant = LLMAssistantMessage(
        [TextBlock(text), ToolUseBlock(id=call_id, name="read", input={"path": "x"})]
    )
    return assistant, LLMUserMessage([ToolResultBlock(tool_use_id=call_id, content=result_content)])


def big_context(n_rounds: int = 3, chars: int = 2000) -> list:
    """user 发起 + n 轮工具往返 + 收尾答复 + 第二轮对话（给 L3 一个安全切点）。"""
    messages = [user_text("请重构登录模块")]
    for i in range(n_rounds):
        a, u = tool_turn(f"看看第 {i} 个文件", f"call_{i}", {"stdout": "x" * chars, "round": i})
        messages += [a, u]
    messages.append(assistant_text("分析完成。"))
    messages += [user_text("继续下一步"), assistant_text("第二轮回复")]
    return messages


# ---------------------------------------------------------------------------
# L2：机械淘汰（PLAN §8 专测项——配对完整性）
# ---------------------------------------------------------------------------


def result_blocks(messages):
    return [
        b
        for m in messages
        if isinstance(m, LLMUserMessage)
        for b in m.blocks
        if isinstance(b, ToolResultBlock)
    ]


def use_blocks(messages):
    return [
        b
        for m in messages
        if isinstance(m, LLMAssistantMessage)
        for b in m.blocks
        if isinstance(b, ToolUseBlock)
    ]


def test_evict_replaces_older_results_keeps_pairing():
    messages = big_context(n_rounds=3)
    before_ids = [b.id for b in use_blocks(messages)]
    report = evict_tool_results(messages, keep_recent=1)

    assert report.evicted_results == 2  # 3 轮保留最近 1 轮
    assert pairing_intact(messages)  # 配对不断裂（PLAN §8 硬约束）
    assert [b.id for b in use_blocks(messages)] == before_ids  # tool_use 原样
    contents = [b.content for b in result_blocks(messages)]
    placeholders = [c for c in contents if is_placeholder(c)]
    assert len(placeholders) == 2
    assert all("原" in c and "字符工具输出" in c for c in placeholders)
    # 幂等：重复触发不二次替换
    report2 = evict_tool_results(messages, keep_recent=1)
    assert report2.evicted_results == 0


def test_evict_noop_when_at_or_below_keep_recent():
    messages = [user_text("hi"), *tool_turn("go", "c1", "out"), assistant_text("done")]
    report = evict_tool_results(messages, keep_recent=1)
    assert report.evicted_results == 0
    assert not any(is_placeholder(b.content) for b in result_blocks(messages))


def test_evict_never_touches_messages_or_roles():
    messages = big_context(n_rounds=2)
    snapshot_len = len(messages)
    evict_tool_results(messages, keep_recent=0)
    assert len(messages) == snapshot_len  # 只换 content，不删消息
    roles = ["user" if isinstance(m, LLMUserMessage) else "assistant" for m in messages]
    assert roles == ["user", "assistant"] * (snapshot_len // 2)  # 严格交替原样


def test_pairing_intact_detects_broken_contexts():
    dangling = [user_text("go"), LLMAssistantMessage([ToolUseBlock(id="a", name="t", input={})])]
    assert pairing_intact(dangling) is False  # 悬空 tool_use
    orphan = [
        user_text("go"),
        assistant_text("ok"),
        LLMUserMessage([ToolResultBlock(tool_use_id="ghost", content="x")]),
    ]
    assert pairing_intact(orphan) is False  # 孤儿 tool_result
    assert pairing_intact([user_text("a"), user_text("b")]) is False  # 连续同角色
    assert pairing_intact([]) is True


# ---------------------------------------------------------------------------
# 阈值触发（manager + usage 口径）
# ---------------------------------------------------------------------------


class LoopStub:
    def __init__(self, messages):
        self.messages = messages


def make_manager(emitted, loop, provider, model_entry=None, **kwargs):
    return CompactionManager(
        emit=emitted.append,
        loop_supplier=lambda: loop,
        provider_supplier=lambda: (
            provider, "m", {"id": "main", "defaultModel": "m"},
            model_entry or {"contextWindow": 10_000},
        ),
        config_supplier=lambda: {},
        settings_supplier=lambda: {},
        provider_factory=lambda entry, key: provider,
        **kwargs,
    )


def compaction_frames(emitted):
    return [m for m in emitted if isinstance(m, Compaction)]


def test_threshold_l2_eviction_emits_report():
    emitted = []
    loop = LoopStub(big_context(n_rounds=3))
    manager = make_manager(emitted, loop, FakeProvider())

    manager.on_model_usage(UsageUpdate(input_tokens=6_000))  # 60% > 50%
    frames = compaction_frames(emitted)
    assert len(frames) == 1 and frames[0].layer == "L2"
    report = frames[0].report
    assert report["evictedResults"] == 2
    assert report["tokensBefore"] >= report["tokensAfter"]
    assert "60%" in report["reason"]
    assert pairing_intact(loop.messages)


def test_threshold_below_50pct_is_noop():
    emitted = []
    loop = LoopStub(big_context(n_rounds=2))
    manager = make_manager(emitted, loop, FakeProvider())
    manager.on_model_usage(UsageUpdate(input_tokens=4_000))  # 40%
    assert compaction_frames(emitted) == []
    assert not any(is_placeholder(b.content) for b in result_blocks(loop.messages))


def test_threshold_85pct_triggers_l3_summary_with_fake_provider():
    fake = FakeProvider(turns=[[TextDelta("## 任务目标\n重构登录模块\n## 下一步\n继续 edit")]])
    emitted = []
    loop = LoopStub(big_context(n_rounds=1, chars=4000))
    manager = make_manager(emitted, loop, fake)

    manager.on_model_usage(UsageUpdate(input_tokens=9_000))  # 90% ≥ 85%
    frames = compaction_frames(emitted)
    assert len(frames) == 1 and frames[0].layer == "L3"
    report = frames[0].report
    assert report["messagesAfter"] < report["messagesBefore"]
    assert report["summaryTokens"] > 0
    assert report["tokensAfter"] < report["tokensBefore"]
    # 摘要请求确实经过 Provider，且带七要素系统提示词
    assert len(fake.requests) == 1
    assert "## 任务目标" in fake.requests[0].system
    # 重建结果：首条 user 消息含摘要块，尾部保留，配对完整
    first = loop.messages[0]
    assert isinstance(first, LLMUserMessage)
    assert "重构登录模块" in first.blocks[0].text
    assert "继续下一步" in first.blocks[1].text  # 摘要并入，尾部原文仍在
    assert pairing_intact(loop.messages)


def test_l3_model_failure_falls_back_to_l2():
    fake = FakeProvider(turns=[[ProviderError("network", "boom")]])
    emitted = []
    loop = LoopStub(big_context(n_rounds=3))
    manager = make_manager(emitted, loop, fake)

    report = manager.compact_now(loop, reason="manual")
    frames = compaction_frames(emitted)
    assert frames and frames[0].layer == "L3"
    assert "error" in report and report.get("fallback") == "L2"
    assert report["evictedResults"] == 2  # 降级 L2 真的淘汰了
    assert pairing_intact(loop.messages)


def test_manual_compact_without_safe_cutpoint_reports_error():
    emitted = []
    loop = LoopStub([user_text("hi"), assistant_text("ok")])
    manager = make_manager(emitted, loop, FakeProvider())
    report = manager.compact_now(loop, reason="manual")
    assert "error" in report  # 报告错误而不是破坏上下文
    assert loop.messages[0].blocks[0].text == "hi"


def test_split_head_tail_respects_pairing_boundaries():
    messages = big_context(n_rounds=3)
    head, tail = split_head_tail(messages, keep_ratio=0.15)
    assert head and tail
    # 安全切点定义保证 head 末条 assistant 无悬空 tool_use、tail 首条是纯
    # user 消息——两段各自配对完整
    assert pairing_intact(head)
    assert pairing_intact(tail)
    assert isinstance(tail[0], LLMUserMessage)


# ---------------------------------------------------------------------------
# 压缩专用模型解析（沿用 vision_relay 模式）
# ---------------------------------------------------------------------------


def test_resolve_compaction_target_provider_field_first():
    config = {"providers": [{"id": "cheap", "protocol": "openai", "defaultModel": "small-1"}]}
    current = {"id": "main", "defaultModel": "big-1", "compactionModel": "cheap/small-1"}
    entry, model, note = resolve_compaction_target(config, current, {"compactionModel": "ignored"})
    assert entry["id"] == "cheap" and model == "small-1" and note == ""


def test_resolve_compaction_target_settings_fallback_and_main_model():
    current = {"id": "main", "defaultModel": "big-1"}
    entry, model, _ = resolve_compaction_target({"providers": []}, current, {"compactionModel": "big-0"})
    assert entry is current and model == "big-0"
    entry2, model2, note2 = resolve_compaction_target({"providers": []}, current, {})
    assert entry2 is current and model2 == "big-1" and note2  # 回落主模型有说明


# ---------------------------------------------------------------------------
# AgentLoop 接线：on_model_usage 在模型回合后收到 usage
# ---------------------------------------------------------------------------


def test_loop_calls_on_model_usage_between_turns():
    fake = FakeProvider(
        turns=[
            [UsageUpdate(input_tokens=100, output_tokens=5), TextDelta("working")],
            [TextDelta("done")],
        ]
    )
    seen = []
    loop = AgentLoop(
        provider=fake,
        model="m",
        emit=lambda msg: None,
        perf=PerfTracker(),
        on_model_usage=lambda u: seen.append(u.input_tokens),
    )
    loop.run_user_text("hi")
    assert seen == [100]  # 每个模型回合一次


# ---------------------------------------------------------------------------
# service 接线：control compact（手动 L3）
# ---------------------------------------------------------------------------


def test_service_control_compact_broadcasts_report(tmp_home, tmp_path):
    import io
    import time

    from zcode_py.main import StdioEmitter, ZcodeService
    from zcode_py.sessions import SessionStore

    fake = FakeProvider(turns=[[TextDelta("第一轮回复")], [TextDelta("第二轮回复")], [TextDelta("## 任务目标\n压缩测试")]])
    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: fake)

    def send(payload: dict) -> None:
        service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames():
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def wait_frames(type_name, count=1):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if len([f for f in frames() if f["type"] == type_name]) >= count:
                return True
            time.sleep(0.01)
        return False

    send({"type": "init", "workspaceRoots": [str(tmp_path)]})
    send({"type": "user_message", "text": "第一问"})
    assert wait_frames("message_complete")
    send({"type": "user_message", "text": "第二问"})
    assert wait_frames("message_complete", 2)

    send({"type": "control", "command": "compact"})
    compactions = [f for f in frames() if f["type"] == "compaction"]
    assert compactions and compactions[0]["layer"] == "L3"
    report = compactions[0]["report"]
    assert report["messagesAfter"] < report["messagesBefore"]
    assert report["summaryTokens"] > 0
    results = [f for f in frames() if f["type"] == "result" and f.get("command") == "compact"]
    assert results and results[0]["ok"] is True
    # 压缩请求确实经过 FakeProvider（七要素系统提示词）
    assert any("## 任务目标" in (r.system or "") for r in fake.requests)


def test_service_control_compact_busy_guard(tmp_home, tmp_path):
    import io

    from zcode_py.llm.fake import Wait
    from zcode_py.main import StdioEmitter, ZcodeService
    from zcode_py.sessions import SessionStore

    release = threading.Event()
    fake = FakeProvider(turns=[[Wait(release), TextDelta("slow")]])
    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: fake)

    service.handle_line(json.dumps({"type": "init", "workspaceRoots": [str(tmp_path)]}))
    service.handle_line(json.dumps({"type": "user_message", "text": "慢回合"}))
    service.handle_line(json.dumps({"type": "control", "command": "compact"}))
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    busy = [f for f in frames if f["type"] == "error" and f["kind"] == "busy"]
    assert busy  # 回合进行中拒绝压缩
    release.set()


# ---------------------------------------------------------------------------
# L4：search_history
# ---------------------------------------------------------------------------


def test_search_transcript_matches_and_caps(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    lines = []
    for i in range(30):
        lines.append(json.dumps({
            "ts": f"t{i}", "dir": "out",
            "event": {"type": "tool_call", "name": "bash",
                      "result": {"stdout": f"needle hit {i}"}},
        }))
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hits = search_transcript(transcript, "needle")
    assert len(hits) == 20  # 30 条命中，上限 20
    assert hits[0]["line"] == 1 and hits[0]["type"] == "tool_call"
    assert "needle hit 0" in hits[0]["snippet"]
    assert search_transcript(transcript, "不存在的关键词") == []
    assert search_transcript(None, "x") == []


def test_search_history_tool_reads_current_transcript(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"ts": "t", "dir": "in", "event": {"type": "user_message", "text": "请修复 login 模块"}}) + "\n",
        encoding="utf-8",
    )
    ctx = ToolContext(cwd=tmp_path)
    ctx.state["history_reader"] = lambda: transcript
    outcome = SEARCH_HISTORY_SPEC.handler({"query": "login"}, ctx)
    assert not outcome.is_error
    assert outcome.result["count"] == 1
    assert "login" in outcome.result["hits"][0]["text"]
    assert outcome.result["source"] == str(transcript)


def test_search_history_without_reader_is_structured_error(tmp_path):
    ctx = ToolContext(cwd=tmp_path)
    outcome = SEARCH_HISTORY_SPEC.handler({"query": "x"}, ctx)
    assert outcome.is_error and outcome.result["error"] == "no_transcript"
    assert SEARCH_HISTORY_SPEC.tier == "readonly"


def test_search_history_runs_without_asking(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    asks = []
    registry = ToolRegistry()
    registry.register(SEARCH_HISTORY_SPEC)
    session = ToolSession(
        registry=registry,
        permissions=PermissionEngine(),
        cwd=tmp_path,
        emit=lambda m: None,
        ask_permission=lambda tool, args: asks.append(tool) or "deny",
    )
    session.ctx.state["history_reader"] = lambda: transcript
    outcome = session.run("search_history", {"query": "x"}, "c1")
    assert asks == []  # readonly 免批
    assert not outcome.is_error
