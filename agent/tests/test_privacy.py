"""隐私护栏表驱动测试（PLAN §2.6 R15：命中与放行）。

对 path：basename 通配（.env / *.pem / id_rsa*…）+ 路径子串（.ssh/ 等）；
对 URL：路径后缀形似敏感文件（防从公网拉私钥/.env），查询串不参与判定。
"""

from __future__ import annotations

import pytest

from zcode_py.privacy import privacy_warning_for_path, privacy_warning_for_url


# ---------------------------------------------------------------------------
# 路径命中表（read/write/edit 传入的 path 主体）
# ---------------------------------------------------------------------------

PATH_HITS = [
    # .env 家族（basename 通配）
    ".env",
    ".env.local",
    ".env.production",
    "prod.env",                      # *.env
    "config/.env",                   # 目录深处的同名文件也命中（basename 匹配）
    # 证书 / 密钥后缀
    "server.pem",
    "cert.key",
    "client.p12",
    "bundle.pfx",
    "bundle.jks",
    "my.keystore",
    "vault.kdbx",
    "secret.key",
    "secrets.yaml",
    # SSH 私钥命名
    "id_rsa",
    "id_ed25519",
    "/home/u/.ssh/id_rsa",
    # 凭据文件
    "credentials.json",              # credentials*
    "aws.credentials",               # *.credentials
    # 包管理器凭据
    ".npmrc",
    ".netrc",
    ".pypirc",
    # 敏感目录子串
    "~/.ssh/config",
    ".aws/credentials",
    ".gnupg/pubring.kbx",
    ".docker/config.json",
    # Windows 反斜杠路径归一后同样命中
    "C:\\Users\\x\\.ssh\\id_rsa",
]

PATH_PASSES = [
    "README.md",
    "docs/notes.md",
    "src/main.py",
    "src/id_utils.py",               # id_ 前缀≠id_rsa
    "identifier.py",                 # 不匹配 id_rsa*
    "package.json",
    ".gitignore",
    "Makefile",
    "env.d.ts",                      # env 开头≠.env / *.env
]


@pytest.mark.parametrize("path", PATH_HITS)
def test_sensitive_paths_trigger_warning(path):
    warning = privacy_warning_for_path(path)
    assert warning is not None, path
    assert warning.startswith("privacy:")
    # 警告文案里能看到命中的文件名/位置，供权限卡展示
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    assert name in warning or any(needle in path.replace("\\", "/").lower()
                                  for needle in (".ssh/", ".aws/", ".gnupg/", ".docker/"))


@pytest.mark.parametrize("path", PATH_PASSES)
def test_normal_paths_pass_clean(path):
    assert privacy_warning_for_path(path) is None


def test_empty_path_no_warning():
    assert privacy_warning_for_path("") is None


# ---------------------------------------------------------------------------
# URL 命中表（webfetch 主体）
# ---------------------------------------------------------------------------

URL_HITS = [
    "https://example.com/keys/server.pem",
    "https://example.com/certs/bundle.p12",
    "http://example.com/.env",
    "https://example.com/id_ed25519",
    "https://example.com/config/credentials.json?fresh=1",  # 查询串不干扰后缀判定
    "https://example.com/secrets.yaml",
]

URL_PASSES = [
    "https://example.com/page.html",
    "https://example.com/api/data.json",
    "https://example.com/.well-known/security.txt",
    "https://example.com/id_rsa-guide",   # 后缀是 -guide，不是裸 id_rsa
]


@pytest.mark.parametrize("url", URL_HITS)
def test_secret_like_urls_trigger_warning(url):
    warning = privacy_warning_for_url(url)
    assert warning is not None, url
    assert warning.startswith("privacy:")


@pytest.mark.parametrize("url", URL_PASSES)
def test_normal_urls_pass_clean(url):
    assert privacy_warning_for_url(url) is None


def test_empty_url_no_warning():
    assert privacy_warning_for_url("") is None
