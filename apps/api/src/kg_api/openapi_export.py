"""`python -m kg_api.openapi_export [path]`: writes the committed contract deterministically (no database needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kg_common.settings import REPO_ROOT, Settings

from kg_api.main import build_app


def generate() -> str:
    app = build_app(Settings(_env_file=None, env="dev"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "contracts" / "openapi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
