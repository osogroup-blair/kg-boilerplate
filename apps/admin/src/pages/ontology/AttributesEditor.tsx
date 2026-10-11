import { useState } from "react";
import { btn, Field, FieldError, inputCls } from "../../components/ui";
import {
  ATTR_KEY, ATTR_TYPES, attrPath, issuesAt, issuesUnder, removeAttribute, renameAttribute, setAttribute, type Attribute, type AttrType, type Issue, type Scope, type Spec,
} from "../../ontology/spec";

interface Props {
  scope: Scope;
  attributes: Record<string, Attribute>;
  issues: Issue[];
  readOnly: boolean;
  setSpec: (fn: (s: Spec) => Spec) => void;
}

const KNOWN = ["type", "default", "pattern", "min", "max", "max_length", "required", "editable", "sensitive", "description"];
const num = (v: string): number | null => (v === "" ? null : Number(v));

function AttributeRow({ scope, name, attr, issues, readOnly, setSpec }: Props & { name: string; attr: Attribute }) {
  const path = (f?: string) => attrPath(scope, name, f);
  const isName = scope.kind === "entity" && name === "name"; // the node's own name: always present, required, never sensitive
  const own = issuesUnder(issues, path());
  const anchored = own.filter((i) => !KNOWN.some((f) => i.path === path(f)));
  return (
    <li className="space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-800" data-testid={`attr-${name}`}>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Name" className="w-44">
          <input aria-label={`Attribute name ${name}`} className={`${inputCls} w-full font-mono`} defaultValue={name} disabled={readOnly || isName}
            onBlur={(e) => { const to = e.target.value.trim(); if (to && to !== name && ATTR_KEY.test(to)) setSpec((s) => renameAttribute(s, scope, name, to)); else e.target.value = name; }} />
        </Field>
        <Field label="Type" issues={issuesAt(issues, path("type"))}>
          <select aria-label={`Type of ${name}`} className={inputCls} value={attr.type} disabled={readOnly} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { type: e.target.value as AttrType }))}>
            {ATTR_TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </Field>
        <label className="flex items-center gap-1 pb-1 text-sm"><input type="checkbox" checked={!!attr.required} disabled={readOnly || isName} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { required: e.target.checked }))} /> Required</label>
        <label className="flex items-center gap-1 pb-1 text-sm"><input type="checkbox" checked={attr.editable !== false} disabled={readOnly} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { editable: e.target.checked }))} /> Editable</label>
        <label className="flex items-center gap-1 pb-1 text-sm" title="Sensitive values stay in Postgres: never in the graph, fact text, embeddings or logs">
          <input type="checkbox" checked={!!attr.sensitive} disabled={readOnly || isName} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { sensitive: e.target.checked, ...(e.target.checked ? { sensitivity: "restricted" } : {}) }))} /> Sensitive
        </label>
        {!readOnly && !isName ? <button type="button" className={`${btn.danger} mb-0.5`} aria-label={`Remove attribute ${name}`} onClick={() => setSpec((s) => removeAttribute(s, scope, name))}>Remove</button> : null}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Default" issues={issuesAt(issues, path("default"))} className="w-36">
          <input aria-label={`Default of ${name}`} className={`${inputCls} w-full`} disabled={readOnly} value={attr.default === null || attr.default === undefined ? "" : String(attr.default)}
            onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { default: e.target.value === "" ? null : attr.type === "integer" || attr.type === "number" ? Number(e.target.value) : attr.type === "boolean" ? e.target.value === "true" : e.target.value }))} />
        </Field>
        {attr.type === "string" ? (
          <>
            <Field label="Max length" issues={issuesAt(issues, path("max_length"))} className="w-28">
              <input type="number" min={1} aria-label={`Max length of ${name}`} className={`${inputCls} w-full`} disabled={readOnly} value={attr.max_length ?? ""} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { max_length: num(e.target.value) }))} />
            </Field>
            <Field label="Pattern" issues={issuesAt(issues, path("pattern"))} className="w-48">
              <input aria-label={`Pattern of ${name}`} className={`${inputCls} w-full font-mono`} disabled={readOnly} value={attr.pattern ?? ""} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { pattern: e.target.value || null }))} />
            </Field>
          </>
        ) : null}
        {attr.type === "integer" || attr.type === "number" ? (
          <>
            <Field label="Min" issues={issuesAt(issues, path("min"))} className="w-24">
              <input type="number" aria-label={`Min of ${name}`} className={`${inputCls} w-full`} disabled={readOnly} value={attr.min ?? ""} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { min: num(e.target.value) }))} />
            </Field>
            <Field label="Max" issues={issuesAt(issues, path("max"))} className="w-24">
              <input type="number" aria-label={`Max of ${name}`} className={`${inputCls} w-full`} disabled={readOnly} value={attr.max ?? ""} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { max: num(e.target.value) }))} />
            </Field>
          </>
        ) : null}
        <Field label="Description" className="min-w-48 flex-1">
          <input aria-label={`Description of ${name}`} className={`${inputCls} w-full`} disabled={readOnly} value={attr.description ?? ""} onChange={(e) => setSpec((s) => setAttribute(s, scope, name, { description: e.target.value }))} />
        </Field>
      </div>
      <FieldError issues={anchored} />
    </li>
  );
}

export function AttributesEditor(props: Props) {
  const { attributes, readOnly, scope, setSpec } = props;
  const [name, setName] = useState("");
  const [type, setType] = useState<AttrType>("string");
  const valid = ATTR_KEY.test(name) && !(name in attributes);
  const entries = Object.entries(attributes);
  return (
    <section aria-label="Attributes" className="space-y-3">
      <h4 className="text-sm font-medium">Attributes</h4>
      {entries.length === 0 ? <p className="text-sm text-slate-500">No attributes.</p> : (
        <ul className="space-y-2">{entries.map(([n, a]) => <AttributeRow key={n} {...props} name={n} attr={a} />)}</ul>
      )}
      {!readOnly ? (
        <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); if (valid) { setSpec((s) => setAttribute(s, scope, name, { type })); setName(""); } }}>
          <Field label="New attribute name" className="w-52">
            <input className={`${inputCls} w-full font-mono`} value={name} placeholder="e.g. nickname" onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="New attribute type">
            <select className={inputCls} value={type} onChange={(e) => setType(e.target.value as AttrType)}>{ATTR_TYPES.map((t) => <option key={t}>{t}</option>)}</select>
          </Field>
          <button type="submit" className={btn.secondary} disabled={!valid}>Add attribute</button>
          {name && !valid ? <span className="pb-1 text-xs text-amber-700">{name in attributes ? "That name is taken." : "Start with a letter; letters, digits and underscores only."}</span> : null}
        </form>
      ) : null}
    </section>
  );
}
