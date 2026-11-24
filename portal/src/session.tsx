/**
 * The signed-in session: the pool, the OIDC user, the API client and `GET /api/me`.
 * `GET /me` decides the navigation (BUILD_SPEC.md section 11).
 */

import type { User, UserManager } from "oidc-client-ts";
import { createContext, useContext } from "react";

import type { Api, Me } from "./api/client";
import type { ActivePool } from "./config";

export interface Session {
  pool: ActivePool;
  manager: UserManager;
  user: User;
  api: Api;
  me: Me;
  token: () => string | null;
  signOut: () => Promise<void>;
  reloadMe: () => Promise<void>;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside a signed-in session");
  return session;
}
