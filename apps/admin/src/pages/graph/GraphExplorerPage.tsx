import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { Badge, Banner, btn, EvidenceBadge, FactStatusBadge, inputCls } from "../../components/ui";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { GraphTable } from "../../graph/GraphTable";
import { GraphView } from "../../graph/GraphView";
import { HOPS_MAX, NODES_MAX } from "../../graph/styles";
import { displayValue, fmt, fmtDate } from "../../lib/format";
import { useActiveOntology } from "../../ontology/queries";

type Fact = components["schemas"]["FactOut"];
type Selection = { kind: "node" | "edge"; id: string } | null;

function EntitySearch({ onPick, selected }: { onPick: (id: string) => void; selected: string | null }) {
  const active = useActiveOntology();
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [type, setType] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setTerm(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  const list = useQuery({
    queryKey: ["graph-entity-search", term, type],
    queryFn: () => api.GET("/v1/entities", { params: { query: { q: term || undefined, type: type || undefined, limit: 30 } } }),
  });
  const rows = list.data?.data?.items ?? [];
  return (
    <section aria-label="Find an entity" className="space-y-2">
      <h2 className="font-medium">Entities</h2>
      <input aria-label="Search entities" className={`${inputCls} w-full`} placeholder="Search by name…" value={q} onChange={(e) => setQ(e.target.value)} />
      <select aria-label="Entity type" className={`${inputCls} w-full`} value={type} onChange={(e) => setType(e.target.value)}>
        <option value="">All types</option>
        {(active.data?.data?.entity_types ?? []).map((t) => <option key={t}>{t}</option>)}
      </select>
      {list.isLoading ? <Loading label="Searching…" /> : rows.length === 0 ? <p className="text-sm text-slate-500">No entities match.</p> : (
        <ul className="max-h-[24rem] space-y-1 overflow-auto text-sm">
          {rows.map((e) => (
            <li key={e.id}>
              <button type="button" onClick={() => onPick(e.id)} aria-current={selected === e.id} className={`flex w-full items-center justify-between rounded px-2 py-1 text-left hover:bg-slate-100 dark:hover:bg-slate-800 ${selected === e.id ? "bg-slate-100 font-medium dark:bg-slate-800" : ""}`}>
                <span>{e.name}</span>
                <span className="flex items-center gap-1">
                  {e.status === "candidate" || e.status === "untyped" ? <Badge tone="amber">{e.status}</Badge> : null}
                  <span className="font-mono text-xs text-slate-500">{e.type}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function EvidenceList({ fact }: { fact: Fact }) {
  if (fact.evidence.length === 0) return <p className="text-sm text-slate-500">No evidence is recorded for this fact.</p>;
  return (
    <ul className="space-y-1 text-sm" aria-label="Evidence">
      {fact.evidence.map((ev, i) => (
        <li key={i} className="flex flex-wrap items-center gap-2" data-testid="evidence-item">
          <Badge tone={ev.kind === "command" ? "green" : "blue"}>{ev.kind === "command" ? "command" : "interaction"}</Badge>
          {ev.kind === "command" && ev.command_id ? <Link className="underline" to={`/commands/${ev.command_id}`}>{ev.source_system ?? "command"}{ev.source_ref ? ` · ${ev.source_ref}` : ""}</Link> : null}
          {ev.kind === "episode" && ev.interaction_id ? <Link className="underline" to={`/interactions/${ev.interaction_id}`}>{ev.source_system ?? "interaction"}{ev.source_ref ? ` · ${ev.source_ref}` : ""}</Link> : null}
          <span className="text-xs text-slate-500">occurred {fmtDate(ev.occurred_at)} · recorded {fmt(ev.recorded_at)}</span>
          {ev.episode_uuid ? <span className="font-mono text-xs text-slate-500" title="The Graphiti episode">episode {ev.episode_uuid.slice(0, 8)}…</span> : null}
        </li>
      ))}
    </ul>
  );
}

function FactDetail({ factId }: { factId: string }) {
  const q = useQuery({ queryKey: ["fact", factId], queryFn: () => api.GET("/v1/relationships/{fact_id}", { params: { path: { fact_id: factId } } }) });
  if (q.isLoading) return <Loading label="Loading fact…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "This fact", () => void q.refetch());
  const f = q.data!.data;
  if (blocked || !f) return <>{blocked}</>;
  return (
    <div className="space-y-3" data-testid="fact-detail">
      <h3 className="font-medium"><span className="font-mono text-sm">{f.rel_type}</span></h3>
      <p className="text-sm">{f.fact_text ?? `${f.source.name} → ${f.target.name}`}</p>
      <div className="flex flex-wrap items-center gap-2"><FactStatusBadge status={f.status} /><EvidenceBadge status={f.evidence_status} /><Badge>{f.authority_class.replace(/_/g, " ")}</Badge></div>
      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Valid</dt><dd>{f.valid_from_unknown ? "start unknown" : fmtDate(f.valid_from)} → {fmtDate(f.valid_to)}</dd>
        <dt className="text-slate-500">Recorded</dt><dd>{fmt(f.recorded_at)}</dd>
        <dt className="text-slate-500">Source says</dt><dd>{f.source_recorded_at ? fmtDate(f.source_recorded_at) : "—"}</dd>
        {f.decision_code ? <><dt className="text-slate-500">Decision</dt><dd className="font-mono text-xs">{f.decision_code}</dd></> : null}
        {Object.entries(f.attributes).map(([k, v]) => <div key={k} className="contents"><dt className="text-slate-500">{k}</dt><dd>{displayValue(v)}</dd></div>)}
      </dl>
      <div className="space-y-1"><h4 className="text-sm font-medium">Why the system holds this</h4><EvidenceList fact={f} /></div>
    </div>
  );
}

function EntityPaneDetail({ entityId }: { entityId: string }) {
  const q = useQuery({ queryKey: ["entity", entityId, false], queryFn: () => api.GET("/v1/entities/{entity_id}", { params: { path: { entity_id: entityId }, query: {} } }) });
  const summary = useQuery({ queryKey: ["entity-summary", entityId], queryFn: () => api.GET("/v1/entities/{entity_id}/summary", { params: { path: { entity_id: entityId } } }) });
  if (q.isLoading) return <Loading label="Loading entity…" />;
  const blocked = stateForStatus(q.data?.response.status, "This entity");
  const e = q.data?.data;
  if (blocked || !e) return <>{blocked ?? <ErrorState />}</>;
  return (
    <div className="space-y-3" data-testid="entity-detail">
      <h3 className="font-medium">{e.name}</h3>
      <div className="flex flex-wrap items-center gap-2"><Badge>{e.type}</Badge>{e.status !== "active" ? <Badge tone="amber">{e.status}</Badge> : null}{e.origin === "extracted" ? <Badge tone="blue" title="Created by an extraction, not by a command">extracted</Badge> : null}</div>
      {e.merged_into_id ? <Banner tone="amber">Merged into <Link className="underline" to={`/graph?entity=${e.merged_into_id}`}>another entity</Link>. This id still resolves.</Banner> : null}
      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Aliases</dt><dd>{e.aliases.join(", ") || "—"}</dd>
        <dt className="text-slate-500">External ids</dt><dd>{e.external_ids.map((x) => `${x.system}:${x.value}`).join(", ") || "none"}</dd>
      </dl>
      {summary.data?.data?.summary ? (
        <div className="rounded border border-dashed border-slate-300 p-2 text-sm dark:border-slate-700">
          <Badge tone="slate" title="Generated text; never a source of facts">derived, unverified</Badge>
          <p className="mt-1">{summary.data.data.summary}</p>
        </div>
      ) : null}
      <Link to={`/entities/${e.id}`} className={btn.link}>Open entity page</Link>
    </div>
  );
}

export function GraphExplorerPage() {
  const can = useCan();
  if (!can.exploreGraph) return <PermissionDenied what="the Graph Explorer" />;
  return <Explorer />;
}

function Explorer() {
  const [params, setParams] = useSearchParams();
  const entityId = params.get("entity");
  const [hops, setHops] = useState(1);
  const [types, setTypes] = useState("");
  const [history, setHistory] = useState(false);
  const [showProposed, setShowProposed] = useState(false);
  const [showDisputed, setShowDisputed] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [knownAtOn, setKnownAtOn] = useState(false);
  const [knownAt, setKnownAt] = useState("");
  const [view, setView] = useState<"graph" | "table">("graph");
  const [sel, setSel] = useState<Selection>(null);
  const active = useActiveOntology();

  const include = useMemo(() => [history ? "historical" : "", showProposed ? "proposed" : "", showDisputed ? "disputed" : ""].filter(Boolean).join(","), [history, showProposed, showDisputed]);
  const q = useQuery({
    queryKey: ["entity-graph", entityId, hops, types, include, asOf, knownAtOn ? knownAt : ""],
    enabled: !!entityId,
    queryFn: () =>
      api.GET("/v1/entities/{entity_id}/graph", {
        params: { path: { entity_id: entityId! }, query: { hops, limit: NODES_MAX, types: types || undefined, include: include || undefined, as_of: asOf || undefined, known_at: knownAtOn && knownAt ? knownAt : undefined } },
      }),
  });
  const graph = q.data?.data;
  useEffect(() => setSel(entityId ? { kind: "node", id: entityId } : null), [entityId]);

  const pick = (id: string) => setParams({ entity: id });
  let center;
  if (!entityId) center = <Empty title="Pick an entity" hint="Search on the left, then follow its relationships." />;
  else if (q.isLoading) center = <Loading label="Loading neighbourhood…" />;
  else if (q.isError) center = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const blocked = stateForStatus(q.data!.response.status, "This neighbourhood", () => void q.refetch());
    if (blocked || !graph) center = blocked;
    else
      center = (
        <div className="space-y-2">
          {graph.truncated ? <Banner tone="amber" role="status">Showing the first {graph.nodes.length} entities. Narrow the view with fewer hops, an entity type or a date.</Banner> : null}
          {graph.nodes.length <= 1 && graph.edges.length === 0 ? <p className="text-sm text-slate-500">No relationships in this view.</p> : null}
          {view === "graph" ? <GraphView graph={graph} onSelect={setSel} /> : null}
          <details open={view === "table"} className="text-sm"><summary className="cursor-pointer text-slate-600 dark:text-slate-300">Table view of the same neighbourhood</summary><div className="mt-2"><GraphTable graph={graph} onSelect={setSel} selectedId={sel?.id} /></div></details>
          <Legend />
        </div>
      );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Graph Explorer</h1>
      <div className="grid gap-6 lg:grid-cols-[16rem_1fr_20rem]">
        <EntitySearch onPick={pick} selected={entityId} />
        <section aria-label="Neighbourhood" className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-end gap-3 text-sm">
            <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Hops</span>
              <select aria-label="Hops" className={inputCls} value={hops} onChange={(e) => setHops(Number(e.target.value))}>{Array.from({ length: HOPS_MAX }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}</option>)}</select></label>
            <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Entity type</span>
              <select aria-label="Show entity type" className={inputCls} value={types} onChange={(e) => setTypes(e.target.value)}><option value="">All</option>{(active.data?.data?.entity_types ?? []).map((t) => <option key={t}>{t}</option>)}</select></label>
            <label><span className="mb-1 block text-slate-600 dark:text-slate-300">True on</span>
              <input aria-label="As of date" type="date" className={inputCls} value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={knownAtOn} onChange={(e) => setKnownAtOn(e.target.checked)} /> Known on</label>
            {knownAtOn ? <input aria-label="Known at date" type="date" className={inputCls} value={knownAt} onChange={(e) => setKnownAt(e.target.value)} /> : null}
            <fieldset className="flex items-center gap-3"><legend className="sr-only">Also show</legend>
              <label className="flex items-center gap-1"><input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} /> ended</label>
              <label className="flex items-center gap-1"><input type="checkbox" checked={showProposed} onChange={(e) => setShowProposed(e.target.checked)} /> proposed</label>
              <label className="flex items-center gap-1"><input type="checkbox" checked={showDisputed} onChange={(e) => setShowDisputed(e.target.checked)} /> disputed</label>
            </fieldset>
            <button type="button" className={btn.secondary} onClick={() => setView(view === "graph" ? "table" : "graph")}>{view === "graph" ? "Hide graph" : "Show graph"}</button>
          </div>
          {center}
        </section>
        <aside aria-label="Details" className="space-y-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          {!sel ? <p className="text-sm text-slate-500">Select an entity or a relationship to see why the system holds it.</p> : sel.kind === "edge" ? <FactDetail factId={sel.id} /> : <EntityPaneDetail entityId={sel.id} />}
        </aside>
      </div>
    </div>
  );
}

function Legend() {
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-300">
      <li><span className="font-mono">──</span> current</li>
      <li><span className="font-mono">- -</span> ended (dates shown)</li>
      <li><span className="text-violet-700">──</span> unverified: from a conversation</li>
      <li><span className="text-amber-700 font-mono">···</span> proposed (waiting for review)</li>
      <li><span className="text-red-700 font-mono">- -</span> disputed</li>
      <li>dashed ring: candidate entity (identity not decided)</li>
    </ul>
  );
}
