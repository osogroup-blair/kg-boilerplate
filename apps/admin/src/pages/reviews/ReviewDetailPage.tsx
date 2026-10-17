import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { AuthorityBadge, Badge, Banner, btn, ClassificationBadge, EvidenceBadge, FactStatusBadge, MemoryStatusBadge, MemoryTypeBadge, ReviewTypeBadge, SubmitterRevokedBadge } from "../../components/ui";
import { displayValue, fmt, fmtDate, isRedacted } from "../../lib/format";
import { ContentView } from "../interactions/InteractionDetailPage";
import { CorrectFactDialog } from "../corrections/CorrectionDialogs";
import { DecisionForm } from "./DecisionForm";

type Detail = components["schemas"]["ReviewDetail"];
type Entity = components["schemas"]["ReviewEntityView"];
type Fact = components["schemas"]["FactOut"];
type Content = components["schemas"]["ContentOut"];
type MemoryView = components["schemas"]["ReviewMemoryView"];

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

/** A memory a review item is about. The text is shown as text; the scopes are the ones THIS reviewer may use (the API filters them). */
function MemoryCard({ title, m }: { title: string; m: MemoryView }) {
  return (
    <section aria-label={title} className="space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800" data-testid={`memory-card-${title.toLowerCase()}`}>
      <h3 className="text-sm font-medium text-slate-500">{title}</h3>
      <blockquote className="whitespace-pre-wrap text-sm">{m.content}</blockquote>
      <div className="flex flex-wrap gap-1"><MemoryTypeBadge type={m.type} /><MemoryStatusBadge status={m.status} /><EvidenceBadge status={m.evidence_status} /><AuthorityBadge authority={m.authority_class} /><ClassificationBadge classification={m.classification} /></div>
      <dl className="grid grid-cols-[7rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Origin</dt><dd>{m.origin}</dd>
        <dt className="text-slate-500">Scopes</dt><dd>{m.scopes.map((s) => `${s.kind}${!s.ref || s.ref === "*" ? "" : `:${s.ref.slice(0, 8)}…`}`).join(", ") || "—"}</dd>
        <dt className="text-slate-500">Recorded</dt><dd>{fmt(m.recorded_at)}</dd>
      </dl>
      <Link className="text-sm underline" to={`/memories/${m.id}`}>Open memory</Link>
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

const EXCEPTION_REASONS: Record<string, string> = {
  shared_memory_mentions_subject: "A memory about someone else names the person whose data was deleted. It was kept: only a person can decide what to do with its wording.",
  legal_hold: "A legal hold covers the target, so nothing was purged. Releasing the hold resumes the request.",
  residual_text: "Text that names the deleted subject was found after the purge.",
  residual_id: "A deleted id was still found after the purge.",
  graph_residual: "The graph still holds text or an id of the deleted subject.",
};

/** A deletion that could not finish by itself. The text that names the subject is never shown here (it would put it on screen again); the memory is opened through its own page. */
function DeletionExceptionCard({ d }: { d: Detail }) {
  const memoryId = typeof d.detail?.memory_id === "string" ? d.detail.memory_id : null;
  const location = typeof d.detail?.location === "string" ? d.detail.location : null;
  return (
    <section aria-label="Deletion exception" className="space-y-2 rounded-lg border border-red-200 p-3 dark:border-red-900" data-testid="deletion-exception">
      <p className="text-sm">{EXCEPTION_REASONS[d.reason_code] ?? "A deletion needs a decision before it can finish."}</p>
      <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Deletion request</dt><dd>{d.deletion_request_id ? <Link className="underline" to={`/deletions/${d.deletion_request_id}`}>{d.deletion_request_id.slice(0, 8)}…</Link> : "—"}</dd>
        {location ? <><dt className="text-slate-500">Where</dt><dd className="font-mono text-xs">{location}</dd></> : null}
        {memoryId ? <><dt className="text-slate-500">Memory</dt><dd><Link className="underline" to={`/memories/${memoryId}`}>open the memory</Link></dd></> : null}
      </dl>
      <p className="text-xs text-slate-500"><strong>Redact</strong> replaces the wording and removes the original for good. <strong>Delete</strong> removes the whole item. <strong>Retain</strong> keeps it under a recorded reason. <strong>Check again</strong> re-runs the verification.</p>
    </section>
  );
}

/** A synthesised memory next to the memories it would fold in: approving supersedes them (their evidence is kept by reference), rejecting changes nothing. */
function ConsolidationCard({ d }: { d: Detail }) {
  const proposal = d.memories.find((m) => m.role === "candidate");
  const sources = d.memories.filter((m) => m.role === "source");
  return (
    <>
      <p className="text-sm">The consolidation job proposes one statement that sums up several memories. Nothing took effect: the sources stay active until you approve.</p>
      <div className="grid gap-4 md:grid-cols-2">
        {proposal ? <MemoryCard title="Proposal" m={proposal} /> : <Banner tone="amber" role="status">The proposed memory is not visible to you.</Banner>}
        <section aria-label="Sources" className="space-y-2" data-testid="consolidation-sources">
          <h3 className="text-sm font-medium text-slate-500">Would be folded in ({sources.length})</h3>
          {sources.map((m, i) => <MemoryCard key={m.id} title={`Source ${i + 1}`} m={m} />)}
        </section>
      </div>
    </>
  );
}

function CorrectionButton({ fact }: { fact: Fact }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className={btn.secondary} onClick={() => setOpen(true)}>Correct this fact…</button>
      {open ? <CorrectFactDialog fact={fact} onClose={() => setOpen(false)} /> : null}
    </>
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
  if (d.item_type === "memory_proposal" || d.item_type === "memory_conflict") {
    const candidate = d.memories.find((m) => m.role === "candidate");
    const competing = d.memories.find((m) => m.role === "competing");
    return (
      <>
        <p className="text-sm">
          {d.item_type === "memory_proposal"
            ? "A memory waits for approval before agents are told it. It was proposed by an agent, an app or the extraction stage, or it is procedural (a way of behaving), which is never activated without a person."
            : competing
              ? "A new memory disagrees with one the platform holds at higher authority. The existing memory stays current until you decide."
              : "A curator disputed this memory. It is not served while the dispute is open."}
        </p>
        <div className="grid gap-4 md:grid-cols-2">{competing ? <MemoryCard title="Existing" m={competing} /> : null}{candidate ? <MemoryCard title={competing ? "Claim" : "Memory"} m={candidate} /> : null}</div>
        {d.memories.length === 0 ? <Banner tone="amber" role="status">The memory is not visible to you, so it cannot be decided from here.</Banner> : null}
      </>
    );
  }
  if (d.item_type === "source_compromised") {
    const memory = d.memories.find((m) => m.role === "candidate");
    return (
      <>
        <Banner tone="red" role="alert">The principal that submitted the source of this was revoked as <strong>compromised</strong>. It is disputed, and contexts show it only as a caution, until you decide.</Banner>
        <div className="grid gap-4 md:grid-cols-2">{memory ? <MemoryCard title="Memory" m={memory} /> : null}{claim ? <FactCard title="Fact" f={claim} /> : null}</div>
        <p className="text-sm text-slate-500"><strong>Restore</strong> puts it back as it was. <strong>Confirm removal</strong> refuses it for good (the record is kept, not served).</p>
      </>
    );
  }
  if (d.item_type === "deletion_exception") return <DeletionExceptionCard d={d} />;
  if (d.item_type === "consolidation_proposal") return <ConsolidationCard d={d} />;
  if (d.item_type === "authoritative_override") {
    return (
      <>
        <Banner tone="red" role="alert">An administrator or reviewer corrected a fact that an <strong>authoritative system</strong> asserted. The correction stands; this item exists so the source's owner is told. Acknowledge it once they have been.</Banner>
        <div className="grid gap-4 md:grid-cols-2">{claim ? <FactCard title="Corrected fact" f={claim} /> : null}{existing ? <FactCard title="Previous fact" f={existing} /> : null}</div>
      </>
    );
  }
  if (d.item_type === "correction_request") {
    return (
      <>
        <p className="text-sm">Someone asked for this fact to be corrected. Deciding records the request's outcome; the correction itself is its own audited command, made from the fact or from here.</p>
        <div className="grid gap-4 md:grid-cols-2">{claim ? <FactCard title="Fact" f={claim} /> : null}</div>
        {claim && claim.status !== "rejected" && d.status === "open" ? <CorrectionButton fact={claim} /> : null}
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
        {d.agent_generated ? <Badge tone="amber" title="Produced by an agent">agent generated</Badge> : null}
        {d.submitter_revoked ? <SubmitterRevokedBadge /> : null}
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
