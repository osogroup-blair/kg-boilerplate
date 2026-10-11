import type { components } from "../../api/schema";
import { Banner, Violations, type Violation } from "../../components/ui";

export type CommandOut = components["schemas"]["CommandOut"];

export interface CommandResult {
  ok: boolean;
  status: number;
  out?: CommandOut;
  code?: string;
  violations?: Violation[];
  commandId?: string;
}

/** Normalises the command API's answers (201/200/202 bodies, 422 envelopes) into one shape the dialogs render. */
export function readCommandResponse(status: number, data: unknown, error: unknown): CommandResult {
  if (data && status < 300) return { ok: true, status, out: data as CommandOut };
  const env = (error as { error?: { code?: string; details?: { violations?: Violation[]; command_id?: string; fields?: string[] } } } | undefined)?.error;
  return { ok: false, status, code: env?.code ?? "unknown_error", violations: env?.details?.violations, commandId: env?.details?.command_id };
}

export function ResultBanner({ result }: { result: CommandResult }) {
  if (result.ok && result.out) {
    const o = result.out;
    const text = o.status === "needs_review" ? "Queued for review: nothing was written." : o.matched_existing ? "An entity with that external id already exists: returned the existing one." : result.status === 202 ? "Saved. The graph projection is still pending and will finish shortly." : "Saved and projected.";
    return <Banner tone={o.status === "needs_review" ? "amber" : "green"} role="status">{text}</Banner>;
  }
  return (
    <Banner tone="red" role="alert">
      Rejected: <span className="font-mono">{result.code}</span>
      {result.violations?.length ? <Violations items={result.violations} /> : null}
    </Banner>
  );
}
