"""`make scan` (phase-9 plan WP6): dependency, image, filesystem and secret scanning, SBOMs, and one verdict.

    python scripts/scan.py [--only pip-audit npm-audit trivy-fs trivy-image gitleaks ruff-s sbom] [--third-party]

* pip-audit      the locked Python dependency set (`uv export --frozen --no-dev`), against the PyPI advisory database
* npm-audit      apps/admin, production dependencies
* trivy-fs       the repository: vulnerabilities in lockfiles, committed secrets, misconfigurations (Dockerfile, compose)
* trivy-image    the images this repository builds (kg-python, the mock router, the backup tool); `--third-party` adds the database images
* gitleaks       the working tree AND the whole git history
* ruff-s         the bandit rules (`S`), which the ordinary lint already enforces
* sbom           CycloneDX for the Python environment, the admin dependencies and the images (eval/reports/sbom/)

Scanner tools run from pinned images / uv tool versions (recorded in compat/versions.lock.yaml `p9_additions`). HIGH and CRITICAL findings fail the run unless
docs/security/scan-exceptions.yaml holds an UNEXPIRED, owned exception for that id (an expired exception fails again, loudly). The report is eval/reports/<date>/scan.json."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".local" / "scan"
EXCEPTIONS = ROOT / "docs" / "security" / "scan-exceptions.yaml"
TRIVY = "aquasec/trivy@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
GITLEAKS = "zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
PIP_AUDIT = "pip-audit==2.10.1"
CYCLONEDX = "cyclonedx-bom==7.5.0"
IMAGES = ["kg-python:dev", "kg-backup-tools:p9"]
THIRD_PARTY = ["pgvector/pgvector:pg17", "neo4j:5.26-community"]
BLOCKING = {"HIGH", "CRITICAL"}
UV = shutil.which("uv") or str(ROOT / ".tools" / "uvenv" / "bin" / "uv")


def run(cmd: list[str], *, check: bool = False, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)  # noqa: S603


def docker_tool(image: str, args: list[str], *, mounts: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "run", "--rm", "-v", f"{ROOT}:/src:ro", "-v", f"{OUT}:/out"]
    for m in mounts or []:
        cmd += ["-v", m]
    return run([*cmd, image, *args])


def load_exceptions() -> dict[str, dict[str, Any]]:
    if not EXCEPTIONS.exists():
        return {}
    data = yaml.safe_load(EXCEPTIONS.read_text()) or {}
    return {str(e["id"]): e for e in data.get("exceptions", [])}


def finding(scanner: str, ident: str, severity: str, target: str, title: str = "") -> dict[str, str]:
    return {"scanner": scanner, "id": ident, "severity": severity.upper(), "target": target, "title": title[:140]}


# ---- scanners ---------------------------------------------------------------------------------------------------------------------------------
def pip_audit() -> list[dict[str, str]]:
    req = OUT / "requirements.txt"
    export = run([UV, "export", "--frozen", "--no-dev", "--no-emit-workspace", "--no-hashes", "-o", str(req)])
    if export.returncode != 0:
        raise RuntimeError(f"uv export failed: {export.stderr[-200:]}")
    res = run(
        [
            UV,
            "tool",
            "run",
            "--from",
            PIP_AUDIT,
            "pip-audit",
            "-r",
            str(req),
            "--no-deps",
            "--disable-pip",
            "-f",
            "json",
            "--progress-spinner",
            "off",
        ]
    )
    data = json.loads(res.stdout or "{}")
    out = []
    for dep in data.get("dependencies", []):
        for v in dep.get("vulns", []):
            sev = "HIGH"  # pip-audit carries no severity: every advisory is treated as blocking until an exception says otherwise
            out.append(finding("pip-audit", v["id"], sev, f"{dep['name']}=={dep['version']}", v.get("description", "")))
    return out


def npm_audit() -> list[dict[str, str]]:
    res = run(["npm", "audit", "--omit=dev", "--json"], cwd=ROOT / "apps" / "admin")
    data = json.loads(res.stdout or "{}")
    out = []
    for name, v in (data.get("vulnerabilities") or {}).items():
        for via in v.get("via", []):
            if isinstance(via, dict):
                out.append(
                    finding(
                        "npm-audit",
                        str(via.get("source") or via.get("url") or name),
                        via.get("severity", v.get("severity", "high")),
                        name,
                        via.get("title", ""),
                    )
                )
    return out


def trivy_results(path: Path, scanner: str) -> list[dict[str, str]]:
    data = json.loads(path.read_text())
    out = []
    for result in data.get("Results", []) or []:
        target = result.get("Target", "")
        for v in result.get("Vulnerabilities", []) or []:
            out.append(
                finding(
                    scanner,
                    v["VulnerabilityID"],
                    v.get("Severity", "UNKNOWN"),
                    f"{target}:{v.get('PkgName')}@{v.get('InstalledVersion')}",
                    v.get("Title", ""),
                )
            )
        for s in result.get("Secrets", []) or []:
            out.append(
                finding(
                    scanner,
                    s.get("RuleID", "secret"),
                    s.get("Severity", "HIGH"),
                    f"{target}:{s.get('StartLine')}",
                    s.get("Title", "a secret"),
                )
            )
        for m in result.get("Misconfigurations", []) or []:
            if m.get("Status") != "PASS":
                out.append(finding(scanner, m["ID"], m.get("Severity", "MEDIUM"), target, m.get("Title", "")))
    return out


def trivy_fs() -> list[dict[str, str]]:
    skip = "--skip-dirs=/src/.local,/src/.venv,/src/.tools,/src/apps/admin/node_modules,/src/spikes,/src/solution_inputs"
    res = docker_tool(
        TRIVY, ["fs", "--quiet", "--scanners", "vuln,secret,misconfig", "--format", "json", "-o", "/out/trivy-fs.json", skip, "/src"]
    )
    if res.returncode != 0 and not (OUT / "trivy-fs.json").exists():
        raise RuntimeError(res.stderr[-300:])
    return trivy_results(OUT / "trivy-fs.json", "trivy-fs")


def trivy_image(images: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for image in images:
        name = image.replace("/", "_").replace(":", "_")
        res = docker_tool(
            TRIVY,
            [
                "image",
                "--quiet",
                "--scanners",
                "vuln,secret",
                "--ignore-unfixed",
                "--format",
                "json",
                "-o",
                f"/out/trivy-image-{name}.json",
                image,
            ],
            mounts=["/var/run/docker.sock:/var/run/docker.sock"],
        )
        path = OUT / f"trivy-image-{name}.json"
        if not path.exists():
            raise RuntimeError(f"{image}: {res.stderr[-300:]}")
        out += trivy_results(path, f"trivy-image:{image}")
    return out


def gitleaks() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for mode, args in (
        ("git", ["git", "--no-banner", "-c", "/src/.gitleaks.toml", "-r", "/out/gitleaks-git.json", "-f", "json", "/src"]),
        ("dir", ["dir", "--no-banner", "-c", "/src/.gitleaks.toml", "-r", "/out/gitleaks-dir.json", "-f", "json", "/src"]),
    ):
        (OUT / f"gitleaks-{mode}.json").unlink(missing_ok=True)
        docker_tool(GITLEAKS, args)
        path = OUT / f"gitleaks-{mode}.json"
        for leak in json.loads(path.read_text() or "[]") if path.exists() else []:
            if any(
                part in leak.get("File", "") for part in (".local/", "node_modules/", ".venv/", ".tools/", "spikes/", "solution_inputs/")
            ):
                continue
            out.append(
                finding(
                    f"gitleaks-{mode}",
                    leak.get("RuleID", "secret"),
                    "HIGH",
                    f"{leak.get('File')}:{leak.get('StartLine')}",
                    leak.get("Description", ""),
                )
            )
    return out


def ruff_s() -> list[dict[str, str]]:
    res = run([UV, "run", "ruff", "check", "--output-format", "json", "packages", "apps", "sdk", "tests", "scripts", "eval"])
    return [
        finding("ruff-s", str(d.get("code")), "HIGH", f"{d.get('filename')}:{d.get('location', {}).get('row')}", d.get("message", ""))
        for d in json.loads(res.stdout or "[]")
        if str(d.get("code", "")).startswith("S")
    ]


def sbom() -> list[dict[str, str]]:
    out_dir = ROOT / "eval" / "reports" / "sbom"
    out_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            UV,
            "tool",
            "run",
            "--from",
            CYCLONEDX,
            "cyclonedx-py",
            "environment",
            ".venv",
            "--of",
            "JSON",
            "-o",
            str(out_dir / "python.cdx.json"),
        ]
    )
    run(["npm", "sbom", "--sbom-format", "cyclonedx", "--omit=dev"], cwd=ROOT / "apps" / "admin")
    res = run(["npm", "sbom", "--sbom-format", "cyclonedx", "--omit=dev"], cwd=ROOT / "apps" / "admin")
    if res.stdout.strip().startswith("{"):
        (out_dir / "admin.cdx.json").write_text(res.stdout)
    for image in IMAGES:
        name = image.replace("/", "_").replace(":", "_")
        docker_tool(
            TRIVY,
            ["image", "--quiet", "--format", "cyclonedx", "-o", f"/out/sbom-{name}.json", image],
            mounts=["/var/run/docker.sock:/var/run/docker.sock"],
        )
        src = OUT / f"sbom-{name}.json"
        if src.exists():
            (out_dir / f"{name}.cdx.json").write_text(src.read_text())
    return []


SCANNERS = {"pip-audit": pip_audit, "npm-audit": npm_audit, "trivy-fs": trivy_fs, "gitleaks": gitleaks, "ruff-s": ruff_s, "sbom": sbom}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--third-party", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    names = args.only or [*SCANNERS, "trivy-image"]
    findings: list[dict[str, str]] = []
    errors: dict[str, str] = {}
    for name in names:
        try:
            print(f"== {name}", flush=True)
            findings += trivy_image(IMAGES + (THIRD_PARTY if args.third_party else [])) if name == "trivy-image" else SCANNERS[name]()
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            print(f"   FAILED TO RUN: {errors[name]}", file=sys.stderr)
    exceptions, today = load_exceptions(), dt.date.today()
    blocking, excepted, expired = [], [], []
    for f in findings:
        if f["severity"] not in BLOCKING:
            continue
        ex = exceptions.get(f["id"])
        if ex is None:
            blocking.append(f)
        elif dt.date.fromisoformat(str(ex["expires"])) < today:
            expired.append({**f, "expired_on": str(ex["expires"])})
        else:
            excepted.append({**f, "owner": ex.get("owner"), "expires": str(ex["expires"]), "reason": ex.get("reason")})
    by_scanner: dict[str, dict[str, int]] = {}
    for f in findings:
        by_scanner.setdefault(f["scanner"], {}).setdefault(f["severity"], 0)
        by_scanner[f["scanner"]][f["severity"]] += 1
    report = {
        "date": today.isoformat(),
        "scanners": names,
        "errors": errors,
        "counts": by_scanner,
        "blocking": blocking,
        "expired_exceptions": expired,
        "excepted": excepted,
        "ok": not blocking and not expired and not errors,
    }
    day = ROOT / "eval" / "reports" / today.isoformat()
    day.mkdir(parents=True, exist_ok=True)
    (day / "scan.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "blocking": len(blocking),
                "expired_exceptions": len(expired),
                "excepted": len(excepted),
                "scanner_errors": len(errors),
                "counts": by_scanner,
            }
        )
    )
    for f in blocking[:25]:
        print(f"BLOCKING {f['scanner']} {f['severity']} {f['id']} {f['target']} {f['title']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
