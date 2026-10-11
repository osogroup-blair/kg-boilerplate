import { NavLink, Outlet } from "react-router-dom";

/** Ontology section shell: the list of versions at /ontology, and a designer per version at /ontology/versions/:id. */
export function OntologyPage() {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="text-xl font-semibold">Ontology</h1>
        <NavLink to="/ontology" end className="text-sm text-slate-500 underline-offset-2 hover:underline">All versions</NavLink>
      </div>
      <Outlet />
    </div>
  );
}
