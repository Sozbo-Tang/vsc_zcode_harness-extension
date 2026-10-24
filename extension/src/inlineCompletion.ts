/**
 * 内联补全（幽灵文本）—— R13 / PLAN §7 M9。默认关闭（决策记录 §10）。
 *
 * 通道决策：内联补全发生在扩展宿主侧（编辑器 Provider），不在 webview，
 * 因此不经 useHostRpc；也非 control 异步事件流消费——而是宿主把请求直接
 * 写进活跃 python 会话 stdin 一条 control `inline_complete{prefix, suffix,
 * path}`（双通道：专用字段 + query JSON 镜像，见 protocol.ts M9 注释），
 * Python 侧回 result 帧（command="inline_complete"，detail=补全文本）；
 * 本模块订阅 manager.onCoreMessage 按 (command, sessionKey) 对账出 Promise。
 *
 * 容错（Python 侧并行定稿期）：CONTROL_COMMANDS 未收编该命令时核心回
 * unknown control / protocol 错误帧，或解码层直接拒收——两种路径在这里
 * 都只是「补全静默失败」（返回空数组，不打扰输入，不弹提示）。
 *
 * 行为约束：
 *  - 开关 zcode-py.inlineCompletion.enabled（默认 false），变更即时生效
 *    （onDidChangeConfiguration 内部增删 Provider，无需重载窗口）；
 *  - 防抖 800ms（连续击键只取最后一次请求，被顶替的请求立即以空补全结算）；
 *  - 仅当存在存活 python 会话（starting/running）时才注册 Provider
 *    （manager.onProcessState 驱动注册/注销；无会话 = 零开销）；
 *  - 单飞：同一时刻至多一条 inline_complete 在途（防抖天然保证），
 *    超时 5s / 进程死亡 / CancellationToken 取消均立即以空补全结算。
 */

import * as vscode from "vscode";
import type { PythonProcessManager } from "./processManager";
import { ControlMessage } from "./protocol";

/** 配置键（package.json contributes.configuration，默认 false） */
const CONFIG_ENABLED = "zcode-py.inlineCompletion.enabled";
/** 击键防抖（ms）：连续触发只执行最后一次 */
const DEBOUNCE_MS = 800;
/** 等待核心 result 回执的上限（ms）——超时视为失败（静默空补全） */
const RESULT_TIMEOUT_MS = 5_000;
/** prefix/suffix 取窗（字符）：贴光标的局部上下文足够 FIM/模拟 */
const PREFIX_WINDOW = 4_000;
const SUFFIX_WINDOW = 2_000;

export interface InlineCompletionDeps {
  manager: PythonProcessManager;
  /** 活跃会话 key（extension.ts rt.activeSessionKey 的只读透传） */
  getActiveSessionKey: () => string | undefined;
  outputChannel: vscode.OutputChannel;
}

/**
 * result.detail → 补全文本（宽容双形状：裸字符串，或 JSON {text|completion|
 * content|detail}；JSON 解析失败按裸字符串处理——文本补全天然不是 JSON 时
 * 这正是期望路径）。空串/空对象 → 空补全。
 */
export function parseCompletionDetail(detail: string | undefined): string {
  if (typeof detail !== "string" || detail === "") return "";
  try {
    const v: unknown = JSON.parse(detail);
    if (typeof v === "string") return v;
    if (v !== null && typeof v === "object") {
      const o = v as Record<string, unknown>;
      for (const key of ["text", "completion", "content", "detail"]) {
        if (typeof o[key] === "string") return o[key] as string;
      }
      return "";
    }
    return "";
  } catch {
    return detail; // 非 JSON：detail 本体即补全文本
  }
}

/** 在途请求对账锚：result/error 帧、超时、进程死亡、取消四路结算 */
interface PendingRequest {
  sessionKey: string;
  settle: (text: string) => void;
  timer: NodeJS.Timeout;
}

interface DebouncedCall {
  resolve: (items: vscode.InlineCompletionItem[]) => void;
  timer: NodeJS.Timeout;
}

class InlineCompletionController implements vscode.Disposable {
  private readonly deps: InlineCompletionDeps;
  private readonly disposables: vscode.Disposable[] = [];
  /** provider 注册句柄（仅 enabled && 存活会话时非空） */
  private providerDisposable?: vscode.Disposable;
  private enabled = false;

  /** 在途核心请求（单飞）与防抖窗口（单飞） */
  private pending?: PendingRequest;
  private debounced?: DebouncedCall;

  constructor(deps: InlineCompletionDeps) {
    this.deps = deps;
    this.enabled = vscode.workspace.getConfiguration().get<boolean>(CONFIG_ENABLED, false);

    // result / error 帧对账（订阅常驻——比 provider 注册更便宜，且避免
    // 注册竞态丢帧：请求发出瞬间 listener 必须已在）
    this.disposables.push(
      deps.manager.onCoreMessage((e) => {
        if (this.pending === undefined || e.sessionKey !== this.pending.sessionKey) return;
        const msg = e.message;
        if (msg.type === "result" && msg.command === "inline_complete") {
          const text = msg.ok ? parseCompletionDetail(msg.detail) : "";
          this.settlePending(text);
        } else if (msg.type === "error" && msg.message.includes("inline_complete")) {
          // 核心未实现/拒绝该命令（Python 并行定稿期常态）——静默失败
          this.settlePending("");
        }
      }, undefined, this.disposables),
      // 进程死亡/停止：在途请求立即作废（避免白等 5s 超时）
      deps.manager.onProcessState((e) => {
        if (this.pending !== undefined && e.sessionKey === this.pending.sessionKey &&
            (e.state === "stopped" || e.state === "crashed" || e.state === "stopping")) {
          this.settlePending("");
        }
        this.syncRegistration();
      }, undefined, this.disposables),
      vscode.workspace.onDidChangeConfiguration((e) => {
        if (!e.affectsConfiguration(CONFIG_ENABLED)) return;
        this.enabled = vscode.workspace.getConfiguration().get<boolean>(CONFIG_ENABLED, false);
        this.syncRegistration();
      }, undefined, this.disposables),
    );
    this.syncRegistration();
  }

  /** 存活会话判定：任一会话处于 starting/running 即算（spawn 是异步的） */
  private liveSessionExists(): boolean {
    return this.deps.manager
      .sessionKeys()
      .some((key) => {
        const state = this.deps.manager.get(key)?.state;
        return state === "starting" || state === "running";
      });
  }

  /** 注册态收敛：enabled && 存活会话 → 注册；否则注销（幂等） */
  private syncRegistration(): void {
    const shouldRegister = this.enabled && this.liveSessionExists();
    if (shouldRegister && this.providerDisposable === undefined) {
      const provider: vscode.InlineCompletionItemProvider = {
        provideInlineCompletionItems: (document, position, _ctx, token) =>
          this.provide(document, position, token),
      };
      this.providerDisposable = vscode.languages.registerInlineCompletionItemProvider(
        [
          { scheme: "file" },
          { scheme: "untitled" },
          { scheme: "vscode-notebook-cell" },
        ],
        provider,
      );
      this.disposables.push(this.providerDisposable);
    } else if (!shouldRegister && this.providerDisposable !== undefined) {
      const disposable = this.providerDisposable;
      this.providerDisposable = undefined;
      disposable.dispose();
      const idx = this.disposables.indexOf(disposable);
      if (idx >= 0) this.disposables.splice(idx, 1);
      // 注册注销时清挂起态（防抖窗口内的等待者立即空结算）
      this.settlePending("");
      if (this.debounced !== undefined) {
        clearTimeout(this.debounced.timer);
        this.debounced.resolve([]);
        this.debounced = undefined;
      }
    }
  }

  private settlePending(text: string): void {
    const p = this.pending;
    if (p === undefined) return;
    this.pending = undefined;
    clearTimeout(p.timer);
    p.settle(text);
  }

  /** 防抖 800ms：新触发顶替旧等待（旧者立即空补全结算） */
  private provide(
    document: vscode.TextDocument,
    position: vscode.Position,
    token: vscode.CancellationToken,
  ): Thenable<vscode.InlineCompletionItem[]> {
    return new Promise<vscode.InlineCompletionItem[]>((resolve) => {
      let settled = false;
      const finish = (items: vscode.InlineCompletionItem[]): void => {
        if (settled) return;
        settled = true;
        resolve(items);
      };

      // 顶替：上一个防抖窗口立即空结算
      if (this.debounced !== undefined) {
        clearTimeout(this.debounced.timer);
        this.debounced.resolve([]);
        this.debounced = undefined;
      }
      const timer = setTimeout(() => {
        // 窗口到期仍是本次请求（未被顶替）才执行
        if (this.debounced !== undefined && this.debounced.resolve === finish) {
          this.debounced = undefined;
        }
        void this.execute(document, position, finish, token);
      }, DEBOUNCE_MS);
      this.debounced = { resolve: finish, timer };

      token.onCancellationRequested(() => finish([]));
    });
  }

  /** 防抖窗口到期：发 control inline_complete 并等 result 对账 */
  private execute(
    document: vscode.TextDocument,
    position: vscode.Position,
    finish: (items: vscode.InlineCompletionItem[]) => void,
    token: vscode.CancellationToken,
  ): void {
    const key = this.deps.getActiveSessionKey();
    const proc = key !== undefined ? this.deps.manager.get(key) : undefined;
    if (key === undefined || proc === undefined) {
      finish([]); // 无活跃会话：静默
      return;
    }

    // FIM 上下文取窗（贴光标局部）
    const full = document.getText();
    const offset = document.offsetAt(position);
    const prefixStart = document.positionAt(Math.max(0, offset - PREFIX_WINDOW));
    const suffixEnd = document.positionAt(Math.min(full.length, offset + SUFFIX_WINDOW));
    const prefix = document.getText(new vscode.Range(prefixStart, position));
    const suffix = document.getText(new vscode.Range(position, suffixEnd));
    const filePath = document.uri.fsPath;

    // 单飞保护：上一请求仍在途则先作废
    this.settlePending("");

    const message: ControlMessage = {
      type: "control",
      command: "inline_complete",
      // 双通道载荷（protocol.ts M9 注释）：专用字段 + query JSON 镜像
      prefix,
      suffix,
      path: filePath,
      query: JSON.stringify({ prefix, suffix, path: filePath }),
    };
    if (!proc.send(message)) {
      finish([]); // 进程不在：静默
      return;
    }

    this.pending = {
      sessionKey: key,
      settle: (text) => {
        const trimmed = text.replace(/\r/g, "");
        finish(trimmed === "" ? [] : [new vscode.InlineCompletionItem(trimmed)]);
      },
      timer: setTimeout(() => this.settlePending(""), RESULT_TIMEOUT_MS),
    };
    token.onCancellationRequested(() => this.settlePending(""));
  }

  public dispose(): void {
    this.settlePending("");
    if (this.debounced !== undefined) {
      clearTimeout(this.debounced.timer);
      this.debounced.resolve([]);
      this.debounced = undefined;
    }
    for (const d of this.disposables) d.dispose();
    this.disposables.length = 0;
    this.providerDisposable = undefined;
  }
}

/**
 * 注册内联补全（R13，默认关）。与 M0–M8 桩的差别：总是返回 Disposable
 * （内部按「开关 && 存活会话」动态增删真正的 Provider，开关变更即时生效），
 * 调用方无条件 push 进 context.subscriptions 即可。
 */
export function registerInlineCompletion(
  context: vscode.ExtensionContext,
  deps: InlineCompletionDeps,
): vscode.Disposable {
  const controller = new InlineCompletionController(deps);
  context.subscriptions.push(controller);
  return controller;
}
