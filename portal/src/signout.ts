/**
 * Sign-out across the portal's tabs in one browser (owner decision, build step 5).
 *
 * Tokens live in each tab's sessionStorage, so every tab has its own sign-in and its own
 * refresh token, and one tab's sign-out used to leave the others signed in. Now a tab that
 * signs out says so on a BroadcastChannel; every other open tab of the same portal (same
 * origin, so the tenant and admin portals never hear each other) revokes its own refresh
 * token, clears its tokens and shows the sign-in screen. Nothing reaches other browsers or
 * devices; nothing ends a Cognito session anywhere but in this browser.
 *
 * No storage-event fallback: that event fires across tabs only for localStorage, which the
 * portal never uses (tokens in sessionStorage only), and BroadcastChannel is in every
 * browser the build targets (Vite's baseline-widely-available). Where it is missing, a tab
 * signs out alone, as before.
 *
 * A tab that is closed or offline when the message is sent never hears it, and keeps its
 * refresh token until it expires or an admin action (disable, MFA reset) revokes it.
 *
 * The message carries no data. Each tab keeps one channel, which it both sends and listens
 * on: a channel does not deliver its own messages to itself, so the tab that signs out never
 * reacts to its own announcement.
 */

export const SIGN_OUT_CHANNEL = "assay-portal-sign-out";
const SIGNED_OUT = "signed-out";

export interface SignOutChannel {
  /** Tell the other tabs that this one signed out. */
  announce(): void;
  close(): void;
}

export function signOutChannel(onSignedOutElsewhere: () => void): SignOutChannel {
  if (typeof BroadcastChannel === "undefined") {
    return { announce: () => undefined, close: () => undefined };
  }
  const channel = new BroadcastChannel(SIGN_OUT_CHANNEL);
  channel.onmessage = (event: MessageEvent<unknown>) => {
    const data = event.data as { type?: unknown } | null;
    if (data?.type === SIGNED_OUT) onSignedOutElsewhere();
  };
  return {
    announce: () => {
      try {
        channel.postMessage({ type: SIGNED_OUT });
      } catch {
        // A closed channel: nothing to tell.
      }
    },
    close: () => channel.close(),
  };
}
