"""MCP server 配置：~/.zcode-py/config/mcp.json（M8 任务书）。

结构（command 是用户配置的 **argv 列表**，启动时原样传给装配缝，绝无
shell 拼接）::

    {
      "servers": [
        {"name": "fs", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/w"], "env": {"K": "V"}, "cwd": "/w"}
      ]
    }

env/cwd 可选。读写都经过 :func:`validate_server_entry` 校验；文件不存在
返回空配置（首次 ``mcp_add`` 时落盘）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import home_dir


class McpConfigError(ValueError):
    """server 条目非法（名称/命令缺失、类型不对）。"""


def mcp_config_path() -> Path:
    return home_dir() / "config" / "mcp.json"


def validate_server_entry(entry: object) -> dict:
    """校验并归一一个 server 条目 → {name, command, env, cwd?}。"""
    if not isinstance(entry, dict):
        raise McpConfigError("server entry must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise McpConfigError("server entry requires a non-empty string 'name'")
    command = entry.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg.strip() for arg in command)
    ):
        raise McpConfigError(
            f"server {name!r} 'command' must be a non-empty argv list of strings"
        )
    env = entry.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise McpConfigError(f"server {name!r} 'env' must be an object of string→string")
    cwd = entry.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise McpConfigError(f"server {name!r} 'cwd' must be a non-empty string when present")
    normalized: dict = {"name": name.strip(), "command": list(command), "env": dict(env)}
    if cwd:
        normalized["cwd"] = cwd
    return normalized


def load_mcp_config(path: Path | None = None) -> dict:
    """读配置；文件不存在/损坏 → 空 servers（损坏时抛 McpConfigError 由调用方广播）。"""
    target = path or mcp_config_path()
    if not target.exists():
        return {"servers": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpConfigError(f"cannot read {target}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        raise McpConfigError(f"{target} must be an object with a 'servers' array")
    servers = [validate_server_entry(s) for s in data["servers"]]
    return {"servers": servers}


def save_mcp_config(config: dict, path: Path | None = None) -> Path:
    target = path or mcp_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def add_server(entry: dict, path: Path | None = None) -> dict:
    """加入（重名覆盖）一个 server 并落盘，返回新配置。"""
    normalized = validate_server_entry(entry)
    config = load_mcp_config(path)
    servers = [s for s in config["servers"] if s["name"] != normalized["name"]]
    servers.append(normalized)
    config["servers"] = servers
    save_mcp_config(config, path)
    return config


def remove_server(name: str, path: Path | None = None) -> dict:
    """按名移除一个 server 并落盘（不存在时无操作），返回新配置。"""
    config = load_mcp_config(path)
    config["servers"] = [s for s in config["servers"] if s["name"] != name]
    save_mcp_config(config, path)
    return config


__all__ = [
    "McpConfigError",
    "add_server",
    "load_mcp_config",
    "mcp_config_path",
    "remove_server",
    "save_mcp_config",
    "validate_server_entry",
]
