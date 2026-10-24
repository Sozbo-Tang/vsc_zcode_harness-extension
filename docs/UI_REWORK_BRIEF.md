# Zcode-Py Webview UI 改版任务书（交接给外部 AI / Kimi K3）

> 目的：当前 UI 功能完整但视觉粗糙（用户原话「太丑了」）。本文档给你
> （改版执行者）完成改版所需的全部上下文。**只改视觉与布局，不改数据流、
> 协议与业务逻辑。**

---

## 1. 项目与技术栈一句话

VSCode 侧边栏扩展的 React webview（聊天式 AI 编码助手）。技术栈：
**React 18 + TypeScript (strict) + Vite + zustand + react-markdown**。
无路由、无 UI 组件库、无 Tailwind——纯手写 CSS（单文件 styles.css）。

## 2. 硬性约束（违反即返工）

1. **零自定颜色**：所有颜色必须用 `--vscode-*` CSS 变量（深浅色主题自动
   跟随）。允许的例外：`transparent`、通用关键字、以及
   `var(--vscode-xxx, fallback)` 的 fallback 位。禁止 `#rrggbb`/`rgb()`/`rgba()`
   自定色值与色相旋转（hue-rotate 滤镜）。
2. **改版验收门禁**（改完必须全部通过，命令在仓库根执行）：
   ```bash
   npm --prefix extension/webview run build   # vite build，退出码 0
   cd extension/webview && npx tsc -p .       # strict 类型检查，退出码 0
   ```
3. **不许动的文件**：
   - `extension/webview/src/protocol.ts`（协议镜像）、`host/`（transport/useHostRpc）
   - `store/store.ts` 的**逻辑**（action/归约器/reducer 分支）。允许且仅允许：
     新增纯展示类字段（如展开态）、拆分组件时搬运渲染代码；`applyEvent`
     及一切 `hostSendCore` 调用点不许改语义。
   - `extension/src/**`（TS 宿主）、`agent/**`（Python 核心）、`docs/PLAN.md`
4. **交互语义不许丢**（每个都要在改版后仍然成立）：
   - 权限卡/任何流内卡片**不打断输入**（不 disable 输入框、不抢焦点）
   - 流式输出自动吸底（用户上滚则停止跟随，回底部恢复）
   - 思考块默认折叠；工具卡/diff 卡/中继卡按事件流顺序交错渲染
   - 输入框：Enter 发送 / Shift+Enter 换行；↑↓ 历史回溯；`@` 文件补全、
     `/` 技能补全（键盘导航 Enter/Tab 选中、Esc 关闭）
   - 车钟（Telegraph）四模式数据驱动渲染，拖拽换挡
5. 字体：界面用继承的 `var(--vscode-font-family)`，代码/路径用 `monospace`。

## 3. 文件地图（你的工作区）

```
extension/webview/
├── index.html                  # vite 入口（一般不动）
├── vite.config.ts              # base './'（webview 资源路径，勿改）
├── tsconfig.json
└── src/
    ├── main.tsx / App.tsx      # 挂载 + 顶部导航 + 事件订阅（订阅代码勿动）
    ├── styles.css              # ★ 全部样式（1550+ 行，主战场）
    ├── protocol.ts             # 类型契约（勿动，读它了解数据形状）
    ├── store/store.ts          # zustand 单 store（逻辑勿动，读它了解状态）
    ├── host/                   # postMessage 封装（勿动）
    ├── views/                  # 七个视图
    │   ├── ChatView.tsx        # ★ 主视图：聊天流 + 控制条 + 输入区
    │   ├── SessionsView.tsx    # 会话列表/搜索/分组/操作
    │   ├── ApiView.tsx         # Provider 管理（CRUD/测试连接/拉模型）
    │   ├── EnvView.tsx         # 硬件 + 工作区语言面板
    │   ├── ToolsView.tsx       # 工具开关+默认权限 / Skills / MCP 三页签
    │   ├── TasksView.tsx       # 后台任务 / 定时任务 / 工作流 三页签
    │   └── SettingsView.tsx    # 设置（较空）
    └── components/
        ├── Telegraph.tsx       # 车钟（SVG 圆形仪表，四模式）
        ├── PermissionCard.tsx  # 权限卡（允许/总是/拒绝/写规则）
        ├── DiffReviewCard.tsx  # diff 审阅（+N/−M、打开对比页、接受/拒绝）
        ├── ToolCallCard.tsx    # 工具卡（状态/耗时/结果折叠/时间线/隐私警告）
        ├── SubagentCard.tsx    # 子代理卡（@model 徽标/最终结果）
        ├── VisionRelayCard.tsx # 视觉中继卡（模型+档名+转述全文）
        ├── ImageArtifactCard.tsx # 截图卡
        ├── CompactionCard.tsx  # 压缩报告卡（L2/L3 token 对比）
        ├── PerfChip.tsx        # 性能格（TTFT + token/s）
        ├── ContextMeter.tsx    # 上下文占用
        ├── TaskListBar.tsx     # 任务清单进度条
        └── WelcomePage.tsx     # 空状态欢迎页 + 建议提示词卡片
```

## 4. 数据流（读懂这个再动手）

- **真相在扩展宿主 + Python 核心**；webview 是投影。侧边栏视图销毁重建后
  由宿主重放事件恢复——所以**所有状态要么在 store、要么可从事件重建**；
  不要把关键状态放组件本地 state（展开/折叠类瞬态除外）。
- 事件流向：宿主 → transport（onEvent/onHostPrompt/...）→
  `store.applyEvent(e)` 归约 → ChatItem[] / 各切片 → 视图渲染。
- 聊天流 = `chat.items: ChatItem[]`，kind 有
  `user | assistant | thinking | tool | permission | diff | relay | image |
   compaction | error`——渲染顺序即事件顺序（交错），不要重排。
- 发消息：`sendUserMessage(draft)` → 清空草稿 + 发 core 帧；待发上下文
  chips 在 `pendingContext`（fileRefs/selection/terminalSel/problems/images）。

## 5. 设计意图（产品方向的线框，来自 PLAN §6，改版应遵循其信息架构）

```
┌─ Zcode-Py ────────────────────[＋新会话]─┐
│  聊天流（气泡流 + 交错卡片）               │
│  …                                       │
│─────────────────────────────────────────│
│  任务清单 ████████░░░ 5/12 [展开]         │
│  🚢高 │ glm-5.3▾ │ 🛡普通 │ ◔42% │ ⏱842ms ⚡45/s │  ← 底部控制条
│  [待发上下文 chips 行]                    │
│  ┌─────────────────────────────┐ [➤/⏹]  │
│  │ 问点什么… (@文件 / 技能)      │        │
│  └─────────────────────────────┘        │
│  [📎文件][🖼图片][⊞选中代码][⚡Commit]     │
└─────────────────────────────────────────┘
```

关键设计决定（勿推翻）：**无会话头行**；模型/车钟/权限/上下文/性能全部
收进底部控制条；车钟是「大和号战舰同款圆形仪表盘」（黄铜外圈+黑盘面+
白刻度+中央把手拖拽+双指针），刻度直接显示思考程度（关闭/低/中/高/最大）。

## 6. 已知的丑点清单（用户视角，按优先级）

1. 聊天气泡层级感差：用户/助手/思考/卡片视觉权重接近，扫读困难
2. 控制条拥挤：五个控件挤一行，窄侧栏（300px）溢出换行难看
3. 卡片样式单调：工具卡/权限卡/diff 卡同一个灰盒子，重要程度无区分
   （权限卡应该最醒目——它是安全闸门）
4. 输入区与快捷按钮行缺乏整体感，边框/圆角/间距不成体系
5. 空状态（欢迎页）单调；各视图空态文案排版随意
6. 滚动条/焦点态/hover 反馈不一致；selected 态弱
7. 字号层级少（12px 一刀切），信息密度高时无呼吸感
8. Telegraph 车钟的拟物感不足（黄铜质感/盘面光泽/指针动画）
9. 深浅主题下部分 `--vscode-*` 变量选择不当（对比度差的地方自行换更
   合适的语义变量，如 list-hoverBackground / editorWidget-* / charts-* 系列）

## 7. 建议的改版方向（可自由发挥，但保持 VSCode 原生感）

- 建立 spacing（4/8/12/16）与字号（11/12/13）刻度、圆角（4/6/8）体系，
  全文件统一
- 聊天流：助手消息不套气泡（贴边 + 左侧角色标识），用户消息浅底色块
  靠右或整行浅块；思考块用更弱的 descriptionForeground + 左细线
- 卡片分级：权限卡（warning 边框强调）> diff 卡（左侧 +/- 色条，用
  charts-red/charts-green）> 工具卡（弱）> 中继/压缩（最弱，可折叠）
- 控制条响应式：窄宽度时车钟/性能格收成图标，模型选择独占一行
- 输入区做成一个整体容器（圆角外框包含 textarea + 按钮行）
- 参考 VSCode 原生面板语言：细边框（panel-border）、编辑器组件底色
  （editorWidget-background）、列表态（list-hover/activeSelection）
- 微交互：卡片展开/收起过渡、流式光标闪烁、权限卡入场轻微强调

## 8. 可用资源

- VSCode 主题变量完整清单：https://code.visualstudio.com/api/references/theme-color
  （charts-green/orange/red、editorWarning-foreground、list-*、
   editorWidget-*、badge-*、button-* 系列都可自由用）
- 现有组件里的 SVG（车钟）可重绘，但保留「数据驱动四模式 + 拖拽换挡」
  的 props 契约（levels/current/paramTranslation/onChange）
- `--vscode-font-monospace` 变量可用于等宽处

## 9. 交付方式

- 直接改 `extension/webview/src/**`（views/components/styles.css），
  小步提交；每步跑第 2 节的两条门禁
- 新组件放 components/，命名 PascalCase；纯展示 props，不接 store 以外
  的数据源
- 改版完成后在仓库根跑一次完整门禁（含 python 侧，确认没碰坏别的）：
  `uv run --project agent pytest -q`（应保持 648 passed + 3 skipped）
- 输出一页「改版说明」：改了哪些文件、视觉决策清单、截图对比（可选）
