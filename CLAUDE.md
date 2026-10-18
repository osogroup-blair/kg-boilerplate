# Instructions for an agent working in this repository

1. **Read [docs/release/handoff.md](docs/release/handoff.md) first.** It says exactly where the work stopped and what to do next. [ROADMAP.md](ROADMAP.md) has the history and the plans are in `docs/plans/`.
2. Python runs through `uv` (`make tools` installs it into `.tools/`, which the Makefile uses). Use the Makefile targets, not ad-hoc commands: `make lint typecheck test`, then with the stack up (`make up seed`) `make test-int test-security test-contract`.
3. Docker Desktop with about 6 GB; the integration suites share one Postgres and one Neo4j. **Never run `make down` or `make reset` in the middle of a test run.** Keep several GB of host disk free: a full disk wedges Docker (it happened once).
4. After any API change run `make openapi`. After any migration edit the dev database must be rebuilt (`make reset up seed`).
5. Do not commit secrets. `.local/`, `.env` and `deploy/compose/secrets/` are git-ignored; keep it that way. Model-affecting changes (aliases, prices, prompts, ranker, Graphiti pin) need a recorded live gate (`make eval-gate-live`, owner action).
6. Commit only when the owner asks. One commit per work package group, messages end with the Co-Authored-By line used in `git log`.
