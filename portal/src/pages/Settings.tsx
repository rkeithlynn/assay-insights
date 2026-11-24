/**
 * Settings (tenant admin; BUILD_SPEC.md sections 7b and 11): two dropdowns, the ingest model
 * and the AI query model, listing only the models available to the tenant, each with its
 * price per million tokens. The API rejects any other model.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback, useState } from "react";

import { call, type CatalogModel, type SettingsModels } from "../api/client";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";

type Job = "ingest" | "query";
const LABELS: Record<Job, { label: string; description: string }> = {
  ingest: { label: "Ingest model", description: "Reads and extracts each new document. Applies to documents ingested after the change." },
  query: { label: "AI query model", description: "Writes the answers to questions." },
};

export function price(model: Pick<CatalogModel, "price_in" | "price_out">): string {
  return `$${model.price_in} in / $${model.price_out} out per million tokens`;
}

function options(models: CatalogModel[]): SelectProps.Option[] {
  return models.map((m) => ({ value: m.id, label: m.name, description: price(m) }));
}

export function Settings() {
  const { api } = useSession();
  const fetchModels = useCallback(async (): Promise<SettingsModels> => call(api.GET("/api/settings/models")), [api]);
  const { data, error, reload } = useLoad(fetchModels);
  const [choice, setChoice] = useState<Partial<Record<Job, string>>>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);

  const selected = (job: Job): string | null => choice[job] ?? data?.selected[job] ?? null;
  const changed = data !== null && (["ingest", "query"] as const).some((job) => selected(job) !== data.selected[job]);

  const save = async () => {
    const ingest = selected("ingest");
    const query = selected("query");
    if (!ingest || !query) return;
    setBusy(true);
    try {
      await call(api.PUT("/api/settings/models", { body: { ingest_model: ingest, query_model: query } }));
      setChoice({});
      setNotice({ type: "success", text: "Model selection saved." });
      await reload();
    } catch (caught) {
      setNotice({ type: "error", text: messageFor(caught) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <ContentLayout header={<Header variant="h1" description="Choose the models your organization uses.">Settings</Header>}>
      <SpaceBetween size="m">
        {notice && (
          <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
            {notice.text}
          </Alert>
        )}
        {error ? (
          <Alert type="error" action={<Button onClick={() => void reload()}>Retry</Button>}>
            {error}
          </Alert>
        ) : (
          <Container header={<Header variant="h2">Models</Header>}>
            {data === null ? (
              <Box>Loading…</Box>
            ) : (
              <Form
                actions={
                  <Button variant="primary" loading={busy} disabled={!changed} onClick={() => void save()}>
                    Save
                  </Button>
                }
              >
                <SpaceBetween size="l">
                  {(["ingest", "query"] as const).map((job) => {
                    const opts = options(data.available[job] ?? []);
                    return (
                      <FormField key={job} label={LABELS[job].label} description={LABELS[job].description}>
                        <Select
                          selectedOption={opts.find((o) => o.value === selected(job)) ?? null}
                          options={opts}
                          placeholder="Choose a model"
                          empty="No models are available. Contact Assay Insights."
                          onChange={({ detail }) => setChoice({ ...choice, [job]: detail.selectedOption.value })}
                        />
                      </FormField>
                    );
                  })}
                </SpaceBetween>
              </Form>
            )}
          </Container>
        )}
      </SpaceBetween>
    </ContentLayout>
  );
}
