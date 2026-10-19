import { useMemo, useState } from "react";
import { CallView, Loading, useTape, type Call } from "./lib";

const fmt = (s?: string | null) => (s ? s.slice(0, 10) : "—");

export default function TimeSlider() {
  const { tape, error } = useTape("time");
  const calls: Call[] = useMemo(() => tape?.steps.flatMap((s) => s.calls) ?? [], [tape]);
  const asOfCalls = useMemo(() => calls.filter((c) => c.query?.as_of).sort((a, b) => a.query!.as_of.localeCompare(b.query!.as_of)), [calls]);
  const [i, setI] = useState(0);
  const [axis, setAxis] = useState<"as_of" | "known_at">("as_of");
  if (!tape) return <Loading error={error} />;
  const call = asOfCalls[Math.min(i, asOfCalls.length - 1)];
  const known = calls.find((c) => c.query?.known_at);
  const latest = calls.find((c) => !c.query || Object.keys(c.query).length === 0);
  const facts = (call?.response as any[]) ?? [];
  return (
    <div className="kgi">
      <div className="kgi-row" role="tablist" aria-label="Time axis" style={{ marginBottom: "1rem" }}>
        <button role="tab" aria-selected={axis === "as_of"} className={`kgi-btn ${axis === "as_of" ? "primary" : ""}`} onClick={() => setAxis("as_of")} type="button">as_of: when was it true?</button>
        <button role="tab" aria-selected={axis === "known_at"} className={`kgi-btn ${axis === "known_at" ? "primary" : ""}`} onClick={() => setAxis("known_at")} type="button">known_at: when did we know it?</button>
      </div>
      {axis === "as_of" ? (
        <>
          <label className="kgi-field">
            <span>Asking about <b>{call.query!.as_of.slice(0, 7)}</b></span>
            <input type="range" min={0} max={asOfCalls.length - 1} value={i} onChange={(e) => setI(Number(e.target.value))} aria-valuetext={call.query!.as_of.slice(0, 7)} />
          </label>
          <div className="kgi-row kgi-note" style={{ justifyContent: "space-between" }}><span>{asOfCalls[0].query!.as_of.slice(0, 7)}</span><span>Sarah leaves Acme on 2027-02-01</span><span>{asOfCalls[asOfCalls.length - 1].query!.as_of.slice(0, 7)}</span></div>
          <FactTable facts={facts} />
          <p className="kgi-note">Valid time: each fact has <code>valid_from</code> and <code>valid_to</code>. Before 2027-02 she works at Acme; after, the same fact is history and drops out of the "current" answer, but is never deleted.</p>
          <CallView call={call} />
        </>
      ) : (
        <>
          <div className="kgi-grid" style={{ display: "grid", gap: "1rem", gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))" }}>
            <div>
              <h3>Known just before the conversation was recorded</h3>
              <FactTable facts={(known?.response as any[]) ?? []} />
            </div>
            <div>
              <h3>Known now</h3>
              <FactTable facts={(latest?.response as any[]) ?? []} />
            </div>
          </div>
          <p className="kgi-note">Record time: the platform stamps when it learned each fact. The "considering entrepreneurship" fact came from a conversation, so it did not exist before that message was recorded, even though it describes the present. Use <code>known_at</code> to answer “what did we believe then?”.</p>
          {known ? <CallView call={known} /> : null}
        </>
      )}
    </div>
  );
}

function FactTable({ facts }: { facts: any[] }) {
  if (!facts.length) return <p className="kgi-note">No facts.</p>;
  return (
    <table className="kgi-table">
      <thead><tr><th>Fact</th><th>Valid from</th><th>Valid to</th><th>Evidence</th></tr></thead>
      <tbody>
        {facts.map((f) => (
          <tr key={f.id}>
            <td>{f.fact_text ?? `${f.rel_type}${f.attributes?.role ? ` (${f.attributes.role})` : ""}`}</td>
            <td>{f.valid_from ? fmt(f.valid_from) : "unknown"}</td>
            <td>{f.valid_to ? fmt(f.valid_to) : "now"}</td>
            <td><span className={`pill ${f.evidence_status === "verified" ? "good" : "warn"}`}>{f.evidence_status ?? "?"}</span></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
