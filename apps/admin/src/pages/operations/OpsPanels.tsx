import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Empty, ErrorState, Loading, stateForStatus } from "../../components/states";
import { Badge, Banner, btn } from "../../components/ui";
import { ageLabel, fmt, newKey } from "../../lib/format";

const usd = (v: number) => (v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`);
const secs = (v: number) => (v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(2)} s`);
const hours = (s: number | null | undefined) => (s == null ? "never" : s < 3600 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} d`);

function Panel<T>({ q, what, children }: { q: ReturnType<typeof useQuery<{ data?: T; response: Response }>>; what: string; children: (d: T) => JSX.Element }) {
  if (q.isLoading) return <Loading label={`Loading ${what}…`} />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, what, () => void q.refetch());
  if (blocked || !q.data!.data) return <>{blocked}</>;
  return children(q.data!.data);
}

const Kpi = ({ k, v, id, bad }: { k: string; v: string; id: string; bad?: boolean }) => (
  <div className="rounded-md border border-slate-200 px-4 py-3 dark:border-slate-800">
    <dt className="text-xs text-slate-500">{k}</dt>
    <dd data-testid={`kpi-${id}`} className={`text-lg font-semibold ${bad ? "text-red-700" : ""}`}>{v}</dd>
  </div>
);

export function Alerts() {
  const q = useQuery({ queryKey: ["ops-alerts"], refetchInterval: 30_000, queryFn: () => api.GET("/v1/operations/alerts") });
  return (
    <Panel q={q} what="alerts">
      {(a) =>
        a.firing.length === 0 ? (
          <Banner tone="green" role="status">Nothing is firing. Checked: {a.checked.join(", ")}.{a.alertmanager === "not_configured" ? " (No Alertmanager is configured; these are the platform's own checks.)" : ""}</Banner>
        ) : (
          <ul className="space-y-2" data-testid="alerts-firing">
            {a.firing.map((f, i) => (
              <li key={`${f.name}-${i}`} className="flex flex-wrap items-center gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
                <Badge tone={f.severity === "critical" ? "red" : f.severity === "warning" ? "amber" : "slate"}>{f.severity}</Badge>
                <span className="font-medium">{f.name}</span>
                <span className="text-slate-600 dark:text-slate-300">{f.summary}</span>
                <span className="ml-auto text-xs text-slate-500">{f.source}</span>
              </li>
            ))}
            {a.alertmanager === "unreachable" ? <li className="text-xs text-amber-700">Alertmanager is configured but did not answer.</li> : null}
          </ul>
        )
      }
    </Panel>
  );
}

export function Cost() {
  const [days, setDays] = useState(14);
  const q = useQuery({ queryKey: ["ops-cost", days], queryFn: () => api.GET("/v1/operations/cost", { params: { query: { days } } }) });
  return (
    <div className="space-y-3">
      <label className="text-sm">Window{" "}
        <select aria-label="Cost window" className="ml-1 rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[1, 7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} day{d > 1 ? "s" : ""}</option>)}
        </select>
      </label>
      <Panel q={q} what="cost">
        {(c) =>
          c.total_calls === 0 ? <Empty title="No model calls in this window" hint="Spend appears here once events are extracted or contexts are summarised." /> : (
            <div className="space-y-4">
              <dl className="grid gap-3 sm:grid-cols-3">
                <Kpi k="Estimated spend" v={usd(c.total_usd)} id="cost-total" />
                <Kpi k="Model calls" v={String(c.total_calls)} id="cost-calls" />
                <Kpi k="Errors" v={String(c.rows.reduce((n, r) => n + r.errors, 0))} id="cost-errors" bad={c.rows.some((r) => r.errors > 0)} />
              </dl>
              <div className="grid gap-6 lg:grid-cols-2">
                <table className="w-full text-left text-sm"><caption className="sr-only">Spend by purpose</caption>
                  <thead className="text-slate-500"><tr><th className="py-1 pr-4">Purpose</th><th>Spend</th></tr></thead>
                  <tbody>{Object.entries(c.by_purpose).sort((a, b) => b[1] - a[1]).map(([k, v]) => <tr key={k} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 font-mono text-xs">{k}</td><td>{usd(v)}</td></tr>)}</tbody>
                </table>
                <table className="w-full text-left text-sm"><caption className="sr-only">Spend by alias</caption>
                  <thead className="text-slate-500"><tr><th className="py-1 pr-4">Alias</th><th>Spend</th></tr></thead>
                  <tbody>{Object.entries(c.by_alias).sort((a, b) => b[1] - a[1]).map(([k, v]) => <tr key={k} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 font-mono text-xs">{k}</td><td>{usd(v)}</td></tr>)}</tbody>
                </table>
              </div>
              {c.top_consumers.length > 0 ? (
                <table className="w-full text-left text-sm"><caption className="mb-1 text-left text-slate-500">Top consumers: principals by context builds</caption>
                  <thead className="text-slate-500"><tr><th className="py-1 pr-4">Principal</th><th className="pr-4">Kind</th><th className="pr-4">Builds</th><th>Tokens</th></tr></thead>
                  <tbody>{c.top_consumers.map((p) => <tr key={p.principal_id} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4">{p.handle}</td><td className="pr-4">{p.kind}</td><td className="pr-4">{p.context_builds}</td><td>{p.tokens_used}</td></tr>)}</tbody>
                </table>
              ) : null}
              <p className="text-xs text-slate-500">{c.note}</p>
            </div>
          )
        }
      </Panel>
    </div>
  );
}

export function Latency() {
  const q = useQuery({ queryKey: ["ops-latency"], refetchInterval: 30_000, queryFn: () => api.GET("/v1/operations/latency", { params: { query: { days: 1 } } }) });
  return (
    <Panel q={q} what="latency">
      {(l) => {
        const block = (title: string, rows: Record<string, { n: number; p50: number; p95: number }>) => (
          <table className="w-full text-left text-sm"><caption className="mb-1 text-left text-slate-500">{title}</caption>
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">&nbsp;</th><th className="pr-4">n</th><th className="pr-4">p50</th><th>p95</th></tr></thead>
            <tbody>{Object.entries(rows).sort().map(([k, v]) => <tr key={k} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 font-mono text-xs">{k}</td><td className="pr-4">{v.n}</td><td className="pr-4">{secs(v.p50)}</td><td>{secs(v.p95)}</td></tr>)}</tbody>
          </table>
        );
        if (!Object.keys(l.context_by_profile).length && !Object.keys(l.router_by_alias).length) return <Empty title="No traffic in the last day" hint="Build a context or ingest an event to see latencies." />;
        return <div className="grid gap-6 lg:grid-cols-3" data-testid="latency">{block("Context builds by profile (24 h)", l.context_by_profile)}{block("Context sources", l.context_by_source)}{block("Router by alias", l.router_by_alias)}</div>;
      }}
    </Panel>
  );
}

export function Freshness() {
  const q = useQuery({ queryKey: ["ops-freshness"], refetchInterval: 30_000, queryFn: () => api.GET("/v1/operations/freshness", { params: { query: { hours: 24 } } }) });
  return (
    <Panel q={q} what="freshness">
      {(f) => (
        <div className="space-y-3">
          <dl className="grid gap-3 sm:grid-cols-4">
            <Kpi k="Accepted → indexed p50" v={f.accepted_to_indexed.n ? secs(f.accepted_to_indexed.p50) : "—"} id="fresh-p50" />
            <Kpi k="Accepted → indexed p95" v={f.accepted_to_indexed.n ? secs(f.accepted_to_indexed.p95) : "—"} id="fresh-p95" />
            <Kpi k="Not yet settled" v={String(Object.values(f.unsettled).reduce((a, b) => a + b, 0))} id="fresh-unsettled" />
            <Kpi k="Oldest unsettled" v={ageLabel(f.oldest_unsettled_age_seconds)} id="fresh-oldest" />
          </dl>
          {f.by_stage.length > 0 ? (
            <table className="w-full text-left text-sm"><caption className="mb-1 text-left text-slate-500">Lag per stage (queue wait + run, 24 h)</caption>
              <thead className="text-slate-500"><tr><th className="py-1 pr-4">Stage</th><th className="pr-4">Jobs</th><th className="pr-4">p50</th><th>p95</th></tr></thead>
              <tbody>{f.by_stage.map((s) => <tr key={s.stage} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 font-mono text-xs">{s.stage}</td><td className="pr-4">{s.n}</td><td className="pr-4">{secs(s.p50)}</td><td>{secs(s.p95)}</td></tr>)}</tbody>
            </table>
          ) : null}
          <p className="text-xs text-slate-500">Newest event {fmt(f.watermarks.newest_event as string | null)} · newest indexed {fmt(f.watermarks.newest_indexed as string | null)} · last job {fmt(f.watermarks.last_job as string | null)}</p>
        </div>
      )}
    </Panel>
  );
}

export function Reconciliation() {
  const can = useCan();
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["ops-reconcile"], queryFn: () => api.GET("/v1/operations/reconcile") });

  async function run(repair: boolean): Promise<string | null> {
    const r = await api.POST("/v1/operations/reconcile", { params: { header: { "Idempotency-Key": newKey() } }, body: { scope: "all", repair, allow_reingest: false } });
    if (!r.data) return (r.error as { error?: { code?: string } } | undefined)?.error?.code ?? "reconcile_failed";
    const d = r.data as { status?: string; drift?: Record<string, number>; repaired?: Record<string, number> };
    const left = Object.values(d.drift ?? {}).reduce((a, b) => a + b, 0);
    const fixed = Object.values(d.repaired ?? {}).reduce((a, b) => a + b, 0);
    setOutcome(repair ? `Repair ${d.status}: ${fixed} repaired, ${left} left for a person.` : `Scan ${d.status}: ${left} drifted item(s).`);
    await qc.invalidateQueries({ queryKey: ["ops-reconcile"] });
    await qc.invalidateQueries({ queryKey: ["ops-alerts"] });
    return null;
  }

  return (
    <Panel q={q} what="reconciliation">
      {(r) => (
        <div className="space-y-3">
          {outcome ? <Banner tone="green" role="status">{outcome}</Banner> : null}
          <div className="flex gap-2">
            <button className={btn.secondary} onClick={() => void run(false)}>Scan now</button>
            {can.repair ? <button className={btn.primary} onClick={() => setConfirming(true)}>Repair safe classes…</button> : null}
          </div>
          {r.runs.length === 0 ? <Empty title="No scan yet" hint="The nightly scan records its result here; run one now to see the current state." /> : (
            <table className="w-full text-left text-sm"><caption className="sr-only">Recent reconciliation runs</caption>
              <thead className="text-slate-500"><tr><th className="py-1 pr-4">Started</th><th className="pr-4">Mode</th><th className="pr-4">Status</th><th>Drift by class</th></tr></thead>
              <tbody>{r.runs.map((x) => (
                <tr key={x.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="py-1 pr-4 whitespace-nowrap text-xs">{fmt(x.started_at as string)}</td><td className="pr-4">{x.mode} · {x.trigger}</td><td className="pr-4">{x.status}</td>
                  <td className="font-mono text-xs">{Object.keys(x.drift).length ? Object.entries(x.drift).map(([k, v]) => `${k}: ${v}`).join(", ") : "clean"}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
          {confirming ? (
            <ConfirmDialog title="Repair safe drift classes" confirmLabel="Repair" onClose={() => setConfirming(false)} onConfirm={() => run(true)}>
              <p className="text-sm">Repairs only the classes that need no model call and no judgement ({r.classes.filter((c) => !c.spends_money && c.repair !== "none").map((c) => c.name).join(", ") || "none"}). Classes that need a person, or that would spend model calls, are listed and left alone. The repair is audited.</p>
            </ConfirmDialog>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

export function Backups() {
  const q = useQuery({ queryKey: ["ops-backups"], refetchInterval: 60_000, queryFn: () => api.GET("/v1/operations/backups") });
  return (
    <Panel q={q} what="backups">
      {(b) => (
        <div className="space-y-3">
          <dl className="grid gap-3 sm:grid-cols-4">
            {b.stores.map((s) => <Kpi key={s.store} k={`${s.store}: last backup`} v={s.age_seconds == null ? "never" : `${hours(s.age_seconds)} ago`} id={`backup-${s.store}`} bad={s.stale} />)}
          </dl>
          <p className="text-sm" data-testid="rehearsal">
            Last restore rehearsal: {b.last_rehearsal.at ? <>{fmt(b.last_rehearsal.at as string)} ({b.last_rehearsal.status}); RTO {b.last_rehearsal.rto_seconds != null ? hours(b.last_rehearsal.rto_seconds) : "—"}, RPO {b.last_rehearsal.rpo_seconds != null ? hours(b.last_rehearsal.rpo_seconds) : "—"}</> : <span className="text-amber-700">none recorded</span>}
          </p>
          {b.recent.length > 0 ? (
            <table className="w-full text-left text-sm"><caption className="sr-only">Recent backup runs</caption>
              <thead className="text-slate-500"><tr><th className="py-1 pr-4">Started</th><th className="pr-4">Store</th><th className="pr-4">Status</th><th>Size</th></tr></thead>
              <tbody>{b.recent.map((r) => <tr key={r.id} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 whitespace-nowrap text-xs">{fmt(r.started_at as string)}</td><td className="pr-4">{r.store}</td><td className="pr-4">{r.status}{r.error_code ? ` (${r.error_code})` : ""}</td><td>{r.size_bytes != null ? `${(r.size_bytes / 1048576).toFixed(1)} MiB` : "—"}</td></tr>)}</tbody>
            </table>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

export function LiveEval() {
  const q = useQuery({ queryKey: ["ops-live-eval"], queryFn: () => api.GET("/v1/operations/live-eval") });
  return (
    <Panel q={q} what="evaluation records">
      {(e) => (
        <div className="space-y-3">
          <p className="text-sm" data-testid="live-session">Last live session: {e.last_live_session ? <>{fmt(e.last_live_session.started_at as string)} · <Badge tone={e.last_live_session.status === "passed" ? "green" : "red"}>{e.last_live_session.status}</Badge></> : <span className="text-amber-700">not run yet (a release candidate needs one)</span>}</p>
          {e.latest_by_kind.length === 0 ? <Empty title="No evaluation records" hint="`make eval-gate`, the release sweep and the live session record themselves here." /> : (
            <table className="w-full text-left text-sm"><caption className="sr-only">Latest evaluation record by kind</caption>
              <thead className="text-slate-500"><tr><th className="py-1 pr-4">Kind</th><th className="pr-4">Status</th><th className="pr-4">When</th><th>Commit</th></tr></thead>
              <tbody>{e.latest_by_kind.map((r) => <tr key={r.id} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 font-mono text-xs">{r.kind}</td><td className="pr-4"><Badge tone={r.status === "passed" ? "green" : r.status === "partial" ? "amber" : "red"}>{r.status}</Badge></td><td className="pr-4 whitespace-nowrap text-xs">{fmt((r.finished_at ?? r.started_at) as string)}</td><td className="font-mono text-xs">{r.git_commit?.slice(0, 8) ?? "—"}</td></tr>)}</tbody>
            </table>
          )}
        </div>
      )}
    </Panel>
  );
}
