import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, exchangeKey, getToken, setToken, setUnauthorizedHandler } from "../api/client";
import type { components } from "../api/schema";

export type Me = components["schemas"]["MeOut"];
type Status = "loading" | "anonymous" | "authenticated";

interface AuthValue {
  status: Status;
  me: Me | null;
  notice: string | null;
  login: (apiKey: string) => Promise<void>;
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

  useEffect(() => {
    setUnauthorizedHandler(() => logout("Your session ended. Sign in again."));
    if (getToken()) void loadMe();
    return () => setUnauthorizedHandler(null);
  }, [loadMe, logout]);

  const login = useCallback(
    async (apiKey: string) => {
      const token = await exchangeKey(apiKey); // the API key is not stored anywhere
      setToken(token);
      setNotice(null);
      await loadMe();
    },
    [loadMe],
  );

  const value = useMemo(() => ({ status, me, notice, login, logout }), [status, me, notice, login, logout]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
