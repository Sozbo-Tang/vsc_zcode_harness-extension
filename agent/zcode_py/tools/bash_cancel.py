"""可取消命令执行（task_stop 真停的核心侧受控实现，M9 补完）。

装配缝约束（docs/BASH_BACKEND_SPEC.md）：真正「拉起 shell 执行动态命令串」
的代码只能存在于用户手工维护的 ``bash_backend.py``——本环境安全钩子拦截
一切该形态代码落盘。因此可取消执行拆成两层分工：

- **装配缝薄接口** ``bash_backend.spawn_command(command, *, cwd, env) ->
  subprocess.Popen``：以 ``start_new_session=True`` 拉起（独立进程组），
  只负责拉起、不做生命周期管理；
- **本模块（核心侧受控文件）**：登记 Popen → 超时/取消时按**进程组**发
  信号（SIGTERM → 宽限 ``TERM_GRACE_S`` 秒 → 仍活 SIGKILL）→ 收割通信
  并组装 ``BashResult``。取消与超时共用同一信号序列。

装配缝缺 ``spawn_command``（用户未按 SPEC 手工同步）时回退到整段阻塞的
``run_command``——取消退化为旧的标记式语义（``cancel`` 返回 ``"missing"``，
调用方如实提示），不炸任何链路。

安全契约：与 bash_backend 相同——本模块不做权限判定，命令必须已过
``permissions.py`` 门禁（DANGER 黑名单优先级最高）；对登记进程只发信号
与收割，不解析、不重试。进程组语义依赖 POSIX（``start_new_session`` /
``os.killpg``），与 bash_backend 的 POSIX-only 前提一致（Windows 留待 M9）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from . import bash_backend

# SIGTERM 后的宽限：仍活才升级 SIGKILL（任务书：2s）
TERM_GRACE_S = 2.0


def terminate_process_tree(proc: Any, *, grace_s: float = TERM_GRACE_S) -> list[str]:
    """按进程组终止 ``proc``：SIGTERM → ``grace_s`` 内退即止；仍活 SIGKILL。

    返回实际发出的信号名序列（可观测性 + 测试断言，如 ``["SIGTERM"]`` 或
    ``["SIGTERM", "SIGKILL"]``）。进程已死（``getpgid``/``killpg`` 抛
    ``ProcessLookupError``）时静默返回已发部分。进程组取 ``os.getpgid(pid)``
    ——装配缝以 ``start_new_session=True`` 拉起，pgid == pid，组内孙进程
    一并收到信号（只杀直接子进程会留下孤儿管道持有者）。

    已知边界（如实声明）：``wait`` 只等直接子进程；若组外进程（命令自己
    ``setsid`` 逃脱）持有 stdout 管道，调用方的 ``communicate`` 可能仍阻塞
    到其退出——属命令自身的对抗行为，权限门禁与黑名单是第一道防线。
    """
    sent: list[str] = []
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return sent
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return sent
        sent.append(signal.Signals(sig).name)
        try:
            proc.wait(timeout=grace_s)
            return sent
        except subprocess.TimeoutExpired:
            continue
    # SIGKILL 后仍未退（D sleep 等不可杀状态）：best-effort 再等一轮即返回
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:  # pragma: no cover —— 内核级挂死，难稳定复现
        pass
    return sent


class CancelRegistry:
    """token → Popen 登记表（线程安全）；stop 与工作线程并发访问。

    生命周期约定：``run_tracked`` 在拉起后登记、收割完注销；期间任意线程
    可 ``cancel(token)`` 触发信号序列——工作线程阻塞中的 ``communicate``
    会因进程（组）死亡而返回，由调用方的状态机收尾。
    """

    def __init__(self, *, grace_s: float = TERM_GRACE_S):
        self._grace_s = grace_s
        self._lock = threading.Lock()
        self._procs: dict[str, Any] = {}

    # ------------------------------------------------------------------ 登记

    def register(self, token: str, proc: Any) -> None:
        with self._lock:
            self._procs[token] = proc

    def unregister(self, token: str) -> None:
        with self._lock:
            self._procs.pop(token, None)

    # ------------------------------------------------------------------ 取消

    def cancel(self, token: str) -> str:
        """真停：对登记进程发信号序列。

        返回 ``"killed"``（已发信号）| ``"missing"``（无登记——命令已结束、
        尚未拉起，或装配缝未升级走回退路径）。只发信号不等收割：终态由
        工作线程的 ``task_finished`` 帧为准（UI 侧约定同款）。
        """
        with self._lock:
            proc = self._procs.get(token)
        if proc is None:
            return "missing"
        terminate_process_tree(proc, grace_s=self._grace_s)
        return "killed"

    # ------------------------------------------------------------------ 执行

    def run_tracked(
        self,
        token: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = 120.0,
        env: dict[str, str] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Any:
        """经装配缝拉起并阻塞收割；登记期间可被 ``cancel(token)`` 真停。

        - 装配缝有 ``spawn_command``：Popen 路径（可取消 + 超时按进程组杀）；
        - 缺位：回退 ``run_command``（整段阻塞，取消只剩调用方标记位）。
        - ``should_cancel``：拉起并登记后的立即复检——覆盖「stop 先于登记
          到达」的启动竞态（标记已置但登记表里还没有进程）。
        返回 ``bash_backend.BashResult``（超时 ``timed_out=True``，取消路径
        交由调用方按标记位归入 cancelled）。
        """
        spawn = getattr(bash_backend, "spawn_command", None)
        if spawn is None:
            # 装配缝未升级：保底旧语义（模块 docstring「回退」分支）
            effective = 120.0 if timeout is None else float(timeout)
            return bash_backend.run_command(
                command, cwd=cwd, timeout=effective, env=env
            )
        started = time.monotonic()
        proc = spawn(command, cwd=cwd, env=env)
        self.register(token, proc)
        try:
            if should_cancel is not None and should_cancel():
                # stop 先于登记到达：立即按同一信号序列终止再收割
                terminate_process_tree(proc, grace_s=self._grace_s)
                out, err = proc.communicate()
            else:
                try:
                    out, err = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # 超时与取消共用信号序列；残余输出随结果返回
                    terminate_process_tree(proc, grace_s=self._grace_s)
                    out, err = proc.communicate()
                    return bash_backend.BashResult(
                        -1, out or "", err or "", True,
                        int((time.monotonic() - started) * 1000),
                    )
            return bash_backend.BashResult(
                proc.returncode if proc.returncode is not None else -1,
                out or "",
                err or "",
                False,
                int((time.monotonic() - started) * 1000),
            )
        finally:
            self.unregister(token)


__all__ = ["TERM_GRACE_S", "CancelRegistry", "terminate_process_tree"]
