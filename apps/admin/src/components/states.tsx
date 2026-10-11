import { AlertTriangle, Inbox, Loader2, Lock, SearchX } from "lucide-react";
import type { ReactNode } from "react";

function Panel({ icon, title, children, role }: { icon: ReactNode; title: string; children?: ReactNode; role?: string }) {
  return (
    <div role={role} className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-slate-300 p-8 text-center dark:border-slate-700">
      <div className="text-slate-400">{icon}</div>
      <p className="font-medium">{title}</p>
      {children ? <div className="text-sm text-slate-500">{children}</div> : null}
    </div>
  );
}

export const Loading = ({ label = "Loading…" }: { label?: string }) => (
  <Panel role="status" icon={<Loader2 className="h-6 w-6 animate-spin" />} title={label} />
);
export const Empty = ({ title, hint }: { title: string; hint?: string }) => <Panel icon={<Inbox className="h-6 w-6" />} title={title}>{hint}</Panel>;
export const PermissionDenied = ({ what }: { what: string }) => (
  <Panel role="alert" icon={<Lock className="h-6 w-6" />} title="Permission denied">
    Your role cannot view {what}. Ask a workspace admin if you need access.
  </Panel>
);
export const NotFoundState = ({ what }: { what: string }) => <Panel role="alert" icon={<SearchX className="h-6 w-6" />} title="Not found">{what} does not exist.</Panel>;
export const ErrorState = ({ message = "Something went wrong.", onRetry }: { message?: string; onRetry?: () => void }) => (
  <Panel role="alert" icon={<AlertTriangle className="h-6 w-6" />} title="Could not load">
    {message}
    {onRetry ? (
      <button type="button" onClick={onRetry} className="ml-2 underline">
        Retry
      </button>
    ) : null}
  </Panel>
);

/** Maps an API response status to the shared state to render, or null when the data is usable. */
export function stateForStatus(status: number | undefined, what: string, onRetry?: () => void): ReactNode | null {
  if (status === 403) return <PermissionDenied what={what} />;
  if (status === 404) return <NotFoundState what={what} />;
  if (status !== undefined && status >= 400) return <ErrorState onRetry={onRetry} />;
  return null;
}
