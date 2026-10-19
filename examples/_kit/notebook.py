"""One call at the top of a notebook: a fresh workspace on the local stack with the mock router in strict mode and the Sarah scenario's model answers loaded.

lab = notebook.start("01_ingest")      # lab.ws (workspace), lab.s (the Sarah Scenario helpers), lab.env
...
lab.close()                            # last cell: fixtures removed and the previous strict mode restored (also done at kernel exit)
"""

from __future__ import annotations

import atexit
from contextlib import AbstractContextManager
from typing import Any

from examples._kit import mock
from examples._kit.env import ROOT, Env, load_env
from examples._kit.report import Report
from examples._kit.workspace import Workspace, fresh_workspace
from examples.sarah.steps import Scenario

GOVERNANCE = ROOT / "examples" / "ontologies" / "personal-knowledge-governance.yaml"


class Lab:
    def __init__(self, env: Env, ws: Workspace, scenario: Scenario, pack: AbstractContextManager[None]) -> None:
        self.env, self.ws, self.s, self._pack, self._open = env, ws, scenario, pack, True

    def close(self) -> None:
        if self._open:
            self._open = False
            self._pack.__exit__(None, None, None)


def start(
    name: str, *, extra_rules: list[dict[str, Any]] | None = None, scenario: type[Scenario] = Scenario, ontology: Any = GOVERNANCE
) -> Lab:
    env = load_env()
    ws = fresh_workspace(env, name, ontology=ontology)
    s = scenario(env, ws, Report(name), out=lambda _m: None)
    pack = mock.mock_pack(env, [*s.mock_rules(), *(extra_rules or [])])
    pack.__enter__()
    lab = Lab(env, ws, s, pack)
    atexit.register(lab.close)
    print(f"workspace {ws.slug} on {env.base_url} (mock router, strict)")
    return lab
