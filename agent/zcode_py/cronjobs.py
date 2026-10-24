"""M9 定时任务（R3，PLAN §2.1 / §7 M9）：~/.zcode-py/config/cron.json + 调度器。

配置条目（PLAN §3.2 风格，JSON 数组）::

    [{"id": "cron_ab12", "name": "日报", "schedule": "*/5 * * * *",
      "action": {"type": "prompt", "text": "总结一下今天的进展"},
      "enabled": true}]

- ``schedule`` 标准 **5 段** cron（分 时 日 月 周），支持 ``*`` / ``*/n`` /
  ``a`` / ``a-b`` / ``a,b,c``（及步进组合 ``a-b/n``）；周 0 与 7 均为周日；
  日/周都受限时按经典 cron 语义取「或」。
- ``action`` 两种：``{"type": "prompt", "text"}`` = 把 text 作为 user_message
  注入当前会话（服务层走同一条用户消息路径）；``{"type": "task", "command"}``
  = 经 BackgroundTaskManager 起后台任务。

调度器：daemon 线程每 ``interval``（默认 30s）tick 一次；触发判定容忍 30s
抖动——每个 tick 在「现在往回 ``lookback`` 窗口」内找最近一个匹配的分钟边界，
``_last_fired`` 记住 (jobId → 分钟键) 防重发。**enabled=false 的条目不触发**
（配置仍加载、面板可见）。

线程安全：``_jobs`` 的读写（control 线程增删改 / 调度线程遍历）与落盘都在
``_lock`` 内串行；``tick(now)`` 可直呼（测试表驱动，不真等时间）。
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import home_dir

CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # 分 时 日 月 周
ACTION_TYPES = ("prompt", "task")
DEFAULT_INTERVAL_S = 30.0
# 触发判定回看窗口：>= 调度间隔（默认 30s 抖动容忍）+ 少量余量
LOOKBACK_S = 65.0


class CronError(ValueError):
    """cron 表达式 / 条目结构不合法。"""


# ---------------------------------------------------------------------------
# 5 段 cron 表达式（纯函数，表驱动可测）
# ---------------------------------------------------------------------------


def _parse_field(spec: str, low: int, high: int, *, is_dow: bool = False) -> frozenset[int]:
    """单段解析 → 允许值集合。空段 / 越界 / 坏语法抛 CronError。"""
    spec = spec.strip()
    if not spec:
        raise CronError("empty cron field")
    allowed: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty list item in cron field {spec!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronError(f"bad step in {part!r}") from exc
            if step <= 0:
                raise CronError(f"step must be positive in {part!r}")
            part = base
        if part == "*":
            start, end = low, high
        elif "-" in part and not part.startswith("-"):
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError as exc:
                raise CronError(f"bad range {part!r}") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise CronError(f"bad value {part!r}") from exc
        if start < low or end > high or start > end:
            raise CronError(
                f"value out of range [{low},{high}] in cron field {spec!r}"
            )
        allowed.update(range(start, end + 1, step))
    if is_dow and 7 in allowed:
        allowed.discard(7)  # 7 与 0 同为周日 → 归一到 0
        allowed.add(0)
    return frozenset(allowed)


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    """5 段表达式 → (分, 时, 日, 月, 周) 允许值集合；不合法抛 CronError。"""
    fields = str(expr).split()
    if len(fields) != 5:
        raise CronError(
            f"schedule must be standard 5-field cron (min hour dom month dow), got {expr!r}"
        )
    parsed = []
    for spec, (low, high) in zip(fields, CRON_FIELD_RANGES):
        parsed.append(_parse_field(spec, low, high, is_dow=(low, high) == (0, 7)))
    return tuple(parsed)


def cron_matches(expr: str, dt: datetime) -> bool:
    """``expr`` 是否匹配 ``dt`` 所在的分钟。日与周都受限时按经典 cron 语义
    取「或」（任一匹配即命中）；只有一方受限时该方必须命中。"""
    minute, hour, dom, month, dow = parse_cron(expr)
    if (
        dt.minute not in minute
        or dt.hour not in hour
        or dt.month not in month
    ):
        return False
    dom_star = len(dom) == 31  # 1..31 全集 = 未受限
    dow_star = len(dow) == 7   # 归一后 0..6 全集 = 未受限
    dom_hit = dt.day in dom
    # datetime.weekday(): 周一=0 … 周日=6 → cron（周日=0）
    dow_hit = (dt.weekday() + 1) % 7 in dow
    if not dom_star and not dow_star:
        return dom_hit or dow_hit
    if not dom_star:
        return dom_hit
    if not dow_star:
        return dow_hit
    return True


def latest_due_minute(
    expr: str, now: datetime, *, lookback_s: float = LOOKBACK_S
) -> datetime | None:
    """``now`` 往回 lookback 秒内最近一个匹配的分钟边界（秒/微秒归零）；
    无匹配返回 None。按绝对时间比界（分钟边界距今 ≤ lookback 才算在窗），
    调度间隔 ≤ lookback 时不会漏发分钟边界（容忍 30s 抖动），lookback 之外
    的迟到不补发（进程停摆一小时不该在重启时爆发一串追发）。"""
    candidate = now.replace(second=0, microsecond=0)
    earliest = now - timedelta(seconds=lookback_s)
    while candidate >= earliest:
        if cron_matches(expr, candidate):
            return candidate
        candidate -= timedelta(minutes=1)
    return None


# ---------------------------------------------------------------------------
# 条目校验（纯函数）
# ---------------------------------------------------------------------------


def validate_action(action) -> dict:
    """action 载荷校验：{"type":"prompt","text"} | {"type":"task","command"}；
    合法返回规范化拷贝，否则抛 CronError。"""
    if not isinstance(action, dict):
        raise CronError("action must be an object")
    kind = action.get("type")
    if kind not in ACTION_TYPES:
        raise CronError(f"action.type must be one of {list(ACTION_TYPES)}, got {kind!r}")
    payload_key = "text" if kind == "prompt" else "command"
    payload = action.get(payload_key)
    if not isinstance(payload, str) or not payload.strip():
        raise CronError(f"action {kind!r} requires a non-empty string {payload_key!r}")
    return {"type": kind, payload_key: payload}


# ---------------------------------------------------------------------------
# 管理器：配置读写 + 调度线程
# ---------------------------------------------------------------------------


class CronManager:
    """cron 条目 CRUD（落盘 cron.json）+ 每 interval 一 tick 的调度线程。

    回调注入：``on_prompt(text)`` 注入当前会话、``on_task(command, name)``
    起后台任务——由服务层装配（本模块不感知会话/后台细节）。
    """

    def __init__(
        self,
        *,
        on_prompt: Callable[[str], None],
        on_task: Callable[[str, str | None], Any],
        interval: float = DEFAULT_INTERVAL_S,
        path: Path | None = None,
    ):
        self._on_prompt = on_prompt
        self._on_task = on_task
        self._interval = max(1.0, float(interval))
        self._path = path if path is not None else home_dir() / "config" / "cron.json"
        self._lock = threading.Lock()
        self._jobs: list[dict] = []
        self._last_fired: dict[str, str] = {}  # jobId → 已触发分钟键（进程内）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.load_error: str | None = None
        self._load()

    # ------------------------------------------------------------------ 配置

    def _load(self) -> None:
        """读 cron.json：缺文件 = 空表；损坏 = 空表 + load_error（面板可见降级）。"""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.load_error = f"cannot read {self._path}: {exc}"
            return
        if not isinstance(data, list):
            self.load_error = f"{self._path} must be a JSON array of cron entries"
            return
        jobs: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue  # 坏条目跳过，不拖垮其余
            try:
                jobs.append(self._normalize(entry))
            except CronError:
                continue
        self._jobs = jobs

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._jobs, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _normalize(entry: dict) -> dict:
        """外部条目（磁盘 / control）→ 规范形态；不合法抛 CronError。"""
        schedule = entry.get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            raise CronError("cron entry requires a string 'schedule'")
        parse_cron(schedule)  # 只验合法性
        action = validate_action(entry.get("action"))
        return {
            "id": str(entry.get("id") or ("cron_" + uuid.uuid4().hex[:8])),
            "name": str(entry.get("name") or ""),
            "schedule": schedule.strip(),
            "action": action,
            "enabled": bool(entry.get("enabled", True)),
        }

    # ------------------------------------------------------------------ CRUD（control 线程）

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [dict(job) for job in self._jobs]

    def add(
        self,
        *,
        schedule: str,
        action: dict,
        name: str | None = None,
        enabled: bool = True,
    ) -> tuple[dict | None, str | None]:
        """新增条目并落盘；返回 (job, error) 二选一。"""
        try:
            job = self._normalize(
                {"schedule": schedule, "action": action, "name": name, "enabled": enabled}
            )
        except CronError as exc:
            return None, str(exc)
        with self._lock:
            self._jobs.append(job)
            self._save_locked()
        return dict(job), None

    def remove(self, job_id: str) -> tuple[bool, str]:
        with self._lock:
            for i, job in enumerate(self._jobs):
                if job["id"] == job_id:
                    del self._jobs[i]
                    self._last_fired.pop(job_id, None)
                    self._save_locked()
                    return True, job_id
        return False, f"no such cron job {job_id!r}"

    def toggle(self, job_id: str, target: bool | None = None) -> tuple[dict | None, str | None]:
        """启停切换：target 缺省取反。返回 (job, error) 二选一。"""
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job["enabled"] = (not job["enabled"]) if target is None else bool(target)
                    self._save_locked()
                    return dict(job), None
        return None, f"no such cron job {job_id!r}"

    # ------------------------------------------------------------------ 调度

    def start(self) -> None:
        """启动调度线程（已在跑则幂等返回）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="zcode-cron"
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001, S112 —— 单次 tick 失败不杀调度线程（无日志设施，静默续拍）
                continue

    def tick(self, now: datetime | None = None) -> list[str]:
        """一个调度拍：找出窗口内到期且未发过的条目并触发。返回触发的 job id
        （测试观测点）。enabled=false 的条目只跳过、不记 last_fired。"""
        # cron 语义 = 本地墙钟的朴素时间（与 crontab 一致），刻意不带时区
        now = now or datetime.now()  # noqa: DTZ005
        with self._lock:
            jobs = [dict(job) for job in self._jobs]
        fired: list[str] = []
        for job in jobs:
            if not job["enabled"]:
                continue
            due = latest_due_minute(job["schedule"], now)
            if due is None:
                continue
            key = due.strftime("%Y-%m-%dT%H:%M")
            with self._lock:
                if self._last_fired.get(job["id"]) == key:
                    continue
                self._last_fired[job["id"]] = key
            self._fire(job)
            fired.append(job["id"])
        return fired

    def _fire(self, job: dict) -> None:
        """触发动作；回调异常吞掉（记录面 leave to caller 的帧流）。"""
        action = job["action"]
        try:
            if action["type"] == "prompt":
                self._on_prompt(action["text"])
            else:
                self._on_task(action["command"], job.get("name") or None)
        except Exception:  # noqa: BLE001 —— 动作失败不影响后续条目/下一拍
            return


__all__ = [
    "ACTION_TYPES",
    "CronError",
    "CronManager",
    "cron_matches",
    "latest_due_minute",
    "parse_cron",
    "validate_action",
]
