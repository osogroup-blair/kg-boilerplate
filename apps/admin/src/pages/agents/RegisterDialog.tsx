import * as Dialog from "@radix-ui/react-dialog";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { api } from "../../api/client";
import { Banner, btn, Field, inputCls } from "../../components/ui";
import { newKey } from "../../lib/format";
import { GrantEditor } from "./GrantEditor";
import { errorCodeOf, HANDLE, type GrantDraft, type Registered } from "./lib";
import { KeyOnce } from "./KeyOnce";

const ERRORS: Record<string, string> = {
  handle_taken: "That handle is already registered in this workspace.",
  validation_failed: "Some fields are not valid; check the handle, grants and quotas.",
  not_found: "A subject you granted does not exist in this workspace.",
  forbidden: "Only workspace admins may register agents.",
  feature_disabled: "Registration through the API is switched off; use `kg principals register`.",
};

const num = (s: string): number | undefined => (s.trim() === "" ? undefined : Math.max(1, Math.floor(Number(s))));

/** Registers an agent or an app. The first key comes back in the answer and is shown once, with a copy button and a warning that it will not be shown again. */
export function RegisterDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<"agent" | "app">("agent");
  const [handle, setHandle] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [disclosure, setDisclosure] = useState<"allowed" | "denied">("allowed");
  const [trusted, setTrusted] = useState(false);
  const [profiles, setProfiles] = useState<string[]>([]);
  const [rpm, setRpm] = useState("");
  const [tokens, setTokens] = useState("");
  const [interactions, setInteractions] = useState("");
  const [grants, setGrants] = useState<GrantDraft[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<Registered | null>(null);

  const profileList = useQuery({ queryKey: ["context-profiles"], queryFn: () => api.GET("/v1/context/profiles", {}) });
  const people = useQuery({ queryKey: ["principals", "owners"], queryFn: () => api.GET("/v1/principals", { params: { query: { limit: 100 } } }) });
  const names = ((profileList.data?.data ?? []) as { name: string }[]).map((p) => p.name);
  const owners = (people.data?.data?.items ?? []).filter((p) => p.kind === "user" && p.status === "active");
  const valid = HANDLE.test(handle) && (name.trim() || handle).length > 0;

  async function submit() {
    setBusy(true);
    setError(null);
    const res = await api.POST("/v1/principals", {
      params: { header: { "Idempotency-Key": newKey() } } as never,
      body: {
        kind,
        handle,
        display_name: name.trim() || handle,
        description,
        owner_principal_id: owner || null,
        trusted_source: kind === "app" ? trusted : false,
        model_disclosure: disclosure,
        allowed_profiles: profiles,
        quotas: { requests_per_minute: num(rpm) ?? null, context_tokens_per_day: num(tokens) ?? null, interactions_per_day: num(interactions) ?? null },
        key_name: "initial",
        key_expires_in_seconds: null,
        grants: grants.map((g) => ({ scope_kind: g.scope_kind, scope_ref: g.scope_ref, privileges: g.privileges })),
      },
    });
    setBusy(false);
    if (res.response.ok && res.data) {
      setDone(res.data);
      await qc.invalidateQueries({ queryKey: ["principals"] });
    } else {
      const code = errorCodeOf(res.error);
      setError(ERRORS[code] ?? code);
    }
  }

  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/30" />
        <Dialog.Content aria-describedby={undefined} className="fixed left-1/2 top-10 max-h-[88vh] w-full max-w-2xl -translate-x-1/2 space-y-4 overflow-y-auto rounded-lg bg-white p-6 shadow-xl dark:bg-slate-900">
          <div className="flex items-center justify-between">
            <Dialog.Title className="text-lg font-semibold">{done ? `Registered ${done.principal.handle}` : "Register an agent or app"}</Dialog.Title>
            <Dialog.Close aria-label="Close" className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-800"><X className="h-4 w-4" /></Dialog.Close>
          </div>
          {done ? (
            <div className="space-y-3">
              <KeyOnce plaintext={done.key.plaintext ?? ""} />
              <p className="text-sm text-slate-500">{done.grants.length} grant{done.grants.length === 1 ? "" : "s"} in place. The agent can use the key from its next request.</p>
              <div className="flex justify-end"><button type="button" className={btn.primary} onClick={onClose}>I have stored the key</button></div>
            </div>
          ) : (
            <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); if (valid) void submit(); }}>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Kind">
                  <select aria-label="Kind" className={`${inputCls} w-full`} value={kind} onChange={(e) => { setKind(e.target.value as "agent" | "app"); setTrusted(false); }}>
                    <option value="agent">agent (proposes, reads through grants)</option>
                    <option value="app">app (writes commands and interactions)</option>
                  </select>
                </Field>
                <Field label="Handle" hint="Letters, digits, . _ @ -">
                  <input aria-label="Handle" className={`${inputCls} w-full font-mono`} value={handle} onChange={(e) => setHandle(e.target.value)} />
                </Field>
                <Field label="Display name"><input aria-label="Display name" className={`${inputCls} w-full`} value={name} onChange={(e) => setName(e.target.value)} /></Field>
                <Field label="Owner">
                  <select aria-label="Owner" className={`${inputCls} w-full`} value={owner} onChange={(e) => setOwner(e.target.value)}>
                    <option value="">No owner</option>
                    {owners.map((o) => <option key={o.id} value={o.id}>{o.handle}</option>)}
                  </select>
                </Field>
              </div>
              <Field label="Description"><textarea aria-label="Description" className={`${inputCls} w-full`} rows={2} maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Policy</legend>
                <Field label="Model disclosure" hint="May content this principal submits be processed by external models? Denied: its model stages are skipped by policy.">
                  <select aria-label="Model disclosure" className={`${inputCls} w-full`} value={disclosure} onChange={(e) => setDisclosure(e.target.value as "allowed" | "denied")}>
                    <option value="allowed">allowed</option>
                    <option value="denied">denied</option>
                  </select>
                </Field>
                {kind === "app" ? (
                  <label className="flex items-start gap-2 text-sm">
                    <input type="checkbox" checked={trusted} onChange={(e) => setTrusted(e.target.checked)} />
                    <span><strong>Trusted source.</strong> Its commands carry authoritative_system authority (a system of record). Otherwise it asserts what a user would.</span>
                  </label>
                ) : null}
                <div role="group" aria-label="Allowed profiles" className="space-y-1">
                  <span className="text-sm text-slate-600 dark:text-slate-300">Allowed context profiles <span className="text-xs text-slate-500">(none ticked: every profile)</span></span>
                  <div className="flex flex-wrap gap-3">
                    {names.map((n) => (
                      <label key={n} className="flex items-center gap-1 text-sm">
                        <input type="checkbox" checked={profiles.includes(n)} onChange={(e) => setProfiles((cur) => (e.target.checked ? [...cur, n] : cur.filter((x) => x !== n)))} /> {n}
                      </label>
                    ))}
                  </div>
                </div>
              </fieldset>
              <fieldset className="grid gap-3 sm:grid-cols-3">
                <legend className="text-sm font-medium">Quotas <span className="text-xs font-normal text-slate-500">(empty: the platform default)</span></legend>
                <Field label="Requests / minute"><input aria-label="Requests per minute" inputMode="numeric" className={`${inputCls} w-full`} value={rpm} onChange={(e) => setRpm(e.target.value)} /></Field>
                <Field label="Context tokens / day"><input aria-label="Context tokens per day" inputMode="numeric" className={`${inputCls} w-full`} value={tokens} onChange={(e) => setTokens(e.target.value)} /></Field>
                <Field label="Interactions / day"><input aria-label="Interactions per day" inputMode="numeric" className={`${inputCls} w-full`} value={interactions} onChange={(e) => setInteractions(e.target.value)} /></Field>
              </fieldset>
              <div className="space-y-2">
                <h3 className="text-sm font-medium">Initial grants</h3>
                {grants.length === 0 ? <p className="text-sm text-slate-500">None yet: the principal sees workspace-wide memory and its own private memory only.</p> : (
                  <ul className="space-y-1" aria-label="Initial grants">
                    {grants.map((g, i) => (
                      <li key={`${g.scope_kind}:${g.scope_ref}`} className="flex items-center justify-between rounded border border-slate-200 px-2 py-1 text-sm dark:border-slate-800">
                        <span><span className="font-mono">{g.scope_kind}:{g.scope_ref}</span> <span className="text-slate-500">[{g.privileges.join(", ")}]</span></span>
                        <button type="button" className={btn.link} onClick={() => setGrants((cur) => cur.filter((_, j) => j !== i))}>remove</button>
                      </li>
                    ))}
                  </ul>
                )}
                <GrantEditor onAdd={(g) => setGrants((cur) => [...cur.filter((x) => !(x.scope_kind === g.scope_kind && x.scope_ref === g.scope_ref)), g])} />
              </div>
              {error ? <Banner tone="red" role="alert">{error}</Banner> : null}
              <div className="flex justify-end gap-2">
                <button type="button" className={btn.secondary} onClick={onClose}>Cancel</button>
                <button type="submit" className={btn.primary} disabled={!valid || busy}>{busy ? "Registering…" : "Register"}</button>
              </div>
            </form>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
