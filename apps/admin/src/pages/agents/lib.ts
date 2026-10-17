import type { components } from "../../api/schema";

export type Grant = components["schemas"]["PrincipalGrantOut"];
export type RegisterBody = components["schemas"]["RegisterIn"];
export type Registered = components["schemas"]["RegisteredOut"];
export type Privilege = "read" | "propose" | "publish";
export const PRIVILEGES: Privilege[] = ["read", "propose", "publish"];
export type GrantKind = "subject" | "project" | "workspace" | "classification";
export const GRANT_KINDS: GrantKind[] = ["subject", "project", "workspace", "classification"];
export const CLASSIFICATION_LEVELS = ["public", "internal", "confidential", "restricted", "local_only"] as const;

export const HANDLE = /^[A-Za-z0-9][A-Za-z0-9._@-]{0,62}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PROJECT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

export interface GrantDraft { scope_kind: GrantKind; scope_ref: string; privileges: Privilege[] }

/** The same rules the API enforces (its answer is authoritative; this only saves a round trip). Returns a message, or null when the draft is fine. */
export function grantProblem(g: GrantDraft): string | null {
  if (!g.privileges.includes("read")) return "propose and publish need read: a principal cannot write into a scope it cannot see";
  if (g.scope_kind === "classification") {
    if (!(CLASSIFICATION_LEVELS as readonly string[]).includes(g.scope_ref)) return "pick a classification level";
    if (g.privileges.length !== 1) return "a classification grant is a read ceiling: read only";
  } else if (g.scope_kind === "workspace") {
    if (g.scope_ref !== "*") return "a workspace grant is workspace:*";
  } else if (g.scope_kind === "project") {
    if (g.scope_ref !== "*" && !PROJECT.test(g.scope_ref)) return "a project key is letters, digits, '.', '_' or '-'";
  } else if (g.scope_ref !== "*" && !UUID.test(g.scope_ref)) {
    return "a subject grant names an entity id (pick one from the directory) or *";
  }
  return null;
}

/** Toggling a privilege keeps the set valid: removing `read` removes the rest (they are meaningless without it), adding the rest adds `read`. */
export function togglePrivilege(current: Privilege[], p: Privilege): Privilege[] {
  const next = current.includes(p) ? current.filter((x) => x !== p) : [...current, p];
  if (p === "read" && !next.includes("read")) return [];
  if (p !== "read" && next.includes(p) && !next.includes("read")) next.unshift("read");
  return PRIVILEGES.filter((x) => next.includes(x));
}

export function errorCodeOf(error: unknown): string {
  return (error as { error?: { code?: string } } | undefined)?.error?.code ?? "failed";
}

export const REVOKE_EFFECTS: Record<"retired" | "compromised", string[]> = {
  retired: [
    "Every key stops working on the next request.",
    "Its working memories expire now; its private memories are frozen (erasure is a later phase).",
    "Proposals it submitted stay in the review queue, flagged as submitted by a revoked principal.",
    "What it already published stays as it is.",
  ],
  compromised: [
    "Every key stops working on the next request.",
    "Its working memories expire now; its private memories are frozen.",
    "Its unreviewed proposals are rejected.",
    "What it published, and what was extracted from what it submitted, becomes disputed with a review item to restore or confirm removal.",
    "Contexts show those items only as cautions.",
    "Its pending model processing is skipped.",
  ],
};
