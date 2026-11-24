/**
 * Ingestion history across tenants (both master roles; BUILD_SPEC.md sections 5 and 11).
 * Metadata only: received, tenant, batch ID, document ID, pages, size, companion file,
 * status, forms found, duration. Never a file name, page text or score; the API reads it
 * through `platform.platform_ingestion_v`, which has none. Filters: date range, status,
 * tenant. A row opens its pipeline steps, times and errors.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import type { DateRangePickerProps } from "@cloudscape-design/components/date-range-picker";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { call, type DocumentStatus, type IngestionDetail, type IngestionRow } from "../api/client";
import type { paths } from "../api/schema";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";
import { rangeOf } from "./Audit";
import { type ColumnChoice, useColumnDisplay } from "./columns";
import {
  DEFAULT_RANGE,
  downloadCsv,
  FilterBar,
  PAGE_SIZE,
  RangeField,
  StatusCell,
  StatusField,
  Steps,
  useRefreshWhile,
} from "./Ingestion";
import { anyInProgress, errorCodeText, errorText, formatBytes, formatDuration, formatShortTime, shortId } from "./ingestionFormat";

type Sort = NonNullable<NonNullable<paths["/api/admin/ingestion"]["get"]["parameters"]["query"]>["sort"]>;

const ONE_LINE = { whiteSpace: "nowrap" } as const;
// IDs are long and have no spaces; let them break anywhere so the table fits.
const ID_STYLE = { overflowWrap: "anywhere", wordBreak: "break-all", fontFamily: "monospace", fontSize: "12px" } as const;

interface Filters {
  range: DateRangePickerProps.Value | null;
  statuses: string[];
  tenantId: string;
}

// Size and Duration are hidden by default on narrower windows (both are in the row's panel).
export const MASTER_COLUMNS: ColumnChoice[] = [
  { id: "received", label: "Received" },
  { id: "tenant", label: "Tenant", alwaysVisible: true },
  { id: "batch", label: "Batch ID" },
  { id: "document", label: "Document ID" },
  { id: "pages", label: "Pages" },
  { id: "size", label: "Size", hiddenWhenNarrow: true },
  { id: "companion", label: "Companion file" },
  { id: "status", label: "Status" },
  { id: "forms", label: "Forms found" },
  { id: "duration", label: "Duration", hiddenWhenNarrow: true },
];

function query(filters: Filters, sort: Sort, page: number) {
  const { from, to } = rangeOf(filters.range);
  return {
    from,
    to,
    status: filters.statuses.length ? (filters.statuses as DocumentStatus[]) : undefined,
    tenant_id: filters.tenantId || undefined,
    sort,
    page,
    page_size: PAGE_SIZE,
  };
}

function StepsPanel({ row, onDismiss }: { row: IngestionRow | null; onDismiss: () => void }) {
  const { api } = useSession();
  // What was loaded, and for which document (see the tenant view's panel).
  const [loaded, setLoaded] = useState<{ id: string; detail?: IngestionDetail; error?: string } | null>(null);
  useEffect(() => {
    if (!row) return;
    const id = row.document_id;
    call(api.GET("/api/admin/ingestion/{document_id}", { params: { path: { document_id: id } } })).then(
      (detail) => setLoaded({ id, detail }),
      (caught: unknown) => setLoaded({ id, error: messageFor(caught) }),
    );
  }, [api, row]);
  const current = row && loaded?.id === row.document_id ? loaded : null;
  const detail = current?.detail ?? null;
  const error = current?.error ?? null;
  return (
    <Modal visible={row !== null} onDismiss={onDismiss} header="Document" size="large">
      {row && (
        <SpaceBetween size="l">
          <KeyValuePairs
            columns={3}
            items={[
              { label: "Tenant", value: row.tenant_name },
              { label: "Status", value: <StatusCell status={row.status} /> },
              {
                label: "Problem",
                value: row.error_code ? (
                  <>
                    {errorText(row.error_code)}
                    <Box variant="code" display="block" fontSize="body-s">
                      {errorCodeText(row.error_code)}
                    </Box>
                  </>
                ) : (
                  "-"
                ),
              },
              { label: "Document ID", value: <span style={ID_STYLE}>{row.document_id}</span> },
              { label: "Batch ID", value: <span style={ID_STYLE}>{row.batch_id}</span> },
              { label: "Received", value: new Date(row.received_at).toLocaleString() },
              { label: "Pages", value: row.page_count ?? "-" },
              { label: "Size", value: formatBytes(row.size_bytes) },
              { label: "Companion file", value: row.has_companion ? "Yes" : "No" },
              { label: "Forms found", value: row.forms_found },
              { label: "Duration", value: formatDuration(row.duration_seconds) },
            ]}
          />
          {error && <Alert type="error">{error}</Alert>}
          {!detail && !error ? <StatusIndicator type="loading">Loading</StatusIndicator> : <Steps events={detail?.events ?? []} showCodes />}
        </SpaceBetween>
      )}
    </Modal>
  );
}

export function AdminIngestion() {
  const { api } = useSession();
  const initial: Filters = { range: DEFAULT_RANGE, statuses: [], tenantId: "" };
  const [draft, setDraft] = useState<Filters>(initial);
  const [filters, setFilters] = useState<Filters>(initial);
  const [sort, setSort] = useState<Sort>("-received");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<IngestionRow | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const { columnDisplay, preferences } = useColumnDisplay("assay.admin-ingestion.columns", MASTER_COLUMNS);

  const fetchTenants = useCallback(async () => (await call(api.GET("/api/admin/tenants"))).tenants, [api]);
  const { data: tenants } = useLoad(fetchTenants);
  const fetchPage = useCallback(async () => {
    const result = await call(api.GET("/api/admin/ingestion", { params: { query: query(filters, sort, page) } }));
    if (typeof result === "string") throw new Error("expected JSON");
    return result;
  }, [api, filters, sort, page]);
  const { data, error, reload } = useLoad(fetchPage);
  useRefreshWhile(anyInProgress(data?.documents ?? []), reload);

  const tenantOptions = useMemo<SelectProps.Option[]>(
    () => [
      { value: "", label: "All tenants" },
      ...(tenants ?? []).filter((t) => t.kind === "business").map((t) => ({ value: t.id, label: t.name })),
    ],
    [tenants],
  );

  const exportCsv = async () => {
    setExporting(true);
    setExportError(null);
    try {
      await downloadCsv(
        () => api.GET("/api/admin/ingestion", { params: { query: { ...query(filters, sort, 1), format: "csv" } }, parseAs: "text" }),
        "ingestion-all-tenants.csv",
      );
    } catch (caught) {
      setExportError(messageFor(caught));
    } finally {
      setExporting(false);
    }
  };

  const columns: TableProps.ColumnDefinition<IngestionRow>[] = [
    {
      id: "received",
      header: "Received",
      sortingField: "received",
      cell: (d) => (
        <span style={ONE_LINE} title={new Date(d.received_at).toLocaleString()}>
          {formatShortTime(d.received_at)}
        </span>
      ),
    },
    {
      id: "tenant",
      header: "Tenant",
      sortingField: "tenant",
      cell: (d) => d.tenant_name,
      isRowHeader: true,
    },
    { id: "batch", header: "Batch ID", sortingField: "batch", cell: (d) => <span style={ID_STYLE} title={d.batch_id}>{shortId(d.batch_id)}</span> },
    {
      id: "document",
      header: "Document ID",
      sortingField: "document",
      cell: (d) => (
        <span style={ID_STYLE} title={d.document_id}>
          {shortId(d.document_id)}
        </span>
      ),
    },
    { id: "pages", header: "Pages", sortingField: "pages", cell: (d) => d.page_count ?? "-" },
    { id: "size", header: "Size", sortingField: "size", cell: (d) => <span style={ONE_LINE}>{formatBytes(d.size_bytes)}</span> },
    { id: "companion", header: "Companion", sortingField: "companion", cell: (d) => (d.has_companion ? "Yes" : "No") },
    { id: "status", header: "Status", sortingField: "status", cell: (d) => <StatusCell status={d.status} error={d.error_code} /> },
    { id: "forms", header: "Forms found", sortingField: "forms", cell: (d) => d.forms_found },
    { id: "duration", header: "Duration", sortingField: "duration", cell: (d) => <span style={ONE_LINE}>{formatDuration(d.duration_seconds)}</span> },
  ];

  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Documents ingested by every tenant. Metadata only: no file names, text or scores."
          actions={
            <Button iconName="download" loading={exporting} onClick={() => void exportCsv()}>
              Export CSV
            </Button>
          }
        >
          Ingestion history
        </Header>
      }
    >
      <SpaceBetween size="m">
        {exportError && (
          <Alert type="error" dismissible onDismiss={() => setExportError(null)}>
            {exportError}
          </Alert>
        )}
        {error && (
          <Alert type="error" action={<Button onClick={() => void reload()}>Retry</Button>}>
            {error}
          </Alert>
        )}
        <Table
          // A page that is one table: Cloudscape's full-page variant, which also leaves the
          // ten columns the width they need at 1280 px.
          variant="full-page"
          loading={data === null && !error}
          loadingText="Loading ingestion history"
          items={data?.documents ?? []}
          trackBy="document_id"
          wrapLines
          columnDefinitions={columns}
          columnDisplay={columnDisplay}
          preferences={preferences}
          sortingColumn={{ sortingField: sort.replace(/^-/, "") }}
          sortingDescending={sort.startsWith("-")}
          onSortingChange={({ detail }) => {
            setSort(((detail.isDescending ? "-" : "") + (detail.sortingColumn.sortingField ?? "received")) as Sort);
            setPage(1);
          }}
          onRowClick={({ detail }) => setOpen(detail.item)}
          empty={<Box textAlign="center">No documents match these filters.</Box>}
          header={<Header counter={data ? `(${data.total})` : undefined}>Documents</Header>}
          filter={
            <FilterBar
              onApply={() => {
                setFilters(draft);
                setPage(1);
              }}
            >
              <RangeField value={draft.range} onChange={(range) => setDraft({ ...draft, range })} />
              <StatusField value={draft.statuses} onChange={(statuses) => setDraft({ ...draft, statuses })} />
              <FormField label="Tenant">
                <Select
                  selectedOption={tenantOptions.find((o) => o.value === draft.tenantId) ?? tenantOptions[0] ?? null}
                  options={tenantOptions}
                  onChange={({ detail }) => setDraft({ ...draft, tenantId: detail.selectedOption.value ?? "" })}
                />
              </FormField>
            </FilterBar>
          }
          pagination={<Pagination currentPageIndex={page} pagesCount={pages} onChange={({ detail }) => setPage(detail.currentPageIndex)} />}
        />
      </SpaceBetween>
      <StepsPanel row={open} onDismiss={() => setOpen(null)} />
    </ContentLayout>
  );
}
