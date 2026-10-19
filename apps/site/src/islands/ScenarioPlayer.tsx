import { useMemo, useState } from "react";
import { GraphCanvas, Detail, type GraphData } from "./Graph";
import { CallView, Loading, useTape, type Call, type Step } from "./lib";
import { ContextCard } from "./Context";

const graphOf = (s?: Step): GraphData | undefined => s?.calls.find((c) => c.snapshot && c.path.endsWith("/graph") && c.status === 200)?.response;
const keyCalls = (s: Step): Call[] => {
  // polling noise (backlog checks, repeated status reads) is folded: only the last of a run of identical GETs is shown
  const calls = s.calls.filter((c) => !c.snapshot && !c.path.startsWith("/v1/operations/"));
  return calls.filter((c, k) => !(c.method === "GET" && calls[k + 1] && calls[k + 1].method === "GET" && calls[k + 1].path === c.path));
};

export default function ScenarioPlayer() {
  const { tape, error } = useTape("sarah");
  const [i, setI] = useState(0);
  const [sel, setSel] = useState<{ kind: "node" | "edge"; id: string }>();
  const steps = tape?.steps ?? [];
  const step = steps[i];
  const graph = useMemo(() => {
    for (let k = i; k >= 0; k--) {
      const g = graphOf(steps[k]);
      if (g) return { g, k };
    }
    return undefined;
  }, [steps, i]);
  const added = useMemo(() => {
    const out = new Set<string>();
    if (!graph) return out;
    let prev: GraphData | undefined;
    for (let k = graph.k - 1; k >= 0 && !prev; k--) prev = graphOf(steps[k]);
    const before = new Set([...(prev?.nodes ?? []).map((n) => n.id), ...(prev?.edges ?? []).map((e) => e.id)]);
    if (graph.k === i) [...graph.g.nodes.map((n) => n.id), ...graph.g.edges.map((e) => e.id)].forEach((id) => !before.has(id) && out.add(id));
    return out;
  }, [graph, steps, i]);
  if (!tape || !step) return <Loading error={error} />;
  const calls = keyCalls(step);
  const contexts = calls.filter((c) => c.path === "/v1/context/build" && c.status === 200 && c.response?.context_text);
  const shown = calls.filter((c) => !contexts.includes(c));
  return (
    <div className="kgi">
      <div className="kgi-layout">
        <nav aria-label="Scenario steps">
          <ol className="kgi-steps">
            {steps.map((s, k) => (
              <li key={s.id}>
                <button type="button" onClick={() => { setI(k); setSel(undefined); }} aria-current={k === i ? "step" : undefined}>
                  <span className="n">{s.id}</span>
                  <span>{s.title}</span>
                </button>
              </li>
            ))}
          </ol>
        </nav>
        <div>
          <div className="kgi-row" style={{ justifyContent: "space-between", marginBottom: ".5rem" }}>
            <span className="pill">Step {step.id} of {steps.length === 11 ? "10 + outage" : steps.length}</span>
            <span className="kgi-row">
              <button className="kgi-btn" type="button" onClick={() => setI((x) => Math.max(0, x - 1))} disabled={i === 0}>← Back</button>
              <button className="kgi-btn primary" type="button" onClick={() => setI((x) => Math.min(steps.length - 1, x + 1))} disabled={i === steps.length - 1}>Next →</button>
            </span>
          </div>
          <h2 style={{ fontSize: "1.4rem" }}>{step.title}</h2>
          {step.story ? <p className="kgi-story">{step.story}</p> : null}
          {step.look_for ? <div className="kgi-look"><b>Look for:</b> {step.look_for}</div> : null}
          {contexts.map((c, k) => (
            <div key={k} style={{ marginBottom: "1rem" }}>
              <p className="kgi-note" style={{ margin: "0 0 .3rem" }}>
                {c.caller} asked: “{(c.request as any)?.task}” with a {(c.request as any)?.max_tokens}-token budget{(c.request as any)?.as_of ? ` as of ${(c.request as any).as_of.slice(0, 10)}` : ""}.
              </p>
              <ContextCard ctx={c.response} />
            </div>
          ))}
          {graph ? (
            <div style={{ margin: "1rem 0" }}>
              <h3>The graph{graph.k === i ? "" : ` (unchanged since step ${steps[graph.k].id})`}</h3>
              <GraphCanvas graph={graph.g} added={added} selected={sel?.id} onSelect={(kind, id) => setSel({ kind, id })} />
              <Detail graph={graph.g} kind={sel?.kind} id={sel?.id} />
            </div>
          ) : null}
          <h3>What the API returned</h3>
          <p className="kgi-note">Every request below is real traffic recorded from the Sarah example running on the platform.</p>
          {shown.length === 0 ? <p className="kgi-note">This step's evidence is the context shown above.</p> : shown.map((c, k) => <CallView key={k} call={c} />)}
        </div>
      </div>
      <p className="kgi-note">Recorded with commit {tape.recorded_with.git_sha} on {tape.recorded_with.router}.</p>
    </div>
  );
}
