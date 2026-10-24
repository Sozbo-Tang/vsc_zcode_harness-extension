"""全局配置 ~/.zcode-py/config/providers.json 读写（PLAN §3.2）。

安全约束（PLAN §9.2）：本文件**绝不含 API Key**。Key 只从环境变量读取：
显式映射（init.apiKeyEnv 指定的变量名）优先，否则用默认命名
``ZCODE_PY_API_KEY_<PROVIDERID>``（非字母数字转 ``_``、大写）。

首次加载时若文件不存在，写入内置示例档案（glm-5.3 / claude 系 / gpt 系 /
gemini 各一，参数依据 PLAN §3.3 与 docs/research/llm-apis.md）。
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from . import telegraph

CONFIG_VERSION = 1
ENV_PREFIX = "ZCODE_PY_API_KEY"

# 示例档案：四家各一条，字段全部来自 PLAN §3.3 与调研笔记。
# GLM-5.3：Anthropic 兼容端点（决策记录 PLAN §10）；强制思考 → off 档 ok=false
# （thinking disabled 直接报错，调研 §3.3）；1M 上下文 / 128K 输出（调研 §3.3）。
# claude-sonnet-4-5：budget ≥1024 且 < max_tokens，65536 预算实际按 max_tokens
# 钳制（调研 §1.3）。
# gpt-5.2：reasoning_effort 档位型；maxOutput 缺省 → 请求不发 max_tokens
# （新模型对 max_tokens/max_completion_tokens 的接受度不一，交给服务端默认）。
# gemini-2.5-flash：0–24576 可关（thinkingBudget=0），max=-1 动态（调研 §6.2）。
DEFAULT_CONFIG: dict = {
    "version": CONFIG_VERSION,
    "defaultProvider": "glm",
    "providers": [
        {
            "id": "glm",
            "name": "GLM（Anthropic 兼容端点）",
            "protocol": "anthropic",
            "baseUrl": "https://open.bigmodel.cn/api/anthropic",
            "defaultModel": "glm-5.3",
            "models": [
                {
                    "id": "glm-5.3",
                    "label": "GLM-5.3",
                    "contextWindow": 1000000,
                    "maxOutput": 131072,
                    "capabilities": {"vision": False, "tools": True, "thinking": True},
                    "telegraph": {
                        "mode": "toggle",
                        "default": "max",
                        "levels": [
                            {"id": "off", "label": "关闭", "param": {"thinking": {"type": "disabled"}}, "ok": False},
                            {"id": "max", "label": "思考", "param": {"thinking": {"type": "enabled"}}, "ok": True},
                        ],
                    },
                }
            ],
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "protocol": "anthropic",
            "baseUrl": "https://api.anthropic.com",
            "defaultModel": "claude-sonnet-4-5",
            "models": [
                {
                    "id": "claude-sonnet-4-5",
                    "label": "Claude Sonnet 4.5",
                    "contextWindow": 200000,
                    "maxOutput": 65536,
                    "capabilities": {"vision": True, "tools": True, "thinking": True},
                    "telegraph": telegraph.reference_telegraph("anthropic"),
                }
            ],
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "protocol": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "defaultModel": "gpt-5.2",
            "models": [
                {
                    "id": "gpt-5.2",
                    "label": "GPT-5.2",
                    "contextWindow": 400000,
                    "capabilities": {"vision": True, "tools": True, "thinking": True},
                    "telegraph": telegraph.reference_telegraph("openai"),
                }
            ],
        },
        {
            "id": "gemini",
            "name": "Google Gemini",
            "protocol": "gemini",
            "baseUrl": "https://generativelanguage.googleapis.com",
            "defaultModel": "gemini-2.5-flash",
            "models": [
                {
                    "id": "gemini-2.5-flash",
                    "label": "Gemini 2.5 Flash",
                    "contextWindow": 1048576,
                    "maxOutput": 65536,
                    "capabilities": {"vision": True, "tools": True, "thinking": True},
                    "telegraph": telegraph.reference_telegraph("gemini"),
                }
            ],
        },
    ],
}


class ConfigError(Exception):
    """配置文件不可读 / 结构不合法。"""


def home_dir() -> Path:
    env = os.environ.get("ZCODE_PY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".zcode-py"


def config_path() -> Path:
    return home_dir() / "config" / "providers.json"


def save_config(config: dict, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_config(path: Path | None = None) -> dict:
    """读配置；文件不存在时落一份默认示例并返回其拷贝。"""
    target = path or config_path()
    if not target.exists():
        save_config(copy.deepcopy(DEFAULT_CONFIG), target)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {target}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        raise ConfigError(f"config {target} must be an object with a 'providers' array")
    return data


def get_provider_entry(config: dict, provider_id: str | None = None) -> dict | None:
    pid = provider_id or config.get("defaultProvider")
    if not pid:
        return None
    for entry in config.get("providers") or []:
        if isinstance(entry, dict) and entry.get("id") == pid:
            return entry
    return None


def get_model_entry(provider_entry: dict, model_id: str | None = None) -> dict | None:
    mid = model_id or provider_entry.get("defaultModel")
    if not mid:
        return None
    for model in provider_entry.get("models") or []:
        if isinstance(model, dict) and model.get("id") == mid:
            return model
    # 档案缺该模型：给一个按协议兜底的最小档案（id + 参考车钟）
    return {
        "id": mid,
        "capabilities": {"vision": False, "tools": True, "thinking": False},
        "telegraph": telegraph.reference_telegraph(provider_entry.get("protocol") or ""),
    }


def env_var_for(provider_id: str) -> str:
    """默认 Key 环境变量名：ZCODE_PY_API_KEY_<PROVIDERID>。"""
    normalized = re.sub(r"[^A-Za-z0-9]", "_", provider_id).upper()
    return f"{ENV_PREFIX}_{normalized}"


def api_key_for(
    provider_id: str, explicit_env_names: dict[str, str] | None = None
) -> str | None:
    """Key 只从环境变量读（显式映射优先，绝无落盘值）。"""
    if explicit_env_names:
        name = explicit_env_names.get(provider_id)
        if name:
            value = os.environ.get(name)
            if value:
                return value
    return os.environ.get(env_var_for(provider_id))
