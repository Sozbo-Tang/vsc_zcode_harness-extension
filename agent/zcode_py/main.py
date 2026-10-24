"""stdio JSONL 服务入口（PLAN §5 协议 v1 的核侧端点）。

读 stdin 逐行帧、写 stdout 逐行帧（立即 flush）。分发：

- ``init`` → 建会话 → ``session_started`` + ``telegraph_state`` + ``sessions_list``
  + 检查点可用性（git 探测；无仓库广播降级说明）
- ``user_message`` → 先做 git 检查点快照（广播 ``checkpoint``；不动工作区）→
  丢进 AgentLoop（工作线程执行，读线程继续收 control/permission_response）
- ``permission_response{id, decision}`` → 路由给阻塞中的权限等待（M2）
- ``diff_review{id, accepted}`` → 路由给 edit 的 diff 审阅闸门（M2）
- ``rollback{checkpointId}`` → ``git checkout <sha> -- .``（M2；命令本身即
  用户的显式确认动作——UI「回滚到这」按钮）
- ``control``：new_session / resume / cancel / telegraph / switch_model / ping
  （compact 属 M8，明确回 unsupported）
- git_commit 属后续里程碑，回 unsupported

会话转录（transcript.jsonl + meta.json）由 sessions/ 落盘；resume 重放历史
out 帧（不重复入账）。API Key 只从环境变量读（config.api_key_for），且不随
bash 子进程环境泄漏（tools/bash.sanitize_child_env）。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, TextIO

from . import telegraph
from .checkpoints import CheckpointManager
from .config import (
    DEFAULT_CONFIG,
    ConfigError,
    api_key_for,
    get_model_entry,
    get_provider_entry,
    load_config,
)
from .llm import Provider, ProviderError, default_provider_factory, ping as ping_provider
from .loop import AgentLoop, DEFAULT_SYSTEM_PROMPT, context_from_frames
from .perf import PerfTracker
from .permissions import PermissionEngine
from .protocol import (
    Checkpoint,
    Control,
    CoreToShellMessage,
    DiffProposed,
    DiffReview,
    Error,
    GitCommit,
    Init,
    MessageContext,
    PermissionRequest,
    PermissionResponse,
    PingResult,
    ProtocolError,
    Result,
    Rollback,
    RollbackResult,
    SessionStarted,
    SessionsList,
    TelegraphState,
    Usage,
    UserMessage,
    decode_line,
    encode_line,
    encode_message,
)
from .sessions import SessionError, SessionStore
from .tools import ToolSession, default_registry

DEFAULT_PERMISSION_TIMEOUT_S = 300.0


class _Waiter:
    """一次阻塞等待的载体：answer 由读线程 resolve 填入。"""

    __slots__ = ("event", "answer")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.answer: dict | None = None


class ResponseRouter:
    """跨线程应答路由：回合线程先 create+广播请求，再阻塞 wait；读线程
    resolve 填入应答。先注册后广播，杜绝「应答先于等待注册到达」的竞态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, _Waiter] = {}

    def create(self, key: str) -> _Waiter:
        waiter = _Waiter()
        with self._lock:
            self._waiters[key] = waiter
        return waiter

    def wait(
        self,
        waiter: _Waiter,
        key: str,
        *,
        timeout: float,
        cancel: threading.Event | None = None,
    ) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            if waiter.event.wait(0.05):
                return waiter.answer
            if cancel is not None and cancel.is_set():
                self._drop(key)
                return None
            if time.monotonic() >= deadline:
                self._drop(key)
                return None

    def resolve(self, key: str, payload: dict) -> bool:
        with self._lock:
            waiter = self._waiters.get(key)
            if waiter is None:
                return False
            waiter.answer = payload
            waiter.event.set()
            return True

    def _drop(self, key: str) -> None:
        with self._lock:
            self._waiters.pop(key, None)


class StdioEmitter:
    """线程安全的帧写出器：stdout 行 + （可选）转录追加共用一把锁。"""

    def __init__(self, stream: TextIO, store: SessionStore | None = None):
        self._stream = stream
        self._store = store
        self._lock = threading.Lock()
        self._session_id: str | None = None

    def bind_session(self, session_id: str | None) -> None:
        with self._lock:
            self._session_id = session_id

    @property
    def session_id(self) -> str | None:
        with self._lock:
            return self._session_id

    def emit(self, msg: CoreToShellMessage, *, record: bool = True) -> None:
        line = encode_line(msg)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
            if (
                record
                and self._store is not None
                and self._session_id is not None
            ):
                self._store.append(self._session_id, encode_message(msg), direction="out")

    def emit_raw(self, frame: dict) -> None:
        """重放历史帧：只写出、不入账（转录里已有原始记录）。"""
        with self._lock:
            self._stream.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self._stream.flush()


class ZcodeService:
    def __init__(
        self,
        emitter: StdioEmitter,
        *,
        provider_factory: Callable[[dict, str | None], Provider] = default_provider_factory,
        config_file: Path | None = None,
        store: SessionStore | None = None,
    ):
        self._emitter = emitter
        self._factory = provider_factory
        self._config_file = config_file
        self._store = store or SessionStore()
        self._config: dict | None = None
        self._provider: Provider | None = None
        self._provider_entry: dict | None = None
        self._model_entry: dict | None = None
        self._provider_error: str | None = None
        self._profile = telegraph.parse_profile(None)
        self._level = "off"
        self._loop: AgentLoop | None = None
        self._cancel = threading.Event()
        self._turn_thread: threading.Thread | None = None
        self._api_key_env: dict[str, str] = {}
        self._settings: dict = {}
        self._workspace_roots: list = []
        self._usage_by_model: dict[str, dict[str, int]] = {}
        # M2：工具/权限/检查点
        self._router = ResponseRouter()
        self._registry = default_registry()
        self._checkpoints: CheckpointManager | None = None
        self._workspace_cwd = Path.cwd()
        self._permission_timeout_s = DEFAULT_PERMISSION_TIMEOUT_S

    # ------------------------------------------------------------------ 帧入口

    def handle_line(self, line: str) -> None:
        try:
            msg = decode_line(line)
        except ProtocolError as exc:
            self._emit(Error(kind="protocol", message=str(exc)))
            return
        if isinstance(msg, Init):
            self._on_init(msg)
        elif isinstance(msg, UserMessage):
            self._on_user_message(msg)
        elif isinstance(msg, Control):
            self._on_control(msg)
        elif isinstance(msg, PermissionResponse):
            self._on_permission_response(msg)
        elif isinstance(msg, DiffReview):
            self._on_diff_review(msg)
        elif isinstance(msg, Rollback):
            self._on_rollback(msg)
        elif isinstance(msg, GitCommit):
            self._emit(Error(kind="unsupported", message="git flow lands in a later milestone"))
        else:  # pragma: no cover —— 注册表里核→壳类型不该出现在入站方向
            self._emit(Error(kind="protocol", message=f"unexpected inbound frame: {type(msg).__name__}"))

    def shutdown(self, join_timeout: float = 2.0) -> None:
        """EOF 收尾：先给进行中的回合机会跑完，超时才取消（再等一次）。

        直接 set 取消会与「回合线程尚未调度」竞态，把本来能正常完成的回合
        误标为 cancelled。
        """
        thread = self._turn_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                self._cancel.set()
                thread.join(timeout=join_timeout)

    # ------------------------------------------------------------------ init

    def _on_init(self, msg: Init) -> None:
        self._workspace_roots = list(msg.workspaceRoots or [])
        self._settings = dict(msg.settings or {})
        self._api_key_env = dict(msg.apiKeyEnv or {})
        self._workspace_cwd = (
            Path(self._workspace_roots[0]).expanduser()
            if self._workspace_roots
            else Path.cwd()
        )
        try:
            self._permission_timeout_s = max(
                1.0, float(self._settings.get("permissionTimeoutSec") or DEFAULT_PERMISSION_TIMEOUT_S)
            )
        except (TypeError, ValueError):
            self._permission_timeout_s = DEFAULT_PERMISSION_TIMEOUT_S
        self._checkpoints = CheckpointManager(self._workspace_cwd)
        self._ensure_config()
        entry = get_provider_entry(self._config or {})
        model = get_model_entry(entry) if entry else None
        self._apply_model(entry, model, keep_level=None)
        self._start_session()

    def _ensure_config(self) -> None:
        if self._config is not None:
            return
        try:
            self._config = load_config(self._config_file)
        except ConfigError as exc:
            self._config = json.loads(json.dumps(DEFAULT_CONFIG))
            self._emit(Error(kind="config", message=str(exc) + " (using built-in defaults)"))

    # ------------------------------------------------------------------ 会话

    def _start_session(self) -> None:
        meta = self._store.create(
            workspace_roots=self._workspace_roots,
            provider_id=(self._provider_entry or {}).get("id"),
            model_id=(self._model_entry or {}).get("id"),
        )
        self._emitter.bind_session(meta["sessionId"])
        self._cancel.clear()
        self._rebuild_loop()
        self._emit(SessionStarted(sessionId=meta["sessionId"]))
        self._emit(self._telegraph_message())
        self._emit(SessionsList(sessions=self._session_summaries()))
        # 检查点可用性：无 git 仓库时广播降级说明而非抛错（R30 优雅降级）。
        # 每次会话开始广播一次，避免逐条消息刷屏。
        if self._checkpoints is not None and not self._checkpoints.available:
            self._emit(
                Result(
                    ok=False,
                    command="checkpoint",
                    detail=f"checkpoints degraded: {self._checkpoints.degraded_reason}",
                )
            )

    def _session_summaries(self) -> list:
        return [
            {
                "sessionId": m.get("sessionId"),
                "title": m.get("title"),
                "updatedAt": m.get("updatedAt"),
                "providerId": m.get("providerId"),
                "modelId": m.get("modelId"),
            }
            for m in self._store.list_sessions()
        ]

    # ------------------------------------------------------------------ 模型

    def _apply_model(self, entry: dict | None, model: dict | None, keep_level: str | None) -> None:
        self._provider_entry = entry
        self._model_entry = model
        self._provider_error = None
        if entry is not None and model is not None:
            self._provider = self._make_provider(entry)
            self._profile = telegraph.parse_profile(model)
            if keep_level is not None:
                self._level = telegraph.resolve_level(self._profile, keep_level)
            else:
                self._level = self._profile.default_level
        else:
            self._provider = None
            self._profile = telegraph.parse_profile(None)
            self._level = "off"
            if entry is None:
                self._provider_error = "no provider configured in providers.json"
        if self._loop is not None and self._provider is not None:
            self._loop.provider = self._provider
            self._loop.model = self.model_id or "unknown"

    def _make_provider(self, entry: dict) -> Provider | None:
        key = api_key_for(entry.get("id") or "", self._api_key_env)
        try:
            return self._factory(entry, key)
        except ProviderError as exc:
            self._provider_error = exc.message
            return None
        except Exception as exc:  # noqa: BLE001
            self._provider_error = f"cannot build provider: {exc}"
            return None

    @property
    def model_id(self) -> str | None:
        return (self._model_entry or {}).get("id")

    def _rebuild_loop(self) -> None:
        perf = PerfTracker()
        system_prompt = str(self._settings.get("systemPrompt") or DEFAULT_SYSTEM_PROMPT)
        allow_rules = [
            str(r) for r in (self._settings.get("permissionAllow") or []) if isinstance(r, str)
        ]
        engine = PermissionEngine(allow_rules=allow_rules)
        session = ToolSession(
            registry=self._registry,
            permissions=engine,
            cwd=self._workspace_cwd,
            emit=self._emit,
            ask_permission=self._ask_permission,
            review_diff=self._review_diff,
            cancel_event=self._cancel,
        )
        if isinstance(self._settings.get("searchBaseUrl"), str):
            session.ctx.state["searchBaseUrl"] = self._settings["searchBaseUrl"]
        self._loop = AgentLoop(
            provider=self._provider or _NullProvider(),
            model=self.model_id or "unknown",
            emit=self._emit,
            perf=perf,
            system_prompt=system_prompt,
            max_turns=int(self._settings.get("maxTurns") or 8),
            max_output_tokens=(self._model_entry or {}).get("maxOutput"),
            thinking_resolver=lambda: telegraph.translate(self._profile, self._level),
            tool_session=session,
            cancel_event=self._cancel,
            on_usage=self._accumulate_usage,
        )

    def _accumulate_usage(self, model: str, usage) -> None:
        bucket = self._usage_by_model.setdefault(model, {"input": 0, "output": 0})
        if usage.input_tokens is not None:
            bucket["input"] += int(usage.input_tokens)
        if usage.output_tokens is not None:
            bucket["output"] += int(usage.output_tokens)

    def _telegraph_message(self) -> TelegraphState:
        return telegraph.telegraph_state(self._profile, self._level)

    # ------------------------------------------------------------------ 用户消息

    def _on_user_message(self, msg: UserMessage) -> None:
        if self._turn_alive():
            self._emit(Error(kind="busy", message="a turn is already running; cancel it first"))
            return
        if self._loop is None or self._store is None or self._emitter.session_id is None:
            self._emit(Error(kind="protocol", message="init required before user_message"))
            return
        if self._provider is None:
            self._emit(
                Error(
                    kind="config",
                    message=self._provider_error or "no provider configured",
                )
            )
            return
        session_id = self._emitter.session_id
        self._store.append(session_id, encode_message(msg), direction="in")
        try:
            self._store.update_meta(session_id, title=(msg.text or "")[:40] or None)
        except SessionError:
            pass
        # 检查点：每条 user_message 处理前做 git 快照（R30；不动工作区）。
        # sha 与消息 id 随 checkpoint 事件入转录，rollback 凭转录找回 sha。
        message_id = "um_" + uuid.uuid4().hex[:10]
        if self._checkpoints is not None:
            snap = self._checkpoints.snapshot(message_id)
            if snap.ok:
                self._emit(
                    Checkpoint(id=snap.checkpoint_id, sha=snap.sha, atMessageId=message_id)
                )
        text = _decorate_user_message(msg)
        self._cancel.clear()  # 上一回合的 cancel 不应泄漏到新回合
        thread = threading.Thread(
            target=self._run_turn, args=(session_id, text), daemon=True, name="zcode-turn"
        )
        self._turn_thread = thread
        thread.start()

    def _run_turn(self, session_id: str, text: str) -> None:
        assert self._loop is not None
        try:
            self._loop.run_user_text(text)
        except ProviderError as exc:
            self._emit(Error(kind=exc.kind, message=exc.message))
        except Exception as exc:  # noqa: BLE001
            self._emit(Error(kind="unknown", message=f"{type(exc).__name__}: {exc}"))
        finally:
            totals = {
                model: vals["input"] + vals["output"]
                for model, vals in self._usage_by_model.items()
            }
            self._emit(Usage(tokens=sum(totals.values()), byModel=dict(self._usage_by_model)))
            try:
                self._store.update_meta(session_id)
            except SessionError:
                pass

    def _turn_alive(self) -> bool:
        thread = self._turn_thread
        return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------ control

    def _on_control(self, msg: Control) -> None:
        handler = {
            "new_session": self._control_new_session,
            "resume": self._control_resume,
            "cancel": self._control_cancel,
            "telegraph": self._control_telegraph,
            "switch_model": self._control_switch_model,
            "ping": self._control_ping,
            "compact": self._control_compact,
        }.get(msg.command)
        if handler is None:
            self._emit(Error(kind="protocol", message=f"unknown control command: {msg.command!r}"))
            return
        handler(msg)

    def _control_new_session(self, msg: Control) -> None:
        if self._turn_alive():
            self._emit(Error(kind="busy", message="cannot start a new session while a turn is running"))
            return
        self._ensure_config()
        self._start_session()
        self._emit(Result(ok=True, command="new_session"))

    def _control_resume(self, msg: Control) -> None:
        if self._turn_alive():
            self._emit(Error(kind="busy", message="cannot resume while a turn is running"))
            return
        session_id = msg.sessionId
        if not session_id:
            self._emit(Error(kind="protocol", message="resume requires sessionId"))
            return
        try:
            meta = self._store.get_meta(session_id)
            frames = self._store.read_frames(session_id)
        except SessionError as exc:
            self._emit(Error(kind="not_found", message=str(exc)))
            return
        self._ensure_config()
        entry = get_provider_entry(self._config or {}, meta.get("providerId"))
        model = get_model_entry(entry, meta.get("modelId")) if entry else None
        if entry is None or model is None:
            self._emit(
                Error(
                    kind="config",
                    message=f"provider {meta.get('providerId')!r} no longer configured; cannot resume",
                )
            )
            return
        self._apply_model(entry, model, keep_level=self._level)
        self._emitter.bind_session(session_id)
        self._cancel.clear()
        self._rebuild_loop()
        self._loop.load_context(context_from_frames(frames))
        # 重放：session_started + 历史 out 帧（不重复入账），再广播当前状态
        self._emitter.emit_raw({"type": "session_started", "sessionId": session_id})
        for envelope in frames:
            if envelope.get("dir") == "out":
                self._emitter.emit_raw(envelope.get("event") or {})
        self._emitter.emit(self._telegraph_message(), record=False)
        self._emitter.emit(SessionsList(sessions=self._session_summaries()), record=False)
        self._emit(Result(ok=True, command="resume", detail=f"{len(frames)} frames replayed"))

    def _control_cancel(self, msg: Control) -> None:
        self._cancel.set()
        self._emit(Result(ok=True, command="cancel"))

    def _control_telegraph(self, msg: Control) -> None:
        level = msg.level
        if level not in telegraph.CANONICAL_LEVELS:
            self._emit(
                Error(
                    kind="protocol",
                    message=f"invalid telegraph level {level!r}; expected one of {list(telegraph.CANONICAL_LEVELS)}",
                )
            )
            return
        resolved = telegraph.resolve_level(self._profile, level)
        self._level = resolved
        self._emitter.emit(self._telegraph_message())
        detail = None
        if resolved != level:
            detail = f"level {level!r} unavailable on this model; downshifted to {resolved!r}"
        self._emit(Result(ok=True, command="telegraph", detail=detail))

    def _control_switch_model(self, msg: Control) -> None:
        if self._turn_alive():
            self._emit(Error(kind="busy", message="cannot switch model while a turn is running"))
            return
        self._ensure_config()
        config = self._config or {}
        entry = get_provider_entry(config, msg.providerId)
        if entry is None:
            self._emit(
                Error(
                    kind="config",
                    message=f"unknown provider {msg.providerId!r}; known: "
                    + ", ".join(sorted(p.get("id", "?") for p in config.get("providers") or [])),
                )
            )
            return
        model = get_model_entry(entry, msg.modelId)
        if model is None or not (msg.modelId or entry.get("defaultModel")):
            self._emit(
                Error(
                    kind="config",
                    message=f"unknown model {msg.modelId!r} for provider {msg.providerId!r}",
                )
            )
            return
        previous_level = self._level
        self._apply_model(entry, model, keep_level=previous_level)
        session_id = self._emitter.session_id
        if session_id is not None:
            try:
                self._store.update_meta(
                    session_id,
                    providerId=entry.get("id"),
                    modelId=model.get("id"),
                )
            except SessionError:
                pass
        self._emitter.emit(self._telegraph_message())
        detail = f"{previous_level} -> {self._level}" if self._level != previous_level else None
        self._emit(Result(ok=True, command="switch_model", detail=detail))

    def _control_ping(self, msg: Control) -> None:
        self._ensure_config()
        config = self._config or {}
        provider_id = msg.providerId or (self._provider_entry or {}).get("id")
        entry = get_provider_entry(config, provider_id)
        if entry is None:
            self._emit(
                PingResult(
                    ok=False,
                    errorKind="unknown",
                    message=f"unknown provider {provider_id!r}",
                )
            )
            return
        same_as_current = provider_id == (self._provider_entry or {}).get("id")
        model_id = msg.modelId or (
            self.model_id if same_as_current else entry.get("defaultModel")
        )
        if not model_id:
            self._emit(PingResult(ok=False, errorKind="unknown", message="no model specified"))
            return
        key = api_key_for(entry.get("id") or "", self._api_key_env)
        try:
            provider = self._factory(entry, key)
        except Exception as exc:  # noqa: BLE001
            self._emit(PingResult(ok=False, errorKind="unknown",
                                  message=f"cannot build provider: {exc}"))
            return
        outcome = ping_provider(provider, model_id)
        self._emit(
            PingResult(
                ok=outcome["ok"],
                latencyMs=outcome["latencyMs"],
                errorKind=outcome["errorKind"],
                message=outcome["message"],
            )
        )

    def _control_compact(self, msg: Control) -> None:
        self._emit(Error(kind="unsupported", message="compaction lands in M8 (PLAN §3.4)"))

    # ------------------------------------------------------------------ M2：权限 / diff 审阅 / 回滚

    def _ask_permission(self, tool: str, args: dict) -> str:
        """回合线程内阻塞等待 permission_response；先注册再广播，防应答竞态。

        返回 allow|deny|always|timeout|cancelled（不合法应答按 deny 处理）。
        """
        request_id = "perm_" + uuid.uuid4().hex[:10]
        waiter = self._router.create(request_id)
        self._emit(PermissionRequest(id=request_id, tool=tool, args=dict(args)))
        reply = self._router.wait(
            waiter,
            request_id,
            timeout=self._permission_timeout_s,
            cancel=self._cancel,
        )
        if reply is None:
            return "cancelled" if self._cancel.is_set() else "timeout"
        decision = reply.get("decision")
        return decision if decision in ("allow", "deny", "always") else "deny"

    def _review_diff(self, path: str, old_text: str, new_text: str) -> bool:
        """edit 的 diff 审阅闸门：广播 diff_proposed，阻塞等 diff_review；
        无应答（超时/取消/拒绝）一律视为拒绝——不动文件。"""
        review_id = "diff_" + uuid.uuid4().hex[:10]
        waiter = self._router.create(review_id)
        self._emit(
            DiffProposed(id=review_id, file=path, oldText=old_text, newText=new_text)
        )
        reply = self._router.wait(
            waiter,
            review_id,
            timeout=self._permission_timeout_s,
            cancel=self._cancel,
        )
        return bool(reply and reply.get("accepted") is True)

    def _on_permission_response(self, msg: PermissionResponse) -> None:
        if not self._router.resolve(msg.id, {"decision": msg.decision}):
            self._emit(
                Error(kind="not_found", message=f"no pending permission request {msg.id!r}")
            )

    def _on_diff_review(self, msg: DiffReview) -> None:
        if not self._router.resolve(msg.id, {"accepted": bool(msg.accepted)}):
            self._emit(
                Error(kind="not_found", message=f"no pending diff proposal {msg.id!r}")
            )

    def _on_rollback(self, msg: Rollback) -> None:
        """rollback = ``git checkout <快照sha> -- .``（恢复全部已跟踪文件）。

        确认语义：rollback 帧本身即用户显式动作（UI「回滚到这」按钮带确认），
        核侧收到即执行——协议 v1 无二次确认往返（PLAN §5）。
        """
        if self._turn_alive():
            self._emit(Error(kind="busy", message="cannot roll back while a turn is running"))
            return
        if self._checkpoints is None or not self._checkpoints.available:
            self._emit(RollbackResult(ok=False))
            self._emit(
                Error(
                    kind="checkpoint",
                    message="checkpoints unavailable: "
                    + (self._checkpoints.degraded_reason if self._checkpoints else "not initialized"),
                )
            )
            return
        sha = self._checkpoint_sha(msg.checkpointId)
        if not sha:
            self._emit(RollbackResult(ok=False))
            self._emit(
                Error(kind="not_found", message=f"unknown checkpoint {msg.checkpointId!r}")
            )
            return
        outcome = self._checkpoints.rollback(sha)
        self._emit(RollbackResult(ok=outcome.ok))
        if not outcome.ok:
            self._emit(Error(kind="checkpoint", message=outcome.detail or "rollback failed"))
        elif outcome.detail:
            self._emit(Result(ok=True, command="rollback", detail=outcome.detail))

    def _checkpoint_sha(self, checkpoint_id: str) -> str | None:
        """从本会话转录找 checkpoint 事件的 sha（resume 后同样可用）。"""
        session_id = self._emitter.session_id
        if session_id is None:
            return None
        try:
            frames = self._store.read_frames(session_id)
        except SessionError:
            return None
        for envelope in frames:
            event = envelope.get("event") or {}
            if (
                envelope.get("dir") == "out"
                and event.get("type") == "checkpoint"
                and event.get("id") == checkpoint_id
            ):
                return event.get("sha")
        return None

    # ------------------------------------------------------------------ emit

    def _emit(self, msg: CoreToShellMessage) -> None:
        self._emitter.emit(msg)


class _NullProvider:
    """占位：无 provider 时 loop 尚未收到用户消息即被拦截，不应走到这里。"""

    def stream(self, request):  # pragma: no cover
        raise ProviderError("config", "no provider available")
    protocol = "null"
    provider_id = "null"


def _decorate_user_message(msg: UserMessage) -> str:
    """把 PLAN §5 的 context 附件拼成一段自然语言上下文（M1 简式）。"""
    parts: list[str] = []
    ctx: MessageContext | None = msg.context
    if ctx is not None:
        refs = [str(r) for r in (ctx.fileRefs or []) if r]
        if refs:
            parts.append("Referenced files: " + ", ".join(refs))
        if ctx.selection:
            parts.append("Selected code:\n```\n" + ctx.selection + "\n```")
        if ctx.terminalSel:
            parts.append("Terminal selection:\n```\n" + ctx.terminalSel + "\n```")
        if ctx.problems:
            parts.append("Problems:\n" + ctx.problems)
    if msg.text:
        parts.append(msg.text)
    return "\n\n".join(p for p in parts if p)


def main(
    stream_in: TextIO | None = None,
    stream_out: TextIO | None = None,
    *,
    provider_factory: Callable[[dict, str | None], Provider] | None = None,
    config_file: Path | None = None,
    sessions_dir: Path | None = None,
) -> int:
    """stdio 服务主循环：逐行读入、逐帧分发；EOF 即退出。"""
    stream_in = stream_in if stream_in is not None else sys.stdin
    stream_out = stream_out if stream_out is not None else sys.stdout
    store = SessionStore(sessions_dir) if sessions_dir is not None else SessionStore()
    emitter = StdioEmitter(stream_out, store=store)
    service = ZcodeService(
        emitter,
        provider_factory=provider_factory or default_provider_factory,
        config_file=config_file,
        store=store,
    )
    for raw in stream_in:
        line = raw.strip()
        if line:
            service.handle_line(line)
    service.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
