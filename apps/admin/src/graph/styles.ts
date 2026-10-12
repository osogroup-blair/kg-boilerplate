/** How the Graph Explorer draws what the platform knows (P4 WP8). Pure, so the mapping is tested without a browser.
 *
 *  current + verified   solid
 *  historical           dashed, with its dates
 *  extracted / unverified   its own colour and a badge
 *  proposed / disputed  warning styles (off by default, toggleable)
 *  candidate / untyped entities are marked
 */
export interface FactLike {
  status: string;
  evidence_status: string;
  phase: string;
  valid_from?: string | null;
  valid_to?: string | null;
  rel_type: string;
}
export interface NodeLike {
  status: string;
  origin: string;
  type: string;
}
export interface EdgeStyle {
  classes: string[];
  line: "solid" | "dashed" | "dotted";
  badge: string | null;
  warning: boolean;
  label: string;
}

const day = (iso?: string | null) => (iso ? iso.slice(0, 10) : null);

export function edgeStyle(f: FactLike): EdgeStyle {
  const classes: string[] = [];
  let line: EdgeStyle["line"] = "solid";
  let badge: string | null = null;
  let warning = false;
  if (f.status === "proposed") {
    classes.push("proposed");
    line = "dotted";
    badge = "proposed";
    warning = true;
  } else if (f.status === "disputed") {
    classes.push("disputed");
    line = "dashed";
    badge = "disputed";
    warning = true;
  } else if (f.status === "rejected") {
    classes.push("rejected");
    line = "dotted";
    badge = "rejected";
  } else if (f.phase === "historical" || f.status === "superseded") {
    classes.push("historical");
    line = "dashed";
    badge = "ended";
  } else if (f.phase === "future") {
    classes.push("future");
    line = "dotted";
    badge = "future";
  }
  if (f.evidence_status === "unverified" && !warning && !classes.includes("historical") && !classes.includes("rejected")) {
    classes.push("unverified");
    badge = badge ?? "unverified";
  }
  if (classes.length === 0) classes.push("current");
  const dates = f.phase === "historical" || f.status === "superseded" ? ` (${day(f.valid_from) ?? "?"} → ${day(f.valid_to) ?? "?"})` : "";
  return { classes, line, badge, warning, label: `${f.rel_type}${dates}${badge && badge !== "ended" ? ` · ${badge}` : ""}` };
}

export function nodeStyle(n: NodeLike): { classes: string[]; badge: string | null } {
  const classes = [`type-${n.type}`];
  let badge: string | null = null;
  if (n.status === "candidate") {
    classes.push("candidate");
    badge = "candidate";
  } else if (n.status === "untyped") {
    classes.push("untyped");
    badge = "untyped";
  }
  if (n.origin === "extracted") classes.push("extracted");
  return { classes, badge };
}

/** Cytoscape stylesheet for the classes above. Colours are paired with a line style or a badge, never carried by colour alone. */
export const STYLESHEET = [
  { selector: "node", style: { label: "data(label)", "font-size": 11, "text-valign": "bottom", "text-margin-y": 4, "background-color": "#475569", color: "#0f172a", width: 26, height: 26 } },
  { selector: "node.type-Person", style: { "background-color": "#334155" } },
  { selector: "node.type-Organization", style: { "background-color": "#0e7490", shape: "round-rectangle" } },
  { selector: "node.type-Topic", style: { "background-color": "#7c3aed", shape: "diamond" } },
  { selector: "node.extracted", style: { "border-width": 2, "border-color": "#7c3aed" } },
  { selector: "node.candidate", style: { "border-width": 3, "border-style": "dashed", "border-color": "#d97706" } },
  { selector: "node.untyped", style: { "border-width": 3, "border-style": "dotted", "border-color": "#6b7280" } },
  { selector: "node.center", style: { "border-width": 4, "border-color": "#0f172a" } },
  { selector: "node:selected", style: { "overlay-color": "#0ea5e9", "overlay-opacity": 0.25, "overlay-padding": 6 } },
  { selector: "edge", style: { width: 2, "curve-style": "bezier", "target-arrow-shape": "triangle", "line-color": "#475569", "target-arrow-color": "#475569", label: "data(label)", "font-size": 9, "text-rotation": "autorotate", "text-background-color": "#ffffff", "text-background-opacity": 0.85 } },
  { selector: "edge.unverified", style: { "line-color": "#7c3aed", "target-arrow-color": "#7c3aed" } },
  { selector: "edge.historical", style: { "line-style": "dashed", "line-color": "#94a3b8", "target-arrow-color": "#94a3b8" } },
  { selector: "edge.future", style: { "line-style": "dotted", "line-color": "#0284c7", "target-arrow-color": "#0284c7" } },
  { selector: "edge.proposed", style: { "line-style": "dotted", "line-color": "#d97706", "target-arrow-color": "#d97706", width: 3 } },
  { selector: "edge.disputed", style: { "line-style": "dashed", "line-color": "#dc2626", "target-arrow-color": "#dc2626", width: 3 } },
  { selector: "edge.rejected", style: { "line-style": "dotted", "line-color": "#9ca3af", "target-arrow-color": "#9ca3af", opacity: 0.6 } },
  { selector: "edge:selected", style: { "overlay-color": "#0ea5e9", "overlay-opacity": 0.25, "overlay-padding": 5 } },
] as const;

export const HOPS_MAX = 2;
export const NODES_MAX = 100;
