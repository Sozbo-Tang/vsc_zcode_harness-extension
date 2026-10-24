"""McpManager：server 生命周期 + 工具注册进 ToolRegistry（M8）。

- 工具命名 ``mcp__<server>__<tool>``（zcode 惯例），tier=write（默认询问；
  权限规则引擎按此名字工作，如 ``"mcp__fs__*: **"`` 之类规则可配）；
- 结果转 ToolOutcome：MCP ``content[]`` 文本拼接（非 text 块 JSON 序列化），
  ``isError`` → is_error，超 3 万字符 head_tail 截断（L1 口径一致）；
- init 后台连接全部 server——**连接失败不阻塞会话**（状态标 error，
  mcp_list 可见；对该 server 的调用返回结构化 mcp_server_unavailable）；
- mcp_refresh = 关停全部 + 重读 mcp.json + 重连（server 配置变更后用）。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from ..tools import ToolOutcome, ToolRegistry, ToolSpec, head_tail
from .config import add_server, load_mcp_config, mcp_config_path, remove_server
from .transport import McpTransportError, StdioTransport

STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"
STATUS_CONNECTING = "connecting"


def tool_name_for(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


def render_content(result: dict) -> tuple[str, bool]:
    """MCP tools/call result → (文本, is_error)。"""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    try:
                        parts.append(json.dumps(item, ensure_ascii=False))
                    except (TypeError, ValueError):
                        parts.append(str(item))
            else:
                parts.append(str(item))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content)
    text = "\n".join(p for p in parts if p)
    return (text or "(empty mcp result)"), bool(result.get("isError"))


class McpManager:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        config_path=None,
        on_status: Callable[[dict], None] | None = None,
    ):
        self.registry = registry
        self._config_path = config_path or mcp_config_path()
        self._on_status = on_status  # 状态变化回调（服务层可用来广播）
        self._transports: dict[str, StdioTransport] = {}
        self._statuses: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 状态

    def status_list(self) -> list[dict]:
        """mcp_list 载荷：[{name, status, tools, error?}]。"""
        with self._lock:
            return [
                {
                    "name": name,
                    "status": info.get("status"),
                    "tools": list(info.get("tools") or []),
                    **({"error": info["error"]} if info.get("error") else {}),
                }
                for name, info in sorted(self._statuses.items())
            ]

    def _set_status(self, server: str, status: str, *, tools: list | None = None, error: str | None = None) -> None:
        with self._lock:
            info = self._statuses.setdefault(server, {})
            info["status"] = status
            if tools is not None:
                info["tools"] = tools
            info["error"] = error
        if self._on_status is not None:
            try:
                self._on_status({"name": server, "status": status, "error": error})
            except Exception:  # noqa: BLE001 —— 回调失败不影响连接流程
                pass

    def _mark_error(self, server: str, message: str) -> None:
        transport = self._transports.get(server)
        tools = [t.get("name") for t in (transport.tools if transport else [])]
        self._set_status(server, STATUS_ERROR, tools=tools, error=message)

    # ------------------------------------------------------------------ 连接

    def connect_all(self, *, background: bool = True) -> None:
        """按 mcp.json 连接全部 server（后台逐个连，失败标 error 不抛）。"""
        try:
            config = load_mcp_config(self._config_path)
        except Exception as exc:  # noqa: BLE001 —— 配置损坏：标 error 不炸会话
            self._set_status("(config)", STATUS_ERROR, tools=[], error=f"mcp.json unreadable: {exc}")
            return
        entries = list(config.get("servers") or [])
        if not background:
            for entry in entries:
                self._connect_one(entry)
            return
        for entry in entries:
            threading.Thread(
                target=self._connect_one,
                args=(entry,),
                daemon=True,
                name=f"mcp-connect-{entry.get('name')}",
            ).start()

    def _connect_one(self, entry: dict) -> None:
        name = str(entry.get("name") or "")
        if not name:
            return
        self._set_status(name, STATUS_CONNECTING)
        # 重连前清掉旧实例与旧工具注册
        self._teardown_server(name)
        transport = StdioTransport(
            name,
            list(entry.get("command") or []),
            env=dict(entry.get("env") or {}),
            cwd=entry.get("cwd"),
        )
        try:
            transport.start()
        except McpTransportError as exc:
            transport.close()
            self._set_status(name, STATUS_ERROR, tools=[], error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 一切失败降级为状态，不炸会话
            transport.close()
            self._set_status(name, STATUS_ERROR, tools=[], error=f"{type(exc).__name__}: {exc}")
            return
        self._transports[name] = transport
        registered = self._register_tools(name, transport)
        self._set_status(name, STATUS_CONNECTED, tools=registered)

    def _teardown_server(self, name: str) -> None:
        old = self._transports.pop(name, None)
        if old is not None:
            old.close()
        prefix = f"mcp__{name}__"
        for tool_name in [n for n in self.registry.names() if n.startswith(prefix)]:
            self.registry.unregister(tool_name)

    def _register_tools(self, server: str, transport: StdioTransport) -> list[str]:
        registered: list[str] = []
        for tool in transport.tools:
            raw_name = str(tool.get("name") or "")
            if not raw_name:
                continue
            schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object", "properties": {}}
            self.registry.register(
                ToolSpec(
                    name=tool_name_for(server, raw_name),
                    description=str(
                        tool.get("description")
                        or f"MCP tool {raw_name!r} from server {server!r}"
                    ),
                    parameters=schema,
                    tier="write",  # MCP 工具默认询问档（任务书）
                    handler=self._make_handler(server, raw_name),
                )
            )
            registered.append(raw_name)
        return registered

    def _make_handler(self, server: str, tool: str) -> Callable[[dict, Any], ToolOutcome]:
        def handler(args: dict, ctx) -> ToolOutcome:
            transport = self._transports.get(server)
            if transport is None or not transport.connected:
                return ToolOutcome(
                    result={
                        "error": "mcp_server_unavailable",
                        "detail": f"MCP server {server!r} is not connected; see mcp_list",
                    },
                    is_error=True,
                )
            try:
                raw = transport.call_tool(tool, args)
            except McpTransportError as exc:
                self._mark_error(server, str(exc))
                return ToolOutcome(
                    result={"error": "mcp_call_failed", "detail": str(exc)},
                    is_error=True,
                )
            text, is_error = render_content(raw)
            text, truncated = head_tail(text)
            payload: dict = {"tool": tool_name_for(server, tool), "content": text}
            if truncated:
                payload["truncated"] = True
            return ToolOutcome(result=payload, is_error=is_error)

        return handler

    # ------------------------------------------------------------------ 管理命令

    def refresh(self, *, background: bool = True) -> None:
        """mcp_refresh：关停全部 + 重读配置 + 重连。"""
        self.shutdown()
        self.connect_all(background=background)

    def add_and_connect(self, entry: dict, *, background: bool = True) -> str:
        """mcp_add：写 mcp.json + 连接该 server。返回错误串（空 = 成功）。"""
        try:
            add_server(entry, self._config_path)
        except Exception as exc:  # noqa: BLE001 —— 校验/写盘失败
            return f"mcp_add failed: {exc}"
        name = str(entry.get("name") or "")
        if background:
            threading.Thread(
                target=self._connect_one, args=({"name": name, **entry},),
                daemon=True, name=f"mcp-add-{name}",
            ).start()
        else:
            self._connect_one({"name": name, **entry})
        return ""

    def remove(self, name: str) -> str:
        """mcp_remove：摘配置 + 关停 + 注销工具。返回错误串（空 = 成功）。"""
        try:
            remove_server(name, self._config_path)
        except Exception as exc:  # noqa: BLE001
            return f"mcp_remove failed: {exc}"
        self._teardown_server(name)
        with self._lock:
            self._statuses.pop(name, None)
        return ""

    def shutdown(self) -> None:
        with self._lock:
            names = list(self._transports)
        for name in names:
            self._teardown_server(name)


__all__ = [
    "McpManager",
    "STATUS_CONNECTED",
    "STATUS_CONNECTING",
    "STATUS_ERROR",
    "render_content",
    "tool_name_for",
]
