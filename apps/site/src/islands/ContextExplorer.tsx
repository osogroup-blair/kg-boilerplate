import { useMemo, useState } from "react";
import { ContextCard } from "./Context";
import { CallView, Loading, useTape, type Call } from "./lib";

const unique = <T,>(xs: T[]) => [...new Set(xs)];

export default function ContextExplorer() {
  const { tape, error } = useTape("context-matrix");
  const calls: Call[] = useMemo(() => tape?.steps.flatMap((s) => s.calls) ?? [], [tape]);
  const tasks = unique(calls.map((c) => (c.request as any).task as string));
  const budgets = unique(calls.map((c) => (c.request as any).max_tokens as number)).sort((a, b) => a - b);
  const callers = unique(calls.map((c) => c.caller));
  const [task, setTask] = useState(0);
  const [profile, setProfile] = useState("planning");
  const [budget, setBudget] = useState(600);
  const [caller, setCaller] = useState("planning-agent");
  if (!tape) return <Loading error={error} />;
  const match = calls.find((c) => (c.request as any).task === tasks[task] && (c.request as any).profile === profile && (c.request as any).max_tokens === budget && c.caller === caller);
  const availableProfiles = unique(calls.filter((c) => c.caller === caller).map((c) => (c.request as any).profile as string));
  const validProfile = availableProfiles.includes(profile) ? profile : availableProfiles[0];
  const effective = calls.find((c) => (c.request as any).task === tasks[task] && (c.request as any).profile === validProfile && (c.request as any).max_tokens === budget && c.caller === caller) ?? match;
  return (
    <div className="kgi">
      <div className="kgi-controls">
        <label className="kgi-field">Task
          <select value={task} onChange={(e) => setTask(Number(e.target.value))}>{tasks.map((t, k) => <option key={k} value={k}>{t}</option>)}</select>
        </label>
        <label className="kgi-field">Who is asking
          <select value={caller} onChange={(e) => { setCaller(e.target.value); }}>{callers.map((c) => <option key={c}>{c}</option>)}</select>
        </label>
        <label className="kgi-field">Profile
          <select value={validProfile} onChange={(e) => setProfile(e.target.value)}>{availableProfiles.map((p) => <option key={p}>{p}</option>)}</select>
        </label>
        <label className="kgi-field">Token budget: {budget}
          <input type="range" min={0} max={budgets.length - 1} value={budgets.indexOf(budget)} onChange={(e) => setBudget(budgets[Number(e.target.value)])} aria-valuetext={`${budget} tokens`} />
        </label>
      </div>
      <p className="kgi-note">Every combination below was built by the real context engine against Sarah's world after step 7 (the job change). Try a 64-token budget, then 1200: the engine reports a budget that is too small instead of silently dropping the facts it must keep. Switch to the admin to compare profiles.</p>
      {effective ? (
        <>
          <ContextCard ctx={effective.response} />
          <h3 style={{ marginTop: "1rem" }}>What the API returned</h3>
          <CallView call={effective} />
        </>
      ) : (
        <p>That combination was not recorded.</p>
      )}
    </div>
  );
}
