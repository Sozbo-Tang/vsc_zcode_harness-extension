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
  + M5 env_refresh（环境重探测）+ M7 skills_list（已加载 SKILL.md 列表）
  （compact 属 M8，明确回 unsupported）
- M6（R8）：browser-use 可选组——可用才注册 browser_navigate/browser_screenshot
  （截图经 image_artifact 帧）；未安装时对幻觉调用返回安装指引
- M7（R25/R26）：run_child 子代理工具（上下文防火墙、深度=1、可指定模型）
  + SKILL.md 加载器（后台扫描注入 system prompt；用户消息 /<name> 开头展开
  并广播 result 帧注明）
- M9（R3）：后台任务（background_start/tasks_list/task_stop，任务面板数据
  源；帧 task_started/task_output/task_finished）+ 定时任务（cron.json +
  30s 调度线程；prompt 动作经 _intake_lock 串行化的同一用户消息路径注入，
  task 动作走后台任务）+ 工作流（~/.zcode-py/workflows/*.py 声明式 STEPS，
  顺序受限子代理执行，报告经 result 帧）
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

from . import telegraph, vision_relay
from .background import DEFAULT_TASK_TIMEOUT_S, BackgroundTaskManager
from .checkpoints import CheckpointManager
from .compaction import SEARCH_HISTORY_SPEC, CompactionManager
from .config import (
    DEFAULT_CONFIG,
    ConfigError,
    api_key_for,
    get_model_entry,
    get_provider_entry,
    home_dir,
    load_config,
)
from .cronjobs import CronManager
from .envprobe import environment_facts
from .envprobe import probe as probe_environment
from .hooks import HooksConfigError, HooksRunner, load_hooks
from .llm import Provider, ProviderError, default_provider_factory
from .llm import ping as ping_provider
from .llm.base import ImageBlock, LLMRequest, LLMUserMessage, TextBlock, TextDelta
from .loop import DEFAULT_SYSTEM_PROMPT, AgentLoop, context_from_frames
from .mcp import McpManager
from .memory import MemoryPermissionEngine, memory_injection, register_memory_tool
from .perf import PerfTracker
from .permissions import ALLOW, DENY, PERMISSION_MODES, mode_aware_engine
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
    SessionsList,
    SessionStarted,
    TelegraphState,
    Usage,
    UserMessage,
    decode_line,
    encode_line,
    encode_message,
)
from .sessions import SessionError, SessionStore
from .skills import SkillsManager, expand_slash, skills_injection
from .subagents import RUN_CHILD_SPEC, ChildRuntime
from .tools import ToolSession, default_registry
from .tools.browser import (
    BROWSER_TOOL_NAMES,
    browser_unavailable_outcome,
    register_browser_tools,
)
from .tools.notebook import register_notebook_tools
from .workflows import WorkflowError, WorkflowManager

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


class _BrowserHintSession(ToolSession):
    """browser-use 未安装（工具未注册）时的兜底：模型幻觉调用 browser_* 工具
    名（schema 里没有，但用户点名要浏览器时模型可能猜）→ 返回结构化安装
    指引而不是干巴巴的 unknown_tool。"""

    def run(self, name: str, args: dict, call_id: str):
        if name in BROWSER_TOOL_NAMES:
            return browser_unavailable_outcome(name)
        return super().run(name, args, call_id)


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
        # M2：工具/权限/检查点；M4：memory 工具（R24）挂进注册表；
        # M6（R8）：browser-use 可选组——未安装不注册（False），幻觉调用由
        # _BrowserHintSession 返回结构化安装指引；M7（R25）：run_child 子代理；
        # M8（R31）：notebook 三件套 + search_history（L4 冷存检索）
        self._router = ResponseRouter()
        self._registry = default_registry()
        # R3 工具开关 + 默认权限（tools.json：disabled / alwaysAllow），注册表
        # 构建后立即应用（重启后开关与白名单保持）
        self._tool_state = self._load_tool_state()
        for _name in self._tool_state.get("disabled") or []:
            if isinstance(_name, str):
                self._registry.set_enabled(_name, False)
        register_memory_tool(self._registry)
        self._browser_registered = register_browser_tools(self._registry)
        self._registry.register(RUN_CHILD_SPEC)
        self._registry.register(SEARCH_HISTORY_SPEC)
        register_notebook_tools(self._registry)
        # M8（R3）：MCP client——server 连接在 init 后台进行（失败不阻塞会话）
        self._mcp = McpManager(registry=self._registry)
        # M8 hooks：init 时按 ~/.zcode-py/hooks.json 装配（缺位/损坏降级见 _on_init）
        self._hooks: HooksRunner | None = None
        # M8 计划模式：build | plan | yolo（init.settings.permissionMode 可预置）
        self._permission_mode = "build"
        # M8（R14）：上下文压缩管理器（阈值在 init 按 settings 调整）
        self._compaction = CompactionManager(
            emit=self._emit,
            loop_supplier=lambda: self._loop,
            provider_supplier=lambda: (
                self._provider, self.model_id, self._provider_entry, self._model_entry
            ),
            config_supplier=lambda: self._config or {},
            settings_supplier=lambda: self._settings,
            provider_factory=self._factory,
            api_key_env=self._api_key_env,
        )
        self._checkpoints: CheckpointManager | None = None
        self._workspace_cwd = Path.cwd()
        self._permission_timeout_s = DEFAULT_PERMISSION_TIMEOUT_S
        # M7（R26）：skills 目录扫描（init 后台线程；快照供 system prompt 注入
        # 与斜杠展开）；_permissions_engine 供子代理共享权限引擎
        self._skills = SkillsManager(self._workspace_cwd)
        self._permissions_engine: MemoryPermissionEngine | None = None
        # M5：环境探测（R7/R21）后台线程状态与注入缓存
        self._env_probe_lock = threading.Lock()
        self._env_probe_thread: threading.Thread | None = None
        self._env_facts: str | None = None
        # M9（R3）：后台任务（任务面板数据源；工作线程走 bash_backend）
        self._background = BackgroundTaskManager(
            emit=self._emit,
            cwd_supplier=lambda: self._workspace_cwd,
        )
        # M9（R3）：定时任务（配置在构造时加载；调度线程 init 时启动）
        self._cron = CronManager(
            on_prompt=self._cron_inject_prompt,
            on_task=self._cron_start_task,
        )
        # M9（R3）：工作流（~/.zcode-py/workflows/*.py 声明式 STEPS）
        self._workflows = WorkflowManager()
        self._workflow_thread: threading.Thread | None = None
        # M9 线程安全：用户消息入口串行化锁——cron 线程注入 prompt 与读线程
        # 的 user_message 并发到达时，_turn_alive 检查+落盘+起回合线程必须是
        # 原子段（否则两条消息同时通过 busy 闸门，转录与回合线程都会交错）
        self._intake_lock = threading.Lock()

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
        """EOF 收尾：先给进行中的回合机会跑完，超时才取消（再等一次）；
        最后关停 MCP server 子进程。

        直接 set 取消会与「回合线程尚未调度」竞态，把本来能正常完成的回合
        误标为 cancelled。
        """
        thread = self._turn_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                self._cancel.set()
                thread.join(timeout=join_timeout)
        self._mcp.shutdown()
        # M9（R3）：停 cron 调度线程 + 后台输出监测线程（后台命令线程是
        # daemon，随进程退场；语义见 background.py docstring）
        self._cron.stop()
        self._background.shutdown()

    # ------------------------------------------------------------------ init

    def _on_init(self, msg: Init) -> None:
        self._workspace_roots = list(msg.workspaceRoots or [])
        self._settings = dict(msg.settings or {})
        # apiKeyEnv 原位更新：compaction manager 等持有同一 dict 的组件不落旧值
        self._api_key_env.clear()
        self._api_key_env.update(msg.apiKeyEnv or {})
        self._workspace_cwd = (
            Path(self._workspace_roots[0]).expanduser()
            if self._workspace_roots
            else Path.cwd()
        )
        # M8 计划模式：settings.permissionMode 预置（control set_permission_mode
        # 可随切；非法值回 build）
        mode = str(self._settings.get("permissionMode") or "build")
        self._permission_mode = mode if mode in PERMISSION_MODES else "build"
        # M8（R14）：压缩阈值（settings.compaction.evictThreshold / summarizeThreshold）
        compaction_cfg = self._settings.get("compaction")
        if isinstance(compaction_cfg, dict):
            for key, attr in (
                ("evictThreshold", "evict_threshold"),
                ("summarizeThreshold", "summarize_threshold"),
            ):
                value = compaction_cfg.get(key)
                if isinstance(value, (int, float)) and 0 < value <= 1:
                    setattr(self._compaction, attr, float(value))
        try:
            self._permission_timeout_s = max(
                1.0, float(self._settings.get("permissionTimeoutSec") or DEFAULT_PERMISSION_TIMEOUT_S)
            )
        except (TypeError, ValueError):
            self._permission_timeout_s = DEFAULT_PERMISSION_TIMEOUT_S
        # M8 hooks：装配 ~/.zcode-py/hooks.json（损坏广播 config error 后按无
        # hooks 继续；缝缺位由 HooksRunner.startup_check 广播一次并禁用）
        try:
            rules = load_hooks()
        except HooksConfigError as exc:
            rules = []
            self._emit(Error(kind="config", message=str(exc) + " (hooks disabled)"))
        self._hooks = HooksRunner(rules, emit=self._emit) if rules else None
        if self._hooks is not None:
            self._hooks.startup_check()
        self._checkpoints = CheckpointManager(self._workspace_cwd)
        self._ensure_config()
        entry = get_provider_entry(self._config or {})
        model = get_model_entry(entry) if entry else None
        self._apply_model(entry, model, keep_level=None)
        self._start_session()
        # M8（R3）：MCP server 后台连接（连接失败只标状态，不阻塞会话）
        self._mcp.connect_all(background=True)
        # M5（R7/R21）：init 后台线程探测硬件+工作区语言 → 广播 env_info；
        # 不阻塞读线程与首条消息（环境注入规则见 _system_prompt_with_env）
        self._spawn_env_probe()
        # M7（R26）：skills 目录后台扫描（不阻塞首条消息；注入规则同环境事实
        # ——扫描完成前的轮次用裸提示词，完成后生效于后续轮次；斜杠展开在
        # _on_user_message 里按需同步加载，不受扫描时机影响）
        self._skills = SkillsManager(self._workspace_cwd)
        self._spawn_skills_scan()
        # M9（R3）：cron 调度器——enabled 条目才触发（tick 内跳过 disabled）；
        # 配置损坏（load_error，构造时加载）广播降级后按空表继续
        if self._cron.load_error:
            self._emit(
                Error(kind="config", message=self._cron.load_error + " (cron jobs disabled)")
            )
        self._cron.start()

    # ------------------------------------------------------------------ M7 skills

    def _spawn_skills_scan(self) -> None:
        def _scan() -> None:
            loaded = self._skills.ensure_loaded()
            loop = self._loop
            if loaded and loop is not None:
                loop.system_prompt = self._system_prompt_with_env()

        threading.Thread(target=_scan, daemon=True, name="zcode-skills").start()

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
        system prompt。探测失败广播 error 但不影响会话。锁在广播前释放：
        紧随首个 env_info 到来的 env_refresh 不应撞上「emit 与 release 之间
        的窗口」而被误报 busy。"""
        try:
            env = probe_environment(self._workspace_roots)
        except Exception as exc:  # noqa: BLE001 —— 探测失败只降级，不拖垮会话
            self._env_probe_lock.release()
            self._emit(
                Error(kind="env", message=f"environment probe failed: {type(exc).__name__}: {exc}")
            )
            return
        self._env_probe_lock.release()
        self._emit(EnvInfo(hardware=env["hardware"], workspaceLangs=env["workspaceLangs"]))
        self._env_facts = environment_facts(env)
        loop = self._loop
        if loop is not None:
            loop.system_prompt = self._system_prompt_with_env()

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
        # M7（R26）：已加载 skills 列表注入（loaded_skills 不触发同步扫描——
        # 后台扫描完成前不阻塞、不占首轮 token；完成后经 _spawn_skills_scan
        # 更新 loop.system_prompt 生效于后续轮次）
        skills_block = skills_injection(self._skills.loaded_skills())
        if skills_block:
            prompt += skills_block
        # R24：会话启动注入跨会话记忆尾部（无 MEMORY.md / 空文件不占 token）
        injection = memory_injection()
        if injection:
            prompt += injection
        return prompt

    def _browser_headless(self) -> bool:
        """R8 有头/无头：init.settings.browser.headless（默认 headless）。"""
        browser_cfg = self._settings.get("browser")
        if isinstance(browser_cfg, dict) and isinstance(browser_cfg.get("headless"), bool):
            return browser_cfg["headless"]
        return True

    def _rebuild_loop(self) -> None:
        perf = PerfTracker()
        system_prompt = self._system_prompt_with_env()
        allow_rules = [
            str(r) for r in (self._settings.get("permissionAllow") or []) if isinstance(r, str)
        ]
        # M8 计划模式：三档语义包在规则引擎外层（build 委托原语义；plan 只读
        # DENY；yolo 免询问但 DANGER 黑名单仍强制，PLAN §9.3）
        engine = mode_aware_engine(
            MemoryPermissionEngine(allow_rules=allow_rules),
            tier_lookup=lambda name: (
                spec.tier if (spec := self._registry.get(name)) is not None else None
            ),
            mode_getter=lambda: self._permission_mode,
        )
        # 子代理与主会话共享同一个权限引擎（会话内 always 记忆互通，M7/R25）
        self._permissions_engine = engine
        # tools.json 的 alwaysAllow 白名单热并入（R3 默认权限配置）
        self._apply_allow_rules()
        session_cls = ToolSession if self._browser_registered else _BrowserHintSession
        session = session_cls(
            registry=self._registry,
            permissions=engine,
            cwd=self._workspace_cwd,
            emit=self._emit,
            ask_permission=self._ask_permission,
            review_diff=self._review_diff,
            cancel_event=self._cancel,
        )
        # 工具 ctx 注入（browser 截图帧广播 / headless 配置 / 子代理运行时）
        session.ctx.state["emit"] = self._emit
        session.ctx.state["browserHeadless"] = self._browser_headless()
        # M8 hooks：pre_tool 阻断 / post_tool stdout 附加（装配缝缺位时 runner
        # 自身已禁用，直通）
        if self._hooks is not None:
            session.ctx.state["hooks"] = self._hooks
        # M8（R14）L4：search_history 的转录路径供应（会话切换自动跟随）
        session.ctx.state["history_reader"] = self._history_reader
        session.ctx.state["child_runtime"] = ChildRuntime(
            registry=self._registry,
            permissions=engine,
            cwd=self._workspace_cwd,
            emit=self._emit,
            ask_permission=self._ask_permission,
            review_diff=self._review_diff,
            provider_supplier=lambda: (self._provider, self.model_id, self._provider_entry),
            factory=self._factory,
            config_supplier=lambda: self._config or {},
            api_key_env=self._api_key_env,
            main_cancel=self._cancel,
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
            on_model_usage=self._compaction.on_model_usage,
        )

    def _history_reader(self):
        """search_history（L4）的转录路径：当前绑定会话的 transcript.jsonl。"""
        session_id = self._emitter.session_id
        if session_id is None:
            return None
        return self._store.transcript_path(session_id)

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
        # M9：入口串行化——本方法同时被读线程（shell user_message 帧）与 cron
        # 调度线程（prompt 动作注入）调用；busy 闸门 + 落盘 + 起回合线程必须
        # 原子（_intake_lock），否则并发消息会双双通过闸门交错落转录。
        # 锁内含 git 检查点快照（子进程调用，百毫秒级）：串行化正确性优先于
        # 注入延迟。读线程与 cron 线程共用同一条路径——checkpoint / 输入历史 /
        # 斜杠展开 / 权限通道全部复用，cron 注入对用户可见可审计。
        with self._intake_lock:
            if self._turn_alive():
                self._emit(Error(kind="busy", message="a turn is already running; cancel it first"))
                return
            if self._workflow_alive():
                # 工作流运行中不接受新消息：_cancel 事件为二者共享，新回合的
                # cancel.clear() 会剥夺工作流的取消能力
                self._emit(
                    Error(kind="busy", message="a workflow is running; cancel it first")
                )
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
            # M7（R26）斜杠命令：/<name> 开头且命中已加载 skill → SKILL.md 正文拼进
            # 该轮消息前缀（斜杠 token 被展开体替换），广播 result 帧注明展开；
            # 未命中原样透传
            skill_prefix, rest, skill = expand_slash(msg.text or "", self._skills.ensure_loaded())
            if skill is not None:
                tail = _decorate_user_message(UserMessage(text=rest, context=msg.context))
                text = skill_prefix + (("\n\n" + tail) if tail else "")
                self._emit(
                    Result(
                        ok=True,
                        command="skill",
                        detail=f"expanded skill {skill.name!r} from {skill.path}",
                    )
                )
            else:
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
            "search_sessions": self._control_search_sessions,
            "env_refresh": self._control_env_refresh,
            "skills_list": self._control_skills_list,
            "set_permission_mode": self._control_set_permission_mode,
            "permission_mode": self._control_set_permission_mode,
            "mcp_list": self._control_mcp_list,
            "mcp_refresh": self._control_mcp_refresh,
            "mcp_add": self._control_mcp_add,
            "mcp_remove": self._control_mcp_remove,
            # M9（R3）：后台任务 / 定时任务 / 工作流
            "background_start": self._control_background_start,
            "tasks_list": self._control_tasks_list,
            "task_stop": self._control_task_stop,
            "cron_list": self._control_cron_list,
            "cron_add": self._control_cron_add,
            "cron_remove": self._control_cron_remove,
            "cron_toggle": self._control_cron_toggle,
            "workflow_run": self._control_workflow_run,
            "workflows_list": self._control_workflows_list,
            "inline_complete": self._control_inline_complete,
            "tools_list": self._control_tools_list,
            "tool_toggle": self._control_tool_toggle,
            "tool_permission": self._control_tool_permission,
        }.get(msg.command)
        if handler is None:
            self._emit(Error(kind="protocol", message=f"unknown control command: {msg.command!r}"))
            return
        handler(msg)

    def _control_new_session(self, msg: Control) -> None:
        # 闸门与变更原子化（独立评审中危 #6：锁外检查与 cron 注入/工作流
        # 启动存在 TOCTOU，可致两会话线程并发共用 _cancel 与转录）
        with self._intake_lock:
            if self._turn_alive() or self._workflow_alive():
                self._emit(Error(kind="busy", message="cannot start a new session while a turn or workflow is running"))
                return
            self._ensure_config()
            self._start_session()
            self._emit(Result(ok=True, command="new_session"))

    def _control_resume(self, msg: Control) -> None:
        # 闸门与会话换绑原子化（同 new_session：防 cron/工作流并发 TOCTOU）
        with self._intake_lock:
            if self._turn_alive() or self._workflow_alive():
                self._emit(Error(kind="busy", message="cannot resume while a turn or workflow is running"))
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
        """M8（R14）L3 手动压缩：control compact（PLAN §5）。

        回合进行中拒绝（压缩要重建 loop.messages，与流式/工具执行竞态）；
        摘要模型失败时 manager 内部降级 L2，报告带 error+fallback 字段。
        """
        if self._turn_alive() or self._workflow_alive():
            self._emit(Error(kind="busy", message="cannot compact while a turn or workflow is running"))
            return
        loop = self._loop
        if loop is None:
            self._emit(Error(kind="protocol", message="init required before compact"))
            return
        report = self._compaction.compact_now(loop, reason="manual")
        ok = not report.get("error")
        self._emit(
            Result(ok=ok, command="compact", detail=json.dumps(report, ensure_ascii=False))
        )

    # ------------------------------------------------------------------ M8：计划模式 / MCP 管理

    def _control_set_permission_mode(self, msg: Control) -> None:
        """M8 计划模式：build | plan | yolo（webview store.setPermissionMode 已按
        此命令名下发；宿主 protocol.ts 枚举镜像暂缺——宽容转发）。

        plan=只读（写档工具结构化拒绝，不弹卡）；yolo=写档免询问但 DANGER
        黑名单仍强制（PLAN §9.3）。生效于下一次工具调用（无状态迁移）。
        """
        mode = msg.mode
        if mode not in PERMISSION_MODES:
            self._emit(
                Error(
                    kind="protocol",
                    message=f"invalid permission mode {mode!r}; expected one of {list(PERMISSION_MODES)}",
                )
            )
            return
        self._permission_mode = mode
        self._emit(Result(ok=True, command="set_permission_mode", detail=mode))

    def _control_mcp_list(self, msg: Control) -> None:
        """M8（R3）MCP 面板：server 状态列表（Result.detail JSON）。

        后台连接进行中会看到 status=connecting；连接失败 status=error + 原因
        （含装配缝缺位指引）。
        """
        self._emit(
            Result(
                ok=True,
                command="mcp_list",
                detail=json.dumps(self._mcp.status_list(), ensure_ascii=False),
            )
        )

    def _control_mcp_refresh(self, msg: Control) -> None:
        """M8：关停全部 server + 重读 mcp.json + 重连（后台）。"""
        if self._turn_alive():
            self._emit(Error(kind="busy", message="cannot refresh MCP servers while a turn is running"))
            return
        self._mcp.refresh(background=True)
        self._emit(Result(ok=True, command="mcp_refresh", detail="reconnecting; poll mcp_list"))

    def _control_mcp_add(self, msg: Control) -> None:
        """M8：写入 mcp.json 并连接（server 字段 = {command:[argv], env?, cwd?}）。

        宽容载荷：webview 提案形态（query JSON 全量 {"command": "空格分隔
        argv" 或 [argv], "env": {...}, "cwd"?}）也收——两侧以 Result 回执对账。
        """
        name = (msg.name or "").strip()
        if not name:
            self._emit(Error(kind="protocol", message="mcp_add requires a non-empty 'name'"))
            return
        server = msg.server if isinstance(msg.server, dict) and msg.server else None
        if server is None:
            server = self._mcp_server_from_query(msg)
        if not server:
            self._emit(
                Error(
                    kind="protocol",
                    message="mcp_add requires 'server' {command:[argv], env?, cwd?} "
                    "或 query JSON {command, env?}",
                )
            )
            return
        error = self._mcp.add_and_connect({"name": name, **server})
        if error:
            self._emit(Error(kind="config", message=error))
            return
        self._emit(Result(ok=True, command="mcp_add", detail=name))

    @staticmethod
    def _mcp_server_from_query(msg: Control) -> dict | None:
        """webview 提案载荷：query JSON 里的 {command, env?, cwd?} → server 条目。"""
        import shlex

        raw = (msg.query or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        command = data.get("command")
        if command is None and isinstance(data.get("args"), list):
            command = data["args"]
        if isinstance(command, str):
            try:
                command = shlex.split(command)
            except ValueError:
                return None
        if not isinstance(command, list) or not command:
            return None
        server: dict = {"command": [str(c) for c in command]}
        env = data.get("env")
        if isinstance(env, dict) and env:
            server["env"] = {str(k): str(v) for k, v in env.items()}
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd:
            server["cwd"] = cwd
        return server

    def _control_mcp_remove(self, msg: Control) -> None:
        """M8：从 mcp.json 摘除 + 关停 + 注销 mcp__<server>__* 工具。"""
        name = (msg.name or "").strip()
        if not name:
            self._emit(Error(kind="protocol", message="mcp_remove requires a non-empty 'name'"))
            return
        error = self._mcp.remove(name)
        if error:
            self._emit(Error(kind="config", message=error))
            return
        self._emit(Result(ok=True, command="mcp_remove", detail=name))

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
        at_message_id = self._resolve_fork_point(source_id, msg.atMessageId)
        if not at_message_id:
            self._emit(
                Error(kind="not_found", message=f"fork point {msg.atMessageId!r} not found in session {source_id}")
            )
            return
        try:
            new_meta = self._store.fork(source_id, at_message_id)
        except SessionError as exc:
            self._emit(Error(kind="not_found", message=str(exc)))
            return
        new_id = new_meta["sessionId"]
        self._emitter.emit(SessionStarted(sessionId=new_id), record=False)
        self._emitter.emit(SessionsList(sessions=self._session_summaries()), record=False)
        self._emit(Result(ok=True, command="fork", detail=new_id))

    FORK_AT_LAST_USER_MESSAGE = "__last_user_message__"

    def _resolve_fork_point(self, source_id: str, at_message_id: str | None) -> str | None:
        """分叉点解析：显式 messageId 直接用；哨兵/缺省 = 源转录最后一条入站
        user_message 的 messageId（webview 无转录内 id 时按此约定发送哨兵）。"""
        if at_message_id and at_message_id != self.FORK_AT_LAST_USER_MESSAGE:
            return at_message_id
        try:
            frames = self._store.read_frames(source_id)
        except SessionError:
            return None
        for envelope in reversed(frames):
            event = envelope.get("event") or {}
            if envelope.get("dir") == "in" and event.get("type") == "user_message":
                mid = event.get("messageId")
                if isinstance(mid, str) and mid:
                    return mid
        return None

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

    def _control_search_sessions(self, msg: Control) -> None:
        """M4 全文搜索：标题 + 转录文本匹配，结果经 Result.detail（JSON 数组）返回。"""
        query = (msg.query or "").strip()
        if not query:
            self._emit(Error(kind="protocol", message="search_sessions requires query"))
            return
        matches = self._store.search_sessions(query)
        self._emit(
            Result(ok=True, command="search_sessions", detail=json.dumps(matches, ensure_ascii=False))
        )

    def _control_env_refresh(self, msg: Control) -> None:
        """M5 环境面板「刷新」：重新探测并广播 env_info（探测线程内完成广播）。"""
        if not self._spawn_env_probe():
            self._emit(
                Error(kind="busy", message="环境探测正在进行中，请稍候（env_info 到达即完成）")
            )
            return
        self._emit(Result(ok=True, command="env_refresh", detail="probing"))

    def _control_skills_list(self, msg: Control) -> None:
        """M7 Skills 面板：已加载 SKILL.md 列表（name+description，JSON 直通 UI）。"""
        skills = self._skills.ensure_loaded()
        payload = [
            {"name": s.name, "description": s.description, "path": s.path}
            for s in sorted(skills.values(), key=lambda s: s.name)
        ]
        self._emit(
            Result(ok=True, command="skills_list", detail=json.dumps(payload, ensure_ascii=False))
        )

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

    # ------------------------------------------------------------------ M9：后台任务 / 定时任务 / 工作流

    def _workflow_alive(self) -> bool:
        thread = self._workflow_thread
        return thread is not None and thread.is_alive()

    def _control_background_start(self, msg: Control) -> None:
        """M9（R3）后台任务启动——**必须先过权限引擎**（独立评审高危 #1 修复：
        该入口曾完全绕过 DANGER 黑名单与 plan 模式，§9.3/§9.5 双违反）。

        策略：danger 命中 → 一律拒绝（后台是长驻无监督执行面，黑名单命令
        请经 bash 工具交互确认）；plan → 引擎 DENY；yolo / 白名单 / 会话
        always → 直接启动；build 其余 → 异步弹权限卡（读线程不能阻塞等
        应答——应答 allow 才真正启动，deny/超时回执 ok=False）。
        """
        command = msg.taskCommand
        if not isinstance(command, str) or not command.strip():
            self._emit(
                Error(
                    kind="protocol",
                    message="background_start requires a non-empty 'taskCommand' "
                    "(the control frame's own 'command' field names the control)",
                )
            )
            return
        timeout = DEFAULT_TASK_TIMEOUT_S
        if msg.timeoutSec is not None:
            if msg.timeoutSec <= 0:
                self._emit(
                    Error(kind="protocol", message="timeoutSec must be a positive number")
                )
                return
            timeout = float(msg.timeoutSec)
        check = self._background_command_check(command)
        if check.outcome == DENY:
            self._emit(Error(kind="permission", message=check.reason))
            return
        if check.danger:
            self._emit(
                Error(
                    kind="permission",
                    message=f"危险命令拒绝后台执行（{check.reason}）；"
                    "如确需运行请通过 bash 工具交互确认",
                )
            )
            return
        if check.outcome == ALLOW:
            self._start_background_now(command, msg.name, timeout)
            return
        # build 默认询问：异步权限卡（独立线程等应答，不阻塞读线程）
        request_id = "perm_" + uuid.uuid4().hex[:10]
        waiter = self._router.create(request_id)
        self._emit(
            PermissionRequest(id=request_id, tool="background", args={"command": command})
        )

        def wait_reply() -> None:
            reply = self._router.wait(
                waiter, request_id, timeout=self._permission_timeout_s
            )
            decision = reply.get("decision") if reply else None
            if decision not in ("allow", "always"):
                reason = "denied" if decision == "deny" else "permission timeout"
                self._emit(Result(ok=False, command="background_start", detail=reason))
                return
            # 应答后复检（模式/规则可能已变；danger 永不因应答放行）
            recheck = self._background_command_check(command)
            if recheck.danger or recheck.outcome == DENY:
                self._emit(
                    Result(ok=False, command="background_start", detail="denied by re-check")
                )
                return
            if decision == "always" and self._permissions_engine is not None:
                try:
                    self._permissions_engine.remember_always("background", {"command": command})
                except Exception:  # noqa: BLE001 —— 记忆失败不影响启动
                    pass
            self._start_background_now(command, msg.name, timeout)

        threading.Thread(target=wait_reply, daemon=True, name="zcode-bg-perm").start()

    def _background_command_check(self, command: str):
        """后台/定时命令的权限判定：走服务级 ModeAware 引擎（伪工具
        ``background``，DANGER_SCANNED_TOOLS 覆盖黑名单；plan 档经 tier
        兜底 DENY）。引擎未建（理论仅 init 前）时按无白名单 build 语义判。"""
        engine = self._permissions_engine
        if engine is None:
            from .permissions import PermissionEngine

            allow_rules = [
                str(r) for r in (self._settings.get("permissionAllow") or [])
                if isinstance(r, str)
            ]
            engine = PermissionEngine(allow_rules=allow_rules)
        return engine.check("background", {"command": command})

    def _start_background_now(self, command: str, name: str | None, timeout: float) -> None:
        record = self._background.start(
            command, name=name, timeout=timeout, cwd=self._workspace_cwd
        )
        self._emit(Result(ok=True, command="background_start", detail=record["taskId"]))

    # ------------------------------------------------------------------ R3：工具开关 + 默认权限

    def _tool_state_path(self) -> Path:
        return home_dir() / "config" / "tools.json"

    def _load_tool_state(self) -> dict:
        try:
            data = json.loads(self._tool_state_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"disabled": [], "alwaysAllow": []}

    def _save_tool_state(self) -> None:
        path = self._tool_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._tool_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _tool_allow_rules(self) -> list[str]:
        return [
            str(r)
            for r in (self._tool_state.get("alwaysAllow") or [])
            if isinstance(r, str) and ":" in r
        ]

    def _apply_allow_rules(self) -> None:
        """热更权限引擎白名单 = settings.permissionAllow + tools.json.alwaysAllow
        （service 级共享引擎就地改写 inner.allow_rules，无需重建 loop）。"""
        engine = self._permissions_engine
        if engine is None:
            return
        engine.inner.allow_rules = [
            str(r) for r in (self._settings.get("permissionAllow") or []) if isinstance(r, str)
        ] + self._tool_allow_rules()

    def _control_tools_list(self, msg: Control) -> None:
        """R3 工具面板数据源：全部注册工具（含禁用态）+ 默认权限态。"""
        always = {r.split(":", 1)[0] for r in self._tool_allow_rules()}
        items = [
            {
                "name": spec.name,
                "description": spec.description,
                "tier": spec.tier,
                "enabled": self._registry.get(spec.name) is not None,
                "alwaysAllowed": spec.name in always,
            }
            for spec in self._registry.specs()
        ]
        self._emit(
            Result(ok=True, command="tools_list", detail=json.dumps(items, ensure_ascii=False))
        )

    def _control_tool_toggle(self, msg: Control) -> None:
        """R3 工具开关：禁用工具不进 schema（模型不可见不可调）；持久化。"""
        name = (msg.name or "").strip()
        if not name:
            self._emit(Error(kind="protocol", message="tool_toggle requires a non-empty 'name'"))
            return
        if not self._registry.set_enabled(name, bool(msg.enabled)):
            self._emit(Error(kind="not_found", message=f"unknown tool {name!r}"))
            return
        self._tool_state["disabled"] = self._registry.disabled_names()
        self._save_tool_state()
        self._emit(
            Result(ok=True, command="tool_toggle", detail=f"{name}: {'on' if msg.enabled else 'off'}")
        )

    def _control_tool_permission(self, msg: Control) -> None:
        """R3 默认权限：always = 该工具免询问白名单规则 ``name: *``；ask = 移除
        该工具全部规则。**always 不绕过 DANGER 黑名单**（PLAN §9.3：引擎判定
        顺序黑名单在最前，bash 危险命令在 always 下仍强制弹卡）。"""
        name = (msg.name or "").strip()
        mode = msg.mode
        if not name:
            self._emit(Error(kind="protocol", message="tool_permission requires a non-empty 'name'"))
            return
        if mode not in ("ask", "always"):
            self._emit(
                Error(kind="protocol", message="tool_permission mode must be 'ask' | 'always'")
            )
            return
        rules = [r for r in self._tool_allow_rules() if r.split(":", 1)[0] != name]
        if mode == "always":
            rules.append(f"{name}: *")
        self._tool_state["alwaysAllow"] = rules
        self._save_tool_state()
        self._apply_allow_rules()
        self._emit(Result(ok=True, command="tool_permission", detail=f"{name}: {mode}"))

    def _control_tasks_list(self, msg: Control) -> None:
        """任务面板数据源：Result.detail JSON（state: running|finished|failed|
        cancelled；outputTail 尾部 8KB）。"""
        self._emit(
            Result(
                ok=True,
                command="tasks_list",
                detail=json.dumps(self._background.list_tasks(), ensure_ascii=False),
            )
        )

    def _control_task_stop(self, msg: Control) -> None:
        """停止标记（不强杀）：cancelRequested 置位，run_command 返回后置
        cancelled；SIGKILL 级强杀待 bash_backend 可取消接口。"""
        task_id = msg.taskId
        if not task_id:
            self._emit(Error(kind="protocol", message="task_stop requires taskId"))
            return
        ok, message = self._background.stop(task_id)
        self._emit(Result(ok=ok, command="task_stop", detail=message))

    def _control_cron_list(self, msg: Control) -> None:
        self._emit(
            Result(
                ok=True,
                command="cron_list",
                detail=json.dumps(self._cron.list_jobs(), ensure_ascii=False),
            )
        )

    def _control_cron_add(self, msg: Control) -> None:
        """新增定时任务：schedule（标准 5 段）+ action（prompt|task）+ name? +
        enabled（缺省 true）；校验失败回 config error（不落盘）。"""
        if not msg.schedule or not msg.schedule.strip():
            self._emit(Error(kind="protocol", message="cron_add requires 'schedule'"))
            return
        if msg.action is None:
            self._emit(
                Error(
                    kind="protocol",
                    message="cron_add requires 'action' {type:'prompt'|'task', text|command}",
                )
            )
            return
        # 命令型动作在「新增时」就要过权限门（独立评审高危 #2 修复：到点
        # 无人值守，不能等到 fire 才弹卡）：danger 一律拒绝；plan 拒绝；
        # 非白名单/yolo 拒绝并指引——fire 时还会复检（规则/模式可能已变）。
        if msg.action.get("type") == "task":
            command = str(msg.action.get("command") or "")
            check = self._background_command_check(command)
            if check.outcome == DENY:
                self._emit(
                    Error(kind="permission", message=f"cron task 动作被拒绝：{check.reason}")
                )
                return
            if check.danger:
                self._emit(
                    Error(
                        kind="permission",
                        message="危险命令不能作为定时任务动作（黑名单优先级高于一切，PLAN §9.3）",
                    )
                )
                return
            if check.outcome != ALLOW:
                self._emit(
                    Error(
                        kind="permission",
                        message="命令型定时任务需先在权限设置加白名单（或 yolo 模式）——"
                        "定时触发无人值守，不接受到点弹卡",
                    )
                )
                return
        job, error = self._cron.add(
            schedule=msg.schedule,
            action=msg.action,
            name=msg.name,
            enabled=True if msg.enabled is None else bool(msg.enabled),
        )
        if error is not None:
            self._emit(Error(kind="config", message=error))
            return
        self._emit(Result(ok=True, command="cron_add", detail=job["id"]))

    def _control_cron_remove(self, msg: Control) -> None:
        cron_id = msg.cronId or msg.name  # 宽容：早期 UI 提案用 name 通道
        if not cron_id:
            self._emit(Error(kind="protocol", message="cron_remove requires cronId"))
            return
        ok, message = self._cron.remove(cron_id)
        self._emit(Result(ok=ok, command="cron_remove", detail=message))

    def _control_cron_toggle(self, msg: Control) -> None:
        """启停切换：enabled 缺省 = 取反。"""
        cron_id = msg.cronId or msg.name  # 宽容：早期 UI 提案用 name 通道
        if not cron_id:
            self._emit(Error(kind="protocol", message="cron_toggle requires cronId"))
            return
        job, error = self._cron.toggle(cron_id, msg.enabled)
        if error is not None:
            self._emit(Result(ok=False, command="cron_toggle", detail=error))
            return
        self._emit(
            Result(
                ok=True,
                command="cron_toggle",
                detail=json.dumps(job, ensure_ascii=False),
            )
        )

    def _control_workflows_list(self, msg: Control) -> None:
        """扫描 ~/.zcode-py/workflows/*.py（坏文件降级 error 条目）。"""
        self._emit(
            Result(
                ok=True,
                command="workflows_list",
                detail=json.dumps(
                    self._workflows.list_workflows(), ensure_ascii=False
                ),
            )
        )

    def _control_inline_complete(self, msg: Control) -> None:
        """R13 内联补全（chat 模型模拟 FIM）：宿主 inlineCompletion 触发。

        provider 调用在 daemon 线程（读线程不阻塞）；一切失败以
        Result.ok=False 静默降级（宿主超时 5s 自弃，不弹 UI 噪音）。
        prefix/suffix 缺省时尝试 query JSON 双通道（宿主侧提案形态）。
        """
        prefix, suffix, path = msg.prefix, msg.suffix, msg.path
        if prefix is None and msg.query:
            try:
                data = json.loads(msg.query)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                prefix = data.get("prefix") if prefix is None else prefix
                suffix = data.get("suffix") if suffix is None else suffix
                path = data.get("path") if path is None else path
        if prefix is None:
            self._emit(
                Result(ok=False, command="inline_complete", detail="missing prefix")
            )
            return
        # 回合/工作流进行中不起补全调用（独立评审低危 #13：与回合帧交错入账
        # 污染转录 + 与主回合抢 provider）；补全 Result 一律不入转录
        if self._turn_alive() or self._workflow_alive():
            self._emit(Result(ok=False, command="inline_complete", detail="busy"))
            return
        # 快照并发引用（switch_model 可能并发改写字段；线程内不再裸读）
        entry = dict(self._provider_entry or {})
        key_env = dict(self._api_key_env or {})
        model_id = self.model_id

        def run() -> None:
            try:
                provider = self._factory(entry, api_key_for(entry.get("id") or "", key_env))
                request = LLMRequest(
                    model=model_id,
                    messages=[
                        LLMUserMessage(
                            [
                                TextBlock(
                                    "You are a code completion engine. Given the "
                                    "code before and after the cursor, output ONLY "
                                    "the text to insert at the cursor (no fences, "
                                    "no explanations, at most ~5 lines).\n\n"
                                    f"File: {path or 'untitled'}\n"
                                    "=== BEFORE CURSOR ===\n"
                                    f"{prefix[-4000:]}\n"
                                    "=== AFTER CURSOR ===\n"
                                    f"{(suffix or '')[:1500]}\n"
                                    "=== INSERT TEXT ==="
                                )
                            ]
                        )
                    ],
                    system=None,
                    tools=None,
                    thinking=None,
                    max_tokens=160,
                )
                chunks: list[str] = []
                for event in provider.stream(request):
                    if isinstance(event, TextDelta) and event.text:
                        chunks.append(event.text)
                self._emitter.emit(
                    Result(ok=True, command="inline_complete", detail="".join(chunks)),
                    record=False,
                )
            except Exception as exc:  # noqa: BLE001 —— 补全失败静默降级
                self._emitter.emit(
                    Result(ok=False, command="inline_complete",
                           detail=f"{type(exc).__name__}: {exc}"),
                    record=False,
                )

        threading.Thread(target=run, daemon=True, name="zcode-inline").start()

    def _control_workflow_run(self, msg: Control) -> None:
        """执行工作流：校验 + 起工作流线程（报告经 result 帧异步返回——逐步
        是真实 LLM 子回合，不能占读线程，否则权限卡应答会被饿死）。
        校验与线程启动整体在 _intake_lock 内（闸门原子化，防 cron 注入并发）。"""
        with self._intake_lock:
            if self._turn_alive() or self._workflow_alive():
                self._emit(
                    Error(kind="busy", message="cannot start a workflow while a turn or workflow is running")
                )
                return
            name = (msg.name or "").strip()
            if not name:
                self._emit(Error(kind="protocol", message="workflow_run requires a non-empty 'name'"))
                return
            try:
                self._workflows.load(name)  # 仅校验可加载（报告由工作流线程产出）
            except WorkflowError as exc:
                self._emit(Error(kind="not_found", message=str(exc)))
                return
            if self._provider is None:
                self._emit(
                    Error(
                        kind="config",
                        message=self._provider_error or "no provider configured",
                    )
                )
                return
            self._cancel.clear()  # 陈旧 cancel 不应秒杀工作流（与 user_message 同口径）
            thread = threading.Thread(
                target=self._run_workflow, args=(name,), daemon=True, name="zcode-workflow"
            )
            self._workflow_thread = thread
            thread.start()

    def _run_workflow(self, name: str) -> None:
        """工作流线程：逐步受限子代理（前步结果作下步上下文前缀），完成广播
        报告（result.detail JSON）。ChildRuntime 每次运行即时构造——cwd 跟随
        set_root，不复用主会话 ctx 里绑定了旧 cwd 的实例。"""
        runtime = ChildRuntime(
            registry=self._registry,
            permissions=self._permissions_engine
            or MemoryPermissionEngine(allow_rules=[]),
            cwd=self._workspace_cwd,
            emit=self._emit,
            ask_permission=self._ask_permission,
            review_diff=self._review_diff,
            provider_supplier=lambda: (
                self._provider, self.model_id, self._provider_entry
            ),
            factory=self._factory,
            config_supplier=lambda: self._config or {},
            api_key_env=self._api_key_env,
            main_cancel=self._cancel,
        )
        try:
            report = self._workflows.run(
                name,
                step_runner=lambda prompt, model: runtime.run(
                    {"prompt": prompt, "model": model}
                ),
                cancel_event=self._cancel,
            )
        except Exception as exc:  # noqa: BLE001 —— 报告帧必须发出，否则 UI 永等
            report = {
                "workflow": name,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "total": 0,
                "completed": 0,
                "steps": [],
            }
        self._emit(
            Result(
                ok=bool(report.get("ok")),
                command="workflow_run",
                detail=json.dumps(report, ensure_ascii=False),
            )
        )

    def _cron_inject_prompt(self, text: str) -> None:
        """cron prompt 动作：走与读线程同一条用户消息路径（busy 闸门 / 检查点 /
        转录 / 回合线程全复用）；并发安全由 _on_user_message 的 _intake_lock
        保证（M9 注释见彼处）。"""
        self._on_user_message(UserMessage(text=text))

    def _cron_start_task(self, command: str, name: str | None):
        """cron task 动作：经后台任务执行（bash_backend 唯一执行点）。
        fire 时复检权限（新增时已过门；模式/规则此后可能已变，宁缺勿滥）。"""
        check = self._background_command_check(command)
        if check.outcome != ALLOW or check.danger:
            self._emit(
                Error(
                    kind="permission",
                    message=f"cron task skipped: {command[:80]!r} 不再被允许（{check.reason}）",
                )
            )
            return None
        return self._background.start(command, name=name, cwd=self._workspace_cwd)

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
        if self._turn_alive() or self._workflow_alive():
            self._emit(Error(kind="busy", message="cannot roll back while a turn or workflow is running"))
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
