"""GR-11..GR-13 (LIVE-DEFERRED): need the real router. Skipped unless OSO_ROUTER_MODE=live; run by `make verify-router` (LV-09).

Uses SYNTHETIC Sarah data only. Costs: ~25 small chat/embedding calls per episode on the open models (well under a cent each).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest

import results_plugin as rp
from graphiti.conftest import EPISODES
from graphiti.harness import Rig, new_group, schema_name
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("OSO_ROUTER_MODE") != "live", reason="OSO_ROUTER_MODE != live")]
UTC = timezone.utc


def _headers():
    return {"Authorization": f"Bearer {os.environ['OSO_ROUTER_API_KEY']}", "x-request-id": str(uuid.uuid4())}


def _url(path: str) -> str:
    return os.environ["OSO_ROUTER_BASE_URL"].rstrip("/") + path


async def test_gr11_sarah_extraction_through_the_real_router(ontology):
    rig = Rig(live=True, model="oso/code-high", small_model="oso/code-high")  # oso/general rejects structured output on this router (GR-12), so extraction uses oso/code-high (glm-5.2 first)
    await rig.g.build_indices_and_constraints()
    gid = new_group()
    per_episode = []
    for name, body, ref in EPISODES:
        rig.reset_spy()
        await rig.g.add_episode(name=name, episode_body=body, source_description="conversation_app", reference_time=ref, group_id=gid,
                                entity_types=ontology.entity_types, edge_types=ontology.edge_types, edge_type_map=ontology.edge_type_map)
        chats = rig.chat_calls()
        per_episode.append({"episode": name, "chat_calls": len(chats), "embedding_calls": len(rig.embed_calls()),
                            "prompt_tokens": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in chats),
                            "completion_tokens": sum((c.get("usage") or {}).get("completion_tokens", 0) for c in chats),
                            "latency_s": round(sum(c.get("latency_ms", 0) for c in chats + rig.embed_calls()) / 1000, 1),
                            "error_codes": sorted({c["error_code"] for c in chats + rig.embed_calls() if c.get("error_code")})})
    nodes = await EntityNode.get_by_group_ids(rig.driver, [gid])
    edges = await EntityEdge.get_by_group_ids(rig.driver, [gid])
    names = {n.name.lower(): sorted(n.labels) for n in nodes}
    sarah = [n for n in nodes if "sarah" in n.name.lower()]
    works = [e for e in edges if e.name == "WORKS_AT"]
    active_works = [e for e in works if e.invalid_at is None and e.expired_at is None]
    considering = [e for e in edges if e.name == "CONSIDERING" or "consider" in e.fact.lower()]
    checks = {
        "sarah_node_is_person": any("Person" in n.labels for n in sarah),
        "acme_node_is_organization": any("acme" in n.name.lower() and "Organization" in n.labels for n in nodes),
        "considering_edge_present": bool(considering),
        "considering_not_a_departure": not any(e.name == "WORKS_AT" and e.invalid_at and e.invalid_at.year >= 2026 for e in works),
        "single_active_works_at": len(active_works) == 1,
        "first_job_edge_closed_at_2024_06": any(e.invalid_at and (e.invalid_at.year, e.invalid_at.month) == (2024, 6) for e in works),
    }
    await rig.close()
    rp.evidence("GR-11", per_episode_cost=per_episode, node_labels=names, edges=[{"name": e.name, "fact": e.fact, "valid_at": e.valid_at, "invalid_at": e.invalid_at} for e in edges],
                checks=checks, models_note="alias oso/code (oso/general has no structured output on this router); physical model unknown to non-admin keys")
    rp.note("GR-11", "Extraction quality + cost/latency per episode through the real router. If any check is False, record which and decide: custom "
                     "extraction instructions, a different alias, or oso/structured (see router-contract §11).")
    assert all(checks.values()), checks


async def test_gr12_structured_output_aliases_and_single_candidate_fragility():
    schema = {"type": "json_schema", "json_schema": {"name": "People", "schema": {
        "type": "object", "properties": {"people": {"type": "array", "items": {"type": "string"}}}, "required": ["people"]}}}
    msgs = [{"role": "user", "content": "List the people mentioned: Sarah Chen told Michael Torres about Acme."}]
    out = {}
    async with httpx.AsyncClient(timeout=120) as c:
        async def call(alias, **extra):
            r = await c.post(_url("/chat/completions"), headers=_headers(),
                             json={"model": alias, "messages": msgs, "response_format": schema, "max_tokens": 200, "temperature": 0,
                                   "metadata": {"application_id": "kg-platform", "task_type": "extraction"}, **extra})
            body = r.json()
            return {"status": r.status_code, "error": (body.get("error") or {}).get("code"),
                    "valid_json": _valid_people(body) if r.status_code == 200 else None}
        for alias in ("oso/general", "oso/code", "oso/code-high", "oso/reasoning", "oso/frontier", "oso/fast"):
            out[alias] = await call(alias)
        out["oso/general+primary_timeout"] = await call("oso/general", routing={"simulate_failure": "primary_timeout"})
        out["oso/code+primary_timeout"] = await call("oso/code", routing={"simulate_failure": "primary_timeout"})
    works = [a for a in ("oso/general", "oso/code", "oso/code-high", "oso/reasoning", "oso/frontier") if out[a]["status"] == 200 and out[a]["valid_json"]]
    general_fragile = out["oso/general+primary_timeout"]["status"] != 200
    code_has_fallback = out["oso/code+primary_timeout"]["status"] == 200
    rp.evidence("GR-12", results=out, aliases_with_valid_structured_output=works, oso_general_single_candidate_fragile=general_fragile,
                oso_code_works_as_fallback=code_has_fallback, oso_fast_rejects_structured=out["oso/fast"]["error"] == "unsupported_capability",
                recommend_oso_structured_alias=general_fragile)
    rp.note("GR-12", "Settles compat/model-purposes.yaml: structured purposes use oso/code (fallback oso/code-high) because oso/general has no structured output; "
                     "request oso/structured (router-contract §11) if oso/general fails under simulated primary_timeout.")
    # decision 2026-10-19 (live run): oso/general and oso/fast answer unsupported_capability; oso/code is the primary for structured purposes
    assert out["oso/fast"]["error"] == "unsupported_capability" and "oso/code-high" in works


def _valid_people(body) -> bool:
    import json
    try:
        return isinstance(json.loads(body["choices"][0]["message"]["content"])["people"], list)
    except Exception:
        return False


async def test_gr13_concurrency_probe():
    results = {}
    async with httpx.AsyncClient(timeout=60) as c:
        async def one():
            r = await c.post(_url("/chat/completions"), headers=_headers(), json={"model": "oso/fast", "max_tokens": 8, "temperature": 0,
                             "messages": [{"role": "user", "content": "Reply: ok"}]})
            return r.status_code, r.headers.get("retry-after")
        for n in (2, 4, 8, 16):
            res = await asyncio.gather(*[one() for _ in range(n)])
            results[n] = {"ok": sum(1 for s, _ in res if s == 200), "rate_limited": sum(1 for s, _ in res if s == 429),
                          "retry_after": sorted({ra for s, ra in res if s == 429 and ra}), "other": sorted({s for s, _ in res if s not in (200, 429)})}
            await asyncio.sleep(2)
    clean = [n for n, r in results.items() if r["rate_limited"] == 0 and not r["other"]]
    best = max(clean) if clean else 1
    rec = max(1, best // 2)                      # Graphiti runs chat and embedding calls in the same semaphore; keep headroom
    rp.evidence("GR-13", probe=results, max_clean_parallelism=best, recommended_semaphore_limit=rec)
    rp.note("GR-13", f"Largest parallelism with no 429 on oso/fast: {best}. Recommended Graphiti SEMAPHORE_LIMIT = {rec} (half, for headroom), plus a client-side "
                     "token bucket under the key's RPM/TPM and Retry-After honoured by the ModelGateway.")
    assert clean
