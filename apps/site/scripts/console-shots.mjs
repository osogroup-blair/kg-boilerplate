// Takes the console screenshots used by "Tour of the console". Needs the dev stack (`make up seed`), a workspace built by `make demo-sarah`
// (its keys are in .local/examples/sarah-latest.json), and the admin dev server on :5273 (`make admin-dev`).
// Run: node scripts/console-shots.mjs      Writes public/console/<screen>-<light|dark>.webp
import { chromium } from "@playwright/test";
import { readFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const here = dirname(fileURLToPath(import.meta.url));
const state = JSON.parse(readFileSync(resolve(here, "../../../.local/examples/sarah-latest.json"), "utf8"));
const out = resolve(here, "../public/console");
mkdirSync(out, { recursive: true });
const CONSOLE = process.env.CONSOLE_URL ?? "http://localhost:5273";

const screens = [
  { name: "dashboard", path: "/", wait: "text=postgres ok" },
  { name: "ontology", path: "/ontology" },
  { name: "entities", path: "/entities" },
  { name: "graph", path: `/graph?entity=${state.ids.sarah}`, settle: 2500, before: async (page) => { for (const l of ["ended", "proposed"]) await page.getByLabel(l, { exact: true }).check().catch(() => {}); await page.getByLabel("Hops").selectOption("2").catch(() => {}); } },
  { name: "memories", path: "/memories" },
  { name: "context-lab", path: "/context" },
  { name: "reviews", path: "/reviews" },
  { name: "interactions", path: "/interactions" },
  { name: "deletions", path: "/deletions" },
  { name: "agents", path: "/agents" },
  { name: "operations", path: "/operations" },
];

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
for (const scheme of ["light", "dark"]) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.goto(CONSOLE + "/");
  await page.getByLabel("API key").fill(state.keys.admin);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForSelector('[data-testid="principal-handle"]');
  for (const s of screens) {
    await page.goto(CONSOLE + s.path);
    if (s.wait) await page.waitForSelector(s.wait, { timeout: 15000 }).catch(() => {});
    await page.waitForLoadState("networkidle").catch(() => {});
    if (s.before) await s.before(page);
    await page.waitForTimeout(s.settle ?? 800);
    const png = await page.screenshot();
    await sharp(png).webp({ quality: 82 }).toFile(resolve(out, `${s.name}-${scheme}.webp`));
    console.log(`${s.name}-${scheme}`);
  }
  await ctx.close();
}
await browser.close();
