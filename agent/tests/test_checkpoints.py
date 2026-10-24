"""检查点测试（PLAN §8：untracked 文件、无 git 仓库降级）。

真实 git 仓库在 tmp 内搭建（字面量命令，无动态拼接）。
"""

from __future__ import annotations

import subprocess

import pytest

from zcode_py.checkpoints import CheckpointManager


def git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "init")
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    git(root, "add", "a.txt")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add a")
    return root


def test_clean_tree_snapshot_uses_head(repo):
    mgr = CheckpointManager(repo)
    assert mgr.available
    snap = mgr.snapshot("um_1")
    assert snap.ok
    assert snap.sha == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert snap.note and "HEAD" in snap.note
    # 快照不动工作区、不写 stash 列表
    assert git(repo, "stash", "list").stdout.strip() == ""
    assert git(repo, "status", "--porcelain").stdout.strip() == ""


def test_dirty_tree_snapshot_captures_uncommitted_changes(repo):
    (repo / "a.txt").write_text("v2-uncommitted\n", encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    snap = CheckpointManager(repo).snapshot("um_2")
    assert snap.ok and snap.sha != head
    # stash create 不动工作区
    assert (repo / "a.txt").read_text() == "v2-uncommitted\n"
    assert git(repo, "stash", "list").stdout.strip() == ""
    # 快照树里是脏内容
    shown = git(repo, "show", f"{snap.sha}:a.txt").stdout
    assert shown == "v2-uncommitted\n"


def test_rollback_restores_tracked_files(repo):
    (repo / "a.txt").write_text("v2-uncommitted\n", encoding="utf-8")
    snap = CheckpointManager(repo).snapshot("um_3")
    # 快照之后继续改（模拟 Agent 误改）
    (repo / "a.txt").write_text("v3-ruined\n", encoding="utf-8")
    result = CheckpointManager(repo).rollback(snap.sha)
    assert result.ok, result.detail
    assert (repo / "a.txt").read_text() == "v2-uncommitted\n"


def test_rollback_leaves_untracked_untouched(repo):
    """快照不含 untracked（stash create 语义）：回滚不删它们——保守不误删。"""
    snap = CheckpointManager(repo).snapshot("um_4")
    (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    (repo / "a.txt").write_text("ruined\n", encoding="utf-8")
    result = CheckpointManager(repo).rollback(snap.sha)
    assert result.ok
    assert (repo / "untracked.txt").read_text() == "keep me\n"
    assert (repo / "a.txt").read_text() == "v1\n"  # 已跟踪文件被恢复


def test_snapshot_with_untracked_present_rollback_keeps_it(repo):
    """untracked 在快照**之前**就存在的场景：快照树不含它，回滚后原样保留。

    stash create 只收已跟踪内容——untracked 文件既不入快照、也不会被回滚
    删除（checkpoints.py docstring 记录的保守取舍）。
    """
    (repo / "scratch.txt").write_text("user data\n", encoding="utf-8")
    snap = CheckpointManager(repo).snapshot("um_8")
    assert snap.ok
    # 快照树里没有 scratch.txt（未跟踪不进快照）
    shown = git(repo, "ls-tree", "-r", "--name-only", snap.sha, check=False)
    assert "scratch.txt" not in shown.stdout
    # 快照之后 Agent 误改已跟踪文件
    (repo / "a.txt").write_text("ruined\n", encoding="utf-8")
    result = CheckpointManager(repo).rollback(snap.sha)
    assert result.ok, result.detail
    assert (repo / "a.txt").read_text() == "v1\n"       # 已跟踪恢复
    assert (repo / "scratch.txt").read_text() == "user data\n"  # untracked 幸存


def test_none_root_degrades_without_raising():
    """workspace 缺省（服务未初始化）：降级返回，绝不抛错。"""
    mgr = CheckpointManager(None)
    assert not mgr.available
    snap = mgr.snapshot("um_9")
    assert snap.ok is False and snap.note
    assert mgr.rollback("0" * 40).ok is False


def test_no_git_repo_degrades_gracefully(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = CheckpointManager(plain)
    assert not mgr.available
    snap = mgr.snapshot("um_5")
    assert snap.ok is False
    assert snap.note and "not a git repository" in snap.note
    result = mgr.rollback("deadbeef")
    assert result.ok is False


def test_empty_repository_degrades(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    mgr = CheckpointManager(empty)
    assert mgr.available
    snap = mgr.snapshot("um_6")
    assert snap.ok is False
    # git 原始原因进降级说明（"You do not have the initial commit yet"）
    assert "initial commit" in snap.note


def test_missing_root_degrades(tmp_path):
    mgr = CheckpointManager(tmp_path / "ghost")
    assert not mgr.available
    assert mgr.snapshot("um_7").ok is False


def test_rollback_rejects_invalid_sha(repo):
    result = CheckpointManager(repo).rollback("not-a-sha; rm -rf /")
    assert result.ok is False
    assert "invalid checkpoint sha" in result.detail
