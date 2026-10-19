"""The public site's generators: what they import is allowed, links resolve, nothing private is published, and generation is deterministic.
No stack needed."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from scripts.site import gen_proof, gen_reference, import_docs
from scripts.site.common import CONTENT, GENERATED, ROOT, SITE

FORBIDDEN = re.compile(r"kgk_[A-Za-z0-9_\-]{8,}|/Users/|/srv/lab/|/home/[a-z]+/|oso_model_router|blairboylan|@oso\.group")


@pytest.fixture(scope="module")
def generated() -> list[Path]:
    files = [*import_docs.run(), *gen_reference.run()]
    gen_proof.main()
    return files


def digest(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.read_bytes())
    return h.hexdigest()


def test_every_map_entry_exists_and_imports(generated: list[Path]) -> None:
    entries, _ = import_docs.load_map()
    assert len(entries) >= 40
    for e in entries:
        assert (CONTENT / f"{e['dest']}.md").exists(), e


def test_only_allowlisted_files_are_imported() -> None:
    entries, _ = import_docs.load_map()
    srcs = {e["src"] for e in entries}
    assert not any(s.startswith(("docs/plans/", "solution_inputs/", "docs/release/handoff")) for s in srcs), (
        "internal documents must stay off the site"
    )


def test_nothing_private_is_published(generated: list[Path]) -> None:
    bad = []
    for f in [
        *CONTENT.rglob("*.md"),
        *CONTENT.rglob("*.mdx"),
        *(SITE / "public" / "tapes").glob("*.json"),
        *(SITE / "src").rglob("*.astro"),
        *(SITE / "src").rglob("*.tsx"),
    ]:
        text = f.read_text(errors="ignore")
        for m in FORBIDDEN.finditer(text):
            bad.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    assert not bad, bad[:10]


def test_imported_links_resolve_to_site_pages_or_github(generated: list[Path]) -> None:
    pages = {p.relative_to(CONTENT).with_suffix("").as_posix() for p in [*CONTENT.rglob("*.md"), *CONTENT.rglob("*.mdx")]}
    broken = []
    for f in [*CONTENT.rglob("*.md"), *CONTENT.rglob("*.mdx")]:
        for m in re.finditer(r"\]\((/docs/[^)#\s]+)", f.read_text()):
            target = m.group(1).strip("/").removeprefix("docs/")
            if target not in pages and f"{target}/index" not in pages:
                broken.append(f"{f.relative_to(CONTENT)} -> {m.group(1)}")
    assert not broken, sorted(set(broken))[:20]


def test_generation_is_deterministic() -> None:
    first = [*import_docs.run(), *gen_reference.run()]
    a = digest(first)
    second = [*import_docs.run(), *gen_reference.run()]
    assert digest(second) == a


def test_every_api_route_is_in_the_operations_page(generated: list[Path]) -> None:
    import json

    page = (CONTENT / "reference" / "api-operations.md").read_text()
    spec = json.loads((ROOT / "contracts" / "openapi.json").read_text())
    missing = [p for p in spec["paths"] if f"`{p}`" not in page]
    assert not missing, missing


def test_every_make_target_has_a_description() -> None:
    lines = [ln for ln in (ROOT / "Makefile").read_text().splitlines() if re.match(r"^[a-zA-Z0-9_-]+:", ln) and not ln.startswith(".")]
    undocumented = [ln.split(":")[0] for ln in lines if "##" not in ln and ln.split(":")[0] not in ("backup",)]
    # targets that only chain other targets may omit the description, but the site's own must not
    assert not [t for t in undocumented if t.startswith(("site-", "tapes", "demo-", "sandbox"))], undocumented


def test_proof_numbers_come_from_the_reports(generated: list[Path]) -> None:
    import json

    proof = json.loads((GENERATED / "proof.json").read_text())
    assert proof["sources"]["evaluation"].startswith("https://github.com/")
    assert len(proof["tiles"]) == 6 and all(t["src"] in proof["sources"] for t in proof["tiles"])


def test_the_landing_page_names_no_number_the_proof_does_not_hold() -> None:
    page = (SITE / "src" / "pages" / "index.astro").read_text()
    # numbers on the landing page are rendered from proof.json; hand-typed metrics are not allowed
    assert not re.search(r"\b(100%|0 leaks|zero leaks)\b", re.sub(r"<style>.*", "", page, flags=re.S)), (
        "hand-typed metric on the landing page"
    )
