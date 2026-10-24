# 开源编码 Agent 架构模式调研（Python Harness 前置研究）

> 调研日期：2026-10-24。方法：浅克隆四个仓库到本地逐行阅读源码（非仅看文档），
> 所行号均来自当日 `main` 分支。为 Zcode-Py 的 loop.py / compaction / permissions / sessions / browser_use 工具提供参照。
> 调研对象：

| 项目 | 版本快照 | 定位 |
|---|---|---|
| Aider-AI/aider | main@2026-10-24 | 交互式 CLI 编码 Agent，"编辑格式"流式文本协议 |
| SWE-agent | main@2026-10-24 | 批处理 SWE 任务 Agent（论文出身），ACI bash 工具协议 |
| OpenHands software-agent-sdk | main@2026-10-24 | 事件溯源型 Agent SDK（OpenHands v1 拆库后的 Python 核心） |
| browser-use | v0.13.13（pyproject 声明） | 浏览器自动化 Agent 库，Zcode-Py R8 计划接成可选工具 |

注：OpenHands 主仓库（All-Hands-AI/OpenHands，现迁移为 OpenHands/OpenHands）已变成 Electron/TS 前端仓库，Python 后端（controller/agenthub/events）拆到了 `OpenHands/software-agent-sdk`（核实方式：克隆主仓库确认无 Python 代码 + Web 检索确认拆库）。

---

## 1. 主循环结构对比（流式 → 工具调用 → 结果回灌）

### 1.1 Aider：REPL + 反射循环，编辑走文本协议而非原生工具调用

- 入口 `run()`（base_coder.py:876）：`while True` 阻塞读用户输入 → `run_one()`。
- `run_one()`（base_coder.py:924）：预处理（斜杠命令 / 文件提及 / URL 检测）→ 内层 `while message:` 循环：`send_message()` 完成后若产生 `reflected_message`（lint 错误、测试失败、新提及文件），把它当新输入再跑一轮，`num_reflections` 上限封顶。
- `send_message()`（base_coder.py:1419）：`format_messages()` 把 system/files-content/repo-map/readonly/done-summary/cur 分块拼装 → `check_tokens()` 预检 token → `warm_cache()` 打提示缓存 → 重试循环里 `yield from self.send(...)`（base_coder.py:1457-1488：指数退避 0.125s 起步翻倍，LiteLLM 异常分类；`FinishReasonLength` 且模型支持 prefill 时把半截输出以 `prefix=True` 回填继续生成，base_coder.py:1492-1505）。
- 流式渲染：`show_send_output_stream` 逐块喂给 `MarkdownStream`（mdstream.py:92，Rich Live 增量重绘"未稳定行"）。
- **结果回灌的关键差异**：aider 的编辑不是 tool_use——模型按编辑格式（search/replace、udiff、whole-file 等）在正文里输出补丁，`apply_updates()` 解析文本补丁落盘；lint/测试输出作为下一轮 user 消息回灌（base_coder.py:1599-1623）。bash 执行走"模型建议命令 → 用户确认 → 输出以 user/assistant 'Ok' 对回灌"（base_coder.py:1609-1614）。
- architect 编辑格式是**两段式子 Agent**：主模型出方案，EditorCoder 用另一模型应用补丁（architect_coder.py:26）。

### 1.2 SWE-agent：step 循环 + requery 错误处理，纯批处理无人工介入

- `run()`（agents.py:1265）：`while not step_output.done: step(); save_trajectory()` —— 每步把整条轨迹 JSON 覆写落盘。
- `step()`（agents.py:1235）：`forward_with_handling(self.messages)` → `add_step_to_history()`（动作与观察回灌为消息）→ `add_step_to_trajectory()`。
- `forward()`（agents.py:1006）：`model.query(history)` 非流式 → `tools.parse_actions()` 解析（既支持 ACI bash 文本协议，也支持原生 tool_calls，agents.py:1047-1049）→ `handle_action()` 在环境中执行。
- `forward_with_handling()`（agents.py:1062）是精华：`while n_format_fails < max_requeries` 里按异常类型分派——格式错/黑名单/语法错 → 带错误模板**重新询问模型**（requery）；上下文超限/成本超限/API 重试耗尽/连续超时 → 统一 `attempt_autosubmission_after_error` 以结构化 `exit_status` 字符串（`exit_context` / `exit_cost` / `exit_format` / `exit_api` …）优雅收场（agents.py:1175-1218）。
- 环境输出里嵌哨兵 token（`RETRY_WITH_OUTPUT_TOKEN` 等）控制重试语义（agents.py:995-999）。

### 1.3 OpenHands SDK：事件溯源 + 状态机外层循环（与 Zcode-Py JSONL 协议最同构）

- 外层 `LocalConversation._run()`（local_conversation.py:1974）：`while True` 状态机——
  - `PAUSED/STUCK` → break；`FINISHED` → 跑 stop hooks，hook 可**否决停止**并注入反馈继续（local_conversation.py:2005-2032）；
  - `WAITING_FOR_CONFIRMATION` → 清标志继续跑（用户已确认）；
  - `agent.step()` 后若又进入 `WAITING_FOR_CONFIRMATION` → break 等用户（local_conversation.py:2067-2071）；
  - 预算超限 / `max_iteration_per_run` → 发 `ConversationErrorEvent` 收场。
  - 故意**不在循环尾检查 FINISHED**，让并发用户消息经 FIFO 锁置 IDLE 后被下一迭代接住（local_conversation.py:2059-2066，注释写明）。
- 内层 `Agent._step()`（agent.py:703）：
  1. 先取 `get_unmatched_actions()`——确认模式下待执行动作，**再次 run() 即隐式确认**，直接执行后 return（agent.py:712-719）；
  2. hook 拦截的用户消息 → 跳过并 FINISHED（agent.py:723-728）；
  3. `prepare_llm_messages(state.view, condenser, llm)` 增量视图 + 压缩；若返回 `Condensation` 事件则先发出并 return（下一迭代再用压缩后的历史）（agent.py:746-753）；
  4. `llm.generate(tools=..., add_security_risk_prediction=True, on_token=stream.token_callback)` 流式（agent.py:786-793）；
  5. `classify_response` 分派 TOOL_CALLS / CONTENT / REASONING_ONLY / EMPTY（agent.py:874-892）。
- **异常即恢复路径**（agent.py:794-868，最值得抄）：函数调用校验失败 → 错误文本作为 user 消息回灌继续；内容政策拦截 → 提示模型改写继续；`LLMMalformedConversationHistoryError`（tool_use/tool_result 断裂）→ `state.rebuild_view()` + 发 `CondensationRequest` 自愈；`LLMContextWindowExceedError` → 同样 `CondensationRequest`。**上下文炸了不是报错给用户，而是触发压缩事件下一轮重试**。
- 一切皆 Event：MessageEvent / ActionEvent / ObservationEvent / Condensation / SecurityAnalysisEvent / ConversationErrorEvent，`on_event` 回调分发——转录即事件流，与 Zcode-Py "转录即 UI 事件流"的设计互为印证。

### 1.4 browser-use：成熟的领域 Agent 主循环（可整只借来当工具）

- `Agent.run()`（agent/service.py:2521）：注册 SIGINT 处理（第一次暂停、第二次强退）→ `browser_session.start()` → 初始动作 → `while n_steps <= max_steps:` 循环（service.py:2620）：暂停等待 → 连续失败 ≥ max_failures 退出 → stopped 标志退出 → `_execute_step()` → done 则 break。
- `_execute_step()`（service.py:2456）给每步套 `asyncio.wait_for(step_timeout)`，超时算一次失败并保证步数推进（超时后 `n_steps` 手动 +1，service.py:2494-2497——异步取消里计数器容易漏加，这个细节值得抄）。
- `step()`（service.py:1036）：三阶段——`_prepare_context`（抓浏览器状态+截图、更新页面动作模型、注入预算/循环检测/重规划 nudge）→ `_get_next_action`（LLM，`wait_for(llm_timeout)`）→ `_execute_actions`（multi_act，单步最多 `max_actions_per_step=5` 个动作）→ `_post_process`；任何异常统一 `_handle_step_error`，finally `_finalize`（service.py:1074-1086）。

---

## 2. 上下文管理（截断 / 淘汰 / 摘要）

四个项目给出三条不同路线：

### 2.1 Aider：repo-map 预防 + 后台线程摘要（保尾不保头）

- **预防优先**：不塞全文件，用 tree-sitter + PageRank 生成 token 预算内的 repo map，且随当前消息提及的文件/标识符动态加权（base_coder.py:711-748）；in-chat 文件、read-only 文件、repo map 各自成块（chat_chunks）。
- `done_messages`（已完成轮次）与 `cur_messages`（本轮）分离，`move_back_cur_messages` 时触发摘要检查（base_coder.py:1036-1046）。
- `ChatSummary`（history.py:7-143）：`too_big`（history.py:15）检查后**起后台线程摘要**（summarize_start/worker/end，base_coder.py:1002-1034），不阻塞主流程；算法是"保尾摘要头"——倒序累计保留约 `max_tokens/2` 的尾部原文，头部按模型 `max_input_tokens` 截断后整段喂给**廉价模型链**（多模型逐个 fallback，history.py:102-117）生成单条 user 摘要消息；头+尾仍超限则递归（深度上限 3），彻底失败则 summarize_all 全量压缩。摘要后强制以 assistant "Ok." 结尾保持轮次配对（history.py:32-35）。

### 2.2 SWE-agent：可配置的 history processor 管道（对应 Zcode-Py 的 L2 机械淘汰）

- `messages` property 每次**动态**把完整 history 过一遍处理器链（agents.py:546-556）——持久层永远全量，发给 LLM 的才是裁剪版。
- `LastNObservations`（history_processors.py:85-176）：只保留最近 n 条 observation，更早的**原位替换**为 `"Old environment output: (N lines omitted)"`（不删除消息，只换 content——天然保住 tool_use/tool_result 配对）；`polling` 参数让它每 polling 步才收缩一次，专门为**不击穿 prompt cache**（docstring 明说，history_processors.py:106-108）；`keep_output`/`remove_output` tag 精细控制例外；第一条 observation（instance 模板）永不淘汰（:154-155）。
- `ClosedWindowHistoryProcessor`（:215-258）：同一文件的旧打开窗口折叠为 "Outdated window with N lines omitted"——领域感知的淘汰。
- `CacheControlHistoryProcessor`（:261-302）：anthropic 手动打 cache_control 断点（清旧标、标最近 N 条 user/tool）。
- `RemoveRegex`（:305）删 `<diff>` 块；`ImageParsingHistoryProcessor`（:340）把 base64 图转多模态格式。
- **源头截断**：observation 超过 `max_observation_length`（默认 100_000 字符，agents.py:78）时用截断模板（agents.py:67-73，还附带教育模型"换命令/head/tail/grep，别用交互式分页器"）+ 记录 `elided_chars`（agents.py:727-735）——L1 与 L2 分离。

### 2.3 OpenHands SDK：condenser 挂钩 + 异常驱动压缩（L3 摘要的参照）

- `LLMSummarizingCondenser`（llm_summarizing_condenser.py:54）：事件数 > `max_size`(默认240) 触发；`keep_first`(默认2) 条永不压缩；压缩目标 `max_size//2`；除事件数外还有 token 阈值触发（`_effective_max_tokens`）；产出摘要 + `ViewRequest` 事件重建视图。
- **被动自愈**：上文 1.3 所述，上下文超限/历史损坏异常都会转成 `CondensationRequest` 事件让下一轮重试——压缩既是例行维护也是错误恢复路径。
- `PipelineCondenser` 支持多 condenser 串联；`state.view` 是增量维护的 LLM 消息视图（为性能，避免每次全量重建，agent.py:744-745 引用 issue #3053）。

### 2.4 browser-use：滚动压缩块（compacted_memory）

- `MessageCompactionSettings`（views.py:35-56）：`compact_every_n_steps=25` 步进节奏为主闸 + 字符数下限（默认 40000 字符 ≈10k token，`chars_per_token=4`）；`keep_last_items=6`；`summary_max_chars=6000`；**可指定独立 `compaction_llm`**。
- `maybe_compact_messages`（message_manager/service.py:216-280）：`<previous_compacted_memory>` + `<agent_history>` 拼接喂给压缩 LLM；系统提示词值得抄——"任务要求/关键事实/决策/部分进展/错误/下一步 + 保留实体值 URL 路径 + **只有看到明确成功确认才能标记完成，否则一律 IN-PROGRESS**"；压缩输入先过敏感数据过滤。

**对 Zcode-Py 的启示**：四层防御（L1 截断/L2 占位/L3 摘要/L4 冷存）在这四个项目里各有实证——L1=aider token 预检 + SWE-agent max_observation_length；L2=SWE-agent LastNObservations（占位符保配对 + polling 保缓存）；L3=aider 保尾摘要/OpenHands condenser/browser-use 滚动块（均可指定便宜模型）；L4=各家"磁盘全量、发送裁剪"的共识。

---

## 3. 权限 / 确认交互

### 3.1 Aider：confirm_ask 五态 + git 自动快照兜底

- `confirm_ask`（io.py:807-925）：Yes / No / **All（本组全部允许，ConfirmGroup 批量确认）** / Skip all / **"Don't ask again"（never_prompts 按 (question,subject) 记忆）**；`explicit_yes_required=True` 用于危险操作（此时 All 失效，io.py:866-867,908-911）；`--yes` 全局自动批准；每次问答写进 chat 历史（blockquote）可审计。
- 没有工具权限系统，安全网是**每次编辑后 git 自动 commit**（auto_commit，base_coder.py:2375-2392）+ `/undo` + `dirty_commit`（编辑前先提交脏工作区）——"回滚即权限"，与 Zcode-Py 检查点思路一致。

### 3.2 SWE-agent：无人值守 → blocklist 硬过滤 + 超时预算

- `should_block_action`（tools/tools.py:353-366）：前缀 blocklist（vim/emacs/nano/nohup/tail -f/gdb…交互式和长驻命令，tools.py:37-49）+ 精确匹配 blocklist_standalone（裸 `python`、`bash`、`su`…，tools.py:51-65）+ `block_unless_regex` 正则白名单（如 radare2 必须带 `-c`）。
- 预算类护栏：`execution_timeout` 单命令、`max_consecutive_execution_timeouts` 连续超时上限、`total_execution_timeout` 总执行时间（agents.py:1018, 966-973）。
- 命中黑名单 → `_BlockedActionError` → requery 循环带 blocklist_error_template 告诉模型为什么不行（agents.py:946-947, 795-820）——**拒绝也要回灌教育模型**。

### 3.3 OpenHands SDK：风险分级 + 策略对象 + 事件化审计（最完整的参照）

- `SecurityRisk` 枚举 UNKNOWN/LOW/MEDIUM/HIGH（risk.py:13）；`ConfirmationPolicyBase.should_confirm(risk)` 三实现：AlwaysConfirm / NeverConfirm / ConfirmRisky（confirmation_policy.py:9-53）——**风险与策略解耦**，策略可配置。
- 双来源定险：`security_analyzer.analyze_actions()` 外部分析器 + LLM 请求时 `add_security_risk_prediction=True` 让模型自报每个 tool call 的风险（agent.py:790, _extract_security_risk agent.py:1200）。
- 需确认 → 全部 action 标 `ActionConfirmationStatus.WAITING`、`execution_status=WAITING_FOR_CONFIRMATION`、循环挂起；发 `SecurityAnalysisEvent`（含每个动作的风险与细节）入转录可审计（agent.py:1160-1198）；用户再 run() 即隐式确认执行 pending actions（见 1.3）。
- 豁免规则：单个 FinishAction/ThinkAction 永不确认（agent.py:1170-1174）。

### 3.4 browser-use：敏感数据占位符（凭据永进上下文）

- `Agent(sensitive_data={"login":"...","password":"..."})`：提示词里只出现占位符名，模型填值时用 `<secret>placeholder</secret>` 标记（message_manager/service.py:415-416），执行时替换真实值，且所有进历史/导出的消息先 `redact_sensitive_string` 过滤（service.py:576-595）；`save_to_file` 导出时同样按 sensitive_data 过滤（views.py:622-632）。
- 权限交互弱（无逐动作确认），靠 sensitive_data + sandbox/cloud_profile 隔离。

**对 Zcode-Py 的启示**：Zcode-Py 的"规则引擎 + DANGER 黑名单最高优先级 + 会话内总是允许"接近 OpenHands 的 risk×policy 与 aider 的 ConfirmGroup/never_prompts 的并集；SWE-agent 证明黑名单拒绝要配"错误模板回灌"；browser-use 的占位符模式适合 R15 隐私护栏（.env 密钥进工具参数时不进转录明文）。

---

## 4. 会话持久化格式取舍

| 项目 | 格式 | 追加 or 覆写 | 取舍 |
|---|---|---|---|
| aider | `.aider.chat.history.md`（人读 Markdown）+ `.aider.input.history`（输入历史）+ 可选 `.aider.llm.history`（YAML 完整 LLM 消息） | 追加 | 人读优先、可 diff 可 git；但 Markdown 有损（默认不恢复，需 `--restore-chat-history`），完整转录要另开 llm-history（args.py:270-300） |
| SWE-agent | 单个 trajectory JSON（`{info, trajectory:[{action,observation,response,thought,execution_time,state,query,extra_info}]}`） | **每步整文件覆写**（agents.py:779-787） | 单文件即全部真相、评测友好（回放/对比/merge 预测都建在其上）；会话大了重写成本高、并发写不安全——批处理场景无所谓 |
| OpenHands SDK | **每事件一文件**：`events/event-{idx:05d}-{event_id}.json` + `base_state.json`；flock 锁 + 长度 marker sidecar；事件带 `parent_id` 构成**树**（fork/navigate_to 分叉） | 只追加 | 事件溯源：崩溃恢复=重放；分叉靠 parent_id 免复制；代价是文件数多、需要锁协议（event_store.py:188-240, persistence_const.py） |
| browser-use | `AgentHistoryList.save_to_file()` 单 JSON（含每步 state/screenshot_path/model_output/usage）+ `save_conversation_path` 另存 LLM 对话 | 结束时一次性 | 结果产物导向（截图路径、GIF、usage 都在 history 里）；导出按 sensitive_data 过滤 |
| Zcode-Py 规划 | 每会话目录 `transcript.jsonl` 追加式，转录即 UI 事件流 | 追加 | ——与 OpenHands 事件溯源同路，但用 JSONL 单文件追加而非每事件一文件：写放大更小、天然 grep；分叉（R16）可学 parent_id 或学"拷贝前缀+新分支" |

**共识**：磁盘上永远保留全量（L4 冷存储），裁剪只发生在发送给 LLM 的那一份（SWE-agent 的 messages property / OpenHands 的 state.view 都是"发送视图"概念）。

---

## 5. browser-use API 形态（供 M6 接成 Agent 工具）

### 5.1 构造与运行

```python
from browser_use import Agent, BrowserSession, BrowserProfile
agent = Agent(
    task='...',                      # 任务自然语言
    llm=...,                         # browser_use.llm 的 BaseChatModel 实现
    browser_session=BrowserSession(BrowserProfile(headless=True)),
    max_actions_per_step=5,          # 单步最多动作数
    use_vision=True,                 # 截图进多模态输入（'auto' 可自动）
    sensitive_data={'login': '...'}, # 凭据占位符
    generate_gif=True,               # 操作时间线 GIF（R8 的"操作时间线"现成实现）
    max_failures=5, step_timeout=180,
    message_compaction=MessageCompactionSettings(...),  # 默认开启
    injected_agent_state=AgentState(...),  # 注入状态可续跑
)
history = await agent.run(max_steps=500)   # 异步主入口（agent/service.py:2521）
history = agent.run_sync()                 # asyncio.run 包装（service.py:4086-4095）
```

- 官方 quickstart（docs.browser-use.com/quickstart）：`pip install browser-use`（uv venv --python 3.12）+ `uvx browser-use install` 装 Chromium；LLM 类：`ChatBrowserUse`（默认）/`ChatOpenAI`/`ChatAnthropic`/`ChatGoogle`。
- `llm` 参数必给但可为 None——回落 `CONFIG.DEFAULT_LLM` 或 `ChatBrowserUse()` 云端默认（service.py:226-236）。**browser-use 自带整套 LLM 适配层**（browser_use/llm/ 下 anthropic/aws/azure/deepseek/gemini/groq/ollama/openai/chat_browser_use，Protocol `BaseChatModel`，llm/base.py:36），已从 langchain 迁到 openai.types 消息类型（llm/base.py:1-5 注释）。
- 关键回调：`register_new_step_callback` / `register_done_callback` / `run(on_step_start, on_step_end)` 钩子（service.py:155-166, 2524-2525）——Zcode-Py 可借此把每步转发成 `tool_call update` 事件。

### 5.2 截图获取（三条路）

1. **历史截图路径**：`history.screenshot_paths()` / `history.screenshots()`（base64 列表）——agent/views.py:768-790，每步 `state.screenshot_path` 已落盘，直接当 artifact 发 `image_artifact{path}` 最省事。
2. **会话级 API**：`BrowserSession.take_screenshot(path=..., full_page=..., format='png'|'jpeg'|'webp', clip=...)` 返回 bytes（CDP CaptureScreenshot，browser/session.py:4077-4133）；另有 `screenshot_element(selector)`（session.py:4135）。
3. **每步自动截图**：step 内部 `get_browser_state_summary(include_screenshot=True)` 总是抓（即使 use_vision=False，service.py:1096-1098）。

### 5.3 自定义动作与扩展

- `Tools.action(description, param_model=Pydantic模型, domains=[...])` 装饰器注册自定义动作（tools/registry/service.py:291-321），`domains` 限定只在特定站点出现——Zcode-Py 若要把浏览器会话与本地工具联动可用。
- 内置 MCP server（browser_use/mcp/server.py）：也可反向把 Zcode-Py 的浏览器工具暴露为 MCP。

### 5.4 作为可选依赖安装的注意点（对 R8 的直接影响）

- **依赖很重**：pyproject.toml 把 `openai`、`anthropic`、`google-genai`、`groq`、`ollama`、`mcp`、`pillow`、`cdp-use`、`reportlab`、`pypdf` 等**全部钉死为硬依赖**（pyproject.toml dependencies 列表，== 精确版本），且 `requires-python >=3.11,<4.0`。放进 `[browser]` 可选组意味着装它就同时装四家 SDK——与 Zcode-Py 自带的多厂商 Provider 有重叠，体积/版本冲突（httpx、pydantic 钉版）需锁文件统筹。
- 官方支持 extras：`pip install browser-use[video]`（GIF/录像）、`[aws]`、`[examples]` 等（pyproject.toml [project.optional-dependencies]）。
- 顶层 `browser_use/__init__.py` 用**惰性导入**（`Agent`、`BrowserSession` 等通过 lazy-loader 挂名，__init__.py:77-87，注释直言"heavy, over 1 second"）——Zcode-Py 未装该组时 `importlib.util.find_spec('browser_use')` 探测 + 注册工具时才 import 的方案可行且符合其自身约定。
- 集成形态二选一（M6 决策点）：
  - A. 整只嵌入：Zcode-Py 的 browser_use 工具内部构造 `Agent(task, llm=自建 BaseChatModel 适配器桥接 Zcode-Py Provider)`，run() 放线程/子进程；截图走 history.screenshot_paths()；无头配置走 BrowserProfile(headless=)。需要为 Zcode-Py Provider 写一个 BaseChatModel Protocol 适配器（llm/base.py:36 显示接口很小：ainvoke + provider/name/model 属性）。
  - B. 只借 BrowserSession/Tools 当工具集：主模型直接发浏览器动作工具调用，跳过 browser-use 的 LLM 循环（省一层模型，但要自己管 DOM 状态上下文）。
  - 推荐 A 起步（浏览器领域循环复杂度白嫖），B 视效果迭代。

---

## 6. 对 Zcode-Py 的直接建议汇总

1. **loop.py** 抄 OpenHands 的双层结构：外层状态机（含 WAITING_FOR_CONFIRMATION 挂起/恢复、并发消息 FIFO）+ 内层 step（压缩优先于报错：ContextWindowExceeded → compaction 事件自愈重试）；SWE-agent 的异常→exit_status 分类收场与 requery 上限也值得做进错误路径。
2. **compaction** L2 照 SWE-agent LastNObservations：占位符替换不删消息（保 tool_use/tool_result 配对）、polling 步长防击穿 cache_control、首条永不淘汰；L3 照 aider/browser-use：保尾摘要头、可指定便宜模型、"未确认完成一律 IN-PROGRESS"写进压缩提示词。
3. **permissions** 照 OpenHands risk×policy 解耦 + aider ConfirmGroup/never_prompts（=会话内总是允许/不再询问）+ SWE-agent 的"黑名单拒绝必须回灌原因模板"；R30 检查点即 aider 的 auto-commit/undo 模式。
4. **sessions** JSONL 追加（已定）+ 磁盘全量/发送裁剪分离（四家共识）+ 分叉预留 parent_id（OpenHands 事件树）。
5. **browser_use 工具**：可选组探测注册（其自身惰性导入惯例支持）；截图 artifact 用 history.screenshot_paths()；GIF 时间线 generate_gif=True；需要写一个 BaseChatModel 适配器把 Zcode-Py 的 Provider 接给 browser-use。

## 来源

- aider：https://github.com/Aider-AI/aider （base_coder.py / io.py / history.py / mdstream.py / args.py，2026-10-24 main）
- SWE-agent：https://github.com/SWE-agent/SWE-agent （sweagent/agent/agents.py、history_processors.py、tools/tools.py）
- OpenHands SDK：https://github.com/OpenHands/software-agent-sdk （openhands-sdk/openhands/sdk/agent/agent.py、conversation/impl/local_conversation.py、conversation/event_store.py、context/condenser/、security/）
- OpenHands 主仓库（现为前端）：https://github.com/OpenHands/OpenHands ；拆库说明 https://www.openhands.dev/blog/the-path-to-openhands-v1
- browser-use：https://github.com/browser-use/browser-use （browser_use/agent/service.py、views.py、browser/session.py、message_manager/service.py、tools/registry/service.py、pyproject.toml）；官方文档 https://docs.browser-use.com/quickstart
