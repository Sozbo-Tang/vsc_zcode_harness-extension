"""todo 工具（M2 只读四件套之一）：会话级任务清单。

ZCode TodoWrite 语义（docs/research/zcode-semantics.md §2）：每次调用以
**整份列表**覆盖（不做增量 patch），状态三态 pending/in_progress/completed；
同一时刻建议至多一项 in_progress。列表存 ToolContext.state（会话生命周期）。
"""

from __future__ import annotations

from . import ToolOutcome, ToolSpec, error_outcome

TODO_STATUSES = ("pending", "in_progress", "completed")
STATE_KEY = "todos"


def tool_todo(args: dict, ctx) -> ToolOutcome:
    todos_raw = args.get("todos")
    if not isinstance(todos_raw, list) or not todos_raw:
        return error_outcome("todo requires a non-empty 'todos' array (send the FULL list each call)")
    todos: list[dict] = []
    for i, item in enumerate(todos_raw):
        if not isinstance(item, dict):
            return error_outcome(f"todos[{i}] must be an object")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            return error_outcome(f"todos[{i}].content must be a non-empty string")
        status = item.get("status", "pending")
        if status not in TODO_STATUSES:
            return error_outcome(
                f"todos[{i}].status must be one of {list(TODO_STATUSES)}, got {status!r}"
            )
        entry = {"content": content.strip(), "status": status}
        active = item.get("activeForm")
        if isinstance(active, str) and active.strip():
            entry["activeForm"] = active.strip()
        todos.append(entry)
    in_progress = sum(1 for t in todos if t["status"] == "in_progress")
    if in_progress > 1:
        return error_outcome(
            f"{in_progress} todos are in_progress; keep at most one at a time"
        )
    ctx.state[STATE_KEY] = todos
    done = sum(1 for t in todos if t["status"] == "completed")
    return ToolOutcome(
        result={"todos": todos, "completed": done, "total": len(todos)}
    )


TODO_SPEC = ToolSpec(
    name="todo",
    description=(
        "Maintain the session task list. Send the FULL list every call "
        "(it replaces the previous one); each item {content, status: "
        "pending|in_progress|completed, activeForm?}; at most one in_progress."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": list(TODO_STATUSES)},
                        "activeForm": {"type": "string"},
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["todos"],
    },
    tier="readonly",
    handler=tool_todo,
)
