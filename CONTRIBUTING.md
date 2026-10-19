# Contributing

Thanks for helping. This is a boilerplate for agent memory and knowledge graphs, and the thing that matters most is that what the documentation says is true. The site is at the repository's homepage; the docs it shows live in this repository.

## Set up

You need Docker (about 6 GB of memory and 20+ GB of free disk), Node 20 and git. Python runs through `uv`, which `make tools` installs.

```bash
git clone https://github.com/osogroup-blair/kg-boilerplate.git && cd kg-boilerplate
make tools sync admin-install
make up seed                     # postgres, neo4j, mock router, migrations, api, worker
make lint typecheck test         # fast checks, no stack needed
```

`make help` lists every target. Use the Makefile targets rather than ad-hoc commands.

## Before you open a pull request

| Run | When |
|---|---|
| `make lint typecheck test` | always |
| `make test-int test-security test-contract` | if you touched `apps/`, `packages/`, `sdk/` (needs `make up seed`) |
| `make openapi` | after any API change (it regenerates `contracts/openapi.json`, the SDK models and the console client) |
| `make reset up seed` | after any migration edit (the dev database must be rebuilt) |
| `make examples test-examples` | if you touched examples, the mock router or the pipeline |
| `make site-check` | if you touched `apps/site`, `docs/`, or anything the site generates from |

Never run `make down` or `make reset` in the middle of a test run: the integration suites share one Postgres and one Neo4j. Keep several GB of host disk free; a full disk wedges Docker.

## Rules that are not negotiable

- **No secrets in git.** `.local/`, `.env` and `deploy/compose/secrets/` are ignored; keep it that way.
- **Model-affecting changes need a recorded live gate.** Aliases, prices, prompts, the ranker or packer, and the Graphiti pin need `make eval-gate-live` (an owner action that costs model calls). CI checks for the record.
- **Security rules have negative tests.** Change a rule only with the test that proves it still refuses what it should.
- **Platform code never mentions an example domain.** Domains are configuration (`examples/domains/*`); a test enforces it.
- **Docs are not copied.** The site imports `docs/`, `examples/**/README.md` and the root Markdown files at build time, and generates the reference from the code. Edit the source, not the site's copy.

## Commits and pull requests

One commit per coherent change, with a message that says what and why. Say what you did **not** verify. For a pull request, describe the behaviour change and how you tested it; a screenshot helps for console or site changes.

## Adding things

- **An example or a domain:** copy `examples/domains/support/` (smallest) or `crm/`, follow [the first-domain tutorial](apps/site/src/content/docs/docs/tutorials/first-domain.mdx) and [docs/guides/add-a-domain.md](docs/guides/add-a-domain.md), and add a test in `tests/examples`.
- **A recorded playground:** see [How the recordings are made](apps/site/src/content/docs/docs/reference/tapes.md); re-record with `make tapes` and check with `make tapes-check`.
- **A docs page:** hand-written pages live in `apps/site/src/content/docs/docs/`. To publish a repository file, add it to `apps/site/import-map.yaml` (an allowlist).
- **An ADR:** `docs/adr/ADR-NNN-title.md`, with status, context and decision.

## Reporting bugs and asking questions

Use the issue templates. For security problems, do **not** open a public issue: see [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contributions are licensed under the Apache License 2.0 (see [LICENSE](LICENSE)).
