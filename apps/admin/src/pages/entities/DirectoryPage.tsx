import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Banner, Badge, btn, inputCls, ProjectionBadge } from "../../components/ui";
import { Empty, ErrorState, Loading, stateForStatus } from "../../components/states";
import { fmt } from "../../lib/format";
import { useActiveOntology } from "../../ontology/queries";
import { NewEntityDialog } from "./NewEntityDialog";

export function DirectoryPage() {
  const can = useCan();
  const active = useActiveOntology();
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setTerm(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);

  const list = useInfiniteQuery({
    queryKey: ["entities", type, term],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.GET("/v1/entities", { params: { query: { type: type || undefined, q: term || undefined, limit: 50, cursor: pageParam } } }),
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
  });
  const types = active.data?.data?.entity_types ?? [];

  let content;
  if (list.isLoading) content = <Loading label="Loading entities…" />;
  else if (list.isError) content = <ErrorState onRetry={() => void list.refetch()} />;
  else {
    const blocked = stateForStatus(list.data!.pages[0]!.response.status, "entities", () => void list.refetch());
    const rows = list.data!.pages.flatMap((p) => p.data?.items ?? []);
    if (blocked) content = blocked;
    else if (rows.length === 0) content = <Empty title={term || type ? "No entities match" : "No entities yet"} hint={term || type ? "Try a different search." : can.writeEntities ? "Create one, or have a trusted app send commands." : undefined} />;
    else
      content = (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500"><tr><th className="py-2 pr-4">Name</th><th className="pr-4">Type</th><th className="pr-4">External ids</th><th className="pr-4">Classification</th><th className="pr-4">Graph</th><th>Recorded</th></tr></thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                  <td className="py-2 pr-4"><Link to={`/entities/${e.id}`} className="font-medium underline-offset-2 hover:underline">{e.name}</Link>{e.status !== "active" ? <> <Badge tone="amber">{e.status}</Badge></> : null}{e.origin === "extracted" ? <> <Badge tone="blue">extracted</Badge></> : null}</td>
                  <td className="pr-4 font-mono text-xs">{e.type}</td>
                  <td className="pr-4 font-mono text-xs">{e.external_ids.map((x) => `${x.system}:${x.value}`).join(", ") || "—"}</td>
                  <td className="pr-4"><Badge tone={e.classification === "internal" ? "slate" : "amber"}>{e.classification}</Badge></td>
                  <td className="pr-4"><ProjectionBadge status={e.projection_status} /></td>
                  <td className="whitespace-nowrap">{fmt(e.recorded_at)}</td>
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
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Search by name</span><input aria-label="Search by name" className={`${inputCls} w-64`} placeholder="Sarah…" value={q} onChange={(e) => setQ(e.target.value)} /></label>
          <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Type</span>
            <select aria-label="Filter by type" className={inputCls} value={type} onChange={(e) => setType(e.target.value)}><option value="">All types</option>{types.map((t) => <option key={t}>{t}</option>)}</select></label>
        </div>
        {can.writeEntities ? <button type="button" className={btn.primary} disabled={types.length === 0} onClick={() => setCreating(true)}>New entity</button> : null}
      </div>
      {active.data && !active.data.data?.version ? <Banner tone="amber">This workspace has no active ontology, so commands are refused. Publish one under Ontology.</Banner> : null}
      {content}
      {creating ? <NewEntityDialog types={types} onClose={() => setCreating(false)} /> : null}
    </div>
  );
}
