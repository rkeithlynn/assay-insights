/**
 * Tenants (master-admin; read-only for the master user. BUILD_SPEC.md sections 5 and 11).
 * Table: name, kind, status, users, documents ingested, created. Add tenant dialog; live
 * status while provisioning; Retry on failure; Suspend and Reactivate. A tenant's detail
 * lists its admins with Reset MFA (hidden for the master user). No tenant content is shown.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useCallback, useEffect, useState } from "react";

import { type AdminOut, call, type TenantOut } from "../api/client";
import { Confirm, type ConfirmRequest } from "../components/Confirm";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";
import { ModelsPanel } from "./ModelsPanel";

const POLL_MS = 5000;

const STATUS: Record<TenantOut["status"], { type: StatusIndicatorProps.Type; label: string }> = {
  provisioning: { type: "in-progress", label: "Provisioning" },
  active: { type: "success", label: "Active" },
  suspended: { type: "stopped", label: "Suspended" },
  failed: { type: "error", label: "Failed" },
};

const ROLE_LABELS: Record<AdminOut["role"], string> = {
  "tenant-admin": "Tenant admin",
  "master-admin": "Master admin",
  "master-user": "Master user",
};

/** What the master admin may do to this tenant now. */
export function tenantActions(tenant: TenantOut): ("retry" | "suspend" | "reactivate")[] {
  if (tenant.kind !== "business") return [];
  if (tenant.status === "failed") return ["retry"];
  if (tenant.status === "active") return ["suspend"];
  if (tenant.status === "suspended") return ["reactivate"];
  return [];
}

function AddTenantDialog({
  visible,
  onDismiss,
  onAdded,
}: {
  visible: boolean;
  onDismiss: () => void;
  onAdded: (tenant: TenantOut) => void;
}) {
  const { api } = useSession();
  const [name, setName] = useState("");
  const [adminName, setAdminName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emailValid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(adminEmail.trim());

  const close = () => {
    setName("");
    setAdminName("");
    setAdminEmail("");
    setError(null);
    onDismiss();
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const tenant = await call(
        api.POST("/api/admin/tenants", {
          body: { name: name.trim(), admin_name: adminName.trim(), admin_email: adminEmail.trim() },
        }),
      );
      setName("");
      setAdminName("");
      setAdminEmail("");
      onAdded(tenant);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={close}
      header="Add a tenant"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={close}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={busy}
              disabled={!name.trim() || !adminName.trim() || !emailValid}
              onClick={() => void submit()}
            >
              Add tenant
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={error ?? undefined}>
        <SpaceBetween size="m">
          <FormField label="Legal name">
            <Input value={name} onChange={({ detail }) => setName(detail.value)} />
          </FormField>
          <FormField label="First admin's name">
            <Input value={adminName} onChange={({ detail }) => setAdminName(detail.value)} />
          </FormField>
          <FormField
            label="First admin's email"
            description="Invited by email once the tenant's storage is ready (a few minutes)."
            errorText={adminEmail && !emailValid ? "Enter an email address." : undefined}
          >
            <Input type="email" value={adminEmail} onChange={({ detail }) => setAdminEmail(detail.value)} />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

function TenantDetail({ tenant, canReset }: { tenant: TenantOut; canReset: boolean }) {
  const { api } = useSession();
  const [actionError, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);

  // The parent keys this component by tenant, so a new tenant starts with fresh state.
  const fetchAdmins = useCallback(
    async () =>
      (
        await call(
          api.GET("/api/admin/tenants/{tenant_id}/admins", { params: { path: { tenant_id: tenant.id } } }),
        )
      ).admins,
    [api, tenant.id],
  );
  const { data: admins, error: loadError } = useLoad(fetchAdmins);
  const error = actionError ?? loadError;

  const resetMfa = (admin: AdminOut) => {
    setConfirm({
      title: "Reset MFA",
      body: `Remove the authenticator of ${admin.name} (${admin.email}) and sign them out everywhere? They set it up again at their next sign-in.`,
      action: "Reset MFA",
      run: async () => {
        setBusy(true);
        try {
          await call(api.POST("/api/admin/users/{user_id}/reset-mfa", { params: { path: { user_id: admin.id } } }));
          setNotice(`MFA reset for ${admin.email}.`);
        } catch (caught) {
          setError(messageFor(caught));
        } finally {
          setBusy(false);
          setConfirm(null);
        }
      },
    });
  };

  return (
    <Container header={<Header variant="h2">{tenant.kind === "platform" ? "Platform users" : "Admins"}</Header>}>
      <SpaceBetween size="m">
        {notice && (
          <Alert type="success" dismissible onDismiss={() => setNotice(null)}>
            {notice}
          </Alert>
        )}
        {error && <Alert type="error">{error}</Alert>}
        <Table
          variant="embedded"
          loading={admins === null && !error}
          loadingText="Loading"
          items={admins ?? []}
          trackBy="id"
          empty={<Box textAlign="center">No admins yet.</Box>}
          columnDefinitions={[
            { id: "name", header: "Name", cell: (a) => a.name, isRowHeader: true },
            { id: "email", header: "Email", cell: (a) => a.email },
            { id: "role", header: "Role", cell: (a) => ROLE_LABELS[a.role] },
            { id: "status", header: "Status", cell: (a) => a.status },
            ...(canReset
              ? [
                  {
                    id: "actions",
                    header: "Actions",
                    cell: (a: AdminOut) =>
                      a.mfa_resettable ? (
                        <Button variant="inline-link" disabled={busy} onClick={() => resetMfa(a)}>
                          Reset MFA
                        </Button>
                      ) : null,
                  },
                ]
              : []),
          ]}
        />
      </SpaceBetween>
      <Confirm request={confirm} busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void confirm?.run()} />
    </Container>
  );
}

export function Tenants() {
  const { api, me } = useSession();
  const isMasterAdmin = me.role === "master-admin";
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchTenants = useCallback(async () => (await call(api.GET("/api/admin/tenants"))).tenants, [api]);
  const { data: tenants, error: loadError, reload: load } = useLoad(fetchTenants);
  const selected = tenants?.find((t) => t.id === selectedId) ?? null;

  // Live status while any tenant is being provisioned.
  const provisioning = tenants?.some((t) => t.status === "provisioning") ?? false;
  useEffect(() => {
    if (!provisioning) return;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [provisioning, load]);

  const change = (tenant: TenantOut, action: "retry" | "suspend" | "reactivate") => {
    const path = { params: { path: { tenant_id: tenant.id } } };
    const work =
      action === "retry"
        ? () => call(api.POST("/api/admin/tenants/{tenant_id}/retry", path))
        : () =>
            call(
              api.PATCH("/api/admin/tenants/{tenant_id}", {
                ...path,
                body: { status: action === "suspend" ? "suspended" : "active" },
              }),
            );
    const labels = {
      retry: { title: "Retry provisioning", action: "Retry", done: "Provisioning restarted." },
      suspend: { title: "Suspend tenant", action: "Suspend", done: "Tenant suspended." },
      reactivate: { title: "Reactivate tenant", action: "Reactivate", done: "Tenant reactivated." },
    }[action];
    const body =
      action === "suspend"
        ? `Suspend ${tenant.name}? Its users are refused at once and cannot upload or use the portal until it is reactivated.`
        : action === "retry"
          ? `Run provisioning for ${tenant.name} again? Every step can be repeated.`
          : `Reactivate ${tenant.name}? Its users can use the portal again.`;
    setConfirm({
      title: labels.title,
      body,
      action: labels.action,
      run: async () => {
        setBusy(true);
        try {
          await work();
          setNotice({ type: "success", text: labels.done });
          await load();
        } catch (caught) {
          setNotice({ type: "error", text: messageFor(caught) });
        } finally {
          setBusy(false);
          setConfirm(null);
        }
      },
    });
  };

  const actions = selected && isMasterAdmin ? tenantActions(selected) : [];

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={isMasterAdmin ? "Add tenants and manage their status." : "Read-only view of every tenant."}
          actions={
            isMasterAdmin ? (
              <Button variant="primary" onClick={() => setAdding(true)}>
                Add tenant
              </Button>
            ) : undefined
          }
        >
          Tenants
        </Header>
      }
    >
      <SpaceBetween size="l">
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
            loading={tenants === null}
            loadingText="Loading tenants"
            items={tenants ?? []}
            trackBy="id"
            variant="container"
            selectionType="single"
            selectedItems={selected ? [selected] : []}
            onSelectionChange={({ detail }) => setSelectedId(detail.selectedItems[0]?.id ?? null)}
            ariaLabels={{
              selectionGroupLabel: "Tenants",
              itemSelectionLabel: (_, item) => item.name,
            }}
            header={
              <Header
                counter={tenants ? `(${tenants.length})` : undefined}
                actions={
                  actions.length ? (
                    <SpaceBetween direction="horizontal" size="xs">
                      {actions.map((action) => (
                        <Button key={action} disabled={busy} onClick={() => selected && change(selected, action)}>
                          {action === "retry" ? "Retry" : action === "suspend" ? "Suspend" : "Reactivate"}
                        </Button>
                      ))}
                    </SpaceBetween>
                  ) : undefined
                }
              >
                Tenants
              </Header>
            }
            empty={<Box textAlign="center">No tenants yet.</Box>}
            columnDefinitions={[
              { id: "name", header: "Name", cell: (t) => t.name, isRowHeader: true },
              { id: "kind", header: "Kind", cell: (t) => (t.kind === "platform" ? "Platform" : "Business") },
              {
                id: "status",
                header: "Status",
                cell: (t) => <StatusIndicator type={STATUS[t.status].type}>{STATUS[t.status].label}</StatusIndicator>,
              },
              { id: "users", header: "Users", cell: (t) => t.users },
              { id: "documents", header: "Documents ingested", cell: (t) => t.documents },
              { id: "created", header: "Created", cell: (t) => new Date(t.created_at).toLocaleDateString() },
            ]}
          />
        )}
        {selected && <TenantDetail key={selected.id} tenant={selected} canReset={isMasterAdmin} />}
        {selected?.kind === "business" && (
          <ModelsPanel key={`models-${selected.id}`} tenant={selected} canEdit={isMasterAdmin} />
        )}
      </SpaceBetween>
      <AddTenantDialog
        visible={adding}
        onDismiss={() => setAdding(false)}
        onAdded={(tenant) => {
          setAdding(false);
          setNotice({ type: "success", text: `${tenant.name} is being provisioned.` });
          void load();
        }}
      />
      <Confirm request={confirm} busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void confirm?.run()} />
    </ContentLayout>
  );
}
