"""权限规则引擎 + 危险命令黑名单（PLAN §2.6 R29、§9.3、§9.5）。

决策管线（固定优先级，黑名单压过一切）：

1. **DANGER_PATTERN 黑名单**：bash 命令命中危险模式（rm -rf / git push
   --force / sudo / dd …）→ ``ask``，ruleId=``danger.*``。优先级高于白名单、
   高于会话内"总是允许"——即使规则命中也永远逐次人工确认（PLAN §9.3）。
2. **只读工具**：read/glob/grep/todo/webfetch/websearch 免批放行（PLAN §9.5
   只约束 写/删/命令；web 类出网由 netguard.py 的 SSRF 校验兜底）。
3. **白名单规则**：形如 ``"tool: pattern"``（``bash: git *``、``write: **/*.md``），
   前缀/通配/全等匹配（zcode 语义参照 docs/research/zcode-semantics.md §3：
   prefix:* / wildcard / 全等，匹配主体按工具取 command/path/url 入参字段）。
4. **会话内总是允许**：用户在权限卡选 always 后按工具记忆（进程生命周期内）。
5. 默认 → ``ask``（写/删/命令默认询问，PLAN §9.5）。

deny 不由引擎产生——默认态是"问"，拒绝来自用户的 permission_response。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Callable

# 决策取值：allow=放行；ask=需要用户确认（广播 permission_request）；
# deny=引擎侧直接拒绝（M8 计划模式只读档——不弹卡，结构化拒绝回灌）
ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# 计划模式三档（M8，PLAN §6.1 控制条「只读计划」档）
# build=默认（写/命令询问）；plan=只读（写档工具一律结构化拒绝，不弹卡）；
# yolo=写档免询问，但 DANGER 黑名单仍强制逐次确认（PLAN §9.3 不变量）
PERMISSION_MODES = ("build", "plan", "yolo")

# 只读工具集合（免批）。webfetch/websearch 属只读出网：SSRF host 校验 +
# 隐私护栏在工具层生效，出网本身不视为副作用（与 PLAN §9.5 一致）。
# M8：search_history（L4 检索本会话转录）与 notebook_read（读 .ipynb 元数据）
# 同属免批只读。
READONLY_TOOLS = frozenset(
    {"read", "glob", "grep", "todo", "webfetch", "websearch", "search_history", "notebook_read"}
)

# DANGER 黑名单扫描的工具面（PLAN §9.3 黑名单优先级高于一切）：bash 工具之外，
# background（M9 后台任务）与 cron_task（M9 定时任务命令动作）都以
# {"command": str} 载荷执行 shell 命令串——同受黑名单管辖（独立评审高危 #1/#2：
# 这两个入口曾完全绕过引擎）。
DANGER_SCANNED_TOOLS = frozenset({"bash", "background", "cron_task"})

# 各工具规则匹配的入参字段（zcode 语义：匹配主体按工具取具体入参）
_SUBJECT_FIELD: dict[str, str] = {
    "bash": "command",
    "write": "path",
    "edit": "path",
    "read": "path",
    "webfetch": "url",
    "websearch": "query",
}

# ---------------------------------------------------------------------------
# 危险命令黑名单（R29）：命中即强制逐次人工确认，优先级最高。
# 模式作用于 bash 完整命令字符串（先经 _normalize 归一化）。
#
# zcode 用 shell AST 解包解释器调用；M2 用「归一化 + 正则」近似，兜住
# 实测过的绕过形态（对不上的宁可多问，绝不漏放，PLAN §9.3）：
# - 换行与 ; & | 同为命令分隔符（bash 语义）→ 锚点类 [\n\r;&|]；
# - shell 会剥掉命令词/旗标外的引号（'rm' "-rf" /）→ 锚点与旗标簇容忍引号；
# - 解释器/包装器展开：sh|bash|… -c "…"、eval "…"、exec … 归一化成独立
#   命令段后再匹配（嵌套包裹循环展开）；
# - 同串 VAR=value 赋值回填 $VAR 后再匹配（CMD="rm -rf /"; $CMD、
#   D="-rf"; rm $D /）；
# - 命令替换 $(…) 与 `…` 的体内是会被 shell 真实执行的代码 → 体内按
#   完整命令复检；命令位本身是动态词（$(echo rm) -rf /）且静态参数带
#   rm 递归+强制旗标 → danger.rm_dynamic。
# 引号内的纯文本参数（echo "rm -rf /"、git commit -m '…'）不会被 shell
# 展开，不触发——误报控制见 tests/test_permissions.py QUOTED_TEXT_SAFE。
# ---------------------------------------------------------------------------

# 命令位锚点：串首或命令分隔符（换行/;/&/|）之后；容忍命令词外的引号
_CMD_POS = r"(?:^['\"]?|[\n\r;&|]\s*['\"]?)"

DANGER_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "danger.rm_root",
        "删除根/家目录",
        re.compile(
            _CMD_POS
            + r"rm\s+(?:['\"]?-[a-zA-Z]+['\"]?\s+)*+"
            + r"(?:/(?:\s|$)|--no-preserve-root|\*(?:\s|$)|~(?:\s|$))"
        ),
    ),
    (
        "danger.git_push_force",
        "强推改写远端历史 git push --force",
        # --force-with-lease 是防覆盖设计，刻意不列入
        re.compile(r"\bgit\s+push\s+[^|;&\n\r]*(?:--force\b(?!-with-lease)|(?<![\w-])-f\b)"),
    ),
    (
        "danger.sudo",
        "特权执行 sudo/su/doas",
        re.compile(
            _CMD_POS + r"(?:sudo|doas)\b['\"]?"
            r"|\bsu\s+(?:-\w+\s+)?\w+\s*$"
            r"|\bsu\s+root\b"
        ),
    ),
    (
        "danger.dd_disk",
        "裸写块设备 dd",
        re.compile(_CMD_POS + r"dd\s+[^|;&\n\r]*\bof=/dev/"),
    ),
    (
        "danger.disk_format",
        "格式化/分区 mkfs/fdisk/parted",
        re.compile(
            _CMD_POS
            + r"(?:mkfs(?:\.\w+)?|mkswap|fdisk|sfdisk|parted|diskutil\s+eraseDisk|format\s+[a-zA-Z]:)\b"
        ),
    ),
    (
        "danger.git_reset_hard",
        "硬重置工作区 git reset --hard",
        re.compile(r"\bgit\s+reset\s+[^|;&\n\r]*--hard\b"),
    ),
    (
        "danger.git_clean_force",
        "强删未跟踪文件 git clean -f",
        re.compile(r"\bgit\s+clean\s+[^|;&\n\r]*(?:--force\b|-[a-zA-Z]*f)"),
    ),
    (
        "danger.chmod_r_777",
        "递归全开放权限 chmod -R 777",
        re.compile(
            r"\bchmod\s+[^|;&\n\r]*-[a-zA-Z]*R[a-zA-Z]*\s*[^|;&\n\r]*\s777\b"
            r"|\bchmod\s+-R\s+777\b"
        ),
    ),
    (
        "danger.pipe_to_shell",
        "网络内容直接执行 curl|sh / wget|bash",
        re.compile(
            r"\b(?:curl|wget|fetch|nc|ncat|socat)\b[^|;&\n\r]*\|\s*(?:ba|z|fi|da|k)?sh\b[^;&\n\r]*"
            r"|\|\s*(?:ba|z|fi|da|k)?sh\s*$"
        ),
    ),
    (
        "danger.power_control",
        "关机重启 shutdown/reboot/halt",
        re.compile(_CMD_POS + r"(?:shutdown|reboot|halt|poweroff)\b"),
    ),
    (
        "danger.kill_wildcard",
        "批量杀进程 kill -9 1 / pkill -9 -f 宽匹配",
        re.compile(
            _CMD_POS + r"kill\s+-9\s+(?:1\b|-1\b|\*)"
            r"|" + _CMD_POS + r"pkill\s+-9\b"
        ),
    ),
)

DangerHit = tuple[str, str, re.Pattern[str]]

# ---------------------------------------------------------------------------
# 归一化（M2 对 zcode AST 解包的正则近似）
# ---------------------------------------------------------------------------

# sh 家族带 -c 的引号脚本体（zsh -e -c '…' 等旗标前缀一并容忍）
_INTERP_WRAP_RE = re.compile(
    r"\b(?:sh|bash|zsh|dash|ksh|ash|csh|tcsh|fish)\b"
    r"(?:\s+-{1,2}[\w=:-]+)*\s+-c\s+(\"[^\"]*\"|'[^']*')"
)
_EVAL_WRAP_RE = re.compile(
    _CMD_POS + r"eval\s+(?:-{1,2}[\w=:-]+\s+)*(\"[^\"]*\"|'[^']*')"
)
# exec 丢弃当前 shell 直接执行后续命令：等价替换成命令分隔符
_EXEC_RE = re.compile(_CMD_POS + r"exec\s+")
# 同串内的 VAR=value 赋值（命令位；值可带引号）
_ASSIGN_RE = re.compile(
    r"(?:^|[\n\r;&|]\s*)([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s;&|\n\r]*)"
)
# 命令替换体：$(…) 与 `…`（体内代码会被 shell 真实执行）
_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
# 命令位上的动态词：命令替换或变量展开（无法静态解析出命令名）
_DYN_CMD_RE = re.compile(
    r"(?:^|[\n\r;&|]\s*)"
    r"(\$\([^()]*\)|`[^`]*`|\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*))"
    r"(?=[\s;&|\n\r)]|$)"
)
_SEG_SEP_RE = re.compile(r"[\n\r;&|]")
# 旗标 token（静态参数里扫描 rm 递归+强制信号用）
_FLAG_TOKEN_RE = re.compile(
    r"(?:^|\s)['\"]?(--recursive|--force|-[A-Za-z]+)['\"]?(?=\s|$)"
)
_MAX_NORMALIZE_PASSES = 5
_MAX_MATCH_DEPTH = 3


def _unwrap(match: re.Match[str]) -> str:
    """把 ``bash -c "X"`` / ``eval 'X'`` 的 X 变成独立命令段 `` ; X ; ``。"""
    inner = match.group(1)
    if len(inner) >= 2 and inner[0] in "\"'" and inner[-1] == inner[0]:
        inner = inner[1:-1]
    return f" ; {inner} ; "


def _resolve_assignments(command: str) -> str:
    """同串内 ``VAR=value`` 赋值回填 ``$VAR``（含 ``${VAR}``）。

    只回填本串内显式赋值且值里无嵌套展开的变量；环境变量等解析不了的
    保持原样，交给动态词规则兜底（宁多问不漏问）。
    """
    values: dict[str, str] = {}
    for m in _ASSIGN_RE.finditer(command):
        name, raw = m.group(1), m.group(2)
        if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
            raw = raw[1:-1]
        values[name] = raw
    if not values:
        return command
    out = command
    for name in sorted(values, key=len, reverse=True):
        value = values[name]
        if not value or "$" in value:
            continue
        out = re.sub(
            r"\$(?:\{%s\}|%s\b)" % (re.escape(name), re.escape(name)),
            lambda _m, _v=value: _v,
            out,
        )
    return out


def _normalize(command: str) -> str:
    """循环归一化：解包解释器包裹/eval/exec + 赋值回填（到不动点或上限）。"""
    out = command
    for _ in range(_MAX_NORMALIZE_PASSES):
        prev = out
        out = _INTERP_WRAP_RE.sub(_unwrap, out)
        out = _EVAL_WRAP_RE.sub(_unwrap, out)
        out = _EXEC_RE.sub(" ; ", out)
        out = _resolve_assignments(out)
        if out == prev:
            break
    return out


def _segment_rm_signal(segment: str) -> bool:
    """静态参数段是否携带 rm 递归+强制旗标信号（-rf/-fr/-r -f/长旗标齐备）。"""
    has_r = has_f = False
    for m in _FLAG_TOKEN_RE.finditer(segment):
        flag = m.group(1)
        if flag == "--recursive":
            has_r = True
        elif flag == "--force":
            has_f = True
        else:
            low = flag[1:].lower()
            if "r" in low:
                has_r = True
            if "f" in low:
                has_f = True
    return has_r and has_f


# rm 递归+强制的旗标簇分析（纯正则写不稳：-rf/-fr/-r --force/--recursive -f
# 组合爆炸，改为抓出 rm 后紧跟的旗标 token 再判定 r 与 f 是否齐备；
# 旗标可带引号——shell 组 argv 时会剥掉 'rm' "-rf" / 里的引号）
_RM_CMD_RE = re.compile(
    r"(?:^['\"]?|[\n\r;&|]\s*['\"]?|\b(?:sudo|env|nohup|xargs|nice)['\"]?\s+)"
    r"rm((?:\s+['\"]?-{1,2}[\w=:-]+['\"]?)+)"
)
_RM_DANGER = ("danger.rm_recursive_force", "递归强制删除 rm -rf")
_RM_DYNAMIC = (
    "danger.rm_dynamic",
    "命令位为命令替换/变量展开且参数带 rm 递归+强制旗标",
    _DYN_CMD_RE,
)


def _rm_flags_hit(command: str) -> DangerHit | None:
    for match in _RM_CMD_RE.finditer(command):
        tokens = [t.strip("\"'") for t in match.group(1).split()]
        has_r = has_f = False
        for token in tokens:
            body = token.lstrip("-")
            if body == "recursive":
                has_r = True
            elif body == "force":
                has_f = True
            elif not token.startswith("--") and body.isalpha():
                low = body.lower()
                if "r" in low:
                    has_r = True
                if "f" in low:
                    has_f = True
        if has_r and has_f:
            return (_RM_DANGER[0], _RM_DANGER[1], _RM_CMD_RE)
    return None


def _match_patterns(command: str) -> DangerHit | None:
    rm_hit = _rm_flags_hit(command)
    if rm_hit is not None:
        return rm_hit
    for entry in DANGER_PATTERNS:
        if entry[2].search(command):
            return entry
    return None


def _match_danger(command: str, depth: int) -> DangerHit | None:
    if depth > _MAX_MATCH_DEPTH:
        return None
    normalized = _normalize(command)
    hit = _match_patterns(normalized)
    if hit is not None:
        return hit
    # 命令替换体会被 shell 真实执行：体内按完整命令递归复检
    for m in _SUBST_RE.finditer(normalized):
        body = (m.group(1) or m.group(2) or "").strip()
        if body:
            hit = _match_danger(body, depth + 1)
            if hit is not None:
                return hit
    # 命令位是动态词（命令替换/变量展开）且静态参数携带 rm 递归+强制信号
    for m in _DYN_CMD_RE.finditer(normalized):
        rest = normalized[m.end():]
        sep = _SEG_SEP_RE.search(rest)
        segment = rest if sep is None else rest[: sep.start()]
        if _segment_rm_signal(segment):
            return _RM_DYNAMIC
    return None


def match_danger(command: str) -> DangerHit | None:
    """命令命中危险黑名单则返回 (ruleId, 描述, pattern)，未命中返回 None。"""
    if not command:
        return None
    return _match_danger(command, 0)


# ---------------------------------------------------------------------------
# 规则解析与匹配："tool: pattern" → (tool, pattern)
# ---------------------------------------------------------------------------


def parse_rule(rule: str) -> tuple[str, str] | None:
    """解析 ``"tool: pattern"``；非法（无冒号/空工具名）返回 None。"""
    if ":" not in rule:
        return None
    tool, _, pattern = rule.partition(":")
    tool = tool.strip()
    pattern = pattern.strip()
    if not tool or not pattern:
        return None
    return tool, pattern


def rule_matches(pattern: str, subject: str) -> bool:
    """规则匹配：``prefix:*`` 前缀 / 含通配符走 fnmatch / 其余全等
    （zcode 语义参照 docs/research/zcode-semantics.md §3）。"""
    if pattern.endswith(":*"):
        return subject.startswith(pattern[:-2])
    if any(ch in pattern for ch in "*?["):
        return fnmatch(subject, pattern)
    return subject == pattern


def subject_for(tool: str, args: dict[str, Any]) -> str:
    """取该工具规则匹配的主体字段值（bash→command、write→path…）。"""
    key = _SUBJECT_FIELD.get(tool)
    value = args.get(key) if key else None
    return value if isinstance(value, str) else ""


def always_rule_for(tool: str, args: dict[str, Any]) -> str:
    """用户选 always 时记忆的会话规则：

    - bash → 首个命令词（命令族，如 ``git *``——危险命令仍会被黑名单拦截）
    - 带路径/URL 的工具 → 精确值（不做目录级泛化，宁多问不漏问）
    - 其余 → ``*``
    """
    subject = subject_for(tool, args)
    if tool == "bash" and subject:
        head = subject.split()[0]
        return f"{head} *"
    if subject:
        return subject
    return "*"


@dataclass
class PermissionCheck:
    """引擎判定结果（不包含用户答复）。"""

    outcome: str  # ALLOW | ASK
    ruleId: str
    reason: str
    danger: bool = False
    subject: str = ""


@dataclass
class PermissionEngine:
    """规则引擎实例（每个会话一个，会话内 always 规则随进程消失）。"""

    allow_rules: list[str] = field(default_factory=list)
    _session_always: set[tuple[str, str]] = field(default_factory=set)

    def check(self, tool: str, args: dict[str, Any]) -> PermissionCheck:
        subject = subject_for(tool, args)
        # 1. 黑名单最高优先级（PLAN §9.3）：压过白名单与总是允许
        if tool in DANGER_SCANNED_TOOLS:
            command = args.get("command")
            if isinstance(command, str):
                hit = match_danger(command)
                if hit is not None:
                    return PermissionCheck(
                        outcome=ASK,
                        ruleId=hit[0],
                        reason=f"dangerous command ({hit[1]}); always requires "
                        "explicit confirmation",
                        danger=True,
                        subject=subject,
                    )
        # 2. 只读工具免批
        if tool in READONLY_TOOLS:
            return PermissionCheck(
                outcome=ALLOW, ruleId="readonly", reason="read-only tool", subject=subject
            )
        # 3. 白名单
        for rule in self.allow_rules:
            parsed = parse_rule(rule)
            if parsed is None:
                continue
            rule_tool, pattern = parsed
            if rule_tool == tool and rule_matches(pattern, subject):
                return PermissionCheck(
                    outcome=ALLOW,
                    ruleId=f"rule:{rule}",
                    reason=f"matches allow rule {rule!r}",
                    subject=subject,
                )
        # 4. 会话内总是允许
        for rule_tool, pattern in self._session_always:
            if rule_tool == tool and rule_matches(pattern, subject):
                return PermissionCheck(
                    outcome=ALLOW,
                    ruleId=f"session_always:{tool}: {pattern}",
                    reason=f"always allowed this session ({pattern!r})",
                    subject=subject,
                )
        # 5. 默认询问（写/删/命令，PLAN §9.5）
        return PermissionCheck(
            outcome=ASK,
            ruleId="default_ask",
            reason="write/delete/command tools ask by default",
            subject=subject,
        )

    def remember_always(self, tool: str, args: dict[str, Any]) -> str:
        """用户答复 always 后记忆会话规则，返回规则 pattern。"""
        pattern = always_rule_for(tool, args)
        self._session_always.add((tool, pattern))
        return pattern

    def session_rules(self) -> list[str]:
        """当前会话内 always 规则（调试/回显用）。"""
        return sorted(f"{tool}: {pattern}" for tool, pattern in self._session_always)


# ---------------------------------------------------------------------------
# M8 计划模式（PLAN §6.1 / §9.3）：build | plan | yolo 三档包装引擎
# ---------------------------------------------------------------------------

PLAN_MODE_DENY_REASON = (
    "计划模式只读——先退出计划模式（permission mode 切回 build/yolo）再执行写操作"
)


def mode_aware_engine(
    inner: PermissionEngine,
    *,
    tier_lookup: Callable[[str], str | None],
    mode_getter: Callable[[], str],
) -> "ModeAwarePermissionEngine":
    """便捷构造：包一层三档模式语义的引擎（不改变 inner 的规则与记忆）。"""
    return ModeAwarePermissionEngine(
        inner=inner, tier_lookup=tier_lookup, mode_getter=mode_getter
    )


class ModeAwarePermissionEngine(PermissionEngine):
    """三档权限模式（M8）：

    - ``build``：完全委托 inner（默认行为，规则引擎原语义）；
    - ``plan``：tier=write 的工具一律 :data:`DENY`（结构化拒绝，**不弹卡**）；
      readonly 工具照常放行——计划模式是「只读调研」档；
    - ``yolo``：tier=write 的工具免询问放行（ruleId=yolo）；**DANGER 黑名单
      仍强制 ask**（PLAN §9.3：黑名单优先级高于一切白名单与"总是允许"，
      yolo 也不例外）——先算 inner 判定，danger 命中原样返回。

    tier_lookup 返回 None（注册表里没有的工具名）按 write 处理（宁拒/问
    不漏放）；模式值非法时按 build 委托。
    """

    def __init__(
        self,
        *,
        inner: PermissionEngine,
        tier_lookup: Callable[[str], str | None],
        mode_getter: Callable[[], str],
    ):
        # 不经 dataclass 默认构造（allow_rules 由 inner 持有；本类只转发）
        self.inner = inner
        self.tier_lookup = tier_lookup
        self.mode_getter = mode_getter

    def check(self, tool: str, args: dict[str, Any]) -> PermissionCheck:
        mode = self.mode_getter()
        if mode not in PERMISSION_MODES:
            mode = "build"
        if mode == "build":
            return self.inner.check(tool, args)
        inner_check = self.inner.check(tool, args)
        tier = self.tier_lookup(tool)
        is_write = tier != "readonly"  # 未知工具按写档（保守）
        if mode == "plan":
            if is_write:
                return PermissionCheck(
                    outcome=DENY,
                    ruleId="plan_mode",
                    reason=PLAN_MODE_DENY_REASON,
                    subject=inner_check.subject,
                )
            return inner_check
        # yolo：危险命中（inner 判 danger=ask）优先保留；已放行的照旧；其余写档放行
        if inner_check.danger:
            return inner_check
        if inner_check.outcome == ALLOW:
            return inner_check
        if tool == "notebook_run":
            # R29 精神（独立评审中危 #3）：notebook_run 执行任意 Python，危险面
            # 等价 shell——yolo 也保留逐次确认，堵住黑名单只扫 shell 模式的盲区
            return PermissionCheck(
                outcome=ASK,
                ruleId="notebook_guard",
                reason="notebook_run executes arbitrary Python; confirm each run",
                subject=inner_check.subject,
            )
        if is_write:
            return PermissionCheck(
                outcome=ALLOW,
                ruleId="yolo",
                reason="yolo mode: write-tier tools allowed without asking",
                subject=inner_check.subject,
            )
        return inner_check

    def remember_always(self, tool: str, args: dict[str, Any]) -> str:
        return self.inner.remember_always(tool, args)

    def session_rules(self) -> list[str]:
        return self.inner.session_rules()


__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "DANGER_PATTERNS",
    "DangerHit",
    "PERMISSION_MODES",
    "PLAN_MODE_DENY_REASON",
    "ModeAwarePermissionEngine",
    "PermissionCheck",
    "PermissionEngine",
    "READONLY_TOOLS",
    "always_rule_for",
    "match_danger",
    "mode_aware_engine",
    "parse_rule",
    "rule_matches",
    "subject_for",
]
