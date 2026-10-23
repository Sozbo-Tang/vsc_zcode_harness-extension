import * as vscode from "vscode";

/**
 * M0 skeleton activation: register placeholder commands only.
 * Process manager / protocol / webview host land in later milestones
 * (docs/PLAN.md §4, §7 M1+). Keep this file free of business logic.
 */
export function activate(context: vscode.ExtensionContext): void {
  const commands: Array<[string, () => void]> = [
    [
      "zcode.newSession",
      () => vscode.window.showInformationMessage("Zcode-Py: new session (M0 placeholder)"),
    ],
    ["zcode.openSidebar", () => vscode.commands.executeCommand("zcode.sidebar.focus")],
    [
      "zcode.apiSettings",
      () => vscode.window.showInformationMessage("Zcode-Py: API settings (M0 placeholder)"),
    ],
  ];

  for (const [id, run] of commands) {
    context.subscriptions.push(vscode.commands.registerCommand(id, run));
  }
}

export function deactivate(): void {
  // Nothing to tear down yet.
}
