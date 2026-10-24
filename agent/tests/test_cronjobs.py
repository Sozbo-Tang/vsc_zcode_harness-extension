"""M9 定时任务测试：5 段 cron 判定表驱动（不真等时间）、CRUD 落盘、tick
触发动作打桩、调度线程启停、服务层 control 往返与 prompt 注入。"""

from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime

import pytest

from zcode_py.cronjobs import (
    CronError,
    CronManager,
    cron_matches,
    latest_due_minute,
    parse_cron,
    validate_action,
)
from zcode_py.llm.base import TextDelta
from zcode_py.llm.fake import FakeProvider
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.sessions import SessionStore
from zcode_py.tools.bash_backend import BashResult


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def dt(s: str) -> datetime:
    # cron 语义 = 朴素本地墙钟（与被测实现一致）
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# 表达式解析与匹配（表驱动）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, when, expected",
    [
        ("* * * * *", "2026-10-24 10:07:30", True),
        # */n 步进
        ("*/5 * * * *", "2026-10-24 10:05:00", True),
        ("*/5 * * * *", "2026-10-24 10:07:00", False),
        ("*/15 * * * *", "2026-10-24 10:45:00", True),
        ("*/15 * * * *", "2026-10-24 10:50:00", False),
        # 范围 + 列表
        ("0 9-17 * * *", "2026-10-24 12:00:00", True),
        ("0 9-17 * * *", "2026-10-24 18:00:00", False),
        ("1,15 * * * *", "2026-10-24 10:15:00", True),
        ("1,15 * * * *", "2026-10-24 10:16:00", False),
        ("30 8 1 * *", "2026-10-01 08:30:00", True),
        ("30 8 1 * *", "2026-10-24 08:30:00", False),
        ("0 0 1 1 *", "2026-01-01 00:00:00", True),
        # 周：0 与 7 都是周日；1-5 = 工作日
        ("0 9 * * 0", "2026-10-25 09:00:00", True),   # 2026-10-25 是周日
        ("0 9 * * 7", "2026-10-25 09:00:00", True),
        ("0 9 * * 1-5", "2026-10-23 09:00:00", True),  # 周五
        ("0 9 * * 1-5", "2026-10-25 09:00:00", False),  # 周日
        # 日/周都受限：经典 cron「或」语义（1 号或周一命中）
        ("0 0 1 * 1", "2026-10-01 00:00:00", True),   # 1 号（周四）
        ("0 0 1 * 1", "2026-10-26 00:00:00", True),   # 周一（非 1 号）
        ("0 0 1 * 1", "2026-10-24 00:00:00", False),  # 周六且非 1 号
        # 范围带步进
        ("10-50/10 * * * *", "2026-10-24 10:30:00", True),
        ("10-50/10 * * * *", "2026-10-24 10:35:00", False),
    ],
)
def test_cron_matches_table(expr, when, expected):
    assert cron_matches(expr, dt(when)) is expected


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",           # 4 段
        "* * * * * *",       # 6 段
        "60 * * * *",        # 分钟越界
        "* 24 * * *",        # 小时越界
        "* * 0 * *",         # 日下界
        "* * 32 * *",        # 日上界
        "* * * 13 *",        # 月越界
        "* * * * 8",         # 周越界
        "a * * * *",         # 非数字
        "*/0 * * * *",       # 零步进
        "5-1 * * * *",       # 反向范围
        "1,,2 * * * *",      # 空列表项
        "",                  # 空串
    ],
)
def test_parse_cron_rejects_invalid(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


def test_latest_due_minute_with_jitter_tolerance():
    # 恰好在分钟边界 → 命中当拍
    assert latest_due_minute("*/5 * * * *", dt("2026-10-24 10:05:03")) == dt("2026-10-24 10:05:00")
    # tick 跨过边界 60s 内（默认 lookback 65s）→ 容忍抖动补发
    assert latest_due_minute("*/5 * * * *", dt("2026-10-24 10:06:04")) == dt("2026-10-24 10:05:00")
    # 超出回看窗口 → 不补
    assert latest_due_minute("*/5 * * * *", dt("2026-10-24 10:06:30"), lookback_s=60) is None
    # 窗口内无匹配分钟
    assert latest_due_minute("0 0 * * *", dt("2026-10-24 10:06:04")) is None
    # 窗口收紧为 5s：跨 30s 的迟到不再补
    assert latest_due_minute("*/5 * * * *", dt("2026-10-24 10:05:31"), lookback_s=5) is None


def test_validate_action_rules():
    assert validate_action({"type": "prompt", "text": "hi"}) == {"type": "prompt", "text": "hi"}
    assert validate_action({"type": "task", "command": "make"}) == {"type": "task", "command": "make"}
    with pytest.raises(CronError):
        validate_action({"type": "shell"})
    with pytest.raises(CronError):
        validate_action({"type": "prompt", "text": "  "})
    with pytest.raises(CronError):
        validate_action({"type": "task"})
    with pytest.raises(CronError):
        validate_action("prompt")


# ---------------------------------------------------------------------------
# CRUD + 落盘
# ---------------------------------------------------------------------------


def make_cron(tmp_path, prompts, tasks, **kwargs):
    path = tmp_path / "config" / "cron.json"
    return CronManager(
        on_prompt=prompts.append,
        on_task=lambda command, name: tasks.append((command, name)),
        path=path,
        **kwargs,
    )


def test_add_persists_and_reload(tmp_path):
    prompts, tasks = [], []
    manager = make_cron(tmp_path, prompts, tasks)
    job, error = manager.add(
        schedule="*/5 * * * *",
        action={"type": "prompt", "text": "总结"},
        name="日报",
    )
    assert error is None and job["enabled"] is True and job["id"].startswith("cron_")

    data = json.loads((tmp_path / "config" / "cron.json").read_text())
    assert data == [job]

    reloaded = make_cron(tmp_path, [], [])
    assert reloaded.list_jobs() == [job]


def test_add_rejects_bad_entries(tmp_path):
    manager = make_cron(tmp_path, [], [])
    job, error = manager.add(schedule="not cron", action={"type": "prompt", "text": "x"})
    assert job is None and "5-field" in error
    job, error = manager.add(schedule="* * * * *", action={"type": "nope"})
    assert job is None and "action.type" in error
    assert manager.list_jobs() == []
    assert not (tmp_path / "config" / "cron.json").exists()


def test_remove_and_toggle_persist(tmp_path):
    manager = make_cron(tmp_path, [], [])
    job, _ = manager.add(schedule="* * * * *", action={"type": "task", "command": "backup"})

    toggled, error = manager.toggle(job["id"])  # 缺省取反
    assert error is None and toggled["enabled"] is False
    toggled, error = manager.toggle(job["id"], True)
    assert toggled["enabled"] is True

    ok, message = manager.remove(job["id"])
    assert ok and manager.list_jobs() == []
    on_disk = json.loads((tmp_path / "config" / "cron.json").read_text())
    assert on_disk == []

    ok, message = manager.remove(job["id"])
    assert not ok and "no such" in message
    _job2, error = manager.toggle("cron_missing")
    assert error and "no such" in error


def test_corrupt_config_degrades(tmp_path):
    path = tmp_path / "config" / "cron.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    manager = make_cron(tmp_path, [], [])
    assert manager.list_jobs() == [] and manager.load_error
    # 非数组同样降级
    path.write_text("{}", encoding="utf-8")
    manager = make_cron(tmp_path, [], [])
    assert manager.load_error and manager.list_jobs() == []


def test_bad_entries_on_disk_skipped(tmp_path):
    path = tmp_path / "config" / "cron.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {"id": "ok1", "schedule": "* * * * *", "action": {"type": "prompt", "text": "hi"}},
                {"id": "bad", "schedule": "99 * * * *", "action": {"type": "prompt", "text": "x"}},
                "not-an-object",
            ]
        ),
        encoding="utf-8",
    )
    manager = make_cron(tmp_path, [], [])
    assert [j["id"] for j in manager.list_jobs()] == ["ok1"]


# ---------------------------------------------------------------------------
# tick 触发（动作打桩）
# ---------------------------------------------------------------------------


def test_tick_fires_prompt_and_task_actions(tmp_path):
    prompts, tasks = [], []
    manager = make_cron(tmp_path, prompts, tasks)
    p, _ = manager.add(schedule="* * * * *", action={"type": "prompt", "text": "去总结"}, name="p1")
    t, _ = manager.add(
        schedule="* * * * *", action={"type": "task", "command": "make report"}, name="t1"
    )

    fired = manager.tick(now=dt("2026-10-24 10:07:00"))
    assert sorted(fired) == sorted([p["id"], t["id"]])
    assert prompts == ["去总结"]
    assert tasks == [("make report", "t1")]

    # 同一分钟内重复 tick 不重发
    assert manager.tick(now=dt("2026-10-24 10:07:29")) == []
    assert prompts == ["去总结"]
    # 下一分钟再发
    assert manager.tick(now=dt("2026-10-24 10:08:00")) and len(prompts) == 2


def test_tick_skips_disabled(tmp_path):
    prompts = []
    manager = make_cron(tmp_path, prompts, [])
    job, _ = manager.add(
        schedule="* * * * *", action={"type": "prompt", "text": "x"}, enabled=False
    )
    assert manager.tick(now=dt("2026-10-24 10:07:00")) == []
    assert prompts == []
    # 重新启用后同分钟即触发（disabled 不记 last_fired）
    manager.toggle(job["id"], True)
    assert manager.tick(now=dt("2026-10-24 10:07:30")) == [job["id"]]


def test_tick_survives_action_failure(tmp_path):
    def boom(text):
        raise RuntimeError("injected")

    path = tmp_path / "config" / "cron.json"
    manager = CronManager(on_prompt=boom, on_task=lambda *a: None, path=path)
    manager.add(schedule="* * * * *", action={"type": "prompt", "text": "x"})
    assert manager.tick(now=dt("2026-10-24 10:07:00"))  # 异常被吞，返回触发列表


def test_scheduler_thread_fires_and_stops(tmp_path):
    fired = threading.Event()
    path = tmp_path / "config" / "cron.json"
    manager = CronManager(
        on_prompt=lambda text: fired.set(),
        on_task=lambda *a: None,
        interval=1.0,  # 最小钳制 1s；* * * * * 任何分钟都匹配
        path=path,
    )
    manager.add(schedule="* * * * *", action={"type": "prompt", "text": "tick"})
    manager.start()
    manager.start()  # 幂等
    try:
        assert fired.wait(timeout=4.0), "scheduler thread did not fire within 4s"
    finally:
        manager.stop()
    thread = manager._thread
    thread.join(timeout=2.0)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# 服务层 control 往返 + prompt 注入路径
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self, provider):
        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter, provider_factory=lambda entry, key: provider
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


def test_service_cron_controls_round_trip(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send(
        {"type": "control", "command": "cron_add", "schedule": "*/5 * * * *",
         "action": {"type": "prompt", "text": "总结"}, "name": "五分钟"}
    )
    ack = harness.results("cron_add")[-1]
    job_id = ack["detail"]

    harness.send({"type": "control", "command": "cron_list"})
    listed = json.loads(harness.results("cron_list")[-1]["detail"])
    assert listed == [
        {"id": job_id, "name": "五分钟", "schedule": "*/5 * * * *",
         "action": {"type": "prompt", "text": "总结"}, "enabled": True}
    ]
    # 落盘在 ZCODE_PY_HOME 下
    data = json.loads((tmp_home / "config" / "cron.json").read_text())
    assert data[0]["id"] == job_id

    harness.send({"type": "control", "command": "cron_toggle", "cronId": job_id, "enabled": False})
    toggled = json.loads(harness.results("cron_toggle")[-1]["detail"])
    assert toggled["enabled"] is False

    harness.send({"type": "control", "command": "cron_remove", "cronId": job_id})
    assert harness.results("cron_remove")[-1]["ok"] is True
    harness.send({"type": "control", "command": "cron_list"})
    assert json.loads(harness.results("cron_list")[-1]["detail"]) == []


def test_service_cron_add_validation_errors(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "cron_add", "schedule": "bad"})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert errors[-1]["kind"] == "protocol"
    harness.send({"type": "control", "command": "cron_add", "schedule": "* * * * *"})
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert "action" in errors[-1]["message"]
    harness.send(
        {"type": "control", "command": "cron_add", "schedule": "* * * * *", "action": {"type": "x"}}
    )
    errors = [f for f in harness.frames() if f["type"] == "error"]
    assert errors[-1]["kind"] == "config"


def test_cron_prompt_injects_via_user_message_path(tmp_home):
    """cron prompt 动作走 service 用户消息路径：直接调 _cron_inject_prompt 验证
    转录/回合全链路（绕开真实时间等待）。"""
    fake = FakeProvider(turns=[[TextDelta("cron 回复")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    session_id = next(
        f for f in harness.frames() if f["type"] == "session_started"
    )["sessionId"]

    harness.service._cron_inject_prompt("定时总结")

    assert wait_for(lambda: any(f.get("text") == "cron 回复" for f in harness.frames() if f["type"] == "message_complete"))
    transcript = (tmp_home / "sessions" / session_id / "transcript.jsonl").read_text()
    assert "定时总结" in transcript
    assert any(
        f["type"] == "message_complete" and f["text"] == "cron 回复"
        for f in harness.frames()
    )


def test_cron_task_action_starts_background(tmp_home, monkeypatch):
    calls: list[str] = []

    def stub(command, *, cwd=None, timeout=120.0, env=None):
        calls.append(command)
        return BashResult(0, "done", "", False, 1)

    monkeypatch.setattr("zcode_py.tools.bash_backend.run_command", stub)
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    # 命令型 cron 动作过权限门（独立评审高危 #2）：yolo 下放行非危险命令
    harness.send({"type": "control", "command": "permission_mode", "mode": "yolo"})

    harness.service._cron_start_task("nightly-build", "构建")
    assert wait_for(lambda: any(f["type"] == "task_finished" for f in harness.frames()))
    started = [f for f in harness.frames() if f["type"] == "task_started"]
    assert started[0]["command"] == "nightly-build" and started[0]["name"] == "构建"
    assert calls == ["nightly-build"]


def test_cron_task_fire_rejected_when_not_permitted(tmp_home, monkeypatch):
    """fire 复检（高危 #2）：build 模式下白名单外命令到点不放行。"""
    calls: list[str] = []

    def stub(command, *, cwd=None, timeout=120.0, env=None):
        calls.append(command)
        return BashResult(0, "done", "", False, 1)

    monkeypatch.setattr("zcode_py.tools.bash_backend.run_command", stub)
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    harness.service._cron_start_task("curl example.com", "拉取")
    assert wait_for(lambda: any(
        f["type"] == "error" and "不再被允许" in str(f.get("message", ""))
        for f in harness.frames()
    ))
    assert calls == []  # 从未执行


def test_cron_add_task_action_gated(tmp_home):
    """add 时门禁（高危 #2）：build 下白名单外 task 动作直接拒绝；危险命令
    任何模式都拒绝。"""
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send({"type": "control", "command": "cron_add", "schedule": "*/5 * * * *",
                  "action": {"type": "task", "command": "make build"}, "name": "b"})
    assert wait_for(lambda: any(
        f["type"] == "error" and f.get("kind") == "permission" for f in harness.frames()
    ))

    harness.send({"type": "control", "command": "permission_mode", "mode": "yolo"})
    harness.send({"type": "control", "command": "cron_add", "schedule": "*/5 * * * *",
                  "action": {"type": "task", "command": "rm -rf /"}, "name": "d"})
    errs = [f for f in harness.frames() if f["type"] == "error"]
    assert any("危险命令" in str(e.get("message", "")) for e in errs)


def test_shutdown_stops_cron_thread(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    thread = harness.service._cron._thread
    assert thread is not None and thread.is_alive()  # init 已拉起调度线程
    harness.service.shutdown()
    assert not thread.is_alive()
