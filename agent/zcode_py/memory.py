"""跨会话持久记忆 MEMORY.md（PLAN §2.7 R24、M4）。

定位是「小事实库」：用户偏好、项目约定、长期有效的决定。不是检索库、
不是第二上下文窗口——全文件在会话启动时注入 system prompt 尾部，上限
4000 字符，超出截断并注明（Agent 可用 replace 精简）。

Agent 经 ``memory`` 工具自主维护：mode=read 只读免批；append/replace 是
持久副作用，与 write 同档默认询问（PLAN §9.5）。READONLY_TOOLS 是按名字
集合判定的（permissions.py），按 mode 分档放不进那个集合，故以
PermissionEngine 子类在 check 处按调用参数分流。
"""

from __future__ import annotations

from pathlib import Path

from .config import home_dir
from .permissions import ALLOW, PermissionCheck, PermissionEngine
from .tools import ToolContext, ToolOutcome, ToolRegistry, ToolSpec, error_outcome

MEMORY_FILENAME = "MEMORY.md"
# 注入上限（字符）：记忆应是少量长期事实；超限截断并在尾部注明省略量，
# 提示 Agent 用 memory(mode=replace) 主动精简而不是无限追加。
MEMORY_INJECTION_LIMIT = 4000


def memory_path() -> Path:
    return home_dir() / MEMORY_FILENAME


def read_memory() -> str:
    path = memory_path()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""  # 读失败按空记忆处理：注入缺席好过注入损坏内容


def append_memory(text: str) -> str:
    """追加一条（保证行界：现有尾部与新增条目各自收尾换行，不粘行）。"""
    content = read_memory()
    addition = text.rstrip("\n")
    if content and not content.endswith("\n"):
        content += "\n"
    content += addition + "\n"
    _write(content)
    return content


def replace_memory(text: str) -> str:
    _write(text)
    return text


def _write(content: str) -> None:
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def memory_injection() -> str | None:
    """拼到 system prompt 尾部的记忆块；无文件/空文件返回 None（不占 token）。"""
    content = read_memory()
    if not content.strip():
        return None
    if len(content) > MEMORY_INJECTION_LIMIT:
        omitted = len(content) - MEMORY_INJECTION_LIMIT
        content = content[:MEMORY_INJECTION_LIMIT].rstrip("\n") + (
            f"\n... [MEMORY.md truncated: {omitted} chars omitted — "
            "use the memory tool (mode=replace) to slim it down] ..."
        )
    return (
        "\n\n## Cross-session memory (MEMORY.md)\n"
        "Small long-lived facts about the user and project, loaded at session "
        "start. Keep entries short; manage with the `memory` tool "
        "(mode=read|append|replace).\n\n" + content
    )


class MemoryPermissionEngine(PermissionEngine):
    """memory 工具按 mode 分档：read 免批；append/replace 落默认询问档。"""

    def check(self, tool: str, args: dict) -> PermissionCheck:
        if tool == "memory" and (args.get("mode") or "read") == "read":
            return PermissionCheck(
                outcome=ALLOW,
                ruleId="readonly",
                reason="memory read is read-only",
            )
        return super().check(tool, args)


def _handle_memory(args: dict, ctx: ToolContext) -> ToolOutcome:
    mode = args.get("mode")
    if mode == "read":
        return ToolOutcome(result={"content": read_memory()})
    if mode not in ("append", "replace"):
        return error_outcome(f"unknown mode {mode!r}; expected read|append|replace")
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return error_outcome("mode=append|replace requires a non-empty 'text'")
    if mode == "append":
        append_memory(text)
    else:
        replace_memory(text)
    return ToolOutcome(result={"ok": True, "mode": mode, "chars": len(text)})


MEMORY_SPEC = ToolSpec(
    name="memory",
    description=(
        "Persistent cross-session memory stored in MEMORY.md under the "
        "zcode-py home. mode=read returns the full file; mode=append adds "
        "one entry; mode=replace rewrites the whole file. Keep entries "
        "short and factual."
    ),
    parameters={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["read", "append", "replace"],
                "description": "read=取全文；append=追加一条；replace=整文件写回",
            },
            "text": {
                "type": "string",
                "description": "append 的条目内容 / replace 的完整新内容",
            },
        },
        "required": ["mode"],
    },
    tier="write",  # 可写工具：与 write 同档；read 分流见 MemoryPermissionEngine
    handler=_handle_memory,
)


def register_memory_tool(registry: ToolRegistry) -> None:
    """把 memory 工具挂进注册表（main 在 default_registry 之后追加）。"""
    registry.register(MEMORY_SPEC)
