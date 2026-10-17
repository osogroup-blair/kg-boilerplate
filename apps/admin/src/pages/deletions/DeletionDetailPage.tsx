import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { Banner, btn, DeletionStatusBadge } from "../../components/ui";
import { fmt } from "../../lib/format";
import { ReceiptView } from "./Receipt";

const ACTIVE = new Set(["received", "frozen", "planned", "purging", "verifying"]);
const STEPS: Array<[string, string]> = [["frozen_at", "Frozen (reads blocked)"], ["planned_at", "Planned"], ["purge_started_at", "Purge started"], ["verified_at", "Verified"], ["completed_at", "Completed"]];

export function DeletionDetailPage() {
  const { requestId = "" } = useParams();
  const can = useCan();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<"cancel" | "retry" | null>(null);
  const q = useQuery({
    queryKey: ["deletion", requestId],
    enabled: can.readDeletions,
    refetchInterval: (query) => (query.state.data?.data && ACTIVE.has(query.state.data.data.status) ? 3000 : false),
    queryFn: () => api.GET("/v1/deletion-requests/{request_id}", { params: { path: { request_id: requestId } } }),
  });
  if (!can.readDeletions) return <PermissionDenied what="deletion requests" />;
  if (q.isLoading) return <Loading label="Loading deletion request…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "This deletion request", () => void q.refetch());
  if (blocked || !q.data!.data) return <div className="space-y-4"><Link to="/deletions" className={btn.link}>← Deletions</Link>{blocked}</div>;
  const d = q.data!.data;
  const cooling = d.kind === "workspace" && d.status === "frozen" && d.cooling_off_until;
  async function act(kind: "cancel" | "retry"): Promise<string | null> {
    const res = kind === "cancel"
      ? await api.POST("/v1/deletion-requests/{request_id}/cancel", { params: { path: { request_id: requestId } } })
      : await api.POST("/v1/deletion-requests/{request_id}/retry", { params: { path: { request_id: requestId } } });
    if (res.response.ok) { await Promise.all([qc.invalidateQueries({ queryKey: ["deletion", requestId] }), qc.invalidateQueries({ queryKey: ["deletions"] })]); return null; }
    const code = (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
    return code === "invalid_request_state" ? "The request is not in a state that allows that." : code === "forbidden" ? "Your role may not do that." : code;
  }
  return (
    <div className="space-y-5">
      <Link to="/deletions" className={btn.link}>← Deletions</Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Deletion of {d.kind.replace(/_/g, " ")}</h1><DeletionStatusBadge status={d.status} />
        <span className="font-mono text-xs text-slate-500">{d.request_id}</span>
      </div>
      <p className="text-sm text-slate-500">Received {fmt(d.received_at)} · reason <span className="font-mono">{d.reason_code}</span>{d.target_id ? <> · target <span className="font-mono">{d.target_id.slice(0, 8)}…</span></> : null}</p>
      {d.status === "blocked_legal_hold" ? <Banner tone="red" role="alert">A legal hold covers this target. It is frozen (unreadable) but nothing is purged. <Link className="underline" to="/deletions?tab=holds">Legal holds</Link></Banner> : null}
      {d.status === "failed" ? <Banner tone="red" role="alert">A step failed (<span className="font-mono">{d.error_code ?? "unknown"}</span>). Steps are safe to repeat: retrying resumes where it stopped.</Banner> : null}
      {cooling ? <Banner tone="amber" role="status">Cooling off until {fmt(d.cooling_off_until)}. The workspace is suspended; the platform operator can still cancel this before then.</Banner> : null}
      {d.open_exceptions > 0 ? <Banner tone="amber" role="status">{d.open_exceptions} exception{d.open_exceptions === 1 ? "" : "s"} need a person. <Link className="underline" to="/reviews?type=deletion_exception">Open the review queue</Link></Banner> : null}
      <ol aria-label="Progress" className="grid gap-1 text-sm md:grid-cols-5">
        {STEPS.map(([k, label]) => { const at = (d as unknown as Record<string, string | null | undefined>)[k]; return <li key={k} className={`rounded border px-2 py-1 ${at ? "border-emerald-300 dark:border-emerald-800" : "border-slate-200 text-slate-400 dark:border-slate-800"}`}>{label}<div className="text-xs">{at ? fmt(at) : "—"}</div></li>; })}
      </ol>
      {Object.keys(d.item_counts).length ? (
        <section aria-label="Plan" className="space-y-1"><h2 className="font-medium">Plan</h2>
          <ul className="flex flex-wrap gap-3 text-sm">{Object.entries(d.item_counts).map(([k, n]) => <li key={k}><span className="font-mono text-xs">{k}</span> · {n}</li>)}</ul></section>
      ) : null}
      {d.receipt ? <ReceiptView receipt={d.receipt} /> : null}
      <div className="flex gap-2">
        {can.requestDeletion && cooling ? <button type="button" className={btn.secondary} onClick={() => setConfirm("cancel")}>Cancel this deletion…</button> : null}
        {can.operateDeletions && d.status === "failed" ? <button type="button" className={btn.primary} onClick={() => setConfirm("retry")}>Retry…</button> : null}
      </div>
      {confirm ? (
        <ConfirmDialog title={confirm === "cancel" ? "Cancel this deletion" : "Retry this deletion"} confirmLabel={confirm === "cancel" ? "Cancel the deletion" : "Retry"} danger={confirm === "cancel"} onClose={() => setConfirm(null)} onConfirm={() => act(confirm)}>
          <p className="text-sm">{confirm === "cancel" ? "The workspace becomes active again and nothing is purged." : "The request resumes from the step that failed."}</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
