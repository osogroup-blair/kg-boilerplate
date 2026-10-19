# The sandbox broker

Hands each visitor a private, pre-seeded workspace for two hours, then deletes it. Deployment, limits and the pre-launch checklist: [deploy/sandbox/README.md](../../deploy/sandbox/README.md).

- `POST /sandbox/sessions` creates a session (claims a pre-seeded workspace; seeds one on demand if the pool is empty) and returns four keys **once**; the broker forgets them.
- `GET /sandbox/sessions/{id}` returns the tour progress. `DELETE` ends the session. `POST .../events/{script}` sends a scripted conversation. `GET /sandbox/scripts` lists them.
- Every route answers the **same 404** for a wrong id, a wrong key and an expired session. Authentication is the visitor's own admin key.
- Sessions live in one SQLite file (`0600`); the reaper deletes workspaces through the governed deletion request; a warm pool keeps `SANDBOX_POOL_SIZE` workspaces ready.
- It uses the platform only from outside (HTTP, the SDK, `kg workspaces create`) and reuses the example kit and the Sarah scenario's own steps, so what a visitor gets is what `make demo-sarah` proves.

It is not a `uv` workspace member (adding one would change `uv.lock`); it runs inside the platform's Python image with `PYTHONPATH` set. Locally: `PYTHONPATH=.:apps/sandbox/src uv run python -m kg_sandbox` after `make up seed`.

```bash
make sandbox-test    # against the dev stack
make sandbox-smoke   # no stack: broker tests, compose, edge
```
