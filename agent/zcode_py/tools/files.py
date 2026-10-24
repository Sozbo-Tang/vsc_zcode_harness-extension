"""只读文件工具：read / glob / grep（M2 任务书「只读四件套」的文件侧）。

- read：行数上限默认 2000（L1「Read 默认行数上限」，PLAN §3.4），内容再过
  30k 头尾截断；隐私护栏（privacy.py）命中时结果携带 warning。
- glob：自实现剪枝遍历（跳过 .git/node_modules/.venv 等目录），上限 500 条。
- grep：正则逐行扫描（无 rg 依赖），跳过二进制（NUL 嗅探）与 >5MB 大文件，
  匹配上限默认 100 条；命中行所在文件过隐私护栏，警告随结果携带（R15，
  与 read 同语义——grep 同样会把文件内容送进模型上下文）。
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

from ..privacy import privacy_warning_for_path
from . import ToolOutcome, ToolSpec, error_outcome, head_tail

READ_DEFAULT_LIMIT = 2000
GLOB_MAX_RESULTS = 500
GREP_MAX_RESULTS = 100
GREP_MAX_FILE_BYTES = 5 * 1024 * 1024

SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
     ".mypy_cache", ".pytest_cache", "dist", "build", ".next", "target"}
)


def _resolve(ctx_path: str, ctx_cwd: Path) -> Path:
    p = Path(ctx_path).expanduser()
    return p if p.is_absolute() else (ctx_cwd / p)


def _iter_files(root: Path, pattern: str | None):
    """自实现剪枝遍历：目录名命中 SKIP_DIRS 直接不下钻。"""
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if pattern is not None and not fnmatch(filename, pattern):
                continue
            yield Path(dirpath) / filename


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def tool_read(args: dict, ctx) -> ToolOutcome:
    path_raw = args.get("path")
    if not isinstance(path_raw, str) or not path_raw:
        return error_outcome("read requires a string 'path'")
    offset = _as_int(args.get("offset"), default=1, minimum=1)
    limit = _as_int(args.get("limit"), default=READ_DEFAULT_LIMIT, minimum=1)
    path = _resolve(path_raw, ctx.cwd)
    if path.is_dir():
        return error_outcome(f"{path} is a directory (read files, not dirs)")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ToolOutcome(result={"error": "not_found", "detail": f"no such file: {path}"},
                           is_error=True)
    except OSError as exc:
        return ToolOutcome(result={"error": "io_error", "detail": f"{type(exc).__name__}: {exc}"},
                           is_error=True)
    lines = raw.splitlines()
    total = len(lines)
    start = min(offset, total + 1)
    selected = lines[start - 1 : start - 1 + limit]
    truncated = (start - 1 + limit) < total or start > 1
    content = "\n".join(selected)
    content, char_truncated = head_tail(content)
    rel = _display(path, ctx.cwd)
    result = {
        "path": rel,
        "content": content,
        "totalLines": total,
        "startLine": start,
        "truncated": bool(truncated or char_truncated),
    }
    if truncated:
        result["note"] = f"showing lines {start}-{start - 1 + len(selected)} of {total}; pass offset/limit to read more"
    elif char_truncated:
        result["note"] = f"content truncated to head+tail (L1)"
    return ToolOutcome(result=result, warning=privacy_warning_for_path(path_raw))


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


def tool_glob(args: dict, ctx) -> ToolOutcome:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return error_outcome("glob requires a string 'pattern'")
    root = _resolve(args.get("path") or ".", ctx.cwd) if isinstance(args.get("path"), str) else ctx.cwd
    if not root.exists():
        return ToolOutcome(result={"error": "not_found", "detail": f"no such directory: {root}"},
                           is_error=True)
    matches: list[str] = []
    truncated = False
    for file in _iter_files(root, None):
        rel = _display(file, ctx.cwd)
        if fnmatch(rel, pattern) or fnmatch(file.name, pattern):
            matches.append(rel)
            if len(matches) >= GLOB_MAX_RESULTS:
                truncated = True
                break
    matches.sort()
    result = {"matches": matches, "count": len(matches)}
    if truncated:
        result["truncated"] = True
        result["note"] = f"capped at {GLOB_MAX_RESULTS} matches"
    return ToolOutcome(result=result)


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def tool_grep(args: dict, ctx) -> ToolOutcome:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return error_outcome("grep requires a string 'pattern'")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return error_outcome(f"invalid regex {pattern!r}: {exc}")
    max_results = _as_int(args.get("max_results"), default=GREP_MAX_RESULTS, minimum=1)
    root = _resolve(args.get("path") or ".", ctx.cwd) if isinstance(args.get("path"), str) else ctx.cwd
    if not root.exists():
        return ToolOutcome(result={"error": "not_found", "detail": f"no such path: {root}"},
                           is_error=True)
    name_filter = args.get("glob") if isinstance(args.get("glob"), str) else None
    matches: list[dict] = []
    privacy_warnings: list[str] = []
    warned_files: set[str] = set()
    truncated = False
    files_scanned = 0
    for file in _iter_files(root, name_filter):
        try:
            if file.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
            blob = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in blob[:8192]:
            continue  # 二进制
        files_scanned += 1
        text = blob.decode("utf-8", errors="replace")
        rel: str | None = None
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                if rel is None:
                    rel = _display(file, ctx.cwd)
                    # R15：grep 同样把文件内容送进模型上下文——敏感文件的
                    # 命中行必须携带隐私警告，不能只有 read 有护栏
                    if rel not in warned_files:
                        warned_files.add(rel)
                        warn = privacy_warning_for_path(rel)
                        if warn:
                            privacy_warnings.append(warn)
                matches.append(
                    {"file": rel, "line": lineno, "text": line.strip()[:500]}
                )
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break
    result = {"matches": matches, "count": len(matches), "filesScanned": files_scanned}
    if truncated:
        result["truncated"] = True
        result["note"] = f"capped at {max_results} matches"
    warning: str | None = None
    if privacy_warnings:
        warning = "; ".join(privacy_warnings[:3])
        if len(privacy_warnings) > 3:
            warning += f" (+{len(privacy_warnings) - 3} more sensitive files)"
    return ToolOutcome(result=result, warning=warning)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _as_int(value, *, default: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(int(value), minimum)


def _display(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


READ_SPEC = ToolSpec(
    name="read",
    description=(
        "Read a UTF-8 text file. Returns numbered-slice content with "
        f"line window [offset, offset+limit) (default limit {READ_DEFAULT_LIMIT}); "
        "content is head+tail truncated at 30k chars (L1). Use offset/limit to page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file path (relative to workspace or absolute)"},
            "offset": {"type": "integer", "description": "1-based first line to read"},
            "limit": {"type": "integer", "description": f"max lines to return (default {READ_DEFAULT_LIMIT})"},
        },
        "required": ["path"],
    },
    tier="readonly",
    handler=tool_read,
)

GLOB_SPEC = ToolSpec(
    name="glob",
    description=(
        "List files matching a glob pattern (e.g. '**/*.py', 'src/*.ts'). "
        f"Skips {', '.join(sorted(SKIP_DIRS))}; caps at {GLOB_MAX_RESULTS} matches."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob pattern matched against file names/relative paths"},
            "path": {"type": "string", "description": "root directory (default workspace root)"},
        },
        "required": ["pattern"],
    },
    tier="readonly",
    handler=tool_glob,
)

GREP_SPEC = ToolSpec(
    name="grep",
    description=(
        "Search file contents with a regular expression; returns file/line/text "
        f"matches (default cap {GREP_MAX_RESULTS}). Skips binary and >5MB files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regex"},
            "path": {"type": "string", "description": "file or directory to search (default workspace root)"},
            "glob": {"type": "string", "description": "filename filter, e.g. '*.py'"},
            "max_results": {"type": "integer"},
        },
        "required": ["pattern"],
    },
    tier="readonly",
    handler=tool_grep,
)
