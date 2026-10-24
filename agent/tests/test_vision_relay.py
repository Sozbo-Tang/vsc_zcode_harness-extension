"""vision_relay 测试（R9）：模型档案视觉能力判定、中继目标解析、三档 preset、
未配置报 config 错、逐图失败降级——FakeProvider 全离线。"""

from __future__ import annotations

import base64

import pytest

from zcode_py.llm.base import TextDelta
from zcode_py.llm.fake import FakeProvider
from zcode_py.vision_relay import (
    PRESET_IDS,
    model_supports_vision,
    preset_prompt,
    relay_images,
    resolve_relay_target,
)

CONFIG = {
    "defaultProvider": "main",
    "providers": [
        {"id": "main", "protocol": "anthropic", "defaultModel": "glm-5.3"},
        {"id": "vis", "protocol": "openai", "defaultModel": "gpt-5.2"},
    ],
}


def _main_entry():
    return CONFIG["providers"][0]


def _png(tmp_path, name="shot.png", payload=b"\x89PNG-fake"):
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def test_model_supports_vision():
    assert model_supports_vision(None) is False
    assert model_supports_vision({"capabilities": {"vision": True}}) is True
    assert model_supports_vision({"id": "glm"}) is False  # 缺省无视觉


def test_resolve_relay_target_unconfigured():
    entry, model, err = resolve_relay_target(CONFIG, _main_entry())
    assert entry is None and model is None
    assert "视觉中继" in err


def test_resolve_relay_target_cross_provider():
    entry = {**_main_entry(), "visionRelayModel": "vis/gpt-5.2-mini"}
    target, model, err = resolve_relay_target(CONFIG, entry)
    assert err == ""
    assert target["id"] == "vis"
    assert model == "gpt-5.2-mini"


def test_resolve_relay_target_same_provider_shorthand():
    entry = {**_main_entry(), "visionRelayModel": "glm-vision-x"}
    target, model, err = resolve_relay_target(CONFIG, entry)
    assert err == ""
    assert target["id"] == "main"
    assert model == "glm-vision-x"


def test_preset_prompt_routes_and_custom_fallback():
    for preset in ("ocr", "describe", "frontend"):
        pid, prompt = preset_prompt(preset, {})
        assert pid == preset and prompt
    # custom 未配置自定义提示词 → 回落通用描述
    pid, prompt = preset_prompt("custom", {})
    assert pid == "describe" and prompt
    # custom 配置了 → 用自定义
    pid, prompt = preset_prompt("custom", {"visionRelayCustomPrompt": "逐像素描述"})
    assert pid == "custom" and "逐像素描述" in prompt
    # 非法值 → 默认档
    pid, _ = preset_prompt("nope", {})
    assert pid == "describe"


def test_relay_images_unconfigured_returns_config_error(tmp_path):
    result = relay_images(
        [_png(tmp_path)],
        config=CONFIG,
        provider_entry=_main_entry(),
        settings={},
        provider_factory=lambda entry, key: FakeProvider(),
    )
    assert result.error is not None
    assert result.error.kind == "config"
    assert result.frames == [] and result.paragraphs == []


def test_relay_images_happy_path(tmp_path):
    provider = FakeProvider(turns=[[TextDelta("一张登录页截图，含用户名与密码输入框")]])
    frames_seen = []
    entry = {**_main_entry(), "visionRelayModel": "vis/gpt-5.2", "visionRelayPreset": "ocr"}
    img = _png(tmp_path)
    result = relay_images(
        [img],
        config=CONFIG,
        provider_entry=entry,
        settings={},
        provider_factory=lambda e, key: provider,
        on_frame=frames_seen.append,
    )
    assert result.error is None and result.errors == []
    assert len(result.frames) == 1
    frame = result.frames[0]
    assert frame.model == "gpt-5.2"
    assert frame.preset == "ocr"
    assert "登录页" in frame.description
    # 转述段落并入用户消息，且带可溯源的图片路径头
    assert len(result.paragraphs) == 1
    assert img in result.paragraphs[0] and "登录页" in result.paragraphs[0]
    # 请求确实带 ImageBlock（原图送视觉模型，不送主模型）
    req = provider.requests[0]
    blocks = req.messages[0].blocks
    assert any(getattr(b, "source_type", None) == "base64" for b in blocks)
    # 中继模型拿到的提示词是 OCR 档
    assert any("OCR" in b.text or "ocr" in b.text.lower() or "文字" in b.text for b in blocks if hasattr(b, "text"))


def test_relay_images_bad_file_degrades_per_image(tmp_path):
    provider = FakeProvider(turns=[[TextDelta("unused")]])
    entry = {**_main_entry(), "visionRelayModel": "vis/gpt-5.2"}
    result = relay_images(
        [str(tmp_path / "nope.png")],
        config=CONFIG,
        provider_entry=entry,
        settings={},
        provider_factory=lambda e, key: provider,
    )
    # 绝不抛异常：读图失败降级为逐图 Error
    assert result.error is None
    assert len(result.errors) == 1
    assert "nope.png" in result.errors[0].message
    assert result.paragraphs == []


def test_image_block_from_file_encodes_base64(tmp_path):
    from zcode_py.llm.base import ImageBlock

    p = _png(tmp_path, "raw.bin", b"hello-image")
    block = ImageBlock.from_file(p)
    assert block.source_type == "base64"
    assert base64.b64decode(block.data) == b"hello-image"
    assert block.path == p
