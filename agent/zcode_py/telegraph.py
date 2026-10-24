"""车钟思考档位（PLAN §2.2 R6 / §3.3）：档位 → 各协议思考参数映射。

设计：
- 六个规范档位 id：off / min / low / medium / high / max（盘面统一渲染，不支持
  的置灰 ok=false）；
- 档案数据驱动：模型档案 ``telegraph`` 块声明 ``mode``（盘面四模式）与
  ``levels: [{id,label,param,ok}]``（param 即「直接合并进请求体」的厂商参数，
  由 llm 适配器消费）；未声明的规范档位自动补 ok=False；
- 四种盘面模式：budget（anthropic/gemini 预算型）、levels（openai effort 档位型）、
  toggle（glm 开关型，仅 关闭/思考 两位）、none（无思考型，整体置灰）；
- 换模型自动降档：当前档位新档案不可用时先向下找最近可用档，全不可用再向上
  兜底，无思考型归 off（PLAN §3.3）。

GLM-5.3 特例（调研 §3.3）：thinking disabled 直接报错 → 其档案 off 档
ok=False，即「强制思考」只有 enabled 一位可选。
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import TelegraphState

CANONICAL_LEVELS: tuple[str, ...] = ("off", "min", "low", "medium", "high", "max")
LEVEL_LABELS: dict[str, str] = {
    "off": "关闭",
    "min": "最低",
    "low": "低",
    "medium": "中",
    "high": "高",
    "max": "最大",
}
MODES: tuple[str, ...] = ("budget", "levels", "toggle", "none")


@dataclass(frozen=True)
class TelegraphLevel:
    id: str
    label: str
    param: dict | None
    ok: bool


@dataclass(frozen=True)
class TelegraphProfile:
    mode: str
    levels: tuple[TelegraphLevel, ...]  # 固定按 CANONICAL_LEVELS 顺序，6 位全列
    default_level: str


def parse_profile(model_entry: dict | None) -> TelegraphProfile:
    """从模型档案 dict 解析；缺省/无思考声明 → none 模式全灰。"""
    entry = model_entry or {}
    raw = entry.get("telegraph") if isinstance(entry.get("telegraph"), dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else "none"
    declared: dict[str, dict] = {}
    for lvl in raw.get("levels") or []:
        if isinstance(lvl, dict) and isinstance(lvl.get("id"), str):
            declared[lvl["id"]] = lvl
    levels: list[TelegraphLevel] = []
    for level_id in CANONICAL_LEVELS:
        d = declared.get(level_id)
        if mode != "none" and d is not None:
            param = d.get("param")
            levels.append(
                TelegraphLevel(
                    id=level_id,
                    label=d.get("label") or LEVEL_LABELS[level_id],
                    param=param if isinstance(param, dict) else None,
                    ok=bool(d.get("ok", param is not None)),
                )
            )
        else:
            levels.append(
                TelegraphLevel(
                    id=level_id, label=LEVEL_LABELS[level_id], param=None, ok=False
                )
            )
    default_level = raw.get("default") if raw.get("default") in CANONICAL_LEVELS else None
    if default_level is None:
        default_level = next((lv.id for lv in levels if lv.ok), "off")
    elif not _level(levels, default_level).ok:
        default_level = next((lv.id for lv in levels if lv.ok), "off")
    return TelegraphProfile(mode=mode, levels=tuple(levels), default_level=default_level)


def _level(levels, level_id: str) -> TelegraphLevel:
    for lv in levels:
        if lv.id == level_id:
            return lv
    return TelegraphLevel(level_id, level_id, None, False)


def translate(profile: TelegraphProfile, level: str) -> dict | None:
    """档位 → 厂商思考参数（None = 不发送，off 档无 param 即 None）。"""
    lv = _level(profile.levels, level)
    if not lv.ok:
        return None
    return lv.param


def downshift(profile: TelegraphProfile, current: str) -> str:
    """换模型自动降档：当前可用即保持；否则就近向下，再向上兜底，全灰归 off。"""
    lv = _level(profile.levels, current)
    if lv.ok:
        return current
    try:
        idx = CANONICAL_LEVELS.index(current)
    except ValueError:
        idx = len(CANONICAL_LEVELS) - 1
    for i in range(idx - 1, -1, -1):
        if _level(profile.levels, CANONICAL_LEVELS[i]).ok:
            return CANONICAL_LEVELS[i]
    for level_id in CANONICAL_LEVELS:
        if _level(profile.levels, level_id).ok:
            return level_id
    return "off"


def resolve_level(profile: TelegraphProfile, requested: str) -> str:
    """请求档位可用即采用，否则降档（用于 control.telegraph 与初始化）。"""
    return requested if _level(profile.levels, requested).ok else downshift(profile, requested)


def state_payload(profile: TelegraphProfile, current: str) -> dict:
    """构造 telegraph_state 事件载荷字段（PLAN §5）。"""
    return {
        "levels": [
            {"id": lv.id, "label": lv.label, "param": lv.param, "ok": lv.ok}
            for lv in profile.levels
        ],
        "current": current,
        "paramTranslation": translate(profile, current) or {},
    }


def telegraph_state(profile: TelegraphProfile, current: str) -> TelegraphState:
    return TelegraphState(**state_payload(profile, current))


# ---------------------------------------------------------------------------
# 参考档案（PLAN §3.3 表）：模型档案未声明 telegraph 时的按协议兜底，
# 也用于 tests/test_config.py 校验示例档案的参数形状。
# ---------------------------------------------------------------------------


def reference_telegraph(protocol: str) -> dict:
    """按协议返回参考 telegraph 块（PLAN §3.3 参考值，以档案声明为准）。"""
    if protocol == "anthropic":
        return {
            "mode": "budget",
            "default": "medium",
            "levels": [
                {"id": "off", "label": "关闭", "param": {"thinking": {"type": "disabled"}}, "ok": True},
                {"id": "min", "label": "最低", "param": {"thinking": {"type": "enabled", "budget_tokens": 2048}}, "ok": True},
                {"id": "low", "label": "低", "param": {"thinking": {"type": "enabled", "budget_tokens": 8192}}, "ok": True},
                {"id": "medium", "label": "中", "param": {"thinking": {"type": "enabled", "budget_tokens": 24576}}, "ok": True},
                {"id": "high", "label": "高", "param": {"thinking": {"type": "enabled", "budget_tokens": 65536}}, "ok": True},
                {"id": "max", "label": "最大", "param": None, "ok": False},
            ],
        }
    if protocol == "openai":
        return {
            "mode": "levels",
            "default": "medium",
            "levels": [
                {"id": "off", "label": "关闭", "param": {"reasoning_effort": "minimal"}, "ok": True},
                {"id": "min", "label": "最低", "param": {"reasoning_effort": "low"}, "ok": True},
                {"id": "low", "label": "低", "param": {"reasoning_effort": "medium"}, "ok": True},
                {"id": "medium", "label": "中", "param": {"reasoning_effort": "high"}, "ok": True},
                {"id": "high", "label": "高", "param": {"reasoning_effort": "xhigh"}, "ok": True},
                {"id": "max", "label": "最大", "param": {"reasoning_effort": "max"}, "ok": True},
            ],
        }
    if protocol == "gemini":
        return {
            "mode": "budget",
            "default": "medium",
            "levels": [
                {"id": "off", "label": "关闭", "param": {"thinkingConfig": {"thinkingBudget": 0}}, "ok": True},
                {"id": "min", "label": "最低", "param": {"thinkingConfig": {"thinkingBudget": 2048, "includeThoughts": True}}, "ok": True},
                {"id": "low", "label": "低", "param": {"thinkingConfig": {"thinkingBudget": 8192, "includeThoughts": True}}, "ok": True},
                {"id": "medium", "label": "中", "param": {"thinkingConfig": {"thinkingBudget": 16384, "includeThoughts": True}}, "ok": True},
                {"id": "high", "label": "高", "param": {"thinkingConfig": {"thinkingBudget": 24576, "includeThoughts": True}}, "ok": True},
                {"id": "max", "label": "最大", "param": {"thinkingConfig": {"thinkingBudget": -1, "includeThoughts": True}}, "ok": True},
            ],
        }
    return {"mode": "none", "levels": []}
