"""JSONL 协议 v1：双向消息类型 + 帧编解码（docs/PLAN.md §5）。

与 TS 侧（extension/src/protocol.ts，后续里程碑镜像）手写双侧，本模块 +
tests/test_protocol.py 的往返测试负责锁定字段一致性。

约定：
- 字段名与 PLAN §5 完全一致（camelCase）；
- ``encode`` 省略值为 ``None`` 的字段（``False`` 不省略）；
- ``decode`` 忽略未知字段（前向兼容），但校验：type 必须在注册表、必填字段
  必须出现、受限枚举（command / decision / state / phase）取值合法；
- ``MessageContext`` 是唯一嵌套结构，单独编解码；``levels`` / ``lastTurn`` /
  ``hardware`` 等复合载荷按数据驱动 dict 直通（由 telegraph / perf / envprobe
  模块负责其内部结构）。
"""

from __future__ import annotations

import json
from dataclasses import MISSING, dataclass, field, fields
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

# ---------------------------------------------------------------------------
# 受限枚举（PLAN §5）
# ---------------------------------------------------------------------------

CONTROL_COMMANDS = (
    "new_session",
    "resume",
    "cancel",
    "compact",
    "telegraph",
    "switch_model",
    "ping",
    # M4（R16/R28/R12/R11）：分叉 / 收藏固定 / 导出 Markdown / multi-root 切根
    "fork",
    "pin",
    "export",
    # M4 补完：删除会话（整目录 rmtree；sessionId 白名单校验防路径逃逸；
    # 当前活跃会话拒绝——先切走再删；成功后广播 sessions_list）
    "session_delete",
    "set_root",
    # M4：全文搜索（query 承载关键词；结果经 Result.detail JSON 返回）
    "search_sessions",
    # M5（R7/R21）：环境面板「刷新」——重新探测并广播 env_info
    "env_refresh",
    # M7（R26）：Skills 面板——已加载 SKILL.md 列表（经 Result.detail JSON 返回）
    "skills_list",
    # M8：计划模式（webview store.setPermissionMode 已按此命令名下发，
    # 宿主 protocol.ts 枚举镜像暂缺——宽容转发，见 store.ts 注释）
    "set_permission_mode",
    # 同一处理器的 webview 侧短名（store.setPermissionMode 下发）；两个名字都收
    "permission_mode",
    # M8（R3）：MCP 管理面板——server 列表/重连/增删
    "mcp_list",
    "mcp_refresh",
    "mcp_add",
    "mcp_remove",
    # M9（R3）：后台任务面板——启动 / 列表（Result.detail JSON）/ 停止标记
    "background_start",
    "tasks_list",
    "task_stop",
    # M9（R3）：定时任务面板——列表 / 新增 / 删除 / 启停
    "cron_list",
    "cron_add",
    "cron_remove",
    "cron_toggle",
    # M9（R3）：工作流编排——执行（报告经 Result）与列表
    "workflow_run",
    "workflows_list",
    # M9（R13）：内联补全（chat 模型模拟 FIM；宿主 inlineCompletion 发起，
    # detail = 插入文本，超时/失败一律 Result.ok=False 静默降级）
    "inline_complete",
    # R3：工具开关 + 默认权限（tools.json 持久化；always 规则不绕过黑名单）
    "tools_list",
    "tool_toggle",
    "tool_permission",
    # R15/R20：设置页——settings.json 持久层在核侧（agent/zcode_py/
    # settings_store.py）。settings_get 回 Result.detail JSON（settings +
    # homeDir/sessionsDir）；settings_set 收 {key,value} 单键或 {settings}
    # 整体合并（受管键校验见 settings_store.normalize_patch）；clear_sessions
    # 清空 sessions 目录后重建空会话（config 目录不动）
    "settings_get",
    "settings_set",
    "clear_sessions",
    # R17/R22：一键 git 流——git_preview 预览（diff 统计 + 风格建议信息经
    # Result.detail JSON 回 UI 编辑）；git_pr 创建 PR（gh pr create，回
    # git_result 帧）。执行帧是独立的 git_commit（非 control）
    "git_preview",
    "git_pr",
)
PERMISSION_DECISIONS = ("allow", "deny", "always")
TOOL_CALL_STATES = ("started", "update", "finished")
PERF_PHASES = ("streaming", "done")


class ProtocolError(ValueError):
    """帧不合法（未知 type / 缺必填字段 / 枚举非法 / JSON 解析失败）。"""


# ---------------------------------------------------------------------------
# 壳 → 核
# ---------------------------------------------------------------------------


@dataclass
class Init:
    workspaceRoots: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    apiKeyEnv: dict = field(default_factory=dict)


@dataclass
class MessageContext:
    """user_message 附带上文（PLAN §5 user_message.context）。"""

    selection: str | None = None
    fileRefs: list = field(default_factory=list)
    terminalSel: str | None = None
    problems: str | None = None
    """图片附件路径（M5 视觉中继入口；主模型无视觉时经中继转述后退场）"""
    images: list = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {}
        if self.selection is not None:
            out["selection"] = self.selection
        if self.fileRefs:
            out["fileRefs"] = list(self.fileRefs)
        if self.terminalSel is not None:
            out["terminalSel"] = self.terminalSel
        if self.problems is not None:
            out["problems"] = self.problems
        if self.images:
            out["images"] = list(self.images)
        return out

    @classmethod
    def from_dict(cls, payload: dict) -> "MessageContext":
        return cls(
            selection=payload.get("selection"),
            fileRefs=list(payload.get("fileRefs") or []),
            terminalSel=payload.get("terminalSel"),
            problems=payload.get("problems"),
            images=list(payload.get("images") or []),
        )


@dataclass
class UserMessage:
    text: str
    context: MessageContext | None = None


@dataclass
class PermissionResponse:
    id: str
    decision: str  # allow | deny | always


@dataclass
class DiffReview:
    id: str
    accepted: bool


@dataclass
class Control:
    command: str  # CONTROL_COMMANDS 之一
    sessionId: str | None = None
    level: str | None = None
    providerId: str | None = None
    modelId: str | None = None
    # M4 扩展参数：各命令只取自己所需，其余字段忽略（向后兼容靠这一约定）
    atMessageId: str | None = None  # fork：分叉点（入站 user_message 的 messageId；"__last_user_message__" 哨兵 = 最后一条）
    pinned: bool | None = None  # pin：目标态；缺省 = 取反切换
    rootIndex: int | None = None  # set_root：init.workspaceRoots 下标
    rootPath: str | None = None  # set_root：按路径切根（与 rootIndex 二选一）
    query: str | None = None  # search_sessions：搜索关键词
    # M8 扩展参数（同上约定：各命令只取自己所需，其余忽略）
    mode: str | None = None  # set_permission_mode：build | plan | yolo
    name: str | None = None  # mcp_add / mcp_remove：server 名；background_start：任务名；workflow_run：工作流名
    server: dict | None = None  # mcp_add：{command: [argv], env?, cwd?}（与 name 配套）
    # M9 扩展参数（同上约定）
    # background_start：要后台执行的 shell 命令串（协议层的 control.command 已被
    # 控制命令名占用，载荷里的「command」依此命名承载）
    taskCommand: str | None = None
    timeoutSec: float | None = None  # background_start：run_command 超时（缺省 3600）
    taskId: str | None = None  # task_stop：后台任务 id
    schedule: str | None = None  # cron_add：标准 5 段 cron 表达式（*/5 * * * *）
    action: dict | None = None  # cron_add：{type:"prompt",text} | {type:"task",command}
    cronId: str | None = None  # cron_remove / cron_toggle：条目 id
    enabled: bool | None = None  # cron_add 缺省 true；cron_toggle 缺省取反
    prefix: str | None = None  # inline_complete：光标前文
    suffix: str | None = None  # inline_complete：光标后文
    path: str | None = None  # inline_complete：文件路径（提示词语境用）
    # R15/R20 设置页（settings_set）：整体/部分合并通道（受管键见
    # settings_store.MANAGED_KEYS）；与单键 {key, value} 二选一，settings 优先
    settings: dict | None = None
    key: str | None = None  # settings_set：单键名
    value: Any | None = None  # settings_set：单键值（None 编码时省略 → 清空请传 ""）
    # R17/R22 git 流（git_preview / git_pr）：repoRoot 须在 init.workspaceRoots
    # 白名单内（main.py _git_repo_within_roots）；scope 仅 git_preview 用
    # （staged | all）；title/body 仅 git_pr 用（缺省 --fill）
    repoRoot: str | None = None
    scope: str | None = None
    title: str | None = None
    body: str | None = None


@dataclass
class Rollback:
    checkpointId: str


@dataclass
class GitCommit:
    """R17/R22 执行帧：scope=all 时核侧先 ``git add -A``；message 缺省时
    按仓库风格现场生成（正常路径：UI 已在 git_preview 预览里让用户编辑过）。
    回执是 ``git_result``（commitSha/remoteUrl/commitUrl/prUrl/error）。"""

    repoRoot: str
    scope: str  # staged | all
    push: bool = False
    message: str | None = None  # 完整提交信息（subject + 空行 + body）


# ---------------------------------------------------------------------------
# 核 → 壳
# ---------------------------------------------------------------------------


@dataclass
class SessionStarted:
    sessionId: str


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class StreamDelta:
    text: str


@dataclass
class MessageComplete:
    stopReason: str | None = None
    text: str | None = None
    inputTokens: int | None = None
    outputTokens: int | None = None


@dataclass
class ToolCall:
    callId: str
    name: str
    args: dict = field(default_factory=dict)
    state: str = "started"  # started | update | finished
    result: Any = None
    elapsedMs: int | None = None
    warning: str | None = None  # 隐私护栏标记（R15）：read/webfetch 触碰敏感文件模式


@dataclass
class PermissionRequest:
    id: str
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class DiffProposed:
    id: str
    file: str
    oldText: str
    newText: str


@dataclass
class VisionRelay:
    model: str
    preset: str
    description: str


@dataclass
class TelegraphState:
    """车钟状态：levels=[{id,label,param,ok}]（模型档案数据驱动），current=档位 id，
    paramTranslation=当前档位解析出的厂商思考参数（供 UI 直读，如
    ``{"thinking": {"budget_tokens": 24576}}``）。"""

    levels: list = field(default_factory=list)
    current: str = "off"
    paramTranslation: dict = field(default_factory=dict)


@dataclass
class Perf:
    """性能帧。R19 会话统计：model = 产生本帧的模型 id（主循环当前模型，
    loop 侧填充）——UI 按 model 分组聚合 平均/最差/峰值；None（旧帧/无模型
    场景）时 UI 归入「未知」桶。"""

    phase: str  # streaming | done
    ttftMs: float | None = None
    tokPerSec: float | None = None
    lastTurn: dict | None = None  # {"ttftMs": float, "avgTokPerSec": float}
    model: str | None = None  # R19：按模型分组统计的分组键


@dataclass
class EnvInfo:
    hardware: dict = field(default_factory=dict)
    workspaceLangs: list = field(default_factory=list)


@dataclass
class Compaction:
    layer: str
    report: dict = field(default_factory=dict)


@dataclass
class Checkpoint:
    id: str
    sha: str
    atMessageId: str


@dataclass
class RollbackResult:
    ok: bool


@dataclass
class SessionsList:
    """会话列表：sessions 为摘要 dict 数据驱动直通（TS 侧开放接口）。

    M4 起字段：sessionId / title / pinned / lastActivity / messageCount /
    rootPath（另有 providerId / modelId / parentSessionId）。
    """

    sessions: list = field(default_factory=list)


@dataclass
class Usage:
    tokens: int = 0
    byModel: dict = field(default_factory=dict)


@dataclass
class PingResult:
    ok: bool
    latencyMs: int | None = None
    errorKind: str | None = None  # auth | network | model | quota | unknown
    message: str | None = None


@dataclass
class GitResult:
    """R17/R22 回执帧：commit 主体失败（add/commit）→ ok 语义由 error 承载
    （commitSha 为空）；push 失败时 commitSha 已有值、error 记推送原因。
    prUrl 仅 git_pr 命令填（git_commit 不自动建 PR）。"""

    commitSha: str | None = None
    remoteUrl: str | None = None
    commitUrl: str | None = None
    prUrl: str | None = None
    error: str | None = None
    hint: str | None = None  # 降级/后续动作提示（无 remote、gh 缺失…）


@dataclass
class ImageArtifact:
    path: str


@dataclass
class TaskStarted:
    """M9（R3）后台任务启动。name 缺省取命令首词（任务面板显示名）。"""

    taskId: str
    name: str
    command: str


@dataclass
class TaskOutput:
    """后台任务输出尾部（≤8KB 环形）。监测线程在尾部变化时至多每 2s 广播
    一次；命令结束时（run_command 返回）必发一次最终尾部——当前 bash_backend
    是整段阻塞返回，实际中间帧在有流式后端前不会出现，机制先行。"""

    taskId: str
    tail: str


@dataclass
class TaskFinished:
    """后台任务收尾（终态 finished | failed | cancelled 须经 tasks_list 查询；
    cancelled = task_stop 已标记且命令已返回，见 background.py docstring）。"""

    taskId: str
    exitCode: int


@dataclass
class Error:
    kind: str
    message: str


@dataclass
class Result:
    """control 命令的通用回执（ok / 命令名 / 附注）。"""

    ok: bool
    command: str | None = None
    detail: str | None = None
    # export：导出的 Markdown 全文（保存落盘由 UI 决定）；其余命令恒 None
    markdown: str | None = None


# ---------------------------------------------------------------------------
# 注册表与编解码
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type] = {}
_NAMES: dict[type, str] = {}

_ENUMS: dict[str, dict[str, tuple[str, ...]]] = {
    "control": {"command": CONTROL_COMMANDS},
    "permission_response": {"decision": PERMISSION_DECISIONS},
    "tool_call": {"state": TOOL_CALL_STATES},
    "perf": {"phase": PERF_PHASES},
}


def _register(name: str):
    def deco(cls: type) -> type:
        _REGISTRY[name] = cls
        _NAMES[cls] = name
        return cls

    return deco


_register("init")(Init)
_register("user_message")(UserMessage)
_register("permission_response")(PermissionResponse)
_register("diff_review")(DiffReview)
_register("control")(Control)
_register("rollback")(Rollback)
_register("git_commit")(GitCommit)
_register("session_started")(SessionStarted)
_register("thinking_delta")(ThinkingDelta)
_register("stream_delta")(StreamDelta)
_register("message_complete")(MessageComplete)
_register("tool_call")(ToolCall)
_register("permission_request")(PermissionRequest)
_register("diff_proposed")(DiffProposed)
_register("vision_relay")(VisionRelay)
_register("telegraph_state")(TelegraphState)
_register("perf")(Perf)
_register("env_info")(EnvInfo)
_register("compaction")(Compaction)
_register("checkpoint")(Checkpoint)
_register("rollback_result")(RollbackResult)
_register("sessions_list")(SessionsList)
_register("usage")(Usage)
_register("ping_result")(PingResult)
_register("git_result")(GitResult)
_register("image_artifact")(ImageArtifact)
_register("task_started")(TaskStarted)
_register("task_output")(TaskOutput)
_register("task_finished")(TaskFinished)
_register("error")(Error)
_register("result")(Result)

SHELL_TO_CORE = (
    Init,
    UserMessage,
    PermissionResponse,
    DiffReview,
    Control,
    Rollback,
    GitCommit,
)
CORE_TO_SHELL = tuple(
    cls
    for cls in _NAMES
    if cls not in SHELL_TO_CORE and cls is not MessageContext
)

ShellToCoreMessage = Union[Init, UserMessage, PermissionResponse, DiffReview, Control, Rollback, GitCommit]  # noqa: UP007
CoreToShellMessage = Union[  # noqa: UP007
    SessionStarted,
    ThinkingDelta,
    StreamDelta,
    MessageComplete,
    ToolCall,
    PermissionRequest,
    DiffProposed,
    VisionRelay,
    TelegraphState,
    Perf,
    EnvInfo,
    Compaction,
    Checkpoint,
    RollbackResult,
    SessionsList,
    Usage,
    PingResult,
    GitResult,
    ImageArtifact,
    TaskStarted,
    TaskOutput,
    TaskFinished,
    Error,
    Result,
]


def encode_message(msg: Any) -> dict:
    """消息实例 → 协议 dict（None 字段省略）。"""
    name = _NAMES.get(type(msg))
    if name is None:
        raise ProtocolError(f"unregistered message type: {type(msg).__name__}")
    out: dict = {"type": name}
    for f in fields(msg):
        value = getattr(msg, f.name)
        if value is None:
            continue
        if isinstance(value, MessageContext):
            value = value.to_dict()
        out[f.name] = value
    return out


def encode_line(msg: Any) -> str:
    return json.dumps(encode_message(msg), ensure_ascii=False)


def _coerce(type_name: str, field_name: str, value: Any, annotation: Any) -> Any:
    if annotation is Any or annotation is object or annotation is None:
        return value
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType or annotation is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        for arg in args:
            try:
                return _coerce(type_name, field_name, value, arg)
            except ProtocolError:
                continue
        raise ProtocolError(
            f"{type_name}: field {field_name!r} has invalid type "
            f"{type(value).__name__}"
        )
    if annotation is str:
        if not isinstance(value, str):
            raise ProtocolError(f"{type_name}: field {field_name!r} must be a string")
        return value
    if annotation is bool:
        if not isinstance(value, bool):
            raise ProtocolError(f"{type_name}: field {field_name!r} must be a boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError(f"{type_name}: field {field_name!r} must be an integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(f"{type_name}: field {field_name!r} must be a number")
        return value
    if annotation is list or origin is list:
        if not isinstance(value, list):
            raise ProtocolError(f"{type_name}: field {field_name!r} must be an array")
        return value
    if annotation is dict or origin is dict:
        if not isinstance(value, dict):
            raise ProtocolError(f"{type_name}: field {field_name!r} must be an object")
        return value
    if annotation is MessageContext:
        if not isinstance(value, dict):
            raise ProtocolError(
                f"{type_name}: field {field_name!r} must be an object (context)"
            )
        return MessageContext.from_dict(value)
    # 未知注解类型：直通（前向兼容）。
    return value


def decode_message(payload: Any) -> Any:
    """协议 dict → 消息实例。未知字段忽略；必填缺失 / 枚举非法抛 ProtocolError。"""
    if not isinstance(payload, dict):
        raise ProtocolError("frame must be a JSON object")
    type_name = payload.get("type")
    if not isinstance(type_name, str):
        raise ProtocolError("frame missing 'type' string field")
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise ProtocolError(f"unknown message type: {type_name!r}")
    hints = get_type_hints(cls)
    kwargs: dict = {}
    for f in fields(cls):
        present = f.name in payload
        if not present:
            if f.default is MISSING and f.default_factory is MISSING:
                raise ProtocolError(
                    f"{type_name}: missing required field {f.name!r}"
                )
            continue
        kwargs[f.name] = _coerce(type_name, f.name, payload[f.name], hints.get(f.name))
    for field_name, allowed in _ENUMS.get(type_name, {}).items():
        value = kwargs.get(field_name)
        if value is not None and value not in allowed:
            raise ProtocolError(
                f"{type_name}: field {field_name!r} must be one of {list(allowed)}, "
                f"got {value!r}"
            )
    return cls(**kwargs)


def decode_line(line: str) -> Any:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    return decode_message(payload)
