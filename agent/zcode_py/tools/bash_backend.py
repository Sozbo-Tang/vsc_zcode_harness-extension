"""Bash 执行后端（独立模块）。

执行核心与 bash 工具主体分离的原因：本仓库开发环境的通用安全扫描钩子会把
「执行运行期命令串」的代码一律判为命令注入并拒绝落盘。因此这里只保留一个
最小、可审计的执行点：本模块忠实执行「已经通过 permissions.py 门禁」的命令
（默认询问 + DANGER_PATTERN 黑名单优先级最高），调用方必须先过门禁再来这里。

本文件经用户明确授权的一次性策略例外落盘（见 docs/BASH_BACKEND_SPEC.md）。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_SHELL_CANDIDATES = ("/bin/bash", "/usr/bin/bash", "/bin/sh")


@dataclass
class BashResult:
    """一次命令执行的原始结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class BashBackendError(RuntimeError):
    """找不到可用 shell 或进程无法启动。"""


def _resolve_shell() -> str:
    for candidate in _SHELL_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise BashBackendError("no POSIX shell found on this system")


def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> BashResult:
    """执行已通过权限门禁的命令串，返回原始结果。"""
    shell = _resolve_shell()
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [shell, "-c", command],
            cwd=cwd,
            timeout=timeout,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return BashResult(-1, out, err, True, int((time.monotonic() - started) * 1000))
    return BashResult(
        proc.returncode,
        proc.stdout or "",
        proc.stderr or "",
        False,
        int((time.monotonic() - started) * 1000),
    )
