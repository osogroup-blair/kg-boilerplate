import { TokenBar, humanize } from "./lib";

export function ContextCard({ ctx }: { ctx: any }) {
  const b = ctx.budgets ?? {};
  const statusTone = ctx.status === "complete" ? "good" : ctx.status === "denied" ? "bad" : "warn";
  const list = (key: string) => (Array.isArray(ctx[key]) ? ctx[key] : []);
  return (
    <div className="kgi-panel">
      <div className="kgi-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>Context package</h3>
        <span className={`pill ${statusTone}`}>{humanize(ctx.status)}</span>
      </div>
      {b.max_tokens ? <TokenBar used={b.used_tokens} max={b.max_tokens} /> : null}
      {ctx.status === "insufficient_budget" && b.required_tokens ? (
        <p className="kgi-note">The budget is too small for the records this task requires ({b.required_tokens} tokens). The package says so instead of cutting facts silently.</p>
      ) : null}
      <pre className="kgi-ctx" tabIndex={0} aria-label="The text the agent would put in its prompt">{ctx.context_text}</pre>
      <div className="kgi-kv">
        <span>facts <b>{list("facts").length}</b></span>
        <span>memories <b>{list("memories").length}</b></span>
        <span>unknowns <b>{list("unknowns").length}</b></span>
        <span>conflicts <b>{list("conflicts").length}</b></span>
        <span>citations <b>{list("citations").length}</b></span>
        {b.truncated ? <span>cut to fit <b>{Object.values(b.truncated as Record<string, number>).reduce((a, c) => a + c, 0)}</b></span> : null}
      </div>
      {list("unknowns").length ? (
        <p className="kgi-note"><b>Unknowns the agent should ask about:</b> {list("unknowns").map((u: any) => u.text ?? u.signal ?? u.key ?? JSON.stringify(u)).join("; ")}</p>
      ) : null}
    </div>
  );
}
