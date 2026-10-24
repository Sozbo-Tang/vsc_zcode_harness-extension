"""M7 子代理（R25）测试：FakeProvider 嵌套——run_child 后子循环真的跑、
结果回灌、深度=1 限制、取消传播、超时、模型覆盖走不同 provider 实例、
子代理写操作沿主权限通道弹卡（args 带 [child] 标记）。全部离线。
"""

from __future__ import annotations

import io
import json
import threading

from zcode_py.llm.base import TextDelta, ToolResultBlock, TurnEnd, ToolCallDelta
from zcode_py.llm.fake import Delay, FakeProvider
from zcode_py.loop import AgentLoop
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.permissions import PermissionEngine
from zcode_py.perf import PerfTracker
from zcode_py.sessions import SessionStore
from zcode_py.subagents import (
    CHILD_SYSTEM_PROMPT,
    DEFAULT_CHILD_TOOLS,
    RUN_CHILD_SPEC,
    ChildRuntime,
)
from zcode_py.tools import ToolContext, ToolSession, default_registry

CONFIG = {
    "providers": [
        {"id": "p1", "protocol": "openai", "defaultModel": "m1", "models": [{"id": "m1"}]},
        {"id": "p2", "protocol": "openai", "defaultModel": "cheap-1",
         "models": [{"id": "cheap-1"}, {"id": "cheap-2"}]},
    ]
}


def tool_turn(call_id, name, args):
    return [
        ToolCallDelta(call_id=call_id, name=name, args_delta=""),
        ToolCallDelta(call_id=call_id, args_delta=json.dumps(args)),
        TurnEnd("tool_use"),
    ]


def make_runtime(
    tmp_path,
    emitted,
    *,
    child_provider=None,
    ask=None,
    factory=None,
    config=None,
    main_cancel=None,
    provider_supplier=None,
):
    registry = default_registry()
    registry.register(RUN_CHILD_SPEC)
    engine = PermissionEngine(allow_rules=[])
    if provider_supplier is None:
        provider_supplier = lambda: (child_provider, "child-model", {"id": "p1"})
    runtime = ChildRuntime(
        registry=registry,
        permissions=engine,
        cwd=tmp_path,
        emit=emitted.append,
        ask_permission=ask or (lambda tool, args: "deny"),
        review_diff=lambda *a: False,
        provider_supplier=provider_supplier,
        factory=factory or (lambda entry, key: child_provider),
        config_supplier=lambda: config if config is not None else CONFIG,
        api_key_env={},
        main_cancel=main_cancel or threading.Event(),
    )
    return registry, engine, runtime


def child_ctx(tmp_path, runtime):
    ctx = ToolContext(cwd=tmp_path)
    ctx.state["child_runtime"] = runtime
    return ctx


# ---------------------------------------------------------------------------
# 子循环真的跑 + 结果/系统提示词/白名单
# ---------------------------------------------------------------------------


def test_run_child_runs_child_loop_and_returns_final_text(tmp_path):
    child = FakeProvider(turns=[[TextDelta("child answer 42"), TurnEnd("end_turn")]])
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "find the answer", "tools": ["read", "grep"]}, child_ctx(tmp_path, runtime)
    )
    assert not outcome.is_error
    assert outcome.result["result"] == "child answer 42"
    assert outcome.result["stopReason"] == "end_turn"
    assert outcome.result["model"] == "p1/child-model"
    # 子循环真的跑了：独立 provider 收到请求，prompt 在首条 user 消息里
    assert len(child.requests) == 1
    first_user = child.requests[0].messages[0]
    assert "find the answer" in getattr(first_user.blocks[0], "text", "")
    # 独立 system prompt（子代理说明）；工具白名单注入 schemas
    assert child.requests[0].system == CHILD_SYSTEM_PROMPT
    names = [t["function"]["name"] for t in child.requests[0].tools]
    assert names == ["grep", "read"]


def test_child_default_whitelist_is_readonly_four_and_excludes_run_child(tmp_path):
    child = FakeProvider(turns=[[TextDelta("ok")]])
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    RUN_CHILD_SPEC.handler({"prompt": "x"}, child_ctx(tmp_path, runtime))
    names = [t["function"]["name"] for t in child.requests[0].tools]
    assert names == sorted(DEFAULT_CHILD_TOOLS)
    assert "run_child" not in names


def test_depth_one_forbidden_in_whitelist_and_handler_guard(tmp_path):
    child = FakeProvider(turns=[])
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    # 白名单显式含 run_child → 参数错
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "x", "tools": ["read", "run_child"]}, child_ctx(tmp_path, runtime)
    )
    assert outcome.is_error and "nesting depth" in outcome.result["detail"]
    # 子代理 ctx（is_child 标记）内直接拒绝
    ctx = ToolContext(cwd=tmp_path)
    ctx.state["is_child"] = True
    ctx.state["child_runtime"] = runtime
    outcome = RUN_CHILD_SPEC.handler({"prompt": "x"}, ctx)
    assert outcome.is_error and "disabled inside child" in outcome.result["detail"]


def test_unknown_whitelist_tool_rejected(tmp_path):
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted)
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "x", "tools": ["read", "nope"]}, child_ctx(tmp_path, runtime)
    )
    assert outcome.is_error and "unknown tool" in outcome.result["detail"]


def test_run_child_requires_prompt(tmp_path):
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted)
    outcome = RUN_CHILD_SPEC.handler({"prompt": "  "}, child_ctx(tmp_path, runtime))
    assert outcome.is_error and outcome.result["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# 模型覆盖（R25）：跨 provider / 未知 provider / 沿用主会话
# ---------------------------------------------------------------------------


def test_model_override_builds_distinct_provider_instance(tmp_path):
    main_provider = FakeProvider(provider_id="p1")
    child_provider = FakeProvider(turns=[[TextDelta("cheap answer")]], provider_id="p2")
    built = []
    emitted = []

    def factory(entry, key):
        built.append(entry["id"])
        return child_provider if entry["id"] == "p2" else main_provider

    _registry, _engine, runtime = make_runtime(
        tmp_path, emitted,
        factory=factory,
        provider_supplier=lambda: (main_provider, "m1", {"id": "p1"}),
    )
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "research", "model": "p2/cheap-1"}, child_ctx(tmp_path, runtime)
    )
    assert built == ["p2"]  # 用 p2 条目重新构建
    assert outcome.result["model"] == "p2/cheap-1"
    assert len(child_provider.requests) == 1  # 子循环走的是新 provider 实例
    assert child_provider.requests[0].model == "cheap-1"
    assert not main_provider.requests  # 主 provider 没被消耗


def test_model_override_unknown_provider_is_structured_error(tmp_path):
    child = FakeProvider(turns=[])
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "x", "model": "ghost/m9"}, child_ctx(tmp_path, runtime)
    )
    assert outcome.is_error and outcome.result["error"] == "config"
    assert "ghost" in outcome.result["detail"]
    assert not child.requests


def test_default_model_inherits_main_session_provider(tmp_path):
    child = FakeProvider(turns=[[TextDelta("inherited")]])
    emitted = []
    calls = []

    def factory(entry, key):
        calls.append(entry["id"])
        return child

    _registry, _engine, runtime = make_runtime(
        tmp_path, emitted,
        factory=factory,
        provider_supplier=lambda: (child, "m1", {"id": "p1"}),
    )
    outcome = RUN_CHILD_SPEC.handler({"prompt": "x"}, child_ctx(tmp_path, runtime))
    assert calls == []  # 默认沿用：不重新构建
    assert outcome.result["model"] == "p1/m1"


# ---------------------------------------------------------------------------
# 权限沿用：子代理写操作沿主通道弹卡，args 带 [child] 标记
# ---------------------------------------------------------------------------


def test_child_write_permission_goes_through_main_channel_with_marker(tmp_path):
    child = FakeProvider(turns=[
        tool_turn("w1", "write", {"path": "child-out.txt", "content": "data"}),
        [TextDelta("written")],
    ])
    asks = []
    emitted = []
    _registry, _engine, runtime = make_runtime(
        tmp_path, emitted,
        child_provider=child,
        ask=lambda tool, args: asks.append((tool, args)) or "allow",
    )
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "write a file", "tools": ["write"]}, child_ctx(tmp_path, runtime)
    )
    assert not outcome.is_error
    assert asks and asks[0][0] == "write"
    assert asks[0][1]["[child]"] is True  # 权限卡 args 前缀标记
    assert asks[0][1]["path"] == "child-out.txt"
    assert (tmp_path / "child-out.txt").read_text() == "data"


# ---------------------------------------------------------------------------
# 取消传播与超时
# ---------------------------------------------------------------------------


def test_main_cancel_propagates_to_child(tmp_path):
    child = FakeProvider(turns=[[Delay(30.0)]])
    main_cancel = threading.Event()
    emitted = []
    _registry, _engine, runtime = make_runtime(
        tmp_path, emitted, child_provider=child, main_cancel=main_cancel
    )
    threading.Timer(0.2, main_cancel.set).start()
    outcome = RUN_CHILD_SPEC.handler({"prompt": "long task"}, child_ctx(tmp_path, runtime))
    assert outcome.is_error
    assert outcome.result["error"] == "child_cancelled"


def test_timeout_cancels_child(tmp_path):
    child = FakeProvider(turns=[[Delay(30.0)]])
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "stuck task", "timeout_sec": 0.5}, child_ctx(tmp_path, runtime)
    )
    assert outcome.is_error
    assert outcome.result["error"] == "child_timeout"
    assert "timeout_sec" in outcome.result["detail"]


def test_invalid_timeout_rejected(tmp_path):
    emitted = []
    _registry, _engine, runtime = make_runtime(tmp_path, emitted)
    outcome = RUN_CHILD_SPEC.handler(
        {"prompt": "x", "timeout_sec": -3}, child_ctx(tmp_path, runtime)
    )
    assert outcome.is_error and outcome.result["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# 主循环集成：tool_call 帧广播 + 结果回灌 + 上下文防火墙（帧过滤）
# ---------------------------------------------------------------------------


def test_run_child_feeds_back_into_main_loop_and_filters_frames(tmp_path):
    child = FakeProvider(turns=[[TextDelta("child answer 42")]])
    main = FakeProvider(turns=[
        tool_turn("c1", "run_child", {"prompt": "find the answer"}),
        [TextDelta("main done")],
    ])
    emitted = []
    registry, engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    session = ToolSession(
        registry=registry,
        permissions=engine,
        cwd=tmp_path,
        emit=emitted.append,
        ask_permission=lambda tool, args: "allow",
        review_diff=lambda *a: False,
    )
    session.ctx.state["child_runtime"] = runtime
    loop = AgentLoop(
        provider=main, model="m1", emit=emitted.append, perf=PerfTracker(),
        tool_session=session,
    )
    loop.run_user_text("delegate it")

    # 主循环 3 帧结构：run_child 工具调用 + 最终回复
    tool_calls = [m for m in emitted if type(m).__name__ == "ToolCall" and m.name == "run_child"]
    assert [t.state for t in tool_calls] == ["started", "finished"]
    assert tool_calls[1].result["result"] == "child answer 42"
    # tool_result 回灌主循环：第二次主请求末尾是配对结果
    assert len(main.requests) == 2
    fed = main.requests[1].messages[-1].blocks[0]
    assert isinstance(fed, ToolResultBlock) and fed.tool_use_id == "c1"
    assert fed.content["result"] == "child answer 42"
    # 上下文防火墙：子代理消息不进主上下文；子 message_complete 被滤掉
    assert len(loop.messages) == 4  # user / assistant(tool_use) / user(result) / assistant(text)
    completes = [m for m in emitted if type(m).__name__ == "MessageComplete"]
    assert len(completes) == 1 and completes[0].text == "main done"
    streams = [m for m in emitted if type(m).__name__ == "StreamDelta"]
    assert [s.text for s in streams] == ["main done"]  # 子流文本不进主聊天流


def test_run_child_error_feeds_back_is_error(tmp_path):
    child = FakeProvider(turns=[[TextDelta("ok")]])
    main = FakeProvider(turns=[
        tool_turn("c1", "run_child", {"prompt": "x", "model": "ghost/m9"}),
        [TextDelta("handled")],
    ])
    emitted = []
    registry, engine, runtime = make_runtime(tmp_path, emitted, child_provider=child)
    session = ToolSession(
        registry=registry, permissions=engine, cwd=tmp_path,
        emit=emitted.append, ask_permission=lambda tool, args: "allow",
        review_diff=lambda *a: False,
    )
    session.ctx.state["child_runtime"] = runtime
    loop = AgentLoop(
        provider=main, model="m1", emit=emitted.append, perf=PerfTracker(),
        tool_session=session,
    )
    loop.run_user_text("delegate")
    fed = main.requests[1].messages[-1].blocks[0]
    assert isinstance(fed, ToolResultBlock) and fed.is_error
    assert fed.content["error"] == "config"


# ---------------------------------------------------------------------------
# 服务层全链路：run_child 权限走 allow 规则，子代理与主会话共用 provider
# ---------------------------------------------------------------------------


def test_service_run_child_roundtrip(tmp_home, tmp_path):
    fake = FakeProvider(turns=[
        tool_turn("c1", "run_child", {"prompt": "count files"}),
        [TextDelta("child says hi")],
        [TextDelta("main done")],
    ])
    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: fake)
    frames = []

    class _Recorder:
        def emit(self, msg, **kw):
            frames.append(msg)

    emitter.emit = _Recorder().emit  # 直接捕获消息对象，便于断言
    service.handle_line(json.dumps({
        "type": "init",
        "workspaceRoots": [str(tmp_path)],
        "settings": {"permissionAllow": ["run_child: *"]},
    }))
    service.handle_line(json.dumps({"type": "user_message", "text": "delegate"}))
    service.shutdown()

    completes = [f for f in frames if type(f).__name__ == "MessageComplete"]
    assert completes and completes[-1].text == "main done"
    run_child_calls = [
        f for f in frames if type(f).__name__ == "ToolCall" and f.name == "run_child"
    ]
    assert [t.state for t in run_child_calls] == ["started", "finished"]
    assert run_child_calls[1].result["result"] == "child says hi"
