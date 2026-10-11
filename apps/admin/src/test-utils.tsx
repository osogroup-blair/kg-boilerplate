import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "./auth/AuthContext";
import { setToken } from "./api/client";

type Handler = (req: Request) => Response | Promise<Response>;

export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
export const errorEnvelope = (status: number, code: string) => json({ error: { code, message: code, request_id: "r", retry_after: null, details: null } }, status);

/** Routes "METHOD /path" (query string ignored) to handlers; any other request fails the test loudly. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(new URL(String(input), "http://localhost"), init);
    const key = `${req.method} ${new URL(req.url).pathname}`;
    calls.push(key);
    const handler = routes[key];
    if (!handler) throw new Error(`unexpected request: ${key}`);
    return handler(req);
  });
  return calls;
}

export const ME = {
  principal: { id: "p1", handle: "admin@demo", kind: "user", roles: ["workspace_admin"] },
  workspace: { id: "w1", slug: "demo", display_name: "Demo Workspace", status: "active" },
};

export function renderApp(ui: ReactElement, { route = "/", signedIn = true }: { route?: string; signedIn?: boolean } = {}) {
  setToken(signedIn ? "test-token" : null);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <AuthProvider>{ui}</AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
