"""Raw recording -> committed tapes. Deterministic in shape: ids become stable aliases, server timestamps become an ordered synthetic clock,
keys and request ids are replaced. Nothing the site needs is lost; nothing secret survives."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from tools.replay_capture.record import ROOT, TAPE_VERSION

HERE = Path(__file__).parent
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
KEY = re.compile(r"\bkgk?_[A-Za-z0-9_\-]{8,}")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
DOMAIN_TIME = {"valid_from", "valid_to", "occurred_at", "as_of", "effective_from", "source_recorded_at", "expires_at", "ttl_until"}
CLOCK0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)


class Normalizer:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.aliases: dict[str, str] = {}
        self.n = 0
        self.ws = str(raw["recorded_with"]["workspace"])
        for name, ident in raw["ids"].items():
            self.aliases.setdefault(ident, {"swe": "fact_works_at_engineer", "em": "fact_works_at_manager", "evt003": "interaction_evt_003"}.get(name, name))
        self.stamps: dict[str, str] = {}
        self.stamp_order: list[str] = []

    def collect(self, o: Any, key: str = "") -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                self.collect(v, k)
        elif isinstance(o, list):
            for x in o:
                self.collect(x, key)
        elif isinstance(o, str) and TS.match(o) and key not in DOMAIN_TIME and key != "known_at":
            self.stamps.setdefault(o, "")
        elif isinstance(o, str) and key == "known_at" and TS.match(o):
            self.stamps.setdefault(o, "")

    def finish_clock(self) -> None:
        for i, s in enumerate(sorted(self.stamps)):
            self.stamps[s] = (CLOCK0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def alias(self, m: re.Match[str]) -> str:
        u = m.group(0)
        if u not in self.aliases:
            self.n += 1
            self.aliases[u] = f"id_{self.n}"
        return self.aliases[u]

    def text(self, s: str) -> str:
        s = KEY.sub("kgk_EXAMPLE", s)
        s = s.replace(self.ws, "demo-workspace")
        return UUID.sub(self.alias, s)

    def walk(self, o: Any, key: str = "") -> Any:
        if isinstance(o, dict):
            return {self.text(k): ("req_example" if k == "request_id" else self.walk(v, k)) for k, v in o.items()}
        if isinstance(o, list):
            return [self.walk(x, key) for x in o]
        if isinstance(o, str):
            if TS.match(o) and o in self.stamps and key not in DOMAIN_TIME:
                return self.stamps[o]
            return self.text(o)
        return o


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True).stdout.strip()
    except Exception:
        return "unknown"


def label_unknown_callers(steps: list[dict[str, Any]], label: str = "another workspace") -> None:
    """Calls made with a key the recorder never saw registered (the second workspace's) are labelled for readers."""
    for st in steps:
        for c in st["calls"]:
            if c["caller"] == "unknown":
                c["caller"] = label


def drop_rate_limited(steps: list[dict[str, Any]]) -> None:
    """A 429 in a recording is the SDK retrying after the platform's rate limit, not something a reader should learn from: the retry that follows is in the tape."""
    for st in steps:
        st["calls"] = [c for c in st["calls"] if c["status"] != 429]


def thin_polls(steps: list[dict[str, Any]]) -> None:
    """Drops the outbox-backlog polls and keeps one interaction poll per distinct stage state (plus the last), so a tape holds what happened, not how often it was asked."""
    for st in steps:
        seen: set[str] = set()
        kept: list[dict[str, Any]] = []
        calls = st["calls"]
        last_poll = {i: k for k, i in enumerate(i for i, c in enumerate(calls) if c["method"] == "GET" and re.fullmatch(r"/v1/interactions/[0-9a-f-]{36}", c["path"]))}
        poll_idx = [i for i, c in enumerate(calls) if c["method"] == "GET" and re.fullmatch(r"/v1/interactions/[0-9a-f-]{36}", c["path"])]
        final = poll_idx[-1] if poll_idx else -1
        for i, c in enumerate(calls):
            if c["path"].startswith("/v1/operations/backlog"):
                continue
            if i in last_poll:
                sig = stage_sig(c)
                if sig in seen and i != final:
                    continue
                seen.add(sig)
            kept.append(c)
        st["calls"] = kept


def tape(name: str, norm: Normalizer, raw: dict[str, Any], steps: list[dict[str, Any]], title: str, narration: dict[str, Any] | None = None) -> dict[str, Any]:
    out_steps = []
    for s in steps:
        extra = (narration or {}).get(str(s["id"]), {})
        out_steps.append({**{k: v for k, v in s.items() if k != "calls"}, **extra, "calls": s["calls"]})
    return {
        "tape_version": TAPE_VERSION,
        "scenario": name,
        "title": title,
        "recorded_with": {"git_sha": git_sha(), "ontology": raw["recorded_with"]["ontology"], "router": "mock (strict fixtures)"},
        "steps": norm.walk(out_steps),
    }


def stage_sig(call: dict[str, Any]) -> str:
    r = call["response"]
    return json.dumps([r.get("processing_status"), [(s["stage"], s["status"]) for s in r.get("stages", [])]]) if isinstance(r, dict) else ""


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".local" / "tapes"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "apps" / "site" / "public" / "tapes"
    out.mkdir(parents=True, exist_ok=True)
    raw = json.loads((src / "_raw.json").read_text())
    norm = Normalizer(raw)
    norm.collect(raw["tapes"])
    norm.finish_clock()
    narration = yaml.safe_load((HERE / "narration" / "sarah.yaml").read_text())
    tapes = raw["tapes"]

    for steps_ in tapes.values():
        drop_rate_limited(steps_)
    label_unknown_callers(tapes["sarah"])
    for name_ in ("sarah", "crm"):
        thin_polls(tapes[name_])
    sarah = tape("sarah", norm, raw, tapes["sarah"], "The Sarah scenario", narration)
    # the permission matrix and time tapes are single recordings
    matrix_calls = [c for c in tapes["context-matrix"][0]["calls"] if c["path"] == "/v1/context/build" and c["status"] == 200]
    matrix = tape("context-matrix", norm, raw, [{**tapes["context-matrix"][0], "calls": matrix_calls}], "Context builds across task, profile, budget and caller")
    times = tape("time", norm, raw, tapes["time"], "Sarah's relationships over time")
    crm = tape("crm", norm, raw, tapes["crm"], "A second domain: CRM, configuration only")
    perms = tape("permissions", norm, raw, tapes["permissions"], "One question, four callers")
    # write path: the polls of evt_003 in step 3, one entry per distinct stage state
    s3 = next(s for s in tapes["sarah"] if s["id"] == "3")
    seen: list[str] = []
    polls = []
    for c in s3["calls"]:
        if c["method"] == "GET" and re.fullmatch(r"/v1/interactions/[0-9a-f-]{36}", c["path"]) and c["status"] == 200:
            sig = stage_sig(c)
            if sig not in seen:
                seen.append(sig)
                polls.append(c)
    post = next(c for c in s3["calls"] if c["method"] == "POST" and c["path"] == "/v1/interactions")
    write_path = tape("write-path", norm, raw, [{"id": "w", "title": "One conversation event, stage by stage", "calls": [post, *polls]}], "The write path")

    for name, t in {"sarah": sarah, "context-matrix": matrix, "time": times, "permissions": perms, "write-path": write_path, "crm": crm}.items():
        text = json.dumps(t, indent=1, ensure_ascii=False) + "\n"
        assert not KEY.search(text.replace("kgk_EXAMPLE", "")), name
        (out / f"{name}.json").write_text(text)
        print(f"{name}.json  {len(text) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
