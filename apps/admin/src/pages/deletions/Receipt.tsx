import { Badge, Banner } from "../../components/ui";
import { fmt } from "../../lib/format";

interface Counts { deleted?: number; redacted?: number; retained?: number }
interface Receipt {
  status?: string;
  completed_at?: string;
  stores?: Record<string, Counts>;
  retained?: Array<{ class: string; reason: string; count: number }>;
  verification?: { id_residuals?: number; text_residuals?: number; graph_residuals?: number; locations?: string[] };
  backup_copies_expire_by?: string;
  journal_ref?: string;
}

/** The record that a deletion finished: counts by store, what was kept and why, what the verification found, and when backup copies stop holding the data. Ids and counts only. */
export function ReceiptView({ receipt }: { receipt: Record<string, unknown> }) {
  const r = receipt as Receipt;
  const stores = Object.entries(r.stores ?? {});
  const v = r.verification ?? {};
  const residuals = (v.id_residuals ?? 0) + (v.text_residuals ?? 0) + (v.graph_residuals ?? 0);
  return (
    <section aria-label="Receipt" className="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800" data-testid="receipt">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-medium">Receipt</h2>
        <Badge tone={residuals === 0 ? "green" : "amber"}>{residuals === 0 ? "verified clean" : `${residuals} residual${residuals === 1 ? "" : "s"}`}</Badge>
        {r.completed_at ? <span className="text-xs text-slate-500">{fmt(r.completed_at)}</span> : null}
      </div>
      <table className="w-full text-left text-sm" aria-label="Receipt by store">
        <thead className="text-slate-500"><tr><th className="py-1 pr-4">Store</th><th className="pr-4">Deleted</th><th className="pr-4">Redacted</th><th>Retained</th></tr></thead>
        <tbody>{stores.map(([name, c]) => <tr key={name} className="border-t border-slate-200 dark:border-slate-800"><td className="py-1 pr-4">{name}</td><td className="pr-4">{c.deleted ?? 0}</td><td className="pr-4">{c.redacted ?? 0}</td><td>{c.retained ?? 0}</td></tr>)}</tbody>
      </table>
      {(r.retained ?? []).length ? (
        <div>
          <h3 className="text-sm font-medium">Kept, and why</h3>
          <ul className="list-disc pl-5 text-sm">{(r.retained ?? []).map((x) => <li key={x.class}><span className="font-mono text-xs">{x.class}</span> · {x.reason.replace(/_/g, " ")}{x.count ? ` (${x.count})` : ""}</li>)}</ul>
        </div>
      ) : null}
      {(v.locations ?? []).length ? <Banner tone="amber" role="status">Found after the purge, by location only: {(v.locations ?? []).join(", ")}.</Banner> : null}
      <dl className="grid grid-cols-[12rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Backup copies expire by</dt><dd data-testid="backup-expiry">{r.backup_copies_expire_by ?? "—"}</dd>
        <dt className="text-slate-500">Journal entry</dt><dd className="font-mono text-xs">{r.journal_ref ?? "—"}</dd>
      </dl>
    </section>
  );
}
