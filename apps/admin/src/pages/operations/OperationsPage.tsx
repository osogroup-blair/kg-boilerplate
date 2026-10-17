import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { inputCls, JobBadge } from "../../components/ui";
import { ageLabel, fmt } from "../../lib/format";

function Status({ label, value }: { label: string; value: string }) {
  const ok = value === "ok";
  return (
    <div className="flex items-center justify-between rounded-md border border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
      <span className="text-sm text-slate-600 dark:text-slate-300">{label}</span>
      <span className={`flex items-center gap-1 text-sm font-medium ${ok ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}`}>
        {ok ? <CheckCircle2 className="h-4 w-4" aria-hidden /> : <XCircle className="h-4 w-4" aria-hidden />}
        {value}
      </span>
    </div>
  );
}

function Health() {
  const q = useQuery({ queryKey: ["ops-health"], refetchInterval: 15_000, queryFn: () => api.GET("/v1/operations/health") });
  if (q.isLoading) return <Loading label="Checking dependencies…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "operations health", () => void q.refetch());
  const h = q.data!.data;
  if (blocked || !h) return <>{blocked}</>;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Status label="Postgres" value={h.postgres} />
      <Status label="Neo4j" value={h.neo4j} />
      <Status label={`Router (${h.router.mode})`} value={h.router.reachability} />
      <div data-testid="worker-heartbeats"><Status label="Workers" value={h.worker.status === "ok" ? `${h.worker.count} running, last beat ${Math.round(h.worker.last_beat_age_seconds ?? 0)}s ago` : h.worker.status} /></div>
    </div>
  );
}

function Backlog() {
  const q = useQuery({ queryKey: ["ops-backlog"], refetchInterval: 10_000, queryFn: () => api.GET("/v1/operations/backlog") });
  if (q.isLoading) return <Loading label="Loading the backlog…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "the backlog", () => void q.refetch());
  const b = q.data!.data;
  if (blocked || !b) return <>{blocked}</>;
  const stages = [...new Set(b.cells.map((c) => c.stage))].sort();
  const cell = (stage: string, status: string) => b.cells.find((c) => c.stage === stage && c.status === status);
  return (
    <div className="space-y-3">
      <dl className="grid gap-3 sm:grid-cols-4">
        {[
          ["Oldest pending", ageLabel(b.oldest_pending_age_seconds)],
          ["Dead letters", String(b.dead)],
          ["Lease reclaims (1 h)", String(b.lease_reclaims_last_hour)],
          ["Lease reclaims (24 h)", String(b.lease_reclaims_last_day)],
        ].map(([k, v]) => (
          <div key={k} className="rounded-md border border-slate-200 px-4 py-3 dark:border-slate-800">
            <dt className="text-xs text-slate-500">{k}</dt>
            <dd data-testid={`kpi-${k!.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`} className={`text-lg font-semibold ${k === "Dead letters" && b.dead > 0 ? "text-red-700" : ""}`}>{v}</dd>
          </div>
        ))}
      </dl>
      {stages.length === 0 ? <Empty title="The outbox is empty" hint="Jobs appear here while events and commands are being processed." /> : (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Jobs by stage and status</caption>
          <thead className="text-slate-500"><tr><th className="py-2 pr-4">Stage</th><th className="pr-4">Pending</th><th className="pr-4">Leased</th><th>Dead</th></tr></thead>
          <tbody>
            {stages.map((s) => (
              <tr key={s} className="border-t border-slate-200 dark:border-slate-800">
                <td className="py-2 pr-4 font-mono text-xs">{s}</td>
                {["pending", "leased", "dead"].map((st) => <td key={st} className={`pr-4 ${st === "dead" && cell(s, st) ? "font-medium text-red-700" : ""}`}>{cell(s, st)?.jobs ?? 0}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

type JobStatus = "pending" | "leased" | "succeeded" | "skipped" | "dead" | "cancelled";

function Jobs() {
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") ?? "dead") as JobStatus;
  const q = useQuery({
    queryKey: ["ops-jobs", status],
    refetchInterval: 10_000,
    queryFn: () => api.GET("/v1/operations/jobs", { params: { query: { status, limit: 50 } } }),
  });
  let body;
  if (q.isLoading) body = <Loading label="Loading jobs…" />;
  else if (q.isError) body = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const blocked = stateForStatus(q.data!.response.status, "jobs", () => void q.refetch());
    const items = q.data!.data?.items ?? [];
    if (blocked) body = blocked;
    else if (items.length === 0) body = <Empty title={status === "dead" ? "No dead letters" : "No jobs"} hint={status === "dead" ? "Every job either succeeded or is still being retried." : undefined} />;
    else
      body = (
        <table className="w-full text-left text-sm">
          <thead className="text-slate-500"><tr><th className="py-2 pr-4">Job</th><th className="pr-4">Subject</th><th className="pr-4">Status</th><th className="pr-4">Attempts</th><th className="pr-4">Last error</th><th>When</th></tr></thead>
          <tbody>
            {items.map((j) => (
              <tr key={j.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                <td className="py-2 pr-4"><Link to={`/operations/jobs/${j.id}`} className="font-mono text-xs underline-offset-2 hover:underline">{j.stage}{j.generation > 0 ? ` g${j.generation}` : ""}</Link></td>
                <td className="pr-4 text-xs">
                  {j.subject_type === "interaction" ? <Link to={`/interactions/${j.subject_id}`} className="underline">event {j.subject_id.slice(0, 8)}…</Link> : j.subject_type === "memory" ? <span className="font-mono text-xs">memory {j.subject_id.slice(0, 8)}…</span> : <Link to={`/commands/${j.subject_id}`} className="underline">command {j.subject_id.slice(0, 8)}…</Link>}
                </td>
                <td className="pr-4"><JobBadge status={j.status} /></td>
                <td className="pr-4">{j.attempts}/{j.max_attempts}</td>
                <td className="pr-4 font-mono text-xs text-red-700">{j.last_error_code ?? "—"}</td>
                <td className="whitespace-nowrap text-xs">{fmt(j.finished_at ?? j.available_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      );
  }
  return (
    <div className="space-y-3">
      <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Show</span>
        <select aria-label="Job status" className={inputCls} value={status} onChange={(e) => setParams({ status: e.target.value }, { replace: true })}>
          {["dead", "pending", "leased", "succeeded", "skipped", "cancelled"].map((s) => <option key={s} value={s}>{s === "dead" ? "dead letters" : s}</option>)}
        </select>
      </label>
      {body}
    </div>
  );
}

export function OperationsPage() {
  const can = useCan();
  if (!can.readOperations) return <div className="space-y-4"><h1 className="text-xl font-semibold">Operations</h1><PermissionDenied what="operations" /></div>;
  return (
    <div className="space-y-8">
      <h1 className="text-xl font-semibold">Operations</h1>
      <section aria-labelledby="h-h"><h2 id="h-h" className="mb-3 font-medium">Health and workers</h2><Health /></section>
      <section aria-labelledby="b-h"><h2 id="b-h" className="mb-3 font-medium">Backlog</h2><Backlog /></section>
      <section aria-labelledby="j-h"><h2 id="j-h" className="mb-3 font-medium">Jobs</h2><Jobs /></section>
    </div>
  );
}
