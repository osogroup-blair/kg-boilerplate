import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { AuthorityBadge, Badge, Banner, btn, ClassificationBadge, EmbeddingBadge, EvidenceBadge, MemoryStatusBadge, MemoryTypeBadge } from "../../components/ui";
import { fmt } from "../../lib/format";
import { CorrectMemoryDialog } from "../corrections/CorrectionDialogs";
import { MemoryActionDialog } from "./MemoryActions";

type Memory = components["schemas"]["MemoryOut"];
type Evidence = components["schemas"]["MemoryEvidenceOut"];
type Lineage = components["schemas"]["LineageOut"];

const LINEAGE_LABEL: Record<Lineage["direction"], string> = {
  replaces: "This memory replaces",
  replaced_by: "Replaced by",
  published_from: "Published from the private memory",
  published_as: "Published as",
  consolidated_from: "Consolidated from",
  consolidated_into: "Consolidated into",
};

/** One evidence id with the place to read it. The CONTENT of a source is never fetched here: it is reached through the audited routes (an interaction for admins, the evidence of a review item for reviewers). */
function EvidenceItem({ e, reveal }: { e: Evidence; reveal: boolean }) {
  const suffix = reveal ? "?private=1" : "";
  let target: React.ReactNode = <span className="font-mono text-xs">{e.external_ref ?? e.source_kind}</span>;
  if (e.interaction_id && (e.source_kind === "interaction" || e.source_kind === "episode"))
    target = <Link className="underline" to={`/interactions/${e.interaction_id}`}>interaction {e.interaction_id.slice(0, 8)}…</Link>;
  else if (e.fact_id) target = <Link className="underline" to={`/graph?fact=${e.fact_id}`}>graph fact {e.fact_id.slice(0, 8)}…</Link>;
  else if (e.command_id) target = <Link className="underline" to={`/commands/${e.command_id}`}>command {e.command_id.slice(0, 8)}…</Link>;
  else if (e.memory_id) target = <Link className="underline" to={`/memories/${e.memory_id}${suffix}`}>memory {e.memory_id.slice(0, 8)}…</Link>;
  return (
    <li className="flex flex-wrap items-center gap-2 text-sm" data-testid={`evidence-${e.source_kind}`}>
      <Badge>{e.source_kind.replace(/_/g, " ")}</Badge>
      {target}
      {e.source_kind === "episode" && e.episode_uuid ? <span className="font-mono text-xs text-slate-500">episode {e.episode_uuid.slice(0, 8)}…</span> : null}
      <span className="text-xs text-slate-500">added {fmt(e.added_at)}</span>
    </li>
  );
}

/** Admin view (it reveals what agents have been reading): how many context builds included this memory, when last, and the builds to open. */
function UsedInContexts({ memoryId }: { memoryId: string }) {
  const can = useCan();
  const q = useQuery({ queryKey: ["memory-usage", memoryId], enabled: can.revealPrivateMemories, queryFn: () => api.GET("/v1/memories/{memory_id}/usage", { params: { path: { memory_id: memoryId } } }) });
  if (!can.revealPrivateMemories) return <section aria-label="Used in contexts" className="space-y-1"><h2 className="font-medium">Used in contexts</h2><p className="text-sm text-slate-500">Only workspace administrators can see which context builds used a memory.</p></section>;
  const u = q.data?.data;
  return (
    <section aria-label="Used in contexts" className="space-y-1">
      <h2 className="font-medium">Used in contexts</h2>
      {q.isLoading ? <Loading label="Loading usage…" /> : !u ? <p className="text-sm text-slate-500">Usage is not available.</p> : u.builds === 0 ? <p className="text-sm text-slate-500" data-testid="usage-none">No context build has included this memory.</p> : (
        <>
          <p className="text-sm" data-testid="usage-count">Included in {u.builds} context build{u.builds === 1 ? "" : "s"}; last used {fmt(u.last_used_at)}.</p>
          <ul className="space-y-1 text-sm">{(u.recent_context_ids ?? []).map((id) => <li key={id}><Link className="font-mono text-xs underline" to={`/context/${id}`}>{id.slice(0, 8)}…</Link></li>)}</ul>
        </>
      )}
    </section>
  );
}

function Lineages({ items, reveal }: { items: Lineage[]; reveal: boolean }) {
  if (items.length === 0) return <p className="text-sm text-slate-500">No other memory is linked to this one (that you may see).</p>;
  return (
    <ol className="space-y-1 border-l border-slate-300 pl-4 dark:border-slate-700" aria-label="Lineage">
      {items.map((l) => (
        <li key={`${l.relation}:${l.memory_id}:${l.direction}`} className="text-sm" data-testid={`lineage-${l.direction}`}>
          {LINEAGE_LABEL[l.direction]} <Link className="font-mono text-xs underline" to={`/memories/${l.memory_id}${reveal ? "?private=1" : ""}`}>{l.memory_id.slice(0, 8)}…</Link>
        </li>
      ))}
    </ol>
  );
}

export function MemoryDetailPage() {
  const { memoryId = "" } = useParams();
  const [params] = useSearchParams();
  const reveal = params.get("private") === "1";
  const can = useCan();
  const [action, setAction] = useState<"supersede" | "expire" | "dispute" | null>(null);
  const [correcting, setCorrecting] = useState(false);
  const q = useQuery({
    queryKey: ["memory", memoryId, reveal],
    enabled: can.readMemories,
    queryFn: async () => {
      const res = await api.GET("/v1/memories/{memory_id}", { params: { path: { memory_id: memoryId }, query: { reveal_private: reveal || undefined } } });
      return { res, etag: res.response.headers.get("etag") ?? "" };
    },
  });
  const reviews = useQuery({
    queryKey: ["memory-review", memoryId],
    enabled: can.curateMemories && q.data?.res.data?.status !== undefined && ["proposed", "disputed"].includes(q.data.res.data.status),
    queryFn: () => api.GET("/v1/reviews", { params: { query: { status: "open", limit: 100 } } }),
  });
  if (!can.readMemories) return <PermissionDenied what="memories" />;
  if (q.isLoading) return <Loading label="Loading memory…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const { res, etag } = q.data!;
  const blocked = stateForStatus(res.response.status, "This memory", () => void q.refetch()); // an invisible id is the same "Not found" as an unknown one
  if (blocked || !res.data) return <div className="space-y-4"><Link to="/memories" className={btn.link}>← Memories</Link>{blocked}</div>;
  const m: Memory = res.data;
  const review = reviews.data?.data?.items.find((r) => r.memory_id === m.id);
  const live = ["active", "disputed"].includes(m.status);
  const private_ = m.scopes.some((s) => s.kind === "agent" || s.kind === "session");
  const canChange = can.curateMemories && !private_;
  return (
    <div className="space-y-6">
      <Link to={`/memories${reveal ? "?private=1" : ""}`} className={btn.link}>← Memories</Link>
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Memory</h1>
        <MemoryTypeBadge type={m.type} /><MemoryStatusBadge status={m.status} /><EvidenceBadge status={m.evidence_status} /><AuthorityBadge authority={m.authority_class} />
        <ClassificationBadge classification={m.classification} /><EmbeddingBadge status={m.embedding_status} />
        <span className="font-mono text-xs text-slate-500">{m.id}</span>
      </div>
      {reveal && private_ ? <Banner tone="amber" role="status">You asked to see private memory. This read is recorded in the audit log with your name.</Banner> : null}
      {m.status === "proposed" ? <Banner tone="amber" role="status">Proposed: not served to agents until a reviewer approves it.{review ? <> <Link className="underline" to={`/reviews/${review.id}`}>Open the review item</Link>.</> : null}</Banner> : null}
      {m.status === "disputed" ? <Banner tone="red" role="status">Disputed: not served while the dispute is open.{review ? <> <Link className="underline" to={`/reviews/${review.id}`}>Open the review item</Link>.</> : null}</Banner> : null}
      {m.status === "superseded" || m.status === "expired" ? <Banner tone="slate" role="status">This memory is kept for history; it is not served any more.</Banner> : null}

      <section aria-label="Content" className="space-y-2">
        <h2 className="font-medium">Content</h2>
        <blockquote className="whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-3 text-sm dark:border-slate-800 dark:bg-slate-900" data-testid="memory-content">{m.content}</blockquote>
        <p className="text-sm text-slate-500">Summary: {m.summary}</p>
      </section>

      <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm" aria-label="Details">
        <dt className="text-slate-500">Origin</dt><dd>{m.origin}{m.derived_by_alias ? ` · model ${m.derived_by_alias}` : ""}{m.prompt_version ? ` · prompt ${m.prompt_version}` : ""} · pipeline {m.pipeline_version}</dd>
        <dt className="text-slate-500">Created by</dt><dd className="font-mono text-xs">{m.created_by}</dd>
        {m.purpose ? <><dt className="text-slate-500">Purpose</dt><dd>{m.purpose}</dd></> : null}
        <dt className="text-slate-500">Valid</dt><dd>from {fmt(m.effective_from)} to {m.effective_until ? fmt(m.effective_until) : "open"}{m.expires_at ? ` · expires ${fmt(m.expires_at)}` : ""}</dd>
        <dt className="text-slate-500">Recorded</dt><dd>{fmt(m.recorded_at)}{m.occurred_at ? ` · happened ${fmt(m.occurred_at)}` : ""}{m.last_confirmed_at ? ` · last confirmed ${fmt(m.last_confirmed_at)}` : ""}</dd>
        <dt className="text-slate-500">Importance</dt>
        <dd>{m.importance.toFixed(2)}{m.confidence != null ? ` · confidence ${m.confidence.toFixed(2)}` : " · confidence not calibrated"} <span className="text-xs text-slate-500">(retention hints, not truth: a high score never makes a guess a fact)</span></dd>
        {m.run_id ? <><dt className="text-slate-500">Run</dt><dd className="font-mono text-xs">{m.run_id}</dd></> : null}
        {m.assertion ? <><dt className="text-slate-500">Assertion</dt><dd className="font-mono text-xs">{JSON.stringify(m.assertion)}</dd></> : null}
      </dl>

      <section aria-label="Subjects and scopes" className="grid gap-4 md:grid-cols-2">
        <div>
          <h2 className="font-medium">About</h2>
          {m.subject_ids.length === 0 && m.topic_ids.length === 0 ? <p className="text-sm text-slate-500">No entity is linked.</p> : (
            <ul className="space-y-1 text-sm">{[...m.subject_ids, ...m.topic_ids].map((id) => <li key={id}><Link className="font-mono text-xs underline" to={`/entities/${id}`}>{id.slice(0, 8)}…</Link></li>)}</ul>
          )}
        </div>
        <div>
          <h2 className="font-medium">Who can see it</h2>
          <ul className="space-y-1 text-sm" data-testid="scopes">{m.scopes.map((s) => <li key={`${s.kind}:${s.ref}`}><Badge>{s.kind}</Badge> <span className="font-mono text-xs">{s.ref === "*" ? "everyone in the workspace" : s.ref}</span></li>)}</ul>
          <p className="mt-1 text-xs text-slate-500">Only the scopes you can use are listed.</p>
        </div>
      </section>

      <section aria-label="Evidence" className="space-y-2">
        <h2 className="font-medium">Evidence</h2>
        {m.evidence.length === 0 ? <p className="text-sm text-slate-500">No evidence recorded.</p> : <ul className="space-y-1">{m.evidence.map((e, i) => <EvidenceItem key={i} e={e} reveal={reveal} />)}</ul>}
        <p className="text-xs text-slate-500">These are references. To read a source, open the interaction (administrators; every read is audited) or the evidence of the review item (reviewers).</p>
      </section>

      <section aria-label="Lineage" className="space-y-2"><h2 className="font-medium">Lineage</h2><Lineages items={m.lineage} reveal={reveal} /></section>

      <section aria-label="Embedding" className="space-y-1">
        <h2 className="font-medium">Embedding</h2>
        <p className="text-sm">{m.embedding.model_id ? <>model <span className="font-mono text-xs">{m.embedding.model_id}</span> · version {m.embedding.version} · {m.embedding.dim} dims · embedded {fmt(m.embedding.embedded_at)}</> : <>No vector yet ({m.embedding.status.replace(/_/g, " ")}). The memory is still found by its words.</>}</p>
      </section>

      <UsedInContexts memoryId={m.id} />

      {canChange && m.status !== "proposed" ? (
        <section aria-label="Actions" className="flex flex-wrap gap-2">
          <button type="button" className={btn.primary} disabled={!live} onClick={() => setAction("supersede")}>Supersede…</button>
          <button type="button" className={btn.secondary} disabled={!live} onClick={() => setAction("expire")}>Expire…</button>
          <button type="button" className={btn.danger} disabled={m.status !== "active"} onClick={() => setAction("dispute")}>Dispute…</button>
          <button type="button" className={btn.secondary} disabled={!live} onClick={() => setCorrecting(true)}>Correct (with impact)…</button>
        </section>
      ) : null}
      {!can.curateMemories ? <Banner tone="slate" role="status">You can read this memory but not change it.</Banner> : null}
      {correcting ? <CorrectMemoryDialog memory={{ id: m.id, revision: m.revision }} onClose={() => setCorrecting(false)} /> : null}
      {action ? <MemoryActionDialog kind={action} memoryId={m.id} revision={etag} onClose={() => setAction(null)} onDone={() => setAction(null)} /> : null}
    </div>
  );
}
