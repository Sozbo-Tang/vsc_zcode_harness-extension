/**
 * 单一 zustand store（PLAN §4 前端约定）：view / sessions / activeSession /
 * providers / env / tasks 切片 + 事件归约器 applyEvent。
 *
 * 归约器思路参照 ZCode 事件溯源 UI 协议（docs/research/zcode-semantics.md：
 * 统一信封事件 → 纯函数归约成只读投影）：核 → 壳事件（protocol.ts CoreEvent，
 * 经宿主 {kind:'core'} 转发）进来后只增改 items 投影，视图层纯消费。
 * 轻量 UI 态（当前视图/输入草稿/权限模式）经 vscode getState/setState 注水
 * 兜底（research §1.4 / §7 决策 2）；真相态以宿主 ready+replay 重放与事件流为准。
 *
 * M4 补完（并行会话独立投影）：applyEvent 按事件信封的宿主会话键路由——
 * 活跃会话归约进顶层切片（视图层零改动），外来会话归约进 sessionProjections
 * 保留投影（换入/换出复用同一归约路径；上限最近 20 个 LRU）。切回任一会话
 * 走 resume → session_started → replay 重建（转录是真相源）。
 */
import { create } from 'zustand';
import { RpcTimeoutError, getVsCodeApi, hostRequest, hostSendCore } from '../host/transport';
import { useI18nStore, resolveLocale, setLocale, t } from '../i18n';
import type {
  BackgroundTask,
  CoreEvent,
  CoreSettings,
  CronEntry,
  GetStateResult,
  GitFileStat,
  GitPreviewPayload,
  HardwareInfo,
  McpServerInfo,
  McpToolInfo,
  PerfTurn,
  PermissionMode,
  PingResult,
  ProviderConfig,
  SessionEnsureResult,
  SessionActivateResult,
  SessionSummary,
  SkillInfo,
  ToolInfo,
  TelegraphLevel,
  TodoItem,
  UsageModelBucket,
  UserMessageContext,
  VisionRelayPreset,
  WorkflowInfo,
  WorkspaceLang,
} from '../protocol';
import { FORK_AT_LAST_USER_MESSAGE } from '../protocol';

/* ---------- 聊天条目投影（由事件归约产生） ---------- */

export type ChatItem =
  | {
      kind: 'user';
      id: string;
      at: number;
      text: string;
      context?: UserMessageContext;
      /**
       * R30「回滚到这」：该消息触发的 git 检查点（checkpoint 帧倒序回填，
       * 见 applyEvent 'checkpoint'）。无检查点（无 git 仓库 / 快照失败 /
       * cron 注入的消息）缺省 → 不显示回滚按钮。
       */
      checkpointId?: string;
      checkpointSha?: string;
    }
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
  | { kind: 'error'; id: string; at: number; errorKind: string; message: string }
  /**
   * R30：中性系统提示行（回滚成功等非错误回执）。与 error（syserr 红样式）
   * 区分——成功反馈不该长着错误脸。
   */
  | { kind: 'system'; id: string; at: number; text: string };

export interface PerfState {
  phase: 'idle' | 'streaming' | 'done';
  ttftMs?: number;
  tokPerSec?: number;
  /** 上一轮（空回合为 null 值字段，见 protocol.PerfTurn 判空说明） */
  lastTurn?: PerfTurn;
}

/**
 * R19 会话统计：按模型聚合的每轮性能（PerfChip 弹层数据源）。
 *  - 首 token 耗时：平均 = ttftSum/turns；最差 = ttftWorst（最慢一次）。
 *    「峰值」与最差同值（延迟的极值即最差），弹层不重复列。
 *  - 输出速度：平均 = tpsSum/turns（回合均速的均值）；最差 = tpsWorst
 *    （最低回合均速，Infinity = 尚无完整回合）；峰值 = tpsPeak（streaming
 *    采样帧的瞬时最高，比回合均值更接近瞬时尖峰）。
 * 数据在事件归约时增量累计（perf 帧带 model 分组键，含 resume 重放的
 * 历史帧——转录逐轮记录，聚合即「从转录聚合」的 UI 侧等价物）。
 */
export interface PerfModelStats {
  /** 计入统计的完整回合数（lastTurn 双值非 null 才计） */
  turns: number;
  ttftSum: number;
  ttftWorst: number;
  tpsSum: number;
  tpsWorst: number;
  tpsPeak: number;
}

export const emptyPerfStats = (): PerfModelStats => ({
  turns: 0,
  ttftSum: 0,
  ttftWorst: 0,
  tpsSum: 0,
  tpsWorst: Number.POSITIVE_INFINITY,
  tpsPeak: 0,
});

/** perf 帧 → perfStats 增量归约（不可变更新；model 缺省归「未知」桶） */
function applyPerfStats(
  prev: Record<string, PerfModelStats>,
  e: Extract<CoreEvent, { type: 'perf' }>,
): Record<string, PerfModelStats> {
  const key = e.model && e.model !== '' ? e.model : '未知';
  const cur = prev[key] ?? emptyPerfStats();
  let next = cur;
  if (e.phase === 'streaming') {
    // 峰值：streaming 采样帧的瞬时速度
    if (typeof e.tokPerSec === 'number' && e.tokPerSec > cur.tpsPeak) {
      next = { ...cur, tpsPeak: e.tokPerSec };
    }
  } else if (e.phase === 'done' && e.lastTurn != null) {
    // 每轮统计：lastTurn 双值非 null 才计（空回合/取消回合为 null）
    const ttft = e.lastTurn.ttftMs;
    const tps = e.lastTurn.avgTokPerSec;
    if (typeof ttft === 'number' && typeof tps === 'number') {
      next = {
        turns: cur.turns + 1,
        ttftSum: cur.ttftSum + ttft,
        ttftWorst: Math.max(cur.ttftWorst, ttft),
        tpsSum: cur.tpsSum + tps,
        tpsWorst: Math.min(cur.tpsWorst, tps),
        tpsPeak: Math.max(cur.tpsPeak, tps),
      };
    }
  }
  return next === cur ? prev : { ...prev, [key]: next };
}

/**
 * usage.byModel 宽容归一：值须为 {input, output} 桶（核侧 main.
 * _accumulate_usage 的形状）；字段缺失按 0 计，形状不对丢该桶（latest-wins
 * 全量快照，坏桶不会残留旧值）。
 */
function normalizeUsageByModel(
  raw: Record<string, UsageModelBucket> | undefined,
): Record<string, UsageModelBucket> {
  const out: Record<string, UsageModelBucket> = {};
  if (raw == null || typeof raw !== 'object') return out;
  for (const [model, v] of Object.entries(raw)) {
    if (v === null || typeof v !== 'object') continue;
    out[model] = {
      input: typeof v.input === 'number' && Number.isFinite(v.input) ? v.input : 0,
      output: typeof v.output === 'number' && Number.isFinite(v.output) ? v.output : 0,
    };
  }
  return out;
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
  busy?: 'resume' | 'fork' | 'pin' | 'export' | 'delete';
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
  /**
   * R15/R20 设置页「通用」组：界面语言。只持久「用户显式选择」——跟随
   * 宿主语言自动解析的结果不落盘（否则首次猜测被固化，宿主语言变更即
   * 失效），门控见 persistUi 的 uiLanguageExplicit。
   */
  uiLanguage?: 'zh' | 'en';
  /** R15/R20 设置页「通用」组：字号缩放百分比（85–150，100 = 不缩放） */
  fontScale?: number;
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
    const { view, inputDraft, permissionMode, uiLanguage, uiLanguageExplicit, fontScale } = get();
    getVsCodeApi().setState({
      view,
      inputDraft,
      permissionMode,
      // 仅显式选择持久（自动跟随宿主语言的结果不固化，见 PersistedUi 注释）
      uiLanguage: uiLanguageExplicit ? uiLanguage : undefined,
      fontScale,
    });
  } catch {
    /* setState 失败不影响功能 */
  }
}

/**
 * 冷启动自愈后再发 core 消息（sendUserMessage / newSession / refreshEnv 共用）：
 * 窗口重载后宿主恢复 activeSessionKey 但 Python 进程不复活，直接发会撞
 * no_active_session / session_not_found（且错误只落聊天记录，设置/环境页
 * 用户完全看不见——表现为「点图标没反应」）。先 ensure：进程健在是宿主侧
 * 一次本地 no-op；不在则宿主新建会话（init 探测即广播 env_info 等）。
 * ensure 失败（如 agentDir 缺失）时宿主已广播错误，此处静默放弃发送。
 */
function ensureSessionThenSend(message: Parameters<typeof hostSendCore>[0]): void {
  hostRequest<SessionEnsureResult>('session.ensure', undefined, 10_000)
    .then(() => {
      hostSendCore(message);
    })
    .catch(() => {
      /* 宿主已广播错误（process_error/host_error），不重复 */
    });
}

/* ---------- 默认值 ---------- */

/**
 * 车钟兜底盘面：宿主尚未下发 telegraph_state 时的中性默认
 * （按 PLAN §3.3 参考值构造；真实盘面永远以模型档案 telegraphLevels 为准）。
 * R20 i18n：挡位名/说明走字典（t 调用时读当前 locale），语言落定后经
 * refreshFallbackTelegraph 重建（仅 telegraphIsFallback 期间）。
 */
function fallbackTelegraph(): TelegraphState {
  return {
    levels: [
      { id: 'off', label: t('telegraph.levelOff'), param: t('telegraph.levelOffParam'), ok: true },
      { id: 'low', label: t('telegraph.levelLow'), param: { budget_tokens: 8192 }, ok: true },
      { id: 'mid', label: t('telegraph.levelMid'), param: { budget_tokens: 24576 }, ok: true },
      { id: 'high', label: t('telegraph.levelHigh'), param: { budget_tokens: 65536 }, ok: true },
    ],
    current: 'mid',
  };
}

/** 初始兜底（模块加载期 locale 已按 navigator 兜底解析） */
const FALLBACK_TELEGRAPH: TelegraphState = fallbackTelegraph();

/** 语言落定后重建兜底盘面（真盘面由 telegraph_state 事件接管，不受影响） */
function refreshFallbackTelegraph(): void {
  if (useStore.getState().telegraphIsFallback) {
    useStore.setState({ telegraph: fallbackTelegraph() });
  }
}

/**
 * M4 补完（并行会话独立投影）：单会话投影的完整快照。活跃会话的投影住在
 * 顶层切片（chat/todos/perf/perfStats/usageByModel，视图层零改动）；非活跃
 * 会话保留在 sessionProjections（key = 核心会话 id），切走时归档、外来事件
 * 持续更新。切回任一会话都走 resume → session_started → replay 重建顶层
 * 投影（转录是真相源，保留表只是并行期间的活缓存，不是恢复源）。
 */
export interface SessionProjection {
  chat: { items: ChatItem[]; running: boolean };
  todos: TodoItem[];
  perf: PerfState;
  /** R19 会话统计（按模型每轮聚合；随会话归档） */
  perfStats: Record<string, PerfModelStats>;
  /** R19 token 累计（latest-wins 快照；随会话归档） */
  usageByModel: Record<string, UsageModelBucket>;
}

/** 保留上限：最近 20 个会话投影防内存膨胀（LRU 淘汰最旧；恢复走 replay） */
export const PROJECTION_RETAIN_LIMIT = 20;

function activeProjectionOf(s: {
  chat: { items: ChatItem[]; running: boolean };
  todos: TodoItem[];
  perf: PerfState;
  perfStats: Record<string, PerfModelStats>;
  usageByModel: Record<string, UsageModelBucket>;
}): SessionProjection {
  return {
    chat: s.chat,
    todos: s.todos,
    perf: s.perf,
    perfStats: s.perfStats,
    usageByModel: s.usageByModel,
  };
}

/** 空投影不归档（避免 newSession 乐观清空后把空态覆盖既有保留） */
function projectionHasContent(p: SessionProjection): boolean {
  return (
    p.chat.items.length > 0 ||
    p.chat.running ||
    p.todos.length > 0 ||
    p.perf.phase !== 'idle' ||
    Object.keys(p.perfStats).length > 0 ||
    Object.keys(p.usageByModel).length > 0
  );
}

/** LRU 触碰：key 挪到序尾（最近使用）；归档与外来更新共用 */
function touchProjectionOrder(order: string[], key: string): string[] {
  const next = order.filter((k) => k !== key);
  next.push(key);
  return next;
}

/**
 * 归档当前活跃会话的投影（M4 补完）：非空才写入保留表（空投影不覆盖既有
 * 保留——newSession 乐观清空后 session_started 再归档时快照已是空态），
 * 超过 PROJECTION_RETAIN_LIMIT 淘汰最旧。返回归档补丁（无需归档 = 空对象）。
 */
function archiveActiveProjection(s: WebviewStore): Partial<WebviewStore> {
  const key = s.coreSessionId;
  if (key === undefined) return {};
  const proj = activeProjectionOf(s);
  if (!projectionHasContent(proj)) return {};
  const projections = { ...s.sessionProjections, [key]: proj };
  const order = touchProjectionOrder(s.projectionOrder, key);
  while (order.length > PROJECTION_RETAIN_LIMIT) {
    const evict = order.shift();
    if (evict !== undefined) delete projections[evict];
  }
  return { sessionProjections: projections, projectionOrder: order };
}

/**
 * 外来会话事件中「属于会话投影」的类型。其余类型（任务表/环境/设置/会话
 * 列表/ping 等）是活跃会话视角的服务级状态——非活跃进程的帧不混入（此前
 * 单投影时代它们会污染活跃视图，正是 M4 遗留问题的一部分）。
 */
const PROJECTION_EVENT_TYPES = new Set<string>([
  'thinking_delta',
  'stream_delta',
  'message_complete',
  'tool_call',
  'permission_request',
  'diff_proposed',
  'vision_relay',
  'perf',
  'usage',
  'image_artifact',
  'checkpoint',
  'compaction',
  'rollback_result',
  'error',
]);

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
  /**
   * R19 会话统计：按模型的每轮 TTFT/速度聚合（perf 帧累计；换会话
   * （session_started 新 id）与 newSession 重置——重放帧随后重建）。
   */
  perfStats: Record<string, PerfModelStats>;
  /** R19：按模型 token 累计（usage 帧 byModel 全量快照，latest-wins） */
  usageByModel: Record<string, UsageModelBucket>;
  contextPct?: number;
  usageTokens?: number;
  /**
   * M8：最近一次压缩校正锚（L2/L3 compaction 帧到达时按 report.afterTokens
   * 校正 usageTokens/contextPct；ContextMeter title 提示校正来源）。
   */
  lastCompaction?: { layer: string; afterTokens?: number; at: number };
  permissionMode: PermissionMode;
  telegraph: TelegraphState;
  /** telegraph 切片仍是兜底盘面（宿主 telegraph_state 未到达；语言落定时重建） */
  telegraphIsFallback: boolean;

  /* ---- M4 补完：并行会话独立投影 ---- */
  /** 非活跃会话的保留投影（key = 核心会话 id；上限见 PROJECTION_RETAIN_LIMIT） */
  sessionProjections: Record<string, SessionProjection>;
  /** 保留顺序（LRU：尾 = 最近使用；超上限淘汰头部） */
  projectionOrder: string[];
  /** 宿主会话键 → 核心会话 id（session_started 信封学习，latest-wins） */
  coreByHostKey: Record<string, string>;
  /** 核心会话 id → 宿主会话键（反查；activateSession 定向 resume/activate 用） */
  hostKeyByCore: Record<string, string>;

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

  /* ---- 切片：tools（R3 工具开关 + 默认权限） ---- */
  /** tools_list 回执投影（ToolsView 工具页签数据源） */
  toolsList: ToolInfo[];
  /** tools_list 已发出、等待回执（刷新按钮禁用；15s 兜底） */
  toolsRefreshing: boolean;
  /** 是否已请求过一次（工具页签首开自动拉取的幂等标记） */
  toolsRequested: boolean;
  /** tool_toggle / tool_permission 进行中（单飞；成功后自动回拉 tools_list） */
  toolActionPending: boolean;
  refreshTools(): void;
  toggleTool(name: string, enabled: boolean): void;
  setToolPermission(name: string, mode: 'ask' | 'always'): void;

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

  /* ---- 切片：settings（R15/R20 设置页） ---- */
  /** 核心受管设置投影（settings_get 回执；null = 未拉取） */
  settings: CoreSettings | null;
  /** settings_get 已发出、等待回执（刷新按钮禁用；15s 兜底） */
  settingsRefreshing: boolean;
  /** settings_set / clear_sessions 进行中（单飞：保存/清除按钮禁用；15s 兜底） */
  settingsSaving: boolean;
  /** 设置页操作回执提示（保存/清除/失败） */
  settingsNotice?: { kind: 'info' | 'error'; text: string };
  /** 会话目录绝对路径（settings_get 附带，「数据」组展示） */
  sessionsDirPath?: string;
  /** 发 control settings_get（设置页首开自动拉取一次） */
  refreshSettings(): void;
  /** 发 control settings_set{settings: patch}（核侧校验 + 持久 + 热应用） */
  saveSettings(patch: Partial<CoreSettings> & { compaction?: Partial<CoreSettings['compaction']> }): void;
  /** 发 control clear_sessions（UI 已二次确认；核侧清空后重建空会话） */
  clearSessionsData(): void;

  /* ---- 切片：git（R17/R22 一键 Commit & Push） ---- */
  /** 面板开关（ChatView ⚡Commit 按钮切换；GitCommitCard 渲染条件） */
  gitPanelOpen: boolean;
  /** workspaceRoots（getState RPC 惰性拉取；multi-root 时仓库下拉数据源） */
  gitRepos: string[];
  /** 当前选中仓库根（缺省第一个 root；核心按 workspaceRoots 白名单校验） */
  gitRepoRoot?: string;
  gitScope: 'staged' | 'all';
  gitPush: boolean;
  /** git_preview 回执投影（ok=false 时 error 即降级文案，卡片内展示） */
  gitPreview?: GitPreviewPayload;
  gitPreviewLoading: boolean;
  /** git_commit / git_pr 执行中（按钮禁用；git_result 帧 / error 帧 / 兜底解锁） */
  gitBusy: boolean;
  /** 最近一次 git_result 帧投影（sha / 可点网址 / PR / error+hint 降级） */
  gitResult?: {
    commitSha?: string;
    remoteUrl?: string;
    commitUrl?: string;
    prUrl?: string;
    error?: string;
    hint?: string;
    at: number;
  };
  /** 打开面板（重置上次回执；workspaceRoots 惰性拉取由组件 useEffect 驱动） */
  openGitPanel(): void;
  closeGitPanel(): void;
  setGitRepo(repoRoot: string): void;
  setGitScope(scope: 'staged' | 'all'): void;
  setGitPush(push: boolean): void;
  /** 发 control git_preview{repoRoot, scope}（回执 reduceM9Result 消费） */
  requestGitPreview(): void;
  /** 发 git_commit 执行帧（message = 预览卡上用户编辑过的提交信息；
   * pushOverride = 「仅提交」按钮强制不推送，缺省按推送勾选走） */
  sendGitCommit(message: string, pushOverride?: boolean): void;
  /** 发 control git_pr（gh pr create；回 git_result 带 prUrl） */
  sendGitPr(): void;

  /* ---- 切片：本地 UI 设置（「通用」组；vscode getState 持久） ---- */
  uiLanguage: 'zh' | 'en';
  /** uiLanguage 是否用户显式选择（true 才随 vscode getState 持久，见 persistUi） */
  uiLanguageExplicit: boolean;
  /** 字号缩放百分比（85–150；App 根节点 font-size，100 = 不缩放） */
  fontScale: number;
  setUiLanguage(lang: 'zh' | 'en'): void;
  setFontScale(scale: number): void;

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
  /**
   * 事件归约入口。sessionKey = 事件信封的宿主会话键（M4 补完）：绑定到
   * 非活跃核心会话时归约进该会话的保留投影（换入→活跃路径→换出，全局
   * 切片快照还原），否则归约进活跃投影。缺省（旧调用方/无信封键）= 活跃。
   */
  applyEvent(e: CoreEvent, sessionKey?: string): void;
  /** 活跃投影事件归约（原 applyEvent 主体，applyEvent 路由后调用；独立
   * 暴露便于外来会话换入/换出复用与测试直呼） */
  reduceActiveEvent(e: CoreEvent): void;
  applyHostError(e: { sessionKey?: string; errorKind: string; message: string }): void;
  applyProcessState(e: { sessionKey: string; state: string; detail?: string }): void;

  sendUserMessage(text: string, context?: UserMessageContext): void;
  newSession(): void;
  cancelRunning(): void;
  /**
   * R30「回滚到这」：发 rollback{checkpointId}（UI 已弹确认；核侧收到即
   * 执行 git checkout <sha> -- .，回执 rollback_result + 失败时 error 帧）。
   * 冷启动自愈与其他 core 消息同款（ensureSessionThenSend）。
   */
  rollbackToCheckpoint(checkpointId: string): void;

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
  /** 删除会话（M4 补完）：control session_delete → result 回执 + 核心广播
   * sessions_list 收敛列表；破坏性操作不做乐观删除，busy 态等回执 */
  deleteSession(sessionId: string): void;
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
/** R3 工具面板回执解析（宽容：数组或 {tools:[...]}；enabled/alwaysAllowed 缺省 false） */
function parseToolsDetail(detail?: string): ToolInfo[] {
  if (typeof detail !== 'string' || detail === '') return [];
  let data: unknown;
  try {
    data = JSON.parse(detail);
  } catch {
    return [];
  }
  const arr = Array.isArray(data)
    ? data
    : data !== null && typeof data === 'object' && Array.isArray((data as { tools?: unknown }).tools)
      ? ((data as { tools: unknown[] }).tools)
      : [];
  const out: ToolInfo[] = [];
  for (const x of arr) {
    if (x === null || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    if (typeof o.name !== 'string' || o.name === '') continue;
    out.push({
      name: o.name,
      description: typeof o.description === 'string' ? o.description : undefined,
      tier: typeof o.tier === 'string' ? o.tier : undefined,
      enabled: o.enabled !== false,
      alwaysAllowed: o.alwaysAllowed === true,
    });
  }
  return out;
}

/**
 * R17/R22：git_preview 回执 result.detail（JSON 字符串）→ GitPreviewPayload。
 * 宽容解析（镜像 Python gitflow.PreviewPayload.to_dict）：summary.files 条目
 * 须有 string path，计数缺省按 files 累加/计数回退；解析失败回 null（调用
 * 方降级为 error 载荷，卡片显示文案而非空白）。
 */
export function parseGitPreview(detail?: string): GitPreviewPayload | null {
  if (typeof detail !== 'string' || detail === '') return null;
  let data: unknown;
  try {
    data = JSON.parse(detail);
  } catch {
    return null;
  }
  if (data === null || typeof data !== 'object') return null;
  const o = data as Record<string, unknown>;
  const summary = (o.summary ?? {}) as Record<string, unknown>;
  const files: GitFileStat[] = [];
  for (const x of Array.isArray(summary.files) ? summary.files : []) {
    if (x === null || typeof x !== 'object') continue;
    const f = x as Record<string, unknown>;
    if (typeof f.path !== 'string' || f.path === '') continue;
    files.push({
      path: f.path,
      status: typeof f.status === 'string' ? f.status : 'M',
      insertions: typeof f.insertions === 'number' ? f.insertions : null,
      deletions: typeof f.deletions === 'number' ? f.deletions : null,
    });
  }
  return {
    ok: o.ok === true,
    repoRoot: typeof o.repoRoot === 'string' ? o.repoRoot : '',
    scope: o.scope === 'all' ? 'all' : 'staged',
    files,
    insertions:
      typeof summary.insertions === 'number'
        ? summary.insertions
        : files.reduce((acc, f) => acc + (f.insertions ?? 0), 0),
    deletions:
      typeof summary.deletions === 'number'
        ? summary.deletions
        : files.reduce((acc, f) => acc + (f.deletions ?? 0), 0),
    untracked:
      typeof summary.untracked === 'number'
        ? summary.untracked
        : files.filter((f) => f.status === '??').length,
    subject: typeof o.subject === 'string' ? o.subject : '',
    body: typeof o.body === 'string' ? o.body : '',
    styleNote: typeof o.styleNote === 'string' ? o.styleNote : '',
    remoteUrl: typeof o.remoteUrl === 'string' ? o.remoteUrl : null,
    remoteKind: typeof o.remoteKind === 'string' ? o.remoteKind : 'none',
    canPr: o.canPr === true,
    ghAvailable: o.ghAvailable === true,
    error: typeof o.error === 'string' ? o.error : null,
  };
}

/**
 * R15/R20：settings_get / settings_set 回执 result.detail（JSON 字符串）→
 * {settings, homeDir, sessionsDir}。双形状宽容：settings_get 是
 * {settings, homeDir, sessionsDir} 包裹；settings_set 直接回生效值（无
 * 包裹）。字段缺失/类型不符回 null（UI 显示空态）；compaction 组与
 * privacyFilePatterns 宽容缺省。
 */
export function parseSettingsDetail(detail?: string): {
  settings: CoreSettings;
  homeDir: string;
  sessionsDir: string;
} | null {
  if (typeof detail !== 'string' || detail === '') return null;
  let data: unknown;
  try {
    data = JSON.parse(detail);
  } catch {
    return null;
  }
  if (data === null || typeof data !== 'object') return null;
  const o = data as Record<string, unknown>;
  const wrapped = o.settings !== null && typeof o.settings === 'object';
  const raw = (wrapped ? o.settings : o) as Record<string, unknown>;
  const compaction = (raw.compaction ?? {}) as Record<string, unknown>;
  const preset = raw.visionRelayPreset;
  const threshold = compaction.summarizeThreshold;
  const maxTurns = raw.maxTurns;
  return {
    settings: {
      systemPromptAppend: typeof raw.systemPromptAppend === 'string' ? raw.systemPromptAppend : '',
      maxTurns: typeof maxTurns === 'number' && Number.isFinite(maxTurns) ? Math.round(maxTurns) : 8,
      compaction: {
        summarizeThreshold:
          typeof threshold === 'number' && Number.isFinite(threshold) && threshold > 0 && threshold <= 1
            ? threshold
            : 0.85,
        compactionModel: typeof compaction.compactionModel === 'string' ? compaction.compactionModel : '',
      },
      visionRelayPreset: (['ocr', 'describe', 'frontend', 'custom'] as const).includes(preset as VisionRelayPreset)
        ? (preset as VisionRelayPreset)
        : 'describe',
      visionRelayCustomPrompt:
        typeof raw.visionRelayCustomPrompt === 'string' ? raw.visionRelayCustomPrompt : '',
      privacyFilePatterns: Array.isArray(raw.privacyFilePatterns)
        ? raw.privacyFilePatterns.filter((p): p is string => typeof p === 'string')
        : [],
      // R6 收尾：车钟咔哒开关缺省开（与核侧 _settings_view 同语义：
      // 只有显式 false 才关，字段缺失/脏值都回落 true）
      telegraphClickSound: raw.telegraphClickSound === false ? false : true,
    },
    homeDir: typeof o.homeDir === 'string' ? o.homeDir : '',
    sessionsDir: typeof o.sessionsDir === 'string' ? o.sessionsDir : '',
  };
}

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
        cronNotice: e.ok ? s.cronNotice : { kind: 'error' as const, text: e.detail ?? t('tasks.cronListFailed') },
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
          text: e.detail ?? (e.ok ? t('common.ok') : t('common.failed')),
        },
      });
      if (e.ok) useStore.getState().refreshCrons();
      return true;
    }
    case 'workflows_list': {
      set((s) => ({
        workflowsRefreshing: false,
        workflows: e.ok ? parseWorkflowsDetail(e.detail) : s.workflows,
        workflowNotice: e.ok ? s.workflowNotice : { kind: 'error' as const, text: e.detail ?? t('tasks.workflowListFailed') },
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
    case 'settings_get': {
      // R15/R20：回执即解锁；ok 时以 detail JSON 覆盖 settings 切片 + 会话目录
      const parsed = e.ok ? parseSettingsDetail(e.detail) : null;
      set((s) => ({
        settingsRefreshing: false,
        settings: parsed?.settings ?? s.settings,
        sessionsDirPath: parsed?.sessionsDir || s.sessionsDirPath,
        settingsNotice: e.ok
          ? undefined
          : { kind: 'error' as const, text: e.detail ?? t('settings.getFailed') },
      }));
      return true;
    }
    case 'settings_set': {
      // 成功回执 detail 即核侧热应用后的生效值（免二次拉取直接覆盖）
      const parsed = e.ok ? parseSettingsDetail(e.detail) : null;
      set((s) => ({
        settingsSaving: false,
        settings: parsed ? { ...parsed.settings } : s.settings,
        settingsNotice: {
          kind: e.ok ? ('info' as const) : ('error' as const),
          text: e.ok ? t('settings.savedApplied') : e.detail ?? t('settings.saveFailed'),
        },
      }));
      return true;
    }
    case 'clear_sessions': {
      set({
        settingsSaving: false,
        settingsNotice: {
          kind: e.ok ? 'info' : 'error',
          text: e.ok ? t('settings.cleared') : e.detail ?? t('settings.clearFailed'),
        },
      });
      return true;
    }
    case 'git_preview': {
      // R17/R22：预览回执 detail JSON → gitPreview 切片（含 ok=false 的降级
      // 载荷——非仓库/scope 非法等文案只在卡片内展示，不进聊天流）
      const parsed = parseGitPreview(e.detail);
      set({
        gitPreviewLoading: false,
        gitPreview:
          parsed ?? {
            ok: false,
            repoRoot: get().gitRepoRoot ?? '',
            scope: get().gitScope,
            files: [],
            insertions: 0,
            deletions: 0,
            untracked: 0,
            subject: '',
            body: '',
            styleNote: '',
            remoteKind: 'none',
            canPr: false,
            ghAvailable: false,
            error: e.detail ?? t('git.previewUnparseable'),
          },
      });
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
  if (e.command === 'tools_list') {
    // R3：回执即解锁；toggle/permission 成功后的自动回拉也走这里刷新
    set((s) => ({ toolsRefreshing: false, toolsList: e.ok ? parseToolsDetail(e.detail) : s.toolsList }));
    return;
  }
  if (e.command === 'tool_toggle' || e.command === 'tool_permission') {
    // 即时回执：解锁并自动回拉 tools_list（核心侧已持久化并热更引擎）
    set({ toolActionPending: false });
    if (e.ok) {
      useStore.getState().refreshTools();
    }
    return;
  }
  if (e.command === 'mcp_list' || e.command === 'mcp_refresh') {
    // M8：回执即解锁刷新按钮；ok 时以 detail JSON 覆盖 mcpServers（失败保留旧表 + 提示）
    set((s) => ({
      mcpRefreshing: false,
      mcpServers: e.ok ? parseMcpDetail(e.detail) : s.mcpServers,
      mcpNotice: e.ok ? s.mcpNotice : { kind: 'error', text: e.detail ?? t('tools.mcpListFailed') },
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
          (e.ok
            ? e.command === 'mcp_add'
              ? t('tools.mcpAdded')
              : t('tools.mcpRemoved')
            : t('common.failed')),
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
      patchSessionUi(set, sessionId, { busy: undefined, notice: t('sessions.exportPreparing'), noticeKind: 'info' });
      void hostRequest<{ path?: string; saved?: boolean }>('workspace.saveExport', {
        markdown: e.markdown,
        sessionId,
        fileName: `zcode-${sessionId}.md`,
      })
        .then((r) => {
          set({ hostConnected: true });
          patchSessionUi(set, sessionId, {
            busy: undefined,
            notice: r?.path ? t('sessions.exportedPath', { path: r.path }) : t('sessions.exported'),
            noticeKind: 'info',
          });
        })
        .catch((err) => {
          set({ hostConnected: classifyRpcError(err) });
          patchSessionUi(set, sessionId, {
            busy: undefined,
            notice: t('sessions.exportFailedPath', {
              error: err instanceof Error ? err.message : String(err),
            }),
            noticeKind: 'error',
          });
        });
    } else {
      patchSessionUi(set, sessionId, {
        busy: undefined,
        notice: e.detail ?? t('sessions.exportFailed'),
        noticeKind: 'error',
      });
    }
    return;
  }
  if (e.command === 'fork') {
    patchSessionUi(set, sessionId, {
      busy: undefined,
      notice: e.ok
        ? t('sessions.forked', { target: e.detail ?? t('sessions.forkTargetNew') })
        : e.detail ?? t('sessions.forkFailed'),
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
      notice: e.ok
        ? e.detail === 'pinned'
          ? t('sessions.pinnedNotice')
          : t('sessions.unpinnedNotice')
        : e.detail ?? t('sessions.pinFailed'),
      noticeKind: e.ok ? 'info' : 'error',
    });
    return;
  }
  if (e.command === 'session_delete') {
    // ok：条目由随后的 sessions_list 广播收敛（回执可能晚于广播，此时条目
    // 已不在列表，patchSessionUi 落在空 ui 上无害）；失败保留提示
    patchSessionUi(set, sessionId, {
      busy: undefined,
      notice: e.ok ? undefined : (e.detail ?? t('sessions.deleteFailed')),
      noticeKind: 'error',
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
  perfStats: {},
  usageByModel: {},
  contextPct: undefined,
  usageTokens: undefined,
  permissionMode: 'build',
  telegraph: FALLBACK_TELEGRAPH,
  telegraphIsFallback: true,
  // M4 补完：并行会话独立投影（保留表 + LRU 序 + 宿主键绑定，均为空起步）
  sessionProjections: {},
  projectionOrder: [],
  coreByHostKey: {},
  hostKeyByCore: {},
  providers: [],
  providerUi: {},
  env: null,
  envRefreshing: false,
  skills: [],
  skillsRefreshing: false,
  toolsList: [],
  toolsRefreshing: false,
  toolsRequested: false,
  toolActionPending: false,
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
  settings: null,
  settingsRefreshing: false,
  settingsSaving: false,
  settingsNotice: undefined,
  sessionsDirPath: undefined,
  // R17/R22：git 切片（repoRoot 在 openGitPanel 后按 getState 回填）
  gitPanelOpen: false,
  gitRepos: [],
  gitRepoRoot: undefined,
  gitScope: 'staged',
  gitPush: false,
  gitPreview: undefined,
  gitPreviewLoading: false,
  gitBusy: false,
  gitResult: undefined,
  // R20：初值与 i18n 模块加载期的 navigator 兜底解析对齐（boot 随后按
  // 持久选择/宿主语言覆写），避免设置页语言下拉首帧闪显默认值
  uiLanguage: useI18nStore.getState().locale,
  uiLanguageExplicit: false,
  fontScale: 100,
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
    // R15/R20 设置页「通用」组：语言 / 字号缩放注水（App 根节点消费）。
    // R20 语言解析链：持久显式选择 > 宿主语言（下方 getState.language，即
    // vscode.env.language）> navigator.language 兜底（i18n 模块加载期已解析
    // 过一次，此处补写主 store 切片保持两处一致）。自动跟随的结果不标
    // explicit、不持久——宿主语言变更在下次启动继续生效。
    if (saved.uiLanguage === 'zh' || saved.uiLanguage === 'en') {
      set({ uiLanguage: saved.uiLanguage, uiLanguageExplicit: true });
      setLocale(saved.uiLanguage);
    } else {
      const nav = resolveLocale(
        typeof navigator !== 'undefined' ? navigator.language : undefined,
      );
      set({ uiLanguage: nav, uiLanguageExplicit: false });
      setLocale(nav);
    }
    refreshFallbackTelegraph();
    if (typeof saved.fontScale === 'number' && Number.isFinite(saved.fontScale)) {
      set({ fontScale: Math.max(85, Math.min(150, Math.round(saved.fontScale))) });
    }
    // 宿主 getState（已注册）：拿 activeSessionKey；ready+replay 由 transport 负责
    try {
      const st = await hostRequest<GetStateResult>('getState', undefined, 4000);
      set({ hostConnected: true, activeSessionKey: st?.activeSessionKey });
      // 未显式选择时跟随宿主显示语言（vscode.env.language；旧宿主缺字段
      // 或值空则维持 navigator 兜底结果）
      const s = useStore.getState();
      if (!s.uiLanguageExplicit && typeof st?.language === 'string' && st.language !== '') {
        const host = resolveLocale(st.language);
        useStore.setState({ uiLanguage: host });
        setLocale(host);
        refreshFallbackTelegraph();
      }
    } catch (err) {
      set({ hostConnected: classifyRpcError(err) });
    }
  },

  applyEvent: (e, sessionKey) => {
    // M4 补完（并行会话独立投影）路由（判定先于绑定学习——活跃进程
    // resume/new_session 换绑的 session_started 若先学习（键→新会话）再路由，
    // 本帧会被误判为外来而丢弃切换）：
    //  - 信封键 = 活跃会话键（hostKeyByCore[coreSessionId]）→ 活跃投影；
    //  - 我方定向 resume 的回执（pendingControl 对账：resume 目标 = 本帧
    //    sessionId，信封键 = 拥有它的进程）→ 活跃投影（这就是切换本身）；
    //  - 其余一律外来：绑定已知 → 更新该核心会话的保留投影；未绑定 → 丢弃
    //    （外来进程 init 的 session_started 只学绑定不抢投影，下一帧起有目标）。
    const s0 = get();
    let foreign = false;
    if (sessionKey !== undefined && s0.coreSessionId !== undefined) {
      const activeHostKey = s0.hostKeyByCore[s0.coreSessionId];
      const isSwitchWeAskedFor =
        e.type === 'session_started' &&
        s0.pendingControl?.command === 'resume' &&
        s0.pendingControl.sessionId === e.sessionId;
      if (sessionKey !== activeHostKey && !isSwitchWeAskedFor) foreign = true;
    }
    if (sessionKey !== undefined && e.type === 'session_started') {
      // 绑定学习（活跃与外来路径都要学）：信封键随每条核 → 壳事件下发
      //（transport dispatchHostMessage）；每个并行会话进程一个键，
      // session_started 告知该进程当前绑定的核心会话（latest-wins——进程
      // resume/new_session 换绑后旧核心会话不再来信）。
      set((s) => {
        const prevBound = s.coreByHostKey[sessionKey];
        const hostKeyByCore = { ...s.hostKeyByCore, [e.sessionId]: sessionKey };
        if (prevBound !== undefined && prevBound !== e.sessionId) {
          delete hostKeyByCore[prevBound];
        }
        return {
          coreByHostKey: { ...s.coreByHostKey, [sessionKey]: e.sessionId },
          hostKeyByCore,
        };
      });
    }
    if (!foreign) {
      useStore.getState().reduceActiveEvent(e);
      return;
    }
    // 外来会话：只处理投影类事件（其余是活跃会话视角的服务级状态，不混入）
    if (!PROJECTION_EVENT_TYPES.has(e.type)) return;
    const bound = sessionKey !== undefined ? s0.coreByHostKey[sessionKey] : undefined;
    const foreignCore =
      bound !== undefined && bound !== s0.coreSessionId ? bound : undefined;
    if (foreignCore === undefined) {
      // 键未绑定（外来进程早期帧，session_started 已学绑定随后即有目标）或
      // 键声索的是活跃会话（双进程同会话的僵尸侧）——无保留目标，丢弃；
      // 切换过去时由 resume replay 按转录重建（恢复链路兼容）
      return;
    }
    const retained = s0.sessionProjections[foreignCore];
    if (retained === undefined) {
      // 从未在本 webview 激活过（或已被 LRU 淘汰）：丢弃，理由同上
      return;
    }
    // 换入保留投影 → 复用活跃归约路径（语义同源）→ 换出：全局切片按快照
    // 原样还原，五个会话切片存回保留投影。同步块内完成，无中间渲染。
    const snapshot = { ...s0 };
    useStore.setState({ ...retained });
    useStore.getState().reduceActiveEvent(e);
    const after = get();
    useStore.setState({
      ...snapshot,
      sessionProjections: {
        ...after.sessionProjections,
        [foreignCore]: {
          chat: after.chat,
          todos: after.todos,
          perf: after.perf,
          perfStats: after.perfStats,
          usageByModel: after.usageByModel,
        },
      },
      projectionOrder: touchProjectionOrder(after.projectionOrder, foreignCore),
    });
  },

  reduceActiveEvent: (e) => {
    switch (e.type) {
      case 'session_started': {
        // 换了核心会话 id 才切投影（同一会话的重复/重放事件不清）。
        // M4 补完：切走时旧会话投影归档进保留表（上限 PROJECTION_RETAIN_LIMIT，
        // LRU 淘汰），新会话从空投影开始、由随后的 resume replay 按转录重建
        // （恢复链路不变——保留表是并行期间的活缓存，不是恢复源）。
        // R19 统计同款重置（perf 一并回 idle——此前漏重置是潜伏不一致）。
        const same = get().coreSessionId === e.sessionId;
        if (same) {
          set({ coreSessionId: e.sessionId });
          break;
        }
        set((s) => {
          const archived = archiveActiveProjection(s);
          const projections = { ...(archived.sessionProjections ?? s.sessionProjections) };
          // 新会话成为活跃：清掉它的保留记录（顶层切片即真相，避免陈旧残留
          // 被外来事件继续更新）；切走时由归档覆写
          const order = (archived.projectionOrder ?? s.projectionOrder).filter(
            (k) => k !== e.sessionId,
          );
          delete projections[e.sessionId];
          return {
            coreSessionId: e.sessionId,
            chat: { items: [], running: false },
            todos: [],
            perf: { phase: 'idle' },
            perfStats: {},
            usageByModel: {},
            sessionProjections: projections,
            projectionOrder: order,
          };
        });
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
          telegraphIsFallback: false, // 真盘面到达，兜底盘面不再随语言重建
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
          // R19 会话统计：按 model 分组增量累计（含 resume 重放的历史帧）
          perfStats: applyPerfStats(s.perfStats, e),
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
          // R19：byModel 全量快照 latest-wins（核侧按会话累计，见 protocol 注释）
          usageByModel: normalizeUsageByModel(e.byModel),
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
                tail: clampTail(e.tail ?? ''),
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
        // 分离语义：环境探测进行中的 busy 提示不入聊天流（env_info 到达
        // 即完成；曾因探测帧打断车钟拖拽）——探测期间 UI 一切照旧
        if (e.kind === 'busy' && e.message.includes('环境探测')) break;
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
        // R3：tools_* 同款——错误帧立即解锁工具面板 busy
        if (e.message.includes('tools_list') || e.message.includes('tool_toggle')
            || e.message.includes('tool_permission')) {
          set({ toolsRefreshing: false, toolActionPending: false });
        }
        // M8：mcp_* 同款——错误帧立即解锁 busy 并把提示带进 MCP 页签
        if (e.message.includes('mcp_')) {
          set((s) => ({
            mcpRefreshing: false,
            mcpPendingAction: undefined,
            mcpNotice: { kind: 'error', text: e.message },
          }));
        }
        // R17/R22：git_* 的协议级错误（白名单拒绝 / busy 门）立即解锁 git
        // busy——执行结果的降级文案走 git_result.error，不进聊天流
        if (
          get().gitPreviewLoading
          && (e.message.includes('git_preview') || e.message.includes('workspaceRoots'))
        ) {
          set({ gitPreviewLoading: false });
        }
        if (get().gitBusy && e.message.includes('git_')) {
          set({ gitBusy: false });
        }
        break;
      }
      case 'checkpoint': {
        // atMessageId 与入站 user_message 的 messageId 同源（main.py 生成同一
        // id 后先落转录再广播 checkpoint）——这是 UI 侧唯一可得的分叉点真实
        // 标识，forkSession 据此发 control fork。
        // R30「回滚到这」：checkpoint 帧在 main._on_user_message 里同步快照后、
        // 回合线程启动前广播，即「紧随其触发的 user 消息到达」——UI 的 user
        // 条目没有核侧 messageId（本地 nid），按位置配对：倒序找最后一条尚未
        // 关联检查点的 user 条目回填 {id, sha}，遇到已关联的更早条目即停
        // （一一配对，不越位）。resume 重放只回放 out 帧、user 帧不在其中
        // （_control_resume），重放的 checkpoint 找不到可回填条目 → 仅更新
        // lastAtMessageId，按钮随 user 条目缺席而不显示（与聊天投影现状一致）。
        // 已知边界：cron 注入的 prompt 走同一核侧路径（也产 checkpoint）但
        // UI 无对应条目——若它恰好是首个可用检查点，可能回填到上一条真实
        // user 消息（回滚目标仍是最新快照，语义可接受）。
        set((s) => {
          const items = s.chat.items.slice();
          for (let i = items.length - 1; i >= 0; i--) {
            const it = items[i];
            if (it.kind !== 'user') continue; // 跳过流内工具卡等，只看 user 条目
            if (it.checkpointId != null) break; // 更早消息已配对完成
            items[i] = { ...it, checkpointId: e.id, checkpointSha: e.sha };
            break;
          }
          return { lastAtMessageId: e.atMessageId, chat: { ...s.chat, items } };
        });
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
      case 'git_result': {
        // R17/R22：git_commit / git_pr 的回执 → git 切片（GitCommitCard 展示
        // sha / 可点 commit 网址 / PR 网址；error + hint 降级文案），解锁 busy
        set({
          gitBusy: false,
          gitResult: {
            commitSha: e.commitSha,
            remoteUrl: e.remoteUrl,
            commitUrl: e.commitUrl,
            prUrl: e.prUrl,
            error: e.error,
            hint: e.hint,
            at: Date.now(),
          },
        });
        break;
      }
      case 'rollback_result': {
        // R30「回滚到这」回执：成功落一行中性系统提示（详细文案在伴随的
        // result 帧 detail，不重复展示）；失败时核侧紧跟 error 帧带原因
        // （not_found / busy / 快照被 GC 等），此处不重复入流
        if (e.ok) {
          set((s) => ({
            chat: {
              ...s.chat,
              items: [
                ...s.chat.items,
                {
                  kind: 'system',
                  id: nid('sys'),
                  at: Date.now(),
                  text: t('chat.rollbackDone'),
                },
              ],
            },
          }));
        }
        break;
      }
      case 'echo':
        // M0 遗留帧：事件到达即忽略（不报错）。
        break;
    }
  },

  applyHostError: (e) => {
    // M4 补完：外来会话进程的宿主错误进该会话的保留投影（有则入卡），
    // 不再混进活跃聊天流；未绑定/无保留时维持旧行为（活跃投影）。
    const bound =
      e.sessionKey !== undefined ? get().coreByHostKey[e.sessionKey] : undefined;
    if (bound !== undefined && bound !== get().coreSessionId) {
      const proj = get().sessionProjections[bound];
      if (proj === undefined) return;
      set((s) => ({
        sessionProjections: {
          ...s.sessionProjections,
          [bound]: {
            ...proj,
            chat: {
              ...proj.chat,
              items: [
                ...proj.chat.items,
                { kind: 'error', id: nid('hosterr'), at: Date.now(), errorKind: e.errorKind, message: e.message },
              ],
            },
          },
        },
      }));
      return;
    }
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
    ensureSessionThenSend({ type: 'user_message', text: t, context: merged });
  },

  newSession: () => {
    // 本地乐观清空；核心 session_started 会带回新 sessionId。
    // 冷启动（首次/重载后无进程）经 ensure 自愈建会话，control 才有归宿。
    // M4 补完：先归档旧会话投影再清空（session_started 到达时快照已空，
    // archiveActiveProjection 的非空守卫不会用空态覆盖）；R19 统计随会话
    // 切换一并清零（核侧 usage_by_model 同步重置）。
    set((s) => ({
      ...archiveActiveProjection(s),
      chat: { items: [], running: false },
      todos: [],
      perf: { phase: 'idle' },
      perfStats: {},
      usageByModel: {},
    }));
    ensureSessionThenSend({ type: 'control', command: 'new_session' });
  },

  rollbackToCheckpoint: (checkpointId) => {
    // R30：确认已在 UI 弹窗完成（PLAN §5 rollback 无二次确认往返），
    // 核侧收到即 git checkout <sha> -- .（sha 凭转录找回，_checkpoint_sha）
    ensureSessionThenSend({ type: 'rollback', checkpointId });
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
      // M4 补完：绑定已知（hostKeyByCore 学习自 session_started 信封）时
      // resume 定向发往「拥有该会话的进程」——并行多开下活跃进程 resume
      // 别的会话会造成双进程绑同一转录；未知绑定维持旧行为（活跃进程）。
      // 顺序：先 resume（重放转录，session_started 到达时 store 切投影——
      // 旧会话投影随即归档保留），再 session.activate（宿主把活跃
      // sessionKey 指到目标进程；未知绑定时载荷退化为 sessionId，宿主
      // 解析能力并行定稿中，见 protocol.ts SessionActivateParams 注释）。
      const knownKey = get().hostKeyByCore[sessionId];
      hostSendCore({ type: 'control', command: 'resume', sessionId }, knownKey);
      void hostRequest<SessionActivateResult>('session.activate', {
        sessionId,
        ...(knownKey !== undefined ? { sessionKey: knownKey } : {}),
      })
        .then((r) =>
          set((s) => ({
            hostConnected: true,
            activeSessionKey:
              typeof r?.activeSessionKey === 'string' ? r.activeSessionKey : s.activeSessionKey,
          })),
        )
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

  deleteSession: (sessionId) => {
    // M4 补完：破坏性操作不做乐观删除——result ok 后核心广播 sessions_list
    // 收敛列表；核侧拒绝当前活跃会话（UI 侧按钮已禁用，error 帧兜底提示）
    set((s) => ({
      sessionsUi: { ...s.sessionsUi, [sessionId]: { ...s.sessionsUi[sessionId], busy: 'delete', notice: undefined } },
      pendingControl: { command: 'session_delete', sessionId, at: Date.now() },
    }));
    hostSendCore({ type: 'control', command: 'session_delete', sessionId });
    armSessionBusyTimeout(sessionId, 'delete');
  },

  refreshEnv: () => {
    if (get().envRefreshing) return;
    // M5/R21：核心重新探测并广播 env_info（归约清除 envRefreshing）；
    // 核心未实现该命令时回 error 帧，兜底超时解锁按钮。
    // 冷启动经 ensure 自愈，且探测与其他功能完全分离：
    //  - ensure 新建了会话 → init 自带环境探测并广播 env_info，不补发
    //    env_refresh（否则双重探测，探测中再刷还会往聊天流回帧）；
    //  - ensure 复用既有会话 → 照常 env_refresh。
    set({ envRefreshing: true });
    hostRequest<SessionEnsureResult>('session.ensure', undefined, 10_000)
      .then((r) => {
        if (!r.created) {
          hostSendCore({ type: 'control', command: 'env_refresh' });
        }
      })
      .catch(() => {
        /* 宿主已广播错误（process_error/host_error），不重复 */
      });
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

  refreshTools: () => {
    if (get().toolsRefreshing) return;
    // R3：control tools_list → result.detail JSON（reduceResult 对账消费）
    set({
      toolsRefreshing: true,
      toolsRequested: true,
      pendingControl: { command: 'tools_list', sessionId: get().coreSessionId ?? '', at: Date.now() },
    });
    hostSendCore({ type: 'control', command: 'tools_list' });
    window.setTimeout(() => {
      if (useStore.getState().toolsRefreshing) {
        useStore.setState({ toolsRefreshing: false });
      }
    }, 15_000);
  },

  toggleTool: (name, enabled) => {
    if (get().toolActionPending) return;
    set({ toolActionPending: true });
    hostSendCore({
      type: 'control',
      command: 'tool_toggle',
      name,
      enabled,
      query: JSON.stringify({ name, enabled }),
    });
    window.setTimeout(() => {
      if (useStore.getState().toolActionPending) {
        useStore.setState({ toolActionPending: false });
      }
    }, 15_000);
  },

  setToolPermission: (name, mode) => {
    if (get().toolActionPending) return;
    set({ toolActionPending: true });
    hostSendCore({
      type: 'control',
      command: 'tool_permission',
      name,
      mode,
      query: JSON.stringify({ name, mode }),
    });
    window.setTimeout(() => {
      if (useStore.getState().toolActionPending) {
        useStore.setState({ toolActionPending: false });
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
      cronId: String(entry?.id ?? name),
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
      cronId: String(get().crons.find((c) => c.name === name)?.id ?? name),
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
              report: t('tasks.workflowTimeout'),
              at: Date.now(),
            },
          },
        }));
      }
    }, 60_000);
  },

  /* ---------- R15/R20：设置页（settings 切片） ---------- */

  refreshSettings: () => {
    if (get().settingsRefreshing) return;
    // control settings_get → result.detail JSON {settings, homeDir, sessionsDir}
    //（reduceM9Result 消费）；15s 兜底解锁刷新按钮
    set({ settingsRefreshing: true, settingsNotice: undefined });
    ensureSessionThenSend({ type: 'control', command: 'settings_get' });
    window.setTimeout(() => {
      if (useStore.getState().settingsRefreshing) {
        useStore.setState({
          settingsRefreshing: false,
          settingsNotice: { kind: 'error', text: t('settings.getTimeout') },
        });
      }
    }, 15_000);
  },

  saveSettings: (patch) => {
    if (get().settingsSaving) return; // 单飞
    // control settings_set{settings: patch} —— 核侧 normalize_patch 校验受管键，
    // 非法值回 error 帧（settingsSaving 由兜底解锁并提示）；成功回执 detail
    // 即生效值（reduceM9Result 直接覆盖 settings 切片）
    set({ settingsSaving: true, settingsNotice: undefined });
    hostSendCore({
      type: 'control',
      command: 'settings_set',
      settings: patch as Record<string, unknown>,
      query: JSON.stringify({ settings: patch }),
    });
    window.setTimeout(() => {
      if (useStore.getState().settingsSaving) {
        useStore.setState({
          settingsSaving: false,
          settingsNotice: { kind: 'error', text: t('settings.saveTimeout') },
        });
      }
    }, 15_000);
  },

  clearSessionsData: () => {
    if (get().settingsSaving) return; // 单飞
    // control clear_sessions —— UI 侧已完成二次确认；核侧清空 sessions 目录
    // 后重建空会话并广播 sessions_list / session_started（聊天投影随之重置）
    set({ settingsSaving: true, settingsNotice: undefined });
    hostSendCore({ type: 'control', command: 'clear_sessions' });
    window.setTimeout(() => {
      if (useStore.getState().settingsSaving) {
        useStore.setState({
          settingsSaving: false,
          settingsNotice: { kind: 'error', text: t('settings.clearTimeout') },
        });
      }
    }, 15_000);
  },

  /* ---------- R17/R22：一键 git 流（git 切片） ---------- */

  openGitPanel: () => {
    // 重置上次回执；workspaceRoots 惰性拉取一次（multi-root 下拉数据源，
    // 与 boot 的 getState 同源）——首根即默认 repoRoot，预览由卡片 effect 驱动
    set({ gitPanelOpen: true, gitResult: undefined });
    if (get().gitRepos.length === 0) {
      void hostRequest<GetStateResult>('getState', undefined, 5000)
        .then((r) => {
          const roots = (Array.isArray(r?.workspaceRoots) ? r.workspaceRoots : []).filter(
            (x): x is string => typeof x === 'string' && x !== '',
          );
          set((s) => ({
            gitRepos: roots,
            gitRepoRoot:
              s.gitRepoRoot !== undefined && roots.includes(s.gitRepoRoot)
                ? s.gitRepoRoot
                : roots[0],
          }));
        })
        .catch(() => {
          /* 宿主不可达：repoRoot 缺省，卡片显示无仓库降级文案 */
        });
    }
  },

  closeGitPanel: () => {
    set({ gitPanelOpen: false, gitPreview: undefined, gitPreviewLoading: false });
  },

  setGitRepo: (repoRoot) => {
    if (repoRoot === get().gitRepoRoot) return;
    set({ gitRepoRoot: repoRoot, gitPreview: undefined, gitResult: undefined });
  },

  setGitScope: (scope) => {
    if (scope === get().gitScope) return;
    set({ gitScope: scope, gitPreview: undefined });
  },

  setGitPush: (push) => {
    set({ gitPush: push });
  },

  requestGitPreview: () => {
    const { gitRepoRoot, gitScope, gitPreviewLoading } = get();
    if (!gitRepoRoot || gitPreviewLoading) return;
    // control git_preview{repoRoot, scope} → result.detail JSON（reduceM9Result
    // 消费 → gitPreview）；冷启动经 ensure 自愈；15s 兜底降级文案
    set({ gitPreviewLoading: true });
    ensureSessionThenSend({ type: 'control', command: 'git_preview', repoRoot: gitRepoRoot, scope: gitScope });
    const repo = gitRepoRoot;
    const scope2 = gitScope;
    window.setTimeout(() => {
      if (useStore.getState().gitPreviewLoading) {
        useStore.setState({
          gitPreviewLoading: false,
          gitPreview: {
            ok: false,
            repoRoot: repo,
            scope: scope2,
            files: [],
            insertions: 0,
            deletions: 0,
            untracked: 0,
            subject: '',
            body: '',
            styleNote: '',
            remoteKind: 'none',
            canPr: false,
            ghAvailable: false,
            error: t('git.previewTimeout'),
          },
        });
      }
    }, 15_000);
  },

  sendGitCommit: (message, pushOverride) => {
    const { gitRepoRoot, gitScope, gitPush, gitBusy } = get();
    if (!gitRepoRoot || gitBusy) return;
    const text = message.trim();
    if (text === '') return;
    // git_commit 执行帧（预览卡「确认 / 仅提交」按钮 = 用户显式确认；push
    // 缺省按勾选、仅提交强制 false；回执 git_result 帧归约解锁 busy）；
    // 60s 兜底（push 可能耗时）
    set({ gitBusy: true, gitResult: undefined });
    ensureSessionThenSend({
      type: 'git_commit',
      repoRoot: gitRepoRoot,
      scope: gitScope,
      push: pushOverride ?? gitPush,
      message: text,
    });
    window.setTimeout(() => {
      if (useStore.getState().gitBusy) {
        useStore.setState({ gitBusy: false });
      }
    }, 60_000);
  },

  sendGitPr: () => {
    const { gitRepoRoot, gitBusy } = get();
    if (!gitRepoRoot || gitBusy) return;
    // control git_pr{repoRoot}（gh pr create --fill——用刚提交的 message；
    // 回 git_result 带 prUrl / error+hint）；60s 兜底
    set({ gitBusy: true });
    ensureSessionThenSend({ type: 'control', command: 'git_pr', repoRoot: gitRepoRoot });
    window.setTimeout(() => {
      if (useStore.getState().gitBusy) {
        useStore.setState({ gitBusy: false });
      }
    }, 60_000);
  },

  setUiLanguage: (lang) => {
    // 「通用」组：R20 显式选择——标 explicit 才持久 + 同步 i18n locale
    // 即时切换界面 + 兜底车钟盘面按新语言重建
    set({ uiLanguage: lang, uiLanguageExplicit: true });
    setLocale(lang);
    refreshFallbackTelegraph();
    persistUi(get);
  },

  setFontScale: (scale) => {
    // 「通用」组：字号缩放百分比，钳制 85–150（App 根节点 font-size 消费）
    const clamped = Math.max(85, Math.min(150, Math.round(scale)));
    set({ fontScale: clamped });
    persistUi(get);
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
      error = err instanceof RpcTimeoutError ? t('api.hostUnreachable') : err instanceof Error ? err.message : String(err);
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
