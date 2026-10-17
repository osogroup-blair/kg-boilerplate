export const fmt = (iso?: string | null): string => (iso ? new Date(iso).toLocaleString() : "—");
export const fmtDate = (iso?: string | null): string => (iso ? iso.slice(0, 10) : "—");
export const shortHash = (hash?: string | null): string => (hash ? hash.slice(0, 12) : "—");

/** A fresh Idempotency-Key for one user action. crypto.randomUUID exists in browsers and Node; the fallback keeps odd environments working. */
export function newKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** A sensitive attribute arrives as {"redacted": true} for callers without entity.read_sensitive; show a label, never the raw object. */
export function isRedacted(value: unknown): boolean {
  return typeof value === "object" && value !== null && (value as { redacted?: unknown }).redacted === true;
}

export function displayValue(value: unknown): string {
  if (isRedacted(value)) return "redacted";
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** "3s", "2m 05s": a duration between two ISO instants, or "—" while one of them is missing. */
export function duration(start?: string | null, end?: string | null): string {
  if (!start || !end) return "—";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export function ageLabel(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** Reasons a curator can give for changing a memory. Audited as codes (lower_snake), never free text. */
export const MEMORY_REASONS: Array<{ code: string; label: string }> = [
  { code: "corrected", label: "It was wrong; corrected" },
  { code: "clarified", label: "Clarified with newer information" },
  { code: "no_longer_relevant", label: "No longer relevant" },
  { code: "contradicted", label: "Contradicted by newer evidence" },
  { code: "duplicate", label: "Duplicate of another memory" },
  { code: "stale", label: "Stale" },
];

/** Why a record is corrected: a closed list (audited as a code). The free-text note stays with the correction and is never copied to the audit log. */
export const CORRECTION_REASONS: Array<{ code: string; label: string }> = [
  { code: "incorrect_extraction", label: "The extraction was wrong" },
  { code: "source_error", label: "The source was wrong" },
  { code: "outdated_information", label: "The information is out of date" },
  { code: "wrong_identity", label: "It belongs to a different person or thing" },
  { code: "duplicate_report", label: "Duplicate report" },
  { code: "privacy_request", label: "A privacy request" },
  { code: "policy_exception", label: "An approved exception" },
  { code: "other", label: "Other" },
];

/** Reasons a deletion request can give (audited as codes). */
export const DELETION_REASONS: Array<{ code: string; label: string }> = [
  { code: "data_subject_request", label: "The data subject asked" },
  { code: "retention_policy", label: "Retention policy" },
  { code: "offboarding", label: "Offboarding" },
  { code: "wrong_data", label: "It should never have been collected" },
  { code: "security_incident", label: "A security incident" },
  { code: "other", label: "Other" },
];

/** Reasons an operator can give for a replay / retry / cancel. They are audited as reason codes (lower_snake), never free text. */
export const REASON_CODES: Array<{ code: string; label: string }> = [
  { code: "fault_cleared", label: "The fault was cleared" },
  { code: "source_fixed", label: "The source data was fixed" },
  { code: "config_fixed", label: "The configuration was fixed" },
  { code: "operator_request", label: "Operator request" },
  { code: "not_needed", label: "No longer needed" },
];
