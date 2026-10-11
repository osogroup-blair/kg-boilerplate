import * as Dialog from "@radix-ui/react-dialog";
import { useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import templateYaml from "../../../../../examples/ontologies/personal-knowledge.yaml?raw";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { Badge, Banner, VersionStateBadge, btn, inputCls } from "../../components/ui";
import { Empty, ErrorState, Loading, stateForStatus } from "../../components/states";
import { fmt, newKey, shortHash } from "../../lib/format";
import { errorCode, useActiveOntology, useVersions } from "../../ontology/queries";
import { bumpTemplateVersion } from "../../ontology/spec";

type Source = "active" | "template" | "yaml";

function NewDraftDialog({ versions, activeId, onClose }: { versions: Array<string | null>; activeId: string | null; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [source, setSource] = useState<Source>(activeId ? "active" : "template");
  const [yamlText, setYamlText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create() {
    setBusy(true);
    setError(null);
    const body =
      source === "active" ? { copy_of: activeId! } : source === "template" ? { content: bumpTemplateVersion(templateYaml, versions), format: "yaml" as const } : { content: yamlText, format: "yaml" as const };
    const res = await api.POST("/v1/ontologies/versions", { body, params: { header: { "Idempotency-Key": newKey() } } });
    setBusy(false);
    if (res.data) {
      await qc.invalidateQueries({ queryKey: ["ontology-versions"] });
      onClose();
      navigate(`/ontology/versions/${res.data.id}`);
    } else setError(errorCode(res.error));
  }

  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/30" />
        <Dialog.Content aria-describedby={undefined} className="fixed left-1/2 top-24 w-full max-w-lg -translate-x-1/2 space-y-4 rounded-lg bg-white p-6 shadow-xl dark:bg-slate-900">
          <div className="flex items-center justify-between">
            <Dialog.Title className="text-lg font-semibold">New ontology draft</Dialog.Title>
            <Dialog.Close aria-label="Close" className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-800"><X className="h-4 w-4" /></Dialog.Close>
          </div>
          <fieldset className="space-y-2 text-sm">
            <legend className="sr-only">Start from</legend>
            {activeId ? (
              <label className="flex items-start gap-2"><input type="radio" name="source" checked={source === "active"} onChange={() => setSource("active")} />
                <span><strong>The active version</strong><br /><span className="text-slate-500">A copy with the next minor version number.</span></span></label>
            ) : null}
            <label className="flex items-start gap-2"><input type="radio" name="source" checked={source === "template"} onChange={() => setSource("template")} />
              <span><strong>The personal-knowledge template</strong><br /><span className="text-slate-500">Person, Organization, Topic; WORKS_AT, KNOWS, CONSIDERING.</span></span></label>
            <label className="flex items-start gap-2"><input type="radio" name="source" checked={source === "yaml"} onChange={() => setSource("yaml")} />
              <span><strong>Paste YAML</strong></span></label>
          </fieldset>
          {source === "yaml" ? <textarea aria-label="Ontology YAML" rows={10} value={yamlText} onChange={(e) => setYamlText(e.target.value)} className={`${inputCls} w-full font-mono text-xs`} /> : null}
          {error ? <Banner tone="red" role="alert">Could not create the draft: <span className="font-mono">{error}</span></Banner> : null}
          <div className="flex justify-end gap-2">
            <button type="button" className={btn.secondary} onClick={onClose}>Cancel</button>
            <button type="button" className={btn.primary} disabled={busy || (source === "yaml" && !yamlText.trim())} onClick={() => void create()}>Create draft</button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function VersionsPage() {
  const can = useCan();
  const q = useVersions();
  const active = useActiveOntology();
  const [creating, setCreating] = useState(false);

  let content;
  if (q.isLoading) content = <Loading label="Loading versions…" />;
  else if (q.isError) content = <ErrorState onRetry={() => void q.refetch()} />;
  else {
    const res = q.data!;
    const blocked = stateForStatus(res.response.status, "ontology versions", () => void q.refetch());
    const items = res.data?.items ?? [];
    if (blocked) content = blocked;
    else if (items.length === 0)
      content = <Empty title="No ontology versions" hint={can.writeOntology ? "Create a draft from the template to give this workspace its vocabulary." : "A workspace admin has not published an ontology yet."} />;
    else
      content = (
        <table className="w-full text-left text-sm">
          <thead className="text-slate-500">
            <tr><th className="py-2 pr-4">Version</th><th className="pr-4">State</th><th className="pr-4">Hash</th><th className="pr-4">Errors</th><th className="pr-4">Published</th><th>Notes</th></tr>
          </thead>
          <tbody>
            {items.map((v) => (
              <tr key={v.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900">
                <td className="py-2 pr-4">
                  <Link to={`/ontology/versions/${v.id}`} className="font-mono underline-offset-2 hover:underline">{v.ontology_key ?? "(unparsed)"} {v.version ? `v${v.version}` : ""}</Link>{" "}
                  {v.active ? <Badge tone="blue">Active</Badge> : null}
                </td>
                <td className="pr-4"><VersionStateBadge state={v.state} /></td>
                <td className="pr-4 font-mono text-xs">{shortHash(v.content_hash)}</td>
                <td className={`pr-4 ${v.error_count ? "text-red-600" : ""}`}>{v.error_count}</td>
                <td className="whitespace-nowrap pr-4">{fmt(v.published_at)}</td>
                <td className="text-slate-500">{v.notes || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      );
  }

  const versions = q.data?.data?.items.map((v) => v.version) ?? [];
  // The template's version number is chosen against the existing versions, so the dialog must not open before they are known.
  const ready = q.isSuccess && active.isSuccess;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-slate-500">Published versions are immutable and content-hashed. Roll back by activating an older one.</p>
        {can.writeOntology ? <button type="button" className={btn.primary} disabled={!ready} onClick={() => setCreating(true)}>New draft</button> : null}
      </div>
      {!can.writeOntology ? <Banner>You can read the ontology. Editing and publishing need the workspace admin role.</Banner> : null}
      {content}
      {creating ? <NewDraftDialog versions={versions} activeId={active.data?.data?.version?.id ?? null} onClose={() => setCreating(false)} /> : null}
    </div>
  );
}
