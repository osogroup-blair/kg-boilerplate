# Examples

Everything here runs against the local stack (`make up seed`) and a **mock router**, so there is no model key, no cost and no variation between runs. Each example creates its own fresh workspace, so they never interfere; `make reset` clears them. Add `--live` to a runner only as the owner: it uses the real router and spends money.

| Example | Run | Time | What it proves |
|---|---|---|---|
| [Sarah two-agent scenario](sarah/README.md) | `make demo-sarah` | ~20 s | the blueprint's 10 reference steps: commands vs extracted facts, a sourced finding shared between agents, a 600-token context, a 2027 job change (`as_of`, `known_at`), denial of an outside agent, a correction, a deletion with a receipt, and a model-outage variant. 11 steps, pass/fail report |
| [Second domain: CRM](domains/crm/README.md) | `make demo-crm`, `make eval-crm` | ~25 s | the platform is reusable with **configuration only**: another ontology, profiles, data and a 9-case benchmark (leakage 0, budget 100%, attribution 100%); a test fails if platform source ever mentions CRM |
| [Agents](agents/README.md) | `make examples-agents` (needs `make mcp-up`) | ~40 s | a Research and a Planning agent over the SDK, the same flow over MCP handing over the same items, and eight denial cases in one table |
| [Notebooks](notebooks/README.md) | `make notebooks` | ~1.5 min | seven walkthroughs: ledger, temporal graph, curated memory, context and budget, multi-agent sharing, corrections and deletion, the second domain |

`make examples` runs the first three (it starts the MCP server first, which recreates the api and mock router: never run it in the middle of a test run). `make test-examples` runs all four as tests; it needs `make examples-install` for the notebook tooling.

## Conventions
- **Black-box.** The examples use only HTTP, the SDK, MCP and the CLI (`tests/examples` enforces what they may import). They never read the database.
- **Strict mock.** The mock router fails loudly for any model request it has no answer for, so a gap in the fixtures is a visible failure, not a silent retry. Each example loads its own answers and restores the previous mode on exit.
- **Shared kit.** [`_kit/`](_kit/) holds the helpers: environment, workspace creation, mock control, waits, reports, a context printer and the notebook starter.
- **Ontologies.** [`ontologies/`](ontologies/) holds the personal-knowledge ontology at each phase; the CRM one is in `domains/crm/`.
