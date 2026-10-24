"""M8 Notebook 工具（R31）：read/write 用真实 nbformat 构造临时 ipynb；
run 全打桩（nbclient importorskip + mock，缺依赖时跳过真跑用例）。"""

from __future__ import annotations

import json

import pytest

nbformat = pytest.importorskip("nbformat")  # 必装主依赖：缺位说明环境破损

from zcode_py.tools import ToolContext
from zcode_py.tools.notebook import (
    NBCLIENT_GUIDE,
    NOTEBOOK_READ_SPEC,
    NOTEBOOK_RUN_SPEC,
    NOTEBOOK_WRITE_SPEC,
    tool_notebook_read,
    tool_notebook_run,
    tool_notebook_write,
)


def make_notebook(path, cells_spec) -> str:
    """cells_spec: [(type, source)] → 真实 .ipynb（带 kernelspec 元数据）。"""
    notebook = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_code_cell(src) if kind == "code" else nbformat.v4.new_markdown_cell(src)
            for kind, src in cells_spec
        ],
        metadata={"kernelspec": {"name": "python3", "display_name": "Python 3"}},
    )
    nbformat.write(notebook, str(path))
    return str(path)


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    return ToolContext(cwd=tmp_path)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_lists_cells_with_summaries(tmp_path, ctx):
    path = make_notebook(tmp_path / "demo.ipynb", [
        ("code", "x = 1\nprint(x)"),
        ("markdown", "# 标题"),
        ("code", "raise ValueError('boom')"),
    ])
    # 预置输出：cell0 stream 输出、cell2 error 输出
    notebook = nbformat.read(path, as_version=4)
    notebook.cells[0]["outputs"] = [
        nbformat.v4.new_output("stream", text="1\n")
    ]
    notebook.cells[2]["outputs"] = [
        nbformat.v4.new_output("error", ename="ValueError", evalue="boom", traceback=["Traceback..."])
    ]
    nbformat.write(notebook, path)

    outcome = tool_notebook_read({"path": path}, ctx)
    assert not outcome.is_error
    result = outcome.result
    assert result["cellCount"] == 3 and result["kernel"] == "python3"
    cells = result["cells"]
    assert cells[0]["type"] == "code" and "print(x)" in cells[0]["source"]
    assert cells[0]["outputs"] == ["1\n"]
    assert cells[1]["type"] == "markdown" and "outputs" not in cells[1]
    assert "ValueError" in cells[2]["outputs"][0]

    assert NOTEBOOK_READ_SPEC.tier == "readonly"


def test_read_relative_path_resolves_against_cwd(tmp_path, ctx):
    make_notebook(tmp_path / "rel.ipynb", [("code", "1")])
    outcome = tool_notebook_read({"path": "rel.ipynb"}, ctx)
    assert not outcome.is_error and outcome.result["cellCount"] == 1


def test_read_missing_notebook_is_structured_error(tmp_path, ctx):
    outcome = tool_notebook_read({"path": str(tmp_path / "nope.ipynb")}, ctx)
    assert outcome.is_error and outcome.result["error"] == "not_found"


def test_read_requires_path(ctx):
    assert tool_notebook_read({"path": ""}, ctx).is_error


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_replaces_source_and_clears_stale_outputs(tmp_path, ctx):
    path = make_notebook(tmp_path / "w.ipynb", [("code", "old = 1")])
    notebook = nbformat.read(path, as_version=4)
    notebook.cells[0]["outputs"] = [nbformat.v4.new_output("stream", text="old\n")]
    notebook.cells[0]["execution_count"] = 7
    nbformat.write(notebook, path)

    outcome = tool_notebook_write(
        {"path": path, "cell_index": 0, "source": "new = 2"}, ctx
    )
    assert not outcome.is_error and outcome.result["ok"] is True

    reloaded = nbformat.read(path, as_version=4)
    assert reloaded.cells[0]["source"] == "new = 2"
    assert reloaded.cells[0]["outputs"] == []  # 旧输出作废
    assert reloaded.cells[0]["execution_count"] is None
    assert NOTEBOOK_WRITE_SPEC.tier == "write"


def test_write_markdown_keeps_cell_type(tmp_path, ctx):
    path = make_notebook(tmp_path / "m.ipynb", [("markdown", "# old"), ("code", "x")])
    outcome = tool_notebook_write({"path": path, "cell_index": 0, "source": "# new"}, ctx)
    assert not outcome.is_error
    assert nbformat.read(path, as_version=4).cells[0]["cell_type"] == "markdown"


def test_write_validates_args(tmp_path, ctx):
    path = make_notebook(tmp_path / "v.ipynb", [("code", "1")])
    assert tool_notebook_write({"path": path, "source": "x"}, ctx).is_error  # 缺 cell_index
    assert tool_notebook_write({"path": path, "cell_index": 9, "source": "x"}, ctx).is_error
    assert tool_notebook_write({"path": path, "cell_index": -1, "source": "x"}, ctx).is_error
    assert tool_notebook_write({"path": path, "cell_index": "0", "source": "x"}, ctx).is_error


# ---------------------------------------------------------------------------
# run：nbclient 缺席 → 结构化指引；在场 → mock 执行
# ---------------------------------------------------------------------------


def test_run_without_nbclient_returns_install_guidance(tmp_path, ctx, monkeypatch):
    import sys

    path = make_notebook(tmp_path / "r.ipynb", [("code", "print(1)")])
    monkeypatch.setitem(sys.modules, "nbclient", None)  # import 即 ImportError
    outcome = tool_notebook_run({"path": path}, ctx)
    assert outcome.is_error
    assert outcome.result["error"] == "dependency_missing"
    assert NBCLIENT_GUIDE in outcome.result["detail"]
    assert NOTEBOOK_RUN_SPEC.tier == "write"


def test_run_reports_cell_outputs_with_mocked_nbclient(tmp_path, ctx, monkeypatch):
    nbclient = pytest.importorskip("nbclient")

    path = make_notebook(tmp_path / "run.ipynb", [
        ("code", "print('hello')"),
        ("markdown", "# note"),
        ("code", "1 + 1"),
    ])

    class FakeNotebookClient:
        def __init__(self, notebook, **kwargs):
            self._notebook = notebook

        def execute(self):
            # 真实 nbclient 语义：原地更新 outputs / execution_count
            self._notebook.cells[0]["outputs"] = [
                nbformat.v4.new_output("stream", text="hello\n")
            ]
            self._notebook.cells[0]["execution_count"] = 1
            self._notebook.cells[2]["outputs"] = [
                nbformat.v4.new_output("execute_result", data={"text/plain": "2"})
            ]
            self._notebook.cells[2]["execution_count"] = 2

    monkeypatch.setattr(nbclient, "NotebookClient", FakeNotebookClient)
    outcome = tool_notebook_run({"path": path, "cell_index": 0}, ctx)
    assert not outcome.is_error
    result = outcome.result
    assert result["ranWholeNotebook"] is True  # 内核顺序性：整本执行
    assert len(result["cells"]) == 1  # cell_index 聚焦：只报 code cell 0
    assert result["cells"][0]["outputs"] == ["hello\n"]
    # 执行产物已落盘（审计回看）
    reloaded = nbformat.read(path, as_version=4)
    assert reloaded.cells[0]["execution_count"] == 1


def test_run_without_cell_index_reports_all_code_cells(tmp_path, ctx, monkeypatch):
    nbclient = pytest.importorskip("nbclient")

    path = make_notebook(tmp_path / "all.ipynb", [("code", "a"), ("code", "b")])

    class FakeNotebookClient:
        def __init__(self, notebook, **kwargs):
            self._notebook = notebook

        def execute(self):
            for i, cell in enumerate(self._notebook.cells):
                cell["outputs"] = [nbformat.v4.new_output("stream", text=f"out{i}")]
                cell["execution_count"] = i

    monkeypatch.setattr(nbclient, "NotebookClient", FakeNotebookClient)
    outcome = tool_notebook_run({"path": path}, ctx)
    assert not outcome.is_error
    assert [c["index"] for c in outcome.result["cells"]] == [0, 1]


def test_run_execution_error_is_structured(tmp_path, ctx, monkeypatch):
    nbclient = pytest.importorskip("nbclient")

    path = make_notebook(tmp_path / "err.ipynb", [("code", "1/0")])

    class ExplodingClient:
        def __init__(self, notebook, **kwargs):
            pass

        def execute(self):
            raise RuntimeError("CellExecutionError: ZeroDivisionError: division by zero")

    monkeypatch.setattr(nbclient, "NotebookClient", ExplodingClient)
    outcome = tool_notebook_run({"path": path, "cell_index": 0}, ctx)
    assert outcome.is_error
    assert outcome.result["error"] == "execution_error"
    assert "ZeroDivisionError" in outcome.result["detail"]


def test_run_validates_cell_index(tmp_path, ctx):
    path = make_notebook(tmp_path / "idx.ipynb", [("code", "1")])
    outcome = tool_notebook_run({"path": path, "cell_index": 99}, ctx)
    assert outcome.is_error  # nbclient 缺席也不放过越界 index（先验参数）


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def test_register_notebook_tools_registers_all_three():
    from zcode_py.tools import ToolRegistry
    from zcode_py.tools.notebook import register_notebook_tools

    registry = ToolRegistry()
    assert register_notebook_tools(registry) is True
    assert set(registry.names()) >= {"notebook_read", "notebook_write", "notebook_run"}
