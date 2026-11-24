import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const cognito = vi.hoisted(() => ({
  associateSoftwareToken: vi.fn(),
  verifySoftwareToken: vi.fn(),
}));
vi.mock("../cognito", async (actual) => ({
  ...(await actual<typeof import("../cognito")>()),
  associateSoftwareToken: cognito.associateSoftwareToken,
  verifySoftwareToken: cognito.verifySoftwareToken,
}));

import { cognitoMessage } from "../cognito";
import { type Session, SessionContext } from "../session";
import { MfaSetup } from "./MfaSetup";

function renderSetup() {
  const post = vi.fn(() => Promise.resolve({ response: { ok: true, status: 204 } }));
  const session = {
    pool: { region: "us-east-1" },
    token: () => "access-token",
    me: { user: { email: "ada@example.test" }, tenant: { name: "Example Tenant" } },
    signOut: () => Promise.resolve(),
    api: { POST: post },
  } as unknown as Session;
  render(
    <SessionContext.Provider value={session}>
      <MfaSetup />
    </SessionContext.Provider>,
  );
  return post;
}

async function enterCode() {
  await userEvent.click(screen.getByRole("button", { name: "Set up authenticator" }));
  await userEvent.type(await screen.findByRole("textbox"), "123456");
  await userEvent.click(screen.getByRole("button", { name: "Verify" }));
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("MFA setup", () => {
  it("reports the enrolment after Cognito accepts the code", async () => {
    cognito.associateSoftwareToken.mockResolvedValue("SECRETSECRET");
    cognito.verifySoftwareToken.mockResolvedValue(undefined);
    const post = renderSetup();
    await enterCode();
    expect(await screen.findByText("Multi-factor authentication is on")).toBeInTheDocument();
    expect(post).toHaveBeenCalledExactlyOnceWith("/api/auth/mfa-enabled");
    // The report goes to the API only; the secret and the code never do.
    expect(JSON.stringify(post.mock.calls)).not.toContain("SECRETSECRET");
    expect(JSON.stringify(post.mock.calls)).not.toContain("123456");
  });

  it("reports nothing when Cognito refuses the code", async () => {
    cognito.associateSoftwareToken.mockResolvedValue("SECRETSECRET");
    const refusal = new Error("EnableSoftwareTokenMFAException");
    cognito.verifySoftwareToken.mockRejectedValue(refusal);
    const post = renderSetup();
    await enterCode();
    expect(await screen.findByText(cognitoMessage(refusal))).toBeInTheDocument();
    expect(screen.queryByText("Multi-factor authentication is on")).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("finishes the setup even if the report fails", async () => {
    cognito.associateSoftwareToken.mockResolvedValue("SECRETSECRET");
    cognito.verifySoftwareToken.mockResolvedValue(undefined);
    const post = renderSetup();
    post.mockImplementation(() => Promise.reject(new TypeError("network down")));
    await enterCode();
    expect(await screen.findByText("Multi-factor authentication is on")).toBeInTheDocument();
  });
});
