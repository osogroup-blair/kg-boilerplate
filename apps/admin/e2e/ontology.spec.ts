import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

// Needs `make up seed demo-p2` (API on :8000, demo workspace with Sarah's story). `make test-e2e` runs demo-p2 first.
const creds: Record<string, string> = JSON.parse(readFileSync(resolve(here, "../../../.local/dev-credentials.json"), "utf8"));

async function signIn(page: Page, who: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(creds[who]!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("principal-handle")).toHaveText(who.split("/")[1]!);
}

test("ADM-01: a workspace admin designs, validates, publishes and activates an ontology version without writing code", async ({ page }) => {
  await signIn(page, "demo/admin@demo");
  await page.getByRole("link", { name: "Ontology" }).click();
  await expect(page.getByRole("heading", { name: "Ontology" })).toBeVisible();

  // a draft from the bundled template
  await page.getByRole("button", { name: "New draft" }).click();
  await page.getByLabel("The personal-knowledge template").check();
  await page.getByRole("button", { name: "Create draft" }).click();
  await expect(page.getByTestId("version-title")).toContainText("personal-knowledge v1.");
  const title = (await page.getByTestId("version-title").innerText()).trim();
  const number = /v(\d+\.\d+\.\d+)/.exec(title)![1]!;
  const versionUrl = page.url();
  await expect(page.getByText("No errors. The ontology is ready to publish.")).toBeVisible();

  // add Person.nickname through the form
  await page.getByRole("link", { name: "Types" }).click();
  await page.getByRole("button", { name: "Person", exact: true }).click();
  await expect(page.getByTestId("attr-name")).toBeVisible();
  await page.getByLabel("New attribute name").fill("nickname");
  await page.getByLabel("New attribute type").selectOption("string");
  await page.getByRole("button", { name: "Add attribute" }).click();
  await expect(page.getByTestId("attr-nickname")).toBeVisible();
  await expect(page.getByText("Unsaved changes")).toBeVisible();

  // a deliberate mistake is anchored to its field, then fixed
  await page.getByLabel("Default of nickname").fill("x");
  await page.getByLabel("Max length of nickname").fill("1");
  await page.getByLabel("Pattern of nickname").fill("(");
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByTestId("attr-nickname").getByRole("alert")).toContainText("invalid_pattern");
  await page.getByLabel("Pattern of nickname").fill("");
  await page.getByLabel("Default of nickname").fill("");
  await page.getByLabel("Max length of nickname").fill("40");
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Unsaved changes")).toBeHidden();

  // validate and publish
  await page.getByRole("link", { name: "Overview" }).click();
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("No errors. The ontology is ready to publish.")).toBeVisible();
  await expect(page.getByText("entity_types.Person.attributes.nickname")).toBeVisible(); // the diff lists the new attribute
  await page.getByRole("button", { name: "Publish…" }).click();
  await page.getByLabel("Release notes").fill("adds Person.nickname (e2e)");
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  await expect(page.getByText("Active", { exact: true })).toBeVisible();
  await expect(page.getByText("published", { exact: true })).toBeVisible();
  await expect(page.getByTestId("version-title")).toContainText(`v${number}`);
  await expect(page.getByText(/cannot be edited/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Save draft" })).toHaveCount(0);

  // the versions list shows it as the active, published version
  await page.getByRole("link", { name: "All versions" }).click();
  const row = page.getByRole("row", { name: new RegExp(`v${number.replace(/\./g, "\\.")}`) });
  await expect(row).toContainText("Active");
  await expect(row).toContainText("published");

  // a reviewer reads the same version, read-only, with no way to publish
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "demo/reviewer@demo");
  await page.goto(versionUrl);
  await expect(page.getByTestId("version-title")).toContainText(`v${number}`);
  await expect(page.getByRole("button", { name: /Publish|Activate|Retire|Delete/ })).toHaveCount(0);
  await page.getByRole("link", { name: "Types" }).click();
  await expect(page.getByLabel("New attribute name")).toHaveCount(0);
  await page.goto("/ontology");
  await expect(page.getByRole("button", { name: "New draft" })).toHaveCount(0);

  // another workspace sees none of it, and the same deep link is Not found
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "other_demo/admin@other");
  await page.goto("/ontology");
  await expect(page.getByText("No ontology versions")).toBeVisible();
  await page.goto(versionUrl);
  await expect(page.getByText(/This ontology version does not exist/)).toBeVisible();
  await page.goto("/entities");
  await expect(page.getByText("No entities yet")).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain("Sarah");
});

test("entities: provenance, history and redaction for a read-only agent; foreign ids are Not found", async ({ page }) => {
  await signIn(page, "demo/agent_planning");
  await page.getByRole("link", { name: "Entities" }).click();
  await page.getByLabel("Search by name").fill("sarah");
  await page.getByRole("link", { name: "Sarah Johnson" }).click();
  await expect(page.getByTestId("entity-name")).toHaveText("Sarah Johnson");
  await expect(page.getByText("hr:E-1001")).toBeVisible();
  await expect(page.getByText("redacted")).toBeVisible(); // birth_date is sensitive: agents never see the value
  expect(await page.locator("body").innerText()).not.toContain("1985-03-02");
  await expect(page.getByRole("button", { name: /Add relationship/ })).toHaveCount(0);

  await expect(page.getByText("Engineering Manager")).toBeVisible();
  await expect(page.getByText("Software Engineer")).toHaveCount(0); // current view only
  await page.getByLabel("Show history").check();
  await expect(page.getByText("Software Engineer")).toBeVisible();
  await expect(page.getByText("ended")).toBeVisible();
  await expect(page.getByText(/source 2024-06-10/)).toBeVisible();

  await page.getByRole("link", { name: "command" }).first().click();
  await expect(page.getByRole("heading", { name: "Command" })).toBeVisible();
  await expect(page.getByText("Permission denied")).toBeVisible(); // agents read entities but not the ledger
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "demo/admin@demo");
  await page.goto("/entities");
  await page.getByRole("link", { name: "Sarah Johnson" }).click();
  await expect(page.getByText("1985-03-02")).toBeVisible(); // a workspace admin sees the sensitive value
  const path = new URL(page.url()).pathname;
  await page.getByRole("link", { name: "Commands", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Commands" })).toBeVisible();
  await page.getByRole("button", { name: "relationship.assert" }).first().click();
  await expect(page.getByText("Decision trace")).toBeVisible();
  await expect(page.getByText("schema_validation")).toBeVisible();
  await page.keyboard.press("Escape"); // close the drawer: while it is open the rest of the page is inert

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "other_demo/admin@other");
  await page.goto(path);
  await expect(page.getByText(/This entity does not exist/)).toBeVisible();
});
