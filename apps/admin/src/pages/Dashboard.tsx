import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Empty, ErrorState, Loading, stateForStatus } from "../components/states";

function Status({ label, value }: { label: string; value: string }) {
  const ok = value === "ok";
  return (
    <div className="flex items-center justify-between rounded-md border border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
      <span className="text-sm text-slate-600 dark:text-slate-300">{label}</span>
      <span className={`flex items-center gap-1 text-sm font-medium ${ok ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}`}>
        {ok ? <CheckCircle2 className="h-4 w-4" aria-hidden /> : <XCircle className="h-4 w-4" aria-hidden />}
        {label.toLowerCase()} {value}
      </span>
    </div>
  );
}

function Health() {
  const q = useQuery({ queryKey: ["ops-health"], refetchInterval: 15_000, queryFn: () => api.GET("/v1/operations/health") });
  if (q.isLoading) return <Loading label="Checking dependencies…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const res = q.data!;
  const blocked = stateForStatus(res.response.status, "dependency health", () => void q.refetch());
  if (blocked || !res.data) return <>{blocked}</>;
  const h = res.data;
  const counts = Object.entries(h.principal_counts);
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Status label="Postgres" value={h.postgres} />
        <Status label="Neo4j" value={h.neo4j} />
        <Status label={`Router (${h.router.mode})`} value={h.router.reachability} />
        <Status label="Worker" value={h.worker.status === "ok" ? "ok" : `${h.worker.status}${h.worker.last_beat_age_seconds != null ? ` (${Math.round(h.worker.last_beat_age_seconds)}s)` : ""}`} />
      </div>
      {h.outbox ? (
        <p data-testid="outbox-summary" className="text-sm text-slate-600 dark:text-slate-300">
          Outbox: <strong>{h.outbox.backlog}</strong> in flight, <strong className={h.outbox.dead > 0 ? "text-red-700" : ""}>{h.outbox.dead}</strong> dead letter{h.outbox.dead === 1 ? "" : "s"}.{" "}
          <Link to="/operations" className="underline">Open Operations</Link>
        </p>
      ) : null}
      {h.router.missing_aliases.length > 0 ? <p className="text-sm text-amber-700">Router is missing aliases: {h.router.missing_aliases.join(", ")}</p> : null}
      <div>
        <h3 className="mb-2 text-sm font-medium text-slate-500">Active principals</h3>
        {counts.length === 0 ? <Empty title="No principals" /> : (
          <ul className="flex flex-wrap gap-2">
            {counts.map(([kind, n]) => (
              <li key={kind} className="rounded-full border border-slate-200 px-3 py-1 text-sm dark:border-slate-700">{kind}: <strong>{n}</strong></li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function RecentAudit() {
  const q = useQuery({ queryKey: ["audit-recent"], queryFn: () => api.GET("/v1/audit", { params: { query: { limit: 20 } } }) });
  if (q.isLoading) return <Loading label="Loading audit events…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const res = q.data!;
  const blocked = stateForStatus(res.response.status, "the audit log", () => void q.refetch());
  if (blocked || !res.data) return <>{blocked}</>;
  if (res.data.items.length === 0) return <Empty title="No audit events yet" />;
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-slate-500"><tr><th className="py-1 pr-4">When</th><th className="pr-4">Action</th><th className="pr-4">Outcome</th><th>Resource</th></tr></thead>
      <tbody>
        {res.data.items.map((e) => (
          <tr key={e.id} className="border-t border-slate-200 dark:border-slate-800">
            <td className="py-1 pr-4 whitespace-nowrap">{new Date(e.created_at).toLocaleString()}</td>
            <td className="pr-4 font-mono">{e.action}</td>
            <td className={`pr-4 ${e.outcome === "denied" ? "text-red-600" : ""}`}>{e.outcome}</td>
            <td className="font-mono text-xs text-slate-500">{e.resource_type ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Dashboard() {
  return (
    <div className="space-y-8">
      <h1 className="text-xl font-semibold">Dashboard</h1>
      <section aria-labelledby="health-h"><h2 id="health-h" className="mb-3 font-medium">Health</h2><Health /></section>
      <section aria-labelledby="audit-h"><h2 id="audit-h" className="mb-3 font-medium">Recent audit events</h2><RecentAudit /></section>
    </div>
  );
}
