"""Failure-injection hook of the deletion steps (FI-13, FI-14). kg_governance sits below the job runner, so it only calls a hook; the process that builds the deletion service
installs the runner's injector (`kg_memory.deletion.build_deletion`). With nothing installed, `hit` does nothing."""

from __future__ import annotations

from collections.abc import Callable

_hook: Callable[[str, str], None] | None = None


def install(hook: Callable[[str, str], None] | None) -> None:
    global _hook
    _hook = hook


def hit(stage: str, point: str) -> None:
    if _hook is not None:
        _hook(stage, point)
