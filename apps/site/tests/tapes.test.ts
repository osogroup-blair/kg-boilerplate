import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const dir = resolve(__dirname, "../public/tapes");
const tapes = Object.fromEntries(readdirSync(dir).filter((f) => f.endsWith(".json")).map((f) => [f.replace(".json", ""), JSON.parse(readFileSync(resolve(dir, f), "utf8"))]));
const calls = (t: any) => t.steps.flatMap((s: any) => s.calls);

describe("the tapes the playgrounds replay", () => {
  it("are all present", () => {
    expect(Object.keys(tapes).sort()).toEqual(["context-matrix", "crm", "permissions", "sarah", "time", "write-path"]);
  });

  it("have the steps the Sarah player lists, each with narration", () => {
    expect(tapes.sarah.steps.map((s: any) => s.id)).toEqual(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "V"]);
    for (const s of tapes.sarah.steps) {
      expect(s.story, `step ${s.id} story`).toBeTruthy();
      expect(s.look_for, `step ${s.id} look_for`).toBeTruthy();
    }
  });

  it("show a planning context inside its budget at step 6", () => {
    const ctx = tapes.sarah.steps.find((s: any) => s.id === "6").calls.find((c: any) => c.path === "/v1/context/build").response;
    expect(ctx.status).toBe("complete");
    expect(ctx.budgets.used_tokens).toBeLessThanOrEqual(ctx.budgets.max_tokens);
    expect(ctx.context_text).toContain("[CONTEXT DATA");
  });

  it("have a graph snapshot for the steps the viewer offers", () => {
    for (const id of ["2", "4", "7", "9"]) {
      const g = tapes.sarah.steps.find((s: any) => s.id === id).calls.find((c: any) => c.snapshot && c.path.endsWith("/graph"));
      expect(g?.response.nodes.length, `graph at step ${id}`).toBeGreaterThan(1);
    }
  });

  it("keep every budget inside its cap across the whole context matrix", () => {
    const builds = calls(tapes["context-matrix"]);
    expect(builds.length).toBeGreaterThan(50);
    for (const c of builds) expect(c.response.budgets.used_tokens).toBeLessThanOrEqual((c.request as any).max_tokens);
  });

  it("include a budget too small for its required records", () => {
    expect(calls(tapes["context-matrix"]).some((c: any) => c.response.status === "insufficient_budget")).toBe(true);
  });

  it("hide Sarah identically from an agent with no grant: a real id and a made-up id answer the same bytes", () => {
    const p = calls(tapes.permissions).filter((c: any) => c.caller === "ungranted-agent" && c.path.startsWith("/v1/entities/") && !c.path.endsWith("/relationships"));
    expect(p).toHaveLength(2);
    expect(JSON.stringify(p[0].response)).toEqual(JSON.stringify(p[1].response));
    expect(p[0].status).toBe(404);
  });

  it("let Sarah's job end: Acme is current in 2026 and gone by March 2027", () => {
    const at = (d: string) => calls(tapes.time).find((c: any) => c.query?.as_of?.startsWith(d)).response.map((f: any) => f.rel_type);
    expect(at("2026-12")).toContain("WORKS_AT");
    expect(at("2027-03")).not.toContain("WORKS_AT");
  });

  it("walk the write path through at least three distinct stage states", () => {
    const polls = calls(tapes["write-path"]).filter((c: any) => c.method === "GET");
    expect(new Set(polls.map((c: any) => JSON.stringify(c.response.stages))).size).toBeGreaterThanOrEqual(3);
  });

  it("carry no key-shaped strings, JWTs or raw UUIDs", () => {
    for (const [name, t] of Object.entries(tapes)) {
      const text = JSON.stringify(t);
      expect(text, name).not.toMatch(/kgk_(?!EXAMPLE)[A-Za-z0-9_-]{8,}/);
      expect(text, name).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}\./);
      expect(text, name).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/);
    }
  });
});
