import { User } from "oidc-client-ts";
import { afterEach, describe, expect, it } from "vitest";

import { logoutUrl, userManager } from "./auth";
import type { ActivePool } from "./config";

const POOL: ActivePool = {
  kind: "tenant",
  region: "us-east-1",
  host: "portal.example.test",
  userPoolId: "us-east-1_T",
  clientId: "tenant-client",
  loginUrl: "https://login.example.test",
  idleTimeoutMinutes: 30,
};

afterEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("auth", () => {
  it("uses the authorization code flow with the pool's callback and the user admin scope", () => {
    const settings = userManager(POOL).settings;
    expect(settings.response_type).toBe("code");
    expect(settings.redirect_uri).toBe("https://portal.example.test/auth/callback");
    expect(settings.authority).toBe("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_T");
    expect(settings.scope.split(" ")).toContain("aws.cognito.signin.user.admin");
  });

  it("keeps tokens in sessionStorage, never localStorage", async () => {
    const manager = userManager(POOL);
    const user = new User({
      access_token: "access-token-value",
      refresh_token: "refresh-token-value",
      token_type: "Bearer",
      profile: { sub: "s", iss: "i", aud: "a", exp: 0, iat: 0 },
      expires_at: Math.floor(Date.now() / 1000) + 3600,
    });
    await manager.storeUser(user);
    const stored = Object.keys(window.sessionStorage).map((k) => window.sessionStorage.getItem(k) ?? "");
    expect(stored.join()).toContain("access-token-value");
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).not.toContain("token");
  });

  it("signs out through Cognito's logout endpoint back to the portal", () => {
    expect(logoutUrl(POOL)).toBe(
      "https://login.example.test/logout?client_id=tenant-client&logout_uri=https%3A%2F%2Fportal.example.test%2F",
    );
  });
});
