"""上下文压缩四层防御（PLAN §3.4，R14）：M8 落地 L2/L3/L4。

- **L2 机械淘汰**（eviction.py）：上下文用量超阈值（默认 50%）后，把较早
  轮次的 tool_result content 替换为占位符。**Anthropic 回放约束（PLAN §8
  专测项）：tool_use/tool_result 配对不断裂**——只换 content，绝不删块、
  不删消息，角色交替原样保留。
- **L3 自动摘要**（summary.py + manager.py）：超阈值（默认 85%）或
  ``control compact`` 手动触发——用 Provider（可指定便宜模型，沿用
  vision_relay 的 providerId/modelId 解析）把头部历史压成七要素结构化摘要
  （任务目标/关键决策/文件及状态/错误与解法/环境事实/待办/下一步），
  保留尾部 ~15% 原文轮次。
- **L4 冷存 + search_history**（coldstore.py）：完整转录本就在
  transcript.jsonl（PLAN §3.2 决策：转录即冷存，天然可 grep）；
  search_history 是面向模型的只读检索工具，命中行带上下文返回。

每次压缩广播 ``compaction{layer, report}``（协议帧已注册，PLAN §5），
报告含 evicted 数 / 摘要 token / 压缩前后 token 对比。
"""

from .coldstore import SEARCH_HISTORY_SPEC, search_transcript
from .eviction import (
    EVICT_PLACEHOLDER_TEMPLATE,
    evict_tool_results,
    is_placeholder,
    pairing_intact,
)
from .manager import CompactionManager, default_context_window
from .summary import (
    SUMMARY_MAX_OUTPUT_TOKENS,
    compact_with_summary,
    resolve_compaction_target,
    summarize_messages,
)

__all__ = [
    "EVICT_PLACEHOLDER_TEMPLATE",
    "SEARCH_HISTORY_SPEC",
    "SUMMARY_MAX_OUTPUT_TOKENS",
    "CompactionManager",
    "compact_with_summary",
    "default_context_window",
    "evict_tool_results",
    "is_placeholder",
    "pairing_intact",
    "resolve_compaction_target",
    "search_transcript",
    "summarize_messages",
]
