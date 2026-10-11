import * as Dialog from "@radix-ui/react-dialog";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { CommandStatusBadge, inputCls, ProjectionBadge } from "../../components/ui";
import { Empty, ErrorState, Loading, stateForStatus } from "../../components/states";
import { fmt } from "../../lib/format";

interface Trace { stage: string; outcome: string; violations?: Array<{ rule_kind: string; rule_id?: string | null; code: string; path: string; action?: string }> }

/** The decision trace: stage, rule, code and path for every refusal. It never contains attribute values, so it is safe to show. */
function CommandDrawer({ commandId, onClose }: { commandId: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["command", commandId], queryFn: () => api.GET("/v1/commands/{command_id}", { params: { path: { command_id: commandId } } }) });
  let body;
  if (q.isLoading) body = <Loading />;
  else if (q.isError) body = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const res = q.data!;
    body = stateForStatus(res.response.status, "This command", () => void q.refetch());
    if (!body && res.data) {
      const c = res.data;
      const trace = c.decision as unknown as Trace[];
      body = (
        <div className="space-y-4 text-sm">
          <dl className="grid grid-cols-[9rem_1fr] gap-y-1">
            <dt className="text-slate-500">Type</dt><dd className="font-mono">{c.command_type}</dd>
            <dt className="text-slate-500">Status</dt><dd><CommandStatusBadge status={c.status} /> <span className="text-slate-500">HTTP {c.http_status}</span></dd>
            <dt className="text-slate-500">Graph</dt><dd><ProjectionBadge status={c.projection_status} />{c.last_error_code ? <span className="ml-2 font-mono text-xs text-red-700">{c.last_error_code}</span> : null} <span className="text-xs text-slate-500">{c.projection_attempts} attempt(s)</span></dd>
            <dt className="text-slate-500">Authority</dt><dd>{c.authority_class.replace(/_/g, " ")} · {c.classification}</dd>
            <dt className="text-slate-500">Source</dt><dd>{c.source_system ?? "—"}{c.source_ref ? ` · ${c.source_ref}` : ""}</dd>
            <dt className="text-slate-500">When</dt><dd>{fmt(c.created_at)}</dd>
            <dt className="text-slate-500">Ontology</dt><dd><Link to={`/ontology/versions/${c.ontology_version_id}`} className="font-mono text-xs underline">{c.ontology_version_id.slice(0, 8)}…</Link></dd>
            {c.entity_id ? <><dt className="text-slate-500">Entity</dt><dd><Link to={`/entities/${c.entity_id}`} className="underline">open</Link></dd></> : null}
            {c.fact_id ? <><dt className="text-slate-500">Fact</dt><dd className="font-mono text-xs">{c.fact_id}</dd></> : null}
          </dl>
          <div>
            <h3 className="mb-1 font-medium">Decision trace</h3>
            <ol className="space-y-2">
              {trace.map((s) => (
                <li key={s.stage} className="rounded border border-slate-200 p-2 dark:border-slate-800">
                  <div className="flex justify-between"><span className="font-mono text-xs">{s.stage}</span><span className={s.outcome === "reject" ? "font-medium text-red-700" : s.outcome === "queue_review" ? "text-amber-700" : "text-slate-600"}>{s.outcome}</span></div>
                  {s.violations?.length ? <ul className="mt-1 space-y-0.5 text-xs">{s.violations.map((v, i) => <li key={i}><span className="font-mono">{v.code}</span> at <span className="font-mono">{v.path}</span> <span className="text-slate-500">({v.rule_kind}{v.action ? `, ${v.action}` : ""})</span></li>)}</ul> : null}
                </li>
              ))}
            </ol>
          </div>
        </div>
      );
    }
  }
  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/30" />
        <Dialog.Content aria-describedby={undefined} className="fixed right-0 top-0 h-full w-full max-w-lg overflow-y-auto bg-white p-6 shadow-xl dark:bg-slate-900">
          <div className="mb-4 flex items-center justify-between">
            <Dialog.Title className="text-lg font-semibold">Command</Dialog.Title>
            <Dialog.Close aria-label="Close" className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-800"><X className="h-4 w-4" /></Dialog.Close>
          </div>
          {body}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function CommandsPage() {
  const { commandId } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const type = params.get("type") ?? "";
  const entityId = params.get("entity_id") ?? "";
  const q = useInfiniteQuery({
    queryKey: ["commands", status, type, entityId],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api.GET("/v1/commands", { params: { query: { status: (status || undefined) as "applied" | undefined, type: (type || undefined) as "entity.create" | undefined, entity_id: entityId || undefined, limit: 50, cursor: pageParam } } }),
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
  });
  const set = (k: string, v: string) => { const next = new URLSearchParams(params); if (v) next.set(k, v); else next.delete(k); setParams(next, { replace: true }); };

  let content;
  if (q.isLoading) content = <Loading label="Loading commands…" />;
  else if (q.isError) content = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const blocked = stateForStatus(q.data!.pages[0]!.response.status, "the command ledger", () => void q.refetch());
    const rows = q.data!.pages.flatMap((p) => p.data?.items ?? []);
    if (blocked) content = blocked;
    else if (rows.length === 0) content = <Empty title="No commands" hint={status || type || entityId ? "No command matches these filters." : "Commands appear here as trusted apps and admins write entities and relationships."} />;
    else
      content = (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-2 pr-4">When</th><th className="pr-4">Command</th><th className="pr-4">Status</th><th className="pr-4">Graph</th><th>Source</th></tr></thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                  <td className="whitespace-nowrap py-2 pr-4">{fmt(c.created_at)}</td>
                  <td className="pr-4"><button type="button" className="font-mono text-xs underline-offset-2 hover:underline" onClick={() => navigate(`/commands/${c.id}?${params.toString()}`)}>{c.command_type}</button></td>
                  <td className="pr-4"><CommandStatusBadge status={c.status} /></td>
                  <td className="pr-4"><ProjectionBadge status={c.projection_status} /></td>
                  <td className="text-xs text-slate-500">{c.source_system ?? "—"}{c.source_ref ? ` · ${c.source_ref}` : ""}</td>
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
      <h1 className="text-xl font-semibold">Commands</h1>
      <p className="text-sm text-slate-500">Every write by a trusted app or admin, with the rules that decided it. Rejected and queued commands are recorded too.</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Status</span>
          <select aria-label="Filter by status" className={inputCls} value={status} onChange={(e) => set("status", e.target.value)}><option value="">All</option><option value="applied">applied</option><option value="rejected">rejected</option><option value="needs_review">needs review</option></select></label>
        <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Command</span>
          <select aria-label="Filter by command type" className={inputCls} value={type} onChange={(e) => set("type", e.target.value)}><option value="">All</option><option>entity.create</option><option>entity.update</option><option>relationship.assert</option><option>relationship.end</option></select></label>
        {entityId ? <button type="button" className="pb-1 text-sm underline" onClick={() => set("entity_id", "")}>Clear entity filter</button> : null}
      </div>
      {content}
      {commandId ? <CommandDrawer commandId={commandId} onClose={() => navigate(`/commands?${params.toString()}`)} /> : null}
    </div>
  );
}
