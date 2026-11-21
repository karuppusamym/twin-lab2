// Who is signed in, and what they may see. The server enforces every rule; this only decides
// which controls to show.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";
import { api, data, setCsrfToken, type Role, type Session } from "./api/client";

const RANK: Record<Role, number> = { viewer: 1, builder: 2, owner: 3 };

export type Need = Role | "admin";

/** Mirrors auth.allowed: ranked roles per application; an admin reads every application. */
export function can(session: Session | undefined, need: Need, appId?: number | null): boolean {
  if (!session?.signed_in) return false;
  if (need === "admin") return session.is_admin;
  if (need === "viewer" && session.is_admin) return true;
  if (appId == null) return false;
  const held = session.roles.find((r) => r.app_id === appId)?.role;
  return held !== undefined && RANK[held] >= RANK[need];
}

export const sessionKey = ["session"] as const;

export function useSessionQuery() {
  return useQuery({
    queryKey: sessionKey,
    queryFn: async () => {
      const session = data(await api.GET("/api/v1/session"));
      setCsrfToken(session.csrf_token);
      return session;
    },
    staleTime: 60_000,
  });
}

const SessionContext = createContext<Session | undefined>(undefined);

export function SessionProvider({ session, children }: { session: Session; children: ReactNode }) {
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

/** The signed-in session (inside the signed-in part of the app). */
export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside SessionProvider");
  return session;
}

/** can(need, appId) for the signed-in user. */
export function useCan() {
  const session = useSession();
  return (need: Need, appId?: number | null) => can(session, need, appId);
}

/** Refresh who-am-I (after a role change, sign-in or sign-out). */
export function useRefreshSession() {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: sessionKey });
}
