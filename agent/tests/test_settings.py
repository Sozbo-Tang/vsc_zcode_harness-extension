"""设置页（R15/R20）：settings_store 存取、服务端 settings_get/set /
clear_sessions、消费点生效（maxTurns / systemPromptAppend / 压缩阈值 /
视觉中继档位 / 隐私模式）——全部离线（FakeProvider + tmp_home）。"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from zcode_py import privacy
from zcode_py.llm.base import TextDelta
from zcode_py.llm.fake import FakeProvider, Wait
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.protocol import decode_message, encode_message
from zcode_py.sessions import SessionStore
from zcode_py.settings_store import (
    apply_patch,
    load_settings,
    merge_settings,
    normalize_patch,
    save_settings,
)
from zcode_py.vision_relay import relay_images


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Harness:
    """直连 ZcodeService（stdout 为 StringIO），帧用 dict 断言（同 test_service）。"""

    def __init__(self, provider: FakeProvider):
        self.out = io.StringIO()
        self.emitter = StdioEmitter(self.out, store=SessionStore())
        self.service = ZcodeService(
            self.emitter,
            provider_factory=lambda entry, key: provider,
        )

    def send(self, payload: dict) -> None:
        self.service.handle_line(json.dumps(payload, ensure_ascii=False))

    def frames(self) -> list:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def frames_of_type(self, type_name: str) -> list:
        return [f for f in self.frames() if f.get("type") == type_name]

    def wait_frames(self, type_name: str, count=1) -> bool:
        return wait_for(lambda: len(self.frames_of_type(type_name)) >= count)


@pytest.fixture(autouse=True)
def reset_privacy_patterns():
    """隐私模式是模块级活动状态——每个用例后恢复内置缺省，防跨用例泄漏。"""
    yield
    privacy.reset_active_file_patterns()


# ---------------------------------------------------------------------------
# settings_store：存取 + 原子写 + 规整
# ---------------------------------------------------------------------------


def test_load_creates_skeleton_and_roundtrip(tmp_path):
    """首次 load 落最小骨架（缺省不落盘）；save → load 往返一致。"""
    path = tmp_path / "config" / "settings.json"
    data = load_settings(path)
    assert data == {"version": 1}
    assert path.exists()

    data["maxTurns"] = 12
    data["privacyFilePatterns"] = [".env", "*.pem"]
    save_settings(data, path)
    assert load_settings(path) == data
    # 原子写不留临时残留文件
    assert not (path.parent / "settings.json.tmp").exists()


def test_load_corrupt_degrades_to_skeleton(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_settings(path) == {"version": 1}


def test_normalize_patch_valid_and_invalid():
    cleaned, err = normalize_patch(
        {
            "systemPromptAppend": "保持简洁",
            "maxTurns": 16,
            "compactionModel": " openai/gpt-5.2-mini ",
            "visionRelayPreset": "custom",
            "visionRelayCustomPrompt": "逐像素",
            "privacyFilePatterns": [".env", "  ", "*.pem"],
            "compaction": {"summarizeThreshold": 0.7},
            "telegraphClickSound": False,
            "unknownKey": "忽略我",
        }
    )
    assert err == ""
    assert cleaned == {
        "systemPromptAppend": "保持简洁",
        "maxTurns": 16,
        "compactionModel": "openai/gpt-5.2-mini",
        "visionRelayPreset": "custom",
        "visionRelayCustomPrompt": "逐像素",
        "privacyFilePatterns": [".env", "*.pem"],
        "compaction": {"summarizeThreshold": 0.7},
        "telegraphClickSound": False,
    }

    assert normalize_patch({"maxTurns": 0})[1] != ""          # 下界外
    assert normalize_patch({"maxTurns": True})[1] != ""       # bool 不是 int
    assert normalize_patch({"maxTurns": "8"})[1] != ""        # 字符串拒收
    assert normalize_patch({"visionRelayPreset": "nope"})[1] != ""
    assert normalize_patch({"privacyFilePatterns": ".env"})[1] != ""  # 须数组
    assert normalize_patch({"compaction": {"summarizeThreshold": 1.5}})[1] != ""
    assert normalize_patch({"systemPromptAppend": None})[1] != ""
    # R6 收尾：车钟咔哒开关只收 bool
    assert normalize_patch({"telegraphClickSound": "on"})[1] != ""
    assert normalize_patch({"telegraphClickSound": 1})[1] != ""
    assert normalize_patch({"telegraphClickSound": None})[1] != ""


def test_merge_settings_nested_and_apply_patch():
    merged = merge_settings(
        {"compaction": {"summarizeThreshold": 0.85}, "maxTurns": 8},
        {"compaction": {"evictThreshold": 0.6}},
    )
    # 嵌套组一层合并（不整块覆盖）
    assert merged == {"compaction": {"summarizeThreshold": 0.85, "evictThreshold": 0.6}, "maxTurns": 8}

    persisted = apply_patch({"version": 1}, {"maxTurns": 20})
    assert persisted == {"version": 1, "maxTurns": 20}
    persisted = apply_patch(persisted, {"compaction": {"summarizeThreshold": 0.9}})
    assert persisted["compaction"] == {"summarizeThreshold": 0.9}


# ---------------------------------------------------------------------------
# 协议：新命令与新字段往返
# ---------------------------------------------------------------------------


def test_control_settings_fields_round_trip():
    frame = {
        "type": "control",
        "command": "settings_set",
        "settings": {"maxTurns": 12},
        "key": "visionRelayPreset",
        "value": "ocr",
    }
    msg = decode_message(frame)
    assert msg.command == "settings_set"
    assert msg.settings == {"maxTurns": 12}
    assert msg.key == "visionRelayPreset"
    assert msg.value == "ocr"
    assert encode_message(msg) == frame

    for cmd in ("settings_get", "settings_set", "clear_sessions"):
        assert decode_message({"type": "control", "command": cmd}).command == cmd


# ---------------------------------------------------------------------------
# 服务端：init 读取 settings.json + settings_get / settings_set / clear_sessions
# ---------------------------------------------------------------------------


def _settings_file(tmp_home):
    return tmp_home / "config" / "settings.json"


def test_init_reads_settings_file_and_consumes(tmp_home):
    """settings.json 的受管键在 init 即消费：maxTurns / 追加提示词 / 隐私模式。"""
    save_settings(
        {
            "version": 1,
            "systemPromptAppend": "回答必须用中文",
            "maxTurns": 3,
            "privacyFilePatterns": ["*topsecret*"],
        },
        _settings_file(tmp_home),
    )
    fake = FakeProvider(turns=[[TextDelta("好")]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})

    # 隐私模式已热替换：topsecret 命中（fnmatch 在 POSIX 区分大小写），
    # 内置 .env 不再命中
    assert privacy.privacy_warning_for_path("/x/topsecret.yml") is not None
    assert privacy.privacy_warning_for_path("/x/.env") is None
    # 路径子串兜底仍在（不受文件模式编辑影响）
    assert privacy.privacy_warning_for_path("/home/u/.ssh/id_rsa") is not None

    harness.send({"type": "user_message", "text": "问题"})
    assert harness.wait_frames("message_complete")
    assert "回答必须用中文" in (fake.requests[0].system or "")
    assert harness.service._loop.max_turns == 3


def test_init_settings_merge_host_overlay(tmp_home):
    """宿主 init.settings 键级覆盖 settings.json（运行时通道优先）。"""
    save_settings({"version": 1, "maxTurns": 12}, _settings_file(tmp_home))
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": [], "settings": {"maxTurns": 5}})
    assert harness.service._effective_max_turns() == 5
    # 覆盖不回写持久层
    assert load_settings(_settings_file(tmp_home))["maxTurns"] == 12


def test_settings_get_returns_effective_values(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "settings_get"})

    results = [f for f in harness.frames_of_type("result") if f["command"] == "settings_get"]
    assert results and results[0]["ok"]
    payload = json.loads(results[0]["detail"])
    assert payload["settings"]["maxTurns"] == 8  # 缺省
    assert payload["settings"]["compaction"]["summarizeThreshold"] == 0.85
    assert payload["settings"]["visionRelayPreset"] == "describe"
    assert ".env" in payload["settings"]["privacyFilePatterns"]
    assert payload["sessionsDir"] == str(tmp_home / "sessions")


def test_settings_set_persists_and_hot_applies(tmp_home):
    """整体合并通道：落盘 + loop 参数 / 压缩阈值 / 隐私模式即时热应用。"""
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send(
        {
            "type": "control",
            "command": "settings_set",
            "settings": {
                "systemPromptAppend": "先给计划再动手",
                "maxTurns": 42,
                "compaction": {"summarizeThreshold": 0.3},
                "compactionModel": "openai/gpt-5.2-mini",
                "visionRelayPreset": "frontend",
                "privacyFilePatterns": ["*.vault"],
            },
        }
    )
    results = [f for f in harness.frames_of_type("result") if f["command"] == "settings_set"]
    assert results and results[0]["ok"]
    effective = json.loads(results[0]["detail"])
    assert effective["maxTurns"] == 42
    assert effective["compaction"]["summarizeThreshold"] == 0.3
    assert effective["compaction"]["compactionModel"] == "openai/gpt-5.2-mini"
    assert effective["visionRelayPreset"] == "frontend"

    # 持久层已写入（受管键全集）
    persisted = load_settings(_settings_file(tmp_home))
    assert persisted["maxTurns"] == 42
    assert persisted["compaction"] == {"summarizeThreshold": 0.3}
    assert persisted["visionRelayPreset"] == "frontend"

    # 热应用：loop 参数 / 压缩阈值 / 隐私模式
    loop = harness.service._loop
    assert loop.max_turns == 42
    assert "先给计划再动手" in loop.system_prompt
    assert harness.service._compaction.summarize_threshold == 0.3
    assert privacy.privacy_warning_for_path("/x/k.vault") is not None
    # compactionModel 经 settings_supplier 现读（供 resolve_compaction_target）
    assert harness.service._settings["compactionModel"] == "openai/gpt-5.2-mini"


def test_settings_set_single_key_channel(tmp_home):
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "control", "command": "settings_set", "key": "maxTurns", "value": 9})
    results = [f for f in harness.frames_of_type("result") if f["command"] == "settings_set"]
    assert results and results[0]["ok"]
    assert harness.service._loop.max_turns == 9
    assert load_settings(_settings_file(tmp_home))["maxTurns"] == 9


def test_settings_telegraph_click_sound_roundtrip(tmp_home):
    """R6 收尾：车钟咔哒开关——settings_get 缺省 True（不落盘）；set False
    落盘 + 回显生效；非法值 protocol error 不落盘。"""

    def last_settings_get(harness) -> dict:
        # frames() 是累计流：同命令多次回执时取最后一条
        results = [f for f in harness.frames_of_type("result") if f["command"] == "settings_get"]
        assert results
        return json.loads(results[-1]["detail"])["settings"]

    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})

    harness.send({"type": "control", "command": "settings_get"})
    assert last_settings_get(harness)["telegraphClickSound"] is True
    # 缺省不落盘：骨架里没有该键（= 用户显式覆盖才出现）
    assert "telegraphClickSound" not in load_settings(_settings_file(tmp_home))

    harness.send(
        {"type": "control", "command": "settings_set", "key": "telegraphClickSound", "value": False}
    )
    results = [f for f in harness.frames_of_type("result") if f["command"] == "settings_set"]
    assert results and results[0]["ok"]
    assert json.loads(results[0]["detail"])["telegraphClickSound"] is False
    assert load_settings(_settings_file(tmp_home))["telegraphClickSound"] is False

    # 再次 settings_get 仍关（内存合并态与持久层一致；脏值回落 True 只针对
    # 非 bool，显式 False 原样保留）
    harness.send({"type": "control", "command": "settings_get"})
    assert last_settings_get(harness)["telegraphClickSound"] is False

    # 非法值：protocol error，持久层保持 False 不被污染
    harness.send(
        {"type": "control", "command": "settings_set", "key": "telegraphClickSound", "value": "on"}
    )
    errors = [f for f in harness.frames_of_type("error") if "telegraphClickSound" in f["message"]]
    assert errors and errors[0]["kind"] == "protocol"
    assert load_settings(_settings_file(tmp_home))["telegraphClickSound"] is False


def test_settings_set_invalid_value_rejected(tmp_home):
    """非法值：protocol error，不落盘不应用。"""
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    before = harness.service._loop.max_turns
    harness.send({"type": "control", "command": "settings_set", "key": "maxTurns", "value": 0})
    errors = [f for f in harness.frames_of_type("error") if "maxTurns" in f["message"]]
    assert errors and errors[0]["kind"] == "protocol"
    assert harness.service._loop.max_turns == before
    assert "maxTurns" not in load_settings(_settings_file(tmp_home))

    # 空载荷：协议错误
    harness.send({"type": "control", "command": "settings_set"})
    assert [f for f in harness.frames_of_type("error") if "settings_set" in f["message"]]


def test_clear_sessions_clears_and_restarts(tmp_home):
    """清空 sessions 目录（保 config），随后重建空会话并广播列表。"""
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    first_id = harness.frames_of_type("session_started")[0]["sessionId"]
    harness.send({"type": "control", "command": "new_session"})

    sessions_dir = tmp_home / "sessions"
    assert len(list(sessions_dir.iterdir())) == 2

    harness.send({"type": "control", "command": "clear_sessions"})
    results = [f for f in harness.frames_of_type("result") if f["command"] == "clear_sessions"]
    assert results and results[0]["ok"]
    # 只剩清空后新建的一个会话；旧会话（含 first_id）目录已删
    remaining = [p.name for p in sessions_dir.iterdir()]
    assert len(remaining) == 1 and first_id not in remaining
    # config 目录原样保留（providers.json / settings.json）
    assert (tmp_home / "config" / "providers.json").exists()
    # 广播了新 session_started + sessions_list
    started = harness.frames_of_type("session_started")
    assert started[-1]["sessionId"] == remaining[0]


def test_clear_sessions_busy_rejected(tmp_home):
    """回合进行中拒绝清空（与 new_session/compact 同口径）。"""
    gate = threading.Event()
    fake = FakeProvider(turns=[[Wait(gate, timeout=10)]])
    harness = Harness(fake)
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send({"type": "user_message", "text": "长任务"})
    assert wait_for(lambda: harness.service._turn_alive())

    harness.send({"type": "control", "command": "clear_sessions"})
    errors = [f for f in harness.frames_of_type("error") if f["kind"] == "busy"]
    assert errors

    gate.set()
    assert harness.wait_frames("message_complete")


# ---------------------------------------------------------------------------
# 消费点：视觉中继档位 settings 覆盖 provider 条目
# ---------------------------------------------------------------------------


def test_relay_preset_settings_overrides_provider_entry(tmp_path):
    """settings.visionRelayPreset 优先于 provider 条目（设置页是真源）。"""
    provider = FakeProvider(turns=[[TextDelta("描述文本")]])
    entry = {
        "id": "main",
        "protocol": "anthropic",
        "defaultModel": "glm-5.3",
        "visionRelayModel": "glm-vision-x",
        "visionRelayPreset": "describe",
    }
    img = str(tmp_path / "shot.png")
    (tmp_path / "shot.png").write_bytes(b"\x89PNG-fake")
    result = relay_images(
        [img],
        config={"providers": [entry]},
        provider_entry=entry,
        settings={"visionRelayPreset": "ocr"},
        provider_factory=lambda e, key: provider,
    )
    assert result.error is None
    assert result.frames[0].preset == "ocr"
    blocks = provider.requests[0].messages[0].blocks
    assert any("提取" in getattr(b, "text", "") for b in blocks)


def test_settings_set_vision_preset_consumed_by_service_view(tmp_home):
    """settings_set 后 settings_get 的生效档位随之更新（UI 回显通道）。"""
    harness = Harness(FakeProvider())
    harness.send({"type": "init", "workspaceRoots": []})
    harness.send(
        {"type": "control", "command": "settings_set", "key": "visionRelayPreset", "value": "custom"}
    )
    harness.send({"type": "control", "command": "settings_get"})
    payload = json.loads(
        next(f["detail"] for f in harness.frames_of_type("result") if f["command"] == "settings_get")
    )
    assert payload["settings"]["visionRelayPreset"] == "custom"


# ---------------------------------------------------------------------------
# main()：settings_file 注入（进程入口参数透传）
# ---------------------------------------------------------------------------


def test_main_accepts_settings_file(tmp_home, monkeypatch):
    """main(settings_file=...) 透传给服务（集成缝：宿主/测试可重定位），
    init 后 settings_get 回执可见该文件里的 maxTurns。"""
    settings_file = tmp_home / "custom-settings.json"
    save_settings({"version": 1, "maxTurns": 7}, settings_file)
    from zcode_py.main import main as service_main

    stream_in = io.StringIO(
        json.dumps({"type": "init", "workspaceRoots": []}) + "\n"
        + json.dumps({"type": "control", "command": "settings_get"}) + "\n"
    )
    stream_out = io.StringIO()
    code = service_main(
        stream_in,
        stream_out,
        provider_factory=lambda entry, key: FakeProvider(),
        settings_file=settings_file,
    )
    assert code == 0
    lines = [json.loads(l) for l in stream_out.getvalue().splitlines()]
    assert any(f.get("type") == "session_started" for f in lines)
    detail = next(
        f["detail"] for f in lines if f.get("type") == "result" and f.get("command") == "settings_get"
    )
    assert json.loads(detail)["settings"]["maxTurns"] == 7
