"""Kit mechanics (not a plan check): the LV-10 recorder, pointed at the MOCK, produces fixtures that the mock replays by hash."""
from __future__ import annotations

import json

from graphiti.conftest import EPISODES, ingest
from graphiti.harness import Rig, new_group
from graphiti.record_fixtures import main as record
from tools.mock_router.app import MockConfig, create_app
from tools.mock_router.tests.conftest import LiveServer


async def test_recorder_roundtrip_replays_by_hash(tmp_path, ontology, monkeypatch):
    with LiveServer(create_app(MockConfig(api_key="rec-key"))) as srv:
        monkeypatch.setenv("OSO_ROUTER_BASE_URL", srv.base_url)
        monkeypatch.setenv("OSO_ROUTER_API_KEY", "rec-key")
        out = tmp_path / "recorded" / "recorded-sarah.json"
        out.parent.mkdir()
        await record(out)
    entries = json.loads(out.read_text())
    keyed = [e for e in entries if "key" in e]
    rules = [e for e in entries if "when" in e]
    assert keyed and rules and all(r["when"]["contains"] for r in rules)

    # replay: a fresh mock that ONLY has the recorded file must answer the Sarah episodes from fixtures, not the schema-faker
    rig = Rig(fixtures_dir=out.parent)
    await ingest(rig, new_group(), ontology, upto=3)
    modes = [r.get("content_mode") for r in rig.mock.state.request_log if r["endpoint"] == "chat"]
    hit = sum(m == "fixture" for m in modes) / len(modes)
    await rig.close()
    assert hit > 0.5, f"only {hit:.0%} of calls replayed from recorded fixtures: {modes}"
    print(f"recorded {len(keyed)} keyed + {len(rules)} rule fixtures; replay hit rate {hit:.0%}")
