"""The notebooks: generated from their sources, committed without outputs, and each one runs top to bottom against the local stack (skipped without it)."""

import os
from pathlib import Path

import httpx
import nbformat
import pytest

from examples.notebooks.build import SRC, build
from examples.notebooks.run import HERE, execute

NOTEBOOKS = sorted(HERE.glob("[0-9][0-9]_*.ipynb"))


def _stack_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:8000/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def test_there_are_seven_notebooks() -> None:
    assert [p.name for p in NOTEBOOKS] == [
        "01_ingest_and_ledger.ipynb",
        "02_temporal_graph_as_of.ipynb",
        "03_curated_memory.ipynb",
        "04_context_build_and_budget.ipynb",
        "05_multi_agent_sharing.ipynb",
        "06_corrections_and_deletion.ipynb",
        "07_second_domain.ipynb",
    ]


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.stem)
def test_notebook_is_generated_from_its_source_and_has_no_outputs(path: Path) -> None:
    assert path.read_text() == build(SRC / f"{path.stem}.py"), f"run `python -m examples.notebooks.build` ({path.name} is out of date)"
    nb = nbformat.read(path, as_version=4)  # type: ignore[no-untyped-call]
    code = [c for c in nb.cells if c.cell_type == "code"]
    assert code and all(not c.outputs and c.execution_count is None for c in code), "commit notebooks without outputs"
    assert any(c.cell_type == "markdown" for c in nb.cells), "a notebook explains itself"


@pytest.mark.skipif(not _stack_up() and os.environ.get("KG_REQUIRE_STACK") != "1", reason="the dev stack is not up")
@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.stem)
def test_notebook_runs_top_to_bottom(path: Path) -> None:
    if path.stem == "05_multi_agent_sharing":
        try:
            httpx.get("http://127.0.0.1:8010/health", timeout=2).raise_for_status()
        except httpx.HTTPError:
            pytest.skip("the MCP server is not up: run `make mcp-up`")
    assert execute(path, timeout=600) < 600
