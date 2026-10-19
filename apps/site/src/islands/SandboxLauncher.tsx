import { useCallback, useEffect, useState } from "react";
import "./islands.css";

const BROKER = (import.meta.env.PUBLIC_SANDBOX_URL as string | undefined)?.replace(/\/$/, "");
const TURNSTILE = import.meta.env.PUBLIC_TURNSTILE_SITE_KEY as string | undefined;

interface Session {
  session_id: string;
  expires_at: string;
  api_url: string;
  console_url: string;
  scenario: string;
  keys: Record<string, string>;
  ids: Record<string, string>;
}
interface Task { id: string; title: string; why: string; auto: boolean; cmd: (s: Session) => string }

const hdr = (k: string) => `-H "Authorization: Bearer ${k}"`;
const BUILD = (s: Session, key: string, tokens: number, extra = "") =>
  `curl -s ${s.api_url}/v1/context/build ${hdr(key)} -H "Content-Type: application/json" \\\n  -d '{"task":"Help Sarah assess the implications of starting a business","subject_ids":["${s.ids.sarah}"],"profile":"planning","max_tokens":${tokens},"project":"newco-plan"${extra}}'`;

// `auto` tasks the sandbox can see happen (counts in the workspace); the rest leave no record, so the visitor ticks them
const TASKS: Task[] = [
  { id: "context", auto: true, title: "Build a context as the planning agent", why: "The core call: a bounded, cited package for a task.", cmd: (s) => BUILD(s, s.keys.planning_agent, 600) },
  { id: "budget", auto: true, title: "Ask again with a 150-token budget", why: "Watch the engine keep what is required and report what it could not fit, within the hard cap.", cmd: (s) => BUILD(s, s.keys.planning_agent, 150) },
  { id: "as_of", auto: false, title: "Ask what was true on 2027-03-01", why: "Valid time: by then the Acme job is history.", cmd: (s) => `curl -s "${s.api_url}/v1/entities/${s.ids.sarah}/relationships?as_of=2027-03-01T00:00:00Z" ${hdr(s.keys.admin)}` },
  { id: "command", auto: true, title: "Add an entity by command", why: "A deterministic write: no model call, validated by the ontology.", cmd: (s) => `curl -s -X POST ${s.api_url}/v1/entities ${hdr(s.keys.admin)} -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \\\n  -d '{"type":"Organization","name":"NewCo","source_system":"sandbox","source_ref":"newco-1"}'` },
  { id: "memory", auto: false, title: "Search curated memory", why: "Hybrid lexical and vector search, scoped to what this key may read.", cmd: (s) => `curl -s -X POST ${s.api_url}/v1/memories/search ${hdr(s.keys.planning_agent)} -H "Content-Type: application/json" -d '{"query":"state licence","limit":5}'` },
  { id: "correct", auto: true, title: "Reject a fact, then rebuild the context", why: "Corrections change what the next context says; the original record stays.", cmd: (s) => `# 1. find the unverified "considering entrepreneurship" fact and its revision\ncurl -s "${s.api_url}/v1/entities/${s.ids.sarah}/relationships" ${hdr(s.keys.admin)}\n# 2. reject it (use the fact id, and its revision in If-Match)\ncurl -s -X POST ${s.api_url}/v1/relationships/<fact id>/corrections ${hdr(s.keys.admin)} -H "Content-Type: application/json" \\\n  -H "Idempotency-Key: $(uuidgen)" -H 'If-Match: W/"rev-1"' -d '{"kind":"reject","reason_code":"incorrect_extraction"}'` },
  { id: "deletion", auto: true, title: "Request a deletion and read the receipt", why: "Freeze, purge, verify, receipt.", cmd: (s) => `curl -s -X POST ${s.api_url}/v1/deletion-requests ${hdr(s.keys.admin)} -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \\\n  -d '{"kind":"subject","target_id":"${s.ids.michael}","reason_code":"data_subject_request"}'\n# then GET /v1/deletion-requests/<request_id> until status is completed` },
  { id: "denied", auto: false, title: "Ask as an agent with no grant", why: "Hidden looks the same as missing: the package is denied and says nothing about Sarah.", cmd: (s) => BUILD(s, s.keys.ungranted_agent, 600) },
];

function remaining(iso: string) {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "expired";
  return `${Math.floor(ms / 3600000)}h ${Math.floor((ms % 3600000) / 60000)}m left`;
}

export default function SandboxLauncher() {
  const [session, setSession] = useState<Session>();
  const [auto, setAuto] = useState<Record<string, boolean>>({});
  const [manual, setManual] = useState<Record<string, boolean>>({});
  const [scripts, setScripts] = useState<Record<string, { title: string; why: string }>>({});
  const [sent, setSent] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [token, setToken] = useState<string>();
  const [, tick] = useState(0);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("kg-sandbox");
      if (raw) {
        const s = JSON.parse(raw) as Session;
        if (new Date(s.expires_at).getTime() > Date.now()) setSession(s);
        setManual(JSON.parse(sessionStorage.getItem("kg-sandbox-manual") ?? "{}"));
      }
    } catch {}
  }, []);

  useEffect(() => {
    if (!TURNSTILE) return;
    (window as any).__kgTurnstile = (t: string) => setToken(t);
    const s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
    s.async = true;
    document.head.appendChild(s);
  }, []);

  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 30000);
    return () => clearInterval(t);
  }, []);

  const poll = useCallback(async () => {
    if (!session || !BROKER) return;
    try {
      const r = await fetch(`${BROKER}/sandbox/sessions/${session.session_id}`, { headers: { authorization: `Bearer ${session.keys.admin}` } });
      if (r.ok) setAuto((await r.json()).tour ?? {});
      if (r.status === 404) { sessionStorage.removeItem("kg-sandbox"); setSession(undefined); }
    } catch {}
  }, [session]);
  useEffect(() => {
    if (!session) return;
    poll();
    const t = setInterval(poll, 6000);
    return () => clearInterval(t);
  }, [session, poll]);
  useEffect(() => {
    if (!BROKER || !session) return;
    fetch(`${BROKER}/sandbox/scripts`).then((r) => r.json()).then(setScripts).catch(() => {});
  }, [session]);

  async function start() {
    setBusy(true);
    setErr(undefined);
    try {
      const r = await fetch(`${BROKER}/sandbox/sessions`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ scenario: "sarah", turnstile_token: token }) });
      const body = await r.json();
      if (!r.ok) throw new Error(body?.error?.message ?? `HTTP ${r.status}`);
      sessionStorage.setItem("kg-sandbox", JSON.stringify(body));
      sessionStorage.removeItem("kg-sandbox-manual");
      setManual({});
      setSession(body);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function send(script: string) {
    if (!session) return;
    setSent(undefined);
    const r = await fetch(`${BROKER}/sandbox/sessions/${session.session_id}/events/${script}`, { method: "POST", headers: { authorization: `Bearer ${session.keys.admin}` } });
    const body = await r.json();
    setSent(r.ok ? `Sent. Follow it with: curl -s ${session.api_url}/v1/interactions/${body.event_id} -H "Authorization: Bearer <your admin key>"` : body?.error?.message ?? "Could not send");
  }

  async function end() {
    if (!session) return;
    await fetch(`${BROKER}/sandbox/sessions/${session.session_id}`, { method: "DELETE", headers: { authorization: `Bearer ${session.keys.admin}` } }).catch(() => {});
    sessionStorage.removeItem("kg-sandbox");
    setSession(undefined);
  }

  if (!BROKER) {
    return (
      <div className="kgi kgi-panel">
        <h3>The hosted sandbox is not open yet</h3>
        <p>This copy of the site was built without a sandbox address, so there is nothing to start here. You can get the same experience on your own machine in about ten minutes, with no model key:</p>
        <pre tabIndex={0}>{`git clone https://github.com/osogroup-blair/kg-boilerplate.git && cd kg-boilerplate
make tools sync admin-install examples-install
make up seed
make demo-sarah     # the same scenario; leaves keys and ids in .local/examples/sarah-latest.json`}</pre>
        <p className="kgi-note">When a sandbox is deployed, set <code>PUBLIC_SANDBOX_URL</code> when building the site and this panel becomes the session launcher. Meanwhile the <a href="/see-it/">playgrounds</a> replay the same scenario with no backend.</p>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="kgi kgi-panel">
        <h3>Start a private sandbox</h3>
        <p>You get your own workspace, pre-loaded with the Sarah scenario, four API keys and the admin console. It is deleted after two hours. Please don't enter personal data.</p>
        {TURNSTILE ? <div className="cf-turnstile" data-sitekey={TURNSTILE} data-callback="__kgTurnstile" /> : null}
        <button className="kgi-btn primary" type="button" onClick={start} disabled={busy || (!!TURNSTILE && !token)}>{busy ? "Starting (up to a minute)…" : "Start a sandbox"}</button>
        {err ? <p className="kgi-err" role="alert">Could not start a sandbox: {err}</p> : null}
        <p className="kgi-note">Extraction in the sandbox runs on recorded model answers, not a live model. See “What the sandbox is” below.</p>
      </div>
    );
  }

  const done = (t: Task) => (t.auto ? !!auto[t.id] : !!manual[t.id]);
  const n = TASKS.filter(done).length;
  return (
    <div className="kgi">
      <div className="kgi-panel" style={{ marginBottom: "1rem" }}>
        <div className="kgi-row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>Your sandbox is ready</h3>
          <span className="pill warn">{remaining(session.expires_at)}</span>
        </div>
        <p className="kgi-note">Copy the keys now: they are kept only in this browser tab. The <b>admin</b> key is the workspace administrator of your throwaway workspace.</p>
        <table className="kgi-table">
          <tbody>
            {Object.entries(session.keys).map(([k, v]) => (
              <tr key={k}><th scope="row">{k.replace(/_/g, "-")}</th><td><code style={{ wordBreak: "break-all" }}>{v}</code></td></tr>
            ))}
            <tr><th scope="row">API</th><td><code>{session.api_url}</code></td></tr>
          </tbody>
        </table>
        <div className="kgi-row" style={{ marginTop: ".75rem" }}>
          <a className="kgi-btn primary" href={session.console_url} target="_blank" rel="noopener" style={{ textDecoration: "none" }}>Open the console (paste the admin key)</a>
          <a className="kgi-btn" href="/api/" style={{ textDecoration: "none" }}>Open the API reference</a>
          <button className="kgi-btn" type="button" onClick={end}>End this sandbox now</button>
        </div>
        <pre tabIndex={0} style={{ marginTop: ".75rem" }}>{`export KG_BASE_URL=${session.api_url}\nexport KG_API_KEY=${session.keys.planning_agent}   # then use the Python SDK: KGClient(...)`}</pre>
      </div>

      {Object.keys(scripts).length ? (
        <div className="kgi-panel" style={{ marginBottom: "1rem" }}>
          <h3>Send a scripted conversation</h3>
          <p className="kgi-note">Free text is not sent to a model here. These events are prepared, and each goes through the real pipeline (normalize, extract, resolve, propose).</p>
          <div className="kgi-row">
            {Object.entries(scripts).map(([k, v]) => (
              <button key={k} className="kgi-btn" type="button" title={v.why} onClick={() => send(k)}>{v.title}</button>
            ))}
          </div>
          {sent ? <pre tabIndex={0} style={{ marginTop: ".5rem" }}>{sent}</pre> : null}
        </div>
      ) : null}

      <h3>Guided tour: {n} of {TASKS.length} done</h3>
      <p className="kgi-note">Run each command in a terminal or in the <a href="/api/">API reference</a>. The sandbox ticks the tasks it can see; tick the others yourself.</p>
      <ol style={{ paddingInlineStart: "1.2rem" }}>
        {TASKS.map((t) => (
          <li key={t.id} style={{ marginBottom: ".9rem" }}>
            <b>{done(t) ? "✓ " : ""}{t.title}</b> <span className="kgi-note">{t.why}</span>
            {!t.auto ? (
              <label className="kgi-note" style={{ display: "block" }}>
                <input type="checkbox" checked={!!manual[t.id]} onChange={(e) => { const m = { ...manual, [t.id]: e.target.checked }; setManual(m); try { sessionStorage.setItem("kg-sandbox-manual", JSON.stringify(m)); } catch {} }} /> I did this
              </label>
            ) : null}
            <pre tabIndex={0} style={{ marginTop: ".3rem" }}>{t.cmd(session)}</pre>
          </li>
        ))}
      </ol>
    </div>
  );
}
