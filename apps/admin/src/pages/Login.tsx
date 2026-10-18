import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { fetchOidcConfig, startLogin, type OidcConfig } from "../auth/oidc";

/** The page the identity provider redirects back to: it exchanges the code and sends the person to where they were going. */
export function OidcCallback() {
  const { loginWithCode } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    loginWithCode(params).then(
      () => live && navigate("/", { replace: true }),
      (err: unknown) => live && setError(err instanceof Error && err.message === "unavailable" ? "The identity provider is unavailable. Try again shortly." : "Sign-in could not be completed."),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one exchange per redirect; the code is single-use
  }, []);
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      {error ? (
        <div role="alert" className="space-y-3 text-center"><p className="text-sm text-red-600">{error}</p><a className="text-sm underline" href="/">Back to sign in</a></div>
      ) : (
        <p role="status" className="text-sm text-slate-500">Signing you in…</p>
      )}
    </div>
  );
}

export function Login() {
  const { login, notice } = useAuth();
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [oidc, setOidc] = useState<OidcConfig | null>(null);
  const [showKey, setShowKey] = useState(false);
  useEffect(() => {
    void fetchOidcConfig().then(setOidc);
  }, []);
  const sso = oidc?.enabled === true;

  async function startSso() {
    setError(null);
    try {
      await startLogin(oidc!);
    } catch {
      setError("Single sign-on is not available right now.");
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(apiKey.trim());
      setApiKey("");
    } catch (err) {
      setError(err instanceof Error && err.message === "unavailable" ? "The API is unavailable. Try again shortly." : "That API key was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <h1 className="text-lg font-semibold">Sign in</h1>
        {sso ? (
          <>
            <p className="text-sm text-slate-500">Sign in with your organisation account.</p>
            {notice ? <p role="status" className="text-sm text-amber-700 dark:text-amber-400">{notice}</p> : null}
            <button type="button" onClick={() => void startSso()} className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white dark:bg-white dark:text-slate-900">Sign in with single sign-on</button>
            {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
            <button type="button" aria-expanded={showKey} onClick={() => setShowKey((v) => !v)} className="text-xs text-slate-500 underline">Break-glass: sign in with an API key</button>
          </>
        ) : (
          <p className="text-sm text-slate-500">Paste an API key. It is exchanged for a short-lived token and then discarded.</p>
        )}
        {!sso && notice ? <p role="status" className="text-sm text-amber-700 dark:text-amber-400">{notice}</p> : null}
        {!sso || showKey ? (<>
        <label className="block text-sm">
          <span className="mb-1 block font-medium">API key</span>
          <input type="password" autoComplete="off" required value={apiKey} onChange={(e) => setApiKey(e.target.value)}
            className="w-full rounded-md border border-slate-300 bg-transparent px-3 py-2 font-mono text-sm dark:border-slate-700" placeholder="kgk_…" />
        </label>
        {error && !sso ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
        <button type="submit" disabled={busy || !apiKey} className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-slate-900">
          {busy ? "Signing in…" : "Sign in"}
        </button></>) : null}
      </form>
    </div>
  );
}
