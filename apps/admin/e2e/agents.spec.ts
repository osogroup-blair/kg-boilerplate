import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const API = process.env.KG_API_URL ?? "http://127.0.0.1:8000";
const RUN = Math.random().toString(36).slice(2, 8);
const HANDLE = `agent_research_v2_${RUN}`;

// P7 end to end (needs `make up seed` and Sarah's story: `make test-e2e` runs the demos first). An admin registers an agent through the console, grants Sarah and the project (with
// publish), builds a context AS it in the Lab; the agent publishes a finding; the admin revokes it as compromised and the review queue flags what it produced. A reviewer has no
// management controls at all, and the other workspace's admin sees none of this workspace's principals.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(root, ".local/dev-credentials.json"), "utf8"));
type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

async function call(path: string, key: string, init: { method?: string; body?: unknown; headers?: Record<string, string> } = {}) {
  const res = await fetch(`${API}${path}`, {
    method: init.method ?? "GET",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json", ...(init.method && init.method !== "GET" ? { "idempotency-key": crypto.randomUUID() } : {}), ...init.headers },
    body: init.body ? JSON.stringify(init.body) : undefined,
  });
  return { status: res.status, body: (await res.json().catch(() => ({}))) as Json };
}

async function signIn(page: Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

test.describe.serial("Agents & Access", () => {
  let agentKey = "";
  let previousActive: string | undefined;

  test.beforeAll(async () => {
    // ontology v1.4.0 (Person is subject_scoped, and a publisher's semantic finding needs no review) must be active for this scenario; the original is restored afterwards
    const admin = creds["demo/admin@demo"]!;
    const active = await call("/v1/ontologies/active", admin);
    previousActive = active.body.version?.id;
    const versions = (await call("/v1/ontologies/versions?limit=100", admin)).body.items as Json[];
    let wanted: string | undefined;
    for (const v of versions.filter((x) => x.state === "published")) {
      const spec = (await call(`/v1/ontologies/versions/${v.id}`, admin)).body.spec as Json;
      if (spec.entity_types?.Person?.access === "subject_scoped") wanted = v.id;
    }
    test.skip(!wanted, "ontology v1.4.0 is not published in this workspace (run `make demo-p7` once)");
    if (wanted !== previousActive) {
      const res = await call("/v1/ontologies/active", admin, { method: "POST", body: { version_id: wanted, reason_code: "e2e_agents" } });
      expect([200, 201]).toContain(res.status);
    }
  });

  test.afterAll(async () => {
    const admin = creds["demo/admin@demo"]!;
    if (previousActive) await call("/v1/ontologies/active", admin, { method: "POST", body: { version_id: previousActive, reason_code: "e2e_agents_done" } });
  });

  test("an admin registers an agent, grants Sarah and the project with publish, and builds as it in the Lab", async ({ page }) => {
    await signIn(page, "demo/admin@demo");
    await page.getByRole("link", { name: "Agents & Access" }).click();
    await page.getByRole("button", { name: "Register agent or app" }).click();
    await page.getByRole("textbox", { name: "Handle" }).fill(HANDLE);
    await page.getByRole("checkbox", { name: "research" }).check();
    await page.getByRole("checkbox", { name: "planning" }).check();
    // grant 1: subject Sarah (read + propose)
    await page.getByLabel("Find a subject").fill("Sarah");
    await page.getByRole("button", { name: /Sarah Johnson \(Person\)/ }).first().click();
    await page.getByRole("button", { name: "Add grant" }).click();
    // grant 2: project newco-plan with publish
    await page.getByRole("combobox", { name: "Grant scope" }).selectOption("project");
    await page.getByRole("textbox", { name: "Grant reference" }).fill("newco-plan");
    await page.getByRole("group", { name: "Privileges" }).getByRole("checkbox", { name: /publish/ }).check();
    await page.getByRole("button", { name: "Add grant" }).click();
    await expect(page.getByRole("list", { name: "Initial grants" }).getByRole("listitem")).toHaveCount(2);
    await page.getByRole("button", { name: "Register", exact: true }).click();
    const key = page.getByLabel("API key");
    await expect(key).toContainText("kgk_");
    await expect(page.getByText(/will not be shown again/i)).toBeVisible();
    agentKey = (await key.textContent())!.trim();
    await page.getByRole("button", { name: "I have stored the key" }).click();
    await expect(page.getByLabel("API key")).toHaveCount(0);
    await expect(page.getByRole("button", { name: HANDLE })).toBeVisible();
    // the key is shown once: the principal's own page never shows it
    await page.getByRole("button", { name: HANDLE }).click();
    await page.getByRole("tab", { name: "keys" }).click();
    await expect(page.getByRole("table", { name: "Keys table" })).toContainText("initial");
    await expect(page.locator("body")).not.toContainText(agentKey);
    await page.getByRole("tab", { name: "grants" }).click();
    const grants = page.getByRole("table", { name: "Grants table" });
    await expect(grants).toContainText("project:newco-plan");
    await expect(grants).toContainText("publish");
    // the Lab can build AS the new agent
    await page.goto("/context");
    await page.getByRole("combobox", { name: "Build as" }).selectOption({ label: `${HANDLE} (agent)` });
    await expect(page.getByText(new RegExp(`${HANDLE} may build only: planning, research`))).toBeVisible();
    await page.getByLabel("Find a subject").fill("Sarah");
    const option = page.getByRole("combobox", { name: "Subject" }).locator("option", { hasText: "Sarah Johnson (Person)" }).first();
    await expect(option).toBeAttached();
    await page.getByRole("combobox", { name: "Subject" }).selectOption({ value: (await option.getAttribute("value"))! });
    await page.getByRole("textbox", { name: "Task" }).fill("Help Sarah assess the implications of starting a business");
    await page.getByRole("button", { name: "Build" }).click();
    await expect(page.getByTestId("context-text")).toContainText("Engineering Manager");
  });

  test("the agent publishes a finding; revoking it as compromised flags what it produced", async ({ page }) => {
    test.skip(!agentKey, "the registration test did not run");
    const finding = `E2E finding ${RUN}: NewCo needs a state licence to operate in this sector.`;
    const sarah = ((await call("/v1/entities?q=Sarah&limit=5", creds["demo/admin@demo"]!)).body.items as Json[]).find((e) => e.name === "Sarah Johnson")!;
    const event = await call("/v1/interactions", agentKey, {
      method: "POST",
      body: { source_system: "e2e", source_ref: `e2e-${RUN}`, kind: "message", content: `analysis ${RUN}`, occurred_at: "2026-10-11T09:00:00Z" },
    });
    expect(event.status).toBe(202);
    const proposed = await call("/v1/memories/proposals", agentKey, {
      method: "POST",
      body: { type: "semantic", content: finding, scopes: [{ kind: "project", ref: "newco-plan" }], source_event_ids: [event.body.event_id], subject_ids: [sarah.id], purpose: "e2e" },
    });
    expect(proposed.status).toBe(202);
    expect(proposed.body.status).toBe("active"); // publish privilege + the v1.4.0 policy
    await signIn(page, "demo/admin@demo");
    await page.goto("/agents");
    await page.getByRole("button", { name: HANDLE }).click();
    await page.getByRole("button", { name: "Revoke…" }).click();
    const confirm = page.getByRole("button", { name: "Revoke", exact: true });
    await expect(confirm).toBeDisabled();
    await page.getByRole("radio", { name: /Compromised/ }).check();
    await expect(page.getByRole("list", { name: "Effects of revocation" })).toContainText("disputed");
    await page.getByRole("textbox", { name: "Confirm handle" }).fill(HANDLE);
    await confirm.click();
    await expect(page.getByText(/Revoked \(compromised\)/)).toBeVisible();
    expect((await call("/v1/me", agentKey)).status).toBe(401);
    // the finding is flagged for review, and the queue says why
    await page.goto("/reviews");
    await page.getByRole("combobox", { name: "Filter by type" }).selectOption("source_compromised");
    await expect(page.getByTestId(/^review-/).first()).toContainText("source compromised");
    const memory = (await call(`/v1/memories/${proposed.body.memory_id}`, creds["demo/admin@demo"]!)).body;
    expect(memory.status).toBe("disputed");
  });

  test("a reviewer has no management controls at all", async ({ page }) => {
    await signIn(page, "demo/reviewer@demo");
    await page.goto("/agents");
    await expect(page.getByText(/permission denied/i)).toBeVisible();
    await expect(page.getByRole("button", { name: "Register agent or app" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Revoke…" })).toHaveCount(0);
  });

  test("the other workspace's admin sees none of this workspace's principals", async ({ page }) => {
    await signIn(page, "other_demo/admin@other");
    await page.getByRole("link", { name: "Agents & Access" }).click();
    await expect(page.getByRole("table", { name: "Principals table" })).toBeVisible();
    await expect(page.getByRole("button", { name: HANDLE })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "agent_planning" })).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText(HANDLE);
  });
});
