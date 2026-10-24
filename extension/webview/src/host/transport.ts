/**
 * webview ↔ 扩展宿主传输层 —— 与 extension/src/protocol.ts 的
 * 「Webview ↔ 扩展宿主 postMessage 桥」信封严格对齐（宿主契约是真相源）：
 *
 *  webview → 宿主：
 *   {kind:'ready'}                              挂载完成，宿主重放缓冲事件
 *   {kind:'core', sessionKey?, message}         转发壳 → 核消息给 Python
 *   {kind:'rpc', id, method, params}            请求-响应 RPC（id: string）
 *  宿主 → webview：
 *   {kind:'core', sessionKey, message}          核 → 壳事件转发
 *   {kind:'host_error', sessionKey?, errorKind, message}
 *   {kind:'process_state', sessionKey, state, detail?}
 *   {kind:'host_prompt', text, context?}        M3 编辑器联动：待发提示预填
 *   {kind:'rpc_result', id, ok, result?, error?}
 *   {kind:'replay', messages[]}                 ready 后的全量重放（有界 2000 条）
 *
 * 事实依据（docs/research/vscode-integration.md）：
 *  - acquireVsCodeApi 每 session 只能调一次，实例必须保存复用（§1.3）；
 *  - 侧边栏视图销毁重建后由宿主重放事件恢复状态（不开 retain，§1.4/§7.2）。
 */

import type { CoreEvent, CoreSend, RpcErrorInfo, UserMessageContext } from '../protocol';

interface VsCodeApi {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
}

declare global {
  interface Window {
    acquireVsCodeApi?: () => VsCodeApi;
  }
}

let cachedApi: VsCodeApi | undefined;

/** 浏览器直开（vite dev 调 UI）时的最小 mock；真实 webview 下不会走这分支 */
function mockApi(): VsCodeApi {
  const mem: { s?: unknown } = {};
  // eslint-disable-next-line no-console
  return {
    postMessage: (m) => console.info('[mock vscode.postMessage]', m),
    getState: () => mem.s,
    setState: (s) => {
      mem.s = s;
    },
  };
}

export function getVsCodeApi(): VsCodeApi {
  if (cachedApi) return cachedApi;
  cachedApi = window.acquireVsCodeApi ? window.acquireVsCodeApi() : mockApi();
  return cachedApi;
}

export class RpcTimeoutError extends Error {
  constructor(method: string, timeoutMs: number) {
    super(`host rpc "${method}" timed out after ${timeoutMs}ms`);
    this.name = 'RpcTimeoutError';
  }
}

export class RpcRemoteError extends Error {
  constructor(
    public errorInfo: RpcErrorInfo,
  ) {
    super(errorInfo.message);
    this.name = 'RpcRemoteError';
  }
}

interface PendingEntry {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

type EventHandler = (e: CoreEvent, sessionKey: string) => void;
type HostErrorHandler = (e: { sessionKey?: string; errorKind: string; message: string }) => void;
type ProcessStateHandler = (e: { sessionKey: string; state: string; detail?: string }) => void;

/* 宿主 → webview 信封（对齐 extension/src/protocol.ts HostToWebviewMessage） */
export interface HostPromptEvent {
  kind: 'host_prompt';
  text: string;
  context?: UserMessageContext;
}

type HostEnvelope =
  | { kind: 'core'; sessionKey: string; message: CoreEvent }
  | { kind: 'host_error'; sessionKey?: string; errorKind: string; message: string }
  | { kind: 'process_state'; sessionKey: string; state: string; detail?: string }
  | HostPromptEvent
  | { kind: 'rpc_result'; id: string; ok: boolean; result?: unknown; error?: string }
  | { kind: 'replay'; messages: HostEnvelope[] };

let nextId = 1;
const pending = new Map<string, PendingEntry>();
const eventHandlers = new Set<EventHandler>();
const hostErrorHandlers = new Set<HostErrorHandler>();
const processStateHandlers = new Set<ProcessStateHandler>();
const hostPromptHandlers = new Set<(e: HostPromptEvent) => void>();
let installed = false;

function dispatchHostMessage(data: HostEnvelope): void {
  switch (data.kind) {
    case 'core':
      // sessionKey 一并下发（M4 补完：并行会话独立投影按信封键路由归约；
      // 每个宿主会话进程一个 key，宿主 forwardCore 全量转发、不带活跃过滤）
      for (const h of eventHandlers) h(data.message, data.sessionKey);
      break;
    case 'host_error':
      for (const h of hostErrorHandlers) h(data);
      break;
    case 'process_state':
      for (const h of processStateHandlers) h(data);
      break;
    case 'host_prompt':
      for (const h of hostPromptHandlers) h(data);
      break;
    case 'rpc_result': {
      const entry = pending.get(data.id);
      if (!entry) return;
      pending.delete(data.id);
      clearTimeout(entry.timer);
      if (data.ok) entry.resolve(data.result);
      else entry.reject(new RpcRemoteError({ code: 'host_error', message: data.error ?? 'unknown' }));
      break;
    }
    case 'replay':
      for (const m of data.messages) dispatchHostMessage(m);
      break;
  }
}

function installListener(): void {
  if (installed) return;
  installed = true;
  window.addEventListener('message', (ev: MessageEvent<unknown>) => {
    const data = ev.data as HostEnvelope | undefined;
    if (!data || typeof data !== 'object' || typeof (data as { kind?: unknown }).kind !== 'string') {
      return;
    }
    dispatchHostMessage(data);
  });
  // 挂载即上报 ready：宿主重放缓冲事件（webviewHost.ts handleFromWebview 'ready'）
  getVsCodeApi().postMessage({ kind: 'ready' });
}

/** 请求-响应；超时拒绝（宿主未实现该方法时收到 rpc_result ok:false） */
export function hostRequest<T = unknown>(
  method: string,
  params?: unknown,
  timeoutMs = 15000,
): Promise<T> {
  installListener();
  const id = String(nextId++);
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new RpcTimeoutError(method, timeoutMs));
    }, timeoutMs);
    pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
    getVsCodeApi().postMessage({ kind: 'rpc', id, method, params });
  });
}

/**
 * 转发一条壳 → 核消息给 Python（宿主 onSendToCore）。
 * sessionKey 缺省 = 活跃会话；携带时定向发往该宿主会话进程
 * （并行多开 R16：宿主 webviewHost 'core' 信封已支持 sessionKey，
 * 目标非活跃会话时宿主按 key 路由，不存在则回 host_error）。
 */
export function hostSendCore(message: CoreSend, sessionKey?: string): void {
  installListener();
  const envelope: { kind: 'core'; message: CoreSend; sessionKey?: string } = { kind: 'core', message };
  if (sessionKey !== undefined) envelope.sessionKey = sessionKey;
  getVsCodeApi().postMessage(envelope);
}

export function onHostEvent(handler: EventHandler): () => void {
  installListener();
  eventHandlers.add(handler);
  return () => eventHandlers.delete(handler);
}

export function onHostError(handler: HostErrorHandler): () => void {
  installListener();
  hostErrorHandlers.add(handler);
  return () => hostErrorHandlers.delete(handler);
}

export function onProcessState(handler: ProcessStateHandler): () => void {
  installListener();
  processStateHandlers.add(handler);
  return () => processStateHandlers.delete(handler);
}

export function onHostPrompt(handler: (e: HostPromptEvent) => void): () => void {
  installListener();
  hostPromptHandlers.add(handler);
  return () => hostPromptHandlers.delete(handler);
}
