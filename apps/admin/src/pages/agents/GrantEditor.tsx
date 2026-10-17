import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { Banner, btn, Field, inputCls } from "../../components/ui";
import { CLASSIFICATION_LEVELS, GRANT_KINDS, grantProblem, PRIVILEGES, togglePrivilege, type GrantDraft, type GrantKind } from "./lib";

const DESCRIBE: Record<string, string> = {
  read: "see memory of the scope (and, for a person, the entity and its facts)",
  propose: "write a proposal or note into the scope",
  publish: "its shared findings become active without review, when the ontology policy also allows it",
};

/** Looks up entities by name so a subject grant carries an id the operator chose, never one typed from memory. */
function SubjectPicker({ onPick }: { onPick: (id: string) => void }) {
  const [q, setQ] = useState("");
  const results = useQuery({
    queryKey: ["grant-subject-search", q],
    enabled: q.trim().length >= 2,
    queryFn: () => api.GET("/v1/entities", { params: { query: { q: q.trim(), limit: 8 } } }),
  });
  const items = results.data?.data?.items ?? [];
  return (
    <div className="space-y-1">
      <input aria-label="Find a subject" className={`${inputCls} w-full`} placeholder="Find a subject by name…" value={q} onChange={(e) => setQ(e.target.value)} />
      {q.trim().length >= 2 && !results.isLoading && items.length === 0 ? <p className="text-xs text-slate-500">No entity matches.</p> : null}
      <ul className="space-y-0.5">
        {items.map((e) => (
          <li key={e.id}><button type="button" className="text-sm underline" onClick={() => { onPick(e.id); setQ(""); }}>{e.name} <span className="text-slate-500">({e.type})</span></button></li>
        ))}
      </ul>
    </div>
  );
}

export function GrantEditor({ onAdd, submitLabel = "Add grant" }: { onAdd: (g: GrantDraft) => void | Promise<void>; submitLabel?: string }) {
  const [kind, setKind] = useState<GrantKind>("subject");
  const [ref, setRef] = useState("");
  const [privileges, setPrivileges] = useState<("read" | "propose" | "publish")[]>(["read", "propose"]);
  const draft: GrantDraft = { scope_kind: kind, scope_ref: kind === "workspace" ? "*" : ref.trim(), privileges: kind === "classification" ? ["read"] : privileges };
  const problem = ref === "" && kind !== "workspace" ? "name the scope" : grantProblem(draft);
  return (
    <fieldset className="space-y-3 rounded border border-slate-200 p-3 dark:border-slate-800">
      <legend className="px-1 text-sm font-medium">Grant</legend>
      <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
        <Field label="Scope">
          <select aria-label="Grant scope" className={`${inputCls} w-full`} value={kind} onChange={(e) => { setKind(e.target.value as GrantKind); setRef(""); }}>
            {GRANT_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </Field>
        {kind === "workspace" ? (
          <p className="self-end text-sm text-slate-500">workspace:* — publishing to the whole workspace.</p>
        ) : kind === "classification" ? (
          <Field label="Highest classification it may read">
            <select aria-label="Classification ceiling" className={`${inputCls} w-full`} value={ref} onChange={(e) => setRef(e.target.value)}>
              <option value="" disabled>Choose…</option>
              {CLASSIFICATION_LEVELS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Field>
        ) : (
          <Field label={kind === "subject" ? "Subject (entity id, or * for every subject)" : "Project key (or * for every project)"}>
            <input aria-label="Grant reference" className={`${inputCls} w-full font-mono`} value={ref} onChange={(e) => setRef(e.target.value)} />
            {kind === "subject" ? <SubjectPicker onPick={setRef} /> : null}
          </Field>
        )}
      </div>
      {kind !== "classification" ? (
        <div role="group" aria-label="Privileges" className="space-y-1">
          {PRIVILEGES.map((p) => (
            <label key={p} className="flex items-start gap-2 text-sm">
              <input type="checkbox" checked={privileges.includes(p)} onChange={() => setPrivileges((cur) => togglePrivilege(cur, p))} />
              <span><span className="font-mono">{p}</span> <span className="text-slate-500">— {DESCRIBE[p]}</span></span>
            </label>
          ))}
        </div>
      ) : null}
      {problem && ref !== "" ? <Banner tone="amber" role="status">{problem}</Banner> : null}
      <button type="button" className={btn.secondary} disabled={problem !== null} onClick={() => { void onAdd(draft); setRef(""); }}>{submitLabel}</button>
    </fieldset>
  );
}
