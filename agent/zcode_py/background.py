"""M9 后台任务（R3，任务面板数据源，PLAN §2.1 / §7 M9）。

`background_start{command, name?}` control 的执行核心：对每条任务起一个
daemon 线程跑 ``bash_cancel.CancelRegistry.run_tracked``（经装配缝
``bash_backend.spawn_command`` 拉起、timeout 参数透传，默认 3600s）。
**必须走 bash_backend/bash_cancel**——动态命令执行在本环境被安全钩子拦截，
bash_backend 是唯一已装配的真实执行点（docs/BASH_BACKEND_SPEC.md），后台
任务不得绕开它另起 subprocess。

帧契约（protocol.py §5 风格）：

- ``task_started{taskId, name, command}`` 启动即发；
- ``task_output{taskId, tail}`` 输出尾部（≤8KB 环形，见 ``TAIL_LIMIT``）。
  监测线程在尾部变化时至多每 2s 广播一次，命令结束时必发一次最终尾部。
  当前收割是整段阻塞返回，中间流帧实际不会出现——机制先行，待后端
  提供流式接口即可自然生效；
- ``task_finished{taskId, exitCode}`` 终态回执。终态明细
  （finished | failed | cancelled）经 ``tasks_list`` 查询，帧里只有退出码。

**stop 语义（task_stop 真停，M9 补完）**：``task_stop{taskId}`` 置
``cancelRequested`` 标记后，对登记进程按**进程组**发 SIGTERM、2s 宽限后
仍活 SIGKILL（``bash_cancel.terminate_process_tree`` 信号序列）——工作
线程的阻塞收割随即返回，看到标记置 ``state=cancelled`` 并广播终态帧。
装配缝缺 ``spawn_command``（用户未按 SPEC 手工同步）时登记表为空，
``cancel`` 返回 missing——退化为旧的标记式取消（命令照常跑完或超时被杀，
stop 回执如实提示 mark-only），不炸链路。

线程安全：``_tasks`` 表与每条记录的字段更新都在 ``_lock`` 内；帧广播一律
放在锁外（emit 自带锁，嵌套虽不死锁但拉长临界区）。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .protocol import TaskFinished, TaskOutput, TaskStarted
from .tools import bash_cancel
from .tools.bash import sanitize_child_env

DEFAULT_TASK_TIMEOUT_S = 3600.0
# 输出尾部环形上限（字符）：任务面板 tail 展示 + task_output 帧
TAIL_LIMIT = 8192
# 监测线程节奏：尾部有变化时至多每 2s 一帧 task_output
OUTPUT_TICK_S = 2.0
# name 缺省显示名：命令首词截 40 字符
_NAME_LIMIT = 40


def _now_iso() -> str:
    # 面板显示用的本地墙钟时间戳，刻意朴素（无时区）
    return datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005


def _default_name(command: str) -> str:
    head = command.strip().split()[0] if command.strip() else "task"
    return head[:_NAME_LIMIT]


class BackgroundTaskManager:
    """后台任务表 + 执行线程 + 输出监测线程；由服务层随 emit 装配一次。"""

    def __init__(
        self,
        *,
        emit: Callable[[Any], None],
        cwd_supplier: Callable[[], Path],
        tick_interval: float = OUTPUT_TICK_S,
        cancel_registry: bash_cancel.CancelRegistry | None = None,
    ):
        self._emit = emit
        self._cwd_supplier = cwd_supplier
        self._tick_interval = tick_interval
        # task_stop 真停的进程登记表（可注入以便测试换宽限时长）
        self._cancels = cancel_registry or bash_cancel.CancelRegistry()
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()

    # ------------------------------------------------------------------ 启动

    def start(
        self,
        command: str,
        *,
        name: str | None = None,
        timeout: float = DEFAULT_TASK_TIMEOUT_S,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """启动一条后台任务：登记 → 广播 task_started → 起工作线程。

        env 缺省走 sanitize_child_env（凭据意味的变量不随后台 shell 泄漏，
        与 tools/bash 同一口径）。
        """
        task_id = "bg_" + uuid.uuid4().hex[:10]
        record: dict = {
            "taskId": task_id,
            "name": (name or "").strip() or _default_name(command),
            "command": command,
            "state": "running",
            "exitCode": None,
            "outputTail": "",
            "cancelRequested": False,
            "startedAt": _now_iso(),
            "finishedAt": None,
        }
        with self._lock:
            self._tasks[task_id] = record
        self._emit(
            TaskStarted(taskId=task_id, name=record["name"], command=command)
        )
        work_cwd = cwd if cwd is not None else self._cwd_supplier()
        work_env = env if env is not None else sanitize_child_env()
        threading.Thread(
            target=self._run_task,
            args=(task_id, command, float(timeout), work_cwd, work_env),
            daemon=True,
            name=f"zcode-bg-{task_id}",
        ).start()
        self._ensure_monitor()
        return self._snapshot(record)

    # ------------------------------------------------------------------ 停止 / 列表

    def stop(self, task_id: str) -> tuple[bool, str]:
        """真停（task_stop）：置标记 + 对登记进程发信号序列（见模块 docstring
        「stop 语义」）。返回 (ok, message)——message 如实区分强杀与降级。"""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return False, f"no such task {task_id!r}"
            if record["state"] != "running":
                return False, f"task {task_id} already {record['state']}"
            record["cancelRequested"] = True
        outcome = self._cancels.cancel(task_id)
        if outcome == "killed":
            return (
                True,
                "cancel requested; process group signalled (SIGTERM, escalates "
                "to SIGKILL after 2s if alive) — state flips to cancelled when "
                "the worker reaps it",
            )
        return (
            True,
            "cancel requested; mark-only (no cancellable process registered — "
            "bash_backend.spawn_command 未装配，见 docs/BASH_BACKEND_SPEC.md) "
            "state flips to cancelled when the command returns",
        )

    def list_tasks(self) -> list[dict]:
        """任务面板数据源（快照，私有字段剥离；运行中在前，组内按启动时间）。"""
        with self._lock:
            tasks = [self._snapshot(t) for t in self._tasks.values()]
        tasks.sort(key=lambda t: (t["state"] != "running", t.get("startedAt") or ""))
        return tasks

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            record = self._tasks.get(task_id)
            return self._snapshot(record) if record is not None else None

    def shutdown(self) -> None:
        """优雅退出：停监测线程。工作线程为 daemon——命令未完的随进程退出
        （run_tracked 的 timeout 兜底进程被杀时的资源回收由 OS 完成）。"""
        self._monitor_stop.set()

    # ------------------------------------------------------------------ 工作线程

    def _run_task(
        self,
        task_id: str,
        command: str,
        timeout: float,
        cwd: Path | None,
        env: dict[str, str],
    ) -> None:
        """跑 run_tracked（阻塞，登记期间可被 stop 真停）→ 更新终态 → 锁外
        广播 task_output/task_finished。should_cancel 复检覆盖「stop 先于
        进程登记到达」的启动竞态（bash_cancel.run_tracked 契约）。"""
        result = None
        error: str | None = None
        try:
            result = self._cancels.run_tracked(
                task_id,
                command,
                cwd=str(cwd) if cwd else None,
                timeout=timeout,
                env=env,
                should_cancel=lambda: self._cancel_requested(task_id),
            )
        except BaseException as exc:  # noqa: BLE001 —— 后端异常入表，不炸线程
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:  # pragma: no cover —— 表项不应被中途摘除
                return
            if error is not None:
                record["state"] = "failed"
                record["error"] = error
                self._append_tail(record, error)
                record["exitCode"] = None
            else:
                assert result is not None
                self._append_tail(
                    record,
                    (result.stdout or "") + (("\n" + result.stderr) if result.stderr else ""),
                )
                record["exitCode"] = int(result.exit_code)
                if record["cancelRequested"]:
                    # stop 已标记：命令实际跑完（或被 timeout 杀掉）才到这
                    record["state"] = "cancelled"
                elif result.timed_out:
                    record["state"] = "failed"
                    record["error"] = f"timed out after {timeout:g}s"
                else:
                    record["state"] = "finished"
            record["finishedAt"] = _now_iso()
            tail = record["outputTail"]
            exit_code = -1 if record["exitCode"] is None else record["exitCode"]
        self._emit(TaskOutput(taskId=task_id, tail=tail))
        self._emit(TaskFinished(taskId=task_id, exitCode=exit_code))

    # ------------------------------------------------------------------ 输出监测

    def _ensure_monitor(self) -> None:
        with self._lock:
            if (
                self._monitor_thread is not None
                and self._monitor_thread.is_alive()
            ):
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="zcode-bg-monitor"
            )
            thread = self._monitor_thread
        thread.start()

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._tick_interval):
            if not self.flush_changed_outputs():
                # 双检：再等一拍确认仍无运行中任务才退场——避免「判定退场的
                # 瞬间恰好 start 新任务」的监测盲区（start 端看到本线程还活着
                # 就不会另起）
                if self._monitor_stop.wait(self._tick_interval):
                    return
                if not self.flush_changed_outputs():
                    return

    def flush_changed_outputs(self) -> bool:
        """对运行中且尾部有变化的任务各广播一帧 task_output；返回是否存在
        运行中任务。监测线程与测试直呼（打桩验证输出流）。"""
        with self._lock:
            to_emit: list[tuple[str, str]] = []
            alive = False
            for record in self._tasks.values():
                if record["state"] != "running":
                    continue
                alive = True
                if len(record["outputTail"]) != record.get("_emittedLen", 0):
                    record["_emittedLen"] = len(record["outputTail"])
                    to_emit.append((record["taskId"], record["outputTail"]))
        for task_id, tail in to_emit:
            self._emit(TaskOutput(taskId=task_id, tail=tail))
        return alive

    # ------------------------------------------------------------------ 内部

    def _cancel_requested(self, task_id: str) -> bool:
        """stop 标记的锁内安全读取（表项缺失 = False，供 run_tracked 复检）。"""
        with self._lock:
            record = self._tasks.get(task_id)
            return bool(record and record["cancelRequested"])

    @staticmethod
    def _append_tail(record: dict, text: str) -> None:
        """尾部 8KB 环形：新输出拼接后只留末尾 TAIL_LIMIT 字符。"""
        record["outputTail"] = (record["outputTail"] + text)[-TAIL_LIMIT:]

    @staticmethod
    def _snapshot(record: dict) -> dict:
        return {k: v for k, v in record.items() if not k.startswith("_")}


__all__ = [
    "DEFAULT_TASK_TIMEOUT_S",
    "TAIL_LIMIT",
    "BackgroundTaskManager",
]
