import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

/** Token holder: memory first, mirrored to sessionStorage so a reload keeps the session. Never localStorage. */
const STORAGE_KEY = "kg.admin.token";
let token: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function readStoredToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setToken(value: string | null): void {
  token = value;
  try {
    if (value) sessionStorage.setItem(STORAGE_KEY, value);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* sessionStorage can be unavailable (private mode); memory still works */
  }
}

export function getToken(): string | null {
  return token ?? readStoredToken();
}

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

const auth: Middleware = {
  onRequest({ request }) {
    const t = getToken();
    if (t) request.headers.set("authorization", `Bearer ${t}`);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401 && getToken()) onUnauthorized?.();
    return response;
  },
};

// fetch is resolved per call (not captured at import) so tests and polyfills can replace globalThis.fetch
export const api = createClient<paths>({ baseUrl: globalThis.location?.origin ?? "", fetch: (request) => globalThis.fetch(request) });
api.use(auth);

/** Exchange an API key for a short-lived token. The key is passed once and is not kept. */
export async function exchangeKey(apiKey: string): Promise<string> {
  const res = await fetch("/v1/auth/token", { method: "POST", headers: { authorization: `Bearer ${apiKey}` } });
  if (!res.ok) throw new Error(res.status === 401 ? "invalid" : "unavailable");
  const body = (await res.json()) as { access_token: string };
  return body.access_token;
}
