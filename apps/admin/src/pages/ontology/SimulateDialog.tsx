import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Badge, Banner, Field, inputCls } from "../../components/ui";

type Sim = components["schemas"]["SimulationOut"];
const ERRORS: Record<string, string> = {
  simulation_refused: "The simulation cannot run (nothing could be sent to a model, the estimate is above the budget, or it was already started).",
  validation_failed: "There is nothing to sample yet, or the draft has validation errors.",
  not_found: "That draft or sample no longer exists.",
  forbidden: "Your role may not run simulations.",
};
const usd = (n?: number | null) => (n == null ? "unknown" : `$${n.toFixed(4)}`);

function Report({ sim }: { sim: Sim }) {
  const r = sim.report as Record<string, unknown>;
  const facts = Object.entries((r.facts_by_type as Record<string, number>) ?? {});
  const nodes = Object.entries((r.nodes_by_type as Record<string, number>) ?? {});
  const dropped = Object.entries((r.dropped_by_draft as Record<string, number>) ?? {});
  const identity = (r.identity as { matched_existing?: number; new?: number }) ?? {};
  const diff = (r.diff as { changes?: number; breaking?: number }) ?? {};
  const newRels = (r.new_relationship_types as string[]) ?? [];
  return (
    <section aria-label="Simulation report" className="space-y-2 text-sm" data-testid="simulation-report">
      <p>Read {String(r.episodes ?? 0)} interaction{r.episodes === 1 ? "" : "s"} with this draft. Nothing was written to the workspace; the staging graph is already removed.</p>
      <ul className="list-disc space-y-0.5 pl-5">
        <li>Facts it would extract: {facts.length ? facts.map(([k, n]) => `${n} ${k}`).join(", ") : "none"}.</li>
        <li>Entities: {nodes.length ? nodes.map(([k, n]) => `${n} ${k}`).join(", ") : "none"} ({identity.matched_existing ?? 0} already known, {identity.new ?? 0} new).</li>
        {newRels.length ? <li>New relationship types in use: <span className="font-mono">{newRels.join(", ")}</span>.</li> : null}
        {dropped.length ? <li>Facts the active version produced that this draft would not: {dropped.map(([k, n]) => `${n} ${k}`).join(", ")}.</li> : null}
        <li>Compared with the active version: {diff.changes ?? 0} change{diff.changes === 1 ? "" : "s"}, {diff.breaking ?? 0} breaking.</li>
        <li>Model cost: {usd(sim.cost_actual)} (estimated {usd(sim.cost_estimate)}).</li>
      </ul>
    </section>
  );
}

export function SimulateDialog({ versionId, onClose }: { versionId: string; onClose: () => void }) {
  const [n, setN] = useState(3);
  const [preview, setPreview] = useState<Sim | null>(null);
  const [started, setStarted] = useState<string | null>(null);
  const status = useQuery({
    queryKey: ["simulation", started],
    enabled: !!started,
    refetchInterval: (q) => (q.state.data?.data && ["running", "previewed"].includes(q.state.data.data.status) ? 2000 : false),
    queryFn: () => api.GET("/v1/ontologies/simulations/{simulation_id}", { params: { path: { simulation_id: started! } } }),
  });
  const sim = status.data?.data ?? null;
  const body = (confirm: boolean) => ({ sample: { n }, confirm, simulation_id: preview?.id ?? null });
  async function run(confirm: boolean): Promise<string | null> {
    const res = await api.POST("/v1/ontologies/versions/{version_id}/simulate", { params: { path: { version_id: versionId } }, body: body(confirm) });
    if (res.data) {
      if (confirm) setStarted(res.data.id);
      else setPreview(res.data);
      return null;
    }
    return ERRORS[(res.error as { error?: { code?: string } } | undefined)?.error?.code ?? ""] ?? "The simulation could not be started.";
  }
  const done = sim && !["running", "previewed"].includes(sim.status);
  return (
    <ConfirmDialog title="Simulate this draft" confirmLabel={started ? "Running…" : preview ? "Run the simulation" : "Estimate the cost"} cancelLabel={done ? "Close" : "Cancel"} closeOnSuccess={false}
      disabled={!!started || (!preview && n < 1)} onClose={onClose} onConfirm={() => run(!!preview)}>
      <p className="text-sm text-slate-500">Reads recent interactions with this draft in a scratch graph that is removed when the run ends. Nothing is written to the workspace. Interactions that may not leave the workspace are skipped.</p>
      {!preview ? (
        <Field label="How many recent interactions" hint="Estimating costs nothing: no model is called."><input aria-label="Sample size" type="number" min={1} max={20} className={`${inputCls} w-24`} value={n} onChange={(e) => setN(Number.parseInt(e.target.value, 10) || 1)} /></Field>
      ) : (
        <div className="space-y-1 text-sm" data-testid="simulation-estimate">
          <p>{preview.runnable_count} of {preview.sample_count} sampled interactions can be sent{preview.skipped.policy ? ` (${preview.skipped.policy} skipped by policy)` : ""}.</p>
          <p>About {preview.estimate.calls ?? 0} model calls, {(preview.estimate.prompt_tokens ?? 0).toLocaleString()} prompt tokens. Estimated cost <strong>{usd(preview.cost_estimate)}</strong>.</p>
        </div>
      )}
      {started && !done ? <Banner tone="slate" role="status">Running… <Badge tone="blue">{sim?.status ?? "starting"}</Badge></Banner> : null}
      {done && sim ? (sim.status === "completed" ? <Report sim={sim} /> : <Banner tone="red" role="alert">The simulation {sim.status}. The scratch graph was removed.</Banner>) : null}
    </ConfirmDialog>
  );
}
