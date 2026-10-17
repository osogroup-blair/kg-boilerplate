import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const API = process.env.KG_API_URL ?? "http://127.0.0.1:8000";

// Needs `make up seed` and the Sarah story (`make demo-p2 demo-p4 demo-p5` run first; `make test-e2e` does it). The spec arranges its own memories through the API (a
// procedural proposal, an agent's private working note, a workspace-wide memory) so that each path of the Memory Explorer is exercised end to end.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(root, ".local/dev-credentials.json"), "utf8"));
type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
const stamp = Date.now();

async function api(path: string, who: string, init: RequestInit = {}) {
  const res = await fetch(`${API}${path}`, { ...init, headers: { authorization: `Bearer ${creds[who]!}`, "content-type": "application/json", ...(init.headers ?? {}) } });
  const text = await res.text();
  return { status: res.status, body: (text ? JSON.parse(text) : {}) as Json, etag: res.headers.get("etag") };
}

async function signIn(page: Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

let sarah = "";
let proposalMemory = "";
let proposalReview = "";
let privateNote = "";
let sharedMemory = "";
const PROCEDURE = `When briefing Sarah on a decision, start with the downside case. (e2e ${stamp})`;
const SHARED = `The weekly planning sync is on Mondays at ten. (e2e ${stamp})`;
const NOTE = `scratch note about the licence checklist (e2e ${stamp})`;

test.beforeAll(async () => {
  test.setTimeout(120_000);
  const found = await api("/v1/entities?q=Sarah&limit=20", "demo/reviewer@demo");
  sarah = String(((found.body.items as Json[]).find((e) => (e.external_ids as Json[]).some((x) => x.system === "hr" && x.value === "E-1001")) as Json).id);
  const event = await api("/v1/interactions", "demo/conversation_app", {
    method: "POST",
    headers: { "idempotency-key": `e2e-p5-ev-${stamp}` },
    body: JSON.stringify({ source_system: "conversation_app", source_ref: `e2e-p5-${stamp}`, kind: "observation", occurred_at: "2026-10-10T09:00:00Z", content: "e2e noise with nothing worth remembering" }),
  });
  expect(event.status).toBe(202);
  const proposal = await api("/v1/memories/proposals", "demo/conversation_app", {
    method: "POST",
    headers: { "idempotency-key": `e2e-p5-prop-${stamp}` },
    body: JSON.stringify({ type: "procedural", content: PROCEDURE, subject_ids: [sarah], requested_scope: { kind: "subject", ref: sarah }, source_event_ids: [event.body.event_id] }),
  });
  expect(proposal.status).toBe(202);
  proposalMemory = String(proposal.body.memory_id);
  proposalReview = String(proposal.body.review_id);
  const note = await api("/v1/memories", "demo/agent_research", {
    method: "POST",
    headers: { "idempotency-key": `e2e-p5-note-${stamp}` },
    body: JSON.stringify({ type: "working", content: NOTE, run_id: `e2e-${stamp}` }),
  });
  expect(note.status).toBe(201);
  privateNote = String(note.body.memory_id);
  const shared = await api("/v1/memories", "demo/admin@demo", {
    method: "POST",
    headers: { "idempotency-key": `e2e-p5-shared-${stamp}` },
    body: JSON.stringify({ type: "semantic", content: SHARED, scopes: [{ kind: "workspace" }], source_ref: "e2e", purpose: "team calendar" }),
  });
  expect(shared.status).toBe(201);
  sharedMemory = String(shared.body.memory_id);
});

test("a reviewer approves a procedural proposal and it becomes active", async ({ page }) => {
  await signIn(page, "demo/reviewer@demo");
  await page.getByRole("link", { name: "Review Queue" }).click();
  await page.getByTestId(`review-${proposalReview}`).getByRole("link").click();
  const card = page.getByTestId("memory-card-memory");
  await expect(card.getByText(PROCEDURE)).toBeVisible();
  await expect(card.getByText("proposed")).toBeVisible();
  await page.getByLabel("Approve", { exact: true }).check();
  const record = page.getByRole("button", { name: "Record decision" });
  await expect(record).toBeDisabled(); // a rationale is required
  await page.getByLabel("Rationale").selectOption({ index: 1 });
  await record.click();
  await expect.poll(async () => (await api(`/v1/memories/${proposalMemory}`, "demo/reviewer@demo")).body.status as string, { timeout: 20_000 }).toBe("active");
  await page.goto(`/memories/${proposalMemory}`);
  await expect(page.getByTestId("memory-content")).toHaveText(PROCEDURE);
  await expect(page.getByRole("button", { name: "Supersede…" })).toBeEnabled();
});

test("an administrator sees another agent's private note only after confirming the audited prompt", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await page.getByRole("link", { name: "Memories" }).click();
  await expect(page.getByRole("heading", { name: "Memories" })).toBeVisible();
  await expect(page.getByTestId(`memory-${privateNote}`)).toHaveCount(0); // not there by default
  await page.getByRole("checkbox", { name: /private memory/i }).click(); // controlled: it flips only after the prompt is confirmed
  await expect(page.getByRole("button", { name: "Show and audit" })).toBeVisible(); // the prompt comes first
  await expect(page.getByTestId(`memory-${privateNote}`)).toHaveCount(0);
  await page.getByRole("button", { name: "Show and audit" }).click();
  await expect(page.getByTestId(`memory-${privateNote}`)).toBeVisible();
  await expect(page.getByText(/recorded in the audit log/).first()).toBeVisible();
  await page.getByTestId(`memory-${privateNote}`).getByRole("link").click();
  await expect(page.getByTestId("memory-content")).toHaveText(NOTE);
  const audit = await api("/v1/audit?action=memory.private_read&limit=20", "demo/admin@demo");
  expect((audit.body.items as Json[]).some((a) => a.resource_id === privateNote || a.action === "memory.private_read")).toBe(true);
  // and without the prompt the same id is simply not found
  await page.goto(`/memories/${privateNote}`);
  await expect(page.getByText("Not found").first()).toBeVisible();
});

test("a role that can read but not curate sees the memory without any action", async ({ page }) => {
  await signIn(page, "demo/operator@demo");
  await page.goto(`/memories/${sharedMemory}`);
  await expect(page.getByTestId("memory-content")).toHaveText(SHARED);
  await expect(page.getByRole("button", { name: "Supersede…" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Expire…" })).toHaveCount(0);
  await expect(page.getByText(/read this memory but not change it/)).toBeVisible();
  // a memory scoped to Sarah is not theirs to see: no grant, no memory (a plain Not found)
  await page.goto(`/memories/${proposalMemory}`);
  await expect(page.getByText("Not found").first()).toBeVisible();
});

test("another workspace's admin sees no demo memories and Not found for their ids", async ({ page }) => {
  await signIn(page, "other_demo/admin@other");
  await page.getByRole("link", { name: "Memories" }).click();
  await expect(page.getByTestId(`memory-${sharedMemory}`)).toHaveCount(0);
  await expect(page.getByText("No memories to show")).toBeVisible();
  await page.goto(`/memories/${sharedMemory}`);
  await expect(page.getByText("Not found").first()).toBeVisible();
});

test("a curator supersedes a memory with a reason, and the old one is kept with its lineage", async ({ page }) => {
  await signIn(page, "demo/reviewer@demo");
  await page.goto(`/memories/${sharedMemory}`);
  await page.getByRole("button", { name: "Supersede…" }).click();
  const confirm = page.getByRole("button", { name: "Supersede", exact: true });
  await expect(confirm).toBeDisabled();
  await page.getByLabel("New wording").fill(`The weekly planning sync moved to Tuesdays at ten. (e2e ${stamp})`);
  await page.getByLabel("Reason").selectOption("clarified");
  await confirm.click();
  await expect.poll(async () => (await api(`/v1/memories/${sharedMemory}`, "demo/reviewer@demo")).body.status as string, { timeout: 20_000 }).toBe("superseded");
  await page.reload();
  await expect(page.getByText(/kept for history/)).toBeVisible();
  await expect(page.getByTestId("lineage-replaced_by")).toBeVisible();
});

test("an agent gets the memory list through the API only if it holds a role that may read memory", async ({ page }) => {
  await signIn(page, "demo/agent_support");
  await page.goto("/memories");
  // allowed to read, granted nothing: only workspace-wide memory is visible, never a subject-scoped, project, or another agent's private one
  await expect(page.getByRole("link", { name: /weekly planning sync/ }).first()).toBeVisible(); // a workspace-wide memory
  await expect(page.getByText(NOTE)).toHaveCount(0);
  const scopes = await page.locator("tbody tr td:nth-child(7)").allInnerTexts();
  expect(scopes.length).toBeGreaterThan(0);
  expect(scopes.every((s) => s.trim() === "workspace")).toBe(true);
});
