import { useEffect, useMemo, useState } from "react";
import { CallView, Loading, useTape, type Call } from "./lib";

const ORDER = ["normalize", "graph_ingest", "memory_propose", "memory_embed"];
const LABELS: Record<string, string> = { normalize: "normalize", graph_ingest: "graph (Graphiti extraction)", memory_propose: "memory proposals + policy", memory_embed: "embed memories", memory_expire: "scheduled expiry" };

export default function StageTimeline() {
  const { tape, error } = useTape("write-path");
  const calls: Call[] = useMemo(() => tape?.steps.flatMap((s) => s.calls) ?? [], [tape]);
  const polls = calls.filter((c) => c.method === "GET");
  const post = calls.find((c) => c.method === "POST");
  const [i, setI] = useState(0);
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    if (i >= polls.length - 1) { setPlaying(false); return; }
    const t = setTimeout(() => setI((x) => x + 1), 1100);
    return () => clearTimeout(t);
  }, [playing, i, polls.length]);
  if (!tape || !post) return <Loading error={error} />;
  const cur = polls[Math.min(i, polls.length - 1)];
  const stages: any[] = cur.response.stages ?? [];
  const names = [...ORDER.filter((n) => stages.some((s) => s.stage === n) || n !== "memory_embed"), ...stages.map((s) => s.stage).filter((s: string) => !ORDER.includes(s))];
  return (
    <div className="kgi">
      <div className="kgi-row" style={{ marginBottom: ".75rem" }}>
        <button className="kgi-btn primary" type="button" onClick={() => { if (i >= polls.length - 1) setI(0); setPlaying((p) => !p); }}>{playing ? "Pause" : i >= polls.length - 1 ? "Replay" : "Play"}</button>
        <button className="kgi-btn" type="button" onClick={() => { setPlaying(false); setI((x) => Math.max(0, x - 1)); }} disabled={i === 0}>← Back</button>
        <button className="kgi-btn" type="button" onClick={() => { setPlaying(false); setI((x) => Math.min(polls.length - 1, x + 1)); }} disabled={i >= polls.length - 1}>Next →</button>
        <span className="pill">Poll {Math.min(i, polls.length - 1) + 1} of {polls.length}</span>
        <span className={`pill ${cur.response.processing_status === "indexed" ? "good" : "warn"}`}>{cur.response.processing_status}</span>
      </div>
      <p className="kgi-note">The conversation <i>“{(post.request as any).content}”</i> was accepted with a <code>202</code>, written to the ledger together with its outbox job in one transaction. Workers then run each stage; the page below is the interaction's status as it was polled during the real run.</p>
      <div className="kgi-stages" aria-live="polite">
        {names.map((n) => {
          const s = stages.find((x) => x.stage === n);
          const status = s?.status ?? "pending";
          const tone = ["done", "succeeded", "completed"].includes(status) ? "done" : ["failed", "dead", "dead_lettered"].includes(status) ? "fail" : status === "running" ? "run" : "";
          return (
            <div key={n} className={`kgi-stage ${tone}`}>
              <b>{LABELS[n] ?? n}</b>
              <div className="kgi-note" style={{ margin: 0 }}>{status}</div>
            </div>
          );
        })}
      </div>
      <ul className="kgi-note">
        <li><b>normalize</b> cleans and chunks the content; <b>graph</b> asks the model router for entities and facts, which arrive as <i>unverified proposals</i>;</li>
        <li><b>memory proposals</b> turn the conversation into candidate memories, and policy decides what activates; <b>embed</b> makes them searchable.</li>
        <li>Every stage is leased, retried with backoff and dead-lettered if it keeps failing, so a crash never loses an event.</li>
      </ul>
      <h3>The raw status</h3>
      <CallView call={cur} open />
      <CallView call={post} />
    </div>
  );
}
