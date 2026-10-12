import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { useEffect, useRef } from "react";
import type { components } from "../api/schema";
import { edgeStyle, nodeStyle, STYLESHEET } from "./styles";

type Graph = components["schemas"]["GraphOut"];

export function toElements(g: Graph): ElementDefinition[] {
  const nodes: ElementDefinition[] = g.nodes.map((n) => {
    const s = nodeStyle(n);
    return { group: "nodes", data: { id: n.id, label: s.badge ? `${n.name} (${s.badge})` : n.name, kind: "node" }, classes: [...s.classes, n.id === g.center_id ? "center" : ""].filter(Boolean).join(" ") };
  });
  const edges: ElementDefinition[] = g.edges.map((e) => {
    const s = edgeStyle(e);
    return { group: "edges", data: { id: e.id, source: e.source_id, target: e.target_id, label: s.label, kind: "edge" }, classes: s.classes.join(" ") };
  });
  return [...nodes, ...edges];
}

/** A thin wrapper: Cytoscape draws, React owns the data. The same neighbourhood is also offered as a table (GraphTable) for keyboards and screen readers. */
export function GraphView({ graph, onSelect }: { graph: Graph; onSelect: (sel: { kind: "node" | "edge"; id: string } | null) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const pick = useRef(onSelect);
  pick.current = onSelect;

  useEffect(() => {
    if (!host.current) return;
    const instance = cytoscape({ container: host.current, elements: toElements(graph), style: STYLESHEET as never, layout: { name: "cose", animate: false, padding: 24 }, wheelSensitivity: 0.2 });
    instance.on("tap", (ev) => {
      if (ev.target === instance) return pick.current(null);
      const t = ev.target;
      pick.current({ kind: t.data("kind"), id: t.id() });
    });
    cy.current = instance;
    return () => {
      instance.destroy();
      cy.current = null;
    };
  }, [graph]);

  return <div ref={host} data-testid="graph-canvas" role="img" aria-label={`Graph of ${graph.nodes.length} entities and ${graph.edges.length} relationships; a table with the same content is available`} className="h-[28rem] w-full rounded border border-slate-200 dark:border-slate-800" />;
}
