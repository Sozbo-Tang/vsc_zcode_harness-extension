/**
 * 编辑器桥（R10 / PLAN M3）：选区上下文、@文件引用、diff 审阅页、
 * 终端选中报错、Problems 面板诊断、编辑跟随（点工具卡跳文件行）。
 *
 * 本文件已落地：diff 审阅页（webview RPC "editor.openDiff" 的服务端，
 * 消息契约见 protocol.ts RPC_EDITOR_OPEN_DIFF）。
 * 其余仍为 M3 TODO —— 尤其 "editor.followToolCall"（编辑跟随）：
 * webview 侧 ToolCallCard 点击即发该 RPC（占位），宿主在此注册
 * registerRpcHandler(RPC_EDITOR_FOLLOW_TOOL_CALL, ...) 后生效。
 */

import * as vscode from "vscode";
import {
  EditorOpenDiffParams,
  RPC_EDITOR_FOLLOW_TOOL_CALL,
  UserMessageContext,
} from "./protocol";

/* ── diff 审阅页 ── */

/** 虚拟文档 scheme：承载 diff 左侧（改动前）与文件不存在时的右侧（新文件全文） */
const DIFF_SCHEME = "zcode-diff";

/** 内容缓存上限（条）：diff 提案 id/key → 文本，防长会话无限增长 */
const CONTENT_CACHE_LIMIT = 200;

export class DiffReviewContentProvider implements vscode.TextDocumentContentProvider {
  private readonly contents = new Map<string, string>();

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.path) ?? "";
  }

  /** key 约定：`/<diffId>/old` 与 `/<diffId>/new`（与 openDiffReview 配对） */
  put(key: string, text: string): void {
    if (this.contents.size >= CONTENT_CACHE_LIMIT) {
      const oldest = this.contents.keys().next().value;
      if (oldest !== undefined) {
        this.contents.delete(oldest);
      }
    }
    this.contents.set(key, text);
  }

  dispose(): void {
    this.contents.clear();
  }
}

/** 注册编辑器桥（diff 虚拟文档 scheme 等）；随扩展生命周期销毁 */
export function registerEditorBridge(context: vscode.ExtensionContext): DiffReviewContentProvider {
  const provider = new DiffReviewContentProvider();
  context.subscriptions.push(
    vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, provider),
  );
  return provider;
}

/**
 * 相对路径按 multi-root 逐根探测（R11）；绝对路径直接用。
 * 返回磁盘上真实存在的文件 Uri，找不到返回 undefined。
 */
async function resolveExistingFile(file: string): Promise<vscode.Uri | undefined> {
  const candidates = file.startsWith("/")
    ? [vscode.Uri.file(file)]
    : (vscode.workspace.workspaceFolders ?? []).map((root) => vscode.Uri.joinPath(root.uri, file));
  for (const uri of candidates) {
    try {
      await vscode.workspace.fs.stat(uri);
      return uri;
    } catch {
      /* 试下一个根 */
    }
  }
  return undefined;
}

/**
 * 打开 diff 审阅页（DiffReviewCard「打开对比页」→ RPC editor.openDiff）：
 *  - 左侧 = 虚拟文档（oldText，scheme zcode-diff）
 *  - 右侧 = 磁盘真实文件优先（文件尚不存在时退化为 newText 虚拟文档）
 * 抛出的异常由 webviewHost RPC 包装为 ok:false 应答回 webview 展示。
 */
export async function openDiffReview(
  provider: DiffReviewContentProvider,
  params: EditorOpenDiffParams,
): Promise<void> {
  const { id, file, oldText, newText } = params;
  provider.put(`/${id}/old`, oldText);
  const left = vscode.Uri.from({ scheme: DIFF_SCHEME, path: `/${id}/old` });

  const existing = await resolveExistingFile(file);
  let right: vscode.Uri;
  let title: string;
  if (existing !== undefined) {
    // 真实文件作右侧：审阅页可直接编辑保存（Agent 只提案、落盘仍走用户）
    right = existing;
    title = `${file}（审阅）`;
  } else {
    provider.put(`/${id}/new`, newText);
    right = vscode.Uri.from({ scheme: DIFF_SCHEME, path: `/${id}/new` });
    title = `${file}（新文件 · 审阅）`;
  }
  await vscode.commands.executeCommand("vscode.diff", left, right, title, { preview: true });
}

/* ── M3 TODO：编辑跟随（占位约定） ── */
/**
 * RPC `${RPC_EDITOR_FOLLOW_TOOL_CALL}`（params: EditorFollowToolCallParams）
 * 的宿主实现：按工具类型从 args 解析 file_path/path + 行号 →
 * workspace.openTextDocument + window.showTextDocument({ selection })。
 * webview 侧 ToolCallCard 点击已发该 RPC 并静默吞掉 unknown_method
 * 应答 —— 在 extension.ts 注册处理器之前该按钮是纯占位。
 */
export const FOLLOW_TOOL_CALL_RPC = RPC_EDITOR_FOLLOW_TOOL_CALL;

/* ── M3 TODO：选区上下文（stub 保持 M1 形状） ── */

/**
 * 收集当前编辑器上下文（M3 实现真逻辑：activeTextEditor.selection、
 * @引用补全源、终端选中、languages.getDiagnostics）。
 * M1 返回空对象——协议侧字段全部可选，空上下文即无联动。
 */
export async function collectEditorContext(): Promise<UserMessageContext> {
  return {};
}
