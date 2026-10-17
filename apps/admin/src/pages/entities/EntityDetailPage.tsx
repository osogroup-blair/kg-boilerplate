import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { Badge, Banner, btn, EvidenceBadge, FactStatusBadge, ProjectionBadge } from "../../components/ui";
import { ErrorState, Loading, stateForStatus } from "../../components/states";
import { displayValue, fmt, fmtDate, isRedacted } from "../../lib/format";
import { CorrectEntityDialog, CorrectFactDialog, SplitEntityDialog } from "../corrections/CorrectionDialogs";
import { AssertRelationshipDialog, EndRelationshipDialog } from "./RelationshipDialogs";

type Fact = components["schemas"]["FactOut"];
const PHASE_TONE = { current: "green", historical: "slate", future: "blue" } as const;

function FactRow({ fact, entityId, onEnd, canEnd, onCorrect }: { fact: Fact; entityId: string; onEnd: (id: string) => void; canEnd: boolean; onCorrect?: (f: Fact) => void }) {
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
      <td className="space-x-1 pr-4"><Badge tone={PHASE_TONE[fact.phase]}>{fact.phase === "historical" ? "ended" : fact.phase}</Badge>{fact.status !== "active" ? <FactStatusBadge status={fact.status} /> : null}<EvidenceBadge status={fact.evidence_status} /></td>
      <td className="whitespace-nowrap pr-4 text-xs"><span title="When this platform recorded it">recorded {fmt(fact.recorded_at)}</span>{fact.source_recorded_at ? <div title="When the source system learned it">source {fmtDate(fact.source_recorded_at)}</div> : null}</td>
      <td className="pr-4 text-xs">{fact.source_system ?? "—"}{fact.source_ref ? ` · ${fact.source_ref}` : ""}<div className="text-slate-500">{fact.authority_class.replace(/_/g, " ")}</div></td>
      <td className="whitespace-nowrap text-xs">
        {fact.source_command_id ? <Link to={`/commands/${fact.source_command_id}`} className="underline-offset-2 hover:underline">command</Link> : fact.source_interaction_id ? <Link to={`/interactions/${fact.source_interaction_id}`} className="underline-offset-2 hover:underline">interaction</Link> : "—"}
        {fact.fact_text ? <div className="max-w-xs whitespace-normal text-slate-500" title="The text the source gave">“{fact.fact_text}”</div> : null}
        {canEnd && fact.status === "active" && !fact.valid_to ? <> · <button type="button" className={btn.link} onClick={() => onEnd(fact.id)}>End…</button></> : null}
        {onCorrect && fact.status !== "deleted" && fact.status !== "superseded" ? <> · <button type="button" className={btn.link} aria-label={`Correct ${fact.rel_type} fact`} onClick={() => onCorrect(fact)}>Correct…</button></> : null}
      </td>
    </tr>
  );
}

export function EntityDetailPage() {
  const { entityId = "" } = useParams();
  const can = useCan();
  const [history, setHistory] = useState(false);
  const [proposed, setProposed] = useState(false);
  const [disputed, setDisputed] = useState(false);
  const [tab, setTab] = useState<"relationships" | "mentions" | "claims">("relationships");
  const include = [history ? "history" : "", proposed ? "proposed" : "", disputed ? "disputed" : ""].filter(Boolean).join(",");
  const [adding, setAdding] = useState(false);
  const [ending, setEnding] = useState<string | null>(null);
  const [correctingFact, setCorrectingFact] = useState<Fact | null>(null);
  const [correctingEntity, setCorrectingEntity] = useState(false);
  const [splitting, setSplitting] = useState(false);
  const q = useQuery({
    queryKey: ["entity", entityId, include === "history" ? true : include === "" ? false : include],
    queryFn: () => api.GET("/v1/entities/{entity_id}", { params: { path: { entity_id: entityId }, query: { include: include || undefined } } }),
  });
  const mentions = useQuery({ queryKey: ["entity-mentions", entityId], enabled: tab === "mentions", queryFn: () => api.GET("/v1/entities/{entity_id}/mentions", { params: { path: { entity_id: entityId } } }) });
  const claims = useQuery({ queryKey: ["entity-claims", entityId], enabled: tab === "claims", queryFn: () => api.GET("/v1/entities/{entity_id}/attribute-claims", { params: { path: { entity_id: entityId } } }) });

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
            <Badge>{e.type}</Badge>{e.status !== "active" ? <Badge tone="amber">{e.status}</Badge> : null}{e.origin === "extracted" ? <Badge tone="blue" title="Created by an extraction, not by a command">extracted</Badge> : null}<Badge tone="amber">{e.classification}</Badge><ProjectionBadge status={e.projection_status} />
            <span className="text-slate-500">revision {e.revision} · recorded {fmt(e.recorded_at)}</span>
          </div>
        </div>
        <div className="flex gap-2">
          <Link to={`/graph?entity=${e.id}`} className={btn.secondary}>Graph Explorer</Link>
          {e.memories != null ? <Link to={`/memories?subject_id=${e.id}`} className={btn.secondary} data-testid="entity-memories-link">Memories ({e.memories})</Link> : null}
          <Link to={`/commands?entity_id=${e.id}`} className={btn.secondary}>Commands</Link>
          {can.correctRecords && e.status === "active" ? <button type="button" className={btn.secondary} onClick={() => setCorrectingEntity(true)}>Correct…</button> : null}
          {can.correctRecords && e.status === "active" ? <button type="button" className={btn.secondary} onClick={() => setSplitting(true)}>Split…</button> : null}
          {can.writeEntities ? <button type="button" className={btn.primary} onClick={() => setAdding(true)}>Add relationship…</button> : null}
        </div>
      </div>

      {e.merged_into_id ? <Banner tone="amber" role="status">This entity was merged into <Link className="underline" to={`/entities/${e.merged_into_id}`}>another entity</Link>. Its id still resolves; its name is now an alias there.</Banner> : null}
      {e.status === "candidate" ? <Banner tone="amber" role="status">This entity came from an extraction and resembles another one. Its facts wait as proposed until a reviewer decides if they are the same. <Link className="underline" to="/reviews">Review queue</Link></Banner> : null}
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

      <div role="tablist" aria-label="Entity sections" className="flex gap-2 border-b border-slate-200 dark:border-slate-800">
        {([["relationships", "Relationships"], ["mentions", "Mentions"], ["claims", "Attribute claims"]] as const).map(([k, label]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)} className={`px-3 py-1.5 text-sm ${tab === k ? "border-b-2 border-slate-900 font-medium dark:border-slate-100" : "text-slate-600 dark:text-slate-300"}`}>{label}</button>
        ))}
      </div>
      {tab === "relationships" ? (
      <section aria-label="Relationships" className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Relationships</h2>
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-2"><input type="checkbox" checked={history} onChange={(ev) => setHistory(ev.target.checked)} /> Show history</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={proposed} onChange={(ev) => setProposed(ev.target.checked)} /> Show proposed</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={disputed} onChange={(ev) => setDisputed(ev.target.checked)} /> Show disputed</label>
          </div>
        </div>
        {e.relationships.length === 0 ? <p className="text-sm text-slate-500">{history ? "No relationships, past or present." : "No current relationships."}</p> : (
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">Relationship</th><th className="pr-4">Valid</th><th className="pr-4">Status</th><th className="pr-4">Recorded</th><th className="pr-4">Source</th><th>Provenance</th></tr></thead>
            <tbody>{e.relationships.map((f) => <FactRow key={f.id} fact={f} entityId={e.id} canEnd={can.writeEntities} onEnd={setEnding} onCorrect={can.correctRecords ? setCorrectingFact : undefined} />)}</tbody>
          </table>
        )}
      </section>
      ) : null}
      {tab === "mentions" ? (
        <section aria-label="Mentions" className="space-y-2">
          <h2 className="font-medium">Mentions</h2>
          {mentions.isLoading ? <Loading label="Loading mentions…" /> : (mentions.data?.data ?? []).length === 0 ? <p className="text-sm text-slate-500">No interaction mentions this entity.</p> : (
            <table className="w-full text-left text-sm"><thead className="text-slate-500"><tr><th className="py-1 pr-4">Interaction</th><th className="pr-4">Resolved by</th><th className="pr-4">Occurred</th><th>Recorded</th></tr></thead>
              <tbody>{(mentions.data?.data ?? []).map((m) => (
                <tr key={`${m.interaction_id}:${m.episode_uuid}`} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="py-1 pr-4"><Link className="underline" to={`/interactions/${m.interaction_id}`}>{m.source_system} · {m.source_ref}</Link></td>
                  <td className="pr-4"><Badge tone={m.status === "accepted" ? "green" : "amber"}>{m.resolution.replace(/_/g, " ")}{m.status === "needs_review" ? " · needs review" : ""}</Badge></td>
                  <td className="pr-4">{fmtDate(m.occurred_at)}</td><td>{fmt(m.recorded_at)}</td>
                </tr>))}</tbody></table>
          )}
        </section>
      ) : null}
      {tab === "claims" ? (
        <section aria-label="Attribute claims" className="space-y-2">
          <h2 className="font-medium">Attribute claims</h2>
          <p className="text-sm text-slate-500">Values an extraction proposed for this entity. They never change it; accepting one is a correction.</p>
          {claims.isLoading ? <Loading label="Loading claims…" /> : (claims.data?.data ?? []).length === 0 ? <p className="text-sm text-slate-500">No claims.</p> : (
            <ul className="space-y-1 text-sm">{(claims.data?.data ?? []).map((c) => <li key={c.id}><span className="font-mono text-xs">{c.attribute_key}</span> = {displayValue(c.value)} <Badge tone="blue">{c.status}</Badge> <Link className="underline" to={`/interactions/${c.interaction_id}`}>evidence</Link></li>)}</ul>
          )}
        </section>
      ) : null}

      {adding ? <AssertRelationshipDialog entity={{ id: e.id, type: e.type, name: e.name }} onClose={() => setAdding(false)} /> : null}
      {ending ? <EndRelationshipDialog factId={ending} onClose={() => setEnding(null)} /> : null}
      {correctingFact ? <CorrectFactDialog fact={correctingFact} onClose={() => setCorrectingFact(null)} /> : null}
      {correctingEntity ? <CorrectEntityDialog entity={{ id: e.id, revision: e.revision, name: e.name }} attributes={Object.keys(e.attributes)} onClose={() => setCorrectingEntity(false)} /> : null}
      {splitting ? <SplitEntityDialog entity={{ id: e.id, revision: e.revision, name: e.name, type: e.type, externalIds: e.external_ids }} onClose={() => setSplitting(false)} /> : null}
    </div>
  );
}
