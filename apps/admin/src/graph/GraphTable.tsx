import type { components } from "../api/schema";
import { EvidenceBadge, FactStatusBadge } from "../components/ui";
import { fmtDate } from "../lib/format";
import { edgeStyle } from "./styles";

type Graph = components["schemas"]["GraphOut"];

/** The accessible form of the neighbourhood: every relationship as a row, selectable with the keyboard. */
export function GraphTable({ graph, onSelect, selectedId }: { graph: Graph; onSelect: (sel: { kind: "node" | "edge"; id: string }) => void; selectedId?: string | null }) {
  const name = (id: string) => graph.nodes.find((n) => n.id === id)?.name ?? id.slice(0, 8);
  if (graph.edges.length === 0) return <p className="text-sm text-slate-500">No relationships in this view.</p>;
  return (
    <table className="w-full text-left text-sm" aria-label="Relationships in this neighbourhood">
      <thead className="text-slate-500"><tr><th className="py-1 pr-3">From</th><th className="pr-3">Relationship</th><th className="pr-3">To</th><th className="pr-3">Valid</th><th>Status</th></tr></thead>
      <tbody>
        {graph.edges.map((e) => {
          const s = edgeStyle(e);
          return (
            <tr key={e.id} data-testid={`edge-row-${e.id}`} aria-selected={selectedId === e.id} className={`border-t border-slate-200 dark:border-slate-800 ${selectedId === e.id ? "bg-sky-50 dark:bg-sky-950" : ""}`}>
              <td className="py-1 pr-3"><button type="button" className="underline-offset-2 hover:underline" onClick={() => onSelect({ kind: "node", id: e.source_id })}>{name(e.source_id)}</button></td>
              <td className="pr-3"><button type="button" className="font-mono text-xs underline-offset-2 hover:underline" onClick={() => onSelect({ kind: "edge", id: e.id })}>{e.rel_type}</button></td>
              <td className="pr-3"><button type="button" className="underline-offset-2 hover:underline" onClick={() => onSelect({ kind: "node", id: e.target_id })}>{name(e.target_id)}</button></td>
              <td className="whitespace-nowrap pr-3">{fmtDate(e.valid_from)} → {fmtDate(e.valid_to)}</td>
              <td className="space-x-1"><FactStatusBadge status={e.status} /><EvidenceBadge status={e.evidence_status} />{s.line !== "solid" ? <span className="text-xs text-slate-500">{s.line}</span> : null}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
