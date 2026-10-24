"""Hooks（M8，R3）：~/.zcode-py/hooks.json 的 pre_tool / post_tool 钩子。

配置格式（顶层 JSON 数组；兼容 ``{"hooks": [...]}`` 包裹形态）::

    [
      {"event": "pre_tool", "tool": "bash", "command": ["python", "guard.py"]},
      {"event": "post_tool", "tool": "*", "command": ["python", "log.py"]}
    ]

- ``event``：``pre_tool`` | ``post_tool``；
- ``tool``（可选）：glob，缺省 ``*`` 匹配一切工具；
- ``command``：**argv 列表**（用户配置，无 shell 拼接）。

执行语义（任务书）：

- **pre_tool**：stdin 收 ``{"event","tool","args"}`` JSON；退出码非 0 = 阻断，
  stdout 作为原因进 tool_result（is_error）；退出码 0 = 放行（stdout 忽略）；
- **post_tool**：stdin 同上；stdout 附加到 tool_call finished 事件
  （ToolOutcome.result["postHook"]，见 tools/__init__.py 的接线）；
- 多条命中规则按序执行：pre_tool 任一阻断即阻断（首条原因返回）；
  post_tool 的 stdout 逐条拼接。

**装配缝**：钩子进程启动与 MCP 共用同一个执行缝（``zcode_py.mcp.proc_backend``
的 ``run_process``——argv 列表 + stdin 喂入 + 超时）。缝未装配时：hooks
配置存在则广播**一次** ``error{kind:"config"}``（指引 docs/MCP_BACKEND_SPEC.md）
后禁用——**绝不阻断工具**（hooks 是旁路增强，不是门禁）。

测试全部打桩（monkeypatch run_process / proc_backend），零真实子进程。
"""

from __future__ import annotations

import fnmatch
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import home_dir
from .protocol import Error

EVENTS = ("pre_tool", "post_tool")
DEFAULT_TIMEOUT_S = 10.0
STDOUT_CAP_CHARS = 2000

BACKEND_MISSING_MESSAGE = (
    "hooks 进程装配缝未落盘（zcode_py.mcp.proc_backend.run_process）："
    "本环境安全钩子拦截了执行核心落盘，装配说明见 docs/MCP_BACKEND_SPEC.md。"
    "hooks 已禁用（不影响工具执行）；落盘后重启会话生效。"
)


class HooksConfigError(ValueError):
    """hooks.json 结构非法。"""


@dataclass(frozen=True)
class HookRule:
    event: str  # pre_tool | post_tool
    tool: str  # glob（缺省 *）
    command: list  # argv 列表
    timeout_s: float = DEFAULT_TIMEOUT_S

    def matches(self, event: str, tool: str) -> bool:
        return self.event == event and fnmatch.fnmatch(tool, self.tool)


def hooks_path() -> Path:
    return home_dir() / "hooks.json"


def parse_hooks(data: object) -> list[HookRule]:
    """配置对象 → 规则列表（顶层数组或 {"hooks": [...]}）。"""
    if isinstance(data, dict):
        data = data.get("hooks")
    if data is None:
        return []
    if not isinstance(data, list):
        raise HooksConfigError("hooks.json must be an array (or {'hooks': [...]})")
    rules: list[HookRule] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise HooksConfigError(f"hooks[{i}] must be an object")
        event = entry.get("event")
        if event not in EVENTS:
            raise HooksConfigError(f"hooks[{i}].event must be one of {list(EVENTS)}")
        command = entry.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(a, str) and a.strip() for a in command)
        ):
            raise HooksConfigError(f"hooks[{i}].command must be a non-empty argv list of strings")
        tool = entry.get("tool") or "*"
        if not isinstance(tool, str) or not tool.strip():
            raise HooksConfigError(f"hooks[{i}].tool must be a non-empty glob string")
        rules.append(HookRule(event=event, tool=tool.strip(), command=list(command)))
    return rules


def load_hooks(path: Path | None = None) -> list[HookRule]:
    """读 hooks.json；文件不存在返回空（hooks 关闭）。损坏抛 HooksConfigError。"""
    target = path or hooks_path()
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HooksConfigError(f"cannot read {target}: {exc}") from exc
    return parse_hooks(data)


def _proc_backend():
    """装配缝 import（缺位返回 None）。"""
    try:
        from .mcp import proc_backend
    except ImportError:
        return None
    return proc_backend


@dataclass
class HookRunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class HooksRunner:
    """ToolSession 的 hooks 执行器（ctx.state["hooks"] 注入）。

    缝未装配且存在规则时：首次执行（或显式 startup_check）广播一次
    ``error{kind:"config"}`` 后置 disabled——之后 pre/post 全部直通。
    """

    def __init__(
        self,
        rules: list[HookRule],
        *,
        emit=None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        runner=None,
    ):
        self.rules = list(rules)
        self.emit = emit
        self.timeout_s = timeout_s
        self.disabled = False
        self._notified = False
        self._lock = threading.Lock()
        # 测试注入口：runner(command_argv, payload_json) -> HookRunResult
        self._runner = runner

    # ------------------------------------------------------------------ 生命周期

    def startup_check(self) -> None:
        """服务 init 时调用：有规则但缝缺位 → 广播一次 config error 后禁用。"""
        if not self.rules or self.disabled:
            return
        if self._runner is not None or _proc_backend() is not None:
            return
        self._notify_disabled()

    def _notify_disabled(self) -> None:
        with self._lock:
            if self._notified:
                return
            self._notified = True
            self.disabled = True
        if self.emit is not None:
            self.emit(Error(kind="config", message=BACKEND_MISSING_MESSAGE))

    # ------------------------------------------------------------------ 执行

    def pre(self, tool: str, args: dict) -> tuple[bool, str | None]:
        """pre_tool 钩子 → (blocked, reason)。无规则/禁用/异常 → (False, None)。"""
        results = self._run_event("pre_tool", tool, args)
        for rule, run in results:
            if run.exit_code != 0:
                reason = (run.stdout or run.stderr or "").strip()
                if run.timed_out:
                    reason = f"pre_tool hook timed out after {rule.timeout_s:g}s (blocked)"
                elif not reason:
                    reason = f"pre_tool hook exited {run.exit_code} with no output"
                return True, reason[:STDOUT_CAP_CHARS]
        return False, None

    def post(self, tool: str, args: dict) -> str | None:
        """post_tool 钩子 → 拼接的 stdout（无输出返回 None）。"""
        chunks: list[str] = []
        for _rule, run in self._run_event("post_tool", tool, args):
            text = (run.stdout or "").strip()
            if text:
                chunks.append(text[:STDOUT_CAP_CHARS])
        return "\n".join(chunks) if chunks else None

    # ------------------------------------------------------------------ 内部

    def _run_event(self, event: str, tool: str, args: dict):
        if self.disabled or not self.rules:
            return []
        matched = [r for r in self.rules if r.matches(event, tool)]
        if not matched:
            return []
        backend = self._runner
        if backend is None:
            backend = _proc_backend()
            if backend is None:
                self._notify_disabled()
                return []
            backend = backend.run_process
        payload = json.dumps({"event": event, "tool": tool, "args": args}, ensure_ascii=False)
        out = []
        for rule in matched:
            out.append((rule, self._run_one(backend, rule, payload)))
        return out

    def _run_one(self, backend, rule: HookRule, payload: str) -> HookRunResult:
        try:
            exit_code, stdout, stderr, timed_out = backend(
                rule.command,
                stdin=payload,
                timeout=rule.timeout_s,
            )
            return HookRunResult(int(exit_code), str(stdout or ""), str(stderr or ""), bool(timed_out))
        except Exception as exc:  # noqa: BLE001 —— 钩子崩溃不阻断工具（旁路，非门禁）
            return HookRunResult(0, "", f"hook crashed: {type(exc).__name__}: {exc}", False)


__all__ = [
    "BACKEND_MISSING_MESSAGE",
    "EVENTS",
    "HookRule",
    "HookRunResult",
    "HooksConfigError",
    "HooksRunner",
    "hooks_path",
    "load_hooks",
    "parse_hooks",
]
