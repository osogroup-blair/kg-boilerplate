import { expect, test, type Page } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const API = process.env.KG_API_URL ?? "http://127.0.0.1:8000";

// Needs `make up seed` (API on :8000, the compose worker with .local/faults mounted). The spec arranges its own dead job through the worker's
// failure-injection file, exactly as `make demo-p3` does, so it does not depend on the demo having run.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(root, ".local/dev-credentials.json"), "utf8"));
const faults = resolve(root, ".local/faults");
const MARKER = `e2e-marker-${Date.now()}`;
let eventId = "";

async function signIn(page: Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

async function api(path: string, who: string, init: RequestInit = {}) {
  const res = await fetch(`${API}${path}`, { ...init, headers: { authorization: `Bearer ${creds[who]!}`, "content-type": "application/json", ...(init.headers ?? {}) } });
  return { status: res.status, body: (await res.json()) as Record<string, unknown> };
}

test.beforeAll(async () => {
  mkdirSync(faults, { recursive: true });
  for (const f of ["counters.json", "spec"]) if (existsSync(resolve(faults, f))) unlinkSync(resolve(faults, f));
  writeFileSync(resolve(faults, "spec"), "normalize:raise_config:1\n"); // the next normalize job is a config-terminal poison
  try {
    const accepted = await api("/v1/interactions", "demo/conversation_app", {
      method: "POST",
      headers: { "idempotency-key": `e2e-adm06-${Date.now()}` },
      body: JSON.stringify({ source_system: "conversation_app", source_ref: "e2e-adm06", kind: "message", content: `Sarah mentioned the Berlin office. ${MARKER}`, occurred_at: "2026-10-10T19:00:00Z" }),
    });
    expect(accepted.status).toBe(202);
    eventId = String(accepted.body.event_id);
    await expect.poll(async () => (await api(`/v1/interactions/${eventId}`, "demo/admin@demo")).body.processing_status as string, { timeout: 60_000 }).toBe("failed");
  } finally {
    if (existsSync(resolve(faults, "spec"))) unlinkSync(resolve(faults, "spec")); // the fault is cleared; a retry will succeed
  }
});

test("ADM-06: an operator finds the dead letter, retries it with a reason and sees it succeed", async ({ page }) => {
  await signIn(page, "demo/operator@demo");
  await page.getByRole("link", { name: "Operations" }).click();
  await expect(page.getByRole("heading", { name: "Operations" })).toBeVisible();
  await expect(page.getByTestId("worker-heartbeats")).toContainText("running");
  await expect(page.getByTestId("kpi-dead-letters")).not.toHaveText("0");

  // the dead letter for our event, then its attempts
  const row = page.getByRole("row").filter({ hasText: `event ${eventId.slice(0, 8)}` });
  await expect(row).toBeVisible();
  await row.getByRole("link", { name: /normalize/ }).click();
  await expect(page.getByText("config_terminal: fault_injected")).toBeVisible();

  // a reason is required
  await page.getByRole("button", { name: "Retry…" }).click();
  const confirm = page.getByRole("button", { name: "Retry", exact: true });
  await expect(confirm).toBeDisabled();
  await page.getByLabel("Reason").selectOption("fault_cleared");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByText("Retry queued as a new generation.")).toBeVisible();

  // the event finishes at generation 1
  await page.goto(`/interactions/${eventId}`);
  await expect(page.getByTestId("stage-normalize")).toContainText("generation 1", { timeout: 30_000 });
  await expect(page.getByTestId("stage-normalize")).toContainText("succeeded", { timeout: 30_000 });
  await expect(page.getByText("indexed", { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId("stage-graph_ingest")).toContainText("enabled in P4");
});

test("ADM-06: a reviewer sees the interactions list but no Reveal and no Replay", async ({ page }) => {
  await signIn(page, "demo/reviewer@demo");
  await page.getByRole("link", { name: "Interactions & Episodes" }).click();
  await expect(page.getByRole("heading", { name: "Interactions & Episodes" })).toBeVisible();
  await expect(page.getByRole("row").nth(1)).toBeVisible(); // there are events to see
  await page.goto(`/interactions/${eventId}`);
  await expect(page.getByTestId("stage-normalize")).toBeVisible();
  await expect(page.getByRole("button", { name: /Reveal content/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Replay…" })).toHaveCount(0);
  await page.getByRole("link", { name: "Operations" }).click();
  await expect(page.getByText("Permission denied")).toBeVisible(); // operations.read is for admins and operators
});

test("EVD-06: an admin reveals the content as text and the read shows up in the audit trail", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await page.goto(`/interactions/${eventId}`);
  await expect(page.getByRole("button", { name: /Reveal content/ })).toBeVisible();
  await expect(page.getByText(MARKER)).toHaveCount(0); // not shown until asked for
  await page.getByRole("button", { name: /Reveal content/ }).click();
  await expect(page.getByText(/recorded in the audit log with your name/)).toBeVisible();
  await page.getByRole("button", { name: "Reveal and audit" }).click();
  await expect(page.getByTestId("revealed-content")).toContainText(MARKER);

  await page.getByRole("link", { name: "Dashboard" }).click();
  await expect(page.getByText("interaction.content_read").first()).toBeVisible();
});

test("ADM-06: another workspace's admin sees none of demo's interactions and gets Not found for its ids", async ({ page }) => {
  await signIn(page, "other_demo/admin@other");
  await page.getByRole("link", { name: "Interactions & Episodes" }).click();
  await expect(page.getByRole("heading", { name: "Interactions & Episodes" })).toBeVisible();
  await expect(page.getByText(eventId.slice(0, 8))).toHaveCount(0);
  await page.goto(`/interactions/${eventId}`);
  await expect(page.getByText("Not found").first()).toBeVisible();
  await page.goto("/operations");
  await expect(page.getByRole("heading", { name: "Operations" })).toBeVisible();
  await expect(page.getByText(eventId.slice(0, 8))).toHaveCount(0);
});
