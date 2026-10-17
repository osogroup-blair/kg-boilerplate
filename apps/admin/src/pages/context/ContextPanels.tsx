import { useState } from "react";
import { Link } from "react-router-dom";
import type { components } from "../../api/schema";
import { Badge, Banner } from "../../components/ui";
import { ageLabel, fmt } from "../../lib/format";
import { HandleText } from "./handles";

export type ContextOut = components["schemas"]["ContextOut"];
export type TraceOut = components["schemas"]["TraceOut"];
type Item = components["schemas"]["ItemOut"];
type TraceItem = components["schemas"]["TraceItemOut"];

const STATUS_TONE = { complete: "green", partial: "amber", denied: "red", insufficient_evidence: "amber", insufficient_budget: "red" } as const;
const STATUS_HINT: Record<string, string> = {
  complete: "Every source the profile needs was consulted. This says nothing about whether the facts are true: read each item's evidence status.",
  partial: "A source was unavailable or stale. The package is labelled, and current state still comes from Postgres.",
  denied: "Nothing is returned: the principal may not ask about these subjects.",
  insufficient_evidence: "No authorised record supports an answer. The package says what is unknown; nothing is invented.",
  insufficient_budget: "The required records do not fit the budget. Ask for at least the number of tokens shown.",
};
export const StatusBadge = ({ status }: { status: ContextOut["status"] }) => <Badge tone={STATUS_TONE[status]} title={STATUS_HINT[status]}>{status.replace(/_/g, " ")}</Badge>;

/** used / reserved / required against the ceiling: the bar is the budget, the marker is what the required tier alone needs. */
export function BudgetBar({ budgets }: { budgets: NonNullable<ContextOut["budgets"]> }) {
  const pct = (n: number) => `${Math.min(100, (n / Math.max(1, budgets.max_tokens)) * 100)}%`;
  return (
    <section aria-label="Budget" className="space-y-1">
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
        <span><strong data-testid="used-tokens">{budgets.used_tokens}</strong> of {budgets.max_tokens} tokens used</span>
        <span className="text-xs text-slate-500">envelope {budgets.reserved_tokens} · required tier needs {budgets.required_tokens} · {budgets.tokenizer_id}</span>
      </div>
      <div role="img" aria-label={`${budgets.used_tokens} of ${budgets.max_tokens} tokens`} className="relative h-3 overflow-hidden rounded bg-slate-200 dark:bg-slate-800">
        <div className="absolute inset-y-0 left-0 bg-slate-700 dark:bg-slate-300" style={{ width: pct(budgets.used_tokens) }} />
        <div className="absolute inset-y-0 w-0.5 bg-amber-500" style={{ left: pct(budgets.required_tokens) }} title="what the required tier alone needs" />
      </div>
      {Object.entries(budgets.truncated ?? {}).some(([, n]) => n > 0) ? (
        <p className="text-xs text-slate-500">Left out to fit: {Object.entries(budgets.truncated ?? {}).filter(([, n]) => n > 0).map(([tier, n]) => `${n} ${tier}`).join(", ")} (authorised candidates; see the trace for which).</p>
      ) : null}
    </section>
  );
}

const SECTION_LABEL: Record<string, string> = { facts: "Facts", memories: "Memories", instructions: "Guidance", unknowns: "Unknowns", conflicts: "Conflicts" };

function ItemRow({ i, active }: { i: Item; active: boolean }) {
  return (
    <li id={`item-${i.handle}`} className={`rounded border px-2 py-1 text-sm ${active ? "border-amber-400" : "border-slate-200 dark:border-slate-800"}`} data-testid={`item-${i.handle}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={i.tier === "required" ? "red" : i.tier === "high" ? "blue" : "slate"} title="Packing tier">{i.tier}</Badge>
        {i.agent_generated ? <Badge tone="amber" title="Produced by an agent, not by a person or a system of record">agent generated</Badge> : null}
        {i.evidence_status ? <Badge tone={i.evidence_status === "verified" ? "green" : "blue"}>{i.evidence_status}</Badge> : null}
        {!i.supported ? <Badge tone="amber" title="No evidence pointer">unsupported</Badge> : null}
        <span className="font-mono text-xs text-slate-500">{i.handle}</span>
        <span className="text-xs text-slate-500">score {i.score.toFixed(3)} · {i.tokens} tokens</span>
      </div>
      <div className="mt-1">{i.text}</div>
    </li>
  );
}

/** The package as the agent receives it: the text block (handles linked), its items, the sources it could not reach, and the citations. */
export function PackageView({ out }: { out: ContextOut }) {
  const [picked, setPicked] = useState<string | null>(null);
  const groups: [keyof typeof SECTION_LABEL, Item[]][] = [
    ["facts", out.facts ?? []], ["memories", out.memories ?? []], ["instructions", out.instructions ?? []], ["unknowns", out.unknowns ?? []], ["conflicts", out.conflicts ?? []],
  ];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={out.status} />
        {out.profile ? <Badge title="Profile">{out.profile.name} ({out.profile.source})</Badge> : null}
        {out.compression ? <Badge title="Compression">{out.compression}</Badge> : null}
        {out.principal_revoked ? <Badge tone="red" title="The principal that built this context has been revoked; every item was re-checked against your current access">principal revoked</Badge> : null}
        {out.rematerialized ? <Badge tone="blue" title="Rebuilt from the stored item ids under your current authorisation">re-materialised{out.withdrawn ? ` · ${out.withdrawn} withdrawn` : ""}</Badge> : null}
        {out.context_id ? <Link className="text-xs underline" to={`/context/${out.context_id}`}>open the trace</Link> : null}
      </div>
      <p className="text-sm text-slate-600 dark:text-slate-300">{STATUS_HINT[out.status]}</p>
      {(out.unavailable_sources ?? []).length > 0 ? (
        <Banner tone="amber" role="status">Unavailable: {(out.unavailable_sources ?? []).map((u) => `${u.source} (${u.reason.replace(/_/g, " ")})`).join(", ")}.</Banner>
      ) : null}
      {out.freshness && (out.freshness.graph_lag_seconds != null || out.freshness.memory_lag_seconds != null) ? (
        <p className="text-xs text-slate-500">Pipeline lag: graph {ageLabel(out.freshness.graph_lag_seconds)} · memory {ageLabel(out.freshness.memory_lag_seconds)}{out.freshness.max_lag_seconds != null ? ` (limit ${ageLabel(out.freshness.max_lag_seconds)})` : ""}</p>
      ) : null}
      {out.budgets ? <BudgetBar budgets={out.budgets} /> : null}
      <section aria-label="Context text">
        <h3 className="mb-1 text-sm font-medium">Context text</h3>
        <pre data-testid="context-text" className="overflow-x-auto whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-3 text-xs leading-relaxed dark:border-slate-800 dark:bg-slate-900"><HandleText text={out.context_text} onPick={setPicked} /></pre>
      </section>
      <div className="grid gap-4 md:grid-cols-2">
        {groups.filter(([, items]) => items.length > 0).map(([key, items]) => (
          <section key={key} aria-label={SECTION_LABEL[key]}>
            <h3 className="mb-1 text-sm font-medium">{SECTION_LABEL[key]} ({items.length})</h3>
            <ul className="space-y-1">{items.map((i) => <ItemRow key={i.handle} i={i} active={picked === i.handle} />)}</ul>
          </section>
        ))}
      </div>
      {(out.citations ?? []).length > 0 ? (
        <section aria-label="Citations">
          <h3 className="mb-1 text-sm font-medium">Citations</h3>
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500"><tr><th className="py-1 pr-3">Handle</th><th className="pr-3">Kind</th><th className="pr-3">Source</th><th>Fetch</th></tr></thead>
            <tbody>
              {(out.citations ?? []).map((c) => (
                <tr key={c.handle} id={`cite-${c.handle}`} className={`border-t border-slate-200 dark:border-slate-800 ${picked === c.handle ? "bg-amber-50 dark:bg-amber-950" : ""}`}>
                  <td className="py-1 pr-3 font-mono">{c.handle}</td><td className="pr-3">{c.kind}</td>
                  <td className="pr-3">{c.source_system ? `${c.source_system}/${c.source_ref ?? ""}` : <span className="font-mono">{c.id.slice(0, 8)}…</span>}</td>
                  <td className="font-mono text-slate-500">{c.url ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}

const FEATURES = ["relevance", "authority", "evidence", "importance", "proximity", "recency"] as const;

function FeatureBars({ f }: { f: Record<string, number> }) {
  return (
    <span className="inline-flex items-end gap-0.5" aria-label="Score breakdown">
      {FEATURES.map((k) => <span key={k} title={`${k} ${(f[k] ?? 0).toFixed(2)}`} className="inline-block w-1.5 bg-slate-500 dark:bg-slate-400" style={{ height: `${4 + (f[k] ?? 0) * 14}px` }} />)}
      {(f.diversity_penalty ?? 0) > 0 ? <span title={`diversity penalty −${f.diversity_penalty!.toFixed(3)}`} className="ml-1 text-[10px] text-amber-700">−{f.diversity_penalty!.toFixed(2)}</span> : null}
    </span>
  );
}

function TraceRows({ items, empty }: { items: TraceItem[]; empty: string }) {
  if (items.length === 0) return <p className="text-sm text-slate-500">{empty}</p>;
  return (
    <table className="w-full text-left text-xs">
      <thead className="text-slate-500"><tr><th className="py-1 pr-2">#</th><th className="pr-2">Kind</th><th className="pr-2">Section</th><th className="pr-2">Tier</th><th className="pr-2">Score</th><th className="pr-2">Features</th><th className="pr-2">Tokens</th><th>Why</th></tr></thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.position} className="border-t border-slate-200 align-top dark:border-slate-800" data-testid={`trace-${i.position}`}>
            <td className="py-1 pr-2">{i.position}</td>
            <td className="pr-2">{i.kind}{i.id && (i.kind === "memory" || i.kind === "fact") ? <> <Link className="font-mono underline" to={i.kind === "memory" ? `/memories/${i.id}` : `/graph?fact=${i.id}`}>{i.id.slice(0, 8)}…</Link></> : null}</td>
            <td className="pr-2">{i.section.replace(/_/g, " ")}</td><td className="pr-2">{i.tier}</td><td className="pr-2 font-mono">{i.score.toFixed(3)}</td>
            <td className="pr-2"><FeatureBars f={i.features as Record<string, number>} /></td><td className="pr-2">{i.tokens}</td>
            <td>{i.included ? <Badge tone="green">packed</Badge> : <Badge tone="slate" title="The reason this authorised candidate is not in the package">{(i.exclusion_reason ?? "").replace(/_/g, " ")}</Badge>}{i.duplicate_of ? <span className="ml-1 font-mono text-slate-500">of {i.duplicate_of.slice(0, 8)}…</span> : null}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Why the engine chose what it chose (AT-12, ADM-04): the plan, per-source counts of AUTHORISED candidates, each candidate's score and reason, and the cost of each stage. Metadata only. */
export function TraceView({ trace }: { trace: TraceOut }) {
  const sources = (trace.counts as { candidates_by_source?: Record<string, number> }).candidates_by_source ?? {};
  const per = (trace.latencies as { per_source?: Record<string, number>; total?: number; authorize?: number }) ?? {};
  const packed = trace.items.filter((i) => i.included);
  const left = trace.items.filter((i) => !i.included);
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-[11rem_1fr] gap-y-1 text-sm" aria-label="Plan and versions">
        <dt className="text-slate-500">Profile</dt><dd>{trace.profile} ({trace.profile_source}) · {trace.compression} · built {fmt(trace.created_at)}</dd>
        <dt className="text-slate-500">Versions</dt><dd className="font-mono text-xs">{Object.entries(trace.versions).map(([k, v]) => `${k}=${String(v)}`).join(" · ")}</dd>
        <dt className="text-slate-500">Plan</dt><dd className="font-mono text-xs">{JSON.stringify(trace.plan)}</dd>
        <dt className="text-slate-500">Tokens</dt><dd>{trace.used_tokens} used of {trace.max_tokens} · the task was {trace.task_tokens} tokens (its text is not stored)</dd>
        {trace.act_as_by ? <><dt className="text-slate-500">Built as</dt><dd>acting for <span className="font-mono text-xs">{trace.principal_id}</span> by <span className="font-mono text-xs">{trace.act_as_by}</span></dd></> : null}
      </dl>
      <section aria-label="Candidates by source">
        <h3 className="mb-1 text-sm font-medium">Authorised candidates by source</h3>
        <p className="text-sm" data-testid="candidates-by-source">{Object.entries(sources).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none"}</p>
        <p className="text-xs text-slate-500">Counts include only what this principal may see; nothing it may not is ever a candidate.</p>
      </section>
      {trace.unavailable.length > 0 ? <Banner tone="amber" role="status">Unavailable: {trace.unavailable.map((u) => `${String(u.source)} (${String(u.reason)})`).join(", ")}</Banner> : null}
      <section aria-label="Selected items"><h3 className="mb-1 text-sm font-medium">Selected ({packed.length})</h3><TraceRows items={packed} empty="Nothing was packed." /></section>
      <section aria-label="Excluded items"><h3 className="mb-1 text-sm font-medium">Excluded ({left.length})</h3><TraceRows items={left} empty="Nothing authorised was left out." /></section>
      <section aria-label="Latency">
        <h3 className="mb-1 text-sm font-medium">Latency</h3>
        <p className="text-xs text-slate-500" data-testid="latency">total {per.total ?? "—"} s · authorise {per.authorize ?? "—"} s · {Object.entries(per.per_source ?? {}).map(([k, v]) => `${k} ${v} s`).join(" · ")}</p>
      </section>
      <section aria-label="Watermarks">
        <h3 className="mb-1 text-sm font-medium">Pipeline watermarks</h3>
        <p className="font-mono text-xs text-slate-500">{JSON.stringify(trace.watermarks)}</p>
      </section>
    </div>
  );
}
