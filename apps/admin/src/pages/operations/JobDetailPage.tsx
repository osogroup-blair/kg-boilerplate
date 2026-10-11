import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ErrorState, Loading, stateForStatus } from "../../components/states";
import { Banner, btn, JobBadge } from "../../components/ui";
import { fmt, newKey } from "../../lib/format";
import { ReasonDialog } from "../interactions/ReasonDialog";

type Action = "retry" | "cancel";

export function JobDetailPage() {
  const { jobId = "" } = useParams();
  const can = useCan();
  const qc = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [outcome, setOutcome] = useState<{ text: string; newJob?: string } | null>(null);
  const q = useQuery({ queryKey: ["ops-job", jobId], queryFn: () => api.GET("/v1/operations/jobs/{job_id}", { params: { path: { job_id: jobId } } }) });
  if (q.isLoading) return <Loading label="Loading job…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const res = q.data!;
  const blocked = stateForStatus(res.response.status, "This job", () => void q.refetch());
  if (blocked || !res.data) return <div className="space-y-4"><Link to="/operations" className={btn.link}>← Operations</Link>{blocked}</div>;
  const j = res.data;

  async function run(kind: Action, reason: string): Promise<string | null> {
    const params = { path: { job_id: jobId }, header: { "Idempotency-Key": newKey() } };
    const r = kind === "retry"
      ? await api.POST("/v1/operations/jobs/{job_id}/retry", { params, body: { reason_code: reason } })
      : await api.POST("/v1/operations/jobs/{job_id}/cancel", { params, body: { reason_code: reason } });
    if (!r.data) return (r.error as { error?: { code?: string } } | undefined)?.error?.code ?? `${kind}_failed`;
    const data = r.data as { new_job_id?: string | null };
    setOutcome(kind === "retry" ? { text: "Retry queued as a new generation.", newJob: data.new_job_id ?? undefined } : { text: "Job cancelled." });
    await qc.invalidateQueries({ queryKey: ["ops-job", jobId] });
    await qc.invalidateQueries({ queryKey: ["ops-jobs"] });
    await qc.invalidateQueries({ queryKey: ["ops-backlog"] });
    return null;
  }

  return (
    <div className="space-y-6">
      <Link to="/operations" className={btn.link}>← Operations</Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Job</h1>
        <JobBadge status={j.status} />
        <span className="font-mono text-xs text-slate-500">{j.id}</span>
      </div>
      <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Stage</dt><dd className="font-mono">{j.stage} · generation {j.generation}</dd>
        <dt className="text-slate-500">Subject</dt>
        <dd>{j.subject_type === "interaction" ? <Link className="underline" to={`/interactions/${j.subject_id}`}>event {j.subject_id.slice(0, 8)}…</Link> : <Link className="underline" to={`/commands/${j.subject_id}`}>command {j.subject_id.slice(0, 8)}…</Link>}</dd>
        <dt className="text-slate-500">Attempts</dt><dd>{j.attempts} of {j.max_attempts}</dd>
        <dt className="text-slate-500">Last error</dt><dd className="font-mono text-xs text-red-700">{j.last_error_category ? `${j.last_error_category}: ` : ""}{j.last_error_code ?? "—"}</dd>
        <dt className="text-slate-500">Pinned</dt><dd className="font-mono text-xs">{j.pipeline_version}{j.ontology_version_id ? ` · ontology ${j.ontology_version_id.slice(0, 8)}…` : ""}</dd>
        <dt className="text-slate-500">{j.finished_at ? "Finished" : "Next attempt"}</dt><dd>{fmt(j.finished_at ?? j.available_at)}</dd>
      </dl>
      {outcome ? <Banner tone="green" role="status">{outcome.text} {outcome.newJob ? <Link className="underline" to={`/operations/jobs/${outcome.newJob}`}>Open the new job</Link> : null}</Banner> : null}
      {can.repair ? (
        <div className="flex gap-2">
          {j.status === "dead" ? <button type="button" className={btn.primary} onClick={() => { setOutcome(null); setAction("retry"); }}>Retry…</button> : null}
          {j.status === "dead" || j.status === "pending" ? <button type="button" className={btn.danger} onClick={() => { setOutcome(null); setAction("cancel"); }}>Cancel…</button> : null}
        </div>
      ) : null}
      <section aria-labelledby="att-h" className="space-y-2">
        <h2 id="att-h" className="font-medium">Attempts</h2>
        <table className="w-full text-left text-sm">
          <thead className="text-slate-500"><tr><th className="py-1 pr-4">#</th><th className="pr-4">Worker</th><th className="pr-4">Started</th><th className="pr-4">Outcome</th><th className="pr-4">Error</th><th>Took</th></tr></thead>
          <tbody>
            {j.attempt_log.map((a) => (
              <tr key={a.attempt_no} className="border-t border-slate-200 dark:border-slate-800">
                <td className="py-1 pr-4">{a.attempt_no}</td>
                <td className="pr-4 font-mono text-xs">{a.worker_id}</td>
                <td className="whitespace-nowrap pr-4 text-xs">{fmt(a.started_at)}</td>
                <td className="pr-4">{a.outcome ?? <span className="text-slate-500">running</span>}</td>
                <td className="pr-4 font-mono text-xs text-red-700">{a.error_code ?? "—"}</td>
                <td className="text-xs">{a.duration_ms != null ? `${a.duration_ms} ms` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {action ? (
        <ReasonDialog title={action === "retry" ? "Retry job" : "Cancel job"} confirmLabel={action === "retry" ? "Retry" : "Cancel job"} danger={action === "cancel"} onClose={() => setAction(null)} onConfirm={(reason) => run(action, reason)}>
          <p className="text-sm">{action === "retry" ? "A dead job becomes a new job one generation higher; the dead one stays as history." : "The job will not run. Its stage is marked dead (cancelled by an operator) and the event is finalised as partial or failed."}</p>
        </ReasonDialog>
      ) : null}
    </div>
  );
}
