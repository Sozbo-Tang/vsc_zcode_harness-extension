"""M7 子代理（R25，PLAN §2.2 / §3.1 subagents.py）：run_child 工具。

语义（任务书）：

- **上下文防火墙**：子代理有独立 messages / system prompt，只有**最终文本**
  作为 tool_result 回灌主循环——中间过程（思考流/正文流/message_complete）
  不进主上下文，但过程帧照常广播给 UI（tool_call / permission_request /
  diff_proposed / image_artifact / error；主消息生命周期的帧——
  thinking_delta / stream_delta / message_complete / perf——被滤掉，否则子代理
  的完成帧会让 UI 误判主回合结束）。
- **嵌套受限**：深度=1。子代理的工具注册表按白名单重建且强制剔除
  ``run_child``（白名单显式含 run_child 直接报参数错）；子会话 ctx 再打
  ``is_child`` 标记双保险。
- **模型覆盖**：``model="providerId/modelId"``（跨 provider）或纯 ``modelId``
  （当前 provider 内）→ 用主会话 factory 重新构建 provider 实例（调研类子
  任务走便宜模型，R25）；缺省沿用主会话当前 provider/model。
- **权限沿用主 ToolSession**：子代理与主会话共享同一个 PermissionEngine
  （会话内 always 记忆互通），写操作同样弹卡——权限卡走主
  ``ask_permission`` 通道，args 加 ``"[child]": True`` 标记，主用户能看出
  这张卡是子代理发起的。
- **取消传播**：主 cancel_event set → 子代理 cancel_event 跟着 set（watcher
  轮询）；``timeout_sec``（默认 300）到点同样以取消收场，tool_result 带
  ``child_timeout`` 结构化错误。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .config import api_key_for, get_model_entry, get_provider_entry
from .llm.base import Provider, ProviderError
from .loop import AgentLoop
from .perf import PerfTracker
from .tools import (
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    ToolSession,
    ToolSpec,
    error_outcome,
)

# 默认白名单＝「只读四件套」（tools/__init__.py 口径：read/glob/grep/todo）
DEFAULT_CHILD_TOOLS = ("read", "glob", "grep", "todo")
DEFAULT_CHILD_TIMEOUT_S = 300.0
CHILD_MAX_TURNS = 8
# 取消后给子线程的收尾宽限（FakeProvider keepalive 节奏下毫秒级退场）
CHILD_JOIN_GRACE_S = 5.0

CHILD_SYSTEM_PROMPT = (
    "You are a child sub-agent of Zcode-Py running a scoped subtask. Work only "
    "on the task in the user message using your tools, then answer with your "
    "final result as plain text. You cannot spawn further sub-agents "
    "(run_child is disabled). Base every claim on tool results you actually "
    "obtained; state clearly what you could not verify."
)


def resolve_child_model(
    config: dict, spec: str, current_entry: dict | None
) -> tuple[dict | None, str | None, str]:
    """解析 model 参数 → (provider 条目, model id, 错误信息)。

    ``"providerId/modelId"`` 跨 provider；纯 ``"modelId"`` 当前 provider 内。
    """
    spec = spec.strip()
    if "/" in spec:
        pid, _, mid = spec.partition("/")
        entry = get_provider_entry(config or {}, pid.strip())
        if entry is None:
            return None, None, (
                f"unknown provider {pid.strip()!r} for run_child model override "
                f"(spec {spec!r}); known: "
                + ", ".join(sorted(p.get("id", "?") for p in (config or {}).get("providers") or []))
            )
        model_id = mid.strip() or entry.get("defaultModel")
        if not model_id:
            return None, None, f"provider {pid.strip()!r} has no model for run_child override"
        return entry, str(model_id), ""
    if current_entry is None:
        return None, None, "no current provider to resolve a bare model id against"
    return current_entry, spec, ""


class ChildRuntime:
    """子代理运行时：由服务层随主 ToolSession 构造，注入 ctx.state["child_runtime"]。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        permissions,
        cwd,
        emit: Callable[[Any], None],
        ask_permission: Callable[[str, dict], str],
        review_diff: Callable[[str, str, str], bool] | None,
        provider_supplier: Callable[[], tuple[Provider | None, str | None, dict | None]],
        factory: Callable[[dict, str | None], Provider],
        config_supplier: Callable[[], dict],
        api_key_env: dict[str, str] | None = None,
        main_cancel: threading.Event | None = None,
    ):
        self.registry = registry
        self.permissions = permissions
        self.cwd = cwd
        self.emit = emit
        self.ask_permission = ask_permission
        self.review_diff = review_diff
        self.provider_supplier = provider_supplier
        self.factory = factory
        self.config_supplier = config_supplier
        self.api_key_env = api_key_env or {}
        self.main_cancel = main_cancel or threading.Event()

    # ------------------------------------------------------------------ 入口

    def run(self, args: dict) -> ToolOutcome:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return error_outcome("run_child requires a non-empty string 'prompt'")
        timeout = self._coerce_timeout(args.get("timeout_sec"))
        if timeout is None:
            return error_outcome("timeout_sec must be a positive number")
        names, err = self._resolve_tools(args.get("tools"))
        if err is not None:
            return error_outcome(err)
        if self.main_cancel.is_set():
            return ToolOutcome(
                result={"error": "child_cancelled", "detail": "main turn cancelled before child start"},
                is_error=True,
            )
        provider, model_id, provider_id, err = self._resolve_provider(args.get("model"))
        if err is not None:
            return ToolOutcome(result={"error": "config", "detail": err}, is_error=True)

        captured: dict = {"text": None, "stop": None}
        child_cancel = threading.Event()
        child_loop = self._build_loop(
            provider=provider,
            model_id=model_id,
            names=names,
            cancel=child_cancel,
            captured=captured,
        )
        exc, timed_out, main_cancelled = self._run_turn(child_loop, prompt, timeout, child_cancel)

        if exc is not None and not timed_out and not main_cancelled:
            return ToolOutcome(
                result={
                    "error": "child_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "model": f"{provider_id}/{model_id}",
                },
                is_error=True,
            )
        stop = captured["stop"] or "end_turn"
        payload: dict = {
            "model": f"{provider_id}/{model_id}",
            "stopReason": stop,
            "tools": list(names),
            "result": captured["text"] or "(child returned no final text)",
        }
        is_error = False
        if timed_out:
            payload["error"] = "child_timeout"
            payload["detail"] = f"child exceeded timeout_sec={timeout:g}"
            is_error = True
        elif main_cancelled or stop == "cancelled":
            payload["error"] = "child_cancelled"
            is_error = True
        elif stop == "max_turns":
            payload["error"] = "child_max_turns"
            is_error = True
        return ToolOutcome(result=payload, is_error=is_error)

    # ------------------------------------------------------------------ 内部

    def _resolve_tools(self, requested) -> tuple[list[str] | None, str | None]:
        if requested is None:
            names = list(DEFAULT_CHILD_TOOLS)
        elif isinstance(requested, list) and all(isinstance(n, str) and n.strip() for n in requested):
            names = [n.strip() for n in requested]
            if not names:
                return None, "tools whitelist must not be empty"
        else:
            return None, "tools must be an array of tool-name strings"
        for name in names:
            if name == "run_child":
                return None, (
                    "run_child is not allowed inside a child agent "
                    "(nesting depth is 1)"
                )
            if self.registry.get(name) is None:
                return None, f"unknown tool {name!r} for the child whitelist"
        return names, None

    def _resolve_provider(self, model_spec) -> tuple[Provider | None, str, str, str | None]:
        if model_spec is None:
            provider, model_id, entry = self.provider_supplier()
            if provider is None:
                return None, "", "", "no provider available in the main session"
            return provider, model_id or "unknown", (entry or {}).get("id") or "main", None
        if not isinstance(model_spec, str) or not model_spec.strip():
            return None, "", "", "model must be a 'providerId/modelId' string"
        _current_provider, _current_model, current_entry = self.provider_supplier()
        entry, model_id, err = resolve_child_model(
            self.config_supplier(), model_spec, current_entry
        )
        if err:
            return None, "", "", err
        assert entry is not None and model_id is not None
        key = api_key_for(entry.get("id") or "", self.api_key_env)
        try:
            provider = self.factory(entry, key)
        except ProviderError as exc:
            return None, "", "", f"cannot build child provider: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            return None, "", "", f"cannot build child provider: {exc}"
        # 档案存在性校验（缺档案时 get_model_entry 给最小兜底档案，不视为错误）
        get_model_entry(entry, model_id)
        return provider, model_id, entry.get("id") or "child", None

    def _build_loop(self, *, provider, model_id, names, cancel, captured) -> AgentLoop:
        child_registry = ToolRegistry()
        for name in names:
            spec = self.registry.get(name)
            assert spec is not None  # _resolve_tools 已校验
            child_registry.register(spec)

        def child_emit(msg) -> None:
            # 上下文防火墙的 UI 侧：过程帧照常广播，主消息生命周期帧被滤掉
            # （message_complete 只用于捕获最终文本，不进主聊天流）。
            type_name = type(msg).__name__
            if type_name in ("ThinkingDelta", "StreamDelta", "Perf"):
                return
            if type_name == "MessageComplete":
                captured["text"] = getattr(msg, "text", None)
                captured["stop"] = getattr(msg, "stopReason", None)
                return
            self.emit(msg)

        def child_ask(tool: str, args: dict) -> str:
            # 权限卡走主通道；args 前缀加 "[child]" 标记（UI 可据此标注子代理卡）
            return self.ask_permission(tool, {"[child]": True, **args})

        session = ToolSession(
            registry=child_registry,
            permissions=self.permissions,
            cwd=self.cwd,
            emit=child_emit,
            ask_permission=child_ask,
            review_diff=self.review_diff,
            cancel_event=cancel,
        )
        session.ctx.state["is_child"] = True  # 双保险：handler 侧也拒绝嵌套
        return AgentLoop(
            provider=provider,
            model=model_id,
            emit=child_emit,
            perf=PerfTracker(),
            system_prompt=CHILD_SYSTEM_PROMPT,
            max_turns=CHILD_MAX_TURNS,
            tool_session=session,
            cancel_event=cancel,
        )

    def _run_turn(
        self, loop: AgentLoop, prompt: str, timeout: float, child_cancel: threading.Event
    ) -> tuple[BaseException | None, bool, bool]:
        """线程内跑子回合；watcher 轮询主取消与超时 → 置子取消。"""
        box: dict = {"exc": None}

        def target() -> None:
            try:
                loop.run_user_text(prompt)
            except BaseException as exc:  # noqa: BLE001 —— 异常装箱返回，不炸主回合
                box["exc"] = exc

        thread = threading.Thread(target=target, daemon=True, name="zcode-child")
        thread.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        while thread.is_alive():
            thread.join(0.05)
            if self.main_cancel.is_set():
                child_cancel.set()  # 取消传播；继续等线程退场
            if time.monotonic() >= deadline:
                child_cancel.set()
                timed_out = True
                break
        if thread.is_alive():
            thread.join(CHILD_JOIN_GRACE_S)
        main_cancelled = self.main_cancel.is_set()
        return box["exc"], timed_out, main_cancelled

    @staticmethod
    def _coerce_timeout(value) -> float | None:
        if value is None:
            return DEFAULT_CHILD_TIMEOUT_S
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None
        return float(value)


def _handle_run_child(args: dict, ctx: ToolContext) -> ToolOutcome:
    if ctx.state.get("is_child"):
        return error_outcome(
            "run_child is disabled inside child agents (nesting depth is 1)"
        )
    runtime = ctx.state.get("child_runtime")
    if runtime is None:
        return error_outcome("run_child is not wired in this session (no child runtime)")
    return runtime.run(args)


RUN_CHILD_SPEC = ToolSpec(
    name="run_child",
    description=(
        "Run a child sub-agent with its own fresh context: give it a "
        "self-contained prompt; only its final text comes back as the result. "
        "Optional model override 'providerId/modelId' (e.g. a cheaper model for "
        "research subtasks); optional tools whitelist (default read/glob/grep/"
        "todo); optional timeout_sec (default 300). Cannot nest further."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "self-contained task for the child agent",
            },
            "model": {
                "type": "string",
                "description": "'providerId/modelId' or bare modelId to override the model",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "tool-name whitelist for the child (default read/glob/grep/todo)",
            },
            "timeout_sec": {"type": "number", "description": "child budget (default 300)"},
        },
        "required": ["prompt"],
    },
    tier="write",  # 派生代理（可能带写工具、可能花钱）默认走询问档
    handler=_handle_run_child,
)

__all__ = [
    "CHILD_SYSTEM_PROMPT",
    "DEFAULT_CHILD_TOOLS",
    "DEFAULT_CHILD_TIMEOUT_S",
    "RUN_CHILD_SPEC",
    "ChildRuntime",
    "resolve_child_model",
]
