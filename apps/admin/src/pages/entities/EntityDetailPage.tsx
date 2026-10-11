import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { Badge, btn, ProjectionBadge } from "../../components/ui";
import { ErrorState, Loading, stateForStatus } from "../../components/states";
import { displayValue, fmt, fmtDate, isRedacted } from "../../lib/format";
import { AssertRelationshipDialog, EndRelationshipDialog } from "./RelationshipDialogs";

type Fact = components["schemas"]["FactOut"];
const PHASE_TONE = { current: "green", historical: "slate", future: "blue" } as const;

function FactRow({ fact, entityId, onEnd, canEnd }: { fact: Fact; entityId: string; onEnd: (id: string) => void; canEnd: boolean }) {
  const outgoing = fact.source.id === entityId;
  const other = outgoing ? fact.target : fact.source;
  const attrs = Object.entries(fact.attributes);
  return (
    <tr className="border-t border-slate-200 align-top dark:border-slate-800" data-testid={`fact-${fact.id}`}>
      <td className="py-2 pr-4">
        <span className="font-mono text-xs">{fact.rel_type}</span>{" "}
        <span aria-label={outgoing ? "to" : "from"}>{outgoing ? "→" : "←"}</span>{" "}
        <Link to={`/entities/${other.id}`} className="underline-offset-2 hover:underline">{other.name}</Link>
        {attrs.length ? <div className="text-xs text-slate-500">{attrs.map(([k, v]) => `${k}: ${displayValue(v)}`).join(" · ")}</div> : null}
      </td>
      <td className="whitespace-nowrap pr-4">{fact.valid_from_unknown ? "unknown" : fmtDate(fact.valid_from)} → {fmtDate(fact.valid_to)}</td>
      <td className="pr-4"><Badge tone={PHASE_TONE[fact.phase]}>{fact.phase === "historical" ? "ended" : fact.phase}</Badge>{fact.status !== "active" ? <> <Badge tone="amber">{fact.status}</Badge></> : null}</td>
      <td className="whitespace-nowrap pr-4 text-xs"><span title="When this platform recorded it">recorded {fmt(fact.recorded_at)}</span>{fact.source_recorded_at ? <div title="When the source system learned it">source {fmtDate(fact.source_recorded_at)}</div> : null}</td>
      <td className="pr-4 text-xs">{fact.source_system ?? "—"}{fact.source_ref ? ` · ${fact.source_ref}` : ""}<div className="text-slate-500">{fact.authority_class.replace(/_/g, " ")}</div></td>
      <td className="whitespace-nowrap text-xs">
        <Link to={`/commands/${fact.source_command_id}`} className="underline-offset-2 hover:underline">command</Link>
        {canEnd && fact.status === "active" && !fact.valid_to ? <> · <button type="button" className={btn.link} onClick={() => onEnd(fact.id)}>End…</button></> : null}
      </td>
    </tr>
  );
}

export function EntityDetailPage() {
  const { entityId = "" } = useParams();
  const can = useCan();
  const [history, setHistory] = useState(false);
  const [adding, setAdding] = useState(false);
  const [ending, setEnding] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["entity", entityId, history],
    queryFn: () => api.GET("/v1/entities/{entity_id}", { params: { path: { entity_id: entityId }, query: { include: history ? "history" : undefined } } }),
  });

  if (q.isLoading) return <Loading label="Loading entity…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const res = q.data!;
  const blocked = stateForStatus(res.response.status, "This entity", () => void q.refetch()); // a foreign id is the same "Not found" as an unknown one
  if (blocked || !res.data) return <>{blocked}</>;
  const e = res.data;
  const attrs = Object.entries(e.attributes);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm"><Link to="/entities" className="text-slate-500 underline-offset-2 hover:underline">Entities</Link> /</p>
          <h1 className="text-xl font-semibold" data-testid="entity-name">{e.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm">
            <Badge>{e.type}</Badge><Badge tone="amber">{e.classification}</Badge><ProjectionBadge status={e.projection_status} />
            <span className="text-slate-500">revision {e.revision} · recorded {fmt(e.recorded_at)}</span>
          </div>
        </div>
        <div className="flex gap-2">
          <Link to={`/commands?entity_id=${e.id}`} className={btn.secondary}>Commands</Link>
          {can.writeEntities ? <button type="button" className={btn.primary} onClick={() => setAdding(true)}>Add relationship…</button> : null}
        </div>
      </div>

      <section aria-label="Identity" className="space-y-2">
        <h2 className="font-medium">Identity</h2>
        <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
          <dt className="text-slate-500">Platform id</dt><dd className="font-mono text-xs">{e.id}</dd>
          <dt className="text-slate-500">External ids</dt><dd>{e.external_ids.length ? e.external_ids.map((x) => <span key={`${x.system}:${x.value}`} className="mr-2 font-mono text-xs">{x.system}:{x.value}</span>) : "none (a name never identifies an entity)"}</dd>
          <dt className="text-slate-500">Aliases</dt><dd>{e.aliases.join(", ") || "—"}</dd>
        </dl>
      </section>

      <section aria-label="Attributes" className="space-y-2">
        <h2 className="font-medium">Attributes</h2>
        {attrs.length === 0 ? <p className="text-sm text-slate-500">No attributes.</p> : (
          <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
            {attrs.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-slate-500">{k}</dt>
                <dd>{isRedacted(v) ? <Badge tone="slate" title="Sensitive: only workspace admins can see the value">redacted</Badge> : displayValue(v)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <section aria-label="Relationships" className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Relationships</h2>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={history} onChange={(ev) => setHistory(ev.target.checked)} /> Show history</label>
        </div>
        {e.relationships.length === 0 ? <p className="text-sm text-slate-500">{history ? "No relationships, past or present." : "No current relationships."}</p> : (
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">Relationship</th><th className="pr-4">Valid</th><th className="pr-4">Status</th><th className="pr-4">Recorded</th><th className="pr-4">Source</th><th>Provenance</th></tr></thead>
            <tbody>{e.relationships.map((f) => <FactRow key={f.id} fact={f} entityId={e.id} canEnd={can.writeEntities} onEnd={setEnding} />)}</tbody>
          </table>
        )}
      </section>

      {adding ? <AssertRelationshipDialog entity={{ id: e.id, type: e.type, name: e.name }} onClose={() => setAdding(false)} /> : null}
      {ending ? <EndRelationshipDialog factId={ending} onClose={() => setEnding(null)} /> : null}
    </div>
  );
}
