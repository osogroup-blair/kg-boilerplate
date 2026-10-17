import { useAuth } from "./AuthContext";

/** UI hints only: the API enforces every action (a hidden button is a convenience, a 403 is the control). Mirrors the role -> action matrix. */
export function useCan() {
  const roles = useAuth().me?.principal.roles ?? [];
  const has = (...wanted: string[]) => wanted.some((r) => (roles as string[]).includes(r));
  return {
    writeOntology: has("workspace_admin"),
    publishOntology: has("workspace_admin"),
    writeEntities: has("workspace_admin", "app_writer"),
    readLedger: has("workspace_admin", "operator", "reviewer", "app_writer"),
    readInteractions: has("workspace_admin", "operator", "reviewer", "app_writer", "agent"), // an agent sees only its own submissions (the API filters)
    readContent: has("workspace_admin"), // decrypted originals: admin only, every read audited
    repair: has("workspace_admin", "operator"), // replay a stage, retry or cancel a job
    readOperations: has("workspace_admin", "operator"),
    // P4
    readReviews: has("workspace_admin", "reviewer"),
    decideReviews: has("workspace_admin", "reviewer"),
    readReviewEvidence: has("workspace_admin", "reviewer"), // the content of an interaction a review item rests on (audited per read)
    searchGraph: has("workspace_admin", "operator", "reviewer"),
    // P5: every role may READ memory (the API scopes what each sees by grants); curation and the audited private reveal are narrower
    readMemories: has("workspace_admin", "operator", "reviewer", "app_writer", "agent", "reader"),
    curateMemories: has("workspace_admin", "reviewer"), // supersede, expire, dispute; decide memory reviews
    revealPrivateMemories: has("workspace_admin"), // another principal's private (agent / session) memory: asked for explicitly, audited
    readScopeGrants: has("workspace_admin"),
    // P7: registering, granting, rotating keys and revoking agents and apps (the API enforces `principal.manage`: workspace admins only)
    managePrincipals: has("workspace_admin"),
    // P6: the Lab acts as an agent or app (workspace admins only, audited); the API scopes every trace to its owner
    contextLab: has("workspace_admin"),
    readContext: has("workspace_admin", "reviewer", "app_writer", "agent"),
    // P8: corrections (reviewers and admins), deletion (admins for every kind; the API narrows by kind for apps and reviewers), holds, settings and the privacy audit (admins)
    correctRecords: has("workspace_admin", "reviewer"),
    readDeletions: has("workspace_admin", "operator", "app_writer"),
    requestDeletion: has("workspace_admin"),
    operateDeletions: has("workspace_admin", "operator"), // retry a failed request
    manageHolds: has("workspace_admin"),
    workspaceAdmin: has("workspace_admin"), // settings, the privacy audit
    simulateOntology: has("workspace_admin"),
    exploreGraph: has("workspace_admin", "operator", "reviewer", "app_writer", "reader"), // an agent gets graph-backed context through the API (P6), not through this console
  };
}
