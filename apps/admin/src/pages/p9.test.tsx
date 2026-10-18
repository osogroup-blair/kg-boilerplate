import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ME, json, mockApi, opsV2Routes, renderApp } from "../test-utils";

vi.mock("../graph/GraphView", () => ({ GraphView: () => <div /> }));

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const HEALTH = { status: "ok", postgres: "ok", neo4j: "ok", router: { mode: "mock", reachability: "ok", missing_aliases: [] }, worker: { status: "ok", last_beat_age_seconds: 1, count: 1 }, principal_counts: {}, graph_node_count: 0, outbox: { backlog: 0, oldest_pending_age_s: 0, dead: 0 } };
const BACKLOG = { cells: [], dead: 0, oldest_pending_age_seconds: 0, lease_reclaims_last_hour: 0, lease_reclaims_last_day: 0 };
const base = (roles = ["operator"]) => ({ "GET /v1/me": () => json(meAs(...roles)), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/operations/backlog": () => json(BACKLOG), "GET /v1/operations/jobs": () => json({ items: [], next_cursor: null }) });

describe("Operations v2", () => {
  it("shows spend by purpose and alias, the top consumers and the error count", async () => {
    mockApi({
      ...opsV2Routes(), ...base(),
      "GET /v1/operations/cost": (req) => {
        expect(new URL(req.url).searchParams.get("days")).toBe("14");
        return json({
          days: 14, total_usd: 0.0212, total_calls: 3, by_purpose: { extraction: 0.02, embedding: 0.0012 }, by_alias: { "oso/fast": 0.02, "oso/embed": 0.0012 },
          rows: [{ day: "2026-10-17", purpose: "extraction", alias: "oso/fast", calls: 2, errors: 1, prompt_tokens: 200, completion_tokens: 40, usd: 0.02 }],
          top_consumers: [{ principal_id: "p1", handle: "agent@demo", kind: "agent", context_builds: 12, tokens_used: 3400 }], note: "estimates from the platform's call log",
        });
      },
    });
    renderApp(<App />, { route: "/operations" });
    expect(await screen.findByTestId("kpi-cost-total")).toHaveTextContent("$0.0212");
    expect(screen.getByTestId("kpi-cost-calls")).toHaveTextContent("3");
    expect(screen.getByTestId("kpi-cost-errors")).toHaveTextContent("1");
    expect(screen.getByRole("cell", { name: "oso/fast" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "agent@demo" })).toBeInTheDocument();
  });

  it("shows context latency by profile and source and the router by alias in seconds", async () => {
    mockApi({
      ...opsV2Routes(), ...base(),
      "GET /v1/operations/latency": () => json({ days: 1, context_by_profile: { planning: { n: 40, p50: 0.31, p95: 1.8 } }, context_by_source: { memory: { n: 40, p50: 0.05, p95: 0.2 } }, router_by_alias: { "oso/fast": { n: 9, p50: 0.9, p95: 2.4 } }, unit: "seconds" }),
    });
    renderApp(<App />, { route: "/operations" });
    const panel = await screen.findByTestId("latency");
    expect(within(panel).getByRole("cell", { name: "planning" })).toBeInTheDocument();
    expect(within(panel).getByText("310 ms")).toBeInTheDocument();
    expect(within(panel).getByText("2.40 s")).toBeInTheDocument();
  });

  it("shows freshness: lag quantiles, what has not settled, and per-stage lag", async () => {
    mockApi({
      ...opsV2Routes(), ...base(),
      "GET /v1/operations/freshness": () => json({ hours: 24, accepted_to_indexed: { n: 5, p50: 12.5, p95: 31 }, unsettled: { accepted: 2 }, oldest_unsettled_age_seconds: 95, by_stage: [{ stage: "graph_ingest", n: 5, p50: 9, p95: 20 }], watermarks: { newest_event: "2026-10-17T10:00:00Z", newest_indexed: null, last_job: null }, unit: "seconds" }),
    });
    renderApp(<App />, { route: "/operations" });
    expect(await screen.findByTestId("kpi-fresh-p50")).toHaveTextContent("12.50 s");
    expect(screen.getByTestId("kpi-fresh-p95")).toHaveTextContent("31.00 s");
    expect(screen.getByTestId("kpi-fresh-unsettled")).toHaveTextContent("2");
    expect(screen.getByRole("cell", { name: "graph_ingest" })).toBeInTheDocument();
  });

  it("shows the age of the newest backup per store, flags a stale one, and the last rehearsal's RTO and RPO", async () => {
    mockApi({
      ...opsV2Routes(), ...base(),
      "GET /v1/operations/backups": () => json({
        stores: [{ store: "postgres", age_seconds: 7200, stale: false }, { store: "neo4j", age_seconds: 100000, stale: true }, { store: "keys", age_seconds: null, stale: true }],
        last_rehearsal: { at: "2026-10-16T04:00:00Z", status: "completed", rto_seconds: 101.5, rpo_seconds: 3600, age_days: 1 }, recent: [], stale_after_hours: 26,
      }),
    });
    renderApp(<App />, { route: "/operations" });
    expect(await screen.findByTestId("kpi-backup-postgres")).toHaveTextContent("2.0 h ago");
    expect(screen.getByTestId("kpi-backup-neo4j")).toHaveClass("text-red-700");
    expect(screen.getByTestId("kpi-backup-keys")).toHaveTextContent("never");
    expect(screen.getByTestId("rehearsal")).toHaveTextContent(/RTO 2 min, RPO 1\.0 h/);
  });

  it("lists what is firing and says when a live session has not run", async () => {
    mockApi({
      ...opsV2Routes(), ...base(),
      "GET /v1/operations/alerts": () => json({ firing: [{ name: "BackupStale", severity: "critical", source: "platform", summary: "no completed neo4j backup in the last 26 hours", value: 100000 }], checked: ["backup_age"], alertmanager: "not_configured" }),
    });
    renderApp(<App />, { route: "/operations" });
    expect(await screen.findByTestId("alerts-firing")).toHaveTextContent("BackupStale");
    expect(await screen.findByTestId("live-session")).toHaveTextContent("not run yet");
  });

  it("runs a scan, and a repair only after confirmation, with an Idempotency-Key and the audited outcome", async () => {
    const posts: { key: string | null; body: Record<string, unknown> }[] = [];
    mockApi({
      ...opsV2Routes(), ...base(),
      "GET /v1/operations/reconcile": () => json({
        classes: [{ name: "graph_missing_node", scope: "graph", description: "a projected row has no node", repair: "reproject", spends_money: false }, { name: "episode_missing", scope: "graph", description: "re-ingest", repair: "reingest", spends_money: true }],
        runs: [{ id: "r1", scope: "all", mode: "check", trigger: "scheduled", status: "completed", drift: { graph_missing_node: 2 }, repaired: {}, error_code: null, started_at: "2026-10-17T02:00:00Z", finished_at: null }],
      }),
      "POST /v1/operations/reconcile": async (req) => { posts.push({ key: req.headers.get("idempotency-key"), body: await req.json() }); return json({ status: "completed", drift: { graph_missing_node: 0 }, repaired: { graph_missing_node: 2 } }); },
    });
    renderApp(<App />, { route: "/operations" });
    const user = userEvent.setup();
    expect(await screen.findByText("graph_missing_node: 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Scan now" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]!.body).toMatchObject({ scope: "all", repair: false });
    await user.click(await screen.findByRole("button", { name: /Repair safe classes/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("graph_missing_node");
    expect(dialog).not.toHaveTextContent("episode_missing"); // a class that spends model calls is not offered
    expect(posts).toHaveLength(1); // nothing has been repaired yet
    await user.click(within(dialog).getByRole("button", { name: "Repair" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]!.body).toMatchObject({ repair: true, allow_reingest: false });
    expect(posts[1]!.key).toBeTruthy();
    expect(await screen.findByText(/Repair completed: 2 repaired, 0 left for a person/)).toBeInTheDocument();
  });
});

describe("Single sign-on", () => {
  const CFG = { enabled: true, issuer: "https://idp.test", client_id: "kg-console", authorization_endpoint: "https://idp.test/authorize", scopes: ["openid", "profile", "email", "groups"], redirect_uris: ["http://localhost:3000/login/callback"] };

  it("offers single sign-on and redirects with a PKCE S256 challenge that matches the stored verifier", async () => {
    const { nav, challengeFor } = await import("../auth/oidc");
    const seen: string[] = [];
    const original = nav.to;
    nav.to = (url) => void seen.push(url);
    try {
      mockApi({ "GET /v1/auth/oidc/config": () => json(CFG) });
      renderApp(<App />, { signedIn: false });
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Sign in with single sign-on" }));
      await waitFor(() => expect(seen).toHaveLength(1));
      const url = new URL(seen[0]!);
      expect(url.origin + url.pathname).toBe("https://idp.test/authorize");
      expect(url.searchParams.get("response_type")).toBe("code");
      expect(url.searchParams.get("client_id")).toBe("kg-console");
      expect(url.searchParams.get("code_challenge_method")).toBe("S256");
      expect(url.searchParams.get("redirect_uri")).toMatch(/\/login\/callback$/);
      const pending = JSON.parse(sessionStorage.getItem("kg.oidc.pending")!);
      expect(url.searchParams.get("state")).toBe(pending.state);
      expect(url.searchParams.get("code_challenge")).toBe(await challengeFor(pending.verifier));
      expect(pending.verifier.length).toBeGreaterThanOrEqual(43);
      // the API-key form stays behind the break-glass disclosure
      expect(screen.queryByLabelText("API key")).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /Break-glass/ }));
      expect(screen.getByLabelText("API key")).toBeInTheDocument();
    } finally {
      nav.to = original;
    }
  });

  it("completes the redirect: checks the state, sends the code with its verifier, and opens the session", async () => {
    sessionStorage.setItem("kg.oidc.pending", JSON.stringify({ verifier: "v".repeat(60), state: "st-1" }));
    let body: Record<string, string> = {};
    mockApi({
      "POST /v1/auth/oidc/callback": async (req) => { body = await req.json(); return json({ id_token: "idp-token", token_type: "Bearer", expires_in: 900, principal_id: "p1", handle: "gina@example.com", roles: ["workspace_admin"] }); },
      "GET /v1/me": () => json(ME), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/audit": () => json({ items: [], next_cursor: null }),
    });
    renderApp(<App />, { signedIn: false, route: "/login/callback?code=abc&state=st-1" });
    expect(await screen.findByTestId("principal-handle")).toBeInTheDocument();
    expect(body).toMatchObject({ code: "abc", code_verifier: "v".repeat(60) });
    expect(body.redirect_uri).toMatch(/\/login\/callback$/);
    expect(sessionStorage.getItem("kg.oidc.pending")).toBeNull(); // single use
  });

  it("refuses a callback whose state does not match, without calling the API", async () => {
    sessionStorage.setItem("kg.oidc.pending", JSON.stringify({ verifier: "v".repeat(60), state: "st-1" }));
    const calls = mockApi({});
    renderApp(<App />, { signedIn: false, route: "/login/callback?code=abc&state=forged" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in could not be completed");
    expect(calls).toHaveLength(0);
  });
});

describe("Graph rebuild", () => {
  const RUN = { id: "r1", namespace: "ws-x-r1", previous_namespace: "ws-x", mode: "project", status: "ready", estimate: {}, progress: { entities: 12, facts: 30 }, report: { counts_match: true }, error_code: null, created_at: "2026-10-17T10:00:00Z" };
  const NS = [{ namespace: "ws-x-r1", status: "building", reindex_run_id: "r1", activated_at: null, retired_at: null }, { namespace: "ws-x", status: "active", reindex_run_id: null, activated_at: "2026-10-01T00:00:00Z", retired_at: null }];
  const ACTIVE = { entity_types: [], relationship_types: [], version: { id: "v1", version: "1.5.0" }, principal_id: "p1" };

  it("previews, starts and cuts over only after confirmation, each with an Idempotency-Key", async () => {
    const posts: { path: string; key: string | null; body: unknown }[] = [];
    mockApi({
      "GET /v1/me": () => json(ME), "GET /v1/ontologies/active": () => json(ACTIVE), "GET /v1/reindex-runs": () => json([RUN]), "GET /v1/graph/namespaces": () => json(NS),
      "POST /v1/ontologies/versions/v1/reindex": async (req) => {
        const body = await req.json();
        posts.push({ path: "reindex", key: req.headers.get("idempotency-key"), body });
        return body.confirm ? json({ id: "r2", status: "building" }, 202) : json({ status: "preview", entities: 12, facts: 30, estimated_cost_usd: 0 });
      },
      "POST /v1/graph/namespaces/ws-x-r1/activate": async (req) => { posts.push({ path: "activate", key: req.headers.get("idempotency-key"), body: await req.json() }); return json({ action: "cutover", run: RUN }); },
    });
    renderApp(<App />, { route: "/graph/rebuild" });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByTestId("reindex-preview")).toHaveTextContent("12");
    await user.click(screen.getByRole("button", { name: /Start rebuild/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(posts.filter((p) => (p.body as { confirm?: boolean }).confirm)).toHaveLength(1));
    expect(posts.at(-1)!.key).toBeTruthy();
    // the ready build offers a cut-over; nothing happens until it is confirmed
    await user.click(await screen.findByRole("button", { name: /Cut over…/ }));
    expect(posts.some((p) => p.path === "activate")).toBe(false);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cut over" }));
    await waitFor(() => expect(posts.some((p) => p.path === "activate")).toBe(true));
    expect(await screen.findByText(/Cut over: the rebuilt graph is live/)).toBeInTheDocument();
  });

  it("is closed to anyone but a workspace admin", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("operator")) });
    renderApp(<App />, { route: "/graph/rebuild" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
  });
});
