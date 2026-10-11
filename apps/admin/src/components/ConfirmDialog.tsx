import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Banner, btn } from "./ui";

interface Props {
  title: string;
  confirmLabel: string;
  children: ReactNode;
  onClose: () => void;
  /** Resolve to an error string to keep the dialog open and show it, or null when done. */
  onConfirm: () => Promise<string | null>;
  disabled?: boolean;
  danger?: boolean;
  /** Command dialogs stay open to show the outcome (applied, queued, rejected with its violations). */
  closeOnSuccess?: boolean;
  cancelLabel?: string;
}

export function ConfirmDialog({ title, confirmLabel, children, onClose, onConfirm, disabled, danger, closeOnSuccess = true, cancelLabel = "Cancel" }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    setBusy(true);
    setError(null);
    const err = await onConfirm();
    setBusy(false);
    if (err) setError(err);
    else if (closeOnSuccess) onClose();
  }
  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/30" />
        <Dialog.Content aria-describedby={undefined} className="fixed left-1/2 top-20 max-h-[80vh] w-full max-w-xl -translate-x-1/2 space-y-4 overflow-y-auto rounded-lg bg-white p-6 shadow-xl dark:bg-slate-900">
          <div className="flex items-center justify-between">
            <Dialog.Title className="text-lg font-semibold">{title}</Dialog.Title>
            <Dialog.Close aria-label="Close" className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-800"><X className="h-4 w-4" /></Dialog.Close>
          </div>
          {children}
          {error ? <Banner tone="red" role="alert">{error}</Banner> : null}
          <div className="flex justify-end gap-2">
            <button type="button" className={btn.secondary} onClick={onClose}>{cancelLabel}</button>
            <button type="button" className={danger ? btn.danger : btn.primary} disabled={busy || disabled} onClick={() => void run()}>{busy ? "Working…" : confirmLabel}</button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
