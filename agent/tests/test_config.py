"""config：providers.json 读写 / 示例档案 / Key 只走环境变量（PLAN §3.2、§9.2）。"""

from __future__ import annotations

import json

from zcode_py import config as C


def test_seeds_default_config_when_missing(tmp_home):
    path = tmp_home / "config" / "providers.json"
    loaded = C.load_config()
    assert path.exists()
    assert loaded["defaultProvider"] == "glm"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == loaded


def test_default_config_has_four_example_profiles():
    ids = {p["id"] for p in C.DEFAULT_CONFIG["providers"]}
    assert ids == {"glm", "anthropic", "openai", "gemini"}
    glm = next(p for p in C.DEFAULT_CONFIG["providers"] if p["id"] == "glm")
    assert glm["protocol"] == "anthropic"
    assert glm["baseUrl"] == "https://open.bigmodel.cn/api/anthropic"
    assert glm["defaultModel"] == "glm-5.3"
    claude = next(p for p in C.DEFAULT_CONFIG["providers"] if p["id"] == "anthropic")
    assert claude["defaultModel"] == "claude-sonnet-4-5"
    openai = next(p for p in C.DEFAULT_CONFIG["providers"] if p["id"] == "openai")
    assert openai["defaultModel"] == "gpt-5.2"
    gemini = next(p for p in C.DEFAULT_CONFIG["providers"] if p["id"] == "gemini")
    assert gemini["defaultModel"] == "gemini-2.5-flash"


def _keys_and_strings(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield "key", key
            yield from _keys_and_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys_and_strings(item)
    elif isinstance(node, str):
        yield "str", node


def test_no_credentials_anywhere_in_default_config():
    """安全约束：配置与示例绝不含 Key——键名不许是凭据字段，值不许长得像凭据。
    （budget_tokens 是 Anthropic 官方参数名，不属于凭据。）"""
    credential_keys = {"apikey", "api_key", "key", "secret", "password", "token", "authorization"}
    for kind, value in _keys_and_strings(C.DEFAULT_CONFIG):
        if kind == "key":
            assert value.lower() not in credential_keys, f"credential-like field: {value}"
        else:
            lowered = value.lower()
            assert not lowered.startswith(("sk-", "ghp_", "xai-", "ak1")), f"credential-like value: {value!r}"
            assert "bearer " not in lowered
            assert "apikey" not in lowered and "api_key" not in lowered


def test_load_invalid_config_raises(tmp_home):
    path = tmp_home / "config" / "providers.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    try:
        C.load_config()
        raise AssertionError("expected ConfigError")
    except C.ConfigError:
        pass


def test_save_load_round_trip(tmp_home):
    custom = {"version": 1, "defaultProvider": "p1", "providers": [
        {"id": "p1", "protocol": "openai", "baseUrl": "http://localhost:11434/v1",
         "auth": "none", "defaultModel": "qwen3"}
    ]}
    C.save_config(custom)
    loaded = C.load_config()
    assert loaded == custom
    assert C.get_provider_entry(loaded)["id"] == "p1"
    assert C.get_model_entry(loaded["providers"][0])["id"] == "qwen3"


def test_get_model_entry_fallback_uses_reference_telegraph():
    entry = {"id": "x", "protocol": "gemini", "defaultModel": "gemini-9-flash"}
    model = C.get_model_entry(entry, "gemini-9-flash")
    assert model["id"] == "gemini-9-flash"
    assert model["telegraph"]["mode"] == "budget"


def test_env_var_name_normalization():
    assert C.env_var_for("glm") == "ZCODE_PY_API_KEY_GLM"
    assert C.env_var_for("my-provider") == "ZCODE_PY_API_KEY_MY_PROVIDER"


def test_api_key_only_from_env(tmp_home, monkeypatch):
    monkeypatch.setenv("ZCODE_PY_API_KEY_GLM", "test-key-not-a-real-credential")
    assert C.api_key_for("glm") == "test-key-not-a-real-credential"
    monkeypatch.delenv("ZCODE_PY_API_KEY_GLM")
    assert C.api_key_for("glm") is None
    # 显式映射优先（init.apiKeyEnv）
    monkeypatch.setenv("MY_CUSTOM_VAR", "test-key-2-not-real")
    assert C.api_key_for("glm", {"glm": "MY_CUSTOM_VAR"}) == "test-key-2-not-real"
    assert C.api_key_for("other", {"glm": "MY_CUSTOM_VAR"}) is None
