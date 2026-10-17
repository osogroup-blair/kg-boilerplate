import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useOutletContext, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Badge, Banner, VersionStateBadge, btn } from "../../components/ui";
import { ErrorState, Loading, stateForStatus } from "../../components/states";
import { newKey, shortHash } from "../../lib/format";
import { errorCode, useVersion, type VersionOut } from "../../ontology/queries";
import { SimulateDialog } from "./SimulateDialog";
import { normaliseDoc, sameSpec, type Issue, type Spec } from "../../ontology/spec";

export interface VersionCtx {
  version: VersionOut;
  spec: Spec;
  setSpec: (next: Spec | ((s: Spec) => Spec)) => void;
  dirty: boolean;
  /** Published/retired versions, and callers without ontology.write, see the same forms read-only. */
  readOnly: boolean;
  issues: Issue[];
  save: () => Promise<boolean>;
  saveContent: (content: string | Record<string, unknown>, format: "yaml" | "json") => Promise<boolean>;
  saving: boolean;
  saveError: string | null;
  reload: () => Promise<void>;
}
export const useVersionCtx = () => useOutletContext<VersionCtx>();

const TAB = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-slate-100 font-medium dark:bg-slate-800" : "text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-800"}`;

const SAVE_ERRORS: Record<string, string> = {
  precondition_failed: "This draft changed elsewhere since you opened it. Reload to see the latest.",
  immutable_version: "This version is published and can no longer be edited.",
  version_exists: "Another version already uses that key and version number.",
  forbidden: "Your role cannot edit the ontology.",
};

export function VersionLayout() {
  const { versionId = "" } = useParams();
  const can = useCan();
  const qc = useQueryClient();
  const q = useVersion(versionId);
  const server = q.data?.data;
  const [spec, setSpecState] = useState<Spec | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [simulating, setSimulating] = useState(false);
  const loadedKey = server ? `${server.id}:${server.revision}` : null;

  // a new server revision (first load, save, reload) replaces the working copy
  useEffect(() => {
    if (server) setSpecState(normaliseDoc(server.spec));
  }, [loadedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const serverSpec = useMemo(() => (server ? normaliseDoc(server.spec) : null), [server]);
  const dirty = !!spec && !!serverSpec && !sameSpec(spec, serverSpec);
  const readOnly = !server || server.state !== "draft" || !can.writeOntology;
  const etag = q.data?.etag ?? null;

  const reload = useCallback(async () => {
    setSaveError(null);
    await qc.invalidateQueries({ queryKey: ["ontology-version", versionId] });
    await qc.invalidateQueries({ queryKey: ["ontology-versions"] });
  }, [qc, versionId]);

  const saveContent = useCallback(
    async (content: string | Record<string, unknown>, format: "yaml" | "json") => {
      setSaving(true);
      setSaveError(null);
      const res = await api.PUT("/v1/ontologies/versions/{version_id}", {
        params: { path: { version_id: versionId }, header: { "Idempotency-Key": newKey(), "If-Match": etag } },
        body: { content, format },
      });
      setSaving(false);
      if (res.data) {
        qc.setQueryData(["ontology-version", versionId], { data: res.data, status: 200, etag: res.response.headers.get("etag") });
        await qc.invalidateQueries({ queryKey: ["ontology-versions"] });
        await qc.invalidateQueries({ queryKey: ["ontology-diff", versionId] });
        return true;
      }
      const code = errorCode(res.error);
      setSaveError(SAVE_ERRORS[code] ?? `Save failed: ${code}`);
      return false;
    },
    [etag, qc, versionId],
  );
  const save = useCallback(() => saveContent(spec as unknown as Record<string, unknown>, "json"), [saveContent, spec]);

  if (q.isLoading) return <Loading label="Loading version…" />;
  if (q.isError) return <ErrorState onRetry={() => void q.refetch()} />;
  const blocked = stateForStatus(q.data!.status, "This ontology version", () => void q.refetch());
  if (blocked) return <>{blocked}</>; // a foreign or unknown id is the same "Not found" state
  if (!server || !spec) return <Loading label="Loading version…" />;

  const ctx: VersionCtx = {
    version: server,
    spec,
    setSpec: (next) => setSpecState((cur) => (cur ? (typeof next === "function" ? next(cur) : next) : cur)),
    dirty,
    readOnly,
    issues: server.validation_report.errors,
    save,
    saveContent,
    saving,
    saveError,
    reload,
  };
  const base = `/ontology/versions/${server.id}`;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-mono text-lg" data-testid="version-title">{server.ontology_key ?? "(unparsed)"} {server.version ? `v${server.version}` : ""}</h2>
          <VersionStateBadge state={server.state} />
          {server.active ? <Badge tone="blue">Active</Badge> : null}
          {server.content_hash ? <span className="font-mono text-xs text-slate-500" title={server.content_hash}>{shortHash(server.content_hash)}</span> : null}
        </div>
        {!readOnly ? (
          <div className="flex items-center gap-2">
            {dirty ? <Badge tone="amber">Unsaved changes</Badge> : null}
            {can.simulateOntology && server.state === "draft" ? <button type="button" className={btn.secondary} disabled={dirty} title={dirty ? "Save the draft first" : undefined} onClick={() => setSimulating(true)}>Simulate…</button> : null}
            <button type="button" className={btn.primary} disabled={!dirty || saving} onClick={() => void save()}>{saving ? "Saving…" : "Save draft"}</button>
          </div>
        ) : null}
      </div>
      {server.state !== "draft" ? <Banner>This version is {server.state}: it cannot be edited. Create a new draft from it to make changes.</Banner> : !can.writeOntology ? <Banner>Read-only: editing needs the workspace admin role.</Banner> : null}
      {saveError ? (
        <Banner tone="red" role="alert">{saveError}{" "}
          <button type="button" className={btn.link} onClick={() => void reload()}>Reload</button>
        </Banner>
      ) : null}
      <nav aria-label="Ontology designer" className="flex gap-1 border-b border-slate-200 pb-2 dark:border-slate-800">
        <NavLink to={base} end className={TAB}>Overview</NavLink>
        <NavLink to={`${base}/types`} className={TAB}>Types</NavLink>
        <NavLink to={`${base}/relationships`} className={TAB}>Relationships &amp; Rules</NavLink>
        <NavLink to={`${base}/yaml`} className={TAB}>YAML</NavLink>
      </nav>
      <Outlet context={ctx} />
      {simulating ? <SimulateDialog versionId={server.id} onClose={() => setSimulating(false)} /> : null}
    </div>
  );
}
