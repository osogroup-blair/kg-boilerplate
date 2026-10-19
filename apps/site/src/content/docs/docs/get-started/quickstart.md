---
title: Quickstart
description: Clone, start the stack, run the examples and open the console in about ten minutes, with no model key.
sidebar:
  order: 2
---

You will run the whole platform on your machine, watch the Sarah scenario pass, and open the admin console. Nothing here needs a model key: the examples run against a **mock router** that answers deterministically, so you get the same results every time.

## Before you start

| You need | Notes |
|---|---|
| Docker | About 6 GB of memory for Docker, and 20 GB of free disk. A full disk wedges Docker. |
| Node 20 | For the admin console. |
| git | |
| Python | You don't install it: `uv` (installed by `make tools`) fetches Python 3.12. |

## 1. Clone and install

```bash test
git clone https://github.com/osogroup-blair/kg-boilerplate.git && cd kg-boilerplate
make tools sync admin-install examples-install
```

`make tools` installs `uv` into `.tools/`, `make sync` installs the Python workspace at the exact versions in `uv.lock`, `admin-install` installs the console, and `examples-install` adds the notebook tooling.

## 2. Start the stack

```bash test
make up seed
```

This builds and starts Postgres, Neo4j, the mock router, runs the migrations and starts the API and the worker (about two minutes the first time). `make seed` creates two demo workspaces and prints a set of API keys **once**; they are also written to `.local/dev-credentials.json` (git-ignored, mode 0600).

Check it is healthy:

```bash
curl -s http://localhost:8000/health
```

## 3. Run the examples

```bash test
make examples
```

This starts the MCP server and runs the Sarah scenario, the CRM domain, the support-desk domain and the agent examples. Each ends with a pass/fail line:

```text
PASS: 11/11 steps          # Sarah
...benchmark PASS           # CRM
PASS: 8/8 steps            # support desk
```

Each example creates its own workspace (`ex-<name>-<time>`), so they never interfere with each other or with the demo data. What each one proves is in [the examples at a glance](/docs/tutorials/examples-overview/).

## 4. Open the console

```bash
make admin-dev
```

Open <http://localhost:5273> and paste a key from `.local/dev-credentials.json`. Use `demo/admin@demo` for the full console, or `other_demo/admin@other` to see a *different* workspace that never sees the first one's data. The [console tour](/docs/get-started/tour-of-the-console/) says what each screen is for.

## 5. Ask for a context yourself

Every example leaves its keys and ids in `.local/examples/<name>-latest.json` (git-ignored) so you can explore the workspace it built. After `make demo-sarah`:

```bash
F=.local/examples/sarah-latest.json
KEY=$(python3 -c "import json;print(json.load(open('$F'))['keys']['planning-agent'])")
SARAH=$(python3 -c "import json;print(json.load(open('$F'))['ids']['sarah'])")
curl -s -X POST http://localhost:8000/v1/context/build \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"task\":\"Help Sarah assess the implications of starting a business\",\"subject_ids\":[\"$SARAH\"],\"profile\":\"planning\",\"max_tokens\":600,\"project\":\"newco-plan\"}"
```

The response has `context_text` (what you put in a prompt), `facts`, `memories`, `unknowns`, `citations` and a `budgets` block that proves it stayed inside 600 tokens. Change `max_tokens` to 150, or add `"as_of":"2027-03-01T00:00:00Z"`, and run it again. The [API recipes](/docs/reference/api-recipes/) have this and more in curl, Python and MCP.

## 6. Then

```bash
make notebooks   # the seven notebooks, executed (copies with outputs in examples/notebooks/out/)
make check       # lint, types, import contracts, unit/integration/security/contract tests, console tests
make help        # every target
```

## Next steps

- [Model your own domain](/docs/tutorials/first-domain/): a support desk from an empty ontology.
- [Connect an agent](/docs/tutorials/connect-an-agent-sdk/) or [Claude Code over MCP](/docs/tutorials/connect-an-agent-mcp/).
- If something does not work, see [troubleshooting](/docs/get-started/troubleshooting/).

:::caution
Never run `make down` or `make reset` in the middle of a test run: the integration suites share one Postgres and one Neo4j. `make reset` drops the volumes and the example workspaces with them.
:::
