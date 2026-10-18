import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { Badge, Banner, btn } from "../../components/ui";
import { fmt, newKey } from "../../lib/format";
import { useActiveOntology } from "../../ontology/queries";

interface Run { id: string; namespace: string; mode: string; status: string; estimate: Record<string, number | string>; progress: Record<string, number | string>; report: Record<string, unknown> | null; error_code: string | null; created_at: string }
interface Ns { namespace: string; status: "building" | "active" | "retired" | "cleared"; reindex_run_id: string | null; activated_at: string | null; retired_at: string | null }
type Pending = { kind: "start"; mode: "project" | "reextract" } | { kind: "activate"; ns: Ns } | { kind: "rollback"; ns: Ns } | { kind: "cancel"; run: Run };

const TONE: Record<string, "green" | "amber" | "red" | "slate"> = { active: "green", ready: "green", activated: "green", building: "amber", catching_up: "amber", retired: "slate", failed: "red", cancelled: "slate", rolled_back: "slate" };
const errCode = (e: unknown, fallback: string) => (e as { error?: { code?: string } } | undefined)?.error?.code ?? fallback;

export function RebuildPage() {
  const can = useCan();
  const qc = useQueryClient();
  const active = useActiveOntology();
  const [pending, setPending] = useState<Pending | null>(null);
  const [preview, setPreview] = useState<Record<string, number | string> | null>(null);
  const [mode, setMode] = useState<"project" | "reextract">("project");
  const [outcome, setOutcome] = useState<string | null>(null);
  const runs = useQuery({ queryKey: ["reindex-runs"], refetchInterval: 5000, enabled: can.workspaceAdmin, queryFn: () => api.GET("/v1/reindex-runs") });
  const spaces = useQuery({ queryKey: ["graph-namespaces"], refetchInterval: 5000, enabled: can.workspaceAdmin, queryFn: () => api.GET("/v1/graph/namespaces") });
  if (!can.workspaceAdmin) return <div className="space-y-4"><h1 className="text-xl font-semibold">Graph rebuild</h1><PermissionDenied what="the graph rebuild" /></div>;
  const versionId = active.data?.data?.version?.id;

  async function refresh() {
    await qc.invalidateQueries({ queryKey: ["reindex-runs"] });
    await qc.invalidateQueries({ queryKey: ["graph-namespaces"] });
  }
  async function showPreview() {
    if (!versionId) return;
    const r = await api.POST("/v1/ontologies/versions/{version_id}/reindex", { params: { path: { version_id: versionId } }, body: { mode, confirm: false } });
    setPreview((r.data ?? null) as Record<string, number | string> | null);
  }
  async function confirm(p: Pending): Promise<string | null> {
    const header = { "Idempotency-Key": newKey() };
    let err: unknown;
    if (p.kind === "start") {
      const r = await api.POST("/v1/ontologies/versions/{version_id}/reindex", { params: { path: { version_id: versionId! }, header }, body: { mode: p.mode, confirm: true } });
      if (!r.data) err = r.error;
      else setOutcome("Build started. It runs in the background; follow it below.");
    } else if (p.kind === "activate" || p.kind === "rollback") {
      const r = await api.POST("/v1/graph/namespaces/{namespace}/activate", { params: { path: { namespace: p.ns.namespace }, header }, body: { reason_code: p.kind === "rollback" ? "rollback" : "operator_decision" } });
      if (!r.data) err = r.error;
      else setOutcome(p.kind === "rollback" ? "Rolled back to the previous namespace." : "Cut over: the rebuilt graph is live.");
    } else {
      const r = await api.POST("/v1/reindex-runs/{run_id}/cancel", { params: { path: { run_id: p.run.id }, header } });
      if (!r.data) err = r.error;
      else setOutcome("Build cancelled and its namespace cleared.");
    }
    if (err) return errCode(err, "request_failed");
    await refresh();
    return null;
  }

  const runList = (runs.data?.data ?? []) as unknown as Run[];
  const nsList = (spaces.data?.data ?? []) as unknown as Ns[];
  const readyRun = (n: Ns) => runList.find((r) => r.id === n.reindex_run_id)?.status === "ready";
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Graph rebuild</h1>
      <p className="max-w-3xl text-sm text-slate-600 dark:text-slate-300">Rebuild the graph from Postgres in a separate namespace while the live one keeps serving, check it, then cut over in one step. The previous namespace is kept for rollback.</p>
      {outcome ? <Banner tone="green" role="status">{outcome}</Banner> : null}

      <section aria-labelledby="start-h" className="space-y-3">
        <h2 id="start-h" className="font-medium">Start a rebuild</h2>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm">Mode{" "}
            <select aria-label="Rebuild mode" className="rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900" value={mode} onChange={(e) => { setMode(e.target.value as "project" | "reextract"); setPreview(null); }}>
              <option value="project">project (from Postgres, no model calls)</option>
              <option value="reextract">reextract (also re-run extraction; spends model calls)</option>
            </select>
          </label>
          <button className={btn.secondary} disabled={!versionId} onClick={() => void showPreview()}>Preview</button>
          <button className={btn.primary} disabled={!versionId || !preview} onClick={() => setPending({ kind: "start", mode })}>Start rebuild…</button>
        </div>
        {preview ? (
          <dl className="grid max-w-3xl grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4" data-testid="reindex-preview">
            {Object.entries(preview).filter(([, v]) => typeof v === "number" || typeof v === "string").map(([k, v]) => <div key={k}><dt className="text-xs text-slate-500">{k.replace(/_/g, " ")}</dt><dd>{String(v)}</dd></div>)}
          </dl>
        ) : <p className="text-xs text-slate-500">Preview writes nothing; it shows what would be built and what it would cost.</p>}
      </section>

      <section aria-labelledby="ns-h" className="space-y-3">
        <h2 id="ns-h" className="font-medium">Namespaces</h2>
        {spaces.isLoading ? <Loading label="Loading namespaces…" /> : spaces.isError ? <ErrorState onRetry={() => void spaces.refetch()} /> : stateForStatus(spaces.data!.response.status, "namespaces") ?? (nsList.length === 0 ? <Empty title="Only the default namespace" hint="A namespace appears here when you start a rebuild." /> : (
          <table className="w-full text-left text-sm"><caption className="sr-only">Graph namespaces</caption>
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">Namespace</th><th className="pr-4">Status</th><th className="pr-4">Activated</th><th>&nbsp;</th></tr></thead>
            <tbody>{nsList.map((n) => (
              <tr key={n.namespace} className="border-t border-slate-200 dark:border-slate-800">
                <td className="py-1 pr-4 font-mono text-xs">{n.namespace}</td><td className="pr-4"><Badge tone={TONE[n.status] ?? "slate"}>{n.status}</Badge></td><td className="pr-4 text-xs">{fmt(n.activated_at)}</td>
                <td className="space-x-2 text-right">
                  {n.status === "building" && readyRun(n) ? <button className={btn.primary} onClick={() => setPending({ kind: "activate", ns: n })}>Cut over…</button> : null}
                  {n.status === "retired" ? <button className={btn.secondary} onClick={() => setPending({ kind: "rollback", ns: n })}>Roll back to this…</button> : null}
                </td>
              </tr>
            ))}</tbody>
          </table>
        ))}
      </section>

      <section aria-labelledby="runs-h" className="space-y-3">
        <h2 id="runs-h" className="font-medium">Runs</h2>
        {runs.isLoading ? <Loading label="Loading runs…" /> : runs.isError ? <ErrorState onRetry={() => void runs.refetch()} /> : runList.length === 0 ? <Empty title="No rebuilds yet" /> : (
          <table className="w-full text-left text-sm"><caption className="sr-only">Rebuild runs</caption>
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">Started</th><th className="pr-4">Mode</th><th className="pr-4">Status</th><th className="pr-4">Progress</th><th>&nbsp;</th></tr></thead>
            <tbody>{runList.map((r) => (
              <tr key={r.id} className="border-t border-slate-200 align-top dark:border-slate-800">
                <td className="py-1 pr-4 whitespace-nowrap text-xs">{fmt(r.created_at)}</td><td className="pr-4">{r.mode}</td>
                <td className="pr-4"><Badge tone={TONE[r.status] ?? "slate"}>{r.status.replace(/_/g, " ")}</Badge>{r.error_code ? <span className="ml-1 font-mono text-xs text-red-700">{r.error_code}</span> : null}</td>
                <td className="pr-4 font-mono text-xs">{Object.entries(r.progress ?? {}).map(([k, v]) => `${k}: ${String(v)}`).join(", ") || "—"}{r.report ? <details className="mt-1"><summary className="cursor-pointer">verification report</summary><pre className="max-w-xl overflow-x-auto whitespace-pre-wrap text-[11px]">{JSON.stringify(r.report, null, 1)}</pre></details> : null}</td>
                <td className="text-right">{["building", "catching_up", "ready"].includes(r.status) ? <button className={btn.secondary} onClick={() => setPending({ kind: "cancel", run: r })}>Cancel…</button> : null}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </section>

      {pending ? (
        <ConfirmDialog
          title={pending.kind === "start" ? "Start the rebuild" : pending.kind === "activate" ? "Cut over to the rebuilt graph" : pending.kind === "rollback" ? "Roll back" : "Cancel the build"}
          confirmLabel={pending.kind === "start" ? "Start" : pending.kind === "activate" ? "Cut over" : pending.kind === "rollback" ? "Roll back" : "Cancel build"}
          danger={pending.kind !== "start"}
          onClose={() => setPending(null)}
          onConfirm={() => confirm(pending)}
        >
          <p className="text-sm">
            {pending.kind === "start" ? `Builds a new namespace in the background (${pending.mode}). The live graph is untouched until you cut over.`
              : pending.kind === "activate" ? "One exchange in Neo4j and one pointer flip in Postgres, under the workspace's graph lock. Reads and writes continue; the previous namespace is kept for rollback."
              : pending.kind === "rollback" ? "Re-activates the retired namespace; the current one is retired in its place." : "Stops the build and clears the namespace it was writing."}
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
