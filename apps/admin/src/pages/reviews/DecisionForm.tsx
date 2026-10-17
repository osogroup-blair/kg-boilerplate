import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { Banner, btn, Field, inputCls } from "../../components/ui";
import { newKey } from "../../lib/format";

/** The closed list the API accepts (a free-text reason is never an audit field); the label is for people. */
export const RATIONALES: Array<{ code: string; label: string }> = [
  { code: "verified_with_source", label: "I checked it with the source" },
  { code: "source_is_authoritative", label: "The existing source is authoritative" },
  { code: "reported_by_trusted_party", label: "Reported by a trusted party" },
  { code: "same_real_world_entity", label: "They are the same person or thing" },
  { code: "different_real_world_entity", label: "They are different people or things" },
  { code: "outdated_information", label: "The information is out of date" },
  { code: "duplicate_report", label: "Duplicate report" },
  { code: "incorrect_extraction", label: "The extraction was wrong" },
  { code: "insufficient_evidence", label: "Not enough evidence yet" },
  { code: "policy_exception", label: "An approved exception" },
  { code: "no_action_needed", label: "No action needed" },
  { code: "other", label: "Other" },
];

export const DECISION_LABELS: Record<string, string> = {
  same_entity: "Same entity (merge)",
  different_entity: "Different entities",
  needs_more_evidence: "Needs more evidence",
  keep_existing: "Keep the existing fact",
  mark_disputed: "Mark both as disputed",
  accept_claim: "Accept the claim",
  acknowledge: "Acknowledge",
  open_correction: "Open a correction",
  approve: "Approve",
  reject: "Reject",
  approve_with_edit: "Approve with my wording",
  supersede_existing: "Replace the existing memory with this one",
  restore: "Restore it (the source was not the problem)",
  confirm_removal: "Confirm removal (it stays out)",
  redact_text: "Redact the text (replace it with my wording)",
  delete_item: "Delete the item",
  retain_with_reason: "Retain it (a hold or an approved exception)",
  retry_verification: "Check again",
  apply_correction: "Apply the correction",
  dismiss: "Dismiss (no correction needed)",
};

/** A decision needs a decision and a rationale code; the free text is optional and stays on the item. The submit button says so by staying disabled. */
export function DecisionForm({ reviewId, allowed, revision, onDecided }: { reviewId: string; allowed: string[]; revision: string; onDecided: () => void }) {
  const qc = useQueryClient();
  const [decision, setDecision] = useState("");
  const [rationale, setRationale] = useState("");
  const [text, setText] = useState("");
  const [edited, setEdited] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    const res = await api.POST("/v1/reviews/{review_id}/decision", {
      params: { path: { review_id: reviewId }, header: { "Idempotency-Key": newKey(), "If-Match": revision } },
      body: { decision: decision as never, rationale_code: rationale as never, rationale_text: text.trim() || null, edited_content: decision === "approve_with_edit" || decision === "redact_text" ? edited.trim() : null },
    });
    setBusy(false);
    if (res.response.ok) {
      await qc.invalidateQueries({ queryKey: ["reviews"] });
      await qc.invalidateQueries({ queryKey: ["review", reviewId] });
      onDecided();
      return;
    }
    const code = (res.error as { error?: { code?: string } } | undefined)?.error?.code ?? "decision_failed";
    setError(
      code === "requires_correction" ? "This would displace a recorded, verified fact. That is a correction, not a review decision." :
      code === "precondition_failed" ? "The item changed since you opened it. Reload and decide again." :
      code === "already_decided" ? "Someone already decided this item." :
      code === "insufficient_authority" ? "The existing memory has higher authority than you hold, so you cannot replace it." :
      code === "invalid_transition" ? "The memory is no longer in a state this decision applies to." :
      code === "duplicate_memory" ? "An identical live memory already exists." :
      code === "not_available" ? "That decision is not available yet." : code,
    );
  }
  return (
    <form className="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800" aria-label="Decision" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <h2 className="font-medium">Decide</h2>
      <fieldset className="space-y-1">
        <legend className="sr-only">Decision</legend>
        {allowed.map((d) => (
          <label key={d} className="flex items-center gap-2 text-sm"><input type="radio" name="decision" value={d} checked={decision === d} onChange={() => setDecision(d)} /> {DECISION_LABELS[d] ?? d}</label>
        ))}
      </fieldset>
      {decision === "approve_with_edit" || decision === "redact_text" ? (
        <Field label="Your wording" hint={decision === "redact_text" ? "Replaces the memory's text. The original is deleted for good." : "Becomes a new active memory that replaces the proposal. The proposal is kept, with its evidence."}>
          <textarea aria-label="Your wording" className={`${inputCls} w-full`} rows={3} maxLength={2000} value={edited} onChange={(e) => setEdited(e.target.value)} />
        </Field>
      ) : null}
      <Field label="Rationale" hint="Required. Recorded in the audit log as a code.">
        <select aria-label="Rationale" className={`${inputCls} w-full`} value={rationale} onChange={(e) => setRationale(e.target.value)}>
          <option value="" disabled>Choose a rationale…</option>
          {RATIONALES.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
        </select>
      </Field>
      <Field label="Notes (optional)" hint="Kept with this item only; never copied to the audit log.">
        <textarea aria-label="Notes" className={`${inputCls} w-full`} rows={2} maxLength={1000} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      {error ? <Banner tone="red" role="alert">{error}</Banner> : null}
      <button type="submit" className={btn.primary} disabled={!decision || !rationale || busy || ((decision === "approve_with_edit" || decision === "redact_text") && !edited.trim())}>{busy ? "Deciding…" : "Record decision"}</button>
    </form>
  );
}
