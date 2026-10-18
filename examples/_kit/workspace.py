"""One fresh workspace per example run (decision D5): created through `kg workspaces create`, never by writing rows."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from examples._kit.env import ROOT, Env


@dataclass
class Workspace:
    slug: str
    admin_key: str
    base_url: str
    keys: dict[str, str]  # handle -> plaintext key, for principals registered during the run

    def client(self, key: str | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, headers={"authorization": f"Bearer {key or self.admin_key}"}, timeout=60)


def fresh_workspace(env: Env, example: str, *, ontology: Path | None = None) -> Workspace:
    slug = f"ex-{re.sub(r'[^a-z0-9]+', '-', example.lower()).strip('-')}-{time.strftime('%y%m%d%H%M%S', time.gmtime())}"[:40].rstrip("-")
    uv = os.environ.get("UV") or shutil.which("uv") or str(ROOT / ".tools/uvenv/bin/uv")
    cmd = [uv, "run", "kg", "workspaces", "create", "--slug", slug, "--name", f"Example {example}"]
    if ontology is not None:
        cmd += ["--ontology", str(ontology)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(f"kg workspaces create failed: {proc.stderr[-400:]}")
    m = re.search(r"admin@[^:\s]*:\s*(\S+)", proc.stdout) or re.search(r"^\S+: (kg_\S+)$", proc.stdout, re.M)
    if not m:
        raise RuntimeError("could not find the administrator key in the CLI output (it is printed once)")
    return Workspace(slug=slug, admin_key=m.group(1), base_url=env.base_url, keys={})
