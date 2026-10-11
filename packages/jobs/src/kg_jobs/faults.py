"""Test-only failure injection (phase-3 plan §5.7). `KG_FAULTS` is a comma list of `stage:point:count`; the setting validator refuses it in production.

Points:  after_claim  after_side_effect  before_complete   -> the process exits hard (os._exit), like a SIGKILL
         raise_retryable  raise_config                      -> the handler call raises a typed JobError
         stall (rule stage:stall:count:seconds)             -> the whole process blocks (time.sleep, heartbeats included): a freeze / long GC pause
A file-backed counter (KG_FAULTS_DIR/counters.json, flock-guarded) makes "crash N times" survive worker restarts. The rule list can also come from
KG_FAULTS_DIR/spec, re-read when it changes, so a dev worker in a container can be armed without a restart."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from kg_jobs.errors import Category, JobError

CRASH_POINTS = frozenset({"after_claim", "after_side_effect", "before_complete"})
RAISE_POINTS = frozenset({"raise_retryable", "raise_config"})
STALL_POINTS = frozenset({"stall"})
ALL_POINTS = CRASH_POINTS | RAISE_POINTS | STALL_POINTS
EXIT_CODE = 137
DEFAULT_STALL_SECONDS = 5.0


@dataclass(frozen=True)
class FaultRule:
    stage: str
    point: str
    count: int
    seconds: float = DEFAULT_STALL_SECONDS

    @property
    def name(self) -> str:
        return f"{self.stage}:{self.point}"


def parse_spec(spec: str) -> list[FaultRule]:
    rules: list[FaultRule] = []
    for part in (p.strip() for p in spec.replace("\n", ",").split(",")):
        if not part or part.startswith("#"):
            continue
        bits = part.split(":")
        if (
            len(bits) not in (3, 4)
            or bits[1] not in ALL_POINTS
            or not bits[2].isdigit()
            or (len(bits) == 4 and bits[1] not in STALL_POINTS)
        ):
            raise ValueError(
                f"invalid fault rule {part!r}; expected stage:point:count (stage:stall:count:seconds) with point in {sorted(ALL_POINTS)}"
            )
        try:
            seconds = float(bits[3]) if len(bits) == 4 else DEFAULT_STALL_SECONDS
        except ValueError as exc:
            raise ValueError(f"invalid stall seconds in {part!r}") from exc
        rules.append(FaultRule(bits[0], bits[1], int(bits[2]), seconds))
    return rules


class Faults:
    def __init__(self, spec: str = "", state_dir: str = ""):
        self._env_rules = parse_spec(spec) if spec else []
        self._dir = Path(state_dir) if state_dir else None
        self._memory: dict[str, int] = {}
        self._file_rules: list[FaultRule] = []
        self._file_mtime = -1.0

    @property
    def active(self) -> bool:
        return bool(self._env_rules) or self._dir is not None

    def _rules(self) -> list[FaultRule]:
        if self._dir is not None:
            spec_file = self._dir / "spec"
            try:
                mtime = spec_file.stat().st_mtime
            except OSError:
                mtime, self._file_rules = -1.0, []
            else:
                if mtime != self._file_mtime:
                    self._file_rules = parse_spec(spec_file.read_text())
            self._file_mtime = mtime
        return [*self._env_rules, *self._file_rules]

    def _fire(self, rule: FaultRule) -> bool:
        """True while fewer than rule.count activations have happened (across processes when a state dir is configured)."""
        if self._dir is None:
            used = self._memory.get(rule.name, 0)
            if used >= rule.count:
                return False
            self._memory[rule.name] = used + 1
            return True
        self._dir.mkdir(parents=True, exist_ok=True)
        with (self._dir / "counters.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = self._dir / "counters.json"
                counters = json.loads(path.read_text()) if path.exists() else {}
                if counters.get(rule.name, 0) >= rule.count:
                    return False
                counters[rule.name] = counters.get(rule.name, 0) + 1
                path.write_text(json.dumps(counters))
                return True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def hit(self, stage: str, point: str) -> None:
        if not self.active:
            return
        for rule in self._rules():
            if rule.stage != stage or rule.point != point or not self._fire(rule):
                continue
            if point in CRASH_POINTS:
                sys.stderr.write(f"fault injected: {rule.name}\n")
                sys.stderr.flush()
                os._exit(EXIT_CODE)  # noqa: SLF001 (the point: die without cleanup, like SIGKILL)
                return  # unreachable outside tests, where _exit is replaced
            if point in STALL_POINTS:
                time.sleep(rule.seconds)  # noqa: ASYNC251 (blocking is the point: the event loop, and with it every heartbeat, freezes)
                return
            category = Category.RETRYABLE if point == "raise_retryable" else Category.CONFIG_TERMINAL
            raise JobError(category, "fault_injected")


_current = Faults()


def configure(spec: str = "", state_dir: str = "") -> Faults:
    global _current
    _current = Faults(spec, state_dir)
    return _current


def current() -> Faults:
    return _current
