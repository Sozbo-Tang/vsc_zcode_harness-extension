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
  GetStateResult,
  HardwareInfo,
  PermissionMode,
  PingResult,
  ProviderConfig,
  SessionSummary,
  TelegraphLevel,
  TodoItem,
  UserMessageContext,
  WorkspaceLang,
} from '../protocol';

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

  /* ---- 切片：activeSession（当前会话投影） ---- */
  chat: { items: ChatItem[]; running: boolean };
  todos: TodoItem[];
  perf: PerfState;
  contextPct?: number;
  usageTokens?: number;
  permissionMode: PermissionMode;
  telegraph: TelegraphState;

  /* ---- 切片：providers ---- */
  providers: ProviderConfig[];
  providerUi: Record<string, ProviderUiState>;

  /* ---- 切片：env ---- */
  env: { hardware: HardwareInfo; workspaceLangs: WorkspaceLang[] } | null;

  /* ---- 切片：tasks ---- */
  tasks: BackgroundTask[];

  /* ---- 输入态 ---- */
  inputDraft: string;
  setInputDraft(text: string): void;

  /* ---- 动作 ---- */
  boot(): Promise<void>;
  applyEvent(e: CoreEvent): void;
  applyHostError(e: { sessionKey?: string; errorKind: string; message: string }): void;
  applyProcessState(e: { sessionKey: string; state: string; detail?: string }): void;

  sendUserMessage(text: string, context?: UserMessageContext): void;
  newSession(): void;
  cancelRunning(): void;
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
  tasks: [],
  inputDraft: '',

  setInputDraft: (text) => {
    set({ inputDraft: text });
    persistUi(get);
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
        set({ env: { hardware: e.hardware, workspaceLangs: e.workspaceLangs } });
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
      case 'error': {
        set((s) => ({
          chat: {
            ...s.chat,
            items: [
              ...s.chat.items,
              { kind: 'error', id: nid('err'), at: Date.now(), errorKind: e.kind, message: e.message },
            ],
          },
        }));
        break;
      }
      case 'checkpoint':
      case 'rollback_result':
      case 'compaction':
      case 'git_result':
      case 'image_artifact':
      case 'result':
      case 'echo':
        // M1 webview 尚不消费：检查点回滚 UI=R30/M2、压缩报告=M8、git=M4、
        // 图片 artifact 展示=M5/M6、echo=M0 遗留帧。事件到达即忽略（不报错）。
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
    set((s) => ({
      inputDraft: '',
      chat: {
        ...s.chat,
        items: [...s.chat.items, { kind: 'user', id: nid('u'), at: Date.now(), text: t, context }],
      },
    }));
    persistUi(get);
    hostSendCore({ type: 'user_message', text: t, context: context ?? {} });
  },

  newSession: () => {
    // 本地乐观清空；核心 session_started 会带回新 sessionId
    set({ chat: { items: [], running: false }, todos: [], perf: { phase: 'idle' } });
    hostSendCore({ type: 'control', command: 'new_session' });
  },

  cancelRunning: () => {
    hostSendCore({ type: 'control', command: 'cancel' });
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
    set({ permissionMode: mode });
    persistUi(get);
    // 宿主 ControlCommand 枚举暂缺 set_permission_mode（见 protocol.ts 注释）
    hostSendCore({ type: 'control', command: 'set_permission_mode', mode });
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
