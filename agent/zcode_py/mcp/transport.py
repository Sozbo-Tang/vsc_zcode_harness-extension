"""MCP stdio transport：单 server 的 JSON-RPC 2.0 会话（M8 任务书）。

**装配缝（关键约束）**：本环境通用安全钩子会拦截「执行动态命令串/argv」
的代码落盘（同 tools/bash_backend 先例，docs/BASH_BACKEND_SPEC.md）。因此
进程启动**收敛到唯一的** :func:`spawn_server`，其真正执行点在
``zcode_py.mcp.proc_backend``（装配缝模块，importorskip 模式）：

- 存在 → ``proc_backend.spawn_process(argv, env=..., cwd=...)`` 返回
  Popen 风格对象（stdin/stdout 文本流、poll/terminate/wait）；
- 不存在 → :class:`McpTransportError`（结构化指引 docs/MCP_BACKEND_SPEC.md）。

绝不尝试 heredoc/Bash 写文件绕钩子；测试一律 monkeypatch ``spawn_server``
返回假进程对象（真实子进程零依赖）。

协议（MCP stdio，newline-delimited JSON-RPC 2.0）：

- ``initialize``（protocolVersion 2024-11-05，capabilities/clientInfo）→
  响应后发 ``notifications/initialized`` 通知；
- ``tools/list`` → ``result.tools[] = {name, description?, inputSchema?}``；
- ``tools/call`` ``{name, arguments}`` → ``result = {content[], isError?}``。

每 server 一把锁（并发安全）；每次请求超时 30s（initialize 60s——冷启动
npm 包解压可能很慢）。服务端推送（notifications/requests）在等待响应时
跳过不阻塞。
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
REQUEST_TIMEOUT_S = 30.0
INITIALIZE_TIMEOUT_S = 60.0
CLIENT_INFO = {"name": "zcode-py", "version": "0.1.0"}

BACKEND_MISSING_MESSAGE = (
    "MCP 进程装配缝未落盘：本环境安全钩子拦截了「执行动态 argv」的执行核心"
    "落盘。请按 docs/MCP_BACKEND_SPEC.md 落盘 agent/zcode_py/mcp/proc_backend.py"
    "（用户授权的一次性策略例外），MCP server 即可用。"
)


class McpTransportError(RuntimeError):
    """server 进程启动/握手/调用失败（含装配缝缺位）。"""


def spawn_server(argv: list[str], env: dict[str, str] | None, cwd: str | None):
    """装配缝收敛点：拉起 MCP server 子进程（argv 列表，无 shell）。

    测试经 monkeypatch 本函数注入假进程；生产路径 try-import
    ``zcode_py.mcp.proc_backend``（存在则用，不存在则结构化报错）。
    """
    try:
        from . import proc_backend  # 装配缝模块（可能未落盘）
    except ImportError as exc:
        raise McpTransportError(BACKEND_MISSING_MESSAGE) from exc
    return proc_backend.spawn_process(argv, env=env, cwd=cwd)


class StdioTransport:
    """一个 MCP server 的 stdio JSON-RPC 会话。

    生命周期：``start()``（spawn → initialize → initialized 通知 →
    tools/list）→ 若干 ``call_tool()`` → ``close()``。任何一步失败进入
    error 态（``last_error``），后续调用直接抛 McpTransportError。
    """

    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        initialize_timeout_s: float = INITIALIZE_TIMEOUT_S,
    ):
        self.name = name
        self.argv = list(argv)
        self.env = dict(env or {})
        self.cwd = cwd
        self.request_timeout_s = request_timeout_s
        self.initialize_timeout_s = initialize_timeout_s
        self.tools: list[dict] = []
        self.last_error: str | None = None
        self._proc: Any = None
        self._reader_thread: threading.Thread | None = None
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()  # 每 server 串行化请求（并发安全）
        self._ids = itertools.count(1)
        self._closed = False

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "StdioTransport":
        """spawn + 握手 + tools/list；失败抛 McpTransportError（并 close）。"""
        # 独立评审中危 #4：MCP server 是用户配置的本地命令——env 以脱敏
        # 基线（剔除 API Key/SECRET/TOKEN 等）起步，再叠加显式配置项，
        # 不把含 Provider Key 的完整 os.environ 交给任意本地进程
        # （与 tools/bash、background 的 sanitize_child_env 同一口径）。
        from ..tools.bash import sanitize_child_env

        merged_env = {**sanitize_child_env(), **(self.env or {})}
        try:
            self._proc = spawn_server(self.argv, merged_env or None, self.cwd)
        except McpTransportError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 启动失败统一分类
            raise McpTransportError(f"server {self.name!r} spawn failed: {exc}") from exc
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"mcp-{self.name}"
        )
        self._reader_thread.start()
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout_s=self.initialize_timeout_s,
            )
            self._notify("notifications/initialized")
            result = self._request("tools/list", {}, timeout_s=self.request_timeout_s)
            tools = (result or {}).get("tools")
            self.tools = [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)] if isinstance(tools, list) else []
        except McpTransportError as exc:
            self.close()
            raise
        return self

    @property
    def connected(self) -> bool:
        return self._proc is not None and not self._closed and self._proc.poll() is None

    def close(self) -> None:
        """终止进程并停用（幂等）。"""
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc is not None:
            terminate = getattr(proc, "terminate", None)
            if terminate is not None:
                try:
                    terminate()
                except Exception:  # noqa: BLE001 —— 尽力收尾
                    pass
            wait = getattr(proc, "wait", None)
            if wait is not None:
                try:
                    wait(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ 调用

    def call_tool(self, tool: str, arguments: dict | None = None, *, timeout_s: float | None = None) -> dict:
        """tools/call → 原始 result dict（content/isError 语义见 MCP）。"""
        result = self._request(
            "tools/call",
            {"name": tool, "arguments": dict(arguments or {})},
            timeout_s=timeout_s or self.request_timeout_s,
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ JSON-RPC 帧内

    def _next_id(self) -> int:
        return next(self._ids)

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or self._closed:
            raise McpTransportError(f"server {self.name!r} is not running")
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            raise McpTransportError(f"server {self.name!r} has no stdin stream")
        try:
            stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpTransportError(f"server {self.name!r} stdin write failed: {exc}") from exc

    def _notify(self, method: str, params: dict | None = None) -> None:
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            frame["params"] = params
        self._send(frame)

    def _request(self, method: str, params: dict, *, timeout_s: float) -> dict:
        """串行请求-响应（锁内写-读；服务端推送跳过）。"""
        with self._lock:
            request_id = self._next_id()
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpTransportError(
                        f"server {self.name!r} {method} timed out after {timeout_s:g}s"
                    )
                try:
                    message = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise McpTransportError(
                        f"server {self.name!r} {method} timed out after {timeout_s:g}s"
                    ) from None
                if message.get("id") != request_id:
                    continue  # 服务端通知/别处请求的响应：跳过
                if "error" in message and message["error"] is not None:
                    error = message["error"]
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise McpTransportError(
                        f"server {self.name!r} {method} returned an error: {detail}"
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    def _read_loop(self) -> None:
        """后台读线程：逐行解析 JSON 入 inbox；EOF/坏行退出（连接态由 poll 反映）。"""
        proc = self._proc
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            return
        while not self._closed:
            try:
                line = stdout.readline()
            except (OSError, ValueError):
                break
            if not line:
                break  # EOF：进程退出或管道关闭
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行（server 日志混入 stdout）：跳过
            if isinstance(message, dict):
                self._inbox.put(message)


__all__ = [
    "BACKEND_MISSING_MESSAGE",
    "CLIENT_INFO",
    "INITIALIZE_TIMEOUT_S",
    "PROTOCOL_VERSION",
    "REQUEST_TIMEOUT_S",
    "McpTransportError",
    "StdioTransport",
    "spawn_server",
]
