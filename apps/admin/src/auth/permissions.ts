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
    exploreGraph: has("workspace_admin", "operator", "reviewer", "app_writer", "reader"), // an agent gets graph-backed context through the API (P6), not through this console
  };
}
