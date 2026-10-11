import { describe, expect, it } from "vitest";
import { displayValue, isRedacted, newKey } from "../lib/format";
import {
  addEntityType, addRelationshipType, bumpTemplateVersion, coerceParam, issuesAt, issuesUnder, keyForPath, nextMinor, normaliseDoc, removeAttribute, removeRule,
  renameAttribute, replaceRule, ruleDefaults, sameSpec, setAttribute, setRules, tabForPath, updateRelationshipType, type Spec,
} from "./spec";

const RAW = {
  ontology: { id: "personal-knowledge", version: "1.0.0", mode: "strict" },
  entity_types: {
    Person: { identity: { key: "platform_uuid", aliases: ["name"] }, attributes: { name: { type: "string", required: true } } },
    Organization: { attributes: { name: { type: "string" } } },
  },
  relationship_types: { WORKS_AT: { source: "Person", target: "Organization", temporal: true, rules: ["require_source", { kind: "succession", params: { match: ["source", "target"] } }] } },
};

describe("normaliseDoc: the blueprint's shorthands and an invalid draft's raw document", () => {
  const spec = normaliseDoc(RAW);
  it("maps ontology.id to key and identity.aliases to aliases_from", () => {
    expect(spec.ontology).toEqual({ key: "personal-knowledge", version: "1.0.0", mode: "strict" });
    expect(spec.entity_types.Person?.identity).toEqual({ key: "platform_uuid", aliases_from: ["name"] });
  });
  it("expands bare rule names", () => {
    expect(spec.relationship_types.WORKS_AT?.rules.map((r) => r.kind)).toEqual(["require_source", "succession"]);
  });
  it("fills missing containers instead of throwing", () => {
    const empty = normaliseDoc({});
    expect(empty).toMatchObject({ entity_types: {}, relationship_types: {}, rules: [], ontology: { key: "", version: "", mode: "strict" } });
    expect(normaliseDoc(null).entity_types).toEqual({});
    expect(normaliseDoc({ entity_types: { A: null }, relationship_types: { B: "x" } }).relationship_types.B).toMatchObject({ source: "", target: "", attributes: {}, rules: [] });
  });
  it("does not alias the input", () => {
    const raw = structuredClone(RAW);
    const out = normaliseDoc(raw);
    out.entity_types.Person!.attributes.name!.required = false;
    expect(raw.entity_types.Person.attributes.name.required).toBe(true);
  });
});

describe("form edits are pure and map to the spec", () => {
  const base = normaliseDoc(RAW);
  const person = { kind: "entity", key: "Person" } as const;

  it("adds an attribute with sensible defaults and leaves the original untouched", () => {
    const next = setAttribute(base, person, "nickname", { type: "string", max_length: 20 });
    expect(next.entity_types.Person?.attributes.nickname).toEqual({ type: "string", required: false, editable: true, sensitive: false, max_length: 20 });
    expect(base.entity_types.Person?.attributes.nickname).toBeUndefined();
  });
  it("edits one field of an attribute", () => {
    const next = setAttribute(setAttribute(base, person, "nickname", { type: "string" }), person, "nickname", { required: true });
    expect(next.entity_types.Person?.attributes.nickname).toMatchObject({ type: "string", required: true });
  });
  it("un-flagging sensitive clears the implied classification", () => {
    const flagged = setAttribute(base, person, "birth_date", { type: "date", sensitive: true, sensitivity: "restricted" });
    expect(setAttribute(flagged, person, "birth_date", { sensitive: false }).entity_types.Person?.attributes.birth_date).toMatchObject({ sensitive: false, sensitivity: null });
  });
  it("renames in place keeping the order, and removes", () => {
    const withTwo = setAttribute(setAttribute(base, person, "a", { type: "string" }), person, "b", { type: "string" });
    const renamed = renameAttribute(withTwo, person, "a", "alpha");
    expect(Object.keys(renamed.entity_types.Person!.attributes)).toEqual(["name", "alpha", "b"]);
    expect(Object.keys(removeAttribute(renamed, person, "alpha").entity_types.Person!.attributes)).toEqual(["name", "b"]);
    expect(renameAttribute(withTwo, person, "a", "a")).toBe(withTwo);
  });
  it("edits relationship attributes through the same functions", () => {
    const rel = { kind: "relationship", key: "WORKS_AT" } as const;
    const next = setAttribute(base, rel, "role", { type: "string" });
    expect(next.relationship_types.WORKS_AT?.attributes.role?.type).toBe("string");
  });
  it("adds a type pre-filled so it validates, and a relationship pointing at an existing type", () => {
    const next = addRelationshipType(addEntityType(base, "Place"), "LIVES_IN");
    expect(next.entity_types.Place?.attributes.name).toMatchObject({ type: "string", required: true });
    expect(next.relationship_types.LIVES_IN).toMatchObject({ source: "Person", cardinality: "many_to_many", temporal: false });
    expect(updateRelationshipType(next, "LIVES_IN", { target: "Place" }).relationship_types.LIVES_IN?.target).toBe("Place");
  });
  it("detects a no-op", () => {
    expect(sameSpec(base, structuredClone(base))).toBe(true);
    expect(sameSpec(base, setAttribute(base, person, "x", {}))).toBe(false);
  });
});

describe("rules are driven by the catalog's JSON schema", () => {
  const maxActive = { properties: { n: { type: "integer", minimum: 1 }, match: { type: "array", items: { enum: ["source", "target"] }, default: ["source"] } }, required: ["n"] };
  it("derives defaults: required ints start at their minimum, declared defaults are kept", () => {
    expect(ruleDefaults(maxActive)).toEqual({ n: 1, match: ["source"] });
    expect(ruleDefaults({ properties: { level: { enum: ["a", "b"] } }, required: ["level"] })).toEqual({ level: "a" });
    expect(ruleDefaults(undefined)).toEqual({});
  });
  it("replaces, removes and sets rules on a relationship", () => {
    const base = normaliseDoc(RAW);
    const rules = base.relationship_types.WORKS_AT!.rules;
    const edited = replaceRule(rules, 0, { action: "queue_review" });
    expect(edited[0]).toMatchObject({ kind: "require_source", action: "queue_review" });
    expect(removeRule(edited, 0).map((r) => r.kind)).toEqual(["succession"]);
    const next: Spec = setRules(base, { kind: "relationship", key: "WORKS_AT" }, removeRule(edited, 1));
    expect(next.relationship_types.WORKS_AT?.rules).toHaveLength(1);
    expect(setRules(base, "workspace", [{ kind: "require_source" }]).rules).toHaveLength(1);
  });
  it("coerces form strings to the param type", () => {
    expect(coerceParam({ type: "integer" }, "3")).toBe(3);
    expect(coerceParam({ type: "integer" }, "")).toBe("");
    expect(coerceParam({ type: "boolean" }, "true")).toBe(true);
    expect(coerceParam({ type: "string" }, "x")).toBe("x");
  });
});

describe("validation errors are anchored to the field that fixes them", () => {
  const issues = [
    { code: "unknown_entity_type", path: "relationship_types.WORKS_AT.target", message: "m" },
    { code: "reserved_graphiti_field", path: "entity_types.Person.attributes.labels", message: "m" },
    { code: "invalid_default", path: "entity_types.Person.attributes.age.default", message: "m" },
    { code: "param.greater_than_equal", path: "relationship_types.WORKS_AT.rules[1].params.n", message: "m" },
  ];
  it("matches exact paths and prefixes", () => {
    expect(issuesAt(issues, "relationship_types.WORKS_AT.target")).toHaveLength(1);
    expect(issuesAt(issues, "relationship_types.WORKS_AT")).toHaveLength(0);
    expect(issuesUnder(issues, "entity_types.Person.attributes.age")).toHaveLength(1);
    expect(issuesUnder(issues, "entity_types.Person")).toHaveLength(2);
    expect(issuesUnder(issues, "relationship_types.WORKS_AT.rules[1]")).toHaveLength(1);
    expect(issuesUnder(issues, "entity_types.Per")).toHaveLength(0); // a prefix must end at a path boundary
  });
  it("names the tab and the type for a path", () => {
    expect(tabForPath("entity_types.Person.attributes.labels")).toBe("types");
    expect(tabForPath("relationship_types.WORKS_AT.rules[0]")).toBe("relationships");
    expect(tabForPath("rules[0].kind")).toBe("relationships");
    expect(tabForPath("ontology.version")).toBe("overview");
    expect(keyForPath("entity_types.Person.attributes.labels")).toBe("Person");
    expect(keyForPath("rules[0]")).toBeNull();
  });
});

describe("versions", () => {
  it("picks the next free minor", () => {
    expect(nextMinor([])).toBe("1.1.0");
    expect(nextMinor(["1.0.0", "1.1.0", null, "garbage"])).toBe("1.2.0");
    expect(nextMinor(["2.3.4"])).toBe("2.4.0");
  });
  it("bumps the bundled template only when its version is taken", () => {
    const yaml = "ontology:\n  id: personal-knowledge\n  version: 1.0.0\n  mode: strict\n";
    expect(bumpTemplateVersion(yaml, [])).toBe(yaml);
    expect(bumpTemplateVersion(yaml, ["1.0.0", "1.1.0"])).toContain("version: 1.2.0");
  });
});

describe("redaction display", () => {
  it("shows a label for a redacted value, never the object", () => {
    expect(isRedacted({ redacted: true })).toBe(true);
    expect(isRedacted({ redacted: false })).toBe(false);
    expect(displayValue({ redacted: true })).toBe("redacted");
    expect(displayValue("1985-03-02")).toBe("1985-03-02");
    expect(displayValue(null)).toBe("—");
    expect(displayValue(0)).toBe("0");
  });
  it("makes unique idempotency keys", () => {
    expect(newKey()).not.toBe(newKey());
  });
});
