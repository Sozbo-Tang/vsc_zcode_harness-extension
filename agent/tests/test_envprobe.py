"""envprobe 测试（R7/R21）：marker→语言映射、工具链探测打桩、缺工具链黄牌、
hardware 解析、environment_facts 注入格式——全部离线（run_argv 打桩）。"""

from __future__ import annotations

import subprocess

from zcode_py.envprobe import _exec, environment_facts, probe
from zcode_py.envprobe import hardware as hw
from zcode_py.envprobe import langs


def _fake_argv_ok(outputs: dict[str, str]):
    """按首参数返回假版本输出的 run_argv 桩（未命中返回 None = 工具缺失）。"""

    def fake(argv, timeout=None):
        key = argv[0] if argv else ""
        out = outputs.get(key)
        if out is None:
            return None
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    return fake


def test_marker_to_language_mapping():
    assert langs.MARKER_LANGUAGES["pyproject.toml"] == "python"
    assert langs.MARKER_LANGUAGES["package.json"] == "nodejs"
    assert langs.MARKER_LANGUAGES["CMakeLists.txt"] == "cpp"


def test_probe_workspace_langs_found_and_missing(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    monkeypatch.setattr(
        langs, "run_argv",
        _fake_argv_ok({"python3": "Python 3.12.1"}),
    )
    entries = langs.probe_workspace_langs([str(tmp_path)])
    by_lang = {e["language"]: e for e in entries}
    # 命中 Python 且有工具链
    assert by_lang["python"]["missing"] is False
    assert "3.12" in str(by_lang["python"]["version"])
    # Rust：marker 命中但 cargo 不可用 → 黄牌 + 安装指引
    assert by_lang["rust"]["missing"] is True
    assert by_lang["rust"]["installHint"]


def test_probe_workspace_langs_only_hits_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(langs, "run_argv", _fake_argv_ok({}))
    entries = langs.probe_workspace_langs([str(tmp_path)])
    assert entries == []  # 无 marker：不显示任何语言（决策记录 §10）


def test_probe_hardware_darwin(monkeypatch):
    sysctl = {
        "machdep.cpu.brand_string": "Apple M4 Pro",
        "hw.ncpu": "14",
        "hw.memsize": "68719476736",
    }

    def fake_argv(argv, timeout=None):
        key = ".".join(argv[2:])
        out = sysctl.get(key)
        return (
            subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            if out is not None
            else None
        )

    monkeypatch.setattr(hw, "run_argv", fake_argv)
    info = hw._probe_darwin()
    assert info["cpuModel"] == "Apple M4 Pro"
    assert info["cpuCores"] == 14
    assert info["ramTotal"].endswith("GB")
    assert "Apple Silicon" in info["gpuModel"]


def test_environment_facts_format():
    env = {
        "hardware": {"cpuModel": "Apple M4 Pro", "cpuCores": 14,
                     "ramTotal": "64GB", "os": "macOS 26.1", "arch": "arm64"},
        "workspaceLangs": [
            {"language": "python", "label": "Python", "missing": False,
             "version": "3.12.1", "packageManagers": {"pip": "24.0"}},
            {"language": "rust", "label": "Rust", "missing": True,
             "installHint": "brew install rust"},
        ],
    }
    facts = environment_facts(env)
    assert "Apple M4 Pro" in facts
    assert "Python" in facts and "3.12.1" in facts
    assert "Rust" in facts and "MISSING" in facts
    assert len(facts) <= 1500


def test_probe_end_to_end_offline(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    monkeypatch.setattr(_exec, "run_argv", _fake_argv_ok({"go": "go version go1.23.0 darwin/arm64"}))
    env = probe([str(tmp_path)])
    assert set(env.keys()) >= {"hardware", "workspaceLangs"}
    assert any(e["language"] == "go" for e in env["workspaceLangs"])
