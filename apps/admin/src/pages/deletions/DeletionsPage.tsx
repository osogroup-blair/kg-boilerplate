import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { Badge, Banner, btn, DeletionStatusBadge, Field, inputCls } from "../../components/ui";
import { fmt, newKey } from "../../lib/format";
import { NewDeletionDialog } from "./NewDeletionDialog";

type Hold = components["schemas"]["LegalHoldOut"];
type Suppression = components["schemas"]["SuppressionOut"];
const STATUSES = ["received", "frozen", "planned", "purging", "verifying", "completed", "completed_with_exceptions", "blocked_legal_hold", "failed", "cancelled"] as const;
type Tab = "requests" | "holds" | "suppressions";

function Requests() {
  const can = useCan();
  const nav = useNavigate();
  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const q = useQuery({ queryKey: ["deletions", status], queryFn: () => api.GET("/v1/deletion-requests", { params: { query: { status: status || undefined, limit: 100 } } }) });
  if (q.isLoading) return <Loading label="Loading deletion requests…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "deletion requests", () => void q.refetch());
  if (blocked) return <>{blocked}</>;
  const rows = q.data!.data ?? [];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Status</span>
          <select aria-label="Filter by status" className={inputCls} value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All</option>{STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}</select></label>
        {can.requestDeletion ? <button type="button" className={btn.danger} onClick={() => setCreating(true)}>Request a deletion…</button> : null}
      </div>
      {rows.length === 0 ? <Empty title="No deletion requests" hint="A request freezes its target at once and then purges it in steps; each one ends with a receipt." /> : (
        <table className="w-full text-left text-sm" aria-label="Deletion requests">
          <thead className="text-slate-500"><tr><th className="py-2 pr-4">Request</th><th className="pr-4">Kind</th><th className="pr-4">Status</th><th className="pr-4">Received</th><th>Open exceptions</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.request_id} className="border-t border-slate-200 dark:border-slate-800" data-testid={`deletion-${r.request_id}`}>
              <td className="py-2 pr-4"><Link className="font-mono text-xs underline" to={`/deletions/${r.request_id}`}>{r.request_id.slice(0, 8)}…</Link></td>
              <td className="pr-4">{r.kind.replace(/_/g, " ")}</td>
              <td className="pr-4"><DeletionStatusBadge status={r.status} /></td>
              <td className="whitespace-nowrap pr-4">{fmt(r.received_at)}</td>
              <td>{r.open_exceptions ? <Badge tone="amber">{r.open_exceptions}</Badge> : "—"}</td>
            </tr>))}</tbody>
        </table>
      )}
      {creating ? <NewDeletionDialog onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); nav(`/deletions/${id}`); }} /> : null}
    </div>
  );
}

function NewHoldDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [scope, setScope] = useState<"subject" | "interaction" | "workspace">("subject");
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("litigation");
  const [note, setNote] = useState("");
  return (
    <ConfirmDialog title="Place a legal hold" confirmLabel="Place hold" disabled={!reason || (scope !== "workspace" && !/^[0-9a-f-]{36}$/i.test(target.trim()))} onClose={onClose}
      onConfirm={async () => {
        const res = await api.POST("/v1/legal-holds", { body: { scope_kind: scope, target_id: scope === "workspace" ? null : target.trim(), reason_code: reason, note } });
        if (res.response.ok) { await qc.invalidateQueries({ queryKey: ["holds"] }); return null; }
        return (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
      }}>
      <p className="text-sm text-slate-500">While a hold covers a target, a deletion request for it is frozen (unreadable) but not purged. Releasing the hold resumes it.</p>
      <Field label="Covers"><select aria-label="Scope" className={`${inputCls} w-full`} value={scope} onChange={(e) => setScope(e.target.value as typeof scope)}><option value="subject">A person or thing</option><option value="interaction">An interaction</option><option value="workspace">The whole workspace</option></select></Field>
      {scope !== "workspace" ? <Field label="Id"><input aria-label="Target id" className={`${inputCls} w-full font-mono`} value={target} onChange={(e) => setTarget(e.target.value)} /></Field> : null}
      <Field label="Reason code"><input aria-label="Reason code" className={`${inputCls} w-full`} value={reason} pattern="[a-z][a-z0-9_]*" onChange={(e) => setReason(e.target.value)} /></Field>
      <Field label="Note (optional)"><input aria-label="Note" className={`${inputCls} w-full`} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
    </ConfirmDialog>
  );
}

function Holds() {
  const can = useCan();
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [releasing, setReleasing] = useState<Hold | null>(null);
  const q = useQuery({ queryKey: ["holds"], enabled: can.manageHolds, queryFn: () => api.GET("/v1/legal-holds", {}) });
  if (!can.manageHolds) return <PermissionDenied what="legal holds" />;
  if (q.isLoading) return <Loading label="Loading legal holds…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "legal holds", () => void q.refetch());
  if (blocked) return <>{blocked}</>;
  const rows = q.data!.data ?? [];
  return (
    <div className="space-y-3">
      <div className="flex justify-end"><button type="button" className={btn.primary} onClick={() => setAdding(true)}>Place a hold…</button></div>
      {rows.length === 0 ? <Empty title="No legal holds" /> : (
        <table className="w-full text-left text-sm" aria-label="Legal holds">
          <thead className="text-slate-500"><tr><th className="py-2 pr-4">Covers</th><th className="pr-4">Reason</th><th className="pr-4">Placed</th><th className="pr-4">State</th><th /></tr></thead>
          <tbody>{rows.map((h) => (
            <tr key={h.id} className="border-t border-slate-200 dark:border-slate-800" data-testid={`hold-${h.id}`}>
              <td className="py-2 pr-4">{h.scope_kind}{h.target_id ? <span className="ml-1 font-mono text-xs">{h.target_id.slice(0, 8)}…</span> : null}</td>
              <td className="pr-4 font-mono text-xs">{h.reason_code}</td>
              <td className="whitespace-nowrap pr-4">{fmt(h.created_at)}</td>
              <td className="pr-4">{h.released_at ? <Badge>released {fmt(h.released_at)}</Badge> : <Badge tone="red">active</Badge>}</td>
              <td>{h.released_at ? null : <button type="button" className={btn.link} aria-label={`Release hold ${h.id.slice(0, 8)}`} onClick={() => setReleasing(h)}>Release…</button>}</td>
            </tr>))}</tbody>
        </table>
      )}
      {adding ? <NewHoldDialog onClose={() => setAdding(false)} /> : null}
      {releasing ? (
        <ConfirmDialog title="Release this hold" confirmLabel="Release" danger onClose={() => setReleasing(null)}
          onConfirm={async () => {
            const res = await api.DELETE("/v1/legal-holds/{hold_id}", { params: { path: { hold_id: releasing.id } } });
            if (res.response.ok) { await Promise.all([qc.invalidateQueries({ queryKey: ["holds"] }), qc.invalidateQueries({ queryKey: ["deletions"] })]); return null; }
            return (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
          }}>
          <p className="text-sm">Deletion requests this hold was blocking resume and will purge. Another active hold on the same target keeps them blocked.</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function Suppressions() {
  const can = useCan();
  const qc = useQueryClient();
  const [lifting, setLifting] = useState<Suppression | null>(null);
  const q = useQuery({ queryKey: ["suppressions"], enabled: can.manageHolds, queryFn: () => api.GET("/v1/suppressions", {}) });
  if (!can.manageHolds) return <PermissionDenied what="suppressions" />;
  if (q.isLoading) return <Loading label="Loading suppressions…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "suppressions", () => void q.refetch());
  if (blocked) return <>{blocked}</>;
  const rows = q.data!.data ?? [];
  return (
    <div className="space-y-3">
      <Banner tone="slate">After a deletion, a hash of the person's identifiers is kept so that a source system cannot quietly recreate them. Only the hash prefix is shown. Lift one only when the person has asked to be known again.</Banner>
      {rows.length === 0 ? <Empty title="No suppressions" /> : (
        <table className="w-full text-left text-sm" aria-label="Suppressions">
          <thead className="text-slate-500"><tr><th className="py-2 pr-4">Hash</th><th className="pr-4">Kind</th><th className="pr-4">Since</th><th className="pr-4">State</th><th /></tr></thead>
          <tbody>{rows.map((s) => (
            <tr key={`${s.request_id}:${s.value_hmac_prefix}`} className="border-t border-slate-200 dark:border-slate-800">
              <td className="py-2 pr-4 font-mono text-xs">{s.value_hmac_prefix}</td><td className="pr-4">{s.kind}</td><td className="whitespace-nowrap pr-4">{fmt(s.created_at)}</td>
              <td className="pr-4">{s.lifted_at ? <Badge>lifted {fmt(s.lifted_at)}</Badge> : <Badge tone="amber">active</Badge>}</td>
              <td>{s.lifted_at ? null : <button type="button" className={btn.link} aria-label={`Lift ${s.value_hmac_prefix}`} onClick={() => setLifting(s)}>Lift…</button>}</td>
            </tr>))}</tbody>
        </table>
      )}
      {lifting ? (
        <ConfirmDialog title="Lift this suppression" confirmLabel="Lift" danger onClose={() => setLifting(null)}
          onConfirm={async () => {
            const res = await api.POST("/v1/suppressions/{prefix}/lift", { params: { path: { prefix: lifting.value_hmac_prefix }, header: { "Idempotency-Key": newKey() } } as never });
            if (res.response.ok) { await qc.invalidateQueries({ queryKey: ["suppressions"] }); return null; }
            return (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
          }}>
          <p className="text-sm">The identifier can be used again to create a person. This is recorded in the audit log with your name.</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

export function DeletionsPage() {
  const can = useCan();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab | null) ?? "requests";
  if (!can.readDeletions) return <PermissionDenied what="deletion requests" />;
  const TABS: Array<[Tab, string]> = [["requests", "Requests"], ["holds", "Legal holds"], ["suppressions", "Suppressions"]];
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Deletions</h1>
      <div role="tablist" aria-label="Deletion sections" className="flex gap-2 border-b border-slate-200 dark:border-slate-800">
        {TABS.filter(([k]) => k === "requests" || can.manageHolds).map(([k, label]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setParams(k === "requests" ? {} : { tab: k })} className={`px-3 py-1.5 text-sm ${tab === k ? "border-b-2 border-slate-900 font-medium dark:border-slate-100" : "text-slate-600 dark:text-slate-300"}`}>{label}</button>
        ))}
      </div>
      {tab === "requests" ? <Requests /> : tab === "holds" ? <Holds /> : <Suppressions />}
    </div>
  );
}
