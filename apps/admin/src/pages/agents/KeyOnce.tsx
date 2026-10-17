import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { Banner, btn } from "../../components/ui";

/** A freshly minted key. It is shown here ONCE: the API never returns it again, and nothing in this console stores it. */
export function KeyOnce({ plaintext, label = "API key" }: { plaintext: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(plaintext);
      setCopied(true);
    } catch {
      setCopied(false); // the key is selectable on screen; a blocked clipboard is not a failure
    }
  }
  return (
    <div className="space-y-2" aria-label="New key">
      <Banner tone="amber" role="alert">
        <strong>This key will not be shown again.</strong> Copy it now and store it in the agent&apos;s secret store. If it is lost, mint another key and revoke this one.
      </Banner>
      <div className="flex items-center gap-2">
        <code aria-label={label} className="min-w-0 flex-1 select-all break-all rounded border border-slate-300 bg-slate-50 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900">{plaintext}</code>
        <button type="button" className={btn.secondary} onClick={() => void copy()} aria-label="Copy key">
          {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        </button>
      </div>
    </div>
  );
}
