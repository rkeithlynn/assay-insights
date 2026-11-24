/**
 * The Models panel on a tenant's page (BUILD_SPEC.md sections 7b and 11): two checklists,
 * Ingest and AI query, of catalog models. The master admin changes them; the master user
 * sees them read-only. Each job keeps at least one model (the API enforces it too).
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback, useState } from "react";

import { call, type TenantModels, type TenantOut } from "../api/client";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";
import { price } from "./Settings";

type Job = "ingest" | "query";
const JOBS: { job: Job; label: string }[] = [
  { job: "ingest", label: "Ingest" },
  { job: "query", label: "AI query" },
];

export function ModelsPanel({ tenant, canEdit }: { tenant: TenantOut; canEdit: boolean }) {
  const { api } = useSession();
  const fetchModels = useCallback(
    async (): Promise<TenantModels> =>
      call(api.GET("/api/admin/tenants/{tenant_id}/models", { params: { path: { tenant_id: tenant.id } } })),
    [api, tenant.id],
  );
  const { data, error, reload } = useLoad(fetchModels);
  const [draft, setDraft] = useState<Partial<Record<Job, string[]>>>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);

  const chosen = (job: Job): string[] => draft[job] ?? data?.available[job] ?? [];
  const toggle = (job: Job, id: string, on: boolean) => {
    const current = chosen(job);
    setDraft({ ...draft, [job]: on ? [...current, id] : current.filter((m) => m !== id) });
  };
  const changed = data !== null && JOBS.some(({ job }) => draft[job] !== undefined);
  const complete = JOBS.every(({ job }) => chosen(job).length > 0);

  const save = async () => {
    setBusy(true);
    try {
      await call(
        api.PUT("/api/admin/tenants/{tenant_id}/models", {
          params: { path: { tenant_id: tenant.id } },
          body: { ingest: chosen("ingest"), query: chosen("query") },
        }),
      );
      setDraft({});
      setNotice({ type: "success", text: "Available models saved." });
      await reload();
    } catch (caught) {
      setNotice({ type: "error", text: messageFor(caught) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={canEdit ? "The models this tenant's admin can choose from." : "Read-only."}
          actions={
            canEdit ? (
              <Button variant="primary" loading={busy} disabled={!changed || !complete} onClick={() => void save()}>
                Save
              </Button>
            ) : undefined
          }
        >
          Models
        </Header>
      }
    >
      <SpaceBetween size="m">
        {notice && (
          <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
            {notice.text}
          </Alert>
        )}
        {error && <Alert type="error">{error}</Alert>}
        {data === null && !error ? (
          <Box>Loading…</Box>
        ) : (
          data && (
            <ColumnLayout columns={2}>
              {JOBS.map(({ job, label }) => (
                <FormField
                  key={job}
                  label={label}
                  errorText={chosen(job).length === 0 ? "Keep at least one model." : undefined}
                >
                  <SpaceBetween size="xs">
                    {data.catalog
                      .filter((m) => m.jobs.includes(job))
                      .map((m) => (
                        <Checkbox
                          key={m.id}
                          checked={chosen(job).includes(m.id)}
                          disabled={!canEdit}
                          description={`${price(m)}${data.selected[job] === m.id ? " · selected by the tenant" : ""}`}
                          onChange={({ detail }) => toggle(job, m.id, detail.checked)}
                        >
                          {m.name}
                        </Checkbox>
                      ))}
                  </SpaceBetween>
                </FormField>
              ))}
            </ColumnLayout>
          )
        )}
      </SpaceBetween>
    </Container>
  );
}
