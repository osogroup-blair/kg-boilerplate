# Runbook: onboarding an agent or an app

Who: a workspace admin. Where: Agents & Access in the console (`/agents`), or `kg principals …`.

## 1. Decide what it needs
| Question | Answer becomes |
|---|---|
| Does it read about people? Which? | `subject:<entity id>` grants with `read` (ontology v1.4.0 hides people from everyone without one). Pick the entity from the directory; never type an id from memory. |
| Does it write findings? Where? | `propose` on the subject or project it writes into. `publish` only on a project you trust it to publish to **and** only if the ontology lets agents through without review (`memory_policies.<type>.agent_shared_requires_review: false`). Procedural memory always waits for a person. |
| Which context profiles does it need? | `allowed_profiles` (empty: all). A planning agent that is only allowed `planning` cannot build `research`. |
| Is the content it submits allowed to reach external models? | `model_disclosure: allowed | denied`. `denied`: its events are never extracted, its tasks are never embedded or summarised. |
| Is it a system of record (an app)? | `trusted_source` — only for an app whose commands may outrank a user's statements. Default no. |
| How much may it use? | quotas: requests/minute, context tokens/day, interactions/day (empty: the platform default; daily limits are unlimited by default). |

## 2. Register
Console: **Register agent or app** → handle, kind, owner, policy, profiles, quotas, initial grants → **Register**. The key appears **once**, with a copy button. CLI: `kg principals register research-agent --grant subject:<id>=read,propose --grant project:newco-plan=read,propose,publish --profile research` prints the key on stdout (everything else on stderr). Store it in the agent's secret store immediately; nothing can show it again.

## 3. Check it
- Context Lab → **Build as** the new agent: you see exactly what it will get (its grants, its ceiling, only its allowed profiles). A subject it may not see is simply absent; if you ask only for hidden subjects the answer is `denied`.
- Agents & Access → the principal → **Grants** (add or remove; effective on its next request), **Keys**, **Usage**, **Activity**.
- Over the API: `GET /v1/me` with the key, `GET /v1/me/usage`.

## 4. Rotate a key
**Keys → Mint key** (a name, optional expiry), deploy the new key, then **revoke** the old one by prefix. Both work meanwhile. A revoked key is a 401 on the next request.

## 5. Change a registration
**PATCH** (the console does it) needs the current revision (`If-Match`); another admin's change in between is a 412, reload. Lowering a quota applies at once in the process that handled the change and within 5 seconds elsewhere.

## Common situations
- *"The agent gets `denied` for a person it should know."* It lacks `subject:<id>` (or the ontology made `Person` scoped after it was registered). Grant it; the Lab shows the difference immediately.
- *"A finding it published is `proposed`, not active."* It lacks `publish` on every target scope, or the ontology still requires review for agents (`reason_code` on the review item says which).
- *"429 `quota_exceeded`."* `details.quota` names the quota and `retry_after` says when it refills (the next minute, or the next UTC midnight).
