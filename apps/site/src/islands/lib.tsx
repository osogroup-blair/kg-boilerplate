import { useEffect, useState } from "react";
import "./islands.css";

export interface Call {
  method: string;
  path: string;
  query?: Record<string, string>;
  caller: string;
  request: unknown;
  status: number;
  response: any;
  snapshot?: boolean;
}
export interface Step {
  id: string;
  title: string;
  story?: string;
  look_for?: string;
  at_ids?: string[];
  calls: Call[];
}
export interface Tape {
  tape_version: number;
  scenario: string;
  title: string;
  recorded_with: { git_sha: string; ontology: string; router: string };
  steps: Step[];
}

export function useTape(name: string): { tape?: Tape; error?: string } {
  const [state, setState] = useState<{ tape?: Tape; error?: string }>({});
  useEffect(() => {
    let live = true;
    fetch(`/tapes/${name}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((tape: Tape) => live && setState({ tape }))
      .catch((e: Error) => live && setState({ error: e.message }));
    return () => {
      live = false;
    };
  }, [name]);
  return state;
}

export function Loading({ error }: { error?: string }) {
  return error ? <p className="kgi-err" role="alert">Could not load the recording ({error}).</p> : <p className="kgi-loading" role="status">Loading the recording…</p>;
}

export function StatusPill({ status }: { status: number }) {
  return <span className={`kgi-st ${status < 400 ? "ok" : "err"}`}>{status}</span>;
}

export function qs(q?: Record<string, string>): string {
  const e = Object.entries(q ?? {});
  return e.length ? "?" + e.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&") : "";
}

export function curlFor(c: Call, base = "http://localhost:8000"): string {
  const lines = [`curl -s ${c.method === "GET" ? "" : `-X ${c.method} `}"${base}${c.path}${qs(c.query)}" \\`, `  -H "Authorization: Bearer $KG_API_KEY"`];
  if (c.request) {
    lines[lines.length - 1] += " \\";
    lines.push(`  -H "Content-Type: application/json" \\`);
    if (c.method !== "GET" && c.path !== "/v1/context/build") lines.push(`  -H "Idempotency-Key: $(uuidgen)" \\`);
    lines.push(`  -d '${JSON.stringify(c.request)}'`);
  }
  return lines.join("\n");
}

export function CallView({ call, open = false }: { call: Call; open?: boolean }) {
  const [copied, setCopied] = useState(false);
  return (
    <details className="kgi-call" open={open}>
      <summary>
        <span className="kgi-m">{call.method}</span>
        <span className="path">{call.path.replace(/\/(id_\d+|[a-z_]+_\d+)\b/g, "/$1")}{qs(call.query)}</span>
        <StatusPill status={call.status} />
        <span className="kgi-who">as {call.caller}</span>
      </summary>
      <div className="body">
        {call.request ? (
          <>
            <strong>Request</strong>
            <pre tabIndex={0}>{JSON.stringify(call.request, null, 2)}</pre>
          </>
        ) : null}
        <strong>Response</strong>
        <pre tabIndex={0}>{JSON.stringify(call.response, null, 2)}</pre>
        <button
          className="kgi-btn"
          type="button"
          onClick={() => {
            navigator.clipboard?.writeText(curlFor(call)).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? "Copied" : "Copy as curl (run it yourself)"}
        </button>
      </div>
    </details>
  );
}

export function TokenBar({ used, max }: { used: number; max: number }) {
  const pct = Math.min(100, Math.round((used / Math.max(1, max)) * 100));
  return (
    <div>
      <div className={`kgi-bar ${used > max ? "over" : ""}`} role="img" aria-label={`${used} of ${max} tokens used`}>
        <i style={{ width: `${pct}%` }} />
      </div>
      <div className="kgi-note">
        {used} of {max} tokens used
      </div>
    </div>
  );
}

export const humanize = (s: string) => s.replace(/_/g, " ");
