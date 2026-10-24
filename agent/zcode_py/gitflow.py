"""一键 Commit & Push（PLAN §2.6 R17 学习式提交 + R22 推送/PR）。

帧流（协议 v1，main.py 接线）：

- ``control git_preview{repoRoot, scope}`` → ``result``（detail JSON =
  ``PreviewPayload``）——diff 统计 + 按仓库风格生成的建议提交信息，UI 预览
  可编辑后再确认
- ``git_commit{repoRoot, scope, push, message}`` → ``git_result``——执行
  add（仅 all 范围）/ commit /（可选）push，回 sha 与可点网址
- ``control git_pr{repoRoot, title?, body?}`` → ``git_result``——UI「创建
  PR」按钮触发 ``gh pr create``（``--fill`` 或 ``--title/--body``），从输出
  抽 PR URL

安全取舍（PLAN §9）：

- 子进程一律**字面量 argv**（``subprocess.run([...])``，无 shell=True、无
  命令拼接；git/gh 各自经 ``_git``/``_gh`` 收口，程序名均为字面量经 PATH
  解析）；用户提供的提交信息经 **stdin**（``git commit -F -``）传入，连
  argv 都不进（顺带避免消息出现在进程列表里）
- 凭据零经手：push 走系统 git（credential-helper 出面），PR 走系统 ``gh``
  （自己的 keyring）；本模块不读不写不转发任何 token，也不设置凭据相关
  环境变量
- 子进程环境剔除宿主注入的 Provider Key（``_child_env``：init.apiKeyEnv
  显式名 + ``ZCODE_PY_API_KEY_*`` 前缀）——commit/push 触发的仓库 hook
  读不到本进程的 LLM Key（与 main.py「不随 bash 子进程环境下发」同一
  不变量）；GH_TOKEN / GIT_* 系统凭据照常透传（credential-helper / gh
  自理认证依赖）
- repoRoot 白名单在服务层（main.py ``_git_repo_within_roots``）——本模块
  只认调用方已放行的路径；测试直接实例化时自行负责
- 提交身份不代配：``git commit`` 用仓库/全局 user.name 与 user.email；
  缺身份时如实报错引导用户配置（不像 checkpoints 内联占位身份——那是
  悬挂对象，这里是会进历史的真提交）

风格学习（R17）：``git log`` 最近 50 条 subject 启发式归纳——conventional
与否（``type(scope): desc`` / ``type: desc`` 占比）、常见 type 前缀、scope
使用习惯、语言（CJK 占比）、句号习惯。建议信息按归纳结果生成：类型推断
（测试文件 → test、纯文档 → docs、新增为主 → feat、删多增少 → refactor、
其余 chore）、scope 取变更集中最有代表性的路径段、描述语言跟随仓库。
建议仅供预览编辑，绝不静默提交。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import ENV_PREFIX

_GIT_TIMEOUT = 15.0
_PUSH_TIMEOUT = 60.0
_PR_TIMEOUT = 30.0
_SUBJECT_MAX = 72
_BODY_FILE_LIMIT = 12
# untracked 行数统计的读取上限（1MB；超限按截断计，二进制不计行）
_UNTRACKED_READ_CAP = 1 << 20

# 固定 argv 片段（模块级常量；用户内容只经 stdin 传递，见模块 docstring；
# -n50 = 风格学习样本上限：最近 50 条 subject）
_LOG_SUBJECT_ARGS = ("log", "-n50", "--pretty=format:%s")
_COMMIT_STDIN_ARGS = ("commit", "-F", "-")
_PR_FILL_ARGS = ("pr", "create", "--fill")

# conventional commits 识别（type 可带 (scope) 与 !）；CJK 判定；PR 网址抽取
_CONVENTIONAL_RE = re.compile(r"^(\w+)(?:\(([^)]+)\))?!?:\s+(.+)$")
_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
_PR_URL_RE = re.compile(r"https?://\S+/(?:pull|merge_requests)/\d+")


@dataclass
class FileStat:
    """单文件变更统计（numstat + name-status 合并视图）。"""

    path: str
    status: str  # A | M | D | R | C | ??
    insertions: int | None = None  # None = 二进制/未知
    deletions: int | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "insertions": self.insertions,
            "deletions": self.deletions,
        }


@dataclass
class DiffSummary:
    files: list[FileStat] = field(default_factory=list)
    untracked: int = 0  # scope=all 时 ls-files --others 收进的条数

    @property
    def insertions(self) -> int:
        return sum(f.insertions or 0 for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions or 0 for f in self.files)

    def to_dict(self) -> dict:
        return {
            "files": [f.to_dict() for f in self.files],
            "insertions": self.insertions,
            "deletions": self.deletions,
            "untracked": self.untracked,
        }


@dataclass
class CommitStyle:
    """git log subject 的启发式归纳（建议信息生成参数）。"""

    conventional_ratio: float = 0.0  # conventional 格式 subject 占比
    types: dict[str, int] = field(default_factory=dict)  # 见过的 type 频次
    scope_ratio: float = 0.0  # conventional 内带 (scope) 的占比
    zh_ratio: float = 0.0  # 描述含 CJK 的 subject 占比
    trailing_period: bool = False  # 仓库习惯 subject 以句号结尾
    samples: int = 0  # 参与归纳的 subject 数

    def to_note(self) -> str:
        """预览卡上的一行风格说明（用户可据此判断建议是否符合惯例）。"""
        if self.samples == 0:
            return "仓库无提交历史，按 conventional 默认建议"
        if self.conventional_ratio >= 0.5:
            top = max(self.types, key=lambda k: self.types[k]) if self.types else "?"
            scope = "带 scope" if self.scope_ratio >= 0.5 else "少带 scope"
            lang = "中文" if self.zh_ratio >= 0.5 else "英文"
            return f"conventional 风格（常用 {top}，{scope}，{lang}描述）"
        lang = "中文" if self.zh_ratio >= 0.5 else "英文"
        return f"自由格式 subject（{lang}描述）"


@dataclass
class PreviewPayload:
    """git_preview 的回执载荷（result.detail JSON）。ok=False 时只填 error。"""

    ok: bool
    repoRoot: str = ""
    scope: str = ""
    summary: dict = field(default_factory=dict)
    subject: str = ""
    body: str = ""
    styleNote: str = ""
    remoteUrl: str | None = None
    remoteKind: str = "none"  # github | gitlab | bitbucket | other | none
    canPr: bool = False  # gh 在 PATH 且 remote 是 github（PR 按钮可见性）
    ghAvailable: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "repoRoot": self.repoRoot,
            "scope": self.scope,
            "summary": self.summary,
            "subject": self.subject,
            "body": self.body,
            "styleNote": self.styleNote,
            "remoteUrl": self.remoteUrl,
            "remoteKind": self.remoteKind,
            "canPr": self.canPr,
            "ghAvailable": self.ghAvailable,
            "error": self.error,
        }


@dataclass
class CommitOutcome:
    """git_commit / git_pr 的执行结果（main.py 映射为 git_result 帧）。"""

    ok: bool
    commit_sha: str | None = None
    remote_url: str | None = None
    commit_url: str | None = None
    pr_url: str | None = None
    pushed: bool = False
    error: str | None = None
    hint: str | None = None  # 降级/后续动作提示（如无 remote、gh 缺失）


class GitFlowError(Exception):
    """git 子进程失败（message 面向用户，含 git 原始 stderr 首行）。"""


def _detail(stderr: str, code: int) -> str:
    """子进程失败详情：stderr 首行，缺省退 exit code。"""
    line = stderr.splitlines()[0] if stderr else ""
    return line if line else f"exit {code}"


class GitFlow:
    """一个仓库根一个实例（repo_root 由服务层白名单放行后传入）。"""

    def __init__(self, repo_root: Path | str, api_key_env: dict[str, str] | None = None):
        self.root = Path(repo_root)
        # providerId → Key 环境变量名（init.apiKeyEnv，服务层传入）——这些
        # 变量是宿主注入本进程的 LLM Key，绝不随 git/gh 子进程下发
        self._api_key_env = dict(api_key_env or {})

    # -- 子进程收口（全模块仅此三处 subprocess；字面量程序名、无 shell）----

    def _child_env(self) -> dict[str, str]:
        """git/gh 子进程环境：os.environ 拷贝剔除 Provider Key 变量。

        剔除口径 = init.apiKeyEnv 显式映射的变量名（含大写变体——Windows
        的 os.environ 键名归大写）+ ``ZCODE_PY_API_KEY`` 前缀（config.
        ENV_PREFIX，宿主注入的默认命名，见 config.env_var_for / 扩展
        secrets.ts）。GH_TOKEN / GIT_* 等系统凭据变量**原样透传**——
        push 的 credential-helper 与 gh 自理认证依赖它们（PLAN §9），
        剥的只是本进程注入的 LLM Provider Key（与 tools/bash.
        sanitize_child_env 的不变量同源：commit/push 触发的仓库 hook
        读不到这些 Key）。
        """
        secret_names = {name for name in self._api_key_env.values() if name}
        secret_upper = {name.upper() for name in secret_names}
        return {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(ENV_PREFIX)
            and k not in secret_names
            and k.upper() not in secret_upper
        }

    def _git(self, args: tuple[str, ...], timeout: float = _GIT_TIMEOUT) -> tuple[int, str, str]:
        """git 子进程：``git -C <root> <args...>``。

        环境走 _child_env：继承系统环境（push 的 credential-helper 依赖），
        但剔除宿主注入的 Provider Key——commit/push 触发的仓库 hook
        （pre-commit / commit-msg / pre-push 等）读不到 LLM Key；
        GH_TOKEN / GIT_* 系统凭据照常透传（PLAN §9）。
        """
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(self.root),
            env=self._child_env(),
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    def _git_stdin(
        self, args: tuple[str, ...], text: str, timeout: float = _GIT_TIMEOUT
    ) -> tuple[int, str, str]:
        """同 _git（环境同 _child_env），但用户文本经 stdin 传入（不进
        argv、不进进程列表）。"""
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(self.root),
            env=self._child_env(),
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    def _gh(
        self, args: tuple[str, ...], timeout: float = _PR_TIMEOUT
    ) -> tuple[int, str, str]:
        """gh 子进程：字面量程序名 + 子命令（cwd=仓库根定位仓库）。

        环境走 _child_env（Provider Key 剔除）。gh 的认证（keyring / 环境
        里的 GH_*）由 gh 自理，本模块不经手。
        """
        if self.gh_path() is None:
            return 127, "", "gh not found in PATH"
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(self.root),
            env=self._child_env(),
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    # -- 仓库探测 ------------------------------------------------------------

    def is_repo(self) -> bool:
        try:
            code, out, _ = self._git(("rev-parse", "--show-toplevel"))
        except (OSError, subprocess.TimeoutExpired):
            return False
        return code == 0 and bool(out)

    def head_sha(self) -> str | None:
        try:
            code, out, _ = self._git(("rev-parse", "HEAD"))
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out if code == 0 and out else None

    # -- R17 风格学习 ---------------------------------------------------------

    def learn_style(self) -> CommitStyle:
        """最近 50 条 subject 启发式归纳（无历史/无仓库 → 全默认值）。"""
        style = CommitStyle()
        try:
            code, out, _ = self._git(_LOG_SUBJECT_ARGS)
        except (OSError, subprocess.TimeoutExpired):
            return style
        if code != 0 or not out:
            return style
        subjects = [s.strip() for s in out.splitlines() if s.strip()]
        style.samples = len(subjects)
        if not subjects:
            return style
        conventional = 0
        scoped = 0
        zh = 0
        periods = 0
        for subject in subjects:
            m = _CONVENTIONAL_RE.match(subject)
            if m:
                conventional += 1
                style.types[m.group(1)] = style.types.get(m.group(1), 0) + 1
                if m.group(2):
                    scoped += 1
                if _CJK_RE.search(m.group(3)):
                    zh += 1
                if m.group(3).endswith("."):
                    periods += 1
            else:
                if _CJK_RE.search(subject):
                    zh += 1
                if subject.endswith("."):
                    periods += 1
        style.conventional_ratio = conventional / len(subjects)
        style.scope_ratio = scoped / conventional if conventional else 0.0
        style.zh_ratio = zh / len(subjects)
        style.trailing_period = periods * 2 >= len(subjects)
        return style

    # -- diff 范围 ------------------------------------------------------------

    def diff_summary(self, scope: str) -> DiffSummary:
        """staged = ``git diff --cached``；all = ``git diff HEAD`` + untracked。

        untracked 文件计入 files（status "??"，行数按文件内容统计——读不到
        /二进制/超限则不计行，只保留条目）。
        """
        if scope not in ("staged", "all"):
            raise GitFlowError(f"scope 非法（应为 staged 或 all）: {scope!r}")
        summary = DiffSummary()
        base = ("--cached",) if scope == "staged" else ("HEAD",)
        try:
            code, numstat, err = self._git(("diff",) + base + ("--numstat",))
            if code != 0:
                raise GitFlowError(f"差异统计失败(numstat): {_detail(err, code)}")
            code2, names, err2 = self._git(("diff",) + base + ("--name-status",))
            if code2 != 0:
                raise GitFlowError(f"差异统计失败(name-status): {_detail(err2, code2)}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitFlowError(f"仓库工具不可用: {exc}") from exc
        # rename 形如 "R100\told -> new"，取 new 端
        status_by_path: dict[str, str] = {}
        for line in names.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status_by_path[parts[-1].strip()] = parts[0][0]
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            ins, dele, path = parts
            summary.files.append(
                FileStat(
                    path=path,
                    status=status_by_path.get(path, "M"),
                    insertions=None if ins == "-" else int(ins),
                    deletions=None if dele == "-" else int(dele),
                )
            )
        if scope == "all":
            try:
                code3, others, err3 = self._git(
                    ("ls-files", "--others", "--exclude-standard")
                )
                if code3 != 0:
                    raise GitFlowError(f"未跟踪文件枚举失败: {_detail(err3, code3)}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GitFlowError(f"仓库工具不可用: {exc}") from exc
            for path in others.splitlines():
                if not path:
                    continue
                summary.untracked += 1
                summary.files.append(
                    FileStat(path=path, status="??", **self._untracked_counts(path))
                )
        return summary

    def _untracked_counts(self, rel_path: str) -> dict:
        """untracked 文件行数统计（读失败/二进制/超限 → 不计行）。"""
        target = self.root / rel_path
        try:
            if target.stat().st_size > _UNTRACKED_READ_CAP:
                return {"insertions": None, "deletions": None}
            data = target.read_bytes()
        except OSError:
            return {"insertions": None, "deletions": None}
        if b"\0" in data[:8192]:
            return {"insertions": None, "deletions": None}
        return {"insertions": len(data.decode("utf-8", "replace").splitlines()), "deletions": 0}

    # -- 建议信息生成 ----------------------------------------------------------

    def suggest_message(self, style: CommitStyle, summary: DiffSummary) -> tuple[str, str]:
        """按风格 + 变更集生成建议 (subject, body)——仅建议，UI 可编辑。"""
        zh = style.zh_ratio >= 0.5
        verb_zh, verb_en = self._dominant_verb(summary)
        scope = self._suggest_scope(summary)
        extra = len(summary.files) - 1
        if zh:
            desc = verb_zh + scope if scope else verb_zh.strip()
            if extra > 0:
                desc += f"（等 {len(summary.files)} 个文件）"
        else:
            desc = (verb_en + " " + scope).strip()
            if extra > 0:
                desc += f" (and {extra} more files)"
        if not style.trailing_period and desc.endswith("."):
            desc = desc[:-1]
        subject = desc[:_SUBJECT_MAX]
        if style.conventional_ratio >= 0.5 or style.samples == 0:
            ctype = self._suggest_type(summary)
            if style.scope_ratio >= 0.3 and scope:
                subject = f"{ctype}({scope}): {subject}"
            else:
                subject = f"{ctype}: {subject}"
        body_lines = []
        for f in summary.files[:_BODY_FILE_LIMIT]:
            counts = ""
            if f.insertions is not None or f.deletions is not None:
                counts = f" (+{f.insertions or 0} −{f.deletions or 0})"
            body_lines.append(f"{f.status} {f.path}{counts}")
        if len(summary.files) > _BODY_FILE_LIMIT:
            body_lines.append(f"... {len(summary.files) - _BODY_FILE_LIMIT} more files")
        return subject, "\n".join(body_lines)

    @staticmethod
    def _dominant_verb(summary: DiffSummary) -> tuple[str, str]:
        """(中文动词, 英文动词)：按新增/删除/修改主导推断。"""
        statuses = [f.status for f in summary.files]
        if statuses and all(s in ("A", "??") for s in statuses):
            return "新增 ", "add"
        if statuses and all(s == "D" for s in statuses):
            return "删除 ", "remove"
        return "更新 ", "update"

    @staticmethod
    def _suggest_scope(summary: DiffSummary) -> str:
        """scope 启发式：单文件 → 去后缀名；多文件 → 最深公共目录名。"""
        paths = [Path(f.path) for f in summary.files]
        if not paths:
            return ""
        if len(paths) == 1:
            return paths[0].stem or paths[0].name
        common = paths[0]
        for p in paths[1:]:
            while common != Path(".") and common not in p.parents:
                common = common.parent
        name = common.name or str(common)
        return "" if name in ("", ".") else name

    @staticmethod
    def _suggest_type(summary: DiffSummary) -> str:
        """type 启发式（conventional 风格时的建议前缀）。

        .txt 不算文档（代码旁的 txt 常见，误判 docs 反而离谱）；仅 .md/.rst。
        """
        paths = [f.path for f in summary.files]
        if paths and all(
            "test" in p or p.startswith(("tests/", "spec/")) for p in paths
        ):
            return "test"
        if paths and all(p.endswith((".md", ".rst")) for p in paths):
            return "docs"
        if summary.files and all(f.status in ("A", "??") for f in summary.files):
            return "feat"
        if summary.deletions > summary.insertions:
            return "refactor"
        if summary.insertions > summary.deletions:
            return "feat"
        return "chore"

    # -- R22 remote / 网址 -----------------------------------------------------

    def remote_info(self) -> tuple[str | None, str]:
        """(remote URL, host kind)。知名托管 → https 网页根；本地路径/未知
        host → 原样 URL（展示推送目的地，但不拼 commit 网址）；无 origin →
        (None, "none")。"""
        try:
            code, url, _ = self._git(("remote", "get-url", "origin"))
        except (OSError, subprocess.TimeoutExpired):
            return None, "none"
        if code != 0 or not url:
            return None, "none"
        web, kind = normalize_remote_url(url)
        if web is not None:
            return web, kind
        return (url if kind == "other" else None), kind

    def commit_url(self, sha: str) -> str | None:
        """commit 网址 = remote URL 规范化 + sha 拼接（仅知名托管）。"""
        web, kind = self.remote_info()
        if web is None:
            return None
        if kind == "github":
            return web + "/commit/" + sha
        if kind == "gitlab":
            return web + "/-/commit/" + sha
        if kind == "bitbucket":
            return web + "/commits/" + sha
        return None  # 本地路径/未知托管：不拼网址，UI 降级展示 sha

    @staticmethod
    def gh_path() -> str | None:
        return shutil.which("gh")

    # -- 预览 -----------------------------------------------------------------

    def preview(self, scope: str) -> PreviewPayload:
        """git_preview 回执：diff 统计 + 风格学习 + 建议信息 + remote 能力。

        任何 git 层失败 → ok=False + error（不抛错——UI 显示降级文案）。
        """
        payload = PreviewPayload(ok=False, repoRoot=str(self.root), scope=scope)
        if scope not in ("staged", "all"):
            payload.error = f"scope 非法（应为 staged 或 all）: {scope!r}"
            return payload
        if not self.is_repo():
            payload.error = f"不是 git 仓库: {self.root}"
            return payload
        try:
            summary = self.diff_summary(scope)
            style = self.learn_style()
        except GitFlowError as exc:
            payload.error = str(exc)
            return payload
        subject, body = self.suggest_message(style, summary)
        remote_url, remote_kind = self.remote_info()
        gh = self.gh_path()
        payload.ok = True
        payload.summary = summary.to_dict()
        payload.subject = subject
        payload.body = body
        payload.styleNote = style.to_note()
        payload.remoteUrl = remote_url
        payload.remoteKind = remote_kind
        payload.ghAvailable = gh is not None
        payload.canPr = gh is not None and remote_kind == "github"
        # ok=True 前提下的降级说明（无 remote / 非 GitHub / 无 gh → UI 文案）
        if remote_kind == "none":
            payload.styleNote += "；无 origin remote（推送/PR 不可用）"
        elif remote_kind != "github":
            payload.styleNote += "；remote 非 GitHub（PR 不可用）"
        elif gh is None:
            payload.styleNote += "；gh 不在 PATH（PR 不可用）"
        return payload

    # -- 执行（R17 commit + R22 push/PR）--------------------------------------

    def commit_and_push(
        self, scope: str, push: bool, message: str | None
    ) -> CommitOutcome:
        """add（仅 all）→ commit →（可选）push。

        message 为空时按风格现场生成建议（正常路径：UI 已让用户编辑过预览
        建议）；git identity 未配置时如实报错引导配置（不代配身份）。
        push 失败不影响已完成的 commit（outcome.ok 仍 True，error 记推送
        失败原因——UI 分开展示）。
        """
        if scope not in ("staged", "all"):
            return CommitOutcome(ok=False, error=f"scope 非法: {scope!r}")
        if not self.is_repo():
            return CommitOutcome(ok=False, error=f"不是 git 仓库: {self.root}")
        text = (message or "").strip()
        if not text:
            try:
                style = self.learn_style()
                summary = self.diff_summary(scope)
                subject, body = self.suggest_message(style, summary)
            except GitFlowError as exc:
                return CommitOutcome(ok=False, error=f"无法生成建议信息: {exc}")
            text = subject if not body else subject + "\n\n" + body
        try:
            if scope == "all":
                code, _, err = self._git(("add", "-A"))
                if code != 0:
                    return CommitOutcome(
                        ok=False, error=f"暂存失败: {_detail(err, code)}"
                    )
            # 用户消息经 stdin（-F -）：不进 argv、不进进程列表
            code, _, err = self._git_stdin(_COMMIT_STDIN_ARGS, text)
            if code != 0:
                hint = None
                if (
                    "Please tell me who you are" in err
                    or "user.email" in err
                    or "useConfigOnly" in err
                    or "email address" in err
                ):
                    hint = "先配置提交身份（user.name / user.email）再提交"
                return CommitOutcome(
                    ok=False, error=f"提交失败: {_detail(err, code)}", hint=hint
                )
        except subprocess.TimeoutExpired:
            return CommitOutcome(ok=False, error="提交超时")
        except OSError as exc:
            return CommitOutcome(ok=False, error=f"仓库工具不可用: {exc}")
        sha = self.head_sha()
        remote_url, remote_kind = self.remote_info()
        outcome = CommitOutcome(
            ok=True,
            commit_sha=sha,
            remote_url=remote_url,
            commit_url=self.commit_url(sha) if sha else None,
        )
        if remote_kind == "none":
            # 无 remote：commit 已成功，推送/PR 能力缺失进 hint（UI 降级提示）
            outcome.hint = "未配置 origin remote，未推送（补 remote 后可推送）"
            return outcome
        if not push:
            return outcome
        outcome.error, outcome.hint, outcome.pushed = self._push()
        return outcome

    def _push(self) -> tuple[str | None, str | None, bool]:
        """推送；无 upstream 时自动以 set-upstream 重试一次（git 语义）。"""
        try:
            code, _, err = self._git(("push",), timeout=_PUSH_TIMEOUT)
            if code == 0:
                return None, None, True
            if "no upstream" in err or "set-upstream" in err:
                code2, _, err2 = self._git(
                    ("push", "--set-upstream", "origin", "HEAD"),
                    timeout=_PUSH_TIMEOUT,
                )
                if code2 == 0:
                    return None, "已设置 upstream 并推送", True
                return (f"推送失败: {_detail(err2, code2)}", None, False)
            return (f"推送失败: {_detail(err, code)}", None, False)
        except subprocess.TimeoutExpired:
            return ("推送超时", None, False)
        except OSError as exc:
            return (f"仓库工具不可用: {exc}", None, False)

    def create_pr(self, title: str | None = None, body: str | None = None) -> CommitOutcome:
        """``gh pr create --fill``（或 --title/--body）→ 从输出抽 PR URL。

        gh 不在 PATH / 非 GitHub remote / 无 remote → ok=False + 引导文案。
        凭据与认证完全由 gh 自理（PLAN §9：插件不经手 token）。title/body
        经 ``_gh`` 以独立 argv 元素传递（无 shell、无拼接）。
        """
        sha = self.head_sha()
        remote_url, remote_kind = self.remote_info()
        outcome = CommitOutcome(
            ok=False,
            commit_sha=sha,
            remote_url=remote_url,
            commit_url=self.commit_url(sha) if sha else None,
        )
        if remote_kind == "none":
            outcome.error = "未配置 origin remote，无法创建 PR"
            outcome.hint = "先补 remote（或用 gh 建仓）再重试"
            return outcome
        if remote_kind != "github":
            outcome.error = f"remote 不是 GitHub，无法创建 PR: {remote_url}"
            return outcome
        if self.gh_path() is None:
            outcome.error = "gh 不在 PATH，无法创建 PR"
            outcome.hint = "安装 GitHub CLI 并完成登录后重试"
            return outcome
        if title:
            # 显式标题（可选正文）；默认 --fill 用刚提交的 message（预览可编辑）
            args = ("pr", "create", "--title", title, "--body", body) if body else (
                "pr", "create", "--title", title
            )
        else:
            args = _PR_FILL_ARGS
        try:
            code, out, err = self._gh(args)
        except subprocess.TimeoutExpired:
            outcome.error = "创建 PR 超时"
            return outcome
        except OSError as exc:
            outcome.error = f"gh 不可用: {exc}"
            return outcome
        combined = out + "\n" + err
        m = _PR_URL_RE.search(combined)
        if code == 0 and m:
            outcome.ok = True
            outcome.pr_url = m.group(0)
            return outcome
        hint = None
        if "gh auth" in combined or "not logged in" in combined:
            hint = "先完成 GitHub CLI 登录"
        elif "already exists" in combined:
            hint = "该分支已有 PR——输出里通常带现有 PR 链接"
        outcome.error = f"创建 PR 失败: {_detail(err or out, code)}"
        outcome.hint = hint
        return outcome


def normalize_remote_url(url: str) -> tuple[str | None, str]:
    """remote URL → (https 网页根, host kind)。

    支持 scp 风格 ``git@github.com:o/r.git``、ssh://（含端口）、https://、
    git://；.git 后缀剥掉。本地路径 / 未知 host → (None, "other")。
    """
    url = url.strip()
    if not url:
        return None, "none"
    m = re.match(r"^(?:ssh://)?git@([^:/]+)(?::(\d+))?[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if m:
        return _web_root(m.group(1), m.group(3))
    m = re.match(r"^(?:ssh|git)://(?:git@)?([^:/]+)(?::(\d+))?/(.+)$", url)
    if m:
        return _web_root(m.group(1), _strip_git_suffix(m.group(3)))
    m = re.match(r"^https?://([^/]+)/(.+)$", url)
    if m:
        return _web_root(m.group(1), _strip_git_suffix(m.group(2)))
    return None, "other"


def _strip_git_suffix(path: str) -> str:
    return path.removesuffix(".git")


def _web_root(host: str, repo_path: str) -> tuple[str | None, str]:
    """host 归类（自建 GitHub Enterprise 视为 other——gh 也未必认识）。"""
    kind = {"github.com": "github", "gitlab.com": "gitlab",
            "bitbucket.org": "bitbucket"}.get(host.lower())
    if kind is None:
        return None, "other"
    return f"https://{host.lower()}/{repo_path}", kind


__all__ = [
    "CommitOutcome",
    "CommitStyle",
    "DiffSummary",
    "FileStat",
    "GitFlow",
    "GitFlowError",
    "PreviewPayload",
    "normalize_remote_url",
]
