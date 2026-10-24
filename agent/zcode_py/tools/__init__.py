"""工具系统：声明式注册表 + 执行会话（PLAN §3.1 tools/、M2 任务书）。

结构参照 zcode「契约/实现分离 + 声明式工具契约」语义
（docs/research/zcode-semantics.md §借鉴①）：

- :class:`ToolSpec` 声明 name/description/parameters(JSON Schema)/tier
  （readonly 免批 | write 默认询问）+ handler；
- :class:`ToolRegistry` 聚合 specs 并产出 OpenAI function 风格 schema
  （llm/base.LLMRequest.tools 约定的内部统一格式，各 Provider 适配器自行
  转换协议形态）；
- :class:`ToolSession` 在 loop 与 handler 之间执行完整编排：
  权限判定（需确认时经 ``ask_permission`` 回调广播 permission_request 并
  阻塞等待）→ 执行（L1 截断在 handler 内）→ 隐私 warning 透传。

edit 的确认交互不是权限卡而是 diff 审阅卡：无条件广播 diff_proposed、
收到 diff_review{accepted:true} 才写盘（任务书原文），权限引擎的
白名单/always 不绕过审阅——编辑是最敏感操作，M2 一律人审。

安全默认：未接 ``ask_permission`` 回调时写类工具一律拒绝（fail-closed），
未接 ``review_diff`` 回调时 edit 一律拒绝——离线单测需显式注入放行回调。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..permissions import ASK, PermissionEngine
from ..protocol import PermissionRequest

# L1 源头治理常量（PLAN §3.4：bash 超 3 万字符取头尾截断）
MAX_TOOL_OUTPUT_CHARS = 30_000


def head_tail(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> tuple[str, bool]:
    """超限取头尾：保留头 limit/2 + 尾 limit/2，中间以省略标记替换。

    返回 (文本, 是否截断)。不超限原样返回。
    """
    if len(text) <= limit:
        return text, False
    head_n = limit // 2
    tail_n = limit - head_n
    omitted = len(text) - limit
    marker = f"\n... [{omitted} chars omitted — output truncated head+tail (L1)] ...\n"
    return text[:head_n] + marker + text[-tail_n:], True


# ---------------------------------------------------------------------------
# 契约与结果
# ---------------------------------------------------------------------------


@dataclass
class ToolOutcome:
    """工具执行结果。is_error=True 时结果作为 tool_result(is_error) 回灌。"""

    result: dict = field(default_factory=dict)
    is_error: bool = False
    warning: str | None = None


@dataclass
class ToolContext:
    """handler 执行环境（每会话一份，随 ToolSession 构造）。"""

    cwd: Path = field(default_factory=Path.cwd)
    # edit 的 diff 审阅闸门：(file, old_text, new_text) -> accepted；None = 拒绝
    review_diff: Callable[[str, str, str], bool] | None = None
    # 会话级共享状态（todo 工具列表等）
    state: dict = field(default_factory=dict)
    cancel_event: threading.Event | None = None


Handler = Callable[[dict, ToolContext], ToolOutcome]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    tier: str = "readonly"  # readonly | write
    handler: Handler | None = None


def error_outcome(detail: str, *, kind: str = "invalid_args") -> ToolOutcome:
    return ToolOutcome(result={"error": kind, "detail": detail}, is_error=True)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        """OpenAI function 风格工具定义（LLMRequest.tools 的内部统一格式）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in (self._tools[k] for k in sorted(self._tools))
        ]


def default_registry() -> ToolRegistry:
    """M2 内置九件套：read/glob/grep/todo（只读）+ write/edit + bash +
    webfetch/websearch。"""
    from . import bash as _bash
    from . import edit as _edit
    from . import files as _files
    from . import todo as _todo
    from . import web as _web

    registry = ToolRegistry()
    for spec in (
        _files.READ_SPEC,
        _files.GLOB_SPEC,
        _files.GREP_SPEC,
        _todo.TODO_SPEC,
        _edit.WRITE_SPEC,
        _edit.EDIT_SPEC,
        _bash.BASH_SPEC,
        _web.WEBFETCH_SPEC,
        _web.WEBSEARCH_SPEC,
    ):
        registry.register(spec)
    return registry


# ---------------------------------------------------------------------------
# 执行会话：loop ↔ handler 之间的编排层
# ---------------------------------------------------------------------------

AskPermission = Callable[[str, dict], str]  # (tool, args) -> allow|deny|always


class ToolSession:
    """loop 每次 tool_use 调用 :meth:`run`；线程语义＝阻塞直到执行完成。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        cwd: Path,
        emit: Callable[[Any], None],
        ask_permission: AskPermission | None = None,
        review_diff: Callable[[str, str, str], bool] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.registry = registry
        self.permissions = permissions
        self.ctx = ToolContext(cwd=cwd, review_diff=review_diff, cancel_event=cancel_event)
        self._emit = emit
        self._ask_permission = ask_permission

    def schemas(self) -> list[dict]:
        return self.registry.schemas()

    def cancelled(self) -> bool:
        return self.ctx.cancel_event is not None and self.ctx.cancel_event.is_set()

    def run(self, name: str, args: dict, call_id: str) -> ToolOutcome:
        spec = self.registry.get(name)
        if spec is None or spec.handler is None:
            return ToolOutcome(
                result={"error": "unknown_tool", "detail": f"no tool named {name!r}"},
                is_error=True,
            )
        if self.cancelled():
            return ToolOutcome(
                result={"error": "cancelled", "detail": "turn cancelled before execution"},
                is_error=True,
            )
        # edit：无条件 diff 审阅闸门（任务书），不走权限卡
        if name != "edit":
            check = self.permissions.check(name, args)
            if check.outcome == ASK:
                decision = self._ask(name, args)
                if decision not in ("allow", "always"):
                    detail = {
                        "deny": "user denied the permission request",
                        "timeout": "permission request timed out without a user answer",
                        "cancelled": "turn cancelled while waiting for permission",
                    }.get(decision, f"permission not granted ({decision})")
                    return ToolOutcome(
                        result={"error": "permission_denied", "detail": detail,
                                "ruleId": check.ruleId},
                        is_error=True,
                    )
                if decision == "always":
                    pattern = self.permissions.remember_always(name, args)
                    self._emit_permission_note(name, pattern)
        try:
            return spec.handler(args, self.ctx)
        except Exception as exc:  # noqa: BLE001 —— 工具异常结构化回灌，不炸回合
            return ToolOutcome(
                result={"error": "tool_failed",
                        "detail": f"{type(exc).__name__}: {exc}"},
                is_error=True,
            )

    # -- 内部 ------------------------------------------------------------------

    def _ask(self, name: str, args: dict) -> str:
        if self._ask_permission is None:
            return "deny"  # fail-closed：没人应答权限卡就不放行
        return self._ask_permission(name, args)

    def _emit_permission_note(self, name: str, pattern: str) -> None:
        # always 记忆对 transcript 可审计：复用 permission_request 之外的轻量
        # 方案是放入 tool 结果，这里选择不额外造协议帧，仅内部记录。
        self.ctx.state.setdefault("always_rules", []).append(f"{name}: {pattern}")


__all__ = [
    "MAX_TOOL_OUTPUT_CHARS",
    "AskPermission",
    "Handler",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSession",
    "ToolSpec",
    "default_registry",
    "error_outcome",
    "head_tail",
    "PermissionRequest",
]
