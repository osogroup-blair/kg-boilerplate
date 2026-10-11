from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from neo4j import GraphDatabase

from graphiti.harness import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, Rig, new_group
from graphiti.ontology_compiler import compile_ontology

ONTOLOGY = Path(__file__).parent / "personal_knowledge.yaml"
UTC = timezone.utc

# Synthetic Sarah scenario (blueprint 06-reference/01). Wording matches tools/mock_router/fixtures/sarah.json rules.
EPISODES = [
    ("evt_001", "Sarah Chen joined Acme as a software engineer in January 2022.", datetime(2022, 1, 10, tzinfo=UTC)),
    ("evt_002", "Sarah Chen was promoted to Engineering Manager at Acme effective 2024-06-01.", datetime(2024, 6, 1, tzinfo=UTC)),
    ("evt_003", "Sarah Chen told Michael Torres she was considering starting a software company but had not decided to leave Acme.",
     datetime(2026, 10, 10, tzinfo=UTC)),
]


@pytest.fixture(scope="session", autouse=True)
def _clean_neo4j():
    """Start from an empty graph; fail loudly (not skip) if the spike stack is down."""
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        drv.verify_connectivity()
    except Exception as e:  # pragma: no cover
        pytest.exit(f"Neo4j not reachable at {NEO4J_URI}: run `make spike-up` first ({e})", returncode=2)
    with drv.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    drv.close()


@pytest.fixture()
async def rig():
    r = Rig()
    await r.g.build_indices_and_constraints()
    yield r
    await r.close()


@pytest.fixture(scope="session")
def ontology():
    return compile_ontology(ONTOLOGY.read_text())


@pytest.fixture()
def gid() -> str:
    return new_group()


async def ingest(rig: Rig, group_id: str, ontology, upto: int = 3, **kw):
    """Run evt_001..evt_0NN through add_episode with the compiled ontology. Returns the AddEpisodeResults list."""
    out = []
    for name, body, ref in EPISODES[:upto]:
        out.append(await rig.g.add_episode(name=name, episode_body=body, source_description="conversation_app", reference_time=ref,
                                           group_id=group_id, entity_types=ontology.entity_types, edge_types=ontology.edge_types,
                                           edge_type_map=ontology.edge_type_map, **kw))
    return out
