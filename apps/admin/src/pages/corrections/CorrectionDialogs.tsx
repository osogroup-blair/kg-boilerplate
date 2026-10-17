import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Badge, Banner, Field, inputCls } from "../../components/ui";
import { CORRECTION_REASONS, newKey } from "../../lib/format";

type Preview = components["schemas"]["PreviewOut"];
type CorrectionOut = components["schemas"]["CorrectionOut"];

/** The If-Match the API expects for a record's revision. */
export const revTag = (revision: number): string => `W/"rev-${revision}"`;

const ERRORS: Record<string, string> = {
  precondition_failed: "The record changed since you opened it. Reload and try again.",
  precondition_required: "The record's revision is missing. Reload and try again.",
  not_found: "That record no longer exists, or you cannot see it.",
  invalid_transition: "The record is not in a state this correction applies to.",
  forbidden: "Your role may not correct records.",
  validation_failed: "Some of what you entered is not valid.",
  idempotency_mismatch: "This request was already sent with different content. Reload and try again.",
  subject_suppressed: "That identifier belongs to a person whose data was deleted; it cannot be used again.",
  not_splittable: "That entity cannot be split (it was merged, or nothing was chosen to move).",
};

/** What a correction would change, counted before anything is written. A correction of an authoritative source's fact says so in red: it needs a person to own that. */
export function Impact({ p }: { p: Preview }) {
  const memories = Object.entries(p.dependent_memory_counts);
  return (
    <section aria-label="Impact preview" className="space-y-2 rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
      <h3 className="font-medium">What this will change</h3>
      {p.authoritative ? <Banner tone="red" role="alert">This fact came from an <strong>authoritative system</strong>. Correcting it overrides that source; a review item records the override so its owner is told.</Banner> : null}
      <ul className="list-disc space-y-0.5 pl-5">
        {p.graph_operations.length ? <li>Graph: {p.graph_operations.map((o) => o.replace(/_/g, " ")).join(", then ")}.</li> : <li>No graph change.</li>}
        {p.predecessor_fact_ids.length ? <li>{p.predecessor_fact_ids.length} earlier fact{p.predecessor_fact_ids.length === 1 ? "" : "s"} it replaced would become current again.</li> : null}
        {p.sibling_fact_ids.length ? <li>{p.sibling_fact_ids.length} other fact{p.sibling_fact_ids.length === 1 ? "" : "s"} from the same source conversation stay as they are.</li> : null}
        {memories.length ? <li>Memories that rest on it are disputed: {memories.map(([k, n]) => `${n} ${k}`).join(", ")}.</li> : <li>No memory rests on it.</li>}
        {p.mention_count != null ? <li>{p.mention_count} mention{p.mention_count === 1 ? "" : "s"} of this entity.</li> : null}
        {p.open_review_ids.length ? <li>{p.open_review_ids.length} open review item{p.open_review_ids.length === 1 ? "" : "s"} about it would be closed.</li> : null}
        {p.grant_holders.length ? <li>{p.grant_holders.length} principal{p.grant_holders.length === 1 ? "" : "s"} can read it and will see the corrected version.</li> : null}
        {p.recent_context_count != null ? <li>{p.recent_context_count} recent context build{p.recent_context_count === 1 ? "" : "s"} included it; their links will leave it out from now on.</li> : null}
      </ul>
    </section>
  );
}

function usePreview(path: "relationships" | "entities" | "memories", id: string, kind: string) {
  return useQuery({
    queryKey: ["correction-preview", path, id, kind],
    enabled: !!kind,
    queryFn: async () => {
      const params = { path: { [path === "relationships" ? "fact_id" : path === "entities" ? "entity_id" : "memory_id"]: id }, query: { kind } } as never;
      const res =
        path === "relationships"
          ? await api.GET("/v1/relationships/{fact_id}/corrections/preview", { params })
          : path === "entities"
            ? await api.GET("/v1/entities/{entity_id}/corrections/preview", { params })
            : await api.GET("/v1/memories/{memory_id}/corrections/preview", { params });
      return res.data as Preview | undefined;
    },
  });
}

function Outcome({ out }: { out: CorrectionOut }) {
  return (
    <Banner tone="green" role="status">
      <p>Done. {out.projection_status === "projected" ? "The graph is up to date." : "The graph update is still pending and will finish shortly."}</p>
      <ul className="mt-1 list-disc pl-5 text-xs">
        {out.reopened_fact_ids.length ? <li>{out.reopened_fact_ids.length} earlier fact(s) are current again.</li> : null}
        {out.dependent_memory_ids.length ? <li>{out.dependent_memory_ids.length} memory(ies) that rested on it were disputed.</li> : null}
        {out.authoritative_override ? <li>An authoritative source was overridden: a review item records it.</li> : null}
        {out.new_target_id ? <li><Link className="underline" to={`/entities/${out.new_target_id}`}>The new record</Link></li> : null}
        {Object.entries(out.moved).map(([k, n]) => <li key={k}>{n} {k} moved.</li>)}
      </ul>
      <p className="mt-1 text-xs"><Link className="underline" to={`/commands/${out.command_id}`}>The correction command</Link> is in the ledger.</p>
    </Banner>
  );
}

function ReasonFields({ reason, setReason, note, setNote }: { reason: string; setReason: (v: string) => void; note: string; setNote: (v: string) => void }) {
  return (
    <>
      <Field label="Reason" hint="Recorded in the audit log with your name, as a code.">
        <select aria-label="Reason" className={`${inputCls} w-full`} value={reason} onChange={(e) => setReason(e.target.value)}>
          <option value="" disabled>Choose a reason…</option>
          {CORRECTION_REASONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
        </select>
      </Field>
      <Field label="Note (optional)" hint="Kept with the correction; never copied to the audit log.">
        <textarea aria-label="Note" className={`${inputCls} w-full`} rows={2} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
    </>
  );
}

async function send(kind: "fact" | "entity" | "memory", id: string, revision: number, body: Record<string, unknown>): Promise<{ out?: CorrectionOut; error?: string }> {
  const params = { path: { [kind === "fact" ? "fact_id" : kind === "entity" ? "entity_id" : "memory_id"]: id }, header: { "Idempotency-Key": newKey(), "If-Match": revTag(revision) } } as never;
  const res =
    kind === "fact"
      ? await api.POST("/v1/relationships/{fact_id}/corrections", { params, body: body as never })
      : kind === "entity"
        ? await api.POST("/v1/entities/{entity_id}/corrections", { params, body: body as never })
        : await api.POST("/v1/memories/{memory_id}/corrections", { params, body: body as never });
  if (res.response.ok && res.data) return { out: res.data as CorrectionOut };
  const code = (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
  return { error: ERRORS[code] ?? code };
}

const FACT_KINDS = [
  { kind: "reject", label: "Reject it (it was never true)" },
  { kind: "supersede", label: "Replace it with a corrected fact" },
  { kind: "end", label: "End it (it was true, and stopped)" },
  { kind: "restore", label: "Restore it (a rejected fact was right)" },
] as const;

export function CorrectFactDialog({ fact, onClose }: { fact: { id: string; revision: number; status: string; rel_type: string }; onClose: () => void }) {
  const qc = useQueryClient();
  const available = FACT_KINDS.filter((k) => (k.kind === "restore" ? fact.status === "rejected" : fact.status !== "rejected"));
  const [kind, setKind] = useState<string>(available[0]?.kind ?? "");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [reopen, setReopen] = useState(true);
  const [out, setOut] = useState<CorrectionOut | null>(null);
  const preview = usePreview("relationships", fact.id, kind);
  const missing = !reason || (kind === "end" && !to);
  return (
    <ConfirmDialog title={`Correct this ${fact.rel_type} fact`} confirmLabel="Apply correction" cancelLabel={out ? "Close" : "Cancel"} closeOnSuccess={false} disabled={missing || !!out} onClose={onClose}
      onConfirm={async () => {
        const body: Record<string, unknown> = { kind, reason_code: reason, reason_text: note.trim() || null };
        if (kind === "supersede") Object.assign(body, { valid_from: from || null, valid_to: to || null });
        if (kind === "end") body.valid_to = to;
        if (kind === "reject") body.reopen_predecessors = reopen;
        const r = await send("fact", fact.id, fact.revision, body);
        if (r.error) return r.error;
        setOut(r.out ?? null);
        await Promise.all([qc.invalidateQueries({ queryKey: ["entity"] }), qc.invalidateQueries({ queryKey: ["commands"] }), qc.invalidateQueries({ queryKey: ["reviews"] })]);
        return null;
      }}>
      <fieldset className="space-y-1">
        <legend className="sr-only">Kind of correction</legend>
        {available.map((k) => <label key={k.kind} className="flex items-center gap-2 text-sm"><input type="radio" name="fact-correction" checked={kind === k.kind} onChange={() => setKind(k.kind)} /> {k.label}</label>)}
      </fieldset>
      {kind === "reject" ? <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={reopen} onChange={(e) => setReopen(e.target.checked)} /> Make the facts it replaced current again</label> : null}
      {kind === "supersede" || kind === "end" ? (
        <div className="grid grid-cols-2 gap-3">
          {kind === "supersede" ? <Field label="Valid from" hint="Leave blank to keep the original."><input aria-label="Valid from" type="date" className={`${inputCls} w-full`} value={from} onChange={(e) => setFrom(e.target.value)} /></Field> : null}
          <Field label={kind === "end" ? "Valid to" : "Valid to (optional)"}><input aria-label="Valid to" type="date" className={`${inputCls} w-full`} value={to} onChange={(e) => setTo(e.target.value)} /></Field>
        </div>
      ) : null}
      <ReasonFields reason={reason} setReason={setReason} note={note} setNote={setNote} />
      {preview.isLoading ? <p className="text-xs text-slate-500">Counting what this would change…</p> : preview.data ? <Impact p={preview.data} /> : null}
      {out ? <Outcome out={out} /> : null}
    </ConfirmDialog>
  );
}

export function CorrectEntityDialog({ entity, attributes, onClose }: { entity: { id: string; revision: number; name: string }; attributes: string[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<"rename" | "correct_attribute">("rename");
  const [name, setName] = useState(entity.name);
  const [attribute, setAttribute] = useState(attributes[0] ?? "");
  const [value, setValue] = useState("");
  const [remove, setRemove] = useState(false);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [out, setOut] = useState<CorrectionOut | null>(null);
  const preview = usePreview("entities", entity.id, kind);
  const missing = !reason || (kind === "rename" ? !name.trim() || name.trim() === entity.name : !attribute || (!remove && value === ""));
  return (
    <ConfirmDialog title={`Correct ${entity.name}`} confirmLabel="Apply correction" cancelLabel={out ? "Close" : "Cancel"} closeOnSuccess={false} disabled={missing || !!out} onClose={onClose}
      onConfirm={async () => {
        const body: Record<string, unknown> = { kind, reason_code: reason, reason_text: note.trim() || null };
        if (kind === "rename") body.name = name.trim();
        else Object.assign(body, { attribute, value: remove ? null : value });
        const r = await send("entity", entity.id, entity.revision, body);
        if (r.error) return r.error;
        setOut(r.out ?? null);
        await Promise.all([qc.invalidateQueries({ queryKey: ["entity"] }), qc.invalidateQueries({ queryKey: ["entities"] }), qc.invalidateQueries({ queryKey: ["commands"] })]);
        return null;
      }}>
      <fieldset className="space-y-1">
        <legend className="sr-only">Kind of correction</legend>
        <label className="flex items-center gap-2 text-sm"><input type="radio" name="entity-correction" checked={kind === "rename"} onChange={() => setKind("rename")} /> Rename it (the id stays; the old name becomes an alias)</label>
        <label className="flex items-center gap-2 text-sm"><input type="radio" name="entity-correction" checked={kind === "correct_attribute"} onChange={() => setKind("correct_attribute")} /> Correct an attribute</label>
      </fieldset>
      {kind === "rename" ? (
        <Field label="Correct name"><input aria-label="Correct name" className={`${inputCls} w-full`} value={name} onChange={(e) => setName(e.target.value)} /></Field>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Attribute"><input aria-label="Attribute" list="entity-attributes" className={`${inputCls} w-full`} value={attribute} onChange={(e) => setAttribute(e.target.value)} /><datalist id="entity-attributes">{attributes.map((a) => <option key={a} value={a} />)}</datalist></Field>
          <Field label="Correct value"><input aria-label="Correct value" className={`${inputCls} w-full`} value={value} disabled={remove} onChange={(e) => setValue(e.target.value)} /></Field>
          <label className="col-span-2 flex items-center gap-2 text-sm"><input type="checkbox" checked={remove} onChange={(e) => setRemove(e.target.checked)} /> Remove the attribute instead</label>
        </div>
      )}
      <ReasonFields reason={reason} setReason={setReason} note={note} setNote={setNote} />
      {preview.data ? <Impact p={preview.data} /> : null}
      {out ? <Outcome out={out} /> : null}
    </ConfirmDialog>
  );
}

/** A wrong identity link: the choices below move to a NEW person (their own id), and the old person keeps the rest. Nothing is guessed; the preview counts what moves. */
export function SplitEntityDialog({ entity, onClose }: { entity: { id: string; revision: number; name: string; type: string; externalIds: Array<{ system: string; value: string }> }; onClose: () => void }) {
  const qc = useQueryClient();
  const mentions = useQuery({ queryKey: ["entity-mentions", entity.id], queryFn: () => api.GET("/v1/entities/{entity_id}/mentions", { params: { path: { entity_id: entity.id } } }) });
  const [newName, setNewName] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [ext, setExt] = useState<Set<string>>(new Set());
  const [reextract, setReextract] = useState(false);
  const [reason, setReason] = useState("wrong_identity");
  const [note, setNote] = useState("");
  const [out, setOut] = useState<CorrectionOut | null>(null);
  const preview = usePreview("entities", entity.id, "split");
  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, v: string) => { const n = new Set(set); if (n.has(v)) n.delete(v); else n.add(v); setter(n); };
  const items = mentions.data?.data ?? [];
  const nothing = picked.size === 0 && ext.size === 0;
  return (
    <ConfirmDialog title={`Split ${entity.name}`} confirmLabel="Split into a new person" cancelLabel={out ? "Close" : "Cancel"} closeOnSuccess={false} disabled={!newName.trim() || nothing || !reason || !!out} onClose={onClose}
      onConfirm={async () => {
        const body = {
          kind: "split", reason_code: reason, reason_text: note.trim() || null, reextract,
          new_entity: { display_name: newName.trim(), type: entity.type, aliases: [] },
          move: { mention_ids: [...picked], fact_ids: [], memory_ids: [], external_ids: entity.externalIds.filter((x) => ext.has(`${x.system}:${x.value}`)) },
        };
        const r = await send("entity", entity.id, entity.revision, body);
        if (r.error) return r.error;
        setOut(r.out ?? null);
        await Promise.all([qc.invalidateQueries({ queryKey: ["entity"] }), qc.invalidateQueries({ queryKey: ["entities"] }), qc.invalidateQueries({ queryKey: ["entity-mentions"] }), qc.invalidateQueries({ queryKey: ["commands"] })]);
        return null;
      }}>
      <p className="text-sm text-slate-500">Use this when two people were wrongly treated as one. The mentions you choose, and what rests on them, move to the new person; everything else stays with {entity.name}.</p>
      <Field label="New person's name"><input aria-label="New person's name" className={`${inputCls} w-full`} value={newName} onChange={(e) => setNewName(e.target.value)} /></Field>
      <fieldset className="space-y-1">
        <legend className="text-sm text-slate-600 dark:text-slate-300">Mentions that belong to the new person</legend>
        {mentions.isLoading ? <p className="text-xs text-slate-500">Loading mentions…</p> : items.length === 0 ? <p className="text-sm text-slate-500">This entity has no mentions to move.</p> : items.map((m) => (
          <label key={m.id} className="flex items-center gap-2 text-sm"><input type="checkbox" aria-label={`Move mention in ${m.source_system} ${m.source_ref}`} checked={picked.has(m.id)} onChange={() => toggle(picked, setPicked, m.id)} /> {m.source_system} · {m.source_ref} <span className="text-xs text-slate-500">({m.resolution.replace(/_/g, " ")})</span></label>
        ))}
      </fieldset>
      {entity.externalIds.length ? (
        <fieldset className="space-y-1">
          <legend className="text-sm text-slate-600 dark:text-slate-300">Identifiers that belong to the new person</legend>
          {entity.externalIds.map((x) => { const k = `${x.system}:${x.value}`; return <label key={k} className="flex items-center gap-2 text-sm"><input type="checkbox" aria-label={`Move identifier ${k}`} checked={ext.has(k)} onChange={() => toggle(ext, setExt, k)} /> <span className="font-mono text-xs">{k}</span></label>; })}
        </fieldset>
      ) : null}
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={reextract} onChange={(e) => setReextract(e.target.checked)} /> Read the moved conversations again (a model call)</label>
      <ReasonFields reason={reason} setReason={setReason} note={note} setNote={setNote} />
      {preview.data ? <Impact p={preview.data} /> : null}
      {out ? <Outcome out={out} /> : null}
      {nothing ? <Badge tone="amber">Choose at least one mention or identifier to move.</Badge> : null}
    </ConfirmDialog>
  );
}

const MEMORY_KINDS = [
  { kind: "supersede", label: "Replace it with corrected wording" },
  { kind: "reject", label: "Reject it (it was never right)" },
  { kind: "expire", label: "Expire it (no longer true)" },
  { kind: "dispute", label: "Dispute it (open a review)" },
] as const;

export function CorrectMemoryDialog({ memory, onClose }: { memory: { id: string; revision: number }; onClose: () => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<string>("reject");
  const [content, setContent] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [out, setOut] = useState<CorrectionOut | null>(null);
  const preview = usePreview("memories", memory.id, kind);
  return (
    <ConfirmDialog title="Correct this memory" confirmLabel="Apply correction" cancelLabel={out ? "Close" : "Cancel"} closeOnSuccess={false} disabled={!reason || (kind === "supersede" && !content.trim()) || !!out} onClose={onClose}
      onConfirm={async () => {
        const body: Record<string, unknown> = { kind, reason_code: reason, reason_text: note.trim() || null };
        if (kind === "supersede") body.content = content.trim();
        const r = await send("memory", memory.id, memory.revision, body);
        if (r.error) return r.error;
        setOut(r.out ?? null);
        await Promise.all([qc.invalidateQueries({ queryKey: ["memories"] }), qc.invalidateQueries({ queryKey: ["memory", memory.id] })]);
        return null;
      }}>
      <fieldset className="space-y-1">
        <legend className="sr-only">Kind of correction</legend>
        {MEMORY_KINDS.map((k) => <label key={k.kind} className="flex items-center gap-2 text-sm"><input type="radio" name="memory-correction" checked={kind === k.kind} onChange={() => setKind(k.kind)} /> {k.label}</label>)}
      </fieldset>
      {kind === "supersede" ? <Field label="Corrected wording"><textarea aria-label="Corrected wording" className={`${inputCls} w-full`} rows={3} maxLength={2000} value={content} onChange={(e) => setContent(e.target.value)} /></Field> : null}
      <ReasonFields reason={reason} setReason={setReason} note={note} setNote={setNote} />
      {preview.data ? <Impact p={preview.data} /> : null}
      {out ? <Outcome out={out} /> : null}
    </ConfirmDialog>
  );
}
