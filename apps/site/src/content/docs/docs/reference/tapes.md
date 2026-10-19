---
title: How the recordings are made
description: The site's playgrounds replay real recordings of the platform. This page says how they are captured, normalised and checked, and how to re-record them.
sidebar:
  order: 30
---

The [playgrounds](/see-it/) run entirely in your browser with no backend, replaying **tapes**: recordings of real HTTP traffic captured while the Sarah and CRM examples ran against the platform. Nothing in them is mocked up; they are what the API returned.

## What is recorded

| Tape | What it holds |
|---|---|
| `sarah` | All eleven steps of the Sarah scenario: every request and response, plus a graph snapshot after each step |
| `context-matrix` | After step 7, `POST /v1/context/build` across three tasks × the planning, research and every admin profile × four budgets (64, 250, 600, 1200) × the callers allowed to use them |
| `time` | Sarah's relationships at thirteen `as_of` dates from 2022 to 2027, plus two `known_at` reads |
| `permissions` | Five questions asked by four callers, including the identical 404 bodies |
| `write-path` | One conversation event, its `202` and each distinct stage state it passed through |
| `crm` | The CRM domain's setup, commands, conversations, graphs and the nine benchmark context builds |

## How

`tools/replay_capture/record.py` wraps `httpx` (which the SDK and the example kit use) so it sees exactly what a client sees. It then runs the existing example code (`examples.sarah.steps.Scenario`, the CRM runner) in fresh workspaces and never reads a database: the examples are black-box and so is the recorder.

```bash
make up seed
make tapes          # record into .local/tapes/_raw.json (git-ignored: it holds plaintext keys), then normalise into apps/site/public/tapes/
```

## Normalisation

`tools/replay_capture/normalize.py` turns the raw recording into the committed tapes:

- **UUIDs become stable aliases** (`sarah`, `acme`, `fact_works_at_manager`, `id_17`), consistent across tapes.
- **Server timestamps become an ordered synthetic clock**; domain times (`valid_from`, `as_of`, `occurred_at`, ...) are kept.
- **Keys and tokens are replaced** with `kgk_EXAMPLE`, and request ids with a placeholder, so the two 404 bodies in the permission lens are byte-identical.
- The workspace slug becomes `demo-workspace`.

Narration shown beside each step lives in `tools/replay_capture/narration/sarah.yaml`, so wording can change without re-recording.

## Checks

`make tapes-check` validates every tape against `tools/replay_capture/tape.schema.json`, greps all of them for key-shaped strings, and checks that every step the narration names exists. Re-recording is allowed to vary in timing and counts (polling), so it compares the **shape** (steps, methods, paths, statuses), not bytes.

## Want to make your own?

Point `record.py` at your own scenario runner: any code that uses `httpx` or the SDK against a local stack can be recorded the same way.
