import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");
const API = process.env.KG_API_URL ?? "http://127.0.0.1:8000";

// Needs `make up seed` and the Sarah story (`make demo-p2 demo-p4` run first; `make test-e2e` does it). The spec reads what the demo left (the unverified
// CONSIDERING fact, the decided reviews) and arranges ONE open identity review of its own (a second "Sarah Johnson" from the cycling club) so the reviewer path is exercised end to end.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(root, ".local/dev-credentials.json"), "utf8"));
type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

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

let openReview = "";

test.beforeAll(async () => {
  test.setTimeout(150_000);
  // A different person with the same name: no subject_ref, so the platform keeps the mention apart and opens an identity_match review for it.
  const stamp = Date.now();
  const accepted = await api("/v1/interactions", "demo/conversation_app", {
    method: "POST",
    headers: { "idempotency-key": `e2e-p4-cyclist-${stamp}` },
    body: JSON.stringify({ source_system: "conversation_app", source_ref: `e2e-p4-${stamp}`, kind: "message", occurred_at: "2026-10-10T09:00:00Z", content: `Sarah Johnson from the cycling club rode 40 km with Tom on Saturday. (e2e ${stamp})` }),
  });
  expect(accepted.status).toBe(202);
  const eventId = String(accepted.body.event_id);
  const find = async () => {
    const list = await api("/v1/reviews?status=open&limit=100", "demo/reviewer@demo");
    return (list.body.items as Json[] | undefined)?.find((r) => r.interaction_id === eventId && r.item_type === "identity_match");
  };
  await expect.poll(async () => Boolean(await find()), { timeout: 120_000, intervals: [1000] }).toBe(true);
  openReview = String((await find())!.id);
});

test("ADM-02: select the CONSIDERING fact and see evt_003 and the unverified label", async ({ page }) => {
  await signIn(page, "demo/reviewer@demo");
  await page.getByRole("link", { name: "Graph Explorer" }).click();
  await expect(page.getByRole("heading", { name: "Graph Explorer" })).toBeVisible();
  // two people are called Sarah Johnson since the cyclist scenario: the one with the HR id is the colleague
  const found = await api("/v1/entities?q=Sarah&limit=20", "demo/reviewer@demo");
  const sarah = (found.body.items as Json[]).find((e) => (e.external_ids as Json[]).some((x) => x.system === "hr" && x.value === "E-1001"));
  expect(sarah).toBeTruthy();
  await page.goto(`/graph?entity=${sarah!.id}`);
  await page.getByText("Table view of the same neighbourhood").click();
  const table = page.getByRole("table", { name: "Relationships in this neighbourhood" });
  await expect(table).toBeVisible();
  await expect(table.getByRole("row").filter({ hasText: "CONSIDERING" }).getByText("unverified")).toBeVisible();
  await table.getByRole("button", { name: "CONSIDERING" }).click(); // the accessible twin of clicking the edge on the canvas
  const details = page.getByRole("complementary", { name: "Details" });
  await expect(details.getByText("unverified").first()).toBeVisible();
  await expect(details.getByText(/conversation_app/).first()).toBeVisible(); // the evidence names the interaction's source system
});

test("ADM-02: a reviewer decides an identity match (different people) and the item leaves the open queue", async ({ page }) => {
  await signIn(page, "demo/reviewer@demo");
  await page.getByRole("link", { name: "Review Queue" }).click();
  await expect(page.getByRole("heading", { name: "Review Queue" })).toBeVisible();
  await page.getByTestId(`review-${openReview}`).getByRole("link").click();
  await expect(page.getByRole("heading", { name: "Review" })).toBeVisible();
  await page.getByLabel("Different entities").check();
  const record = page.getByRole("button", { name: "Record decision" });
  await expect(record).toBeDisabled(); // a rationale is required
  await page.getByLabel("Rationale").selectOption({ index: 1 });
  await expect(record).toBeEnabled();
  await record.click();
  await expect.poll(async () => (await api(`/v1/reviews/${openReview}`, "demo/reviewer@demo")).body.status as string, { timeout: 20_000 }).not.toBe("open");
});

test("a reviewer's decided item cannot be decided twice", async () => {
  const detail = await api(`/v1/reviews/${openReview}`, "demo/reviewer@demo");
  const again = await api(`/v1/reviews/${openReview}/decision`, "demo/reviewer@demo", {
    method: "POST",
    headers: { "idempotency-key": `e2e-p4-again-${Date.now()}`, "if-match": detail.etag ?? 'W/"rev-1"' },
    body: JSON.stringify({ decision: "different_entity", rationale_code: "other" }),
  });
  expect([409, 412]).toContain(again.status);
});

test("another workspace's admin sees no demo review items and gets Not found for their ids", async ({ page }) => {
  await signIn(page, "other_demo/admin@other");
  await page.getByRole("link", { name: "Review Queue" }).click();
  await expect(page.getByRole("heading", { name: "Review Queue" })).toBeVisible();
  await expect(page.getByTestId(`review-${openReview}`)).toHaveCount(0);
  await page.goto(`/reviews/${openReview}`);
  await expect(page.getByText("Not found").first()).toBeVisible();
});

test("an agent role gets a permission state, not a broken page", async ({ page }) => {
  await signIn(page, "demo/agent_planning");
  await page.goto("/reviews");
  await expect(page.getByText("Permission denied")).toBeVisible();
  await page.goto("/graph");
  await expect(page.getByText("Permission denied")).toBeVisible();
});
