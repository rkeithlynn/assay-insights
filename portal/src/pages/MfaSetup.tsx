/**
 * TOTP setup for a tenant-admin without MFA (BUILD_SPEC.md sections 4 and 13). The tenant
 * pool's MFA is optional, and managed login does not prompt users to set it up in that
 * case, so the portal does: it shows nothing else until MFA is on. The QR code is drawn
 * in the browser; the secret goes nowhere but Cognito and the user's authenticator app.
 *
 * Once verified, the portal reports it (`POST /api/auth/mfa-enabled`); the API records it only
 * if Cognito confirms TOTP is on. A failed report never holds the user up. Then the user signs
 * in again: the new session's token says MFA was used.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import qrcode from "qrcode-generator";
import { useCallback, useMemo, useState } from "react";

import { associateSoftwareToken, cognitoMessage, totpUri, verifySoftwareToken } from "../cognito";
import { useSession } from "../session";

/** The QR code as an SVG data URL (no network, no innerHTML). */
export function qrDataUrl(text: string): string {
  const code = qrcode(0, "M");
  code.addData(text);
  code.make();
  return code.createDataURL(6, 2);
}

export function MfaSetup() {
  const { pool, token, me, signOut, api } = useSession();
  const [secret, setSecret] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const qr = useMemo(() => (secret ? qrDataUrl(totpUri(secret, me.user.email)) : null), [secret, me.user.email]);

  const start = useCallback(async () => {
    const accessToken = token();
    if (!accessToken) return;
    setBusy(true);
    setError(null);
    try {
      setSecret(await associateSoftwareToken(pool.region, accessToken));
    } catch (caught) {
      setError(cognitoMessage(caught));
    } finally {
      setBusy(false);
    }
  }, [pool.region, token]);

  const verify = useCallback(async () => {
    const accessToken = token();
    if (!accessToken) return;
    setBusy(true);
    setError(null);
    try {
      await verifySoftwareToken(pool.region, accessToken, code.trim());
      await api.POST("/api/auth/mfa-enabled").catch(() => undefined);
      setDone(true);
    } catch (caught) {
      setError(cognitoMessage(caught));
    } finally {
      setBusy(false);
    }
  }, [api, code, pool.region, token]);

  return (
    <Box padding={{ vertical: "xxl", horizontal: "l" }}>
      <div style={{ maxWidth: 640, margin: "0 auto" }}>
        <ContentLayout
          header={
            <Header variant="h1" description={`Signed in as ${me.user.email}, ${me.tenant.name}`}>
              Set up multi-factor authentication
            </Header>
          }
        >
          <Container>
            <SpaceBetween size="l">
              {error && <Alert type="error">{error}</Alert>}
              {done ? (
                <>
                  <Alert type="success" header="Multi-factor authentication is on">
                    Sign in again to continue. From now on you will be asked for a code from your authenticator app
                    each time you sign in.
                  </Alert>
                  <Button variant="primary" onClick={() => void signOut()}>
                    Sign in again
                  </Button>
                </>
              ) : !secret ? (
                <>
                  <Box>
                    As an admin you need an authenticator app (for example Microsoft Authenticator, Google Authenticator
                    or 1Password) before you can use the portal.
                  </Box>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="primary" loading={busy} onClick={() => void start()}>
                      Set up authenticator
                    </Button>
                    <Button variant="link" onClick={() => void signOut()}>
                      Sign out
                    </Button>
                  </SpaceBetween>
                </>
              ) : (
                <>
                  <Box>1. Scan this code with your authenticator app.</Box>
                  {qr && <img src={qr} alt="QR code for your authenticator app" width={220} height={220} />}
                  <ExpandableSection headerText="Can't scan the code?">
                    <Box variant="code">{secret}</Box>
                  </ExpandableSection>
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      void verify();
                    }}
                  >
                    <FormField label="2. Enter the 6-digit code the app shows">
                      <SpaceBetween direction="horizontal" size="xs">
                        <Input
                          value={code}
                          inputMode="numeric"
                          autoComplete="one-time-code"
                          onChange={({ detail }) => setCode(detail.value)}
                        />
                        <Button variant="primary" loading={busy} disabled={!/^\d{6}$/.test(code.trim())}>
                          Verify
                        </Button>
                      </SpaceBetween>
                    </FormField>
                  </form>
                </>
              )}
            </SpaceBetween>
          </Container>
        </ContentLayout>
      </div>
    </Box>
  );
}
