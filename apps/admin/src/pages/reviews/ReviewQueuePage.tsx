import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { Badge, inputCls, ReviewTypeBadge, SubmitterRevokedBadge } from "../../components/ui";
import { ageLabel, fmt } from "../../lib/format";

const TYPES = ["identity_match", "contradiction", "graphiti_invalidation", "policy_violation", "memory_proposal", "memory_conflict", "source_compromised", "deletion_exception", "consolidation_proposal", "correction_request", "authoritative_override"] as const;

export function ReviewQueuePage() {
  const can = useCan();
  const [params] = useSearchParams();
  const [type, setType] = useState<string>(params.get("type") ?? "");
  const [status, setStatus] = useState<"open" | "decided">("open");
  const list = useInfiniteQuery({
    queryKey: ["reviews", type, status],
    enabled: can.readReviews,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.GET("/v1/reviews", { params: { query: { type: (type || undefined) as never, status, limit: 50, cursor: pageParam } } }),
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
  });
  if (!can.readReviews) return <PermissionDenied what="the review queue" />;

  let content;
  if (list.isLoading) content = <Loading label="Loading review items…" />;
  else if (list.isError) content = <ErrorState onRetry={() => void list.refetch()} />;
  else {
    const blocked = stateForStatus(list.data!.pages[0]!.response.status, "the review queue", () => void list.refetch());
    const rows = list.data!.pages.flatMap((p) => p.data?.items ?? []);
    if (blocked) content = blocked;
    else if (rows.length === 0) content = <Empty title={status === "open" ? "Nothing to review" : "No decided items"} hint={status === "open" ? "Identity matches, contradictions, policy exceptions, memory proposals, deletion exceptions and consolidation proposals land here." : undefined} />;
    else
      content = (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-2 pr-4">Type</th><th className="pr-4">Why</th><th className="pr-4">Opened</th><th className="pr-4">Age</th><th className="pr-4">Due</th><th>Status</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900" data-testid={`review-${r.id}`}>
                  <td className="py-2 pr-4"><ReviewTypeBadge type={r.item_type} />{r.agent_generated ? <> <Badge tone="amber" title="Produced by an agent">agent generated</Badge></> : null}{r.submitter_revoked ? <> <SubmitterRevokedBadge /></> : null}</td>
                  <td className="pr-4"><Link className="font-mono text-xs underline-offset-2 hover:underline" to={`/reviews/${r.id}`}>{r.reason_code}</Link></td>
                  <td className="whitespace-nowrap pr-4">{fmt(r.created_at)}</td>
                  <td className="pr-4">{ageLabel((Date.now() - new Date(r.created_at).getTime()) / 1000)}</td>
                  <td className="whitespace-nowrap pr-4">{r.due_at ? fmt(r.due_at) : "—"}</td>
                  <td>{r.status === "open" ? <Badge tone="amber">open</Badge> : <Badge tone="green">{r.decision?.replace(/_/g, " ") ?? "decided"}</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {list.hasNextPage ? <button type="button" onClick={() => void list.fetchNextPage()} className="mt-3 text-sm underline">Load more</button> : null}
        </>
      );
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Review Queue</h1>
        <div className="flex gap-3 text-sm">
          <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Type</span>
            <select aria-label="Filter by type" className={inputCls} value={type} onChange={(e) => setType(e.target.value)}><option value="">All types</option>{TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}</select></label>
          <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Status</span>
            <select aria-label="Filter by status" className={inputCls} value={status} onChange={(e) => setStatus(e.target.value as "open" | "decided")}><option value="open">Open</option><option value="decided">Decided</option></select></label>
        </div>
      </div>
      {content}
    </div>
  );
}
