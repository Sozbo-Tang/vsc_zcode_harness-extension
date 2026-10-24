# Zcode-Py 里程碑任务清单（ROADMAP）

> 依据 docs/PLAN.md §7 生成，实施时逐项勾选；每里程碑完成需全量 pytest + 类型检查（PLAN §8）。
> 勾选规则：一项功能验收通过才勾；里程碑全部勾完后在该节标题后追加 ✅。

## M0 骨架 ✅

- [x] git 仓库初始化 + .gitignore
- [x] agent/ Python 包骨架（uv 管理，python>=3.12，anthropic/openai/httpx）
- [x] zcode_py stdin JSONL → stdout echo 往返（M1 已演进为真实协议分发，全链路 stdin JSONL → stdout JSONL 由 test_smoke.py 保持覆盖）
- [x] extension/ TS 薄壳骨架（activityBar 容器 + 侧边栏 webview view + 命令占位）
- [x] extension/webview/ React 独立包骨架（vite + react + zustand + react-markdown）
- [x] 三条门禁命令通过：`uv run --project agent pytest -q` / `npm --prefix extension run compile` / `npm --prefix extension/webview run build`
- [x] 远程开发设计原则落地（workspaceFolders、无硬编码绝对路径、python 子进程在扩展宿主侧拉起）

**验收：全链路往返**
完成于本次 workflow，验证：pytest/compile/build 门禁 + 独立评审

## M1 聊天 + 多厂商

- [x] anthropic 协议适配器（流式 + tool_use 原生块）
- [x] openai 协议适配器
- [x] FakeProvider（pytest 脚本化假流式，无网可测——开发命脉）
- [x] API 连接管理 UI：多套 Provider 配置 CRUD
- [x] API Key 存 SecretStorage（环境变量注入子进程）
- [x] 测试连接（Ping）按钮：✓延迟+模型回复 / ✗错误分类
- [x] 拉取模型列表按钮 + 模型档案（上下文长度/能力标记）
- [x] 流式 Markdown 渲染（react-markdown）
- [x] 车钟控件四模式（预算型/档位型/开关型/无思考型，模型档案 `telegraphLevels` 数据驱动）
- [x] 性能格：首 token 耗时 + token/s 实时/定格/上轮值
- [ ] gemini / ollama / openrouter / 自定义 BaseURL 适配器（gemini 与自定义 BaseURL 已通；ollama/openrouter 独立适配与真实厂商联调未验证）
- [x] 协议往返 pytest 测试锁定 TS/Python 字段一致

**验收：任意厂商流式对话**
完成于本次 workflow，验证：pytest/compile/build 门禁 + 独立评审

## M2 工具 + 权限安全

- [x] 工具注册表 + 只读三件套（read/glob/grep）
- [x] 权限引擎（表驱动测试）
- [x] 危险命令黑名单（优先级高于一切白名单与"总是允许"）
- [x] 权限卡 UI（PermissionCard 允许/总是/拒绝/写规则 → permission_response，构建门禁通过）
- [x] write/edit(str_replace)/bash 工具 + diff 事件（bash 执行核心已于 2026-10-24 装配，见 docs/BASH_BACKEND_SPEC.md；守门冒烟全绿）
- [x] diff 审阅卡 + 工具卡 UI（DiffReviewCard LCS 统计/打开对比页/接受拒绝、ToolCallCard + 编辑跟随占位，构建门禁通过）
- [x] 检查点（用户消息前 git 快照）+ 回滚命令 + 无 git 仓库降级（UI「回滚到这」按钮属 webview）
- [x] webfetch / websearch 工具（host 校验，拒绝环回/私有地址）
- [x] L1 源头截断（bash 超 3 万字符取头尾、Read 行数上限）
- [x] 隐私护栏（.env / *.pem / id_rsa 模式警告）

**验收：读码答问；写弹卡；可回滚**（核侧全链路 pytest 覆盖；弹卡 UI 待 webview）
完成于本次 workflow，验证：pytest/compile/build 门禁 + 独立评审

## M3 编辑器联动

- [x] 终端选中报错右键发送 Agent（copySelection+剪贴板通道读取，剪贴板尽力恢复）
- [x] Problems 面板诊断一键发送（命令面板/快捷键，错误+警告，上限 30 条）
- [x] 选区上下文 + @文件引用补全（editor.getSelection/listFiles/pickFile RPC；
      输入框 @ 触发下拉，chips 待发上下文随 user_message.context 发送）
- [x] diff 审阅页（对比视图）（M2 落地，M3 复验）
- [x] 编辑跟随（点工具卡跳转对应文件行；read/edit/write 用入参行号，grep 用首个命中行）
- [x] 编辑器右键菜单：解释/优化/写测试/修 Bug（「发给 Zcode-Py」子菜单，预填草稿不代发）
- [x] 快捷键：唤起侧边栏 ⌃⌘Z / 停止生成 ⌥⌘. / 新会话 ⌥⌘N

**验收：Copilot 式 edit 体验**（2026-10-24 完成；pytest 398 + compile/build 门禁全绿，
context 四字段 e2e 冒烟通过；UI 交互需 F5 人工验收）

## M4 多会话 + 记忆

- [ ] 会话 JSONL 持久化（transcript 即 UI 事件流）
- [ ] 会话列表/搜索/恢复
- [ ] 会话分叉（从某消息分支）
- [ ] 并行多开（每会话一个独立 Python 子进程）
- [ ] multi-root 多根工作区（会话选用根目录作为 Agent cwd）
- [ ] 会话导出 Markdown
- [ ] MEMORY.md 跨会话记忆（自动加载/自动更新）
- [ ] 输入框 ↑ 历史输入 + 草稿自动保留
- [ ] 收藏/固定重要回复
- [ ] 空状态欢迎页 + 建议提示词卡片

**验收：重启续聊**

## M5 环境 + Vision

- [ ] 硬件探测（CPU/GPU/RAM/OS，三平台）
- [ ] 工作区特征文件 → 语言检测（只显示命中的语言）
- [ ] 工具链版本探测 + 缺失黄牌与安装指引
- [ ] 环境事实注入 Agent 上下文
- [ ] Vision 中继：不支持视觉时路由多模态模型转述
- [ ] 中继卡 UI（模型名+转述全文可审计）+ 三档预设 + 自定义
- [ ] 转述后原图退场；未配置视觉模型时一键跳转

**验收：贴图给纯文本模型能答**

## M6 浏览器

- [ ] browser-use 可选依赖组（未安装不注册工具）
- [ ] 截图 artifact 回聊天流展示
- [ ] 有头/无头配置 + 操作时间线

**验收：「打开网站并总结」**

## M7 子代理 + Skills

- [ ] run_child 子代理（嵌套受限 loop，上下文防火墙）
- [ ] 子代理可指定不同模型
- [ ] Task 工具 + 子代理卡 UI
- [ ] SKILL.md（zcode 兼容）+ 斜杠命令

**验收：并行调研 + 自定义 skill**

## M8 harness 对标

- [ ] MCP client + 管理页（添加/状态灯/工具浏览）
- [ ] hooks 系统
- [ ] 计划模式
- [ ] 压缩 L2 机械淘汰（>50% 旧工具结果换占位符，保 tool_use/tool_result 配对）
- [ ] 压缩 L3 自动摘要（85% 阈值，七要素结构化，可指定便宜模型）+ /compact
- [ ] 压缩 L4 冷存 + search_history 工具 + 压缩报告
- [ ] Notebook 工具（读写 .ipynb 单元格并执行回传）

**验收：接真实 MCP；跑 .ipynb**

## M9 工作流 + 后台 + 补全

- [ ] Python 工作流编排
- [ ] 后台任务面板（运行中/定时任务）
- [ ] cron 定时任务
- [ ] 内联补全（幽灵文本，默认关闭；FIM 或 chat 模型模拟）
- [ ] 多语言界面（中/英）
- [ ] 远程开发实测（SSH / WSL / Dev Container / Codespaces）

**验收：工作流脚本 + 远程跑通**
