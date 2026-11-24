/**
 * Sign-in with Cognito managed login: authorization code flow with PKCE, public client.
 *
 * Tokens are kept in sessionStorage only (owner condition, build step 5): never
 * localStorage, never a cookie. They end with the browser tab. The PKCE state is kept there
 * too (oidc-client-ts would otherwise use localStorage for it).
 *
 * Sign-out: the portal records it (`POST /api/auth/signed-out`), revokes the refresh token,
 * clears the session and sends the browser to Cognito's logout endpoint, which ends the
 * managed login session and returns to the portal's root.
 */

import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";

import type { ActivePool } from "./config";

export const CALLBACK_PATH = "/auth/callback";
export const SCOPES = "openid email profile aws.cognito.signin.user.admin";

export function userManager(pool: ActivePool): UserManager {
  const origin = `https://${pool.host}`;
  const store = new WebStorageStateStore({ store: window.sessionStorage });
  return new UserManager({
    authority: `https://cognito-idp.${pool.region}.amazonaws.com/${pool.userPoolId}`,
    client_id: pool.clientId,
    redirect_uri: `${origin}${CALLBACK_PATH}`,
    post_logout_redirect_uri: `${origin}/`,
    response_type: "code",
    scope: SCOPES,
    userStore: store,
    stateStore: store,
    // Refresh with the refresh token before the access token expires (60 minutes).
    automaticSilentRenew: true,
    // Cognito's revocation endpoint takes refresh tokens only.
    revokeTokenTypes: ["refresh_token"],
    loadUserInfo: false,
  });
}

/** Cognito's logout endpoint (managed login), which the OIDC metadata does not list. */
export function logoutUrl(pool: ActivePool): string {
  const params = new URLSearchParams({
    client_id: pool.clientId,
    logout_uri: `https://${pool.host}/`,
  });
  return `${pool.loginUrl}/logout?${params.toString()}`;
}

/** Leave the portal for another page (Cognito's logout endpoint). */
export function redirectTo(url: string): void {
  window.location.assign(url);
}

export function accessToken(user: User | null): string | null {
  if (!user || user.expired) return null;
  return user.access_token;
}
