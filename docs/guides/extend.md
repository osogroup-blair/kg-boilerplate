# Extend this boilerplate

## Add a domain
No platform code. Follow [add-a-domain.md](add-a-domain.md); the CRM in `examples/domains/crm/` is the worked case and a test keeps it config-only.

## Add or change a context profile
Profiles live in the ontology (`context_profiles:`). A new name starts from the built-in `quick_answer` and overrides field by field; an existing name (`planning`, ...) overrides the built-in. Set `expected_signals` to make the engine report what is missing. A request or an agent can only **narrow** a profile (lower caps, fewer sections); asking for more is a 422. Publish a new ontology version, or try it first with a simulation (console: Ontology → the draft → *Simulate…*, or `POST /v1/ontologies/versions/{id}/simulate`), which runs the draft in an isolated staging namespace: [ontology-simulation](../runbooks/ontology-simulation.md). Ranking weights and prompts are model-affecting: they need a recorded live gate (`make eval-gate-live`, owner action).

## Onboard an agent
Register it with scopes, allowed profiles, quotas and a model-disclosure policy, then give it grants on the subjects and projects it may use: [agent-onboarding](../runbooks/agent-onboarding.md) and `POST /v1/principals`. Give it the SDK (`sdk/python`) or point an MCP client at the server (`make mcp-up`, configs in `examples/agents/mcp/`). To retire or revoke one: [agent-revocation](../runbooks/agent-revocation.md). Agents hold no platform credential but their own key.

## Add an ingestion source
Any client can `POST /v1/interactions` with its own `source_system` and `source_ref`, an `Idempotency-Key`, `occurred_at` and `subject_refs`. For a **system of record** that should write facts directly (authoritative, no model), register an *app* with `trusted_source: true` and use the command endpoints (`/v1/entities`, `/v1/relationships`); the ontology's rules still apply, including `require_source`. A new kind of payload that needs parsing is a new `normalize` step in the worker (`packages/ingestion`).

## Swap the router or the embedding model
Set `OSO_ROUTER_MODE=live` and the router URL and keys (see [startup](../runbooks/startup.md)); keys must be non-admin and allow the classifications you send. The embedding dimension and model are pinned (`vector(384)`, [ADR-013](../adr/ADR-013-embeddings.md)); changing the model is a managed re-embed: [memory-reembed](../runbooks/memory-reembed.md). Extraction needs a model with structured output ([router contract](../router-contract.md)). Before relying on a new model run `make verify-router` and the live evaluations; the owner records the live gate.

## Change the ontology safely
Publish a new immutable version; old provenance stays on the old one. To rebuild the graph into a parked namespace and cut over atomically: [reindex-ontology](../runbooks/reindex-ontology.md).

## Deploy
The production reference is `deploy/compose/docker-compose.prod.yml`; sizing, secrets, OIDC and backups are in [deploy-production](../runbooks/deploy-production.md), [rotate-secrets](../runbooks/rotate-secrets.md), [oidc-setup](../runbooks/oidc-setup.md) and [backup-restore](../runbooks/backup-restore.md). Dependency bumps: [upgrade-pin-dependencies](../runbooks/upgrade-pin-dependencies.md).

## Where the hard rules live
Workspace row-level security and the two database roles (`packages/db`), subject filters on every read (`packages/knowledge`, `packages/memory`), the egress policy (`packages/modelgw`, workspace settings), the purge gate (migration `0008`), and the import-linter contracts in `pyproject.toml` that stop a package from reaching around them. Change a rule only with its negative test.
