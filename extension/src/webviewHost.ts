/**
 * 侧边栏 Webview 宿主（PLAN §3.1 / 调研 docs/research/vscode-integration.md）。
 *
 * - 不开 retainContextWhenHidden（内存开销高 + 隐藏时脚本挂起、postMessage
 *   不可达）。代价补偿：宿主侧维护有界重放缓冲，webview 每次 resolve 后
 *   发 {kind:"ready"}，宿主重放全部缓冲事件——状态真相在扩展宿主+Python。
 * - CSP：default-src 'none'; script-src 'nonce-<32位随机>'；style-src 加
 *   'unsafe-inline'（React style 属性受 style-src 管辖，官方 sample 的
 *   纯 cspSource 写法会打断内联 style）；资源一律经 asWebviewUri，
 *   localResourceRoots 收窄到 webview/dist。
 * - 资产解析双兼容：优先固定产物名 index.js/assets/index.css（PLAN §4
 *   前端约定），否则回退解析 dist/index.html 里的 hash 产物（vite 默认）。
 * - postMessage：webview→host 三种 kind（ready/core/rpc）；host→webview
 *   五种 kind（core/host_error/process_state/rpc_result/replay）。
 *   webview.postMessage 只能投递 live webview，返回 true 不代表收到——
 *   需要确认送达的场景由 webview 回发确认（rpc 即此模式）。
 */

import * as vscode from "vscode";
import * as crypto from "node:crypto";
import {
  CoreToHostMessage,
  HostToWebviewMessage,
  ReplayableWebviewMessage,
  UserMessageContext,
  WebviewCoreSendMessage,
  WebviewToHostMessage,
} from "./protocol";

/** 重放缓冲上限（条）：覆盖长会话，溢出丢最旧 */
const REPLAY_LIMIT = 2_000;

type RpcHandler = (params: unknown) => unknown | Promise<unknown>;

interface WebviewAssets {
  scriptUri: string | undefined;
  styleHref: string | undefined;
}

export class ZcodeWebviewViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "zcode.sidebar";

  private view?: vscode.WebviewView;
  private readonly replayBuffer: ReplayableWebviewMessage[] = [];
  private readonly rpcHandlers = new Map<string, RpcHandler>();
  private readonly sendToCoreEmitter = new vscode.EventEmitter<WebviewCoreSendMessage>();
  /** webview 请求把消息发往 Python 核心（sessionKey 缺省 = 活跃会话） */
  public readonly onSendToCore = this.sendToCoreEmitter.event;

  constructor(private readonly extensionUri: vscode.Uri) {}

  /** 注册宿主侧 RPC 方法（webview useHostRpc 的服务端） */
  public registerRpcHandler(method: string, handler: RpcHandler): void {
    this.rpcHandlers.set(method, handler);
  }

  /**
   * M6（R8）：本地文件 URI → webview 可加载的 URI（asWebviewUri 是 Webview
   * 实例方法，实例由本类私有持有；此只读透传是 workspace.imageUri RPC 的
   * 最小暴露面）。视图未 resolve 时返回 undefined（调用方应答错误即可）。
   */
  public asWebviewUriOf(uri: vscode.Uri): string | undefined {
    return this.view !== undefined ? this.view.webview.asWebviewUri(uri).toString() : undefined;
  }

  public async resolveWebviewView(view: vscode.WebviewView): Promise<void> {
    this.view = view;
    const distUri = vscode.Uri.joinPath(this.extensionUri, "webview", "dist");
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [distUri],
    };
    view.webview.html = await this.buildHtml(view.webview, distUri);

    const sub: vscode.Disposable[] = [
      view.webview.onDidReceiveMessage((raw: unknown) => {
        void this.handleFromWebview(raw);
      }),
    ];
    view.onDidDispose(() => {
      sub.forEach((d) => d.dispose());
      if (this.view === view) {
        this.view = undefined;
      }
    });
  }

  /* ── host → webview ── */

  /** 投递一条消息给 live webview；视图未 resolve 时返回 false（缓冲已兜底） */
  public post(message: HostToWebviewMessage): boolean {
    const view = this.view;
    if (view === undefined) {
      return false;
    }
    void view.webview.postMessage(message).then(undefined, () => {
      /* 隐藏/销毁瞬间投递失败可接受：重放缓冲覆盖恢复场景 */
    });
    return true;
  }

  /** 转发 Python 核心事件（进重放缓冲 + 即时投递） */
  public forwardCore(sessionKey: string, message: CoreToHostMessage): void {
    const envelope = { kind: "core" as const, sessionKey, message };
    this.appendReplay(envelope);
    this.post(envelope);
  }

  /** 宿主侧错误（进程崩溃等）广播给 UI（进重放缓冲） */
  public postHostError(sessionKey: string | undefined, errorKind: string, message: string): void {
    const envelope = { kind: "host_error" as const, sessionKey, errorKind, message };
    this.appendReplay(envelope);
    this.post(envelope);
  }

  /** 子进程状态变迁广播给 UI（进重放缓冲） */
  public postProcessState(sessionKey: string, state: string, detail?: string): void {
    const envelope = { kind: "process_state" as const, sessionKey, state, detail };
    this.appendReplay(envelope);
    this.post(envelope);
  }

  /** 宿主 → webview 待发提示（编辑器右键/终端/Problems 联动，进重放缓冲） */
  public postHostPrompt(text: string, context?: UserMessageContext): void {
    const envelope = { kind: "host_prompt" as const, text, context };
    this.appendReplay(envelope);
    this.post(envelope);
  }

  private appendReplay(message: ReplayableWebviewMessage): void {
    this.replayBuffer.push(message);
    if (this.replayBuffer.length > REPLAY_LIMIT) {
      this.replayBuffer.shift();
    }
  }

  /* ── webview → host ── */

  private async handleFromWebview(raw: unknown): Promise<void> {
    if (typeof raw !== "object" || raw === null) {
      return;
    }
    const msg = raw as WebviewToHostMessage;
    switch (msg.kind) {
      case "ready":
        // 视图（重）挂载完成：重放全部缓冲事件（不 retainContext 的代价补偿）
        this.post({ kind: "replay", messages: [...this.replayBuffer] });
        return;
      case "core":
        this.sendToCoreEmitter.fire(msg);
        return;
      case "rpc": {
        const handler = this.rpcHandlers.get(msg.method);
        if (handler === undefined) {
          this.post({
            kind: "rpc_result",
            id: msg.id,
            ok: false,
            error: `unknown_method: ${msg.method}`,
          });
          return;
        }
        try {
          const result = await handler(msg.params);
          this.post({ kind: "rpc_result", id: msg.id, ok: true, result });
        } catch (err) {
          this.post({
            kind: "rpc_result",
            id: msg.id,
            ok: false,
            error: err instanceof Error ? err.message : String(err),
          });
        }
        return;
      }
      default:
        return;
    }
  }

  /* ── HTML / 资产 ── */

  private async buildHtml(webview: vscode.Webview, distUri: vscode.Uri): Promise<string> {
    const assets = await this.resolveAssets(webview, distUri);
    const nonce = crypto.randomBytes(16).toString("hex");
    const csp = [
      "default-src 'none'",
      `img-src ${webview.cspSource} data: https:`,
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `font-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
    ].join("; ");

    const scriptTag =
      assets.scriptUri !== undefined
        ? `<script type="module" nonce="${nonce}" src="${assets.scriptUri}"></script>`
        : "";
    const linkTag =
      assets.styleHref !== undefined ? `<link rel="stylesheet" href="${assets.styleHref}">` : "";
    const fallbackNote =
      assets.scriptUri === undefined
        ? `<div style="font-family:var(--vscode-font-family);color:var(--vscode-descriptionForeground);padding:12px;">webview 未构建：请先运行 npm --prefix extension/webview run build</div>`
        : "";

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zcode-Py</title>
${linkTag}
</head>
<body>
<div id="root">${fallbackNote}</div>
${scriptTag}
</body>
</html>`;
  }

  /**
   * 资产解析双兼容：
   * 1) 固定产物名 webview/dist/index.js（+ assets/index.css）——PLAN §4 前端约定；
   * 2) 回退读 dist/index.html 提取 vite 默认 hash 产物（type="module" script
   *    与 link[rel=stylesheet] 的相对路径）。
   */
  private async resolveAssets(webview: vscode.Webview, distUri: vscode.Uri): Promise<WebviewAssets> {
    // 1) 固定名
    try {
      const fixedScript = vscode.Uri.joinPath(distUri, "index.js");
      await vscode.workspace.fs.stat(fixedScript);
      const styleHref = await this.tryStyle(
        webview,
        vscode.Uri.joinPath(distUri, "assets", "index.css"),
      );
      return { scriptUri: webview.asWebviewUri(fixedScript).toString(), styleHref };
    } catch {
      /* 落到 hash 产物回退 */
    }
    // 2) hash 产物回退
    try {
      const htmlBytes = await vscode.workspace.fs.readFile(
        vscode.Uri.joinPath(distUri, "index.html"),
      );
      const html = Buffer.from(htmlBytes).toString("utf8");
      const scriptMatch = /<script[^>]+src="([^"]+)"/.exec(html);
      const styleMatch = /<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"/.exec(html);
      const scriptUri =
        scriptMatch !== null
          ? webview.asWebviewUri(this.assetUri(distUri, scriptMatch[1])).toString()
          : undefined;
      const styleHref =
        styleMatch !== null
          ? webview.asWebviewUri(this.assetUri(distUri, styleMatch[1])).toString()
          : undefined;
      return { scriptUri, styleHref };
    } catch {
      return { scriptUri: undefined, styleHref: undefined };
    }
  }

  private async tryStyle(webview: vscode.Webview, uri: vscode.Uri): Promise<string | undefined> {
    try {
      await vscode.workspace.fs.stat(uri);
      return webview.asWebviewUri(uri).toString();
    } catch {
      return undefined;
    }
  }

  /**
   * dist/index.html 内的相对资产引用 → dist 下的 URI。
   * 只接受以 "./" 开头的纯相对路径；含 ".." 或绝对/带 scheme 的引用一律拒绝
   * （资产必须落在 localResourceRoots 收窄的 dist 内，纵深防御）。
   */
  private assetUri(distUri: vscode.Uri, href: string): vscode.Uri {
    if (!href.startsWith("./") || href.includes("..") || href.includes(":")) {
      throw new Error("非法 webview 资产引用（仅接受 dist 内相对路径）");
    }
    return vscode.Uri.joinPath(distUri, href.slice(2));
  }

  public dispose(): void {
    this.sendToCoreEmitter.dispose();
  }
}
