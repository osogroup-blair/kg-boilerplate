import { useState } from "react";
import type { components } from "../../api/schema";
import { Badge, btn, Field, FieldError, inputCls } from "../../components/ui";
import { coerceParam, issuesAt, issuesUnder, newRule, removeRule, replaceRule, type Issue, type JsonSchemaProp, type ParamsSchema, type Rule, type RuleTest } from "../../ontology/spec";

type RuleKind = components["schemas"]["RuleKindOut"];
export type RuleScope = "entity" | "relationship" | "workspace";

interface Props {
  rules: Rule[];
  onChange: (rules: Rule[]) => void;
  basePath: string;
  scope: RuleScope;
  catalog: RuleKind[];
  issues: Issue[];
  readOnly: boolean;
}

/** A JSON object edited as text: applied on blur, and a syntax error stays next to the field instead of corrupting the spec. */
function JsonField({ label, value, onCommit, disabled }: { label: string; value: Record<string, unknown> | undefined; onCommit: (v: Record<string, unknown>) => void; disabled: boolean }) {
  const [text, setText] = useState(JSON.stringify(value ?? {}, null, 1));
  const [bad, setBad] = useState(false);
  return (
    <label className="block text-xs">
      <span className="mb-1 block text-slate-500">{label}</span>
      <textarea aria-label={label} rows={4} disabled={disabled} className={`${inputCls} w-full font-mono ${bad ? "border-red-500" : ""}`} value={text} onChange={(e) => setText(e.target.value)}
        onBlur={() => { try { const parsed: unknown = JSON.parse(text || "{}"); if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) { setBad(false); onCommit(parsed as Record<string, unknown>); } else setBad(true); } catch { setBad(true); } }} />
      {bad ? <span role="alert" className="text-red-700">Not a valid JSON object; the last good value is kept.</span> : null}
    </label>
  );
}

function ParamInput({ name, prop, value, onChange, disabled }: { name: string; prop: JsonSchemaProp; value: unknown; onChange: (v: unknown) => void; disabled: boolean }) {
  const label = prop.title ?? name;
  if (prop.type === "array" && prop.items?.enum) {
    const chosen = Array.isArray(value) ? (value as string[]) : [];
    return (
      <fieldset className="text-sm" title={prop.description}>
        <legend className="mb-1 text-slate-600 dark:text-slate-300">{label}</legend>
        <div className="flex gap-3">
          {prop.items.enum.map((opt) => (
            <label key={String(opt)} className="flex items-center gap-1"><input type="checkbox" disabled={disabled} checked={chosen.includes(String(opt))}
              onChange={(e) => onChange(e.target.checked ? [...chosen, String(opt)] : chosen.filter((x) => x !== String(opt)))} /> {String(opt)}</label>
          ))}
        </div>
      </fieldset>
    );
  }
  if (prop.enum) {
    return (
      <Field label={label}>
        <select aria-label={label} className={inputCls} disabled={disabled} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
          {prop.enum.map((o) => <option key={String(o)}>{String(o)}</option>)}
        </select>
      </Field>
    );
  }
  if (prop.type === "boolean") {
    return (
      <label className="flex items-center gap-1 pb-1 text-sm" title={prop.description}>
        <input type="checkbox" disabled={disabled} checked={value === true} onChange={(e) => onChange(e.target.checked)} /> {label}
      </label>
    );
  }
  return (
    <Field label={label} hint={prop.description}>
      <input aria-label={label} type={prop.type === "integer" || prop.type === "number" ? "number" : "text"} min={prop.minimum} max={prop.maximum} className={`${inputCls} w-28`} disabled={disabled}
        value={value === undefined || value === null ? "" : String(value)} onChange={(e) => onChange(coerceParam(prop, e.target.value))} />
    </Field>
  );
}

function TestsEditor({ tests, onChange, disabled }: { tests: RuleTest[]; onChange: (t: RuleTest[]) => void; disabled: boolean }) {
  return (
    <details className="rounded border border-slate-200 p-2 text-sm dark:border-slate-800">
      <summary className="cursor-pointer">Rule tests ({tests.length})</summary>
      <p className="mt-1 text-xs text-slate-500">Scenarios that run when the ontology is validated; publishing is blocked if one fails. <code>given.facts</code> lists active facts; <code>command</code> is what is asserted.</p>
      <ul className="mt-2 space-y-3">
        {tests.map((t, i) => (
          <li key={i} className="space-y-2 border-t border-slate-200 pt-2 dark:border-slate-800">
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Test name" className="min-w-56 flex-1"><input aria-label="Test name" className={`${inputCls} w-full`} disabled={disabled} value={t.name ?? ""} onChange={(e) => onChange(tests.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} /></Field>
              <Field label="Expect"><select aria-label="Expected outcome" className={inputCls} disabled={disabled} value={t.expect} onChange={(e) => onChange(tests.map((x, j) => (j === i ? { ...x, expect: e.target.value as RuleTest["expect"] } : x)))}><option>accept</option><option>reject</option><option>queue_review</option></select></Field>
              {!disabled ? <button type="button" className={btn.danger} onClick={() => onChange(tests.filter((_, j) => j !== i))}>Remove test</button> : null}
            </div>
            <div className="grid gap-2 md:grid-cols-2">
              <JsonField label="Given" value={t.given} disabled={disabled} onCommit={(v) => onChange(tests.map((x, j) => (j === i ? { ...x, given: v } : x)))} />
              <JsonField label="Command" value={t.command} disabled={disabled} onCommit={(v) => onChange(tests.map((x, j) => (j === i ? { ...x, command: v } : x)))} />
            </div>
          </li>
        ))}
      </ul>
      {!disabled ? <button type="button" className={`${btn.secondary} mt-2`} onClick={() => onChange([...tests, { name: "", given: {}, command: {}, expect: "accept" }])}>Add test</button> : null}
    </details>
  );
}

export function RuleEditor({ rules, onChange, basePath, scope, catalog, issues, readOnly }: Props) {
  const declarable = catalog.filter((k) => !k.implicit && k.applies_to.includes(scope));
  const [adding, setAdding] = useState("");
  const byKind = new Map(catalog.map((k) => [k.kind, k]));
  return (
    <section aria-label="Rules" className="space-y-3">
      <h4 className="text-sm font-medium">{scope === "workspace" ? "Rules for every type" : "Rules"}</h4>
      {rules.length === 0 ? <p className="text-sm text-slate-500">No rules. The built-in checks (types, endpoints, attribute types, valid interval, classification) always apply.</p> : null}
      <ul className="space-y-3">
        {rules.map((rule, i) => {
          const kind = byKind.get(rule.kind);
          const path = `${basePath}[${i}]`;
          const schema = (kind?.params_schema ?? {}) as ParamsSchema;
          return (
            <li key={i} className="space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-800" data-testid={`rule-${i}`}>
              <div className="flex flex-wrap items-end gap-3">
                <Field label="Rule" issues={issuesAt(issues, `${path}.kind`)}>
                  <select aria-label={`Rule ${i + 1} kind`} className={inputCls} disabled={readOnly} value={rule.kind}
                    onChange={(e) => { const k = byKind.get(e.target.value); onChange(replaceRule(rules, i, { kind: e.target.value, params: newRule(e.target.value, k?.params_schema as ParamsSchema).params, action: null })); }}>
                    {!kind ? <option value={rule.kind}>{rule.kind || "(choose)"}</option> : null}
                    {declarable.map((k) => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
                  </select>
                </Field>
                {Object.entries(schema.properties ?? {}).map(([name, prop]) => (
                  <div key={name}>
                    <ParamInput name={name} prop={prop} disabled={readOnly} value={rule.params?.[name]} onChange={(v) => onChange(replaceRule(rules, i, { params: { ...rule.params, [name]: v } }))} />
                    <FieldError issues={issuesAt(issues, `${path}.params.${name}`)} />
                  </div>
                ))}
                {kind ? (
                  <Field label="Action" issues={issuesAt(issues, `${path}.action`)}>
                    <select aria-label={`Rule ${i + 1} action`} className={inputCls} disabled={readOnly} value={rule.action ?? ""} onChange={(e) => onChange(replaceRule(rules, i, { action: e.target.value || null }))}>
                      <option value="">default ({kind.default_action})</option>
                      {kind.allowed_actions.map((a) => <option key={a}>{a}</option>)}
                    </select>
                  </Field>
                ) : null}
                {!readOnly ? <button type="button" className={`${btn.danger} mb-0.5`} aria-label={`Remove rule ${i + 1}`} onClick={() => onChange(removeRule(rules, i))}>Remove</button> : null}
              </div>
              {kind ? (
                <details className="text-xs text-slate-600 dark:text-slate-400">
                  <summary className="cursor-pointer"><Badge tone="slate">{kind.family}</Badge> {kind.description}</summary>
                  <p className="mt-1"><strong>Conflict policy:</strong> {kind.conflict_policy}</p>
                </details>
              ) : null}
              <TestsEditor tests={rule.tests ?? []} disabled={readOnly} onChange={(tests) => onChange(replaceRule(rules, i, { tests }))} />
              <FieldError issues={issuesUnder(issues, path).filter((x) => !x.path.startsWith(`${path}.params.`) && x.path !== `${path}.kind` && x.path !== `${path}.action`)} />
            </li>
          );
        })}
      </ul>
      {!readOnly ? (
        <div className="flex items-end gap-2">
          <Field label={scope === "workspace" ? "Add a workspace-wide rule" : "Add a rule"}>
            <select aria-label={scope === "workspace" ? "Add a workspace-wide rule" : "Add a rule"} className={inputCls} value={adding} onChange={(e) => setAdding(e.target.value)}>
              <option value="">Choose a rule…</option>
              {declarable.map((k) => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
            </select>
          </Field>
          <button type="button" className={btn.secondary} disabled={!adding} onClick={() => { onChange([...rules, newRule(adding, byKind.get(adding)?.params_schema as ParamsSchema)]); setAdding(""); }}>{scope === "workspace" ? "Add workspace rule" : "Add rule"}</button>
        </div>
      ) : null}
    </section>
  );
}
