import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Badge, Banner, btn, Field, inputCls } from "../../components/ui";
import { ErrorState, Loading } from "../../components/states";
import { newKey } from "../../lib/format";
import { errorCode, useDiff, type DiffOut } from "../../ontology/queries";
import { keyForPath, SEMVER, setHeader, tabForPath, type Issue } from "../../ontology/spec";
import { useVersionCtx } from "./VersionLayout";

function ReportPanel({ errors, warnings, tests, base }: { errors: Issue[]; warnings: Issue[]; tests: Array<{ path: string; name: string; expected: string; actual: string; passed: boolean }>; base: string }) {
  const link = (path: string) => {
    const tab = tabForPath(path);
    const key = keyForPath(path);
    return tab === "overview" ? base : `${base}/${tab}${key ? `/${key}` : ""}`;
  };
  return (
    <section aria-label="Validation" className="space-y-3">
      <h3 className="font-medium">Validation</h3>
      {errors.length === 0 ? <Banner tone="green">No errors. The ontology is ready to publish.</Banner> : (
        <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-800 dark:bg-red-950">
          <p className="font-medium">{errors.length} error{errors.length === 1 ? "" : "s"} to fix before publishing</p>
          <ul className="mt-2 space-y-1">
            {errors.map((e) => (
              <li key={`${e.code}:${e.path}`}>
                <span className="font-mono text-xs">{e.code}</span> — {e.message}{" "}
                {e.path ? <Link to={link(e.path)} className="font-mono text-xs underline">{e.path}</Link> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
      {warnings.length > 0 ? <ul className="list-disc pl-5 text-sm text-amber-700">{warnings.map((w) => <li key={`${w.code}:${w.path}`}>{w.message} <span className="font-mono text-xs">{w.path}</span></li>)}</ul> : null}
      {tests.length > 0 ? (
        <details className="text-sm">
          <summary className="cursor-pointer">Rule tests: {tests.filter((t) => t.passed).length}/{tests.length} passing</summary>
          <ul className="mt-1 space-y-0.5">{tests.map((t) => <li key={t.path}><Badge tone={t.passed ? "green" : "red"}>{t.passed ? "pass" : "fail"}</Badge> {t.name || t.path} <span className="text-slate-500">expected {t.expected}, got {t.actual}</span></li>)}</ul>
        </details>
      ) : null}
    </section>
  );
}

function DiffPanel({ diff }: { diff: DiffOut }) {
  if (diff.changes.length === 0) return <p className="text-sm text-slate-500">No differences from the active version.</p>;
  return (
    <div className="space-y-3">
      <p className="text-sm">{diff.changes.length} change{diff.changes.length === 1 ? "" : "s"}, <strong>{diff.changes.filter((c) => c.breaking).length} breaking</strong>{diff.from_version ? ` against v${diff.from_version}` : ""}.</p>
      <table className="w-full text-left text-sm">
        <thead className="text-slate-500"><tr><th className="py-1 pr-3">Change</th><th className="pr-3">Where</th><th>Impact</th></tr></thead>
        <tbody>
          {diff.changes.map((c) => (
            <tr key={`${c.kind}:${c.path}:${c.detail}`} className="border-t border-slate-200 dark:border-slate-800">
              <td className="py-1 pr-3">{c.kind}</td>
              <td className="pr-3 font-mono text-xs">{c.path}{c.detail ? <span className="text-slate-500"> ({c.detail})</span> : null}</td>
              <td>{c.breaking ? <Badge tone="red" title={c.reason ?? undefined}>breaking</Badge> : <Badge tone="slate">compatible</Badge>} {c.reason ? <span className="text-xs text-slate-500">{c.reason}</span> : null}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {diff.impact.length > 0 ? (
        <div>
          <h4 className="text-sm font-medium">Rows these types hold today</h4>
          <ul className="text-sm">{diff.impact.map((i) => <li key={`${i.kind}:${i.type_key}`}><span className="font-mono">{i.type_key}</span>: {i.active} active {i.kind === "entity" ? "entities" : "facts"}</li>)}</ul>
        </div>
      ) : null}
    </div>
  );
}

export function OverviewTab() {
  const { version, spec, setSpec, readOnly, dirty, issues } = useVersionCtx();
  const can = useCan();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const diff = useDiff(version.id, "active");
  const [dialog, setDialog] = useState<"publish" | "activate" | "retire" | "delete" | null>(null);
  const [notes, setNotes] = useState("");
  const [activate, setActivate] = useState(true);
  const [reason, setReason] = useState("rollback");
  const [fresh, setFresh] = useState<typeof version.validation_report | null>(null);
  const report = fresh ?? version.validation_report;
  const base = `/ontology/versions/${version.id}`;
  const blocked = dirty || report.errors.length > 0;
  const needsNotes = notes.trim().length === 0; // a release carries a reason: it is the first thing a later reader asks for

  async function afterChange() {
    await Promise.all([qc.invalidateQueries({ queryKey: ["ontology-version"] }), qc.invalidateQueries({ queryKey: ["ontology-versions"] }), qc.invalidateQueries({ queryKey: ["ontology-active"] }), qc.invalidateQueries({ queryKey: ["ontology-diff"] })]);
  }
  async function validate() {
    const res = await api.POST("/v1/ontologies/versions/{version_id}/validate", { params: { path: { version_id: version.id } } });
    if (res.data) setFresh(res.data);
  }
  const header = { "Idempotency-Key": newKey() };

  return (
    <div className="space-y-8">
      <section aria-label="Details" className="space-y-3">
        <h3 className="font-medium">Details</h3>
        <div className="flex flex-wrap gap-4">
          <Field label="Ontology key" issues={issues.filter((i) => i.path === "ontology.key")}>
            <input aria-label="Ontology key" className={`${inputCls} font-mono`} disabled={readOnly} value={spec.ontology.key} onChange={(e) => setSpec((s) => setHeader(s, { key: e.target.value }))} />
          </Field>
          <Field label="Version" hint="major.minor.patch" issues={issues.filter((i) => i.path === "ontology.version")}>
            <input aria-label="Version" className={`${inputCls} w-28 font-mono ${spec.ontology.version && !SEMVER.test(spec.ontology.version) ? "border-red-500" : ""}`} disabled={readOnly} value={spec.ontology.version} onChange={(e) => setSpec((s) => setHeader(s, { version: e.target.value }))} />
          </Field>
          <Field label="Mode" hint="strict rejects unknown attributes; permissive keeps and warns." issues={issues.filter((i) => i.path === "ontology.mode")}>
            <select aria-label="Mode" className={inputCls} disabled={readOnly} value={spec.ontology.mode} onChange={(e) => setSpec((s) => setHeader(s, { mode: e.target.value as "strict" | "permissive" }))}>
              <option>strict</option><option>permissive</option>
            </select>
          </Field>
        </div>
        {version.notes ? <p className="text-sm text-slate-500">Release notes: {version.notes}</p> : null}
        <div className="flex flex-wrap gap-2">
          <button type="button" className={btn.secondary} onClick={() => void validate()}>Validate</button>
          {can.publishOntology && version.state === "draft" ? <button type="button" className={btn.primary} onClick={() => setDialog("publish")}>Publish…</button> : null}
          {can.publishOntology && version.state === "published" && !version.active ? <button type="button" className={btn.primary} onClick={() => setDialog("activate")}>Activate (roll back)…</button> : null}
          {can.publishOntology && version.state === "published" && !version.active ? <button type="button" className={btn.secondary} onClick={() => setDialog("retire")}>Retire…</button> : null}
          {can.writeOntology && version.state === "draft" ? <button type="button" className={btn.danger} onClick={() => setDialog("delete")}>Delete draft…</button> : null}
        </div>
      </section>

      <ReportPanel errors={report.errors} warnings={report.warnings} tests={report.rule_tests} base={base} />

      <section aria-label="Changes against the active version" className="space-y-3">
        <h3 className="font-medium">Changes against the active version</h3>
        {diff.isLoading ? <Loading label="Comparing…" /> : diff.isError || !diff.data?.data ? (
          diff.data?.response.status === 422 ? <p className="text-sm text-slate-500">The draft has structural errors, so it cannot be compared yet.</p> : <ErrorState onRetry={() => void diff.refetch()} />
        ) : <DiffPanel diff={diff.data.data} />}
      </section>

      {dialog === "publish" ? (
        <ConfirmDialog title={`Publish ${version.ontology_key ?? ""} v${version.version ?? ""}`} confirmLabel="Publish" onClose={() => setDialog(null)} disabled={blocked || needsNotes}
          onConfirm={async () => {
            const res = await api.POST("/v1/ontologies/versions/{version_id}/publish", { params: { path: { version_id: version.id }, header }, body: { notes, activate } });
            if (res.data) { await afterChange(); return null; }
            return `Could not publish: ${errorCode(res.error)}`;
          }}>
          {dirty ? <Banner tone="amber" role="alert">Save your changes first: only the saved draft is published.</Banner> : null}
          {report.errors.length > 0 ? <Banner tone="red" role="alert">Fix the {report.errors.length} validation error(s) first.</Banner> : null}
          <p className="text-sm">Publishing freezes this version and records its content hash. It can never be edited; to change it later, publish a new version.</p>
          {diff.data?.data ? <p className="text-sm"><strong>{diff.data.data.changes.length}</strong> change(s) against the active version, <strong>{diff.data.data.changes.filter((c) => c.breaking).length}</strong> breaking.</p> : null}
          <Field label="Release notes" hint="Required: say what changed and why."><textarea aria-label="Release notes" rows={3} className={`${inputCls} w-full`} value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} /> Make it the active ontology</label>
        </ConfirmDialog>
      ) : null}
      {dialog === "activate" ? (
        <ConfirmDialog title={`Activate v${version.version ?? ""}`} confirmLabel="Activate" onClose={() => setDialog(null)} disabled={!/^[a-z][a-z0-9_]{0,63}$/.test(reason)}
          onConfirm={async () => {
            const res = await api.POST("/v1/ontologies/active", { params: { header }, body: { version_id: version.id, reason_code: reason } });
            if (res.data) { await afterChange(); return null; }
            return `Could not activate: ${errorCode(res.error)}`;
          }}>
          <p className="text-sm">New commands will be validated against this version. Nothing already stored is changed, and no version is edited; the activation is recorded.</p>
          <Field label="Reason code" hint="lowercase letters, digits and underscores"><input aria-label="Reason code" className={`${inputCls} font-mono`} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        </ConfirmDialog>
      ) : null}
      {dialog === "retire" ? (
        <ConfirmDialog title="Retire this version" confirmLabel="Retire" danger onClose={() => setDialog(null)}
          onConfirm={async () => {
            const res = await api.POST("/v1/ontologies/versions/{version_id}/retire", { params: { path: { version_id: version.id }, header } });
            if (res.data) { await afterChange(); return null; }
            return `Could not retire: ${errorCode(res.error)}`;
          }}>
          <p className="text-sm">A retired version can no longer be activated. Entities and facts written under it keep pointing at it.</p>
        </ConfirmDialog>
      ) : null}
      {dialog === "delete" ? (
        <ConfirmDialog title="Delete this draft" confirmLabel="Delete draft" danger onClose={() => setDialog(null)}
          onConfirm={async () => {
            const res = await api.DELETE("/v1/ontologies/versions/{version_id}", { params: { path: { version_id: version.id }, header } });
            if (res.data) { await qc.invalidateQueries({ queryKey: ["ontology-versions"] }); navigate("/ontology"); return null; }
            return `Could not delete: ${errorCode(res.error)}`;
          }}>
          <p className="text-sm">Only drafts can be deleted. This cannot be undone.</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
