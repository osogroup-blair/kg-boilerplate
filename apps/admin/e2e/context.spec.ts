import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const API = process.env.KG_API_URL ?? "http://127.0.0.1:8000";

// ADM-04 / ADM-05 end to end. Needs `make up seed` and Sarah's story (`make demo-p2 demo-p4 demo-p5 demo-p6` run first; `make test-e2e` does it): the Lab builds AS agent_planning
// and AS agent_support through the real console and the real API, and the page must show what each of them would get, and nothing more.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(root, ".local/dev-credentials.json"), "utf8"));
type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

async function api(path: string, who: string) {
  const res = await fetch(`${API}${path}`, { headers: { authorization: `Bearer ${creds[who]!}` } });
  return { status: res.status, body: (await res.json()) as Json };
}

async function signIn(page: Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

async function pickSarah(page: Page) {
  await page.getByLabel("Find a subject").fill("Sarah");
  const option = page.getByRole("combobox", { name: "Subject" }).locator("option", { hasText: "Sarah Johnson (Person)" }).first();
  await expect(option).toBeAttached();
  await page.getByRole("combobox", { name: "Subject" }).selectOption({ value: (await option.getAttribute("value"))! });
}

async function build(page: Page, as: string, task: string, profile: string, opts: { budget?: number; project?: string } = {}) {
  await page.getByRole("combobox", { name: "Build as" }).selectOption({ label: `${as} (agent)` });
  await page.getByRole("combobox", { name: "Profile" }).selectOption(profile);
  await pickSarah(page);
  if (opts.project) await page.getByLabel("Project").fill(opts.project);
  await page.getByRole("textbox", { name: "Task" }).fill(task);
  if (opts.budget) await page.getByRole("slider", { name: "Budget" }).fill(String(opts.budget));
  await page.getByRole("button", { name: "Build" }).click();
}

test("the Lab shows the planning agent's package, why each item is in or out, and the budget (ADM-04)", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await page.getByRole("link", { name: "Context Lab" }).click();
  await build(page, "agent_planning", "Help Sarah assess the implications of starting a business", "planning", { budget: 600, project: "newco-plan" });
  const text = page.getByTestId("context-text");
  await expect(text).toContainText("Engineering Manager");
  await expect(text).toContainText("Acme");
  await expect(text).toContainText("No verified financial runway for Sarah Johnson");
  await expect(text).not.toContainText("cycling club");
  await expect(text).not.toContainText("NewCo licensing rules"); // the research agent's private note
  const used = Number(await page.getByTestId("used-tokens").first().textContent());
  expect(used).toBeGreaterThan(0);
  expect(used).toBeLessThanOrEqual(600);
  // the business memory is included, with a reason in the trace; the cycling memory is out (below threshold) or never a candidate
  await expect(page.getByRole("region", { name: "Selected items" }).first()).toContainText("packed");
  await expect(page.getByTestId("candidates-by-source").first()).toContainText("memory");
  const excluded = page.getByRole("region", { name: "Excluded items" }).first();
  await expect(excluded).not.toContainText("note"); // nothing private was ever a candidate
  // the trace can be opened from the package and shows the same build
  await page.getByRole("link", { name: "open the trace" }).first().click();
  await expect(page.getByRole("heading", { name: /Context trace/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "Trace" })).toContainText("ctx-rank.v1");
});

test("the Lab as agent_support shows facts and none of Sarah's memories (ADM-05, S7)", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await page.goto("/context");
  await page.getByRole("combobox", { name: "Build as" }).selectOption({ label: "agent_support (agent)" });
  await page.getByRole("combobox", { name: "Profile" }).selectOption("planning");
  await pickSarah(page);
  await page.getByRole("textbox", { name: "Task" }).fill("Help Sarah assess the implications of starting a business");
  await page.getByRole("button", { name: "Build" }).click();
  const text = page.getByTestId("context-text");
  await expect(text).toContainText("Engineering Manager"); // facts are workspace-readable until P7
  await expect(text).not.toContainText("software business"); // her memories are not agent_support's to see
  await expect(text).not.toContainText("GUIDANCE");
  await expect(page.getByRole("region", { name: "Memories" })).toHaveCount(0);
  await expect(page.getByTestId("candidates-by-source").first()).not.toContainText("memory");
});

test("the compare mode builds the same task at two budgets", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await page.goto("/context");
  await page.getByRole("checkbox", { name: /Compare with a second budget/ }).check();
  await build(page, "agent_planning", "Help Sarah assess the implications of starting a business", "planning", { budget: 1200, project: "newco-plan" });
  await expect(page.getByTestId("run-1200")).toBeVisible();
  await expect(page.getByTestId("run-250")).toBeVisible();
  const big = Number(await page.getByTestId("run-1200").getByTestId("used-tokens").textContent());
  const small = Number(await page.getByTestId("run-250").getByTestId("used-tokens").textContent());
  expect(small).toBeLessThan(big);
  expect(small).toBeLessThanOrEqual(250);
});

test("a reviewer has no Context Lab, and cannot open it", async ({ page }) => {
  await signIn(page, "demo/reviewer@demo");
  await expect(page.getByRole("link", { name: "Context Lab" })).toHaveCount(0);
  await page.goto("/context");
  await expect(page.getByText(/not permitted|permission/i)).toBeVisible();
});

test("the admin of another workspace cannot act as this workspace's agent", async () => {
  const principals = await api("/v1/principals?limit=100", "demo/admin@demo");
  const planning = (principals.body.items as Json[]).find((p) => p.handle === "agent_planning")!;
  const res = await fetch(`${API}/v1/context/build`, {
    method: "POST",
    headers: { authorization: `Bearer ${creds["other_demo/admin@other"]!}`, "content-type": "application/json", "x-act-as-principal": String(planning.id) },
    body: JSON.stringify({ task: "anything" }),
  });
  expect(res.status).toBe(404); // the same answer as a principal that does not exist
});

test("the memory page counts the builds that used it (administrators)", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  const memories = await api("/v1/memories?type=episodic&limit=50", "demo/admin@demo");
  const business = (memories.body.items as Json[]).find((m) => String(m.summary).toLowerCase().includes("entrepreneurship"));
  test.skip(!business, "the Sarah story has not been run");
  await page.goto(`/memories/${business!.id}`);
  await expect(page.getByRole("region", { name: "Used in contexts" })).toBeVisible();
  await expect(page.getByTestId("usage-count")).toContainText("context build");
});
