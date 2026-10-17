import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { useCan } from "../../auth/permissions";
import { ErrorState, Loading, PermissionDenied, stateForStatus } from "../../components/states";
import { btn } from "../../components/ui";
import { PackageView, TraceView } from "./ContextPanels";

/** A past build: the trace as stored (ids, scores, reasons) beside the package re-materialised NOW under the viewer's current authorisation. Owner or workspace admin only. */
export function ContextTracePage() {
  const { contextId = "" } = useParams();
  const can = useCan();
  const trace = useQuery({ queryKey: ["context-trace", contextId], enabled: can.readContext, queryFn: () => api.GET("/v1/context/{context_id}/trace", { params: { path: { context_id: contextId } } }) });
  const pkg = useQuery({ queryKey: ["context-pkg", contextId], enabled: can.readContext, queryFn: () => api.GET("/v1/context/{context_id}", { params: { path: { context_id: contextId } } }) });
  if (!can.readContext) return <PermissionDenied what="context traces" />;
  if (trace.isLoading || pkg.isLoading) return <Loading label="Loading the trace…" />;
  if (trace.isError || pkg.isError) return <ErrorState onRetry={() => { void trace.refetch(); void pkg.refetch(); }} />;
  const blocked = stateForStatus(trace.data!.response.status, "This context", () => void trace.refetch()); // someone else's build is the same "Not found" as one that never existed
  if (blocked || !trace.data!.data) return <div className="space-y-4"><Link to="/context" className={btn.link}>← Context</Link>{blocked}</div>;
  return (
    <div className="space-y-6">
      <Link to={can.contextLab ? "/context" : "/"} className={btn.link}>← Context</Link>
      <h1 className="text-xl font-semibold">Context trace <span className="font-mono text-sm text-slate-500">{contextId}</span></h1>
      {pkg.data?.data ? <section aria-label="Package now"><h2 className="mb-2 font-medium">The package, re-materialised now</h2><PackageView out={pkg.data.data} /></section> : null}
      <section aria-label="Trace"><h2 className="mb-2 font-medium">Why</h2><TraceView trace={trace.data!.data} /></section>
    </div>
  );
}
