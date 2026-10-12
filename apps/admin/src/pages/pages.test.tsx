import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { getToken } from "../api/client";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";

const PRINCIPALS = {
  items: [
    { id: "a1", handle: "agent_planning", display_name: "Planning Agent", kind: "agent", status: "active", roles: ["agent"], created_at: "2026-10-11T10:00:00Z", revoked_at: null,
      keys: [{ prefix: "8t3JleBW", created_at: "2026-10-11T10:00:00Z", expires_at: null, revoked_at: null, last_used_at: null }] },
    { id: "a2", handle: "admin@demo", display_name: "Demo Admin", kind: "user", status: "active", roles: ["workspace_admin"], created_at: "2026-10-11T10:00:00Z", revoked_at: null, keys: [] },
  ],
  next_cursor: null,
};
const HEALTH = { status: "ok", postgres: "ok", neo4j: "ok", router: { mode: "mock", reachability: "ok", missing_aliases: [] }, worker: { status: "ok", last_beat_age_seconds: 3, count: 1 }, principal_counts: { agent: 2, user: 3 }, graph_node_count: 0 };
const AUDIT = { items: [{ id: "e1", action: "auth.token_issued", outcome: "allowed", created_at: "2026-10-11T10:00:00Z", resource_type: "api_key", metadata: {} }], next_cursor: null };

describe("Login", () => {
  it("exchanges the key for a token, discards the key and shows the workspace", async () => {
    mockApi({ "POST /v1/auth/token": () => json({ access_token: "jwt-123", token_type: "Bearer", expires_in: 900 }), "GET /v1/me": () => json(ME), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/audit": () => json(AUDIT) });
    renderApp(<App />, { signedIn: false });
    await userEvent.type(screen.getByLabelText("API key"), "kgk_abcdefgh_secret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("workspace-name")).toHaveTextContent("Demo Workspace");
    expect(getToken()).toBe("jwt-123");
    expect(sessionStorage.getItem("kg.admin.token")).toBe("jwt-123");
    expect(document.body.innerHTML).not.toContain("kgk_abcdefgh_secret");
    expect(window.localStorage.length).toBe(0); // the rule above bans app code from localStorage; the test inspects it through window
  });

  it("shows an error for a rejected key and stays on the login page", async () => {
    mockApi({ "POST /v1/auth/token": () => errorEnvelope(401, "unauthenticated") });
    renderApp(<App />, { signedIn: false });
    await userEvent.type(screen.getByLabelText("API key"), "kgk_bad");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not accepted");
    expect(getToken()).toBeNull();
  });

  it("goes back to login when the API answers 401 mid-session", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/operations/health": () => errorEnvelope(401, "unauthenticated"), "GET /v1/audit": () => errorEnvelope(401, "unauthenticated") });
    renderApp(<App />);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("session ended");
    expect(getToken()).toBeNull();
  });
});

describe("Dashboard", () => {
  it("shows dependency status, principal counts and audit events", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/audit": () => json(AUDIT) });
    renderApp(<App />);
    expect(await screen.findByText("postgres ok")).toBeInTheDocument();
    expect(screen.getByText("neo4j ok")).toBeInTheDocument();
    expect(screen.getByText("router (mock) ok")).toBeInTheDocument();
    expect(screen.getByText("auth.token_issued")).toBeInTheDocument();
    expect(screen.getByText(/agent:/)).toBeInTheDocument();
  });

  it("shows permission-denied panels for what the role cannot read", async () => {
    mockApi({ "GET /v1/me": () => json({ ...ME, principal: { ...ME.principal, roles: ["operator"] } }), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/audit": () => errorEnvelope(403, "forbidden") });
    renderApp(<App />);
    expect(await screen.findByText("postgres ok")).toBeInTheDocument();
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
  });
});

describe("Agents & Access", () => {
  it("lists principals with roles, status and key prefixes (never secrets)", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/principals": () => json(PRINCIPALS) });
    renderApp(<App />, { route: "/agents" });
    expect(await screen.findByRole("button", { name: "agent_planning" })).toBeInTheDocument();
    expect(screen.getByText("8t3JleBW")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(document.body.textContent).not.toContain("kgk_");
  });

  it("shows permission denied for roles without principal.read", async () => {
    mockApi({ "GET /v1/me": () => json({ ...ME, principal: { ...ME.principal, roles: ["agent"] } }), "GET /v1/principals": () => errorEnvelope(403, "forbidden") });
    renderApp(<App />, { route: "/agents" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/principals": () => json({ items: [], next_cursor: null }) });
    renderApp(<App />, { route: "/agents" });
    expect(await screen.findByText("No principals")).toBeInTheDocument();
  });

  it("opens a detail drawer from a deep link, and an unknown or foreign id is just Not found", async () => {
    const detail = PRINCIPALS.items[0]!;
    mockApi({
      "GET /v1/me": () => json(ME),
      "GET /v1/principals": () => json(PRINCIPALS),
      "GET /v1/principals/a1": () => json(detail),
      "GET /v1/principals/foreign-or-random": () => errorEnvelope(404, "not_found"),
    });
    const first = renderApp(<App />, { route: "/agents/a1" });
    expect(await screen.findByText("Planning Agent")).toBeInTheDocument();
    expect(screen.getByText(/8t3JleBW… \(active\)/)).toBeInTheDocument();
    first.unmount();
    renderApp(<App />, { route: "/agents/foreign-or-random" });
    await waitFor(() => expect(screen.getByText(/This principal does not exist/)).toBeInTheDocument());
  });

  it("navigation lists only the pages that exist", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/operations/health": () => json(HEALTH), "GET /v1/audit": () => json(AUDIT) });
    renderApp(<App />);
    await screen.findByTestId("workspace-name");
    const links = screen.getAllByRole("link").map((l) => l.textContent);
    expect(links).toEqual(["Dashboard", "Ontology", "Entities", "Graph Explorer", "Review Queue", "Commands", "Interactions & Episodes", "Operations", "Agents & Access"]);
    vi.unstubAllGlobals();
  });
});

