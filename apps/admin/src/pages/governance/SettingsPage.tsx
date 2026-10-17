import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { Badge, Banner, btn, ClassificationBadge, Field, inputCls } from "../../components/ui";
import { fmt, newKey } from "../../lib/format";

type Settings = components["schemas"]["WorkspaceSettings"];
const CLASSES = ["public", "internal", "confidential", "restricted", "local_only"] as const;
const RETENTION: Array<[keyof NonNullable<Settings["retention"]>, string, string]> = [
  ["raw_content_days", "Raw interaction content", "Deletes the original text after N days. The normalized form stays for replay."],
  ["normalized_content_days", "Normalized content", "After this, an interaction can no longer be replayed."],
  ["episode_content_days", "Graph episode text", "Blanks the text stored on the graph's episode nodes; the nodes stay as provenance."],
  ["context_trace_days", "Context traces", "How long the record of why a context was built is kept."],
  ["model_calls_days", "Model call log", "Metadata about calls to models (never their content)."],
  ["memory_archive_after_days", "Unconfirmed memories", "Expires an active memory nobody confirmed for N days. It is kept, not served."],
];
const QUOTAS: Array<[keyof NonNullable<Settings["quotas"]>, string]> = [["requests_per_minute", "Requests per minute"], ["context_tokens_per_day", "Context tokens per day"], ["interactions_per_day", "Interactions per day"]];

const num = (v: string): number | null => (v.trim() === "" ? null : Number.parseInt(v, 10));

export function SettingsPage() {
  const can = useCan();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["settings"],
    enabled: can.workspaceAdmin,
    queryFn: async () => ({ res: await api.GET("/v1/workspaces/current/settings", {}) }),
  });
  const hist = useQuery({ queryKey: ["settings-history"], enabled: can.workspaceAdmin, queryFn: () => api.GET("/v1/workspaces/current/settings/history", {}) });
  const [draft, setDraft] = useState<Settings | null>(null);
  const [message, setMessage] = useState<{ tone: "green" | "red"; text: string } | null>(null);
  const server = q.data?.res.data;
  useEffect(() => { if (server) setDraft(structuredClone(server.settings)); }, [server?.version]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!can.workspaceAdmin) return <PermissionDenied what="workspace settings" />;
  if (q.isLoading || !draft) return q.isError ? <ErrorState onRetry={() => void q.refetch()} /> : <Loading label="Loading settings…" />;
  const blocked = stateForStatus(q.data!.res.response.status, "workspace settings", () => void q.refetch());
  if (blocked || !server) return <>{blocked}</>;
  const denied = new Set(draft.egress?.denied_classifications ?? []);
  const platform = new Set(server.effective_egress.platform_denied);
  const toggle = (c: (typeof CLASSES)[number]) => { const n = new Set(denied); if (n.has(c)) n.delete(c); else n.add(c); setDraft({ ...draft, egress: { denied_classifications: [...n] } }); };
  const dirty = JSON.stringify(draft) !== JSON.stringify(server.settings);
  const effectiveDenied = new Set([...platform, ...denied]);

  async function save() {
    setMessage(null);
    const res = await api.PUT("/v1/workspaces/current/settings", { params: { header: { "Idempotency-Key": newKey(), "If-Match": `W/"rev-${server!.version}"` } as never }, body: draft! });
    if (res.response.ok) {
      await Promise.all([qc.invalidateQueries({ queryKey: ["settings"] }), qc.invalidateQueries({ queryKey: ["settings-history"] })]);
      setMessage({ tone: "green", text: "Saved as a new version." });
      return;
    }
    const code = (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
    setMessage({ tone: "red", text: code === "precondition_failed" ? "Someone else changed the settings. Reload and try again." : code === "validation_failed" ? "One of the values is out of range." : code });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3"><h1 className="text-xl font-semibold">Workspace settings</h1><Badge>version {server.version}</Badge>{server.updated_at ? <span className="text-xs text-slate-500">updated {fmt(server.updated_at)}</span> : null}</div>

      <section aria-label="Retention" className="space-y-3">
        <h2 className="font-medium">Retention</h2>
        <p className="text-sm text-slate-500">Leave a field empty to keep that kind of data. Expiry runs as a deletion with its own receipt and audit entry.</p>
        <div className="grid gap-3 md:grid-cols-2">
          {RETENTION.map(([key, label, hint]) => (
            <Field key={key} label={label} hint={hint}>
              <input aria-label={label} type="number" min={1} className={`${inputCls} w-40`} value={draft.retention?.[key] ?? ""} onChange={(e) => setDraft({ ...draft, retention: { ...draft.retention, [key]: num(e.target.value) } })} />
              <span className="ml-2 text-sm text-slate-500">days</span>
            </Field>
          ))}
          <Field label="Backup retention" hint="Shown on deletion receipts as when backup copies stop holding deleted data.">
            <input aria-label="Backup retention" type="number" min={1} className={`${inputCls} w-40`} value={draft.backup_retention_days ?? ""} onChange={(e) => setDraft({ ...draft, backup_retention_days: num(e.target.value) })} /><span className="ml-2 text-sm text-slate-500">days</span>
          </Field>
        </div>
      </section>

      <section aria-label="Egress" className="space-y-3">
        <h2 className="font-medium">What may be sent to a model</h2>
        <Banner tone="slate">You can only <strong>narrow</strong> this. Classes the platform never sends stay denied whatever you choose; there is no way to allow more here.</Banner>
        <table className="w-full text-left text-sm" aria-label="Egress by classification">
          <thead className="text-slate-500"><tr><th className="py-1 pr-4">Class</th><th className="pr-4">Platform</th><th className="pr-4">Deny for this workspace</th><th>Effective</th></tr></thead>
          <tbody>{CLASSES.map((c) => (
            <tr key={c} className="border-t border-slate-200 dark:border-slate-800">
              <td className="py-1 pr-4"><ClassificationBadge classification={c} /></td>
              <td className="pr-4">{platform.has(c) ? "never sent" : "allowed"}</td>
              <td className="pr-4"><input type="checkbox" aria-label={`Deny ${c}`} checked={platform.has(c) || denied.has(c)} disabled={platform.has(c)} onChange={() => toggle(c)} /></td>
              <td>{effectiveDenied.has(c) ? <Badge tone="red">denied</Badge> : <Badge tone="green">allowed</Badge>}</td>
            </tr>))}</tbody>
        </table>
        <p className="text-xs text-slate-500">A change applies to new work at once on this server and within about half a minute everywhere.</p>
      </section>

      <section aria-label="Quotas" className="space-y-3">
        <h2 className="font-medium">Default quotas for new agents and apps</h2>
        <div className="grid gap-3 md:grid-cols-3">
          {QUOTAS.map(([key, label]) => <Field key={key} label={label}><input aria-label={label} type="number" min={1} className={`${inputCls} w-full`} value={draft.quotas?.[key] ?? ""} onChange={(e) => setDraft({ ...draft, quotas: { ...draft.quotas, [key]: num(e.target.value) } })} /></Field>)}
        </div>
      </section>

      {message ? <Banner tone={message.tone} role={message.tone === "red" ? "alert" : "status"}>{message.text}</Banner> : null}
      <div className="flex gap-2"><button type="button" className={btn.primary} disabled={!dirty} onClick={() => void save()}>Save settings</button><button type="button" className={btn.secondary} disabled={!dirty} onClick={() => setDraft(structuredClone(server.settings))}>Discard changes</button></div>

      <section aria-label="History" className="space-y-2">
        <h2 className="font-medium">History</h2>
        {(hist.data?.data ?? []).length === 0 ? <p className="text-sm text-slate-500">No changes yet.</p> : (
          <ul className="space-y-1 text-sm">{(hist.data?.data ?? []).map((h) => <li key={h.version}>Version {h.version} · {fmt(h.updated_at)}{h.updated_by ? <span className="ml-1 font-mono text-xs text-slate-500">{h.updated_by.slice(0, 8)}…</span> : null}</li>)}</ul>
        )}
      </section>
    </div>
  );
}
