import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";

vi.mock("../graph/GraphView", () => ({ GraphView: () => <div /> }));

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const REVIEW = "r0000000-0000-4000-8000-000000000001";
const FACT = "f0000000-0000-4000-8000-000000000001";
const SARAH = "a0000000-0000-4000-8000-000000000001";
const MEM = "m0000000-0000-4000-8000-000000000001";
const REQ = "d0000000-0000-4000-8000-000000000001";
const withEtag = (body: unknown, etag = 'W/"rev-1"') => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json", etag } });

const review = (over: Record<string, unknown> = {}) => ({
  id: REVIEW, item_type: "deletion_exception", status: "open", rule_kind: "deletion", reason_code: "shared_memory_mentions_subject", revision: 1, created_at: "2026-10-14T10:00:00Z", due_at: null,
  command_id: null, subject_entity_id: null, candidate_entity_id: null, fact_id: null, competing_fact_id: null, interaction_id: null, memory_id: null, competing_memory_id: null, decision: null,
  rationale_code: null, decided_by: null, decided_at: null, submitter_revoked: false, agent_generated: false, allowed_decisions: ["redact_text", "delete_item", "retain_with_reason", "retry_verification"],
  rationale_text: null, entities: [], facts: [], candidates: [], interactions: [], violations: [], memories: [], deletion_request_id: REQ, detail: { reason: "shared_memory_mentions_subject", memory_id: MEM, location: "curated_memories" }, ...over,
});
const memoryView = (id: string, role: string, content: string) => ({
  role, id, type: "episodic", origin: "agent", status: role === "source" ? "active" : "proposed", summary: content.slice(0, 20), content, authority_class: "agent_generated", evidence_status: "unverified",
  classification: "internal", scopes: [{ kind: "workspace", ref: "*" }], subject_ids: [], created_by: "p1", recorded_at: "2026-10-14T09:00:00Z",
});
const fact = (over: Record<string, unknown> = {}) => ({
  id: FACT, rel_type: "WORKS_AT", source: { id: SARAH, type: "Person", name: "Sarah Johnson" }, target: { id: "o1", type: "Organization", name: "Acme" }, attributes: {}, fact_text: null,
  valid_from: "2024-06-01T00:00:00Z", valid_from_unknown: false, valid_to: null, source_recorded_at: null, recorded_at: "2026-10-11T09:00:00Z", superseded_at: null, succeeded_by_fact_id: null,
  status: "active", phase: "current", authority_class: "authoritative_system", classification: "internal", source_type: "command", evidence_status: "verified", evidence: [],
  decision_code: null, decided_at: null, source_command_id: "c1", source_interaction_id: null, source_system: "hr", source_ref: "evt_002", ontology_version_id: "v1", revision: 3, projection_status: "projected", ...over,
});
const entity = (over: Record<string, unknown> = {}) => ({
  id: SARAH, type: "Person", name: "Sarah Johnson", aliases: [], attributes: { role: "Manager" }, external_ids: [{ system: "hr", value: "E-1001" }], status: "active", origin: "command", classification: "internal",
  recorded_at: "2026-10-11T09:00:00Z", updated_at: "2026-10-11T09:00:00Z", ontology_version_id: "v1", revision: 4, projection_status: "projected", embedding_status: "ready", merged_into_id: null, relationships: [fact()], memories: 0, ...over,
});
const preview = (over: Record<string, unknown> = {}) => ({
  kind: "fact.reject", target_kind: "fact", target_id: FACT, authoritative: true, graph_operations: ["expire_edge"], dependent_memories: [], dependent_memory_counts: { active: 2 }, predecessor_fact_ids: ["p1"],
  sibling_fact_ids: [], open_review_ids: [], grant_holders: ["a", "b"], recent_context_count: 3, mention_count: null, ...over,
});

describe("Review Queue v2 cards", () => {
  it("shows a deletion exception without any of the text that names the subject, and a redact decision needs the new wording", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(review()),
      [`POST /v1/reviews/${REVIEW}/decision`]: async (req) => { posts.push({ body: await req.json(), match: req.headers.get("if-match") }); return json({ review_id: REVIEW, decision: "redact_text", status: "decided", command_id: "c1", revision: 2, projection_status: "not_applicable", entity_ids: [], fact_ids: [], memory_ids: [MEM] }); },
      "GET /v1/reviews": () => json({ items: [], next_cursor: null }),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    const card = await screen.findByTestId("deletion-exception");
    expect(within(card).getByText(/names the person whose data was deleted/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "open the memory" })).toHaveAttribute("href", `/memories/${MEM}`);
    expect(within(card).getByRole("link", { name: /d0000000/ })).toHaveAttribute("href", `/deletions/${REQ}`);
    await userEvent.click(screen.getByLabelText(/Redact the text/));
    const record = screen.getByRole("button", { name: "Record decision" });
    await userEvent.selectOptions(screen.getByLabelText("Rationale"), "other");
    expect(record).toBeDisabled(); // no replacement wording yet
    await userEvent.type(screen.getByRole("textbox", { name: "Your wording" }), "Sarah thanked a colleague.");
    expect(record).toBeEnabled();
    await userEvent.click(record);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ body: { decision: "redact_text", edited_content: "Sarah thanked a colleague." }, match: 'W/"rev-1"' });
  });

  it("shows a consolidation proposal next to the memories it would fold in", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(review({ item_type: "consolidation_proposal", reason_code: "consolidation_proposal", allowed_decisions: ["approve", "reject"], deletion_request_id: null, detail: {}, memory_id: "m-new",
        memories: [memoryView("m-new", "candidate", "Sarah cycles most weekends."), memoryView("m-a", "source", "Sarah rode on Saturday."), memoryView("m-b", "source", "Sarah rode on Sunday.")] })),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    const sources = await screen.findByTestId("consolidation-sources");
    expect(within(sources).getByText("Would be folded in (2)")).toBeInTheDocument();
    expect(within(sources).getByText("Sarah rode on Saturday.")).toBeInTheDocument();
    expect(screen.getByTestId("memory-card-proposal")).toHaveTextContent("Sarah cycles most weekends.");
    expect(screen.getByLabelText("Approve")).toBeInTheDocument();
  });

  it("warns that an authoritative override was made and offers only to acknowledge it", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(review({ item_type: "authoritative_override", allowed_decisions: ["acknowledge"], fact_id: FACT, facts: [fact()], detail: {}, deletion_request_id: null })),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    expect(await screen.findByRole("alert")).toHaveTextContent("authoritative system");
    expect(screen.getAllByRole("radio")).toHaveLength(1);
  });

  it("filters the queue by the new types", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/reviews": () => json({ items: [], next_cursor: null }) });
    renderApp(<App />, { route: "/reviews?type=deletion_exception" });
    const select = (await screen.findByLabelText("Filter by type")) as HTMLSelectElement;
    expect(select.value).toBe("deletion_exception");
    expect([...select.options].map((o) => o.value)).toEqual(expect.arrayContaining(["consolidation_proposal", "correction_request", "authoritative_override"]));
  });
});

describe("Corrections with an impact preview", () => {
  it("previews what a rejection changes, warns about an authoritative source, and sends the revision it read", async () => {
    const posts: Array<{ body: unknown; match: string | null; key: string | null }> = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/entities/${SARAH}`]: () => json(entity()),
      [`GET /v1/relationships/${FACT}/corrections/preview`]: () => json(preview()),
      [`POST /v1/relationships/${FACT}/corrections`]: async (req) => {
        posts.push({ body: await req.json(), match: req.headers.get("if-match"), key: req.headers.get("idempotency-key") });
        return json({ correction_id: "c1", command_id: "cmd1", target_kind: "fact", target_id: FACT, kind: "fact.reject", revision: 4, status_url: "/x", projection_status: "projected", authoritative_override: true, dependent_memory_ids: ["m1", "m2"], reopened_fact_ids: ["p1"], review_item_ids: ["r1"], grant_holders: [], moved: {}, new_target_id: null });
      },
      "GET /v1/commands": () => json({ items: [], next_cursor: null }),
    });
    renderApp(<App />, { route: `/entities/${SARAH}` });
    await userEvent.click(await screen.findByRole("button", { name: "Correct WORKS_AT fact" }));
    const dialog = await screen.findByRole("dialog");
    const impact = await within(dialog).findByLabelText("Impact preview");
    expect(impact).toHaveTextContent("authoritative system");
    expect(impact).toHaveTextContent("expire edge");
    expect(impact).toHaveTextContent("2 principals can read it");
    expect(impact).toHaveTextContent("3 recent context builds");
    const apply = within(dialog).getByRole("button", { name: "Apply correction" });
    expect(apply).toBeDisabled(); // a reason is required
    await userEvent.selectOptions(within(dialog).getByLabelText("Reason"), "source_error");
    await userEvent.click(apply);
    expect(await within(dialog).findByText(/The graph is up to date/)).toBeInTheDocument();
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ body: { kind: "reject", reason_code: "source_error", reopen_predecessors: true }, match: 'W/"rev-3"' });
    expect(posts[0]!.key).toBeTruthy();
    expect(within(dialog).getByText(/2 memory\(ies\) that rested on it were disputed/)).toBeInTheDocument();
  });

  it("explains a stale revision instead of leaving the dialog silent", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/entities/${SARAH}`]: () => json(entity()),
      [`GET /v1/relationships/${FACT}/corrections/preview`]: () => json(preview({ authoritative: false })),
      [`POST /v1/relationships/${FACT}/corrections`]: () => errorEnvelope(412, "precondition_failed"),
    });
    renderApp(<App />, { route: `/entities/${SARAH}` });
    await userEvent.click(await screen.findByRole("button", { name: "Correct WORKS_AT fact" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(await within(dialog).findByLabelText("Reason"), "other");
    await userEvent.click(within(dialog).getByRole("button", { name: "Apply correction" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("changed since you opened it");
  });

  it("splits only what is chosen: the new person's name and at least one mention or identifier are required", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/entities/${SARAH}`]: () => json(entity()),
      [`GET /v1/entities/${SARAH}/mentions`]: () => json([{ id: "mn1", interaction_id: "i1", episode_uuid: "e1", resolution: "graphiti_name_match", status: "accepted", created_at: "2026-10-11T09:00:00Z", source_system: "chat", source_ref: "msg-7", occurred_at: null, recorded_at: "2026-10-11T09:00:00Z" }]),
      [`GET /v1/entities/${SARAH}/corrections/preview`]: () => json(preview({ kind: "entity.split", target_kind: "entity", target_id: SARAH, authoritative: false, mention_count: 1, graph_operations: [], dependent_memory_counts: {}, predecessor_fact_ids: [], grant_holders: [], recent_context_count: null })),
      [`POST /v1/entities/${SARAH}/corrections`]: async (req) => { posts.push({ body: await req.json(), match: req.headers.get("if-match") }); return json({ correction_id: "c1", command_id: "cmd1", target_kind: "entity", target_id: SARAH, kind: "entity.split", revision: 5, status_url: "/x", projection_status: "projected", authoritative_override: false, dependent_memory_ids: [], reopened_fact_ids: [], review_item_ids: [], grant_holders: [], moved: { mentions: 1 }, new_target_id: "n1" }); },
      "GET /v1/commands": () => json({ items: [], next_cursor: null }),
    });
    renderApp(<App />, { route: `/entities/${SARAH}` });
    await userEvent.click(await screen.findByRole("button", { name: "Split…" }));
    const dialog = await screen.findByRole("dialog");
    const go = within(dialog).getByRole("button", { name: "Split into a new person" });
    expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("New person's name"), "Sarah Johnson (cyclist)");
    expect(go).toBeDisabled(); // nothing chosen to move
    await userEvent.click(await within(dialog).findByLabelText("Move mention in chat msg-7"));
    expect(go).toBeEnabled();
    await userEvent.click(go);
    expect(await within(dialog).findByText(/mentions moved/)).toBeInTheDocument();
    expect(posts[0]).toMatchObject({ match: 'W/"rev-4"', body: { kind: "split", reason_code: "wrong_identity", new_entity: { display_name: "Sarah Johnson (cyclist)", type: "Person" }, move: { mention_ids: ["mn1"], external_ids: [] } } });
  });

  it("hides the correction buttons from roles that cannot correct", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("operator")), [`GET /v1/entities/${SARAH}`]: () => json(entity()) });
    renderApp(<App />, { route: `/entities/${SARAH}` });
    await screen.findByTestId("entity-name");
    expect(screen.queryByRole("button", { name: "Split…" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Correct/ })).toBeNull();
  });
});

describe("Deletions", () => {
  const request = (over: Record<string, unknown> = {}) => ({
    request_id: REQ, kind: "subject", status: "completed_with_exceptions", reason_code: "data_subject_request", target_id: SARAH, received_at: "2026-10-14T10:00:00Z", frozen_at: "2026-10-14T10:00:00Z", planned_at: "2026-10-14T10:00:05Z",
    purge_started_at: "2026-10-14T10:00:09Z", verified_at: "2026-10-14T10:00:30Z", completed_at: "2026-10-14T10:00:31Z", cancelled_at: null, cooling_off_until: null, error_code: null, include_submissions: false,
    item_counts: { entities: 1, facts: 3, interactions: 1 }, open_exceptions: 1, revision: 4, status_url: `/v1/deletion-requests/${REQ}`, requested_by: "p1", receipt: null, ...over,
  });
  const receipt = { status: "completed_with_exceptions", completed_at: "2026-10-14T10:00:31Z", stores: { postgres: { deleted: 12, redacted: 2, retained: 1 }, graph: { deleted: 5 } }, retained: [{ class: "audit_log", reason: "ids_and_codes_only", count: 0 }], verification: { id_residuals: 0, text_residuals: 1, graph_residuals: 0, locations: ["curated_memories"] }, backup_copies_expire_by: "2026-11-13", journal_ref: "7:abc123" };

  it("lists requests with their state and exceptions, and lets an admin start one", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/deletion-requests": () => json([request(), request({ request_id: "d2", status: "blocked_legal_hold", open_exceptions: 0 })]),
      "POST /v1/deletion-requests": async (req) => { posts.push(await req.json()); return new Response(JSON.stringify(request({ status: "frozen" })), { status: 202, headers: { "content-type": "application/json" } }); },
      [`GET /v1/deletion-requests/${REQ}`]: () => json(request({ status: "frozen", receipt: null })),
    });
    renderApp(<App />, { route: "/deletions" });
    const table = await screen.findByRole("table", { name: "Deletion requests" });
    expect(within(table).getByText("completed with exceptions")).toBeInTheDocument();
    expect(within(table).getByText("blocked legal hold")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Request a deletion…" }));
    const dialog = await screen.findByRole("dialog");
    const go = within(dialog).getByRole("button", { name: "Delete" });
    expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Entity id"), SARAH);
    expect(go).toBeDisabled(); // the acknowledgement is part of the request
    await userEvent.click(within(dialog).getByLabelText(/permanently/));
    expect(go).toBeEnabled();
    await userEvent.click(go);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ kind: "subject", target_id: SARAH, reason_code: "data_subject_request", include_submissions: false });
  });

  it("asks for the workspace slug before a workspace deletion", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("workspace_admin")), "GET /v1/deletion-requests": () => json([]) });
    renderApp(<App />, { route: "/deletions" });
    await userEvent.click(await screen.findByRole("button", { name: "Request a deletion…" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByLabelText(/The whole workspace/));
    await userEvent.click(within(dialog).getByLabelText(/permanently/));
    const go = within(dialog).getByRole("button", { name: "Delete" });
    expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Workspace slug"), "wrong");
    expect(go).toBeDisabled();
    await userEvent.clear(within(dialog).getByLabelText("Workspace slug"));
    await userEvent.type(within(dialog).getByLabelText("Workspace slug"), "demo");
    expect(go).toBeEnabled();
  });

  it("shows the receipt with counts by store, what was kept, residual locations and the backup expiry", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("operator")), [`GET /v1/deletion-requests/${REQ}`]: () => json(request({ receipt })) });
    renderApp(<App />, { route: `/deletions/${REQ}` });
    const r = await screen.findByTestId("receipt");
    expect(within(r).getByRole("table", { name: "Receipt by store" })).toHaveTextContent("postgres");
    expect(within(r).getByText("1 residual")).toBeInTheDocument();
    expect(within(r).getByText(/curated_memories/)).toBeInTheDocument();
    expect(within(r).getByTestId("backup-expiry")).toHaveTextContent("2026-11-13");
    expect(screen.getByText(/1 exception need a person/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel this deletion/ })).toBeNull();
  });

  it("offers cancel only while a workspace deletion is cooling off, and retry only for a failed one", async () => {
    const cancelled: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/deletion-requests/${REQ}`]: () => json(request({ kind: "workspace", status: "frozen", cooling_off_until: "2026-10-20T10:00:00Z", open_exceptions: 0, receipt: null })),
      [`POST /v1/deletion-requests/${REQ}/cancel`]: () => { cancelled.push("cancel"); return json(request({ status: "cancelled" })); },
    });
    renderApp(<App />, { route: `/deletions/${REQ}` });
    expect(await screen.findByText(/Cooling off until/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Cancel this deletion…" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel the deletion" }));
    await waitFor(() => expect(cancelled).toEqual(["cancel"]));
  });

  it("keeps holds and suppressions for admins, and shows only the hash prefix", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/legal-holds": () => json([{ id: "h1aaaaaa-0000-4000-8000-000000000001", scope_kind: "subject", target_id: SARAH, reason_code: "litigation", note: "", created_by: "p1", created_at: "2026-10-14T10:00:00Z", released_at: null, released_by: null }]),
      "GET /v1/suppressions": () => json([{ request_id: REQ, kind: "external_id", value_hmac_prefix: "ab12cd34ef56", created_at: "2026-10-14T10:00:30Z", lifted_at: null, lifted_by: null }]),
    });
    renderApp(<App />, { route: "/deletions?tab=holds" });
    expect(await screen.findByRole("table", { name: "Legal holds" })).toHaveTextContent("litigation");
    await userEvent.click(screen.getByRole("tab", { name: "Suppressions" }));
    const t = await screen.findByRole("table", { name: "Suppressions" });
    expect(t).toHaveTextContent("ab12cd34ef56");
    expect(t).not.toHaveTextContent("E-1001");
  });

  it("an app writer sees the requests page but not holds or the new-deletion button", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("app_writer")), "GET /v1/deletion-requests": () => json([]) });
    renderApp(<App />, { route: "/deletions" });
    await screen.findByText("No deletion requests");
    expect(screen.queryByRole("button", { name: "Request a deletion…" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Legal holds" })).toBeNull();
  });
});

describe("Settings", () => {
  const settings = (over: Record<string, unknown> = {}) => ({
    version: 2, updated_at: "2026-10-14T10:00:00Z", updated_by: "p1",
    settings: { retention: { raw_content_days: 30, normalized_content_days: null, episode_content_days: null, context_trace_days: null, model_calls_days: null, memory_archive_after_days: null }, egress: { denied_classifications: ["restricted"] }, quotas: {}, backup_retention_days: null },
    effective_egress: { platform_denied: ["local_only"], workspace_denied: ["restricted"], effective_denied: ["restricted", "local_only"], effective_allowed: ["public", "internal", "confidential"] }, ...over,
  });

  it("can only narrow egress: the platform's class is locked and there is no way to allow more", async () => {
    const puts: Array<{ body: { egress: { denied_classifications: string[] }; retention: { raw_content_days: number } }; match: string | null }> = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/workspaces/current/settings": () => json(settings()),
      "GET /v1/workspaces/current/settings/history": () => json([{ version: 2, updated_at: "2026-10-14T10:00:00Z", updated_by: "p1", settings: settings().settings }, { version: 1, updated_at: "2026-10-13T10:00:00Z", updated_by: "p1", settings: settings().settings }]),
      "PUT /v1/workspaces/current/settings": async (req) => { puts.push({ body: await req.json(), match: req.headers.get("if-match") }); return json(settings({ version: 3 })); },
    });
    renderApp(<App />, { route: "/settings" });
    const local = (await screen.findByLabelText("Deny local_only")) as HTMLInputElement;
    expect(local.checked).toBe(true);
    expect(local.disabled).toBe(true); // the platform never sends it: not the workspace's to change
    expect(screen.getByLabelText("Deny restricted")).toBeChecked();
    expect(screen.queryByLabelText(/allow/i)).toBeNull();
    const save = screen.getByRole("button", { name: "Save settings" });
    expect(save).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Deny confidential"));
    await userEvent.clear(screen.getByLabelText("Raw interaction content"));
    await userEvent.type(screen.getByLabelText("Raw interaction content"), "14");
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]!.match).toBe('W/"rev-2"');
    expect(puts[0]!.body.egress.denied_classifications.sort()).toEqual(["confidential", "restricted"]);
    expect(puts[0]!.body.retention.raw_content_days).toBe(14);
    expect(await screen.findByText("Saved as a new version.")).toBeInTheDocument();
    expect(screen.getByText("Version 2 ·", { exact: false })).toBeInTheDocument();
  });

  it("says when someone else changed the settings first", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/workspaces/current/settings": () => json(settings()),
      "GET /v1/workspaces/current/settings/history": () => json([]),
      "PUT /v1/workspaces/current/settings": () => errorEnvelope(412, "precondition_failed"),
    });
    renderApp(<App />, { route: "/settings" });
    await userEvent.click(await screen.findByLabelText("Deny internal"));
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone else changed the settings");
  });

  it("is for workspace admins only", async () => {
    const calls = mockApi({ "GET /v1/me": () => json(meAs("reviewer")) });
    renderApp(<App />, { route: "/settings" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(calls).not.toContain("GET /v1/workspaces/current/settings");
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  });
});

describe("Privacy audit", () => {
  const page = (items: unknown[]) => ({ items, next_cursor: null, egress: [{ purpose: "graph_extraction", classification: "internal", outcome: "success", count: 7 }, { purpose: "graph_extraction", classification: "restricted", outcome: "egress_denied", count: 2 }] });
  const item = (over: Record<string, unknown> = {}) => ({ id: "x1", at: "2026-10-14T10:00:00Z", actor_principal_id: "11111111-2222-4333-8444-555555555555", action: "interaction.content_read", category: "reveals", resource_type: "interaction", resource_id: "abcdef12-0000-4000-8000-000000000001", outcome: "ok", metadata: {}, ...over });

  it("shows the timeline and the model calls by decision, and filters by category", async () => {
    const urls: string[] = [];
    const calls = mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/governance/privacy-audit": (req) => { urls.push(new URL(req.url).search); return json(page([item(), item({ id: "x2", action: "legal_hold.created", category: "holds" })])); },
    });
    renderApp(<App />, { route: "/privacy" });
    const timeline = await screen.findByRole("table", { name: "Privacy timeline" });
    expect(timeline).toHaveTextContent("interaction.content_read");
    expect(timeline).toHaveTextContent("legal_hold.created");
    const egress = screen.getByRole("table", { name: "Model calls by purpose" });
    expect(egress).toHaveTextContent("egress denied");
    expect(egress).toHaveTextContent("7");
    await userEvent.selectOptions(screen.getByLabelText("Filter by category"), "holds");
    await waitFor(() => expect(urls.some((u) => u.includes("category=holds"))).toBe(true));
    expect(calls.filter((c) => c === "GET /v1/governance/privacy-audit").length).toBeGreaterThan(1);
  });

  it("is for workspace admins only", async () => {
    const calls = mockApi({ "GET /v1/me": () => json(meAs("operator")) });
    renderApp(<App />, { route: "/privacy" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(calls).not.toContain("GET /v1/governance/privacy-audit");
  });
});

describe("Ontology simulation", () => {
  const VERSION = "v0000000-0000-4000-8000-000000000001";
  const SPEC = { ontology: { key: "personal-knowledge", version: "1.5.0", mode: "strict" }, entity_types: { Person: { attributes: { name: { type: "string", required: true } }, rules: [] } }, relationship_types: {}, rules: [] };
  const draft = {
    id: VERSION, ontology_key: "personal-knowledge", version: "1.5.0", state: "draft", active: false, content_hash: null, revision: 1, error_count: 0, notes: "", created_at: "2026-10-14T10:00:00Z",
    published_at: null, retired_at: null, spec: SPEC, source_text: "ontology:\n  id: personal-knowledge\n", base_version_id: null, activated: false,
    validation_report: { errors: [], warnings: [], rule_tests: [], content_hash: null },
  };
  const DIFF = { from_version: null, to_version: "1.5.0", changes: [], impact: [] };
  const sim = (over: Record<string, unknown> = {}) => ({ id: "s1", draft_version_id: VERSION, status: "previewed", sample_count: 3, runnable_count: 2, skipped: { policy: 1 }, cost_estimate: 0.0123, cost_actual: null, estimate: { calls: 6, prompt_tokens: 4200, completion_tokens: 600 }, report: {}, created_at: "2026-10-14T10:00:00Z", started_at: null, finished_at: null, status_url: "/v1/ontologies/simulations/s1", ...over });

  it("estimates first, runs only after a confirmation, and reports counts", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/ontologies/versions/${VERSION}`]: () => withEtag(draft),
      [`GET /v1/ontologies/versions/${VERSION}/diff`]: () => json(DIFF),
      "GET /v1/ontologies/rule-catalog": () => json({ stages: [], kinds: [] }),
      "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }),
      [`POST /v1/ontologies/versions/${VERSION}/simulate`]: async (req) => {
        const body = (await req.json()) as { confirm: boolean };
        posts.push(body);
        return body.confirm ? new Response(JSON.stringify(sim({ status: "running" })), { status: 202, headers: { "content-type": "application/json" } }) : json(sim());
      },
      "GET /v1/ontologies/simulations/s1": () => json(sim({ status: "completed", cost_actual: 0.0098, report: { episodes: 2, facts_by_type: { MANAGES: 1 }, nodes_by_type: { Person: 2 }, new_relationship_types: ["MANAGES"], dropped_by_draft: {}, identity: { matched_existing: 1, new: 1 }, diff: { changes: 3, breaking: 0 } } })),
    });
    renderApp(<App />, { route: `/ontology/versions/${VERSION}` });
    await userEvent.click(await screen.findByRole("button", { name: "Simulate…" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Estimate the cost" }));
    const estimate = await within(dialog).findByTestId("simulation-estimate");
    expect(estimate).toHaveTextContent("2 of 3");
    expect(estimate).toHaveTextContent("1 skipped by policy");
    expect(estimate).toHaveTextContent("$0.0123");
    expect(posts).toEqual([{ sample: { n: 3 }, confirm: false, simulation_id: null }]); // the estimate called no model
    await userEvent.click(within(dialog).getByRole("button", { name: "Run the simulation" }));
    const report = await within(dialog).findByTestId("simulation-report", undefined, { timeout: 5000 });
    expect(report).toHaveTextContent("1 MANAGES");
    expect(report).toHaveTextContent("MANAGES");
    expect(report).toHaveTextContent("staging graph is already removed");
    expect(posts[1]).toEqual({ sample: { n: 3 }, confirm: true, simulation_id: "s1" });
  });

  it("is not offered to a role that cannot write the ontology", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), [`GET /v1/ontologies/versions/${VERSION}`]: () => withEtag(draft), [`GET /v1/ontologies/versions/${VERSION}/diff`]: () => json(DIFF), "GET /v1/ontologies/rule-catalog": () => json({ stages: [], kinds: [] }), "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }) });
    renderApp(<App />, { route: `/ontology/versions/${VERSION}` });
    await screen.findByTestId("version-title");
    expect(screen.queryByRole("button", { name: "Simulate…" })).toBeNull();
  });
});

describe("Memory corrections", () => {
  it("previews the impact of rejecting a memory", async () => {
    const memory = {
      id: MEM, type: "semantic", status: "active", content: "Sarah prefers tea.", summary: "tea", origin: "user", authority_class: "user_asserted", evidence_status: "unverified", classification: "internal", scopes: [{ kind: "workspace", ref: "*" }],
      subject_ids: [], topic_ids: [], purpose: "pref", confidence: null, importance: 0.5, recorded_at: "2026-10-14T09:00:00Z", occurred_at: null, effective_from: null, effective_until: null, expires_at: null, last_confirmed_at: null,
      created_by: "p1", command_id: "c1", run_id: null, assertion: null, embedding_status: "ready", embedding: { status: "ready", model_id: "intfloat/multilingual-e5-small", version: 1, dim: 384, embedded_at: "2026-10-14T09:01:00Z" }, evidence: [], lineage: [], derived_by_alias: null, pipeline_version: null, prompt_version: null, revision: 5,
    };
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/memories/${MEM}`]: () => withEtag(memory, 'W/"rev-5"'),
      [`GET /v1/memories/${MEM}/usage`]: () => json({ builds: 0, last_used_at: null, recent_context_ids: [] }),
      [`GET /v1/memories/${MEM}/corrections/preview`]: () => json(preview({ kind: "memory.reject", target_kind: "memory", authoritative: false, graph_operations: [], dependent_memory_counts: {}, predecessor_fact_ids: [], grant_holders: ["a"], recent_context_count: 1 })),
    });
    renderApp(<App />, { route: `/memories/${MEM}` });
    await userEvent.click(await screen.findByRole("button", { name: "Correct (with impact)…" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByLabelText("Impact preview")).toHaveTextContent("1 principal can read it");
    expect(within(dialog).getByRole("button", { name: "Apply correction" })).toBeDisabled();
  });
});
