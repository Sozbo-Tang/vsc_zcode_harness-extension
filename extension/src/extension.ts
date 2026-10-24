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
import {
  EditorFollowToolCallParams,
  EditorListFilesParams,
  EditorOpenDiffParams,
  RPC_EDITOR_FOLLOW_TOOL_CALL,
  RPC_EDITOR_GET_SELECTION,
  RPC_EDITOR_LIST_FILES,
  RPC_EDITOR_PICK_FILE,
  UserMessageContext,
} from "./protocol";
import {
  collectDiagnostics,
  collectSelection,
  followToolCall,
  listWorkspaceFiles,
  openDiffReview,
  pickFile,
  readTerminalSelection,
  registerEditorBridge,
  selectionToContext,
} from "./editorBridge";

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

/** 编辑器右键四动作共用：选区 → 上下文 + 预填草稿 → 唤起侧边栏 */
async function promptFromSelection(rt: Runtime, template: string): Promise<void> {
  const sel = collectSelection();
  if (sel === null) {
    vscode.window.showInformationMessage("Zcode-Py: 请先在编辑器中选中代码");
    return;
  }
  const context: UserMessageContext = selectionToContext(sel);
  rt.provider.postHostPrompt(template, context);
  await vscode.commands.executeCommand("zcode.sidebar.focus");
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
  provider.registerRpcHandler("editor.openDiff", (params) => {
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

  // 编辑器桥 M3：编辑跟随 / 选区 / @补全 / 文件挑选
  provider.registerRpcHandler(RPC_EDITOR_FOLLOW_TOOL_CALL, (params) => {
    const p = params as Partial<EditorFollowToolCallParams> | undefined;
    if (p === undefined || typeof p.callId !== "string" || typeof p.name !== "string") {
      throw new Error("editor.followToolCall: 参数需为 {callId, name, args}");
    }
    return followToolCall({
      callId: p.callId,
      name: p.name,
      args: (p.args ?? {}) as Record<string, unknown>,
      result: p.result,
    });
  });
  provider.registerRpcHandler(RPC_EDITOR_GET_SELECTION, () => collectSelection());
  provider.registerRpcHandler(RPC_EDITOR_LIST_FILES, (params) => {
    const q = (params as Partial<EditorListFilesParams> | undefined)?.query;
    return { files: listWorkspaceFiles(typeof q === "string" ? q : "") };
  });
  provider.registerRpcHandler(RPC_EDITOR_PICK_FILE, async () => {
    const file = await pickFile();
    return file !== null ? { file } : null;
  });

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

    // —— M3 编辑器联动（PLAN §6.7）：一律「预填草稿 + 挂上下文 chip」，
    // 不代发——用户保持对输入的最终控制权；随后唤起侧边栏。
    vscode.commands.registerCommand("zcode.explainSelection", () =>
      promptFromSelection(rt, "请解释这段代码："),
    ),
    vscode.commands.registerCommand("zcode.optimizeSelection", () =>
      promptFromSelection(rt, "请优化这段代码，说明改动理由："),
    ),
    vscode.commands.registerCommand("zcode.writeTests", () =>
      promptFromSelection(rt, "请为这段代码编写测试："),
    ),
    vscode.commands.registerCommand("zcode.fixBug", () =>
      promptFromSelection(rt, "请修复这段代码中的 Bug："),
    ),
    vscode.commands.registerCommand("zcode.sendTerminalSelection", async () => {
      const text = await readTerminalSelection();
      if (!text) {
        vscode.window.showInformationMessage("Zcode-Py: 终端里没有选中内容（先在终端选中报错再右键）");
        return;
      }
      rt.provider.postHostPrompt("请诊断这个终端报错：", { terminalSel: text });
      await vscode.commands.executeCommand("zcode.sidebar.focus");
    }),
    vscode.commands.registerCommand("zcode.sendProblems", async () => {
      const problems = collectDiagnostics();
      if (!problems) {
        vscode.window.showInformationMessage("Zcode-Py: Problems 面板为空，没有可诊断的问题");
        return;
      }
      rt.provider.postHostPrompt("请诊断这些 Problems：", { problems });
      await vscode.commands.executeCommand("zcode.sidebar.focus");
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
