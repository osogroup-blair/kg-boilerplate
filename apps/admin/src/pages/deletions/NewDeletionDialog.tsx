import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Banner, Field, inputCls } from "../../components/ui";
import { DELETION_REASONS, newKey } from "../../lib/format";

type Kind = "subject" | "interaction" | "memory" | "principal_data" | "workspace";
const KINDS: Array<{ kind: Kind; label: string; hint: string; target?: string }> = [
  { kind: "subject", label: "A person or thing (and what rests on it)", hint: "Its facts, mentions, the interactions that name it, and the memories that only concern it. Memories that merely mention it go to review.", target: "Entity id" },
  { kind: "interaction", label: "One interaction", hint: "Its content, its episode and what only it supported. Facts with other evidence stay.", target: "Interaction id" },
  { kind: "memory", label: "One memory", hint: "The memory, its embedding and its evidence links.", target: "Memory id" },
  { kind: "principal_data", label: "What a principal alone could see", hint: "Its private and unapproved memories. Shared knowledge stays.", target: "Principal id" },
  { kind: "workspace", label: "The whole workspace", hint: "Everything. The workspace is suspended at once, and the deletion runs after a cooling-off period you can cancel in." },
];
const ERRORS: Record<string, string> = {
  not_found: "That target does not exist, or you cannot see it.",
  forbidden: "Your role may not request this kind of deletion.",
  feature_disabled: "Deletion intake is switched off on this platform.",
  validation_failed: "Some of what you entered is not valid.",
  idempotency_mismatch: "This request was already sent with different content.",
  subject_suppressed: "That target belongs to a deleted person.",
};

export function NewDeletionDialog({ onClose, onCreated, preset }: { onClose: () => void; onCreated: (id: string) => void; preset?: { kind: Kind; target: string } }) {
  const qc = useQueryClient();
  const slug = useAuth().me?.workspace.slug ?? "";
  const [kind, setKind] = useState<Kind>(preset?.kind ?? "subject");
  const [target, setTarget] = useState(preset?.target ?? "");
  const [reason, setReason] = useState("data_subject_request");
  const [basis, setBasis] = useState("");
  const [submissions, setSubmissions] = useState(false);
  const [typed, setTyped] = useState("");
  const [ack, setAck] = useState(false);
  const spec = KINDS.find((k) => k.kind === kind)!;
  const ready = ack && reason && (kind === "workspace" ? typed === slug && slug !== "" : /^[0-9a-f-]{36}$/i.test(target.trim()));
  return (
    <ConfirmDialog title="Request a deletion" confirmLabel="Delete" danger disabled={!ready} onClose={onClose}
      onConfirm={async () => {
        const res = await api.POST("/v1/deletion-requests", {
          params: { header: { "Idempotency-Key": newKey() } },
          body: { kind, target_id: kind === "workspace" ? null : target.trim(), reason_code: reason, legal_basis: basis.trim() || null, include_submissions: kind === "principal_data" ? submissions : false, confirm_slug: kind === "workspace" ? typed : null },
        });
        if (res.response.ok && res.data) {
          await qc.invalidateQueries({ queryKey: ["deletions"] });
          onCreated(res.data.request_id);
          return null;
        }
        const code = (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
        return ERRORS[code] ?? code;
      }}>
      <fieldset className="space-y-1">
        <legend className="sr-only">What to delete</legend>
        {KINDS.map((k) => <label key={k.kind} className="flex items-center gap-2 text-sm"><input type="radio" name="deletion-kind" checked={kind === k.kind} onChange={() => setKind(k.kind)} /> {k.label}</label>)}
      </fieldset>
      <p className="text-sm text-slate-500">{spec.hint}</p>
      {kind !== "workspace" ? <Field label={spec.target ?? "Id"}><input aria-label={spec.target} className={`${inputCls} w-full font-mono`} value={target} onChange={(e) => setTarget(e.target.value)} placeholder="00000000-0000-0000-0000-000000000000" /></Field> : (
        <Field label={`Type the workspace slug (${slug}) to confirm`}><input aria-label="Workspace slug" className={`${inputCls} w-full`} value={typed} onChange={(e) => setTyped(e.target.value)} /></Field>
      )}
      {kind === "principal_data" ? <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={submissions} onChange={(e) => setSubmissions(e.target.checked)} /> Also delete the interactions it submitted</label> : null}
      <Field label="Reason" hint="Recorded in the audit log as a code.">
        <select aria-label="Reason" className={`${inputCls} w-full`} value={reason} onChange={(e) => setReason(e.target.value)}>{DELETION_REASONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}</select>
      </Field>
      <Field label="Legal basis (optional)" hint="Kept on the request."><input aria-label="Legal basis" className={`${inputCls} w-full`} maxLength={200} value={basis} onChange={(e) => setBasis(e.target.value)} /></Field>
      <Banner tone="red" role="note">From the moment you confirm, the target disappears from every read. The purge cannot be undone{kind === "workspace" ? "" : ", but a legal hold or the cooling-off rules may stop it"}. A hash of its identifiers is kept so it cannot be recreated by accident.</Banner>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I understand this deletes data permanently</label>
    </ConfirmDialog>
  );
}
