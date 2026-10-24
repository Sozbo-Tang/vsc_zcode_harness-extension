/**
 * 扩展入口（PLAN §4 / M1）：
 * - 组装 ApiKeyStore（SecretStorage）→ PythonProcessManager（每会话一个
 *   uv 子进程）→ ZcodeWebviewViewProvider（侧边栏桥）三方接线；
 * - 命令：新会话 / 停止生成 / 打开设置 / 打开侧边栏 / API 设置占位；
 * - 状态栏项占位（PLAN §6.7：🚢档位│模型│运行状态│上下文 —— M1 只显状态）；
 * - deactivate 返回 Promise：优雅回收全部子进程（EOF→SIGTERM→SIGKILL）。
 */

import * as vscode from "vscode";
import { ApiKeyStore } from "./secrets";
import {
  ControlMessage,
  HostToCoreMessage,
  looksLikeHostToCoreMessage,
} from "./protocol";
import { PythonProcessManager } from "./processManager";
import { ZcodeWebviewViewProvider } from "./webviewHost";
import { registerInlineCompletionIfEnabled } from "./inlineCompletion";
import { EditorOpenDiffParams, RPC_EDITOR_OPEN_DIFF } from "./protocol";
import { openDiffReview, registerEditorBridge } from "./editorBridge";

interface Runtime {
  channel: vscode.OutputChannel;
  keys: ApiKeyStore;
  manager: PythonProcessManager;
  provider: ZcodeWebviewViewProvider;
  statusBar: vscode.StatusBarItem;
  activeSessionKey: string | undefined;
}

let runtime: Runtime | undefined;

function agentDirFrom(extensionUri: vscode.Uri): string {
  // 扩展装在 <repo>/extension/，agent 在 <repo>/agent/ —— 由扩展位置推导，
  // 不硬编码绝对路径（远程开发时即远程扩展宿主侧路径）
  return vscode.Uri.joinPath(extensionUri, "..", "agent").fsPath;
}

function updateStatusBar(rt: Runtime): void {
  const key = rt.activeSessionKey;
  const proc = key !== undefined ? rt.manager.get(key) : undefined;
  const state = proc?.state ?? "idle";
  const icon = state === "running" ? "$(play)" : state === "starting" ? "$(loading~spin)" : "$(circle-outline)";
  rt.statusBar.text = `${icon} Zcode-Py · ${state}`;
  rt.statusBar.tooltip = `会话 ${key ?? "无"} · ${state}（M1 占位，档位/模型/上下文见 PLAN §6.7）`;
}

function sendToActiveSession(rt: Runtime, message: HostToCoreMessage): boolean {
  const key = rt.activeSessionKey;
  if (key === undefined) {
    rt.provider.postHostError(undefined, "no_active_session", "没有活跃会话，请先新建会话");
    return false;
  }
  const proc = rt.manager.get(key);
  if (proc === undefined) {
    rt.provider.postHostError(key, "session_not_found", `会话 ${key} 不存在或已停止`);
    return false;
  }
  const delivered = proc.send(message);
  if (!delivered) {
    rt.provider.postHostError(key, "send_failed", `消息发往会话 ${key} 失败（进程不在）`);
  }
  return delivered;
}

export function activate(context: vscode.ExtensionContext): void {
  const channel = vscode.window.createOutputChannel("Zcode-Py");
  const keys = new ApiKeyStore(context.secrets);
  const manager = new PythonProcessManager({
    agentDir: agentDirFrom(context.extensionUri),
    outputChannel: channel,
    resolveApiKeyEnv: () => keys.resolve(),
  });
  const provider = new ZcodeWebviewViewProvider(context.extensionUri);
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.name = "Zcode-Py";
  statusBar.command = "zcode.openSidebar";
  const rt: Runtime = { channel, keys, manager, provider, statusBar, activeSessionKey: undefined };
  runtime = rt;
  updateStatusBar(rt);

  // —— Python 核心 → webview ——
  manager.onCoreMessage((e) => {
    provider.forwardCore(e.sessionKey, e.message);
  }, undefined, context.subscriptions);

  manager.onProcessState((e) => {
    provider.postProcessState(e.sessionKey, e.state, e.detail);
    if (e.sessionKey === rt.activeSessionKey) {
      updateStatusBar(rt);
    }
  }, undefined, context.subscriptions);

  manager.onProcessError((e) => {
    provider.postHostError(e.sessionKey, e.errorKind, e.message);
    channel.appendLine(`[${e.sessionKey}] ${e.errorKind}: ${e.message}`);
    updateStatusBar(rt);
  }, undefined, context.subscriptions);

  // —— webview → Python 核心 ——
  provider.onSendToCore((req) => {
    if (!looksLikeHostToCoreMessage(req.message)) {
      provider.postHostError(req.sessionKey, "invalid_core_message", "消息信封不合法（缺 type 或未知类型）");
      return;
    }
    if (req.sessionKey !== undefined && req.sessionKey !== rt.activeSessionKey) {
      const target = manager.get(req.sessionKey);
      if (target === undefined) {
        provider.postHostError(req.sessionKey, "session_not_found", `会话 ${req.sessionKey} 不存在`);
        return;
      }
      if (!target.send(req.message)) {
        provider.postHostError(req.sessionKey, "send_failed", "发送失败（进程不在）");
      }
      return;
    }
    sendToActiveSession(rt, req.message);
  }, undefined, context.subscriptions);

  // —— 宿主 RPC（webview useHostRpc 的服务端）——
  provider.registerRpcHandler("echo", (params) => params);
  provider.registerRpcHandler("getState", () => ({
    activeSessionKey: rt.activeSessionKey,
    sessions: manager.sessionKeys().map((key) => ({
      sessionKey: key,
      state: manager.get(key)?.state ?? "unknown",
    })),
    workspaceRoots: PythonProcessManager.workspaceRoots(),
  }));

  // 编辑器桥：diff 审阅页（DiffReviewCard「打开对比页」；契约 protocol.ts
  // RPC_EDITOR_OPEN_DIFF，实现 editorBridge.openDiffReview）
  const diffProvider = registerEditorBridge(context);
  provider.registerRpcHandler(RPC_EDITOR_OPEN_DIFF, (params) => {
    const p = params as Partial<EditorOpenDiffParams> | undefined;
    if (
      p === undefined ||
      typeof p.id !== "string" ||
      typeof p.file !== "string" ||
      typeof p.oldText !== "string" ||
      typeof p.newText !== "string"
    ) {
      throw new Error("editor.openDiff: 参数需为 {id, file, oldText, newText}（均 string）");
    }
    return openDiffReview(diffProvider, p as EditorOpenDiffParams);
  });
  // TODO(M3 编辑跟随)：registerRpcHandler("editor.followToolCall", ...) ——
  // webview ToolCallCard 点击已按该约定发 RPC（占位，当前会收
  // unknown_method 应答并被 webview 静默忽略），实现见 editorBridge.ts 注释

  // —— 视图与命令 ——
  context.subscriptions.push(
    channel,
    manager,
    provider,
    statusBar,
    vscode.window.registerWebviewViewProvider(ZcodeWebviewViewProvider.viewType, provider, {
      // 调研决策（vscode-integration.md §1.5）：不开 retain，靠宿主真相+重放
      webviewOptions: { retainContextWhenHidden: false },
    }),
    vscode.commands.registerCommand("zcode.newSession", async () => {
      const proc = await manager.createSession();
      if (proc !== undefined) {
        rt.activeSessionKey = proc.sessionKey;
        updateStatusBar(rt);
        channel.appendLine(`[host] 会话已创建: ${proc.sessionKey}（等待 session_started）`);
      }
      await vscode.commands.executeCommand("zcode.sidebar.focus");
    }),
    vscode.commands.registerCommand("zcode.stopGeneration", () => {
      const cancel: ControlMessage = { type: "control", command: "cancel" };
      if (!sendToActiveSession(rt, cancel)) {
        vscode.window.showWarningMessage("Zcode-Py: 没有可停止的会话");
      }
    }),
    vscode.commands.registerCommand("zcode.openSidebar", () => {
      void vscode.commands.executeCommand("zcode.sidebar.focus");
    }),
    vscode.commands.registerCommand("zcode.apiSettings", async () => {
      // M1 占位：API 管理页在 webview（ApiView）；先唤起侧边栏
      await vscode.commands.executeCommand("zcode.sidebar.focus");
    }),
    vscode.commands.registerCommand("zcode.openSettings", () => {
      void vscode.commands.executeCommand("workbench.action.openSettings", "@ext:zcode-py.zcode-py");
    }),
  );

  const inline = registerInlineCompletionIfEnabled(context);
  if (inline !== undefined) {
    context.subscriptions.push(inline);
  }
}

export async function deactivate(): Promise<void> {
  const rt = runtime;
  runtime = undefined;
  if (rt !== undefined) {
    // 全量优雅回收（EOF→SIGTERM→SIGKILL，预算约 5s）；返回 Promise 等 VS Code
    await rt.manager.stopAll();
  }
}
