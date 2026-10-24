"""M8 MCP client：transport 全打桩（spawn 缝返回假进程）、JSON-RPC 帧、
工具注册命名、失败降级、超时、mcp.json 管理、service control 命令。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from zcode_py.mcp import McpConfigError, McpManager, McpTransportError, StdioTransport, tool_name_for
from zcode_py.mcp import config as mcp_config
from zcode_py.mcp import transport as mcp_transport
from zcode_py.tools import ToolContext, ToolRegistry

try:  # 装配缝守门：proc_backend 已装配时「缝缺位」用例跳过
    from zcode_py.mcp import proc_backend  # noqa: F401

    PROC_BACKEND_ASSEMBLED = True
except ImportError:
    PROC_BACKEND_ASSEMBLED = False


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# 假进程：脚本化 stdin→stdout 的 JSON-RPC 对端
# ---------------------------------------------------------------------------


class FakeStdin:
    def __init__(self, proc):
        self._proc = proc
        self._buf = ""

    def write(self, data: str) -> None:
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._proc.on_request(line)

    def flush(self) -> None:
        pass


class FakeStdout:
    """条件变量阻塞队列语义的行读（对齐真管道的 readline 阻塞行为）。"""

    def __init__(self, proc):
        self._proc = proc
        self._cond = threading.Condition()
        self._lines: list[str] = []
        self._eof = False

    def push(self, line: str) -> None:
        with self._cond:
            self._lines.append(line)
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def readline(self) -> str:
        with self._cond:
            while not self._lines and not self._eof:
                if not self._cond.wait(timeout=0.05):
                    if self._proc.terminated:
                        return ""
            if self._lines:
                return self._lines.pop(0)
            return ""


class FakeProcess:
    """按 responder(request_dict) -> response_dict|None 脚本化的 MCP server。"""

    def __init__(self, responder=None, tools=None):
        self.requests: list[dict] = []
        self.terminated = False
        self.exit_code = None
        self.stdout = FakeStdout(self)
        self.stdin = FakeStdin(self)
        self._responder = responder or _default_responder(tools or _DEFAULT_TOOLS)

    def on_request(self, line: str) -> None:
        request = json.loads(line)
        self.requests.append(request)
        response = self._responder(request)
        if response is not None:
            self.stdout.push(json.dumps(response, ensure_ascii=False))

    def poll(self):
        return self.exit_code if self.terminated else None

    def terminate(self):
        self.terminated = True
        self.exit_code = -15  # SIGTERM 语义：poll() 从此报告退出
        self.stdout.close()

    def wait(self, timeout=None):
        return self.exit_code or 0


_DEFAULT_TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
]


def _default_responder(tools):
    def respond(request: dict):
        if request.get("method") == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            }
        if request.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": tools}}
        if request.get("method") == "tools/call":
            text = (request.get("params") or {}).get("arguments", {}).get("text", "")
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"content": [{"type": "text", "text": f"echo: {text}"}]},
            }
        return None  # 通知（notifications/initialized）不回应

    return respond


@pytest.fixture
def fake_spawn(monkeypatch):
    """打桩 spawn_server：记录调用并返回 FakeProcess。"""
    created: list[FakeProcess] = []
    calls: list[dict] = []

    def spawn(argv, env, cwd):
        calls.append({"argv": list(argv), "env": env, "cwd": cwd})
        proc = FakeProcess()
        created.append(proc)
        return proc

    monkeypatch.setattr(mcp_transport, "spawn_server", spawn)
    return {"spawn": spawn, "created": created, "calls": calls}


# ---------------------------------------------------------------------------
# transport：JSON-RPC 帧 / 握手 / 调用
# ---------------------------------------------------------------------------


def test_handshake_sends_initialize_notification_and_tools_list(fake_spawn):
    transport = StdioTransport("srv", ["mcp-server-fake", "--flag"], env={"K": "V"}, cwd="/w").start()
    proc = fake_spawn["created"][0]
    # env 语义（独立评审中危 #4）：脱敏基线 + 显式配置项叠加——配置键必须在，
    # 凭据意味的变量绝不出现（基线可能带 PATH 等，只做包含性断言）
    call = fake_spawn["calls"][0]
    assert call["argv"] == ["mcp-server-fake", "--flag"] and call["cwd"] == "/w"
    assert call["env"].get("K") == "V"
    assert not any(
        key.upper().startswith(("ZCODE_PY_API_KEY", "API_KEY", "SECRET", "TOKEN", "PASSWORD"))
        for key in call["env"]
    )
    methods = [r.get("method") for r in proc.requests]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
    init = proc.requests[0]
    assert init["jsonrpc"] == "2.0" and init["params"]["protocolVersion"] == "2024-11-05"
    assert init["params"]["clientInfo"]["name"] == "zcode-py"
    notify = proc.requests[1]
    assert "id" not in notify  # 通知无 id
    assert [t["name"] for t in transport.tools] == ["echo"]
    transport.close()
    assert proc.terminated


def test_call_tool_sends_frame_and_returns_result(fake_spawn):
    transport = StdioTransport("srv", ["x"]).start()
    result = transport.call_tool("echo", {"text": "你好"})
    assert result["content"][0]["text"] == "echo: 你好"
    last = [r for r in fake_spawn["created"][0].requests if r.get("method") == "tools/call"][-1]
    assert last["params"] == {"name": "echo", "arguments": {"text": "你好"}}
    assert last["jsonrpc"] == "2.0" and isinstance(last["id"], int)
    transport.close()


def test_call_timeout_raises_transport_error(monkeypatch):
    proc = FakeProcess(responder=lambda request: None)  # 谁都不应答
    monkeypatch.setattr(mcp_transport, "spawn_server", lambda argv, env, cwd: proc)
    transport = StdioTransport("srv", ["x"], request_timeout_s=0.15, initialize_timeout_s=0.15)
    with pytest.raises(McpTransportError, match="timed out"):
        transport.start()
    assert not transport.connected


def test_server_error_response_raises(fake_spawn, monkeypatch):
    proc = FakeProcess(
        responder=lambda request: (
            {"jsonrpc": "2.0", "id": request.get("id"), "error": {"message": "boom"}}
            if request.get("method") == "tools/list"
            else _default_responder(_DEFAULT_TOOLS)(request)
        )
    )
    monkeypatch.setattr(mcp_transport, "spawn_server", lambda argv, env, cwd: proc)
    transport = StdioTransport("srv", ["x"])
    with pytest.raises(McpTransportError, match="boom"):
        transport.start()


@pytest.mark.skipif(PROC_BACKEND_ASSEMBLED, reason="proc_backend 已装配，缺位分支不可达")
def test_spawn_seam_missing_is_structured_error():
    with pytest.raises(McpTransportError, match="MCP_BACKEND_SPEC"):
        mcp_transport.spawn_server(["anything"], None, None)


# ---------------------------------------------------------------------------
# manager：注册命名 / 失败降级 / 状态
# ---------------------------------------------------------------------------


def make_registry() -> ToolRegistry:
    return ToolRegistry()


def test_manager_registers_tools_with_mcp_naming(fake_spawn, tmp_path):
    from pathlib import Path

    path = tmp_path / "mcp.json"
    mcp_config.save_mcp_config(
        {"servers": [{"name": "srv", "command": ["mcp-server-fake"]}]}, path
    )
    registry = make_registry()
    manager = McpManager(registry=registry, config_path=path)
    manager.connect_all(background=False)

    assert "mcp__srv__echo" in registry.names()
    spec = registry.get("mcp__srv__echo")
    assert spec.tier == "write"  # 默认询问档
    assert "Echo text back" in spec.description
    statuses = manager.status_list()
    assert statuses[0]["name"] == "srv" and statuses[0]["status"] == "connected"
    assert statuses[0]["tools"] == ["echo"]

    outcome = spec.handler({"text": "hi"}, ToolContext(cwd=Path(".")))
    assert not outcome.is_error
    assert outcome.result["content"] == "echo: hi"
    manager.shutdown()
    assert "mcp__srv__echo" not in registry.names()  # 关停即注销


def test_manager_spawn_failure_degrades_to_error_status(monkeypatch, tmp_path):
    def broken_spawn(argv, env, cwd):
        raise McpTransportError("MCP 进程装配缝未落盘：见 docs/MCP_BACKEND_SPEC.md")

    monkeypatch.setattr(mcp_transport, "spawn_server", broken_spawn)
    path = tmp_path / "mcp.json"
    mcp_config.save_mcp_config({"servers": [{"name": "srv", "command": ["x"]}]}, path)
    registry = make_registry()
    manager = McpManager(registry=registry, config_path=path)
    manager.connect_all(background=False)  # 不抛：状态降级

    statuses = manager.status_list()
    assert statuses[0]["name"] == "srv" and statuses[0]["status"] == "error"
    assert "MCP_BACKEND_SPEC" in statuses[0]["error"]
    assert "mcp__srv__echo" not in registry.names()
    manager.shutdown()


def test_manager_handler_reports_unavailable_after_server_death(fake_spawn, tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.save_mcp_config({"servers": [{"name": "srv", "command": ["x"]}]}, path)
    registry = make_registry()
    manager = McpManager(registry=registry, config_path=path)
    manager.connect_all(background=False)
    spec = registry.get("mcp__srv__echo")
    fake_spawn["created"][0].terminate()  # server 死亡
    from pathlib import Path

    outcome = spec.handler({}, ToolContext(cwd=Path(".")))
    assert outcome.is_error and outcome.result["error"] == "mcp_server_unavailable"
    manager.shutdown()


def test_manager_truncates_huge_tool_output(fake_spawn, monkeypatch, tmp_path):
    big_tools = [{"name": "flood", "inputSchema": {"type": "object"}}]

    def respond(request):
        if request.get("method") == "tools/call":
            return {
                "jsonrpc": "2.0", "id": request.get("id"),
                "result": {"content": [{"type": "text", "text": "x" * 80_000}]},
            }
        return _default_responder(big_tools)(request)

    proc = FakeProcess(responder=respond)
    monkeypatch.setattr(mcp_transport, "spawn_server", lambda argv, env, cwd: proc)
    path = tmp_path / "mcp.json"
    mcp_config.save_mcp_config({"servers": [{"name": "srv", "command": ["x"]}]}, path)
    registry = make_registry()
    manager = McpManager(registry=registry, config_path=path)
    from pathlib import Path

    manager.connect_all(background=False)
    outcome = registry.get("mcp__srv__flood").handler({}, ToolContext(cwd=Path(".")))
    assert outcome.result["truncated"] is True
    assert len(outcome.result["content"]) < 32_000  # 3 万字符截断（L1 口径）
    assert "omitted" in outcome.result["content"]
    manager.shutdown()


def test_tool_name_for_format():
    assert tool_name_for("fs", "read_file") == "mcp__fs__read_file"


# ---------------------------------------------------------------------------
# mcp.json 管理
# ---------------------------------------------------------------------------


def test_config_add_remove_roundtrip(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(
        {"name": "fs", "command": ["npx", "-y", "server-fs", "/w"], "env": {"A": "1"}}, path
    )
    mcp_config.add_server({"name": "db", "command": ["mcp-server-db"]}, path)
    config = mcp_config.load_mcp_config(path)
    assert [s["name"] for s in config["servers"]] == ["fs", "db"]

    mcp_config.add_server({"name": "fs", "command": ["replaced"]}, path)  # 重名覆盖
    config = mcp_config.load_mcp_config(path)
    assert next(s for s in config["servers"] if s["name"] == "fs")["command"] == ["replaced"]

    mcp_config.remove_server("fs", path)
    assert [s["name"] for s in mcp_config.load_mcp_config(path)["servers"]] == ["db"]


def test_config_validation_rejects_bad_entries(tmp_path):
    with pytest.raises(McpConfigError):
        mcp_config.validate_server_entry({"name": "", "command": ["x"]})
    with pytest.raises(McpConfigError):
        mcp_config.validate_server_entry({"name": "x", "command": "not-a-list"})
    with pytest.raises(McpConfigError):
        mcp_config.validate_server_entry({"name": "x", "command": []})
    with pytest.raises(McpConfigError):
        mcp_config.validate_server_entry({"name": "x", "command": ["a"], "env": "nope"})
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(McpConfigError):
        mcp_config.load_mcp_config(tmp_path / "bad.json")


# ---------------------------------------------------------------------------
# service control：mcp_list / mcp_add / mcp_remove / mcp_refresh
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self):
        import io

        from zcode_py.llm.fake import FakeProvider
        from zcode_py.main import StdioEmitter, ZcodeService
        from zcode_py.sessions import SessionStore

        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter, provider_factory=lambda entry, key: FakeProvider()
        )

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def frames_of_type(self, type_name: str) -> list:
        return [f for f in self.frames() if f.get("type") == type_name]

    def results(self, command: str) -> list:
        return [f for f in self.frames() if f["type"] == "result" and f.get("command") == command]


def test_service_mcp_controls(tmp_home, fake_spawn):
    harness = Harness()
    harness.send({"type": "init", "workspaceRoots": [str(tmp_home)]})

    # 空配置：mcp_list 空
    harness.send({"type": "control", "command": "mcp_list"})
    assert json.loads(harness.results("mcp_list")[-1]["detail"]) == []

    # mcp_add：后台连接 → connected + 工具注册
    harness.send({
        "type": "control", "command": "mcp_add", "name": "fs",
        "server": {"command": ["mcp-server-fake"], "env": {"K": "V"}},
    })
    assert harness.results("mcp_add")[-1]["ok"] is True
    assert wait_for(lambda: "mcp__fs__echo" in harness.service._registry.names())
    assert wait_for(lambda: harness.service._mcp.status_list()[0]["status"] == "connected")
    harness.send({"type": "control", "command": "mcp_list"})
    statuses = json.loads(harness.results("mcp_list")[-1]["detail"])
    assert statuses[0]["name"] == "fs" and statuses[0]["status"] == "connected"
    assert statuses[0]["tools"] == ["echo"]

    # mcp_add 参数校验
    harness.send({"type": "control", "command": "mcp_add", "name": ""})
    assert harness.frames_of_type("error")[-1]["kind"] == "protocol"

    # mcp_remove：工具注销 + 配置摘除
    harness.send({"type": "control", "command": "mcp_remove", "name": "fs"})
    assert harness.results("mcp_remove")[-1]["ok"] is True
    assert "mcp__fs__echo" not in harness.service._registry.names()
    assert mcp_config.load_mcp_config()["servers"] == []


def test_service_mcp_spawn_failure_does_not_block_session(tmp_home, monkeypatch):
    def broken_spawn(argv, env, cwd):
        raise McpTransportError("MCP 进程装配缝未落盘")

    monkeypatch.setattr(mcp_transport, "spawn_server", broken_spawn)
    (tmp_home / "config").mkdir(parents=True)
    (tmp_home / "config" / "mcp.json").write_text(
        json.dumps({"servers": [{"name": "dead", "command": ["x"]}]}), encoding="utf-8"
    )
    harness = Harness()
    harness.send({"type": "init", "workspaceRoots": [str(tmp_home)]})
    # init 正常走完（session_started 已广播）——连接失败不阻塞会话
    assert harness.frames_of_type("session_started")
    assert wait_for(lambda: harness.service._mcp.status_list()[0]["status"] == "error")
    harness.send({"type": "control", "command": "mcp_list"})
    statuses = json.loads(harness.results("mcp_list")[-1]["detail"])
    assert statuses[0]["name"] == "dead" and statuses[0]["status"] == "error"
