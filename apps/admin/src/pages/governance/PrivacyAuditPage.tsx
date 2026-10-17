import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getToken } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Empty, ErrorState, Loading, PermissionDenied } from "../../components/states";
import { Badge, btn, inputCls } from "../../components/ui";
import { fmt } from "../../lib/format";

interface Item { id: string; at: string; actor_principal_id: string | null; action: string; category: string; resource_type: string | null; resource_id: string | null; outcome: string; metadata: Record<string, unknown> }
interface Page { items: Item[]; next_cursor: string | null; egress: Array<{ purpose: string; classification: string; outcome: string; count: number }> }
const CATEGORIES = ["reveals", "private_reads", "debug_reads", "act_as", "deletions", "holds", "grants", "overrides", "settings", "simulations"] as const;
const LABEL: Record<string, string> = { reveals: "Content revealed", private_reads: "Private memory read", debug_reads: "Debug read", act_as: "Built as another principal", deletions: "Deletions", holds: "Legal holds", grants: "Grants and principals", overrides: "Corrections and overrides", settings: "Settings", simulations: "Ontology simulations" };

async function fetchPage(category: string, cursor?: string): Promise<Page> {
  const qs = new URLSearchParams({ limit: "50" });
  if (category) qs.append("category", category);
  if (cursor) qs.set("cursor", cursor);
  const res = await fetch(`/v1/governance/privacy-audit?${qs}`, { headers: { authorization: `Bearer ${getToken() ?? ""}` } });
  if (!res.ok) throw new Error(String(res.status));
  return (await res.json()) as Page;
}

/** Who looked at what, who changed what, and what went to which model under which decision. Ids and codes only: the page has no content to show. */
export function PrivacyAuditPage() {
  const can = useCan();
  const [category, setCategory] = useState("");
  const list = useInfiniteQuery({ queryKey: ["privacy-audit", category], enabled: can.workspaceAdmin, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => fetchPage(category, pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  if (!can.workspaceAdmin) return <PermissionDenied what="the privacy audit" />;
  if (list.isLoading) return <Loading label="Loading the privacy audit…" />;
  if (list.isError) return <ErrorState onRetry={() => void list.refetch()} />;
  const rows = list.data!.pages.flatMap((p) => p.items);
  const egress = list.data!.pages[0]!.egress;

  async function download() {
    const qs = new URLSearchParams({ format: "csv", limit: "200" });
    if (category) qs.append("category", category);
    const res = await fetch(`/v1/governance/privacy-audit?${qs}`, { headers: { authorization: `Bearer ${getToken() ?? ""}` } });
    const url = URL.createObjectURL(new Blob([await res.text()], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "privacy-audit.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Privacy audit</h1>
        <div className="flex items-end gap-3">
          <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">Category</span>
            <select aria-label="Filter by category" className={inputCls} value={category} onChange={(e) => setCategory(e.target.value)}><option value="">Everything</option>{CATEGORIES.map((c) => <option key={c} value={c}>{LABEL[c]}</option>)}</select></label>
          <button type="button" className={btn.secondary} onClick={() => void download()}>Export CSV</button>
        </div>
      </div>
      <section aria-label="Sent to models" className="space-y-1">
        <h2 className="font-medium">Sent to models</h2>
        {egress.length === 0 ? <p className="text-sm text-slate-500">No model calls recorded.</p> : (
          <table className="w-full text-left text-sm" aria-label="Model calls by purpose">
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">Purpose</th><th className="pr-4">Class</th><th className="pr-4">Outcome</th><th>Calls</th></tr></thead>
            <tbody>{egress.map((e) => <tr key={`${e.purpose}:${e.classification}:${e.outcome}`} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4 font-mono text-xs">{e.purpose}</td><td className="pr-4">{e.classification}</td><td className="pr-4"><Badge tone={e.outcome === "success" ? "green" : e.outcome === "egress_denied" ? "red" : "amber"}>{e.outcome.replace(/_/g, " ")}</Badge></td><td>{e.count}</td></tr>)}</tbody>
          </table>
        )}
      </section>
      <section aria-label="Timeline" className="space-y-1">
        <h2 className="font-medium">Timeline</h2>
        {rows.length === 0 ? <Empty title="Nothing recorded in this category" /> : (
          <table className="w-full text-left text-sm" aria-label="Privacy timeline">
            <thead className="text-slate-500"><tr><th className="py-1 pr-4">When</th><th className="pr-4">What</th><th className="pr-4">Who</th><th>About</th></tr></thead>
            <tbody>{rows.map((i) => (
              <tr key={i.id} className="border-t border-slate-200 align-top dark:border-slate-800">
                <td className="whitespace-nowrap py-1 pr-4">{fmt(i.at)}</td>
                <td className="pr-4"><Badge>{LABEL[i.category] ?? i.category}</Badge> <span className="font-mono text-xs">{i.action}</span></td>
                <td className="pr-4 font-mono text-xs">{i.actor_principal_id ? `${i.actor_principal_id.slice(0, 8)}…` : "system"}</td>
                <td className="font-mono text-xs">{i.resource_type ?? ""} {i.resource_id ? `${String(i.resource_id).slice(0, 8)}…` : ""}</td>
              </tr>))}</tbody>
          </table>
        )}
        {list.hasNextPage ? <button type="button" onClick={() => void list.fetchNextPage()} className="mt-2 text-sm underline">Load more</button> : null}
      </section>
    </div>
  );
}
