import { useState, type ReactNode } from "react";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Field, inputCls } from "../../components/ui";
import { REASON_CODES } from "../../lib/format";

/** A repair needs a reason (ING-05, ADM-06): the confirm button stays disabled until one is chosen, and the reason travels as an audited code. */
export function ReasonDialog({ title, confirmLabel, danger, children, onClose, onConfirm }: {
  title: string;
  confirmLabel: string;
  danger?: boolean;
  children?: ReactNode;
  onClose: () => void;
  onConfirm: (reasonCode: string) => Promise<string | null>;
}) {
  const [reason, setReason] = useState("");
  return (
    <ConfirmDialog title={title} confirmLabel={confirmLabel} danger={danger} disabled={!reason} onClose={onClose} onConfirm={() => onConfirm(reason)}>
      {children}
      <Field label="Reason" hint="Recorded in the audit log with your name.">
        <select aria-label="Reason" className={`${inputCls} w-full`} value={reason} onChange={(e) => setReason(e.target.value)}>
          <option value="" disabled>Choose a reason…</option>
          {REASON_CODES.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
        </select>
      </Field>
    </ConfirmDialog>
  );
}
