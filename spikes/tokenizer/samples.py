"""Synthetic context packages (Sarah scenario) in the blueprint's output convention (06-reference/02 section F), plus stress shapes."""
from __future__ import annotations

import json
import uuid

PLANNING = """# CURRENT VERIFIED FACTS
- Sarah Chen is an Engineering Manager at Acme. [fact_002; evt_002]
- Sarah Chen has worked at Acme since 2022-01-10. [fact_001; evt_001]

# RELEVANT EXPERIENCES
- Discussed possible entrepreneurship with Michael Torres on 2026-10-10; no departure decision. [mem_003; evt_003]

# PREFERENCES / INSTRUCTIONS
- Explicitly prefers risk/tradeoff comparisons. [mem_004; evt_004]

# MATERIAL UNKNOWN / CONFLICT
- Financial runway is not verified.
"""

SAMPLES: dict[str, str] = {
    "planning_package": PLANNING,
    "planning_x20": "\n\n".join([PLANNING] * 20),
    "prose_english": ("Sarah has led a platform team for two years and is weighing whether to start a software company. She has not resigned, "
                      "has no confirmed runway, and prefers to compare risks and trade-offs before deciding. ") * 10,
    "json_facts": json.dumps([{"fact_id": f"fact_{i:03d}", "text": f"Sarah works at Acme as role {i}", "status": "active", "valid_from": "2022-01-10",
                               "source_ids": [f"evt_{i:03d}"]} for i in range(40)], indent=1),
    "uuid_heavy": "\n".join(f"memory {uuid.UUID(int=i * 7919)} scope subject:{uuid.UUID(int=i * 104729)}" for i in range(60)),
    "multilingual": ("Sarah trabaja en Acme como gerente de ingeniería. サラはAcmeでエンジニアリングマネージャーとして働いています。 "
                     "Сара работает в Acme менеджером по разработке. سارة تعمل في Acme كمديرة هندسة. ") * 8,
    "code_and_paths": "def build_context(task: str, max_tokens: int = 600) -> dict:\n    return {'task': task, 'budget': max_tokens}\n# /v1/context/build\n" * 12,
    "numbers_dates": " ".join(f"2026-10-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z 12,345.67 {i * 1013}" for i in range(80)),
}
