import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const MEM = "m0000000-0000-4000-8000-000000000001";
const OLD = "m0000000-0000-4000-8000-000000000002";
const SARAH = "a0000000-0000-4000-8000-000000000001";
const INTERACTION = "e0000000-0000-4000-8000-000000000001";
const FACT = "f0000000-0000-4000-8000-000000000001";
const REVIEW = "r0000000-0000-4000-8000-000000000001";

const summary = (over: Record<string, unknown> = {}) => ({
  id: MEM, type: "episodic", origin: "extracted", status: "active", summary: "Sarah is considering entrepreneurship", authority_class: "conversation_report", evidence_status: "unverified",
  classification: "internal", importance: 0.8, created_by: "c1", recorded_at: "2026-10-12T09:00:00Z", effective_from: "2026-10-10T09:00:00Z", effective_until: null, expires_at: null,
  embedding_status: "ready", revision: 1, scopes: [{ kind: "subject", ref: SARAH }], subject_ids: [SARAH], ...over,
});
const detail = (over: Record<string, unknown> = {}) => ({
  ...summary(), content: "Sarah discussed a possible software business with Michael; she had not decided to leave Acme. <img src=x onerror=alert(1)>", assertion: null, topic_ids: [], confidence: null, occurred_at: "2026-10-10T09:00:00Z",
  last_confirmed_at: null, run_id: null, purpose: null, derived_by_alias: "oso/general", pipeline_version: "p5.v1", prompt_version: "mem.v1", command_id: "c1",
  evidence: [
    { source_kind: "interaction", interaction_id: INTERACTION, added_at: "2026-10-12T09:00:00Z" },
    { source_kind: "episode", interaction_id: INTERACTION, episode_uuid: "ep000000-0000-4000-8000-000000000001", added_at: "2026-10-12T09:00:00Z" },
    { source_kind: "fact", fact_id: FACT, added_at: "2026-10-12T09:00:00Z" },
  ],
  lineage: [{ relation: "replaces", memory_id: OLD, direction: "replaces" }],
  embedding: { status: "ready", model_id: "intfloat/multilingual-e5-small", version: 1, dim: 384, embedded_at: "2026-10-12T09:01:00Z" }, ...over,
});
const withEtag = (body: unknown, etag = 'W/"rev-1"') => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json", etag } });
const empty = () => json({ items: [], next_cursor: null });

describe("Memories list", () => {
  it("lists memories with their status, trust, class and embedding as badges", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      "GET /v1/memories": () => json({ items: [summary(), summary({ id: OLD, summary: "Sarah prefers risks shown", type: "procedural", status: "proposed", evidence_status: "unverified", embedding_status: "pending", classification: "restricted" })], next_cursor: null }),
    });
    renderApp(<App />, { route: "/memories" });
    const row = await screen.findByTestId(`memory-${MEM}`);
    expect(within(row).getByRole("link", { name: "Sarah is considering entrepreneurship" })).toHaveAttribute("href", `/memories/${MEM}`);
    expect(within(row).getByText("episodic")).toBeInTheDocument();
    expect(within(row).getByText("active")).toBeInTheDocument();
    expect(within(row).getByText("unverified")).toBeInTheDocument();
    expect(within(row).getByText("conversation report")).toBeInTheDocument();
    expect(within(row).getByText("vector ready")).toBeInTheDocument();
    const proposed = screen.getByTestId(`memory-${OLD}`);
    expect(within(proposed).getByText("proposed")).toBeInTheDocument();
    expect(within(proposed).getByText("restricted")).toBeInTheDocument();
    expect(within(proposed).getByText("vector pending")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Memories" })).toBeInTheDocument();
  });

  it("sends the chosen filters to the API", async () => {
    const queries: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      "GET /v1/memories": (req) => { queries.push(new URL(req.url).search); return empty(); },
    });
    renderApp(<App />, { route: "/memories" });
    await screen.findByText("No memories to show");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "procedural");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Status" }), "proposed");
    await userEvent.type(screen.getByRole("textbox", { name: "Project" }), "newco");
    await waitFor(() => expect(queries.at(-1)).toMatch(/type=procedural/));
    expect(queries.at(-1)).toMatch(/status=proposed/);
    expect(queries.at(-1)).toMatch(/project=newco/);
    expect(queries.at(-1)).not.toMatch(/reveal_private/);
  });

  it("offers the private view only to administrators and asks first", async () => {
    const queries: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/memories": (req) => { queries.push(new URL(req.url).search); return empty(); },
      "GET /v1/principals": empty,
    });
    renderApp(<App />, { route: "/memories" });
    const toggle = await screen.findByRole("checkbox", { name: /private memory/i });
    await userEvent.click(toggle);
    expect(queries.every((q) => !q.includes("reveal_private"))).toBe(true); // nothing is requested until the admin confirms
    await userEvent.click(await screen.findByRole("button", { name: "Show and audit" }));
    expect(await screen.findByText(/recorded in the audit log/)).toBeInTheDocument();
    await waitFor(() => expect(queries.at(-1)).toMatch(/reveal_private=true/));
  });

  it("does not offer the private view to a reviewer", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/memories": empty });
    renderApp(<App />, { route: "/memories" });
    await screen.findByText("No memories to show");
    expect(screen.queryByRole("checkbox", { name: /private memory/i })).toBeNull();
  });

  it("shows the permission state to a principal with no role and a 403 from the API alike", async () => {
    const calls = mockApi({ "GET /v1/me": () => json(meAs()) });
    renderApp(<App />, { route: "/memories" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(calls).not.toContain("GET /v1/memories");
  });
});

describe("Memory detail", () => {
  const routes = (roles: string[], d = detail(), extra: Record<string, (req: Request) => Response | Promise<Response>> = {}) => ({
    "GET /v1/me": () => json(meAs(...roles)),
    [`GET /v1/memories/${MEM}`]: () => withEtag(d),
    "GET /v1/reviews": () => json({ items: [{ id: REVIEW, memory_id: MEM, item_type: "memory_proposal", status: "open" }], next_cursor: null }),
    ...extra,
  });

  it("shows the content as text, the evidence references, the lineage, the embedding and the honest copy about scores", async () => {
    mockApi(routes(["reviewer"]));
    renderApp(<App />, { route: `/memories/${MEM}` });
    const content = await screen.findByTestId("memory-content");
    expect(content).toHaveTextContent("<img src=x onerror=alert(1)>"); // data, never markup
    expect(content.querySelector("img")).toBeNull();
    expect(screen.getAllByRole("link", { name: /^interaction e0000000/ })[0]).toHaveAttribute("href", `/interactions/${INTERACTION}`); // the interaction and its episode both point there
    expect(screen.getByRole("link", { name: /graph fact f0000000/ })).toHaveAttribute("href", `/graph?fact=${FACT}`);
    expect(screen.getByTestId("evidence-episode")).toHaveTextContent("episode ep000000");
    expect(screen.getByText("Replaces", { exact: false })).toBeInTheDocument();
    expect(within(screen.getByTestId("lineage-replaces")).getByRole("link")).toHaveAttribute("href", `/memories/${OLD}`);
    expect(screen.getByText(/intfloat\/multilingual-e5-small/)).toBeInTheDocument();
    expect(screen.getByText(/384 dims/)).toBeInTheDocument();
    expect(screen.getByText(/retention hints, not truth/)).toBeInTheDocument();
    expect(screen.getByText(/confidence not calibrated/)).toBeInTheDocument();
    expect(screen.getByText(/Only workspace administrators can see which context builds/)).toBeInTheDocument(); // P6: the usage panel (admin only) replaced the placeholder
    expect(screen.getByRole("link", { name: /^a0000000/ })).toHaveAttribute("href", `/entities/${SARAH}`);
    expect(within(screen.getByTestId("scopes")).getByText("subject")).toBeInTheDocument();
  });

  it("offers supersede, expire and dispute to a curator and nothing to a reader", async () => {
    mockApi(routes(["reader"]));
    const { unmount } = renderApp(<App />, { route: `/memories/${MEM}` });
    await screen.findByTestId("memory-content");
    expect(screen.queryByRole("button", { name: "Supersede…" })).toBeNull();
    expect(screen.getByText(/read this memory but not change it/)).toBeInTheDocument();
    unmount();
    mockApi(routes(["reviewer"]));
    renderApp(<App />, { route: `/memories/${MEM}` });
    expect(await screen.findByRole("button", { name: "Supersede…" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Expire…" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Dispute…" })).toBeEnabled();
  });

  it("disables what the status does not allow and explains a proposed memory", async () => {
    mockApi(routes(["reviewer"], detail({ status: "proposed" })));
    renderApp(<App />, { route: `/memories/${MEM}` });
    expect(await screen.findByText(/not served to agents until a reviewer approves it/)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open the review item" })).toHaveAttribute("href", `/reviews/${REVIEW}`);
    expect(screen.queryByRole("button", { name: "Supersede…" })).toBeNull(); // approval is the review item's job
  });

  it("a superseded memory is read-only", async () => {
    mockApi(routes(["workspace_admin"], detail({ status: "superseded", lineage: [{ relation: "replaces", memory_id: OLD, direction: "replaced_by" }] })));
    renderApp(<App />, { route: `/memories/${MEM}` });
    expect(await screen.findByText(/kept for history/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Supersede…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Expire…" })).toBeDisabled();
    expect(screen.getByText("Replaced by", { exact: false })).toBeInTheDocument();
  });

  it("a supersession needs the new wording and a reason, and sends If-Match and an Idempotency-Key", async () => {
    const posts: Array<{ body: unknown; match: string | null; key: string | null }> = [];
    mockApi(routes(["reviewer"], detail(), {
      [`POST /v1/memories/${MEM}/supersede`]: async (req) => { posts.push({ body: await req.json(), match: req.headers.get("if-match"), key: req.headers.get("idempotency-key") }); return json({ memory_id: OLD, status: "active", revision: 1, deduplicated: false }, 201); },
    }));
    renderApp(<App />, { route: `/memories/${MEM}` });
    await userEvent.click(await screen.findByRole("button", { name: "Supersede…" }));
    const confirm = screen.getByRole("button", { name: "Supersede" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "New wording" }), "Sarah is weighing a software business.");
    expect(confirm).toBeDisabled(); // wording alone is not enough
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Reason" }), "clarified");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ body: { content: "Sarah is weighing a software business.", reason_code: "clarified" }, match: 'W/"rev-1"' });
    expect(posts[0]!.key).toMatch(/.{8,}/);
  });

  it("explains a stale revision instead of failing silently", async () => {
    mockApi(routes(["reviewer"], detail(), { [`POST /v1/memories/${MEM}/expire`]: () => errorEnvelope(412, "precondition_failed") }));
    renderApp(<App />, { route: `/memories/${MEM}` });
    await userEvent.click(await screen.findByRole("button", { name: "Expire…" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Reason" }), "stale");
    await userEvent.click(screen.getByRole("button", { name: "Expire" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed since you opened it");
  });

  it("an invisible id is the plain Not found state", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), [`GET /v1/memories/${MEM}`]: () => errorEnvelope(404, "not_found") });
    renderApp(<App />, { route: `/memories/${MEM}` });
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });

  it("asks for the private view explicitly: the flag travels to the API and a banner says it is audited", async () => {
    const queries: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/memories/${MEM}`]: (req) => { queries.push(new URL(req.url).search); return withEtag(detail({ scopes: [{ kind: "session", ref: "p1/r1" }], run_id: "r1", type: "working" })); },
    });
    renderApp(<App />, { route: `/memories/${MEM}?private=1` });
    expect(await screen.findByText(/recorded in the audit log with your name/)).toBeInTheDocument();
    expect(queries[0]).toMatch(/reveal_private=true/);
    expect(screen.queryByRole("button", { name: "Supersede…" })).toBeNull(); // a private note is its owner's, not the console's
  });
});

describe("Review detail for memory", () => {
  const proposal = (over: Record<string, unknown> = {}) => ({
    id: REVIEW, item_type: "memory_proposal", status: "open", rule_kind: "memory_policy", reason_code: "procedural_requires_review", revision: 1, created_at: "2026-10-12T10:00:00Z", due_at: "2026-10-19T10:00:00Z",
    command_id: null, subject_entity_id: SARAH, candidate_entity_id: null, fact_id: null, competing_fact_id: null, interaction_id: INTERACTION, memory_id: MEM, competing_memory_id: null,
    decision: null, rationale_code: null, decided_by: null, decided_at: null, allowed_decisions: ["approve", "reject", "approve_with_edit"], rationale_text: null, entities: [], facts: [], candidates: [], interactions: [], violations: [],
    memories: [{ role: "candidate", id: MEM, type: "procedural", origin: "extracted", status: "proposed", summary: "Present options with risks", content: "When advising Sarah, present options with risks and trade-offs.", authority_class: "conversation_report", evidence_status: "unverified", classification: "internal", scopes: [{ kind: "subject", ref: SARAH }], subject_ids: [SARAH], created_by: "c1", recorded_at: "2026-10-12T10:00:00Z" }], ...over,
  });

  it("shows the proposed memory and lets a reviewer approve with their own wording only when they give it", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(proposal()),
      [`POST /v1/reviews/${REVIEW}/decision`]: async (req) => { posts.push(await req.json()); return json({ review_id: REVIEW, decision: "approve_with_edit", status: "decided", command_id: "c9", revision: 2, projection_status: "not_applicable", entity_ids: [], fact_ids: [], memory_ids: [MEM] }); },
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    const card = await screen.findByTestId("memory-card-memory");
    expect(within(card).getByText(/When advising Sarah/)).toBeInTheDocument();
    expect(within(card).getByText("procedural")).toBeInTheDocument();
    expect(within(card).getByText("proposed")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Open memory" })).toHaveAttribute("href", `/memories/${MEM}`);
    await userEvent.click(screen.getByRole("radio", { name: "Approve with my wording" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Rationale" }), "verified_with_source");
    const submit = screen.getByRole("button", { name: "Record decision" });
    expect(submit).toBeDisabled(); // an edit needs its text
    await userEvent.type(screen.getByRole("textbox", { name: "Your wording" }), "Show options with risks first.");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ decision: "approve_with_edit", edited_content: "Show options with risks first." });
  });

  it("a plain approval sends no edited text", async () => {
    const posts: unknown[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(proposal()),
      [`POST /v1/reviews/${REVIEW}/decision`]: async (req) => { posts.push(await req.json()); return json({ review_id: REVIEW, decision: "approve", status: "decided", command_id: "c9", revision: 2, projection_status: "not_applicable", entity_ids: [], fact_ids: [], memory_ids: [MEM] }); },
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    await userEvent.click(await screen.findByRole("radio", { name: "Approve" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Rationale" }), "verified_with_source");
    await userEvent.click(screen.getByRole("button", { name: "Record decision" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ decision: "approve", edited_content: null });
  });

  it("shows a conflict as the existing memory against the claim and explains a refused supersession", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/reviews/${REVIEW}`]: () => withEtag(proposal({
        item_type: "memory_conflict", reason_code: "higher_authority_conflict", competing_memory_id: OLD, allowed_decisions: ["keep_existing", "supersede_existing", "mark_disputed"],
        memories: [
          { ...proposal().memories[0]!, role: "competing", id: OLD, type: "semantic", status: "active", content: "Sarah prefers morning meetings.", authority_class: "admin_asserted", evidence_status: "verified" },
          { ...proposal().memories[0]!, role: "candidate", type: "semantic", content: "Sarah prefers evening meetings." },
        ],
      })),
      [`POST /v1/reviews/${REVIEW}/decision`]: () => errorEnvelope(409, "insufficient_authority"),
    });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    expect(within(await screen.findByTestId("memory-card-existing")).getByText("Sarah prefers morning meetings.")).toBeInTheDocument();
    expect(within(screen.getByTestId("memory-card-claim")).getByText("Sarah prefers evening meetings.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Replace the existing memory with this one" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Rationale" }), "verified_with_source");
    await userEvent.click(screen.getByRole("button", { name: "Record decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("higher authority than you hold");
  });

  it("says so when the memory is not visible to the reviewer", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), [`GET /v1/reviews/${REVIEW}`]: () => withEtag(proposal({ memories: [] })) });
    renderApp(<App />, { route: `/reviews/${REVIEW}` });
    expect(await screen.findByText(/not visible to you/)).toBeInTheDocument();
  });

  it("lists memory proposals in the queue with their own type", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      "GET /v1/reviews": () => json({ items: [proposal()], next_cursor: null }),
    });
    renderApp(<App />, { route: "/reviews" });
    expect(await screen.findByText("procedural_requires_review")).toBeInTheDocument();
    expect(screen.getAllByText("memory proposal").length).toBeGreaterThan(0);
    expect(within(screen.getByRole("combobox", { name: "Filter by type" })).getByRole("option", { name: "memory conflict" })).toBeInTheDocument();
  });
});

describe("Agents & Access", () => {
  it("shows an administrator an agent's grants, with their privileges, on the Grants tab", async () => {
    const agent = { id: "p-agent", handle: "agent_research", kind: "agent", roles: ["agent"], status: "active", keys: [], created_at: "2026-10-12T09:00:00Z", revoked_at: null, display_name: "Research", workspace_id: "w1" };
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/principals": () => json({ items: [agent], next_cursor: null }),
      "GET /v1/principals/p-agent": () => json(agent),
      "GET /v1/principals/p-agent/grants": () => json([
        { id: "g1", principal_id: "p-agent", scope_kind: "project", scope_ref: "newco-plan", privileges: ["read", "propose", "publish"], created_at: "2026-10-12T09:00:00Z" },
        { id: "g2", principal_id: "p-agent", scope_kind: "subject", scope_ref: "*", privileges: ["read", "propose"], created_at: "2026-10-12T09:00:00Z" },
      ]),
    });
    renderApp(<App />, { route: "/agents/p-agent" });
    await userEvent.click(await screen.findByRole("tab", { name: "grants" }));
    const table = await screen.findByRole("table", { name: "Grants table" });
    expect(within(table).getByText("project:newco-plan")).toBeInTheDocument();
    expect(within(table).getByText("publish")).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Remove grant project:newco-plan" })).toBeInTheDocument();
  });

  it("does not offer a non-admin the registration controls, and never asks for the grants", async () => {
    const calls = mockApi({
      "GET /v1/me": () => json(meAs("operator")),
      "GET /v1/principals": () => json({ items: [], next_cursor: null }),
    });
    renderApp(<App />, { route: "/agents" });
    await screen.findByText("No principals");
    expect(screen.queryByRole("button", { name: "Register agent or app" })).toBeNull();
    expect(calls).not.toContain("GET /v1/scopes");
  });
});

describe("Interaction detail", () => {
  it("summarises what the memory stage did: made, dropped and cost, ids as links", async () => {
    const stage = (name: string, status: string, result: Record<string, unknown> = {}) => ({ stage: name, status, generation: 0, attempts: 1, enabled: true, enabled_in: null, last_error_code: null, last_error_category: null, started_at: "2026-10-12T09:00:00Z", finished_at: "2026-10-12T09:00:01Z", result, derivatives: [] });
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      [`GET /v1/interactions/${INTERACTION}`]: () => json({
        id: INTERACTION, kind: "message", source_system: "chat", source_ref: "msg-1", classification: "internal", consent_external: true, occurred_at: "2026-10-10T09:00:00Z", occurred_at_unknown: false, recorded_at: "2026-10-12T09:00:00Z",
        submitted_by: "c1", processing_status: "indexed", pipeline_version: "p5.v1", ontology_version_id: null, byte_size: 100, token_estimate: 20, subjects: [],
        stages: [stage("normalize", "succeeded"), stage("graph_ingest", "succeeded"), stage("memory_propose", "succeeded", { prompt_version: "mem.v1", candidates: 3, kept: 1, statuses: { active: 1 }, drops: { escalated_claim: 1, below_importance: 1 }, memory_ids: [MEM], review_ids: [REVIEW], chat_calls: 1, prompt_tokens: 500, completion_tokens: 100 })],
      }),
    });
    renderApp(<App />, { route: `/interactions/${INTERACTION}` });
    const summary = await screen.findByTestId("memory-propose-summary");
    expect(within(summary).getByText(/3 proposed by the model \(mem\.v1\) · 1 kept/)).toBeInTheDocument();
    expect(within(summary).getByText(/1 active/)).toBeInTheDocument();
    expect(within(summary).getByText(/1 escalated claim · 1 below importance/)).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: /^m0000000/ })).toHaveAttribute("href", `/memories/${MEM}`);
    expect(within(summary).getByRole("link", { name: /^r0000000/ })).toHaveAttribute("href", `/reviews/${REVIEW}`);
  });
});

describe("Entity detail", () => {
  it("links to the memories about the entity with the count the caller may see", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      [`GET /v1/entities/${SARAH}`]: () => json({
        id: SARAH, type: "Person", name: "Sarah Johnson", aliases: [], attributes: {}, classification: "internal", status: "active", origin: "command", merged_into_id: null, revision: 1, external_ids: [],
        ontology_version_id: "v1", recorded_at: "2026-10-11T09:00:00Z", updated_at: "2026-10-11T09:00:00Z", projection_status: "projected", embedding_status: "embedded", projected_at: null, relationships: [], memories: 2,
      }),
    });
    renderApp(<App />, { route: `/entities/${SARAH}` });
    const link = await screen.findByTestId("entity-memories-link");
    expect(link).toHaveTextContent("Memories (2)");
    expect(link).toHaveAttribute("href", `/memories?subject_id=${SARAH}`);
  });
});
