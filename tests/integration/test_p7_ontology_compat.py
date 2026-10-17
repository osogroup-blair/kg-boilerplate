"""P7 WP0: adding `access` to the ontology spec must not change the hash of anything already published. Every stored version re-parses and re-hashes to its stored content hash,
and the shipped examples keep the hashes they had before the field existed (an absent `access` and an explicit `workspace` are the same document)."""

from __future__ import annotations

import pytest
from kg_ontology import OntologySpec, content_hash, parse_text
from sqlalchemy import text

from tests.support.p5 import owner_sql
from tests.support.tenants import AGENTS_YAML, CONTEXT_YAML, MEMORY_YAML, NO_MEMORY_YAML, make_tenant

pytestmark = pytest.mark.integration


async def test_every_published_version_reparses_and_keeps_its_stored_hash(api):
    for yaml_text in (NO_MEMORY_YAML, MEMORY_YAML, CONTEXT_YAML, AGENTS_YAML):
        await make_tenant(api, ontology=yaml_text)
    rows = await owner_sql(
        "SELECT id, ontology_key, version, spec, content_hash FROM ontology_versions WHERE state IN ('published', 'retired') AND content_hash IS NOT NULL AND spec ? 'ontology'"
    )  # (the raw-SQL fixtures of the db tests store a stub spec, which is not a published document)
    assert len(rows) >= 4
    for r in rows:
        spec = OntologySpec.model_validate(r.spec)
        assert content_hash(spec) == r.content_hash, (r.ontology_key, r.version)
    _ = text


def test_the_examples_before_p7_hash_as_they_did_and_the_new_one_differs():
    hashes = {}
    for name, yaml_text in (("v1.0.0", NO_MEMORY_YAML), ("v1.2.0", MEMORY_YAML), ("v1.3.0", CONTEXT_YAML), ("v1.4.0", AGENTS_YAML)):
        spec, issues = parse_text(yaml_text)
        assert spec is not None and not issues, name
        hashes[name] = content_hash(spec)
    assert len(set(hashes.values())) == 4
    # an explicit `access: workspace` is the same document as no `access` at all
    explicit = CONTEXT_YAML.replace(
        "  Person:\n    description: A human being\n", "  Person:\n    description: A human being\n    access: workspace\n", 1
    )
    assert explicit != CONTEXT_YAML
    spec, _ = parse_text(explicit)
    assert spec is not None and content_hash(spec) == hashes["v1.3.0"]
