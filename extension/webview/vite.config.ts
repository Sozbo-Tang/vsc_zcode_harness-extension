import { defineConfig } from "vite";

// base './' so asset URLs resolve inside the VSCode webview
// (webview origin is not the site root; absolute '/' breaks).
export default defineConfig({
  base: "./",
  // TSX without the react plugin: esbuild's automatic JSX runtime.
  esbuild: {
    jsx: "automatic",
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // 固定产物名（无 content hash）：扩展宿主侧模板直接
    // asWebviewUri(dist/index.js + dist/assets/index.css)，
    // 不需要读取/重写 dist/index.html（docs/research/vscode-integration.md §2.1）。
    rollupOptions: {
      output: {
        entryFileNames: "index.js",
        assetFileNames: "assets/[name][extname]",
        chunkFileNames: "chunks/[name].js",
      },
    },
  },
});
