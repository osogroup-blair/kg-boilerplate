import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

// Needs `make up seed` (API on :8000). Keys come from the gitignored credentials file written by the seed.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(here, "../../../.local/dev-credentials.json"), "utf8"));
const DEMO_HANDLES = ["admin@demo", "reviewer@demo", "operator@demo", "conversation_app", "agent_research", "agent_planning", "agent_support"];

async function signIn(page: import("@playwright/test").Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

test("demo admin: dashboard is healthy and Agents lists the demo principals (plus any agents the P7 demo registered and revoked)", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await expect(page.getByTestId("workspace-name")).toHaveText("Demo Workspace");
  await expect(page.getByText("postgres ok")).toBeVisible();
  await page.getByRole("link", { name: "Agents & Access" }).click();
  await expect(page.getByRole("table", { name: "Principals table" }).getByRole("row").nth(7)).toBeVisible(); // header + the 7 seeded (at least); `make demo-p7` adds its own (revoked) agents
  for (const h of DEMO_HANDLES) await expect(page.getByRole("button", { name: h, exact: true })).toBeVisible();
});

test("other_demo admin sees only its own principals and none of demo's handles anywhere", async ({ page }) => {
  await signIn(page, "other_demo/admin@other");
  await page.getByRole("link", { name: "Agents & Access" }).click();
  await expect(page.getByRole("table", { name: "Principals table" }).getByRole("row")).toHaveCount(3); // header + 2
  await expect(page.getByRole("button", { name: "agent_other" })).toBeVisible();
  const text = await page.locator("body").innerText();
  for (const h of DEMO_HANDLES) expect(text).not.toContain(h);
  expect(text).not.toMatch(/(^|\n)Demo Workspace/); // "Other Demo Workspace" is this workspace's own name
  await page.getByRole("link", { name: "Dashboard" }).click();
  await expect(page.getByText("postgres ok")).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain("agent_planning");
});
