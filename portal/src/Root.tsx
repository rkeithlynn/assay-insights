/**
 * Starts the portal: reads /config.json, picks the user pool from the host, completes a
 * sign-in redirect if this is one, and loads `GET /api/me`. Until then it shows a loading,
 * signed-out or error page.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import type { User, UserManager } from "oidc-client-ts";
import { useCallback, useEffect, useState } from "react";

import { ApiError, apiClient, call, type Me } from "./api/client";
import { App } from "./App";
import { accessToken, CALLBACK_PATH, logoutUrl, userManager } from "./auth";
import { type ActivePool, loadConfig, poolFor } from "./config";
import { messageFor, SIGN_IN_AGAIN } from "./errors";
import { type Session, SessionContext } from "./session";
import logoUrl from "@assets/assay-insights-logo.svg";

type Phase =
  | { kind: "loading" }
  | { kind: "signed-out"; note?: string }
  | { kind: "refused"; message: string }
  | { kind: "error"; message: string }
  | { kind: "ready"; user: User; me: Me };

function Page({ children }: { children: React.ReactNode }) {
  return (
    <Box padding={{ vertical: "xxxl", horizontal: "l" }}>
      <div style={{ maxWidth: 480, margin: "0 auto" }}>
        <SpaceBetween size="l">
          <img src={logoUrl} alt="Assay Insights" style={{ height: 48 }} />
          {children}
        </SpaceBetween>
      </div>
    </Box>
  );
}

// One portal per page: the signed-in user lives here, outside React's render, and the API
// client reads its access token on every request (the token renews in the background).
let currentUser: User | null = null;
const token = (): string | null => accessToken(currentUser);
const api = apiClient(token);

interface Started {
  pool: ActivePool;
  manager: UserManager;
}

export function Root() {
  const [started, setStarted] = useState<Started | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  const loadMe = useCallback(
    async (user: User, manager: UserManager) => {
      currentUser = user;
      try {
        const me = await call(api.GET("/api/me"));
        setPhase({ kind: "ready", user, me });
      } catch (error) {
        if (error instanceof ApiError && SIGN_IN_AGAIN.has(error.code)) {
          await manager.removeUser();
          setPhase({ kind: "signed-out", note: messageFor(error) });
        } else if (error instanceof ApiError && error.status === 403) {
          setPhase({ kind: "refused", message: messageFor(error) });
        } else {
          setPhase({ kind: "error", message: messageFor(error) });
        }
      }
    },
    [],
  );

  useEffect(() => {
    const run = { cancelled: false };
    const cancelled = () => run.cancelled;
    void (async () => {
      try {
        const config = await loadConfig();
        const active = poolFor(config, window.location.host);
        const manager = userManager(active);
        manager.events.addUserLoaded((user) => {
          currentUser = user;
        });
        if (cancelled()) return;
        setStarted({ pool: active, manager });
        let user: User | null;
        if (window.location.pathname === CALLBACK_PATH) {
          user = await manager.signinRedirectCallback();
          window.history.replaceState(null, "", "/");
        } else {
          user = await manager.getUser();
          if (user?.expired && user.refresh_token) {
            user = await manager.signinSilent().catch(() => null);
          }
        }
        if (cancelled()) return;
        if (!user || user.expired) {
          setPhase({ kind: "signed-out" });
          return;
        }
        await loadMe(user, manager);
      } catch {
        if (!cancelled()) setPhase({ kind: "error", message: "The portal could not start. Reload the page." });
      }
    })();
    return () => {
      run.cancelled = true;
    };
  }, [loadMe]);

  const signIn = useCallback(() => {
    void started?.manager.signinRedirect();
  }, [started]);

  const signOut = useCallback(async () => {
    if (!started) return;
    const { manager, pool } = started;
    // Recorded first, while the token is still valid. A failure must not keep anyone in.
    await api.POST("/api/auth/signed-out").catch(() => undefined);
    await manager.revokeTokens(["refresh_token"]).catch(() => undefined);
    await manager.removeUser();
    currentUser = null;
    window.location.assign(logoutUrl(pool));
  }, [started]);

  const reloadMe = useCallback(async () => {
    if (currentUser && started) await loadMe(currentUser, started.manager);
  }, [loadMe, started]);

  const pool = started?.pool ?? null;

  if (phase.kind === "loading") {
    return (
      <Page>
        <Box textAlign="center">
          <Spinner size="large" /> <Box variant="span">Loading…</Box>
        </Box>
      </Page>
    );
  }
  if (phase.kind === "signed-out") {
    return (
      <Page>
        <Container
          header={
            <Header variant="h1" description={pool?.kind === "platform" ? "Administration" : undefined}>
              Sign in
            </Header>
          }
        >
          <SpaceBetween size="m">
            {phase.note && <Alert type="info">{phase.note}</Alert>}
            <Button variant="primary" onClick={signIn}>
              Sign in
            </Button>
          </SpaceBetween>
        </Container>
      </Page>
    );
  }
  if (phase.kind === "refused" || phase.kind === "error") {
    return (
      <Page>
        <Alert type="error" header={phase.kind === "refused" ? "Access refused" : "Something went wrong"}>
          {phase.message}
        </Alert>
        <Button onClick={() => void signOut()}>Sign out</Button>
      </Page>
    );
  }
  if (!started) return null;
  const session: Session = {
    pool: started.pool,
    manager: started.manager,
    user: phase.user,
    api,
    me: phase.me,
    token,
    signOut,
    reloadMe,
  };
  return (
    <SessionContext.Provider value={session}>
      <App />
    </SessionContext.Provider>
  );
}
