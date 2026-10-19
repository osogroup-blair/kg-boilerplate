---
title: Operations overview
description: The runbooks, grouped by what you are trying to do.
sidebar:
  order: 1
---

The runbooks are short, step-by-step procedures. They are imported from the repository's `docs/runbooks/`, so they are the same files engineers use.

| You want to | Read |
|---|---|
| Run the stack locally, reset it, find ports and keys | [startup](/docs/operations/startup/) |
| Deploy to production, size it, set secrets and OIDC | [deploy-production](/docs/operations/deploy-production/), [oidc-setup](/docs/operations/oidc-setup/), [rotate-secrets](/docs/operations/rotate-secrets/), [payload-key-rotation](/docs/operations/payload-key-rotation/) |
| Back up and restore | [backup-restore](/docs/operations/backup-restore/) |
| Upgrade pinned dependencies | [upgrade-pin-dependencies](/docs/operations/upgrade-pin-dependencies/) |
| Onboard or revoke an agent | [agent-onboarding](/docs/operations/agent-onboarding/), [agent-revocation](/docs/operations/agent-revocation/) |
| Debug a bad context | [context-debugging](/docs/operations/context-debugging/) |
| Handle an outage | [llm-outage](/docs/operations/llm-outage/), [graph-outage](/docs/operations/graph-outage/), [projection-repair](/docs/operations/projection-repair/), [stuck-job-replay](/docs/operations/stuck-job-replay/) |
| Handle bad extraction | [extraction-quarantine](/docs/operations/extraction-quarantine/), [memory-quarantine](/docs/operations/memory-quarantine/), [identity-correction](/docs/operations/identity-correction/) |
| Change the ontology or embeddings | [ontology-simulation](/docs/operations/ontology-simulation/), [reindex-ontology](/docs/operations/reindex-ontology/), [memory-reembed](/docs/operations/memory-reembed/) |
| Honour deletion, holds and retention | [subject-deletion](/docs/operations/subject-deletion/), [legal-hold](/docs/operations/legal-hold/), [retention](/docs/operations/retention/) |

Operators should also read the [threat model](/docs/reference/threat-model/) and the [release checklist](/docs/project/release-checklist/).
