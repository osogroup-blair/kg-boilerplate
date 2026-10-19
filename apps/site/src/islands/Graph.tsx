import { useEffect, useRef, useState } from "react";
import "./islands.css";

export interface GNode { id: string; type: string; name: string; status?: string; origin?: string; hop?: number }
export interface GEdge {
  id: string; rel_type: string; source_id: string; target_id: string; status?: string; evidence_status?: string;
  authority_class?: string; phase?: string; valid_from?: string | null; valid_to?: string | null; fact_text?: string | null;
}
export interface GraphData { center_id?: string; nodes: GNode[]; edges: GEdge[] }

const css = (name: string, fallback: string) => (typeof document === "undefined" ? fallback : getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback);
const nodeColor = (type: string) => (type === "Person" ? css("--kg-node-person", "#4f6bed") : type === "Organization" ? css("--kg-node-org", "#0f9d8a") : type === "Topic" ? css("--kg-node-topic", "#d9822b") : css("--kg-node-other", "#7a8499"));

export function GraphCanvas({ graph, added = new Set<string>(), selected, onSelect }: { graph: GraphData; added?: Set<string>; selected?: string; onSelect?: (kind: "node" | "edge", id: string) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const [table, setTable] = useState(false);
  useEffect(() => {
    if (table || !host.current) return;
    let cy: any;
    let dead = false;
    import("cytoscape").then(({ default: cytoscape }) => {
      if (dead || !host.current) return;
      const text = css("--kg-text", "#151a28");
      const muted = css("--kg-muted", "#566074");
      cy = cytoscape({
        container: host.current,
        elements: [
          ...graph.nodes.map((n) => ({ data: { id: n.id, label: n.name, type: n.type, color: nodeColor(n.type), isNew: added.has(n.id) ? 1 : 0 } })),
          ...graph.edges.map((e) => ({
            data: { id: e.id, source: e.source_id, target: e.target_id, label: e.rel_type.replace(/_/g, " ").toLowerCase(), unverified: e.evidence_status === "unverified" ? 1 : 0, ended: e.phase && e.phase !== "current" ? 1 : 0, isNew: added.has(e.id) ? 1 : 0 },
          })),
        ],
        layout: { name: "cose", animate: false, padding: 24, nodeRepulsion: () => 9000, idealEdgeLength: () => 110 },
        minZoom: 0.4,
        maxZoom: 2.5,
        style: [
          { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: text, "font-size": 12, "text-valign": "bottom", "text-margin-y": 5, width: 30, height: 30, "border-width": 2, "border-color": "data(color)" } },
          { selector: "node[isNew = 1]", style: { "border-color": "#0f7b4f", "border-width": 5 } },
          { selector: "edge", style: { width: 2, "line-color": muted, "target-arrow-color": muted, "target-arrow-shape": "triangle", "curve-style": "bezier", label: "data(label)", "font-size": 10, color: muted, "text-rotation": "autorotate", "text-background-color": css("--kg-surface", "#fff"), "text-background-opacity": 0.85, "text-background-padding": "2px" } },
          { selector: "edge[unverified = 1]", style: { "line-style": "dashed" } },
          { selector: "edge[ended = 1]", style: { opacity: 0.45 } },
          { selector: "edge[isNew = 1]", style: { "line-color": "#0f7b4f", "target-arrow-color": "#0f7b4f", width: 4 } },
          { selector: ":selected", style: { "overlay-color": css("--kg-accent", "#3b4fd8"), "overlay-opacity": 0.2, "overlay-padding": 8 } },
        ],
      });
      cy.on("tap", "node", (ev: any) => onSelect?.("node", ev.target.id()));
      cy.on("tap", "edge", (ev: any) => onSelect?.("edge", ev.target.id()));
      if (selected) cy.getElementById(selected).select();
    });
    return () => {
      dead = true;
      cy?.destroy();
    };
  }, [graph, table, selected, onSelect, added]);

  const byId = new Map(graph.nodes.map((n) => [n.id, n.name]));
  return (
    <div>
      <div className="kgi-row" style={{ justifyContent: "space-between", marginBottom: ".4rem" }}>
        <span className="kgi-note" style={{ margin: 0 }}>Solid line: verified. Dashed: unverified. Faded: ended. Green outline: new in this step.</span>
        <button className="kgi-btn" type="button" onClick={() => setTable((t) => !t)} aria-pressed={table}>{table ? "Show graph" : "Show as table"}</button>
      </div>
      {table ? (
        <table className="kgi-table">
          <caption className="visually-hidden">Entities and relationships</caption>
          <thead><tr><th>From</th><th>Relationship</th><th>To</th><th>Evidence</th><th>Valid</th></tr></thead>
          <tbody>
            {graph.edges.map((e) => (
              <tr key={e.id}>
                <td>{byId.get(e.source_id) ?? e.source_id}</td>
                <td>{e.rel_type}</td>
                <td>{byId.get(e.target_id) ?? e.target_id}</td>
                <td>{e.evidence_status ?? "-"}</td>
                <td>{(e.valid_from ?? "?").slice(0, 10)} → {e.valid_to ? e.valid_to.slice(0, 10) : "now"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div ref={host} className="kgi-graph" role="img" aria-label={`Graph with ${graph.nodes.length} entities and ${graph.edges.length} relationships. Use the table view for a text version.`} />
      )}
    </div>
  );
}

export function Detail({ graph, kind, id }: { graph: GraphData; kind?: "node" | "edge"; id?: string }) {
  if (!id) return <p className="kgi-note">Click an entity or a line to see its facts, evidence and validity.</p>;
  if (kind === "edge") {
    const e = graph.edges.find((x) => x.id === id);
    if (!e) return null;
    const name = (i: string) => graph.nodes.find((n) => n.id === i)?.name ?? i;
    return (
      <div className="kgi-panel">
        <h3>{name(e.source_id)} → {e.rel_type} → {name(e.target_id)}</h3>
        <div className="kgi-kv">
          <span>evidence <b>{e.evidence_status}</b></span>
          <span>authority <b>{e.authority_class}</b></span>
          <span>phase <b>{e.phase}</b></span>
          <span>valid <b>{(e.valid_from ?? "unknown").slice(0, 10)} → {e.valid_to ? e.valid_to.slice(0, 10) : "now"}</b></span>
        </div>
        {e.fact_text ? <p>“{e.fact_text}”</p> : <p className="kgi-note">Written by a command from a system of record: no model was involved.</p>}
      </div>
    );
  }
  const n = graph.nodes.find((x) => x.id === id);
  if (!n) return null;
  const edges = graph.edges.filter((e) => e.source_id === id || e.target_id === id);
  return (
    <div className="kgi-panel">
      <h3>{n.name} <span className="pill plain">{n.type}</span></h3>
      <div className="kgi-kv"><span>origin <b>{n.origin}</b></span><span>status <b>{n.status}</b></span><span>relationships <b>{edges.length}</b></span></div>
    </div>
  );
}
