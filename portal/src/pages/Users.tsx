/**
 * Users (tenant-admin; BUILD_SPEC.md sections 10 and 11). Table: name, email, role, status,
 * last sign-in. Invite dialog; row actions for role, disable or enable, resend invitation,
 * reset password and reset MFA (tenant users only).
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import type { ButtonDropdownProps } from "@cloudscape-design/components/button-dropdown";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useCallback, useState } from "react";

import { call, type UserOut } from "../api/client";
import { Confirm, type ConfirmRequest } from "../components/Confirm";
import { RowActions } from "../components/RowActions";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";

type Role = UserOut["role"];

const ROLE_LABELS: Record<Role, string> = {
  "tenant-admin": "Admin",
  "tenant-user": "User",
};

const STATUS: Record<UserOut["status"], { type: "success" | "pending" | "stopped"; label: string }> = {
  active: { type: "success", label: "Active" },
  invited: { type: "pending", label: "Invited" },
  disabled: { type: "stopped", label: "Disabled" },
};

export function formatTime(value: string | null | undefined): string {
  if (!value) return "Never";
  return new Date(value).toLocaleString();
}

/** The actions a tenant-admin may take on this user (the API enforces each one). */
export function actionsFor(user: UserOut, selfId: string): ButtonDropdownProps.Item[] {
  const self = user.id === selfId;
  const items: ButtonDropdownProps.Item[] = [];
  if (!self) {
    items.push(
      user.role === "tenant-user"
        ? { id: "make-admin", text: "Make admin" }
        : { id: "make-user", text: "Make user" },
    );
    items.push(user.status === "disabled" ? { id: "enable", text: "Enable" } : { id: "disable", text: "Disable" });
  }
  if (user.status === "invited") items.push({ id: "resend", text: "Resend invitation" });
  if (user.status === "active" && !self) items.push({ id: "reset-password", text: "Reset password" });
  if (user.role === "tenant-user") items.push({ id: "reset-mfa", text: "Reset MFA" });
  return items;
}

function InviteDialog({
  visible,
  onDismiss,
  onInvited,
}: {
  visible: boolean;
  onDismiss: () => void;
  onInvited: (user: UserOut) => void;
}) {
  const { api } = useSession();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("tenant-user");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setEmail("");
    setRole("tenant-user");
    setError(null);
  };
  const emailValid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim());

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const user = await call(
        api.POST("/api/users", { body: { name: name.trim(), email: email.trim(), role } }),
      );
      reset();
      onInvited(user);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={() => {
        reset();
        onDismiss();
      }}
      header="Invite a user"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="link"
              onClick={() => {
                reset();
                onDismiss();
              }}
            >
              Cancel
            </Button>
            <Button variant="primary" loading={busy} disabled={!name.trim() || !emailValid} onClick={() => void submit()}>
              Send invitation
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={error ?? undefined}>
        <SpaceBetween size="m">
          <FormField label="Name">
            <Input value={name} onChange={({ detail }) => setName(detail.value)} />
          </FormField>
          <FormField
            label="Email"
            description="Cognito emails the invitation with a temporary password, valid for 7 days."
            errorText={email && !emailValid ? "Enter an email address." : undefined}
          >
            <Input type="email" value={email} onChange={({ detail }) => setEmail(detail.value)} />
          </FormField>
          <FormField label="Role">
            <Select
              selectedOption={{ value: role, label: ROLE_LABELS[role] }}
              options={[
                { value: "tenant-user", label: ROLE_LABELS["tenant-user"], description: "Ask, ingestion history" },
                { value: "tenant-admin", label: ROLE_LABELS["tenant-admin"], description: "Also uploads, users, settings" },
              ]}
              onChange={({ detail }) => setRole(detail.selectedOption.value as Role)}
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

export function Users() {
  const { api, me } = useSession();
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [inviting, setInviting] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchUsers = useCallback(async () => (await call(api.GET("/api/users"))).users, [api]);
  const { data: users, error: loadError, reload: load } = useLoad(fetchUsers);

  const run = useCallback(
    async (work: () => Promise<unknown>, success: string) => {
      setBusy(true);
      try {
        await work();
        setNotice({ type: "success", text: success });
        await load();
      } catch (caught) {
        setNotice({ type: "error", text: messageFor(caught) });
      } finally {
        setBusy(false);
        setConfirm(null);
      }
    },
    [load],
  );

  const onAction = (user: UserOut, id: string) => {
    const path = { params: { path: { user_id: user.id } } };
    const who = `${user.name} (${user.email})`;
    switch (id) {
      case "make-admin":
      case "make-user": {
        const role: Role = id === "make-admin" ? "tenant-admin" : "tenant-user";
        setConfirm({
          title: id === "make-admin" ? "Make admin" : "Make user",
          body: `Change ${who} to ${ROLE_LABELS[role]}? It takes effect when they next sign in.`,
          action: "Change role",
          run: () => run(() => call(api.PATCH("/api/users/{user_id}", { ...path, body: { role } })), "Role changed."),
        });
        return;
      }
      case "disable":
        setConfirm({
          title: "Disable user",
          body: `Disable ${who}? They are signed out at once and cannot sign in until enabled.`,
          action: "Disable",
          run: () =>
            run(() => call(api.PATCH("/api/users/{user_id}", { ...path, body: { status: "disabled" } })), "User disabled."),
        });
        return;
      case "enable":
        void run(() => call(api.PATCH("/api/users/{user_id}", { ...path, body: { status: "active" } })), "User enabled.");
        return;
      case "resend":
        void run(() => call(api.POST("/api/users/{user_id}/resend-invite", path)), "Invitation sent again.");
        return;
      case "reset-password":
        setConfirm({
          title: "Reset password",
          body: `Send ${who} a code to set a new password? Their current password stops working.`,
          action: "Reset password",
          run: () => run(() => call(api.POST("/api/users/{user_id}/reset-password", path)), "Password reset sent."),
        });
        return;
      case "reset-mfa":
        setConfirm({
          title: "Reset MFA",
          body: `Remove ${who}'s authenticator and sign them out everywhere? They set it up again at their next sign-in.`,
          action: "Reset MFA",
          run: () => run(() => call(api.POST("/api/users/{user_id}/reset-mfa", path)), "MFA reset."),
        });
        return;
    }
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Invite people to your organization and manage their access."
          actions={
            <Button variant="primary" onClick={() => setInviting(true)}>
              Invite user
            </Button>
          }
        >
          Users
        </Header>
      }
    >
      <SpaceBetween size="m">
        {notice && (
          <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
            {notice.text}
          </Alert>
        )}
        {loadError ? (
          <Alert type="error" action={<Button onClick={() => void load()}>Retry</Button>}>
            {loadError}
          </Alert>
        ) : (
          <Table
            loading={users === null}
            loadingText="Loading users"
            items={users ?? []}
            trackBy="id"
            variant="container"
            header={<Header counter={users ? `(${users.length})` : undefined}>Users</Header>}
            empty={<Box textAlign="center">No users yet.</Box>}
            columnDefinitions={[
              { id: "name", header: "Name", cell: (u) => u.name, isRowHeader: true },
              { id: "email", header: "Email", cell: (u) => u.email },
              { id: "role", header: "Role", cell: (u) => ROLE_LABELS[u.role] },
              {
                id: "status",
                header: "Status",
                cell: (u) => <StatusIndicator type={STATUS[u.status].type}>{STATUS[u.status].label}</StatusIndicator>,
              },
              { id: "last", header: "Last sign-in", cell: (u) => formatTime(u.last_sign_in_at) },
              {
                id: "actions",
                header: "Actions",
                cell: (u) => {
                  const items = actionsFor(u, me.user.id);
                  return items.length ? (
                    <RowActions
                      items={items}
                      ariaLabel={`Actions for ${u.name}`}
                      disabled={busy}
                      onItemClick={(id) => onAction(u, id)}
                    />
                  ) : null;
                },
              },
            ]}
          />
        )}
      </SpaceBetween>
      <InviteDialog
        visible={inviting}
        onDismiss={() => setInviting(false)}
        onInvited={(user) => {
          setInviting(false);
          setNotice({ type: "success", text: `Invitation sent to ${user.email}.` });
          void load();
        }}
      />
      <Confirm
        request={confirm}
        busy={busy}
        onCancel={() => setConfirm(null)}
        onConfirm={() => void confirm?.run()}
      />
    </ContentLayout>
  );
}
