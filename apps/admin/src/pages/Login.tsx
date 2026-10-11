import { useState, type FormEvent } from "react";
import { useAuth } from "../auth/AuthContext";

export function Login() {
  const { login, notice } = useAuth();
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
        <p className="text-sm text-slate-500">Development sign-in: paste an API key. It is exchanged for a short-lived token and then discarded.</p>
        {notice ? <p role="status" className="text-sm text-amber-700 dark:text-amber-400">{notice}</p> : null}
        <label className="block text-sm">
          <span className="mb-1 block font-medium">API key</span>
          <input type="password" autoComplete="off" required value={apiKey} onChange={(e) => setApiKey(e.target.value)}
            className="w-full rounded-md border border-slate-300 bg-transparent px-3 py-2 font-mono text-sm dark:border-slate-700" placeholder="kgk_…" />
        </label>
        {error ? <p role="alert" className="text-sm text-red-600">{error}</p> : null}
        <button type="submit" disabled={busy || !apiKey} className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-slate-900">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
