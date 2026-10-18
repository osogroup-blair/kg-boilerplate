# Sarah two-agent scenario

```bash
make up seed        # once
make demo-sarah     # about 20 s on the mock router; exits non-zero if any step fails
```

Each run creates its own workspace (`ex-sarah-<utc time>`), so runs never interfere; `make reset` clears them. The report goes to `examples/sarah/reports/sarah-<date>.{md,json}` (the committed copy is a sample). Pass `--live` only as the owner: it uses the real router and costs money.

| Step | Shows | Blueprint ids |
|---|---|---|
| 1 | The ontology is published; a trusted app and agents are registered with grants | ONT-01/02 |
| 2 | `evt_001`/`evt_002` as commands: projected, **zero model calls**, a repeated `Idempotency-Key` is one write | AT-01, ONT-06 |
| 3 | `evt_003` conversation runs through normalize, graph and memory stages | ING-01 |
| 4 | Current employment is Acme; "considering entrepreneurship" is a separate, unverified fact; an episodic memory exists | AT-02..05 |
| 5 | The Research Agent publishes a sourced finding (`agent_generated`); its private working note stays private | AT-10 |
| 6 | The Planning Agent gets a 600-token context: current facts, memory, finding, the "runway" unknown, citations; no cycling distractor | AT-06..09, AT-12 |
| 7 | `evt_006`: Sarah left Acme effective 2027-02-01. `as_of` 2026-12 shows Acme, `as_of` 2027-03 does not; `known_at` shows what was recorded when | AT-02/03, DEC-12 |
| 8 | An agent from another workspace gets the same answer for Sarah as for a random id; an agent without a grant sees no restricted memory | AT-11, AT-14, SEC-01 |
| 9 | The wrongly extracted "Michael manages Sarah" is shown as unverified, rejected by a reviewer, and gone from the next context; the episode stays | AT-13, MEM-06 |
| 10 | Deleting a subject blocks reads at once, the purge completes and a receipt is issued | AT-16, AT-17 |
| V | Model outage: the event is queued (not lost), context still serves what Postgres holds, replay completes it after the fault clears | failure matrix |

## Things worth knowing
- **`known_at` is record time, set by the database.** The source's "learned on 2027-02-05" is stored as `source_recorded_at` (informational). Ending a fact edits its `valid_to` in place, so `known_at` cannot "un-know" an end date; step 7 therefore shows `known_at` on a fact that really was recorded later (the CONSIDERING fact from `evt_003`).
- The mock router answers from `tools/mock_router/fixtures/scenarios` in strict mode for the length of the run; a missing fixture fails loudly. Fixtures and the previous strict setting are restored at the end.
- Step 9 builds its context as the admin: the planning agent has no grant on Michael, so his facts are hidden from it by design.
