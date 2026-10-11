"""NW-01 (OFFLINE): can a container reach a service bound ONLY to the host's 127.0.0.1? (The router binds loopback only.)"""
from __future__ import annotations

import http.server
import socket
import subprocess
import threading
import urllib.request

import pytest

import results_plugin as rp

PYCODE = "import urllib.request,sys; r=urllib.request.urlopen(sys.argv[1], timeout=5); print(r.status, r.read().decode()[:80])"


def docker(*args, timeout=90):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


class _Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok","stub":true}')

    def log_message(self, *a):
        pass


def serve(bind: str):
    srv = http.server.HTTPServer((bind, 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def from_container(url: str, extra=()):
    r = docker("run", "--rm", *extra, "python:3.12-slim", "python", "-c", PYCODE, url)
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip().splitlines()[-1][:160])


def test_nw01_container_to_host_loopback():
    srv, port = serve("127.0.0.1")
    try:
        # (a) the documented Docker Desktop route
        ok_a, out_a = from_container(f"http://host.docker.internal:{port}/health")
        # (a2) explicit host-gateway mapping (what Linux CI needs; Desktop accepts it too)
        ok_a2, out_a2 = from_container(f"http://host.docker.internal:{port}/health", ["--add-host", "host.docker.internal:host-gateway"])
        # (b) negative control: the container's own loopback is NOT the host
        ok_neg, out_neg = from_container(f"http://127.0.0.1:{port}/health")
    finally:
        srv.shutdown()
    # (c) the REAL router (read-only unauthenticated /health) if it is running on the host
    s = socket.socket(); s.settimeout(1)
    router_up = s.connect_ex(("127.0.0.1", 8780)) == 0
    s.close()
    ok_r, out_r = from_container("http://host.docker.internal:8780/health") if router_up else (None, "router not running on this host")
    assert ok_neg is False                                                  # a container's 127.0.0.1 is never the host
    rp.evidence("NW-01", stub_bound_to="127.0.0.1 (host only)", via_host_docker_internal={"ok": ok_a, "out": out_a},
                via_host_gateway_flag={"ok": ok_a2, "out": out_a2}, via_container_loopback={"ok": ok_neg, "out": out_neg},
                real_router_health_from_container={"ok": ok_r, "out": out_r}, docker_context="Docker Desktop for Mac")
    if ok_a:
        rp.note("NW-01", "VERIFIED on macOS Docker Desktop: a container reaches a service bound ONLY to the host's 127.0.0.1 through "
                         "http://host.docker.internal:<port> (Docker Desktop forwards it to host loopback). The real router's unauthenticated /health "
                         + ("answered from inside a container. " if ok_r else "was NOT tested from a container because the router was not running during this run. ") +
                         "DEFAULTS: OSO_ROUTER_BASE_URL=http://host.docker.internal:8780/v1 for containerised api/worker in dev; "
                         "http://127.0.0.1:8780/v1 for host-run processes; add `extra_hosts: [\"host.docker.internal:host-gateway\"]` to compose so Linux "
                         "CI resolves the name. Option (b) (router on 0.0.0.0) is NOT needed and widens exposure; option (c) (api/worker on the host) "
                         "is the fallback if a Docker Desktop update changes the forwarding.")
    else:
        rp.set_status("NW-01", "fail")
        rp.note("NW-01", f"host.docker.internal did NOT reach the loopback-only stub ({out_a}); choose option (b) or (c).")
