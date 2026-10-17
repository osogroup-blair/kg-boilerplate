import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Field, inputCls } from "../../components/ui";
import { MEMORY_REASONS, newKey } from "../../lib/format";

type Kind = "supersede" | "expire" | "dispute";

const COPY: Record<Kind, { title: string; confirm: string; danger: boolean; text: string }> = {
  supersede: {
    title: "Supersede this memory",
    confirm: "Supersede",
    danger: false,
    text: "A new active memory replaces this one. The old memory is kept as superseded, with its evidence, and a lineage link between the two.",
  },
  expire: { title: "Expire this memory", confirm: "Expire", danger: true, text: "The memory stops being served from now on. It is kept, with its history." },
  dispute: { title: "Dispute this memory", confirm: "Dispute", danger: true, text: "The memory is no longer served and a review item opens to decide how the dispute ends." },
};

const ERRORS: Record<string, string> = {
  precondition_failed: "The memory changed since you opened it. Reload and try again.",
  invalid_transition: "The memory is not in a state that allows this any more.",
  duplicate_memory: "An identical live memory already exists.",
  content_too_long: "That wording is longer than the memory policy allows.",
  forbidden: "Your role may not do this.",
};

/** A change to a memory needs a reason code (audited) and, for a supersession, the new wording: the confirm button stays disabled until both are given. */
export function MemoryActionDialog({ kind, memoryId, revision, onClose, onDone }: { kind: Kind; memoryId: string; revision: string; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const copy = COPY[kind];
  const [reason, setReason] = useState("");
  const [content, setContent] = useState("");
  async function run(): Promise<string | null> {
    const params = { path: { memory_id: memoryId }, header: { "Idempotency-Key": newKey(), "If-Match": revision } } as never;
    const res =
      kind === "supersede"
        ? await api.POST("/v1/memories/{memory_id}/supersede", { params, body: { content: content.trim(), reason_code: reason } })
        : kind === "expire"
          ? await api.POST("/v1/memories/{memory_id}/expire", { params, body: { reason_code: reason } })
          : await api.POST("/v1/memories/{memory_id}/dispute", { params, body: { reason_code: reason } });
    if (res.response.ok) {
      await qc.invalidateQueries({ queryKey: ["memories"] });
      await qc.invalidateQueries({ queryKey: ["memory", memoryId] });
      onDone();
      return null;
    }
    const code = (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
    return ERRORS[code] ?? code;
  }
  return (
    <ConfirmDialog title={copy.title} confirmLabel={copy.confirm} danger={copy.danger} disabled={!reason || (kind === "supersede" && !content.trim())} onClose={onClose} onConfirm={run}>
      <p className="text-sm">{copy.text}</p>
      {kind === "supersede" ? (
        <Field label="New wording" hint="Say it as a self-contained statement. The change is attributed to you.">
          <textarea aria-label="New wording" className={`${inputCls} w-full`} rows={4} maxLength={2000} value={content} onChange={(e) => setContent(e.target.value)} />
        </Field>
      ) : null}
      <Field label="Reason" hint="Recorded in the audit log with your name.">
        <select aria-label="Reason" className={`${inputCls} w-full`} value={reason} onChange={(e) => setReason(e.target.value)}>
          <option value="" disabled>Choose a reason…</option>
          {MEMORY_REASONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
        </select>
      </Field>
    </ConfirmDialog>
  );
}
