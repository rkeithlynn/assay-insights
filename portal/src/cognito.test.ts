import { afterEach, describe, expect, it, vi } from "vitest";

import { changePassword, CognitoError, cognitoMessage, totpUri } from "./cognito";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("cognito", () => {
  it("calls ChangePassword on Cognito itself, never the Assay Insights API", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await changePassword("us-east-1", "token", "old-password", "new-password");
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://cognito-idp.us-east-1.amazonaws.com/");
    expect((init.headers as Record<string, string>)["X-Amz-Target"]).toBe(
      "AWSCognitoIdentityProviderService.ChangePassword",
    );
  });

  it("turns Cognito's error type into a code and a plain message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ __type: "InvalidPasswordException", message: "x" }), { status: 400 }),
      ),
    );
    const error = await changePassword("us-east-1", "t", "a", "b").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CognitoError);
    expect((error as CognitoError).code).toBe("InvalidPasswordException");
    expect(cognitoMessage(error)).toMatch(/password rules/);
  });

  it("builds an otpauth URI with the issuer", () => {
    expect(totpUri("ABC", "ada@example.test")).toBe(
      "otpauth://totp/Assay%20Insights%3Aada%40example.test?secret=ABC&issuer=Assay+Insights",
    );
  });
});
