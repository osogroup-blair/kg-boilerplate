import type { components } from "../../api/schema";
import { StageBadge } from "../../components/ui";
import { duration, fmt } from "../../lib/format";

export type Stage = components["schemas"]["StageOut"];

/** The per-stage timeline of one interaction (blueprint Flow A): state, attempts, generation, timings and the error that stopped it. A stage that is declared
 * but not yet implemented is greyed out and says when it arrives; a derivative shows the id that gives the provenance path (EVD-03). */
export function StageTimeline({ stages }: { stages: Stage[] }) {
  return (
    <ol aria-label="Stage timeline" className="space-y-2">
      {stages.map((s) => (
        <li key={s.stage} data-testid={`stage-${s.stage}`} className={`rounded border p-3 text-sm ${s.enabled ? "border-slate-200 dark:border-slate-800" : "border-dashed border-slate-300 opacity-60 dark:border-slate-700"}`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs">{s.stage}</span>
              <StageBadge status={s.status} />
              {s.generation > 0 ? <span className="text-xs text-slate-500" title="Raised by each replay">generation {s.generation}</span> : null}
            </div>
            <span className="text-xs text-slate-500">
              {s.enabled ? `${s.attempts} attempt${s.attempts === 1 ? "" : "s"} · ${duration(s.started_at, s.finished_at)}` : `enabled in ${s.enabled_in ?? "a later phase"}`}
            </span>
          </div>
          {s.enabled && (s.started_at || s.finished_at) ? <p className="mt-1 text-xs text-slate-500">{fmt(s.started_at)} → {fmt(s.finished_at)}</p> : null}
          {s.last_error_code ? (
            <p className="mt-1 text-xs text-red-700 dark:text-red-400">
              <span className="font-mono">{s.last_error_category ?? "error"}</span>: <span className="font-mono">{s.last_error_code}</span>
            </p>
          ) : null}
          {s.derivatives.length > 0 ? (
            <ul className="mt-1 text-xs text-slate-600 dark:text-slate-300">
              {s.derivatives.map((d) => <li key={d.id}>derivative <span className="font-mono">{d.kind}</span> <span className="font-mono text-slate-500">{d.id.slice(0, 8)}…</span></li>)}
            </ul>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
