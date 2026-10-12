import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { Badge, Banner, btn, EvidenceBadge, FactStatusBadge, ReviewTypeBadge } from "../../components/ui";
import { displayValue, fmt, fmtDate, isRedacted } from "../../lib/format";
import { ContentView } from "../interactions/InteractionDetailPage";
import { DecisionForm } from "./DecisionForm";

type Detail = components["schemas"]["ReviewDetail"];
type Entity = components["schemas"]["ReviewEntityView"];
type Fact = components["schemas"]["FactOut"];
type Content = components["schemas"]["ContentOut"];

function EntityCard({ title, e }: { title: string; e: Entity }) {
  return (
    <section aria-label={title} className="space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800" data-testid={`entity-card-${title.toLowerCase()}`}>
      <h3 className="text-sm font-medium text-slate-500">{title}</h3>
      <p className="font-medium"><Link className="underline-offset-2 hover:underline" to={`/entities/${e.id}`}>{e.name}</Link></p>
      <div className="flex flex-wrap gap-1"><Badge>{e.type}</Badge><Badge tone={e.status === "candidate" ? "amber" : "slate"}>{e.status}</Badge>{e.origin === "extracted" ? <Badge tone="blue">extracted</Badge> : <Badge tone="green">from a trusted source</Badge>}</div>
      <dl className="grid grid-cols-[7rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Aliases</dt><dd>{e.aliases.join(", ") || "—"}</dd>
        <dt className="text-slate-500">External ids</dt><dd>{e.external_ids.map((x) => `${x.system}:${x.value}`).join(", ") || "none"}</dd>
        <dt className="text-slate-500">Mentioned in</dt><dd>{e.mention_count} interaction{e.mention_count === 1 ? "" : "s"}</dd>
        <dt className="text-slate-500">Facts</dt><dd>{e.fact_count}</dd>
        {Object.entries(e.attributes).map(([k, v]) => <div key={k} className="contents"><dt className="text-slate-500">{k}</dt><dd>{isRedacted(v) ? <Badge tone="slate">redacted</Badge> : displayValue(v)}</dd></div>)}
      </dl>
    </section>
  );
}

/** Two valid-time bars on one axis: where the existing fact and the claim overlap is what the contradiction is about. */
function Timeline({ facts }: { facts: Fact[] }) {
  const stamp = (iso?: string | null) => (iso ? new Date(iso).getTime() : null);
  const known = facts.flatMap((f) => [stamp(f.valid_from), stamp(f.valid_to)]).filter((x): x is number => x !== null);
  if (known.length === 0) return <p className="text-xs text-slate-500">Neither fact states when it began, so there is no timeline to draw.</p>;
  const lo = Math.min(...known), hi = Math.max(...known, lo + 86_400_000 * 30);
  const pct = (t: number) => `${(((t - lo) / (hi - lo)) * 100).toFixed(1)}%`;
  return (
    <div aria-label="Valid time of both facts" className="space-y-1">
      {facts.map((f) => {
        const from = stamp(f.valid_from), to = stamp(f.valid_to);
        return (
          <div key={f.id} className="flex items-center gap-2 text-xs" data-testid={`timeline-${f.id}`}>
            <span className="w-24 shrink-0 truncate">{f.status === "active" && f.evidence_status === "verified" ? "existing" : "claim"}</span>
            <div className="relative h-3 flex-1 rounded bg-slate-100 dark:bg-slate-800">
              <div className={`absolute h-3 rounded ${f.evidence_status === "verified" ? "bg-slate-600" : "bg-violet-500"}`} style={{ left: from === null ? "0%" : pct(from), right: to === null ? "0%" : `calc(100% - ${pct(to)})` }} />
            </div>
            <span className="w-44 shrink-0 text-slate-500">{f.valid_from_unknown ? "start unknown" : fmtDate(f.valid_from)} → {to === null ? "open" : fmtDate(f.valid_to)}</span>
          </div>
        );
      })}
    </div>
  );
}

function FactCard({ title, f }: { title: string; f: Fact }) {
  return (
    <section aria-label={title} className="space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800" data-testid={`fact-card-${title.toLowerCase()}`}>
      <h3 className="text-sm font-medium text-slate-500">{title}</h3>
      <p className="text-sm"><span className="font-mono text-xs">{f.rel_type}</span> · {f.fact_text ?? `${f.source.name} → ${f.target.name}`}</p>
      <div className="flex flex-wrap gap-1"><FactStatusBadge status={f.status} /><EvidenceBadge status={f.evidence_status} /><Badge>{f.authority_class.replace(/_/g, " ")}</Badge></div>
      <dl className="grid grid-cols-[7rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Valid</dt><dd>{f.valid_from_unknown ? "start unknown" : fmtDate(f.valid_from)} → {fmtDate(f.valid_to)}</dd>
        <dt className="text-slate-500">Recorded</dt><dd>{fmt(f.recorded_at)}</dd>
        <dt className="text-slate-500">Evidence</dt>
        <dd>{f.evidence.length === 0 ? "none recorded" : f.evidence.map((ev, i) => <span key={i} className="mr-2">{ev.kind === "episode" && ev.interaction_id ? <Link className="underline" to={`/interactions/${ev.interaction_id}`}>interaction</Link> : ev.command_id ? <Link className="underline" to={`/commands/${ev.command_id}`}>command</Link> : ev.kind}</span>)}</dd>
      </dl>
    </section>
  );
}

/** Reading an interaction's content is audited per read, so it asks first (Q14). Content is shown as text only. */
function EvidenceReveal({ reviewId, interaction }: { reviewId: string; interaction: components["schemas"]["InteractionRef"] }) {
  const can = useCan();
  const [confirming, setConfirming] = useState(false);
  const [content, setContent] = useState<Content | null>(null);
  async function load(): Promise<string | null> {
    const res = await api.GET("/v1/reviews/{review_id}/evidence/{interaction_id}", { params: { path: { review_id: reviewId, interaction_id: interaction.interaction_id } } });
    if (!res.data) return res.response.status === 403 ? "Your role may not read evidence." : res.response.status === 404 ? "This interaction is not part of this item." : "Could not read the evidence.";
    setContent(res.data);
    return null;
  }
  return (
    <li className="space-y-1" data-testid={`interaction-${interaction.interaction_id}`}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Link className="underline" to={`/interactions/${interaction.interaction_id}`}>{interaction.source_system} · {interaction.source_ref}</Link>
        <span className="text-xs text-slate-500">occurred {fmtDate(interaction.occurred_at)} · recorded {fmt(interaction.recorded_at)}</span>
        {can.readReviewEvidence ? (content ? <button type="button" className={btn.link} onClick={() => setContent(null)}>Hide</button> : <button type="button" className={btn.secondary} onClick={() => setConfirming(true)}>View evidence…</button>) : null}
      </div>
      {content ? <ContentView content={content} /> : null}
      {confirming ? (
        <ConfirmDialog title="View evidence" confirmLabel="View and audit" onClose={() => setConfirming(false)} onConfirm={load}>
          <p className="text-sm">You are about to read the text of an interaction this review item rests on. The read is recorded in the audit log with your name.</p>
        </ConfirmDialog>
      ) : null}
    </li>
  );
}

function Body({ d }: { d: Detail }) {
  const existing = d.facts.find((f) => f.id === d.competing_fact_id);
  const claim = d.facts.find((f) => f.id === d.fact_id);
  if (d.item_type === "identity_match") {
    const subject = d.entities.find((e) => e.id === d.subject_entity_id);
    const candidate = d.entities.find((e) => e.id === d.candidate_entity_id);
    return (
      <>
        <p className="text-sm">An extraction mentioned an entity that looks like one the platform already knows. A name is not an identity, so nothing was merged: the mention is its own <em>candidate</em> until you decide.</p>
        <div className="grid gap-4 md:grid-cols-2">{subject ? <EntityCard title="Existing" e={subject} /> : null}{candidate ? <EntityCard title="Candidate" e={candidate} /> : null}</div>
        {d.candidates.length > 0 ? (
          <section aria-label="Why these look alike" className="text-sm">
            <h3 className="font-medium">Why these look alike</h3>
            <ul className="mt-1 space-y-0.5">{d.candidates.map((c) => <li key={c.entity_id} className="font-mono text-xs">{c.entity_id.slice(0, 8)}… · {c.reasons.join(", ")}{c.trigram != null ? ` · name ${c.trigram}` : ""}{c.cosine != null ? ` · embedding ${c.cosine}` : ""}</li>)}</ul>
          </section>
        ) : null}
      </>
    );
  }
  if (d.item_type === "contradiction") {
    return (
      <>
        <p className="text-sm">A conversation made a claim that conflicts with what the platform holds. The existing fact stays current; the claim waits here.</p>
        <div className="grid gap-4 md:grid-cols-2">{existing ? <FactCard title="Existing" f={existing} /> : null}{claim ? <FactCard title="Claim" f={claim} /> : null}</div>
        <Timeline facts={[existing, claim].filter((f): f is Fact => !!f)} />
      </>
    );
  }
  if (d.item_type === "graphiti_invalidation") {
    return (
      <>
        <Banner tone="amber">The extraction tried to end or change a recorded fact. The platform put it back; this item records that it happened.</Banner>
        <div className="grid gap-4 md:grid-cols-2">{claim ? <FactCard title="Restored fact" f={claim} /> : null}</div>
      </>
    );
  }
  return (
    <>
      <p className="text-sm">A rule asked for a human before this is applied.</p>
      {d.violations.length > 0 ? <ul className="list-disc pl-5 text-sm">{d.violations.map((v, i) => <li key={i}><span className="font-mono text-xs">{String(v.code)}</span> at <span className="font-mono text-xs">{String(v.path)}</span> ({String(v.rule_kind)})</li>)}</ul> : null}
      {claim ? <FactCard title="Fact" f={claim} /> : null}
    </>
  );
}

export function ReviewDetailPage() {
  const { reviewId = "" } = useParams();
  const can = useCan();
  const q = useQuery({
    queryKey: ["review", reviewId],
    enabled: can.readReviews,
    queryFn: async () => {
      const res = await api.GET("/v1/reviews/{review_id}", { params: { path: { review_id: reviewId } } });
      return { res, etag: res.response.headers.get("etag") ?? "" };
    },
  });
  if (!can.readReviews) return <PermissionDenied what="review items" />;
  if (q.isLoading) return <Loading label="Loading review item…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const { res, etag } = q.data!;
  const blocked = stateForStatus(res.response.status, "This review item", () => void q.refetch());
  if (blocked || !res.data) return <div className="space-y-4"><Link to="/reviews" className={btn.link}>← Review queue</Link>{blocked}</div>;
  const d = res.data;
  const open = d.status === "open";
  return (
    <div className="space-y-6">
      <Link to="/reviews" className={btn.link}>← Review queue</Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Review</h1><ReviewTypeBadge type={d.item_type} />
        {open ? <Badge tone="amber">open</Badge> : <Badge tone="green">decided: {d.decision?.replace(/_/g, " ")}</Badge>}
        <span className="font-mono text-xs text-slate-500">{d.id}</span>
      </div>
      <p className="text-sm text-slate-500">Opened {fmt(d.created_at)}{d.due_at ? ` · due ${fmt(d.due_at)}` : ""} · reason <span className="font-mono">{d.reason_code}</span></p>
      <Body d={d} />
      <section aria-label="Evidence" className="space-y-2">
        <h2 className="font-medium">Evidence</h2>
        {d.interactions.length === 0 ? <p className="text-sm text-slate-500">No interaction is attached to this item.</p> : <ul className="space-y-2">{d.interactions.map((i) => <EvidenceReveal key={i.interaction_id} reviewId={d.id} interaction={i} />)}</ul>}
      </section>
      {open && can.decideReviews && d.allowed_decisions.length > 0 ? <DecisionForm reviewId={d.id} allowed={d.allowed_decisions} revision={etag} onDecided={() => void q.refetch()} /> : null}
      {open && !can.decideReviews ? <Banner tone="slate" role="status">You can read this item but not decide it.</Banner> : null}
      {!open ? (
        <Banner tone="green" role="status">Decided {fmt(d.decided_at)} · {d.decision?.replace(/_/g, " ")} · rationale <span className="font-mono">{d.rationale_code}</span>{d.rationale_text ? <> · “{d.rationale_text}”</> : null}</Banner>
      ) : null}
    </div>
  );
}
