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
  },
});
