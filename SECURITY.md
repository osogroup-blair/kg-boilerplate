# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately through GitHub: on the repository's *Security* tab choose **Report a vulnerability** (a private advisory only the maintainers can see).

Include what you found, how to reproduce it, and the commit you tested. We will acknowledge a report promptly, keep you informed while we investigate, and credit you in the advisory if you would like.

## What is in scope

- The platform: the API, worker, MCP server, SDK, admin console and CLI in this repository.
- Tenant isolation: anything that lets one workspace read, change or learn the existence of another's data, or an agent exceed its grants, including through the graph, memory search, context builds, traces or the deletion path.
- The hosted **sandbox**, when one is running: it is a public instance on a mock router, isolated from anything else, and it is in scope.
- Prompt-injection routes that cross a trust boundary (for example, retrieved text that can forge a citation or close the context envelope).

## What is out of scope

- The mock router's test control plane (`/__mock/*`) on a stack you run yourself. It is a development tool and is never exposed by the sandbox.
- Findings that need a malicious operator with database or host access.
- Denial of service by volume against a deployment you operate, and missing hardening on a development stack started with `make up` (which uses published development credentials by design).
- Known and documented limits: graph tenancy is application-enforced on a shared Neo4j database ([decision DEC-06](docs/release/decisions.md)), and memory search is an exact scan that slows with workspace size.

## What we already do

The [threat model](docs/security/threat-model.md) lists every trust boundary and the test or report that proves each control. The evaluation gate runs about 940 authorization probes and a frozen injection corpus, and the release sweep runs the leak suites against two API replicas. `make scan` produces dependency, image, filesystem and secret scans with SBOMs.

## Supported versions

The project is pre-1.0; fixes land on `main`.
