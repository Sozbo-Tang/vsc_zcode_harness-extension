"""M6 浏览器自动化（R8，PLAN §2.7 / §7）：browser-use 库作为 Agent 工具。

可选依赖组设计（任务书原文「未安装不注册」）：

- ``browser-use`` 属 pyproject 的 ``[browser]`` 依赖组，**不随核心安装**；
- 模块级 try-import：导入失败时 ``_BROWSER_USE=False``，
  :func:`register_browser_tools` 直接返回 ``False``、不注册任何工具
  （模型看不到 schema）；
- 服务层（main.py）在未注册时用 :class:`ToolSession` 子类拦截幻觉调用的
  ``browser_*`` 工具名，返回 :func:`browser_unavailable_outcome` 的结构化
  安装指引（uv 命令字面量，无 shell 拼接）。

安全边界（PLAN §9.1 SSRF，与 webfetch 同源但有一处**如实记录的限制**）：

- ``browser_navigate`` 的 url 在启动浏览器**前**过 ``netguard.validate_url``
  ——仅 http/https、拒绝 localhost/环回/私有/保留地址（含 IP 变体归一）；
- webfetch 对重定向是逐跳重新校验的；browser-use 库自身驱动页面跳转与
  点击，**无法逐 hop 校验**其后续导航目标——前置校验只保证初始 URL 非内
  网，页面内 302/JS 跳转不受控。此为本工具的已知限制（与 netguard 的
  DNS rebinding TOCTOU 同一处理方式：记录在案，不假装解决）。缓解：给库
  Agent 的任务文本明确「不要导航到其它源」。

权限档：browser_navigate / browser_screenshot 均不在 permissions 的
READONLY_TOOLS 集合内 → 权限引擎默认 ``ask``（write 档、弹卡确认）。

测试策略：库绝不真实安装/运行——安装路径全部打桩（monkeypatch 模块内
``_create_session`` / ``_create_agent`` / ``_run_agent`` / ``_capture_screenshot``
四个接缝）。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Callable

from ..config import home_dir
from ..netguard import NetguardError, validate_url
from ..protocol import ImageArtifact
from . import MAX_TOOL_OUTPUT_CHARS, ToolOutcome, ToolRegistry, ToolSpec, error_outcome, head_tail

# ---------------------------------------------------------------------------
# 可选依赖 try-import（未装 → 整个模块降级为「不注册」）
# ---------------------------------------------------------------------------

try:  # pragma: no cover - 取决于环境是否装了可选组
    from browser_use import Agent as BrowserAgent  # type: ignore[assignment]

    try:  # browser-use 1.x：BrowserSession + BrowserProfile；0.x 只有 Browser
        from browser_use import BrowserProfile, BrowserSession  # type: ignore[assignment]
    except ImportError:  # pragma: no cover
        BrowserProfile = None  # type: ignore[assignment]
        BrowserSession = None  # type: ignore[assignment]
    _BROWSER_USE = True
    _IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001 —— ImportError 之外的结构性差异也按未安装处理
    BrowserAgent = None  # type: ignore[assignment]
    BrowserProfile = None  # type: ignore[assignment]
    BrowserSession = None  # type: ignore[assignment]
    _BROWSER_USE = False
    _IMPORT_ERROR = _exc

BROWSER_TOOL_NAMES = ("browser_navigate", "browser_screenshot")

# 操作时间线上限（任务书：≤30 条动作序列）
TIMELINE_LIMIT = 30
DEFAULT_TASK = "Summarize the main content of the page in a few bullet points."

INSTALL_COMMAND = "uv sync --project agent --group browser"
INSTALL_HINT = (
    "browser-use is not installed (optional [browser] dependency group, PLAN "
    "M6/R8). Install it with: " + INSTALL_COMMAND + " — then restart the "
    "session and the browser tools will be registered. One-off usage: "
    "uv run --project agent --group browser <command>."
)


def browser_unavailable_outcome(name: str) -> ToolOutcome:
    """库未安装时对「想用」的结构化指引（服务层拦截幻觉调用时返回）。"""
    return ToolOutcome(
        result={
            "error": "browser_use_not_installed",
            "tool": name,
            "install": INSTALL_COMMAND,
            "detail": INSTALL_HINT,
        },
        is_error=True,
    )


def register_browser_tools(registry: ToolRegistry) -> bool:
    """browser-use 可用才注册两个工具；未安装返回 False（不注册）。"""
    if not _BROWSER_USE:
        return False
    registry.register(NAVIGATE_SPEC)
    registry.register(SCREENSHOT_SPEC)
    return True


# ---------------------------------------------------------------------------
# 基础设施：目录 / 会话 / 截图（测试接缝：模块级函数，可 monkeypatch）
# ---------------------------------------------------------------------------


def browser_dir() -> Path:
    """截图落盘目录：<zcode-py home>/browser（config.home_dir，默认 ~/.zcode-py）。"""
    return home_dir() / "browser"


def _create_session(headless: bool) -> Any:
    """创建/复用前的浏览器会话工厂（测试 monkeypatch 接缝）。

    browser-use 1.x：``BrowserSession(BrowserProfile(headless=...))``；
    0.x：``Browser(headless=...)``。有头/无头由 init.settings browser.headless
    决定（默认 headless）。
    """
    if BrowserSession is not None and BrowserProfile is not None:
        return BrowserSession(BrowserProfile(headless=headless))
    from browser_use import Browser as _Browser  # type: ignore[no-redef]

    return _Browser(headless=headless)


def _ensure_session(ctx) -> Any:
    """会话级懒初始化（存在 ctx.state，跨工具调用复用同一浏览器）。"""
    session = ctx.state.get("browserSession")
    if session is None:
        headless = bool(ctx.state.get("browserHeadless", True))
        session = _create_session(headless)
        ctx.state["browserSession"] = session
    return session


def _create_agent(task: str, session: Any, llm: Any = None) -> Any:
    """browser-use Agent 工厂（测试 monkeypatch 接缝）。

    ``llm`` 来自 ctx.state["browserLLM"]（服务层可注入厂商适配器）；缺省
    交给 browser-use 自己的默认 LLM 配置。
    """
    kwargs: dict = {"task": task}
    if llm is not None:
        kwargs["llm"] = llm
    # 1.x 用 session=，0.x 用 browser=
    kwargs["browser" if hasattr(session, "browser") else "session"] = session
    return BrowserAgent(**kwargs)


def _run_agent(agent: Any) -> Any:
    """跑一个 browser-use Agent（异步库 → 线程内事件循环；测试接缝）。"""
    import asyncio

    return asyncio.run(agent.run())


def _agent_final_text(agent: Any) -> str:
    """提取 Agent 最终文本（final_result() / result()，容错多版本）。"""
    for getter in ("final_result", "result"):
        fn = getattr(agent, getter, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:  # noqa: BLE001 —— 版本差异一律跳过试下一个
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _history_items(agent: Any) -> list:
    """从 agent.state.history 提取动作序列原始项（容错多版本，best-effort）。"""
    state = getattr(agent, "state", None)
    history = getattr(state, "history", None) if state is not None else None
    if history is None:
        return []
    for getter in ("model_actions", "action_history"):
        fn = getattr(history, getter, None)
        if callable(fn):
            try:
                raw = fn()
            except Exception:  # noqa: BLE001
                continue
            if raw:
                return list(raw)
    if isinstance(history, (list, tuple)):
        return list(history)
    return []


def _format_action(item: Any) -> str:
    line: str
    if isinstance(item, dict):
        action = item.get("action") or item.get("name")
        if action is None and item:
            action = next(iter(item))
        detail = item.get("detail") or item.get("input") or ""
        line = f"{action}: {detail}" if str(detail).strip() else str(action)
    elif isinstance(item, (list, tuple)):
        line = " ".join(str(p) for p in item if p is not None)
    else:
        line = str(item)
    return line[:200]


def _extract_timeline(agent: Any, limit: int = TIMELINE_LIMIT) -> tuple[list[str], bool]:
    """动作序列 → (≤limit 行文本, 是否截断)。截断时末行注明省略量。"""
    items = _history_items(agent)
    lines = [_format_action(i) for i in items]
    if len(lines) <= limit:
        return lines, False
    omitted = len(lines) - (limit - 1)
    return lines[: limit - 1] + [f"... [{omitted} more actions omitted (timeline truncated)]"], True


def _capture_screenshot(session: Any) -> bytes | None:
    """当前页面截图原始字节（测试 monkeypatch 接缝；失败返回 None）。"""
    fn = getattr(session, "take_screenshot", None)
    if not callable(fn):
        return None
    try:
        shot = fn()
    except Exception:  # noqa: BLE001 —— 截图失败不拖垮整个导航结果
        return None
    if isinstance(shot, (bytes, bytearray)):
        return bytes(shot)
    if isinstance(shot, str):  # 某些版本返回临时文件路径
        try:
            return Path(shot).read_bytes()
        except OSError:
            return None
    data = getattr(shot, "value", None)  # 1.x ScreenshotResult.value（base64 str）
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        try:
            return base64.b64decode(data)
        except Exception:  # noqa: BLE001
            return None
    return None


def _save_screenshot(data: bytes) -> Path:
    directory = browser_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{int(time.time() * 1000)}.png"
    path.write_bytes(data)
    return path


def _emit_artifact(ctx, path: Path) -> None:
    """广播 image_artifact{path} 帧（协议 §5 已有；emit 由服务层注入 state）。"""
    emit: Callable[..., None] | None = ctx.state.get("emit")
    if callable(emit):
        emit(ImageArtifact(path=str(path)))


# ---------------------------------------------------------------------------
# 工具 handler
# ---------------------------------------------------------------------------


def tool_browser_navigate(args: dict, ctx) -> ToolOutcome:
    if not _BROWSER_USE:
        return browser_unavailable_outcome("browser_navigate")
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return error_outcome("browser_navigate requires a string 'url'")
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        task = DEFAULT_TASK
    # SSRF 前置校验（限制见模块 docstring：库自身跳转无法逐 hop 校验）
    try:
        url = validate_url(url)
    except NetguardError as exc:
        return ToolOutcome(
            result={"error": "blocked", "detail": f"host check failed: {exc.reason}"},
            is_error=True,
        )
    session = _ensure_session(ctx)
    full_task = (
        f"Open {url} and complete the following task: {task} "
        "Do not navigate to a different origin."
    )
    agent = _create_agent(full_task, session, ctx.state.get("browserLLM"))
    try:
        _run_agent(agent)
    except Exception as exc:  # noqa: BLE001 —— 库异常结构化回灌，不炸回合
        return ToolOutcome(
            result={"error": "browser_failed", "detail": f"{type(exc).__name__}: {exc}"},
            is_error=True,
        )
    final = _agent_final_text(agent)
    final, text_truncated = head_tail(final, MAX_TOOL_OUTPUT_CHARS)
    timeline, timeline_truncated = _extract_timeline(agent)
    result: dict = {"url": url, "task": task, "result": final, "timeline": timeline}
    if text_truncated:
        result["resultTruncated"] = True
    if timeline_truncated:
        result["timelineTruncated"] = True
    shot = _capture_screenshot(session)
    if shot:
        path = _save_screenshot(shot)
        _emit_artifact(ctx, path)
        result["screenshot"] = str(path)
    return ToolOutcome(result=result)


def tool_browser_screenshot(args: dict, ctx) -> ToolOutcome:
    if not _BROWSER_USE:
        return browser_unavailable_outcome("browser_screenshot")
    session = ctx.state.get("browserSession")
    if session is None:
        return error_outcome(
            "no active browser session; call browser_navigate first",
            kind="no_session",
        )
    shot = _capture_screenshot(session)
    if not shot:
        return ToolOutcome(
            result={"error": "screenshot_failed", "detail": "take_screenshot returned no data"},
            is_error=True,
        )
    path = _save_screenshot(shot)
    _emit_artifact(ctx, path)
    return ToolOutcome(result={"screenshot": str(path), "bytes": len(shot)})


NAVIGATE_SPEC = ToolSpec(
    name="browser_navigate",
    description=(
        "Drive a real browser (browser-use) to open a URL and complete a task "
        "(default: summarize the page). Returns the final answer, an action "
        "timeline (<=30 steps) and a screenshot artifact path. Internal/private "
        "hosts are rejected before launch (SSRF guard)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https URL to open"},
            "task": {
                "type": "string",
                "description": "what to do on the page (default: summarize it)",
            },
        },
        "required": ["url"],
    },
    tier="write",  # 权限档：默认询问（真实浏览器出网 + 本机自动化）
    handler=tool_browser_navigate,
)

SCREENSHOT_SPEC = ToolSpec(
    name="browser_screenshot",
    description=(
        "Capture a screenshot of the current browser page (from the last "
        "browser_navigate) and broadcast it as an image artifact."
    ),
    parameters={"type": "object", "properties": {}},
    tier="write",
    handler=tool_browser_screenshot,
)

__all__ = [
    "BROWSER_TOOL_NAMES",
    "BrowserAgent",
    "DEFAULT_TASK",
    "INSTALL_COMMAND",
    "INSTALL_HINT",
    "NAVIGATE_SPEC",
    "SCREENSHOT_SPEC",
    "TIMELINE_LIMIT",
    "browser_dir",
    "browser_unavailable_outcome",
    "register_browser_tools",
    "tool_browser_navigate",
    "tool_browser_screenshot",
]
