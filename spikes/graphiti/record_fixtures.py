"""LV-10: run the synthetic Sarah episodes through Graphiti against the REAL router and save the responses as mock-router fixtures.

Usage (via the verify kit): python -m graphiti.record_fixtures <output.json>
Entries: hash-keyed (exact replay, tools/mock_router/fixtures.fixture_key) plus rule-keyed extraction responses (robust to prompt drift).
Synthetic Sarah content only.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from graphiti.conftest import EPISODES, ONTOLOGY
from graphiti.harness import Rig, new_group, schema_name
from graphiti.ontology_compiler import compile_ontology
from tools.mock_router.fixtures import fixture_key

RULE_SCHEMAS = {"ExtractedEntities", "ExtractedEdges", "EdgeDuplicate"}


async def main(out: Path) -> None:
    rig = Rig(live=True, keep_responses=True)
    await rig.g.build_indices_and_constraints()
    onto = compile_ontology(ONTOLOGY.read_text())
    gid = new_group()
    entries, seen = [], set()
    for name, body, ref in EPISODES:
        rig.reset_spy()
        await rig.g.add_episode(name=name, episode_body=body, source_description="conversation_app", reference_time=ref, group_id=gid,
                                entity_types=onto.entity_types, edge_types=onto.edge_types, edge_type_map=onto.edge_type_map)
        for c in rig.chat_calls():
            if c.get("status") != 200 or not c.get("response_json"):
                continue
            b = c["body"]
            content = c["response_json"]["choices"][0]["message"]["content"]
            try:
                response = json.loads(content)
            except ValueError:
                response = content
            msgs = [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str) else ""} for m in b["messages"]]
            sname = schema_name(c)
            key = fixture_key(b["model"], msgs, sname)
            if key not in seen:
                seen.add(key)
                entries.append({"key": key, "response": response})
            if sname in RULE_SCHEMAS:
                entries.append({"when": {"alias": b["model"], "schema_name": sname, "contains": [body[:60]]}, "response": response})
    await rig.close()
    out.write_text(json.dumps(entries, indent=1) + "\n")
    print(f"recorded {len(entries)} fixture entries -> {out}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
