/**
 * M0 placeholder app. The seven views (Chat / Sessions / API / Env /
 * Tools·Skills·MCP / Tasks / Settings) land in later milestones
 * (docs/PLAN.md §4). Zero custom colors: only --vscode-* CSS variables.
 */
export function App() {
  return (
    <main style={{ padding: "12px", fontFamily: "var(--vscode-font-family)" }}>
      <h1 style={{ fontSize: "var(--vscode-font-size-h3)", margin: "0 0 8px" }}>
        Zcode-Py
      </h1>
      <p
        style={{
          margin: 0,
          color: "var(--vscode-descriptionForeground)",
          fontSize: "var(--vscode-font-size-body)",
        }}
      >
        M0 webview skeleton — chat views arrive in later milestones.
      </p>
    </main>
  );
}
