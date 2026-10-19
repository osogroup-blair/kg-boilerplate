import ast
from pathlib import Path

from examples._kit.mock import coverage_check
from examples._kit.report import Report, Step
from examples._kit.show import context_lines, show_context

ALLOWED = {"kg_platform_sdk", "httpx", "examples", "mcp", "openai", "yaml", "nbformat", "nbclient", "networkx", "matplotlib", "pytest"}


def test_examples_import_only_the_outside_world() -> None:
    std = set(__import__("sys").stdlib_module_names)
    bad: list[str] = []
    for f in Path("examples").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            mods = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            bad += [f"{f}: {m}" for m in mods if m and m.split(".")[0] not in std | ALLOWED]
    assert not bad, bad


def test_report_verdict_and_markdown() -> None:
    r = Report("demo")
    assert not r.passed
    r.add(Step("1", "a", ["AT-01"], True))
    assert r.passed and "| 1 | a | AT-01 | pass |" in r.to_markdown()
    r.add(Step("2", "b", [], False, error="boom"))
    assert not r.passed and "FAIL: boom" in r.to_markdown()


def test_show_context() -> None:
    out = {
        "status": "ok",
        "context_text": "x\n- fact one\n- fact two",
        "tokens_used": 40,
        "max_tokens": 600,
        "unknowns": [{"signal": "runway"}],
    }
    s = show_context(out)
    assert "(40/600 tokens)" in s and "unknowns: runway" in s
    assert context_lines(out) == ["- fact one", "- fact two"]


def test_coverage_check() -> None:
    assert coverage_check([{"when": {"schema_name": "A"}}], {"A", "B"}) == {"B"}


def test_scripted_rules_select_one_tool_per_turn() -> None:
    from examples.agents.common import handles, scripted_rules

    rules = scripted_rules("[m]", [("a", {"x": 1}), ("b", {})], "done")
    assert [r["when"].get("not_contains") for r in rules[:2]] == [['"tool": "a"'], ['"tool": "b"']]
    assert rules[1]["when"]["contains"] == ["[m]", '"tool": "a"']  # the second call only after the first tool's result
    assert rules[-1]["response"] == "done" and rules[-1]["when"]["contains"] == ["[m]", '"tool": "b"']
    assert handles("- fact [F:0de9d78b \u00b7 E:evt_002] and [M:03ce4d76 \u00b7 E:3be4c892] [U:d87495e7]") == {
        "F:0de9d78b",
        "M:03ce4d76",
        "U:d87495e7",
    }
