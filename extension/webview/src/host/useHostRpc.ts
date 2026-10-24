/**
 * useHostRpc：postMessage 封装 Hook（PLAN §4 前端约定）。
 *
 * - request()：correlation id 请求-响应（宿主 rpcHandlers，webviewHost.ts）
 * - sendCore()：壳 → 核消息转发（user_message / control / permission_response…）
 * - onEvent() / onHostError() / onProcessState()：订阅（返回退订函数）
 *
 * 传输层是模块级单例（transport.ts），Hook 只返回稳定引用；
 * React 重挂载（侧边栏视图销毁重建）后 transport 自动重发 ready 换取重放。
 */
import { useCallback } from 'react';
import {
  hostRequest,
  hostSendCore,
  onHostError,
  onHostEvent,
  onHostPrompt,
  onProcessState,
} from './transport';
import type { CoreEvent, CoreSend, HostRpcMethod, UserMessageContext } from '../protocol';

export interface HostRpc {
  request<T = unknown>(method: HostRpcMethod, params?: unknown, timeoutMs?: number): Promise<T>;
  sendCore(message: CoreSend): void;
  onEvent(handler: (e: CoreEvent) => void): () => void;
  onHostError(handler: (e: { sessionKey?: string; errorKind: string; message: string }) => void): () => void;
  onProcessState(handler: (e: { sessionKey: string; state: string; detail?: string }) => void): () => void;
  /** M3 编辑器联动：宿主预填待发提示（编辑器右键/终端选中/Problems） */
  onHostPrompt(handler: (e: { text: string; context?: UserMessageContext }) => void): () => void;
}

const rpc: HostRpc = {
  request: (method, params, timeoutMs) => hostRequest(method, params, timeoutMs),
  sendCore: (message) => hostSendCore(message),
  onEvent: (handler) => onHostEvent(handler),
  onHostError: (handler) => onHostError(handler),
  onProcessState: (handler) => onProcessState(handler),
  onHostPrompt: (handler) => onHostPrompt(handler),
};

export function useHostRpc(): HostRpc {
  // 单例：无状态 Hook，仅保持 API 形态与 PLAN §4 命名一致
  return useCallback(() => rpc, [])();
}
