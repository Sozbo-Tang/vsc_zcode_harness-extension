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
  + M4 四件：fork（分叉点之前轮次复制成新会话并广播）/ pin（收藏切换）/
  export（Markdown 经 result 帧返回）/ set_root（multi-root 切当前根）
  （compact 属 M8，明确回 unsupported）
- git_commit 属后续里程碑，回 unsupported

会话转录（transcript.jsonl + meta.json）由 sessions/ 落盘；resume 重放历史
out 帧（不重复入账）。入站 user_message 落转录时附 messageId（fork 分叉点与
checkpoint.atMessageId 对齐）。MEMORY.md 跨会话记忆（R24）在会话启动时注入
system prompt 尾部，``memory`` 工具读写它（read 免批 / 写与 write 同档）。
API Key 只从环境变量读（config.api_key_for），且不随 bash 子进程环境泄漏
（tools/bash.sanitize_child_env）。
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
from . import vision_relay
from .checkpoints import CheckpointManager
from .config import (
    DEFAULT_CONFIG,
    ConfigError,
    api_key_for,
    get_model_entry,
    get_provider_entry,
    load_config,
)
from .envprobe import environment_facts, probe as probe_environment
from .llm import Provider, ProviderError, default_provider_factory, ping as ping_provider
from .llm.base import ImageBlock
from .loop import AgentLoop, DEFAULT_SYSTEM_PROMPT, context_from_frames
from .memory import MemoryPermissionEngine, memory_injection, register_memory_tool
from .perf import PerfTracker
from .protocol import (
    Checkpoint,
    Control,
    CoreToShellMessage,
    DiffProposed,
    DiffReview,
    EnvInfo,
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
        # M2：工具/权限/检查点；M4：memory 工具（R24）挂进注册表
        self._router = ResponseRouter()
        self._registry = default_registry()
        register_memory_tool(self._registry)
        self._checkpoints: CheckpointManager | None = None
        self._workspace_cwd = Path.cwd()
        self._permission_timeout_s = DEFAULT_PERMISSION_TIMEOUT_S
        # M5：环境探测（R7/R21）后台线程状态与注入缓存
        self._env_probe_lock = threading.Lock()
        self._env_probe_thread: threading.Thread | None = None
        self._env_facts: str | None = None

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
        # M5（R7/R21）：init 后台线程探测硬件+工作区语言 → 广播 env_info；
        # 不阻塞读线程与首条消息（环境注入规则见 _system_prompt_with_env）
        self._spawn_env_probe()

    # ------------------------------------------------------------------ M5 环境探测

    def _spawn_env_probe(self) -> bool:
        """启动一次环境探测（已在跑则返回 False）。"""
        if not self._env_probe_lock.acquire(blocking=False):
            return False
        thread = threading.Thread(
            target=self._run_env_probe, daemon=True, name="zcode-envprobe"
        )
        self._env_probe_thread = thread
        thread.start()
        return True

    def _run_env_probe(self) -> None:
        """探测 → 广播 env_info（含环境面板所需全部事实）→ 环境事实注入
        system prompt。探测失败广播 error 但不影响会话。"""
        try:
            env = probe_environment(self._workspace_roots)
            self._emit(
                EnvInfo(hardware=env["hardware"], workspaceLangs=env["workspaceLangs"])
            )
            self._env_facts = environment_facts(env)
            loop = self._loop
            if loop is not None:
                loop.system_prompt = self._system_prompt_with_env()
        except Exception as exc:  # noqa: BLE001 —— 探测失败只降级，不拖垮会话
            self._emit(
                Error(kind="env", message=f"environment probe failed: {type(exc).__name__}: {exc}")
            )
        finally:
            self._env_probe_lock.release()

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
        # R11：新会话默认第一个根（会话内 set_root 过的根不带到新会话）；
        # resume 的会话根恢复在 _control_resume 处理
        if self._workspace_roots:
            self._workspace_cwd = Path(self._workspace_roots[0]).expanduser()
            self._checkpoints = CheckpointManager(self._workspace_cwd)
        meta = self._store.create(
            workspace_roots=self._workspace_roots,
            provider_id=(self._provider_entry or {}).get("id"),
            model_id=(self._model_entry or {}).get("id"),
            root_path=str(self._workspace_cwd) if self._workspace_roots else None,
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

    def _session_summaries(self, metas: list | None = None) -> list:
        """sessions_list 载荷（M4 字段集，None 值剔除保持载荷紧凑）。"""
        summaries = [
            {
                "sessionId": m.get("sessionId"),
                "title": m.get("title"),
                "pinned": bool(m.get("pinned")),
                "lastActivity": m.get("updatedAt"),
                "messageCount": int(m.get("messageCount") or 0),
                "rootPath": m.get("rootPath"),
                "providerId": m.get("providerId"),
                "modelId": m.get("modelId"),
                "parentSessionId": m.get("parentSessionId"),
            }
            for m in (metas if metas is not None else self._store.list_sessions())
        ]
        return [{k: v for k, v in s.items() if v is not None} for s in summaries]

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

    def _system_prompt_with_env(self) -> str:
        """系统提示词组装：settings 覆盖 → 环境事实 → 跨会话记忆。

        M5 环境注入规则（R21）：探测在后台线程进行，**不阻塞首条消息**——
        探测完成前的轮次用裸提示词；探测完成后（``_run_env_probe`` 更新
        ``loop.system_prompt``）注入生效于**后续轮次**（LLMRequest.system
        每轮从 loop.system_prompt 现取）。
        """
        prompt = str(self._settings.get("systemPrompt") or DEFAULT_SYSTEM_PROMPT)
        if self._env_facts:
            prompt += "\n\n" + self._env_facts
        # R24：会话启动注入跨会话记忆尾部（无 MEMORY.md / 空文件不占 token）
        injection = memory_injection()
        if injection:
            prompt += injection
        return prompt

    def _rebuild_loop(self) -> None:
        perf = PerfTracker()
        system_prompt = self._system_prompt_with_env()
        allow_rules = [
            str(r) for r in (self._settings.get("permissionAllow") or []) if isinstance(r, str)
        ]
        engine = MemoryPermissionEngine(allow_rules=allow_rules)
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
        # messageId 先于一切落盘动作生成：转录入站帧、checkpoint.atMessageId、
        # fork 分叉点共用同一标识（R16/R30 对齐）
        message_id = "um_" + uuid.uuid4().hex[:10]
        inbound = encode_message(msg)
        inbound["messageId"] = message_id
        self._store.append(session_id, inbound, direction="in")
        try:
            # R16/R27：首条用户消息定 title（前 30 字符）+ 输入历史 + 轮次计数
            self._store.record_user_input(session_id, msg.text or "")
        except SessionError:
            pass
        # 检查点：每条 user_message 处理前做 git 快照（R30；不动工作区）。
        # sha 与消息 id 随 checkpoint 事件入转录，rollback 凭转录找回 sha。
        if self._checkpoints is not None:
            snap = self._checkpoints.snapshot(message_id)
            if snap.ok:
                self._emit(
                    Checkpoint(id=snap.checkpoint_id, sha=snap.sha, atMessageId=message_id)
                )
        text = _decorate_user_message(msg)
        images = [
            str(p)
            for p in ((msg.context.images if msg.context is not None else None) or [])
            if str(p).strip()
        ]
        self._cancel.clear()  # 上一回合的 cancel 不应泄漏到新回合
        thread = threading.Thread(
            target=self._run_turn, args=(session_id, text, images), daemon=True, name="zcode-turn"
        )
        self._turn_thread = thread
        thread.start()

    def _run_turn(self, session_id: str, text: str, images: list[str] | None = None) -> None:
        assert self._loop is not None
        try:
            # M5 视觉入口（R9）：主模型支持视觉 → ImageBlock 直入请求；否则
            # 中继转述段落并入用户文本（原图不进主请求）。中继在回合线程内
            # 做——它本身是一次模型流式调用，可能秒级阻塞。
            blocks, relay_paragraphs = self._prepare_images(images or [])
            if relay_paragraphs:
                suffix = "\n\n".join(relay_paragraphs)
                text = f"{text}\n\n{suffix}" if text else suffix
            self._loop.run_user_text(text, images=blocks or None)
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

    def _prepare_images(self, images: list[str]) -> tuple[list, list[str]]:
        """图片分流（R9）：返回 (主请求 ImageBlock 列表, 中继转述段落)。

        - 主模型档案 ``capabilities.vision`` → 读文件 base64 成 ImageBlock；
        - 无视觉 → 视觉中继（vision_relay.relay_images）：每图广播
          ``vision_relay`` 帧（经 emitter 入转录，可审计）；未配置中继模型
          广播 ``error{kind:"config"}``，图片按无视觉处理（跳过，不阻塞）。
        """
        if not images:
            return [], []
        if vision_relay.model_supports_vision(self._model_entry):
            blocks: list = []
            for path in images:
                try:
                    blocks.append(ImageBlock.from_file(path))
                except OSError as exc:
                    self._emit(Error(kind="vision", message=f"cannot read image {path}: {exc}"))
            return blocks, []
        result = vision_relay.relay_images(
            images,
            config=self._config or {},
            provider_entry=self._provider_entry,
            settings=self._settings,
            provider_factory=self._factory,
            api_key_env=self._api_key_env,
            on_frame=self._emit,
            cancel_event=self._cancel,
        )
        if result.error is not None:
            self._emit(result.error)
        for err in result.errors:
            self._emit(err)
        return [], result.paragraphs

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
            "fork": self._control_fork,
            "pin": self._control_pin,
            "export": self._control_export,
            "set_root": self._control_set_root,
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
        # R11：恢复会话回到它记录的根（meta.rootPath 须仍在当前 workspaceRoots
        # 内——工作区已移除的根不能复活）；检查点管理器随根重建（sha 绑定仓库）
        self._restore_session_root(meta)
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

    # ------------------------------------------------------------------ M4：fork / pin / export / set_root

    def _control_fork(self, msg: Control) -> None:
        """R16 分叉：分叉点之前的完整轮次复制成新会话（语义见 SessionStore.fork）。

        新会话是并列的独立会话（各自进程），本进程只代为落盘 + 广播；新会话的
        session_started / 更新的 sessions_list 用 record=False——它们描述的是
        兄弟会话，不属于本会话的 UI 事件流（fork() 落盘时已在新转录写首帧）。
        """
        source_id = msg.sessionId or self._emitter.session_id
        if not source_id:
            self._emit(Error(kind="protocol", message="fork requires sessionId"))
            return
        if not msg.atMessageId:
            self._emit(Error(kind="protocol", message="fork requires atMessageId"))
            return
        try:
            new_meta = self._store.fork(source_id, msg.atMessageId)
        except SessionError as exc:
            self._emit(Error(kind="not_found", message=str(exc)))
            return
        new_id = new_meta["sessionId"]
        self._emitter.emit(SessionStarted(sessionId=new_id), record=False)
        self._emitter.emit(SessionsList(sessions=self._session_summaries()), record=False)
        self._emit(Result(ok=True, command="fork", detail=new_id))

    def _control_pin(self, msg: Control) -> None:
        """R28：pinned 标志切换（pinned 缺省 = 取反）；置顶渲染由 UI 决定。"""
        session_id = msg.sessionId or self._emitter.session_id
        if not session_id:
            self._emit(Error(kind="protocol", message="pin requires sessionId"))
            return
        try:
            current = bool(self._store.get_meta(session_id).get("pinned"))
            target = (not current) if msg.pinned is None else bool(msg.pinned)
            self._store.set_pinned(session_id, target)
        except SessionError as exc:
            self._emit(Error(kind="not_found", message=str(exc)))
            return
        self._emit(Result(ok=True, command="pin", detail="pinned" if target else "unpinned"))

    def _control_export(self, msg: Control) -> None:
        """R12：transcript → Markdown，经 result.markdown 返回全文；
        保存落盘由 UI 决定（核侧不替用户选路径）。"""
        session_id = msg.sessionId or self._emitter.session_id
        if not session_id:
            self._emit(Error(kind="protocol", message="export requires sessionId"))
            return
        try:
            markdown = self._store.export_markdown(session_id)
        except SessionError as exc:
            self._emit(Error(kind="not_found", message=str(exc)))
            return
        self._emit(Result(ok=True, command="export", markdown=markdown))

    def _control_set_root(self, msg: Control) -> None:
        """R11 multi-root：切换当前会话工具 ctx 的 cwd（不指定 = 默认第一个根）。

        切根即切仓库：CheckpointManager 随根重建（检查点 sha 绑定原仓库，
        留在旧管理器上无从回滚）；rootIndex / rootPath 都必须命中
        init.workspaceRoots——不接受工作区之外的路径当根。
        """
        roots = list(self._workspace_roots or [])
        if not roots:
            self._emit(
                Error(kind="protocol", message="set_root requires init.workspaceRoots")
            )
            return
        candidates = {str(Path(r).expanduser()) for r in roots} | {str(r) for r in roots}
        if msg.rootIndex is not None:
            if not 0 <= msg.rootIndex < len(roots):
                self._emit(
                    Error(
                        kind="protocol",
                        message=f"rootIndex {msg.rootIndex} out of range "
                        f"(0..{len(roots) - 1})",
                    )
                )
                return
            root = str(Path(roots[msg.rootIndex]).expanduser())
        elif msg.rootPath:
            if str(Path(msg.rootPath).expanduser()) not in candidates:
                self._emit(
                    Error(
                        kind="protocol",
                        message=f"rootPath {msg.rootPath!r} is not one of "
                        "init.workspaceRoots",
                    )
                )
                return
            root = str(Path(msg.rootPath).expanduser())
        else:
            root = str(Path(roots[0]).expanduser())
        self._workspace_cwd = Path(root)
        if self._loop is not None and self._loop.tool_session is not None:
            self._loop.tool_session.ctx.cwd = self._workspace_cwd
        self._checkpoints = CheckpointManager(self._workspace_cwd)
        session_id = self._emitter.session_id
        if session_id is not None:
            try:
                self._store.update_meta(session_id, rootPath=root)
            except SessionError:
                pass
        self._emit(Result(ok=True, command="set_root", detail=root))

    def _restore_session_root(self, meta: dict) -> None:
        """resume 时回到 meta.rootPath 记录的根（仍在当前 workspaceRoots 内才生效）。"""
        recorded = meta.get("rootPath")
        if not recorded:
            return
        roots = list(self._workspace_roots or [])
        candidates = {str(Path(r).expanduser()) for r in roots} | {str(r) for r in roots}
        if str(Path(recorded).expanduser()) in candidates:
            self._workspace_cwd = Path(recorded).expanduser()
            self._checkpoints = CheckpointManager(self._workspace_cwd)

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
