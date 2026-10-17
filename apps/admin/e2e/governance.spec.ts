import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const API = process.env.KG_API_URL ?? "http://127.0.0.1:8000";
const RUN = Math.random().toString(36).slice(2, 8);

// P8 end to end (needs `make up seed`). A reviewer corrects a memory in the Memory Explorer and the next context build reflects it (ADM-03); an admin deletes a person through the
// console and reads the receipt, then holds another person and sees the deletion wait until the hold is released; the Settings page narrows egress and cannot widen it; and the other
// workspace's admin sees none of this. Every run creates its own people, so it can be repeated. `make test-e2e` re-seeds afterwards (the deletions here are permanent).
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(root, ".local/dev-credentials.json"), "utf8"));
type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

async function call(path: string, key: string, init: { method?: string; body?: unknown; headers?: Record<string, string> } = {}) {
  const res = await fetch(`${API}${path}`, {
    method: init.method ?? "GET",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json", ...(init.method && init.method !== "GET" ? { "idempotency-key": crypto.randomUUID() } : {}), ...init.headers },
    body: init.body ? JSON.stringify(init.body) : undefined,
  });
  return { status: res.status, body: (await res.json().catch(() => ({}))) as Json, headers: res.headers };
}

async function signIn(page: Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

async function person(name: string): Promise<string> {
  const r = await call("/v1/entities", creds["demo/conversation_app"]!, { method: "POST", body: { type: "Person", name, source_system: "hr", source_ref: `e2e-${name}`, external_ids: [{ system: "hr", value: `E-${name}` }] } });
  expect([200, 201, 202]).toContain(r.status);
  return r.body.entity_id as string;
}

test.describe.serial("Governance", () => {
  const admin = creds["demo/admin@demo"]!;

  test("ADM-03: a reviewer corrects a memory in the Memory Explorer and the next context build reflects it", async ({ page }) => {
    const sarah = ((await call("/v1/entities?q=Sarah&limit=5", admin)).body.items as Json[]).find((e) => e.name === "Sarah Johnson")?.id as string | undefined;
    test.skip(!sarah, "Sarah's story is not in this workspace (run the earlier demos)");
    const wrong = `Sarah is allergic to ${RUN}nuts and avoids every kind of snack at team events.`;
    const made = await call("/v1/memories", admin, { method: "POST", body: { type: "semantic", content: wrong, subject_ids: [sarah], purpose: "e2e", source_ref: `e2e-${RUN}` } });
    expect(made.status).toBe(201);
    const id = made.body.memory_id as string;
    await signIn(page, "demo/reviewer@demo");
    await page.goto(`/memories/${id}`);
    await page.getByRole("button", { name: "Correct (with impact)…" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Replace it with corrected wording").check();
    const right = `Sarah has no allergies; she skips snacks at team events by preference ${RUN}.`;
    await dialog.getByRole("textbox", { name: "Corrected wording" }).fill(right);
    await dialog.getByLabel("Reason").selectOption("incorrect_extraction");
    await expect(dialog.getByLabel("Impact preview")).toBeVisible();
    await dialog.getByRole("button", { name: "Apply correction" }).click();
    await expect(dialog.getByText(/Done\./)).toBeVisible();
    // the Lab/API build now carries the corrected memory and not the wrong one
    let text = "";
    for (let i = 0; i < 40 && !text.includes(right); i++) {
      const ctx = await call("/v1/context/build", admin, { method: "POST", body: { task: `What should we know about Sarah and snacks ${RUN}?`, subject_ids: [sarah], profile: "planning" } });
      text = String(ctx.body.context_text ?? "");
      if (!text.includes(right)) await new Promise((r) => setTimeout(r, 1000));
    }
    expect(text).toContain(right);
    expect(text).not.toContain(wrong);
  });

  test("delete a person through the console, read the receipt, and see the person gone", async ({ page }) => {
    const name = `E2E Gone ${RUN}`;
    const id = await person(name);
    await signIn(page, "demo/admin@demo");
    await page.getByRole("link", { name: "Deletions" }).click();
    await page.getByRole("button", { name: "Request a deletion…" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Entity id").fill(id);
    await dialog.getByLabel(/permanently/).check();
    await dialog.getByRole("button", { name: "Delete" }).click();
    await expect(page.getByRole("heading", { name: /Deletion of subject/ })).toBeVisible();
    expect((await call(`/v1/entities/${id}`, admin)).status).toBe(404); // unreadable the moment it was accepted
    await expect(page.getByText("completed", { exact: true }).first()).toBeVisible({ timeout: 90_000 });
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toBeVisible();
    await expect(receipt.getByRole("table", { name: "Receipt by store" })).toContainText("postgres");
    await expect(receipt.getByTestId("backup-expiry")).not.toHaveText("—");
    expect(await page.locator("body").innerText()).not.toContain(name); // the receipt holds counts, never a name
    // re-creating him is refused until an admin lifts the suppression
    const again = await call("/v1/entities", creds["demo/conversation_app"]!, { method: "POST", body: { type: "Person", name: "Someone else", source_system: "hr", source_ref: `again-${RUN}`, external_ids: [{ system: "hr", value: `E-${name}` }] } });
    expect(again.status).toBe(409);
  });

  test("a legal hold freezes a deletion without purging it, and releasing the hold lets it finish", async ({ page }) => {
    const name = `E2E Held ${RUN}`;
    const id = await person(name);
    await signIn(page, "demo/admin@demo");
    await page.goto("/deletions?tab=holds");
    await page.getByRole("button", { name: "Place a hold…" }).click();
    let dialog = page.getByRole("dialog");
    await dialog.getByLabel("Target id").fill(id);
    await dialog.getByRole("button", { name: "Place hold" }).click();
    await expect(page.getByRole("table", { name: "Legal holds" })).toContainText("litigation");
    await page.getByRole("tab", { name: "Requests" }).click();
    await page.getByRole("button", { name: "Request a deletion…" }).click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Entity id").fill(id);
    await dialog.getByLabel(/permanently/).check();
    await dialog.getByRole("button", { name: "Delete" }).click();
    await expect(page.getByText(/A legal hold covers this target/)).toBeVisible();
    expect((await call(`/v1/entities/${id}`, admin)).status).toBe(404); // frozen
    const rows = (await call(`/v1/legal-holds?active=true`, admin)).body as unknown as Json[];
    const hold = rows.find((h) => h.target_id === id)!;
    await page.goto("/deletions?tab=holds");
    await page.getByRole("button", { name: new RegExp(`Release hold ${hold.id.slice(0, 8)}`) }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Release" }).click();
    await page.goto("/deletions");
    await expect(page.getByRole("table", { name: "Deletion requests" })).toBeVisible();
    await expect.poll(async () => {
      const list = (await call("/v1/deletion-requests?limit=20", admin)).body as unknown as Json[];
      return list.find((r) => r.target_id === id)?.status;
    }, { timeout: 90_000 }).toMatch(/^completed/);
  });

  test("Settings can narrow egress and shows that the platform's class cannot be allowed; the Privacy audit lists what happened", async ({ page }) => {
    await signIn(page, "demo/admin@demo");
    await page.getByRole("link", { name: "Settings" }).click();
    const local = page.getByLabel("Deny local_only");
    await expect(local).toBeChecked();
    await expect(local).toBeDisabled();
    await expect(page.getByLabel(/allow/i)).toHaveCount(0);
    await page.getByLabel("Deny restricted").check();
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByText("Saved as a new version.")).toBeVisible();
    await page.getByLabel("Deny restricted").uncheck(); // put it back
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByText("Saved as a new version.")).toBeVisible();
    await page.getByRole("link", { name: "Privacy audit" }).click();
    const table = page.getByRole("table", { name: "Privacy timeline" });
    await expect(table).toContainText("settings.updated");
    await expect(table).toContainText("deletion.requested");
    expect(await page.locator("body").innerText()).not.toContain(`E2E Gone ${RUN}`);
  });

  test("the other workspace's admin sees none of it", async ({ page }) => {
    await signIn(page, "other_demo/admin@other");
    await page.getByRole("link", { name: "Deletions" }).click();
    await expect(page.getByText("No deletion requests")).toBeVisible();
    await page.getByRole("link", { name: "Privacy audit" }).click();
    expect(await page.locator("body").innerText()).not.toContain("deletion.requested");
    expect((await call("/v1/deletion-requests", creds["other_demo/admin@other"]!)).body).toEqual([]);
  });

  test("a reviewer has no Deletions, Settings or Privacy audit entry", async ({ page }) => {
    await signIn(page, "demo/reviewer@demo");
    for (const name of ["Deletions", "Settings", "Privacy audit"]) await expect(page.getByRole("link", { name })).toHaveCount(0);
  });
});
