import { Activity, Hammer, BookOpen, Brain, Eraser, EyeOff, FlaskConical, Gauge, GitFork, ListChecks, LogOut, MessagesSquare, Network, Settings, ShieldQuestion, Users } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useCan } from "../auth/permissions";

// Navigation lists only the pages that exist in this phase; later phases add theirs.
const NAV = [
  { to: "/", label: "Dashboard", icon: Activity, end: true },
  { to: "/ontology", label: "Ontology", icon: BookOpen, end: false },
  { to: "/entities", label: "Entities", icon: Network, end: false },
  { to: "/graph", label: "Graph Explorer", icon: GitFork, end: false },
  { to: "/graph/rebuild", label: "Graph rebuild", icon: Hammer, end: false, needs: "workspaceAdmin" },
  { to: "/memories", label: "Memories", icon: Brain, end: false },
  { to: "/context", label: "Context Lab", icon: FlaskConical, end: false, needs: "contextLab" },
  { to: "/reviews", label: "Review Queue", icon: ShieldQuestion, end: false },
  { to: "/commands", label: "Commands", icon: ListChecks, end: false },
  { to: "/interactions", label: "Interactions & Episodes", icon: MessagesSquare, end: false },
  { to: "/operations", label: "Operations", icon: Gauge, end: false },
  { to: "/agents", label: "Agents & Access", icon: Users, end: false },
  { to: "/deletions", label: "Deletions", icon: Eraser, end: false, needs: "readDeletions" },
  { to: "/privacy", label: "Privacy audit", icon: EyeOff, end: false, needs: "workspaceAdmin" },
  { to: "/settings", label: "Settings", icon: Settings, end: false, needs: "workspaceAdmin" },
];

export function Layout() {
  const { me, logout } = useAuth();
  const can = useCan();
  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3 dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-6">
          <span className="font-semibold">Knowledge Platform</span>
          <nav aria-label="Main" className="flex gap-1">
            {NAV.filter((n) => !n.needs || can[n.needs as "contextLab" | "readDeletions" | "workspaceAdmin"]).map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end}
                className={({ isActive }) => `flex items-center gap-2 rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-slate-100 font-medium dark:bg-slate-800" : "text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-800"}`}>
                <Icon className="h-4 w-4" aria-hidden />
                {label}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="flex items-center gap-4 text-sm">
          <span data-testid="workspace-name" className="rounded bg-slate-100 px-2 py-1 dark:bg-slate-800">{me?.workspace.display_name}</span>
          <span data-testid="principal-handle" className="text-slate-600 dark:text-slate-300">{me?.principal.handle}</span>
          <button type="button" onClick={() => logout()} className="flex items-center gap-1 text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white">
            <LogOut className="h-4 w-4" aria-hidden /> Sign out
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-6xl p-6">
        <Outlet />
      </main>
    </div>
  );
}
