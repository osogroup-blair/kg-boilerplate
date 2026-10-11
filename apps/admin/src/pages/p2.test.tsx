import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ME, errorEnvelope, json, mockApi, renderApp } from "../test-utils";

const meAs = (...roles: string[]) => ({ ...ME, principal: { ...ME.principal, roles } });
const SPEC = {
  ontology: { key: "personal-knowledge", version: "1.0.0", mode: "strict" },
  entity_types: {
    Person: { description: "", identity: { key: "platform_uuid", external_id_systems: ["hr"], aliases_from: ["name"] }, allowed_classifications: ["internal"], attributes: { name: { type: "string", required: true }, birth_date: { type: "date", sensitive: true, sensitivity: "restricted" } }, rules: [] },
    Organization: { attributes: { name: { type: "string", required: true } }, rules: [] },
  },
  relationship_types: { WORKS_AT: { source: "Person", target: "Organization", temporal: true, cardinality: "many_to_many", attributes: { role: { type: "string" } }, rules: [{ kind: "require_source", params: {} }] } },
  rules: [],
};
const REPORT_OK = { errors: [], warnings: [], rule_tests: [], content_hash: null };
const version = (over: Record<string, unknown> = {}) => ({
  id: "v1", ontology_key: "personal-knowledge", version: "1.0.0", state: "draft", active: false, content_hash: null, revision: 1, error_count: 0, notes: "",
  created_at: "2026-10-11T10:00:00Z", published_at: null, retired_at: null, spec: SPEC, source_text: "ontology:\n  id: personal-knowledge\n", base_version_id: null, validation_report: REPORT_OK, activated: false, ...over,
});
const CATALOG = {
  stages: ["schema_validation"],
  kinds: [
    { kind: "require_source", family: "authority", stage: "mutation_authorization", implicit: false, commands: [], applies_to: ["entity", "relationship", "workspace"], default_action: "reject", allowed_actions: ["reject", "queue_review", "warn"], description: "The command must carry a source.", conflict_policy: "Independent of authority.", params_schema: { properties: {} } },
    { kind: "max_active_per_source", family: "cardinality", stage: "temporal_rules", implicit: false, commands: [], applies_to: ["relationship"], default_action: "reject", allowed_actions: ["reject"], description: "At most n.", conflict_policy: "Counts the chain.", params_schema: { properties: { n: { type: "integer", minimum: 1, title: "N" } }, required: ["n"] } },
  ],
};
const DIFF = { from_version: null, to_version: "1.0.0", changes: [], impact: [] };

type Handler = (req: Request) => Response | Promise<Response>;
const designer = (v = version(), extra: Record<string, Handler> = {}) =>
  mockApi({
    "GET /v1/me": () => json(meAs("workspace_admin")),
    "GET /v1/ontologies/versions/v1": () => new Response(JSON.stringify(v), { status: 200, headers: { "content-type": "application/json", etag: `W/"rev-${String(v.revision)}"` } }),
    "GET /v1/ontologies/versions/v1/diff": () => json(DIFF),
    "GET /v1/ontologies/rule-catalog": () => json(CATALOG),
    ...extra,
  });

describe("Ontology versions", () => {
  const LIST = { items: [version({ id: "v2", version: "1.1.0", state: "draft", error_count: 2 }), version({ id: "v1", state: "published", active: true, content_hash: "ab".repeat(32), published_at: "2026-10-11T11:00:00Z", notes: "first" })], next_cursor: null };

  it("lists versions with state, active marker, hash and error count", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/ontologies/versions": () => json(LIST), "GET /v1/ontologies/active": () => json({ version: LIST.items[1], entity_types: [], relationship_types: [] }) });
    renderApp(<App />, { route: "/ontology" });
    const rows = await screen.findAllByRole("row");
    expect(within(rows[1]!).getByText("draft")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("2")).toHaveClass("text-red-600");
    expect(within(rows[2]!).getByText("Active")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("abababababab")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New draft" })).toBeInTheDocument();
  });

  it("does not open the New draft dialog until the versions are known, so the template is never created at a taken version", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => { release = r; });
    let posted: string | null = null;
    mockApi({
      "GET /v1/me": () => json(ME),
      "GET /v1/ontologies/versions": async () => { await gate; return json(LIST); },
      "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }),
      "POST /v1/ontologies/versions": async (req) => { posted = String(((await req.json()) as { content: string }).content); return json(version({ id: "v3" }), 201); },
      "GET /v1/ontologies/versions/v3": () => json(version({ id: "v3" })), "GET /v1/ontologies/versions/v3/diff": () => json(DIFF), "GET /v1/ontologies/rule-catalog": () => json(CATALOG),
    });
    renderApp(<App />, { route: "/ontology" });
    const button = await screen.findByRole("button", { name: "New draft" });
    expect(button).toBeDisabled();
    release();
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await userEvent.click(screen.getByLabelText(/The personal-knowledge template/));
    await userEvent.click(screen.getByRole("button", { name: "Create draft" }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toContain("version: 1.2.0"); // 1.0.0 and 1.1.0 are taken
  });

  it("a reviewer reads versions but sees no New draft button", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/ontologies/versions": () => json(LIST), "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }) });
    renderApp(<App />, { route: "/ontology" });
    await screen.findByText("1.1.0", { exact: false });
    expect(screen.queryByRole("button", { name: "New draft" })).not.toBeInTheDocument();
    expect(screen.getByText(/need the workspace admin role/)).toBeInTheDocument();
  });

  it("shows the empty, denied and error states", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/ontologies/versions": () => json({ items: [], next_cursor: null }), "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }) });
    const first = renderApp(<App />, { route: "/ontology" });
    expect(await screen.findByText("No ontology versions")).toBeInTheDocument();
    first.unmount();
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/ontologies/versions": () => errorEnvelope(403, "forbidden"), "GET /v1/ontologies/active": () => errorEnvelope(403, "forbidden") });
    const second = renderApp(<App />, { route: "/ontology" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    second.unmount();
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/ontologies/versions": () => errorEnvelope(500, "internal_error"), "GET /v1/ontologies/active": () => errorEnvelope(500, "internal_error") });
    renderApp(<App />, { route: "/ontology" });
    expect(await screen.findByText("Could not load")).toBeInTheDocument();
  });

  it("creates a draft from the template and opens it", async () => {
    let posted: { body: Record<string, unknown>; key: string | null } | null = null;
    mockApi({
      "GET /v1/me": () => json(ME), "GET /v1/ontologies/versions": () => json({ items: [], next_cursor: null }), "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }),
      "POST /v1/ontologies/versions": async (req) => { posted = { body: (await req.json()) as Record<string, unknown>, key: req.headers.get("idempotency-key") }; return json(version(), 201); },
      "GET /v1/ontologies/versions/v1": () => json(version()), "GET /v1/ontologies/versions/v1/diff": () => json(DIFF), "GET /v1/ontologies/rule-catalog": () => json(CATALOG),
    });
    renderApp(<App />, { route: "/ontology" });
    await userEvent.click(await screen.findByRole("button", { name: "New draft" }));
    await userEvent.click(screen.getByRole("button", { name: "Create draft" }));
    expect(await screen.findByTestId("version-title")).toHaveTextContent("personal-knowledge v1.0.0");
    expect(posted!.key).toBeTruthy();
    expect(posted!.body.format).toBe("yaml");
    expect(String(posted!.body.content)).toContain("personal-knowledge"); // the bundled template, not a hard-coded copy
  });
});

describe("Ontology designer", () => {
  it("anchors a server error to the form field that owns its path", async () => {
    const report = { errors: [{ code: "unknown_entity_type", path: "relationship_types.WORKS_AT.target", message: "'Company' is not a declared entity type" }, { code: "reserved_graphiti_field", path: "entity_types.Person.attributes.labels", message: "'labels' is a reserved Graphiti node field" }], warnings: [], rule_tests: [], content_hash: null };
    const bad = { ...SPEC, entity_types: { ...SPEC.entity_types, Person: { ...SPEC.entity_types.Person, attributes: { ...SPEC.entity_types.Person.attributes, labels: { type: "string" } } } }, relationship_types: { WORKS_AT: { ...SPEC.relationship_types.WORKS_AT, target: "Company" } } };
    designer(version({ spec: bad, validation_report: report, error_count: 2 }));
    const types = renderApp(<App />, { route: "/ontology/versions/v1/types/Person" });
    const attr = await screen.findByTestId("attr-labels");
    expect(within(attr).getByRole("alert")).toHaveTextContent("reserved_graphiti_field");
    expect(within(screen.getByTestId("attr-birth_date")).queryByRole("alert")).not.toBeInTheDocument();
    types.unmount();
    designer(version({ spec: bad, validation_report: report, error_count: 2 }));
    renderApp(<App />, { route: "/ontology/versions/v1/relationships/WORKS_AT" });
    const target = await screen.findByLabelText("Target type");
    expect(target.closest("label")).toHaveTextContent("unknown_entity_type");
    expect(screen.getByLabelText("Source type").closest("label")).not.toHaveTextContent("unknown_entity_type");
  });

  it("the overview lists each error with a link to the form that fixes it, and blocks publishing", async () => {
    const report = { errors: [{ code: "reserved_graphiti_field", path: "entity_types.Person.attributes.labels", message: "reserved" }], warnings: [], rule_tests: [], content_hash: null };
    designer(version({ validation_report: report, error_count: 1 }));
    renderApp(<App />, { route: "/ontology/versions/v1" });
    const link = await screen.findByRole("link", { name: "entity_types.Person.attributes.labels" });
    expect(link).toHaveAttribute("href", "/ontology/versions/v1/types/Person");
    await userEvent.click(screen.getByRole("button", { name: "Publish…" }));
    expect(await screen.findByText(/Fix the 1 validation error/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
  });

  it("publishing needs release notes, then publishes with them and activates by default", async () => {
    let body: unknown = null;
    designer(version(), { "POST /v1/ontologies/versions/v1/publish": async (req) => { body = await req.json(); return json(version({ state: "published", active: true, content_hash: "ab".repeat(32), published_at: "2026-10-11T11:00:00Z" })); } });
    renderApp(<App />, { route: "/ontology/versions/v1" });
    await userEvent.click(await screen.findByRole("button", { name: "Publish…" }));
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
    expect(screen.getByText(/0 breaking|change\(s\) against the active version/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Release notes"), "adds nickname");
    await userEvent.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(body).toEqual({ notes: "adds nickname", activate: true }));
  });

  it("adds an attribute through the form, saves with If-Match and an Idempotency-Key, then adopts the new revision", async () => {
    let put: { body: { content: { entity_types: { Person: { attributes: Record<string, unknown> } } }; format: string }; headers: Headers } | null = null;
    designer(version(), {
      "PUT /v1/ontologies/versions/v1": async (req) => {
        put = { body: (await req.json()) as never, headers: req.headers };
        return new Response(JSON.stringify(version({ revision: 2, spec: (put.body as { content: unknown }).content })), { status: 200, headers: { "content-type": "application/json", etag: 'W/"rev-2"' } });
      },
    });
    renderApp(<App />, { route: "/ontology/versions/v1/types/Person" });
    await userEvent.type(await screen.findByLabelText("New attribute name"), "nickname");
    await userEvent.click(screen.getByRole("button", { name: "Add attribute" }));
    expect(await screen.findByTestId("attr-nickname")).toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(put).not.toBeNull());
    expect(put!.headers.get("if-match")).toBe('W/"rev-1"');
    expect(put!.headers.get("idempotency-key")).toBeTruthy();
    expect(put!.body.format).toBe("json");
    expect(put!.body.content.entity_types.Person.attributes.nickname).toMatchObject({ type: "string" });
    await waitFor(() => expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument());
  });

  it("explains a lost update (412) instead of overwriting", async () => {
    designer(version(), { "PUT /v1/ontologies/versions/v1": () => errorEnvelope(412, "precondition_failed") });
    renderApp(<App />, { route: "/ontology/versions/v1/types/Person" });
    await userEvent.type(await screen.findByLabelText("New attribute name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Add attribute" }));
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText(/changed elsewhere/)).toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument(); // the user's edits are kept
  });

  it("a published version is read-only: no save, no add, disabled inputs", async () => {
    designer(version({ state: "published", active: true, content_hash: "cd".repeat(32), published_at: "2026-10-11T11:00:00Z" }));
    renderApp(<App />, { route: "/ontology/versions/v1/types/Person" });
    expect(await screen.findByText(/cannot be edited/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save draft" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("New attribute name")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Type description")).toBeDisabled();
    expect(screen.getByLabelText("Type of birth_date")).toBeDisabled();
  });

  it("a reviewer sees a draft read-only with no Publish button", async () => {
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")), "GET /v1/ontologies/versions/v1": () => json(version()), "GET /v1/ontologies/versions/v1/diff": () => json(DIFF), "GET /v1/ontologies/rule-catalog": () => json(CATALOG),
    });
    renderApp(<App />, { route: "/ontology/versions/v1" });
    expect(await screen.findByText(/Read-only: editing needs the workspace admin role/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Publish/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save draft" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Validate" })).toBeInTheDocument(); // validation is a read
  });

  it("a rolled-back candidate can be activated with a reason; the active version cannot", async () => {
    let body: unknown = null;
    designer(version({ state: "published", active: false, content_hash: "cd".repeat(32), published_at: "2026-10-11T11:00:00Z" }), {
      "POST /v1/ontologies/active": async (req) => { body = await req.json(); return json({ version: version({ active: true }), entity_types: [], relationship_types: [] }); },
    });
    const first = renderApp(<App />, { route: "/ontology/versions/v1" });
    await userEvent.click(await screen.findByRole("button", { name: "Activate (roll back)…" }));
    await userEvent.clear(screen.getByLabelText("Reason code"));
    await userEvent.type(screen.getByLabelText("Reason code"), "bad regression");
    expect(screen.getByRole("button", { name: "Activate" })).toBeDisabled(); // not a valid reason code
    await userEvent.clear(screen.getByLabelText("Reason code"));
    await userEvent.type(screen.getByLabelText("Reason code"), "rollback_regression");
    await userEvent.click(screen.getByRole("button", { name: "Activate" }));
    await waitFor(() => expect(body).toEqual({ version_id: "v1", reason_code: "rollback_regression" }));
    first.unmount();
    designer(version({ state: "published", active: true, content_hash: "cd".repeat(32), published_at: "2026-10-11T11:00:00Z" }));
    renderApp(<App />, { route: "/ontology/versions/v1" });
    await screen.findByTestId("version-title");
    expect(screen.queryByRole("button", { name: /Activate/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retire/ })).not.toBeInTheDocument();
  });

  it("a foreign or unknown version id is the same Not found state", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/ontologies/versions/other": () => errorEnvelope(404, "not_found") });
    renderApp(<App />, { route: "/ontology/versions/other" });
    expect(await screen.findByText(/This ontology version does not exist/)).toBeInTheDocument();
  });

  it("rule params come from the catalog's schema and a new rule starts from its defaults", async () => {
    let put: { content: { relationship_types: { WORKS_AT: { rules: Array<{ kind: string; params: Record<string, unknown> }> } } } } | null = null;
    designer(version(), { "PUT /v1/ontologies/versions/v1": async (req) => { put = (await req.json()) as never; return json(version({ revision: 2 })); } });
    renderApp(<App />, { route: "/ontology/versions/v1/relationships/WORKS_AT" });
    await userEvent.selectOptions(await screen.findByLabelText("Add a rule"), "max_active_per_source"); // the workspace-wide editor below has its own, differently named control
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    const n = await screen.findByLabelText("N");
    expect(n).toHaveValue(1);
    await userEvent.clear(n);
    await userEvent.type(n, "2");
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(put).not.toBeNull());
    expect(put!.content.relationship_types.WORKS_AT.rules.map((r) => r.kind)).toEqual(["require_source", "max_active_per_source"]);
    expect(put!.content.relationship_types.WORKS_AT.rules[1]!.params.n).toBe(2);
  });
});

const ENTITY = {
  id: "e1", type: "Person", name: "Sarah Johnson", aliases: [], attributes: { birth_date: { redacted: true }, nickname: "Sas" }, classification: "internal", status: "active", revision: 3,
  external_ids: [{ system: "hr", value: "E-1001" }], ontology_version_id: "v1", recorded_at: "2026-10-11T10:00:00Z", updated_at: "2026-10-11T10:00:00Z", projection_status: "projected", embedding_status: "embedded", projected_at: null,
};
const fact = (over: Record<string, unknown>) => ({
  id: "f1", rel_type: "WORKS_AT", source: { id: "e1", type: "Person", name: "Sarah Johnson" }, target: { id: "e2", type: "Organization", name: "Acme" }, attributes: { role: "Engineering Manager" },
  valid_from: "2024-06-01T00:00:00Z", valid_from_unknown: false, valid_to: null, source_recorded_at: "2024-06-10T00:00:00Z", recorded_at: "2026-10-11T10:05:00Z", superseded_at: null, succeeded_by_fact_id: null,
  status: "active", phase: "current", authority_class: "authoritative_system", classification: "internal", source_type: "command", source_command_id: "c9", source_system: "hr", source_ref: "evt_002", ontology_version_id: "v1", revision: 1, projection_status: "projected", ...over,
});

describe("Entity directory and detail", () => {
  it("searches by name, filters by type and links to the detail", async () => {
    const seen: string[] = [];
    mockApi({
      "GET /v1/me": () => json(ME), "GET /v1/ontologies/active": () => json({ version: version(), entity_types: ["Organization", "Person"], relationship_types: [] }),
      "GET /v1/entities": (req) => { seen.push(new URL(req.url).search); return json({ items: [ENTITY], next_cursor: null }); },
    });
    renderApp(<App />, { route: "/entities" });
    const link = await screen.findByRole("link", { name: "Sarah Johnson" });
    expect(link).toHaveAttribute("href", "/entities/e1");
    expect(screen.getByText("hr:E-1001")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search by name"), "sarah");
    await waitFor(() => expect(seen.some((s) => s.includes("q=sarah"))).toBe(true));
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "Person");
    await waitFor(() => expect(seen.some((s) => s.includes("type=Person"))).toBe(true));
  });

  it("shows empty and no-match states", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/ontologies/active": () => json({ version: null, entity_types: [], relationship_types: [] }), "GET /v1/entities": () => json({ items: [], next_cursor: null }) });
    renderApp(<App />, { route: "/entities" });
    expect(await screen.findByText("No entities yet")).toBeInTheDocument();
    expect(screen.getByText(/no active ontology/)).toBeInTheDocument();
  });

  it("shows a sensitive attribute as 'redacted', never the object, and the history toggle asks for history", async () => {
    const urls: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("reviewer")),
      "GET /v1/entities/e1": (req) => {
        urls.push(new URL(req.url).search);
        const history = new URL(req.url).searchParams.get("include") === "history";
        return json({ ...ENTITY, relationships: history ? [fact({ id: "f0", attributes: { role: "Software Engineer" }, valid_from: "2022-01-10T00:00:00Z", valid_to: "2024-06-01T00:00:00Z", phase: "historical", source_recorded_at: null }), fact({})] : [fact({})] });
      },
    });
    renderApp(<App />, { route: "/entities/e1" });
    expect(await screen.findByTestId("entity-name")).toHaveTextContent("Sarah Johnson");
    expect(screen.getByText("redacted")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("{&quot;redacted");
    expect(screen.getByText("Sas")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add relationship/ })).not.toBeInTheDocument(); // a reviewer reads
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + the current fact
    expect(screen.getByText(/role: Engineering Manager/)).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Show history"));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(3));
    expect(urls.at(-1)).toContain("include=history");
    expect(screen.getByText("ended")).toBeInTheDocument();
    expect(screen.getByText(/role: Software Engineer/)).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    expect(within(rows[1]!).getByText(/2022-01-10 → 2024-06-01/)).toBeInTheDocument();
    expect(within(rows[2]!).getByText(/source 2024-06-10/)).toBeInTheDocument(); // source-recorded time is shown apart from the platform's record time
    expect(within(rows[2]!).getByRole("link", { name: "command" })).toHaveAttribute("href", "/commands/c9");
  });

  it("an admin sees the unredacted value and the write actions", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("workspace_admin")), "GET /v1/entities/e1": () => json({ ...ENTITY, attributes: { birth_date: "1985-03-02" }, relationships: [fact({})] }) });
    renderApp(<App />, { route: "/entities/e1" });
    expect(await screen.findByText("1985-03-02")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add relationship/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "End…" })).toBeInTheDocument();
  });

  it("a foreign or unknown entity id is the same Not found; denied and failing reads have their own states", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/entities/x": () => errorEnvelope(404, "not_found") });
    const a = renderApp(<App />, { route: "/entities/x" });
    expect(await screen.findByText(/This entity does not exist/)).toBeInTheDocument();
    a.unmount();
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/entities/x": () => errorEnvelope(403, "forbidden") });
    const b = renderApp(<App />, { route: "/entities/x" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    b.unmount();
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/entities/x": () => errorEnvelope(500, "internal_error") });
    renderApp(<App />, { route: "/entities/x" });
    expect(await screen.findByText("Could not load")).toBeInTheDocument();
  });

  it("creating an entity shows a rejection by code and path, never the submitted values, and uses a fresh key per click", async () => {
    const keys: string[] = [];
    mockApi({
      "GET /v1/me": () => json(meAs("app_writer")), "GET /v1/ontologies/active": () => json({ version: version(), entity_types: ["Person"], relationship_types: [] }),
      "GET /v1/entities": () => json({ items: [], next_cursor: null }),
      "POST /v1/entities": (req) => { keys.push(req.headers.get("idempotency-key") ?? ""); return json({ error: { code: "rule_violation", message: "m", request_id: "r", retry_after: null, details: { command_id: "c1", violations: [{ rule_kind: "attribute_types", rule_id: null, code: "required_attribute", path: "attributes.birth_date" }] } } }, 422); },
    });
    renderApp(<App />, { route: "/entities" });
    await userEvent.click(await screen.findByRole("button", { name: "New entity" }));
    await userEvent.type(screen.getByLabelText("Name"), "Secret Name 123");
    await userEvent.click(screen.getByRole("button", { name: "Create entity" }));
    const alert = await screen.findByText(/Rejected:/);
    expect(alert.closest("[role=alert]")).toHaveTextContent("required_attribute");
    expect(alert.closest("[role=alert]")).toHaveTextContent("attributes.birth_date");
    expect(alert.closest("[role=alert]")).not.toHaveTextContent("Secret Name 123");
    await userEvent.click(screen.getByRole("button", { name: "Create entity" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).not.toBe(keys[1]);
  });
});

describe("Command ledger", () => {
  const LIST = { items: [{ id: "c1", command_type: "relationship.assert", status: "rejected", actor_principal_id: "p", authority_class: "authoritative_system", classification: "internal", source_system: "hr", source_ref: "evt_9", ontology_version_id: "v1", http_status: 422, projection_status: "not_applicable", projection_attempts: 0, last_error_code: null, created_at: "2026-10-11T10:00:00Z", projected_at: null, entity_id: null, fact_id: null }], next_cursor: null };
  const DETAIL = { ...LIST.items[0], decision: [{ stage: "schema_validation", outcome: "reject", violations: [{ rule_kind: "endpoint_types", rule_id: null, code: "disallowed_endpoint", path: "target", action: "reject" }] }, { stage: "decision", outcome: "reject" }], result: {}, status_url: "/v1/commands/c1" };

  it("lists commands and opens the decision trace from a deep link", async () => {
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/commands": () => json(LIST), "GET /v1/commands/c1": () => json(DETAIL) });
    renderApp(<App />, { route: "/commands/c1" });
    expect(await screen.findByText("Decision trace")).toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("disallowed_endpoint")).toBeInTheDocument();
    expect(within(dialog).getByText(/endpoint_types, reject/)).toBeInTheDocument();
    expect(within(dialog).getByText("schema_validation")).toBeInTheDocument();
    expect(within(dialog).getByText("hr · evt_9")).toBeInTheDocument();
  });

  it("an agent is denied the ledger; a foreign command id is Not found", async () => {
    mockApi({ "GET /v1/me": () => json(meAs("agent")), "GET /v1/commands": () => errorEnvelope(403, "forbidden") });
    const a = renderApp(<App />, { route: "/commands" });
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    a.unmount();
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/commands": () => json(LIST), "GET /v1/commands/zzz": () => errorEnvelope(404, "not_found") });
    renderApp(<App />, { route: "/commands/zzz" });
    expect(await screen.findByText(/This command does not exist/)).toBeInTheDocument();
  });

  it("filters by status through the URL and shows the empty state", async () => {
    const seen: string[] = [];
    mockApi({ "GET /v1/me": () => json(ME), "GET /v1/commands": (req) => { seen.push(new URL(req.url).search); return json({ items: [], next_cursor: null }); } });
    renderApp(<App />, { route: "/commands" });
    await userEvent.selectOptions(await screen.findByLabelText("Filter by status"), "rejected");
    await waitFor(() => expect(seen.some((s) => s.includes("status=rejected"))).toBe(true));
    expect(await screen.findByText("No commands")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
