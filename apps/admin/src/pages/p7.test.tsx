import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";
import { grantProblem, togglePrivilege } from "./agents/lib";

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const AGENT = "a0000000-0000-4000-8000-0000000000a1";
const SARAH = "a0000000-0000-4000-8000-000000000001";
const KEY = "kgk_abcd1234_THE-ONE-TIME-SECRET-0123456789";

const agent = (over: Record<string, unknown> = {}) => ({
  id: AGENT, handle: "research-agent", display_name: "Research Agent", kind: "agent", status: "active", roles: ["agent"], keys: [], created_at: "2026-10-12T09:00:00Z", revoked_at: null,
  description: "", owner_principal_id: null, trusted_source: false, model_disclosure: "allowed", allowed_profiles: ["research"], quota_requests_per_minute: null, quota_context_tokens_per_day: null,
  quota_interactions_per_day: null, revoke_reason: null, revoked_by: null, updated_at: "2026-10-12T09:00:00Z", revision: 1, ...over,
});
const list = (...items: unknown[]) => ({ items, next_cursor: null });

describe("grant rules (the same ones the API enforces)", () => {
  it("keeps the privilege set valid: propose and publish need read", () => {
    expect(togglePrivilege(["read", "propose"], "publish")).toEqual(["read", "propose", "publish"]);
    expect(togglePrivilege(["read", "propose", "publish"], "read")).toEqual([]);
    expect(togglePrivilege([], "publish")).toEqual(["read", "publish"]);
    expect(togglePrivilege(["read"], "propose")).toEqual(["read", "propose"]);
  });

  it("accepts a well-formed grant and names the problem with a bad one", () => {
    expect(grantProblem({ scope_kind: "subject", scope_ref: SARAH, privileges: ["read"] })).toBeNull();
    expect(grantProblem({ scope_kind: "subject", scope_ref: "*", privileges: ["read", "propose"] })).toBeNull();
    expect(grantProblem({ scope_kind: "project", scope_ref: "newco-plan", privileges: ["read", "propose", "publish"] })).toBeNull();
    expect(grantProblem({ scope_kind: "subject", scope_ref: "Sarah", privileges: ["read"] })).toMatch(/entity id/);
    expect(grantProblem({ scope_kind: "project", scope_ref: "bad key!", privileges: ["read"] })).toMatch(/project key/);
    expect(grantProblem({ scope_kind: "project", scope_ref: "newco-plan", privileges: ["propose"] })).toMatch(/need read/);
    expect(grantProblem({ scope_kind: "classification", scope_ref: "secret", privileges: ["read"] })).toMatch(/classification/);
    expect(grantProblem({ scope_kind: "classification", scope_ref: "restricted", privileges: ["read", "publish"] })).toMatch(/read only/);
  });
});

describe("Agents & Access: registration", () => {
  const routes = (extra: Record<string, () => Response | Promise<Response>> = {}) => ({
    "GET /v1/me": () => json(meAs("workspace_admin")),
    "GET /v1/principals": () => json(list(agent())),
    "GET /v1/context/profiles": () => json([{ name: "research" }, { name: "planning" }]),
    ...extra,
  });

  it("shows the new key ONCE, with a warning, and never again", async () => {
    const posted: unknown[] = [];
    mockApi(
      routes({
        "POST /v1/principals": async () => json({ principal: agent({ handle: "agent_research_v2" }), key: { id: "k1", name: "initial", prefix: "abcd1234", created_at: "2026-10-12T10:00:00Z", plaintext: KEY }, grants: [] }, 201),
      }),
    );
    const fetchSpy = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input.clone() : new Request(String(input), init);
      if (req.method === "POST") posted.push(await req.json());
      return fetchSpy(input, init);
    });
    renderApp(<App />, { route: "/agents" });
    await userEvent.click(await screen.findByRole("button", { name: "Register agent or app" }));
    await userEvent.type(await screen.findByRole("textbox", { name: "Handle" }), "agent_research_v2");
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    expect(await screen.findByLabelText("API key")).toHaveTextContent(KEY);
    expect(screen.getByText(/will not be shown again/i)).toBeInTheDocument();
    expect(posted[0]).toMatchObject({ kind: "agent", handle: "agent_research_v2", model_disclosure: "allowed", trusted_source: false });
    await userEvent.click(screen.getByRole("button", { name: "I have stored the key" }));
    await waitFor(() => expect(screen.queryByLabelText("API key")).toBeNull());
    expect(document.body.textContent).not.toContain(KEY);
  });

  it("refuses a handle the API would refuse, and says when it is taken", async () => {
    mockApi(routes({ "POST /v1/principals": () => errorEnvelope(409, "handle_taken") }));
    renderApp(<App />, { route: "/agents" });
    await userEvent.click(await screen.findByRole("button", { name: "Register agent or app" }));
    const submit = screen.getByRole("button", { name: "Register" });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Handle" }), "research-agent");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent("already registered");
  });

  it("offers the trusted-source switch to apps only", async () => {
    mockApi(routes());
    renderApp(<App />, { route: "/agents" });
    await userEvent.click(await screen.findByRole("button", { name: "Register agent or app" }));
    expect(screen.queryByRole("checkbox", { name: /Trusted source/ })).toBeNull();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Kind" }), "app");
    expect(screen.getByRole("checkbox", { name: /Trusted source/ })).toBeInTheDocument();
  });

  it("builds the initial grants with privileges and sends them", async () => {
    let body: Record<string, unknown> = {};
    mockApi(
      routes({
        "POST /v1/principals": async () => json({ principal: agent(), key: { id: "k1", name: "initial", prefix: "abcd1234", created_at: "2026-10-12T10:00:00Z", plaintext: KEY }, grants: [] }, 201),
      }),
    );
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input.clone() : new Request(String(input), init);
      if (req.method === "POST" && req.url.endsWith("/v1/principals")) body = await req.json();
      return real(input, init);
    });
    renderApp(<App />, { route: "/agents" });
    await userEvent.click(await screen.findByRole("button", { name: "Register agent or app" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Handle" }), "planner");
    await userEvent.type(screen.getByRole("textbox", { name: "Grant reference" }), SARAH);
    const group = screen.getByRole("group", { name: "Privileges" });
    await userEvent.click(within(group).getByRole("checkbox", { name: /publish/ }));
    await userEvent.click(screen.getByRole("button", { name: "Add grant" }));
    expect(within(screen.getByRole("list", { name: "Initial grants" })).getByText(`subject:${SARAH}`)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    await screen.findByLabelText("API key");
    expect(body.grants).toEqual([{ scope_kind: "subject", scope_ref: SARAH, privileges: ["read", "propose", "publish"] }]);
  });
});

describe("Agents & Access: revocation", () => {
  const drawerRoutes = (over: Record<string, () => Response | Promise<Response>> = {}) => ({
    "GET /v1/me": () => json(meAs("workspace_admin")),
    "GET /v1/principals": () => json(list(agent())),
    [`GET /v1/principals/${AGENT}`]: () => json(agent()),
    ...over,
  });

  it("gates the confirm button on the typed handle and shows what each reason does", async () => {
    mockApi(drawerRoutes());
    renderApp(<App />, { route: `/agents/${AGENT}` });
    await userEvent.click(await screen.findByRole("button", { name: "Revoke…" }));
    const confirm = screen.getByRole("button", { name: "Revoke" });
    expect(confirm).toBeDisabled();
    const effects = screen.getByRole("list", { name: "Effects of revocation" });
    expect(effects).not.toHaveTextContent(/rejected/); // retired: nothing it produced is distrusted
    await userEvent.click(screen.getByRole("radio", { name: /Compromised/ }));
    expect(effects).toHaveTextContent(/unreviewed proposals are rejected/);
    expect(effects).toHaveTextContent(/disputed/);
    await userEvent.type(screen.getByRole("textbox", { name: "Confirm handle" }), "research");
    expect(confirm).toBeDisabled(); // a prefix is not the handle
    await userEvent.type(screen.getByRole("textbox", { name: "Confirm handle" }), "-agent");
    expect(confirm).toBeEnabled();
  });

  it("revokes with the chosen reason and an idempotency key", async () => {
    let seen: { body: Record<string, unknown>; key: string | null } | null = null;
    mockApi(
      drawerRoutes({
        [`POST /v1/principals/${AGENT}/revoke`]: async () => json({ principal: agent({ status: "revoked", revoke_reason: "compromised" }), keys_revoked: 1 }),
      }),
    );
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input.clone() : new Request(String(input), init);
      if (req.method === "POST" && req.url.endsWith("/revoke")) seen = { body: await req.json(), key: req.headers.get("idempotency-key") };
      return real(input, init);
    });
    renderApp(<App />, { route: `/agents/${AGENT}` });
    await userEvent.click(await screen.findByRole("button", { name: "Revoke…" }));
    await userEvent.click(screen.getByRole("radio", { name: /Compromised/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Confirm handle" }), "research-agent");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(seen).not.toBeNull());
    expect(seen!.body).toMatchObject({ reason: "compromised" });
    expect(seen!.key).toBeTruthy();
  });

  it("offers a reviewer no management controls at all", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/principals": () => errorEnvelope(403, "forbidden") });
    renderApp(<App />, { route: "/agents" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Register agent or app" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke…" })).toBeNull();
  });

  it("marks a revoked principal and drops the controls that no longer apply", async () => {
    mockApi(drawerRoutes({ [`GET /v1/principals/${AGENT}`]: () => json(agent({ status: "revoked", revoke_reason: "compromised", revoked_at: "2026-10-12T11:00:00Z" })) }));
    renderApp(<App />, { route: `/agents/${AGENT}` });
    expect(await screen.findByText(/Revoked \(compromised\)/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke…" })).toBeNull();
  });
});

describe("Ontology access and the Context Lab", () => {
  it("lists source_compromised in the review queue filter", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/reviews": () => json(list()) });
    renderApp(<App />, { route: "/reviews" });
    expect(await screen.findByText("Nothing to review")).toBeInTheDocument();
    expect(within(screen.getByRole("combobox", { name: "Filter by type" })).getByRole("option", { name: "source compromised" })).toBeInTheDocument();
  });

  it("limits the Lab's profiles to those the target agent is registered for", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("workspace_admin")),
      "GET /v1/principals": () => json(list(agent({ allowed_profiles: ["research"] }))),
      "GET /v1/context/profiles": () => json([
        { name: "research", source: "builtin", max_tokens: 2400, max_items: 40, include: [], compression: ["structured"], history: "never" },
        { name: "planning", source: "ontology", max_tokens: 1200, max_items: 30, include: [], compression: ["structured"], history: "never" },
      ]),
    });
    renderApp(<App />, { route: "/context" });
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Build as" }), AGENT);
    const profile = screen.getByRole("combobox", { name: "Profile" });
    expect(within(profile).queryByRole("option", { name: /planning/ })).toBeNull();
    expect(within(profile).getByRole("option", { name: /research/ })).toBeInTheDocument();
    expect(screen.getByText(/may build only: research/)).toBeInTheDocument();
  });
});
