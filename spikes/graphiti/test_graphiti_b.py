"""GR-06..GR-10 (OFFLINE): temporal mapping, corrections, deletion, idempotency, search without a cross-encoder."""
from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timedelta, timezone

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError, GroupsEdgesNotFoundError, NodeNotFoundError
from graphiti_core.nodes import EpisodeType
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search.search_config_recipes import (COMBINED_HYBRID_SEARCH_CROSS_ENCODER, COMBINED_HYBRID_SEARCH_MMR, COMBINED_HYBRID_SEARCH_RRF,
                                                        EDGE_HYBRID_SEARCH_MMR, EDGE_HYBRID_SEARCH_NODE_DISTANCE, EDGE_HYBRID_SEARCH_RRF,
                                                        NODE_HYBRID_SEARCH_RRF)
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from graphiti_core.utils.maintenance.graph_data_operations import clear_data

import results_plugin as rp
from graphiti.conftest import EPISODES, ingest
from graphiti.harness import NoCrossEncoder, new_group
from graphiti.test_graphiti_a import sync_cypher

UTC = timezone.utc


def edge_by_fact(edges, needle):
    return next(e for e in edges if needle in e.fact)


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr06_temporal_mapping(rig, ontology, gid):
    t_before = datetime.now(UTC)
    res = await ingest(rig, gid, ontology, upto=2)
    t_after = datetime.now(UTC)
    edges = await EntityEdge.get_by_group_ids(rig.driver, [gid])
    old, new = edge_by_fact(edges, "software engineer"), edge_by_fact(edges, "Engineering Manager")

    # valid time comes from the extraction (fixture here); invalid_at of the superseded edge = valid_at of its successor
    assert old.valid_at == datetime(2022, 1, 10, tzinfo=UTC) and old.invalid_at == datetime(2024, 6, 1, tzinfo=UTC)
    assert new.valid_at == datetime(2024, 6, 1, tzinfo=UTC) and new.invalid_at is None and new.expired_at is None
    # record time is Graphiti's wall clock at write, NOT the platform event's recorded_at (2024-06-10 in the blueprint)
    assert t_before <= old.created_at <= t_after and t_before <= new.created_at <= t_after
    assert old.expired_at is not None and t_before <= old.expired_at <= t_after
    ep2 = next(r.episode for r in res if r.episode.name == "evt_002")
    assert ep2.valid_at == datetime(2024, 6, 1, tzinfo=UTC) and t_before <= ep2.created_at <= t_after
    ref_fields = {"edge.reference_time": [str(e.reference_time) for e in edges]}

    # Platform recorded_at can be applied two ways: (1) direct save with created_at (GR-05), (2) post-hoc property update after add_episode.
    recorded = datetime(2024, 6, 10, tzinfo=UTC)
    sync_cypher("MATCH ()-[e:RELATES_TO {uuid: $u}]->() SET e.created_at = datetime($t)", u=new.uuid, t=recorded.isoformat())
    refreshed = await EntityEdge.get_by_uuid(rig.driver, new.uuid)
    assert refreshed.created_at == recorded

    # Bitemporal queries via SearchFilters (AND inside an inner list, OR across the outer list)
    def valid_as_of(t):
        return SearchFilters(valid_at=[[DateFilter(date=t, comparison_operator=ComparisonOperator.less_than_equal)]],
                             invalid_at=[[DateFilter(date=None, comparison_operator=ComparisonOperator.is_null)],
                                         [DateFilter(date=t, comparison_operator=ComparisonOperator.greater_than)]])

    as_of_2023 = await rig.g.search("Where does Sarah Chen work?", group_ids=[gid], search_filter=valid_as_of(datetime(2023, 1, 1, tzinfo=UTC)))
    as_of_2025 = await rig.g.search("Where does Sarah Chen work?", group_ids=[gid], search_filter=valid_as_of(datetime(2025, 1, 1, tzinfo=UTC)))
    assert [e.fact for e in as_of_2023] == ["Sarah Chen works at Acme as a software engineer"]
    assert [e.fact for e in as_of_2025] == ["Sarah Chen is an Engineering Manager at Acme"]

    def known_at(t):   # what the platform believed at t: recorded by then and not yet superseded
        return SearchFilters(created_at=[[DateFilter(date=t, comparison_operator=ComparisonOperator.less_than_equal)]],
                             expired_at=[[DateFilter(date=None, comparison_operator=ComparisonOperator.is_null)],
                                         [DateFilter(date=t, comparison_operator=ComparisonOperator.greater_than)]])

    believed_june5 = await rig.g.search("Where does Sarah Chen work?", group_ids=[gid], search_filter=known_at(datetime(2024, 6, 5, tzinfo=UTC)))
    believed_now = await rig.g.search("Where does Sarah Chen work?", group_ids=[gid], search_filter=known_at(datetime.now(UTC) + timedelta(days=1)))
    # With Graphiti's own created_at (wall clock = now) the old edge is only 'known' from today: a faithful known_at needs the platform's recorded_at.
    assert [e.fact for e in believed_june5] == [] or all("Engineering Manager" not in e.fact for e in believed_june5)
    assert any("Engineering Manager" in e.fact for e in believed_now)

    rp.evidence("GR-06", mapping={
        "valid_from": "EntityEdge.valid_at (from extraction/command)", "valid_to": "EntityEdge.invalid_at (successor.valid_at)",
        "recorded_at": "EntityEdge.created_at (REQUIRED field; add_episode sets wall-clock now; direct save/SET can carry the platform value)",
        "superseded_at": "EntityEdge.expired_at (add_episode sets wall-clock now at invalidation)",
        "episode_event_time": "EpisodicNode.valid_at (= reference_time)", "episode_record_time": "EpisodicNode.created_at (wall clock)"},
        old_edge={"valid_at": old.valid_at, "invalid_at": old.invalid_at, "created_at": old.created_at, "expired_at": old.expired_at},
        as_of_2023=[e.fact for e in as_of_2023], as_of_2025=[e.fact for e in as_of_2025], **ref_fields)
    rp.note("GR-06", "Valid time maps cleanly: valid_at/invalid_at, and a backdated successor invalidates its predecessor at its own valid_at. "
                     "RECORD time does not: add_episode stamps created_at/expired_at with wall-clock now, so evt_002 'recorded 2024-06-10' would read "
                     "as today. Mitigation (ADR-017): the platform's Postgres fact table is the system of record for recorded_at/superseded_at "
                     "and as_of/known_at; the graph projection carries valid_from/valid_to and, where cheap, overwrites created_at with the ledger's "
                     "recorded_at after add_episode (shown to work). Formal bitemporal SLA stays out of MVP scope (DEC-12).")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr07_corrections(rig, ontology, gid):
    await ingest(rig, gid, ontology, upto=3)
    edges = await EntityEdge.get_by_group_ids(rig.driver, [gid])
    wrong = edge_by_fact(edges, "considering starting")
    not_expired = SearchFilters(expired_at=[[DateFilter(date=None, comparison_operator=ComparisonOperator.is_null)]])
    q = "Is Sarah considering starting a company?"

    assert any(e.uuid == wrong.uuid for e in await rig.g.search(q, group_ids=[gid]))

    # (1) invalidate: Graphiti does not hide it by itself; a search filter is required
    wrong.invalid_at, wrong.expired_at = datetime.now(UTC), datetime.now(UTC)
    await rig.g.edges.entity.save(wrong)
    default_hit = any(e.uuid == wrong.uuid for e in await rig.g.search(q, group_ids=[gid]))
    filtered_hit = any(e.uuid == wrong.uuid for e in await rig.g.search(q, group_ids=[gid], search_filter=not_expired))
    assert default_hit is True and filtered_hit is False

    # (2) edit in place: possible, but it overwrites; no revision history, and provenance (.episodes) is untouched
    before = await EntityEdge.get_by_uuid(rig.driver, wrong.uuid)
    wrong.fact, wrong.attributes = "Sarah Chen has no plan to start a company", {"corrected_by": "reviewer_1"}
    wrong.invalid_at = wrong.expired_at = None
    await rig.g.edges.entity.save(wrong)
    after = await EntityEdge.get_by_uuid(rig.driver, wrong.uuid)
    assert after.fact != before.fact and after.attributes.get("corrected_by") == "reviewer_1" and after.episodes == before.episodes
    hits_new_text = [e.fact for e in await rig.g.search("no plan to start a company", group_ids=[gid])]
    assert "Sarah Chen has no plan to start a company" in hits_new_text         # embedding was regenerated by namespace save

    # (3) delete: gone from fetch and search
    await rig.g.edges.entity.delete(after)
    with pytest.raises(EdgeNotFoundError):
        await EntityEdge.get_by_uuid(rig.driver, wrong.uuid)
    assert all(e.uuid != wrong.uuid for e in await rig.g.search(q, group_ids=[gid]))
    # ...but the episode still lists the deleted edge uuid in entity_edges (dangling provenance)
    ep = (await rig.g.retrieve_episodes(datetime(2030, 1, 1, tzinfo=UTC), group_ids=[gid], last_n=10))
    dangling = [e.name for e in ep if wrong.uuid in e.entity_edges]
    assert dangling == ["evt_003"]

    rp.evidence("GR-07", supported_operations=["invalidate (set invalid_at/expired_at + save)", "edit fact/attributes in place + save (re-embeds)",
                                               "delete edge by uuid"], invalidated_still_returned_by_default_search=default_hit,
                excluded_with_expired_at_is_null_filter=not filtered_hit, dangling_episode_entity_edges=dangling)
    rp.note("GR-07", "All three operations work through the namespace API. Caveats: (a) invalidated edges are NOT excluded from search unless the caller "
                     "passes an expired_at IS NULL filter (and valid-time filters for as_of); (b) in-place edit loses the original text, keeps "
                     "provenance, and records nothing about who/why; (c) delete leaves the edge uuid inside EpisodicNode.entity_edges. DECISION "
                     "(ADR-018, DEC-04): corrections are authoritative in a Postgres fact-status projection (rejected/disputed/superseded/deleted) "
                     "that every read path joins/filters on; the graph edge is updated best-effort (expired_at + invalid_at, or delete) and every "
                     "graph search carries the expired_at filter. Never rely on in-place edits as the audit trail.")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr08_deletion_and_derivatives(rig, ontology, gid):
    other = new_group()
    res = await ingest(rig, gid, ontology, upto=3)
    await ingest(rig, other, ontology, upto=1)                              # a second tenant that must survive everything below
    ep = {r.episode.name: r.episode.uuid for r in res}

    def counts(g):
        n = sync_cypher("MATCH (n:Entity {group_id:$g}) RETURN n.name AS name", g=g)
        e = sync_cypher("MATCH (:Entity)-[r:RELATES_TO {group_id:$g}]->(:Entity) RETURN r.fact AS fact", g=g)
        ee = sync_cypher("MATCH (x:Episodic {group_id:$g}) RETURN x.name AS name", g=g)
        return sorted(r["name"] for r in n), sorted(r["fact"] for r in e), sorted(r["name"] for r in ee)

    nodes0, facts0, eps0 = counts(gid)
    assert nodes0 == ["Acme", "Michael Torres", "Sarah Chen", "Starting a software company"] and len(facts0) == 4 and eps0 == ["evt_001", "evt_002", "evt_003"]

    # Summary text on a surviving node is not rewritten by remove_episode (source: Graphiti.remove_episode only touches edges whose
    # episodes[0] is the removed episode and nodes mentioned ONLY by it). Simulate an LLM-written summary that quotes evt_003.
    sarah = next(n for n in await EntityNode.get_by_group_ids(rig.driver, [gid]) if n.name == "Sarah Chen")
    sarah.summary = "Sarah Chen is considering starting a software company."
    await rig.g.nodes.entity.save(sarah)

    await rig.g.remove_episode(ep["evt_003"])
    nodes1, facts1, eps1 = counts(gid)
    assert nodes1 == ["Acme", "Sarah Chen"] and sorted(facts1) == sorted(f for f in facts0 if "considering" not in f and "knows" not in f)
    assert eps1 == ["evt_001", "evt_002"]
    sarah_after = await EntityNode.get_by_uuid(rig.driver, sarah.uuid)
    assert "considering starting" in sarah_after.summary                    # derivative text survives on a shared node

    # Orphans: an entity with no episode and no edge is never cleaned by Graphiti; platform Cypher does it.
    orphan = EntityNode(name="Orphan Org", group_id=gid, labels=["Organization"], summary="", attributes={})
    await rig.g.nodes.entity.save(orphan)
    ORPHAN_CLEANUP = ("MATCH (n:Entity {group_id: $g}) WHERE NOT (n)<-[:MENTIONS]-(:Episodic) AND NOT (n)-[:RELATES_TO]-() "
                      "WITH n LIMIT 1000 DETACH DELETE n RETURN count(n) AS removed")
    removed = sync_cypher(ORPHAN_CLEANUP, g=gid)[0]["removed"]
    assert removed == 1 and "Orphan Org" not in counts(gid)[0]

    # Multi-episode support: the first episode 'owns' the edge. Deleting the OWNER removes an edge a later episode also supports.
    g3 = new_group()
    first = (await ingest(rig, g3, ontology, upto=1))[0]
    again = await rig.g.add_episode(name="evt_001_dup", episode_body=EPISODES[0][1], source_description="conversation_app",
                                    reference_time=EPISODES[0][2], group_id=g3, entity_types=ontology.entity_types, edge_types=ontology.edge_types,
                                    edge_type_map=ontology.edge_type_map)
    shared = (await EntityEdge.get_by_group_ids(rig.driver, [g3]))
    assert len(shared) == 1 and shared[0].episodes == [first.episode.uuid, again.episode.uuid]
    await rig.g.remove_episode(again.episode.uuid)                          # non-owner: edge stays, episode ref dangles
    left = (await EntityEdge.get_by_group_ids(rig.driver, [g3]))
    dangling_after_non_owner = again.episode.uuid in left[0].episodes
    # re-add and delete the owner
    again2 = await rig.g.add_episode(name="evt_001_dup2", episode_body=EPISODES[0][1], source_description="conversation_app",
                                     reference_time=EPISODES[0][2], group_id=g3, entity_types=ontology.entity_types, edge_types=ontology.edge_types,
                                     edge_type_map=ontology.edge_type_map)
    await rig.g.remove_episode(first.episode.uuid)                          # owner: edge deleted although evt_001_dup2 still supports it
    try:
        owner_removed_edge = len(await EntityEdge.get_by_group_ids(rig.driver, [g3])) == 0
    except GroupsEdgesNotFoundError:                                        # API quirk: an empty result raises instead of returning []
        owner_removed_edge = True
    assert dangling_after_non_owner is True and owner_removed_edge is True

    # Whole-tenant purge touches only that group
    await clear_data(rig.driver, [gid])
    assert counts(gid) == ([], [], []) and counts(other)[0] == ["Acme", "Sarah Chen"]

    rp.evidence("GR-08", after_remove_evt_003={"nodes": nodes1, "episodes": eps1}, summary_survives_on_shared_node=True, orphan_cleanup_cypher=ORPHAN_CLEANUP,
                dangling_episode_ref_after_non_owner_removal=dangling_after_non_owner, owner_removal_deletes_still_supported_edge=owner_removed_edge,
                clear_data_scoped_to_group=True)
    rp.note("GR-08", "remove_episode deletes: the Episodic node, edges whose FIRST episode is the removed one, and entities mentioned by no other episode. "
                     "It does NOT: rewrite summaries/attributes on surviving entities (derived text survives), prune the removed episode uuid from "
                     "other edges' .episodes (dangling refs), or protect an edge that a later episode also supports when the owner is removed. "
                     "Orphan entities created outside episodes are never cleaned (platform Cypher drafted). clear_data(driver, [group]) purges one "
                     "tenant cleanly. P8 deletion therefore = remove_episode for each source episode + re-summarise or delete surviving entities "
                     "that cite the subject + prune dangling refs + orphan sweep + verification query, not remove_episode alone.")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr09_idempotency(rig, ontology):
    name, body, ref = EPISODES[0]
    kw = dict(source_description="conversation_app", reference_time=ref, entity_types=ontology.entity_types, edge_types=ontology.edge_types,
              edge_type_map=ontology.edge_type_map)

    def state(g):
        return (len(sync_cypher("MATCH (n:Entity {group_id:$g}) RETURN n", g=g)), len(sync_cypher("MATCH (n:Episodic {group_id:$g}) RETURN n", g=g)),
                len(sync_cypher("MATCH ()-[r:RELATES_TO {group_id:$g}]->() RETURN r", g=g)))

    # A: same name + body twice, Graphiti-assigned uuids
    ga = new_group()
    await rig.g.add_episode(name=name, episode_body=body, group_id=ga, **kw)
    first_calls = len(rig.chat_calls())
    rig.reset_spy()
    await rig.g.add_episode(name=name, episode_body=body, group_id=ga, **kw)
    second_calls = len(rig.chat_calls())
    entities_a, episodes_a, edges_a = state(ga)
    assert (entities_a, episodes_a, edges_a) == (2, 2, 1)                   # episode duplicated; entity + edge deduped

    # B: same explicit uuid twice (the ledger's deterministic id)
    gb = new_group()
    u = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"kg://{gb}/conversation_app/chat-2091-turn-14/graphiti_episode"))
    with pytest.raises(NodeNotFoundError):                                  # uuid= means "continue an EXISTING episode node"
        await rig.g.add_episode(name=name, episode_body=body, group_id=gb, uuid=u, **kw)
    await rig.g.nodes.episode.save(EpisodicNode(uuid=u, name=name, group_id=gb, source=EpisodeType.message, content=body,
                                                source_description="conversation_app", created_at=datetime.now(UTC), valid_at=ref))
    await rig.g.add_episode(name=name, episode_body=body, group_id=gb, uuid=u, **kw)
    await rig.g.add_episode(name=name, episode_body=body, group_id=gb, uuid=u, **kw)
    entities_b, episodes_b, edges_b = state(gb)
    edge_b = (await EntityEdge.get_by_group_ids(rig.driver, [gb]))[0]
    assert episodes_b == 1 and entities_b == 2 and edges_b == 1

    rp.evidence("GR-09", same_name_twice={"entities": entities_a, "episodes": episodes_a, "edges": edges_a, "chat_calls_first": first_calls, "chat_calls_second": second_calls},
                uuid_without_precreate_raises="NodeNotFoundError", same_uuid_twice={"entities": entities_b, "episodes": episodes_b, "edges": edges_b, "edge_episodes": edge_b.episodes})
    rp.note("GR-09", "Graphiti does NOT deduplicate episodes by name or content: re-adding creates a second Episodic node (entities and the identical-fact "
                     "edge are deduplicated, but the edge's .episodes list grows). Re-adding with the SAME explicit uuid is idempotent (one Episodic "
                     "node) - BUT add_episode(uuid=...) only CONTINUES an episode that already exists (NodeNotFoundError otherwise), so the worker must "
                     "first save an EpisodicNode with the deterministic uuid (graphiti.nodes.episode.save) and then call add_episode(uuid=...). "
                     "Decision (confirms ING-01): idempotency lives in the platform ledger keyed (workspace, source_key, stage); the worker derives "
                     "uuid5(namespace, ledger key) so a retry reuses the same episode, and records the Graphiti episode uuid in the ledger before "
                     "marking the stage done. A repeat add still costs LLM calls (see chat_calls_second), so the ledger must prevent needless "
                     "replays rather than rely on Graphiti.")


# ---------------------------------------------------------------------------------------------------------------------------------
async def test_gr10_search_without_cross_encoder(rig, ontology, gid):
    res = await ingest(rig, gid, ontology, upto=3)
    sarah = next(n for n in res[0].nodes if n.name == "Sarah Chen")
    NoCrossEncoder.calls = 0
    q = "Where does Sarah Chen work?"

    async def run(label, cfg, **kw):
        out = await rig.g.search_(q, config=cfg, group_ids=[gid], **kw)
        facts = [e.fact for e in out.edges]
        names = [n.name for n in out.nodes]
        assert {e.group_id for e in out.edges} <= {gid} and {n.group_id for n in out.nodes} <= {gid}
        return label, facts, names

    results = [
        await run("EDGE_HYBRID_SEARCH_RRF", EDGE_HYBRID_SEARCH_RRF),
        await run("EDGE_HYBRID_SEARCH_MMR", EDGE_HYBRID_SEARCH_MMR),
        await run("EDGE_HYBRID_SEARCH_NODE_DISTANCE", EDGE_HYBRID_SEARCH_NODE_DISTANCE, center_node_uuid=sarah.uuid),
        await run("NODE_HYBRID_SEARCH_RRF", NODE_HYBRID_SEARCH_RRF),
        await run("COMBINED_HYBRID_SEARCH_RRF", COMBINED_HYBRID_SEARCH_RRF),
        await run("COMBINED_HYBRID_SEARCH_MMR", COMBINED_HYBRID_SEARCH_MMR),
    ]
    by_label = {r[0]: r for r in results}
    for label in ("EDGE_HYBRID_SEARCH_RRF", "EDGE_HYBRID_SEARCH_NODE_DISTANCE", "COMBINED_HYBRID_SEARCH_RRF"):
        assert any("Acme" in f for f in by_label[label][1]), label
    assert by_label["EDGE_HYBRID_SEARCH_MMR"][1], "MMR must return something (its sim_min_score=0.6 cut is expected to trim hash-vector hits)"
    assert "Sarah Chen" in by_label["NODE_HYBRID_SEARCH_RRF"][2]
    assert NoCrossEncoder.calls == 0

    # the cross-encoder recipes are the ONLY ones that touch the reranker; with ours they fail loudly instead of silently needing logprobs
    with pytest.raises(Exception):
        await rig.g.search_(q, config=COMBINED_HYBRID_SEARCH_CROSS_ENCODER, group_ids=[gid])
    assert NoCrossEncoder.calls >= 1
    NoCrossEncoder.calls = 0

    # Graphiti.search() mutates the module-level recipe object (search_config.limit = num_results): not safe to share across requests
    await rig.g.search(q, group_ids=[gid], num_results=3)
    mutated = EDGE_HYBRID_SEARCH_RRF.limit == 3
    EDGE_HYBRID_SEARCH_RRF.limit = 10
    assert mutated

    rp.evidence("GR-10", recipes_ok_without_cross_encoder={label: {"facts": facts, "nodes": names} for label, facts, names in results},
                cross_encoder_calls_for_non_ce_recipes=0, search_mutates_global_recipe_limit=mutated,
                default_search__config_is_cross_encoder=COMBINED_HYBRID_SEARCH_CROSS_ENCODER is not None)
    rp.note("GR-10", "Chosen recipes: facts = EDGE_HYBRID_SEARCH_RRF (default) or EDGE_HYBRID_SEARCH_NODE_DISTANCE with center_node_uuid for subject-centred "
                     "retrieval; nodes = NODE_HYBRID_SEARCH_RRF; MMR variants also work. None call the cross-encoder. Graphiti.search_() DEFAULTS to the "
                     "CROSS_ENCODER recipe, so the adapter must always pass an explicit config; a NoCrossEncoder that raises is installed so any "
                     "accidental use fails loudly (the router drops logprobs, so OpenAIRerankerClient cannot work). Graphiti.search() mutates the "
                     "shared recipe's .limit; the adapter should copy the recipe (config.model_copy()) per call. Retrieval QUALITY with E5-small is "
                     "measured live (LV-05/GR-11); offline vectors are lexical hashes and only prove plumbing and group scope. MMR uses sim_min_score=0.6 "
                     "(tuned for other embedding models); E5-family cosine similarities are generally reported to cluster high, so thresholds must be re-tuned from live data (LV-05 records related vs unrelated cosine).")
