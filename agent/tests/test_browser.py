"""M6 浏览器自动化（R8）测试：browser-use 不安装、不真实运行——安装路径
全部打桩（monkeypatch 模块内 _create_session / _create_agent / _run_agent /
_capture_screenshot 接缝）；未安装路径覆盖「注册返回 False + registry 无工具
+ 结构化安装指引」。域名解析按 test_netguard 惯例伪造 getaddrinfo（离线）。
"""

from __future__ import annotations

import socket

import pytest

from zcode_py.tools import ToolContext, ToolRegistry
from zcode_py.tools import browser as browser_mod
from zcode_py.tools.browser import (
    BROWSER_TOOL_NAMES,
    INSTALL_COMMAND,
    TIMELINE_LIMIT,
    browser_dir,
    browser_unavailable_outcome,
    register_browser_tools,
    tool_browser_navigate,
    tool_browser_screenshot,
)


def _fake_resolver(addresses):
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]

    return resolve


class FakeHistory:
    def __init__(self, count):
        self._items = [{"action": f"step-{i}", "detail": f"target {i}"} for i in range(count)]

    def model_actions(self):
        return list(self._items)


class FakeState:
    def __init__(self, history):
        self.history = history


class FakeAgent:
    def __init__(self, final="The page lists three release notes.", actions=3):
        self._final = final
        self.state = FakeState(FakeHistory(actions))

    def final_result(self):
        return self._final


class FakeSession:
    def __init__(self):
        self.closed = False


@pytest.fixture
def installed(monkeypatch):
    """「已安装」形态：_BROWSER_USE=True + 四个接缝全打桩。"""
    monkeypatch.setattr(browser_mod, "_BROWSER_USE", True)
    created: dict = {}

    def fake_create_session(headless):
        created["headless"] = headless
        session = FakeSession()
        created["session"] = session
        return session

    agents: list[FakeAgent] = []

    def fake_create_agent(task, session, llm=None):
        agent = FakeAgent()
        agents.append(agent)
        return agent

    monkeypatch.setattr(browser_mod, "_create_session", fake_create_session)
    monkeypatch.setattr(browser_mod, "_create_agent", fake_create_agent)
    monkeypatch.setattr(browser_mod, "_run_agent", lambda agent: None)
    monkeypatch.setattr(browser_mod, "_capture_screenshot", lambda session: b"PNGDATA")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["93.184.216.34"]))
    return created, agents


def make_ctx(tmp_path, emitted):
    ctx = ToolContext(cwd=tmp_path)
    ctx.state["emit"] = emitted.append
    return ctx


# ---------------------------------------------------------------------------
# 注册：未安装不注册；安装后注册两件套
# ---------------------------------------------------------------------------


def test_register_returns_false_when_not_installed(monkeypatch):
    monkeypatch.setattr(browser_mod, "_BROWSER_USE", False)
    registry = ToolRegistry()
    assert register_browser_tools(registry) is False
    assert registry.names() == []
    assert "browser_navigate" not in registry.names()
    assert "browser_screenshot" not in registry.names()


def test_register_both_tools_when_installed(installed):
    _created, _agents = installed
    registry = ToolRegistry()
    assert register_browser_tools(registry) is True
    names = registry.names()
    assert "browser_navigate" in names and "browser_screenshot" in names
    for name in BROWSER_TOOL_NAMES:
        assert registry.get(name).tier == "write"  # 权限档：默认询问


def test_unavailable_outcome_carries_install_guidance():
    for name in BROWSER_TOOL_NAMES:
        outcome = browser_unavailable_outcome(name)
        assert outcome.is_error
        assert outcome.result["error"] == "browser_use_not_installed"
        assert outcome.result["tool"] == name
        assert INSTALL_COMMAND in outcome.result["install"]
        assert "--group browser" in outcome.result["detail"]


# ---------------------------------------------------------------------------
# navigate：SSRF 前置校验 / headless 配置 / 时间线 / 截图 artifact
# ---------------------------------------------------------------------------


def test_navigate_rejects_internal_url_before_launch(tmp_home, tmp_path, installed):
    created, _agents = installed
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_navigate({"url": "http://127.0.0.1:8080/admin"}, ctx)
    assert outcome.is_error
    assert outcome.result["error"] == "blocked"
    assert "host check failed" in outcome.result["detail"]
    assert "browserSession" not in ctx.state  # 校验先于浏览器启动
    assert created == {}  # 未创建任何会话


def test_navigate_rejects_private_dns(tmp_home, tmp_path, installed, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["10.1.2.3"]))
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_navigate({"url": "http://internal.corp/api"}, ctx)
    assert outcome.is_error and outcome.result["error"] == "blocked"


def test_navigate_success_timeline_and_artifact(tmp_home, tmp_path, installed, monkeypatch):
    created, agents = installed
    # 40 个动作 → 时间线截断到 TIMELINE_LIMIT
    agent = FakeAgent(actions=TIMELINE_LIMIT + 10)
    monkeypatch.setattr(browser_mod, "_create_agent", lambda task, session, llm=None: agent)
    emitted = []
    ctx = make_ctx(tmp_path, emitted)
    ctx.state["browserHeadless"] = False
    outcome = tool_browser_navigate({"url": "https://example.com/", "task": "find the title"}, ctx)
    assert not outcome.is_error
    assert outcome.result["url"] == "https://example.com/"
    assert outcome.result["task"] == "find the title"
    assert outcome.result["result"].startswith("The page lists")
    # 时间线截断：≤30 行 + truncated 标记 + 末行注明省略
    timeline = outcome.result["timeline"]
    assert len(timeline) <= TIMELINE_LIMIT
    assert outcome.result["timelineTruncated"] is True
    assert "omitted" in timeline[-1]
    # headless 配置透传给会话工厂；会话复用进 ctx.state
    assert created["headless"] is False
    assert ctx.state["browserSession"] is created["session"]
    # 截图落盘 <home>/browser/*.png + image_artifact 帧
    artifacts = [m for m in emitted if type(m).__name__ == "ImageArtifact"]
    assert len(artifacts) == 1
    saved = browser_dir() / artifacts[0].path.split("/")[-1]
    assert saved.read_bytes() == b"PNGDATA"
    assert outcome.result["screenshot"] == str(saved)


def test_navigate_timeline_not_truncated_below_limit(tmp_home, tmp_path, installed):
    _created, _agents = installed
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_navigate({"url": "https://example.com/"}, ctx)
    assert len(outcome.result["timeline"]) == 3
    assert "timelineTruncated" not in outcome.result
    assert outcome.result["timeline"][0] == "step-0: target 0"


def test_navigate_default_task_and_agent_invocation(tmp_home, tmp_path, installed):
    created, agents = installed
    ctx = make_ctx(tmp_path, [])
    tool_browser_navigate({"url": "https://example.com/"}, ctx)
    assert agents, "agent factory was invoked"
    # 默认任务=总结页面；agent 任务里带 URL 且限定不跨源
    assert len(agents) == 1


def test_navigate_requires_url(tmp_home, tmp_path, installed):
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_navigate({"task": "x"}, ctx)
    assert outcome.is_error and outcome.result["error"] == "invalid_args"


def test_navigate_reports_browser_failure(tmp_home, tmp_path, installed, monkeypatch):
    def boom(agent):
        raise RuntimeError("chromium crashed")

    monkeypatch.setattr(browser_mod, "_run_agent", boom)
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_navigate({"url": "https://example.com/"}, ctx)
    assert outcome.is_error and outcome.result["error"] == "browser_failed"
    assert "chromium crashed" in outcome.result["detail"]


# ---------------------------------------------------------------------------
# screenshot：无会话报错；有会话存盘 + 广播 artifact
# ---------------------------------------------------------------------------


def test_screenshot_without_session_errors(tmp_home, tmp_path, installed):
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_screenshot({}, ctx)
    assert outcome.is_error
    assert outcome.result["error"] == "no_session"


def test_screenshot_saves_and_broadcasts(tmp_home, tmp_path, installed):
    created, _agents = installed
    emitted = []
    ctx = make_ctx(tmp_path, emitted)
    ctx.state["browserSession"] = FakeSession()
    outcome = tool_browser_screenshot({}, ctx)
    assert not outcome.is_error
    assert outcome.result["bytes"] == len(b"PNGDATA")
    saved = browser_dir() / outcome.result["screenshot"].split("/")[-1]
    assert saved.read_bytes() == b"PNGDATA"
    assert [m for m in emitted if type(m).__name__ == "ImageArtifact"]


def test_handlers_guard_when_module_flag_off(tmp_home, tmp_path, monkeypatch):
    """handler 双保险：_BROWSER_USE=False 时直接返回安装指引（不触碰会话）。"""
    monkeypatch.setattr(browser_mod, "_BROWSER_USE", False)
    ctx = make_ctx(tmp_path, [])
    outcome = tool_browser_navigate({"url": "https://example.com/"}, ctx)
    assert outcome.result["error"] == "browser_use_not_installed"
    shot = tool_browser_screenshot({}, ctx)
    assert shot.result["error"] == "browser_use_not_installed"
