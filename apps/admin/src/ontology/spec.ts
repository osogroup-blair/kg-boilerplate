/** The ontology as the designer edits it. Pure functions only: every edit returns a new spec, so the form <-> spec mapping can be tested without React. */

export type AttrType = "string" | "integer" | "number" | "boolean" | "date" | "datetime";
export const ATTR_TYPES: AttrType[] = ["string", "integer", "number", "boolean", "date", "datetime"];
export const CLASSIFICATIONS = ["public", "internal", "confidential", "restricted", "local_only"] as const;
export const CARDINALITIES = ["many_to_many", "one_to_many", "many_to_one", "one_to_one"] as const;

export interface Attribute {
  type: AttrType;
  required?: boolean;
  default?: string | number | boolean | null;
  editable?: boolean;
  sensitive?: boolean;
  sensitivity?: string | null;
  description?: string;
  pattern?: string | null;
  min?: number | null;
  max?: number | null;
  max_length?: number | null;
}
export interface RuleTest {
  name?: string;
  given?: Record<string, unknown>;
  command?: Record<string, unknown>;
  expect: "accept" | "reject" | "queue_review";
}
export interface Rule {
  kind: string;
  id?: string | null;
  params?: Record<string, unknown>;
  severity?: string | null;
  action?: string | null;
  tests?: RuleTest[];
}
export interface EntityType {
  description?: string;
  identity?: { key?: string; external_id_systems?: string[]; aliases_from?: string[] };
  allowed_classifications?: string[];
  attributes: Record<string, Attribute>;
  rules: Rule[];
}
export interface RelationshipType {
  description?: string;
  source: string;
  target: string;
  cardinality?: string;
  temporal?: boolean;
  fact_template?: string | null;
  attributes: Record<string, Attribute>;
  rules: Rule[];
}
export interface Spec {
  ontology: { key: string; version: string; mode: "strict" | "permissive" };
  entity_types: Record<string, EntityType>;
  relationship_types: Record<string, RelationshipType>;
  rules: Rule[];
  context_profiles?: Record<string, unknown>;
  memory_policies?: Record<string, unknown>;
}
export type Scope = { kind: "entity" | "relationship"; key: string };
export interface Issue { code: string; path: string; message: string }

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function normaliseRules(raw: unknown): Rule[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((r) => (typeof r === "string" ? { kind: r } : isObj(r) ? ({ ...r } as unknown as Rule) : { kind: "" }));
}

/** Tolerant loader: an invalid draft is stored as the raw document (blueprint shorthands included), so accept `ontology.id`, `identity.aliases` and bare rule names. */
export function normaliseDoc(raw: unknown): Spec {
  const doc = isObj(raw) ? raw : {};
  const head = isObj(doc.ontology) ? { ...doc.ontology } : {};
  if (head.key === undefined && typeof head.id === "string") head.key = head.id;
  delete head.id;
  const entity_types: Record<string, EntityType> = {};
  for (const [k, v] of Object.entries(isObj(doc.entity_types) ? doc.entity_types : {})) {
    const e = isObj(v) ? v : {};
    const identity = isObj(e.identity) ? { ...e.identity } : undefined;
    if (identity && identity.aliases_from === undefined && Array.isArray(identity.aliases)) identity.aliases_from = identity.aliases;
    if (identity) delete identity.aliases;
    entity_types[k] = { ...(e as object), identity, attributes: isObj(e.attributes) ? (structuredClone(e.attributes) as Record<string, Attribute>) : {}, rules: normaliseRules(e.rules) } as EntityType;
  }
  const relationship_types: Record<string, RelationshipType> = {};
  for (const [k, v] of Object.entries(isObj(doc.relationship_types) ? doc.relationship_types : {})) {
    const r = isObj(v) ? v : {};
    relationship_types[k] = {
      ...(r as object),
      source: typeof r.source === "string" ? r.source : "",
      target: typeof r.target === "string" ? r.target : "",
      attributes: isObj(r.attributes) ? (structuredClone(r.attributes) as Record<string, Attribute>) : {},
      rules: normaliseRules(r.rules),
    } as RelationshipType;
  }
  return {
    ...(doc as object),
    ontology: { key: String(head.key ?? ""), version: String(head.version ?? ""), mode: head.mode === "permissive" ? "permissive" : "strict" },
    entity_types,
    relationship_types,
    rules: normaliseRules(doc.rules),
  } as Spec;
}

export const cloneSpec = (s: Spec): Spec => structuredClone(s);
export const sameSpec = (a: Spec, b: Spec): boolean => JSON.stringify(a) === JSON.stringify(b);

// ---- identifiers (mirrors the server's checks as a hint; the server is authoritative) --------------------------------------------------------
export const ENTITY_KEY = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
export const REL_KEY = /^[A-Z][A-Z0-9_]{0,63}$/;
export const ATTR_KEY = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
export const SEMVER = /^\d{1,4}\.\d{1,5}\.\d{1,6}$/;

// ---- edits ----------------------------------------------------------------------------------------------------------------------------------
export function defaultAttribute(type: AttrType = "string"): Attribute {
  return { type, required: false, editable: true, sensitive: false };
}

export function setHeader(spec: Spec, patch: Partial<Spec["ontology"]>): Spec {
  return { ...spec, ontology: { ...spec.ontology, ...patch } };
}

export function addEntityType(spec: Spec, key: string): Spec {
  return { ...spec, entity_types: { ...spec.entity_types, [key]: { description: "", identity: { key: "platform_uuid", external_id_systems: [], aliases_from: ["name"] }, allowed_classifications: ["internal", "confidential", "restricted"], attributes: { name: { type: "string", required: true } }, rules: [] } } };
}
export function removeEntityType(spec: Spec, key: string): Spec {
  const { [key]: _gone, ...rest } = spec.entity_types;
  void _gone;
  return { ...spec, entity_types: rest };
}
export function updateEntityType(spec: Spec, key: string, patch: Partial<EntityType>): Spec {
  const current = spec.entity_types[key];
  return current ? { ...spec, entity_types: { ...spec.entity_types, [key]: { ...current, ...patch } } } : spec;
}
export function addRelationshipType(spec: Spec, key: string): Spec {
  const [first] = Object.keys(spec.entity_types);
  return { ...spec, relationship_types: { ...spec.relationship_types, [key]: { description: "", source: first ?? "", target: first ?? "", cardinality: "many_to_many", temporal: false, fact_template: "", attributes: {}, rules: [] } } };
}
export function removeRelationshipType(spec: Spec, key: string): Spec {
  const { [key]: _gone, ...rest } = spec.relationship_types;
  void _gone;
  return { ...spec, relationship_types: rest };
}
export function updateRelationshipType(spec: Spec, key: string, patch: Partial<RelationshipType>): Spec {
  const current = spec.relationship_types[key];
  return current ? { ...spec, relationship_types: { ...spec.relationship_types, [key]: { ...current, ...patch } } } : spec;
}

const attributesOf = (spec: Spec, scope: Scope): Record<string, Attribute> =>
  (scope.kind === "entity" ? spec.entity_types[scope.key]?.attributes : spec.relationship_types[scope.key]?.attributes) ?? {};

function withAttributes(spec: Spec, scope: Scope, attributes: Record<string, Attribute>): Spec {
  return scope.kind === "entity" ? updateEntityType(spec, scope.key, { attributes }) : updateRelationshipType(spec, scope.key, { attributes });
}
export function setAttribute(spec: Spec, scope: Scope, name: string, patch: Partial<Attribute>): Spec {
  const current = attributesOf(spec, scope)[name] ?? defaultAttribute();
  const next: Attribute = { ...current, ...patch };
  if (patch.sensitive === false) next.sensitivity = null; // un-flagging also drops the implied restricted classification
  return withAttributes(spec, scope, { ...attributesOf(spec, scope), [name]: next });
}
export function removeAttribute(spec: Spec, scope: Scope, name: string): Spec {
  const { [name]: _gone, ...rest } = attributesOf(spec, scope);
  void _gone;
  return withAttributes(spec, scope, rest);
}
/** Renames in place (key order kept) so the table does not reshuffle while typing. */
export function renameAttribute(spec: Spec, scope: Scope, from: string, to: string): Spec {
  if (from === to) return spec;
  const renamed: Record<string, Attribute> = {};
  for (const [k, v] of Object.entries(attributesOf(spec, scope))) renamed[k === from ? to : k] = v;
  return withAttributes(spec, scope, renamed);
}

// ---- rules ----------------------------------------------------------------------------------------------------------------------------------
export interface JsonSchemaProp { type?: string; enum?: unknown[]; default?: unknown; items?: { enum?: unknown[]; type?: string }; minimum?: number; maximum?: number; description?: string; title?: string }
export interface ParamsSchema { properties?: Record<string, JsonSchemaProp>; required?: string[] }

/** The params a new rule of this kind starts with, derived from the catalog's JSON schema (so a new server-side kind needs no UI code). */
export function ruleDefaults(schema: ParamsSchema | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, p] of Object.entries(schema?.properties ?? {})) {
    if (p.default !== undefined) out[name] = p.default;
    else if ((schema?.required ?? []).includes(name)) {
      if (p.enum?.length) out[name] = p.enum[0];
      else if (p.type === "integer" || p.type === "number") out[name] = p.minimum ?? 1;
      else if (p.type === "boolean") out[name] = false;
      else if (p.type === "array") out[name] = [];
      else out[name] = "";
    }
  }
  return out;
}
export const newRule = (kind: string, schema?: ParamsSchema): Rule => ({ kind, params: ruleDefaults(schema) });

export function rulesAt(spec: Spec, scope: Scope | "workspace"): Rule[] {
  if (scope === "workspace") return spec.rules;
  return (scope.kind === "entity" ? spec.entity_types[scope.key]?.rules : spec.relationship_types[scope.key]?.rules) ?? [];
}
export function setRules(spec: Spec, scope: Scope | "workspace", rules: Rule[]): Spec {
  if (scope === "workspace") return { ...spec, rules };
  return scope.kind === "entity" ? updateEntityType(spec, scope.key, { rules }) : updateRelationshipType(spec, scope.key, { rules });
}
export function replaceRule(rules: Rule[], index: number, patch: Partial<Rule>): Rule[] {
  return rules.map((r, i) => (i === index ? { ...r, ...patch } : r));
}
export const removeRule = (rules: Rule[], index: number): Rule[] => rules.filter((_, i) => i !== index);

/** Reads the value back out of an <input>/<select> string into the schema's type. */
export function coerceParam(prop: JsonSchemaProp | undefined, raw: string): unknown {
  if (prop?.type === "integer") return raw === "" ? "" : Number.parseInt(raw, 10);
  if (prop?.type === "number") return raw === "" ? "" : Number(raw);
  if (prop?.type === "boolean") return raw === "true";
  return raw;
}

// ---- validation issues (the server reports JSON paths; the form anchors them to fields) ------------------------------------------------
export const attrPath = (scope: Scope, name: string, field?: string): string =>
  `${scope.kind === "entity" ? "entity_types" : "relationship_types"}.${scope.key}.attributes.${name}${field ? `.${field}` : ""}`;
export const typePath = (scope: Scope, field?: string): string => `${scope.kind === "entity" ? "entity_types" : "relationship_types"}.${scope.key}${field ? `.${field}` : ""}`;
export const issuesAt = (issues: Issue[], path: string): Issue[] => issues.filter((i) => i.path === path);
export const issuesUnder = (issues: Issue[], prefix: string): Issue[] => issues.filter((i) => i.path === prefix || i.path.startsWith(`${prefix}.`) || i.path.startsWith(`${prefix}[`));

/** Which tab a path belongs to, so the overview can link each error to the form that fixes it. */
export function tabForPath(path: string): "types" | "relationships" | "overview" {
  if (path.startsWith("entity_types")) return "types";
  if (path.startsWith("relationship_types") || path.startsWith("rules")) return "relationships";
  return "overview";
}
export function keyForPath(path: string): string | null {
  const m = /^(?:entity_types|relationship_types)\.([A-Za-z0-9_]+)/.exec(path);
  return m?.[1] ?? null;
}

// ---- versions -------------------------------------------------------------------------------------------------------------------------------
export function nextMinor(versions: Array<string | null | undefined>): string {
  let best: [number, number, number] = [1, 0, 0];
  for (const v of versions) {
    const m = v ? /^(\d+)\.(\d+)\.(\d+)$/.exec(v) : null;
    if (!m) continue;
    const t: [number, number, number] = [Number(m[1]), Number(m[2]), Number(m[3])];
    if (t[0] > best[0] || (t[0] === best[0] && (t[1] > best[1] || (t[1] === best[1] && t[2] > best[2])))) best = t;
  }
  return `${best[0]}.${best[1] + 1}.0`;
}
/** The bundled template carries version 1.0.0; a workspace that already has that version needs the next free minor. */
export function bumpTemplateVersion(yamlText: string, existing: Array<string | null | undefined>): string {
  const taken = new Set(existing);
  return taken.has("1.0.0") ? yamlText.replace(/^(\s*version:\s*)1\.0\.0\s*$/m, `$1${nextMinor([...taken])}`) : yamlText;
}
