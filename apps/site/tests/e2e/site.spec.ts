import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const pages = [
  "/",
  "/see-it/",
  "/see-it/sarah/",
  "/see-it/context/",
  "/see-it/time/",
  "/see-it/permissions/",
  "/see-it/graph/",
  "/see-it/write-path/",
  "/sandbox/",
  "/docs/get-started/introduction/",
  "/docs/get-started/quickstart/",
  "/docs/get-started/faq/",
  "/docs/concepts/context-engine/",
  "/docs/concepts/time/",
  "/docs/tutorials/first-domain/",
  "/docs/tutorials/connect-an-agent-sdk/",
  "/docs/reference/api-recipes/",
  "/docs/reference/configuration/",
];

for (const path of pages) {
  test(`${path}: loads with no console errors and no serious accessibility problems`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    const res = await page.goto(path);
    expect(res?.status()).toBe(200);
    await page.waitForLoadState("networkidle");
    expect(errors, errors.join("\n")).toEqual([]);
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const bad = axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(bad.map((v) => `${v.id}: ${v.nodes.slice(0, 2).map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("the Sarah player steps, shows a context at step 6 and its graph at step 2", async ({ page }) => {
  await page.goto("/see-it/sarah/");
  await expect(page.getByRole("heading", { name: /workspace, an ontology/i })).toBeVisible();
  await page.getByRole("button", { name: /bounded, cited context/i }).click();
  await expect(page.getByRole("heading", { name: "Context package" })).toBeVisible();
  await expect(page.getByText(/tokens used/)).toBeVisible();
  await page.getByRole("button", { name: /Facts that come from systems/i }).click();
  await expect(page.getByRole("button", { name: "Show as table" })).toBeVisible();
  await page.getByRole("button", { name: "Show as table" }).click();
  await expect(page.getByRole("table")).toContainText("WORKS_AT");
});

test("the context explorer reports a budget that is too small, then a complete package", async ({ page }) => {
  await page.goto("/see-it/context/");
  await page.getByLabel(/Token budget/).fill("0");
  await expect(page.getByText("insufficient budget")).toBeVisible();
  await page.getByLabel(/Token budget/).fill("2");
  await expect(page.getByText("complete").first()).toBeVisible();
});

test("time travel: Acme is gone by 2027-03", async ({ page }) => {
  await page.goto("/see-it/time/");
  const slider = page.getByRole("slider");
  const max = await slider.getAttribute("max");
  await slider.fill(String(Number(max) - 2));
  await expect(page.getByText(/Asking about 2027-03|Asking about 2027-02/)).toBeVisible();
  await expect(page.locator(".kgi-table")).not.toContainText("WORKS_AT");
});

test("the permission lens shows identical bodies", async ({ page }) => {
  await page.goto("/see-it/permissions/");
  await expect(page.getByText(/byte-for-byte identical/)).toBeVisible();
});

test("the write path plays through its stages", async ({ page }) => {
  await page.goto("/see-it/write-path/");
  await page.getByRole("button", { name: "Next →" }).click();
  await page.getByRole("button", { name: "Next →" }).click();
  await expect(page.locator(".kgi-stage.done").first()).toBeVisible();
});

test("the sandbox page explains itself when no sandbox is configured", async ({ page }) => {
  await page.goto("/sandbox/");
  await expect(page.getByText("The hosted sandbox is not open yet")).toBeVisible();
  await expect(page.getByText("What the sandbox is, and is not")).toBeVisible();
});

test("search finds a concept page", async ({ page }) => {
  await page.goto("/docs/get-started/introduction/");
  await page.getByRole("button", { name: /search/i }).first().click();
  await page.getByPlaceholder(/search/i).fill("succession");
  await expect(page.locator("site-search, dialog").getByText(/succession/i).first()).toBeVisible({ timeout: 15000 });
});

test("the API reference mounts and lists the platform's routes", async ({ page }) => {
  await page.goto("/api/");
  await expect(page.getByText("Knowledge & Memory Platform API").first()).toBeVisible({ timeout: 30000 });
  await expect(page.getByText("Five calls to try first")).toBeVisible();
});

for (const path of ["/", "/see-it/sarah/", "/see-it/context/", "/see-it/permissions/", "/sandbox/", "/docs/get-started/quickstart/"]) {
  test(`${path}: no horizontal scroll at phone width`, async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 360, height: 780 } });
    const page = await ctx.newPage();
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await ctx.close();
  });
}

for (const path of [
  "/docs/get-started/introduction/",
  "/docs/concepts/architecture/",
  "/docs/concepts/ledger-and-graph/",
  "/docs/concepts/time/",
  "/docs/concepts/curated-memory/",
  "/docs/concepts/ontology/",
  "/docs/concepts/security-model/",
  "/docs/concepts/commands-vs-extraction/",
  "/docs/concepts/context-engine/",
  "/docs/concepts/identity/",
  "/docs/concepts/multi-agent/",
  "/docs/concepts/governance/",
  "/docs/concepts/models-and-router/",
  "/docs/guides/deploy-to-production/",
]) {
  test(`${path}: every diagram renders`, async ({ page }) => {
    await page.goto(path);
    const diagrams = page.locator("pre.mermaid, .mermaid");
    const n = await diagrams.count();
    expect(n).toBeGreaterThan(0);
    await expect(page.locator(".mermaid svg").first()).toBeVisible({ timeout: 20000 });
    await page.waitForTimeout(500);
    const text = await page.locator("body").innerText();
    expect(text).not.toMatch(/Syntax error in (text|graph)|Parse error on line/);
    expect(await page.locator(".mermaid svg").count()).toBe(n);
  });
}
