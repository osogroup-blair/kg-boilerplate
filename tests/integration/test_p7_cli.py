"""P7 WP2: `kg principals` is the CLI face of the registration API. It talks to the running dev stack (`make up seed`) with the demo admin's key, so it is skipped when the stack
or the credentials are missing. Everything it registers is revoked again."""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest
from kg_cli.principals_cmds import CREDENTIALS, principals_app
from typer.testing import CliRunner

pytestmark = pytest.mark.integration
BASE = os.environ.get("KG_API_URL", "http://127.0.0.1:8000")


@pytest.fixture()
def stack(monkeypatch):
    if not CREDENTIALS.exists():
        pytest.skip("no .local/dev-credentials.json (run `make seed`)")
    try:
        ready = httpx.get(f"{BASE}/health", timeout=2).status_code == 200
        info = httpx.get(f"{BASE}/openapi.json", timeout=5).json()
    except httpx.HTTPError:
        pytest.skip("the dev stack is not running (`make up`)")
    if not ready or "/v1/principals/{principal_id}/revoke" not in info.get("paths", {}):
        pytest.skip("the dev stack does not serve the P7 API yet (`make up` rebuilds it)")
    monkeypatch.setenv("KG_API_URL", BASE)


def test_register_grant_rotate_and_revoke_from_the_command_line(stack):
    runner = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    handle = f"cli-agent-{uuid.uuid4().hex[:8]}"
    reg = runner.invoke(
        principals_app,
        ["register", handle, "--grant", "project:newco-plan=read,propose,publish", "--profile", "planning", "--requests-per-minute", "600"],
    )
    assert reg.exit_code == 0, reg.output
    key = reg.stdout.strip().splitlines()[-1]
    assert key.startswith("kgk_") and key not in (
        getattr(reg, "stderr", "") or ""
    )  # the key is the only thing on stdout; the summary is on stderr
    try:
        me = httpx.get(f"{BASE}/v1/me", headers={"authorization": f"Bearer {key}"})
        assert me.status_code == 200 and me.json()["principal"]["handle"] == handle
        grants = runner.invoke(principals_app, ["grants", handle])
        assert grants.exit_code == 0 and "project:newco-plan" in grants.stdout and "publish" in grants.stdout
        again = runner.invoke(principals_app, ["keys", handle, "--mint", "--name", "rotation"])
        assert again.exit_code == 0 and again.stdout.strip().startswith("kgk_") and again.stdout.strip() != key
        listing = runner.invoke(principals_app, ["keys", handle])
        assert listing.stdout.count("active") == 2
        upd = runner.invoke(principals_app, ["update", handle, "--description", "from the CLI", "--context-tokens-per-day", "5000"])
        assert upd.exit_code == 0 and "updated" in upd.stdout
        act = runner.invoke(principals_app, ["activity", handle])
        assert act.exit_code == 0 and json.loads(act.stdout)["context_builds"] == 0
    finally:
        out = runner.invoke(principals_app, ["revoke", handle, "--reason", "retired", "--yes"])
        assert out.exit_code == 0 and "revoked" in out.stdout
    assert httpx.get(f"{BASE}/v1/me", headers={"authorization": f"Bearer {key}"}).status_code == 401
    missing = runner.invoke(principals_app, ["grants", "no-such-handle"])
    assert missing.exit_code == 1 and "no principal" in (getattr(missing, "stderr", "") + missing.output)
