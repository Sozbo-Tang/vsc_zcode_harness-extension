/**
 * Zcode-Py stdio JSONL 协议 v1 —— TypeScript 镜像
 *
 * 依据 docs/PLAN.md 第 5 节 + Python 侧 agent/zcode_py/protocol.py
 * （真源）逐字段对齐：消息信封 = 单个 JSON 对象 + `type` 判别字段，
 * 每行一帧；Python 侧 encode 省略 None 字段 ↔ TS 侧 JSON.stringify
 * 天然省略 undefined；Python 侧 decode 忽略未知字段（前向兼容）。
 * 已按 protocol.py 对齐的关键差异：control.command（含 "ping"）、
 * message_complete 带 stopReason/text/inputTokens/outputTokens、
 * telegraph_state.levels[]{id,label,param,ok} 且 paramTranslation 为 dict、
 * compaction.layer 为 string 且 report 为 dict、ping_result.message、
 * result{ok,command,detail}。
 *
 * 解析策略：只严格校验信封（合法 JSON + string type），载荷字段宽容、
 * 未知类型也放行转发（见 parseCoreMessage）——Python 侧 decode 对
 * 未知 type 抛 ProtocolError 是它作为接收端的严格，本侧作为接收端
 * 选择前向容忍，避免 Python 先行演进时把 UI 打死。
 */

/* ─────────────────────────── 壳 → 核（Host → Core） ─────────────────────────── */

/** init{workspaceRoots, settings, apiKeyEnv} */
export interface InitMessage {
  type: "init";
  /** 多根工作区根目录（扩展宿主侧真实路径，远程下即远程侧路径） */
  workspaceRoots: string[];
  /** 预留：VS Code 侧设置快照（M1 为空对象，Python 侧自管 ~/.zcode-py 配置） */
  settings: Record<string, unknown>;
  /**
   * providerId → 存放该 Provider API Key 的环境变量名。
   * 只传变量名、绝不传 Key 值——Key 值仅在 spawn 时注入子进程环境。
   */
  apiKeyEnv: Record<string, string>;
}

/** user_message.context：编辑器联动上下文（R10，M3 起填充，字段全部可选） */
export interface UserMessageContext {
  selection?: string;
  fileRefs?: string[];
  terminalSel?: string;
  problems?: string;
}

/** user_message{text, context{...}} */
export interface UserMessageMessage {
  type: "user_message";
  text: string;
  context?: UserMessageContext;
}

/**
 * permission_response{id, decision: allow|deny|always}
 * 「⚙写规则」扩展：decision='always' 时可附 rule 文本（M2 权限规则引擎
 * 消费：持久化到 ~/.zcode-py/config 权限规则，PLAN §3.2/§6.3）。当前
 * Python 侧 PermissionResponse 尚无该字段，但 decode 忽略未知字段
 * （agent/zcode_py/protocol.py decode_message）→ 前向兼容：规则暂不
 * 持久化，decision 语义照常生效。
 */
export type PermissionDecision = "allow" | "deny" | "always";
export interface PermissionResponseMessage {
  type: "permission_response";
  id: string;
  decision: PermissionDecision;
  /** 写规则按钮提交的规则文本（自由文本，语法 M2 随规则引擎定稿） */
  rule?: string;
}

/** diff_review{id, accepted} */
export interface DiffReviewMessage {
  type: "diff_review";
  id: string;
  accepted: boolean;
}

/**
 * control{new_session | resume<id> | cancel | compact | telegraph<level> |
 * switch_model{providerId, modelId}} —— Python 侧（protocol.py）以 `command`
 * 判别 + 可选参数承载，枚举 CONTROL_COMMANDS 额外含 "ping"（测试连接）。
 */
export type ControlCommand =
  | "new_session"
  | "resume"
  | "cancel"
  | "compact"
  | "telegraph"
  | "switch_model"
  | "ping";
export interface ControlMessage {
  type: "control";
  command: ControlCommand;
  /** command = "resume" */
  sessionId?: string;
  /** command = "telegraph"：档位 id（telegraph_state.levels[].id） */
  level?: string;
  /** command = "switch_model" */
  providerId?: string;
  /** command = "switch_model" */
  modelId?: string;
}

/** rollback{checkpointId} */
export interface RollbackMessage {
  type: "rollback";
  checkpointId: string;
}

/** git_commit{repoRoot, scope: staged|all, push: bool} */
export type GitCommitScope = "staged" | "all";
export interface GitCommitMessage {
  type: "git_commit";
  repoRoot: string;
  scope: GitCommitScope;
  push: boolean;
}

export type HostToCoreMessage =
  | InitMessage
  | UserMessageMessage
  | PermissionResponseMessage
  | DiffReviewMessage
  | ControlMessage
  | RollbackMessage
  | GitCommitMessage;

/* ─────────────────────────── 核 → 壳（Core → Host） ─────────────────────────── */

/** session_started{sessionId} */
export interface SessionStartedMessage {
  type: "session_started";
  sessionId: string;
}

/** thinking_delta{text} */
export interface ThinkingDeltaMessage {
  type: "thinking_delta";
  text: string;
}

/** stream_delta{text} */
export interface StreamDeltaMessage {
  type: "stream_delta";
  text: string;
}

/** message_complete{stopReason?, text?, inputTokens?, outputTokens?}（Python 侧扩展） */
export interface MessageCompleteMessage {
  type: "message_complete";
  stopReason?: string;
  text?: string;
  inputTokens?: number;
  outputTokens?: number;
}

export type ToolCallState = "started" | "update" | "finished";
/** tool_call{callId, name, args, state, result?, elapsedMs?} */
export interface ToolCallMessage {
  type: "tool_call";
  callId: string;
  name: string;
  /** 工具入参（Python 侧 dict；None 编码时省略 → 可能缺省，默认 {}） */
  args: Record<string, unknown>;
  state: ToolCallState;
  result?: unknown;
  elapsedMs?: number;
  /** 隐私护栏标记（R15）：read/webfetch/grep 命中敏感模式时随帧广播，UI 需醒目展示 */
  warning?: string;
}

/** permission_request{id, tool, args} */
export interface PermissionRequestMessage {
  type: "permission_request";
  id: string;
  tool: string;
  args: Record<string, unknown>;
}

/** diff_proposed{id, file, oldText, newText} */
export interface DiffProposedMessage {
  type: "diff_proposed";
  id: string;
  file: string;
  oldText: string;
  newText: string;
}

/** vision_relay{model, preset, description} */
export interface VisionRelayMessage {
  type: "vision_relay";
  model: string;
  preset: string;
  description: string;
}

/** 模型档案 telegraphLevels：Python 侧数据驱动 dict 直通，建议含 {id,label,param,ok} */
export interface TelegraphLevel {
  /** 档位 id（如 "off"/"low"/"high"；telegraph_state.current 与 control.level 引用它） */
  id: string;
  label: string;
  /** 档位映射到该协议的思考参数（任意 JSON：budget 数值 / effort 字符串 / 开关对象） */
  param: unknown;
  /** false = 该模型不支持此档位（UI 置灰） */
  ok: boolean;
  [key: string]: unknown;
}

/** telegraph_state{levels[], current, paramTranslation} */
export interface TelegraphStateMessage {
  type: "telegraph_state";
  levels: TelegraphLevel[];
  /** 当前档位 id */
  current: string;
  /** 当前档位解析出的厂商思考参数（dict，如 {"thinking":{"budget_tokens":24576}}） */
  paramTranslation: Record<string, unknown>;
}

export interface PerfLastTurn {
  ttftMs: number;
  avgTokPerSec: number;
}

/** perf{phase: streaming|done, ttftMs?, tokPerSec?, lastTurn{...}} */
export interface PerfMessage {
  type: "perf";
  phase: "streaming" | "done";
  ttftMs?: number;
  tokPerSec?: number;
  lastTurn?: PerfLastTurn;
}

/**
 * env_info{hardware, workspaceLangs[]}
 * 内层字段 PLAN §5 未锁定，按 R7/R21 语义给出建议字段且全部可选 +
 * 索引签名宽容（Python 侧定稿后收紧）。
 */
export interface HardwareInfo {
  cpu?: string;
  gpu?: string;
  ram?: string;
  os?: string;
  arch?: string;
  [key: string]: unknown;
}
export interface WorkspaceLangInfo {
  /** 语言名，如 "Python" */
  language?: string;
  /** 工具链是否就绪 */
  ok?: boolean;
  /** 明细/缺失提示 */
  detail?: string;
  [key: string]: unknown;
}
export interface EnvInfoMessage {
  type: "env_info";
  hardware: HardwareInfo;
  workspaceLangs: WorkspaceLangInfo[];
}

/** compaction{layer, report} —— Python 侧 layer 为 string（如 "L2"），report 为 dict 直通 */
export interface CompactionMessage {
  type: "compaction";
  layer: string;
  report: Record<string, unknown>;
}

/** checkpoint{id, sha, atMessageId} */
export interface CheckpointMessage {
  type: "checkpoint";
  id: string;
  sha: string;
  atMessageId: string;
}

/** rollback_result{ok} */
export interface RollbackResultMessage {
  type: "rollback_result";
  ok: boolean;
}

/**
 * sessions_list —— Python 侧 sessions 为数据驱动 list 直通，
 * 此处给建议字段（全部可选 + 索引签名）。
 */
export interface SessionSummary {
  sessionId?: string;
  title?: string;
  updatedAt?: string;
  [key: string]: unknown;
}
export interface SessionsListMessage {
  type: "sessions_list";
  sessions: SessionSummary[];
}

/** usage{tokens, byModel} */
export interface UsageMessage {
  type: "usage";
  tokens: number;
  /** 模型名 → token 数 */
  byModel: Record<string, number>;
}

/** ping_result{ok, latencyMs?, errorKind?, message?} —— 错误分类与 Python 侧 ping 一致 */
export type PingErrorKind = "auth" | "network" | "model" | "quota" | "unknown";
export interface PingResultMessage {
  type: "ping_result";
  ok: boolean;
  latencyMs?: number;
  errorKind?: PingErrorKind;
  /** 失败时的可读补充（Python 侧扩展字段） */
  message?: string;
}

/** git_result{commitSha?, remoteUrl?, commitUrl?, prUrl?, error?} */
export interface GitResultMessage {
  type: "git_result";
  commitSha?: string;
  remoteUrl?: string;
  commitUrl?: string;
  prUrl?: string;
  error?: string;
}

/** image_artifact{path} */
export interface ImageArtifactMessage {
  type: "image_artifact";
  /** 扩展宿主侧的本地绝对路径（远程下即远程侧路径） */
  path: string;
}

/** error{kind, message} —— kind 为稳定错误码（Python 侧定义） */
export interface ErrorMessage {
  type: "error";
  kind: string;
  message: string;
}

/** result{ok, command?, detail?} —— control 命令的通用回执（Python 侧语义） */
export interface ResultMessage {
  type: "result";
  ok: boolean;
  /** 回执对应的 control.command */
  command?: string;
  detail?: string;
}

/** M0 冒烟遗留的 echo 帧（agent/zcode_py/__main__.py），保留以兼容过渡期 */
export interface EchoMessage {
  type: "echo";
  line: string;
}

export type CoreToHostMessage =
  | SessionStartedMessage
  | ThinkingDeltaMessage
  | StreamDeltaMessage
  | MessageCompleteMessage
  | ToolCallMessage
  | PermissionRequestMessage
  | DiffProposedMessage
  | VisionRelayMessage
  | TelegraphStateMessage
  | PerfMessage
  | EnvInfoMessage
  | CompactionMessage
  | CheckpointMessage
  | RollbackResultMessage
  | SessionsListMessage
  | UsageMessage
  | PingResultMessage
  | GitResultMessage
  | ImageArtifactMessage
  | ErrorMessage
  | ResultMessage
  | EchoMessage;

/* ─────────────────────── JSONL 帧编解码 ─────────────────────── */

export const HOST_TO_CORE_TYPES: readonly string[] = [
  "init",
  "user_message",
  "permission_response",
  "diff_review",
  "control",
  "rollback",
  "git_commit",
];

export const CORE_TO_HOST_TYPES: readonly string[] = [
  "session_started",
  "thinking_delta",
  "stream_delta",
  "message_complete",
  "tool_call",
  "permission_request",
  "diff_proposed",
  "vision_relay",
  "telegraph_state",
  "perf",
  "env_info",
  "compaction",
  "checkpoint",
  "rollback_result",
  "sessions_list",
  "usage",
  "ping_result",
  "git_result",
  "image_artifact",
  "error",
  "result",
  "echo",
];

const CORE_TYPE_SET: ReadonlySet<string> = new Set(CORE_TO_HOST_TYPES);
const HOST_TYPE_SET: ReadonlySet<string> = new Set(HOST_TO_CORE_TYPES);

/** 编码为一帧（单行 JSON + 换行）。绝不把 API Key 值放进消息体。 */
export function encodeJsonlFrame(message: HostToCoreMessage | CoreToHostMessage): string {
  return `${JSON.stringify(message)}\n`;
}

export type ParsedCoreFrame =
  | { ok: true; message: CoreToHostMessage; knownType: boolean }
  | { ok: false; error: string; raw: string };

/**
 * 解析核 → 壳的一行。跨 chunk 缓冲由调用方（readline）负责；
 * 单行解析失败只降级本帧（返回 ok:false），绝不抛异常打死监听器。
 * 未知 type 的合法 JSON 也放行（knownType:false），UI 前向兼容。
 */
export function parseCoreMessage(line: string): ParsedCoreFrame {
  const raw = line.trim();
  if (!raw) {
    return { ok: false, error: "empty_line", raw };
  }
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch (err) {
    return { ok: false, error: `invalid_json: ${String(err)}`, raw };
  }
  if (typeof value !== "object" || value === null || typeof (value as { type?: unknown }).type !== "string") {
    return { ok: false, error: "missing_type_field", raw };
  }
  const type = (value as { type: string }).type;
  if (CORE_TYPE_SET.has(type)) {
    return { ok: true, message: value as CoreToHostMessage, knownType: true };
  }
  return { ok: true, message: value as unknown as CoreToHostMessage, knownType: false };
}

/** 宽容判别：用于把宿主侧对象当作壳 → 核消息发送前的最小校验 */
export function looksLikeHostToCoreMessage(value: unknown): value is HostToCoreMessage {
  return (
    typeof value === "object" &&
    value !== null &&
    HOST_TYPE_SET.has(typeof (value as { type?: unknown }).type === "string" ? (value as { type: string }).type : "")
  );
}

/* ──────────── API Key 环境变量约定（init.apiKeyEnv 语义 + spawn 注入） ──────────── */

/** providerId → 环境变量名 / 环境变量名 → Key 值 的解析结果 */
export interface ApiKeyEnvResolution {
  /** init 消息用：providerId → 环境变量名（不含值） */
  mapping: Record<string, string>;
  /** spawn 注入用：环境变量名 → Key 值（值绝不入日志/转录/消息） */
  values: Record<string, string>;
}

/* ──────────── Webview ↔ 扩展宿主 postMessage 桥（非 stdio 协议） ──────────── */
/**
 * 与 stdio 协议分属两层：webview 与宿主之间的 RPC/事件信封。
 * 定义在此使 webview 侧（useHostRpc）与宿主共用同一契约。
 */

/** webview 加载完成后上报，宿主随后重放缓冲的全部事件（不 retainContext 的代价补偿） */
export interface WebviewReadyMessage {
  kind: "ready";
}

/** webview 请求宿主转发一条消息给 Python 核心（sessionKey 缺省 = 活跃会话） */
export interface WebviewCoreSendMessage {
  kind: "core";
  sessionKey?: string;
  message: HostToCoreMessage;
}

/** 请求-响应 RPC（correlation id 由 webview 生成） */
export interface WebviewRpcRequest {
  kind: "rpc";
  id: string;
  method: string;
  params?: unknown;
}

/* ──────────── 编辑器桥 RPC 方法契约（webview → 宿主，editorBridge 实现） ──────────── */
/**
 * 消息约定（两端镜像：webview/src/protocol.ts EditorRpcMethod）：
 *  - "editor.openDiff"        DiffReviewCard「打开对比页」→ 宿主已注册
 *    （extension.ts → editorBridge.openDiffReview），vscode.diff 打开审阅页
 *  - "editor.followToolCall"  ToolCallCard 点击编辑跟随（R10，M3 编辑器桥
 *    TODO）：宿主暂未注册 → webview 收 unknown_method 应答并静默忽略（占位），
 *    M3 在 extension.ts registerRpcHandler 落地同名方法即可生效
 */
export const RPC_EDITOR_OPEN_DIFF = "editor.openDiff";
export interface EditorOpenDiffParams {
  /** diff_proposed.id（同一次提案可重复打开） */
  id: string;
  /** 目标文件路径（绝对或相对工作区根，multi-root 逐根探测） */
  file: string;
  oldText: string;
  newText: string;
}

export const RPC_EDITOR_FOLLOW_TOOL_CALL = "editor.followToolCall";
export interface EditorFollowToolCallParams {
  /** tool_call.callId */
  callId: string;
  /** 工具名（read/edit/bash…，宿主按工具类型解析跳转目标） */
  name: string;
  /** 工具入参原样直通（宿主从中取 file_path/path/行号） */
  args: Record<string, unknown>;
}

export type WebviewToHostMessage = WebviewReadyMessage | WebviewCoreSendMessage | WebviewRpcRequest;

export interface WebviewCoreForwardMessage {
  kind: "core";
  sessionKey: string;
  message: CoreToHostMessage;
}

/** 宿主侧错误（进程崩溃/spawn 失败等，区别于核心发来的 error 事件） */
export interface WebviewHostErrorMessage {
  kind: "host_error";
  sessionKey?: string;
  errorKind: string;
  message: string;
}

export interface WebviewProcessStateMessage {
  kind: "process_state";
  sessionKey: string;
  state: string;
  detail?: string;
}

export interface WebviewRpcResponseMessage {
  kind: "rpc_result";
  id: string;
  ok: boolean;
  result?: unknown;
  error?: string;
}

/** 可进重放缓冲的消息（rpc_result 是即时应答、replay 自身不重放） */
export type ReplayableWebviewMessage =
  | WebviewCoreForwardMessage
  | WebviewHostErrorMessage
  | WebviewProcessStateMessage;

/** 视图重 resolve 后的全量重放（bounded，见 webviewHost） */
export interface WebviewReplayMessage {
  kind: "replay";
  messages: ReplayableWebviewMessage[];
}

export type HostToWebviewMessage =
  | WebviewCoreForwardMessage
  | WebviewHostErrorMessage
  | WebviewProcessStateMessage
  | WebviewRpcResponseMessage
  | WebviewReplayMessage;
