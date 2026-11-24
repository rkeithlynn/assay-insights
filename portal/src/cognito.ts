/**
 * Cognito user API calls made with the signed-in user's own access token (scope
 * aws.cognito.signin.user.admin). They go straight to Cognito, so a password never passes
 * through the Assay Insights API (owner decision D3, build step 5).
 *
 * - ChangePassword (user menu), followed by `POST /api/auth/password-changed` for the audit.
 * - AssociateSoftwareToken, VerifySoftwareToken and SetUserMFAPreference: TOTP setup, which
 *   managed login does not offer when the pool's MFA is optional (the tenant pool).
 */

export class CognitoError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CognitoError";
  }
}

type Body = Record<string, unknown>;

async function cognito(region: string, operation: string, body: Body): Promise<Body> {
  const response = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${operation}`,
    },
    body: JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => ({}))) as Body;
  if (!response.ok) {
    const type = typeof payload.__type === "string" ? payload.__type : "UnknownError";
    const code = type.split("#").pop() ?? type;
    throw new CognitoError(code, typeof payload.message === "string" ? payload.message : code);
  }
  return payload;
}

export async function changePassword(
  region: string,
  accessToken: string,
  previous: string,
  proposed: string,
): Promise<void> {
  await cognito(region, "ChangePassword", {
    AccessToken: accessToken,
    PreviousPassword: previous,
    ProposedPassword: proposed,
  });
}

/** Starts TOTP setup; returns the shared secret to show as a QR code. */
export async function associateSoftwareToken(region: string, accessToken: string): Promise<string> {
  const result = await cognito(region, "AssociateSoftwareToken", { AccessToken: accessToken });
  if (typeof result.SecretCode !== "string") throw new CognitoError("NoSecret", "No secret returned");
  return result.SecretCode;
}

export async function verifySoftwareToken(
  region: string,
  accessToken: string,
  code: string,
): Promise<void> {
  const result = await cognito(region, "VerifySoftwareToken", {
    AccessToken: accessToken,
    UserCode: code,
    FriendlyDeviceName: "Assay Insights",
  });
  if (result.Status !== "SUCCESS") throw new CognitoError("CodeMismatchException", "Wrong code");
  await cognito(region, "SetUserMFAPreference", {
    AccessToken: accessToken,
    SoftwareTokenMfaSettings: { Enabled: true, PreferredMfa: true },
  });
}

/** The otpauth URI an authenticator app scans. */
export function totpUri(secret: string, account: string): string {
  const issuer = "Assay Insights";
  const label = encodeURIComponent(`${issuer}:${account}`);
  const params = new URLSearchParams({ secret, issuer });
  return `otpauth://totp/${label}?${params.toString()}`;
}

export function cognitoMessage(error: unknown): string {
  if (!(error instanceof CognitoError)) return "Something went wrong. Try again.";
  switch (error.code) {
    case "NotAuthorizedException":
      return "The current password is wrong, or your session has ended.";
    case "InvalidPasswordException":
      return "The new password does not meet the password rules: at least 12 characters with upper and lower case letters, a number and a symbol, and not one of your last 5.";
    case "LimitExceededException":
    case "TooManyRequestsException":
      return "Too many attempts. Wait a few minutes and try again.";
    case "CodeMismatchException":
    case "EnableSoftwareTokenMFAException":
      return "That code did not match. Check the time on your device and try the next code.";
    default:
      return "Something went wrong. Try again.";
  }
}
