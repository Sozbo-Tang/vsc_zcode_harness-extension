"""M7 Skills（R26）：SKILL.md 兼容加载器 + 斜杠命令（zcode 语义）。

按 zcode 的语义（PLAN §1「参照 zcode specs 与工具语义」）：**不注册 `skill`
工具**——已加载 skills 列表（名字 + description，一行一个）注入 system
prompt；用户消息以 ``/<name>`` 开头时由**服务层**展开：读正文拼进该轮消息
前缀，并广播一条 ``result`` 帧注明 skill 展开。``skills_list`` control 命令
返回已加载列表（Result.detail JSON）。

搜索路径（后加载者覆盖同名——**工作区（项目级）覆盖用户级**）：

1. ``<zcode-py home>/skills/<name>/SKILL.md``（config.home_dir，默认
   ``~/.zcode-py``）
2. ``<工作区>/.zcode-py/skills/<name>/SKILL.md``

SKILL.md 格式（zcode 兼容的 frontmatter 子集，无 YAML 依赖）::

    ---
    name: deploy            # 缺省 = 目录名
    description: 部署本服务   # 一行；进 system prompt 注入列表
    model: glm-5.3          # 可选：建议模型（本里程碑只读不强制）
    allowed-tools: bash, read  # 可选：建议工具集（同上）
    ---
    正文：拼进用户消息前缀的提示词模板。

frontmatter 解析是容忍性的：键名 ``allowed_tools`` 与 ``allowed-tools`` 等
价；引号剥除；无 frontmatter 的文件按「目录名 + 空 description」整篇正文
收录。坏文件（读失败/目录名非法）跳过，不拖垮会话。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import home_dir

SKILL_FILENAME = "SKILL.md"
# 目录名（=缺省 skill 名）合法字符：字母数字开头，允许 . _ -
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
# 消息首 token：/<name>
_SLASH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_.\-]*)(\s+|$)")


@dataclass
class Skill:
    name: str
    description: str = ""
    body: str = ""
    path: str = ""
    model: str | None = None
    allowed_tools: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 解析（纯函数，离线可测）
# ---------------------------------------------------------------------------


def _parse_kv(block: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace("_", "-")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            meta[key] = value
    return meta


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``---`` 围起来的头部 → (kv, 正文)。无/不闭合 frontmatter 全文当正文。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return _parse_kv("\n".join(lines[1:i])), "\n".join(lines[i + 1 :]).lstrip("\n")
    return {}, text


def parse_skill_md(text: str, *, fallback_name: str) -> Skill:
    meta, body = parse_frontmatter(text)
    allowed = [
        t.strip()
        for t in (meta.get("allowed-tools") or "").split(",")
        if t.strip()
    ]
    return Skill(
        name=meta.get("name") or fallback_name,
        description=meta.get("description") or "",
        body=body.strip(),
        model=meta.get("model") or None,
        allowed_tools=allowed,
    )


# ---------------------------------------------------------------------------
# 加载（搜索路径：工作区覆盖用户级）
# ---------------------------------------------------------------------------


def skills_dirs(workspace_root: Path | str | None = None, *, home: Path | None = None) -> list[Path]:
    """搜索目录（home 在前、工作区在后——load_skills 后加载者覆盖同名：
    工作区（项目级）skill 优先于用户级）。"""
    dirs: list[Path] = [(home or home_dir()) / "skills"]
    if workspace_root is not None:
        dirs.append(Path(workspace_root).expanduser() / ".zcode-py" / "skills")
    return dirs


def load_skills(
    workspace_root: Path | str | None = None, *, home: Path | None = None
) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for directory in skills_dirs(workspace_root, home=home):
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or not _NAME_RE.match(entry.name):
                continue
            md = entry / SKILL_FILENAME
            if not md.is_file():
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            skill = parse_skill_md(text, fallback_name=entry.name)
            skill.path = str(md)  # 供 UI/转录审计
            skills[skill.name] = skill
    return skills


# ---------------------------------------------------------------------------
# 注入与斜杠展开
# ---------------------------------------------------------------------------


def skills_injection(skills: dict[str, Skill] | None) -> str | None:
    """system prompt 尾部注入块：每个 skill 一行（名字 + description）。"""
    if not skills:
        return None
    lines = [
        f"- /{s.name} — {s.description or '(no description)'}"
        for s in sorted(skills.values(), key=lambda s: s.name)
    ]
    return (
        "\n\n## Skills (slash commands)\n"
        "Start a message with /<name> to invoke a skill; its SKILL.md body "
        "will be expanded into that message.\n" + "\n".join(lines)
    )


def expand_slash(text: str, skills: dict[str, Skill]) -> tuple[str, str, Skill | None]:
    """消息以 ``/<name>`` 开头且命中已加载 skill →

    ``(SKILL.md 正文前缀块, 去掉斜杠 token 的剩余文本, skill)``；
    未命中（非斜杠开头 / 未知名字 / 名字后紧跟非空白字符）→ ``(原文, "", None)``。
    服务层用剩余文本重组该轮消息（斜杠 token 被展开体替换，不重复出现）。
    """
    if not text or not text.startswith("/"):
        return text, "", None
    match = _SLASH_RE.match(text)
    if match is None:
        return text, "", None
    skill = skills.get(match.group(1))
    if skill is None:
        return text, "", None
    rest = text[match.end() :].lstrip()
    prefix = f"## Skill: {skill.name}\n{skill.body}\n## End of skill {skill.name}"
    return prefix, rest, skill


# ---------------------------------------------------------------------------
# 会话级管理器（init 后台扫描；control/展开路径按需同步加载）
# ---------------------------------------------------------------------------


class SkillsManager:
    """线程安全的 skills 快照：init 后台扫描不阻塞首条消息；首次展开时
    按需同步加载（几次文件读，成本可忽略）。"""

    def __init__(self, workspace_root: Path | str | None = None, *, home: Path | None = None):
        self._workspace_root = workspace_root
        self._home = home
        self._lock = threading.Lock()
        self._skills: dict[str, Skill] | None = None

    def ensure_loaded(self) -> dict[str, Skill]:
        with self._lock:
            if self._skills is None:
                self._skills = load_skills(self._workspace_root, home=self._home)
            return self._skills

    def refresh(self) -> dict[str, Skill]:
        with self._lock:
            self._skills = load_skills(self._workspace_root, home=self._home)
            return self._skills

    def loaded_skills(self) -> dict[str, Skill] | None:
        """已加载快照；尚未扫描完返回 None（**不**触发同步加载——供 init 路径
        组装 system prompt 时不阻塞）。"""
        with self._lock:
            return self._skills


__all__ = [
    "Skill",
    "SkillsManager",
    "expand_slash",
    "load_skills",
    "parse_frontmatter",
    "parse_skill_md",
    "skills_dirs",
    "skills_injection",
]
