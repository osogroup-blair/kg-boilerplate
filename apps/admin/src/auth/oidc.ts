/** Console sign-in with the organisation's identity provider: authorization code + PKCE (S256). The platform's API does the token exchange (the console never holds a client
 * secret); the session token is an ID token kept in memory/sessionStorage like the key-exchange token, and the long-lived refresh token is an httpOnly SameSite=Strict
 * cookie the page cannot read. */

export interface OidcConfig {
  enabled: boolean;
  issuer?: string | null;
  client_id?: string | null;
  authorization_endpoint?: string | null;
  scopes?: string[];
  redirect_uris?: string[];
}

export interface OidcSession {
  id_token: string;
  expires_in: number;
  handle: string;
  roles: string[];
}

const PENDING = "kg.oidc.pending";
const MODE = "kg.admin.mode";

/** Tests replace `to`; production navigates the window. */
export const nav = { to: (url: string): void => void globalThis.location.assign(url) };

const b64url = (bytes: Uint8Array): string => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const random = (n: number): string => b64url(globalThis.crypto.getRandomValues(new Uint8Array(n)));

export async function challengeFor(verifier: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(digest));
}

export const redirectUri = (): string => `${globalThis.location.origin}/login/callback`;

export function sessionMode(): "oidc" | "key" {
  try {
    return sessionStorage.getItem(MODE) === "oidc" ? "oidc" : "key";
  } catch {
    return "key";
  }
}

export function setSessionMode(mode: "oidc" | "key"): void {
  try {
    sessionStorage.setItem(MODE, mode);
  } catch {
    /* private mode: the session simply cannot be refreshed after a reload */
  }
}

export async function fetchOidcConfig(): Promise<OidcConfig> {
  try {
    const res = await fetch("/v1/auth/oidc/config");
    if (!res.ok) return { enabled: false };
    return (await res.json()) as OidcConfig;
  } catch {
    return { enabled: false };
  }
}

/** Starts the redirect to the identity provider. The verifier and the state live in sessionStorage for this tab only. */
export async function startLogin(cfg: OidcConfig): Promise<void> {
  if (!cfg.enabled || !cfg.authorization_endpoint || !cfg.client_id) throw new Error("oidc_not_configured");
  const verifier = random(48);
  const state = random(24);
  sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state }));
  const url = new URL(cfg.authorization_endpoint);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: cfg.client_id,
    redirect_uri: redirectUri(),
    scope: (cfg.scopes ?? ["openid"]).join(" "),
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
    state,
  }).toString();
  nav.to(url.toString());
}

/** Completes the redirect: checks the state, then exchanges the code (with the verifier) through the API. */
export async function completeLogin(params: URLSearchParams): Promise<OidcSession> {
  let pending: { verifier: string; state: string } | null = null;
  try {
    pending = JSON.parse(sessionStorage.getItem(PENDING) ?? "null");
    sessionStorage.removeItem(PENDING);
  } catch {
    pending = null;
  }
  const code = params.get("code");
  if (!pending || !code || params.get("state") !== pending.state) throw new Error("state_mismatch");
  const res = await fetch("/v1/auth/oidc/callback", {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ code, code_verifier: pending.verifier, redirect_uri: redirectUri() }),
  });
  if (!res.ok) throw new Error(res.status >= 500 ? "unavailable" : "rejected");
  return (await res.json()) as OidcSession;
}

/** A new ID token from the httpOnly refresh cookie. null: no session to refresh (sign in again). */
export async function refreshSession(): Promise<OidcSession | null> {
  try {
    const res = await fetch("/v1/auth/oidc/refresh", { method: "POST", credentials: "include", headers: { "x-requested-with": "kg-console" } });
    return res.ok ? ((await res.json()) as OidcSession) : null;
  } catch {
    return null;
  }
}

export async function endSession(): Promise<void> {
  try {
    await fetch("/v1/auth/oidc/logout", { method: "POST", credentials: "include" });
  } catch {
    /* the cookie expires on its own */
  }
}
