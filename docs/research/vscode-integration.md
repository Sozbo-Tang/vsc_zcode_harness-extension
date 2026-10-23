# VSCode 扩展集成调研笔记（TS 薄壳 + React webview + 常驻 Python 子进程）

> 调研日期：2026-10-24。所有结论均来自本次联网抓取的官方文档 / 官方示例仓库 / vscode.d.ts 源码（已下载到本地核对原文行号）。行号引用基于 `https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.d.ts`（21241 行版本，2026-10-24 抓取）。

## 1. Webview 官方指南要点

来源：<https://code.visualstudio.com/api/extension-guides/webview>；API 原文见 vscode.d.ts；官方侧边栏示例见 <https://github.com/microsoft/vscode-extension-samples/tree/main/webview-view-sample>。

### 1.1 CSP 与 nonce

- webview 必须设置 Content-Security-Policy，最小策略是 `default-src 'none'` 再按需放开。官方指南给出的模板（用 `${webview.cspSource}` 占位）：
  ```html
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; img-src ${webview.cspSource} https:; script-src ${webview.cspSource}; style-src ${webview.cspSource};" />
  ```
  该策略"implicitly disables inline scripts and styles"，官方最佳实践是把脚本/样式抽到外部文件。
- 更严格的官方 sample 写法（script 用 nonce 白名单，而非放开整个 cspSource）——出自 `webview-view-sample/src/extension.ts`：
  ```html
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
  ...
  <script nonce="${nonce}" src="${scriptUri}"></script>
  ```
  nonce 生成函数（sample 原文）：32 位随机字母数字字符串。
- `webview.cspSource` 的官方定义（vscode.d.ts:10042-10050）："Content security policy source for webview resources. This is the origin that should be used in a content security policy rule"，示例 `img-src https: ${webview.cspSource} ...;`。
- 若 webview 需要连 dev server（HMR），CSP 还需加 `connect-src`（官方指南未在基础模板里给，属派生实践）。

### 1.2 asWebviewUri 与 localResourceRoots

- `asWebviewUri`（vscode.d.ts:10032-10039）原文："Webviews cannot directly load resources from the workspace or local file system using `file:` uris. The `asWebviewUri` function takes a local `file:` uri and converts it into a uri that can be used inside of a webview"。**永远不要硬编码资源 URI 格式**（webview 资源 scheme 随 VS Code 版本变过），一律经 `asWebviewUri` 转换。
- `localResourceRoots`（vscode.d.ts:9926-9933）原文："Root paths from which the webview can load local (filesystem) resources using uris from `asWebviewUri`. **Default to the root folders of the current workspace plus the extension's install directory.** Pass in an empty array to disallow access to any local resources."
- 官方指南同时警告 localResourceRoots "does not offer complete security protection on its own"，必须配合 CSP。推荐显式收窄：`localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, 'dist')]`。
- 其它安全清单（官方指南原文要点）：不需脚本就不开 `enableScripts: true`；清洗所有用户输入但"Never rely on sanitization alone for security"；`acquireVsCodeApi()` 返回的对象不得泄漏到全局作用域。

### 1.3 postMessage 双向通信

- 扩展 → webview：`webview.postMessage(message)`（vscode.d.ts:10026）。消息体须 JSON 可序列化；VS Code 1.57+ 起 `ArrayBuffer` 可高效传输。返回值语义（d.ts 原文）："Messages can only be posted to **live webviews** (i.e. either visible webviews or hidden webviews that set `retainContextWhenHidden`)"; 且 "A response of `true` does not mean that the message was actually received by the webview"——需要确认送达时，让 webview 回发确认消息。
- webview → 扩展：webview 内 `const vscode = acquireVsCodeApi(); vscode.postMessage({...})`；官方注释强调 "This function can only be invoked once per session"，实例必须保存复用。扩展端：`webview.onDidReceiveMessage(message => {...}, undefined, context.subscriptions)`。
- React 侧标准封装：`window.addEventListener('message', e => ...)` 收，`vscode.postMessage` 发（官方指南示例）。

### 1.4 setState/getState 持久化

- webview 内 `vscode.getState()` / `vscode.setState(obj)`（JSON 可序列化对象）。官方语义（指南原文）："This state is persisted even after the webview content itself is destroyed when a webview panel becomes hidden"，"The state is destroyed when the webview panel is destroyed."
- 对 **WebviewView（侧边栏视图）** 生命周期更关键（vscode.d.ts:10265-10310 WebviewViewResolveContext 原文）：视图折叠或用户切到别的活动栏时，WebviewView 本体保活但底层 iframe 文档会被**销毁释放**，再次可见时重建——`resolveWebviewView` 会再次被调用，此时 `context.state` 里就是上次 `setState` 存的对象（"The editor ensures that the persisted state is saved correctly when a webview is hidden and across editor restarts."）。
- 结论：React 应用必须（a）在卸载前把 UI 状态 `setState`；（b）`resolveWebviewView` 重建时从 `context.state` 注水；或（c）把真实状态放在扩展宿主侧（子进程/扩展全局变量），重建后扩展主动 push 全量状态到 webview——后者对本项目（状态源头在 Python 常驻进程）最自然。

### 1.5 retainContextWhenHidden 取舍

- vscode.d.ts 原文（两处：WebviewPanelOptions 10056-10080；registerWebviewViewProvider options 11763-11773）："**You cannot send messages to a hidden webview, even with `retainContextWhenHidden` enabled**"（隐藏时脚本被挂起）；"`retainContextWhenHidden` has a high memory overhead and should only be used if your view's context cannot be quickly saved and restored."
- 官方推荐替代：persisted state（setState/getState）+ 重建时恢复。**对本项目的决策**：侧边栏视图不开 retainContextWhenHidden，靠"扩展宿主持有真相 + 视图可见时重放状态"；这样切走再切回只是 React 重挂载，代价低。
- 注意隐藏 ≠ 销毁：用右键菜单取消勾选视图会触发 `onDidDispose`（WebviewView d.ts:10244-10252），provider 需处理重 resolve。

### 1.6 WebviewPanel 序列化（若用面板形态）

- 面板要跨 VS Code 重启恢复需 `WebviewPanelSerializer` + `registerWebviewPanelSerializer(viewType, ...)`，package.json 加激活事件 `"onWebviewPanel:viewType"`；`deserializeWebviewPanel(panel, state)` 的 state 即 setState 存的 JSON（官方指南）。首次创建仍需其它激活事件先激活扩展。

## 2. Vite + React 构建 webview 的成熟配置

来源：atman-33/vscode-extension-boilerplate（React 19 + Vite 模板，本次抓取源码核对）<https://github.com/atman-33/vscode-extension-boilerplate>；Stack Overflow 讨论 <https://stackoverflow.com/questions/75657294/use-vite-to-create-webview-in-vscode-extension>（本次直接访问被 403，未核对正文，仅从搜索摘要获知其讨论点）。

### 2.1 核心 trick：固定产物文件名，跳过"读 dist/index.html 再重写"

Vite 默认产物带 content hash（`index-a1b2c3.js`），路径不可预知。成熟模板的解法是**关掉 hash、固定输出名**，扩展侧直接拼固定路径再 `asWebviewUri`。boilerplate 的 `webview-ui/vite.config.ts` 原文：

```ts
export default defineConfig({
  base: "./",
  build: {
    emptyOutDir: true,
    outDir: resolve(import.meta.dirname, "../dist/webview/app"),
    rollupOptions: {
      input: resolve(import.meta.dirname, "index.html"),
      output: {
        assetFileNames: "assets/[name][extname]",   // 无 [hash]
        chunkFileNames: "chunks/[name].js",
        entryFileNames: "index.js",                  // 固定入口名
      },
    },
  },
  plugins: [react(), tailwindcss()],
});
```

扩展侧 HTML 由自己模板字符串生成（`src/utils/get-webview-content.ts` 原文），只引用两个固定产物：

```ts
const scriptUri = webview.asWebviewUri(Uri.joinPath(extensionUri, "dist", "webview", "app", "index.js"));
const styleUri  = webview.asWebviewUri(Uri.joinPath(extensionUri, "dist", "webview", "app", "assets", "index.css"));
// CSP: default-src 'none'; img-src ${cspSource} data:; style-src ${cspSource}; script-src 'nonce-${nonce}';
// <script type="module" nonce="${nonce}" src="${scriptUri}"></script>
```

要点：
- `base: "./"` + 固定文件名 → 产物引用全部由扩展侧模板控制，**不读取、不重写 dist/index.html**，构建链路最简单。
- 入口用 `<script type="module">`，Vite 的 ES module 产物直接可用；CSP 的 `script-src` 用 `'nonce-...'` 即可覆盖 module 脚本。
- 若追求 code-splitting 的确定性可加 `manualChunks`；否则关闭 hash 后多入口 chunk 名仍由 Vite 决定，`chunkFileNames` 固定名在同名冲突时会被动加后缀（Vite 行为，注意验证）。
- `emptyOutDir: true` 且 outDir 在扩展目录外层（`../dist/...`），需要 Vite 显式确认；模板已这样配。

### 2.2 开发期（HMR）与 portMapping 的坑

- vscode.d.ts:9934-9949 `WebviewOptions.portMapping` 原文：端口映射"only work for `http` or `https` urls. **Websocket urls (e.g. `ws://localhost:3000`) cannot be mapped to another port**"。
- 推论（工程实践）：Vite HMR 默认走 WebSocket，webview 里连 `ws://localhost:5173` 在远程场景/端口映射下不可靠。务实做法：开发期把 webview HTML 直接指向 dev server URL（`http://localhost:5173`，CSP 临时放开 `connect-src ws: http://localhost:*`），或干脆开发期禁用 HMR（`server.hmr: false`）用整页刷新。官方指南本身也建议"让 webview 尽量无状态"，降低对 HMR 保状态的依赖。

### 2.3 与主题/样式

- 官方 UX 指南：<https://code.visualstudio.com/api/ux-guidelines/webviews>。旧 `@vscode/webview-ui-toolkit` 已被微软废弃（本次未展开调研其替代品细节，未核实）。用 CSS 变量（`var(--vscode-*)`）跟主题即可（boilerplate 的 app.css 即此做法）。

## 3. SecretStorage API

来源：vscode.d.ts:8627-8663（SecretStorage / SecretStorageChangeEvent）、:8464（ExtensionContext.secrets）；远程指南 <https://code.visualstudio.com/api/advanced-topics/remote-extensions>。

```ts
// vscode.d.ts 原文签名（8627-8663）
export interface SecretStorage {
  keys(): Thenable<string[]>;                              // 列出本扩展存储的 key
  get(key: string): Thenable<string | undefined>;          // 不存在返回 undefined
  store(key: string, value: string): Thenable<void>;       // 只能存 string
  delete(key: string): Thenable<void>;
  readonly onDidChange: Event<SecretStorageChangeEvent>;   // { key: string }
}
context.secrets  // ExtensionContext 上的实例（d.ts:8464）
```

- 存取入口：`context.secrets.store('openai.apiKey', token)` / `await context.secrets.get('openai.apiKey')`。**只接受 string**（存对象需自行 JSON 序列化）。
- 变更监听：`context.secrets.onDidChange(e => { if (e.key === 'openai.apiKey') ... })`，事件体只有一个 `key` 字段（SecretStorageChangeEvent，d.ts:8627-8631 原文："The key of the secret that has changed."）。同扩展的任意窗口 store/delete 都会触发。
- 官方定位（d.ts 注释原文）："stored **encrypted**. The implementation ... will be different on each platform and the secrets **will not be synced across machines**"——不跨机器同步，多机需各自登录。
- 远程指南明确："passwords should not be stored in workspaceState/globalState, since these APIs store data in plaintext"——密钥只进 SecretStorage，不进 memento、不进配置文件。
- 注意：SecretStorage 挂在 ExtensionContext 上，需在激活时把 `context.secrets` 交给需要它的服务；webview 不能直接访问，取密钥走"webview → postMessage → 扩展 → secrets.get"。

## 4. child_process.spawn 长驻子进程的坑

来源：Node.js 官方文档 <https://nodejs.org/api/child_process.html>、<https://nodejs.org/api/readline.html>（2026-10-24 抓取）。

### 4.1 stdout 按行拆帧与跨 chunk 缓冲

- Node 文档**没有**明说 `data` 事件会跨行拆分——这是流语义的隐含行为；`child.stdout` 是 `stream.Readable`，`'data'` 回调给到的是任意大小的 Buffer 切片，一次 `data` 可能是半行、多行或两行各一半。**自行按 `\n` split 会丢帧**，必须做跨 chunk 缓冲。
- 官方按行方案是 `readline`（文档原文："The `node:readline` module provides an interface for reading data from a Readable stream (such as `process.stdin`) **one line at a time**"），对子进程 stdout 同样适用（任意 Readable 流）：
  ```ts
  import { createInterface } from 'node:readline';
  const rl = createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => dispatch(JSON.parse(line)));  // 按行帧协议
  rl.on('close', () => { /* input 流 'end' 后触发 */ });
  ```
  - `'line'` 事件在收到 `\n`/`\r`/`\r\n` 时触发；**流结束时若最后一行没有换行符也会触发**（官方原文），不会丢尾巴。
  - `crlfDelay: Infinity`：保证 `\r\n` 始终算一个换行（默认 100ms 内到达才算）。跨平台子进程（Windows 侧 Python 输出 CRLF）建议常开。
  - 若子进程输出为 NDJSON 流，readline + JSON.parse 即完整拆帧层；解析失败要 try/catch 单行降级，不能让异常打死监听器。
  - 性能：官方注明 `for await...of` 迭代 readline 比 `'line'` 事件慢，高频输出用事件 API。

### 4.2 事件语义：spawn / error / exit / close

- `'spawn'`（v15.1.0+）：在所有其它事件之前触发，可作为"进程确实起来了"的锚点。
- `'error'`：进程无法 spawn、无法 kill、发 IPC 消息失败、被 AbortSignal 终止时触发。**官方警告**："The `'exit'` event may or may not fire after an error ... guard against accidentally invoking handler functions multiple times"——`error` 和 `exit` 要防重入（用 `settled` 标志）。
- `'exit'` 原文："When the 'exit' event is triggered, child process stdio streams **might still be open**"——**判"进程彻底结束、stdout 读完"要用 `'close'`**："The 'close' event is emitted after a process has ended **and the stdio streams** of a child process have been closed ... will always emit after 'exit'"。回调 `(code, signal)` 二者其一必非 null。长驻进程的"异常退出→重启"逻辑应挂在 `close` 上，否则重启时可能还读得到旧进程的尾部输出。

### 4.3 优雅退出（SIGTERM）与强杀

- `child.kill()` 无参默认发 **SIGTERM**；返回 `true` 只代表 kill(2) 成功，"the signal delivered to the child process may not actually terminate the process"（进程可捕获 SIGTERM 不退出）。
- **Windows**：官方原文——`'SIGKILL'`、`'SIGTERM'`、`'SIGINT'`、`'SIGQUIT'` 都会被"terminate the process **forcefully and abruptly** (similar to `'SIGKILL'`)"，即 Windows 上没有 SIGTERM 优雅退出；Python 侧的 signal handler 不会被执行。跨平台工程方案：先走应用层 shutdown 消息（stdin 写入 `{"cmd":"shutdown"}`），超时（如 3-5s）未退出再 `kill()`，Windows 上等价强杀。
- `options.killSignal`（默认 `'SIGTERM'`）用于 timeout/abort 触发的 kill。
- PID 复用风险（官方原文）：kill 前进程若已死、PID 被复用，信号会发给新进程——退出流程以事件为准，不要盲发多次 kill。
- Linux：shell 拉起的孙进程不会随父进程 kill 而终止——**不要 `shell: true`**，直接 spawn python 可执行文件；若 Python 又拉自己的子进程，考虑进程组（`detached: true` + `process.kill(-pid)`，非 Windows）。

### 4.4 AbortSignal / 资源回收

- `options.signal`（AbortController）：`.abort()` 等价 kill，回调收 AbortError。但官方已废弃把它当资源清理用（**DEP0209** 原文："Using the signal option to destroy a long-lived child process as a resource cleanup mechanism is deprecated"）；扩展的 deactivate 清理用显式 `kill()`。
- 管道反压（官方原文）："If the subprocess writes to stdout in excess of that limit without the output being captured, the subprocess **blocks**"——必须持续消费 stdout/stderr；完全不读的流配 `stdio: 'ignore'`。长驻进程 stderr 也要接（至少记日志），否则 Python 报错时静默卡死。
- `child.stdin` 是 Writable；子进程若读 stdin 等全部输入，需 `end()` 才继续。写 stdin 前判断 `writableEnded`/`destroyed`，EPIPE 要捕获。
- `detached: true` + `stdio: 'ignore'` + `unref()` 是"父退出后子进程续命"的官方配方——**本项目不要用**（扩展停用时应带走 Python 进程），保持默认 attached，由 `deactivate()` 显式 kill。
- Windows 细节：`windowsHide: true` 防控制台窗口闪现；`.bat/.cmd` 不能被 `spawn(cmd)` 直接拉起（官方原文需 `shell` 或 `spawn('cmd.exe', ['/c', ...])`）——拉 Python 应解析到 `.exe`。
- spawn 失败（如 python 不存在）：表现是 `'error'` 事件（ENOENT），**不是** exit code 非 0；命令查找用 `options.env.PATH`（默认 process.env），自定义 env 时务必带上 PATH。
- 重启策略（工程实践，非官方文档内容）：指数退避 + 最大次数上限，`close` 事件里记录 `code/signal` 供诊断；重启前确保旧 child 的所有监听器与 readline 已关闭。

## 5. 扩展激活与 contributes（activityBar + 侧边栏 webview view）

来源：contribution points 参考 <https://code.visualstudio.com/api/references/contribution-points>（本次下载 .md 原文核对）；activation events 参考 <https://code.visualstudio.com/api/references/activation-events>；官方示例 package.json/extension.ts（webview-view-sample，本次抓取原文）。

### 5.1 package.json 写法（自定义 activityBar 容器 + webview 视图）

```jsonc
{
  "activationEvents": [],   // 1.74+ 视图激活事件隐式生成，可留空
  "main": "./out/extension.js",
  "contributes": {
    "viewsContainers": {
      "activitybar": [
        { "id": "my-explorer", "title": "My Explorer", "icon": "resources/icon.svg" }
      ]
    },
    "views": {
      "my-explorer": [                       // key = 容器 id
        { "type": "webview", "id": "myExt.myView", "name": "My View" }
      ]
    },
    "menus": {
      "view/title": [
        { "command": "myExt.refresh", "group": "navigation", "when": "view == myExt.myView" }
      ]
    }
  }
}
```

- `viewsContainers.activitybar`：必须 `id/title/icon`；icon 官方规范原文 "Size: Icons should be 24x24 and centered / single color / SVG recommended"，默认 60% 不透明度、hover/active 100%。
- `views`：容器可填内置 `explorer/scm/debug/test` 或自定义容器 id；用户打开视图时 VS Code 自动 emit `onView:${viewId}` 激活事件（官方原文）。
- **`"type": "webview"`**：官方 webview-view-sample 的 package.json 明确使用该字段（本次抓取原文核实：`{"type": "webview", "id": "calicoColors.colorsView", "name": "Calico Colors"}`），表示该视图由 `registerWebviewViewProvider` 填充而非 TreeView。注：contribution-points 文档正文的字段表未列出 `type`（本次核对的 .md 中未见），但官方 sample 即此写法，`"type": "tree"` 为缺省。
- 常用可选字段：`when`（上下文可见性）、`contextualTitle`（视图移出默认容器后的标题）、`icon`、`visibility`。

### 5.2 扩展侧注册（官方 sample extension.ts 原文骨架）

```ts
class ColorsViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'calicoColors.colorsView';
  resolveWebviewView(webviewView, _context, _token) {
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this._extensionUri],   // 收窄到扩展目录
    };
    webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);
    webviewView.webview.onDidReceiveMessage(data => { /* ... */ });
  }
}
// activate():
context.subscriptions.push(
  vscode.window.registerWebviewViewProvider(ColorsViewProvider.viewType, provider));
```

- `registerWebviewViewProvider(viewId, provider, options?)`（vscode.d.ts:11752-11775）：`options.webviewOptions.retainContextWhenHidden` 控制隐藏时保 iframe 上下文（见 §1.5 取舍）。
- `resolveWebviewView` 在视图**首次可见**时调用（d.ts:10338 原文："called when a view first becomes visible. This may happen when the view is first loaded or when the user hides and then shows a view again"）——即每次 iframe 被回收后重 resolve，provider 内要保存 `webviewView` 引用供主动 `postMessage`，且消息发送要容忍视图未 resolve（`this._view` 为 undefined）。
- 视图可见性：`webviewView.visible` / `onDidChangeVisibility`（d.ts:10270-10283）；右键取消勾选视图 → `onDidDispose`（不触发 visibility change）。
- `webviewView.show(preserveFocus?)` 可从命令里唤起视图。

### 5.3 激活事件要点

- 1.74.0 起，`contributes.views` 里声明的视图**自动生成** `onView:viewId` 激活事件，`activationEvents` 无需手写（官方原文："views contributed by your extension do not require a corresponding onView activation event declaration"）；`onCommand`（contributes.commands 声明的）同理。
- `onWebviewPanel:viewType` 仅用于面板恢复场景（serializer）；侧边栏 webview view 不需要。
- `*`（启动即活）官方劝阻："use this activation event ... only when no other activation events combination works"；常驻 Python 子进程若需"开机即起"，用 `onStartupFinished`（不拖慢启动，在所有 `*` 扩展后触发）。
- `deactivate()`：官方建议导出做清理，异步清理须返回 Promise——这里是 kill Python 子进程的官方位置。

## 6. 远程开发（SSH/WSL/Codespaces）下的子进程与 workspaceFolders

来源：<https://code.visualstudio.com/api/advanced-topics/remote-extensions>（官方"Supporting Remote Development and GitHub Codespaces"）；vscode.d.ts（workspace/WorkspaceFolder、env.remoteName）。

### 6.1 扩展到底跑在哪：UI vs Workspace extensions

- 官方架构原文：UI extensions "are always run on the user's local machine"，且"cannot directly access files in the remote workspace, or run scripts/tools installed in that workspace"；Workspace extensions "run on the same machine as where the workspace is located"——本地工作区在本地跑；Remote-SSH/WSL/Codespaces/Dev Containers 时跑在**远程机/容器里 VS Code Server 内的 Remote Extension Host**。
- **对本项目的直接结论**：扩展要 spawn 工作区里的 Python、读工作区文件 → 必须是 Workspace 扩展，package.json 配 `"extensionKind": ["workspace"]`（官方 sample 用 `["ui","workspace"]` 表示两栖，VS Code 自动择优；含子进程逻辑的扩展按官方指南"需要访问工作区文件/运行工具的扩展应标记为 workspace"）。于是 `child_process.spawn('python', ...)` 拉起的是**远程侧**的 python、用远程侧 PATH，用户装在 Windows 本机的解释器/包不会生效——环境探测、解释器查找必须在扩展宿主侧（远程）执行。
- spawn 的进程在远程侧还带来官方点名过的坑（原文）：用 Node 模块操纵 GUI/剪贴板会"cause the application to launch on the wrong side"，应改 `vscode.env.openExternal`、`vscode.env.clipboard`。
- 原生 Node 模块（.node 二进制）需注意 Electron/Node ABI 双份打包（electron-rebuild 编译的模块在远程 VS Code Server 的标准 Node 下会失败）；纯 spawn Python 不受影响，但若打包随扩进程分发的二进制要按 `process.arch`、Alpine（`/etc/alpine-release`）适配（均为官方指南原文建议）。
- 不要依赖 `~/.vscode`、`~/.config/Code` 等路径约定（官方警告会出问题）；持久化用 `context.workspaceState/globalState`、`storageUri/globalStorageUri` + `vscode.workspace.fs`。

### 6.2 探测远程与 extensionKind

- `vscode.env.remoteName`（vscode.d.ts:10832-10842 原文）："The name of a remote. Defined by extensions, popular samples are `wsl` for the Windows Subsystem for Linux or `ssh-remote` for remotes using a secure shell. *Note* that the value is `undefined` when there is no remote extension host **but that the value is defined in all extension hosts (local and remote) in case a remote extension host exists**. Use `Extension.extensionKind` to know if a specific extension runs remote or not."
  - 即：判断"当前是否远程"不能只看 `remoteName`（本地 EH 里也有值），要 `remoteName !== undefined` 且（必要时）结合 `vscode.extensions.getExtension(<id>)!.extensionKind`。
- `vscode.env.uiKind === UIKind.Web` 表示浏览器端（Codespaces 浏览器形态）。
- 测试建议（官方）：在限制端口的环境（Codespaces/容器/受限防火墙 SSH）里可用的扩展，通常 WSL 等宽松环境也可用；可直接在远程环境 F5 调试，或 `vsce package` 出 VSIX 在远程装。

### 6.3 workspaceFolders 用法

```ts
// vscode.d.ts:13764-13782
export interface WorkspaceFolder {
  readonly uri: Uri;    // 官方注释：特意用 Uri 类型以支持非本地盘工作区
  readonly name: string;
  readonly index: number;
}
workspace.workspaceFolders: readonly WorkspaceFolder[] | undefined  // 无工作区时 undefined
workspace.getWorkspaceFolder(uri): WorkspaceFolder | undefined
workspace.onDidChangeWorkspaceFolders: Event<WorkspaceFoldersChangeEvent>
```

- **无工作区窗口**（File 菜单单开文件）`workspaceFolders` 为 `undefined`——常驻 Python 进程的 cwd/项目根不能假设存在，需兜底（如 `os.homedir()` 或提示用户开文件夹）。
- 官方注释原文警告：`onDidChangeWorkspaceFolders` "**will not fire if the first workspace folder is added, removed or changed**"，因为那种情况下扩展会被终止重启——首个文件夹变化靠激活事件重新走 activate。
- 远程下 folder uri 的 scheme：本地为 `file:`；Remote-SSH/WSL/容器下工作区 URI 使用 `vscode-remote://` scheme（authority 形如 `ssh-remote+host`、`wsl+distro`，官方 Troubleshooting 文档 <https://code.visualstudio.com/docs/remote/troubleshooting> 有 `vscode-remote://ssh-remote+...` 形态的示例）。**派生要点**（社区高频坑，官方 issue #284417/#301167 佐证）：拿到 folder uri 后不要用 `vscode.Uri.file(uri.fsPath)` 重建 URI（会把 scheme 抹成 `file:`）；需要本地路径给 spawn 用时取 `uri.fsPath` 即可（Workspace 扩展宿主与工作区同机，fsPath 就是宿主侧真实路径）；要访问文件优先 `vscode.workspace.fs`（自动跑在正确机器上）。
- 多根工作区：按 `index` 排序取第一个作默认根，用 `onDidChangeWorkspaceFolders` 跟踪增删；Python 子进程 cwd 绑定 folder 时，folder 被移除要处理（重启或终止）。

## 7. 对本项目实现的直接决策清单

1. 侧边栏形态：`viewsContainers.activitybar` + `views`（`"type": "webview"`），`activationEvents: []`；需要常驻进程随启动拉起则加 `onStartupFinished`。
2. webview 不开 `retainContextWhenHidden`；状态真相放扩展宿主 + Python 进程，`resolveWebviewView` 每次重放全量状态；React 内轻 UI 状态用 `getState/setState` 注水兜底。
3. Vite：固定产物名（无 hash）+ 扩展侧模板 HTML 直接 `asWebviewUri(dist/index.js)`；CSP 用 `script-src 'nonce-...'`；HMR 仅开发期放开 `connect-src`，注意 ws 不能被 portMapping 映射。
4. 密钥只进 `context.secrets`（string），`onDidChange` 联动刷新内存态；严禁 globalState/workspaceState。
5. Python 子进程：`spawn(pythonExe, args, { windowsHide: true })`（禁 shell）；stdout 用 `readline.createInterface` 拆行（NDJSON 帧），stderr 也要消费；退出以 `close` 事件为准做重启（退避+上限），`error` 与 `exit` 防重入；关闭流程 = stdin 发 shutdown 消息 → 超时 `kill()`（默认 SIGTERM；Windows 无优雅信号，等价强杀）；deactivate() 返回清理 Promise；不使用 detached/unref/AbortSignal-清理（DEP0209）。
6. 远程：`extensionKind: ["workspace"]`，spawn 与解释器探测天然落在远程宿主侧；cwd 用 `workspaceFolders[0].uri.fsPath` 并处理 undefined 与 onDidChangeWorkspaceFolders；探测远程用 `env.remoteName`（注意本地 EH 也有值）。

## 附：本次核对过的原始材料

- 官方 webview 指南：<https://code.visualstudio.com/api/extension-guides/webview>
- vscode.d.ts（main 分支，21241 行）：<https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.d.ts>（关键行：8627-8663 SecretStorage、8464 context.secrets、9898-9960 WebviewOptions、10026 postMessage、10032-10050 asWebviewUri/cspSource、10056-10080 retainContextWhenHidden、10225-10350 WebviewView/ResolveContext/Provider、11752-11775 registerWebviewViewProvider、13764-13782 WorkspaceFolder、13883 onDidChangeWorkspaceFolders、10832-10842 env.remoteName）
- 官方 webview-view-sample（package.json / src/extension.ts，抓取原文）：<https://github.com/microsoft/vscode-extension-samples/tree/main/webview-view-sample>
- contribution points（下载 .md 核对）：<https://code.visualstudio.com/api/references/contribution-points>
- activation events：<https://code.visualstudio.com/api/references/activation-events>
- 远程扩展指南：<https://code.visualstudio.com/api/advanced-topics/remote-extensions>
- Remote Troubleshooting（vscode-remote URI 形态）：<https://code.visualstudio.com/docs/remote/troubleshooting>
- Node child_process：<https://nodejs.org/api/child_process.html>
- Node readline：<https://nodejs.org/api/readline.html>
- Vite+React 模板（抓取 vite.config.ts / get-webview-content.ts / simple-view-provider.ts 原文）：<https://github.com/atman-33/vscode-extension-boilerplate>
- 未核对成功：Stack Overflow 75657294 正文（403，仅搜索摘要）
