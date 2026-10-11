import * as Dialog from "@radix-ui/react-dialog";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { Empty, ErrorState, Loading, stateForStatus } from "../components/states";

type Principal = components["schemas"]["PrincipalOut"];

const fmt = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

function PrincipalDetail({ principalId, onClose }: { principalId: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["principal", principalId], queryFn: () => api.GET("/v1/principals/{principal_id}", { params: { path: { principal_id: principalId } } }), retry: false });
  let body;
  if (q.isLoading) body = <Loading />;
  else if (q.isError) body = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const res = q.data!;
    // A foreign or unknown id is the same "Not found" state (the API answers both with an identical 404).
    body = stateForStatus(res.response.status, "This principal", () => void q.refetch());
    if (!body && res.data) {
      const p = res.data;
      body = (
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2 text-sm">
          <dt className="text-slate-500">Handle</dt><dd className="font-mono">{p.handle}</dd>
          <dt className="text-slate-500">Name</dt><dd>{p.display_name}</dd>
          <dt className="text-slate-500">Kind</dt><dd>{p.kind}</dd>
          <dt className="text-slate-500">Status</dt><dd>{p.status}</dd>
          <dt className="text-slate-500">Roles</dt><dd>{p.roles.join(", ") || "—"}</dd>
          <dt className="text-slate-500">Created</dt><dd>{fmt(p.created_at)}</dd>
          <dt className="text-slate-500">Revoked</dt><dd>{fmt(p.revoked_at)}</dd>
          <dt className="text-slate-500">API keys</dt>
          <dd>
            {p.keys.length === 0 ? "none" : (
              <ul className="space-y-1">
                {p.keys.map((k) => (
                  <li key={k.prefix} className="font-mono text-xs">{k.prefix}… {k.revoked_at ? "(revoked)" : "(active)"} · last used {fmt(k.last_used_at)}</li>
                ))}
              </ul>
            )}
          </dd>
        </dl>
      );
    }
  }
  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/30" />
        <Dialog.Content aria-describedby={undefined} className="fixed right-0 top-0 h-full w-full max-w-md overflow-y-auto bg-white p-6 shadow-xl dark:bg-slate-900">
          <div className="mb-4 flex items-center justify-between">
            <Dialog.Title className="text-lg font-semibold">Principal</Dialog.Title>
            <Dialog.Close aria-label="Close" className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-800"><X className="h-4 w-4" /></Dialog.Close>
          </div>
          {body}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function AgentsAccess() {
  const navigate = useNavigate();
  const { principalId } = useParams();
  const q = useInfiniteQuery({
    queryKey: ["principals"],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.GET("/v1/principals", { params: { query: { limit: 100, cursor: pageParam } } }),
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
  });

  let content;
  if (q.isLoading) content = <Loading label="Loading principals…" />;
  else if (q.isError) content = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const first = q.data!.pages[0]!;
    const blocked = stateForStatus(first.response.status, "principals", () => void q.refetch());
    const rows: Principal[] = q.data!.pages.flatMap((p) => p.data?.items ?? []);
    if (blocked) content = blocked;
    else if (rows.length === 0) content = <Empty title="No principals" />;
    else
      content = (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-slate-500">
              <tr><th className="py-2 pr-4">Handle</th><th className="pr-4">Kind</th><th className="pr-4">Roles</th><th className="pr-4">Status</th><th className="pr-4">Key prefixes</th><th>Created</th></tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                  <td className="py-2 pr-4"><button type="button" onClick={() => navigate(`/agents/${p.id}`)} className="font-mono underline-offset-2 hover:underline">{p.handle}</button></td>
                  <td className="pr-4">{p.kind}</td>
                  <td className="pr-4">{p.roles.join(", ")}</td>
                  <td className="pr-4">{p.status}</td>
                  <td className="pr-4 font-mono text-xs">{p.keys.map((k) => k.prefix).join(", ") || "—"}</td>
                  <td className="whitespace-nowrap">{fmt(p.created_at)}</td>
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
      <h1 className="text-xl font-semibold">Agents &amp; Access</h1>
      <p className="text-sm text-slate-500">Read-only in this phase. Registration, scopes and revocation arrive with the multi-agent phase.</p>
      {content}
      {principalId ? <PrincipalDetail principalId={principalId} onClose={() => navigate("/agents")} /> : null}
    </div>
  );
}
