/**
 * 编辑器桥（R10 / PLAN M3）：选区上下文、@文件引用、diff 审阅页、
 * 终端选中报错、Problems 面板诊断、编辑跟随（点工具卡跳文件行）。
 *
 * 已落地：diff 审阅页（editor.openDiff）、编辑跟随（editor.followToolCall）、
 * 选区获取（editor.getSelection）、@补全文件列表（editor.listFiles）、
 * 文件挑选（editor.pickFile）、终端选中读取、Problems 收集。
 */

import * as vscode from "vscode";
import {
  EditorFollowToolCallParams,
  EditorOpenDiffParams,
  EditorSelectionResult,
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

/* ── M3：编辑跟随（ToolCallCard 点击 → RPC editor.followToolCall） ── */

interface FollowTarget {
  file: string;
  line?: number;
}

/** 按工具类型从入参/结果解析跳转目标：read/edit/write 用入参，grep 用首个命中 */
function followTargetFrom(params: EditorFollowToolCallParams): FollowTarget | undefined {
  const args = params.args ?? {};
  const strArg = (v: unknown): string | undefined =>
    typeof v === "string" && v.length > 0 ? v : undefined;
  if (params.name === "grep") {
    const result = params.result as
      | { matches?: Array<{ file?: unknown; line?: unknown }> }
      | undefined;
    const first = result?.matches?.[0];
    if (first !== undefined) {
      return {
        file: strArg(first.file) ?? strArg(args.path) ?? "",
        line: typeof first.line === "number" && first.line > 0 ? first.line : undefined,
      };
    }
    return { file: strArg(args.path) ?? "" };
  }
  const file = strArg(args.file_path) ?? strArg(args.path) ?? strArg(args.file);
  if (file === undefined) {
    return undefined;
  }
  const offset = args.offset ?? args.line;
  const line =
    typeof offset === "number" && offset > 0 ? Math.floor(offset) : undefined;
  return { file, line };
}

export async function followToolCall(params: EditorFollowToolCallParams): Promise<void> {
  const target = followTargetFrom(params);
  if (target === undefined || target.file.length === 0) {
    vscode.window.showInformationMessage("Zcode-Py: 该工具卡没有可跳转的文件位置");
    return;
  }
  const uri = await resolveExistingFile(target.file);
  if (uri === undefined) {
    vscode.window.showWarningMessage(`Zcode-Py: 找不到文件 ${target.file}`);
    return;
  }
  const doc = await vscode.workspace.openTextDocument(uri);
  const line =
    target.line !== undefined
      ? Math.min(Math.max(target.line, 1), doc.lineCount)
      : 1;
  const pos = new vscode.Position(line - 1, 0);
  await vscode.window.showTextDocument(doc, {
    selection: new vscode.Range(pos, pos),
    preview: true,
  });
}

/* ── M3：选区上下文 / @补全 / 文件挑选 ── */

/** 相对路径显示（multi-root 下相对所在根；盘外文件回退绝对路径） */
function displayPath(uri: vscode.Uri): string {
  try {
    return vscode.workspace.asRelativePath(uri, false);
  } catch {
    return uri.fsPath;
  }
}

/** 收集当前编辑器选区；无编辑器 / 无选中 / 纯空白时返回 null */
export function collectSelection(): EditorSelectionResult | null {
  const editor = vscode.window.activeTextEditor;
  if (editor === undefined) {
    return null;
  }
  const sel = editor.selection;
  if (sel.isEmpty) {
    return null;
  }
  const text = editor.document.getText(sel);
  if (!text.trim()) {
    return null;
  }
  return {
    file: displayPath(editor.document.uri),
    startLine: sel.start.line + 1,
    endLine: sel.end.line + 1,
    text,
  };
}

/** 把选区格式化为 user_message.context.selection（文件与行号随文可溯） */
export function selectionToContext(sel: EditorSelectionResult): UserMessageContext {
  return {
    fileRefs: [sel.file],
    selection: `# ${sel.file} L${sel.startLine}-${sel.endLine}\n${sel.text}`,
  };
}

const FILE_EXCLUDE = "**/{node_modules,.git,dist,build,.venv,venv,__pycache__,.next,target,out}/**";
const LIST_FILES_SCAN = 500;
const LIST_FILES_CAP = 20;

/** @补全：按前缀过滤工作区文件（相对路径，最多 20 条） */
export async function listWorkspaceFiles(query: string): Promise<string[]> {
  const uris = await vscode.workspace.findFiles("**/*", FILE_EXCLUDE, LIST_FILES_SCAN);
  const rels = uris.map((u) => displayPath(u));
  const q = query.trim().toLowerCase();
  const filtered = q === "" ? rels : rels.filter((r) => r.toLowerCase().includes(q));
  filtered.sort();
  return filtered.slice(0, LIST_FILES_CAP);
}

/** 📎文件：QuickPick 挑选（扫描上限 1000；fuzzy 匹配交给 QuickPick） */
export async function pickFile(): Promise<string | null> {
  const uris = await vscode.workspace.findFiles("**/*", FILE_EXCLUDE, 1000);
  const picks = uris.map((u) => ({ label: displayPath(u) }));
  const picked = await vscode.window.showQuickPick(picks, {
    placeHolder: "选择要引用的文件（可输入过滤）",
    matchOnDescription: false,
  });
  return picked !== undefined ? picked.label : null;
}

/* ── M3：终端选中（官方 copySelection+剪贴板通道读取，尽力恢复剪贴板） ── */

export async function readTerminalSelection(): Promise<string> {
  const before = await vscode.env.clipboard.readText();
  try {
    await vscode.commands.executeCommand("workbench.action.terminal.copySelection");
  } catch {
    return "";
  }
  const text = (await vscode.env.clipboard.readText()).trim();
  if (before !== text) {
    await vscode.env.clipboard.writeText(before);
  }
  return text;
}

/* ── M3：Problems 面板（错误+警告，上限 30 条） ── */

const DIAGNOSTICS_CAP = 30;

export function collectDiagnostics(): string {
  const severityName = (s: vscode.DiagnosticSeverity): string =>
    s === vscode.DiagnosticSeverity.Error
      ? "error"
      : s === vscode.DiagnosticSeverity.Warning
        ? "warning"
        : "info";
  const lines: string[] = [];
  for (const [uri, diags] of vscode.languages.getDiagnostics()) {
    for (const d of diags) {
      if (lines.length >= DIAGNOSTICS_CAP) {
        lines.push("…（超过 30 条已截断）");
        return lines.join("\n");
      }
      lines.push(
        `[${severityName(d.severity)}] ${displayPath(uri)}:${d.range.start.line + 1}: ${d.message}`,
      );
    }
  }
  return lines.join("\n");
}
