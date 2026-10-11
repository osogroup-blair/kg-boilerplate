"""Request spy shared by the gateway and Graphiti spikes. Records what was sent and (for JSON replies) status, usage and latency."""
from __future__ import annotations

import json
import time

import httpx


class SpyTransport(httpx.AsyncBaseTransport):
    """Wraps a transport and records every request actually sent (G-02, G-04, GR-02, GR-05 ...). Bodies are kept in memory only."""

    def __init__(self, inner: httpx.AsyncBaseTransport, keep_responses: bool = False):
        self.inner, self.requests, self.keep_responses = inner, [], keep_responses     # keep_responses: fixture recording only (LV-10)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        rec = {"path": request.url.path, "headers": dict(request.headers), "body": body}
        self.requests.append(rec)
        t0 = time.perf_counter()
        response = await self.inner.handle_async_request(request)
        await response.aread()
        rec["status"], rec["latency_ms"] = response.status_code, (time.perf_counter() - t0) * 1000
        try:
            data = json.loads(response.content)
            rec["usage"] = data.get("usage") if isinstance(data, dict) else None
            if self.keep_responses:
                rec["response_json"] = data
            rec["error_code"] = (data.get("error") or {}).get("code") if isinstance(data, dict) else None
        except ValueError:
            rec["usage"], rec["error_code"] = None, None
        rec["response_headers"] = dict(response.headers)
        return response
