/**
 * 单一 zustand store（PLAN §4 前端约定）：view / sessions / activeSession /
 * providers / env / tasks 切片 + 事件归约器 applyEvent。
 *
 * 归约器思路参照 ZCode 事件溯源 UI 协议（docs/research/zcode-semantics.md：
 * 统一信封事件 → 纯函数归约成只读投影）：核 → 壳事件（protocol.ts CoreEvent，
 * 经宿主 {kind:'core'} 转发）进来后只增改 items 投影，视图层纯消费。
 * 轻量 UI 态（当前视图/输入草稿/权限模式）经 vscode getState/setState 注水
 * 兜底（research §1.4 / §7 决策 2）；真相态以宿主 ready+replay 重放与事件流为准。
 */
import { create } from 'zustand';
import { RpcTimeoutError, getVsCodeApi, hostRequest, hostSendCore } from '../host/transport';
import type {
  BackgroundTask,
  CoreEvent,
  CronEntry,
  GetStateResult,
  HardwareInfo,
  McpServerInfo,
  McpToolInfo,
  PermissionMode,
  PingResult,
  ProviderConfig,
  SessionSummary,
  SkillInfo,
  TelegraphLevel,
  TodoItem,
  UserMessageContext,
  WorkflowInfo,
  WorkspaceLang,
} from '../protocol';
import { FORK_AT_LAST_USER_MESSAGE } from '../protocol';

/* ---------- 聊天条目投影（由事件归约产生） ---------- */

export type ChatItem =
  | { kind: 'user'; id: string; at: number; text: string; context?: UserMessageContext }
  | { kind: 'assistant'; id: string; at: number; text: string; streaming: boolean }
  | {
      kind: 'thinking';
      id: string;
      at: number;
      text: string;
      startedAt: number;
      endedAt?: number;
    }
  | {
      kind: 'tool';
      id: string;
      callId: string;
      name: string;
      args: Record<string, unknown>;
      state: 'started' | 'update' | 'finished';
      result?: unknown;
      elapsedMs?: number;
      /** 隐私护栏标记（R15）：命中敏感模式时醒目展示 */
      warning?: string;
    }
  | {
      kind: 'permission';
      id: string;
      at: number;
      tool: string;
      args: Record<string, unknown>;
      decision?: 'allow' | 'deny' | 'always';
      /** 「写规则」提交的规则文本（decision='always' 时随 permission_response 附带） */
      rule?: string;
    }
  | {
      kind: 'diff';
      id: string;
      at: number;
      file: string;
      oldText: string;
      newText: string;
      accepted?: boolean;
    }
  | { kind: 'relay'; id: string; at: number; model: string; preset: string; description: string }
  /**
   * M6（R8）：截图/图片 artifact（image_artifact{path} 帧归约）。path 是宿主侧
   * 本地绝对路径——渲染端经宿主 RPC workspace.imageUri 换 webview URI 后
   * <img> 显示（失败回退路径文本）。
   */
  | { kind: 'image'; id: string; at: number; path: string }
  /**
   * M8（R14/§3.4）：compaction{layer, report} 帧归约的压缩报告卡。
   * 仅 L2（机械淘汰）/ L3（自动摘要）产卡；L4 冷存无卡。字段从 report
   * 宽容提取（extractCompaction），raw 保留原始 report 供卡内兜底展示。
   */
  | {
      kind: 'compaction';
      id: string;
      at: number;
      /** 归一化层级（"L2" | "L3"） */
      layer: string;
      /** L2：淘过的工具输出条数（evicted 数字或列表长度） */
      evicted?: number;
      savedTokens?: number;
      /** L3：摘要前后 token 对比 */
      beforeTokens?: number;
      afterTokens?: number;
      /** L3：七要素摘要全文（任务目标/关键决策/文件及状态/错误与解法/环境事实/待办/下一步） */
      summary?: string;
      /** 原始 report（宽容兜底展示） */
      raw?: Record<string, unknown>;
    }
  | { kind: 'error'; id: string; at: number; errorKind: string; message: string };

export interface PerfState {
  phase: 'idle' | 'streaming' | 'done';
  ttftMs?: number;
  tokPerSec?: number;
  lastTurn?: { ttftMs: number; avgTokPerSec: number };
}

export type ViewId = 'chat' | 'sessions' | 'api' | 'env' | 'tools' | 'tasks' | 'settings';

/** telegraph.current 是挡位 id（宿主 TelegraphStateMessage.current 语义） */
export interface TelegraphState {
  levels: TelegraphLevel[];
  current: string;
  paramTranslation?: Record<string, unknown>;
}

/** ping/拉模型每 Provider 的临时态 */
interface ProviderUiState {
  pingBusy: boolean;
  ping?: PingResult & { at: number };
  modelsBusy: boolean;
  modelsError?: string;
}

/** 会话条目级瞬态 UI（M4：R16/R28/R12 动作的 busy 与回执提示），key = sessionId */
export interface SessionUiState {
  busy?: 'resume' | 'fork' | 'pin' | 'export';
  notice?: string;
  noticeKind?: 'info' | 'error';
}

/** 最近一次异步 control（fork/pin/export/resume）的对账锚：result 回执不带
 * sessionId，用 (command, sessionId) 配对一次（单用户侧栏的 single-flight 假定） */
interface PendingControl {
  command: string;
  sessionId: string;
  at: number;
}

/* ---------- 轻量 UI 态持久化（vscode getState/setState） ---------- */

interface PersistedUi {
  view?: ViewId;
  inputDraft?: string;
  permissionMode?: PermissionMode;
}

function readPersisted(): PersistedUi {
  try {
    const s = getVsCodeApi().getState();
    if (s && typeof s === 'object') return s as PersistedUi;
  } catch {
    /* mock/异常场景忽略 */
  }
  return {};
}

function persistUi(get: () => WebviewStore): void {
  try {
    const { view, inputDraft, permissionMode } = get();
    getVsCodeApi().setState({ view, inputDraft, permissionMode });
  } catch {
    /* setState 失败不影响功能 */
  }
}

/* ---------- 默认值 ---------- */

/**
 * 车钟兜底盘面：宿主尚未下发 telegraph_state 时的中性默认
 * （按 PLAN §3.3 参考值构造；真实盘面永远以模型档案 telegraphLevels 为准）。
 */
const FALLBACK_TELEGRAPH: TelegraphState = {
  levels: [
    { id: 'off', label: '关闭', param: '不发送思考参数', ok: true },
    { id: 'low', label: '低', param: { budget_tokens: 8192 }, ok: true },
    { id: 'mid', label: '中', param: { budget_tokens: 24576 }, ok: true },
    { id: 'high', label: '高', param: { budget_tokens: 65536 }, ok: true },
  ],
  current: 'mid',
};

export interface WebviewStore {
  /* ---- 切片：view ---- */
  view: ViewId;
  hostConnected: boolean;
  setView(v: ViewId): void;

  /* ---- 切片：sessions ---- */
  sessions: SessionSummary[];
  /** 宿主会话键（getState.activeSessionKey，宿主侧真相） */
  activeSessionKey?: string;
  /** Python 核心会话 id（session_started.sessionId，与宿主 sessionKey 是两个命名空间） */
  coreSessionId?: string;
  processState?: { sessionKey: string; state: string; detail?: string };
  /** 会话条目瞬态 UI（busy/回执提示），key = sessionId */
  sessionsUi: Record<string, SessionUiState>;
  /**
   * 最近一次 checkpoint 的 atMessageId —— 与入站 user_message 的 messageId
   * 同源（main.py _on_user_message 生成同一 id），是 UI 侧唯一可得的分叉点
   * 真实标识；无 git 仓库（无 checkpoint 帧）时为 undefined。
   */
  lastAtMessageId?: string;
  /** 异步 control 对账锚（见 PendingControl） */
  pendingControl?: PendingControl;

  /* ---- 切片：activeSession（当前会话投影） ---- */
  chat: { items: ChatItem[]; running: boolean };
  todos: TodoItem[];
  perf: PerfState;
  contextPct?: number;
  usageTokens?: number;
  /**
   * M8：最近一次压缩校正锚（L2/L3 compaction 帧到达时按 report.afterTokens
   * 校正 usageTokens/contextPct；ContextMeter title 提示校正来源）。
   */
  lastCompaction?: { layer: string; afterTokens?: number; at: number };
  permissionMode: PermissionMode;
  telegraph: TelegraphState;

  /* ---- 切片：providers ---- */
  providers: ProviderConfig[];
  providerUi: Record<string, ProviderUiState>;

  /* ---- 切片：env ---- */
  env: { hardware: HardwareInfo; workspaceLangs: WorkspaceLang[] } | null;
  /** env_refresh 已发出、等待 env_info 回流（按钮禁用态；15s 兜底解锁） */
  envRefreshing: boolean;

  /* ---- 切片：skills（M7，R3） ---- */
  /** skills_list 回执投影（ToolsView Skills 页签 + 输入框 / 命令提示数据源） */
  skills: SkillInfo[];
  /** skills_list 已发出、等待 result 回执（刷新按钮禁用态；15s 兜底解锁） */
  skillsRefreshing: boolean;
  /** 是否已请求过一次（Skills 页签首开自动拉取一次的幂等标记） */
  skillsRequested: boolean;
  /** 发 control skills_list（核心扫描 SKILL.md → result.detail JSON 回执） */
  refreshSkills(): void;

  /* ---- 切片：mcp（M8，R3/M8） ---- */
  /** mcp_list / mcp_refresh 回执投影（ToolsView MCP 页签数据源） */
  mcpServers: McpServerInfo[];
  /** mcp_list/mcp_refresh 已发出、等待 result 回执（刷新按钮禁用；15s 兜底） */
  mcpRefreshing: boolean;
  /** 是否已请求过一次（MCP 页签首开自动拉取一次的幂等标记） */
  mcpRequested: boolean;
  /** mcp_add / mcp_remove 进行中（单飞：表单提交/删除按钮禁用；15s 兜底） */
  mcpPendingAction?: 'add' | 'remove';
  /** MCP 页签操作回执提示（添加/删除/列表失败） */
  mcpNotice?: { kind: 'info' | 'error'; text: string };
  /** 发 control mcp_list（缺省）或 mcp_refresh（重连重扫） */
  refreshMcp(command?: 'mcp_list' | 'mcp_refresh'): void;
  /** 添加服务：control mcp_add（name + 空格分隔 argv + env；载荷兼容通道见 protocol.ts） */
  addMcpServer(input: { name: string; command: string; env: Record<string, string> }): void;
  /** 删除服务：control mcp_remove */
  removeMcpServer(name: string): void;

  /* ---- 切片：tasks（M9，R3/§6.5） ---- */
  /** 后台任务投影：task_started/output/finished 帧增量归约 + tasks_list 回执全量 */
  tasks: BackgroundTask[];
  tasksRefreshing: boolean;
  /** 是否已请求过一次（运行中页签首开自动拉取的幂等标记） */
  tasksRequested: boolean;
  /** ⏹停止（task_stop）进行中的任务 id → 按钮禁用 */
  taskStopping: Record<string, boolean>;
  /** 任务耗时走表 tick（TasksView 存在运行中任务时秒级更新，驱动重渲染） */
  taskTick?: number;
  /** 发 control tasks_list（首开/手动刷新；回执 result.detail JSON 宽容解析） */
  refreshTasks(): void;
  /** 发 control task_stop{taskId}（终态以 task_finished 帧为准） */
  stopTask(taskId: string): void;

  /* ---- 切片：crons（M9，§6.5 定时任务） ---- */
  crons: CronEntry[];
  cronsRefreshing: boolean;
  /** 是否已请求过一次（定时任务页签首开自动拉取的幂等标记） */
  cronsRequested: boolean;
  /** 新建/删除/启停进行中（单飞：表单与行按钮禁用；15s 兜底） */
  cronPendingAction?: 'add' | 'remove' | 'toggle';
  cronNotice?: { kind: 'info' | 'error'; text: string };
  /** 发 control cron_list */
  refreshCrons(): void;
  /**
   * 发 control cron_add —— prompt 型（schedule + prompt 文本）或 command 型
   * （schedule + 命令行）；双通道载荷（schedule/prompt/args 专用字段 +
   * query JSON 镜像），成功自动回拉列表。
   */
  addCron(input: { schedule: string; prompt?: string; command?: string }): void;
  /** 发 control cron_remove{name} */
  removeCron(name: string): void;
  /** 发 control cron_toggle{name, enabled}（乐观翻转，!ok 回执由回拉纠正） */
  toggleCron(name: string, enabled: boolean): void;

  /* ---- 切片：workflows（M9，§6.5 工作流） ---- */
  workflows: WorkflowInfo[];
  workflowsRefreshing: boolean;
  /** 是否已请求过一次（工作流页签首开自动拉取的幂等标记） */
  workflowsRequested: boolean;
  /** 按工作流名的最近一次运行态（运行中标记 / 报告宽容渲染） */
  workflowRuns: Record<string, { status: 'running' | 'done' | 'failed'; report?: string; at: number }>;
  /** workflow_run 在途的工作流名（result 回执不带目标，单飞对账锚） */
  pendingWorkflowRun?: string;
  workflowNotice?: { kind: 'info' | 'error'; text: string };
  /** 发 control workflows_list */
  refreshWorkflows(): void;
  /** 发 control workflow_run{name}（运行态沿 workflowRuns + 工具卡显示） */
  runWorkflow(name: string): void;

  /* ---- 输入态 ---- */
  inputDraft: string;
  setInputDraft(text: string): void;
  /** M3/M5：待发上下文（📎文件 / 🖼图片 / ⊞选中 / 终端选中 / Problems 的 chip 集合） */
  pendingContext: UserMessageContext;
  attachContext(patch: Partial<UserMessageContext>): void;
  removeContextChip(
    kind: 'fileRef' | 'selection' | 'terminalSel' | 'problems' | 'image',
    file?: string,
  ): void;
  applyHostPrompt(text: string, context?: UserMessageContext): void;

  /* ---- 动作 ---- */
  boot(): Promise<void>;
  applyEvent(e: CoreEvent): void;
  applyHostError(e: { sessionKey?: string; errorKind: string; message: string }): void;
  applyProcessState(e: { sessionKey: string; state: string; detail?: string }): void;

  sendUserMessage(text: string, context?: UserMessageContext): void;
  newSession(): void;
  cancelRunning(): void;

  /* ---- M4 会话动作（R16/R28/R12；均发 control，回执经 result 归约） ---- */
  /** 恢复/切换活跃会话：control resume（重放转录，session_started 切投影）
   *  + session.activate RPC（宿主切换活跃进程，并行多开）+ 切聊天视图 */
  activateSession(sessionId: string): void;
  /** 从分叉点复制前缀成新会话（R16；atMessageId 见 FORK_AT_LAST_USER_MESSAGE 注释） */
  forkSession(sessionId: string): void;
  /** 收藏/取消收藏（R28；乐观翻转 —— core pin 只回 result 不回 sessions_list） */
  togglePinSession(sessionId: string): void;
  /** 导出 Markdown（R12）：control export → result.markdown → workspace.saveExport 落盘 */
  exportSession(sessionId: string): void;
  /** 环境面板刷新（M5/R21）：control env_refresh → 核心重新探测广播 env_info */
  refreshEnv(): void;
  /** Skills 刷新（M7/R3）：control skills_list → result.detail JSON → skills 切片 */
  refreshSkills(): void;
  /** 决策按钮 → §5 permission_response；rule 为「写规则」附带文本（可选） */
  respondPermission(id: string, decision: 'allow' | 'deny' | 'always', rule?: string): void;
  reviewDiff(id: string, accepted: boolean): void;
  setTelegraphLevel(id: string): void;
  switchModel(providerId: string, modelId: string): void;
  setPermissionMode(mode: PermissionMode): void;

  saveProvider(p: ProviderConfig, newApiKey?: string): Promise<void>;
  deleteProvider(id: string): void;
  setDefaultProvider(id: string): void;
  pingProvider(id: string): Promise<void>;
  fetchModels(id: string): Promise<void>;
}

let nseq = 0;
const nid = (p: string): string => `${p}-${Date.now().toString(36)}-${(nseq++).toString(36)}`;

/** 子代理工具名（M7 落地 run_child；这里先按 PLAN §6.1 线框做占位识别） */
const SUBAGENT_TOOLS = new Set(['run_child', 'task', 'agent']);
export const isSubagentTool = (name: string): boolean => SUBAGENT_TOOLS.has(name);

/** todo 工具名（§5 暂无独立 todo 事件，M2 工具系统落地前的临时归约口径） */
const TODO_TOOLS = new Set(['todo', 'todowrite', 'todo_write']);

/** 车钟显示辅助：当前挡位（含兜底） */
export function currentLevel(tg: TelegraphState): TelegraphLevel | undefined {
  return tg.levels.find((l) => l.id === tg.current) ?? tg.levels.find((l) => l.ok);
}

/** param（unknown：string / 数值 / dict）→ 盘面下方一行说明 */
export function formatParam(p: unknown): string {
  if (p == null) return '—';
  if (typeof p === 'string') return p;
  if (typeof p === 'number') return String(p);
  try {
    return JSON.stringify(p);
  } catch {
    return String(p);
  }
}

function currentModelContextLength(providers: ProviderConfig[]): number | undefined {
  const def = providers.find((p) => p.isDefault) ?? providers[0];
  const m = def?.models.find((x) => x.id === def.selectedModel);
  return m?.contextLength;
}

const emptyUi = (): ProviderUiState => ({ pingBusy: false, modelsBusy: false });

function closeStreaming(items: ChatItem[]): ChatItem[] {
  return items.map((it) => {
    if (it.kind === 'assistant' && it.streaming) return { ...it, streaming: false };
    if (it.kind === 'thinking' && !it.endedAt) return { ...it, endedAt: Date.now() };
    return it;
  });
}

/** rpc 失败分类：超时=宿主不可达；RpcRemoteError=宿主已应答（连通正常） */
function classifyRpcError(err: unknown): boolean {
  return !(err instanceof RpcTimeoutError);
}

type StoreSet = (partial: Partial<WebviewStore> | ((s: WebviewStore) => Partial<WebviewStore>)) => void;
type StoreGet = () => WebviewStore;

/** 单条会话 UI 态打补丁 */
function patchSessionUi(set: StoreSet, sessionId: string, patch: Partial<SessionUiState>): void {
  set((s) => ({
    sessionsUi: { ...s.sessionsUi, [sessionId]: { ...s.sessionsUi[sessionId], ...patch } },
  }));
}

/**
 * M7：skills_list 回执 result.detail（JSON 字符串）→ SkillInfo[]。
 * Python 侧（skills.py）载荷形状未定稿——宽容解析：数组本身，或
 * {skills: [...]} 包裹；条目须有 string name，description/path 宽容
 * （path 兼容 source 命名）；解析失败回空数组（UI 显示空态）。
 */
function parseSkillsDetail(detail?: string): SkillInfo[] {
  if (typeof detail !== 'string' || detail === '') return [];
  let data: unknown;
  try {
    data = JSON.parse(detail);
  } catch {
    return [];
  }
  const arr = Array.isArray(data)
    ? data
    : data !== null && typeof data === 'object' && Array.isArray((data as { skills?: unknown }).skills)
      ? ((data as { skills: unknown[] }).skills)
      : [];
  const out: SkillInfo[] = [];
  for (const x of arr) {
    if (x === null || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    if (typeof o.name !== 'string' || o.name === '') continue;
    out.push({
      name: o.name,
      description: typeof o.description === 'string' ? o.description : undefined,
      path:
        typeof o.path === 'string'
          ? o.path
          : typeof o.source === 'string'
            ? o.source
            : undefined,
    });
  }
  return out;
}

/**
 * M8：mcp_list / mcp_refresh 回执 result.detail（JSON 字符串）→
 * McpServerInfo[]。Python 侧（mcp_client.py）载荷形状并行定稿——宽容解析
 * （约定：数组或 {servers:[...]}；服务 name 必有、status 兼容 state 命名、
 * detail 兼容 error 命名；tools 条目可为 {name,description} 或纯字符串；
 * 仅有计数时取 toolCount/toolsCount）。解析失败回空数组（UI 空态）。
 */
export function parseMcpDetail(detail?: string): McpServerInfo[] {
  if (typeof detail !== 'string' || detail === '') return [];
  let data: unknown;
  try {
    data = JSON.parse(detail);
  } catch {
    return [];
  }
  const arr = Array.isArray(data)
    ? data
    : data !== null &&
        typeof data === 'object' &&
        Array.isArray((data as { servers?: unknown }).servers)
      ? ((data as { servers: unknown[] }).servers)
      : [];
  const out: McpServerInfo[] = [];
  for (const x of arr) {
    if (x === null || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    const name =
      typeof o.name === 'string' && o.name !== ''
        ? o.name
        : typeof o.id === 'string' && o.id !== ''
          ? o.id
          : undefined;
    if (name === undefined) continue;
    const tools: McpToolInfo[] = [];
    if (Array.isArray(o.tools)) {
      for (const t of o.tools) {
        if (typeof t === 'string') {
          if (t !== '') tools.push({ name: t });
          continue;
        }
        if (t === null || typeof t !== 'object') continue;
        const to = t as Record<string, unknown>;
        if (typeof to.name === 'string' && to.name !== '') {
          tools.push({
            name: to.name,
            description: typeof to.description === 'string' ? to.description : undefined,
          });
        }
      }
    }
    const toolsCount =
      typeof o.toolCount === 'number'
        ? o.toolCount
        : typeof o.toolsCount === 'number'
          ? o.toolsCount
          : undefined;
    out.push({
      name,
      status:
        typeof o.status === 'string'
          ? o.status
          : typeof o.state === 'string'
            ? o.state
            : 'unknown',
      detail:
        typeof o.detail === 'string'
          ? o.detail
          : typeof o.error === 'string'
            ? o.error
            : undefined,
      tools,
      toolsCount,
    });
  }
  return out;
}

/* ---------- M9：后台任务 / 定时任务 / 工作流宽容解析 ---------- */

/** task_output 尾部输出滚动窗口上限（§6.5 任务卡「尾部输出 ≤4KB」） */
export const TASK_TAIL_LIMIT = 4_096;

/** 追加并截尾：超限丢头保留最新 4KB */
function clampTail(text: string): string {
  return text.length <= TASK_TAIL_LIMIT ? text : text.slice(text.length - TASK_TAIL_LIMIT);
}

/** result.detail（JSON 字符串）→ 条目数组：数组本身，或 {items:[...]} /
 *  {<pluralKey>:[...]} 包裹均收（Python 侧并行定稿，形状宽容） */
function parseDetailItems(detail: string | undefined, pluralKey?: string): unknown[] {
  if (typeof detail !== 'string' || detail === '') return [];
  let data: unknown;
  try {
    data = JSON.parse(detail);
  } catch {
    return [];
  }
  if (Array.isArray(data)) return data;
  if (data !== null && typeof data === 'object') {
    const o = data as Record<string, unknown>;
    for (const k of [pluralKey, 'items', 'list', 'entries']) {
      if (k !== undefined && Array.isArray(o[k])) return o[k] as unknown[];
    }
  }
  return [];
}

function asStr(v: unknown): string | undefined {
  return typeof v === 'string' && v !== '' ? v : undefined;
}

function asNum(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
}

/** 终态字符串归一（宽容：done/ok/success…→done；fail/error…→failed；stop/cancel…→stopped） */
function normTaskStatus(v: unknown): BackgroundTask['status'] {
  const s = typeof v === 'string' ? v.toLowerCase() : '';
  if (['fail', 'failed', 'error', 'crashed'].includes(s)) return 'failed';
  if (['stop', 'stopped', 'cancel', 'cancelled', 'canceled', 'killed'].includes(s)) return 'stopped';
  return 'done'; // done/ok/success/缺省
}

/** tasks_list 回执 → BackgroundTask[]（id 兼容 taskId/task_id；tail/时间戳宽容） */
export function parseTasksDetail(detail?: string): BackgroundTask[] {
  const out: BackgroundTask[] = [];
  for (const x of parseDetailItems(detail, 'tasks')) {
    if (x === null || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    const id = asStr(o.taskId) ?? asStr(o.task_id) ?? asStr(o.id);
    if (id === undefined) continue;
    const status = asStr(o.status);
    out.push({
      id,
      name: asStr(o.name) ?? asStr(o.title) ?? asStr(o.command) ?? id,
      command: asStr(o.command) ?? asStr(o.cmd),
      status: status === 'running' ? 'running' : normTaskStatus(status),
      startedAt: asNum(o.startedAt) ?? asNum(o.started_at) ?? asNum(o.at),
      endedAt: asNum(o.endedAt) ?? asNum(o.ended_at),
      tail: asStr(o.tail) ?? asStr(o.output),
      exitCode: asNum(o.exitCode) ?? asNum(o.exit_code),
      detail: asStr(o.detail) ?? asStr(o.error),
    });
  }
  return out;
}

/** cron_list 回执 → CronEntry[]（name 兼容 id；schedule 兼容 cron/expr；
 *  action 兼容 prompt/command；enabled 宽容缺省 true） */
export function parseCronsDetail(detail?: string): CronEntry[] {
  const out: CronEntry[] = [];
  for (const x of parseDetailItems(detail, 'crons')) {
    if (x === null || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    const name = asStr(o.name) ?? asStr(o.id);
    if (name === undefined) continue;
    out.push({
      name,
      schedule: asStr(o.schedule) ?? asStr(o.cron) ?? asStr(o.expr) ?? '—',
      action: asStr(o.action) ?? asStr(o.prompt) ?? asStr(o.command),
      kind: asStr(o.kind) ?? asStr(o.type),
      enabled: typeof o.enabled === 'boolean' ? o.enabled : typeof o.active === 'boolean' ? o.active : true,
    });
  }
  return out;
}

/** workflows_list 回执 → WorkflowInfo[]（name 兼容 id；description/path 宽容） */
export function parseWorkflowsDetail(detail?: string): WorkflowInfo[] {
  const out: WorkflowInfo[] = [];
  for (const x of parseDetailItems(detail, 'workflows')) {
    if (x === null || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    const name = asStr(o.name) ?? asStr(o.id);
    if (name === undefined) continue;
    out.push({
      name,
      description: asStr(o.description) ?? asStr(o.summary) ?? asStr(o.doc),
      path: asStr(o.path) ?? asStr(o.source) ?? asStr(o.file),
    });
  }
  return out;
}

/** tasks_list 全量快照合并：以回执为准，保留既有任务的尾部输出（帧归约的
 *  增量信息不丢） */
function mergeTasks(prev: BackgroundTask[], next: BackgroundTask[]): BackgroundTask[] {
  return next.map((t) => {
    const old = prev.find((x) => x.id === t.id);
    if (old === undefined) return t;
    return t.tail === undefined && old.tail !== undefined ? { ...t, tail: old.tail } : t;
  });
}

/* ---------- M8：compaction.report 宽容提取 ---------- */

function asCount(v: unknown): number | undefined {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v !== '' && Number.isFinite(Number(v))) return Number(v);
  return undefined;
}

function pickCount(o: Record<string, unknown>, keys: string[]): number | undefined {
  for (const k of keys) {
    const v = asCount(o[k]);
    if (v !== undefined) return v;
  }
  return undefined;
}

function pickText(o: Record<string, unknown>, keys: string[]): string | undefined {
  for (const k of keys) {
    const v = o[k];
    if (typeof v === 'string' && v !== '') return v;
  }
  return undefined;
}

/**
 * compaction{layer, report} → 卡片字段（宽容：多命名兼容 evicted 数字或
 * 列表长度 / savedTokens / beforeTokens / afterTokens / summary；七要素
 * 摘要若为对象则整体 JSON 化保真展示）。
 */
function extractCompaction(
  layer: string,
  report: Record<string, unknown>,
): {
  evicted?: number;
  savedTokens?: number;
  beforeTokens?: number;
  afterTokens?: number;
  summary?: string;
  raw: Record<string, unknown>;
} {
  const o = report ?? {};
  const evictedList = Array.isArray(o.evicted) ? (o.evicted as unknown[]).length : undefined;
  let summary = pickText(o, ['summary', 'summaryText', 'summary_text']);
  if (summary === undefined && o.summary !== null && typeof o.summary === 'object') {
    try {
      summary = JSON.stringify(o.summary, null, 2);
    } catch {
      summary = undefined;
    }
  }
  return {
    evicted: pickCount(o, ['evicted', 'evictedCount', 'evicted_count', 'count']) ?? evictedList,
    savedTokens: pickCount(o, ['savedTokens', 'saved_tokens', 'saved']),
    beforeTokens: pickCount(o, ['beforeTokens', 'before_tokens', 'before']),
    afterTokens: pickCount(o, ['afterTokens', 'after_tokens', 'after']),
    summary,
    raw: o,
  };
}

/**
 * M9 result 回执归约（自描述命令，无需 pendingControl）：
 *  - tasks_list / cron_list / workflows_list：detail JSON 宽容解析覆盖切片，
 *    解锁刷新 busy；!ok 时保留旧表并带错误提示
 *  - task_stop：解锁停止按钮（终态以 task_finished 帧为准）
 *  - cron_add / cron_remove / cron_toggle：提示 + 成功自动回拉列表
 *  - workflow_run：消费 pendingWorkflowRun 对账锚 → 运行态落定 + 报告
 * 返回 true = 已消费（旧闸门跳过）。
 */
function reduceM9Result(
  e: Extract<CoreEvent, { type: 'result' }>,
  get: StoreGet,
  set: StoreSet,
): boolean {
  switch (e.command) {
    case 'tasks_list': {
      set((s) => ({
        tasksRefreshing: false,
        tasks: e.ok ? mergeTasks(s.tasks, parseTasksDetail(e.detail)) : s.tasks,
      }));
      return true;
    }
    case 'task_stop': {
      set((s) => {
        if (Object.keys(s.taskStopping).length === 0) return {};
        return { taskStopping: {} };
      });
      return true;
    }
    case 'cron_list': {
      set((s) => ({
        cronsRefreshing: false,
        crons: e.ok ? parseCronsDetail(e.detail) : s.crons,
        cronNotice: e.ok ? s.cronNotice : { kind: 'error' as const, text: e.detail ?? '定时任务列表获取失败' },
      }));
      return true;
    }
    case 'cron_add':
    case 'cron_remove':
    case 'cron_toggle': {
      set({
        cronPendingAction: undefined,
        cronNotice: {
          kind: e.ok ? 'info' : 'error',
          text: e.detail ?? (e.ok ? '操作成功' : '操作失败'),
        },
      });
      if (e.ok) useStore.getState().refreshCrons();
      return true;
    }
    case 'workflows_list': {
      set((s) => ({
        workflowsRefreshing: false,
        workflows: e.ok ? parseWorkflowsDetail(e.detail) : s.workflows,
        workflowNotice: e.ok ? s.workflowNotice : { kind: 'error' as const, text: e.detail ?? '工作流列表获取失败' },
      }));
      return true;
    }
    case 'workflow_run': {
      const name = get().pendingWorkflowRun;
      set((s) => ({
        pendingWorkflowRun: undefined,
        workflowRuns:
          name !== undefined
            ? {
                ...s.workflowRuns,
                [name]: { status: e.ok ? 'done' : 'failed', report: e.detail, at: Date.now() },
              }
            : s.workflowRuns,
      }));
      return true;
    }
    default:
      return false;
  }
}

/**
 * result 回执归约（M4）：按 pendingControl 对账（result 帧不带 sessionId，
 * command 匹配才消费），处理 export 落盘 / fork / pin / resume 的 busy 与提示。
 * export 的 markdown 经宿主 workspace.saveExport RPC 落盘（R12：核心只产文本）。
 */
function reduceResult(
  e: Extract<CoreEvent, { type: 'result' }>,
  get: StoreGet,
  set: StoreSet,
): void {
  // M9：任务/定时任务/工作流命令的回执自描述（result.command 携带完整
  // 语义）——不依赖 pendingControl 单槽对账，先于旧闸门处理
  if (reduceM9Result(e, get, set)) return;
  const pend = get().pendingControl;
  if (pend === undefined || e.command !== pend.command) return;
  const sessionId = pend.sessionId;
  set({ pendingControl: undefined });
  if (e.command === 'skills_list') {
    // M7：回执即解锁刷新按钮；ok 时以 detail JSON 覆盖 skills 切片（失败保留旧表）
    set((s) => ({ skillsRefreshing: false, skills: e.ok ? parseSkillsDetail(e.detail) : s.skills }));
    return;
  }
  if (e.command === 'mcp_list' || e.command === 'mcp_refresh') {
    // M8：回执即解锁刷新按钮；ok 时以 detail JSON 覆盖 mcpServers（失败保留旧表 + 提示）
    set((s) => ({
      mcpRefreshing: false,
      mcpServers: e.ok ? parseMcpDetail(e.detail) : s.mcpServers,
      mcpNotice: e.ok ? s.mcpNotice : { kind: 'error', text: e.detail ?? 'MCP 列表获取失败' },
    }));
    return;
  }
  if (e.command === 'mcp_add' || e.command === 'mcp_remove') {
    set({
      mcpPendingAction: undefined,
      mcpNotice: {
        kind: e.ok ? 'info' : 'error',
        text:
          e.detail ??
          (e.ok ? (e.command === 'mcp_add' ? '已添加，装配中…' : '已移除') : '操作失败'),
      },
    });
    if (e.ok) {
      // 成功后重拉列表（新服务装配 → 状态灯/工具数同步）
      useStore.getState().refreshMcp('mcp_list');
    }
    return;
  }
  if (e.command === 'export') {
    if (e.ok && e.markdown != null && e.markdown !== '') {
      patchSessionUi(set, sessionId, { busy: undefined, notice: '已生成导出，选择保存位置…', noticeKind: 'info' });
      void hostRequest<{ path?: string; saved?: boolean }>('workspace.saveExport', {
        markdown: e.markdown,
        sessionId,
        fileName: `zcode-${sessionId}.md`,
      })
        .then((r) => {
          set({ hostConnected: true });
          patchSessionUi(set, sessionId, {
            busy: undefined,
            notice: r?.path ? `已导出：${r.path}` : '已导出',
            noticeKind: 'info',
          });
        })
        .catch((err) => {
          set({ hostConnected: classifyRpcError(err) });
          patchSessionUi(set, sessionId, {
            busy: undefined,
            notice: `导出失败：${err instanceof Error ? err.message : String(err)}`,
            noticeKind: 'error',
          });
        });
    } else {
      patchSessionUi(set, sessionId, {
        busy: undefined,
        notice: e.detail ?? '导出失败',
        noticeKind: 'error',
      });
    }
    return;
  }
  if (e.command === 'fork') {
    patchSessionUi(set, sessionId, {
      busy: undefined,
      notice: e.ok ? `已分叉 → ${e.detail ?? '新会话'}` : e.detail ?? '分叉失败',
      noticeKind: e.ok ? 'info' : 'error',
    });
    return;
  }
  if (e.command === 'pin') {
    // 乐观翻转已在 togglePinSession 先行；失败（!ok）回滚
    if (!e.ok) {
      set((s) => ({
        sessions: s.sessions.map((x) => (x.sessionId === sessionId ? { ...x, pinned: !x.pinned } : x)),
      }));
    }
    patchSessionUi(set, sessionId, {
      busy: undefined,
      notice: e.ok ? (e.detail === 'pinned' ? '已收藏' : '已取消收藏') : e.detail ?? '收藏切换失败',
      noticeKind: e.ok ? 'info' : 'error',
    });
    return;
  }
  if (e.command === 'resume') {
    patchSessionUi(set, sessionId, { busy: undefined });
  }
}

export const useStore = create<WebviewStore>()((set, get) => ({
  view: 'chat',
  hostConnected: false,
  setView: (v) => {
    set({ view: v });
    persistUi(get);
  },

  sessions: [],
  activeSessionKey: undefined,
  coreSessionId: undefined,
  processState: undefined,
  sessionsUi: {},
  lastAtMessageId: undefined,
  pendingControl: undefined,
  chat: { items: [], running: false },
  todos: [],
  perf: { phase: 'idle' },
  contextPct: undefined,
  usageTokens: undefined,
  permissionMode: 'build',
  telegraph: FALLBACK_TELEGRAPH,
  providers: [],
  providerUi: {},
  env: null,
  envRefreshing: false,
  skills: [],
  skillsRefreshing: false,
  skillsRequested: false,
  mcpServers: [],
  mcpRefreshing: false,
  mcpRequested: false,
  mcpPendingAction: undefined,
  mcpNotice: undefined,
  tasks: [],
  tasksRefreshing: false,
  tasksRequested: false,
  taskStopping: {},
  crons: [],
  cronsRefreshing: false,
  cronsRequested: false,
  cronPendingAction: undefined,
  cronNotice: undefined,
  workflows: [],
  workflowsRefreshing: false,
  workflowsRequested: false,
  workflowRuns: {},
  pendingWorkflowRun: undefined,
  workflowNotice: undefined,
  inputDraft: '',

  setInputDraft: (text) => {
    set({ inputDraft: text });
    persistUi(get);
  },

  pendingContext: {},

  attachContext: (patch) => {
    set((s) => {
      const cur = s.pendingContext;
      const next: UserMessageContext = { ...cur };
      if (patch.fileRefs !== undefined) {
        const merged = [...(cur.fileRefs ?? []), ...patch.fileRefs];
        next.fileRefs = [...new Set(merged)];
      }
      if (patch.images !== undefined) {
        const mergedImgs = [...(cur.images ?? []), ...patch.images];
        next.images = [...new Set(mergedImgs)];
      }
      if (patch.selection !== undefined) next.selection = patch.selection;
      if (patch.terminalSel !== undefined) next.terminalSel = patch.terminalSel;
      if (patch.problems !== undefined) next.problems = patch.problems;
      return { pendingContext: next };
    });
  },

  removeContextChip: (kind, file) => {
    set((s) => {
      const cur = s.pendingContext;
      const next: UserMessageContext = { ...cur };
      if (kind === 'fileRef') {
        next.fileRefs = (cur.fileRefs ?? []).filter((f) => f !== file);
      } else if (kind === 'image') {
        next.images = (cur.images ?? []).filter((f) => f !== file);
      } else if (kind === 'selection') {
        delete next.selection;
      } else if (kind === 'terminalSel') {
        delete next.terminalSel;
      } else {
        delete next.problems;
      }
      return { pendingContext: next };
    });
  },

  applyHostPrompt: (text, context) => {
    // 宿主预填（编辑器右键/终端选中/Problems）：既有草稿在前、新文本换行接续，
    // 上下文并入 pendingContext，并切到聊天视图（不代发——用户最终确认）
    set((s) => ({
      inputDraft: s.inputDraft ? `${s.inputDraft}\n${text}` : text,
      view: 'chat',
    }));
    if (context !== undefined) {
      useStore.getState().attachContext(context);
    }
  },

  boot: async () => {
    const saved = readPersisted();
    if (saved.view) set({ view: saved.view });
    if (typeof saved.inputDraft === 'string') set({ inputDraft: saved.inputDraft });
    if (saved.permissionMode) set({ permissionMode: saved.permissionMode });
    // 宿主 getState（已注册）：拿 activeSessionKey；ready+replay 由 transport 负责
    try {
      const st = await hostRequest<GetStateResult>('getState', undefined, 4000);
      set({ hostConnected: true, activeSessionKey: st?.activeSessionKey });
    } catch (err) {
      set({ hostConnected: classifyRpcError(err) });
    }
  },

  applyEvent: (e) => {
    switch (e.type) {
      case 'session_started': {
        // 换了核心会话 id 才清空投影（同一会话的重复/重放事件不清）
        set((s) => ({
          coreSessionId: e.sessionId,
          chat: s.coreSessionId === e.sessionId ? s.chat : { items: [], running: false },
          todos: s.coreSessionId === e.sessionId ? s.todos : [],
        }));
        break;
      }
      case 'thinking_delta': {
        set((s) => {
          const items = s.chat.items.slice();
          const last = items[items.length - 1];
          if (last && last.kind === 'thinking' && !last.endedAt) {
            items[items.length - 1] = { ...last, text: last.text + e.text };
          } else {
            items.push({
              kind: 'thinking',
              id: nid('think'),
              at: Date.now(),
              text: e.text,
              startedAt: Date.now(),
            });
          }
          return { chat: { ...s.chat, items } };
        });
        break;
      }
      case 'stream_delta': {
        set((s) => {
          const items = s.chat.items.slice();
          const last = items[items.length - 1];
          // 首段正文到达即闭合思考块（§5 无 thinking_complete，以流分段隐含）
          if (last && last.kind === 'thinking' && !last.endedAt) {
            items[items.length - 1] = { ...last, endedAt: Date.now() };
          }
          const cur = items[items.length - 1];
          if (cur && cur.kind === 'assistant' && cur.streaming) {
            items[items.length - 1] = { ...cur, text: cur.text + e.text };
          } else {
            items.push({
              kind: 'assistant',
              id: nid('a'),
              at: Date.now(),
              text: e.text,
              streaming: true,
            });
          }
          return { chat: { items, running: true } };
        });
        break;
      }
      case 'message_complete': {
        set((s) => {
          const items = s.chat.items.slice();
          for (let i = items.length - 1; i >= 0; i--) {
            const it = items[i];
            if (it.kind === 'assistant') {
              items[i] = { ...it, streaming: false };
              break;
            }
            if (it.kind === 'thinking' && !it.endedAt) items[i] = { ...it, endedAt: Date.now() };
          }
          const usage =
            e.inputTokens != null
              ? { usageTokens: e.inputTokens }
              : {};
          const ctx = currentModelContextLength(get().providers);
          const contextPct =
            e.inputTokens != null && ctx
              ? Math.min(100, Math.round((e.inputTokens / ctx) * 100))
              : s.contextPct;
          // 兜底：perf(done) 缺席时也不能卡在「运行中」
          return { chat: { items, running: false }, ...usage, contextPct };
        });
        break;
      }
      case 'tool_call': {
        // 临时口径：todo 工具的 args.tasks 直接投影为任务清单条（M2 后由正式事件驱动）
        if (TODO_TOOLS.has(e.name.toLowerCase()) && Array.isArray(e.args.tasks)) {
          const todos = (e.args.tasks as TodoItem[]).filter(
            (t) => t && typeof t.content === 'string',
          );
          set({ todos });
          break;
        }
        set((s) => {
          const items = s.chat.items.slice();
          // 交错接线：started 时按事件顺序追加；update/finished 按 callId
          // 原地更新（卡片停留在首次出现的位置，不随完成跳动到流尾）
          const idx = items.findIndex((it) => it.kind === 'tool' && it.callId === e.callId);
          if (idx >= 0) {
            const prev = items[idx] as Extract<ChatItem, { kind: 'tool' }>;
            items[idx] = {
              ...prev,
              args: e.args ?? prev.args,
              state: e.state,
              result: e.result ?? prev.result,
              elapsedMs: e.elapsedMs ?? prev.elapsedMs,
              warning: e.warning ?? prev.warning,
            };
          } else {
            items.push({
              kind: 'tool',
              id: nid('tool'),
              callId: e.callId,
              name: e.name,
              args: e.args,
              state: e.state,
              result: e.result,
              elapsedMs: e.elapsedMs,
              warning: e.warning,
            });
          }
          return { chat: { ...s.chat, items } };
        });
        break;
      }
      case 'permission_request': {
        // 交错接线：items 数组即转录事件流顺序（UI 只追加/原地更新，
        // 从不重排），权限卡因此天然落在「触发它的工具调用」与后续流式
        // 文本之间。仅追加条目 —— 不触碰 inputDraft、不动 running。
        set((s) => ({
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              { kind: 'permission', id: e.id, at: Date.now(), tool: e.tool, args: e.args },
            ],
          },
        }));
        break;
      }
      case 'diff_proposed': {
        // 同上：按事件到达顺序追加，与消息流正确交错
        set((s) => ({
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              {
                kind: 'diff',
                id: e.id,
                at: Date.now(),
                file: e.file,
                oldText: e.oldText,
                newText: e.newText,
              },
            ],
          },
        }));
        break;
      }
      case 'vision_relay': {
        set((s) => ({
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              {
                kind: 'relay',
                id: nid('relay'),
                at: Date.now(),
                model: e.model,
                preset: e.preset,
                description: e.description,
              },
            ],
          },
        }));
        break;
      }
      case 'telegraph_state': {
        set({
          telegraph: {
            levels: e.levels,
            current: e.current,
            paramTranslation: e.paramTranslation,
          },
        });
        break;
      }
      case 'perf': {
        set((s) => ({
          perf: {
            phase: e.phase,
            ttftMs: e.ttftMs ?? (e.phase === 'streaming' ? s.perf.ttftMs : undefined),
            tokPerSec: e.tokPerSec ?? s.perf.tokPerSec,
            lastTurn: e.lastTurn ?? s.perf.lastTurn,
          },
          chat:
            e.phase === 'done'
              ? { items: closeStreaming(s.chat.items), running: false }
              : { ...s.chat, running: true },
        }));
        break;
      }
      case 'usage': {
        const ctx = currentModelContextLength(get().providers);
        set({
          usageTokens: e.tokens,
          contextPct: ctx ? Math.min(100, Math.round((e.tokens / ctx) * 100)) : undefined,
        });
        break;
      }
      case 'env_info': {
        // M5：刷新回流（env_refresh 发起）即解锁刷新按钮
        set({ env: { hardware: e.hardware, workspaceLangs: e.workspaceLangs }, envRefreshing: false });
        break;
      }
      case 'sessions_list': {
        set({ sessions: e.sessions });
        break;
      }
      case 'ping_result': {
        const pid = e.providerId;
        if (!pid) break;
        set((s) => ({
          providerUi: {
            ...s.providerUi,
            [pid]: {
              ...(s.providerUi[pid] ?? emptyUi()),
              pingBusy: false,
              ping: { ok: e.ok, latencyMs: e.latencyMs, errorKind: e.errorKind, at: Date.now() },
            },
          },
        }));
        break;
      }
      case 'image_artifact': {
        // M6（R8）：截图 artifact 入聊天流（ImageArtifactCard 经宿主 RPC
        // workspace.imageUri 换 <img src>；失败回退路径文本）
        set((s) => ({
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              { kind: 'image', id: nid('img'), at: Date.now(), path: e.path },
            ],
          },
        }));
        break;
      }
      case 'task_started': {
        // M9（R3/§6.5）：后台任务卡 upsert（tasks_list 已见过的任务保留尾部输出）
        set((s) => {
          const idx = s.tasks.findIndex((t) => t.id === e.taskId);
          const tasks = s.tasks.slice();
          if (idx >= 0) {
            const prev = tasks[idx];
            tasks[idx] = {
              ...prev,
              name: e.name ?? prev.name,
              command: e.command ?? prev.command,
              status: 'running',
              startedAt: e.atMs ?? prev.startedAt ?? Date.now(),
              endedAt: undefined,
              exitCode: undefined,
              detail: undefined,
            };
          } else {
            tasks.push({
              id: e.taskId,
              name: e.name ?? e.command ?? e.taskId,
              command: e.command,
              status: 'running',
              startedAt: e.atMs ?? Date.now(),
            });
          }
          return { tasks };
        });
        break;
      }
      case 'task_output': {
        // 增量追加 + 4KB 滚动窗口截尾；未见 task_started 的迟到输出 upsert 占位卡
        set((s) => {
          const exists = s.tasks.some((t) => t.id === e.taskId);
          if (exists) {
            return {
              tasks: s.tasks.map((t) =>
                t.id === e.taskId ? { ...t, tail: clampTail(`${t.tail ?? ''}${e.tail ?? ''}`) } : t,
              ),
            };
          }
          return {
            tasks: [
              ...s.tasks,
              {
                id: e.taskId,
                name: e.taskId,
                status: 'running' as const,
                startedAt: Date.now(),
                tail: clampTail(e.text),
              },
            ],
          };
        });
        break;
      }
      case 'task_finished': {
        set((s) => ({
          tasks: s.tasks.map((t) =>
            t.id === e.taskId
              ? {
                  ...t,
                  status: normTaskStatus(e.status),
                  endedAt: Date.now(),
                  exitCode: e.exitCode ?? t.exitCode,
                  detail: e.detail ?? t.detail,
                }
              : t,
          ),
        }));
        break;
      }
      case 'error': {
        // M8 宽容：permission_mode 未被核心识别时回 error 帧 —— 本地 UI 态
        // 已先行生效（setPermissionMode 乐观切换），此类错误不入聊天流
        // （不打断交互；Python 侧并行实现落地后帧自然消失）
        if (e.message.includes('permission_mode')) break;
        // M9 宽容同款：任务/定时任务/工作流/补全命令未被核心识别（Python
        // 并行定稿期常态）——解锁对应页签 busy 并把提示带过去，不入聊天流
        // （页签首开即发列表命令，进聊天流会被 unknown control 噪音淹没）
        if (
          e.message.includes('tasks_list') ||
          e.message.includes('task_stop') ||
          e.message.includes('cron_') ||
          e.message.includes('workflow') ||
          e.message.includes('inline_complete')
        ) {
          const patch: Partial<WebviewStore> = {};
          if (e.message.includes('tasks_list')) patch.tasksRefreshing = false;
          if (e.message.includes('task_stop')) patch.taskStopping = {};
          if (e.message.includes('cron_')) {
            patch.cronsRefreshing = false;
            patch.cronPendingAction = undefined;
            patch.cronNotice = { kind: 'error', text: e.message };
          }
          if (e.message.includes('workflow')) {
            patch.workflowsRefreshing = false;
            patch.workflowNotice = { kind: 'error', text: e.message };
            const name = get().pendingWorkflowRun;
            if (name !== undefined) {
              patch.pendingWorkflowRun = undefined;
              patch.workflowRuns = {
                ...get().workflowRuns,
                [name]: { status: 'failed', report: e.message, at: Date.now() },
              };
            }
          }
          set(patch);
          break;
        }
        set((s) => ({
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              { kind: 'error', id: nid('err'), at: Date.now(), errorKind: e.kind, message: e.message },
            ],
          },
        }));
        // M7：skills_list 未被核心识别（Python 侧并行实现中会回 unknown control
        // command error 帧）时立即解锁刷新按钮——不等 15s 兜底
        if (get().skillsRefreshing && e.message.includes('skills_list')) {
          set({ skillsRefreshing: false });
        }
        // M8：mcp_* 同款——错误帧立即解锁 busy 并把提示带进 MCP 页签
        if (e.message.includes('mcp_')) {
          set((s) => ({
            mcpRefreshing: false,
            mcpPendingAction: undefined,
            mcpNotice: { kind: 'error', text: e.message },
          }));
        }
        break;
      }
      case 'checkpoint': {
        // atMessageId 与入站 user_message 的 messageId 同源（main.py 生成同一
        // id 后先落转录再广播 checkpoint）——这是 UI 侧唯一可得的分叉点真实
        // 标识，forkSession 据此发 control fork
        set({ lastAtMessageId: e.atMessageId });
        break;
      }
      case 'result': {
        reduceResult(e, get, set);
        break;
      }
      case 'compaction': {
        // M8（R14/§3.4）：L2 机械淘汰 / L3 自动摘要 → 压缩报告卡入聊天流；
        // L1 源头治理随工具系统无独立帧、L4 冷存（转录在盘）无卡。
        const norm = e.layer.toUpperCase().startsWith('L') ? e.layer.toUpperCase() : `L${e.layer}`;
        if (norm !== 'L2' && norm !== 'L3') break;
        const f = extractCompaction(norm, e.report ?? {});
        // ContextMeter 校正：L2/L3 后按 report.afterTokens（缺省时
        // before-saved 推导）；无任何 token 字段则仅记校正锚不改动数值
        const after =
          f.afterTokens !== undefined
            ? f.afterTokens
            : f.beforeTokens !== undefined && f.savedTokens !== undefined
              ? Math.max(0, Math.round(f.beforeTokens - f.savedTokens))
              : undefined;
        const ctx = currentModelContextLength(get().providers);
        const patch: Partial<WebviewStore> = {
          lastCompaction: { layer: norm, afterTokens: after, at: Date.now() },
        };
        if (after !== undefined) {
          patch.usageTokens = after;
          patch.contextPct = ctx ? Math.min(100, Math.round((after / ctx) * 100)) : undefined;
        }
        set((s) => ({
          ...patch,
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              { kind: 'compaction', id: nid('compact'), at: Date.now(), layer: norm, ...f },
            ],
          },
        }));
        break;
      }
      case 'rollback_result':
      case 'git_result':
      case 'echo':
        // 尚不消费：git=M4 宿主侧、echo=M0 遗留帧。
        // 事件到达即忽略（不报错）。
        break;
    }
  },

  applyHostError: (e) => {
    set((s) => ({
      chat: {
        ...s.chat,
        items: [
          ...s.chat.items,
          { kind: 'error', id: nid('hosterr'), at: Date.now(), errorKind: e.errorKind, message: e.message },
        ],
      },
    }));
  },

  applyProcessState: (e) => {
    set({ processState: e });
  },

  sendUserMessage: (text, context) => {
    const t = text.trim();
    if (!t) return;
    const merged = { ...get().pendingContext, ...(context ?? {}) };
    set((s) => ({
      inputDraft: '',
      pendingContext: {},
      chat: {
        ...s.chat,
        items: [...s.chat.items, { kind: 'user', id: nid('u'), at: Date.now(), text: t, context: merged }],
      },
    }));
    persistUi(get);
    hostSendCore({ type: 'user_message', text: t, context: merged });
  },

  newSession: () => {
    // 本地乐观清空；核心 session_started 会带回新 sessionId
    set({ chat: { items: [], running: false }, todos: [], perf: { phase: 'idle' } });
    hostSendCore({ type: 'control', command: 'new_session' });
  },

  cancelRunning: () => {
    hostSendCore({ type: 'control', command: 'cancel' });
  },

  activateSession: (sessionId) => {
    // 已是当前投影会话：仅切视图（避免重复 resume 造成转录重放翻倍）
    if (get().coreSessionId !== sessionId) {
      set((s) => ({
        sessionsUi: { ...s.sessionsUi, [sessionId]: { ...s.sessionsUi[sessionId], busy: 'resume' } },
        pendingControl: { command: 'resume', sessionId, at: Date.now() },
      }));
      // 顺序：先 resume（发往当前活跃进程，核心绑定该会话并重放转录 ——
      // session_started 到达时 store 切投影），再 session.activate（宿主把
      // 活跃 sessionKey 指到绑定该会话的进程；并行多开 R16。方法名调用，
      // 宿主侧并行实现，未注册时静默降级 —— 路由仍走默认活跃会话）
      hostSendCore({ type: 'control', command: 'resume', sessionId });
      void hostRequest('session.activate', { sessionId })
        .then(() => set({ hostConnected: true }))
        .catch((err) => set({ hostConnected: classifyRpcError(err) }));
      armSessionBusyTimeout(sessionId, 'resume');
    }
    set({ view: 'chat' });
    persistUi(get);
  },

  forkSession: (sessionId) => {
    const s = get();
    // 分叉点（R16）：UI 侧没有转录内 messageId —— 当前会话回退到最近一次
    // checkpoint 的 atMessageId（与入站 user_message messageId 同源，即
    // 「最后一条用户消息」）；非当前会话用哨兵 FORK_AT_LAST_USER_MESSAGE
    // （约定字段：核心侧未支持时回 not_found 错误，协议字段待补）
    const atMessageId =
      sessionId === s.coreSessionId && s.lastAtMessageId != null
        ? s.lastAtMessageId
        : FORK_AT_LAST_USER_MESSAGE;
    set((st) => ({
      sessionsUi: { ...st.sessionsUi, [sessionId]: { ...st.sessionsUi[sessionId], busy: 'fork', notice: undefined } },
      pendingControl: { command: 'fork', sessionId, at: Date.now() },
    }));
    hostSendCore({ type: 'control', command: 'fork', sessionId, atMessageId });
    armSessionBusyTimeout(sessionId, 'fork');
  },

  togglePinSession: (sessionId) => {
    const cur = get().sessions.find((x) => x.sessionId === sessionId)?.pinned ?? false;
    // 乐观翻转：core pin 只回 result 不回 sessions_list（置顶渲染是 UI 策略，
    // sessions.list_sessions 注释）——本地先行，result !ok 时归约器回滚
    set((s) => ({
      sessions: s.sessions.map((x) => (x.sessionId === sessionId ? { ...x, pinned: !cur } : x)),
      sessionsUi: { ...s.sessionsUi, [sessionId]: { ...s.sessionsUi[sessionId], busy: 'pin' } },
      pendingControl: { command: 'pin', sessionId, at: Date.now() },
    }));
    hostSendCore({ type: 'control', command: 'pin', sessionId, pinned: !cur });
    armSessionBusyTimeout(sessionId, 'pin');
  },

  exportSession: (sessionId) => {
    // R12：control export → result{command:'export', markdown} → 宿主
    // workspace.saveExport 落盘（归约在 reduceResult；核心不替用户选路径）
    set((s) => ({
      sessionsUi: { ...s.sessionsUi, [sessionId]: { ...s.sessionsUi[sessionId], busy: 'export', notice: undefined } },
      pendingControl: { command: 'export', sessionId, at: Date.now() },
    }));
    hostSendCore({ type: 'control', command: 'export', sessionId });
    armSessionBusyTimeout(sessionId, 'export');
  },

  refreshEnv: () => {
    if (get().envRefreshing) return;
    // M5/R21：核心重新探测并广播 env_info（归约清除 envRefreshing）；
    // 核心未实现该命令时回 error 帧，兜底超时解锁按钮
    set({ envRefreshing: true });
    hostSendCore({ type: 'control', command: 'env_refresh' });
    window.setTimeout(() => {
      if (useStore.getState().envRefreshing) {
        useStore.setState({ envRefreshing: false });
      }
    }, 15_000);
  },

  refreshSkills: () => {
    if (get().skillsRefreshing) return;
    // M7/R3：control skills_list → result{command:'skills_list', detail:JSON}
    // （reduceResult 对账消费 → skills 切片）。核心未实现/命令非法时回 error
    // 帧（error 归约里按 skills_list 字样立即解锁）+ 15s 兜底。
    set({
      skillsRefreshing: true,
      skillsRequested: true,
      pendingControl: { command: 'skills_list', sessionId: get().coreSessionId ?? '', at: Date.now() },
    });
    hostSendCore({ type: 'control', command: 'skills_list' });
    window.setTimeout(() => {
      if (useStore.getState().skillsRefreshing) {
        useStore.setState({ skillsRefreshing: false });
      }
    }, 15_000);
  },

  refreshMcp: (command = 'mcp_list') => {
    if (get().mcpRefreshing) return;
    // M8：control mcp_list（首开/添加后回拉）或 mcp_refresh（「↻ 刷新」重连
    // 重扫）→ result.detail JSON（reduceResult 对账 → mcpServers 切片）。
    // 核心未实现/命令非法时回 error 帧（error 归约按 mcp_ 字样立即解锁）
    // + 15s 兜底（沿 skills 同款模式）。
    set({
      mcpRefreshing: true,
      mcpRequested: true,
      pendingControl: { command, sessionId: get().coreSessionId ?? '', at: Date.now() },
    });
    hostSendCore({ type: 'control', command });
    window.setTimeout(() => {
      if (useStore.getState().mcpRefreshing) {
        useStore.setState({ mcpRefreshing: false });
      }
    }, 15_000);
  },

  addMcpServer: ({ name, command, env }) => {
    if (get().mcpPendingAction !== undefined) return;
    // M8：control mcp_add —— 提案载荷顶层 name/args/env + query JSON 兼容
    // 通道双发（protocol.ts CoreSend 注释：Control.query 为现有协议字段，
    // 今天即可无损穿透 Python decode；专用字段定稿前核心从 query 取参）。
    // 回执经 reduceResult：成功自动 mcp_list 回拉；15s 兜底解锁。
    set({
      mcpPendingAction: 'add',
      mcpNotice: undefined,
      pendingControl: { command: 'mcp_add', sessionId: get().coreSessionId ?? '', at: Date.now() },
    });
    hostSendCore({
      type: 'control',
      command: 'mcp_add',
      name,
      args: command,
      env,
      query: JSON.stringify({
        name,
        command,
        argv: command.split(/\s+/).filter(Boolean),
        env,
      }),
    });
    window.setTimeout(() => {
      if (useStore.getState().mcpPendingAction === 'add') {
        useStore.setState({ mcpPendingAction: undefined });
      }
    }, 15_000);
  },

  removeMcpServer: (name) => {
    if (get().mcpPendingAction !== undefined) return;
    // M8：control mcp_remove —— 同 add 的双通道载荷约定
    set({
      mcpPendingAction: 'remove',
      mcpNotice: undefined,
      pendingControl: { command: 'mcp_remove', sessionId: get().coreSessionId ?? '', at: Date.now() },
    });
    hostSendCore({
      type: 'control',
      command: 'mcp_remove',
      name,
      query: JSON.stringify({ name }),
    });
    window.setTimeout(() => {
      if (useStore.getState().mcpPendingAction === 'remove') {
        useStore.setState({ mcpPendingAction: undefined });
      }
    }, 15_000);
  },

  /* ---------- M9：任务 / 定时任务 / 工作流（回执自描述，reduceM9Result 消费） ---------- */

  refreshTasks: () => {
    if (get().tasksRefreshing) return;
    // control tasks_list → result{command:'tasks_list', detail:JSON}（宽容
    // 解析 mergeTasks）。核心未实现时回 error 帧（上方 error 归约立即解锁）
    // + 15s 兜底，沿 skills/mcp 同款模式。
    set({ tasksRefreshing: true, tasksRequested: true });
    hostSendCore({ type: 'control', command: 'tasks_list' });
    window.setTimeout(() => {
      if (useStore.getState().tasksRefreshing) {
        useStore.setState({ tasksRefreshing: false });
      }
    }, 15_000);
  },

  stopTask: (taskId) => {
    if (get().taskStopping[taskId]) return;
    // control task_stop —— 双通道载荷（taskId/name 专用字段 + query JSON）；
    // 终态以 task_finished 帧为准，result/error/15s 兜底只解锁按钮
    set((s) => ({ taskStopping: { ...s.taskStopping, [taskId]: true } }));
    hostSendCore({
      type: 'control',
      command: 'task_stop',
      taskId,
      name: taskId,
      query: JSON.stringify({ taskId }),
    });
    window.setTimeout(() => {
      if (useStore.getState().taskStopping[taskId]) {
        const next = { ...useStore.getState().taskStopping };
        delete next[taskId];
        useStore.setState({ taskStopping: next });
      }
    }, 15_000);
  },

  refreshCrons: () => {
    if (get().cronsRefreshing) return;
    // control cron_list → result.detail JSON（parseCronsDetail 宽容解析）
    set({ cronsRefreshing: true, cronsRequested: true });
    hostSendCore({ type: 'control', command: 'cron_list' });
    window.setTimeout(() => {
      if (useStore.getState().cronsRefreshing) {
        useStore.setState({ cronsRefreshing: false });
      }
    }, 15_000);
  },

  addCron: ({ schedule, prompt, command }) => {
    if (get().cronPendingAction !== undefined) return;
    const kind: 'prompt' | 'command' =
      command !== undefined && command !== '' ? 'command' : 'prompt';
    // control cron_add —— 契约载荷（core _control_cron_add）：schedule（标准
    // 5 段）+ action {type:'prompt',text} | {type:'task',command}；query JSON
    // 镜像兜底；命令型动作在核心侧过权限门（新增时即校验，无人值守不弹卡）
    const action =
      kind === 'prompt'
        ? { type: 'prompt' as const, text: prompt ?? '' }
        : { type: 'task' as const, command: command ?? '' };
    set({ cronPendingAction: 'add', cronNotice: undefined });
    hostSendCore({
      type: 'control',
      command: 'cron_add',
      schedule,
      action,
      query: JSON.stringify({ schedule, kind, prompt, command }),
    });
    window.setTimeout(() => {
      if (useStore.getState().cronPendingAction === 'add') {
        useStore.setState({ cronPendingAction: undefined });
      }
    }, 15_000);
  },

  removeCron: (name) => {
    if (get().cronPendingAction !== undefined) return;
    const entry = get().crons.find((c) => c.name === name);
    set({ cronPendingAction: 'remove', cronNotice: undefined });
    hostSendCore({
      type: 'control',
      command: 'cron_remove',
      cronId: entry?.id ?? name,
      name,
      query: JSON.stringify({ cronId: entry?.id, name }),
    });
    window.setTimeout(() => {
      if (useStore.getState().cronPendingAction === 'remove') {
        useStore.setState({ cronPendingAction: undefined });
      }
    }, 15_000);
  },

  toggleCron: (name, enabled) => {
    if (get().cronPendingAction !== undefined) return;
    // 乐观翻转 + control cron_toggle；!ok / error 由成功后的回拉纠正
    set((s) => ({
      crons: s.crons.map((c) => (c.name === name ? { ...c, enabled } : c)),
      cronPendingAction: 'toggle',
      cronNotice: undefined,
    }));
    hostSendCore({
      type: 'control',
      command: 'cron_toggle',
      cronId: get().crons.find((c) => c.name === name)?.id ?? name,
      name,
      enabled,
      query: JSON.stringify({ name, enabled }),
    });
    window.setTimeout(() => {
      if (useStore.getState().cronPendingAction === 'toggle') {
        useStore.setState({ cronPendingAction: undefined });
      }
    }, 15_000);
  },

  refreshWorkflows: () => {
    if (get().workflowsRefreshing) return;
    // control workflows_list → result.detail JSON（parseWorkflowsDetail 宽容解析）
    set({ workflowsRefreshing: true, workflowsRequested: true });
    hostSendCore({ type: 'control', command: 'workflows_list' });
    window.setTimeout(() => {
      if (useStore.getState().workflowsRefreshing) {
        useStore.setState({ workflowsRefreshing: false });
      }
    }, 15_000);
  },

  runWorkflow: (name) => {
    if (get().pendingWorkflowRun !== undefined) return; // 单飞
    // control workflow_run —— 异步运行：立即标 running（运行态沿本切片 +
    // 工具卡），result.detail JSON 报告到达（reduceM9Result 消费对账锚）
    // 或 error 帧 / 60s 兜底落定
    set((s) => ({
      pendingWorkflowRun: name,
      workflowRuns: { ...s.workflowRuns, [name]: { status: 'running', at: Date.now() } },
    }));
    hostSendCore({
      type: 'control',
      command: 'workflow_run',
      name,
      query: JSON.stringify({ name }),
    });
    window.setTimeout(() => {
      if (useStore.getState().pendingWorkflowRun === name) {
        useStore.setState((s) => ({
          pendingWorkflowRun: undefined,
          workflowRuns: {
            ...s.workflowRuns,
            [name]: {
              status: 'failed',
              report: '运行回执超时（核心未实现 workflow_run 或无活跃会话）',
              at: Date.now(),
            },
          },
        }));
      }
    }, 60_000);
  },

  respondPermission: (id, decision, rule) => {
    // 投影记录决策（+规则文本），权限卡据此切换为「已处理」态。
    // 不变量（PLAN §6.3 接线要求）：权限卡的出现与应答都不触碰
    // inputDraft / 不 disable 输入框 / 不抢焦点 —— 用户输入不被打断。
    set((s) => ({
      chat: {
        ...s.chat,
        items: s.chat.items.map((it) =>
          it.kind === 'permission' && it.id === id ? { ...it, decision, rule } : it,
        ),
      },
    }));
    hostSendCore(
      rule != null && rule !== ''
        ? { type: 'permission_response', id, decision, rule }
        : { type: 'permission_response', id, decision },
    );
  },

  reviewDiff: (id, accepted) => {
    set((s) => ({
      chat: {
        ...s.chat,
        items: s.chat.items.map((it) =>
          it.kind === 'diff' && it.id === id ? { ...it, accepted } : it,
        ),
      },
    }));
    hostSendCore({ type: 'diff_review', id, accepted });
  },

  setTelegraphLevel: (id) => {
    const tg = get().telegraph;
    const lv = tg.levels.find((l) => l.id === id);
    if (!lv || !lv.ok || id === tg.current) return;
    set({ telegraph: { ...tg, current: id } });
    hostSendCore({ type: 'control', command: 'telegraph', level: id });
  },

  switchModel: (providerId, modelId) => {
    set((s) => ({
      providers: s.providers.map((p) =>
        p.id === providerId ? { ...p, selectedModel: modelId } : p,
      ),
    }));
    hostSendCore({ type: 'control', command: 'switch_model', providerId, modelId });
    // 期待核心随后下发 telegraph_state（换模型自动降档，PLAN R6）
  },

  setPermissionMode: (mode) => {
    // M8：本地乐观生效 + 同步核心（control permission_mode{mode}，Python 侧
    // 并行实现；宿主 ControlCommand 枚举暂缺但转发宽容）。核心未识别时回
    // error 帧 —— error 归约按宽容约定静默（不入聊天流），UI 即刻生效的
    // 本地态不被打断。
    set({ permissionMode: mode });
    persistUi(get);
    hostSendCore({ type: 'control', command: 'permission_mode', mode });
  },

  saveProvider: async (p, newApiKey) => {
    // 本地投影不落 Key（Key 只走 SecretStorage 代理，research §3）
    set((s) => {
      const exists = s.providers.some((x) => x.id === p.id);
      return {
        providers: exists ? s.providers.map((x) => (x.id === p.id ? p : x)) : [...s.providers, p],
      };
    });
    try {
      await hostRequest('providers.save', { ...p, apiKey: newApiKey });
      set({ hostConnected: true });
    } catch (err) {
      set({ hostConnected: classifyRpcError(err) });
    }
  },

  deleteProvider: (id) => {
    set((s) => ({ providers: s.providers.filter((p) => p.id !== id) }));
    void hostRequest('providers.delete', { id }).catch(() => undefined);
  },

  setDefaultProvider: (id) => {
    set((s) => ({
      providers: s.providers.map((p) => ({ ...p, isDefault: p.id === id })),
    }));
    void hostRequest('providers.set_default', { id }).catch(() => undefined);
  },

  pingProvider: async (id) => {
    set((s) => ({
      providerUi: {
        ...s.providerUi,
        [id]: { ...(s.providerUi[id] ?? emptyUi()), pingBusy: true },
      },
    }));
    let result: PingResult & { at: number };
    try {
      const r = await hostRequest<PingResult>('provider.ping', { providerId: id });
      set({ hostConnected: true });
      result = { ...r, at: Date.now() };
    } catch (err) {
      set({ hostConnected: classifyRpcError(err) });
      result = err instanceof RpcTimeoutError
        ? { ok: false, errorKind: 'host_unreachable', at: Date.now() }
        : {
            ok: false,
            errorKind: 'rpc_error',
            message: err instanceof Error ? err.message : String(err),
            at: Date.now(),
          };
    }
    set((s) => ({
      providerUi: {
        ...s.providerUi,
        [id]: { ...(s.providerUi[id] ?? emptyUi()), pingBusy: false, ping: result },
      },
    }));
  },

  fetchModels: async (id) => {
    set((s) => ({
      providerUi: {
        ...s.providerUi,
        [id]: { ...(s.providerUi[id] ?? emptyUi()), modelsBusy: true, modelsError: undefined },
      },
    }));
    let models: ProviderConfig['models'] = [];
    let error: string | undefined;
    try {
      const r = await hostRequest<{ models: ProviderConfig['models'] }>('provider.list_models', {
        providerId: id,
      });
      set({ hostConnected: true });
      models = Array.isArray(r?.models) ? r.models : [];
    } catch (err) {
      set({ hostConnected: classifyRpcError(err) });
      error = err instanceof RpcTimeoutError ? '宿主未连接' : err instanceof Error ? err.message : String(err);
    }
    set((s) => ({
      providers: s.providers.map((p) =>
        p.id === id
          ? {
              ...p,
              models,
              selectedModel:
                p.selectedModel && models.some((m) => m.id === p.selectedModel)
                  ? p.selectedModel
                  : models[0]?.id,
            }
          : p,
      ),
      providerUi: {
        ...s.providerUi,
        [id]: { ...(s.providerUi[id] ?? emptyUi()), modelsBusy: false, modelsError: error },
      },
    }));
  },
}));

/**
 * busy 兜底解锁：control 回执（result）丢失或核心未实现该命令（回 error
 * 帧、无 result）时，避免会话条目/刷新按钮卡在 busy 态（15s 单飞超时）。
 */
function armSessionBusyTimeout(sessionId: string, kind: SessionUiState['busy']): void {
  window.setTimeout(() => {
    const cur = useStore.getState().sessionsUi[sessionId];
    if (cur?.busy === kind) {
      useStore.setState((s) => ({
        sessionsUi: { ...s.sessionsUi, [sessionId]: { ...cur, busy: undefined } },
      }));
    }
  }, 15_000);
}
