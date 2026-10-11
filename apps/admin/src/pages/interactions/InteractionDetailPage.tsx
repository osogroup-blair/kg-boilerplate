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

function ReplayButton({ eventId, stages }: { eventId: string; stages: string[] }) {
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
            const res = await api.POST("/v1/interactions/{event_id}/replay", { params: { path: { event_id: eventId }, header: { "Idempotency-Key": newKey() } }, body: { reason_code: reason, stage: stage || null } });
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
              {stages.map((s) => <option key={s} value={s}>{s}</option>)}
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
  const replayable = i.stages.filter((s) => REPLAYABLE.has(s.status)).map((s) => s.stage);
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
