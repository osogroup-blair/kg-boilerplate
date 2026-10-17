import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Field, inputCls } from "../../components/ui";
import { newKey } from "../../lib/format";
import { errorCodeOf, REVOKE_EFFECTS } from "./lib";

/** Revocation is final. The operator picks the reason, reads what it will do, and types the handle: the confirm button stays disabled until all three are done. */
export function RevokeDialog({ principalId, handle, onClose, onDone }: { principalId: string; handle: string; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState<"retired" | "compromised">("retired");
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState("");

  async function run(): Promise<string | null> {
    const res = await api.POST("/v1/principals/{principal_id}/revoke", {
      params: { path: { principal_id: principalId }, header: { "Idempotency-Key": newKey() } } as never,
      body: { reason, note: note.trim() || null },
    });
    if (res.response.ok) {
      await qc.invalidateQueries({ queryKey: ["principals"] });
      await qc.invalidateQueries({ queryKey: ["principal", principalId] });
      await qc.invalidateQueries({ queryKey: ["reviews"] });
      onDone();
      return null;
    }
    return errorCodeOf(res.error);
  }

  return (
    <ConfirmDialog title={`Revoke ${handle}`} confirmLabel="Revoke" danger disabled={typed !== handle} onClose={onClose} onConfirm={run}>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Why</legend>
        <label className="flex items-start gap-2 text-sm">
          <input type="radio" name="revoke-reason" checked={reason === "retired"} onChange={() => setReason("retired")} />
          <span><strong>Retired</strong> — it is being decommissioned or replaced. Nothing it produced is distrusted.</span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="radio" name="revoke-reason" checked={reason === "compromised"} onChange={() => setReason("compromised")} />
          <span><strong>Compromised</strong> — its key or behaviour cannot be trusted. What it produced is quarantined for review.</span>
        </label>
      </fieldset>
      <div>
        <h3 className="text-sm font-medium">What happens</h3>
        <ul aria-label="Effects of revocation" className="list-disc space-y-0.5 pl-5 text-sm">
          {REVOKE_EFFECTS[reason].map((e) => <li key={e}>{e}</li>)}
        </ul>
        <p className="mt-2 text-sm font-medium text-red-700 dark:text-red-400">There is no undo: a replacement is a new registration.</p>
      </div>
      <Field label="Note (kept in the ledger, not in the audit log)"><input aria-label="Revocation note" className={`${inputCls} w-full`} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      <Field label={`Type the handle (${handle}) to confirm`}>
        <input aria-label="Confirm handle" className={`${inputCls} w-full font-mono`} autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
      </Field>
    </ConfirmDialog>
  );
}
