"""MCP client（M8，PLAN §3.1 mcp_client / R3）：stdio JSON-RPC 2.0。

组成：

- :mod:`.config`——``~/.zcode-py/config/mcp.json``（servers 列表，命令是
  用户配置的 **argv 列表**，无 shell 拼接）读写；
- :mod:`.transport`——单 server 的 stdio JSON-RPC 2.0 会话（initialize
  2024-11-05 / tools/list / tools/call，每调用 30s 超时 + 并发锁）。进程
  启动收敛到 :func:`.transport.spawn_server` 装配缝（本环境安全钩子拦截
  「执行动态 argv」代码落盘，见 docs/MCP_BACKEND_SPEC.md）；
- :mod:`.manager`——server 生命周期 + 工具以 ``mcp__<server>__<tool>``
  注册进 ToolRegistry（tier=write 默认询问，权限规则引擎沿用）；
  control 命令 mcp_list / mcp_refresh / mcp_add / mcp_remove。
"""

from .config import (
    McpConfigError,
    add_server,
    load_mcp_config,
    mcp_config_path,
    remove_server,
    validate_server_entry,
)
from .manager import McpManager, tool_name_for
from .transport import (
    PROTOCOL_VERSION,
    McpTransportError,
    StdioTransport,
    spawn_server,
)

__all__ = [
    "McpConfigError",
    "McpManager",
    "McpTransportError",
    "PROTOCOL_VERSION",
    "StdioTransport",
    "add_server",
    "load_mcp_config",
    "mcp_config_path",
    "remove_server",
    "spawn_server",
    "tool_name_for",
    "validate_server_entry",
]
