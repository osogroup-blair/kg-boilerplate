import * as Dialog from "@radix-ui/react-dialog";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Badge, Banner, btn } from "../../components/ui";
import { ErrorState, Loading, stateForStatus } from "../../components/states";
import { fmt, newKey } from "../../lib/format";
import { GrantEditor } from "./GrantEditor";
import { KeyOnce } from "./KeyOnce";
import { errorCodeOf } from "./lib";
import { RevokeDialog } from "./RevokeDialog";

type Tab = "overview" | "grants" | "keys" | "usage" | "activity";
const TABS: Tab[] = ["overview", "grants", "keys", "usage", "activity"];
const Row = ({ k, children }: { k: string; children: React.ReactNode }) => (<><dt className="text-slate-500">{k}</dt><dd>{children}</dd></>);

function GrantsTab({ principalId, revoked }: { principalId: string; revoked: boolean }) {
  const can = useCan();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["principal-grants", principalId], queryFn: () => api.GET("/v1/principals/{principal_id}/grants", { params: { path: { principal_id: principalId } } }) });
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.response.status, "grants", () => void q.refetch());
  if (blocked) return <>{blocked}</>;
  const rows = q.data!.data ?? [];
  const refresh = () => qc.invalidateQueries({ queryKey: ["principal-grants", principalId] });
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">A grant takes effect on the principal&apos;s next request. Scopes do not inherit: a project grant reveals nothing that carries only a subject scope.</p>
      {rows.length === 0 ? <p className="text-sm text-slate-500">No grants: workspace-wide memory and its own private memory only.</p> : (
        <table className="w-full text-left text-sm" aria-label="Grants table">
          <thead className="text-slate-500"><tr><th className="py-1 pr-3">Scope</th><th className="pr-3">Privileges</th><th /></tr></thead>
          <tbody>
            {rows.map((g) => (
              <tr key={g.id} className="border-t border-slate-200 dark:border-slate-800">
                <td className="py-1 pr-3 font-mono text-xs">{g.scope_kind}:{g.scope_ref}</td>
                <td className="pr-3">{g.privileges.map((p) => <Badge key={p} tone={p === "publish" ? "amber" : "slate"}>{p}</Badge>)}</td>
                <td className="text-right">
                  {can.managePrincipals && !revoked ? (
                    <button type="button" className={btn.link} aria-label={`Remove grant ${g.scope_kind}:${g.scope_ref}`} onClick={async () => {
                      const res = await api.DELETE("/v1/principals/{principal_id}/grants/{grant_id}", { params: { path: { principal_id: principalId, grant_id: g.id }, header: { "Idempotency-Key": newKey() } } as never });
                      if (res.response.ok) await refresh(); else setError(errorCodeOf(res.error));
                    }}>remove</button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error ? <Banner tone="red" role="alert">{error}</Banner> : null}
      {can.managePrincipals && !revoked ? (
        <GrantEditor onAdd={async (g) => {
          setError(null);
          const res = await api.POST("/v1/principals/{principal_id}/grants", { params: { path: { principal_id: principalId }, header: { "Idempotency-Key": newKey() } } as never, body: { scope_kind: g.scope_kind, scope_ref: g.scope_ref, privileges: g.privileges } });
          if (res.response.ok) await refresh(); else setError(res.response.status === 404 ? "That subject does not exist in this workspace." : errorCodeOf(res.error));
        }} />
      ) : null}
    </div>
  );
}

function KeysTab({ principalId, revoked }: { principalId: string; revoked: boolean }) {
  const can = useCan();
  const qc = useQueryClient();
  const [minted, setMinted] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["principal-keys", principalId], queryFn: () => api.GET("/v1/principals/{principal_id}/keys", { params: { path: { principal_id: principalId } } }) });
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const rows = q.data?.data ?? [];
  const refresh = () => qc.invalidateQueries({ queryKey: ["principal-keys", principalId] });
  return (
    <div className="space-y-3">
      {minted ? <KeyOnce plaintext={minted} /> : null}
      <table className="w-full text-left text-sm" aria-label="Keys table">
        <thead className="text-slate-500"><tr><th className="py-1 pr-3">Name</th><th className="pr-3">Prefix</th><th className="pr-3">Status</th><th className="pr-3">Last used</th><th /></tr></thead>
        <tbody>
          {rows.map((k) => (
            <tr key={k.id} className="border-t border-slate-200 dark:border-slate-800">
              <td className="py-1 pr-3">{k.name || "—"}</td>
              <td className="pr-3 font-mono text-xs">{k.prefix}…</td>
              <td className="pr-3">{k.revoked_at ? <Badge tone="red">revoked</Badge> : k.expires_at ? <Badge tone="amber">expires {fmt(k.expires_at)}</Badge> : <Badge tone="green">active</Badge>}</td>
              <td className="pr-3">{fmt(k.last_used_at)}</td>
              <td className="text-right">
                {can.managePrincipals && !k.revoked_at ? (
                  <button type="button" className={btn.link} aria-label={`Revoke key ${k.prefix}`} onClick={async () => {
                    const res = await api.DELETE("/v1/principals/{principal_id}/keys/{key_id}", { params: { path: { principal_id: principalId, key_id: k.id }, header: { "Idempotency-Key": newKey() } } as never });
                    if (res.response.ok) await refresh(); else setError(errorCodeOf(res.error));
                  }}>revoke</button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error ? <Banner tone="red" role="alert">{error}</Banner> : null}
      {can.managePrincipals && !revoked ? (
        <form className="flex items-end gap-2" onSubmit={async (e) => {
          e.preventDefault();
          setError(null);
          const res = await api.POST("/v1/principals/{principal_id}/keys", { params: { path: { principal_id: principalId }, header: { "Idempotency-Key": newKey() } } as never, body: { name } });
          if (res.response.ok && res.data) { setMinted(res.data.plaintext ?? null); setName(""); await refresh(); } else setError(errorCodeOf(res.error));
        }}>
          <label className="text-sm"><span className="mb-1 block text-slate-600 dark:text-slate-300">New key name</span><input aria-label="New key name" className="rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} /></label>
          <button type="submit" className={btn.secondary}>Mint key</button>
        </form>
      ) : null}
    </div>
  );
}

function Bar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return <div className="h-2 w-full rounded bg-slate-100 dark:bg-slate-800" role="img" aria-label={`${value} of ${max}`}><div className="h-2 rounded bg-sky-500" style={{ width: `${pct}%` }} /></div>;
}

function UsageTab({ principalId }: { principalId: string }) {
  const q = useQuery({ queryKey: ["principal-usage", principalId], queryFn: () => api.GET("/v1/principals/{principal_id}/usage", { params: { path: { principal_id: principalId } } }) });
  if (q.isLoading) return <Loading />;
  if (q.isError || !q.data?.data) return <ErrorState onRetry={() => void q.refetch()} />;
  const u = q.data.data;
  const days = [{ day: u.day, requests: u.requests, context_tokens: u.context_tokens, context_builds: u.context_builds, interactions: u.interactions, proposals: u.proposals }, ...u.history];
  const max = Math.max(1, ...days.map((d) => d.context_tokens));
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-[12rem_1fr] gap-y-2 text-sm">
        <Row k="Today (UTC)">{u.day}</Row>
        <Row k="Requests">{u.requests}{u.quotas.requests_per_minute ? <span className="text-slate-500"> · limit {u.quotas.requests_per_minute}/min</span> : <span className="text-slate-500"> · default {u.defaults.requests_per_minute}/min</span>}</Row>
        <Row k="Context builds">{u.context_builds}</Row>
        <Row k="Context tokens">{u.context_tokens}{u.quotas.context_tokens_per_day ? <span className="text-slate-500"> of {u.quotas.context_tokens_per_day}</span> : <span className="text-slate-500"> · no daily limit</span>}</Row>
        <Row k="Interactions">{u.interactions}{u.quotas.interactions_per_day ? <span className="text-slate-500"> of {u.quotas.interactions_per_day}</span> : <span className="text-slate-500"> · no daily limit</span>}</Row>
        <Row k="Proposals">{u.proposals}</Row>
      </dl>
      <section aria-label="Daily context tokens" className="space-y-1">
        <h3 className="text-sm font-medium">Context tokens per day</h3>
        {days.map((d) => (
          <div key={d.day} className="grid grid-cols-[6rem_1fr_5rem] items-center gap-2 text-xs">
            <span className="font-mono">{d.day}</span><Bar value={d.context_tokens} max={max} /><span className="text-right">{d.context_tokens}</span>
          </div>
        ))}
      </section>
    </div>
  );
}

function ActivityTab({ principalId }: { principalId: string }) {
  const q = useQuery({ queryKey: ["principal-activity", principalId], queryFn: () => api.GET("/v1/principals/{principal_id}/activity", { params: { path: { principal_id: principalId } } }) });
  if (q.isLoading) return <Loading />;
  if (q.isError || !q.data?.data) return <ErrorState onRetry={() => void q.refetch()} />;
  const a = q.data.data;
  const counts = (m: Record<string, number>) => Object.entries(m).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ") || "none";
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">The last {a.window_days} days. Counts and ids only: never memory content from a private scope.</p>
      <dl className="grid grid-cols-[12rem_1fr] gap-y-2 text-sm">
        <Row k="Last seen">{fmt(a.last_seen_at)}</Row>
        <Row k="Interactions">{counts(a.interactions)}</Row>
        <Row k="Proposals">{counts(a.proposals)}</Row>
        <Row k="Published findings">{a.published_findings} <span className="text-slate-500">({a.verified_findings} verified)</span></Row>
        <Row k="Context builds">{a.context_builds} <span className="text-slate-500">· {a.context_tokens} tokens · {counts(a.context_profiles)}</span></Row>
        <Row k="Denials">{a.denials}</Row>
      </dl>
    </div>
  );
}

export function PrincipalDrawer({ principalId, onClose }: { principalId: string; onClose: () => void }) {
  const can = useCan();
  const [tab, setTab] = useState<Tab>("overview");
  const [revoking, setRevoking] = useState(false);
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
      const revoked = p.status === "revoked";
      const registrable = p.kind === "agent" || p.kind === "app";
      body = (
        <div className="space-y-4">
          {revoked ? <Banner tone="red" role="status">Revoked {p.revoke_reason ? `(${p.revoke_reason})` : ""} {fmt(p.revoked_at)}. Its keys no longer work.</Banner> : null}
          <div role="tablist" aria-label="Principal sections" className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800">
            {TABS.filter((t) => registrable || t === "overview").map((t) => (
              <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)} className={`px-3 py-1.5 text-sm capitalize ${tab === t ? "border-b-2 border-slate-900 font-medium dark:border-slate-100" : "text-slate-500"}`}>{t}</button>
            ))}
          </div>
          {tab === "overview" ? (
            <dl className="grid grid-cols-[9rem_1fr] gap-y-2 text-sm">
              <Row k="Handle"><span className="font-mono">{p.handle}</span></Row>
              <Row k="Name">{p.display_name}</Row>
              <Row k="Description">{p.description || "—"}</Row>
              <Row k="Kind">{p.kind}{p.trusted_source ? <> <Badge tone="blue" title="Its commands carry authoritative_system authority">trusted source</Badge></> : null}</Row>
              <Row k="Status">{p.status}</Row>
              <Row k="Roles">{p.roles.join(", ") || "—"}</Row>
              <Row k="Model disclosure">{p.model_disclosure}</Row>
              <Row k="Profiles">{(p.allowed_profiles ?? []).length ? (p.allowed_profiles ?? []).join(", ") : "every profile"}</Row>
              <Row k="Quotas">{p.quota_requests_per_minute ?? "default"}/min · {p.quota_context_tokens_per_day ?? "no"} tokens/day · {p.quota_interactions_per_day ?? "no"} interactions/day</Row>
              <Row k="Created">{fmt(p.created_at)}</Row>
            </dl>
          ) : tab === "grants" ? <GrantsTab principalId={principalId} revoked={revoked} />
            : tab === "keys" ? <KeysTab principalId={principalId} revoked={revoked} />
            : tab === "usage" ? <UsageTab principalId={principalId} />
            : <ActivityTab principalId={principalId} />}
          {can.managePrincipals && registrable && !revoked ? (
            <div className="border-t border-slate-200 pt-3 dark:border-slate-800"><button type="button" className={btn.danger} onClick={() => setRevoking(true)}>Revoke…</button></div>
          ) : null}
          {revoking ? <RevokeDialog principalId={principalId} handle={p.handle} onClose={() => setRevoking(false)} onDone={() => { setRevoking(false); void q.refetch(); }} /> : null}
        </div>
      );
    }
  }
  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/30" />
        <Dialog.Content aria-describedby={undefined} className="fixed right-0 top-0 h-full w-full max-w-xl overflow-y-auto bg-white p-6 shadow-xl dark:bg-slate-900">
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
