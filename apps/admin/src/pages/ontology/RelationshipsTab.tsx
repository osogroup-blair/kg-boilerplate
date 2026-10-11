import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { btn, Field, inputCls } from "../../components/ui";
import { Empty, ErrorState, Loading } from "../../components/states";
import { useRuleCatalog } from "../../ontology/queries";
import { addRelationshipType, CARDINALITIES, issuesAt, REL_KEY, removeRelationshipType, rulesAt, setRules, typePath, updateRelationshipType } from "../../ontology/spec";
import { AttributesEditor } from "./AttributesEditor";
import { RuleEditor } from "./RuleEditor";
import { useVersionCtx } from "./VersionLayout";

export function RelationshipsTab() {
  const { spec, setSpec, issues, readOnly, version } = useVersionCtx();
  const catalog = useRuleCatalog();
  const { relKey } = useParams();
  const navigate = useNavigate();
  const keys = Object.keys(spec.relationship_types);
  const selected = relKey && spec.relationship_types[relKey] ? relKey : keys[0];
  const rel = selected ? spec.relationship_types[selected] : undefined;
  const [newKey, setNewKey] = useState("");
  const keyOk = REL_KEY.test(newKey) && !(newKey in spec.relationship_types) && Object.keys(spec.entity_types).length > 0;
  const base = `/ontology/versions/${version.id}/relationships`;
  const scope = { kind: "relationship", key: selected ?? "" } as const;
  const at = (f?: string) => (selected ? typePath(scope, f) : "");
  const entityKeys = Object.keys(spec.entity_types);

  if (catalog.isLoading) return <Loading label="Loading the rule catalog…" />;
  if (catalog.isError || !catalog.data?.data) return <ErrorState onRetry={() => void catalog.refetch()} />;
  const kinds = catalog.data.data.kinds;

  return (
    <div className="space-y-8">
      <div className="grid gap-6 md:grid-cols-[14rem_1fr]">
        <aside className="space-y-3">
          <h3 className="text-sm font-medium text-slate-500">Relationship types</h3>
          <ul className="space-y-1">
            {keys.map((k) => (
              <li key={k}>
                <button type="button" onClick={() => navigate(`${base}/${k}`)} aria-current={k === selected ? "true" : undefined}
                  className={`w-full rounded px-2 py-1 text-left font-mono text-sm ${k === selected ? "bg-slate-100 font-medium dark:bg-slate-800" : "hover:bg-slate-50 dark:hover:bg-slate-900"}`}>{k}</button>
              </li>
            ))}
          </ul>
          {!readOnly ? (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (keyOk) { setSpec((s) => addRelationshipType(s, newKey)); navigate(`${base}/${newKey}`); setNewKey(""); } }}>
              <input aria-label="New relationship type key" className={`${inputCls} w-full font-mono`} placeholder="NEW_TYPE" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
              <button type="submit" className={btn.secondary} disabled={!keyOk}>Add</button>
            </form>
          ) : null}
        </aside>
        <div className="space-y-5">
          {!rel || !selected ? <Empty title="No relationship types" hint={readOnly ? undefined : "Add one on the left (it needs at least two entity types)."} /> : (
            <>
              <div className="flex items-center justify-between">
                <h3 className="font-mono text-lg">{selected}</h3>
                {!readOnly ? <button type="button" className={btn.danger} onClick={() => { setSpec((s) => removeRelationshipType(s, selected)); navigate(base); }}>Remove type</button> : null}
              </div>
              <Field label="Type description" issues={issuesAt(issues, at("description"))}>
                <input aria-label="Type description" className={`${inputCls} w-full`} disabled={readOnly} value={rel.description ?? ""} onChange={(e) => setSpec((s) => updateRelationshipType(s, selected, { description: e.target.value }))} />
              </Field>
              <div className="flex flex-wrap items-start gap-4">
                <Field label="Source type" issues={issuesAt(issues, at("source"))}>
                  <select aria-label="Source type" className={inputCls} disabled={readOnly} value={rel.source} onChange={(e) => setSpec((s) => updateRelationshipType(s, selected, { source: e.target.value }))}>
                    {!entityKeys.includes(rel.source) ? <option value={rel.source}>{rel.source || "(choose)"}</option> : null}
                    {entityKeys.map((k) => <option key={k}>{k}</option>)}
                  </select>
                </Field>
                <Field label="Target type" issues={issuesAt(issues, at("target"))}>
                  <select aria-label="Target type" className={inputCls} disabled={readOnly} value={rel.target} onChange={(e) => setSpec((s) => updateRelationshipType(s, selected, { target: e.target.value }))}>
                    {!entityKeys.includes(rel.target) ? <option value={rel.target}>{rel.target || "(choose)"}</option> : null}
                    {entityKeys.map((k) => <option key={k}>{k}</option>)}
                  </select>
                </Field>
                <Field label="Cardinality" hint="Informative: enforced only through rules." issues={issuesAt(issues, at("cardinality"))}>
                  <select aria-label="Cardinality" className={inputCls} disabled={readOnly} value={rel.cardinality ?? "many_to_many"} onChange={(e) => setSpec((s) => updateRelationshipType(s, selected, { cardinality: e.target.value }))}>
                    {CARDINALITIES.map((c) => <option key={c}>{c}</option>)}
                  </select>
                </Field>
                <label className="flex items-center gap-1 pt-6 text-sm"><input type="checkbox" disabled={readOnly} checked={!!rel.temporal} onChange={(e) => setSpec((s) => updateRelationshipType(s, selected, { temporal: e.target.checked }))} /> Temporal (has valid time)</label>
              </div>
              <Field label="Fact template" hint="Deterministic graph text, no LLM. {source}, {target}, {attribute}, and {attribute?: optional text with {attribute}}." issues={issuesAt(issues, at("fact_template"))}>
                <input aria-label="Fact template" className={`${inputCls} w-full font-mono`} disabled={readOnly} value={rel.fact_template ?? ""} onChange={(e) => setSpec((s) => updateRelationshipType(s, selected, { fact_template: e.target.value || null }))} />
              </Field>
              <AttributesEditor scope={scope} attributes={rel.attributes} issues={issues} readOnly={readOnly} setSpec={setSpec} />
              <RuleEditor rules={rulesAt(spec, scope)} onChange={(r) => setSpec((s) => setRules(s, scope, r))} basePath={`relationship_types.${selected}.rules`} scope="relationship" catalog={kinds} issues={issues} readOnly={readOnly} />
            </>
          )}
        </div>
      </div>
      <div className="border-t border-slate-200 pt-6 dark:border-slate-800">
        <h3 className="mb-3 font-medium">Workspace-wide rules</h3>
        <RuleEditor rules={spec.rules} onChange={(r) => setSpec((s) => setRules(s, "workspace", r))} basePath="rules" scope="workspace" catalog={kinds} issues={issues} readOnly={readOnly} />
      </div>
    </div>
  );
}
