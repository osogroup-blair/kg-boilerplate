import { type ReactNode } from "react";
import type { Issue } from "../ontology/spec";

export const inputCls =
  "rounded border border-slate-300 bg-white px-2 py-1 text-sm disabled:bg-slate-100 disabled:text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:disabled:bg-slate-800";
export const btn = {
  primary: "rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-slate-300",
  secondary: "rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800",
  danger: "rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950",
  link: "text-sm underline underline-offset-2 hover:no-underline",
};

const TONES: Record<string, string> = {
  green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  blue: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  amber: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  red: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  slate: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};
export function Badge({ tone = "slate", children, title }: { tone?: keyof typeof TONES; children: ReactNode; title?: string }) {
  return <span title={title} className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>{children}</span>;
}

export const VersionStateBadge = ({ state }: { state: string }) => <Badge tone={state === "published" ? "green" : state === "draft" ? "amber" : "slate"}>{state}</Badge>;
const PROJECTION_TONE: Record<string, string> = { projected: "green", pending: "amber", failed: "red", skipped_by_policy: "slate", not_applicable: "slate" };
export const ProjectionBadge = ({ status }: { status: string }) => <Badge tone={PROJECTION_TONE[status] ?? "slate"} title="Graph projection status">{status.replace(/_/g, " ")}</Badge>;
const COMMAND_TONE: Record<string, string> = { applied: "green", rejected: "red", needs_review: "amber" };
export const CommandStatusBadge = ({ status }: { status: string }) => <Badge tone={COMMAND_TONE[status] ?? "slate"}>{status.replace(/_/g, " ")}</Badge>;

const PROCESSING_TONE: Record<string, string> = { indexed: "green", processing: "blue", accepted: "slate", partial: "amber", failed: "red", deletion_pending: "slate" };
export const ProcessingBadge = ({ status }: { status: string }) => <Badge tone={PROCESSING_TONE[status] ?? "slate"} title="Processing status">{status.replace(/_/g, " ")}</Badge>;
const STAGE_TONE: Record<string, string> = { succeeded: "green", running: "blue", pending: "slate", failed: "amber", dead: "red", invalid: "red", skipped_by_policy: "slate", skipped_disabled: "slate" };
export const StageBadge = ({ status }: { status: string }) => <Badge tone={STAGE_TONE[status] ?? "slate"} title="Stage state">{status === "failed" ? "failed (retrying)" : status.replace(/_/g, " ")}</Badge>;
const JOB_TONE: Record<string, string> = { succeeded: "green", leased: "blue", pending: "amber", dead: "red", skipped: "slate", cancelled: "slate" };
export const JobBadge = ({ status }: { status: string }) => <Badge tone={JOB_TONE[status] ?? "slate"} title="Outbox job status">{status}</Badge>;

/** Validation errors anchored at a form field: the server reports a JSON path, the form shows its issues next to the field that owns that path. */
export function FieldError({ issues }: { issues: Issue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul role="alert" className="mt-1 space-y-0.5 text-xs text-red-700 dark:text-red-400">
      {issues.map((i) => <li key={`${i.code}:${i.path}`}><span className="font-mono">{i.code}</span>: {i.message}</li>)}
    </ul>
  );
}

export function Field({ label, issues = [], hint, children, className = "" }: { label: string; issues?: Issue[]; hint?: string; children: ReactNode; className?: string }) {
  return (
    <label className={`block text-sm ${className}`}>
      <span className="mb-1 block text-slate-600 dark:text-slate-300">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-xs text-slate-500">{hint}</span> : null}
      <FieldError issues={issues} />
    </label>
  );
}

export function Banner({ tone = "slate", children, role }: { tone?: "slate" | "amber" | "red" | "green"; children: ReactNode; role?: string }) {
  const cls = { slate: "border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-900", amber: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950", red: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950", green: "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950" }[tone];
  return <div role={role} className={`rounded-md border px-3 py-2 text-sm ${cls}`}>{children}</div>;
}

/** The command API answers 422 with details.violations ({code, path, rule_kind}); show them without ever rendering request values. */
export interface Violation { code: string; path: string; rule_kind: string }
export function Violations({ items }: { items: Violation[] }) {
  return (
    <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">
      {items.map((v, i) => <li key={`${v.code}:${v.path}:${i}`}><span className="font-mono">{v.code}</span> at <span className="font-mono">{v.path}</span> <span className="text-slate-500">({v.rule_kind})</span></li>)}
    </ul>
  );
}
