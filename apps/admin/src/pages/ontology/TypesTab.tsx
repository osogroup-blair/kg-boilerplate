import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { btn, Field, inputCls } from "../../components/ui";
import { Empty } from "../../components/states";
import { addEntityType, CLASSIFICATIONS, ENTITY_KEY, issuesAt, removeEntityType, typePath, updateEntityType } from "../../ontology/spec";
import { AttributesEditor } from "./AttributesEditor";
import { useVersionCtx } from "./VersionLayout";

const list = (s: string): string[] => s.split(",").map((x) => x.trim()).filter(Boolean);

export function TypesTab() {
  const { spec, setSpec, issues, readOnly, version } = useVersionCtx();
  const { typeKey } = useParams();
  const navigate = useNavigate();
  const keys = Object.keys(spec.entity_types);
  const selected = typeKey && spec.entity_types[typeKey] ? typeKey : keys[0];
  const type = selected ? spec.entity_types[selected] : undefined;
  const [newKey, setNewKey] = useState("");
  const keyOk = ENTITY_KEY.test(newKey) && !(newKey in spec.entity_types);
  const base = `/ontology/versions/${version.id}/types`;
  const scope = { kind: "entity", key: selected ?? "" } as const;
  const at = (f?: string) => (selected ? typePath(scope, f) : "");

  return (
    <div className="grid gap-6 md:grid-cols-[14rem_1fr]">
      <aside className="space-y-3">
        <h3 className="text-sm font-medium text-slate-500">Entity types</h3>
        <ul className="space-y-1">
          {keys.map((k) => (
            <li key={k}>
              <button type="button" onClick={() => navigate(`${base}/${k}`)} aria-current={k === selected ? "true" : undefined}
                className={`w-full rounded px-2 py-1 text-left font-mono text-sm ${k === selected ? "bg-slate-100 font-medium dark:bg-slate-800" : "hover:bg-slate-50 dark:hover:bg-slate-900"}`}>{k}</button>
            </li>
          ))}
        </ul>
        {!readOnly ? (
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (keyOk) { setSpec((s) => addEntityType(s, newKey)); navigate(`${base}/${newKey}`); setNewKey(""); } }}>
            <input aria-label="New entity type key" className={`${inputCls} w-full font-mono`} placeholder="NewType" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
            <button type="submit" className={btn.secondary} disabled={!keyOk}>Add</button>
          </form>
        ) : null}
      </aside>
      <div className="space-y-5">
        {!type || !selected ? <Empty title="No entity types" hint={readOnly ? undefined : "Add one on the left."} /> : (
          <>
            <div className="flex items-center justify-between">
              <h3 className="font-mono text-lg">{selected}</h3>
              {!readOnly ? <button type="button" className={btn.danger} onClick={() => { setSpec((s) => removeEntityType(s, selected)); navigate(base); }}>Remove type</button> : null}
            </div>
            <Field label="Type description" issues={issuesAt(issues, at("description"))}>
              <input aria-label="Type description" className={`${inputCls} w-full`} disabled={readOnly} value={type.description ?? ""} onChange={(e) => setSpec((s) => updateEntityType(s, selected, { description: e.target.value }))} />
            </Field>
            <div className="flex flex-wrap gap-4">
              <Field label="Identity" issues={issuesAt(issues, at("identity.key"))}>
                <select aria-label="Identity strategy" className={inputCls} disabled={readOnly} value={type.identity?.key ?? "platform_uuid"}
                  onChange={(e) => setSpec((s) => updateEntityType(s, selected, { identity: { ...type.identity, key: e.target.value } }))}>
                  <option value="platform_uuid">platform_uuid (the platform assigns it)</option>
                  <option value="external_id">external_id (a source system's id is required)</option>
                </select>
              </Field>
              <Field label="External ID systems" hint="Comma separated. Only these systems' ids match deterministically; names never do." issues={issuesAt(issues, at("identity.external_id_systems"))} className="min-w-64 flex-1">
                <input aria-label="External ID systems" className={`${inputCls} w-full font-mono`} disabled={readOnly} defaultValue={(type.identity?.external_id_systems ?? []).join(", ")} key={`${selected}-ext`}
                  onBlur={(e) => setSpec((s) => updateEntityType(s, selected, { identity: { ...type.identity, external_id_systems: list(e.target.value) } }))} />
              </Field>
              <Field label="Name aliases from" hint="Attributes whose values count as aliases." issues={issuesAt(issues, at("identity.aliases_from"))} className="min-w-48">
                <input aria-label="Aliases from" className={`${inputCls} w-full font-mono`} disabled={readOnly} defaultValue={(type.identity?.aliases_from ?? []).join(", ")} key={`${selected}-al`}
                  onBlur={(e) => setSpec((s) => updateEntityType(s, selected, { identity: { ...type.identity, aliases_from: list(e.target.value) } }))} />
              </Field>
            </div>
            <Field label="Access" hint="Subject-scoped: this type's entities, and every fact touching one, are visible only to principals granted that subject (admins and reviewers see all). Tightening is a breaking change.">
              <select aria-label="Entity access" className={inputCls} disabled={readOnly} value={type.access ?? "workspace"}
                onChange={(e) => setSpec((s) => updateEntityType(s, selected, { access: e.target.value === "subject_scoped" ? "subject_scoped" : "workspace" }))}>
                <option value="workspace">workspace (every principal sees it)</option>
                <option value="subject_scoped">subject_scoped (only with a grant on the subject)</option>
              </select>
            </Field>
            <fieldset className="space-y-1">
              <legend className="text-sm text-slate-600 dark:text-slate-300">Allowed classifications</legend>
              <div className="flex flex-wrap gap-3">
                {CLASSIFICATIONS.map((c) => (
                  <label key={c} className="flex items-center gap-1 text-sm">
                    <input type="checkbox" disabled={readOnly} checked={(type.allowed_classifications ?? []).includes(c)}
                      onChange={(e) => setSpec((s) => updateEntityType(s, selected, { allowed_classifications: e.target.checked ? [...(type.allowed_classifications ?? []), c] : (type.allowed_classifications ?? []).filter((x) => x !== c) }))} /> {c}
                  </label>
                ))}
              </div>
            </fieldset>
            <AttributesEditor scope={scope} attributes={type.attributes} issues={issues} readOnly={readOnly} setSpec={setSpec} />
          </>
        )}
      </div>
    </div>
  );
}
