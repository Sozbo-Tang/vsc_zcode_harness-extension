"""bash 工具（M2 任务书）：L1 源头治理的第一现场。

流程（编排方决定，docs/BASH_BACKEND_SPEC.md）：
权限判定（默认询问，permissions.py + DANGER_PATTERN 黑名单最高优先级，
由 ToolSession 在 handler 之前完成）→ 本 handler 执行 → L1 截断 →
tool_result（带 durationMs）。

执行核心是**装配缝**：真正拉起 shell 的代码在 ``zcode_py/tools/bash_backend``
（``run_command(command,*,cwd,timeout,env) -> BashResult``）。该模块因环境
安全钩子（Mimosa Write 拦截一切执行动态命令串的代码，双向复现确认）无法
在本环境落盘——缺位时本工具返回结构化错误而非抛异常，loop 不受影响；
装配说明见 docs/BASH_BACKEND_SPEC.md。

- 输出超 3 万字符取头尾截断（任务书原文；PLAN §3.4 L1）。
- 非零退出**不抛异常**，结构化 ``{exitCode, stdout, stderr, …}`` 回灌
  （zcode Bash 语义：非零退出是结构化 status，见
  docs/research/zcode-semantics.md §2）。
- 子进程环境脱敏：名字含 API_KEY/SECRET/TOKEN/PASSWORD/CREDENTIAL 的变量
  一律剔除——本进程注入的 Provider Key 不随 bash 泄漏给任意命令。
- 超时默认 60s（timeout_ms 上限 600s）；后端超时杀进程，已产出部分输出
  仍随结果返回并标注 timedOut。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import MAX_TOOL_OUTPUT_CHARS, ToolOutcome, ToolSpec, error_outcome, head_tail

try:  # 装配缝：执行核心可能未装配（环境级钩子拦截），见模块 docstring
    from . import bash_backend
except ImportError:  # pragma: no cover —— 装配后分支不触发
    bash_backend = None

DEFAULT_TIMEOUT_MS = 60_000
MAX_TIMEOUT_MS = 600_000

_SECRET_ENV_RE = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PASSPHRASE)", re.IGNORECASE)

BACKEND_MISSING_DETAIL = (
    "bash 执行后端未装配：本环境安全钩子拦截了执行核心落盘，"
    "装配说明见 docs/BASH_BACKEND_SPEC.md"
)


def sanitize_child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """剔除名字带凭据意味的环境变量（默认取 os.environ 的拷贝）。"""
    source = dict(os.environ if env is None else env)
    return {k: v for k, v in source.items() if not _SECRET_ENV_RE.search(k)}


def tool_bash(args: dict, ctx) -> ToolOutcome:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return error_outcome("bash requires a non-empty string 'command'")
    if bash_backend is None:
        return ToolOutcome(
            result={"error": "backend_unavailable", "detail": BACKEND_MISSING_DETAIL},
            is_error=True,
        )
    timeout_ms = _clamp_timeout(args.get("timeout_ms"))
    cwd = ctx.cwd
    if isinstance(args.get("cwd"), str) and args["cwd"].strip():
        candidate = Path(args["cwd"]).expanduser()
        cwd = candidate if candidate.is_absolute() else ctx.cwd / candidate
        if not cwd.is_dir():
            return error_outcome(f"cwd does not exist: {cwd}")
    started = time.perf_counter()
    try:
        result = bash_backend.run_command(
            command,
            cwd=str(cwd),
            timeout=timeout_ms / 1000,
            env=sanitize_child_env(),
        )
    except bash_backend.BashBackendError as exc:
        return ToolOutcome(
            result={"error": "spawn_failed", "detail": str(exc)},
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001 —— 后端异常结构化回灌，不炸回合
        return ToolOutcome(
            result={"error": "backend_failed",
                    "detail": f"{type(exc).__name__}: {exc}"},
            is_error=True,
        )
    duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
    out, out_trunc = head_tail(result.stdout or "")
    err, err_trunc = head_tail(result.stderr or "")
    payload = {
        "command": command,
        "exitCode": result.exit_code,
        "stdout": out,
        "stderr": err,
        "timedOut": bool(result.timed_out),
        "truncated": bool(out_trunc or err_trunc),
        "outputBudget": MAX_TOOL_OUTPUT_CHARS,
        "durationMs": duration_ms,
    }
    if result.timed_out:
        payload["error"] = "timeout"
        payload["detail"] = f"command exceeded {timeout_ms}ms and was killed; partial output included"
        return ToolOutcome(result=payload, is_error=True)
    return ToolOutcome(result=payload)


def _clamp_timeout(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT_MS
    return max(1000, min(int(value), MAX_TIMEOUT_MS))


BASH_SPEC = ToolSpec(
    name="bash",
    description=(
        "Run a shell command and return structured output "
        "{exitCode, stdout, stderr, durationMs}. Non-zero exit is data, not an "
        f"error. Output is head+tail truncated at {MAX_TOOL_OUTPUT_CHARS} chars; "
        f"timeout default {DEFAULT_TIMEOUT_MS // 1000}s (max {MAX_TIMEOUT_MS // 1000}s)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_ms": {"type": "integer", "description": "kill after this many ms"},
            "cwd": {"type": "string", "description": "working directory (default workspace root)"},
        },
        "required": ["command"],
    },
    tier="write",
    handler=tool_bash,
)
