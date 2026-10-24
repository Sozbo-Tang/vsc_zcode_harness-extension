"""task_stop 真停测试（M9 补完）：bash_cancel 信号序列 + 注册表 + run_tracked。

全部 monkeypatch 假进程（FakeProc）与假 os.killpg/os.getpgid——不真拉起
任何子进程（BASH_BACKEND_SPEC 测试约定）。装配缝 spawn_command 经
monkeypatch.setattr(..., raising=False) 注入，不依赖真实装配状态（用户
手工同步前后测试都成立：注入走真停路径，delattr 走回退路径）。
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time

import pytest

from zcode_py.background import BackgroundTaskManager
from zcode_py.protocol import encode_message
from zcode_py.tools import bash_backend, bash_cancel
from zcode_py.tools.bash_backend import BashResult


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def as_dict(frame):
    return frame if isinstance(frame, dict) else encode_message(frame)


# ---------------------------------------------------------------------------
# 假进程与假信号原语
# ---------------------------------------------------------------------------


class FakeProc:
    """假 Popen：returncode 由 killpg 假件置位（进程死亡）；communicate 阻塞
    到死亡（管道关闭）或超时。auto_result 非 None = 命令自行跑完。"""

    def __init__(self, pid=4242, *, term_works=True, auto_result=None):
        self.pid = pid
        self.returncode = 0 if auto_result is not None else None
        self.auto_result = auto_result  # (stdout, stderr)
        self.term_works = term_works  # False = 忽略 SIGTERM，只有 SIGKILL 有效
        self.dead = threading.Event()
        if auto_result is not None:
            self.dead.set()
        self.communicate_log: list[float | None] = []
        self.wait_log: list[float | None] = []

    def wait(self, timeout=None):
        self.wait_log.append(timeout)
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    def communicate(self, timeout=None):
        self.communicate_log.append(timeout)
        if self.auto_result is not None:
            return self.auto_result
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            if self.dead.wait(0.005):
                break
        return (f"out-of-{self.returncode}", "err-tail")


class SignalHarness:
    """假 os.getpgid/os.killpg：按 pgid 路由到 FakeProc 并记录信号序列。
    vanish_after_term=True 模拟「SIGTERM 送达后整组消失」（孙进程清场）。"""

    def __init__(self):
        self.signals: list[tuple[int, str]] = []
        self.procs: dict[int, FakeProc] = {}
        self._lock = threading.Lock()
        self.vanish_after_term = False

    def bind(self, proc: FakeProc) -> FakeProc:
        self.procs[proc.pid] = proc
        return proc

    def getpgid(self, pid):
        if pid not in self.procs:
            raise ProcessLookupError(f"no such pid {pid}")
        return pid  # start_new_session 语义：pgid == pid

    def killpg(self, pgid, sig):
        proc = self.procs.get(pgid)
        if proc is None:
            raise ProcessLookupError(f"no such pgid {pgid}")
        name = signal.Signals(sig).name
        with self._lock:
            self.signals.append((pgid, name))
        if sig == signal.SIGKILL:
            proc.returncode = -9
            proc.dead.set()
        elif proc.term_works:
            proc.returncode = -15
            proc.dead.set()
        # term_works=False：SIGTERM 被进程忽略——不置死，等宽限后 SIGKILL
        if self.vanish_after_term and sig == signal.SIGTERM:
            self.procs.pop(pgid, None)

    def names(self) -> list[str]:
        with self._lock:
            return [name for _, name in self.signals]

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(bash_cancel.os, "getpgid", self.getpgid)
        monkeypatch.setattr(bash_cancel.os, "killpg", self.killpg)


@pytest.fixture
def harness(monkeypatch):
    h = SignalHarness()
    h.install(monkeypatch)
    return h


def fake_spawn(harness: SignalHarness, proc: FakeProc):
    def _spawn(command, *, cwd=None, env=None):
        harness.bind(proc)
        return proc

    return _spawn


def inject_spawn(monkeypatch, fn) -> None:
    monkeypatch.setattr(bash_backend, "spawn_command", fn, raising=False)


def drop_spawn(monkeypatch) -> None:
    monkeypatch.delattr(bash_backend, "spawn_command", raising=False)


# ---------------------------------------------------------------------------
# terminate_process_tree：信号序列（任务书：SIGTERM 进程组 → 2s 仍活 SIGKILL）
# ---------------------------------------------------------------------------


def test_terminate_sigterm_suffices_no_kill(harness):
    proc = harness.bind(FakeProc(term_works=True))
    sent = bash_cancel.terminate_process_tree(proc, grace_s=0.05)
    assert sent == ["SIGTERM"]
    assert proc.returncode == -15


def test_terminate_escalates_to_sigkill_when_term_ignored(harness):
    proc = harness.bind(FakeProc(term_works=False))
    sent = bash_cancel.terminate_process_tree(proc, grace_s=0.05)
    assert sent == ["SIGTERM", "SIGKILL"]
    assert proc.returncode == -9
    # 时序：TERM 后先宽限等待（超时）才升级 KILL（两次 wait 均带超时）
    assert len(proc.wait_log) == 2 and all(t is not None for t in proc.wait_log)


def test_terminate_dead_process_sends_nothing(harness):
    proc = harness.bind(FakeProc())
    proc.returncode = 0  # 已退出：getpgid 抛 ProcessLookupError
    harness.procs.pop(proc.pid)
    sent = bash_cancel.terminate_process_tree(proc, grace_s=0.05)
    assert sent == []
    assert harness.names() == []


def test_terminate_group_vanishes_after_term(harness):
    """SIGTERM 送达后整组消失（killpg ProcessLookupError）：返回已发部分。"""
    harness.vanish_after_term = True
    proc = harness.bind(FakeProc(term_works=False))
    sent = bash_cancel.terminate_process_tree(proc, grace_s=0.05)
    assert sent == ["SIGTERM"]


# ---------------------------------------------------------------------------
# CancelRegistry：登记 / 取消 / 未知 token
# ---------------------------------------------------------------------------


def test_registry_cancel_missing_token(harness):
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    assert registry.cancel("bg_nope") == "missing"
    assert harness.names() == []


def test_registry_cancel_sends_signals(harness):
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    proc = harness.bind(FakeProc(term_works=False))
    registry.register("bg_1", proc)
    assert registry.cancel("bg_1") == "killed"
    assert harness.names() == ["SIGTERM", "SIGKILL"]
    assert proc.returncode == -9


# ---------------------------------------------------------------------------
# run_tracked：回退 / 正常完成 / 超时 / 取消 / 启动竞态
# ---------------------------------------------------------------------------


def test_run_tracked_falls_back_without_spawn(monkeypatch):
    drop_spawn(monkeypatch)
    calls: list[dict] = []

    def fake_run(command, *, cwd=None, timeout=120.0, env=None):
        calls.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        return BashResult(7, "fb", "", False, 3)

    monkeypatch.setattr(bash_backend, "run_command", fake_run)
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    result = registry.run_tracked("bg_fb", "echo x", cwd="/tmp", timeout=9.0, env={"A": "1"})
    assert result.exit_code == 7 and result.stdout == "fb" and result.timed_out is False
    assert calls == [{"command": "echo x", "cwd": "/tmp", "timeout": 9.0, "env": {"A": "1"}}]
    assert registry.cancel("bg_fb") == "missing"  # 回退路径无登记 → mark-only


def test_run_tracked_normal_completion(harness, monkeypatch):
    proc = FakeProc(auto_result=("done-out", "done-err"))
    inject_spawn(monkeypatch, fake_spawn(harness, proc))
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    result = registry.run_tracked("bg_ok", "echo done", cwd="/w", timeout=30.0, env={})
    assert result.exit_code == 0 and result.stdout == "done-out"
    assert result.stderr == "done-err" and result.timed_out is False
    assert result.duration_ms >= 0
    assert harness.names() == []
    assert registry.cancel("bg_ok") == "missing"  # finally 已注销


def test_run_tracked_timeout_kills_group_with_partial_output(harness, monkeypatch):
    proc = FakeProc(term_works=False)  # 超时杀时顺带验证 TERM→KILL 升级
    inject_spawn(monkeypatch, fake_spawn(harness, proc))
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    result = registry.run_tracked("bg_slow", "sleep 100", cwd=None, timeout=0.15, env=None)
    assert result.timed_out is True and result.exit_code == -1
    assert "out-of--9" in result.stdout  # 收割残余输出仍随结果返回
    assert harness.names() == ["SIGTERM", "SIGKILL"]
    assert proc.communicate_log[0] == 0.15 and proc.communicate_log[1] is None


def test_run_tracked_cancel_from_other_thread(harness, monkeypatch):
    """真停主路径：工作线程阻塞收割中，另一线程 cancel → 进程组被杀、
    communicate 随管道关闭返回（timed_out=False，终态交调用方状态机）。"""
    proc = FakeProc(term_works=True)
    inject_spawn(monkeypatch, fake_spawn(harness, proc))
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    box: dict = {}

    def worker():
        box["result"] = registry.run_tracked("bg_live", "long", cwd=None, timeout=30.0, env=None)

    thread = threading.Thread(target=worker)
    thread.start()
    assert wait_for(lambda: "bg_live" in registry._procs)
    assert registry.cancel("bg_live") == "killed"
    assert harness.names() == ["SIGTERM"]
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    result = box["result"]
    assert result.timed_out is False and result.exit_code == -15


def test_run_tracked_cancel_before_register_race(harness, monkeypatch):
    """启动竞态：stop 先于登记到达——should_cancel 复检立即按序列终止。"""
    proc = FakeProc(term_works=False)
    inject_spawn(monkeypatch, fake_spawn(harness, proc))
    registry = bash_cancel.CancelRegistry(grace_s=0.05)
    result = registry.run_tracked(
        "bg_race", "long", cwd=None, timeout=30.0, env=None,
        should_cancel=lambda: True,
    )
    assert harness.names() == ["SIGTERM", "SIGKILL"]
    assert result.exit_code == -9 and result.timed_out is False


# ---------------------------------------------------------------------------
# BackgroundTaskManager 集成：stop 真停端到端（假进程，无真实子进程）
# ---------------------------------------------------------------------------


def make_manager(emitted, tmp_path, grace=0.05):
    return BackgroundTaskManager(
        emit=emitted.append,
        cwd_supplier=lambda: tmp_path,
        tick_interval=0.05,
        cancel_registry=bash_cancel.CancelRegistry(grace_s=grace),
    )


def test_manager_stop_really_kills_running_task(monkeypatch, tmp_path):
    harness = SignalHarness()
    harness.install(monkeypatch)
    proc = FakeProc(term_works=True)
    inject_spawn(monkeypatch, fake_spawn(harness, proc))
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    record = manager.start("longrun")

    ok, message = manager.stop(record["taskId"])
    assert ok and "SIGTERM" in message and "mark-only" not in message
    assert harness.names() == ["SIGTERM"]
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "cancelled")
    final = manager.get(record["taskId"])
    assert final["exitCode"] == -15 and final["cancelRequested"] is True
    finished = [as_dict(f) for f in emitted if as_dict(f).get("type") == "task_finished"]
    assert finished and finished[-1]["exitCode"] == -15


def test_manager_stop_ignores_sigterm_still_kills(monkeypatch, tmp_path):
    harness = SignalHarness()
    harness.install(monkeypatch)
    proc = FakeProc(term_works=False)  # 顽固进程：TERM 不理 → KILL 兜底
    inject_spawn(monkeypatch, fake_spawn(harness, proc))
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    record = manager.start("stubborn")

    ok, _ = manager.stop(record["taskId"])
    assert ok
    assert harness.names() == ["SIGTERM", "SIGKILL"]
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "cancelled")
    assert manager.get(record["taskId"])["exitCode"] == -9


def test_manager_fallback_stop_is_mark_only(monkeypatch, tmp_path):
    """装配缝未同步（无 spawn_command）：stop 退化为标记式并如实提示。"""
    drop_spawn(monkeypatch)
    release = threading.Event()

    def fake_run(command, *, cwd=None, timeout=120.0, env=None):
        release.wait(5.0)
        return BashResult(0, "late", "", False, 100)

    monkeypatch.setattr(bash_backend, "run_command", fake_run)
    manager = make_manager([], tmp_path)
    record = manager.start("fallback")

    ok, message = manager.stop(record["taskId"])
    assert ok and "mark-only" in message and "spawn_command" in message
    assert manager.get(record["taskId"])["state"] == "running"  # 命令仍阻塞
    release.set()
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "cancelled")
