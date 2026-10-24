/**
 * 内联补全（幽灵文本）stub —— R13 / PLAN M9：默认关闭，设置里开启。
 *
 * 注册入口读取配置 zcode-py.inlineCompletion.enabled（contributes 里
 * 默认 false）；M9 才有真 Provider（FIM 模型或 chat 模型模拟），
 * 当前即使配置打开也只登记占位（返回 undefined 不注册），避免半成品行为。
 */

import * as vscode from "vscode";

export function registerInlineCompletionIfEnabled(
  _context: vscode.ExtensionContext,
): vscode.Disposable | undefined {
  const enabled = vscode.workspace
    .getConfiguration("zcode-py")
    .get<boolean>("inlineCompletion.enabled", false);
  if (!enabled) {
    return undefined; // 默认关（R13 决策）
  }
  // M9：在此 registerInlineCompletionItemProvider（FIM / chat 模拟）
  return undefined;
}
