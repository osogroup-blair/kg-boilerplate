import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import react from "@astrojs/react";
import mermaid from "astro-mermaid";

// DEC-14: the public host is a placeholder until the owner picks the domain. Set SITE_URL in CI to override.
const site = process.env.SITE_URL ?? "https://kg.oso.group";

export default defineConfig({
  site,
  redirects: {
    "/docs": "/docs/get-started/introduction/",
    "/docs/get-started": "/docs/get-started/introduction/",
    "/docs/concepts": "/docs/concepts/architecture/",
    "/docs/tutorials": "/docs/tutorials/first-domain/",
    "/docs/guides": "/docs/guides/add-a-domain/",
    "/docs/reference": "/docs/reference/api-recipes/",
    "/docs/operations": "/docs/operations/overview/",
    "/docs/project": "/docs/project/roadmap/",
  },
  integrations: [
    mermaid({ theme: "neutral", autoTheme: true }),
    starlight({
      title: "Knowledge Graph Boilerplate",
      description: "A multi-tenant knowledge graph and curated-memory platform for agents: sourced, time-aware, permissioned and correctable.",
      head: [
        {
          // keyboard users must be able to scroll wide tables: make a table that overflows focusable
          tag: "script",
          content: "addEventListener('DOMContentLoaded',function(){document.querySelectorAll('.sl-markdown-content table').forEach(function(t){if(t.scrollWidth>t.clientWidth){t.setAttribute('tabindex','0');t.setAttribute('role','region');t.setAttribute('aria-label','Scrollable table')}})})",
        },
      ],
      customCss: ["./src/styles/tokens.css", "./src/styles/starlight.css"],
      components: { SocialIcons: "./src/components/NavLinks.astro" },
      social: [{ icon: "github", label: "GitHub", href: "https://github.com/osogroup-blair/kg-boilerplate" }],
      editLink: { baseUrl: "https://github.com/osogroup-blair/kg-boilerplate/edit/main/" },
      lastUpdated: false,
      pagination: true,
      sidebar: [
        { label: "Get started", items: [{ autogenerate: { directory: "docs/get-started" } }] },
        { label: "Concepts", items: [{ autogenerate: { directory: "docs/concepts" } }] },
        { label: "Tutorials", items: [{ autogenerate: { directory: "docs/tutorials" } }] },
        { label: "Guides", items: [{ autogenerate: { directory: "docs/guides" } }] },
        { label: "Reference", items: [{ autogenerate: { directory: "docs/reference", collapsed: true } }] },
        { label: "Operations", items: [{ autogenerate: { directory: "docs/operations", collapsed: true } }] },
        { label: "Project", items: [{ autogenerate: { directory: "docs/project", collapsed: true } }] },
      ],
    }),
    react(),
  ],
});
