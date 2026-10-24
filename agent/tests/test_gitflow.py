"""git 流测试（R17 学习式提交 + R22 推送/PR；PLAN §2.6 / §8）。

真实 git 仓库在 tmp 内搭建（字面量 argv 子进程，无 shell）；推送走本地
bare 仓库当 origin（无网络）；gh 用假脚本（PATH 注入，输出 PR URL）。
服务层（main.py）白名单与帧往返用迷你 Harness 直连 ZcodeService。
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from zcode_py.gitflow import GitFlow, normalize_remote_url
from zcode_py.llm.fake import FakeProvider
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.sessions import SessionStore


def git(repo, *args, check=True, env=None):
    """字面量 argv 跑 git（与 checkpoints 测试同款 fixture 约定）。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def commit(repo, *args):
    """带内联身份的 commit（fixture 专用；gitflow 生产路径不代配身份）。"""
    return git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", *args)


@pytest.fixture
def repo(tmp_path):
    """conventional 英文风格仓库（feat/fix、无 scope）。"""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    git(root, "add", "a.txt")
    commit(root, "-q", "-m", "feat: init")
    (root / "a.txt").write_text("v1\nv2\n", encoding="utf-8")
    git(root, "add", "a.txt")
    commit(root, "-q", "-m", "fix: a bug")
    (root / "b.md").write_text("# doc\n", encoding="utf-8")
    git(root, "add", "b.md")
    commit(root, "-q", "-m", "docs: add b")
    return root


# ---------------------------------------------------------------------------
# 风格学习（R17）
# ---------------------------------------------------------------------------


def test_learn_style_conventional(repo):
    style = GitFlow(repo).learn_style()
    assert style.samples == 3
    assert style.conventional_ratio == 1.0
    assert style.types == {"feat": 1, "fix": 1, "docs": 1}
    assert style.scope_ratio == 0.0  # fixture 无 scope
    assert style.zh_ratio == 0.0
    assert not style.trailing_period


def test_learn_style_zh_freeform(tmp_path):
    root = tmp_path / "zhproj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "x.txt").write_text("1\n", encoding="utf-8")
    git(root, "add", "x.txt")
    commit(root, "-q", "-m", "初始化项目")
    (root / "x.txt").write_text("1\n2\n", encoding="utf-8")
    git(root, "add", "x.txt")
    commit(root, "-q", "-m", "修复问题")
    style = GitFlow(root).learn_style()
    assert style.samples == 2
    assert style.conventional_ratio == 0.0  # 自由格式
    assert style.zh_ratio == 1.0


def test_learn_style_empty_repo(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    style = GitFlow(root).learn_style()
    assert style.samples == 0
    assert style.conventional_ratio == 0.0
    assert "无提交历史" in style.to_note()


# ---------------------------------------------------------------------------
# diff 范围（staged / all + untracked）
# ---------------------------------------------------------------------------


def test_diff_summary_staged_vs_all(repo):
    # 已暂存：新增 c.txt；未暂存：a.txt 再改；未跟踪：u.txt
    (repo / "c.txt").write_text("c1\nc2\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    (repo / "a.txt").write_text("v1\nv2\nv3\n", encoding="utf-8")
    (repo / "u.txt").write_text("u1\nu2\nu3\n", encoding="utf-8")
    flow = GitFlow(repo)
    staged = flow.diff_summary("staged")
    assert [f.path for f in staged.files] == ["c.txt"]
    assert staged.files[0].status == "A"
    assert staged.files[0].insertions == 2
    assert staged.untracked == 0
    alld = flow.diff_summary("all")
    paths = {f.path: f for f in alld.files}
    assert set(paths) == {"c.txt", "a.txt", "u.txt"}
    assert paths["a.txt"].insertions == 1 and paths["a.txt"].deletions == 0
    assert paths["u.txt"].status == "??"
    assert paths["u.txt"].insertions == 3  # untracked 行数按内容统计
    assert alld.untracked == 1


def test_diff_summary_untracked_binary_no_counts(repo):
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02data")
    summary = GitFlow(repo).diff_summary("all")
    f = next(x for x in summary.files if x.path == "blob.bin")
    assert f.status == "??"
    assert f.insertions is None  # 二进制不计行


def test_diff_summary_invalid_scope_raises(repo):
    with pytest.raises(Exception):
        GitFlow(repo).diff_summary("everything")


# ---------------------------------------------------------------------------
# 信息生成（R17）
# ---------------------------------------------------------------------------


def test_suggest_message_conventional_english(repo):
    flow = GitFlow(repo)
    style = flow.learn_style()
    (repo / "new.py").write_text("print(1)\n", encoding="utf-8")
    git(repo, "add", "new.py")
    subject, body = flow.suggest_message(style, flow.diff_summary("staged"))
    # 新增主导 → feat；scope_ratio=0 → 不带 (scope)；英文风格
    assert subject == "feat: add new"
    assert body.startswith("A new.py (+1")


def test_suggest_message_chinese_freeform(tmp_path):
    root = tmp_path / "zhproj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "x.txt").write_text("1\n", encoding="utf-8")
    git(root, "add", "x.txt")
    commit(root, "-q", "-m", "初始化项目")
    flow = GitFlow(root)
    style = flow.learn_style()
    (root / "x.txt").write_text("1\n2\n3\n", encoding="utf-8")
    subject, _ = flow.suggest_message(style, flow.diff_summary("all"))
    # 中文自由格式：无 conventional 前缀；单文件无「等 N 个」后缀
    assert subject == "更新 x"


def test_suggest_type_heuristics(repo):
    flow = GitFlow(repo)
    style = flow.learn_style()
    # 纯文档新增 → docs（fixture 风格本身即 conventional）
    (repo / "guide.md").write_text("# g\n", encoding="utf-8")
    git(repo, "add", "guide.md")
    subject, _ = flow.suggest_message(style, flow.diff_summary("staged"))
    assert subject.startswith("docs: ")
    # 删除主导（非文档文件）→ refactor
    git(repo, "reset", "-q")
    git(repo, "rm", "-q", "--cached", "a.txt")
    subject2, _ = flow.suggest_message(style, flow.diff_summary("staged"))
    assert subject2.startswith("refactor: ")


# ---------------------------------------------------------------------------
# remote URL 规范化 + commit 网址（R22）
# ---------------------------------------------------------------------------


def test_normalize_remote_url_variants():
    assert normalize_remote_url("git@github.com:owner/repo.git") == (
        "https://github.com/owner/repo", "github")
    assert normalize_remote_url("ssh://git@github.com/owner/repo.git") == (
        "https://github.com/owner/repo", "github")
    assert normalize_remote_url("ssh://git@github.com:22/owner/repo.git") == (
        "https://github.com/owner/repo", "github")
    assert normalize_remote_url("https://github.com/owner/repo.git") == (
        "https://github.com/owner/repo", "github")
    assert normalize_remote_url("https://gitlab.com/o/r") == (
        "https://gitlab.com/o/r", "gitlab")
    assert normalize_remote_url("git://github.com/o/r.git") == (
        "https://github.com/o/r", "github")
    # 本地路径 / 未知 host → other（不拼网址）
    assert normalize_remote_url("/tmp/origin.git") == (None, "other")
    assert normalize_remote_url("git@git.corp.local:o/r.git") == (None, "other")
    assert normalize_remote_url("") == (None, "none")


def test_commit_url_concatenation(tmp_path):
    root = tmp_path / "gh"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a").write_text("1\n", encoding="utf-8")
    git(root, "add", "a")
    commit(root, "-q", "-m", "feat: init")
    flow = GitFlow(root)
    # 无 remote → None
    assert flow.commit_url("0" * 40) is None
    # GitHub remote → 网页根 + /commit/ + sha
    git(root, "remote", "add", "origin", "git@github.com:owner/repo.git")
    assert flow.commit_url("abc123") == "https://github.com/owner/repo/commit/abc123"
    # 本地 bare remote（非知名托管）→ None（UI 降级展示 sha）
    git(root, "remote", "set-url", "origin", str(tmp_path / "origin.git"))
    assert flow.commit_url("abc123") is None


# ---------------------------------------------------------------------------
# 预览与降级
# ---------------------------------------------------------------------------


def test_preview_no_remote_degrades(repo):
    payload = GitFlow(repo).preview("all")
    assert payload.ok
    assert payload.remoteKind == "none"
    assert payload.remoteUrl is None
    assert payload.canPr is False
    assert "无 origin remote" in payload.styleNote
    assert payload.subject != ""


def test_preview_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    payload = GitFlow(plain).preview("all")
    assert not payload.ok
    assert "不是 git 仓库" in (payload.error or "")


def test_preview_invalid_scope(repo):
    payload = GitFlow(repo).preview("everything")
    assert not payload.ok
    assert "scope" in (payload.error or "")


# ---------------------------------------------------------------------------
# 执行：commit / push（本地 bare remote 当 origin，无网络）
# ---------------------------------------------------------------------------


def test_commit_staged_only_leaves_unstaged(repo):
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    (repo / "a.txt").write_text("v1\nv2\nDIRTY\n", encoding="utf-8")
    out = GitFlow(repo).commit_and_push("staged", False, "feat: add c\n\n- A c.txt")
    assert out.ok, out.error
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert out.commit_sha == sha
    # 未暂存的 a.txt 改动仍在工作区（staged 范围不 add）
    status = git(repo, "status", "--porcelain").stdout
    assert "DIRTY" in (repo / "a.txt").read_text()
    assert "a.txt" in status
    # 提交信息（多行 body 经 stdin）原样落库
    shown = git(repo, "show", "-s", "--pretty=format:%B", "HEAD").stdout
    assert shown.startswith("feat: add c")
    assert "- A c.txt" in shown


def test_commit_all_includes_untracked(repo):
    (repo / "u.txt").write_text("u\n", encoding="utf-8")
    (repo / "a.txt").write_text("v1\nv2\nv3\n", encoding="utf-8")
    out = GitFlow(repo).commit_and_push("all", False, None)  # message 缺省 → 现场生成
    assert out.ok, out.error
    tracked = git(repo, "ls-files").stdout.split()
    assert "u.txt" in tracked  # untracked 已被 add -A 收进
    # 缺省信息按风格生成（fixture = conventional 英文）
    shown = git(repo, "show", "-s", "--pretty=format:%s", "HEAD").stdout
    assert shown.startswith(("feat: ", "chore: "))


def test_commit_staged_with_nothing_staged_fails_cleanly(repo):
    (repo / "a.txt").write_text("v1\nv2\nDIRTY\n", encoding="utf-8")  # 只有未暂存
    out = GitFlow(repo).commit_and_push("staged", False, "feat: x")
    assert not out.ok
    assert "提交失败" in (out.error or "")


def test_commit_identity_missing_reports_hint(repo, tmp_path, monkeypatch):
    """无 user.name/user.email（隔离全局/系统配置 + useConfigOnly 禁自动探测）
    → 如实报错 + 配置指引。"""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-config"))
    # 强制 C locale：git 的身份报错文案可预期（关键字匹配不依赖系统语言）
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    git(repo, "config", "user.useConfigOnly", "true")
    (repo / "n.txt").write_text("n\n", encoding="utf-8")
    git(repo, "add", "n.txt")
    out = GitFlow(repo).commit_and_push("staged", False, "feat: n")
    assert not out.ok
    assert "提交失败" in (out.error or "")
    assert out.hint and "user.name" in out.hint


def test_push_without_remote_degrades_with_hint(repo):
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    out = GitFlow(repo).commit_and_push("staged", True, "feat: c")
    assert out.ok  # 提交本身成功
    assert not out.pushed
    assert out.hint and "remote" in out.hint


@pytest.fixture
def repo_with_bare_origin(repo, tmp_path):
    """origin = 本地 bare 仓库（真实 push，无网络；无 upstream → set-upstream
    重试路径也被覆盖）。git -C 要求目录已存在，先 mkdir 再 init。"""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(bare))
    return repo, bare


def test_push_to_local_bare_origin(repo_with_bare_origin):
    repo, bare = repo_with_bare_origin
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    out = GitFlow(repo).commit_and_push("staged", True, "feat: c")
    assert out.ok, out.error
    assert out.pushed, out.error  # 首推无 upstream → --set-upstream 重试成功
    # bare 侧确实收到 main 分支
    heads = git(bare, "show-ref", "--heads").stdout
    assert "refs/heads/main" in heads
    assert out.remote_url == str(bare)  # 本地路径原样
    assert out.commit_url is None  # 非知名托管不拼网址


def test_push_failure_surfaced_but_commit_kept(repo_with_bare_origin, monkeypatch):
    repo, _ = repo_with_bare_origin
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    # 把 origin 指向不存在的本地路径 → push 失败
    git(repo, "remote", "set-url", "origin", "/nonexistent/origin.git")
    out = GitFlow(repo).commit_and_push("staged", True, "feat: c")
    assert out.ok  # commit 已完成
    assert not out.pushed
    assert out.error and "推送失败" in out.error
    assert out.commit_sha == git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# PR（gh）：假脚本注入 PATH —— monkeypatch 可控
# ---------------------------------------------------------------------------


@pytest.fixture
def github_repo(repo):
    git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    return repo


def fake_gh(tmp_path, monkeypatch, script_body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(script_body, encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return gh


def test_create_pr_extracts_url_from_output(github_repo, tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    # --fill 路径：URL 在 stdout
    gh_script = tmp_path / "bin" / "gh"
    gh_script.write_text(
        "#!/bin/sh\necho 'Creating pull request for main into master in owner/repo'\n"
        "echo 'https://github.com/owner/repo/pull/7'\n",
        encoding="utf-8",
    )
    out = GitFlow(github_repo).create_pr()
    assert out.ok, out.error
    assert out.pr_url == "https://github.com/owner/repo/pull/7"


def test_create_pr_title_and_body_passed_as_argv(github_repo, tmp_path, monkeypatch):
    # 假 gh 把收到的 argv 落盘，验证 --title/--body 为独立元素（无 shell 拼接）
    marker = tmp_path / "argv.log"
    fake_gh(
        tmp_path,
        monkeypatch,
        "#!/bin/sh\nfor a in \"$@\"; do echo \"$a\" >> " + shquote(str(marker)) + "; done\n"
        "echo 'https://github.com/owner/repo/pull/9'\n",
    )
    out = GitFlow(github_repo).create_pr("feat: 手动标题", "正文第一行\n第二行")
    assert out.ok, out.error
    assert out.pr_url == "https://github.com/owner/repo/pull/9"
    argv_lines = marker.read_text().splitlines()
    assert argv_lines[:5] == ["pr", "create", "--title", "feat: 手动标题", "--body"]
    assert "正文第一行\n第二行" in marker.read_text() or "第二行" in argv_lines


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def test_create_pr_gh_missing_degrades(github_repo, monkeypatch):
    # 只屏蔽 gh（git 仍可用）：monkeypatch 静态探测，不动 PATH
    monkeypatch.setattr(GitFlow, "gh_path", staticmethod(lambda: None))
    out = GitFlow(github_repo).create_pr()
    assert not out.ok
    assert "gh 不在 PATH" in (out.error or "")
    assert out.hint


def test_create_pr_non_github_remote(repo_with_bare_origin):
    repo, _ = repo_with_bare_origin
    out = GitFlow(repo).create_pr()
    assert not out.ok
    assert "不是 GitHub" in (out.error or "")


def test_create_pr_no_remote(repo, monkeypatch):
    monkeypatch.setattr(GitFlow, "gh_path", staticmethod(lambda: None))
    out = GitFlow(repo).create_pr()
    assert not out.ok
    assert "未配置 origin remote" in (out.error or "")


def test_create_pr_failure_hint_on_auth_error(github_repo, tmp_path, monkeypatch):
    fake_gh(
        tmp_path,
        monkeypatch,
        "#!/bin/sh\necho 'gh auth: please run gh auth login' >&2\nexit 1\n",
    )
    out = GitFlow(github_repo).create_pr()
    assert not out.ok
    assert "创建 PR 失败" in (out.error or "")
    assert out.hint and "登录" in out.hint


def test_preview_can_pr_with_fake_gh(github_repo, tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    payload = GitFlow(github_repo).preview("staged")
    assert payload.ok
    assert payload.canPr is True
    assert payload.remoteKind == "github"


# ---------------------------------------------------------------------------
# 子进程环境：Provider Key 不下发（仓库 hook 读不到；独立评审中危修复）
# ---------------------------------------------------------------------------


def test_child_env_strips_provider_keys_keeps_git_credentials(repo, monkeypatch):
    """_child_env：ZCODE_PY_API_KEY_* 前缀与 apiKeyEnv 显式名剔除；
    GH_TOKEN / GIT_* 系统凭据照常透传（credential-helper / gh 语义）。"""
    monkeypatch.setenv("ZCODE_PY_API_KEY_GLM", "sk-test-glm-not-a-real-credential")
    monkeypatch.setenv("ZCODE_PY_API_KEY_OPENROUTER", "sk-test-or-not-a-real-credential")
    monkeypatch.setenv("MY_EXPLICIT_KEY", "sk-test-explicit-not-a-real-credential")
    monkeypatch.setenv("GH_TOKEN", "gh-token-test-marker")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -oTest=marker")
    flow = GitFlow(
        repo,
        api_key_env={"glm": "ZCODE_PY_API_KEY_GLM", "custom": "MY_EXPLICIT_KEY"},
    )
    child = flow._child_env()
    assert "ZCODE_PY_API_KEY_GLM" not in child
    assert "ZCODE_PY_API_KEY_OPENROUTER" not in child  # 前缀兜底（未映射的 provider）
    assert "MY_EXPLICIT_KEY" not in child
    assert child["GH_TOKEN"] == "gh-token-test-marker"
    assert child["GIT_SSH_COMMAND"] == "ssh -oTest=marker"
    assert "PATH" in child  # 环境仍是继承拷贝，不是白名单重建


def test_commit_hook_cannot_read_provider_keys(repo, monkeypatch):
    """攻击路径回归：pre-commit hook 以 git 子进程环境执行——Provider Key
    （前缀名 + apiKeyEnv 显式名）必须不在其中，系统凭据变量仍在。"""
    key_value = "sk-test-hook-not-a-real-credential"
    explicit_value = "sk-test-explicit-not-a-real-credential"
    monkeypatch.setenv("ZCODE_PY_API_KEY_GLM", key_value)
    monkeypatch.setenv("MY_EXPLICIT_KEY", explicit_value)
    monkeypatch.setenv("GH_TOKEN", "gh-token-test-marker")
    # 固定 hooksPath 到仓库自身（防全局 core.hooksPath 覆盖让 hook 不跑）
    git(repo, "config", "core.hooksPath", ".git/hooks")
    captured = repo / "hook-env.txt"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nenv > " + shquote(str(captured)) + "\n", encoding="utf-8"
    )
    hook.chmod(0o755)
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    out = GitFlow(
        repo, api_key_env={"glm": "ZCODE_PY_API_KEY_GLM", "custom": "MY_EXPLICIT_KEY"}
    ).commit_and_push("staged", False, "feat: hook env")
    assert out.ok, out.error
    env_dump = captured.read_text()
    assert key_value not in env_dump
    assert explicit_value not in env_dump
    assert "ZCODE_PY_API_KEY" not in env_dump  # 前缀整体不出现
    assert "MY_EXPLICIT_KEY" not in env_dump
    assert "gh-token-test-marker" in env_dump  # GH_TOKEN 透传不受影响
    assert "PATH=" in env_dump  # 环境是继承拷贝（hook 正常拿到系统环境）


def test_gh_env_strips_provider_keys(github_repo, tmp_path, monkeypatch):
    """gh 子进程同口径：Provider Key 不下发，GH_* 认证变量透传。"""
    key_value = "sk-test-gh-not-a-real-credential"
    monkeypatch.setenv("ZCODE_PY_API_KEY_GLM", key_value)
    monkeypatch.setenv("GH_TOKEN", "gh-token-test-marker")
    marker = tmp_path / "gh-env.txt"
    fake_gh(
        tmp_path,
        monkeypatch,
        "#!/bin/sh\nenv > " + shquote(str(marker)) + "\n"
        "echo 'https://github.com/owner/repo/pull/11'\n",
    )
    out = GitFlow(
        github_repo, api_key_env={"glm": "ZCODE_PY_API_KEY_GLM"}
    ).create_pr()
    assert out.ok, out.error
    env_dump = marker.read_text()
    assert key_value not in env_dump
    assert "ZCODE_PY_API_KEY" not in env_dump
    assert "gh-token-test-marker" in env_dump


def test_service_git_commit_hooks_do_not_see_provider_keys(repo, tmp_home, monkeypatch):
    """服务层接线：init.apiKeyEnv 显式名与默认前缀名都经 _child_env 剔除
    （main.py 把 self._api_key_env 传进 GitFlow）。"""
    key_value = "sk-test-svc-not-a-real-credential"
    monkeypatch.setenv("MY_GLM_KEY", key_value)
    monkeypatch.setenv("ZCODE_PY_API_KEY_ANTHROPIC", "sk-test-anthropic-not-a-real-credential")
    git(repo, "config", "core.hooksPath", ".git/hooks")
    captured = repo / "hook-env.txt"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nenv > " + shquote(str(captured)) + "\n", encoding="utf-8"
    )
    hook.chmod(0o755)
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    harness = GitHarness(repo, api_key_env={"glm": "MY_GLM_KEY"})
    harness.send({
        "type": "git_commit", "repoRoot": str(repo),
        "scope": "staged", "push": False, "message": "feat: svc hook env",
    })
    gitres = harness.last_of("git_result")
    assert gitres is not None and gitres.get("error") is None
    env_dump = captured.read_text()
    assert key_value not in env_dump
    assert "MY_GLM_KEY" not in env_dump
    assert "ZCODE_PY_API_KEY" not in env_dump


# ---------------------------------------------------------------------------
# 服务层（main.py）：帧往返 + repoRoot 白名单
# ---------------------------------------------------------------------------


class GitHarness:
    """直连 ZcodeService（stdout 为 StringIO），workspaceRoots=[repo]。"""

    def __init__(self, repo, api_key_env=None):
        import io

        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter,
            provider_factory=lambda entry, key: FakeProvider(),
        )
        init = {"type": "init", "workspaceRoots": [str(repo)]}
        if api_key_env:
            init["apiKeyEnv"] = api_key_env
        self.send(init)

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def last_of(self, type_name: str):
        frames = [f for f in self.frames() if f.get("type") == type_name]
        return frames[-1] if frames else None


def test_service_git_preview_roundtrip(repo, tmp_home):
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    harness = GitHarness(repo)
    harness.send({
        "type": "control", "command": "git_preview",
        "repoRoot": str(repo), "scope": "staged",
    })
    result = harness.last_of("result")
    assert result is not None and result["command"] == "git_preview"
    payload = json.loads(result["detail"])
    assert payload["ok"] is True
    assert payload["summary"]["files"][0]["path"] == "c.txt"
    assert payload["subject"].startswith("feat: ")
    assert payload["styleNote"]


def test_service_git_preview_whitelist_rejects_outside(repo, tmp_home):
    harness = GitHarness(repo)
    harness.send({
        "type": "control", "command": "git_preview",
        "repoRoot": "/etc", "scope": "all",
    })
    errors = [f for f in harness.frames() if f.get("type") == "error"]
    assert any("workspaceRoots" in f["message"] for f in errors)


def test_service_git_commit_whitelist_reject_returns_git_result(repo, tmp_home):
    harness = GitHarness(repo)
    harness.send({
        "type": "git_commit", "repoRoot": "/definitely/outside",
        "scope": "staged", "push": False, "message": "x",
    })
    gitres = harness.last_of("git_result")
    assert gitres is not None
    # encode 省略值为 None 的字段（protocol.py 约定）→ 用 .get 断言缺省
    assert gitres.get("commitSha") is None
    assert "白名单" in (gitres.get("error") or "")
    assert gitres.get("hint")


def test_service_git_commit_emits_git_result(repo, tmp_home):
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    harness = GitHarness(repo)
    harness.send({
        "type": "git_commit", "repoRoot": str(repo),
        "scope": "staged", "push": False, "message": "feat: svc\n\n- A c.txt",
    })
    gitres = harness.last_of("git_result")
    assert gitres is not None
    assert gitres.get("error") is None
    assert gitres.get("commitSha") == git(repo, "rev-parse", "HEAD").stdout.strip()
    # 无 remote → remoteUrl 缺省、hint 引导补 remote
    assert gitres.get("remoteUrl") is None
    assert gitres.get("hint")


def test_service_git_pr_whitelist_reject(repo, tmp_home):
    harness = GitHarness(repo)
    harness.send({"type": "control", "command": "git_pr", "repoRoot": "/outside"})
    errors = [f for f in harness.frames() if f.get("type") == "error"]
    assert any("workspaceRoots" in f["message"] for f in errors)


def test_service_nested_repo_within_root_allowed(tmp_path, tmp_home):
    """workspace 根之内的嵌套仓库放行（multi-root 白名单语义）。"""
    nested = tmp_path / "ws" / "inner"
    nested.mkdir(parents=True)
    git(nested, "init", "-q", "-b", "main")
    (nested / "n.txt").write_text("n\n", encoding="utf-8")
    git(nested, "add", "n.txt")
    commit(nested, "-q", "-m", "feat: init")
    (nested / "n.txt").write_text("n\nn2\n", encoding="utf-8")
    git(nested, "add", "n.txt")
    harness = GitHarness(tmp_path / "ws")
    harness.send({
        "type": "control", "command": "git_preview",
        "repoRoot": str(nested), "scope": "staged",
    })
    result = harness.last_of("result")
    assert result is not None and result["command"] == "git_preview"
    assert json.loads(result["detail"])["ok"] is True
