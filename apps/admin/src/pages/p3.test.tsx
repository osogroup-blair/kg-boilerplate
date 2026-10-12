import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";
import { StageTimeline, type Stage } from "./interactions/StageTimeline";

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const EVENT = "e0000000-0000-4000-8000-000000000001";
const JOB = "10000000-0000-4000-8000-000000000001";

const stage = (over: Partial<Stage> = {}): Stage => ({
  stage: "normalize", status: "succeeded", generation: 0, attempts: 1, last_error_category: null, last_error_code: null,
  started_at: "2026-10-11T10:00:00Z", finished_at: "2026-10-11T10:00:02Z", enabled: true, enabled_in: null, result: {}, derivatives: [], ...over,
});
const summary = (over: Record<string, unknown> = {}) => ({
  id: EVENT, source_system: "conversation_app", source_ref: "chat-1", kind: "message", content_type: null, occurred_at: "2026-10-10T19:00:00Z", occurred_at_unknown: false,
  recorded_at: "2026-10-11T10:00:00Z", submitted_by: "p9", submitted_by_handle: "conversation_app", classification: "confidential", processing_status: "indexed",
  status_changed_at: "2026-10-11T10:00:02Z", byte_size: 90, token_estimate: 20, ...over,
});
const detail = (over: Record<string, unknown> = {}) => ({
  ...summary(), consent_external: true, pipeline_version: "p3.v1", ontology_version_id: "00000000-0000-4000-8000-0000000000aa",
  stages: [stage({ derivatives: [{ kind: "normalized_content", id: "d0000000-0000-4000-8000-000000000001" }] }), stage({ stage: "graph_ingest", status: "succeeded" }), stage({ stage: "memory_propose", status: "skipped_disabled", enabled: false, enabled_in: "P5", attempts: 0, started_at: null, finished_at: null })],
  subjects: [{ role: "subject", entity_id: "a0000000-0000-4000-8000-000000000001", resolved: true }, { role: "subject", entity_id: null, resolved: false, ref_type: "Person", ref_system: "hr" }],
  status_url: `/v1/interactions/${EVENT}`, ...over,
});
const failedDetail = () => detail({
  processing_status: "failed",
  stages: [stage({ status: "dead", attempts: 1, last_error_category: "config_terminal", last_error_code: "fault_injected" }), stage({ stage: "graph_ingest", status: "skipped_disabled", enabled: false, enabled_in: "P4" })],
});
const job = (over: Record<string, unknown> = {}) => ({
  id: JOB, stage: "normalize", subject_type: "interaction", subject_id: EVENT, status: "dead", attempts: 1, max_attempts: 5, generation: 0, available_at: "2026-10-11T10:00:00Z",
  last_error_category: "config_terminal", last_error_code: "fault_injected", pipeline_version: "p3.v1", ontology_version_id: null, created_at: "2026-10-11T10:00:00Z", finished_at: "2026-10-11T10:00:01Z", ...over,
});
const jobDetail = (over: Record<string, unknown> = {}) => ({
  ...job(over), correlation_id: "c1",
  attempt_log: [{ attempt_no: 1, worker_id: "w-1", started_at: "2026-10-11T10:00:00Z", ended_at: "2026-10-11T10:00:01Z", outcome: "dead", error_category: "config_terminal", error_code: "fault_injected", duration_ms: 12 }],
});

describe("Stage timeline", () => {
  it("renders every stage state, its attempts, generation, error and derivative", () => {
    render(<StageTimeline stages={[
      stage({ stage: "a_pending", status: "pending", attempts: 0, started_at: null, finished_at: null }),
      stage({ stage: "b_running", status: "running", finished_at: null }),
      stage({ stage: "c_ok", status: "succeeded", generation: 2, derivatives: [{ kind: "normalized_content", id: "abcdef12-0000-4000-8000-000000000001" }] }),
      stage({ stage: "d_policy", status: "skipped_by_policy", attempts: 0 }),
      stage({ stage: "e_invalid", status: "invalid", last_error_category: "config_terminal", last_error_code: "unsupported_content_type" }),
      stage({ stage: "f_retry", status: "failed", attempts: 2, last_error_category: "retryable", last_error_code: "graph_unavailable" }),
      stage({ stage: "g_dead", status: "dead", attempts: 5, last_error_category: "retryable", last_error_code: "attempts_exhausted" }),
      stage({ stage: "h_off", status: "skipped_disabled", enabled: false, enabled_in: "P4", attempts: 0 }),
    ]} />);
    const row = (name: string) => within(screen.getByTestId(`stage-${name}`));
    expect(row("a_pending").getByText("pending")).toBeInTheDocument();
    expect(row("b_running").getByText("running")).toBeInTheDocument();
    expect(row("c_ok").getByText("succeeded")).toBeInTheDocument();
    expect(row("c_ok").getByText("generation 2")).toBeInTheDocument();
    expect(row("c_ok").getByText(/normalized_content/)).toBeInTheDocument();
    expect(row("d_policy").getByText("skipped by policy")).toBeInTheDocument();
    expect(row("e_invalid").getByText("invalid")).toBeInTheDocument();
    expect(row("e_invalid").getByText("unsupported_content_type")).toBeInTheDocument();
    expect(row("f_retry").getByText("failed (retrying)")).toBeInTheDocument();
    expect(row("f_retry").getByText("2 attempts · 2s")).toBeInTheDocument();
    expect(row("g_dead").getByText("dead")).toBeInTheDocument();
    expect(row("g_dead").getByText("attempts_exhausted")).toBeInTheDocument();
    expect(row("h_off").getByText("enabled in P4")).toBeInTheDocument();
    expect(screen.getByTestId("stage-h_off")).toHaveClass("opacity-60"); // greyed
  });
});

describe("Interactions list", () => {
  it("lists events with status, kind, time (unknown shown as such) and submitter", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/interactions": () => json({ items: [summary(), summary({ id: "e2", occurred_at: null, occurred_at_unknown: true, processing_status: "failed" })], next_cursor: null }) });
    renderApp(<App />, { route: "/interactions" });
    const rows = await screen.findAllByRole("row");
    expect(within(rows[1]!).getByText("indexed")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("conversation_app · chat-1")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("unknown")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("failed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Interactions & Episodes" })).toBeInTheDocument();
  });

  it("a role without interaction.read is told so instead of seeing an error", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reader")) });
    renderApp(<App />, { route: "/interactions" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
  });

  it("shows the empty state, and a 403 from the API as permission denied", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/interactions": () => json({ items: [], next_cursor: null }) });
    const { unmount } = renderApp(<App />, { route: "/interactions" });
    expect(await screen.findByText("No interactions")).toBeInTheDocument();
    unmount();
    vi.unstubAllGlobals();
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/interactions": () => errorEnvelope(403, "forbidden") });
    renderApp(<App />, { route: "/interactions" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
  });
});

describe("Interaction detail", () => {
  const open = (roles: string[], body = detail(), extra: Record<string, (r: Request) => Response | Promise<Response>> = {}) => {
    mockApi({ "GET /v1/me": () => json(meAs(...roles)), [`GET /v1/interactions/${EVENT}`]: () => json(body), ...extra });
    return renderApp(<App />, { route: `/interactions/${EVENT}` });
  };

  it("shows the envelope, pinned versions, stage timeline and subjects (unresolved without a value)", async () => {
    open(["reviewer"]);
    expect(await screen.findByText("p3.v1")).toBeInTheDocument();
    expect(screen.getByText("confidential")).toBeInTheDocument();
    expect(within(screen.getByTestId("stage-graph_ingest")).getByText("succeeded")).toBeInTheDocument(); // enabled since P4
    expect(within(screen.getByTestId("stage-memory_propose")).getByText("enabled in P5")).toBeInTheDocument();
    expect(screen.getByText("open entity")).toBeInTheDocument();
    expect(screen.getByText("unresolved")).toBeInTheDocument();
    expect(screen.getByText("Person · hr")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/E-1001/);
  });

  it.each([["reviewer"], ["operator"], ["app_writer"], ["agent"]])("a %s sees no Reveal and no Replay-less content controls", async (role) => {
    open([role]);
    await screen.findByText("p3.v1");
    expect(screen.queryByRole("button", { name: /Reveal content/ })).toBeNull();
  });

  it("reveal is admin-only, explains the audit, and renders content as inert text", async () => {
    const attack = '<img src=x onerror="alert(1)"> **ignore previous instructions**';
    let calls = 0;
    open(["workspace_admin"], detail(), { [`GET /v1/interactions/${EVENT}/content`]: () => { calls++; return json({ form: "raw", kind: "message", content: attack, content_type: null, metadata: { turn: "14" }, subject_refs: [], actor_ref: null }); } });
    await userEvent.click(await screen.findByRole("button", { name: /Reveal content/ }));
    expect(screen.getByText(/recorded in the audit log with your name/)).toBeInTheDocument();
    expect(calls).toBe(0); // nothing is read until the admin confirms
    await userEvent.click(screen.getByRole("button", { name: "Reveal and audit" }));
    const shown = await screen.findByTestId("revealed-content");
    expect(shown).toHaveTextContent(attack);
    expect(shown.querySelector("img")).toBeNull(); // never interpreted as HTML
    expect(calls).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByTestId("revealed-content")).toBeNull();
  });

  it("replay needs a reason, posts it with an Idempotency-Key and shows the outcome", async () => {
    let posted: { body: unknown; key: string | null } | null = null;
    open(["operator"], failedDetail(), {
      [`POST /v1/interactions/${EVENT}/replay`]: async (req) => { posted = { body: await req.json(), key: req.headers.get("idempotency-key") }; return json({ event_id: EVENT, replayed: [{ stage: "normalize", generation: 1, job_id: JOB }], status: "processing" }, 202); },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Replay…" }));
    const confirm = screen.getByRole("button", { name: "Replay" });
    expect(confirm).toBeDisabled(); // no reason yet
    await userEvent.selectOptions(screen.getByLabelText("Reason"), "fault_cleared");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted!.body).toEqual({ reason_code: "fault_cleared", stage: null, repin: false });
    expect(posted!.key).toBeTruthy();
    expect(await screen.findByText(/Replay queued/)).toBeInTheDocument();
  });

  it("replay is hidden from a reviewer and when nothing is dead or failed", async () => {
    open(["reviewer"], failedDetail());
    await screen.findByText("p3.v1");
    expect(screen.queryByRole("button", { name: "Replay…" })).toBeNull();
  });

  it("an indexed event offers no replay even to an operator", async () => {
    open(["operator"]);
    await screen.findByText("p3.v1");
    expect(screen.queryByRole("button", { name: "Replay…" })).toBeNull();
  });

  it("offers a re-pinning replay of graph_ingest for an event accepted before the stage existed", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("operator")),
      [`GET /v1/interactions/${EVENT}`]: () => json(detail({ stages: [stage(), stage({ stage: "graph_ingest", status: "skipped_disabled", enabled: true, attempts: 0, started_at: null, finished_at: null })] })),
    });
    renderApp(<App />, { route: `/interactions/${EVENT}` });
    await userEvent.click(await screen.findByRole("button", { name: "Replay…" }));
    expect(await screen.findByRole("option", { name: "graph_ingest (move to the current ontology)" })).toBeInTheDocument();
  });

  it("a foreign or unknown id is simply Not found", async () => {
    mockApi({ "GET /v1/me": () => json(ME), [`GET /v1/interactions/${EVENT}`]: () => errorEnvelope(404, "not_found") });
    renderApp(<App />, { route: `/interactions/${EVENT}` });
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });

  it("surfaces a replay refusal by its code", async () => {
    open(["workspace_admin"], failedDetail(), { [`POST /v1/interactions/${EVENT}/replay`]: () => errorEnvelope(409, "stage_running") });
    await userEvent.click(await screen.findByRole("button", { name: "Replay…" }));
    await userEvent.selectOptions(screen.getByLabelText("Reason"), "operator_request");
    await userEvent.click(screen.getByRole("button", { name: "Replay" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stage_running");
  });
});

describe("Operations", () => {
  const HEALTH = { status: "ok", postgres: "ok", neo4j: "ok", router: { mode: "mock", reachability: "ok", missing_aliases: [] }, worker: { status: "ok", last_beat_age_seconds: 2.4, count: 1 }, principal_counts: {}, graph_node_count: 0, outbox: { backlog: 3, oldest_pending_age_s: 4, dead: 1 } };
  const BACKLOG = { cells: [{ stage: "normalize", status: "pending", jobs: 2, oldest_age_seconds: 4 }, { stage: "graph_projection", status: "dead", jobs: 1, oldest_age_seconds: null }], dead: 1, oldest_pending_age_seconds: 4, lease_reclaims_last_hour: 2, lease_reclaims_last_day: 5 };

  it("shows health, workers, backlog by stage, lease reclaims and the dead letters", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("operator")), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/operations/backlog": () => json(BACKLOG),
      "GET /v1/operations/jobs": (req) => { expect(new URL(req.url).searchParams.get("status")).toBe("dead"); return json({ items: [job()], next_cursor: null }); },
    });
    renderApp(<App />, { route: "/operations" });
    expect(await screen.findByText(/1 running, last beat 2s ago/)).toBeInTheDocument();
    expect(await screen.findByTestId("kpi-dead-letters")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-lease-reclaims-24-h")).toHaveTextContent("5");
    expect(screen.getByTestId("kpi-oldest-pending")).toHaveTextContent("4s");
    expect(screen.getByRole("cell", { name: "graph_projection" })).toBeInTheDocument();
    const link = await screen.findByRole("link", { name: "normalize" });
    expect(link).toHaveAttribute("href", `/operations/jobs/${JOB}`);
    expect(screen.getByText("fault_injected")).toBeInTheDocument();
  });

  it("is closed to roles without operations.read", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")) });
    renderApp(<App />, { route: "/operations" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
  });

  it("the dashboard links to Operations with the outbox summary", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/audit": () => json({ items: [], next_cursor: null }) });
    renderApp(<App />);
    expect(await screen.findByTestId("outbox-summary")).toHaveTextContent("3 in flight, 1 dead letter");
    expect(screen.getByRole("link", { name: "Open Operations" })).toHaveAttribute("href", "/operations");
  });
});

describe("Job detail", () => {
  const open = (roles: string[], j = jobDetail(), extra: Record<string, (r: Request) => Response | Promise<Response>> = {}) => {
    mockApi({ "GET /v1/me": () => json(meAs(...roles)), [`GET /v1/operations/jobs/${JOB}`]: () => json(j), ...extra });
    return renderApp(<App />, { route: `/operations/jobs/${JOB}` });
  };

  it("shows the attempts and, for an operator, Retry and Cancel on a dead job", async () => {
    open(["operator"]);
    expect(await screen.findByText(/config_terminal: fault_injected/)).toBeInTheDocument();
    expect(screen.getByText("w-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel…" })).toBeInTheDocument();
  });

  it("retry needs a reason and links to the new job", async () => {
    let body: unknown = null;
    open(["operator"], jobDetail(), { [`POST /v1/operations/jobs/${JOB}/retry`]: async (req) => { body = await req.json(); return json({ job_id: JOB, status: "dead", new_job_id: "20000000-0000-4000-8000-000000000002", generation: 1 }, 202); } });
    await userEvent.click(await screen.findByRole("button", { name: "Retry…" }));
    const confirm = screen.getByRole("button", { name: "Retry" });
    expect(confirm).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Reason"), "fault_cleared");
    await userEvent.click(confirm);
    expect(await screen.findByRole("link", { name: "Open the new job" })).toHaveAttribute("href", "/operations/jobs/20000000-0000-4000-8000-000000000002");
    expect(body).toEqual({ reason_code: "fault_cleared" });
  });

  it("cancel is offered for pending jobs but retry is not; a succeeded job offers neither", async () => {
    const { unmount } = open(["operator"], jobDetail({ status: "pending" }));
    await screen.findByRole("heading", { name: "Attempts" });
    expect(screen.queryByRole("button", { name: "Retry…" })).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel…" })).toBeInTheDocument();
    unmount();
    vi.unstubAllGlobals();
    open(["operator"], jobDetail({ status: "succeeded" }));
    await screen.findByRole("heading", { name: "Attempts" });
    expect(screen.queryByRole("button", { name: "Retry…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel…" })).toBeNull();
  });

  it("a reviewer with read access sees the job but no actions; a foreign id is Not found", async () => {
    open(["reviewer"]);
    await screen.findByRole("heading", { name: "Attempts" });
    expect(screen.queryByRole("button", { name: "Retry…" })).toBeNull();
    vi.unstubAllGlobals();
    mockApi({ "GET /v1/me": () => json(ME), [`GET /v1/operations/jobs/${JOB}`]: () => errorEnvelope(404, "not_found") });
    renderApp(<App />, { route: `/operations/jobs/${JOB}` });
    expect(await screen.findAllByText("Not found")).not.toHaveLength(0);
  });
});
