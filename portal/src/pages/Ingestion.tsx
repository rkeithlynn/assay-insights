/**
 * Ingestion history (BUILD_SPEC.md section 11). One row per PDF, newest first, 50 per page,
 * server-side sort and filter, CSV export, refreshed every 10 seconds while any row is in
 * progress. Filters: date range (today, 7 days, 30 days, custom), status, batch; the master
 * view adds tenant. A row opens a panel with each pipeline step, its time and any error;
 * tenants also see the forms found and can open the PDF.
 *
 * Tenant columns: received, batch, file name, pages, size, companion file, status, forms
 * found, duration, uploaded by. Master columns (`AdminIngestion`): received, tenant, batch
 * ID, document ID, pages, size, companion file, status, forms found, duration; never a file
 * name. Discarded documents are left out unless the status filter asks for them.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import DateRangePicker, { type DateRangePickerProps } from "@cloudscape-design/components/date-range-picker";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Multiselect, { type MultiselectProps } from "@cloudscape-design/components/multiselect";
import Pagination from "@cloudscape-design/components/pagination";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";

import { call, type DocumentDetail, type DocumentRow, type DocumentStatus, errorFrom } from "../api/client";
import type { paths } from "../api/schema";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";
import { rangeOf } from "./Audit";
import { type ColumnChoice, useColumnDisplay } from "./columns";
import {
  anyInProgress,
  DOCUMENT_STATUS,
  errorCodeText,
  errorText,
  laterStepsPending,
  resultText,
  formatBytes,
  formatDuration,
  formatShortTime,
  STATUS_OPTIONS,
  stepText,
} from "./ingestionFormat";

export const PAGE_SIZE = 50;
export const REFRESH_MS = 10_000;
const ONE_LINE = { whiteSpace: "nowrap" } as const;
// A file name (often one long hyphenated token) may break anywhere, so it never widens its
// column; other names break between words, and inside one only if it cannot fit.
const WRAP_NAME = { overflowWrap: "anywhere" } as const;
const WRAP_WORDS = { overflowWrap: "break-word" } as const;

export const DEFAULT_RANGE: DateRangePickerProps.RelativeOption = {
  type: "relative",
  amount: 7,
  unit: "day",
  key: "previous-7-days",
};
export const RELATIVE_OPTIONS: DateRangePickerProps.RelativeOption[] = [
  { key: "today", amount: 1, unit: "day", type: "relative" },
  DEFAULT_RANGE,
  { key: "previous-30-days", amount: 30, unit: "day", type: "relative" },
];

export const i18n: DateRangePickerProps.I18nStrings = {
  formatRelativeRange: (range) => (range.key === "today" ? "Today" : `Last ${range.amount} days`),
};

export function StatusCell({ status, error }: { status: DocumentStatus; error?: string | null }) {
  const s = DOCUMENT_STATUS[status];
  return (
    <span style={ONE_LINE} title={errorText(error) || undefined}>
      <StatusIndicator type={s.type}>{s.label}</StatusIndicator>
    </span>
  );
}

/** Refresh every 10 seconds while anything shown is still in progress. */
export function useRefreshWhile(active: boolean, reload: () => Promise<void>) {
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void reload(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [active, reload]);
}

/** Download a CSV answer of `fetch` (the API sets the file name). */
export async function downloadCsv(fetch: () => Promise<{ data?: unknown; response: Response }>, fallback: string) {
  const { data: text, response } = await fetch();
  if (!response.ok || typeof text !== "string") throw errorFrom(response.status, null);
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? fallback;
  link.click();
  URL.revokeObjectURL(url);
}

interface StepEvent {
  step: string;
  status: string;
  at: string;
  error_code: string | null;
  // The master view only: a failed step's error type.
  error_type?: string | null;
  // The tenant view, for tenant admins only (the API leaves it out for tenant users): who
  // opened the PDF.
  viewer?: string | null;
}

function result(e: StepEvent): string {
  const words = resultText(e.status);
  return e.step === "viewed" && e.viewer ? `${words} by ${e.viewer}` : words;
}

/** A document's pipeline steps. `showCodes` (master admins only) adds each error's code and
 * error type under its plain words; tenant users see the plain words alone. */
export function Steps({
  events,
  documentStatus,
  showCodes = false,
}: {
  events: StepEvent[];
  documentStatus: string;
  showCodes?: boolean;
}) {
  return (
    <SpaceBetween size="s">
      <Table
        variant="embedded"
        items={events}
        wrapLines
        header={<Header variant="h3">Pipeline steps</Header>}
        empty={<Box textAlign="center">No steps yet.</Box>}
        columnDefinitions={[
          { id: "step", header: "Step", cell: (e) => stepText(e.step) },
          { id: "status", header: "Result", cell: result },
        { id: "at", header: "Time", cell: (e) => <span style={ONE_LINE}>{new Date(e.at).toLocaleString()}</span> },
        {
          id: "error",
          header: "Error",
          cell: (e) => {
            const words = errorText(e.error_code) || "-";
            const codes = showCodes ? errorCodeText(e.error_code, e.error_type) : "";
            return codes ? (
              <>
                {words}
                <Box variant="code" display="block" fontSize="body-s">
                  {codes}
                </Box>
              </>
            ) : (
              words
            );
          },
        },
      ]}
      />
      {laterStepsPending(documentStatus, events) && (
        // TODO(build step 6, second half, stage 4): remove with laterStepsPending.
        <Alert type="info">
          This document passed every step there is so far. The steps after them are not available yet, so it
          shows as Processing for now.
        </Alert>
      )}
    </SpaceBetween>
  );
}

interface Filters {
  range: DateRangePickerProps.Value | null;
  statuses: string[];
  batchId: string;
}

// Size and Duration are hidden by default on narrower windows (both are in the row's panel).
export const TENANT_COLUMNS: ColumnChoice[] = [
  { id: "received", label: "Received" },
  { id: "batch", label: "Batch" },
  { id: "file_name", label: "File name", alwaysVisible: true },
  { id: "pages", label: "Pages" },
  { id: "size", label: "Size", hiddenWhenNarrow: true },
  { id: "companion", label: "Companion file" },
  { id: "status", label: "Status" },
  { id: "forms", label: "Forms found" },
  { id: "duration", label: "Duration", hiddenWhenNarrow: true },
  { id: "uploaded_by", label: "Uploaded by" },
];

type Sort = NonNullable<NonNullable<paths["/api/documents"]["get"]["parameters"]["query"]>["sort"]>;

function query(filters: Filters, sort: Sort, page: number) {
  const { from, to } = rangeOf(filters.range);
  return {
    from,
    to,
    status: filters.statuses.length ? (filters.statuses as DocumentStatus[]) : undefined,
    batch_id: filters.batchId || undefined,
    sort,
    page,
    page_size: PAGE_SIZE,
  };
}

function DocumentPanel({ id, onDismiss }: { id: string | null; onDismiss: () => void }) {
  const { api } = useSession();
  // What was loaded, and for which document: a panel opened on another document shows nothing
  // until its own detail arrives.
  const [loaded, setLoaded] = useState<{ id: string; detail?: DocumentDetail; error?: string } | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);

  useEffect(() => {
    if (!id) return;
    call(api.GET("/api/documents/{document_id}", { params: { path: { document_id: id } } })).then(
      (detail) => setLoaded({ id, detail }),
      (caught: unknown) => setLoaded({ id, error: messageFor(caught) }),
    );
  }, [api, id]);
  const current = loaded?.id === id ? loaded : null;
  const detail = current?.detail ?? null;
  const error = current?.error ?? linkError;
  const setError = setLinkError;

  const openPdf = async () => {
    if (!id) return;
    setOpening(true);
    try {
      const link = await call(api.GET("/api/documents/{document_id}/pdf-url", { params: { path: { document_id: id } } }));
      window.open(link.url, "_blank", "noopener,noreferrer");
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setOpening(false);
    }
  };

  return (
    <Modal
      visible={id !== null}
      onDismiss={() => {
        setLinkError(null);
        onDismiss();
      }}
      header={detail?.file_name ?? "Document"}
      size="large"
    >
      <SpaceBetween size="l">
        {error && <Alert type="error">{error}</Alert>}
        {!detail && !error && <StatusIndicator type="loading">Loading</StatusIndicator>}
        {detail && (
          <>
            <KeyValuePairs
              columns={3}
              items={[
                { label: "Status", value: <StatusCell status={detail.status} /> },
                { label: "Problem", value: errorText(detail.error_code) || "-" },
                { label: "Received", value: new Date(detail.received_at).toLocaleString() },
                { label: "Batch", value: detail.batch_name ?? detail.batch_id },
                { label: "Uploaded by", value: detail.uploaded_by },
                { label: "Pages", value: detail.page_count ?? "-" },
                { label: "Size", value: formatBytes(detail.size_bytes) },
                { label: "Companion file", value: detail.has_companion ? "Yes" : "No" },
                { label: "Duration", value: formatDuration(detail.duration_seconds) },
              ]}
            />
            {detail.pdf_available && (
              <Box>
                <Button iconName="external" iconAlign="right" loading={opening} onClick={() => void openPdf()}>
                  Open PDF
                </Button>
              </Box>
            )}
            <Steps events={detail.events} documentStatus={detail.status} />
            <Table
              variant="embedded"
              items={detail.parts}
              header={<Header variant="h3">Forms found</Header>}
              empty={<Box textAlign="center">No forms found yet.</Box>}
              columnDefinitions={[
                { id: "form", header: "Form", cell: (p) => p.form_type },
                { id: "pages", header: "Pages", cell: (p) => (p.start_page === p.end_page ? p.start_page : `${p.start_page}-${p.end_page}`) },
                { id: "method", header: "Found by", cell: (p) => (p.method === "rule" ? "Rule" : "Model") },
              ]}
            />
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
}

export function FilterBar({ children, onApply }: { children: ReactNode; onApply: () => void }) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onApply();
      }}
    >
      <SpaceBetween direction="horizontal" size="s" alignItems="end">
        {children}
        <Button formAction="submit">Apply</Button>
      </SpaceBetween>
    </form>
  );
}

export function RangeField({ value, onChange }: { value: DateRangePickerProps.Value | null; onChange: (v: DateRangePickerProps.Value | null) => void }) {
  return (
    <FormField label="Received">
      <DateRangePicker
        value={value}
        relativeOptions={RELATIVE_OPTIONS}
        i18nStrings={i18n}
        isValidRange={(range) =>
          range?.type === "absolute" && (!range.startDate || !range.endDate)
            ? { valid: false, errorMessage: "Choose a start and an end." }
            : { valid: true }
        }
        rangeSelectorMode="default"
        onChange={({ detail }) => onChange(detail.value)}
        placeholder="Last 7 days"
      />
    </FormField>
  );
}

export function StatusField({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const selected: MultiselectProps.Options = STATUS_OPTIONS.filter((o) => value.includes(o.value));
  return (
    <FormField label="Status">
      <Multiselect
        selectedOptions={selected}
        options={STATUS_OPTIONS}
        placeholder="All but discarded"
        onChange={({ detail }) => onChange(detail.selectedOptions.map((o) => o.value ?? ""))}
        hideTokens
      />
    </FormField>
  );
}

export function Ingestion() {
  const { api } = useSession();
  const [params] = useSearchParams();
  const initial: Filters = { range: DEFAULT_RANGE, statuses: [], batchId: params.get("batch") ?? "" };
  const [draft, setDraft] = useState<Filters>(initial);
  const [filters, setFilters] = useState<Filters>(initial);
  const [sort, setSort] = useState<Sort>("-received");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const { columnDisplay, preferences } = useColumnDisplay("assay.ingestion.columns", TENANT_COLUMNS);

  const fetchBatches = useCallback(async () => (await call(api.GET("/api/batches", { params: { query: { limit: 200 } } }))).batches, [api]);
  const { data: batches } = useLoad(fetchBatches);
  const fetchPage = useCallback(async () => {
    const result = await call(api.GET("/api/documents", { params: { query: query(filters, sort, page) } }));
    if (typeof result === "string") throw new Error("expected JSON");
    return result;
  }, [api, filters, sort, page]);
  const { data, error, reload } = useLoad(fetchPage);
  useRefreshWhile(anyInProgress(data?.documents ?? []), reload);

  const batchOptions = useMemo<SelectProps.Option[]>(
    () => [
      { value: "", label: "All batches" },
      ...(batches ?? []).map((b) => ({
        value: b.id,
        label: b.name ?? `Batch of ${new Date(b.opened_at).toLocaleDateString()}`,
        description: `${b.status}, ${b.pdf_count} PDFs, opened by ${b.opened_by}`,
      })),
    ],
    [batches],
  );

  const exportCsv = async () => {
    setExporting(true);
    setExportError(null);
    try {
      await downloadCsv(
        () => api.GET("/api/documents", { params: { query: { ...query(filters, sort, 1), format: "csv" } }, parseAs: "text" }),
        "ingestion.csv",
      );
    } catch (caught) {
      setExportError(messageFor(caught));
    } finally {
      setExporting(false);
    }
  };

  const columns: TableProps.ColumnDefinition<DocumentRow>[] = [
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
    { id: "batch", header: "Batch", sortingField: "batch", cell: (d) => <span style={WRAP_WORDS}>{d.batch_name ?? "-"}</span> },
    {
      id: "file_name",
      header: "File name",
      sortingField: "file_name",
      isRowHeader: true,
      cell: (d) => (
        <Link
          href="#"
          onFollow={(event) => {
            event.preventDefault();
            setOpen(d.id);
          }}
        >
          <span style={WRAP_NAME}>{d.file_name}</span>
        </Link>
      ),
    },
    { id: "pages", header: "Pages", sortingField: "pages", cell: (d) => d.page_count ?? "-" },
    { id: "size", header: "Size", sortingField: "size", cell: (d) => <span style={ONE_LINE}>{formatBytes(d.size_bytes)}</span> },
    { id: "companion", header: "Companion", sortingField: "companion", cell: (d) => (d.has_companion ? "Yes" : "No") },
    { id: "status", header: "Status", sortingField: "status", cell: (d) => <StatusCell status={d.status} error={d.error_code} /> },
    { id: "forms", header: "Forms found", sortingField: "forms", cell: (d) => d.forms_found },
    { id: "duration", header: "Duration", sortingField: "duration", cell: (d) => <span style={ONE_LINE}>{formatDuration(d.duration_seconds)}</span> },
    { id: "uploaded_by", header: "Uploaded by", sortingField: "uploaded_by", cell: (d) => <span style={WRAP_WORDS}>{d.uploaded_by}</span> },
  ];

  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Every PDF your organization uploaded, and where it is in processing."
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
          trackBy="id"
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
          onRowClick={({ detail }) => setOpen(detail.item.id)}
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
              <FormField label="Batch">
                <Select
                  selectedOption={batchOptions.find((o) => o.value === draft.batchId) ?? batchOptions[0] ?? null}
                  options={batchOptions}
                  onChange={({ detail }) => setDraft({ ...draft, batchId: detail.selectedOption.value ?? "" })}
                />
              </FormField>
            </FilterBar>
          }
          pagination={<Pagination currentPageIndex={page} pagesCount={pages} onChange={({ detail }) => setPage(detail.currentPageIndex)} />}
        />
      </SpaceBetween>
      <DocumentPanel id={open} onDismiss={() => setOpen(null)} />
    </ContentLayout>
  );
}
