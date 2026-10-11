# Mock Oso Model Router

Reproduces `docs/router-contract.md` for offline development and tests (P1-P6 reuse it).

```bash
make mock-router          # host process on 127.0.0.1:8781, key from MOCK_ROUTER_API_KEY (default: mock-key)
```

- Catalog (`catalog.json`) is generated from the router seed: `python -m tools.mock_router.sync_catalog`.
- Content modes: fixture replay (`fixtures/*.json`) -> schema-faker for `json_schema` -> echo.
- Embeddings: deterministic 384-dim feature-hashed unit vectors (lexical similarity only).
- Test-only control plane (not in the real router): `POST /__mock/faults`, `GET|DELETE /__mock/requests`.
- Settings: `MOCK_ROUTER_MAX_CLASSIFICATION` (default `restricted`; the real seeded key is `internal`),
  `MOCK_ROUTER_E5_OVERLONG` (`truncate`|`accept`), `MOCK_ROUTER_RETRY_AFTER`.
- Not implemented: tool-call generation, vision, `/health/providers`, `/mcp`, real E5 semantics.
