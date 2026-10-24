"""工作区语言检测（R21，PLAN §2.5 / §6.4 环境面板）：特征文件 → 语言 →
工具链并行探测。

- 特征文件表：pyproject.toml→Python、package.json→Node/TS、Cargo.toml→Rust、
  go.mod→Go、pom.xml/build.gradle→Java、CMakeLists.txt→C/C++。只显示工作区
  命中的语言（决策记录 PLAN §10），比全机扫描更有行动价值。
- 扫描：每个工作区根 ≤3 层深度、剪枝 node_modules/.git/target 等重型目录、
  每 marker 最多记 20 条路径（防巨型 monorepo 撑爆帧）。
- 工具链探测：命中语言后并行（ThreadPoolExecutor）跑版本命令（每条字面量
  argv、5s 超时）；语言出现但主工具链缺失 → ``missing=True`` + 一句安装指引
  （黄牌，PLAN §6.4）。
- ``nvm`` 是 shell 函数而非二进制——按 ``NVM_DIR`` / ``~/.nvm`` 存在性判定，
  不起子进程。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._exec import TIMEOUT_S, completed_output, run_argv  # noqa: F401 (TIMEOUT_S re-export for tests)

# R21 特征文件表：marker 文件名 → 语言 id
MARKER_LANGUAGES: dict[str, str] = {
    "pyproject.toml": "python",
    "package.json": "nodejs",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "CMakeLists.txt": "cpp",
}

LANGUAGE_LABELS: dict[str, str] = {
    "python": "Python",
    "nodejs": "Node.js/TypeScript",
    "rust": "Rust",
    "go": "Go",
    "java": "Java",
    "cpp": "C/C++",
}

# 语言展示顺序：按特征文件表声明序（确定性输出，测试可断言）
_LANG_ORDER: list[str] = list(dict.fromkeys(MARKER_LANGUAGES.values()))

_MAX_DEPTH = 3
_MAX_PATHS_PER_MARKER = 20
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "build",
    "dist",
    "out",
    ".gradle",
    "vendor",
}

# 每语言工具链规格：primary 缺失即 missing（黄牌）；managers 进 packageManagers；
# extras 是包管理器之外的补充工具（conda/gradle/cc 等）。
_TOOLCHAINS: dict[str, dict] = {
    "python": {
        "primary": ["python3", "--version"],
        "managers": [["pip3", "--version"]],
        "extras": [["conda", "--version"]],
        "installHint": (
            "install Python 3 from https://www.python.org/downloads/ "
            "(or `brew install python`, or use miniconda)"
        ),
    },
    "nodejs": {
        "primary": ["node", "--version"],
        "managers": [["npm", "--version"]],
        "extras": [],  # nvm 是 shell 函数：按 NVM_DIR/~/.nvm 存在性特判
        "installHint": (
            "install Node.js LTS from https://nodejs.org/ "
            "(or `brew install node`; nvm: https://github.com/nvm-sh/nvm)"
        ),
    },
    "rust": {
        "primary": ["rustc", "--version"],
        "managers": [["cargo", "--version"]],
        "extras": [],
        "installHint": "install the Rust toolchain via https://rustup.rs",
    },
    "go": {
        "primary": ["go", "version"],
        "managers": [],
        "extras": [],
        "installHint": "install Go from https://go.dev/dl/ (or `brew install go`)",
    },
    "java": {
        "primary": ["java", "-version"],  # java -version 输出在 stderr，_exec 已兜底
        "managers": [["mvn", "--version"]],
        "extras": [["gradle", "--version"]],
        "installHint": (
            "install a JDK from https://adoptium.net (or `brew install openjdk`); "
            "Maven: https://maven.apache.org/download.cgi"
        ),
    },
    "cpp": {
        "primary": ["cmake", "--version"],
        "managers": [],
        "extras": [["cc", "--version"]],
        "installHint": (
            "install a C/C++ toolchain (Xcode Command Line Tools via "
            "`xcode-select --install`, or build-essential on Debian/Ubuntu) "
            "and CMake from https://cmake.org/download/"
        ),
    },
}


def _scan_markers(workspace_roots: list) -> dict[str, list[str]]:
    """marker 文件名 → 命中的绝对路径列表（≤3 层、剪枝、每 marker 封顶）。"""
    found: dict[str, list[str]] = {}
    for root in workspace_roots or []:
        base = Path(str(root)).expanduser()
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= _MAX_DEPTH:
                dirnames[:] = []  # 当前层照查，但不再下钻
            for name in filenames:
                if name in MARKER_LANGUAGES:
                    bucket = found.setdefault(name, [])
                    if len(bucket) < _MAX_PATHS_PER_MARKER:
                        bucket.append(str(Path(dirpath) / name))
    return found


def _nvm_present() -> bool:
    if os.environ.get("NVM_DIR"):
        return True
    return (Path.home() / ".nvm").exists()


def _shorten_version(output: str | None) -> str | None:
    """首行化 + 去掉 pip 的安装路径尾巴 + 截断，供 UI/注入直读。"""
    if not output:
        return None
    first = output.splitlines()[0].strip()
    if first.startswith("pip") and " from " in first:
        first = first.split(" from ", 1)[0].strip()
    if len(first) > 80:
        first = first[:77] + "..."
    return first or None


def probe_workspace_langs(workspace_roots: list) -> list[dict]:
    """工作区语言 + 工具链并行探测；返回 env_info 帧 workspaceLangs 直通的
    条目列表（结构见环境面板 §6.4：语言/marker/版本/包管理器/缺失黄牌）。"""
    found = _scan_markers(workspace_roots)
    by_lang: dict[str, dict] = {}
    for marker, paths in found.items():
        lang = MARKER_LANGUAGES[marker]
        slot = by_lang.setdefault(lang, {"markers": [], "paths": []})
        if marker not in slot["markers"]:
            slot["markers"].append(marker)
        slot["paths"].extend(paths)
    if not by_lang:
        return []

    # 汇总全部子进程探测任务（字面量 argv），一次性并行执行；key 带语言前缀
    jobs: list[tuple[str, str, list[str]]] = []  # (lang, kind:name, argv)
    for lang in by_lang:
        spec = _TOOLCHAINS[lang]
        jobs.append((lang, "primary:", spec["primary"]))
        for argv in spec["managers"]:
            jobs.append((lang, "manager:" + argv[0], argv))
        for argv in spec.get("extras") or []:
            jobs.append((lang, "extra:" + argv[0], argv))
    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
        futures = {
            (lang, key): pool.submit(run_argv, argv) for lang, key, argv in jobs
        }
        for (lang, key), fut in futures.items():
            results[lang + "|" + key] = _shorten_version(completed_output(fut.result()))

    entries: list[dict] = []
    for lang in sorted(by_lang, key=_LANG_ORDER.index):
        spec = _TOOLCHAINS[lang]
        slot = by_lang[lang]
        version = results.get(lang + "|primary:")
        managers = {}
        for argv in spec["managers"]:
            out = results.get(lang + "|manager:" + argv[0])
            if out is not None:
                managers[argv[0]] = out
        extras = {}
        for argv in spec.get("extras") or []:
            out = results.get(lang + "|extra:" + argv[0])
            if out is not None:
                extras[argv[0]] = out
        if lang == "nodejs" and _nvm_present():
            extras["nvm"] = "installed"
        missing = version is None
        entries.append(
            {
                "language": lang,
                "label": LANGUAGE_LABELS.get(lang, lang),
                "markers": list(slot["markers"]),
                "markerPaths": list(slot["paths"]),
                "missing": missing,
                "version": version,
                "packageManagers": managers,
                "extras": extras,
                "installHint": spec["installHint"] if missing else None,
            }
        )
    return entries
