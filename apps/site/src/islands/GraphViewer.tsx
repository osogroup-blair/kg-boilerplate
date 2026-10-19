import { useMemo, useState } from "react";
import { Detail, GraphCanvas, type GraphData } from "./Graph";
import { Loading, useTape } from "./lib";

export default function GraphViewer() {
  const sarah = useTape("sarah");
  const crm = useTape("crm");
  const options = useMemo(() => {
    const out: { key: string; label: string; graph: GraphData }[] = [];
    for (const s of sarah.tape?.steps ?? []) {
      const g = s.calls.find((c) => c.snapshot && c.path.endsWith("/graph") && c.status === 200)?.response;
      if (g && ["2", "4", "7", "9"].includes(s.id)) out.push({ key: `s${s.id}`, label: `Sarah, after step ${s.id}: ${s.title}`, graph: g });
    }
    const gs = crm.tape?.steps.find((s) => s.id === "graph")?.calls ?? [];
    gs.forEach((c, k) => c.status === 200 && out.push({ key: `c${k}`, label: `CRM: ${k === 0 ? "Globex" : "Initech"} neighbourhood`, graph: c.response }));
    return out;
  }, [sarah.tape, crm.tape]);
  const [k, setK] = useState(0);
  const [sel, setSel] = useState<{ kind: "node" | "edge"; id: string }>();
  if (!options.length) return <Loading error={sarah.error ?? crm.error} />;
  const cur = options[Math.min(k, options.length - 1)];
  return (
    <div className="kgi">
      <label className="kgi-field" style={{ marginBottom: ".75rem" }}>
        Graph
        <select value={k} onChange={(e) => { setK(Number(e.target.value)); setSel(undefined); }}>{options.map((o, i) => <option key={o.key} value={i}>{o.label}</option>)}</select>
      </label>
      <GraphCanvas graph={cur.graph} selected={sel?.id} onSelect={(kind, id) => setSel({ kind, id })} />
      <div style={{ marginTop: ".75rem" }}><Detail graph={cur.graph} kind={sel?.kind} id={sel?.id} /></div>
      <p className="kgi-note">This is the Neo4j/Graphiti projection of the Postgres ledger as the admin console's Graph Explorer draws it. Facts from systems of record are solid; facts extracted from conversations are dashed until a person or a trusted source verifies them.</p>
    </div>
  );
}
