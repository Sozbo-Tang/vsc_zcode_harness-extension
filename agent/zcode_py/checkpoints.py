"""检查点 / 回滚（PLAN §2.6 R30）：每条 user_message 处理前 git 快照。

机制：``git stash create`` —— 它只**创建**一个悬挂 commit 对象（树 = 当前
工作区状态），不写 stash 引用、不动工作区、不碰 index，对用户完全不可见；
快照 sha 与消息 id 由调用方写入转录并广播 ``checkpoint`` 事件。

- 无本地变更时 ``git stash create`` 输出为空 → 退回 ``HEAD``（工作区本就
  干净，HEAD 即快照）。
- 回滚 = ``git checkout <sha> -- .``（按字面执行"checkout 快照"）：把全部
  已跟踪文件恢复到快照树。已知边界（记录在案）：快照**不含 untracked
  文件**（stash create 语义），回滚也不会删除快照之后新增的 untracked
  文件——不误删数据，属保守取舍。
- 无 git 仓库 / git 不可用 / 空仓库（无任何提交）：优雅降级——返回
  ok=False + 原因，由服务层广播降级说明而非抛错（PLAN §8 检查点单测
  覆盖 untracked 与无仓库两分支）。
- 快照命令带 ``-c user.name/-c user.email``：仅本次调用内联身份，避免
  机器没配 git identity 时 stash create 造 commit 失败（不落任何配置）。

确认语义：rollback 帧本身就是用户的显式动作（UI「回滚到这」按钮带确认
对话框），核侧收到即执行——协议 v1 无二次确认往返（PLAN §5 rollback 只
有 checkpointId 一个字段）。
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 10.0


@dataclass
class SnapshotResult:
    ok: bool
    checkpoint_id: str | None = None
    sha: str | None = None
    message_id: str | None = None
    note: str | None = None  # ok=True 时为补充说明（如 "clean tree, HEAD used"）


@dataclass
class RollbackResultData:
    ok: bool
    detail: str | None = None


class CheckpointManager:
    """一个仓库根一个实例。repo_root 缺省时视为"无仓库"（降级路径）。"""

    def __init__(self, repo_root: Path | str | None):
        self.root = Path(repo_root) if repo_root else None
        self._toplevel: Path | None = None
        self._degraded_reason: str | None = None
        self._resolved = False
        if self.root is not None:
            self._resolve_repo()

    # -- 仓库探测 ------------------------------------------------------------

    def _run(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        proc = subprocess.run(
            ["git", "-C", str(cwd or self._toplevel or self.root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    def _resolve_repo(self) -> None:
        assert self.root is not None
        if not self.root.exists():
            self._degraded_reason = f"workspace root {self.root} does not exist"
            self._resolved = True
            return
        try:
            code, out, _ = self._run("rev-parse", "--show-toplevel", cwd=self.root)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._degraded_reason = f"git unavailable: {exc}"
            self._resolved = True
            return
        if code != 0 or not out:
            self._degraded_reason = f"not a git repository: {self.root}"
            self._resolved = True
            return
        self._toplevel = Path(out)
        self._resolved = True

    @property
    def available(self) -> bool:
        return self._toplevel is not None

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    # -- 快照 / 回滚 ----------------------------------------------------------

    def snapshot(self, message_id: str) -> SnapshotResult:
        """处理 user_message 前调用。永不抛错——失败走降级返回值。"""
        if not self.available:
            return SnapshotResult(
                ok=False,
                message_id=message_id,
                note=self._degraded_reason or "checkpoints unavailable",
            )
        assert self._toplevel is not None
        try:
            # stash create：只造 commit 对象；无本地变更时输出空串
            code, out, err = self._run(
                "-c", "user.name=zcode-py",
                "-c", "user.email=zcode-py@localhost.invalid",
                "stash", "create",
            )
            sha: str | None = out if code == 0 and out else None
            note = None
            if sha is None:
                if code != 0:
                    # 空仓库：git 报 "You do not have the initial commit yet"
                    detail = err.splitlines()[0] if err else f"exit {code}"
                    return SnapshotResult(
                        ok=False, message_id=message_id,
                        note=f"git stash create failed: {detail}",
                    )
                code2, head, _ = self._run("rev-parse", "HEAD")
                if code2 != 0 or not head:
                    return SnapshotResult(
                        ok=False, message_id=message_id,
                        note="empty repository (no commits to snapshot)",
                    )
                sha = head
                note = "clean worktree; HEAD used as snapshot"
        except subprocess.TimeoutExpired:
            return SnapshotResult(ok=False, message_id=message_id, note="git snapshot timed out")
        except OSError as exc:
            return SnapshotResult(ok=False, message_id=message_id, note=f"git failed: {exc}")
        return SnapshotResult(
            ok=True,
            checkpoint_id="ck_" + uuid.uuid4().hex[:10],
            sha=sha,
            message_id=message_id,
            note=note,
        )

    def rollback(self, sha: str) -> RollbackResultData:
        """checkout 快照：``git checkout <sha> -- .``（恢复全部已跟踪文件）。"""
        if not self.available:
            return RollbackResultData(
                ok=False, detail=self._degraded_reason or "checkpoints unavailable"
            )
        # sha 必须像 git 对象名，防命令注入（子进程本就 argv 传参，双保险）
        if not _is_sha_like(sha):
            return RollbackResultData(ok=False, detail=f"invalid checkpoint sha {sha!r}")
        try:
            code, out, err2 = self._run("cat-file", "-e", f"{sha}^{{commit}}")
            if code != 0:
                return RollbackResultData(
                    ok=False, detail=f"snapshot object {sha[:12]} no longer exists "
                    "(garbage-collected); cannot roll back"
                )
            code, out, err2 = self._run("checkout", sha, "--", ".")
        except subprocess.TimeoutExpired:
            return RollbackResultData(ok=False, detail="git checkout timed out")
        except OSError as exc:
            return RollbackResultData(ok=False, detail=f"git failed: {exc}")
        if code != 0:
            return RollbackResultData(ok=False, detail=f"git checkout failed (exit {code}): {out}")
        return RollbackResultData(
            ok=True,
            detail="tracked files restored; untracked files were left untouched",
        )


def _is_sha_like(value: str) -> bool:
    return bool(value) and all(ch in "0123456789abcdef" for ch in value.lower()) and 7 <= len(value) <= 40


__all__ = ["CheckpointManager", "RollbackResultData", "SnapshotResult"]
