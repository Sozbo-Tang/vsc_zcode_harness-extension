"""Notebook 工具（R31，M8）：notebook_read / notebook_write / notebook_run。

依赖口径（任务书）：

- ``nbformat`` 是**必装**主依赖（轻量、纯元数据操作）——read/write 用它；
- ``nbclient`` 放可选组 ``[notebook]``——run 需要真执行；未安装时返回
  **结构化安装指引**（同 browser-use 可选组模式），不抛异常。

权限档：

- ``notebook_read``：readonly（免批）；
- ``notebook_write`` / ``notebook_run``：write（默认询问）——改源码与执行
  代码都是副作用（执行可写文件/发网络，与 bash 同级敏感）。

``notebook_run`` 执行语义：内核状态下前面的单元格是后面的输入，故执行
始终跑**整个 notebook**（nbclient 无可靠的单段执行），``cell_index``
只是**报告聚焦**——有它只回传该格的输出/错误摘要，无它回传全部执行
单元格的输出摘要。测试对 nbclient 全打桩（importorskip + mock）。
"""

from __future__ import annotations

from pathlib import Path

from . import ToolOutcome, ToolSpec, error_outcome

try:
    import nbformat
except ImportError:  # pragma: no cover —— nbformat 是必装依赖，缺位只在破损环境
    nbformat = None

NBCLIENT_GUIDE = (
    "notebook 执行需要可选依赖 nbclient（未安装）。安装："
    "`uv sync --project agent --group notebook`（或 `pip install nbclient`），"
    "重启会话后 notebook_run 即可用；notebook_read/notebook_write 不受影响。"
)

SOURCE_SNIPPET_CHARS = 400
OUTPUT_SNIPPET_CHARS = 400


def _load_notebook(path: Path):
    if nbformat is None:
        return None, ToolOutcome(
            result={"error": "dependency_missing", "detail": "nbformat is not installed"},
            is_error=True,
        )
    try:
        notebook = nbformat.read(str(path), as_version=4)
    except FileNotFoundError:
        return None, ToolOutcome(
            result={"error": "not_found", "detail": f"notebook not found: {path}"},
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001 —— nbformat 校验/解析错统一结构化
        return None, ToolOutcome(
            result={"error": "invalid_notebook", "detail": f"{type(exc).__name__}: {exc}"},
            is_error=True,
        )
    return notebook, None


def _snippet(text: str, limit: int) -> str:
    text = text or ""
    return text[:limit] + ("…" if len(text) > limit else "")


def _resolve_path(raw, ctx) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else ctx.cwd / candidate


def _cell_source(cell) -> str:
    source = cell.get("source", "")
    return source if isinstance(source, str) else "".join(source)


def _output_summary(output: dict) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        return _snippet(str(output.get("text") or ""), OUTPUT_SNIPPET_CHARS)
    if kind in ("execute_result", "display_data"):
        data = output.get("data") or {}
        for key in ("text/plain", "text/markdown", "text/html"):
            if key in data:
                value = data[key]
                if isinstance(value, list):
                    value = "".join(value)
                return _snippet(str(value), OUTPUT_SNIPPET_CHARS)
        return f"({kind}: no textual payload)"
    if kind == "error":
        return _snippet(
            f"{output.get('ename')}: {output.get('evalue')}\n"
            + "\n".join(output.get("traceback") or [])[:OUTPUT_SNIPPET_CHARS],
            OUTPUT_SNIPPET_CHARS * 2,
        )
    return f"({kind})"


def _cell_entry(index: int, cell, *, with_outputs: bool = True) -> dict:
    entry: dict = {
        "index": index,
        "type": cell.get("cell_type"),
        "source": _snippet(_cell_source(cell), SOURCE_SNIPPET_CHARS),
    }
    if with_outputs and cell.get("cell_type") == "code":
        outputs = cell.get("outputs") or []
        entry["outputs"] = [_output_summary(o) for o in outputs][:5]
        entry["outputCount"] = len(outputs)
        ec = cell.get("execution_count")
        if ec is not None:
            entry["executionCount"] = ec
    return entry


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------


def tool_notebook_read(args: dict, ctx) -> ToolOutcome:
    path = _resolve_path(args.get("path"), ctx)
    if path is None:
        return error_outcome("notebook_read requires a non-empty string 'path'")
    notebook, failure = _load_notebook(path)
    if failure is not None:
        return failure
    cells = notebook.get("cells") or []
    return ToolOutcome(
        result={
            "path": str(path),
            "cellCount": len(cells),
            "kernel": (notebook.get("metadata") or {}).get("kernelspec", {}).get("name"),
            "cells": [_cell_entry(i, c) for i, c in enumerate(cells)],
        }
    )


def tool_notebook_write(args: dict, ctx) -> ToolOutcome:
    path = _resolve_path(args.get("path"), ctx)
    if path is None:
        return error_outcome("notebook_write requires a non-empty string 'path'")
    source = args.get("source")
    if not isinstance(source, str):
        return error_outcome("notebook_write requires a string 'source'")
    cell_index = args.get("cell_index")
    if isinstance(cell_index, bool) or not isinstance(cell_index, int):
        return error_outcome("notebook_write requires an integer 'cell_index'")
    notebook, failure = _load_notebook(path)
    if failure is not None:
        return failure
    cells = notebook.get("cells") or []
    if not 0 <= cell_index < len(cells):
        return error_outcome(
            f"cell_index {cell_index} out of range (notebook has {len(cells)} cells)"
        )
    cells[cell_index]["source"] = source
    # 改源码即作废旧执行痕迹（输出与 execution_count 不再对应当前源码）
    if cells[cell_index].get("cell_type") == "code":
        cells[cell_index]["outputs"] = []
        cells[cell_index]["execution_count"] = None
    try:
        nbformat.write(notebook, str(path))
    except OSError as exc:
        return ToolOutcome(
            result={"error": "write_failed", "detail": str(exc)}, is_error=True
        )
    return ToolOutcome(
        result={
            "ok": True,
            "path": str(path),
            "cellIndex": cell_index,
            "sourceChars": len(source),
        }
    )


# ---------------------------------------------------------------------------
# run（nbclient 可选组）
# ---------------------------------------------------------------------------


def tool_notebook_run(args: dict, ctx) -> ToolOutcome:
    path = _resolve_path(args.get("path"), ctx)
    if path is None:
        return error_outcome("notebook_run requires a non-empty string 'path'")
    cell_index = args.get("cell_index")
    if cell_index is not None and (isinstance(cell_index, bool) or not isinstance(cell_index, int)):
        return error_outcome("cell_index must be an integer when present")
    try:
        import nbclient  # noqa: F401 —— 可选组依赖
    except ImportError:
        return ToolOutcome(
            result={"error": "dependency_missing", "detail": NBCLIENT_GUIDE},
            is_error=True,
        )
    import nbclient as _nbclient

    notebook, failure = _load_notebook(path)
    if failure is not None:
        return failure
    cells = notebook.get("cells") or []
    if cell_index is not None and not 0 <= cell_index < len(cells):
        return error_outcome(
            f"cell_index {cell_index} out of range (notebook has {len(cells)} cells)"
        )
    started_notebook = notebook
    error_text: str | None = None
    try:
        client = _nbclient.NotebookClient(started_notebook, timeout=120)
        client.execute()
    except Exception as exc:  # noqa: BLE001 —— 执行错误（含 CellExecutionError）
        error_text = _summarize_exec_error(exc)
    # 执行后的 notebook 由 client 原地更新；落盘保留输出（审计与 UI 回看）
    try:
        nbformat.write(started_notebook, str(path))
    except OSError:
        pass
    focus = [cell_index] if cell_index is not None else range(len(cells))
    executed = [
        _cell_entry(i, cells[i]) for i in focus if cells[i].get("cell_type") == "code"
    ]
    payload: dict = {
        "path": str(path),
        "cells": executed,
        "ranWholeNotebook": True,
        "note": "kernel state is sequential; the whole notebook is executed, reporting focuses on the requested cells",
    }
    if error_text:
        payload["error"] = "execution_error"
        payload["detail"] = error_text
        return ToolOutcome(result=payload, is_error=True)
    return ToolOutcome(result=payload)


def _summarize_exec_error(exc: Exception) -> str:
    """CellExecutionError 的可读摘要（traceback 太长，取 ename/evalue/首处行号）。"""
    text = str(exc)
    return _snippet(text, 1200)


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------

NOTEBOOK_READ_SPEC = ToolSpec(
    name="notebook_read",
    description=(
        "Read a Jupyter notebook (.ipynb): lists every cell with index, type, "
        "source snippet (first 400 chars) and output summaries. Use before "
        "editing a cell."
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": ".ipynb path (absolute or cwd-relative)"}},
        "required": ["path"],
    },
    tier="readonly",
    handler=tool_notebook_read,
)

NOTEBOOK_WRITE_SPEC = ToolSpec(
    name="notebook_write",
    description=(
        "Replace the source of one notebook cell (notebook_write{path, "
        "cell_index, source}). Clears that cell's stale outputs; the file is "
        "written with nbformat."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "cell_index": {"type": "integer", "description": "0-based cell index"},
            "source": {"type": "string", "description": "new cell source"},
        },
        "required": ["path", "cell_index", "source"],
    },
    tier="write",
    handler=tool_notebook_write,
)

NOTEBOOK_RUN_SPEC = ToolSpec(
    name="notebook_run",
    description=(
        "Execute a notebook with nbclient and return output/error summaries "
        "(cell_index focuses the report; the whole notebook is executed because "
        "kernel state is sequential). Requires the optional [notebook] "
        "dependency group; the error result carries install guidance when "
        "nbclient is missing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "cell_index": {"type": "integer", "description": "report focus cell (optional)"},
        },
        "required": ["path"],
    },
    tier="write",
    handler=tool_notebook_run,
)

NOTEBOOK_TOOL_NAMES = ("notebook_read", "notebook_write", "notebook_run")


def register_notebook_tools(registry) -> bool:
    """注册三件套。nbformat 缺位（破损环境）不注册并返回 False。"""
    if nbformat is None:
        return False
    for spec in (NOTEBOOK_READ_SPEC, NOTEBOOK_WRITE_SPEC, NOTEBOOK_RUN_SPEC):
        registry.register(spec)
    return True


__all__ = [
    "NBCLIENT_GUIDE",
    "NOTEBOOK_READ_SPEC",
    "NOTEBOOK_RUN_SPEC",
    "NOTEBOOK_TOOL_NAMES",
    "NOTEBOOK_WRITE_SPEC",
    "register_notebook_tools",
]
