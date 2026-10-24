"""写侧工具：write / edit(str_replace)（M2 任务书）。

- write：整文件写入（父目录自动创建）。权限走引擎（write 档默认询问，
  由 ToolSession 在 handler 之外完成 permission_request 往返）。
- edit：str_replace 语义——先广播 diff_proposed，收到 diff_review
  {accepted:true} 才写盘，拒绝则不动文件（任务书原文）。审阅回调由
  ToolContext.review_diff 注入；**未注入时一律拒绝**（fail-closed）。
  old_str 必须在文件中恰好出现一次（除非 replace_all），防盲替换。
"""

from __future__ import annotations

from pathlib import Path

from . import ToolOutcome, ToolSpec, error_outcome


def _resolve(path_raw: str, cwd: Path) -> Path:
    p = Path(path_raw).expanduser()
    return p if p.is_absolute() else (cwd / p)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def tool_write(args: dict, ctx) -> ToolOutcome:
    path_raw = args.get("path")
    content = args.get("content")
    if not isinstance(path_raw, str) or not path_raw:
        return error_outcome("write requires a string 'path'")
    if not isinstance(content, str):
        return error_outcome("write requires a string 'content'")
    path = _resolve(path_raw, ctx.cwd)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(
            result={"error": "io_error", "detail": f"{type(exc).__name__}: {exc}"},
            is_error=True,
        )
    return ToolOutcome(result={"path": str(path_raw), "bytes": len(content.encode("utf-8")),
                               "status": "written"})


# ---------------------------------------------------------------------------
# edit（str_replace + diff 审阅闸门）
# ---------------------------------------------------------------------------


def tool_edit(args: dict, ctx) -> ToolOutcome:
    path_raw = args.get("path")
    old_str = args.get("old_str")
    new_str = args.get("new_str")
    if not isinstance(path_raw, str) or not path_raw:
        return error_outcome("edit requires a string 'path'")
    if not isinstance(old_str, str):
        return error_outcome("edit requires a string 'old_str'")
    if not isinstance(new_str, str):
        return error_outcome("edit requires a string 'new_str'")
    replace_all = args.get("replace_all") is True
    if old_str == new_str:
        return error_outcome("old_str and new_str are identical")
    path = _resolve(path_raw, ctx.cwd)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ToolOutcome(result={"error": "not_found", "detail": f"no such file: {path}"},
                           is_error=True)
    except OSError as exc:
        return ToolOutcome(result={"error": "io_error", "detail": f"{type(exc).__name__}: {exc}"},
                           is_error=True)

    count = text.count(old_str)
    if count == 0:
        return ToolOutcome(
            result={"error": "old_str_not_found",
                    "detail": "old_str does not appear in the file; read the file and retry"},
            is_error=True,
        )
    if count > 1 and not replace_all:
        return ToolOutcome(
            result={"error": "old_str_ambiguous",
                    "detail": f"old_str appears {count} times; add surrounding context or set replace_all"},
            is_error=True,
        )

    new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)

    # diff 审阅闸门：回调缺位＝无人审阅＝拒绝（fail-closed）
    if ctx.review_diff is None:
        return ToolOutcome(
            result={"error": "diff_review_unavailable",
                    "detail": "no diff reviewer attached; refusing to modify the file"},
            is_error=True,
        )
    accepted = ctx.review_diff(path_raw, old_str, new_str)
    if not accepted:
        return ToolOutcome(
            result={"error": "diff_rejected",
                    "detail": "user rejected the proposed change; file left unchanged"},
            is_error=True,
        )
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(result={"error": "io_error", "detail": f"{type(exc).__name__}: {exc}"},
                           is_error=True)
    return ToolOutcome(
        result={"path": path_raw, "status": "applied",
                "replaced": count if replace_all else 1,
                "diff": f"-{old_str[:400]}\n+{new_str[:400]}"},
    )


WRITE_SPEC = ToolSpec(
    name="write",
    description="Write a whole file (creates parent directories). Asks permission by default.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "full new file content"},
        },
        "required": ["path", "content"],
    },
    tier="write",
    handler=tool_write,
)

EDIT_SPEC = ToolSpec(
    name="edit",
    description=(
        "str_replace edit: replace old_str with new_str in a file. old_str must "
        "match exactly once (or set replace_all). Every edit is shown to the user "
        "as a diff for review and is applied only after acceptance; rejections "
        "leave the file unchanged."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_str": {"type": "string", "description": "exact text to replace (include surrounding lines for uniqueness)"},
            "new_str": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "replace every occurrence (default false)"},
        },
        "required": ["path", "old_str", "new_str"],
    },
    tier="write",
    handler=tool_edit,
)
