import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";

// Cytoscape draws on a canvas jsdom does not have; the table view carries the same content and is what these tests read.
vi.mock("../graph/GraphView", () => ({ GraphView: ({ graph }: { graph: { nodes: unknown[] } }) => <div data-testid="graph-canvas-stub">{graph.nodes.length} nodes</div> }));

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const REVIEW = "r0000000-0000-4000-8000-000000000001";
const SARAH = "a0000000-0000-4000-8000-000000000001";
const CAND = "a0000000-0000-4000-8000-000000000002";
const INTERACTION = "e0000000-0000-4000-8000-000000000001";
const FACT_EM = "f0000000-0000-4000-8000-000000000001";
const FACT_CLAIM = "f0000000-0000-4000-8000-000000000002";

const item = (over: Record<string, unknown> = {}) => ({
  id: REVIEW, item_type: "identity_match", status: "open", rule_kind: "identity_resolution", reason_code: "similar_entity", revision: 1, created_at: "2026-10-11T10:00:00Z", due_at: "2026-10-18T10:00:00Z",
  command_id: null, subject_entity_id: SARAH, candidate_entity_id: CAND, fact_id: null, competing_fact_id: null, interaction_id: INTERACTION, decision: null, rationale_code: null, decided_by: null, decided_at: null, ...over,
});
const entity = (id: string, over: Record<string, unknown> = {}) => ({
  id, type: "Person", name: "Sarah Johnson", aliases: [], attributes: { birth_date: { redacted: true } }, external_ids: [], status: "active", origin: "command", classification: "internal", mention_count: 1, fact_count: 2, merged_into_id: null, ...over,
});
const fact = (id: string, over: Record<string, unknown> = {}) => ({
  id, rel_type: "WORKS_AT", source: { id: SARAH, type: "Person", name: "Sarah Johnson" }, target: { id: "o1", type: "Organization", name: "Acme" }, attributes: {}, fact_text: null,
  valid_from: "2024-06-01T00:00:00Z", valid_from_unknown: false, valid_to: null, source_recorded_at: null, recorded_at: "2026-10-11T09:00:00Z", superseded_at: null, succeeded_by_fact_id: null,
  status: "active", phase: "current", authority_class: "authoritative_system", classification: "internal", source_type: "command", evidence_status: "verified", evidence: [{ kind: "command", command_id: "c1", added_at: "2026-10-11T09:00:00Z" }],
  decision_code: null, decided_at: null, source_command_id: "c1", source_interaction_id: null, source_system: "hr", source_ref: "evt_002", ontology_version_id: "v1", revision: 1, projection_status: "projected", ...over,
});
const identityDetail = (over: Record<string, unknown> = {}) => ({
  ...item(), allowed_decisions: ["same_entity", "different_entity", "needs_more_evidence"], rationale_text: null,
  entities: [entity(SARAH), entity(CAND, { status: "candidate", origin: "extracted", mention_count: 1, fact_count: 1 })], facts: [],
  candidates: [{ entity_id: SARAH, trigram: 1, cosine: 1, reasons: ["graphiti_name_match"] }],
  interactions: [{ interaction_id: INTERACTION, episode_uuid: "ep1", source_system: "chat", source_ref: "msg-1", occurred_at: "2026-10-10T09:00:00Z", recorded_at: "2026-10-11T10:00:00Z" }], violations: [], ...over,
});
const contradictionDetail = () => ({
  ...item({ item_type: "contradiction", rule_kind: "conflict_policy", reason_code: "authority_conflict", subject_entity_id: null, candidate_entity_id: null, fact_id: FACT_CLAIM, competing_fact_id: FACT_EM }),
  allowed_decisions: ["keep_existing", "mark_disputed", "accept_claim"], rationale_text: null, entities: [],
  facts: [fact(FACT_EM), fact(FACT_CLAIM, { status: "proposed", phase: "current", authority_class: "conversation_report", evidence_status: "unverified", source_type: "extracted", fact_text: "Sarah Johnson works at NewCo", valid_from: "2026-10-10T00:00:00Z", target: { id: "o2", type: "Organization", name: "NewCo" }, evidence: [{ kind: "episode", interaction_id: INTERACTION, episode_uuid: "ep1", added_at: "2026-10-11T10:00:00Z" }] })],
  candidates: [], interactions: [], violations: [],
});
const withEtag = (body: unknown, etag = 'W/"rev-1"') => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json", etag } });

describe("Review queue", () => {
  it("lists open items by type and age for a reviewer", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      "GET /v1/reviews": () => json({ items: [item(), item({ id: "r2", item_type: "contradiction", reason_code: "authority_conflict" })], next_cursor: null }),
    });
    renderApp(<App />, { route: "/reviews" });
    expect(await screen.findByText("similar_entity")).toBeInTheDocument();
    expect(screen.getByText("authority_conflict")).toBeInTheDocument();
    expect(screen.getAllByText("identity match").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Review Queue" })).toBeInTheDocument();
  });
  it("shows a permission state to roles that cannot read it, without calling the API", async () => {
    const calls = mockApi({ "GET /v1/me": () => json(meAs("agent")) });
    renderApp(<App />, { route: "/reviews" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(calls).not.toContain("GET /v1/reviews");
  });
  it("answers a 403 from the API with the same state", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("workspace_admin")), "GET /v1/reviews": () => errorEnvelope(403, "forbidden") });
    renderApp(<App />, { route: "/reviews" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
  });
  it("is empty when nothing waits", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/reviews": () => json({ items: [], next_cursor: null }) });
    renderApp(<App />, { route: "/reviews" });
    expect(await screen.findByText("Nothing to review")).toBeInTheDocument();
  });
});

describe("Review detail", () => {
  it("shows an identity match side by side and keeps the decision form disabled until a rationale is chosen", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(identityDetail()),
      [`POST /v1/reviews/${REVIEW}/decision`]: async (req) => { posts.push({ body: await req.json(), match: req.headers.get("if-match"), key: req.headers.get("idempotency-key") }); return json({ review_id: REVIEW, decision: "different_entity", status: "decided", command_id: "c9", revision: 2, projection_status: "projected", entity_ids: [], fact_ids: [] }); },
      "GET /v1/reviews": () => json({ items: [], next_cursor: null }),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    const existing = await screen.findByTestId("entity-card-existing");
    expect(within(existing).getByText("Sarah Johnson")).toBeInTheDocument();
    expect(within(existing).getByText("from a trusted source")).toBeInTheDocument();
    const candidate = screen.getByTestId("entity-card-candidate");
    expect(within(candidate).getByText("candidate")).toBeInTheDocument();
    expect(within(candidate).getByText("extracted")).toBeInTheDocument();
    expect(within(existing).getByText("redacted")).toBeInTheDocument(); // a sensitive attribute is never shown
    const submit = screen.getByRole("button", { name: "Record decision" });
    expect(submit).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: "Different entities" }));
    expect(submit).toBeDisabled(); // a decision alone is not enough
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Rationale" }), "different_real_world_entity");
    expect(submit).toBeEnabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Notes" }), "lives in Lyon");
    await userEvent.click(submit);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ body: { decision: "different_entity", rationale_code: "different_real_world_entity", rationale_text: "lives in Lyon" }, match: 'W/"rev-1"' });
    expect((posts[0] as { key: string }).key).toMatch(/.{8,}/);
  });

  it("shows a contradiction as existing versus claim with both timelines, and explains a refused acceptance", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(contradictionDetail()),
      [`POST /v1/reviews/${REVIEW}/decision`]: () => errorEnvelope(409, "requires_correction"),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    const existing = await screen.findByTestId("fact-card-existing");
    const claim = screen.getByTestId("fact-card-claim");
    expect(within(existing).getByText("verified")).toBeInTheDocument();
    expect(within(claim).getByText("unverified")).toBeInTheDocument();
    expect(within(claim).getByText("proposed")).toBeInTheDocument();
    expect(within(claim).getByText(/Sarah Johnson works at NewCo/)).toBeInTheDocument();
    expect(screen.getByTestId(`timeline-${FACT_EM}`)).toBeInTheDocument();
    expect(screen.getByTestId(`timeline-${FACT_CLAIM}`)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Accept the claim" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Rationale" }), "verified_with_source");
    await userEvent.click(screen.getByRole("button", { name: "Record decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("correction, not a review decision");
  });

  it("gates the evidence read behind a confirmation and renders content as text only", async () => {
    const gets: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(identityDetail()),
      [`GET /v1/reviews/${REVIEW}/evidence/${INTERACTION}`]: () => { gets.push("evidence"); return json({ form: "normalized", kind: "message", content: "<img src=x onerror=alert(1)> Sarah Johnson from the cycling club", content_type: null, metadata: {}, subject_refs: [], actor_ref: null }); },
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    await userEvent.click(await screen.findByRole("button", { name: "View evidence…" }));
    expect(gets).toEqual([]); // nothing is read until the reviewer confirms
    await userEvent.click(await screen.findByRole("button", { name: "View and audit" }));
    const view = await screen.findByTestId("revealed-content");
    expect(view).toHaveTextContent("<img src=x onerror=alert(1)>"); // shown as text
    expect(view.querySelector("img")).toBeNull();
    expect(gets).toEqual(["evidence"]);
  });

  it("is read-only for a role that can read but not decide", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("operator")), [`GET /v1/reviews/${REVIEW}`]: () => withEtag(identityDetail()) });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument(); // an operator has no review.read at all
  });

  it("shows a decided item with its rationale and no form", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(identityDetail({ status: "decided", decision: "different_entity", rationale_code: "different_real_world_entity", decided_at: "2026-10-12T08:00:00Z", allowed_decisions: [] })),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    expect(await screen.findByText(/different_real_world_entity/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record decision" })).toBeNull();
  });
});

describe("Graph Explorer", () => {
  const graph = (over: Record<string, unknown> = {}) => ({
    center_id: SARAH, truncated: false,
    nodes: [{ id: SARAH, type: "Person", name: "Sarah Johnson", status: "active", origin: "command", hop: 0 }, { id: "o1", type: "Organization", name: "Acme", status: "active", origin: "command", hop: 1 }, { id: "t1", type: "Topic", name: "Entrepreneurship", status: "active", origin: "command", hop: 1 }],
    edges: [
      { id: FACT_EM, rel_type: "WORKS_AT", source_id: SARAH, target_id: "o1", status: "active", evidence_status: "verified", authority_class: "authoritative_system", phase: "current", valid_from: "2024-06-01T00:00:00Z", valid_to: null, fact_text: null },
      { id: FACT_CLAIM, rel_type: "CONSIDERING", source_id: SARAH, target_id: "t1", status: "active", evidence_status: "unverified", authority_class: "conversation_report", phase: "current", valid_from: null, valid_to: null, fact_text: "Sarah Johnson is considering entrepreneurship" },
    ], ...over,
  });
  const routes = (over: Record<string, () => Response | Promise<Response>> = {}) => ({
    "GET /v1/me": () => json(meAs("reviewer")),
    "GET /v1/ontologies/active": () => json({ version: { id: "v1" }, entity_types: ["Person", "Organization", "Topic"], relationship_types: [] }),
    "GET /v1/entities": () => json({ items: [{ id: SARAH, type: "Person", name: "Sarah Johnson", status: "active", external_ids: [] }], next_cursor: null }),
    [`GET /v1/entities/${SARAH}/graph`]: () => json(graph()),
    [`GET /v1/entities/${SARAH}`]: () => json({ ...entity(SARAH), relationships: [], revision: 1, ontology_version_id: "v1", recorded_at: "2026-10-11T09:00:00Z", updated_at: "2026-10-11T09:00:00Z", projection_status: "projected", embedding_status: "embedded", projected_at: null }),
    [`GET /v1/entities/${SARAH}/summary`]: () => json({ entity_id: SARAH, summary: "Sarah is an engineering manager.", label: "derived_unverified", graph_available: true }),
    ...over,
  });

  it("shows the neighbourhood as an accessible table with statuses and evidence badges", async () => {
    mockApi(routes());
    renderApp(<App />, { route: `/graph?entity=${SARAH}` });
    const table = await screen.findByRole("table", { name: "Relationships in this neighbourhood" });
    const em = within(table).getByTestId(`edge-row-${FACT_EM}`);
    expect(within(em).getByText("verified")).toBeInTheDocument();
    const claim = within(table).getByTestId(`edge-row-${FACT_CLAIM}`);
    expect(within(claim).getByText("unverified")).toBeInTheDocument();
  });

  it("selecting an unverified fact shows its interaction evidence (ADM-02)", async () => {
    mockApi(routes({
      [`GET /v1/relationships/${FACT_CLAIM}`]: () => json(fact(FACT_CLAIM, { rel_type: "CONSIDERING", status: "active", evidence_status: "unverified", authority_class: "conversation_report", source_type: "extracted", source_command_id: null, source_interaction_id: INTERACTION, fact_text: "Sarah Johnson is considering entrepreneurship", valid_from: null, valid_from_unknown: true, evidence: [{ kind: "episode", interaction_id: INTERACTION, episode_uuid: "abcdef12-0000-4000-8000-000000000001", source_system: "chat", source_ref: "msg-1", occurred_at: "2026-10-10T09:00:00Z", recorded_at: "2026-10-11T10:00:00Z", added_at: "2026-10-11T10:00:00Z" }] })),
    }));
    renderApp(<App />, { route: `/graph?entity=${SARAH}` });
    const table = await screen.findByRole("table", { name: "Relationships in this neighbourhood" });
    await userEvent.click(within(within(table).getByTestId(`edge-row-${FACT_CLAIM}`)).getByRole("button", { name: "CONSIDERING" }));
    const detail = await screen.findByTestId("fact-detail");
    expect(within(detail).getByText("unverified")).toBeInTheDocument();
    expect(within(detail).getByText("Why the system holds this")).toBeInTheDocument();
    const link = within(detail).getByRole("link", { name: /chat · msg-1/ });
    expect(link).toHaveAttribute("href", `/interactions/${INTERACTION}`);
  });

  it("labels the derived summary as derived and unverified", async () => {
    mockApi(routes());
    renderApp(<App />, { route: `/graph?entity=${SARAH}` });
    const detail = await screen.findByTestId("entity-detail");
    expect(await within(detail).findByText("derived, unverified")).toBeInTheDocument();
    expect(within(detail).getByText("Sarah is an engineering manager.")).toBeInTheDocument();
  });

  it("warns when the node cap cut the neighbourhood and asks for the filters the server caps", async () => {
    const seen: string[] = [];
    mockApi(routes({ [`GET /v1/entities/${SARAH}/graph`]: (async (req: Request) => { seen.push(new URL(req.url).search); return json(graph({ truncated: true })); }) as never }));
    renderApp(<App />, { route: `/graph?entity=${SARAH}` });
    expect(await screen.findByText(/Showing the first 3 entities/)).toBeInTheDocument();
    expect(seen[0]).toContain("hops=1");
    expect(seen[0]).toContain("limit=100");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Hops" }), "2");
    await waitFor(() => expect(seen.some((s) => s.includes("hops=2"))).toBe(true));
    await userEvent.click(screen.getByRole("checkbox", { name: "proposed" }));
    await waitFor(() => expect(seen.some((s) => s.includes("include=proposed"))).toBe(true));
  });

  it("sends the date controls as as_of and known_at", async () => {
    const seen: string[] = [];
    mockApi(routes({ [`GET /v1/entities/${SARAH}/graph`]: (async (req: Request) => { seen.push(new URL(req.url).search); return json(graph()); }) as never }));
    renderApp(<App />, { route: `/graph?entity=${SARAH}` });
    await screen.findByRole("table", { name: "Relationships in this neighbourhood" });
    await userEvent.type(screen.getByLabelText("As of date"), "2023-01-01");
    await waitFor(() => expect(seen.some((s) => s.includes("as_of=2023-01-01"))).toBe(true));
    await userEvent.click(screen.getByRole("checkbox", { name: "Known on" }));
    await userEvent.type(screen.getByLabelText("Known at date"), "2026-10-01");
    await waitFor(() => expect(seen.some((s) => s.includes("known_at=2026-10-01"))).toBe(true));
  });

  it("is not offered to an agent", async () => {
    const calls = mockApi({ "GET /v1/me": () => json(meAs("agent")) });
    renderApp(<App />, { route: "/graph" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(calls).not.toContain("GET /v1/entities");
  });

  it("answers an unknown or foreign entity like a missing one", async () => {
    mockApi(routes({ [`GET /v1/entities/${SARAH}/graph`]: () => errorEnvelope(404, "not_found") }));
    renderApp(<App />, { route: `/graph?entity=${SARAH}` });
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });
});
