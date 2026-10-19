---
title: Going live with a real router
description: Point the platform at a real OpenAI-compatible router, verify it, run the recorded live session, and re-measure. Includes swapping the router or a model.
sidebar:
  order: 4
---

In development every model call goes to a deterministic **mock router**. Going live means pointing the one model door (`ModelGateway`) at a real router. It spends money and sends data to a provider, so it is an **owner action**, done in this order.

## What you need

- An **OpenAI-compatible router** with chat completions that support `response_format: json_schema` (structured output) and an embeddings endpoint. See [models and the router](/docs/concepts/models-and-router/) and the [router contract](https://github.com/osogroup-blair/kg-boilerplate/blob/main/docs/router-contract.md).
- Two **non-admin** router keys (one for the API, one for the worker) whose scope allows `chat` and whose maximum data classification allows what you will send.
- A router alias for each purpose in `compat/model-purposes.yaml`. The platform names *purposes* (`graph_extraction`, `memory_extraction`, ...), never models; change the aliases there, not in code.

## 1. Verify the router

```bash
OSO_ROUTER_MODE=live OSO_ROUTER_API_KEY=<key> make verify-router
```

This runs a handful of small checks against the live router (the `LV-*` kit): structured output, embeddings (and their dimension), classification handling, error shapes. Fix anything it reports before going further.

## 2. Run the live session

```bash
make live-session
```

It prints a **cost estimate and asks first**, then runs the owner session (extraction, memory, context, consolidation, simulation), records calibration, and writes a proposed diff to `eval/reports/live/<date>/proposal.diff`. Read the diff.

```bash
make live-session-apply
```

applies the reviewed proposal: thresholds in `.env.example`, model-purpose status, ADR statuses and the latency profile.

## 3. Run the stack against the router

```bash
make up OSO_ROUTER_MODE=live OSO_ROUTER_API_KEY_API=<key> OSO_ROUTER_API_KEY_WORKER=<key>
```

The examples accept `--live` and then use the real router (and cost money); strict-mock helpers are disabled in that mode.

## 4. Re-measure

The sizing and SLO proposals were measured against a **modelled** router, so run `make perf-baseline` again, set your SLOs (DEC-11) and revisit the retention and backup windows (DEC-07).

## Swapping the router or a model later

- **A different router:** set the live URL and keys, re-run `make verify-router` and the live evaluations (`make eval-gate-live`).
- **A different chat model:** change the alias for the purpose in `compat/model-purposes.yaml`. Extraction needs structured output; the live kit showed that some aliases do not support it.
- **A different embedding model:** the dimension and model are pinned (`vector(384)`). A same-dimension change is a managed re-embed under a new version ([memory-reembed](/docs/operations/memory-reembed/)); a dimension change is a migration.

:::caution
Model-affecting changes (aliases, prices, prompts, the ranker, the Graphiti pin) need a **recorded live gate**: `make eval-gate-live`. CI requires the record for such a change.
:::

A gotcha that cost time: some CDNs reject the default `Python-urllib` user agent with 403 (error 1010). The platform's own clients (`httpx`, the OpenAI SDK) are accepted.
