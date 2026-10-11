import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Field, inputCls } from "../../components/ui";
import { newKey } from "../../lib/format";
import { useActiveOntology, useVersion } from "../../ontology/queries";
import { normaliseDoc } from "../../ontology/spec";
import { readCommandResponse, ResultBanner, type CommandResult } from "./commandUi";

const asValue = (type: string, raw: string): string | number | boolean => (type === "integer" ? Number.parseInt(raw, 10) : type === "number" ? Number(raw) : type === "boolean" ? raw === "true" : raw);

export function AssertRelationshipDialog({ entity, onClose }: { entity: { id: string; type: string; name: string }; onClose: () => void }) {
  const qc = useQueryClient();
  const active = useActiveOntology();
  const activeId = active.data?.data?.version?.id ?? "";
  const version = useVersion(activeId);
  const spec = version.data?.data ? normaliseDoc(version.data.data.spec) : null;
  const options = (active.data?.data?.relationship_types ?? []).filter((r) => r.source === entity.type);
  const [relType, setRelType] = useState("");
  const chosen = relType || options[0]?.type || "";
  const rel = options.find((r) => r.type === chosen);
  const relSpec = spec?.relationship_types[chosen];
  const targets = useQuery({
    enabled: !!rel,
    queryKey: ["entities", "picker", rel?.target],
    queryFn: () => api.GET("/v1/entities", { params: { query: { type: rel?.target, limit: 100 } } }),
  });
  const [target, setTarget] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [attrs, setAttrs] = useState<Record<string, string>>({});
  const [sourceSystem, setSourceSystem] = useState("admin_console");
  const [sourceRef, setSourceRef] = useState("");
  const [result, setResult] = useState<CommandResult | null>(null);
  const choices = targets.data?.data?.items ?? [];

  return (
    <ConfirmDialog title={`Add a relationship from ${entity.name}`} confirmLabel="Assert relationship" cancelLabel={result ? "Close" : "Cancel"} closeOnSuccess={false} onClose={onClose}
      disabled={!rel || !(target || choices[0])}
      onConfirm={async () => {
        const attributes: Record<string, unknown> = {};
        for (const [name, a] of Object.entries(relSpec?.attributes ?? {})) if (attrs[name]) attributes[name] = asValue(a.type, attrs[name]!);
        const res = await api.POST("/v1/relationships", {
          params: { header: { "Idempotency-Key": newKey() } },
          body: {
            type: chosen, source: { entity_id: entity.id }, target: { entity_id: target || choices[0]!.id }, attributes,
            classification: "internal", valid_from_unknown: false, valid_from: from || null, valid_to: to || null, source_system: sourceSystem || null, source_ref: sourceRef || null,
          },
        });
        setResult(readCommandResponse(res.response.status, res.data, res.error));
        await Promise.all([qc.invalidateQueries({ queryKey: ["entity"] }), qc.invalidateQueries({ queryKey: ["commands"] })]);
        return null;
      }}>
      {options.length === 0 ? <p className="text-sm text-slate-500">The active ontology has no relationship type that starts at a {entity.type}.</p> : (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Relationship"><select aria-label="Relationship type" className={`${inputCls} w-full`} value={chosen} onChange={(e) => { setRelType(e.target.value); setTarget(""); }}>{options.map((o) => <option key={o.type} value={o.type}>{o.type} → {o.target}</option>)}</select></Field>
          <Field label={`Target (${rel?.target ?? ""})`}>
            <select aria-label="Target entity" className={`${inputCls} w-full`} value={target || choices[0]?.id || ""} onChange={(e) => setTarget(e.target.value)}>{choices.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
          </Field>
          <Field label="Valid from" hint="Never defaulted to now."><input aria-label="Valid from" type="date" className={`${inputCls} w-full`} value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="Valid to (optional)"><input aria-label="Valid to" type="date" className={`${inputCls} w-full`} value={to} onChange={(e) => setTo(e.target.value)} /></Field>
          {Object.entries(relSpec?.attributes ?? {}).map(([name, a]) => (
            <Field key={name} label={name}><input aria-label={`Attribute ${name}`} className={`${inputCls} w-full`} value={attrs[name] ?? ""} placeholder={a.type} onChange={(e) => setAttrs({ ...attrs, [name]: e.target.value })} /></Field>
          ))}
          <Field label="Source system"><input aria-label="Source system" className={`${inputCls} w-full`} value={sourceSystem} onChange={(e) => setSourceSystem(e.target.value)} /></Field>
          <Field label="Source reference"><input aria-label="Source reference" className={`${inputCls} w-full`} value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} /></Field>
        </div>
      )}
      {result ? <ResultBanner result={result} /> : null}
    </ConfirmDialog>
  );
}

export function EndRelationshipDialog({ factId, onClose }: { factId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [to, setTo] = useState("");
  const [sourceSystem, setSourceSystem] = useState("admin_console");
  const [sourceRef, setSourceRef] = useState("");
  const [result, setResult] = useState<CommandResult | null>(null);
  return (
    <ConfirmDialog title="End this relationship" confirmLabel="End relationship" cancelLabel={result ? "Close" : "Cancel"} closeOnSuccess={false} onClose={onClose} disabled={!to}
      onConfirm={async () => {
        const res = await api.POST("/v1/relationships/{fact_id}/end", {
          params: { path: { fact_id: factId }, header: { "Idempotency-Key": newKey() } },
          body: { valid_to: to, classification: "internal", source_system: sourceSystem || null, source_ref: sourceRef || null },
        });
        setResult(readCommandResponse(res.response.status, res.data, res.error));
        await Promise.all([qc.invalidateQueries({ queryKey: ["entity"] }), qc.invalidateQueries({ queryKey: ["commands"] })]);
        return null;
      }}>
      <p className="text-sm text-slate-500">The fact stays in the history as a true past fact; only its valid-to date is set.</p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Valid to" className="col-span-2"><input aria-label="Valid to" type="date" className={`${inputCls} w-full`} value={to} onChange={(e) => setTo(e.target.value)} /></Field>
        <Field label="Source system"><input aria-label="Source system" className={`${inputCls} w-full`} value={sourceSystem} onChange={(e) => setSourceSystem(e.target.value)} /></Field>
        <Field label="Source reference"><input aria-label="Source reference" className={`${inputCls} w-full`} value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} /></Field>
      </div>
      {result ? <ResultBanner result={result} /> : null}
    </ConfirmDialog>
  );
}
