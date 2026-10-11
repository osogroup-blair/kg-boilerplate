import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { inputCls, ProcessingBadge } from "../../components/ui";
import { fmt } from "../../lib/format";

type Status = "accepted" | "processing" | "indexed" | "partial" | "failed";
type Kind = "message" | "conversation" | "observation" | "document" | "json";

export function InteractionsPage() {
  const can = useCan();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const kind = params.get("kind") ?? "";
  const source = params.get("source_system") ?? "";
  const q = useInfiniteQuery({
    queryKey: ["interactions", status, kind, source],
    initialPageParam: undefined as string | undefined,
    enabled: can.readInteractions,
    queryFn: ({ pageParam }) =>
      api.GET("/v1/interactions", { params: { query: { status: (status || undefined) as Status | undefined, kind: (kind || undefined) as Kind | undefined, source_system: source || undefined, limit: 50, cursor: pageParam } } }),
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
  });
  const set = (k: string, v: string) => { const next = new URLSearchParams(params); if (v) next.set(k, v); else next.delete(k); setParams(next, { replace: true }); };

  let content;
  if (!can.readInteractions) content = <PermissionDenied what="interactions" />;
  else if (q.isLoading) content = <Loading label="Loading interactions…" />;
  else if (q.isError) content = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const blocked = stateForStatus(q.data!.pages[0]!.response.status, "interactions", () => void q.refetch());
    const rows = q.data!.pages.flatMap((p) => p.data?.items ?? []);
    if (blocked) content = blocked;
    else if (rows.length === 0) content = <Empty title="No interactions" hint={status || kind || source ? "No interaction matches these filters." : "Events appear here as apps and agents submit them."} />;
    else
      content = (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-2 pr-4">Recorded</th><th className="pr-4">Kind</th><th className="pr-4">Source</th><th className="pr-4">Status</th><th className="pr-4">Occurred</th><th className="pr-4">Submitted by</th><th>Classification</th></tr></thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                  <td className="whitespace-nowrap py-2 pr-4"><Link to={`/interactions/${i.id}`} className="underline-offset-2 hover:underline">{fmt(i.recorded_at)}</Link></td>
                  <td className="pr-4 font-mono text-xs">{i.kind}</td>
                  <td className="pr-4 text-xs text-slate-500">{i.source_system} · {i.source_ref}</td>
                  <td className="pr-4"><ProcessingBadge status={i.processing_status} /></td>
                  <td className="whitespace-nowrap pr-4 text-xs">{i.occurred_at_unknown ? <span className="text-slate-500">unknown</span> : fmt(i.occurred_at)}</td>
                  <td className="pr-4 text-xs">{i.submitted_by_handle ?? "—"}</td>
                  <td className="text-xs">{i.classification}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {q.hasNextPage ? <button type="button" onClick={() => void q.fetchNextPage()} className="mt-3 text-sm underline">Load more</button> : null}
        </>
      );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Interactions &amp; Episodes</h1>
      <p className="text-sm text-slate-500">The ledger of every event the platform accepted, what became of it, and the stage-by-stage trail. Content is stored encrypted and is not shown here.</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Status</span>
          <select aria-label="Filter by status" className={inputCls} value={status} onChange={(e) => set("status", e.target.value)}><option value="">All</option>{["accepted", "processing", "indexed", "partial", "failed"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Kind</span>
          <select aria-label="Filter by kind" className={inputCls} value={kind} onChange={(e) => set("kind", e.target.value)}><option value="">All</option>{["message", "conversation", "observation", "document", "json"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Source system</span>
          <input aria-label="Filter by source system" className={inputCls} value={source} onChange={(e) => set("source_system", e.target.value.trim())} placeholder="e.g. conversation_app" /></label>
      </div>
      {content}
    </div>
  );
}
