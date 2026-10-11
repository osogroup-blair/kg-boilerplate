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

/** Reasons an operator can give for a replay / retry / cancel. They are audited as reason codes (lower_snake), never free text. */
export const REASON_CODES: Array<{ code: string; label: string }> = [
  { code: "fault_cleared", label: "The fault was cleared" },
  { code: "source_fixed", label: "The source data was fixed" },
  { code: "config_fixed", label: "The configuration was fixed" },
  { code: "operator_request", label: "Operator request" },
  { code: "not_needed", label: "No longer needed" },
];
