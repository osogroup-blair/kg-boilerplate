import { useMemo, useState } from "react";
import { CallView, Loading, useTape, type Call } from "./lib";

const CALLERS = [
  ["admin", "Workspace admin"],
  ["planning-agent", "Planning agent (granted on Sarah)"],
  ["ungranted-agent", "Agent with no grants"],
  ["other-workspace-agent", "Agent from another workspace"],
] as const;

function label(c: Call): string {
  if (c.path.startsWith("/v1/entities/") && c.path.endsWith("/relationships")) return c.status === 200 ? "Sarah's relationships" : "Sarah's relationships";
  if (c.path === "/v1/memories/search") return "Search memory: “state licence”";
  if (c.path === "/v1/context/build") return "Build a planning context about Sarah";
  return c.path === "/v1/entities/sarah" ? "Read Sarah (a real entity)" : "Read an id that does not exist";
}
function outcome(c: Call): { tone: "ok" | "hidden" | "empty"; text: string } {
  const r = c.response;
  if (c.status === 404) return { tone: "hidden", text: "404 not found" };
  if (c.path === "/v1/context/build") return r.status === "denied" ? { tone: "hidden", text: "status: denied (empty)" } : { tone: "ok", text: `status: ${r.status}, ${r.facts.length} facts, ${r.memories.length} memories` };
  if (c.path === "/v1/memories/search") return r.hits.length ? { tone: "ok", text: `${r.hits.length} hit(s)` } : { tone: "empty", text: "0 hits (nothing visible)" };
  if (Array.isArray(r)) return r.length ? { tone: "ok", text: `${r.length} relationships` } : { tone: "empty", text: "none" };
  return { tone: "ok", text: `200: ${r.name ?? "ok"}` };
}

export default function PermissionLens() {
  const { tape, error } = useTape("permissions");
  const calls = useMemo(() => tape?.steps.flatMap((s) => s.calls) ?? [], [tape]);
  const [open, setOpen] = useState<Call>();
  if (!tape) return <Loading error={error} />;
  const questions = calls.filter((c) => c.caller === "admin");
  const at = (q: Call, who: string) => calls.find((c) => c.method === q.method && c.path === q.path && c.caller === who)!;
  const a404 = calls.filter((c) => c.path.startsWith("/v1/entities/") && c.status === 404);
  const real = at(questions[0], "ungranted-agent");
  const fake = at(questions[1], "ungranted-agent");
  const same = JSON.stringify(real.response) === JSON.stringify(fake.response);
  return (
    <div className="kgi">
      <div className="kgi-lens">
        <table className="kgi-table">
          <caption className="visually-hidden">The same five questions asked by four callers</caption>
          <thead>
            <tr><th scope="col">Question</th>{CALLERS.map(([, name]) => <th scope="col" key={name}>{name}</th>)}</tr>
          </thead>
          <tbody>
            {questions.map((q) => (
              <tr key={q.path + q.method}>
                <th scope="row">{label(q)}</th>
                {CALLERS.map(([who]) => {
                  const c = at(q, who);
                  const o = outcome(c);
                  return (
                    <td key={who}>
                      <button type="button" className={`kgi-cell ${o.tone}`} style={{ border: 0, width: "100%", textAlign: "left", cursor: "pointer", color: "inherit", font: "inherit", fontSize: ".8rem" }} onClick={() => setOpen(c)} aria-label={`${label(q)} as ${who}: ${o.text}. Show the response.`}>
                        {o.text}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="kgi-note"><span className="pill good">visible</span> <span className="pill warn">allowed but empty</span> <span className="pill bad">hidden</span> Click any cell to see the exact response.</p>
      <div className="kgi-panel" style={{ margin: "1rem 0" }}>
        <h3>A hidden thing and a missing thing look identical</h3>
        <p>
          The agent with no grants reads Sarah's real id and a made-up id. {same ? <b>The two response bodies are byte-for-byte identical</b> : <b>The bodies differ</b>}: it cannot tell whether Sarah exists. Nothing, not even existence, leaks.
        </p>
        <div className="grid c2" style={{ display: "grid", gap: ".75rem", gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))" }}>
          <div><b>Sarah's real id</b><pre tabIndex={0}>{JSON.stringify(real.response, null, 2)}</pre></div>
          <div><b>An id that does not exist</b><pre tabIndex={0}>{JSON.stringify(fake.response, null, 2)}</pre></div>
        </div>
        <p className="kgi-note">(The per-request <code>request_id</code> is replaced with a placeholder in the recording; the rest is unchanged.) {a404.length} of the {calls.length} recorded calls were 404s.</p>
      </div>
      {open ? (<div><h3>Selected response</h3><CallView call={open} open /></div>) : null}
    </div>
  );
}
