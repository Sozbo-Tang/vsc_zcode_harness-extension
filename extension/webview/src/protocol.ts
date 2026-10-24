/**
 * Webview 侧协议类型 —— 镜像 extension/src/protocol.ts（宿主契约真相源）。
 *
 * 注：本文件初版为按 docs/PLAN.md §5 写的临时镜像（当时 extension/src/
 * protocol.ts 尚不存在）；宿主协议落地后已对齐其字段：
 *  - 事件联合 CoreEvent ↔ 宿主 CoreToHostMessage（webview 消费端）
 *  - CoreSend ↔ 宿主 HostToCoreMessage 的 webview 发送子集
 *  - 车钟档位带 id，current 引用 id；paramTranslation 为 dict
 *
 * 未对齐部分（已在注释标出，待宿主 M1 后续补齐）：
 *  - providers.* / provider.* RPC 方法：宿主 rpcHandlers 目前仅注册了
 *    echo / getState（extension/src/extension.ts L120-128），Provider CRUD
 *    的方法名是 webview 侧提案，宿主实现后如有出入以宿主为准
 *  - control command 'set_permission_mode'：宿主 ControlCommand 枚举暂缺
 */

/* ---------- 基础类型（镜像 extension/src/protocol.ts） ---------- */

/** 车钟挡位（模型档案声明，UI 只消费不计算，PLAN §3.3；镜像 TelegraphLevel） */
export interface TelegraphLevel {
  /** 挡位 id（如 "off"/"high"；telegraph_state.current 与 control.level 引用它） */
  id: string;
  label: string;
  /** 挡位映射的厂商思考参数（任意 JSON：budget 数值 / effort 字符串 / 开关对象） */
  param?: unknown;
  /** false = 该模型不支持此挡位 → 盘面置灰且不可吸附 */
  ok: boolean;
}

/** 权限模式（ZCode CollaborationMode 子集；宿主 ControlCommand 暂缺 set_permission_mode） */
export type PermissionMode = 'build' | 'plan' | 'yolo';

export interface HardwareInfo {
  cpu?: string;
  gpu?: string;
  ram?: string;
  os?: string;
  arch?: string;
}

export interface WorkspaceLang {
  language?: string;
  ok?: boolean;
  detail?: string;
}

/** 镜像宿主 SessionSummary：{sessionId, title?, updatedAt?（ISO 字符串）} */
export interface SessionSummary {
  sessionId: string;
  title?: string;
  updatedAt?: string;
}

export interface PerfTurn {
  ttftMs: number;
  avgTokPerSec: number;
}

/* ---------- 核 → 壳事件（镜像 CoreToHostMessage，webview 消费端） ---------- */

export type CoreEvent =
  | { type: 'session_started'; sessionId: string }
  | { type: 'thinking_delta'; text: string }
  | { type: 'stream_delta'; text: string }
  | {
      type: 'message_complete';
      stopReason?: string;
      text?: string;
      inputTokens?: number;
      outputTokens?: number;
    }
  | {
      type: 'tool_call';
      callId: string;
      name: string;
      args: Record<string, unknown>;
      state: 'started' | 'update' | 'finished';
      result?: unknown;
      elapsedMs?: number;
      /** 隐私护栏标记（R15），命中敏感文件/URL 模式时由 Python 侧随帧携带 */
      warning?: string;
    }
  | { type: 'permission_request'; id: string; tool: string; args: Record<string, unknown> }
  | { type: 'diff_proposed'; id: string; file: string; oldText: string; newText: string }
  | { type: 'vision_relay'; model: string; preset: string; description: string }
  | {
      type: 'telegraph_state';
      levels: TelegraphLevel[];
      /** 当前挡位 id */
      current: string;
      paramTranslation?: Record<string, unknown>;
    }
  | {
      type: 'perf';
      phase: 'streaming' | 'done';
      ttftMs?: number;
      tokPerSec?: number;
      lastTurn?: PerfTurn;
    }
  | { type: 'env_info'; hardware: HardwareInfo; workspaceLangs: WorkspaceLang[] }
  | { type: 'compaction'; layer: 1 | 2 | 3 | 4; report: string }
  | { type: 'checkpoint'; id: string; sha: string; atMessageId: string }
  | { type: 'rollback_result'; ok: boolean }
  | { type: 'sessions_list'; sessions: SessionSummary[] }
  | { type: 'usage'; tokens: number; byModel?: Record<string, number> }
  | {
      type: 'ping_result';
      providerId?: string; // 宿主协议未带 providerId；rpc 场景由 correlation id 关联
      ok: boolean;
      latencyMs?: number;
      errorKind?: string;
    }
  | {
      type: 'git_result';
      commitSha?: string;
      remoteUrl?: string;
      commitUrl?: string;
      prUrl?: string;
      error?: string;
    }
  | { type: 'image_artifact'; path: string }
  | { type: 'error'; kind: string; message: string }
  | { type: 'result' }
  | { type: 'echo'; line: string };

/* ---------- 壳 → 核（镜像 HostToCoreMessage 的 webview 发送子集） ---------- */

export interface UserMessageContext {
  selection?: string;
  fileRefs?: string[];
  terminalSel?: string;
  problems?: string;
  /** 图片附件路径（M5 视觉中继入口） */
  images?: string[];
}

export type PermissionDecision = 'allow' | 'deny' | 'always';

export type CoreSend =
  | { type: 'user_message'; text: string; context?: UserMessageContext }
  | {
      type: 'permission_response';
      id: string;
      decision: PermissionDecision;
      /**
       * 「⚙写规则」提交的规则文本（decision='always' 时附带）。镜像宿主
       * PermissionResponseMessage.rule：M2 权限规则引擎消费并持久化
       * （PLAN §3.2）；当前 Python 侧 decode 忽略未知字段 → 规则暂只
       * 影响本次会话（同 always），不报错。
       */
      rule?: string;
    }
  | { type: 'diff_review'; id: string; accepted: boolean }
  | {
      type: 'control';
      command:
        | 'new_session'
        | 'resume'
        | 'cancel'
        | 'compact'
        | 'telegraph'
        | 'switch_model'
        | 'set_permission_mode'; // 宿主 ControlCommand 枚举暂缺，提案项
      sessionId?: string;
      level?: string;
      providerId?: string;
      modelId?: string;
      mode?: PermissionMode;
    };

/* ---------- webview ↔ 宿主 RPC ---------- */

/** 宿主已注册（extension/src/extension.ts）：echo / getState / editor.* */
export type RegisteredRpcMethod =
  | 'echo'
  | 'getState'
  | 'editor.openDiff'
  | 'editor.followToolCall'
  | 'editor.getSelection'
  | 'editor.listFiles'
  | 'editor.pickFile';

/** 编辑器桥 RPC 契约（镜像宿主 protocol.ts RPC_EDITOR_* 常量与注释） */
export interface EditorOpenDiffParams {
  /** diff_proposed.id（同一次提案可重复打开） */
  id: string;
  /** 目标文件路径（绝对或相对工作区根，宿主 multi-root 逐根探测） */
  file: string;
  oldText: string;
  newText: string;
}

export interface EditorFollowToolCallParams {
  /** tool_call.callId */
  callId: string;
  /** 工具名（read/edit/bash…，宿主按工具类型解析跳转目标） */
  name: string;
  /** 工具入参原样直通（宿主从中取 file_path/path/行号） */
  args: Record<string, unknown>;
  /** 工具结果原样直通（grep 的行号在结果里） */
  result?: unknown;
}

/** M3 选区联动（editor.getSelection 结果；无编辑器/无选中时 null） */
export interface EditorSelectionResult {
  file: string;
  startLine: number;
  endLine: number;
  text: string;
}

/** M3 @补全（editor.listFiles {query} → {files}，最多 20 条） */
export interface EditorListFilesResult {
  files: string[];
}

/** M3 文件挑选（editor.pickFile → {file}；用户取消 null） */
export interface EditorPickFileResult {
  file: string;
}

export type EditorRpcMethod =
  | 'editor.openDiff'
  | 'editor.followToolCall'
  | 'editor.getSelection'
  | 'editor.listFiles'
  | 'editor.pickFile';

export interface GetStateResult {
  activeSessionKey?: string;
  sessions: Array<{ sessionKey: string; state: string }>;
  workspaceRoots: string[];
}

/** Provider CRUD RPC：webview 侧提案方法名（宿主 M1 后续注册后即通） */
export type ProviderRpcMethod =
  | 'providers.save'
  | 'providers.delete'
  | 'providers.set_default'
  | 'provider.ping'
  | 'provider.list_models';

export type HostRpcMethod = RegisteredRpcMethod | ProviderRpcMethod | EditorRpcMethod;

export type ProviderProtocol =
  | 'anthropic'
  | 'openai'
  | 'gemini'
  | 'ollama'
  | 'openrouter'
  | 'custom';

export interface ModelCapabilities {
  thinking?: boolean;
  vision?: boolean;
  tools?: boolean;
}

export interface ModelInfo {
  id: string;
  contextLength?: number;
  capabilities?: ModelCapabilities;
  telegraphLevels?: TelegraphLevel[];
}

/** Provider 配置（providers.json；不含 Key —— Key 只进 SecretStorage） */
export interface ProviderConfig {
  id: string;
  name: string;
  protocol: ProviderProtocol;
  baseURL: string;
  isDefault: boolean;
  models: ModelInfo[];
  selectedModel?: string;
  visionRelayModel?: string;
  compactionModel?: string;
  /** Key 只存 SecretStorage，webview 仅拿到存在性/尾号用于掩码显示 */
  hasKey?: boolean;
  apiKeyLast4?: string;
}

export interface PingResult {
  ok: boolean;
  latencyMs?: number;
  errorKind?: string;
  message?: string;
}

export interface ListModelsResult {
  models: ModelInfo[];
}

export interface RpcErrorInfo {
  code: string;
  message: string;
}

/** 后台任务（M9 完整落地，tasks 切片先建） */
export interface BackgroundTask {
  id: string;
  name: string;
  status: 'running' | 'done' | 'failed';
  detail?: string;
}

export interface TodoItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
}
