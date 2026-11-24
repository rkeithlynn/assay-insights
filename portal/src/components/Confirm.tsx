/** Destructive actions ask for confirmation (BUILD_SPEC.md section 11). */

import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ReactNode } from "react";

export interface ConfirmRequest {
  title: string;
  body: ReactNode;
  action: string;
  run: () => Promise<void>;
}

export function Confirm({
  request,
  busy,
  onCancel,
  onConfirm,
}: {
  request: ConfirmRequest | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal
      visible={request !== null}
      onDismiss={onCancel}
      header={request?.title}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy} onClick={onConfirm}>
              {request?.action}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      {request?.body}
    </Modal>
  );
}
