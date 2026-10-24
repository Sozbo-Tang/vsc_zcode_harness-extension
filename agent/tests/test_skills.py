"""M7 Skills（R26）测试：SKILL.md 解析（frontmatter 子集）/ 搜索顺序（工作区
覆盖用户级）/ 斜杠展开 / skills_injection / SkillsManager 懒加载与服务层
接线（skills_list control、system prompt 注入、user_message 展开 + result 帧）。
"""

from __future__ import annotations

import io
import json
import time

from zcode_py.llm.fake import FakeProvider
from zcode_py.llm.base import TextDelta
from zcode_py.main import StdioEmitter, ZcodeService
from zcode_py.sessions import SessionStore
from zcode_py.skills import (
    SkillsManager,
    expand_slash,
    load_skills,
    parse_skill_md,
    skills_dirs,
    skills_injection,
)


def make_skill(root, name, text=None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    default = (
        f"---\nname: {name}\ndescription: {name} skill\n---\n"
        f"Body instructions for {name}."
    )
    (directory / "SKILL.md").write_text(text if text is not None else default, encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_skill_md_frontmatter_fields():
    text = (
        "---\n"
        "name: deploy\n"
        'description: "Deploy the service"\n'
        "model: glm-5.3\n"
        "allowed-tools: bash, read\n"
        "---\n"
        "\n"
        "1. Run the deploy script.\n"
        "2. Verify the endpoint.\n"
    )
    skill = parse_skill_md(text, fallback_name="dir-name")
    assert skill.name == "deploy"
    assert skill.description == "Deploy the service"  # 引号剥除
    assert skill.model == "glm-5.3"
    assert skill.allowed_tools == ["bash", "read"]
    assert skill.body == "1. Run the deploy script.\n2. Verify the endpoint."


def test_parse_skill_md_defaults_and_underscore_alias():
    # name 缺省 = 目录名；allowed_tools 下划线写法等价；无 description → 空
    skill = parse_skill_md(
        "---\nallowed_tools: read, grep\n---\nJust do it.",
        fallback_name="my-skill",
    )
    assert skill.name == "my-skill"
    assert skill.description == ""
    assert skill.allowed_tools == ["read", "grep"]
    assert skill.body == "Just do it."


def test_parse_skill_md_without_frontmatter():
    skill = parse_skill_md("Plain body only.", fallback_name="plain")
    assert skill.name == "plain"
    assert skill.body == "Plain body only."


def test_parse_skill_md_unclosed_frontmatter_is_all_body():
    skill = parse_skill_md("---\nname: x\nbody junk", fallback_name="fb")
    assert skill.body.startswith("---")  # 不闭合 → 全文当正文
    assert skill.name == "fb"


# ---------------------------------------------------------------------------
# 加载与搜索顺序
# ---------------------------------------------------------------------------


def test_skills_dirs_order_workspace_overrides_home(tmp_home, tmp_path):
    dirs = skills_dirs(tmp_path)
    assert dirs[0] == tmp_home / "skills"  # home 在前
    assert dirs[1] == tmp_path / ".zcode-py" / "skills"  # 工作区在后（覆盖同名）


def test_load_skills_workspace_overrides_home(tmp_home, tmp_path):
    make_skill(tmp_home / "skills", "deploy", "---\nname: deploy\ndescription: home\n---\nHOME BODY")
    make_skill(tmp_path / ".zcode-py" / "skills", "deploy",
               "---\nname: deploy\ndescription: ws\n---\nWS BODY")
    make_skill(tmp_home / "skills", "review")  # 仅 home 的 skill 照常收录
    skills = load_skills(tmp_path)
    assert set(skills) == {"deploy", "review"}
    assert skills["deploy"].description == "ws"
    assert skills["deploy"].body == "WS BODY"
    assert skills["deploy"].path.endswith("SKILL.md")


def test_load_skills_skips_invalid_entries(tmp_home, tmp_path):
    home_skills = tmp_home / "skills"
    make_skill(home_skills, "good")
    (home_skills / "not-a-dir.md").write_text("---\n---\nx", encoding="utf-8")  # 文件非目录
    make_skill(home_skills, "has space")  # 目录名非法（含空格）→ 跳过
    bad = home_skills / "no-md"
    bad.mkdir()  # 目录无 SKILL.md → 跳过
    skills = load_skills(tmp_path)
    assert set(skills) == {"good"}


# ---------------------------------------------------------------------------
# 注入与斜杠展开
# ---------------------------------------------------------------------------


def test_skills_injection_lines():
    from zcode_py.skills import Skill

    skills = {
        "b": Skill(name="b", description="second"),
        "a": Skill(name="a", description="first"),
    }
    block = skills_injection(skills)
    assert "/a — first" in block and "/b — second" in block
    assert block.index("/a") < block.index("/b")  # 按名字排序
    assert skills_injection({}) is None
    assert skills_injection(None) is None


def test_expand_slash_hit_and_miss():
    from zcode_py.skills import Skill

    skills = {"deploy": Skill(name="deploy", body="Deploy steps.")}
    prefix, rest, skill = expand_slash("/deploy now please", skills)
    assert skill is not None and skill.name == "deploy"
    assert prefix.startswith("## Skill: deploy")
    assert "Deploy steps." in prefix
    assert prefix.endswith("## End of skill deploy")
    assert rest == "now please"
    # 非斜杠 / 未知名字 / 名字后紧跟非空白 → 原样
    assert expand_slash("plain text", skills) == ("plain text", "", None)
    assert expand_slash("/unknown x", skills) == ("/unknown x", "", None)
    assert expand_slash("/deployx tail", skills) == ("/deployx tail", "", None)
    # 纯斜杠名（无剩余文本）
    _prefix, rest, skill = expand_slash("/deploy", skills)
    assert skill is not None and rest == ""


# ---------------------------------------------------------------------------
# SkillsManager：懒加载 / 刷新 / 快照不触发同步加载
# ---------------------------------------------------------------------------


def test_manager_lazy_refresh_and_loaded_snapshot(tmp_home, tmp_path):
    make_skill(tmp_home / "skills", "one")
    manager = SkillsManager(tmp_path)
    assert manager.loaded_skills() is None  # 未扫描 → None（不阻塞）
    loaded = manager.ensure_loaded()
    assert set(loaded) == {"one"}
    assert manager.loaded_skills() == loaded
    # 新增 skill 后 refresh 可见
    make_skill(tmp_path / ".zcode-py" / "skills", "two")
    assert set(manager.ensure_loaded()) == {"one"}  # 缓存不自动失效
    assert set(manager.refresh()) == {"one", "two"}


# ---------------------------------------------------------------------------
# 服务层接线：skills_list / system prompt 注入 / 斜杠展开 + result 帧
# ---------------------------------------------------------------------------


def make_harness(tmp_path):
    fake = FakeProvider(turns=[[TextDelta("ok")]])
    out = io.StringIO()
    emitter = StdioEmitter(out, store=SessionStore())
    service = ZcodeService(emitter, provider_factory=lambda entry, key: fake)

    class Recorder:
        def __init__(self):
            self.frames = []

        def emit(self, msg, **kw):
            self.frames.append(msg)

    recorder = Recorder()
    emitter.emit = recorder.emit
    service.handle_line(json.dumps({"type": "init", "workspaceRoots": [str(tmp_path)]}))
    return service, fake, recorder


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_service_skills_list_control(tmp_home, tmp_path):
    make_skill(tmp_path / ".zcode-py" / "skills", "deploy",
               "---\nname: deploy\ndescription: 部署服务\n---\nbody")
    service, _fake, recorder = make_harness(tmp_path)
    service.handle_line(json.dumps({"type": "control", "command": "skills_list"}))
    results = [f for f in recorder.frames if type(f).__name__ == "Result" and f.command == "skills_list"]
    assert results and results[-1].ok
    payload = json.loads(results[-1].detail)
    assert payload == [{"name": "deploy", "description": "部署服务", "path": payload[0]["path"]}]


def test_service_slash_expansion_roundtrip(tmp_home, tmp_path):
    make_skill(tmp_path / ".zcode-py" / "skills", "deploy",
               "---\nname: deploy\ndescription: d\n---\nDEPLOY STEPS")
    service, fake, recorder = make_harness(tmp_path)
    service.handle_line(json.dumps({"type": "user_message", "text": "/deploy to staging"}))
    service.shutdown()
    # result 帧注明 skill 展开
    results = [f for f in recorder.frames if type(f).__name__ == "Result" and f.command == "skill"]
    assert results and "deploy" in results[0].detail
    # 展开体拼进该轮消息前缀；斜杠 token 被替换（不再出现 "/deploy"）
    user_text = fake.requests[0].messages[0].blocks[0].text
    assert user_text.startswith("## Skill: deploy")
    assert "DEPLOY STEPS" in user_text
    assert "to staging" in user_text
    assert "/deploy" not in user_text


def test_service_unknown_slash_passes_through(tmp_home, tmp_path):
    make_skill(tmp_path / ".zcode-py" / "skills", "deploy")
    service, fake, recorder = make_harness(tmp_path)
    service.handle_line(json.dumps({"type": "user_message", "text": "/nope hello"}))
    service.shutdown()
    user_text = fake.requests[0].messages[0].blocks[0].text
    assert user_text == "/nope hello"
    assert not [f for f in recorder.frames if type(f).__name__ == "Result" and f.command == "skill"]


def test_service_system_prompt_injects_skills_after_scan(tmp_home, tmp_path):
    make_skill(tmp_path / ".zcode-py" / "skills", "deploy",
               "---\nname: deploy\ndescription: deploy things\n---\nbody")
    service, _fake, _recorder = make_harness(tmp_path)
    # 后台扫描完成后 system prompt 生效（不阻塞 init，注入于后续轮次）
    assert wait_for(
        lambda: service._loop is not None and "/deploy — deploy things" in service._loop.system_prompt
    )
