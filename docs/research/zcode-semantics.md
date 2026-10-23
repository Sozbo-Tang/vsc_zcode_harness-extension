# ZCode（zai-org/ZCode）语义调研笔记

> 调研日期：2026-10-24。对象：[zai-org/ZCode](https://github.com/zai-org/ZCode)（Apache-2.0，TypeScript monorepo，Z.ai 的编码 Agent harness，README 标注 v3.16.2，Node.js 24.14.0 + pnpm 10.33.2）。
> 目的：为用 **Python 重写同类 Agent 核心**提供结构、工具契约、权限模型与 UI 协议的参照。
> 方法：通过 GitHub API + raw.githubusercontent.com 直接读取 `main` 分支源码（非猜测，下列每条结论均给出文件来源）。

---

## 1. 仓库顶层结构与核心模块划分

### 1.1 顶层布局（桌面/Web/服务端 + Agent CLI 四位一体）

来源：[README.en.md](https://github.com/zai-org/ZCode/blob/main/README.en.md)、[architecture.md](https://github.com/zai-org/ZCode/blob/main/docs/architecture/architecture.md)

| 目录 | 职责 |
| --- | --- |
| `packages/desktop` | Electron 主进程/Host/Renderer 与桌面打包 |
| `packages/web` | Web 客户端 |
| `packages/server` / `packages/zcode-server-cli` | HTTP/WebSocket 服务与远程连接、独立 server 启动 |
| `packages/ui` | 共享 React 组件与 Zustand 状态（三端复用） |
| `packages/services` | 业务服务接口与 Node 实现 |
| `packages/shared` / `packages/rpc` / `packages/client` | 共享协议与类型、RPC 通道、Agent 客户端 SDK |
| `packages/provider` / `provider-node` | 模型 Provider 通用能力与 Node 实现 |
| `apps/zcode-cli` | **Agent CLI、TUI、运行时与工具**（本次调研重点） |

架构文档明确：`apps/zcode-cli` 是 "Agent runtime + V4 authority"，Agent 运行时把状态**单向投影**给 UI（`ZCode CLI ProductProjection -> V4 topic -> read-only UI store`），UI 只是只读消费者；会话协议定义集中在 `packages/shared/src/zcode-protocol-v4/`。

### 1.2 Agent 运行时（apps/zcode-cli/packages/）

`apps/zcode-cli/packages/` 下 30 个包：`core`（运行时核心）、`contracts`（跨包契约/类型）、`cli`、`tui`、`shared-types`、`adapters`、`dynamic-workflow(-runtime)`、`bundled-skills`、各官方插件（browser-use / pdf / documents / spreadsheets / presentations / cua …）、`telemetry`、`i18n` 等。

`core/src/` 的模块划分（来源：[core/src 目录](https://github.com/zai-org/ZCode/tree/main/apps/zcode-cli/packages/core/src)，逐目录经 GitHub API 列出）：

| 模块 | 职责（据文件名与源码内容） |
| --- | --- |
| `agent/` | **回合状态机**：`turn-machine.ts`（TurnMachine 接口 + 显式 phase 迁移）、`turn-state.ts`（10 个 TurnPhase）、消息历史水合/压缩、工具分片顺序 |
| `tool/` | 工具面：`registry.ts`（注册表+别名）、`scheduler.ts`（并行分组调度）、`executor/`（执行管线：权限流、审批门、超时、结果序列化/display 投影）、`handlers/`（每个内置工具的实现）、`types.ts`（ToolEntry 形状） |
| `permission/` | 权限判定：`service.ts`（PermissionService 决策管线）、`rule-matching.ts`（规则通配）、`plan-mode-policy.ts`、`broker.ts` |
| `subagent/` | 子代理：`runner.ts`（前台/后台生命周期+registry+完成通知，2162 行）、`definitions.ts`/`profile.ts`（AgentProfile）、`tool-policy.ts`（子代理工具面裁剪）、`explore.ts`/`explore-tools.ts`（Explore 只读子代理）、`general-purpose.ts` |
| `mcp/` | MCP 桥接：`index.ts`（MCP descriptor → ToolEntry 投影）、`app-tools.ts`（App-Provided Tools）、`image-normalization.ts`、`name.ts`（`mcp__server__tool` 命名） |
| `hooks/`、`system-reminder/`、`compact/`、`context/`、`memory/`、`model/`、`repl/`、`session-context/`、`workflow/`、`runtime-task/`、`telemetry/` | 钩子、系统提醒注入、会话压缩、上下文管理、模型适配、REPL、会话上下文、动态工作流、后台任务注册表、遥测 |

`contracts/src/` 是“契约层”：`tools/`（每个工具的 zod input/output schema）、`events/`（会话事件协议）、`skills/`、`config/`、`model/`、`hooks/`、`interfaces/`（含 `session.port.ts` 的 CollaborationMode）等。**契约与实现分离**是这个仓库最鲜明的结构特征。

---

## 2. 内置工具清单与输入/输出约定

### 2.1 工具契约的统一形状（ToolContractDeclaration / ToolEntry）

来源：[contracts/src/tools/contract.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/contract.ts)、[core/src/tool/types.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/types.ts)

每个工具同时声明**能力与治理策略**，不止 input/output schema：

```
ToolContractDeclaration = {
  capability: string                       // 一句话能力描述
  inputSchema / outputSchema: JSONSchema   // 模型可见形态（zod → JSON Schema）
  strict?: boolean                         // 是否有资格走 provider 严格模式
  permission: {
    permission: string                     // 权限名（如 "bash"）
    riskLevel: "low"|"medium"|"high"|"critical"
    sideEffectScope: "none"|"workspace"|"git"|"network"|"system"|"session"|"userInteraction"
    needsApproval: boolean
    patternSources / alwaysAllowPatternSources: ToolPermissionPatternSource[]  // 规则可匹配哪些入参维度
    denyPriority: "beforeAsk"|"afterStaticAllow"
    alwaysAsk?: true                       // 任何模式下都必须人工确认（yolo 也压不住）
    askOptions?: { allowAlways: false|"session" }  // 禁止/限制"永远允许"记忆
  }
  resultBudget: {                          // 结果预算：UI 与模型分开
    maxInlineBytes / maxModelBytes
    strategy: "inline"|"truncate"|"artifact"
    preview?: { maxBytes, maxLines, direction: "head"|"tail" }
    artifact?: { enabled, retention: "session"|"project"|"temporary" }
  }
  timeout: { kind:"none" } | { defaultMs, maxMs, allowCallOverride, cleanupGraceMs }
  cancellation: { supported, cleanup:"none"|"bestEffort"|"required", userVisibleMessage }
  trace: { required:true, propagateToAdapters, recordInput/Output: "summary"|"full"|"none" }
}
```

`sideEffectScope` 还派生两条全局判定（contract.ts 内）：`isWorkspaceMutatingToolCall`（readOnly!==true 且 scope ∈ {workspace,git,system} 即算“写工作区”，scope 缺席按会写处理——保守）与 `isWorldTouchingToolCall`（只有 `session`/`userInteraction` 两个“纯协议档”不算“碰外部世界”，用于缓存纯度判定）。

### 2.2 内置工具全清单

来源：[core/src/tool/handlers/index.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/handlers/index.ts) `builtInTools` 数组（L80-148）。注册时按 options 灰度过滤（`includeAgent`/`includeSkill`/`includeDynamicWorkflow`/`includeNodeRepl` 等，L212-286）：

- 文件/搜索：`Read`、`Write`、`Edit`、`ApplyPatch`(注释停用)、`Glob`、`Grep`
- 执行：`Bash`、`js`(node_repl，默认关闭由 browser-use 插件启用)
- Web：`WebFetch`、`WebSearch`
- 规划/待办：`TodoRead`、`TodoWrite`、`EnterPlanMode`、`ExitPlanMode`
- 交互/编排：`AskUserQuestion`、`Skill`、`Agent`/`Task`（子代理分派）、`SendMessage`、`RespondToCoordinator`、`submit_result`、`escalate`、`TaskOutput`、`TaskStop`、`ReadSessionContext`、`ReplyToChannel`
- 自动化：`CronCreate/List/Update/Delete`、`OffPeakCreate/List`
- 动态工作流（11 个，灰度门控）：`CreateWorkflow`、`AmendWorkflow`、`SaveWorkflow`、`ListSavedWorkflows`、`ListModels`、`EvalWorkflowSnippet`、`ListWorkflowRuns`、`GetWorkflowRun`、`ResumeWorkflowRun`、`ResolveWorkflowQuestion`、`FillWorkflowHole`
- 元工具：`ToolSearch`、`DeferredToolCall`（工具太多时按需检索/延迟装载）

### 2.3 各工具输入/输出约定（读 contracts/src/tools/*.ts 原文提炼）

**Read**（[read.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/read.ts)）
- 输入：`{ file_path: string(绝对路径), offset?: int≥0, limit?: int>0, pages?: string(仅 PDF, "1-5,8") }`
- 语义校验：拒绝二进制扩展名（.zip/.exe/.so 等 20 种）与阻塞设备文件（/dev/zero、/dev/stdout 等）；常量 `READ_MAX_FILE_SIZE_BYTES=256KiB`、`READ_MAX_OUTPUT_TOKENS=25000`、`READ_DEFAULT_MAX_LINES=2000`、图片 base64 上限 5MiB / 输入 20MiB / 最大边 2000px
- 输出为 tagged union：`type:"text"`（content+numLines+startLine+totalLines+truncated/truncatedByTokenCap/partialViewNotice）、`"image"`（base64+mimeType+dimensions+resized/compressed）、`"video"`、`"notebook"`（cells+outputs）、`"pdf"`、`"parts"`（多页 PDF 分片）、`"file_unchanged"`（mtime 未变时的去重短路）

**Write**（[write.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/write.ts)）
- 输入：`{ file_path, content }`
- 输出：`{ type:"create"|"update", filePath, content, structuredPatch: DiffHunk[]（oldStart/oldLines/newStart/newLines/lines）, originalFile: string|null, gitDiff?, userModified? }`——**把 diff 直接作为结构化输出**，UI 工具卡用一个字段即可渲染
- 错误码：`write_file_not_read / write_partial_read / write_stale_file / write_secret_detected / …`（先读后写、脏文件、写时密钥检测都有显式错误通道）

**Edit**（[edit.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/edit.ts)）
- 输入：`{ file_path, old_string, new_string, replace_all?: bool }`；布尔经 `semanticBoolean()` 预处理（"true"/"1"/"yes"/"on" 都接受——**容忍模型的字符串化输出**）
- 输出：`{ filePath, oldString, newString, originalFile, structuredPatch, userModified, replaceAll, matchStrategy?, matchCandidateCount?, gitDiff? }`；错误码为**数字枚举**（1 no_change、6 file_not_read、7 stale_file、8 old_string_not_found、9 ambiguous_replace…），与 Claude Code 语义对齐
- 匹配策略可降级（matchStrategy 字段回显实际用了精确/宽松匹配）

**Bash**（[bash.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/bash.ts) + [handlers/bash.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/handlers/bash.ts)）
- 输入：`{ command, timeout?: number(≤600000ms), description?: string（给权限卡看的主动语态描述，schema description 里带正反例）, run_in_background?: bool, dangerouslyDisableSandbox?: bool }`
- 输出 `BashOutput`：`stdout/stderr`（+ 各自 truncated/bytes/persisted 路径）、`status: "completed"|"failed"|"timed_out"|"cancelled"|"spawn_error"|"backgrounded"`、`exitCode`（**非零退出不抛异常，结构化返回**）、`signal`、`interrupted`、`backgroundTaskId`、`backgroundedByUser`/`assistantAutoBackgrounded`、`staleReadFileStateHint`（**该命令期间 mtime 变化的已读文件提示**，防模型用旧缓存）、`ghRateLimitHint` 等
- 治理声明（bashToolEntry 原文）：`riskLevel:"high"、needsApproval:true、sideEffectScope:"system"`；resultBudget：`strategy:"artifact"`，maxInline 30KB / maxModel 30KB / preview tail 30KB / artifact retention session；timeout：默认可配 + max 600s + allowCallOverride + cleanupGraceMs 6000
- 常量：`MAX_INLINE_OUTPUT_BYTES=30000`、后台任务结构 `BackgroundTask{id,command,status,exitCode,stdout/stderrPersistedOutputPath}`
- 错误码族：`bash_command_not_parsable / bash_high_risk_combination / bash_sandbox_violation / bash_output_too_large / …`

**Glob**（[glob.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/glob.ts)）
- 输入：`{ pattern, path? }`（path 的 description 显式叮嘱“不要填 'undefined'/'null'，省略即默认目录”——面向模型 UX 的细节）
- 输出：`{ durationMs, numFiles, filenames: string[], truncated }`

**Grep**（[grep.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/grep.ts)）
- 输入对齐 ripgrep：`{ pattern, path?, glob?, output_mode: "content"|"files_with_matches"|"count"(默认 files_with_matches), -B/-A/-C/context?, -n?, -i?, -o?, type?, head_limit?(默认250，0=不限), offset?, multiline? }`
- 输出：`{ mode, durationMs, numFiles, filenames, content?, numLines?, numMatches?, truncated, appliedLimit?, appliedOffset? }`（回显实际应用的 limit/offset）

**TodoRead/TodoWrite**（[todo.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/todo.ts)）
- TodoItem：`{ content, status: "pending"|"in_progress"|"completed", priority: "high"|"medium"|"low" }`
- TodoWrite 输入为**全量列表**（"The complete updated todo list"）；旧的“最多一个 in_progress” superRefine 已注释掉（多 subagent/并行任务需多个 in_progress，注释保留便于回滚——见源文件 L60-70）
- 输出 `{ oldTodos, todos, summary:{total,pending,inProgress,completed} }`；另有 `formatTodoStateForModel()` 把 todo 状态格式化为权威文本注入模型

**WebFetch**（[webfetch.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/webfetch.ts)）
- 输入：`{ url, prompt }`（**小模型对抓取内容跑 prompt** 的形态）
- 输出：`{ url, finalUrl, status, statusText, contentType, bytes, durationMs, result, cacheHit(15min 缓存), redirects:[{from,to,status}], artifactUri?, truncated }`
- 错误码：`webfetch_unsupported_protocol / webfetch_credentials_in_url / webfetch_unsafe_redirect / webfetch_egress_blocked / webfetch_response_too_large / …`

**WebSearch**（[websearch.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/websearch.ts)）
- 输入：`{ query(min 2), allowed_domains?/blocked_domains?(互斥), maxUses? }`；输出 `{ query, results[], sources[] }`（结果与“Sources 引用列表”分离）

**Skill**（[skill.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/skill.ts) + [skills/index.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/skills/index.ts)）
- 输入：`{ skill, args? }`（兼容旧 `{name,args}`，zod union + transform）
- 输出：字符串或 `{ name, content, baseDirectory, truncated }`——Skill 即“按需装载的本地指令包”（SKILL.md frontmatter：name/description/whenToUse；来源 scope：project/user/system/admin；source：agents/zcode/bundled/plugin/remote；带 `safeToAutoLoad` 与诊断码体系 skill_missing_frontmatter 等）

**Agent/Task（子代理分派）**（[agent.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/agent.ts)）
- 输入：`{ description(3-5 词), prompt, subagent_type?, run_in_background? }`；**刻意不暴露调用级 model 参数**（源码注释：历史 tool call 会持续生成旧 override 覆盖当前配置，模型由 Settings/Profile 统一决定）
- 输出 tagged union：`status:"completed"`（content 文本块 + totalToolUseCount/totalDurationMs/usage）或 `status:"async_launched"`（childSessionId/backgroundTaskId/outputFile/canReadOutputFile）

**AskUserQuestion**（[ask-user-question.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/ask-user-question.ts)）：结构化选择题 `{question, header(≤12字), options:[{label,description,preview?}], multiSelect?}` + `answers/annotations` 回传——这是“模型问人”协议化的样例（本调研任务所用的同款工具面）。

---

## 3. 权限模型

来源：[core/src/permission/service.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/permission/service.ts)（732 行全文精读）+ [session.port.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/interfaces/session.port.ts) + [guarded/command.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/handlers/guarded/command.ts) + [bash-command-permission-policy.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/handlers/bash-command-permission-policy.ts)

### 3.1 协作模式（CollaborationMode）

`"plan" | "build" | "edit" | "yolo" | "guarded" | "auto"`（auto 保留未实现，直接 deny）。子代理继承父模式时 `inheritPermissionMode`：父是 guarded 则子 yolo 收紧为 guarded。

### 3.2 决策管线（checkPermission 的判定顺序，即优先级）

每个判定返回 `PermissionDecisionResult{ decision:"allow"|"ask"|"deny", allowed, escalated, reason, ruleId, riskLevel, sideEffectScope, alwaysAsk?, modifiedInput?, approvalMode? }`——**每条决策都带 ruleId**（如 `mode.plan.readOnly`、`rule.project.deny`、`safety.bash.remove-critical-path`），可追溯、可打点。

顺序（service.ts L101-266）：
1. Plan 模式进/出迁移的特殊判定（plan-mode-policy）
2. `requiresUserInteraction` 工具（AskUserQuestion 等）：在 disallowedTools 硬禁之后直接 ask（`tool.userInteraction`）
3. **guarded 模式 + 危险命令命中**（工具的 `userApprovalRule`）→ ask + `approvalMode:"user-once"`（一次一批，不可记住）
4. 工具自报 `alwaysAsk` → 走 checkAlwaysAsk（见 3.4）
5. `yolo`/`guarded`（非 plan）→ allow（`mode.yolo`，跳过弹窗；**注意：yolo 放行在 disallowedTools 之前**，源码注释明说这是现状）
6. auto 模式 → deny（未实现保护）
7. 配置 `disallowedTools` → deny（`rule.disallowedTools`）
8. 项目规则 **deny** → deny（`rule.project.deny`）
9. 项目规则 **ask** → ask（`rule.project.ask`）
10. 特例免批：ReplyToChannel（回源频道，sideEffect=network 且非破坏）、WebFetch 预批 URL 域名、workflow 草稿路径写入（`tool.webfetch.preapproved` / `tool.workflowDraft.preapproved`）→ allow
11. plan 模式 → checkPlanMode：readOnly 且非 destructive 放行；非破坏 MCP 放行；显式 session 域能力放行；否则 deny（**plan 是 deny 不是 ask**）
12. 项目规则 **allow** → allow（`rule.project.allow`）
13. 配置 `allowedTools` → allow
14. `edit` 模式：`permission==="edit"` 且 scope==="workspace"` 的文件编辑工具放行，其余回落 build
15. build 模式（**默认模式**的语义）：
    - readOnly && !destructive && !needsApproval → allow（`mode.build.readOnly`）
    - riskLevel critical → ask；high 且未开 autoApproveHighRisk → ask
    - session 作用域低风险 → allow
    - needsApproval || destructive || sideEffect!=="none" → ask
    - 其余低风险 → allow

**默认权限总结**：默认（build）模式下只读工具（Read/Glob/Grep/WebSearch/WebFetch/TodoRead/TodoWrite/AskUserQuestion/Agent/Task/Skill，service.ts `isReadOnlyTool`）免批；Write/Edit/Bash（`isWriteTool`）与一切有副作用的工具 ask；Bash 同时在 `isDestructiveTool` 名单。

### 3.3 规则形态与匹配

- `PermissionRuleset = { version:1, allow?: Rule[], ask?: Rule[], deny?: Rule[] }`；`Rule = { toolName, ruleContent? }`；更新走 `PermissionUpdate{type:"addRules", behavior, rules}`（[permission-rules.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/executor/permission-rules.ts)，dedupe by toolName+ruleContent）
- **匹配主体**按工具类型取入参字段（`ruleSubjects`）：优先 WebFetch 的 url（拆 host），否则取 `command|url|file_path|path|pattern|patch_text` 第一个 string 字段
- **匹配语法**（matchesRuleContent）：`prefix:*` 前缀匹配（含空格/制表符后缀）、含 `*` 的 wildcard 转正则、否则全等。例：`Bash(npm run:*)` 放行所有 npm run 子命令
- Write/Edit 规则互通（`matchesRuleToolName`：Write 规则可匹配 Edit 调用）
- **Bash 专用规则策略**（bash-command-permission-policy.ts）：AST 解析命令（analyzeBashCommand），把复合命令拆成独立 invocation；只对**非只读**的子命令生成必需规则组；建议规则上限 5 条（`MAX_SUGGESTED_RULES`），超限回落为整条 exact 规则；生成稳定前缀（剥掉 `--dir`/`-C` 等全局 flag）作 `prefix:*` 建议——即权限卡上的"Always allow `pnpm run lint:*`"就是这里算出来的。高危根命令集合（bash/chmod/chown/dd/rm/sudo/zsh 等）与 wrapper（env/nohup/sudo/time）解包有专门表

### 3.4 alwaysAsk 与"永远允许"治理

- `alwaysAsk:true` 的工具（如 CreateWorkflow、ResumeWorkflowRun）在任何模式（含 yolo、plan 直通）都必须确认，但**压不过 deny**：checkAlwaysAsk 先跑 auto/disallowedTools/项目 deny 三个硬阻断，再看**会话级 allow**（"Always allow in this session"，随实例消亡、重启即失效）、**owned workflow amend**（本会话发起且非用户停下的 run 的修订免确认），最后才 ask（`tool.alwaysAsk`）
- `askOptions:{allowAlways:false}`：每次输入都不同的工具（如 fill-workflow-hole）**禁止**持久化放行规则；`"session"`：只给会话级规则不给项目级
- `denyPriority:"beforeAsk"`：显式 deny 永远先于 ask 生效

### 3.5 危险命令处理（guarded 模式的 AST 匹配器）

来源：[guarded/command.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/core/src/tool/handlers/guarded/command.ts)（130 行全文）：

- `matchDangerousCommand(command, dialect)`，dialect 支持 posix/cmd/git-bash；命令超 10000 字符或解析失败返回 `unsupported`（保守）
- 命中规则返回 `{status:"matched", ruleId:"safety.bash.<rule>", start, end}`——**ruleId 直接进权限卡**；规则族：
  - `rm`：递归(-r/-R/--recursive) 或 ≥2 个目标 或 目标含未引用 glob → `remove-critical-path`
  - `git`（专门 matchGit）、`find -delete`、`rsync --delete*`（dry-run/--help 豁免）→ 各自规则
  - `dd`/`mount`/`umount`/`mkfs*` → `system-storage`（裸 `mount` 查询豁免）
  - Windows：`rd/rmdir/del/erase/robocopy` 删除语义
  - 解释器（bash/sh/zsh/cmd/powershell/python/node/perl/ruby/eval/source…）**不做根命令匹配**而是解包后看内层命令
- 命中后走 service.ts 第 3 步：`user-once` 审批（当次放行，不产生可记忆规则）
- guarded 的定位：比 yolo 多一道"AST 识别出的危险命令必须逐次人工批"的窄门；非危险命令仍按 yolo 直通

### 3.6 运行时权限能力解析（关键创新点）

工具的权限属性**不是静态的**：`ToolEntry.resolvePermissionCapability(input, context)` 按具体入参解析。Bash 的实现（handlers/bash.ts `resolveBashPermissionCapability`）：guarded 模式先跑危险命令匹配；然后对命令做**只读语义分析**（bash-readonly-policy-* 系列约 20 个文件：按 git 子命令、gh 多词、io 重定向 flag、进程/文本 flag 等判定）——判定为只读的命令整体降级为 `{readOnly:true, riskLevel:"low", sideEffectScope:"none", needsApproval:false}`。也就是说 `git status` 与 `rm -rf /` 虽是同一个 Bash 工具，权限属性完全不同。`ToolCallStarted` 事件也携带解析后的 `readOnly/sideEffectScope`（在 handler 动手之前发出）。

---

## 4. 驱动 UI 的协议事件形态

来源：[contracts/src/events/session.events.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/events/session.events.ts)（48KB 全文）、[event-reducer.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/events/event-reducer.ts)、[tool-result-metadata.ts](https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/packages/contracts/src/tools/tool-result-metadata.ts)

### 4.1 事件信封（Event Sourcing）

```
SessionEvent = { id, sessionId, turnId?, type: SessionEventType, timestamp, traceId, sequenceNumber, payload }
```

- `sequenceNumber` 单调递增（会话内全序），配合 in-memory-session-event-store 与 retention 策略；UI 侧用纯函数 `eventReducer` 归约成投影状态（冷恢复/回放同一条路径）——架构文档称之为 "ProductProjection -> read-only UI store"
- 事件类型约 80 个（SessionEventType 常量表 L84-185），分簇：会话生命周期（created/resumed/forked/compacted/ended）、Turn（started/input_received/complete/error + steer 队列族 queued/reordered/drained/rejected…）、模型流（model_request/selected/streaming/complete/error + stream_recovery 族 + anomaly_warning）、**工具族**、**权限族**、后台任务族、动态工作流进度、Hook 族、压缩/检查点/rewind、Subagent（spawned/message/stopped）、interrupt/cancel/resume

### 4.2 工具卡（Tool Call 生命周期 + display 载荷）

事件序列：`tool_call_scheduled → tool_call_started → (tool_call_progress)* → tool_call_result | tool_call_error → tool_batch_complete`

- `ToolCallScheduledPayload{toolCallId, assistantMessageId?, toolName, input, dependencies?, parallelGroupIndex?, canRunParallel?, display?, schedule}`——调度期就带依赖与并行组信息
- `ToolCallStartedPayload{toolCallId, startedAt, display?, readOnly?, sideEffectScope?}`——**handler 动手前**发出解析后的副作用能力
- `ToolCallProgressPayload{elapsedMs?, progress?/total?/message?(MCP progress 转发), pid?, stdoutBytes/stderrBytes?, stdoutTail/stderrTail?, outputPreview?}`——长任务实时增量
- `ToolCallResultPayload{toolCallId, result:{…, display?}, duration, skillMetadata?, pluginId?}`
- **display 是 UI 的"工具卡"渲染契约**：`toolResultDisplayPayloadSchema = discriminatedUnion("kind")`，kind 有 `bash_output`(output≤150KB+truncated+outputPath)、`file_diff`(filePath+additions/deletions+structuredPatch——Write/Edit 工具卡的 diff 源)、`task_output`、`task_stop`、`local_agent_message`、`respond_to_coordinator`、`cua`、`node_repl_images`、`mcp_tool`(serverName/toolName/description/unavailable{code}/ui)、`create_workflow`、`get_workflow_run`、`list_workflow_runs`、`eval_workflow_snippet`、`saved_workflow_list`、`list_models`、`resume_workflow_run`。所有 display 均为 **strict schema + 限长**，且落库后每次读取都要重新过严格解析（parseCompletedToolPartMetadata 统一清洗入口）
- 序列化元数据 `toolResultSerializationMetadata{truncated, originalBytes, returnedBytes, budgetStrategy, artifactPath?}` 记录预算执行情况

### 4.3 权限卡（Permission 事件族）

- `permission_requested`：`{toolCallId, toolName, riskLevel, reason, input, suggestedPermissionUpdates?[]（建议的"永远允许"规则，来自 3.3 的 Bash 规则建议器）, display?（prepareApproval 钩子产出的确认预览，复用工具卡 display 投影）, approvalMode?("user-once"), optionsPolicy?（对应 askOptions：无"永远允许"按钮/仅会话级）, origin?, fullAccessSupported?}`
- `permission_resolved`：`{toolCallId, decision: "allow"|"deny"|"escalate"|"modify", reason?, modifiedInput?}`——**decision 含 "modify"**：用户可以在权限对话框里改写工具入参后再放行（Write 的 `userModified` 字段即回显此事）
- `permission_denied`：`{toolCallId, toolName, reason, inputSummary?}`
- 配套：`user_input_auto_resolution_updated`（无人值守时输入框自动应答倒计时状态机 hiddenGrace/visibleCountdown/snoozed）

### 4.4 其他 UI 驱动事件

- 后台任务面板：`background_task_started/updated/completed`（taskKind: bash|subagent|workflow，blocked/blockedReason:"interactive_prompt_detected"，outputTail/stdoutTail 增量）
- 子代理：`subagent_spawned/message/stopped`
- 模型流式：`model_streaming(kind 区分文本/思考/工具调用增量)、model_selected、turn_execution_model_fallback、model_anomaly_warning`
- 输入队列（steer）：`turn_steer_queued/reordered/drained/rejected/discarded、queue_auto_drain_changed、followup_mode_changed`——运行中插话、排队、改序的完整协议化
- 工作流：`dynamic_workflow_run_progress`（一条引擎 RunEvent 一条会话事件，附 actorSessionId/launchInputId 派生键）

---

## 5. 值得借鉴的设计决策（对 Python 重写）

1. **契约层与实现层物理分离（contracts/core 两包）**：每个工具的 zod schema、权限规格、结果预算、超时/取消/追踪策略全部是**声明式数据**，注册表只消费契约。Python 版可用 `pydantic` + dataclass 声明同样的 ToolContract，让“加一个工具”= 声明一个 entry，权限/预算/超时治理自动获得，而不是散落在 handler 里各写各的。来源：contract.ts + registry.ts（toContracts 直接投影给 provider）。

2. **权限是“决策管线”而非布尔判断，且决策带 ruleId**：固定优先级（硬 deny → 项目 ask → 模式语义 → 项目 allow → 内置能力）+ 每条结果携带 `ruleId/reason/riskLevel`，使权限行为可解释、可测试、可遥测；`alwaysAsk`（yolo 压不住的确认）与 `askOptions.allowAlways:false|"session"`（禁止把每次输入都不同的 gate 记成永久规则）解决了“永远允许”被滥用的问题。Python 版照抄这条管线顺序即可获得成熟默认行为。

3. **运行时权限能力解析：权限属性按“这一次调用”计算**。Bash 工具静态声明 high/system/needsApproval，但 `resolvePermissionCapability` 用 AST 只读分析把 `git status` 降级为只读低风险免批、guarded 模式下危险命令命中 `safety.bash.*` 升级为逐次审批。**同一工具不同入参不同权限**，这是比“按工具名白名单”精细一代的模型，值得作为 Python 版的硬需求。

4. **结果预算三策略（inline/truncate/artifact）+ UI/模型双预算**：`maxInlineBytes`（事件/存储）与 `maxModelBytes`（给模型的）分离；超限走 artifact 持久化（session 级 retention）+ head/tail preview。Bash 30KB/30KB、MCP 100KB/50KB、官方 CUA 特批 256KB——大输出永远不直接塞上下文。Python 版第一天就该有 ResultBudget 抽象，避免后期重构。

5. **事件溯源的 UI 协议：一个信封 + 全序 sequenceNumber + 纯函数 reducer + display 判别联合**。工具卡/权限卡不是 UI 自己解析工具输出，而是执行器产出**有界、strict schema 的 display 载荷**（file_diff/bash_output/mcp_tool…）挂在事件上；`permission_requested` 里的 `suggestedPermissionUpdates` 让“记住这个决定”由引擎计算建议、用户点选，规则语法（`prefix:*`、wildcard）与匹配主体抽取（command/url/file_path…）集中一处。这样 TUI/Web/桌面三端共享同一投影，冷恢复=重放事件。Python 版实现成本不高（dataclass + json schema + 单个 reducer），收益是免费获得多前端与 session 回放。

其他值得一提（次级）：turn 显式状态机（10 phase + canTransitionTo 校验）让循环可恢复、可观测；Bash 非零退出不抛异常（结构化 exitCode + status）+ `staleReadFileStateHint` 提醒已读文件过期；子代理强制剔除 EnterPlanMode/ExitPlanMode（子代理无审批面会卡死父 turn）、Explore 子代理白名单刻意不含写工具；语义化布尔/数字预处理（`"true"/"1"/"yes"` 都接受）防御模型的字符串化输出。

---

## 6. 调研方法与未验证事项

- 调研路径：GitHub API（api.github.com/repos/zai-org/ZCode/contents/...）列目录 + raw.githubusercontent.com 拉取 `main` 分支源文件全文精读；关键文件包括但不限于：`contracts/src/tools/{contract,read,write,edit,bash,glob,grep,todo,webfetch,websearch,skill,agent,index}.ts`、`contracts/src/events/session.events.ts`、`contracts/src/tools/tool-result-metadata.ts`、`core/src/permission/service.ts`、`core/src/agent/{turn-machine,turn-state}.ts`、`core/src/tool/{registry,scheduler,types}.ts`、`core/src/tool/handlers/{index,bash}.ts`、`core/src/tool/handlers/guarded/command.ts`、`core/src/tool/handlers/bash-command-permission-policy.ts`、`core/src/subagent/{runner(节选),definitions,tool-policy,explore-tools}.ts`、`core/src/mcp/index.ts(节选)`、`docs/architecture/architecture.md`、`README.en.md`。
- 未逐行核验：`subagent/runner.ts` 仅读头部与接口（2162 行）；`core/src/runtime.ts`、hooks、compact、model 适配层未展开；`packages/rpc`/`zcode-protocol-v4` 的传输编码未读（本次聚焦语义层）。工具面以 `main@2026-10-24` 为准，仓库更新频繁（README 显示 v3.16.2，2026-10-19）。
