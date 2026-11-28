/**
 * Starts the portal: reads /config.json, picks the user pool from the host, completes a
 * sign-in redirect if this is one, and loads `GET /api/me`. Until then it shows a loading,
 * signed-out or error page.
 *
 * After a sign-in redirect only (not a reload, not a token refresh), it reports the session's
 * start, so the audit trail has the sign-in's IP address and browser, which Cognito's
 * trigger never sees. The API keeps one row per sign-in, however often this is called.
 *
 * Signing out in one tab signs out the portal's other open tabs in this browser
 * (`signout.ts`): each revokes its own refresh token, clears its tokens and shows the
 * sign-in screen. Only the tab where the user signed out records the sign-out and goes to
 * Cognito's logout endpoint.
 *
 * A session that cannot be renewed (the refresh token expired, was revoked, or was rotated out
 * by another tab that shares it) ends on the sign-in screen with a message, whether the
 * refusal comes while the tab is open or when it is reloaded.
 *
 * While signed in, `idle.ts` watches for activity in every tab. After the configured idle
 * time it signs out exactly as the Sign out button does, recorded with the reason
 * `idle_timeout`, with a warning 2 minutes before.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import { ErrorResponse, type User, type UserManager } from "oidc-client-ts";
import { useCallback, useEffect, useRef, useState } from "react";

import { type Api, ApiError, apiClient, call, type Me } from "./api/client";
import { App } from "./App";
import { accessToken, CALLBACK_PATH, logoutUrl, redirectTo, userManager } from "./auth";
import { type ActivePool, loadConfig, poolFor } from "./config";
import { messageFor, SIGN_IN_AGAIN } from "./errors";
import { formatCountdown, inOneTab, type IdleWatch, watchIdle } from "./idle";
import { type Session, SessionContext } from "./session";
import { type SignOutChannel, type SignOutReason, signOutChannel } from "./signout";
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

export const SIGNED_OUT_ELSEWHERE = "You signed out in another tab.";
// A session that could not be renewed: the refresh token expired (8 hours after the sign-in),
// was revoked, or was rotated out by another tab that shares it (a duplicated or restored
// tab; refresh-token rotation). The tab cannot tell which, so the message does not guess.
export const SESSION_ENDED = "Your session has ended. Sign in again to continue.";

/** Whether Cognito answered the refresh and refused it, as opposed to a request that got no
 * answer (offline, a timeout). Only a refusal means the stored tokens are dead. */
function refusedByCognito(error: unknown): boolean {
  return error instanceof ErrorResponse;
}
export const signedOutIdle = (minutes: number) =>
  `You were signed out after ${minutes} minutes without activity. Sign in again to continue.`;
// Set just before an idle sign-out leaves for Cognito's logout, so this tab's sign-in screen
// can say why when it comes back. sessionStorage, like the tokens; read once, then removed.
export const IDLE_NOTE_KEY = "assay-portal-idle-signed-out";
// A tab that timed out but did not sign out itself waits this long for the tab that did.
const SIGN_OUT_WAIT_MS = 10_000;

function takeIdleNote(): number | null {
  try {
    const value = window.sessionStorage.getItem(IDLE_NOTE_KEY);
    window.sessionStorage.removeItem(IDLE_NOTE_KEY);
    return value ? Number(value) : null;
  } catch {
    return null;
  }
}

/** Report a session's start. Never blocks or fails the sign-in. */
export async function reportSessionStart(client: Api): Promise<void> {
  await client.POST("/api/auth/session-started").catch(() => undefined);
}

// One portal per page: the signed-in user lives here, outside React's render, and the API
// client reads its access token on every request (the token renews in the background).
let currentUser: User | null = null;
const token = (): string | null => accessToken(currentUser);
const api = apiClient(token);

interface Started {
  pool: ActivePool;
  manager: UserManager;
  channel: SignOutChannel;
}

export function Root() {
  const [started, setStarted] = useState<Started | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  const loadMe = useCallback(
    async (user: User, manager: UserManager, justSignedIn = false) => {
      currentUser = user;
      try {
        const me = await call(api.GET("/api/me"));
        setPhase({ kind: "ready", user, me });
        if (justSignedIn) void reportSessionStart(api);
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
    let channel: SignOutChannel | null = null;
    void (async () => {
      try {
        const config = await loadConfig();
        const active = poolFor(config, window.location.host);
        const manager = userManager(active);
        manager.events.addUserLoaded((user) => {
          currentUser = user;
        });
        if (cancelled()) return;
        // Another tab signed out: end this tab's session too, without a second audit row
        // and without leaving the portal.
        channel = signOutChannel((reason) => {
          void (async () => {
            await manager.revokeTokens(["refresh_token"]).catch(() => undefined);
            await manager.removeUser().catch(() => undefined);
            currentUser = null;
            const note = reason === "idle_timeout" ? signedOutIdle(active.idleTimeoutMinutes) : SIGNED_OUT_ELSEWHERE;
            if (!cancelled()) setPhase({ kind: "signed-out", note });
          })();
        });
        // A session that cannot be renewed ends on the sign-in screen, with the reason: never
        // an error page, never the app left open over a dead token, and never a second try
        // (so no loop). Tokens Cognito refused are removed; after a request that got no
        // answer they are kept, so a reload can try again. No audit row: there is no valid
        // token left to report with. Other tabs are not told: only this tab's session ended.
        const endSession = async (error: unknown) => {
          if (refusedByCognito(error)) await manager.removeUser().catch(() => undefined);
          currentUser = null;
          if (!cancelled()) setPhase({ kind: "signed-out", note: SESSION_ENDED });
        };
        // The background refresh failed (the library does not try again). If Cognito refused
        // the refresh token, the session is over now. If the request got no answer, the
        // access token still has about a minute: wait for the expiry below.
        manager.events.addSilentRenewError((error) => {
          if (refusedByCognito(error)) void endSession(error);
        });
        // The access token ran out and nothing renewed it: one more try, then the sign-in
        // screen.
        manager.events.addAccessTokenExpired(() => {
          void manager.signinSilent().then(
            (renewed) => (renewed ? undefined : endSession(null)),
            (error: unknown) => endSession(error),
          );
        });
        setStarted({ pool: active, manager, channel });
        let user: User | null;
        let ended = false;
        const justSignedIn = window.location.pathname === CALLBACK_PATH;
        if (justSignedIn) {
          user = await manager.signinRedirectCallback();
          window.history.replaceState(null, "", "/");
        } else {
          user = await manager.getUser();
          if (user?.expired) {
            // A tab reloaded or restored after its access token ran out: refresh once.
            ended = true;
            const stored = user;
            user = null;
            if (stored.refresh_token) {
              try {
                user = await manager.signinSilent();
              } catch (error) {
                if (refusedByCognito(error)) await manager.removeUser().catch(() => undefined);
              }
            } else {
              await manager.removeUser().catch(() => undefined);
            }
          }
        }
        if (cancelled()) return;
        if (!user || user.expired) {
          const idle = takeIdleNote();
          const note = idle ? signedOutIdle(idle) : ended ? SESSION_ENDED : undefined;
          setPhase(note ? { kind: "signed-out", note } : { kind: "signed-out" });
          return;
        }
        await loadMe(user, manager, justSignedIn);
      } catch {
        if (!cancelled()) setPhase({ kind: "error", message: "The portal could not start. Reload the page." });
      }
    })();
    return () => {
      run.cancelled = true;
      channel?.close();
    };
  }, [loadMe]);

  const signIn = useCallback(() => {
    void started?.manager.signinRedirect();
  }, [started]);

  const signOutFor = useCallback(
    async (reason: SignOutReason) => {
      if (!started) return;
      const { manager, pool, channel } = started;
      const body =
        reason === "idle_timeout" ? { reason, idle_minutes: pool.idleTimeoutMinutes } : { reason };
      // Recorded first, while the token is still valid. A failure must not keep anyone in.
      await api.POST("/api/auth/signed-out", { body }).catch(() => undefined);
      channel.announce(reason);
      await manager.revokeTokens(["refresh_token"]).catch(() => undefined);
      await manager.removeUser();
      currentUser = null;
      if (reason === "idle_timeout") {
        try {
          window.sessionStorage.setItem(IDLE_NOTE_KEY, String(pool.idleTimeoutMinutes));
        } catch {
          // No note on the sign-in screen; the sign-out itself is unaffected.
        }
      }
      redirectTo(logoutUrl(pool));
    },
    [started],
  );
  const signOut = useCallback(() => signOutFor("user"), [signOutFor]);

  // The idle timeout, while signed in: a warning 2 minutes before, then a sign-out in exactly
  // one tab (the others hear it on the sign-out channel).
  const [idleDeadline, setIdleDeadline] = useState<number | null>(null);
  const [clock, setClock] = useState(0);
  const watch = useRef<IdleWatch | null>(null);
  const signedIn = phase.kind === "ready";
  useEffect(() => {
    if (!signedIn || !started) return;
    const { manager } = started;
    const watcher = watchIdle({
      timeoutMs: started.pool.idleTimeoutMinutes * 60_000,
      onWarn: (deadline) => {
        setClock(Date.now());
        setIdleDeadline(deadline);
      },
      onActive: () => setIdleDeadline(null),
      onTimeout: () => {
        setIdleDeadline(null);
        void inOneTab(() => signOutFor("idle_timeout")).then((ran) => {
          if (ran) return;
          // Another tab is signing out and will tell this one. If it never does, end this
          // tab's session alone (no second audit row).
          window.setTimeout(() => {
            if (!currentUser) return;
            void (async () => {
              await manager.revokeTokens(["refresh_token"]).catch(() => undefined);
              await manager.removeUser().catch(() => undefined);
              currentUser = null;
              setPhase({ kind: "signed-out", note: signedOutIdle(started.pool.idleTimeoutMinutes) });
            })();
          }, SIGN_OUT_WAIT_MS);
        });
      },
    });
    watch.current = watcher;
    return () => {
      watcher.stop();
      watch.current = null;
      setIdleDeadline(null);
    };
  }, [signedIn, started, signOutFor]);
  // Count the warning down.
  useEffect(() => {
    if (idleDeadline === null) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [idleDeadline]);

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
      <Modal
        visible={idleDeadline !== null}
        onDismiss={() => watch.current?.touch()}
        header="Are you still there?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => void signOut()}>
                Sign out now
              </Button>
              <Button variant="primary" onClick={() => watch.current?.touch()}>
                Stay signed in
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        You have not used the portal for a while. You will be signed out in{" "}
        <Box variant="strong" display="inline">
          {formatCountdown((idleDeadline ?? 0) - clock)}
        </Box>{" "}
        unless you stay signed in.
      </Modal>
    </SessionContext.Provider>
  );
}
