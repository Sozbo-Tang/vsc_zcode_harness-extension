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
 *  - M9（R3/R13）：新帧 task_started/task_output/task_finished 与新命令
 *    tasks_list/task_stop/cron_list/cron_add/cron_remove/cron_toggle/
 *    workflow_run/workflows_list 已双侧镜像（宿主 protocol.ts 同步落地；
 *    Python 侧并行定稿中——未识别命令回 error 帧，UI 静默降级）
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
  /** 无 token 回合（取消/空回合）为 null（perf.turn_end），聚合端须判空 */
  ttftMs?: number | null;
  avgTokPerSec?: number | null;
}

/** usage.byModel 值形状：该模型的 {input, output} 累计桶（R19） */
export interface UsageModelBucket {
  input: number;
  output: number;
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
      /**
       * R19 会话统计分组键：产生本帧的模型 id（Python protocol.Perf.model，
       * loop 侧填充，与 usage.byModel 键同源）。旧帧/无模型场景缺省——
       * store 聚合时归入「未知」桶。
       */
      model?: string;
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
  /**
   * usage：tokens = 全模型合计；byModel = 模型 → {input, output} 累计桶
   * （R19 会话统计；核侧按「本会话」累计——new_session 清零、resume 从
   * 转录播种，见 main._usage_seed_from_frames）。
   */
  | { type: 'usage'; tokens: number; byModel?: Record<string, UsageModelBucket> }
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
      /** 降级/后续动作提示（无 remote、gh 缺失…；R17/R22） */
      hint?: string;
    }
  | { type: 'image_artifact'; path: string }
  /**
   * M9（R3）：后台任务事件帧（Python 侧并行定稿，字段宽容——归一在 store
   * 归约器：taskId 兼容 id/task_id 命名；status 宽容 done/failed/stopped）。
   * task_output 为增量追加，UI 侧滚动窗口截尾（≤4KB，store TASK_TAIL_LIMIT）。
   */
  | { type: 'task_started'; taskId: string; name?: string; command?: string; atMs?: number }
  | { type: 'task_output'; taskId: string; tail: string; stream?: string }
  | { type: 'task_finished'; taskId: string; status?: string; exitCode?: number; detail?: string }
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
  /**
   * R30「回滚到这」：checkpointId 来自 checkpoint 帧的 id（store 已把
   * checkpoint 关联到对应 user 消息条目）。核侧 = git checkout <sha> -- .
   * （恢复全部已跟踪文件；untracked 不动）。确认语义：本帧即用户在 UI
   * 确认后的显式动作，核侧收到即执行（PLAN §5 无二次确认往返）。回执
   * rollback_result{ok} + 失败时伴随 error 帧；宿主侧 RollbackMessage
   * 早已镜像（extension/src/protocol.ts），此处补齐 webview 发送子集。
   */
  | { type: 'rollback'; checkpointId: string }
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
        // M4 补完：删除会话（sessionId；核侧拒绝当前活跃会话 + id 白名单
        // 校验；成功回执后广播 sessions_list）
        | 'session_delete'
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
        | 'mcp_remove'
        // M9（R3/R13，Python 侧并行定稿）：后台任务 / 定时任务 / 工作流 /
        // 内联补全（inline_complete 由宿主侧发起，webview 不发）。列表类
        // 命令（tasks_list / cron_list / workflows_list）回执走
        // result.detail JSON（数组或 {items:[...]} 均收，宽容解析见 store）；
        // 增删启停即时生效；核心未识别命令时回 error 帧由 UI 宽容消化。
        | 'tasks_list'
        | 'task_stop'
        | 'cron_list'
        | 'cron_add'
        | 'cron_remove'
        | 'cron_toggle'
        | 'workflow_run'
        | 'workflows_list'
        // R3：工具开关 + 默认权限（tools.json 持久化在核心侧）
        | 'tools_list'
        | 'tool_toggle'
        | 'tool_permission'
        // R15/R20：设置页——settings_get 回 result.detail JSON {settings,
        // homeDir, sessionsDir}；settings_set 收 {settings} 整体合并或
        // {key, value} 单键（受管键校验在核侧）；clear_sessions 清空
        // sessions 目录保 config（UI 二次确认后发送）
        | 'settings_get'
        | 'settings_set'
        | 'clear_sessions'
        // R17/R22：一键 git 流——git_preview 预览（diff 统计 + 风格建议
        // 信息经 result.detail JSON 回本组件编辑）；git_pr 创建 PR（gh pr
        // create，回 git_result 帧）。执行帧是独立的 git_commit（见下）
        | 'git_preview'
        | 'git_pr';
      sessionId?: string;
      level?: string;
      providerId?: string;
      modelId?: string;
      /**
       * permission_mode：目标模式（build | plan | yolo）；
       * tool_permission：默认权限（ask = 每次询问 | always = 始终允许白名单）
       */
      mode?: PermissionMode | 'ask' | 'always';
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
      /** M9 提案字段：task_stop 的目标任务 id；cron_remove/cron_toggle 的
       *  定时任务 id（与 name 双发，核心侧 cronId 优先） */
      taskId?: string;
      /** M9：cron_remove / cron_toggle 的定时任务条目 id（core 契约字段） */
      cronId?: string;
      /** M9 提案字段（cron_add）：cron 调度表达式（标准 5 段） */
      schedule?: string;
      /** M9 提案字段（cron_add）：prompt 型动作文本（command 型用 action.command） */
      prompt?: string;
      /** M9（cron_add 契约字段）：到点动作 {type:'prompt',text} | {type:'task',command} */
      action?: { type: 'prompt' | 'task'; text?: string; command?: string };
      /** M9 提案字段（cron_toggle）：目标启停态 */
      enabled?: boolean;
      /** R15/R20（settings_set）：整体/部分合并载荷（与 key/value 二选一，优先） */
      settings?: Record<string, unknown>;
      /** R15/R20（settings_set）：单键名（与 value 成对） */
      key?: string;
      /** R15/R20（settings_set）：单键值（清空字符串键请传 ''） */
      value?: unknown;
      /**
       * R17/R22 git 流字段：repoRoot 须在 init.workspaceRoots 白名单内
       * （核心校验拒绝工作区外仓库）；scope 仅 git_preview（缺省 staged）；
       * title/body 仅 git_pr（缺省 --fill 用提交信息）
       */
      repoRoot?: string;
      scope?: 'staged' | 'all';
      title?: string;
      body?: string;
    }
  /** R17/R22 执行帧：scope=all 时核心先 git add -A；message 为预览卡上用户
   *  编辑过的完整提交信息（缺省时核心按仓库风格生成）。回执 git_result */
  | {
      type: 'git_commit';
      repoRoot: string;
      scope: 'staged' | 'all';
      push: boolean;
      message?: string;
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

/** M4：切换宿主侧活跃会话（并行多开 R16；宿主侧并行实现，参数为提案）；
 * session.ensure：冷启动自愈（窗口重载后 Python 进程不复活，webview 侧
 * control / env_refresh / user_message 全部无处投递——发任何 core 消息前
 * 先 ensure：进程健在原样返回，否则宿主新建会话并激活） */
export type SessionRpcMethod = 'session.activate' | 'session.ensure';

export interface SessionEnsureResult {
  sessionKey: string;
  /** false=复用既有活跃会话；true=本次新建 */
  created: boolean;
}

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
export type WorkspaceRpcMethod = 'workspace.saveExport' | 'workspace.imageUri' | 'workspace.revealPath';

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

/** R3 工具面板条目（tools_list 回执投影） */
export interface ToolInfo {
  name: string;
  description?: string;
  /** readonly | write */
  tier?: string;
  enabled: boolean;
  /** true = 已有 `name: *` 免询问白名单规则（不绕过危险命令黑名单） */
  alwaysAllowed: boolean;
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
  /**
   * R20 i18n：宿主显示语言（vscode.env.language，如 "zh-cn" / "en"）。
   * 可选兼容：旧宿主未下发时 webview 回落 navigator.language。
   */
  language?: string;
}

/** Provider CRUD RPC：webview 侧提案方法名（宿主 M1 后续注册后即通） */
export type ProviderRpcMethod =
  | 'providers.save'
  | 'providers.delete'
  | 'providers.set_default'
  | 'provider.ping'
  | 'provider.list_models';

/**
 * R15/R20 设置页：VS Code 原生配置读写（宿主已注册 extension.ts）。读
 * {key} → {value}；写 {key, value} → Global 目标（宿主侧限 zcode-py.* 键，
 * 内联补全开关 zcode-py.inlineCompletion.enabled 即走此通道）。
 */
export type ConfigRpcMethod = 'config.get' | 'config.update';

export interface ConfigGetResult {
  value?: unknown;
}

export interface ConfigUpdateResult {
  value?: unknown;
}

export interface WorkspaceRevealPathResult {
  revealed: boolean;
}

export type HostRpcMethod =
  | RegisteredRpcMethod
  | ProviderRpcMethod
  | EditorRpcMethod
  | SessionRpcMethod
  | WorkspaceRpcMethod
  | ConfigRpcMethod;

/* ---------- R15/R20：核心侧设置（settings_get 回执投影） ---------- */

/** 视觉中继转述档位（vision_relay.PRESET_IDS） */
export type VisionRelayPreset = 'ocr' | 'describe' | 'frontend' | 'custom';

/**
 * 核心受管设置（~/.zcode-py/config/settings.json，Agent/压缩/视觉中继/
 * 隐私护栏/音效五组）。「通用」（语言/字号）是 webview 本地设置、「内联补全」
 * 是 VS Code 原生配置，都不在此列。
 */
export interface CoreSettings {
  /** Agent：系统提示词追加文本（接在基础提示词之后） */
  systemPromptAppend: string;
  /** Agent：单回合最大轮数（loop 上限，1..200） */
  maxTurns: number;
  compaction: {
    /** L3 自动摘要触发阈值（0 < x ≤ 1，默认 0.85） */
    summarizeThreshold: number;
    /** 压缩专用模型（providerId/modelId 或 modelId；空 = 用主模型） */
    compactionModel: string;
  };
  /** 视觉中继：转述档位（settings 覆盖 providers.json 条目级配置） */
  visionRelayPreset: VisionRelayPreset;
  /** 视觉中继：custom 档的自定义转述提示词 */
  visionRelayCustomPrompt: string;
  /** 隐私护栏：文件名通配模式列表（含 / 的模式按完整路径匹配） */
  privacyFilePatterns: string[];
  /** R6 收尾：车钟换挡咔哒音效开关（纯 webview 消费；缺省 true = 开） */
  telegraphClickSound: boolean;
}

/** settings_get 回执 detail JSON 形状（宽容解析见 store.parseSettingsDetail） */
export interface SettingsGetPayload {
  settings: CoreSettings;
  homeDir: string;
  sessionsDir: string;
}

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

/**
 * 后台任务投影（M9 完整落地）。数据源双轨：
 *  - 事件帧 task_started / task_output / task_finished 增量归约（store.applyEvent）
 *  - control tasks_list 回执 result.detail JSON 全量覆盖（宽容解析
 *    parseTasksDetail：数组或 {items:[...]}；id 兼容 taskId/task_id，
 *    status 宽容 done/failed/stopped/cancelled…）
 */
export interface BackgroundTask {
  id: string;
  name: string;
  status: 'running' | 'done' | 'failed' | 'stopped';
  /** 发起命令行（bash 后台/工作流步骤带出；缺省显示 id） */
  command?: string;
  /** 启动时刻（epoch ms；帧缺省时为帧到达时刻） */
  startedAt?: number;
  /** 终态时刻（epoch ms） */
  endedAt?: number;
  /** 尾部输出滚动窗口（≤4KB，task_output 增量追加、超限丢头） */
  tail?: string;
  exitCode?: number;
  /** 终态附注（失败原因等） */
  detail?: string;
}

/**
 * 定时任务条目（M9；cron_list 回执 result.detail JSON 宽容解析产物）。
 * Python 侧并行定稿：name 必有（兼容 id），schedule/action/enabled 宽容
 * （action 兼容 prompt/command 命名，kind 缺省按内容推断）。
 */
export interface CronEntry {
  name: string;
  /** cron 调度（7 段表达式或描述文本，原样展示） */
  schedule: string;
  /** 动作：prompt 文本或 command 命令行 */
  action?: string;
  /** 动作类型（宽容缺省：action 以字母开头且含空格→按内容猜，展示端兜底 '—'） */
  kind?: 'prompt' | 'command' | string;
  enabled: boolean;
  [key: string]: unknown;
}

/**
 * 工作流条目（M9；workflows_list 回执 result.detail JSON 宽容解析产物）。
 * name 必有（兼容 id）；description/path 可选。运行态不落列表——由
 * workflowRuns（store 切片，按 name 记 running/done/failed + 报告）承载。
 */
export interface WorkflowInfo {
  name: string;
  description?: string;
  /** 工作流定义文件路径（SKILL.md 同款来源展示） */
  path?: string;
  [key: string]: unknown;
}

/* ---------- R17/R22：一键 git 流投影（git_preview 回执 result.detail JSON） ---------- */

/** 单文件变更统计（agent/zcode_py/gitflow.py FileStat） */
export interface GitFileStat {
  path: string;
  /** A | M | D | R | C | ??（?? = untracked，scope=all 才收） */
  status: string;
  insertions?: number | null;
  deletions?: number | null;
}

/** git_preview 回执载荷（宽容解析见 store.parseGitPreview；ok=false 只填 error） */
export interface GitPreviewPayload {
  ok: boolean;
  repoRoot: string;
  scope: 'staged' | 'all';
  files: GitFileStat[];
  insertions: number;
  deletions: number;
  untracked: number;
  /** 按仓库风格生成的建议（用户可编辑后随 git_commit 发回） */
  subject: string;
  body: string;
  /** 一行风格说明（conventional 与否 / 语言 / 降级附注） */
  styleNote: string;
  remoteUrl?: string | null;
  /** github | gitlab | bitbucket | other | none */
  remoteKind: string;
  /** gh 在 PATH 且 remote 是 github → UI 显示「创建 PR」按钮 */
  canPr: boolean;
  ghAvailable: boolean;
  error?: string | null;
}

export interface TodoItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
}
