import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { useCan } from "../auth/permissions";
import { Empty, ErrorState, Loading, stateForStatus } from "../components/states";
import { Badge, btn } from "../components/ui";
import { fmt } from "../lib/format";
import { PrincipalDrawer } from "./agents/PrincipalDrawer";
import { RegisterDialog } from "./agents/RegisterDialog";

type Principal = components["schemas"]["PrincipalOut"];

/** Agents & Access (phase 7): who may do what. Workspace admins register, grant, rotate keys and revoke; nobody else sees the controls (the API refuses them anyway). */
export function AgentsAccess() {
  const can = useCan();
  const navigate = useNavigate();
  const { principalId } = useParams();
  const [registering, setRegistering] = useState(false);
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
          <table className="w-full text-left text-sm" aria-label="Principals table">
            <thead className="text-slate-500">
              <tr><th className="py-2 pr-4">Handle</th><th className="pr-4">Kind</th><th className="pr-4">Roles</th><th className="pr-4">Status</th><th className="pr-4">Active keys</th><th>Created</th></tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                  <td className="py-2 pr-4"><button type="button" onClick={() => navigate(`/agents/${p.id}`)} className="font-mono underline-offset-2 hover:underline">{p.handle}</button></td>
                  <td className="pr-4">{p.kind}{p.trusted_source ? <> <Badge tone="blue" title="Trusted source">trusted</Badge></> : null}</td>
                  <td className="pr-4">{p.roles.join(", ")}</td>
                  <td className="pr-4">{p.status === "revoked" ? <Badge tone="red" title={p.revoke_reason ?? undefined}>revoked{p.revoke_reason ? ` · ${p.revoke_reason}` : ""}</Badge> : p.status}</td>
                  <td className="pr-4 font-mono text-xs">{p.keys.filter((k) => !k.revoked_at).map((k) => k.prefix).join(", ") || "—"}</td>
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
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Agents &amp; Access</h1>
        {can.managePrincipals ? <button type="button" className={btn.primary} onClick={() => setRegistering(true)}>Register agent or app</button> : null}
      </div>
      <p className="text-sm text-slate-500">Each agent or app has its own identity, its own grants (read, propose, publish) and its own quotas. A principal with no grants sees workspace-wide memory and its own private memory only.</p>
      {content}
      {principalId ? <PrincipalDrawer principalId={principalId} onClose={() => navigate("/agents")} /> : null}
      {registering ? <RegisterDialog onClose={() => setRegistering(false)} /> : null}
    </div>
  );
}
