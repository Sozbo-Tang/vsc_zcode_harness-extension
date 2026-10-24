"""envprobe 共用的子进程执行器（字面量 argv，R7/R21）。

安全约束：**只用字面量 argv 列表调用 subprocess.run**——绝无 shell=True、
绝无拼接命令串（环境安全钩子会拦截一切 shell 形态调用）。

失败语义（「字段尽力探测」）：进程不存在 / 超时 / 非零退出码都返回 None，
由调用方把对应字段降级为 None，不抛异常。
"""

from __future__ import annotations

import subprocess

TIMEOUT_S = 5.0


def run_argv(argv: list[str], *, timeout: float = TIMEOUT_S) -> subprocess.CompletedProcess | None:
    """执行一条字面量 argv 命令；任何失败（不存在/超时/非零退出码）→ None。"""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc


def completed_output(proc: subprocess.CompletedProcess | None) -> str | None:
    """取命令输出：stdout 优先、stderr 兜底（``java -version`` 写 stderr 的老牌
    约定）；无有效输出返回 None。"""
    if proc is None:
        return None
    out = (proc.stdout or "").strip()
    if out:
        return out
    return (proc.stderr or "").strip() or None
