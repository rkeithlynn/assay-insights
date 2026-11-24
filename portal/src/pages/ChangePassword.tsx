/**
 * Change password (user menu, every role). The new password goes straight to Cognito with
 * the user's access token; then the API records `password_changed` (section 7a).
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useState } from "react";

import { changePassword, cognitoMessage } from "../cognito";
import { useSession } from "../session";

export function ChangePassword({ visible, onDismiss }: { visible: boolean; onDismiss: () => void }) {
  const { pool, token, api } = useSession();
  const [current, setCurrent] = useState("");
  const [proposed, setProposed] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const close = () => {
    setCurrent("");
    setProposed("");
    setConfirm("");
    setError(null);
    setDone(false);
    onDismiss();
  };

  const mismatch = confirm.length > 0 && proposed !== confirm;

  const submit = async () => {
    const accessToken = token();
    if (!accessToken) {
      setError("Your session has ended. Sign in again.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await changePassword(pool.region, accessToken, current, proposed);
      await api.POST("/api/auth/password-changed").catch(() => undefined);
      setDone(true);
    } catch (caught) {
      setError(cognitoMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={close}
      header="Change password"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={close}>
              {done ? "Close" : "Cancel"}
            </Button>
            {!done && (
              <Button
                variant="primary"
                loading={busy}
                disabled={!current || !proposed || proposed !== confirm}
                onClick={() => void submit()}
              >
                Change password
              </Button>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      {done ? (
        <Alert type="success">Your password has been changed.</Alert>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <Form errorText={error ?? undefined}>
            <SpaceBetween size="m">
              <FormField label="Current password">
                <Input type="password" value={current} onChange={({ detail }) => setCurrent(detail.value)} />
              </FormField>
              <FormField
                label="New password"
                constraintText="At least 12 characters, with upper and lower case letters, a number and a symbol."
              >
                <Input type="password" value={proposed} onChange={({ detail }) => setProposed(detail.value)} />
              </FormField>
              <FormField label="Confirm new password" errorText={mismatch ? "The passwords do not match." : undefined}>
                <Input type="password" value={confirm} onChange={({ detail }) => setConfirm(detail.value)} />
              </FormField>
            </SpaceBetween>
          </Form>
        </form>
      )}
    </Modal>
  );
}
