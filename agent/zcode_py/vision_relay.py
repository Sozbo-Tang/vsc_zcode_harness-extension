"""Vision 中继（R9，PLAN §2.2 / §3.1 vision_relay.py）。

主模型**不支持视觉**（模型档案 ``capabilities.vision`` 非 true）而用户消息带
图片时：图片逐张路由给用户指定的多模态模型转述成文字，转述段落并入该轮用户
消息，**原图不进主模型请求**（省 token，L1 源头治理）；每图广播
``vision_relay{model, preset, description}`` 帧（聊天流中继卡，全文可审计）。

配置（providers.json 的 provider 条目新字段，本模块只读不写）：

- ``visionRelayModel``：视觉模型。``"providerId/modelId"`` 跨 provider 引用；
  纯 ``"modelId"`` 表示当前 provider 内的模型。Key 走同一环境变量机制
  （config.api_key_for）。
- ``visionRelayPreset``：转述档位 ``ocr``（提取文字）| ``describe``（通用描述）
  | ``frontend``（前端还原）| ``custom``（用 init.settings 的
  ``visionRelayCustomPrompt`` 自定义提示词）。

主模型**支持视觉**时图片以 ImageBlock 直入 LLM 请求（llm/base.py），不经本
模块。未配置视觉模型 → ``error{kind:"config"}``（含配置指引），图片按无视觉
处理（跳过，不阻塞回合）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .config import api_key_for, get_provider_entry
from .llm.base import (
    ImageBlock,
    LLMRequest,
    LLMUserMessage,
    Provider,
    ProviderError,
    TextBlock,
    TextDelta,
    TurnEnd,
)
from .protocol import Error, VisionRelay

PRESET_IDS = ("ocr", "describe", "frontend", "custom")
DEFAULT_PRESET = "describe"
CUSTOM_PROMPT_SETTING = "visionRelayCustomPrompt"

_UNCONFIGURED_MESSAGE = (
    "未配置视觉中继模型：当前主模型不支持视觉，图片无法处理。请在 API 连接"
    "管理中为该 provider 填写「视觉中继模型」（visionRelayModel，任一多模态"
    "模型，如 gpt-5.2 / claude-sonnet-4-5 / gemini-2.5-flash），或切换到支持"
    "视觉的主模型。"
)

# 三档预设（+custom）：(标签, 提示词)。提示词面向「供纯文本编程助手消费」。
_PRESET_PROMPTS: dict[str, tuple[str, str]] = {
    "ocr": (
        "OCR 提取",
        "逐字提取图片中所有可见文本，保持原始阅读顺序与结构（标题/列表/表格/"
        "代码块用 Markdown 表示）。只输出提取到的文本；图中没有文字时回答"
        "“（图中无文字）”。不要解释、不要评论。",
    ),
    "describe": (
        "通用描述",
        "请详细描述这张图片：主体对象、文字要点、数据趋势、界面元素、颜色与"
        "布局等对理解图片必要的所有信息。用结构化的中文条目输出，供一个只能"
        "读文本的编程助手使用；不要省略图中的关键文字。",
    ),
    "frontend": (
        "前端还原",
        "你是前端工程师。请把这张 UI 截图描述成可直接复现的规格：整体布局与"
        "栅格、每个区域的组件类型与层级、文案原文、图标与图片占位说明、主要"
        "颜色（尽量给出十六进制估值）、字号与间距的相对关系。目标是让另一个"
        "模型仅凭你的文字就能用 HTML/CSS 还原该界面。",
    ),
}

RELAY_MAX_OUTPUT_TOKENS = 4096


def model_supports_vision(model_entry: dict | None) -> bool:
    """模型档案 capabilities.vision（缺档案/缺字段一律按不支持处理→走中继）。"""
    caps = (model_entry or {}).get("capabilities")
    return bool(isinstance(caps, dict) and caps.get("vision") is True)


def preset_prompt(preset: str, settings: dict) -> tuple[str, str]:
    """(档位 id, 提示词)。未知档位回落 describe；custom 缺自定义提示词时也回落。"""
    if preset not in PRESET_IDS:
        preset = DEFAULT_PRESET
    if preset == "custom":
        custom = str((settings or {}).get(CUSTOM_PROMPT_SETTING) or "").strip()
        if custom:
            return "custom", custom
        preset = DEFAULT_PRESET  # 自定义提示词未配置：回落通用描述
    return preset, _PRESET_PROMPTS[preset][1]


def resolve_relay_target(config: dict, provider_entry: dict | None) -> tuple[dict | None, str | None, str]:
    """解析视觉中继目标 → (relay provider 条目, model id, 错误信息)。

    ``visionRelayModel`` 形如 ``"providerId/modelId"``（跨 provider）或
    ``"modelId"``（当前 provider 内）；空/未配置/找不到 provider 都返回错误。
    """
    spec = (provider_entry or {}).get("visionRelayModel")
    if not isinstance(spec, str) or not spec.strip():
        return None, None, _UNCONFIGURED_MESSAGE
    spec = spec.strip()
    if "/" in spec:
        pid, _, mid = spec.partition("/")
        entry = get_provider_entry(config or {}, pid)
        if entry is None:
            return None, None, (
                f"视觉中继 provider {pid!r} 不在 providers.json 中；请在 API 连接"
                "管理中修正 visionRelayModel（格式 providerId/modelId）。"
            )
        model_id = mid.strip() or entry.get("defaultModel")
        if not model_id:
            return None, None, f"视觉中继 provider {pid!r} 未声明模型（defaultModel 缺失）。"
        return entry, model_id, ""
    entry = provider_entry
    return entry, spec, ""


@dataclass
class RelayResult:
    """中继产出：转述段落（并入用户消息）+ 协议帧 + 错误帧。"""

    paragraphs: list[str] = field(default_factory=list)
    frames: list[VisionRelay] = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)  # 逐图失败（读图/中继模型错）
    error: Error | None = None  # 配置级错误（未配置中继模型等）——图片整体跳过


def relay_images(
    images: list[str],
    *,
    config: dict,
    provider_entry: dict | None,
    settings: dict,
    provider_factory: Callable[[dict, str | None], Provider],
    api_key_env: dict[str, str] | None = None,
    on_frame: Callable[[VisionRelay], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> RelayResult:
    """逐图调用视觉模型转述。绝不抛异常——一切失败以 Error 帧降级。"""
    result = RelayResult()
    entry, model_id, err = resolve_relay_target(config, provider_entry)
    if err:
        result.error = Error(kind="config", message=err)
        return result
    assert entry is not None and model_id is not None
    # 转述档位优先级（R15/R20 设置页接线）：settings.visionRelayPreset
    # （~/.zcode-py/config/settings.json，设置页「视觉中继」组）→ provider
    # 条目 visionRelayPreset（providers.json）→ describe 缺省。自定义提示词
    # 同理经 settings.visionRelayCustomPrompt（preset_prompt 内消费）。
    preset = str(
        (settings or {}).get("visionRelayPreset")
        or (provider_entry or {}).get("visionRelayPreset")
        or DEFAULT_PRESET
    )
    preset_id, prompt = preset_prompt(preset, settings)
    key = api_key_for(entry.get("id") or "", api_key_env)
    try:
        provider = provider_factory(entry, key)
    except ProviderError as exc:
        result.error = Error(kind=exc.kind, message=f"视觉中继 provider 构建失败: {exc.message}")
        return result
    except Exception as exc:  # noqa: BLE001
        result.error = Error(kind="config", message=f"视觉中继 provider 构建失败: {exc}")
        return result

    for i, raw_path in enumerate(images, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        path = str(raw_path)
        try:
            block = ImageBlock.from_file(path)
        except OSError as exc:
            result.errors.append(Error(kind="vision", message=f"cannot read image {path}: {exc}"))
            continue
        description = _describe_one(provider, model_id, block, prompt)
        if isinstance(description, ProviderError):
            result.errors.append(
                Error(
                    kind=description.kind,
                    message=f"vision relay for {path} failed: {description.message}",
                )
            )
            continue
        frame = VisionRelay(model=model_id, preset=preset_id, description=description)
        result.frames.append(frame)
        if on_frame is not None:
            on_frame(frame)
        result.paragraphs.append(f"Image {i} ({path}):\n{description}")
    return result


def _describe_one(
    provider: Provider, model_id: str, block: ImageBlock, prompt: str
) -> str | ProviderError:
    """单图转述：[图片, 提示词] → 流式文本拼接。ProviderError 原样上抛给调用方。"""
    request = LLMRequest(
        model=model_id,
        messages=[LLMUserMessage([block, TextBlock(prompt)])],
        system=None,
        tools=None,
        thinking=None,
        max_tokens=RELAY_MAX_OUTPUT_TOKENS,
    )
    chunks: list[str] = []
    stream = provider.stream(request)
    try:
        for event in stream:
            if isinstance(event, TextDelta):
                if event.text:
                    chunks.append(event.text)
            elif isinstance(event, TurnEnd):
                break
    except ProviderError as exc:
        return exc
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    text = "".join(chunks).strip()
    return text or "(vision relay returned an empty description)"


__all__ = [
    "PRESET_IDS",
    "RelayResult",
    "model_supports_vision",
    "preset_prompt",
    "relay_images",
    "resolve_relay_target",
]
