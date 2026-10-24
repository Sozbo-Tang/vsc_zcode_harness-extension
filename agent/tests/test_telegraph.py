"""车钟档位映射：四模式 / 档案数据驱动 / 降档 / telegraph_state（PLAN §3.3）。"""

from __future__ import annotations

import pytest

from zcode_py import telegraph as T
from zcode_py.config import DEFAULT_CONFIG
from zcode_py.llm.anthropic import build_anthropic_body
from zcode_py.llm.base import LLMRequest


def _model(provider_id: str, model_id: str) -> dict:
    for entry in DEFAULT_CONFIG["providers"]:
        if entry["id"] == provider_id:
            for model in entry.get("models", []):
                if model["id"] == model_id:
                    return model
    raise KeyError(f"{provider_id}/{model_id}")


# ---------------------------------------------------------------------------
# 表驱动：四种协议档案 × 全部规范档位 → translate 期望值（PLAN §3.3 表）
# glm=anthropic 协议 toggle / claude=anthropic budget / openai effort / gemini budget
# None = 该档置灰不可用（translate 返回 None → 适配器不发送思考参数）
# ---------------------------------------------------------------------------

EXPECTED_TRANSLATIONS: dict[tuple[str, str], dict[str, dict | None]] = {
    ("glm", "glm-5.3"): {
        "off": None,  # GLM-5.3 强制思考：off（thinking disabled）置灰
        "min": None,
        "low": None,
        "medium": None,
        "high": None,
        "max": {"thinking": {"type": "enabled"}},
    },
    ("anthropic", "claude-sonnet-4-5"): {
        "off": {"thinking": {"type": "disabled"}},
        "min": {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        "low": {"thinking": {"type": "enabled", "budget_tokens": 8192}},
        "medium": {"thinking": {"type": "enabled", "budget_tokens": 24576}},
        "high": {"thinking": {"type": "enabled", "budget_tokens": 65536}},
        "max": None,  # claude 档案无 max 档
    },
    ("openai", "gpt-5.2"): {
        "off": {"reasoning_effort": "minimal"},
        "min": {"reasoning_effort": "low"},
        "low": {"reasoning_effort": "medium"},
        "medium": {"reasoning_effort": "high"},
        "high": {"reasoning_effort": "xhigh"},
        "max": {"reasoning_effort": "max"},
    },
    ("gemini", "gemini-2.5-flash"): {
        "off": {"thinkingConfig": {"thinkingBudget": 0}},
        "min": {"thinkingConfig": {"thinkingBudget": 2048, "includeThoughts": True}},
        "low": {"thinkingConfig": {"thinkingBudget": 8192, "includeThoughts": True}},
        "medium": {"thinkingConfig": {"thinkingBudget": 16384, "includeThoughts": True}},
        "high": {"thinkingConfig": {"thinkingBudget": 24576, "includeThoughts": True}},
        "max": {"thinkingConfig": {"thinkingBudget": -1, "includeThoughts": True}},
    },
}


@pytest.mark.parametrize("provider_id,model_id", list(EXPECTED_TRANSLATIONS))
def test_translate_table_covers_all_levels(provider_id, model_id):
    expected = EXPECTED_TRANSLATIONS[(provider_id, model_id)]
    assert set(expected) == set(T.CANONICAL_LEVELS)  # 表覆盖全部档位
    profile = T.parse_profile(_model(provider_id, model_id))
    for level in T.CANONICAL_LEVELS:
        assert T.translate(profile, level) == expected[level], f"{model_id}/{level}"


@pytest.mark.parametrize("provider_id,model_id", list(EXPECTED_TRANSLATIONS))
def test_state_payload_lists_all_levels_with_ok_flags(provider_id, model_id):
    expected = EXPECTED_TRANSLATIONS[(provider_id, model_id)]
    profile = T.parse_profile(_model(provider_id, model_id))
    payload = T.state_payload(profile, profile.default_level)
    assert [lv["id"] for lv in payload["levels"]] == list(T.CANONICAL_LEVELS)
    assert profile.default_level in {lv["id"] for lv in payload["levels"] if lv["ok"]}
    for lv in payload["levels"]:
        assert lv["ok"] == (expected[lv["id"]] is not None)
        if lv["ok"]:
            assert lv["param"] == expected[lv["id"]]  # 可用档 param 即翻译结果
    assert payload["paramTranslation"] == (T.translate(profile, payload["current"]) or {})


# ---------------------------------------------------------------------------
# 模型上限截断：anthropic budget_tokens 按模型 maxOutput 钳制（PLAN §3.3
# 「65536 按模型上限截断」——钳制发生在适配器请求体，budget 必须 < max_tokens）
# ---------------------------------------------------------------------------


def test_anthropic_budget_clamped_to_model_cap():
    model = _model("anthropic", "claude-sonnet-4-5")
    profile = T.parse_profile(model)
    param = T.translate(profile, "high")
    assert param == {"thinking": {"type": "enabled", "budget_tokens": 65536}}

    # 档位参数直达适配器：budget 被钳到 maxOutput - 1
    body = build_anthropic_body(
        LLMRequest(model=model["id"], messages=[], thinking=param, max_tokens=model["maxOutput"])
    )
    assert body["thinking"]["budget_tokens"] == model["maxOutput"] - 1

    # 上限更小的模型（如 maxOutput 8192）：high 档同样截断到 8191
    body = build_anthropic_body(
        LLMRequest(model="small-cap", messages=[], thinking=param, max_tokens=8192)
    )
    assert body["thinking"]["budget_tokens"] == 8191


def test_gemini_and_openai_levels_not_clamped_by_max_tokens():
    """截断是 anthropic 协议约束（budget < max_tokens）；gemini/openai 档位
    参数按档案原样透传，不因 maxOutput 变形。"""
    from zcode_py.llm.gemini import build_gemini_body
    from zcode_py.llm.openai import build_openai_body

    g = _model("gemini", "gemini-2.5-flash")
    gparam = T.translate(T.parse_profile(g), "max")
    gbody = build_gemini_body(LLMRequest(model=g["id"], messages=[], thinking=gparam, max_tokens=1024))
    assert gbody["generationConfig"]["thinkingConfig"] == {"thinkingBudget": -1, "includeThoughts": True}

    o = _model("openai", "gpt-5.2")
    oparam = T.translate(T.parse_profile(o), "high")
    obody = build_openai_body(LLMRequest(model=o["id"], messages=[], thinking=oparam, max_tokens=1024))
    assert obody["reasoning_effort"] == "xhigh"


def test_glm_toggle_off_grayed():
    """GLM-5.3 强制思考：off 档（thinking disabled）必须置灰（调研 §3.3）。"""
    profile = T.parse_profile(_model("glm", "glm-5.3"))
    assert profile.mode == "toggle"
    assert T.translate(profile, "off") is None
    off = next(lv for lv in profile.levels if lv.id == "off")
    assert off.ok is False
    assert T.translate(profile, "max") == {"thinking": {"type": "enabled"}}


def test_claude_budget_mode():
    profile = T.parse_profile(_model("anthropic", "claude-sonnet-4-5"))
    assert profile.mode == "budget"
    assert T.translate(profile, "off") == {"thinking": {"type": "disabled"}}
    assert T.translate(profile, "min") == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    assert T.translate(profile, "medium") == {"thinking": {"type": "enabled", "budget_tokens": 24576}}
    assert T.translate(profile, "high") == {"thinking": {"type": "enabled", "budget_tokens": 65536}}
    assert T.translate(profile, "max") is None  # claude 档案未提供 max 档


def test_openai_levels_mode():
    profile = T.parse_profile(_model("openai", "gpt-5.2"))
    assert profile.mode == "levels"
    assert T.translate(profile, "off") == {"reasoning_effort": "minimal"}
    assert T.translate(profile, "max") == {"reasoning_effort": "max"}


def test_gemini_budget_mode():
    profile = T.parse_profile(_model("gemini", "gemini-2.5-flash"))
    assert profile.mode == "budget"
    assert T.translate(profile, "off") == {"thinkingConfig": {"thinkingBudget": 0}}
    assert T.translate(profile, "max") == {"thinkingConfig": {"thinkingBudget": -1, "includeThoughts": True}}


def test_none_mode_all_gray():
    profile = T.parse_profile({"id": "x"})
    assert profile.mode == "none"
    assert all(not lv.ok for lv in profile.levels)
    assert T.translate(profile, "high") is None
    assert T.downshift(profile, "high") == "off"


def test_missing_levels_filled_gray():
    profile = T.parse_profile(
        {"telegraph": {"mode": "toggle", "levels": [{"id": "max", "param": {"thinking": {"type": "enabled"}}}]}}
    )
    assert [lv.id for lv in profile.levels] == list(T.CANONICAL_LEVELS)
    assert [lv.ok for lv in profile.levels] == [False, False, False, False, False, True]


def test_downshift_across_models():
    """换模型自动降档：claude medium → glm（toggle 只有 max 可用）→ max。"""
    glm = T.parse_profile(_model("glm", "glm-5.3"))
    assert T.downshift(glm, "medium") == "max"
    claude = T.parse_profile(_model("anthropic", "claude-sonnet-4-5"))
    assert T.downshift(claude, "max") == "high"  # claude 无 max → 降到 high
    gpt = T.parse_profile(_model("openai", "gpt-5.2"))
    assert T.downshift(gpt, "max") == "max"


def test_resolve_level_keeps_valid():
    profile = T.parse_profile(_model("anthropic", "claude-sonnet-4-5"))
    assert T.resolve_level(profile, "low") == "low"
    assert T.resolve_level(profile, "max") == "high"


def test_state_payload_shape():
    profile = T.parse_profile(_model("glm", "glm-5.3"))
    payload = T.state_payload(profile, "max")
    assert payload["current"] == "max"
    assert payload["paramTranslation"] == {"thinking": {"type": "enabled"}}
    ids = [lv["id"] for lv in payload["levels"]]
    assert ids == list(T.CANONICAL_LEVELS)
    entry = next(lv for lv in payload["levels"] if lv["id"] == "off")
    assert entry == {"id": "off", "label": "关闭", "param": {"thinking": {"type": "disabled"}}, "ok": False}


def test_reference_telegraph_modes():
    assert T.reference_telegraph("anthropic")["mode"] == "budget"
    assert T.reference_telegraph("openai")["mode"] == "levels"
    assert T.reference_telegraph("gemini")["mode"] == "budget"
    assert T.reference_telegraph("weird") == {"mode": "none", "levels": []}
