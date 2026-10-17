import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ErrorState, Loading, stateForStatus } from "../../components/states";
import { Badge, Banner, btn, Field, inputCls, ProcessingBadge } from "../../components/ui";
import { fmt, newKey } from "../../lib/format";
import { ReasonDialog } from "./ReasonDialog";
import { StageTimeline } from "./StageTimeline";

type Content = components["schemas"]["ContentOut"];
const REPLAYABLE = new Set(["dead", "failed", "invalid"]);

type StageRow = components["schemas"]["InteractionDetail"]["stages"][number];
/** Stages a replay can pick. An event accepted before graph_ingest existed (skipped_disabled) or while no ontology was active can be replayed with a re-pin. */
export function replayableStages(stages: StageRow[]): Array<{ stage: string; repin: boolean }> {
  return stages.flatMap((s): Array<{ stage: string; repin: boolean }> => {
    if (REPLAYABLE.has(s.status)) return [{ stage: s.stage, repin: false }];
    if (s.stage === "memory_propose" && s.status === "skipped_disabled" && s.enabled) return [{ stage: s.stage, repin: true }]; // accepted before the memory stage existed
    if (s.stage === "graph_ingest" && (s.status === "skipped_disabled" || (s.status === "skipped_by_policy" && s.last_error_code === "no_active_ontology"))) return [{ stage: s.stage, repin: true }];
    return [];
  });
}

/** What the graph_ingest stage produced (EVD-03): the episode, the entities and facts, the review items it opened, what it cost. Ids and counts only. */
export function GraphIngestSummary({ stage }: { stage: StageRow }) {
  const r = (stage.result ?? {}) as Record<string, unknown>;
  if (stage.stage !== "graph_ingest" || !r.episode_uuid) return null;
  const list = (k: string) => ((r[k] as string[] | undefined) ?? []);
  const facts = (r.facts ?? {}) as Record<string, number>;
  return (
    <section aria-labelledby="gi-h" className="space-y-2" data-testid="graph-ingest-summary">
      <h2 id="gi-h" className="font-medium">Extraction</h2>
      <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Episode</dt><dd className="font-mono text-xs">{String(r.episode_uuid)}{r.reused ? " · reused: the model was not called again" : ""}</dd>
        <dt className="text-slate-500">Entities</dt><dd>{String(r.entities_linked ?? 0)} linked to named subjects · {String(r.entities_new ?? 0)} new · {String(r.entities_forked ?? 0)} kept apart for review · {String(r.name_variants ?? 0)} name variants</dd>
        <dt className="text-slate-500">Facts</dt><dd>{Object.keys(facts).length === 0 ? "none" : Object.entries(facts).map(([k, v]) => `${v} ${k}`).join(" · ")}{Number(r.evidence_added ?? 0) > 0 ? ` · ${String(r.evidence_added)} corroborations` : ""}</dd>
        <dt className="text-slate-500">Reverted</dt><dd>{Number(r.reverts ?? 0) === 0 ? "nothing: the model changed no recorded fact" : `${String(r.reverts)} change${Number(r.reverts) === 1 ? "" : "s"} the model made to recorded facts were undone`}</dd>
        <dt className="text-slate-500">Model calls</dt><dd>{String(r.chat_calls ?? 0)} chat · {String(r.embedding_calls ?? 0)} embeddings · {String(r.prompt_tokens ?? 0)} + {String(r.completion_tokens ?? 0)} tokens</dd>
      </dl>
      {list("entity_ids").length > 0 ? <p className="text-sm">New entities: {list("entity_ids").map((id) => <Link key={id} className="mr-2 font-mono text-xs underline" to={`/graph?entity=${id}`}>{id.slice(0, 8)}…</Link>)}</p> : null}
      {list("review_ids").length > 0 ? <p className="text-sm">Review items: {list("review_ids").map((id) => <Link key={id} className="mr-2 font-mono text-xs underline" to={`/reviews/${id}`}>{id.slice(0, 8)}…</Link>)}</p> : null}
    </section>
  );
}

/** What the memory stage did (P5): the memories it made by status, why candidates were dropped, what it cost. Ids, counts and codes only. */
export function MemoryProposeSummary({ stage }: { stage: StageRow }) {
  const r = (stage.result ?? {}) as Record<string, unknown>;
  if (stage.stage !== "memory_propose" || r.prompt_version === undefined) return null;
  const statuses = (r.statuses ?? {}) as Record<string, number>;
  const drops = (r.drops ?? {}) as Record<string, number>;
  const ids = (r.memory_ids as string[] | undefined) ?? [];
  const reviews = (r.review_ids as string[] | undefined) ?? [];
  return (
    <section aria-labelledby="mp-h" className="space-y-2" data-testid="memory-propose-summary">
      <h2 id="mp-h" className="font-medium">Memories</h2>
      <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Candidates</dt><dd>{String(r.candidates ?? 0)} proposed by the model ({String(r.prompt_version)}) · {String(r.kept ?? 0)} kept</dd>
        <dt className="text-slate-500">Made</dt><dd>{Object.keys(statuses).length === 0 ? "none" : Object.entries(statuses).map(([k, v]) => `${v} ${k}`).join(" · ")}</dd>
        <dt className="text-slate-500">Dropped</dt><dd>{Object.keys(drops).length === 0 ? "nothing" : Object.entries(drops).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(" · ")}</dd>
        <dt className="text-slate-500">Model calls</dt><dd>{String(r.chat_calls ?? 0)} chat · {String(r.prompt_tokens ?? 0)} + {String(r.completion_tokens ?? 0)} tokens</dd>
      </dl>
      {ids.length > 0 ? <p className="text-sm">Memories: {ids.map((id) => <Link key={id} className="mr-2 font-mono text-xs underline" to={`/memories/${id}`}>{id.slice(0, 8)}…</Link>)}</p> : null}
      {reviews.length > 0 ? <p className="text-sm">Review items: {reviews.map((id) => <Link key={id} className="mr-2 font-mono text-xs underline" to={`/reviews/${id}`}>{id.slice(0, 8)}…</Link>)}</p> : null}
    </section>
  );
}

/** The decrypted original, rendered as TEXT and nothing else: content is data, so it is never interpreted as markup. */
export function ContentView({ content }: { content: Content }) {
  const text = typeof content.content === "string" ? content.content : JSON.stringify(content.content, null, 2);
  return (
    <div data-testid="revealed-content" className="space-y-2">
      <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-slate-200 bg-slate-50 p-3 font-mono text-xs dark:border-slate-800 dark:bg-slate-900">{text}</pre>
      {Object.keys(content.metadata ?? {}).length > 0 ? <p className="text-xs text-slate-500">metadata: {Object.keys(content.metadata ?? {}).join(", ")}</p> : null}
    </div>
  );
}

function RevealContent({ eventId }: { eventId: string }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [content, setContent] = useState<Content | null>(null);
  const [form, setForm] = useState<"raw" | "normalized">("raw");

  async function load(f: "raw" | "normalized"): Promise<string | null> {
    const res = await api.GET("/v1/interactions/{event_id}/content", { params: { path: { event_id: eventId }, query: { form: f } } });
    if (res.response.status === 404) return f === "normalized" ? "The normalised form does not exist yet." : "This interaction no longer exists.";
    if (!res.data) return res.response.status === 403 ? "Your role may not read content." : "Could not read the content.";
    setContent(res.data);
    setForm(f);
    await qc.invalidateQueries({ queryKey: ["audit-recent"] });
    return null;
  }

  return (
    <section aria-labelledby="content-h" className="space-y-2">
      <h2 id="content-h" className="font-medium">Content</h2>
      {content ? (
        <>
          <div className="flex items-center gap-3 text-sm">
            <Badge tone="amber">{form}</Badge>
            <button type="button" className={btn.link} onClick={() => void load(form === "raw" ? "normalized" : "raw")}>Show {form === "raw" ? "normalised" : "raw"} form</button>
            <button type="button" className={btn.link} onClick={() => setContent(null)}>Hide</button>
          </div>
          <ContentView content={content} />
        </>
      ) : (
        <>
          <p className="text-sm text-slate-500">Stored encrypted. Reading it is recorded in the audit log.</p>
          <button type="button" className={btn.secondary} onClick={() => setConfirming(true)}>Reveal content…</button>
        </>
      )}
      {confirming ? (
        <ConfirmDialog title="Reveal content" confirmLabel="Reveal and audit" onClose={() => setConfirming(false)} onConfirm={() => load("raw")}>
          <p className="text-sm">You are about to read the original of this event. The read is recorded in the audit log with your name, and the content is shown on this page only.</p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

function ReplayButton({ eventId, stages }: { eventId: string; stages: Array<{ stage: string; repin: boolean }> }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [stage, setStage] = useState("");
  const [done, setDone] = useState<string | null>(null);
  return (
    <>
      <button type="button" className={btn.secondary} onClick={() => { setDone(null); setOpen(true); }}>Replay…</button>
      {done ? <span role="status" className="ml-3 text-sm text-emerald-700">{done}</span> : null}
      {open ? (
        <ReasonDialog title="Replay" confirmLabel="Replay" onClose={() => setOpen(false)}
          onConfirm={async (reason) => {
            const res = await api.POST("/v1/interactions/{event_id}/replay", { params: { path: { event_id: eventId }, header: { "Idempotency-Key": newKey() } }, body: { reason_code: reason, stage: stage || null, repin: stages.find((x) => x.stage === stage)?.repin ?? false, upgrade_pipeline: false } });
            if (res.response.status !== 202) return (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "replay_failed";
            setDone("Replay queued: a new generation of the stage will run.");
            await qc.invalidateQueries({ queryKey: ["interaction", eventId] });
            await qc.invalidateQueries({ queryKey: ["interactions"] });
            return null;
          }}>
          <p className="text-sm">A replay runs the stage again as a new generation. Results are written once, so nothing is duplicated.</p>
          <Field label="Stage" hint="Leave on “all dead or failed stages” unless one stage needs a rerun.">
            <select aria-label="Stage" className={`${inputCls} w-full`} value={stage} onChange={(e) => setStage(e.target.value)}>
              <option value="">All dead or failed stages</option>
              {stages.map((s) => <option key={s.stage} value={s.stage}>{s.stage}{s.repin ? " (move to the current ontology)" : ""}</option>)}
            </select>
          </Field>
        </ReasonDialog>
      ) : null}
    </>
  );
}

export function InteractionDetailPage() {
  const { eventId = "" } = useParams();
  const can = useCan();
  const q = useQuery({
    queryKey: ["interaction", eventId],
    refetchInterval: (query) => (["accepted", "processing"].includes(query.state.data?.data?.processing_status ?? "") ? 3000 : false),
    queryFn: () => api.GET("/v1/interactions/{event_id}", { params: { path: { event_id: eventId } } }),
  });
  if (q.isLoading) return <Loading label="Loading interaction…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const res = q.data!;
  const blocked = stateForStatus(res.response.status, "This interaction", () => void q.refetch());
  if (blocked || !res.data) return <div className="space-y-4"><Link to="/interactions" className={btn.link}>← Interactions</Link>{blocked}</div>;
  const i = res.data;
  const replayable = replayableStages(i.stages);
  return (
    <div className="space-y-6">
      <Link to="/interactions" className={btn.link}>← Interactions</Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Interaction</h1>
        <ProcessingBadge status={i.processing_status} />
        <span className="font-mono text-xs text-slate-500">{i.id}</span>
      </div>
      <section aria-labelledby="env-h" className="space-y-2">
        <h2 id="env-h" className="font-medium">Envelope</h2>
        <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
          <dt className="text-slate-500">Kind</dt><dd className="font-mono">{i.kind}{i.content_type ? ` · ${i.content_type}` : ""}</dd>
          <dt className="text-slate-500">Source</dt><dd>{i.source_system} · {i.source_ref}</dd>
          <dt className="text-slate-500">Occurred</dt><dd>{i.occurred_at_unknown ? "unknown" : fmt(i.occurred_at)}</dd>
          <dt className="text-slate-500">Recorded</dt><dd>{fmt(i.recorded_at)}</dd>
          <dt className="text-slate-500">Submitted by</dt><dd>{i.submitted_by_handle ?? "—"}</dd>
          <dt className="text-slate-500">Classification</dt><dd>{i.classification}{i.consent_external ? "" : " · external processing denied"}</dd>
          <dt className="text-slate-500">Size</dt><dd>{i.byte_size} bytes{i.token_estimate != null ? ` · ~${i.token_estimate} tokens` : ""}</dd>
        </dl>
      </section>
      <section aria-labelledby="pin-h" className="space-y-2">
        <h2 id="pin-h" className="font-medium">Pinned versions</h2>
        <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
          <dt className="text-slate-500">Pipeline</dt><dd className="font-mono">{i.pipeline_version}</dd>
          <dt className="text-slate-500">Ontology</dt>
          <dd>{i.ontology_version_id ? <Link to={`/ontology/versions/${i.ontology_version_id}`} className="font-mono text-xs underline">{i.ontology_version_id.slice(0, 8)}…</Link> : <span className="text-slate-500">none was active</span>}</dd>
        </dl>
      </section>
      <section aria-labelledby="stages-h" className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 id="stages-h" className="font-medium">Stages</h2>
          {can.repair && replayable.length > 0 ? <ReplayButton eventId={i.id} stages={replayable} /> : null}
        </div>
        <StageTimeline stages={i.stages} />
        {i.stages.map((s) => <GraphIngestSummary key={s.stage} stage={s} />)}
        {i.stages.map((s) => <MemoryProposeSummary key={s.stage} stage={s} />)}
        {i.processing_status === "failed" ? <Banner tone="red" role="status">Normalisation failed, so nothing was derived from this event. Fix the cause and replay it.</Banner> : null}
        {i.processing_status === "partial" ? <Banner tone="amber" role="status">Some derivative stages did not complete. The source is safe; replay the dead stages when ready.</Banner> : null}
      </section>
      <section aria-labelledby="subj-h" className="space-y-2">
        <h2 id="subj-h" className="font-medium">Subjects</h2>
        {i.subjects.length === 0 ? <p className="text-sm text-slate-500">No subjects: a source-only event, still valid evidence.</p> : (
          <ul className="space-y-1 text-sm">
            {i.subjects.map((s, n) => (
              <li key={n} className="flex items-center gap-2">
                <span className="w-14 text-xs text-slate-500">{s.role}</span>
                {s.resolved && s.entity_id ? <Link to={`/entities/${s.entity_id}`} className="underline">open entity</Link> : <Badge tone="amber" title="Kept for identity resolution; the value is not shown">unresolved</Badge>}
                {!s.resolved && s.ref_type ? <span className="text-xs text-slate-500">{s.ref_type} · {s.ref_system}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      {can.readContent ? <RevealContent eventId={i.id} /> : null}
    </div>
  );
}
