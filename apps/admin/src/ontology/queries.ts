import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { components } from "../api/schema";

export type VersionOut = components["schemas"]["OntologyVersionOut"];
export type VersionSummary = components["schemas"]["OntologyVersionSummary"];
export type RuleCatalog = components["schemas"]["RuleCatalogOut"];
export type DiffOut = components["schemas"]["DiffResult"];

export function useVersions() {
  return useQuery({ queryKey: ["ontology-versions"], queryFn: () => api.GET("/v1/ontologies/versions", { params: { query: { limit: 100 } } }) });
}

/** The version plus the ETag header the API sent with it (If-Match for the next edit). */
export function useVersion(id: string) {
  return useQuery({
    queryKey: ["ontology-version", id],
    queryFn: async () => {
      const res = await api.GET("/v1/ontologies/versions/{version_id}", { params: { path: { version_id: id } } });
      return { data: res.data as VersionOut | undefined, status: res.response.status, etag: res.response.headers.get("etag") };
    },
  });
}

export function useActiveOntology() {
  return useQuery({ queryKey: ["ontology-active"], queryFn: () => api.GET("/v1/ontologies/active") });
}

export function useRuleCatalog() {
  return useQuery({ queryKey: ["rule-catalog"], staleTime: 5 * 60_000, queryFn: () => api.GET("/v1/ontologies/rule-catalog") });
}

export function useDiff(id: string, against: string) {
  return useQuery({
    queryKey: ["ontology-diff", id, against],
    queryFn: () => api.GET("/v1/ontologies/versions/{version_id}/diff", { params: { path: { version_id: id }, query: { against } } }),
  });
}

/** API errors carry {error: {code, details}}; the designer shows the code, never request values. */
export function errorCode(error: unknown): string {
  return (error as { error?: { code?: string } } | undefined)?.error?.code ?? "unknown_error";
}
