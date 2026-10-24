"""MCP/hooks 进程执行后端（装配缝，见 docs/MCP_BACKEND_SPEC.md）。

按字面 argv 拉起子进程；权限门禁在调用方（permissions.py）。
本文件经用户明确授权的一次性策略例外落盘。
"""

from __future__ import annotations

import subprocess


def spawn_process(argv, *, env=None, cwd=None):
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


def run_process(argv, *, stdin=None, timeout=10.0, env=None, cwd=None):
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return -1, out, err, True
    return proc.returncode, proc.stdout or "", proc.stderr or "", False
