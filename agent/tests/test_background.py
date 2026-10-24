"""M9 后台任务测试：bash_backend 打桩（run_command 慢速返回），验证启动 /
输出流 / 结束帧 / 列表 / 停止标记（不强杀语义）；服务层 control 往返。"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from zcode_py.background import TAIL_LIMIT, BackgroundTaskManager
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.protocol import encode_message
from zcode_py.sessions import SessionStore
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


class Recorder:
    """打桩 run_command：按命令路由到延迟/输出/异常。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.behaviors: dict[str, callable] = {}

    def add(self, command: str, fn) -> None:
        with self.lock:
            self.behaviors[command] = fn

    def __call__(self, command, *, cwd=None, timeout=120.0, env=None):
        with self.lock:
            self.calls.append(
                {"command": command, "cwd": cwd, "timeout": timeout, "env": env}
            )
            fn = self.behaviors.get(command, lambda: BashResult(0, "", "", False, 1))
        return fn()


@pytest.fixture
def stub_backend(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(
        "zcode_py.tools.bash_backend.run_command", recorder
    )
    return recorder


def make_manager(emitted, tmp_path, tick=0.05):
    return BackgroundTaskManager(
        emit=emitted.append,
        cwd_supplier=lambda: tmp_path,
        tick_interval=tick,
    )


def frames_of(emitted, type_name):
    return [as_dict(f) for f in emitted if as_dict(f).get("type") == type_name]


# ---------------------------------------------------------------------------
# 启动 / 输出 / 结束帧
# ---------------------------------------------------------------------------


def test_start_streams_frames_and_finishes(stub_backend, tmp_path):
    stub_backend.add("echo hi", lambda: (time.sleep(0.05), BashResult(0, "hi", "", False, 3))[1])
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    record = manager.start("echo hi", timeout=90)

    assert record["state"] == "running" and record["taskId"].startswith("bg_")
    assert record["name"] == "echo"  # 缺省名 = 命令首词
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "finished")

    started = frames_of(emitted, "task_started")
    assert started == [
        {"type": "task_started", "taskId": record["taskId"], "name": "echo", "command": "echo hi"}
    ]
    output = frames_of(emitted, "task_output")
    assert output[-1]["taskId"] == record["taskId"] and output[-1]["tail"] == "hi"
    finished = frames_of(emitted, "task_finished")
    assert finished == [{"type": "task_finished", "taskId": record["taskId"], "exitCode": 0}]

    final = manager.get(record["taskId"])
    assert final["exitCode"] == 0 and final["outputTail"] == "hi"
    # timeout 透传 + cwd 供应
    assert stub_backend.calls[0]["timeout"] == 90
    assert stub_backend.calls[0]["cwd"] == str(tmp_path)


def test_stderr_appended_and_nonzero_exit_is_data(stub_backend, tmp_path):
    stub_backend.add("fail", lambda: BashResult(3, "out", "err!", False, 2))
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    manager.start("fail")
    assert wait_for(lambda: len(frames_of(emitted, "task_finished")) >= 1)
    final = manager.get(frames_of(emitted, "task_started")[0]["taskId"])
    assert final["state"] == "finished" and final["exitCode"] == 3
    assert final["outputTail"] == "out\nerr!"


def test_output_tail_ring_8kb(stub_backend, tmp_path):
    big = "x" * (TAIL_LIMIT + 5000)
    stub_backend.add("big", lambda: BashResult(0, big, "", False, 2))
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    manager.start("big")
    assert wait_for(lambda: len(frames_of(emitted, "task_finished")) >= 1)
    task_id = frames_of(emitted, "task_started")[0]["taskId"]
    final = manager.get(task_id)
    assert len(final["outputTail"]) == TAIL_LIMIT
    assert set(final["outputTail"]) == {"x"}
    assert frames_of(emitted, "task_output")[-1]["tail"] == final["outputTail"]


def test_backend_exception_marks_failed(stub_backend, tmp_path):
    def boom():
        raise RuntimeError("spawn lost")

    stub_backend.add("boom", boom)
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    manager.start("boom")
    assert wait_for(lambda: manager.get(frames_of(emitted, "task_started")[0]["taskId"])["state"] == "failed")
    final = manager.get(frames_of(emitted, "task_started")[0]["taskId"])
    assert "RuntimeError" in final["error"]
    assert frames_of(emitted, "task_finished")[0]["exitCode"] == -1


def test_timeout_marks_failed(stub_backend, tmp_path):
    stub_backend.add("slow", lambda: BashResult(-1, "partial", "", True, 3610))
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    manager.start("slow")
    assert wait_for(lambda: len(frames_of(emitted, "task_finished")) >= 1)
    final = manager.get(frames_of(emitted, "task_started")[0]["taskId"])
    assert final["state"] == "failed" and "timed out" in final["error"]
    assert final["exitCode"] == -1 and "partial" in final["outputTail"]


# ---------------------------------------------------------------------------
# 停止标记（不强杀）语义
# ---------------------------------------------------------------------------


def test_stop_marks_cancel_requested_then_cancelled(stub_backend, tmp_path):
    release = threading.Event()

    def slow():
        release.wait(5.0)
        return BashResult(0, "late", "", False, 100)

    stub_backend.add("longrun", slow)
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    record = manager.start("longrun")

    ok, message = manager.stop(record["taskId"])
    assert ok and "cancel requested" in message
    assert manager.get(record["taskId"])["cancelRequested"] is True
    # 命令未返回前仍是 running（无法强杀）
    assert manager.get(record["taskId"])["state"] == "running"

    release.set()
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "cancelled")
    final = manager.get(record["taskId"])
    assert final["exitCode"] == 0 and final["outputTail"] == "late"
    assert frames_of(emitted, "task_finished")[0]["exitCode"] == 0


def test_stop_unknown_or_finished_task(stub_backend, tmp_path):
    ok, message = make_manager([], tmp_path).stop("bg_nope")
    assert not ok and "no such task" in message

    stub_backend.add("quick", lambda: BashResult(0, "", "", False, 1))
    manager = make_manager([], tmp_path)
    record = manager.start("quick")
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "finished")
    ok, message = manager.stop(record["taskId"])
    assert not ok and "already finished" in message


# ---------------------------------------------------------------------------
# 列表 / 输出监测 / env 脱敏
# ---------------------------------------------------------------------------


def test_list_tasks_snapshot_and_order(stub_backend, tmp_path):
    hold = threading.Event()
    stub_backend.add("hold", lambda: (hold.wait(5.0), BashResult(0, "", "", False, 1))[1])
    stub_backend.add("done", lambda: BashResult(0, "", "", False, 1))
    manager = make_manager([], tmp_path)
    done = manager.start("done", name="快速")
    running = manager.start("hold")
    assert wait_for(lambda: manager.get(done["taskId"])["state"] == "finished")

    tasks = manager.list_tasks()  # hold 仍在跑：running 在前
    ids = [t["taskId"] for t in tasks]
    assert ids.index(running["taskId"]) < ids.index(done["taskId"])
    by_id = {t["taskId"]: t for t in tasks}
    assert by_id[done["taskId"]]["name"] == "快速"
    assert all(not k.startswith("_") for t in tasks for k in t)
    hold.set()
    assert wait_for(lambda: manager.get(running["taskId"])["state"] == "finished")


def test_monitor_flushes_changed_tail(stub_backend, tmp_path):
    """监测线程路径：运行中任务的尾部一旦变化（流式后端就绪后即出现），
    flush_changed_outputs 广播一帧 task_output，且按变化去重。"""
    hold = threading.Event()
    stub_backend.add("hold", lambda: (hold.wait(5.0), BashResult(0, "", "", False, 1))[1])
    emitted: list = []
    manager = make_manager(emitted, tmp_path)
    record = manager.start("hold")

    assert manager.flush_changed_outputs() is True  # 有运行中任务
    assert frames_of(emitted, "task_output") == []  # 尾部未变 → 不发

    with manager._lock:  # 白盒模拟流式后端追加输出
        manager._tasks[record["taskId"]]["outputTail"] = "epoch 1"
    assert manager.flush_changed_outputs() is True
    assert frames_of(emitted, "task_output") == [
        {"type": "task_output", "taskId": record["taskId"], "tail": "epoch 1"}
    ]
    assert manager.flush_changed_outputs() is True  # 未再变 → 不重复发
    assert len(frames_of(emitted, "task_output")) == 1

    hold.set()
    assert wait_for(lambda: manager.get(record["taskId"])["state"] == "finished")
    assert manager.flush_changed_outputs() is False  # 无运行中任务


def test_default_env_sanitized(stub_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sekrit")
    monkeypatch.setenv("PLAIN_VAR", "ok")
    stub_backend.add("envprobe", lambda: BashResult(0, "", "", False, 1))
    manager = make_manager([], tmp_path)
    manager.start("envprobe")
    assert wait_for(lambda: len(stub_backend.calls) >= 1)
    env = stub_backend.calls[0]["env"]
    assert "MY_API_KEY" not in env and env["PLAIN_VAR"] == "ok"


# ---------------------------------------------------------------------------
# 服务层 control 往返
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self):
        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter, provider_factory=lambda entry, key: None
        )

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def results(self, command: str) -> list:
        return [
            f for f in self.frames()
            if f.get("type") == "result" and f.get("command") == command
        ]


def test_service_background_controls_round_trip(stub_backend, tmp_home):
    stub_backend.add("train.py", lambda: (time.sleep(0.05), BashResult(0, "epoch done", "", False, 5))[1])
    harness = Harness()
    harness.send({"type": "init", "workspaceRoots": [str(tmp_home)]})
    harness.send(
        {"type": "control", "command": "background_start", "taskCommand": "train.py", "name": "训练"}
    )

    ack = harness.results("background_start")
    assert ack and ack[0]["ok"] is True
    task_id = ack[0]["detail"]
    assert wait_for(
        lambda: len([f for f in harness.frames() if f["type"] == "task_finished"]) >= 1
    )

    started = [f for f in harness.frames() if f["type"] == "task_started"]
    assert started[0]["taskId"] == task_id and started[0]["name"] == "训练"
    output = [f for f in harness.frames() if f["type"] == "task_output"]
    assert output[-1]["tail"] == "epoch done"

    harness.send({"type": "control", "command": "tasks_list"})
    detail = json.loads(harness.results("tasks_list")[-1]["detail"])
    assert detail[0]["taskId"] == task_id
    assert detail[0]["state"] == "finished" and detail[0]["exitCode"] == 0

    stop = harness.results("background_start")  # 只有启动那一条回执
    harness.send({"type": "control", "command": "task_stop", "taskId": task_id})
    result = harness.results("task_stop")[-1]
    assert result["ok"] is False and "already finished" in result["detail"]
    assert len(stop) == 1


def test_service_background_start_rejects_bad_payload(tmp_home):
    harness = Harness()
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "background_start"})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert errors and errors[-1]["kind"] == "protocol" and "taskCommand" in errors[-1]["message"]
    harness.send({"type": "control", "command": "background_start", "taskCommand": "x", "timeoutSec": 0})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert "timeoutSec" in errors[-1]["message"]


def test_shutdown_stops_monitor_thread(stub_backend, tmp_path):
    hold = threading.Event()
    stub_backend.add("hold", lambda: (hold.wait(5.0), BashResult(0, "", "", False, 1))[1])
    manager = make_manager([], tmp_path)
    manager.start("hold")  # 运行中 → 监测线程存活
    monitor = manager._monitor_thread
    assert monitor is not None and monitor.is_alive()

    manager.shutdown()
    monitor.join(timeout=2.0)
    assert not monitor.is_alive()
    hold.set()
