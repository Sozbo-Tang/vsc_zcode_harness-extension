# Zcode-Py 项目规划文档

> Python 手搓 Agent Harness + VSCode 侧边栏插件 · 规划封版 R1–R32
> 本文档是全部规划共识的落地记录，实施以此为准。
> 生成日期：2026-10-24

---

## 1. 项目定位

做一个类 GitHub Copilot 的 VSCode 侧边栏插件：

- **Agent 大脑全部用 Python 手搓**——工具循环、权限系统、多厂商 LLM 接入、会话管理、上下文压缩、子代理、skills、MCP、工作流编排
- **TS 只写不可避免的薄壳**（约 10% 代码）——VSCode 扩展宿主仅支持 Node/TS，侧边栏壳（注册视图、拉起进程、转发消息）物理上无法用 Python
- 设计参照开源 [zai-org/ZCode](https://github.com/zai-org/ZCode)（Apache-2.0，TS monorepo）的 specs 与工具语义，**代码全部自写，不复制其代码**
- UI 按会话中逐轮确认的线框执行（见第 6 节）

## 2. 需求池（封版）

### 2.1 核心

| # | 需求 |
|---|---|
| R1 | Python 手搓 Agent 核心，TS 只做薄壳 |
| R2 | React 侧边栏聊天 UI（顶部无会话头行，模型/车钟/权限在底部控制条） |
| R3 | zcode 全套能力对标：工具系统 / 子代理 / skills / MCP / hooks / 计划模式 / 工作流 / 后台任务 / 定时任务 |
| R4 | 多厂商 API 接入：任意 BaseURL + Key + Model（anthropic / openai / gemini / deepseek / ollama / openrouter / 自定义） |
| R14 | 上下文压缩四层防御（详见 3.4） |

### 2.2 API 与模型

| # | 需求 |
|---|---|
| R5 | API 连接管理 UI：多套 Provider 配置 CRUD、API Key 存 SecretStorage、**测试连接（Ping）按钮**（✓延迟+模型回复 / ✗错误分类）、拉取模型列表按钮、模型档案（上下文长度/能力标记）、用量统计 |
| R6 | **车钟思考模式控件**：大和号战舰同款圆形仪表盘（黄铜外圈+黑盘面+中央把手拖拽+双指针回令动画+咔哒音效可关）；**刻度直接显示思考程度**（不显示"前进几"）；**按模型四模式自适应**：预算型（anthropic/gemini）/ 档位型（openai effort）/ 开关型（glm）/ 无思考型（整体置灰）；盘面由模型档案 `telegraphLevels[{label,param,ok}]` 数据驱动渲染，不支持的挡位置灰；换模型自动降档 |
| R9 | **Vision 中继**：主模型不支持视觉时，图片自动路由给用户指定的多模态模型转述成文字；三档预设（OCR 提取/通用描述/前端还原）+自定义；聊天流显示中继卡（模型名+转述全文可审计）；转述后原图从上下文移除省 token；未配置视觉模型时明确报错并一键跳转配置 |
| R19 | **性能指标**：首 token 耗时 + token 实时输出速度（token/s）；运行中实时显示（首 token 到达瞬间定格，速度每 0.5s 重算）；任务结束后显示上一轮任务的数值；点击弹出会话统计（平均/最差/峰值、按模型分组） |
| R25 | 子代理可指定不同模型（调研类子任务用便宜模型） |

### 2.3 编辑器与集成

| # | 需求 |
|---|---|
| R10 | 编辑器联动包：终端选中报错右键发送 Agent + Problems 面板诊断一键发送 + 编辑跟随（点工具卡/diff 卡跳转对应文件行） |
| R11 | multi-root 多根工作区（会话可选用哪个根目录作为 Agent cwd） |
| R12 | 会话导出 Markdown |
| R13 | 内联补全（幽灵文本）：**默认关闭**，设置里开启；FIM 模型或 chat 模型模拟 |
| R31 | Notebook 支持：Agent 能读写 .ipynb 单元格并执行回传输出 |

### 2.4 Git

| # | 需求 |
|---|---|
| R17 | 一键 git commit：读 `git log` 学习仓库既有提交风格 → 按范围（仅已暂存/全部变更）取 diff → 生成符合惯例的提交信息 → 预览可编辑 → 确认执行 |
| R22 | Commit 界面集成推送 GitHub：推送后**显示可点击的 commit 网址**（remote URL + sha 拼出）；`gh` 可用时提供「创建 PR」并显示 PR 网址；无 remote 时引导补 remote 或 `gh repo create`；凭据用系统 gh/credential-helper，**插件不经手 token** |

### 2.5 环境

| # | 需求 |
|---|---|
| R7 | 硬件可视化：CPU 型号/核数、GPU 型号/显存、RAM、OS 版本/架构（macOS·Linux·Windows 三平台探测） |
| R21 | **工作区语言检测**：只显示当前工作区特征文件命中的语言（pyproject.toml→Python、package.json→Node/TS、Cargo.toml→Rust、go.mod→Go、pom.xml/build.gradle→Java、CMakeLists.txt→C/C++）；对命中的语言探测工具链版本、包管理器、conda/nvm 等；出现语言但缺工具链→黄牌+安装指引；以上事实注入 Agent 上下文 |

### 2.6 安全

| # | 需求 |
|---|---|
| R15 | 隐私护栏：.env / *.pem / id_rsa 等模式匹配的文件在读取或发送前弹警告 |
| R29 | 危险命令黑名单：rm -rf、git push --force、sudo、dd 等永远弹确认，**优先级高于一切白名单与"总是允许"** |
| R30 | 检查点/一键回滚：每次用户消息前自动 git 快照（不动工作区，如 `git stash create` 类机制）；每个用户消息旁有「回滚到这」按钮；无 git 仓库时优雅降级 |

### 2.7 体验

| # | 需求 |
|---|---|
| R8 | 浏览器自动化：browser-use Python 库作为 Agent 工具（可选依赖组，未安装不注册）；截图 artifact 回聊天流展示；有头/无头配置 |
| R20 | 多语言界面（中/英） |
| R23 | WebSearch 工具（与 webfetch 并列的 harness 标配） |
| R24 | 跨会话持久记忆：MEMORY.md 小事实库，每次会话自动加载、变动自动更新 |
| R26 | 空状态欢迎页 + 建议提示词卡片 |
| R27 | 输入框 ↑ 调出历史输入 + 未发送草稿自动保留 |
| R28 | 收藏/固定重要回复（钉在会话顶部） |
| R16 | 多会话管理：列表/搜索/恢复/分叉（从某消息分支）/并行多开（每会话一个独立 Python 子进程） |

### 2.8 工程

| # | 需求 |
|---|---|
| R32 | 远程开发兼容：SSH / WSL / Dev Container / Codespaces 可用；设计期保证（workspaceFolders、无硬编码绝对路径、python 子进程在扩展宿主侧拉起）；M9 实测验收 |

### 2.9 明确排除（防范围爆炸）

沙箱 bash（seatbelt/bubblewrap）· 编辑器内联 Chat（Ctrl+I）· 命令产物图表自动展示 · GPU 实时监控 · 预算上限告警 · 模型定价→成本换算 · VSIX/Marketplace 发布 · 代码库语义索引（RAG）· 语音输入 · 多 Agent 辩论 · JetBrains 版 · 团队协作服务器 · 插件系统

## 3. 总体架构

### 3.1 架构图

```
┌────────────────────── VSCode (本地或远程宿主) ──────────────────────┐
│ React Webview：7 视图（聊天/会话/API/环境/工具·Skills·MCP/任务/设置）  │
│   内嵌组件：车钟、权限卡、diff审阅卡、中继卡、子代理卡、检查点按钮      │
│        ↕ postMessage                                                │
│ TS 薄壳 extension/ (~10%)                                           │
│  · 进程管理器（每会话一个 python 子进程，SIGTERM 优雅退出+崩溃重启）    │
│  · 编辑器桥（选区/@引用/diff页/终端/Problems/编辑跟随）               │
│  · SecretStorage（API Key）· InlineCompletionProvider（默认关）       │
│        ↕ stdio JSONL 自定义协议                                     │
├──────────────────────────────────────────────────────────────────────┤
│ Python Agent 核心 agent/zcode_py/ (~90%)                             │
│  · loop.py        主循环：流式→tool_use聚合→权限→执行→回灌→循环        │
│  · llm/           Provider 注册表（协议适配器）                       │
│      anthropic/openai/gemini/ollama/openrouter/custom               │
│      + telegraph.py 挡位→各协议思考参数映射                          │
│  · perf.py        首 token 耗时 / 输出速度测量                        │
│  · vision_relay.py 图片中继                                          │
│  · envprobe/      工作区特征文件→语言→工具链并行探测                   │
│  · tools/         注册表：read/write/edit(str_replace)/bash/glob/     │
│      grep/todo/webfetch/websearch/notebook/browser_use(可选组)/       │
│      search_history                                                │
│  · permissions.py 规则引擎 + DANGER_PATTERN 黑名单（最高优先级）       │
│  · checkpoints.py git 快照/回滚                                     │
│  · sessions/      JSONL 持久化 + 分叉                                │
│  · memory.py      MEMORY.md 跨会话记忆                               │
│  · compaction/    L1截断→L2淘汰→L3摘要→L4冷存                        │
│  · gitflow.py     commit 风格学习 + 生成 + push + URL 拼接            │
│  · subagents.py   嵌套受限 loop（上下文防火墙，可指定模型）            │
│  · 后期模块        skills / mcp_client / hooks / workflows / planmode │
└──────────────────────────────────────────────────────────────────────┘
```

关键决策：**一个会话 = 一个 Python 子进程**（隔离干净、并行多会话天然支持、崩溃不拖垮 UI）。

### 3.2 存储设计

| 存储 | 位置 | 内容 |
|---|---|---|
| 全局配置 | `~/.zcode-py/config/providers.json` | Provider 列表（**不含 Key**）、模型档案、车钟默认档、权限规则、转述提示词 |
| 密钥 | VSCode SecretStorage | `provider:{id}:apikey`，环境变量注入子进程，永不落盘明文 |
| 会话转录 | `~/.zcode-py/sessions/<sessionId>/` | `meta.json` + `transcript.jsonl`（追加式；**转录即 UI 事件流**，权限卡/diff 卡/中继卡都是 uiEvent，回放即完整还原界面；同时就是 L4 冷存储，天然可 grep，不引入 SQLite） |
| 工作区状态 | VSCode workspaceState | 当前会话、选用环境、multi-root 每会话根目录 |
| 检查点 | git 快照 sha（存转录） | 回滚 = checkout 快照（带确认） |

### 3.3 车钟 → 厂商参数映射（参考值，以模型档案声明为准）

| 档位 | anthropic 协议 | openai 协议 | gemini | glm 系 |
|---|---|---|---|---|
| 关闭 | 不发 thinking 块 | `reasoning_effort:"minimal"` 或不支持 | `thinkingBudget: 0` | `thinking.type:"disabled"` |
| 最低 | `budget_tokens: 2048` | `"low"` | `2048` | enabled |
| 低/中 | `8192 / 24576` | `"medium"` | `8192 / 16384` | enabled |
| 高（最大） | `65536`（按模型上限截断） | `"high"`/`"x-high"` 若有 | `24576` 或 `-1` 动态 | enabled 拉满 |

- 模型档案**声明**支持档位（`telegraphLevels`），车钟 UI 据此渲染与置灰——规则归 Python 核心算，UI 只消费
- 四种盘面模式：预算型 / 档位型 / 开关型（仅 关闭/思考 两位）/ 无思考型（置灰提示）

### 3.4 上下文压缩：四层防御（已决策）

```
L1 源头治理    工具输出截断(bash 超3万字符取头尾)、Read 默认行数上限、
              子代理隔离、图片中继后原图退场            ← M2 随工具系统做
L2 机械淘汰    上下文>50% 后旧工具结果换占位符
              （保 tool_use/tool_result 配对，只换 content）  ← M8
L3 自动摘要    85% 阈值触发结构化压缩（七要素：任务目标/关键决策/
              文件及状态/错误与解法/环境事实/待办/下一步）+
              保留尾部 ~15% 原文；/compact 手动触发；
              压缩可指定便宜模型（复用视觉中继的模式）      ← M8
L4 冷存储     完整转录在盘 + search_history 工具          ← M8
```

不做 RAG（决策记录见第 10 节）。配合 anthropic 协议 `cache_control` 缓存断点控成本（压缩会击穿缓存，阈值不宜过激）。

## 4. 仓库结构

```
Zcode_harness_vsc_extension/
├── extension/                     # TS 薄壳 + React webview
│   ├── package.json               # contributes: activityBar + sidebar webview + commands
│   ├── src/
│   │   ├── extension.ts           # 激活、命令注册
│   │   ├── processManager.ts      # spawn/restart python 子进程，JSONL 帧
│   │   ├── protocol.ts            # 消息类型（与 python 侧镜像，pytest 锁定）
│   │   ├── webviewHost.ts         # React webview 宿主 + postMessage 桥
│   │   ├── editorBridge.ts        # 选区/@引用/diff 页/终端/Problems/编辑跟随
│   │   ├── inlineCompletion.ts    # 幽灵文本（默认关）
│   │   └── secrets.ts             # API Key 存取
│   └── webview/
│       └── src/                   # vite + react + zustand + react-markdown +
│           ├── views/             #   react-virtuoso(虚拟滚动) + shiki(代码高亮,懒加载)
│           │   ChatView / SessionsView / ApiView /
│           │   EnvView / ToolsView / TasksView / SettingsView
│           └── components/
│               Telegraph(车钟) / PermissionCard / DiffReviewCard /
│               VisionRelayCard / SubagentCard / ToolCallCard /
│               PerfChip(性能格) / ContextMeter(上下文) / WelcomePage
├── agent/                         # Python 手搓核心
│   ├── pyproject.toml             # uv 管理，python>=3.12
│   │                              # 依赖：anthropic、openai、httpx
│   │                              # 可选组：[browser](browser-use)
│   └── zcode_py/                  # 模块见 3.1 架构图
├── docs/
│   ├── PLAN.md                    # 本文档
│   └── ROADMAP.md                 # 里程碑任务勾选追踪（实施时创建）
└── .gitignore
```

前端约定：零自定颜色，全 `--vscode-*` CSS 变量（深浅色自动跟随）；单 zustand store（view/sessions/activeSession/providers/env/tasks 切片）；`useHostRpc()` 封装 postMessage（correlation id 请求-响应 + 事件订阅）。

## 5. JSONL 协议 v1

**壳 → 核**：
- `init{workspaceRoots, settings, apiKeyEnv}`
- `user_message{text, context{selection?, fileRefs[], terminalSel?, problems?}}`
- `permission_response{id, decision: allow|deny|always}`
- `diff_review{id, accepted}`
- `control{new_session | resume<id> | cancel | compact | telegraph<level> | switch_model{providerId, modelId}}`
- `rollback{checkpointId}`
- `git_commit{repoRoot, scope: staged|all, push: bool}`

**核 → 壳**：
- `session_started{sessionId}`
- `thinking_delta{text}` / `stream_delta{text}` / `message_complete`
- `tool_call{callId, name, args, state: started|update|finished, result?, elapsedMs?}`
- `permission_request{id, tool, args}`
- `diff_proposed{id, file, oldText, newText}`
- `vision_relay{model, preset, description}`
- `telegraph_state{levels[], current, paramTranslation}`
- `perf{phase: streaming|done, ttftMs?, tokPerSec?, lastTurn{ttftMs, avgTokPerSec}}`
- `env_info{hardware, workspaceLangs[]}`
- `compaction{layer, report}`
- `checkpoint{id, sha, atMessageId}` · `rollback_result{ok}`
- `sessions_list` · `usage{tokens, byModel}`
- `ping_result{ok, latencyMs, errorKind}`
- `git_result{commitSha?, remoteUrl?, commitUrl?, prUrl?, error?}`
- `image_artifact{path}` · `error{kind, message}` · `result`

双侧手写但配 pytest 协议往返测试锁定字段。

## 6. UI 规格（已确认线框，最终版）

### 6.1 聊天视图（无会话头行）

```
┌─ Zcode-Py ───────────────────[＋新会话]─┐
│─────────────────────────────────────────│
│ 👤 用户                                  │
│ │ 把 @src/auth.py 重构成类形式          │
│ │ 📎 auth.py · 📎 选中: login() L42-67 │
│                                         │
│ ▸ 🧠 思考 12.4s                        │ ← 思考块(默认折叠)
│ ▸ 🔨 read src/auth.py          ✓ 34ms  │ ← 工具卡(点跳文件行=编辑跟随)
│ ▸ ✏️ edit src/auth.py   +38 −12        │ ← diff审阅卡
│   [打开对比页] [✓接受] [✗拒绝]          │
│ 🤖 流式回复文本███████▌                 │
│ ▸ 🖼 图片已由 gpt-5.3-vision 转述  ▾   │ ← 中继卡
│ ▸ 🤖 子代理·代码搜索            运行中 │ ← 子代理卡
│─────────────────────────────────────────│
│ 任务清单 ████████░░░░ 5/12  [展开▾]     │
│ 🚢高│glm-5.3▾│🛡普通│◔42%│⏱842ms ⚡45/s│ ← 控制条+性能格
│ ┌───────────────────────────────┐      │
│ │ 问点什么… (@引用  /  技能)      │ [➤] │
│ └───────────────────────────────┘      │
│ [📎文件][🖼图片][⊞选中代码][⚡Commit]    │
└─────────────────────────────────────────┘
```

性能格语义：运行中=首 token 到达瞬间定格 + 速度每 0.5s 实时；结束=显示上一轮数值；点击看会话统计详情。

### 6.2 车钟（大和号样式 · 思考程度刻度 · 模式自适应）

```
          最大
      ╭────┴────╮
   高 │  ╭───╮  │ 关闭
    ╲ │ ╱▒▒▒╲ │ ╱
  中  │▕ ⚓  ▏│   （低）
    ╱ │ ╲▒▒▒╱ │ ╲
   低 │  ╰─╥─╯  │
      ╰────╩────╯
        (把手拖拽)
  ─────────────────
  当前: 高 → budget_tokens 24576
```

黄铜外圈+黑盘面+白色刻度字；把手拖拽换挡有吸附咔哒（音效可关）；主指针即时、回令针延迟 0.5s 追上；盘面挡位由模型档案数据驱动（四模式见 3.3）。

### 6.3 权限卡 / 会话 / API 管理

```
权限卡:                          会话视图:
┌─ ⚠ 需要确认 ───────────┐     ┌─ 会话 ────────[🔍][＋]─┐
│ Agent 想执行:           │     │ 🔍 搜索历史消息…        │
│ $ python train.py …    │     │ ▾ 今天                  │
│ [✓允许][✓✓会话内总是]  │     │  ● 重构登录模块  2分钟前 │
│ [✗拒绝][⚙写规则]       │     │ ▾ 本周                  │
└────────────────────────┘     │  ○ ml-env 排查   周二   │
                               │  └ ↳ 分叉自…           │
                               └────────────────────────┘
```

```
API 连接管理:
┌─ API 连接 ───────────[＋新增]─┐
│ 🟢 GLM编程套餐 [默认]          │
│ ⚪ OpenRouter  ⚪ 本地 Ollama   │
│ ▼ 编辑：GLM 编程套餐           │
│  协议[anthropic▾] BaseURL[…a…] │
│  API Key [••••••] [👁]        │
│  [⚡测试连接 → ✓ 412ms 模型正常]│
│  模型[glm-5.3▾] [↘拉取列表]    │
│  档案 200k·🧠✓👁✓⚙✓           │
│  🖼 视觉中继模型 [______ ▾]    │
│  🗜 压缩专用模型   [______ ▾]  │
│  [💾保存] [🗑删除]             │
└────────────────────────────────┘
```

### 6.4 环境面板（只显示工作区出现的语言）

```
┌─ 环境 ────────────[🔄刷新]─┐
│ 💻 硬件                     │
│  CPU Apple M4 Pro · RAM 64G │
│  OS  macOS 26.1 · arm64     │
│ 📂 工作区语言检测            │
│  发现: pyproject.toml       │
│        package.json        │
│ ▼ Python ✓                  │
│  conda ●base py3.12         │
│  ☑ Agent执行用 [base ▾]     │
│ ▼ Node.js/TypeScript ✓      │
│  node v26.8.2 · npm 11      │
│ ⚠ Rust: 检测到 Cargo.toml   │
│   但未装工具链 [查看指引]     │
└─────────────────────────────┘
```

### 6.5 工具/Skills/MCP · 后台任务 · 设置

```
┌─[工具][Skills][MCP]──────┐  ┌─ 任务 ─────────────┐
│ 工具开关+默认权限[▾]      │  │ ▼ 运行中(2)         │
│ Skills 列表+启用+/命令    │  │  ⠿ train.py #8912   │
│ MCP 添加/状态灯/工具浏览   │  │    epoch 34/100 [⏹] │
└──────────────────────────┘  │ ▼ 定时任务(1) [＋新建]│
                              └─────────────────────┘
设置: 通用(语言/字号) · Agent(系统提示词/最大轮数) ·
压缩(阈值/压缩模型) · 视觉中继(三档预设+自定义) ·
内联补全(默认关) · 隐私护栏(模式列表) · 数据(会话目录/清除)
```

### 6.6 一键 Commit & Push

```
┌─ ⚡ 一键 Commit & Push ──────┐
│ 仓库 [▾] 范围(○暂存●全部)    │
│ ☑ 推送到 GitHub              │
│ ⠿ 12 文件 +830 −210         │
│ 建议: feat(auth): 重构登录…  │
│ [✓提交并推送][仅提交][取消]   │
│ ✓ 已提交 a1b2c3d            │
│ ✓ 已推送 origin/main         │
│ 🔗 github.com/you/repo/      │
│    commit/a1b2c3d    [打开↗] │
│ [🔀 创建 PR]                 │
└──────────────────────────────┘
```

### 6.7 原生集成

- 状态栏：`🚢高 │ glm-5.3 │ ⠿运行中 │ ◔42%`
- 命令面板：新会话/打开侧边栏/API设置/环境面板/一键提交/压缩会话…
- 编辑器右键：发给 Zcode-Py ▸ 解释/优化/写测试/修Bug
- 终端选中右键：让 Agent 诊断此报错；Problems 面板同
- 快捷键：唤起侧边栏 / 停止生成 / 新会话

## 7. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 骨架 | git/目录/TS壳+React七视图空壳 F5/python spawn+echo/远程开发设计原则落地 | 全链路往返 |
| M1 聊天+多厂商 | anthropic+openai 适配器、API管理页+Ping+拉模型、SecretStorage、流式 Markdown、车钟四模式、性能格；随后 gemini/ollama/openrouter | 任意厂商流式对话 |
| M2 工具+权限安全 | 工具注册表+只读三件套、权限引擎+危险命令黑名单、权限卡、write/edit/bash+diff 事件、工具卡、**检查点+回滚**、webfetch/websearch、L1 截断、隐私护栏 | 读码答问；写弹卡；可回滚 |
| M3 编辑器联动 | 选区上下文、@补全、diff 审阅、终端右键、Problems 联动、编辑跟随 | Copilot 式 edit 体验 |
| M4 多会话+记忆 | 持久化+回放、列表/搜索/恢复/分叉/并行、multi-root、MD 导出、MEMORY.md、输入历史/草稿、收藏、欢迎页 | 重启续聊 |
| M5 环境+Vision | 工作区语言检测+硬件卡、环境注入、vision 中继+中继卡+三档预设 | 贴图给纯文本模型能答 |
| M6 浏览器 | browser-use 可选组、截图 artifact、操作时间线 | 「打开网站并总结」 |
| M7 子代理+Skills | run_child（可指定模型）、Task 工具+子代理卡、SKILL.md(zcode 兼容)+斜杠命令 | 并行调研+自定义 skill |
| M8 harness 对标 | MCP client+管理页、hooks、计划模式、压缩 L2/L3/L4+压缩报告、Notebook 工具 | 接真实 MCP；跑 .ipynb |
| M9 工作流+后台+补全 | Python 工作流编排、后台任务面板、cron、内联补全(默认关)、远程开发实测(SSH/WSL) | 工作流脚本+远程跑通 |

工作量估计（业余时间）：M0–M2 约 1–2 周；M3–M5 约 1–2 周；M6–M7 约 1 周；M8–M9 约 2–3 周；全程 5–8 周。

## 8. 测试与质量

- **FakeProvider**：pytest 脚本化假流式（吐 delta/tool_use），全 loop 无网可测——M1 建立，开发命脉
- 协议往返测试锁 TS/Python 字段一致
- 权限引擎表驱动测试（含黑名单优先级）
- compaction 单测专测 Anthropic 约束（tool_use/tool_result 配对不断裂）
- 检查点单测（untracked 文件、无 git 仓库降级）
- webview vitest 组件测试；@vscode/test-electron 冒烟
- 每里程碑全量 pytest + 类型检查，ROADMAP.md 勾任务

## 9. 安全约束（实现验收条件）

1. 网络出口仅 http/https；Agent 发起的 webfetch/websearch/MCP HTTP 请求做 host 校验，拒绝 localhost/环回/私有/保留地址；用户显式配置的本地推理端点（如 Ollama :11434）属于主动配置例外
2. 凭据只从 SecretStorage/环境变量读取；源码、示例、测试中不得出现可用凭据字面量
3. 危险命令黑名单优先级高于一切白名单与"总是允许"
4. 插件不经手 GitHub 凭据（用系统 gh/credential-helper）
5. 写/删/命令默认询问；YOLO 模式需显式切换

## 10. 决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| Agent 核心语言 | Python 手搓，TS 只薄壳 | 用户明确要求手搓 Python；VSCode 宿主仅 Node API 是物理约束 |
| zcode 代码复用 | 参照 specs 自写，不复制 | zcode 是 TS monorepo，Python 手搓与其代码不兼容 |
| 进程模型 | 每会话一个 Python 子进程 | 隔离、并行、崩溃恢复简单 |
| LLM 接入 | Provider 抽象；默认 anthropic 协议端点（GLM: `https://open.bigmodel.cn/api/anthropic`），Key 后配 | tool_use 原生块+流式最干净；用户表示 Key"先放一边" |
| 上下文压缩 | 四层防御组合；**不做 RAG** | 对编码 Agent grep 检索已覆盖，RAG 收益/复杂度比低 |
| 内联补全 | 做但默认关 | 用户要求；与聊天主线互不干扰 |
| 内联 Chat(Ctrl+I) | 不做 | 工作量大，侧边栏+diff 审阅已覆盖主场景 |
| 环境面板 | 只显示工作区出现的语言 | 用户明确要求；比全机扫描更有行动价值 |
| 车钟形态 | 大和号圆形仪表样式，但刻度=思考程度，四模式数据驱动 | 用户两轮修订的合并结果 |
| 性能指标 | 运行中实时/结束后上轮值 | 用户明确规则 |
| Commit 授权 | 预览确认即授权，不再二次弹权限 | 减少打断；凭据不经手 |
| 会话头行 | 移除 | 用户明确要求，信息已在底部控制条 |

---

*本文档由规划会话生成于 2026-10-24；实施启动前如需求变更，先更新本文档再动代码。*
