import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ErrorState, Loading, PermissionDenied } from "../../components/states";
import { Banner, btn, inputCls } from "../../components/ui";
import { PackageView, TraceView, type ContextOut, type TraceOut } from "./ContextPanels";

interface Profile { name: string; source: string; max_tokens: number; max_items: number; include: string[]; compression: string[]; history: string }
interface Run { budget: number; out: ContextOut; trace: TraceOut | null }
interface Failure { status: number; code: string; fields?: string[] }

/** Finds an entity by name: the Lab builds about one subject at a time. */
function SubjectPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const [q, setQ] = useState("");
  const found = useQuery({ queryKey: ["lab-subject", q], enabled: q.trim().length >= 2, queryFn: () => api.GET("/v1/entities", { params: { query: { q: q.trim(), limit: 10 } } }) });
  const options = found.data?.data?.items ?? [];
  return (
    <div className="flex flex-col gap-1">
      <span className="text-slate-600 dark:text-slate-300">Subject</span>
      <div className="flex gap-1">
        <input aria-label="Find a subject" placeholder="Search entities…" className={`${inputCls} w-40`} value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Subject" className={inputCls} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">No subject</option>
          {value && !options.some((o) => o.id === value) ? <option value={value}>{value.slice(0, 8)}…</option> : null}
          {options.map((o) => <option key={o.id} value={o.id}>{o.name} ({o.type})</option>)}
        </select>
      </div>
    </div>
  );
}

export function ContextLabPage() {
  const can = useCan();
  const [principalId, setPrincipalId] = useState("");
  const [subjectId, setSubjectId] = useState("");
  const [profileName, setProfileName] = useState("planning");
  const [task, setTask] = useState("");
  const [budget, setBudget] = useState(600);
  const [budgetB, setBudgetB] = useState(250);
  const [compare, setCompare] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [knownAt, setKnownAt] = useState("");
  const [project, setProject] = useState("");
  const [compression, setCompression] = useState("");
  const [allowPartial, setAllowPartial] = useState(true);
  const [evidenceRequired, setEvidenceRequired] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [failure, setFailure] = useState<Failure | null>(null);

  const principals = useQuery({ queryKey: ["lab-principals"], enabled: can.contextLab, queryFn: () => api.GET("/v1/principals", { params: { query: { limit: 100 } } }) });
  const profiles = useQuery({
    queryKey: ["lab-profiles"],
    enabled: can.contextLab,
    queryFn: async () => (await api.GET("/v1/context/profiles")).data as unknown as Profile[] | undefined,
  });
  // act-as applies the TARGET's registration: only the profiles it is allowed to build (none registered: every profile)
  const target = (principals.data?.data?.items ?? []).find((p) => p.id === principalId);
  const allowedProfiles = target && target.allowed_profiles && target.allowed_profiles.length > 0 ? target.allowed_profiles : null;
  const offered = (profiles.data ?? []).filter((p) => !allowedProfiles || allowedProfiles.includes(p.name));
  const profile = offered.find((p) => p.name === profileName) ?? offered[0];
  const cap = profile?.max_tokens ?? 1200;

  const run = useMutation({
    mutationFn: async () => {
      const budgets = compare ? [budget, budgetB] : [budget];
      const done: Run[] = [];
      for (const b of budgets) {
        const res = await api.POST("/v1/context/build", {
          params: { header: { "X-Act-As-Principal": principalId } },
          body: {
            task,
            profile: profile?.name ?? profileName,
            evidence_required: evidenceRequired,
            subject_ids: subjectId ? [subjectId] : [],
            max_tokens: Math.min(b, cap),
            as_of: asOf || undefined,
            known_at: knownAt || undefined,
            project: project || undefined,
            compression: (compression || undefined) as never,
            allow_partial: allowPartial,
            debug_snapshot: false,
            response_mode: "both",
          },
        });
        if (!res.data) {
          const err = (res.error as { error?: { code?: string; details?: { fields?: string[] } } } | undefined)?.error;
          throw Object.assign(new Error("build failed"), { failure: { status: res.response.status, code: err?.code ?? "error", fields: err?.details?.fields } satisfies Failure });
        }
        const trace = res.data.context_id ? (await api.GET("/v1/context/{context_id}/trace", { params: { path: { context_id: res.data.context_id } } })).data ?? null : null;
        done.push({ budget: b, out: res.data, trace });
      }
      return done;
    },
    onSuccess: (done) => { setFailure(null); setRuns(done); },
    onError: (e) => { setRuns([]); setFailure((e as { failure?: Failure }).failure ?? { status: 0, code: "unavailable" }); },
  });

  if (!can.contextLab) return <PermissionDenied what="the Context Lab" />;
  if (principals.isLoading || profiles.isLoading) return <Loading label="Loading the Lab…" />;
  if (principals.isError || profiles.isError) return <ErrorState onRetry={() => { void principals.refetch(); void profiles.refetch(); }} />;
  const actable = (principals.data?.data?.items ?? []).filter((p) => (p.kind === "agent" || p.kind === "app") && p.status === "active");
  const ready = principalId !== "" && task.trim() !== "" && !run.isPending;
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Context Lab</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">Builds a context AS an agent or app of this workspace, through the same endpoint and authorisation the agent uses. You see its view, never your own wider one. Every build is audited.</p>
      </div>
      <form className="grid grid-cols-1 gap-3 text-sm md:grid-cols-3" aria-label="Build a context" onSubmit={(e) => { e.preventDefault(); if (ready) run.mutate(); }}>
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Build as</span>
          <select aria-label="Build as" className={`${inputCls} w-full`} value={principalId} onChange={(e) => setPrincipalId(e.target.value)}>
            <option value="">Choose an agent or app…</option>
            {actable.map((p) => <option key={p.id} value={p.id}>{p.handle} ({p.kind})</option>)}
          </select></label>
        <SubjectPicker value={subjectId} onChange={setSubjectId} />
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Profile</span>
          <select aria-label="Profile" className={`${inputCls} w-full`} value={profile?.name ?? profileName} onChange={(e) => { setProfileName(e.target.value); setCompression(""); }}>
            {offered.map((p) => <option key={p.name} value={p.name}>{p.name} ({p.source})</option>)}
          </select>
          {allowedProfiles ? <span className="mt-1 block text-xs text-slate-500">{target?.handle} may build only: {allowedProfiles.join(", ")}.</span> : null}</label>
        <label className="md:col-span-3"><span className="mb-1 block text-slate-600 dark:text-slate-300">Task</span>
          <textarea aria-label="Task" rows={2} maxLength={2000} className={`${inputCls} w-full`} value={task} onChange={(e) => setTask(e.target.value)} placeholder="Help Sarah assess the implications of starting a business" /></label>
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Budget: <strong data-testid="budget-value">{Math.min(budget, cap)}</strong> tokens (cap {cap})</span>
          <input aria-label="Budget" type="range" min={64} max={cap} step={1} className="w-full" value={Math.min(budget, cap)} onChange={(e) => setBudget(Number(e.target.value))} /></label>
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">As of (valid time)</span><input aria-label="As of" type="date" className={`${inputCls} w-full`} value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Known at (record time)</span><input aria-label="Known at" type="date" className={`${inputCls} w-full`} value={knownAt} onChange={(e) => setKnownAt(e.target.value)} /></label>
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Project</span><input aria-label="Project" className={`${inputCls} w-full`} value={project} onChange={(e) => setProject(e.target.value)} placeholder="newco-plan" /></label>
        <label><span className="mb-1 block text-slate-600 dark:text-slate-300">Compression</span>
          <select aria-label="Compression" className={`${inputCls} w-full`} value={compression} onChange={(e) => setCompression(e.target.value)}>
            <option value="">Profile default</option>{(profile?.compression ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
          </select></label>
        <div className="flex flex-col justify-end gap-1">
          <label className="flex items-center gap-2"><input type="checkbox" checked={allowPartial} onChange={(e) => setAllowPartial(e.target.checked)} /> Allow a partial package</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={evidenceRequired} onChange={(e) => setEvidenceRequired(e.target.checked)} /> Require verified evidence <span className="text-xs text-slate-500">(drops unverified agent findings)</span></label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} /> Compare with a second budget
            {compare ? <input aria-label="Second budget" type="number" min={64} max={cap} className={`${inputCls} w-24`} value={budgetB} onChange={(e) => setBudgetB(Number(e.target.value))} /> : null}</label>
        </div>
        <div className="md:col-span-3"><button type="submit" className={btn.primary} disabled={!ready}>{run.isPending ? "Building…" : "Build"}</button></div>
      </form>

      {failure ? (
        <Banner tone="red" role="alert">
          {failure.code === "exceeds_profile" ? <>The profile does not allow {failure.fields?.join(", ")}: a request can only narrow a profile.</>
            : failure.code === "context_incomplete" ? <>A source the context needs is unavailable and “allow partial” is off, so no package was built.</>
            : failure.code === "profile_not_allowed" ? <>That principal is not registered to build this profile.</>
            : failure.code === "quota_exceeded" ? <>That principal has used its daily context quota.</>
            : failure.status === 429 ? <>Too many builds: wait a moment and try again.</>
            : <>The build failed ({failure.code}).</>}
        </Banner>
      ) : null}
      {runs.length > 0 ? (
        <div className={`grid gap-6 ${runs.length > 1 ? "lg:grid-cols-2" : ""}`} data-testid="runs">
          {runs.map((r) => (
            <div key={r.budget} className="min-w-0 space-y-6 rounded-md border border-slate-200 p-3 dark:border-slate-800" data-testid={`run-${r.budget}`}>
              <h2 className="font-medium">{r.budget} tokens</h2>
              <PackageView out={r.out} />
              {r.trace ? <section aria-label="Why this package"><h3 className="mb-2 font-medium">Why this package</h3><TraceView trace={r.trace} /></section> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
