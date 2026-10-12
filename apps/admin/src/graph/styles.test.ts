import { describe, expect, it } from "vitest";
import { edgeStyle, HOPS_MAX, nodeStyle, NODES_MAX } from "./styles";

const fact = (over: Record<string, unknown> = {}) => ({ status: "active", evidence_status: "verified", phase: "current", rel_type: "WORKS_AT", valid_from: "2024-06-01T00:00:00Z", valid_to: null, ...over });

describe("edge styles", () => {
  it("draws a current, verified fact solid", () => {
    const s = edgeStyle(fact());
    expect(s).toMatchObject({ line: "solid", badge: null, warning: false, classes: ["current"] });
  });
  it("draws an ended fact dashed with its dates", () => {
    const s = edgeStyle(fact({ phase: "historical", valid_from: "2022-01-10T00:00:00Z", valid_to: "2024-06-01T00:00:00Z" }));
    expect(s.line).toBe("dashed");
    expect(s.classes).toContain("historical");
    expect(s.label).toContain("2022-01-10 → 2024-06-01");
  });
  it("gives an extracted, unverified fact its own class and badge", () => {
    const s = edgeStyle(fact({ evidence_status: "unverified", rel_type: "CONSIDERING" }));
    expect(s.classes).toEqual(["unverified"]);
    expect(s.badge).toBe("unverified");
    expect(s.label).toBe("CONSIDERING · unverified");
  });
  it("warns on proposed and disputed facts and never draws them like current ones", () => {
    expect(edgeStyle(fact({ status: "proposed", evidence_status: "unverified" }))).toMatchObject({ line: "dotted", warning: true, badge: "proposed", classes: ["proposed"] });
    expect(edgeStyle(fact({ status: "disputed" }))).toMatchObject({ line: "dashed", warning: true, badge: "disputed", classes: ["disputed"] });
  });
  it("keeps a rejected fact visible but muted, and marks a future one", () => {
    expect(edgeStyle(fact({ status: "rejected", evidence_status: "unverified" })).classes).toEqual(["rejected"]);
    expect(edgeStyle(fact({ phase: "future" }))).toMatchObject({ line: "dotted", classes: ["future"] });
  });
  it("never leaves colour as the only signal: every non-current style also changes the line or adds a badge", () => {
    for (const over of [{ phase: "historical" }, { status: "proposed" }, { status: "disputed" }, { status: "rejected" }, { phase: "future" }, { evidence_status: "unverified" }]) {
      const s = edgeStyle(fact(over));
      expect(s.line !== "solid" || s.badge !== null).toBe(true);
    }
  });
});

describe("node styles", () => {
  it("marks candidate and untyped entities and extracted origin", () => {
    expect(nodeStyle({ status: "candidate", origin: "extracted", type: "Person" })).toEqual({ classes: ["type-Person", "candidate", "extracted"], badge: "candidate" });
    expect(nodeStyle({ status: "untyped", origin: "extracted", type: "Entity" }).badge).toBe("untyped");
    expect(nodeStyle({ status: "active", origin: "command", type: "Organization" })).toEqual({ classes: ["type-Organization"], badge: null });
  });
});

describe("caps", () => {
  it("matches the server's caps", () => {
    expect(HOPS_MAX).toBe(2);
    expect(NODES_MAX).toBe(100);
  });
});
