"""GR-01..GR-05 (OFFLINE): install/connect, router wire format, ontology compiler, namespacing, deterministic writes.

Each test asserts what it found AND records the finding via results_plugin (note/evidence/set_status) for compatibility-report.md.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import GroupIdValidationError
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.prompts.models import Message
from graphiti_core.utils.ontology_utils.entity_types_utils import EntityTypeValidationError, validate_entity_types
from neo4j import GraphDatabase
from pydantic import BaseModel, create_model

import results_plugin as rp
from graphiti.conftest import EPISODES, ingest
from graphiti.harness import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, Rig, new_group, schema_name
from graphiti.ontology_compiler import ENTITY_RESERVED, OntologyCompileError, compile_ontology

UTC = timezone.utc
# Top-level fields the router's zod schema keeps (router src/api/chat/schema.ts, src/api/embeddings/schema.ts)
ROUTER_CHAT_FIELDS = {"model", "messages", "stream", "stream_options", "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "tools",
                      "tool_choice", "response_format", "user", "metadata", "routing"}
ROUTER_EMBED_FIELDS = {"model", "input", "user"}


def sync_cypher(q: str, **p):
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with d.session() as s:
            return [r.data() for r in s.run(q, **p)]
    finally:
        d.close()


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr01_install_connect_and_build_indices(rig):
    await rig.g.build_indices_and_constraints()                       # idempotent
    version = sync_cypher("CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS v, edition")[0]
    indexes = sync_cypher("SHOW INDEXES YIELD name, type RETURN name, type")
    try:
        apoc = sync_cypher("RETURN apoc.version() AS v")[0]["v"]
    except Exception:
        apoc = None
    assert version["v"].startswith("5.26") and version["edition"] == "community"
    assert len(indexes) >= 15 and any(i["type"] == "FULLTEXT" for i in indexes)
    rp.evidence("GR-01", neo4j_server=version["v"], neo4j_edition=version["edition"], index_count=len(indexes), apoc_version=apoc,
                graphiti_core="0.30.2", index_types=sorted({i["type"] for i in indexes}))
    rp.note("GR-01", "Graphiti 0.30.2 builds its range + fulltext indexes on Neo4j 5.26 Community (index types seen: "
                     + ", ".join(sorted({i["type"] for i in indexes})) + "). The Neo4j driver also schedules build_indices_and_constraints() "
                     "itself at construction, so an explicit call races it and logs harmless 'equivalent index already exists' errors.")
    rp.note("GR-01", "APOC is " + ("present" if apoc else "absent") + " in the spike image; Graphiti 0.30.2 core paths used here ran without needing it.")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr02_wire_format_chat_completions_only(rig, ontology, gid):
    await ingest(rig, gid, ontology, upto=2)
    paths = rig.paths()
    assert paths == {"/v1/chat/completions", "/v1/embeddings"}              # never /v1/responses
    chats, embeds = rig.chat_calls(), rig.embed_calls()
    assert chats and embeds
    chat_keys = set().union(*(c["body"].keys() for c in chats))
    assert chat_keys <= ROUTER_CHAT_FIELDS, f"fields the router would strip: {chat_keys - ROUTER_CHAT_FIELDS}"
    formats = {(c["body"].get("response_format") or {}).get("type") for c in chats}
    strict = {(c["body"].get("response_format") or {}).get("json_schema", {}).get("strict") for c in chats}
    assert formats == {"json_schema"} and strict == {None}
    embed_keys = set().union(*(e["body"].keys() for e in embeds))
    names = sorted({schema_name(c) for c in chats})
    prefixed = all(all(i.startswith("query: ") for i in (e["body"]["input"] if isinstance(e["body"]["input"], list) else [e["body"]["input"]])) for e in embeds)
    assert prefixed
    rp.evidence("GR-02", endpoints=sorted(paths), chat_body_fields=sorted(chat_keys), embedding_body_fields=sorted(embed_keys),
                response_format_types=sorted(formats), schema_names_seen=names, strict_sent=False,
                embedding_fields_stripped_by_router=sorted(embed_keys - ROUTER_EMBED_FIELDS), chat_calls_for_2_episodes=len(chats),
                embedding_calls_for_2_episodes=len(embeds))
    rp.note("GR-02", "OpenAIGenericClient posts only to /v1/chat/completions with {model,messages,temperature,max_tokens,response_format}; all are kept "
                     "by the router's zod schema. response_format is json_schema WITHOUT strict. The embedder posts /v1/embeddings (the SDK adds "
                     "encoding_format, which the router strips). Result: the generic client is router-compatible as is.")


async def test_gr02_default_openai_client_needs_responses_api(rig):
    """Graphiti's default OpenAIClient uses the Responses API, which the router does not expose -> must not be used."""
    llm = OpenAIClient(LLMConfig(api_key="test-key", base_url="http://mock/v1", model="oso/general", small_model="oso/general"), client=rig.g.llm_client.client)

    class Out(BaseModel):
        x: int = 0

    rig.reset_spy()
    with pytest.raises(Exception):
        await llm.generate_response([Message(role="user", content="hi")], response_model=Out)
    hit = [r["path"] for r in rig.spy.requests]
    assert any(p.endswith("/responses") for p in hit), hit
    rp.evidence("GR-02", default_client_paths=hit)
    rp.note("GR-02", "Graphiti's default OpenAIClient calls /v1/responses -> 404 on the router. Decision: always construct OpenAIGenericClient "
                     "(json_schema mode). Wrap it in an adapter that keeps this invariant under test.")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr03_ontology_compiler_dynamic_types(rig, gid):
    text = open("graphiti/personal_knowledge.yaml").read()
    comp = compile_ontology(text)
    assert set(comp.entity_types) == {"Person", "Organization", "Topic"} and set(comp.edge_types) == {"WORKS_AT", "CONSIDERING", "KNOWS"}
    assert comp.edge_type_map == {("Person", "Organization"): ["WORKS_AT"], ("Person", "Topic"): ["CONSIDERING"], ("Person", "Person"): ["KNOWS"]}
    assert comp.dropped_fields == {"Person": ["name"], "Organization": ["name"], "Topic": ["name"]}
    validate_entity_types(comp.entity_types)                               # Graphiti's own validator accepts the compiled types
    assert set(comp.entity_types["Person"].model_fields) == {"birth_date"}
    assert set(comp.edge_types["WORKS_AT"].model_fields) == {"role"}

    # The blueprint ontology as literally written collides with Graphiti's reserved EntityNode fields (Person.name).
    naive = create_model("Person", name=(str, ...), birth_date=(str | None, None))
    with pytest.raises(EntityTypeValidationError):
        validate_entity_types({"Person": naive})

    # A dynamic change takes effect at runtime (no code generation / import): add an attribute and recompile.
    comp2 = compile_ontology(text.replace("birth_date:", "pronouns: {type: string, required: false}\n      birth_date:"))
    assert "pronouns" in comp2.entity_types["Person"].model_fields

    # Unsafe or reserved identifiers are rejected before they can reach Cypher labels.
    for bad in ("Per son", "X;MATCH (n) DETACH DELETE n", "Entity", "Episodic", "1Person"):
        with pytest.raises(OntologyCompileError):
            compile_ontology(f"entity_types:\n  '{bad}':\n    attributes: {{}}\nrelationship_types: {{}}\n")
    with pytest.raises(OntologyCompileError):
        compile_ontology("entity_types:\n  A:\n    attributes: {}\nrelationship_types:\n  MENTIONS: {source: A, target: A}\n")
    with pytest.raises(OntologyCompileError):
        compile_ontology("entity_types:\n  A:\n    attributes: {uuid: {type: string}}\nrelationship_types: {}\n")
    with pytest.raises(OntologyCompileError):
        compile_ontology("entity_types:\n  A:\n    attributes: {}\nrelationship_types:\n  R: {source: A, target: Ghost}\n")

    # The compiled types are accepted by add_episode and produce typed labels + typed edges.
    results = await ingest(rig, gid, comp, upto=1)
    labels = {tuple(sorted(n.labels)) for n in results[0].nodes}
    assert ("Entity", "Person") in labels and ("Entity", "Organization") in labels
    assert [e.name for e in results[0].edges] == ["WORKS_AT"]
    schemas = {schema_name(c) for c in rig.chat_calls()}
    assert {"Person", "WORKS_AT"} <= schemas                                # attribute-extraction calls use the compiled models as JSON schema

    rp.evidence("GR-03", compiled_entity_types=sorted(comp.entity_types), compiled_edge_types=sorted(comp.edge_types),
                dropped_fields=comp.dropped_fields, entity_reserved_fields=sorted(ENTITY_RESERVED),
                person_json_schema=comp.entity_types["Person"].model_json_schema())
    rp.note("GR-03", "PASS with one required adaptation. create_model() builds types at runtime and Graphiti accepts them. The blueprint's own example "
                     "ontology (Person.attributes.name) COLLIDES with the reserved EntityNode field 'name' and Graphiti raises "
                     "EntityTypeValidationError. Compiler rule: 'name' is the node's own name, so it is not emitted (kept as the platform identity "
                     "alias); any other reserved collision is a compile error for the author. All compiled fields are Optional so one missing "
                     "attribute cannot fail an extraction; 'required' is enforced by the platform validator. Type/edge names are checked against "
                     "^[A-Za-z][A-Za-z0-9_]{0,63}$ and Graphiti's reserved labels/edges. The allowlisted-static-class fallback is NOT needed.")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr04_namespacing_group_id(rig, ontology):
    a, b = new_group(), new_group()
    ra = await ingest(rig, a, ontology, upto=1)
    rb = await ingest(rig, b, ontology, upto=1)
    node_b = next(n for n in rb[0].nodes if n.name == "Acme")
    edge_b = rb[0].edges[0]
    from graphiti_core.edges import EntityEdge as EE
    from graphiti_core.nodes import EntityNode as EN

    # (1) scoped search never crosses groups, in both directions
    for g, other in ((a, b), (b, a)):
        facts = await rig.g.search("Where does Sarah work?", group_ids=[g])
        assert facts and {f.group_id for f in facts} == {g}
        nodes = await EN.get_by_group_ids(rig.driver, [g])
        assert {n.group_id for n in nodes} == {g}
        eps = await rig.g.retrieve_episodes(datetime(2030, 1, 1, tzinfo=UTC), group_ids=[g])
        assert {e.group_id for e in eps} == {g}
        for sf in await EE.get_by_group_ids(rig.driver, [g]):
            assert sf.group_id == g

    # (2) TRAPS the adapter must close: searching without group_ids, and fetching by uuid, are NOT group-scoped.
    unscoped = await rig.g.search("Where does Sarah work?")
    leaked_groups = {f.group_id for f in unscoped}
    by_uuid_node = await EN.get_by_uuid(rig.driver, node_b.uuid)           # group A's code path fetching group B's node
    by_uuid_edge = await EE.get_by_uuid(rig.driver, edge_b.uuid)
    assert {a, b} <= leaked_groups                                             # superset: also returns groups left by earlier tests
    assert by_uuid_node.group_id == b and by_uuid_edge.group_id == b
    # an empty group id silently maps to the default '' partition
    r0 = await rig.g.add_episode(name="x", episode_body="Sarah Chen joined Acme as a software engineer in January 2022.", source_description="t",
                                 reference_time=datetime(2022, 1, 10, tzinfo=UTC), group_id=None, entity_types=ontology.entity_types,
                                 edge_types=ontology.edge_types, edge_type_map=ontology.edge_type_map)
    assert r0.episode.group_id == ""

    # (3) allowed group_id charset
    for ok in ("ws-123e4567-e89b-12d3-a456-426614174000", "ws_abc", "A1-b2_c3"):
        from graphiti_core.helpers import validate_group_id
        assert validate_group_id(ok)
    for bad in ("ws abc", "ws:abc", "ws.abc", "ws;DROP", "wś", "a/b"):
        with pytest.raises(GroupIdValidationError):
            from graphiti_core.helpers import validate_group_id
            validate_group_id(bad)

    # (4) database-per-tenant is not available on Community, and the Neo4j driver does not switch databases per group_id
    assert rig.driver.clone("tenant_x") is rig.driver
    with pytest.raises(Exception) as ex:
        sync_cypher("CREATE DATABASE tenant_probe")
    rp.evidence("GR-04", scoped_search_isolated=True, unscoped_search_groups_returned=len(leaked_groups), get_by_uuid_is_group_unaware=True,
                none_group_maps_to_default_partition=True, group_id_charset="^[a-zA-Z0-9_-]+$ (UUIDs with hyphens are valid)",
                create_database_error=str(ex.value)[:160], neo4j_driver_clone_is_noop=True)
    rp.note("GR-04", "Shared database + group_id property is the only option on Neo4j Community (CREATE DATABASE is unsupported; the driver's clone() "
                     "is a no-op so group_id never selects a database). Isolation holds ONLY if every call passes group_ids: search() with "
                     "group_ids=None, get_by_uuid and group_id=None (-> '') are NOT scoped. ADR-015: the graph adapter (packages/graph) must "
                     "derive group_id from the authenticated workspace, refuse None/empty, always pass group_ids, and re-check group_id after "
                     "any by-uuid fetch. Recommended mapping group_id = 'ws-<workspace uuid>'. Database-per-tenant needs Neo4j Enterprise "
                     "(decision deferred to production: record in ADR-015).")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr05_deterministic_writes_without_llm(rig, gid):
    ref = datetime(2022, 1, 10, tzinfo=UTC)
    sarah = EntityNode(name="Sarah Chen", group_id=gid, labels=["Person"], summary="", attributes={})
    acme = EntityNode(name="Acme", group_id=gid, labels=["Organization"], summary="", attributes={})
    rig.reset_spy()
    # Path A: direct save through the namespace API (zero chat calls; embeddings only).
    await rig.g.nodes.entity.save(sarah)
    await rig.g.nodes.entity.save(acme)
    edge = EntityEdge(group_id=gid, source_node_uuid=sarah.uuid, target_node_uuid=acme.uuid, name="WORKS_AT", fact="Sarah Chen works at Acme",
                      valid_at=ref, created_at=datetime(2022, 1, 12, tzinfo=UTC), episodes=[], attributes={"role": "Software Engineer"})
    await rig.g.edges.entity.save(edge)
    assert rig.chat_calls() == [] and len(rig.embed_calls()) == 3
    back = await EntityEdge.get_by_uuid(rig.driver, edge.uuid)
    assert back.fact == "Sarah Chen works at Acme" and back.attributes["role"] == "Software Engineer"
    injected_attr_keys = sorted(set(back.attributes) - {"role"})                      # Graphiti adds endpoint ids to attributes on read
    assert injected_attr_keys == ["source_uuid", "target_uuid"]
    assert back.valid_at == ref and back.created_at == datetime(2022, 1, 12, tzinfo=UTC)     # explicit timestamps survive a direct save

    # Path B: add_triplet on an EMPTY graph with valid_at set: does it call the LLM?
    g2 = new_group()
    s2 = EntityNode(name="Sarah Chen", group_id=g2, labels=["Person"], summary="", attributes={})
    a2 = EntityNode(name="Acme", group_id=g2, labels=["Organization"], summary="", attributes={})
    e2 = EntityEdge(group_id=g2, source_node_uuid=s2.uuid, target_node_uuid=a2.uuid, name="WORKS_AT", fact="Sarah Chen works at Acme", valid_at=ref, created_at=datetime.now(UTC), episodes=[])
    rig.reset_spy()
    await rig.g.add_triplet(s2, e2, a2)
    first = [schema_name(c) for c in rig.chat_calls()]

    # ...and a second triplet between the same nodes (candidate edges now exist -> edge dedupe/contradiction LLM call)
    e3 = EntityEdge(group_id=g2, source_node_uuid=s2.uuid, target_node_uuid=a2.uuid, name="WORKS_AT", fact="Sarah Chen manages engineering at Acme",
                    valid_at=datetime(2024, 6, 1, tzinfo=UTC), created_at=datetime.now(UTC), episodes=[])
    rig.reset_spy()
    await rig.g.add_triplet(s2, e3, a2)
    second = [schema_name(c) for c in rig.chat_calls()]
    assert second, "add_triplet is expected to call the LLM once an edge already exists between the nodes"
    assert "EdgeDuplicate" in second

    rp.evidence("GR-05", attributes_keys_injected_on_read=injected_attr_keys, direct_save_chat_calls=0, direct_save_embedding_calls=3, add_triplet_first_chat_schemas=first, add_triplet_second_chat_schemas=second)
    rp.note("GR-05", "EntityEdge.created_at is a REQUIRED constructor field (no default), so the caller always chooses the record time.")
    rp.note("GR-05", "Deterministic path = graphiti.nodes.entity.save + graphiti.edges.entity.save: ZERO chat calls (only embeddings, which a command "
                     "write needs for search anyway). add_triplet is NOT LLM-free: it dedupes/contradiction-checks via the LLM whenever an edge "
                     "between the nodes already exists (and for node resolution). Decision (ADR-016): P2 writes trusted commands with direct "
                     "save through a platform projection writer that sets uuid/valid_at/created_at itself; it never calls add_triplet/add_episode. "
                     "Supersession for commands is done by the writer (set invalid_at/expired_at on the previous edge), not by Graphiti. Quirk: an edge read "
                     "back carries source_uuid/target_uuid inside .attributes; the projection reader must strip them before comparing attributes.")
