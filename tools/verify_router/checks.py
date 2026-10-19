"""LV-01 .. LV-08: direct checks of the live router contract. Each writes spikes/results/live-LV-0N.json via Ctx.record().

Prompts are tiny and synthetic; the whole set costs a few cents at most. Nothing here prints or stores the API key.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess

from .context import REQUIRED_ALIASES, ROOT, Ctx

KNOWN_CODES = {"invalid_request", "authentication_error", "model_not_found", "unsupported_capability", "privacy_policy_violation", "data_policy_violation",
               "classification_not_permitted", "cost_limit_exceeded", "context_too_large", "rate_limited", "budget_exceeded", "provider_unavailable",
               "routing_failed", "timeout", "unknown_provider_error", "forbidden"}
PEOPLE_SCHEMA = {"type": "json_schema", "json_schema": {"name": "People", "schema": {"type": "object", "properties": {"people": {"type": "array", "items": {"type": "string"}}}, "required": ["people"]}}}


def _err(r) -> str | None:
    try:
        return r.json().get("error", {}).get("code")
    except Exception:
        return None


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def lv01(ctx: Ctx) -> None:
    h = ctx.client.get(f"{ctx.root_url}/health")
    m = ctx.client.get(f"{ctx.base_url}/models", headers=ctx.headers())
    ids = {d["id"] for d in m.json().get("data", [])} if m.status_code == 200 else set()
    table = {d["id"]: {k: d.get(k) for k in ("capabilities", "context_length", "max_output_tokens")} for d in m.json().get("data", [])} if m.status_code == 200 else {}
    ok = h.status_code == 200 and h.json().get("status") == "ok" and REQUIRED_ALIASES <= ids  # the router also reports version and gitSha
    ctx.record("LV-01", "pass" if ok else "fail", {"health": h.status_code, "models_status": m.status_code, "missing_aliases": sorted(REQUIRED_ALIASES - ids),
                                                   "extra_aliases": sorted(ids - REQUIRED_ALIASES), "alias_table": table})


def lv02(ctx: Ctx) -> None:
    bad = ctx.chat("oso/fast", key="definitely-not-a-key")
    good = ctx.chat("oso/fast")
    admin_headers = sorted(h for h in ("x-gateway-provider", "x-gateway-model") if h in good.headers)
    ok = (bad.status_code == 401 and _err(bad) == "authentication_error" and "x-request-id" in bad.headers
          and bad.json()["error"].get("request_id") == bad.headers["x-request-id"] and good.status_code == 200 and "x-gateway-request-id" in good.headers and not admin_headers)
    notes = "KEY HAS ADMIN SCOPE: provider/model headers were returned. Platform keys must be non-admin (router-contract §3)." if admin_headers else ""
    ctx.record("LV-02", "pass" if ok else "fail", {"bad_key_status": bad.status_code, "bad_key_code": _err(bad), "bad_key_has_request_id_header": "x-request-id" in bad.headers,
                                                   "good_status": good.status_code, "good_has_gateway_request_id": "x-gateway-request-id" in good.headers,
                                                   "admin_headers_present": admin_headers}, notes)


def lv03(ctx: Ctx) -> None:
    out, ok = {}, True
    for alias in ("oso/fast", "oso/general", "oso/code", "oso/reasoning"):
        r = ctx.chat(alias, max_tokens=64 if alias == "oso/reasoning" else 16)
        body = r.json() if r.status_code == 200 else {}
        row = {"status": r.status_code, "error": _err(r), "model_echo": body.get("model"), "usage": body.get("usage"), "has_content": bool((body.get("choices") or [{}])[0].get("message", {}).get("content")),
               "gateway_request_id": r.headers.get("x-gateway-request-id")}
        ok &= r.status_code == 200 and body.get("model") == alias and bool(body.get("usage"))
        out[alias] = row
    # router-contract §2 expects the OpenAI `developer` role to be rejected (zod accepts system|user|assistant|tool only); evidence, not pass/fail
    dev = ctx.client.post(f"{ctx.base_url}/chat/completions", headers=ctx.headers(), json={"model": "oso/fast", "max_tokens": 8, "messages": [{"role": "developer", "content": "hi"}]})
    out["developer_role_probe"] = {"status": dev.status_code, "error": _err(dev)}
    ctx.record("LV-03", "pass" if ok else "fail", out)


def lv04(ctx: Ctx) -> None:
    """Structured output per alias. The platform's structured purposes (compat/model-purposes.yaml) must resolve to aliases that return valid schema JSON; on this
    router (live run 2026-10-19) oso/general and oso/fast answer 422 unsupported_capability, which is recorded, not a failure."""
    import json

    import yaml

    msgs = "List the people mentioned: Sarah Chen told Michael Torres about Acme."
    out = {}
    for alias in ("oso/general", "oso/code", "oso/code-high", "oso/fast"):
        r = ctx.chat(alias, msgs, response_format=PEOPLE_SCHEMA, max_tokens=200)
        valid = False
        if r.status_code == 200:
            try:
                valid = isinstance(json.loads(r.json()["choices"][0]["message"]["content"])["people"], list)
            except Exception:
                valid = False
        out[alias] = {"status": r.status_code, "error": _err(r), "valid_schema_json": valid}
    purposes = yaml.safe_load((ROOT / "compat" / "model-purposes.yaml").read_text())["model_purposes"]
    structured = {a for p in purposes.values() if "structured_output" in (p.get("requires") or []) for a in (p["alias"], p.get("fallback_alias")) if a}
    missing = sorted(a for a in structured if not out.get(a, {}).get("valid_schema_json"))
    ok = not missing and out["oso/fast"]["error"] == "unsupported_capability"
    out["structured_purpose_aliases"] = sorted(structured)
    ctx.record("LV-04", "pass" if ok else "fail", out, "" if ok else f"Aliases used by structured purposes did not return valid schema JSON: {missing}; revisit compat/model-purposes.yaml")


def lv05(ctx: Ctx) -> None:
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    one = ctx.embed("hello world")
    dim = len(one.json()["data"][0]["embedding"]) if one.status_code == 200 else None
    batch = ctx.embed(["alpha", "beta beta", "gamma gamma gamma"])
    bvecs = [d["embedding"] for d in sorted(batch.json()["data"], key=lambda d: d["index"])] if batch.status_code == 200 else []
    again = ctx.embed("hello world")
    determinism = _cos(one.json()["data"][0]["embedding"], again.json()["data"][0]["embedding"]) if again.status_code == 200 and dim else None
    words = ("river stone paper window garden engine harbor lantern " * 200)
    long_text = enc.decode(enc.encode(words)[:700])
    prefix_text = enc.decode(enc.encode(long_text)[:480])      # well inside the model limit even after tokenizer differences
    lg, pf = ctx.embed(long_text), ctx.embed(prefix_text)
    long_info = {"status": lg.status_code, "error": _err(lg)}
    if lg.status_code == 200 and pf.status_code == 200:
        sim = _cos(lg.json()["data"][0]["embedding"], pf.json()["data"][0]["embedding"])
        long_info.update(prompt_tokens=lg.json()["usage"]["prompt_tokens"], cosine_vs_first_480_tokens=round(sim, 4),
                         behaviour="silent truncation near the 512-token model limit" if sim > 0.995 else "no truncation observed (text beyond 512 tokens changes the vector)")
    huge = ctx.embed(enc.decode(enc.encode(words * 6)[:9000]))
    q = ctx.embed(["query: where does sarah work", "passage: Sarah Chen works at Acme as an engineering manager", "passage: Results of the July cycling race"])
    sem = None
    if q.status_code == 200:
        v = [d["embedding"] for d in sorted(q.json()["data"], key=lambda d: d["index"])]
        sem = {"cos_related": round(_cos(v[0], v[1]), 4), "cos_unrelated": round(_cos(v[0], v[2]), 4)}
    ok = dim == 384 and len(bvecs) == 3 and all(len(v) == 384 for v in bvecs) and determinism is not None and determinism > 0.999 and bool(sem and sem["cos_related"] > sem["cos_unrelated"])
    ctx.record("LV-05", "pass" if ok else "fail", {"dimension": dim, "batch_vectors": len(bvecs), "determinism_cosine": determinism, "long_700_tokens": long_info,
                                                   "over_8192_tokens": {"status": huge.status_code, "error": _err(huge)}, "e5_semantic_sanity": sem},
               "Similarity range note: if cos_unrelated is already high (E5 behaviour) Graphiti's 0.6 sim_min_score must be re-tuned.")


def lv06(ctx: Ctx) -> None:
    matrix = {}
    for cls in ("public", "internal", "confidential", "restricted", "local_only"):
        row = {}
        for alias in ("oso/fast", "oso/general"):
            r = ctx.chat(alias, routing={"data_classification": cls})
            row[alias] = {"status": r.status_code, "error": _err(r)}
        matrix[cls] = row
    ok = all(v["error"] == "data_policy_violation" for v in matrix["local_only"].values())
    capped = [c for c in ("confidential", "restricted") if any(v["error"] == "classification_not_permitted" for v in matrix[c].values())]
    notes = (f"Key is capped below {capped}: ask the router admin to raise maxClassification for the kg-api / kg-worker keys." if capped else "")
    ctx.record("LV-06", "pass" if ok else "fail", {"matrix": matrix, "classes_blocked_by_key_cap": capped}, notes)


def lv07(ctx: Ctx) -> None:
    table, ok = {}, True
    for sim in ("primary_timeout", "provider_unavailable", "rate_limited", "http_500", "circuit_breaker_open"):
        row = {}
        for label, extra in (("fallback_default", {}), ("fallback_false", {"fallback": False})):
            r = ctx.chat("oso/general", routing={"simulate_failure": sim, **extra})
            row[label] = {"status": r.status_code, "error": _err(r), "retry_after": r.headers.get("retry-after")}
            ok &= r.status_code == 200 or _err(r) in KNOWN_CODES
        table[sim] = row
    s = ctx.chat("oso/general", "List people: Sarah and Michael", response_format=PEOPLE_SCHEMA, max_tokens=100, routing={"simulate_failure": "primary_timeout"})
    table["structured_on_oso/general+primary_timeout"] = {"status": s.status_code, "error": _err(s)}
    ctx.record("LV-07", "pass" if ok else "fail", table, "A non-200 for the structured row means oso/general has no structured-output fallback: request oso/structured.")


def lv08(ctx: Ctx) -> None:
    import uuid
    rid = str(uuid.uuid4())
    r = ctx.chat("oso/fast", rid=rid, metadata={"application_id": "kg-platform", "request_id": rid, "task_type": "extraction", "agent": "verify-router",
                                                "user_id": "hashed-principal-0000", "conversation_id": "hashed-workspace-0000"})
    echoed = r.headers.get("x-request-id") == rid and r.headers.get("x-gateway-request-id") == rid
    ctx.say(f"  LV-08 MANUAL STEP: in the router Admin UI open Operations > Request Logs and search for request id {rid}; confirm application_id=kg-platform, "
            "task_type=extraction and the user id are shown.")
    ctx.record("LV-08", "pass" if echoed else "fail", {"request_id": rid, "echoed_in_both_headers": echoed, "manual_confirmation": "PENDING - user checks Operations > Request Logs"},
               "Automated half passes when the router reuses our UUID as its request id. The Admin-UI half is manual.")


def lv09(ctx: Ctx) -> None:
    """Run the live pytest files (G-05, GR-11, GR-12, GR-13, TK-02). Needs the spike stack (Neo4j) for GR-11."""
    uv = shutil.which("uv") or str(ROOT / ".tools/uvenv/bin/uv")
    env = {**os.environ, "OSO_ROUTER_MODE": "live", "OSO_ROUTER_BASE_URL": ctx.base_url, "OSO_ROUTER_API_KEY": ctx.api_key, "GRAPHITI_TELEMETRY_ENABLED": "false"}
    proc = subprocess.run([uv, "run", "pytest", "-q", "-p", "no:cacheprovider", "-m", "live", "gateway/test_gateway_live.py", "graphiti/test_graphiti_live.py",
                           "tokenizer/test_tokenizer_live.py"], cwd=ROOT / "spikes", env=env, capture_output=True, text=True, timeout=3600)
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
    results = {}
    for cid in ("G-05", "GR-11", "GR-12", "GR-13", "TK-02"):
        f = ctx.results_dir / f"live-{cid}.json"
        if f.exists():
            import json
            results[cid] = json.loads(f.read_text())["status"]
    ctx.record("LV-09", "pass" if proc.returncode == 0 else "fail", {"pytest_exit": proc.returncode, "statuses": results, "tail": tail},
               "Per-check evidence is in spikes/results/<ID>.json (tag LIVE).")


def lv10(ctx: Ctx) -> None:
    uv = shutil.which("uv") or str(ROOT / ".tools/uvenv/bin/uv")
    env = {**os.environ, "OSO_ROUTER_MODE": "live", "OSO_ROUTER_BASE_URL": ctx.base_url, "OSO_ROUTER_API_KEY": ctx.api_key, "GRAPHITI_TELEMETRY_ENABLED": "false",
           "PYTHONPATH": f"{ROOT / 'spikes'}:{ROOT}:{ROOT / 'packages/common/src'}"}
    proc = subprocess.run([uv, "run", "python", "-m", "graphiti.record_fixtures", str(ctx.fixtures_dir / "recorded-sarah.json")], cwd=ROOT / "spikes", env=env,
                          capture_output=True, text=True, timeout=1800)
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-12:])
    ctx.record("LV-10", "pass" if proc.returncode == 0 else "fail", {"exit": proc.returncode, "tail": tail, "file": "tools/mock_router/fixtures/recorded-sarah.json"},
               "Fixtures contain synthetic Sarah content only. Re-run the mock tests after recording.")


ALL = [("LV-01", lv01), ("LV-02", lv02), ("LV-03", lv03), ("LV-04", lv04), ("LV-05", lv05), ("LV-06", lv06), ("LV-07", lv07), ("LV-08", lv08), ("LV-09", lv09), ("LV-10", lv10)]
