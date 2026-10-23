# LLM 多厂商 API 形态调研笔记

> 调研日期：2026-10-24。用途：Python 多厂商适配器实现参考。
> 验证方式：官方文档原文（Mintlify `.md` 原文 / llms.txt 索引）、官方 SDK 源码类型定义（anthropic-sdk-python、openai-python、google-genai）、OpenAI 官方 OpenAPI spec。
> 注意：本机直连 `docs.anthropic.com` / `platform.claude.com` 会被地区屏蔽（"App unavailable in region"），Anthropic 文档结论以官方 SDK 源码类型定义为主要依据，文档正文经第三方通道获取并交叉验证。

---

## 1. Anthropic Messages API

官方文档（被地区屏蔽，经镜像/SDK 验证）：
- 流式：<https://docs.anthropic.com/en/docs/build-with-claude/streaming>
- 扩展思考：<https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking>
- 工具调用：<https://docs.anthropic.com/en/docs/build-with-claude/tool-use/overview>
- 提示缓存：<https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- SDK 类型权威来源（本次实际抓取验证）：
  - <https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/__init__.py>
  - <https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/streaming/_messages.py>

### 1.1 端点与最小请求

```text
POST https://api.anthropic.com/v1/messages
头: x-api-key: <KEY>   anthropic-version: 2023-06-01   content-type: application/json
```

```json
{
  "model": "claude-sonnet-4-5",
  "max_tokens": 1024,
  "stream": true,
  "messages": [{"role": "user", "content": "你好"}]
}
```

### 1.2 流式事件序列（SSE）

SSE 帧格式为 `event: <类型>` + `data: <JSON>`，整体顺序：

```text
message_start
  → [对每个内容块] content_block_start → content_block_delta ×N → content_block_stop
  → message_delta（累计 usage + stop_reason）
  → message_stop
中间可穿插 ping 事件；中途出错时收到 event: error
```

关键事件形态（字段名经 SDK `types/` 逐个核实）：

```jsonl
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","content":[],"model":"...","stop_reason":null,"usage":{"input_tokens":25,"output_tokens":1}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":15}}

event: message_stop
data: {"type":"message_stop"}
```

`content_block_delta.delta` 的四种类型（SDK 源文件 `text_delta.py` / `thinking_delta.py` / `signature_delta.py` / `input_json_delta.py`）：

| delta.type | 字段 | 说明 |
|---|---|---|
| `text_delta` | `text: str` | 正文增量 |
| `thinking_delta` | `thinking: str` | 思考文本增量，需拼接 |
| `signature_delta` | `signature: str` | thinking 块签名，在 `content_block_stop` **之前**下发 |
| `input_json_delta` | `partial_json: str` | tool_use 参数的部分 JSON 字符串，拼接后 `json.loads` |

注意事项（SDK `_messages.py:373-547` 流状态机验证）：
- `message_delta.usage` 是**累计值**，实现时应整体覆盖而非累加；
- `stop_reason` 在 `message_delta.delta` 中，不在 `message_start`；
- 中途错误事件：`data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}`；
- `content_block_start` 时 thinking 块为 `{"type":"thinking","thinking":"","signature":""}`，tool_use 块为 `{"type":"tool_use","id":"toolu_...","name":"get_weather","input":{}}`，参数随后经 `input_json_delta` 增量下发。

### 1.3 extended thinking 参数

`thinking` 请求参数（SDK `thinking_config_param.py` 为 Union，2026-10 时点共 4 种变体）：

```json
{"thinking": {"type": "enabled", "budget_tokens": 10000}}
```

| type | 必带字段 | 说明 |
|---|---|---|
| `enabled` | `budget_tokens`（必填） | 预算制思考 |
| `disabled` | 无 | 关闭 |
| `adaptive` | 可选 `display` | 自适应思考（新模型） |
| `between_tools` | 无 | 仅在工具调用之间思考 |

- `budget_tokens` 必须 **≥1024** 且 **< max_tokens**（SDK docstring 原文："Must be ≥1024 and less than `max_tokens`"）；
- 可选 `display: "summarized" | "omitted"` 控制思考内容呈现（Claude 4 起默认 summarized，即摘要式思考）；
- 与 `temperature`/`top_k` 修改、assistant prefill 不兼容；开启 thinking 时 tool_choice 只支持 `auto`/`none`；
- **工具调用回传要求**：thinking 块必须连同 `signature` 原样回传，缺失会被 API 拒绝；`redacted_thinking` 块（`{"type":"redacted_thinking","data":"..."}`）同样必须原样回传；
- `max_tokens > 21333` 时必须使用流式（否则 400）；
- 交错思考（工具调用间思考）需 beta 头 `anthropic-beta: interleaved-thinking-2025-05-14`。

各模型预算上限（数字来源为搜索摘要交叉验证 [Anthropic Claude 4 发布页](https://www.anthropic.com/news/claude-4) 与 [AWS Bedrock 扩展思考指南](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html)、[PromptHub 汇总](https://www.prompthub.us/blog/the-complete-guide-to-claude-opus-4-and-claude-sonnet-4)；本环境无法直连 Anthropic 官方模型表逐字核对，实现时建议按"budget < max_tokens 且 ≥1024"做校验）：

| 模型 | max output tokens | 最大思考预算 |
|---|---|---|
| claude-3-7-sonnet | 64k（beta 可 128k） | 64k |
| claude-sonnet-4 / 4-5 | 64k | 64k |
| claude-opus-4 / 4-1 | 32k | 64k（受 max_tokens 约束实际达不到） |

### 1.4 tool_use 块形态

请求侧（tools 定义）与响应/回传侧：

```json
{"tools": [{"name": "get_weather", "description": "查天气",
            "input_schema": {"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}]}
```

响应 `content[]` 中：`{"type":"tool_use","id":"toolu_01A...","name":"get_weather","input":{"city":"北京"}}`
回传（user 消息 content 中）：`{"type":"tool_result","tool_use_id":"toolu_01A...","content":"晴，25℃"}`
（SDK `tool_use_block.py` 另有可选 `caller`、`toolset_name` 字段，为服务端工具预留，普通函数调用可忽略。）

### 1.5 cache_control 断点用法

在 content 块、tools、system 上打标（SDK `cache_control_ephemeral_param.py`）：

```json
{"system": [{"type":"text","text":"<长系统提示>","cache_control":{"type":"ephemeral"}}]}
```

- `cache_control: {"type":"ephemeral","ttl":"5m"|"1h"}`，`ttl` 缺省 `5m`；
- 每个 prompt 最多 **4 个断点**（官方西语版文档原文确认："Puedes definir hasta 4 puntos de interrupción de caché"）；
- 可缓存前缀最小 **1024 tokens**（较新模型 2048，搜索摘要来源）；
- usage 中 `cache_creation_input_tokens` / `cache_read_input_tokens` 分别对应写缓存与读缓存计费；总输入 = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`（SDK `message_delta_usage.py` docstring 原文）。

---

## 2. OpenAI Chat Completions

- API 参考：<https://developers.openai.com/api/docs/api-reference/chat/streaming>（正文 JS 渲染，本次以 openai-python SDK 类型 + 官方 [openapi spec](https://github.com/openai/openai-openapi/blob/master/openapi.yaml) 为准）
- Reasoning 指南：<https://developers.openai.com/api/docs/guides/reasoning>

### 2.1 端点与最小请求

```text
POST https://api.openai.com/v1/chat/completions
头: Authorization: Bearer <KEY>
```

```json
{"model":"gpt-5.2","messages":[{"role":"user","content":"你好"}],"stream":true}
```

### 2.2 流式增量形态（chat.completion.chunk）

`openai-python` `chat_completion_chunk.py` 核实：

```jsonl
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"gpt-5.2","choices":[{"index":0,"delta":{"role":"assistant","content":"你"},"finish_reason":null}]}
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"gpt-5.2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"gpt-5.2","choices":[],"usage":{"prompt_tokens":9,"completion_tokens":12,"total_tokens":21}}
data: [DONE]
```

- `choices[].delta` 可含：`role`、`content`、`tool_calls`（`{index, id?, function:{name?, arguments?}, type:"function"}`）、`refusal`、`function_call`（废弃）、`audio`；
- `finish_reason`：`stop | length | tool_calls | content_filter | function_call`（废弃）；
- 设 `stream_options: {"include_usage": true}` 时**最后一个 chunk** `choices` 为空数组、携带 `usage`；chunk 顶层新增 `obfuscation`（填充字符串，`stream_options.include_obfuscation=false` 可关）与 `service_tier`；
- **OpenAI 官方 Chat Completions 不返回 reasoning 文本**；推理过程摘要需走 Responses API（`reasoning.summary`，流式事件 `response.reasoning_summary_text.delta` / `response.reasoning_text.delta`，SDK `responses/__init__.py` 类型确认）。兼容厂商普遍用非标的 `delta.reasoning_content`（见 GLM 一节）。

### 2.3 reasoning_effort

openai-python `shared/reasoning_effort.py` 与 openapi spec `ReasoningEffort` 枚举（默认 `medium`）：

```text
none | minimal | low | medium | high | xhigh | max
```

- 顶层请求参数：`"reasoning_effort": "low"`；
- **支持子集因模型而异**（官方指南原文："Supported values are model-dependent"）：GPT-6.1 Sol 不支持 `none`/`minimal`；GPT-6 Astra 不支持 `none`（返回 400）；旧 o3/o4-mini 系列仅 `minimal/low/medium/high`；适配器应做好 400 时降档重试或透传错误；
- `none` = 完全不做推理（延迟敏感任务），`minimal` = 尽量少推理；
- 官方指南同时出现嵌套形态 `reasoning: {"effort": ...}`（新 API 形态），顶层 `reasoning_effort` 是 Chat Completions 的经典形态，二者在最新文档中并存。

### 2.4 developer message

- `role: "developer"` 是 `system` 的替代（o 系列/GPT-5 起推荐），指令优先级：`developer`/`system` > `user`（openapi spec `EasyInputMessage` 描述原文："Instructions given with the `developer` or `system` role take precedence over instructions given with the `user` role"）；
- 示例：`{"role": "developer", "content": "You are a helpful assistant."}`（spec 示例原文）；
- 流式 delta 中 `role` 枚举同样包含 `developer`。

---

## 3. 智谱 GLM

文档（Mintlify，本次已抓 `.md` 原文）：
- Claude API 兼容：<https://docs.bigmodel.cn/cn/guide/develop/claude/introduction>
- 深度思考：<https://docs.bigmodel.cn/cn/guide/capabilities/thinking>
- 思考模式（交错/保留/轮级）：<https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode>
- 对话补全 API 参考：<https://docs.bigmodel.cn/api-reference/模型-api/对话补全>
- GLM-5.3 模型页：<https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3>

### 3.1 Anthropic 兼容端点

```text
base_url: https://open.bigmodel.cn/api/anthropic   （SDK 自动拼 /v1/messages）
完整路径: POST https://open.bigmodel.cn/api/anthropic/v1/messages
认证: x-api-key: <智谱APIKey>（官方 curl 示例原文）
```

官方 curl 示例（文档原文）：

```bash
curl https://open.bigmodel.cn/api/anthropic/v1/messages \
  --header "x-api-key: YOUR_API_KEY" \
  --header "content-type: application/json" \
  --data '{"model":"glm-5.3","max_tokens":1024,"stream":true,
           "messages":[{"role":"user","content":"Hello, ZHIPU"}]}'
```

- 换 Anthropic SDK 只改 `base_url` + `api_key` + `model` 三处，其余（stream/tools 等）保持 Anthropic 语义；官方声明"某些场景下仍存在差异，但不影响整体兼容性"。

### 3.2 OpenAI 兼容端点

```text
POST https://open.bigmodel.cn/api/paas/v4/chat/completions
认证: Authorization: Bearer <KEY>
```

```json
{"model":"glm-5.3","messages":[{"role":"user","content":"解释量子计算"}],
 "thinking":{"type":"enabled"},"reasoning_effort":"max",
 "stream":true,"max_tokens":4096,"temperature":1.0}
```

### 3.3 思考参数（重点差异）

| 参数 | 取值 | 说明 |
|---|---|---|
| `thinking.type` | `enabled` / `disabled` | **GLM-5.3 / 5.3-FLASH 传 `disabled` 会直接报错**（强制思考）；5.3 系列为"强制思考"，5.2/5.1/5/4.6/4.5 为"自动判断" |
| `reasoning_effort` | 标准 API：GLM-5.3 仅 `max`(默认)/`high`/`low`，其余值报错；GLM-5.2 支持 `max/xhigh/high/medium/low/minimal/none`，且 `low`/`medium`→`high`、`xhigh`→`max`、`none`/`minimal`=放弃思考 | 仅 GLM-5.2 及以上支持 |
| `reasoning_effort`（Coding Plan 端点） | 5.3：`none/minimal/low`→`low`，`medium/high`→`high`，`xhigh/max`→`max` | Coding Plan 是订阅制端点，映射更宽容 |
| `clear_thinking` | `true`(默认)/`false` | `false` 开启"保留式思考"（Preserved Thinking）：须把历史 `reasoning_content` **原样、按序**回传 |

思考内容输出：非流式 `choices[0].message.reasoning_content`，流式 `choices[0].delta.reasoning_content`（与 DeepSeek 约定一致，非 OpenAI 官方字段）。

其它要点：
- GLM-5.3：1M 上下文、128K 最大输出、仅文本模态；
- 交错式思考（工具间思考）自 GLM-4.5 起默认支持；工具调用回传时**必须保留 reasoning content 一并传回**（thinking-mode 文档原文）；
- 轮级思考（Turn-level，GLM-4.7+）：每轮可独立开关 thinking；
- Anthropic 兼容端点上思考块按 Anthropic `thinking` 块语义映射（官方未公布逐字段映射表，适配器实现时应实测 `thinking_delta`/`signature_delta` 是否下发——**此项未验证**）。

---

## 4. OpenRouter

文档（本次抓取 <https://openrouter.ai/docs/llms-full.txt> 全文 4.8MB 核对）：

### 4.1 端点与认证

```text
base: https://openrouter.ai/api/v1
皮肤: /chat/completions（OpenAI 兼容） · /responses（OpenAI Responses） · /messages（Anthropic 兼容）
认证: Authorization: Bearer <KEY>；可选 HTTP-Referer、X-Title 头（用于排行归因）
模型名: vendor/model，如 anthropic/claude-4.6-sonnet
```

### 4.2 与 OpenAI 的差异（适配器必处理）

1. **SSE 注释行**：流中会插入 `: OPENROUTER PROCESSING` 注释帧（防超时心跳）。手写解析器必须跳过以 `:` 开头的行再 `JSON.parse`，否则崩溃（官方警告原文）。
2. **usage chunk 偏离 spec**：流末尾 `[DONE]` 前有一个带 `usage` 的 chunk，但与 OpenAI 的空 `choices` 不同，OpenRouter 故意放入**一个 content 为空、重复 finish_reason 的 choice**（`native_finish_reason` 同现）。校验流时不要把它当第二个终止事件。
3. **中途错误**：以普通 `data:` chunk 顶层 `error` 对象出现，`error.metadata.error_type` 是稳定错误码（如 `provider_unavailable`）；非流式时错误嵌在最终响应里、`finish_reason: "error"`。
4. **usage 位置**：Chat Completions 在末尾 usage chunk；`/responses` 在 `response.completed` 事件；`/messages` 在 `message_delta` 事件（与 Anthropic 一致）。
5. 响应头 `X-Generation-Id` 用于调试/对账。

### 4.3 reasoning 参数（OpenRouter 私有）

```json
{"model":"anthropic/claude-4.6-opus","reasoning":{"enabled":true,"effort":"high","max_tokens":10000,"exclude":false}}
```

- `reasoning.effort`：`xhigh|high|medium|low|minimal|none`（顶层 `reasoning_effort` 为 OpenAI 风格别名）；
- `reasoning.max_tokens` 显式设置时走预算制思考，会转换为厂商 budget；
- `reasoning.exclude: true` 不返回思考文本；`include_reasoning` 是**废弃别名**；
- 对 Claude 4.6+：`reasoning.effort` 映射到 Anthropic `output_config.effort`（2026-06-22 起），`verbosity` 参数优先级更高；老模型（4.5/3.7）effort 仍转 `thinking.budget_tokens`。

---

## 5. Ollama

文档（本次抓取 GitHub 原文核对）：
- OpenAI 兼容：<https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx>
- Thinking 能力：<https://github.com/ollama/ollama/blob/main/docs/capabilities/thinking.mdx>
- 原生 API：<https://github.com/ollama/ollama/blob/main/docs/api.md>
- Anthropic 兼容：<https://github.com/ollama/ollama/blob/main/docs/api/anthropic-compatibility.mdx>

### 5.1 OpenAI 兼容接入

```text
本地: base_url = http://localhost:11434/v1/   （api_key 任意非空值，服务端忽略，如 "ollama"）
云端: base_url = https://ollama.com/v1        （api_key = OLLAMA_API_KEY）
```

`/v1/chat/completions` 支持与不支持（文档清单原文）：
- 支持：`stream`、`stream_options.include_usage`、`max_tokens`、`tools`、`response_format`(JSON mode)、`seed`、`stop`、`temperature`、`top_p`、`frequency_penalty`、`presence_penalty`、`reasoning_effort`、`reasoning.effort`；
- **不支持**：`tool_choice`、`logit_bias`、`user`、`n`、Logprobs；图片仅支持 base64 data-URL，不支持远程 `image_url`；
- `reasoning_effort` 兼容映射（无 thinking 元数据的模型）：`minimal`→`low`、`xhigh`/`ultra`→`max`、`none`→关闭；`/api/show` 返回模型 `thinking.values`/`default` 可发现各模型真实档位。

### 5.2 原生 API 与 thinking

```bash
curl http://localhost:11434/api/chat -d '{
  "model":"qwen3","messages":[{"role":"user","content":"..."}],
  "think":true,"stream":false}'
```

- `think`: `true` / `false` / `null`(用模型默认) / 字符串档位（如 `"low"`，须与 `/api/show` 的 `values` 完全一致）；
- 响应思考与正文分字段：`message.thinking` 与 `message.content`（generate 则是 `thinking` / `response`）；
- 流式为 **ndjson**（每行一个 JSON 对象，非 SSE）：增量行 `{"model":...,"created_at":...,"message":{"role":"assistant","content":"The"},"done":false}`，末行 `done:true` 并带 `total_duration`/`prompt_eval_count`/`eval_count` 等统计。

### 5.3 Anthropic 兼容层

```text
本地: POST http://localhost:11434/v1/messages
      头: x-api-key: ollama（任意） + anthropic-version: 2023-06-01
云端: POST https://ollama.com/v1/messages —— 必须用 Authorization: Bearer，不接受单独 x-api-key
```

`/v1/responses` 仅支持无状态形态（无 `previous_response_id`/`conversation`）。

---

## 6. Google Gemini generateContent

文档：
- Thinking（generateContent 版）：<https://ai.google.dev/gemini-api/docs/generate-content/thinking>（本次经 WebFetch 获取，页面语言协商异常返回意/西语版本，数字与结构与英文版一致）
- API 参考：<https://ai.google.dev/api/generate-content>
- SDK 类型：<https://github.com/googleapis/python-genai/blob/main/google/genai/types.py>（本次已抓取核对 `ThinkingConfig`）

### 6.1 REST 端点

```text
非流式: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
流式:   POST https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse
认证:   头 x-goog-api-key: <KEY>  或查询参数 ?key=<KEY>
流式形态: alt=sse 时为 SSE data: 帧（GenerateContentResponse 实例流）；不加 alt 则返回 JSON 数组流
```

### 6.2 thinkingConfig（REST 字段为 camelCase）

```bash
curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' -X POST \
  -d '{
    "contents": [{"parts": [{"text": "列出三位著名物理学家"}]}],
    "generationConfig": {"thinkingConfig": {"thinkingBudget": 1024, "includeThoughts": true}}
  }'
```

SDK `types.py:5802` `ThinkingConfig` 核实（REST JSON 用 camelCase，SDK 用 snake_case）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `thinkingBudget` | int | `0`=禁用思考，`-1`=自动（动态）；范围模型相关 |
| `includeThoughts` | bool | 响应中返回思考摘要 part |
| `thinkingLevel` | enum | `minimal/low/medium/high`（Gemini 3/4 系列使用；`ThinkingLevel` 大小写不敏感枚举） |

thinkingBudget 各模型范围（官方 thinking 文档表格）：

| 模型 | 范围 | 默认 | 能否关闭 |
|---|---|---|---|
| gemini-2.5-pro | 128–32768 | 动态(-1) | **不能**（0 报错） |
| gemini-2.5-flash | 0–24576 | 动态(-1) | budget=0 |
| gemini-2.5-flash-lite | 512–24576 | **不思考** | budget=0 |

- Gemini 3/4 系列：改用 `thinkingLevel`，`thinkingBudget` 已弃用——单独传会映射到最近 level 并返回警告，与 `thinkingLevel` 同传报 400；
- 思考 part 形态：`candidates[0].content.parts[]` 中 `{"text":"...","thought":true}` 为思考摘要，无 `thought` 字段的为正文；流式时摘要增量下发；
- `maxOutputTokens` 是**含思考 token** 的硬上限；思考超限会以 `finish_reason: MAX_TOKENS` 截断且照常计费（计费字段 `usageMetadata.thoughtsTokenCount`，按完整思考 token 而非摘要）。

---

## 7. 适配器实现要点汇总（跨厂商）

1. **思考内容三种通道**：Anthropic=`thinking` 内容块（流式 `thinking_delta`）；OpenAI=不透出（Responses summary 事件）；GLM/DeepSeek 系=`delta.reasoning_content`；Ollama 原生=`message.thinking`；Gemini=`part.thought=true`。适配层应统一抽象为 (thinking_delta, text_delta, tool_call_delta) 三路。
2. **工具调用流式三种编码**：Anthropic=`input_json_delta.partial_json` 按 content block index 聚合；OpenAI=`delta.tool_calls[].function.arguments` 按 `index` 聚合（id/name 只在首块）；Gemini=functionCall part 整块下发（非增量）。
3. **SSE 解析健壮性**：跳过 `:` 注释行（OpenRouter 心跳）；`[DONE]` 只在 OpenAI 系出现；Ollama 原生是 ndjson 不是 SSE；Gemini `alt=sse` 与非 sse 两种流形态。
4. **思考开关校验**：GLM-5.3 禁止 disabled；Gemini 2.5 Pro 禁止 budget=0；Claude budget≥1024 且 <max_tokens；OpenAI 各模型 reasoning_effort 子集不同，400 时应能降档。
5. **认证头差异**：Anthropic 系 `x-api-key`+`anthropic-version`；OpenAI 系 `Authorization: Bearer`；Ollama 云端 Anthropic 端点要 Bearer；Gemini `x-goog-api-key` 或 `?key=`。
