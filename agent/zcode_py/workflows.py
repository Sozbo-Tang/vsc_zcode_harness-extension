"""M9 工作流编排（最小可用，R3，PLAN §2.1 / §7 M9）。

声明式工作流：``~/.zcode-py/workflows/<name>.py`` 模块级 ``STEPS`` 列表，每步
``{"prompt": str, "model": "providerId/modelId"?}``。执行语义（服务层
``workflow_run{name}`` control 驱动）：

- 顺序执行，每步一个**独立受限子代理**（subagents.py 的 run_child 模式：独立
  上下文、只读工具白名单、可指定便宜模型）；
- 前步最终文本作为下步 prompt 的**上下文前缀**拼接（截尾
  ``WORKFLOW_CONTEXT_CHAR_LIMIT`` 防上下文爆炸）；
- 失败步（含子代理超时/取消/max_turns）即停，后续步不执行；
- 完成广播 ``result{command:"workflow_run", detail:JSON 摘要}``（报告含逐步
  ok/stopReason/结果尾段与 failed_at/cancelled）。

``workflows_list`` control 扫描目录返回列表（坏文件降级为 error 条目，不拖垮
其余）。模块加载用独立模块名、不注册 sys.modules——同名重载不受缓存干扰。
"""

from __future__ import annotations

import importlib.util
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from .config import home_dir
from .tools import ToolOutcome

# 目录名（=工作流名）合法字符：与 skills 同口径
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
# 前步结果拼进下步 prompt 的截尾上限（字符）
WORKFLOW_CONTEXT_CHAR_LIMIT = 8000
# 报告里每步结果摘要的截尾上限（字符）
RESULT_SNIPPET_LIMIT = 500


class WorkflowError(ValueError):
    """工作流名非法 / 模块缺失 / STEPS 声明不合法。"""


def _validate_steps(steps) -> list[dict]:
    """STEPS 声明校验：非空列表、每项含非空 str prompt、model 可选 str。"""
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("STEPS must be a non-empty list")
    validated: list[dict] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkflowError(f"STEPS[{i}] must be an object")
        prompt = step.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError(f"STEPS[{i}] requires a non-empty string 'prompt'")
        model = step.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise WorkflowError(f"STEPS[{i}].model must be a non-empty string or null")
        entry = {"prompt": prompt}
        if isinstance(model, str) and model.strip():
            entry["model"] = model.strip()
        validated.append(entry)
    return validated


def load_workflow(path: Path) -> list[dict]:
    """导入工作流模块并校验 STEPS；失败抛 WorkflowError（含原因）。"""
    if not path.is_file():
        raise WorkflowError(f"workflow file not found: {path}")
    module_name = f"zcode_workflow_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover —— 平台相关兜底
        raise WorkflowError(f"cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 坏模块降级为可展示的错误（不拖垮列表/其余工作流）
        raise WorkflowError(f"cannot import {path.name}: {type(exc).__name__}: {exc}") from exc
    steps = getattr(module, "STEPS", None)
    if steps is None:
        raise WorkflowError(f"{path.name} has no module-level STEPS declaration")
    try:
        return _validate_steps(steps)
    except WorkflowError as exc:
        raise WorkflowError(f"{path.name}: {exc}") from exc


class WorkflowManager:
    """工作流目录扫描 + 顺序执行。执行经注入的 ``step_runner``（服务层用
    ChildRuntime 装配），本模块不感知 provider / 权限细节。"""

    def __init__(self, directory: Path | None = None):
        self._dir = directory if directory is not None else home_dir() / "workflows"

    # ------------------------------------------------------------------ 扫描 / 加载

    def list_workflows(self) -> list[dict]:
        """目录内全部工作流摘要（坏文件 error 条目，按名排序）。"""
        out: list[dict] = []
        if not self._dir.is_dir():
            return out
        for path in sorted(self._dir.glob("*.py")):
            name = path.stem
            entry: dict = {"name": name}
            try:
                steps = load_workflow(path)
            except WorkflowError as exc:
                entry["error"] = str(exc)
                entry["stepCount"] = 0
            else:
                entry["stepCount"] = len(steps)
                entry["models"] = [s.get("model") for s in steps if s.get("model")]
            out.append(entry)
        return out

    def load(self, name: str) -> list[dict]:
        """按名加载（名字仅允许文件名字符——拒绝路径穿越）。"""
        if not isinstance(name, str) or not NAME_RE.match(name.strip()):
            raise WorkflowError(f"invalid workflow name {name!r}")
        return load_workflow(self._dir / f"{name.strip()}.py")

    # ------------------------------------------------------------------ 执行

    def run(
        self,
        name: str,
        *,
        step_runner: Callable[[str, str | None], ToolOutcome],
        cancel_event: threading.Event | None = None,
    ) -> dict:
        """顺序执行；报告 dict（服务层序列化进 result.detail）。"""
        report: dict = {"workflow": str(name), "total": 0, "completed": 0, "steps": []}
        try:
            steps = self.load(name)
        except WorkflowError as exc:
            report["error"] = str(exc)
            report["ok"] = False
            return report
        report["total"] = len(steps)
        prev_result: str | None = None
        for index, step in enumerate(steps):
            if cancel_event is not None and cancel_event.is_set():
                report["cancelled"] = True
                break
            prompt = step["prompt"]
            if prev_result:
                # 前步结果作为下步上下文前缀（截尾防上下文爆炸）
                context = prev_result[-WORKFLOW_CONTEXT_CHAR_LIMIT:]
                prompt = (
                    f"[Context — result of previous workflow step]\n{context}\n\n"
                    f"---\n\n{prompt}"
                )
            outcome = step_runner(prompt, step.get("model"))
            result = outcome.result if isinstance(outcome.result, dict) else {}
            entry: dict = {
                "index": index,
                "model": step.get("model") or "(session default)",
                "ok": not outcome.is_error,
                "stopReason": result.get("stopReason"),
                "result": str(result.get("result") or "")[-RESULT_SNIPPET_LIMIT:],
            }
            report["steps"].append(entry)
            if outcome.is_error:
                entry["error"] = result.get("error") or "step_failed"
                if result.get("detail"):
                    entry["error"] = f"{entry['error']}: {result['detail']}"
                report["failed_at"] = index
                break
            report["completed"] += 1
            prev_result = str(result.get("result") or "")
        report["ok"] = (
            report["completed"] == report["total"] and not report.get("cancelled")
        )
        return report


__all__ = [
    "WORKFLOW_CONTEXT_CHAR_LIMIT",
    "WorkflowError",
    "WorkflowManager",
    "load_workflow",
]
