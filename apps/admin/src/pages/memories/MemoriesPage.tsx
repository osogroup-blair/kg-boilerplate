import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Empty, ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { AuthorityBadge, Banner, ClassificationBadge, EmbeddingBadge, EvidenceBadge, inputCls, MemoryStatusBadge, MemoryTypeBadge, Badge } from "../../components/ui";
import { fmt } from "../../lib/format";

const TYPES = ["working", "episodic", "semantic", "procedural"] as const;
const STATUSES = ["active", "proposed", "disputed", "superseded", "expired", "rejected"] as const;
const ORIGINS = ["explicit", "proposal", "extracted", "working", "published"] as const;
const CLASSES = ["public", "internal", "confidential", "restricted", "local_only"] as const;

/** Finds an entity by name and offers it as the subject filter: the list below only ever shows memory the caller may see. */
function SubjectPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const [q, setQ] = useState("");
  const found = useQuery({
    queryKey: ["memory-subject-picker", q],
    enabled: q.trim().length >= 2,
    queryFn: () => api.GET("/v1/entities", { params: { query: { q: q.trim(), limit: 10 } } }),
  });
  const options = found.data?.data?.items ?? [];
  return (
    <div className="flex flex-col gap-1">
      <span className="text-slate-600 dark:text-slate-300">Subject</span>
      <div className="flex gap-1">
        <input aria-label="Find a subject" placeholder="Search entities…" className={`${inputCls} w-40`} value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Subject" className={inputCls} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">Any subject</option>
          {value && !options.some((o) => o.id === value) ? <option value={value}>{value.slice(0, 8)}…</option> : null}
          {options.map((o) => <option key={o.id} value={o.id}>{o.name} ({o.type})</option>)}
        </select>
      </div>
    </div>
  );
}

export function MemoriesPage() {
  const can = useCan();
  const [params, setParams] = useSearchParams();
  const [confirmPrivate, setConfirmPrivate] = useState(false);
  const get = (k: string) => params.get(k) ?? "";
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const reveal = params.get("private") === "1";
  const filters = { type: get("type"), status: get("status"), origin: get("origin"), classification: get("classification"), project: get("project"), subject_id: get("subject_id"), created_by: get("created_by"), since: get("since") };
  const list = useInfiniteQuery({
    queryKey: ["memories", filters, reveal],
    enabled: can.readMemories,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api.GET("/v1/memories", {
        params: {
          query: {
            type: (filters.type || undefined) as never,
            status: (filters.status || undefined) as never,
            origin: (filters.origin || undefined) as never,
            classification: (filters.classification || undefined) as never,
            project: filters.project || undefined,
            subject_id: filters.subject_id || undefined,
            created_by: filters.created_by || undefined,
            updated_since: filters.since ? new Date(filters.since).toISOString() : undefined,
            reveal_private: reveal || undefined,
            limit: 50,
            cursor: pageParam,
          },
        },
      }),
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
  });
  const principals = useQuery({ queryKey: ["principals-for-memory-filter"], enabled: can.readScopeGrants, queryFn: () => api.GET("/v1/principals", { params: { query: { limit: 100 } } }) });
  if (!can.readMemories) return <PermissionDenied what="memories" />;

  let content;
  if (list.isLoading) content = <Loading label="Loading memories…" />;
  else if (list.isError) content = <ErrorState onRetry={() => void list.refetch()} />;
  else {
    const blocked = stateForStatus(list.data!.pages[0]!.response.status, "memories", () => void list.refetch());
    const rows = list.data!.pages.flatMap((p) => p.data?.items ?? []);
    if (blocked) content = blocked;
    else if (rows.length === 0) content = <Empty title="No memories to show" hint="You only see memories whose scopes you have been granted, in the statuses you may read." />;
    else
      content = (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500">
              <tr><th className="py-2 pr-4">Memory</th><th className="pr-3">Type</th><th className="pr-3">Status</th><th className="pr-3">Trust</th><th className="pr-3">Class</th><th className="pr-3">Origin</th><th className="pr-3">Scopes</th><th>Recorded</th></tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.id} className="border-t border-slate-200 align-top hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900" data-testid={`memory-${m.id}`}>
                  <td className="max-w-md py-2 pr-4">
                    <Link className="underline-offset-2 hover:underline" to={`/memories/${m.id}${reveal ? "?private=1" : ""}`}>{m.summary}</Link>
                    <div className="mt-1"><EmbeddingBadge status={m.embedding_status} /></div>
                  </td>
                  <td className="pr-3"><MemoryTypeBadge type={m.type} /></td>
                  <td className="pr-3"><MemoryStatusBadge status={m.status} /></td>
                  <td className="space-y-1 pr-3"><EvidenceBadge status={m.evidence_status} /><div><AuthorityBadge authority={m.authority_class} /></div></td>
                  <td className="pr-3"><ClassificationBadge classification={m.classification} /></td>
                  <td className="pr-3 text-xs">{m.origin}</td>
                  <td className="pr-3 text-xs">{m.scopes.map((s) => s.kind).join(", ") || "—"}</td>
                  <td className="whitespace-nowrap text-xs">{fmt(m.recorded_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {list.hasNextPage ? <button type="button" onClick={() => void list.fetchNextPage()} className="mt-3 text-sm underline">Load more</button> : null}
        </>
      );
  }

  const select = (label: string, key: string, options: readonly string[]) => (
    <label><span className="mb-1 block text-slate-600 dark:text-slate-300">{label}</span>
      <select aria-label={label} className={inputCls} value={get(key)} onChange={(e) => set(key, e.target.value)}>
        <option value="">Any</option>{options.map((o) => <option key={o} value={o}>{o.replace(/_/g, " ")}</option>)}
      </select></label>
  );
  const agentOrigins = (principals.data?.data?.items ?? []).filter((p) => p.kind === "agent" || p.kind === "app");
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Memories</h1>
        {can.revealPrivateMemories ? (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={reveal} onChange={(e) => (e.target.checked ? setConfirmPrivate(true) : set("private", ""))} />
            Include other principals’ private memory
          </label>
        ) : null}
      </div>
      {reveal ? <Banner tone="amber" role="status">Private memory is shown. This view is recorded in the audit log with your name.</Banner> : null}
      <div className="flex flex-wrap items-end gap-3 text-sm" role="search" aria-label="Memory filters">
        <SubjectPicker value={filters.subject_id} onChange={(v) => set("subject_id", v)} />
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Project</span><input aria-label="Project" className={`${inputCls} w-32`} value={filters.project} onChange={(e) => set("project", e.target.value)} /></label>
        {select("Type", "type", TYPES)}
        {select("Status", "status", STATUSES)}
        {select("Origin", "origin", ORIGINS)}
        {select("Classification", "classification", CLASSES)}
        {agentOrigins.length > 0 ? (
          <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Created by</span>
            <select aria-label="Created by" className={inputCls} value={filters.created_by} onChange={(e) => set("created_by", e.target.value)}>
              <option value="">Anyone</option>{agentOrigins.map((p) => <option key={p.id} value={p.id}>{p.handle}</option>)}
            </select></label>
        ) : null}
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Updated since</span><input aria-label="Updated since" type="date" className={inputCls} value={filters.since} onChange={(e) => set("since", e.target.value)} /></label>
      </div>
      <p className="text-xs text-slate-500">Statuses other than active are shown only to curators. <Badge>unverified</Badge> means extracted or proposed, not vouched for by a person.</p>
      {content}
      {confirmPrivate ? (
        <ConfirmDialog title="Show private memory" confirmLabel="Show and audit" onClose={() => setConfirmPrivate(false)} onConfirm={async () => { set("private", "1"); return null; }}>
          <p className="text-sm">Agents keep working notes and private memories that nobody else is meant to see. Showing them is allowed for workspace administrators, but every such view is recorded in the audit log with your name.</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
