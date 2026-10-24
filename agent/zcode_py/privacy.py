"""隐私护栏（PLAN §2.6 R15）：敏感文件模式在 read/webfetch 前触发警告。

不阻断（警告由 UI 决定如何强调；PLAN §6.5 隐私护栏模式列表可在设置页配置），
命中时工具结果事件携带 ``warning`` 标记随 tool_call 帧广播并写入转录。

匹配对 read/write/edit 取文件**路径**（重点看 basename），对 webfetch 取
URL 全串（防止从公网拉私钥/.env）。

设置页接线（R15/R20）：basename 通配模式可经 ``privacyFilePatterns``
（~/.zcode-py/config/settings.json）整体替换——含 ``/`` 的模式额外对完整
归一化路径做 fnmatch（如 ``.ssh/*``）；路径子串与 URL 后缀两类保持内置
兜底不开放编辑（设置页覆盖的是文件名维度）。热更新见
:func:`set_active_file_patterns`，服务层 init 读取 + settings_set 后重放。
"""

from __future__ import annotations

from fnmatch import fnmatch
from urllib.parse import urlparse

# basename 通配模式 + 路径子串模式两类；M2 固定列表，设置页可配置属后续里程碑
PRIVACY_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.kdbx",
    "credentials*",
    "*.credentials",
    ".npmrc",
    ".netrc",
    ".pypirc",
    "secrets.*",
    "secret.key",
)

PRIVACY_PATH_SUBSTRINGS: tuple[str, ...] = (
    ".ssh/",
    ".aws/credentials",
    ".gnupg/",
    ".docker/config.json",
)

PRIVACY_URL_SUFFIXES: tuple[str, ...] = (
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
)

# 活动文件模式（设置页 privacyFilePatterns 热替换；缺省 = 内置清单）
_active_file_patterns: list[str] = list(PRIVACY_BASENAME_PATTERNS)


def set_active_file_patterns(patterns: list[str]) -> None:
    """设置页热应用：整体替换文件名通配模式（空列表 = 关闭该维度警告，
    用户显式选择；路径子串/URL 后缀兜底不受影响）。"""
    global _active_file_patterns
    _active_file_patterns = [str(p) for p in patterns if str(p).strip()]


def reset_active_file_patterns() -> None:
    """恢复内置缺省清单（设置页「恢复默认」/配置缺位时调用）。"""
    set_active_file_patterns(list(PRIVACY_BASENAME_PATTERNS))


def active_file_patterns() -> tuple[str, ...]:
    return tuple(_active_file_patterns)


def privacy_warning_for_path(path: str) -> str | None:
    """文件路径命中敏感模式 → 警告文案；未命中 → None。"""
    if not path:
        return None
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    for pattern in _active_file_patterns:
        # basename 命中；含 / 的模式同时按完整归一化路径匹配（.ssh/* 等）
        if fnmatch(name, pattern) or ("/" in pattern and fnmatch(normalized, pattern)):
            return (
                f"privacy: {name!r} matches sensitive file pattern {pattern!r}; "
                "its contents are likely secrets — think twice before sending "
                "them to the model"
            )
    lowered = normalized.lower()
    for needle in PRIVACY_PATH_SUBSTRINGS:
        if needle in lowered:
            return (
                f"privacy: path {path!r} contains sensitive location {needle!r}; "
                "its contents are likely secrets"
            )
    return None


def privacy_warning_for_url(url: str) -> str | None:
    """URL 命中敏感后缀（从公网拉私钥/凭据文件）→ 警告文案；未命中 → None。"""
    if not url:
        return None
    path = urlparse(url).path.lower()
    # 去掉查询串后再看后缀
    stem = path.split("?", 1)[0]
    for suffix in PRIVACY_URL_SUFFIXES:
        if stem.endswith(suffix):
            return (
                f"privacy: URL path looks like a secret file ({suffix!r}); "
                "verify this is intentional"
            )
    return None


__all__ = [
    "PRIVACY_BASENAME_PATTERNS",
    "PRIVACY_PATH_SUBSTRINGS",
    "PRIVACY_URL_SUFFIXES",
    "active_file_patterns",
    "privacy_warning_for_path",
    "privacy_warning_for_url",
    "reset_active_file_patterns",
    "set_active_file_patterns",
]
