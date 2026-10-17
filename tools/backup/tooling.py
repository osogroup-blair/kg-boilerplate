"""The shell-outs: `docker compose` for the services, the pinned tool image for pg_dump / pg_restore / psql / age, and sha256 of what was written."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

from tools.backup.config import IMAGE, REPO, Config


class ToolError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[backup] {msg}", file=sys.stderr, flush=True)


def run(
    cmd: Sequence[str], *, check: bool = True, capture: bool = True, input: bytes | None = None, cwd: Path = REPO
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(list(cmd), cwd=cwd, input=input, capture_output=capture, check=False)  # noqa: S603
    if check and proc.returncode != 0:
        tail = (proc.stderr or b"").decode(errors="replace")[-600:]
        raise ToolError(f"{' '.join(cmd[:4])} failed ({proc.returncode}): {tail}")
    return proc


def compose(cfg: Config, *args: str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return run(
        ["docker", "compose", "-p", cfg.compose_project, "-f", str(cfg.compose_file), "--env-file", str(cfg.compose_env), *args],
        check=check,
        input=input,
    )


def image_present() -> bool:
    return run(["docker", "image", "inspect", IMAGE], check=False).returncode == 0


def build_image() -> None:
    log(f"building {IMAGE}")
    run(["docker", "build", "-q", "-f", str(REPO / "tools" / "backup" / "Dockerfile"), "-t", IMAGE, str(REPO / "tools" / "backup")])


def ensure_image() -> None:
    if not image_present():
        build_image()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tool(
    args: Sequence[str], *, mounts: Sequence[tuple[Path, str, str]] = (), stdin: bool = False, network: str | None = None
) -> list[str]:
    """`docker run` of the tool image: `mounts` are (host path, container path, 'ro'|'rw')."""
    cmd = ["docker", "run", "--rm"]
    if stdin:
        cmd.append("-i")
    if network:
        cmd += ["--network", network]
    for host, inside, mode in mounts:
        cmd += ["-v", f"{host}:{inside}:{mode}"]
    return [*cmd, IMAGE, *args]


# ---- age -------------------------------------------------------------------------------------------------------------------------------------
def keygen(identity_path: Path) -> str:
    """Writes a fresh age identity (0600) and returns its public key. The identity is the RECOVERY key: keep it off the machine that makes the backups."""
    ensure_image()
    out = run(tool(["age-keygen"])).stdout.decode()
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(out)
    identity_path.chmod(0o600)
    public = next(line.split(": ", 1)[1].strip() for line in out.splitlines() if line.startswith("# public key:"))
    return public


def encrypt_stream(source: Sequence[str], dest: Path, recipients: Sequence[str]) -> tuple[int, str]:
    """`source | age -r R... > dest` (a pipeline, so a dump never exists unencrypted on disk). Returns (size, sha256 of the encrypted file)."""
    ensure_image()
    args = ["age"]
    for r in recipients:
        args += ["-r", r]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        producer = subprocess.Popen(list(source), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)  # noqa: S603
        assert producer.stdout is not None
        encryptor = subprocess.Popen(tool(args, stdin=True), stdin=producer.stdout, stdout=out, stderr=subprocess.PIPE)  # noqa: S603
        producer.stdout.close()
        enc_err = encryptor.communicate()[1]
        prod_err = producer.stderr.read() if producer.stderr else b""
        producer.wait()
    if producer.returncode != 0 or encryptor.returncode != 0:
        dest.unlink(missing_ok=True)
        raise ToolError(
            f"producer exit {producer.returncode}: {prod_err.decode(errors='replace')[-400:]} / age exit {encryptor.returncode}: {enc_err.decode(errors='replace')[-200:]}"
        )
    return dest.stat().st_size, sha256_file(dest)


def encrypt_bytes(data: bytes, dest: Path, recipients: Sequence[str]) -> tuple[int, str]:
    ensure_image()
    args = ["age"]
    for r in recipients:
        args += ["-r", r]
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = run(tool(args, stdin=True), input=data)
    dest.write_bytes(proc.stdout)
    return dest.stat().st_size, sha256_file(dest)


def decrypt_to(src: Path, dest: Path, identity: Path) -> None:
    """`age -d` into `dest` (a file on the restore host, inside the rehearsal's own directory)."""
    ensure_image()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as inp, dest.open("wb") as out:
        proc = subprocess.run(  # noqa: S603
            tool(["age", "-d", "-i", "/identity"], mounts=[(identity.resolve(), "/identity", "ro")], stdin=True),
            stdin=inp,
            stdout=out,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise ToolError(f"age -d failed: {proc.stderr.decode(errors='replace')[-300:]}")


def decrypt_bytes(src: Path, identity: Path) -> bytes:
    ensure_image()
    with src.open("rb") as inp:
        proc = subprocess.run(  # noqa: S603
            tool(["age", "-d", "-i", "/identity"], mounts=[(identity.resolve(), "/identity", "ro")], stdin=True),
            stdin=inp,
            capture_output=True,
            check=False,
        )
    if proc.returncode != 0:
        raise ToolError(f"age -d failed: {proc.stderr.decode(errors='replace')[-300:]}")
    return proc.stdout


def walk(path: Path) -> Iterator[Path]:
    yield from sorted(p for p in path.rglob("*") if p.is_file())
