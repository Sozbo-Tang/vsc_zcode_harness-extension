"""工具层测试：read/glob/grep/todo/write/edit/bash/webfetch/websearch。

bash 全部走假 bash_backend（真实执行核心因环境安全钩子未落盘，见
docs/BASH_BACKEND_SPEC.md；真实冒烟由 importorskip 守门）。webfetch /
websearch 的出网路径用 monkeypatch 替换取回函数——离线可测。
"""

from __future__ import annotations

import types

import pytest

from zcode_py.netguard import NetguardError
from zcode_py.tools import ToolContext, ToolOutcome, default_registry, head_tail
from zcode_py.tools import bash as bash_mod
from zcode_py.tools import web as web_mod
from zcode_py.tools.edit import tool_edit, tool_write
from zcode_py.tools.files import tool_glob, tool_grep, tool_read
from zcode_py.tools.todo import tool_todo


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("gamma = 3\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# notes\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(ws):
    return ToolContext(cwd=ws)


# ---------------------------------------------------------------------------
# registry / 通用
# ---------------------------------------------------------------------------


def test_default_registry_schemas():
    registry = default_registry()
    names = registry.names()
    assert names == [
        "bash", "edit", "glob", "grep", "read", "todo", "webfetch", "websearch", "write",
    ]
    schemas = registry.schemas()
    by_name = {s["function"]["name"]: s for s in schemas}
    assert set(by_name) == set(names)
    assert by_name["read"]["type"] == "function"
    assert by_name["read"]["function"]["parameters"]["required"] == ["path"]
    assert by_name["write"]["function"]["parameters"]["required"] == ["path", "content"]
    tiers = {name: registry.get(name).tier for name in names}
    assert tiers["read"] == "readonly"
    assert tiers["bash"] == "write"
    assert tiers["webfetch"] == "readonly"


def test_head_tail_truncation():
    text = "x" * 70_000
    out, truncated = head_tail(text, limit=30_000)
    assert truncated is True
    assert out.startswith("x" * 15_000)
    assert out.endswith("x" * 15_000)
    assert "omitted" in out
    assert len(out) < 40_000  # 明显小于原文
    short = "hello"
    assert head_tail(short) == (short, False)


def test_head_tail_keeps_distinct_head_and_tail():
    """头尾截断保留可区分的头与尾，中段整块丢弃（bash 输出截断的实质）。"""
    text = "H" * 40_000 + "M" * 20_000 + "T" * 40_000
    out, truncated = head_tail(text, limit=30_000)
    assert truncated is True
    assert out.startswith("H" * 15_000)     # 头 15000 保留
    assert out.endswith("T" * 15_000)       # 尾 15000 保留
    assert "M" not in out                   # 中段全丢
    assert "omitted" in out and "70000" in out  # 标注省略量（100000-30000）


def test_head_tail_default_limit_30k():
    out, truncated = head_tail("y" * 30_001)
    assert truncated is True
    out_eq, truncated_eq = head_tail("y" * 30_000)
    assert truncated_eq is False and out_eq == "y" * 30_000


# ---------------------------------------------------------------------------
# read / glob / grep
# ---------------------------------------------------------------------------


def test_read_basic_and_window(ctx):
    out = tool_read({"path": "src/a.py"}, ctx)
    assert out.is_error is False
    assert out.result["content"] == "alpha = 1\nbeta = 2"
    assert out.result["totalLines"] == 2
    assert out.result["truncated"] is False
    assert out.warning is None


def test_read_offset_limit(ctx):
    out = tool_read({"path": "src/a.py", "offset": 2, "limit": 5}, ctx)
    assert out.result["content"] == "beta = 2"
    assert out.result["startLine"] == 2


def test_read_privacy_warning(ws, ctx):
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    out = tool_read({"path": ".env"}, ctx)
    assert out.is_error is False
    assert out.warning and "privacy" in out.warning and ".env" in out.warning


def test_read_errors(ctx):
    assert tool_read({"path": "nope.py"}, ctx).result["error"] == "not_found"
    assert tool_read({"path": "src"}, ctx).result["error"] == "invalid_args"
    assert tool_read({}, ctx).result["error"] == "invalid_args"


def test_glob_matches_and_skip_dirs(ws, ctx):
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "skipme.js").write_text("x", encoding="utf-8")
    out = tool_glob({"pattern": "**/*.py"}, ctx)
    assert sorted(out.result["matches"]) == ["src/a.py", "src/b.py"]
    # 简单 basename 模式
    out = tool_glob({"pattern": "*.md"}, ctx)
    assert out.result["matches"] == ["notes.md"]


def test_grep_matches(ctx):
    out = tool_grep({"pattern": r"alpha|gamma"}, ctx)
    files = {m["file"] for m in out.result["matches"]}
    assert files == {"src/a.py", "src/b.py"}
    match = next(m for m in out.result["matches"] if m["file"] == "src/a.py")
    assert match["line"] == 1 and "alpha" in match["text"]
    # glob 过滤
    out = tool_grep({"pattern": r"\w+ = \d", "glob": "b.py"}, ctx)
    assert {m["file"] for m in out.result["matches"]} == {"src/b.py"}


def test_grep_skips_binary(ws, ctx):
    (ws / "blob.bin").write_bytes(b"alpha\x00\x01binary")
    out = tool_grep({"pattern": "alpha"}, ctx)
    assert all(m["file"] != "blob.bin" for m in out.result["matches"])


def test_grep_invalid_regex(ctx):
    assert tool_grep({"pattern": "("}, ctx).result["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# todo
# ---------------------------------------------------------------------------


def test_todo_replaces_whole_list(ctx):
    out = tool_todo({"todos": [{"content": "step1", "status": "completed"},
                               {"content": "step2", "status": "in_progress"}]}, ctx)
    assert out.result["completed"] == 1 and out.result["total"] == 2
    # 第二次调用整份覆盖
    out2 = tool_todo({"todos": [{"content": "only", "status": "pending"}]}, ctx)
    assert out2.result["total"] == 1
    assert ctx.state["todos"][0]["content"] == "only"


def test_todo_validation(ctx):
    assert tool_todo({}, ctx).result["error"] == "invalid_args"
    bad = tool_todo({"todos": [{"content": "x", "status": "wat"}]}, ctx)
    assert bad.result["error"] == "invalid_args"
    two_active = tool_todo(
        {"todos": [{"content": "a", "status": "in_progress"},
                   {"content": "b", "status": "in_progress"}]}, ctx)
    assert two_active.result["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# write / edit
# ---------------------------------------------------------------------------


def test_write_creates_file(ws, ctx):
    out = tool_write({"path": "src/new.py", "content": "print(1)\n"}, ctx)
    assert out.is_error is False
    assert (ws / "src" / "new.py").read_text() == "print(1)\n"
    assert out.result["bytes"] == 9


def test_edit_accepted_via_review(ws, ctx):
    ctx.review_diff = lambda path, old, new: True
    out = tool_edit({"path": "src/a.py", "old_str": "alpha = 1", "new_str": "alpha = 42"}, ctx)
    assert out.is_error is False
    assert out.result["status"] == "applied"
    assert "alpha = 42" in (ws / "src" / "a.py").read_text()


def test_edit_rejected_leaves_file_untouched(ws, ctx):
    before = (ws / "src" / "a.py").read_text()
    ctx.review_diff = lambda path, old, new: False
    out = tool_edit({"path": "src/a.py", "old_str": "alpha = 1", "new_str": "alpha = 42"}, ctx)
    assert out.is_error is True
    assert out.result["error"] == "diff_rejected"
    assert (ws / "src" / "a.py").read_text() == before


def test_edit_no_reviewer_refuses(ws, ctx):
    before = (ws / "src" / "a.py").read_text()
    out = tool_edit({"path": "src/a.py", "old_str": "alpha = 1", "new_str": "x"}, ctx)
    assert out.result["error"] == "diff_review_unavailable"
    assert (ws / "src" / "a.py").read_text() == before


def test_edit_ambiguous_and_missing(ws, ctx):
    (ws / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    ctx.review_diff = lambda *a: True
    amb = tool_edit({"path": "dup.txt", "old_str": "x", "new_str": "y"}, ctx)
    assert amb.result["error"] == "old_str_ambiguous"
    gone = tool_edit({"path": "src/a.py", "old_str": "nope", "new_str": "y"}, ctx)
    assert gone.result["error"] == "old_str_not_found"
    # replace_all 通过
    ok = tool_edit({"path": "dup.txt", "old_str": "x", "new_str": "y", "replace_all": True}, ctx)
    assert ok.result["status"] == "applied" and ok.result["replaced"] == 2


def test_edit_review_gets_exact_texts(ws, ctx):
    seen = {}
    def review(path, old, new):
        seen.update(path=path, old=old, new=new)
        return False
    ctx.review_diff = review
    tool_edit({"path": "src/a.py", "old_str": "beta = 2", "new_str": "beta = 3"}, ctx)
    assert seen == {"path": "src/a.py", "old": "beta = 2", "new": "beta = 3"}


# ---------------------------------------------------------------------------
# bash（假后端；装配缝语义）
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, *, exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=5):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration_ms = duration_ms
        self.calls = []

    class BashBackendError(RuntimeError):
        pass

    def run_command(self, command, *, cwd=None, timeout=120.0, env=None):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        return types.SimpleNamespace(
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            duration_ms=self.duration_ms,
        )


def test_bash_backend_missing_returns_structured_error(ctx, monkeypatch):
    monkeypatch.setattr(bash_mod, "bash_backend", None)
    out = bash_mod.tool_bash({"command": "echo hi"}, ctx)
    assert out.is_error is True
    assert out.result["error"] == "backend_unavailable"
    assert "BASH_BACKEND_SPEC" in out.result["detail"]


def test_bash_allowed_path_through_fake_backend(ctx, monkeypatch):
    fake = _FakeBackend(stdout="hello\n", stderr="")
    monkeypatch.setattr(bash_mod, "bash_backend", fake)
    out = bash_mod.tool_bash({"command": "echo hi", "timeout_ms": 5000}, ctx)
    assert out.is_error is False
    assert out.result["exitCode"] == 0
    assert out.result["stdout"] == "hello\n"
    assert out.result["durationMs"] == 5
    call = fake.calls[0]
    assert call["command"] == "echo hi"
    assert call["timeout"] == 5.0


def test_bash_nonzero_exit_is_data_not_error(ctx, monkeypatch):
    fake = _FakeBackend(exit_code=3, stderr="boom")
    monkeypatch.setattr(bash_mod, "bash_backend", fake)
    out = bash_mod.tool_bash({"command": "false"}, ctx)
    assert out.is_error is False  # 非零退出=结构化数据
    assert out.result["exitCode"] == 3 and out.result["stderr"] == "boom"


def test_bash_truncates_oversized_output(ctx, monkeypatch):
    fake = _FakeBackend(stdout="A" * 80_000, stderr="B" * 80_000)
    monkeypatch.setattr(bash_mod, "bash_backend", fake)
    out = bash_mod.tool_bash({"command": "cat big"}, ctx)
    assert out.result["truncated"] is True
    assert "omitted" in out.result["stdout"]
    assert len(out.result["stdout"]) < 40_000
    assert out.result["outputBudget"] == 30_000


def test_bash_timeout_marked(ctx, monkeypatch):
    fake = _FakeBackend(timed_out=True, stdout="partial")
    monkeypatch.setattr(bash_mod, "bash_backend", fake)
    out = bash_mod.tool_bash({"command": "sleep 999", "timeout_ms": 1000}, ctx)
    assert out.is_error is True
    assert out.result["error"] == "timeout"
    assert out.result["timedOut"] is True
    assert out.result["stdout"] == "partial"  # 部分输出仍回灌


def test_bash_spawn_failure(ctx, monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(bash_mod, "bash_backend", fake)

    def boom(command, **kw):
        raise fake.BashBackendError("no shell found")

    fake.run_command = boom
    out = bash_mod.tool_bash({"command": "x"}, ctx)
    assert out.result["error"] == "spawn_failed"


def test_bash_env_sanitized(ctx, monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(bash_mod, "bash_backend", fake)
    monkeypatch.setenv("ZCODE_PY_API_KEY_GLM", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "t")
    monkeypatch.setenv("HARMLESS_VAR", "ok")
    bash_mod.tool_bash({"command": "env"}, ctx)
    child_env = fake.calls[0]["env"]
    assert "ZCODE_PY_API_KEY_GLM" not in child_env
    assert "MY_TOKEN" not in child_env
    assert child_env["HARMLESS_VAR"] == "ok"


def test_bash_timeout_clamped(ctx, monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(bash_mod, "bash_backend", fake)
    bash_mod.tool_bash({"command": "x", "timeout_ms": 999_999_999}, ctx)
    assert fake.calls[-1]["timeout"] == 600.0  # 上限 10 分钟
    bash_mod.tool_bash({"command": "x"}, ctx)
    assert fake.calls[-1]["timeout"] == 60.0  # 默认 60s


# ---------------------------------------------------------------------------
# webfetch / websearch（monkeypatch 出网）
# ---------------------------------------------------------------------------


def test_webfetch_blocks_internal_url(ctx):
    """真实 fetch_page 路径：validate_url 在任何 httpx 调用之前抛 NetguardError
    （离线安全——不会发起网络），工具转结构化 blocked 错误。"""
    out = web_mod.tool_webfetch({"url": "http://127.0.0.1:8080/api"}, ctx)
    assert out.is_error is True
    assert out.result["error"] == "blocked"
    assert "127.0.0.1" in out.result["detail"]


def test_webfetch_happy_path_and_privacy_warning(ctx, monkeypatch):
    def fake_fetch(url):
        return {"url": url, "finalUrl": url, "status": 200,
                "contentType": "text/html",
                "body": b"<html><script>evil()</script><body><p>Hello world</p></body></html>"}

    monkeypatch.setattr(web_mod, "fetch_page", fake_fetch)
    out = web_mod.tool_webfetch({"url": "https://example.com/a.pem"}, ctx)
    assert out.is_error is False
    assert "Hello world" in out.result["content"]
    assert "evil()" not in out.result["content"]  # script 剥除
    assert out.warning and "pem" in out.warning


def test_webfetch_http_error_status(ctx, monkeypatch):
    monkeypatch.setattr(web_mod, "fetch_page", lambda url: {
        "url": url, "finalUrl": url, "status": 404, "contentType": "text/plain", "body": b""})
    out = web_mod.tool_webfetch({"url": "https://example.com/x"}, ctx)
    assert out.result["error"] == "http" and out.result["status"] == 404


def test_webfetch_truncates_long_content(ctx, monkeypatch):
    monkeypatch.setattr(web_mod, "fetch_page", lambda url: {
        "url": url, "finalUrl": url, "status": 200,
        "contentType": "text/plain", "body": b"z" * 90_000})
    out = web_mod.tool_webfetch({"url": "https://example.com/big"}, ctx)
    assert out.result["truncated"] is True and len(out.result["content"]) < 40_000


def test_websearch_parses_results(ctx, monkeypatch):
    html = """
    <table>
    <tr><td><a rel="nofollow" href="https://a.example/x" class='result-link'>Result A</a></td></tr>
    <tr><td class='result-snippet'>Snip <b>A</b></td></tr>
    <tr><td><a rel="nofollow" href="https://b.example/y" class='result-link'>Result B</a></td></tr>
    <tr><td class='result-snippet'>Snip B</td></tr>
    <tr><td><a rel="nofollow" href="https://duckduckgo.com/ad" class='result-link'>Ad</a></td></tr>
    </table>
    """
    monkeypatch.setattr(web_mod, "_search_fetch", lambda url: (200, "text/html", url, html.encode()))
    out = web_mod.tool_websearch({"query": "example"}, ctx)
    assert out.is_error is False
    urls = [r["url"] for r in out.result["results"]]
    assert urls == ["https://a.example/x", "https://b.example/y"]  # 广告链接被滤
    assert out.result["results"][0]["snippet"] == "Snip A"


def test_websearch_no_results_structured_error(ctx, monkeypatch):
    monkeypatch.setattr(web_mod, "_search_fetch", lambda url: (200, "text/html", url, b"<html></html>"))
    out = web_mod.tool_websearch({"query": "q"}, ctx)
    assert out.is_error is True
    assert out.result["error"] == "no_results"


def test_websearch_blocked_backend_url(ctx):
    """自建搜索后端指向内网：fetch_page 内 validate_url 先于任何 HTTP 抛错。"""
    ctx.state["searchBaseUrl"] = "http://192.168.1.10:8888"  # 内网后端
    out = web_mod.tool_websearch({"query": "q"}, ctx)
    assert out.result["error"] == "blocked"


def test_redirects_validated_per_hop(monkeypatch):
    """重定向逐跳校验：公网→内网的重定向被拒（用真 fetch_page + 假 httpx 层验证逻辑不易，
    这里直接验证 validate_url 对 Location 目标的行为——fetch_page 内部对每跳调用它）。"""
    from zcode_py.netguard import validate_url

    with pytest.raises(NetguardError):
        validate_url("http://10.0.0.5/internal")


def test_html_to_text():
    raw = "<style>.x{}</style><p>Hi</p><script>bad()</script><div>There</div>"
    text = web_mod.html_to_text(raw)
    assert "Hi" in text and "There" in text
    assert "bad()" not in text and ".x{}" not in text


# ---------------------------------------------------------------------------
# 真实 bash 冒烟（装配缝守门）：bash_backend 落盘后自动生效
# ---------------------------------------------------------------------------


def test_bash_real_backend_smoke(tmp_path):
    """真实后端冒烟：字面量 echo 命令。backend 未装配时整条跳过（不算失败）。

    装配方式见 docs/BASH_BACKEND_SPEC.md。
    """
    pytest.importorskip("zcode_py.tools.bash_backend")
    import importlib

    from zcode_py.tools import bash as bash_module

    importlib.reload(bash_module)  # 拿到非 None 的 bash_backend 引用
    ctx = ToolContext(cwd=tmp_path)
    out = bash_module.tool_bash({"command": "echo smoke-ok"}, ctx)
    assert out.is_error is False, out.result
    assert out.result["exitCode"] == 0
    assert out.result["stdout"].strip() == "smoke-ok"


# ---------------------------------------------------------------------------
# grep 隐私护栏（R15，独立评审中危 #4：grep 同样把文件内容送进模型上下文）
# ---------------------------------------------------------------------------


def test_grep_flags_sensitive_file_matches(ws, ctx):
    (ws / ".env").write_text("API_KEY=sk-test\n", encoding="utf-8")
    out = tool_grep({"pattern": "API_KEY"}, ctx)
    assert out.is_error is False
    assert out.result["count"] == 1
    assert out.warning and "privacy" in out.warning


def test_grep_no_warning_for_normal_matches(ws, ctx):
    out = tool_grep({"pattern": "alpha"}, ctx)
    assert out.warning is None
    assert out.result["count"] >= 1


def test_grep_direct_sensitive_file_warns(ws, ctx):
    (ws / "id_rsa").write_text("contains alpha\n", encoding="utf-8")
    out = tool_grep({"pattern": "alpha", "path": "id_rsa"}, ctx)
    assert out.result["count"] == 1
    assert out.warning and "privacy" in out.warning
