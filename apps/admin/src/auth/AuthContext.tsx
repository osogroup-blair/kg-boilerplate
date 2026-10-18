import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, exchangeKey, getToken, setToken, setUnauthorizedHandler } from "../api/client";
import type { components } from "../api/schema";
import { completeLogin, endSession, refreshSession, sessionMode, setSessionMode } from "./oidc";

export type Me = components["schemas"]["MeOut"];
type Status = "loading" | "anonymous" | "authenticated";

interface AuthValue {
  status: Status;
  me: Me | null;
  notice: string | null;
  login: (apiKey: string) => Promise<void>;
  /** Finish an identity-provider redirect (the callback page): resolves when the session is open. */
  loginWithCode: (params: URLSearchParams) => Promise<void>;
  logout: (notice?: string) => void;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>(getToken() ? "loading" : "anonymous");
  const [me, setMe] = useState<Me | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const logout = useCallback(
    (message?: string) => {
      if (sessionMode() === "oidc") void endSession();
      setSessionMode("key");
      setToken(null);
      setMe(null);
      setNotice(message ?? null);
      setStatus("anonymous");
      queryClient.clear();
    },
    [queryClient],
  );

  const loadMe = useCallback(async () => {
    const { data } = await api.GET("/v1/me");
    if (data) {
      setMe(data);
      setStatus("authenticated");
    } else {
      logout();
    }
  }, [logout]);

  /** An identity-provider session renews itself from the httpOnly refresh cookie; a 401 first tries that, and only then ends the session. */
  const renew = useCallback(async (): Promise<boolean> => {
    const next = await refreshSession();
    if (!next) return false;
    setToken(next.id_token);
    return true;
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (sessionMode() !== "oidc") return logout("Your session ended. Sign in again.");
      void renew().then((ok) => (ok ? void queryClient.invalidateQueries() : logout("Your session ended. Sign in again.")));
    });
    if (getToken()) void loadMe();
    return () => setUnauthorizedHandler(null);
  }, [loadMe, logout, renew, queryClient]);

  // renew an identity-provider token a little before it expires
  useEffect(() => {
    if (status !== "authenticated" || sessionMode() !== "oidc") return;
    const id = setInterval(() => void renew(), 10 * 60 * 1000);
    return () => clearInterval(id);
  }, [status, renew]);

  const login = useCallback(
    async (apiKey: string) => {
      const token = await exchangeKey(apiKey); // the API key is not stored anywhere
      setSessionMode("key");
      setToken(token);
      setNotice(null);
      await loadMe();
    },
    [loadMe],
  );

  const loginWithCode = useCallback(
    async (params: URLSearchParams) => {
      const session = await completeLogin(params);
      setToken(session.id_token);
      setSessionMode("oidc");
      setNotice(null);
      await loadMe();
    },
    [loadMe],
  );

  const value = useMemo(() => ({ status, me, notice, login, loginWithCode, logout }), [status, me, notice, login, loginWithCode, logout]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
