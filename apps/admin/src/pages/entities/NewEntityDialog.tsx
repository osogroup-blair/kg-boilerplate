import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Field, inputCls } from "../../components/ui";
import { newKey } from "../../lib/format";
import { CLASSIFICATIONS } from "../../ontology/spec";
import { readCommandResponse, ResultBanner, type CommandResult } from "./commandUi";

export function NewEntityDialog({ types, onClose }: { types: string[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [type, setType] = useState(types[0] ?? "");
  const [name, setName] = useState("");
  const [system, setSystem] = useState("");
  const [value, setValue] = useState("");
  const [classification, setClassification] = useState("internal");
  const [sourceSystem, setSourceSystem] = useState("admin_console");
  const [sourceRef, setSourceRef] = useState("");
  const [result, setResult] = useState<CommandResult | null>(null);

  return (
    <ConfirmDialog title="New entity" confirmLabel="Create entity" cancelLabel={result ? "Close" : "Cancel"} closeOnSuccess={false} onClose={onClose} disabled={!type || !name.trim()}
      onConfirm={async () => {
        const res = await api.POST("/v1/entities", {
          params: { header: { "Idempotency-Key": newKey() } }, // one key per click: a rejected command is recorded under its key, so a corrected retry needs a new one
          body: {
            type, name, classification: classification as "internal", source_system: sourceSystem || null, source_ref: sourceRef || null,
            external_ids: system && value ? [{ system, value }] : [],
          },
        });
        const r = readCommandResponse(res.response.status, res.data, res.error);
        setResult(r);
        await qc.invalidateQueries({ queryKey: ["entities"] });
        await qc.invalidateQueries({ queryKey: ["commands"] });
        return null;
      }}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Type"><select aria-label="Entity type" className={`${inputCls} w-full`} value={type} onChange={(e) => setType(e.target.value)}>{types.map((t) => <option key={t}>{t}</option>)}</select></Field>
        <Field label="Classification"><select aria-label="Classification" className={`${inputCls} w-full`} value={classification} onChange={(e) => setClassification(e.target.value)}>{CLASSIFICATIONS.map((c) => <option key={c}>{c}</option>)}</select></Field>
        <Field label="Name" className="col-span-2"><input aria-label="Name" className={`${inputCls} w-full`} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="External id system" hint="e.g. hr"><input aria-label="External id system" className={`${inputCls} w-full font-mono`} value={system} onChange={(e) => setSystem(e.target.value)} /></Field>
        <Field label="External id value" hint="Matches deterministically; names never do."><input aria-label="External id value" className={`${inputCls} w-full font-mono`} value={value} onChange={(e) => setValue(e.target.value)} /></Field>
        <Field label="Source system"><input aria-label="Source system" className={`${inputCls} w-full`} value={sourceSystem} onChange={(e) => setSourceSystem(e.target.value)} /></Field>
        <Field label="Source reference"><input aria-label="Source reference" className={`${inputCls} w-full`} value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} /></Field>
      </div>
      {result ? <ResultBanner result={result} /> : null}
    </ConfirmDialog>
  );
}
