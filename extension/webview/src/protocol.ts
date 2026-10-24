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
 *  - control command 'permission_mode'（M8，旧名 set_permission_mode 已废弃）：
 *    宿主 ControlCommand 枚举暂缺；Python 侧并行实现（见 M8 条目）
 *  - M4/M5：editor.pickImage / workspace.saveExport / session.activate RPC
 *    为 webview 侧按方法名先行调用（宿主侧并行实现中，参数提案见下方接口）
 *  - control 新命令 fork/pin/export/set_root/env_refresh 已在 Python 侧
 *    CONTROL_COMMANDS 定稿；宿主镜像枚举暂缺（宽容转发不影响）
 *  - M8：compaction{layer:string, report:dict} 对齐宿主镜像（Python 侧
 *    layer 为 "L2"/"L3"… 字符串，report 为 dict 直通）；control 命令
 *    permission_mode / mcp_list / mcp_refresh / mcp_add / mcp_remove 为
 *    Python 侧并行定稿项（约定见 docs/MCP_BACKEND_SPEC.md），宿主镜像
 *    枚举暂缺（宽容转发不影响，核心未识别时回 error 帧由 UI 宽容消化）
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

/** 权限模式（ZCode CollaborationMode 子集；M8 起经 control permission_mode{mode} 下发，Python 侧并行实现） */
export type PermissionMode = 'build' | 'plan' | 'yolo';

/**
 * env_info.hardware（数据驱动 dict 直通，见 envprobe/hardware.py probe_hardware）。
 * 前 5 个字段是宿主镜像的早期建议形状；M5 定稿字段为 platform/cpuModel/
 * cpuCores/ramTotal/ramTotalBytes/gpuModel/gpuVram —— 两套并存，UI 各自降级。
 */
export interface HardwareInfo {
  /* 早期建议字段（宿主 protocol.ts HardwareInfo） */
  cpu?: string;
  gpu?: string;
  ram?: string;
  os?: string;
  arch?: string;
  /* M5 定稿字段（agent envprobe/hardware.py） */
  /** sys.platform：darwin / linux / win32 */
  platform?: string;
  cpuModel?: string;
  cpuCores?: number;
  /** 人读 RAM 总量（如 "64 GB"）；ramTotalBytes 为原始字节数 */
  ramTotal?: string;
  ramTotalBytes?: number;
  gpuModel?: string;
  gpuVram?: string;
}

/**
 * env_info.workspaceLangs[] 条目（envprobe/langs.py probe_workspace_langs）。
 * ok/detail 是早期建议形状（宿主镜像）；M5 定稿为 markers/missing/version/
 * packageManagers/extras/installHint —— missing=true 即黄牌（R21）。
 */
export interface WorkspaceLang {
  language?: string;
  ok?: boolean;
  detail?: string;
  /* M5 定稿字段 */
  /** 展示名（如 "Node.js/TypeScript"，LANGUAGE_LABELS） */
  label?: string;
  /** 命中的特征文件名（pyproject.toml / package.json…） */
  markers?: string[];
  /** 特征文件绝对路径（≤20 条/语言） */
  markerPaths?: string[];
  /** true = 语言出现但主工具链缺失 → 黄牌 + installHint（R21） */
  missing?: boolean;
  /** 主工具链版本（如 "Python 3.12.4"）；缺失时 null */
  version?: string | null;
  /** 包管理器 → 版本（pip3 / npm / cargo…） */
  packageManagers?: Record<string, string>;
  /** 补充工具 → 版本/存在性（conda / gradle / nvm…） */
  extras?: Record<string, string>;
  /** 缺失时的安装指引（英文一句话，envprobe/_TOOLCHAINS） */
  installHint?: string | null;
}

/**
 * sessions_list 摘要（protocol.py SessionsList 数据驱动直通；meta.json 字段集）。
 * M4 定稿：sessionId / title / pinned / lastActivity / messageCount / rootPath
 * （另有 providerId / modelId / parentSessionId）；updatedAt 为早期镜像字段。
 */
export interface SessionSummary {
  sessionId: string;
  title?: string;
  pinned?: boolean;
  /** 最近活跃（ISO，sessions/_now_iso：本地时间拼 Z 后缀——分组容忍小时级偏差） */
  lastActivity?: string;
  /** 兼容字段（早期宿主镜像用 updatedAt） */
  updatedAt?: string;
  /** 入站 user_message 条数（轮次计数） */
  messageCount?: number;
  /** multi-root 当前根（R11） */
  rootPath?: string;
  providerId?: string;
  modelId?: string;
  /** 分叉来源会话（R16 ↳ 标记） */
  parentSessionId?: string;
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
  /**
   * M8（R14/§3.4）压缩报告：layer 为 "L1"–"L4" 字符串（Python 侧 str，
   * 如 "L2"），report 为 dict 直通——字段宽容提取（evicted / savedTokens /
   * beforeTokens / afterTokens / summary 等，见 store extractCompaction）。
   * UI 侧仅 L2/L3 产生压缩卡，L4 冷存无卡。
   */
  | { type: 'compaction'; layer: string; report: Record<string, unknown> }
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
  /**
   * control 命令通用回执（protocol.py Result）。export 命令经 markdown 携带
   * 导出全文（R12：核心只产文本，落盘由 UI 调 workspace.saveExport）。
   */
  | { type: 'result'; ok: boolean; command?: string; detail?: string; markdown?: string }
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
        // M8：权限模式下发（Python 侧并行实现；{mode} 见下）。旧名
        // set_permission_mode 已废弃 —— 核心未识别时回 error 帧，UI 本地
        // 态已先行生效（宽容，不被打断）
        | 'permission_mode'
        | 'ping' // 测试连接（CONTROL_COMMANDS；宿主镜像枚举暂缺）
        // M4（protocol.py CONTROL_COMMANDS：R16/R28/R12/R11）
        | 'fork'
        | 'pin'
        | 'export'
        | 'set_root'
        // M5（R7/R21）：环境面板「刷新」→ 核心重新探测并广播 env_info
        | 'env_refresh'
        // M7（R3）：Skills 列表——核心扫描 SKILL.md 后经 result 帧回执
        // （Python 侧 CONTROL_COMMANDS 定稿为准；宿主镜像枚举暂缺，宽容转发）
        | 'skills_list'
        // M8（R3/M8）：MCP 管理页——mcp_list/mcp_refresh 经 result.detail
        // JSON 回执（宽容解析见 store parseMcpDetail）；mcp_add/mcp_remove
        // 提案载荷见下方 name/args/env 注释。Python 侧并行定稿
        // （docs/MCP_BACKEND_SPEC.md），未识别时回 error 帧由 UI 宽容消化
        | 'mcp_list'
        | 'mcp_refresh'
        | 'mcp_add'
        | 'mcp_remove';
      sessionId?: string;
      level?: string;
      providerId?: string;
      modelId?: string;
      /** permission_mode：目标模式（build | plan | yolo） */
      mode?: PermissionMode;
      /** fork：分叉点（转录内入站 user_message 的 messageId） */
      atMessageId?: string;
      /** pin：目标态；缺省 = 取反切换 */
      pinned?: boolean;
      /** set_root：init.workspaceRoots 下标（与 rootPath 二选一） */
      rootIndex?: number;
      /** set_root：按路径切根 */
      rootPath?: string;
      /**
       * 通用载荷通道（search_sessions 关键词 / M8 兼容）：mcp_add /
       * mcp_remove 把载荷镜像为 JSON 字符串放这里 —— Control.query 是
       * Python 侧现有协议字段，今天即可无损穿透 decode；专用字段
       * （name/args/env）定稿前核心可从 query JSON 取参。
       */
      query?: string;
      /**
       * M8 提案字段（mcp_add：服务名；mcp_remove：目标服务名）。
       * Python Control 侧并行定稿；未定义时被 decode 忽略（query 兜底）。
       */
      name?: string;
      /** M8 提案字段（mcp_add）：启动命令，空格分隔 argv（如 "npx -y @mcp/server ."） */
      args?: string;
      /** M8 提案字段（mcp_add）：子进程环境变量键值 */
      env?: Record<string, string>;
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
  | 'editor.pickFile'
  /** M5（R9）：图片附件挑选（文件对话框过滤图片后缀；用户取消 null） */
  | 'editor.pickImage';

/** M5：editor.pickImage → {file}；用户取消时 null（形状对齐 editor.pickFile） */
export interface EditorPickImageResult {
  file: string;
}

/**
 * M4 分叉点哨兵（约定字段，非协议帧值）：UI 侧没有转录内 messageId 时，
 * fork 以此值发送——核心侧若不支持该哨兵将回 not_found 错误（main.py
 * _control_fork 要求 atMessageId 命中转录）。当前会话优先用 checkpoint
 * 事件的 atMessageId（与入站 user_message messageId 同源，见 store
 * applyEvent 'checkpoint'），哨兵仅为「非当前会话分叉」的降级路径。
 */
export const FORK_AT_LAST_USER_MESSAGE = '__last_user_message__';

/** M4：切换宿主侧活跃会话（并行多开 R16；宿主侧并行实现，参数为提案） */
export type SessionRpcMethod = 'session.activate';

export interface SessionActivateParams {
  /** Python 核心会话 id（sessions_list.sessionId，非宿主 sessionKey） */
  sessionId: string;
  /** 已知宿主 sessionKey 时可携带（webview 通常不知道，缺省由宿主解析） */
  sessionKey?: string;
}

export interface SessionActivateResult {
  /** 激活后的宿主 sessionKey（宿主实现可回传用于后续定向 sendCore） */
  activeSessionKey?: string;
}

/** M4（R12）：会话导出 Markdown 落盘（宿主保存对话框 + 写文件） */
export type WorkspaceRpcMethod = 'workspace.saveExport' | 'workspace.imageUri';

export interface WorkspaceSaveExportParams {
  /** control export 回执 result.markdown 全文 */
  markdown: string;
  /** 建议（缺省）文件名：zcode-<sessionId>.md */
  fileName?: string;
  sessionId?: string;
}

export interface WorkspaceSaveExportResult {
  /** 实际保存路径（用户取消时缺省） */
  path?: string;
  saved?: boolean;
}

/**
 * M6（R8）：本地图片路径 → webview URI（image_artifact 帧 → <img src>）。
 * CSP img-src 只认 cspSource，本地文件必须经宿主 asWebviewUri 转换；宿主侧
 * 白名单 = 首工作区根 + ~/.zcode_py 系（越界应答错误 → 组件回退显示路径）。
 */
export interface WorkspaceImageUriParams {
  /** image_artifact.path（宿主侧本地绝对路径） */
  path: string;
}

export interface WorkspaceImageUriResult {
  /** 可直接作 <img src> 的 webview URI */
  uri: string;
}

/**
 * M7：Skill 条目（control skills_list 回执 result.detail JSON 解析产物）。
 * Python 侧（skills.py 并行实现中）字段宽容：name 必有，description/path
 * （来源 SKILL.md 路径）可选——解析端做形状归一。
 */
export interface SkillInfo {
  name: string;
  description?: string;
  /** SKILL.md 来源路径（列表 title 属性展示） */
  path?: string;
  [key: string]: unknown;
}

/**
 * M8：MCP 服务/工具条目（control mcp_list / mcp_refresh 回执
 * result.detail JSON 宽容解析产物，见 store parseMcpDetail）。
 * Python 侧（mcp_client.py 并行实现中）字段宽容：name 必有；status
 * 宽容（connected=绿 / error=红 / 其余=灰未连）；tools[{name,description}]
 * 条目亦可为纯字符串；toolCount 是仅有计数时的降级。
 */
export interface McpToolInfo {
  name: string;
  description?: string;
}

export interface McpServerInfo {
  name: string;
  /** 原样状态字符串（connected / error / disconnected / 未装配缝…） */
  status: string;
  /** 状态附注/错误说明（装配缝缺失判定用） */
  detail?: string;
  tools: McpToolInfo[];
  /** 仅有工具计数、无清单时的降级展示 */
  toolsCount?: number;
  [key: string]: unknown;
}

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

export type HostRpcMethod =
  | RegisteredRpcMethod
  | ProviderRpcMethod
  | EditorRpcMethod
  | SessionRpcMethod
  | WorkspaceRpcMethod;

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
