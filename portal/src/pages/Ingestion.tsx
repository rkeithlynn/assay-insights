/**
 * Ingestion history (BUILD_SPEC.md section 11). One row per PDF, newest first, 50 per page,
 * server-side sort and filter, CSV export, refreshed every 10 seconds while any row is in
 * progress. Filters: date range (today, 7 days, 30 days, custom), status, batch; the master
 * view adds tenant. A row opens a panel with each pipeline step, its time and any error;
 * tenants also see the forms found and can open the PDF.
 *
 * Tenant admins can also (build step 6, second half; owner decisions 3 and 7): select
 * documents and process them again, after a confirmation that says how many are Ready (their
 * AI steps run again, which costs money); mark a document that needs review as reviewed; and
 * attach or replace a document's companion file. Every tenant member sees the companion
 * file's values in the panel. The master view shows none of this.
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
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { type Api, call, type DocumentDetail, type DocumentRow, type DocumentStatus, errorFrom } from "../api/client";
import type { paths } from "../api/schema";
import { Confirm, type ConfirmRequest } from "../components/Confirm";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { putFile, sha256Hex } from "../upload";
import { useLoad } from "../useLoad";
import { rangeOf } from "./Audit";
import { type ColumnChoice, useColumnDisplay } from "./columns";
import {
  anyInProgress,
  canReprocess,
  COMPANION_REPLACEABLE,
  companionErrorLines,
  companionFileText,
  type CompanionProblem,
  DOCUMENT_STATUS,
  reprocessSummary,
  errorCodeText,
  errorText,
  laterStepsPending,
  resultText,
  formatBytes,
  formatDuration,
  formatShortTime,
  STATUS_OPTIONS,
  stepText,
  textPagesText,
  type TextPages,
} from "./ingestionFormat";

export const PAGE_SIZE = 50;
export const REFRESH_MS = 10_000;
// While a companion file is waiting for its scan, the panel looks again this often.
export const COMPANION_POLL_MS = 5_000;
const ONE_LINE = { whiteSpace: "nowrap" } as const;
// A file name (often one long hyphenated token) may break anywhere, so it never widens its
// column; other names break between words, and inside one only if it cannot fit.
const WRAP_NAME = { overflowWrap: "anywhere" } as const;
const WRAP_WORDS = { overflowWrap: "break-word" } as const;
// The history's File name column (owner, 2026-11-25): at least this wide, one line, and a
// long name cut with an ellipsis. The full name is the cell's tooltip and the panel's title.
// The width grows a little with the window. It cannot grow far: the page's content stops at
// about 1280 px however wide the window is, and a wide window also shows Size and Duration.
export const FILE_NAME_MIN_WIDTH = 200;
const FILE_NAME = {
  display: "inline-block",
  verticalAlign: "bottom",
  maxWidth: `clamp(${FILE_NAME_MIN_WIDTH - 40}px, 14vw, 240px)`,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} as const;

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

/** A companion upload's problems, one line each, in the words the Upload screen uses. */
export function CompanionProblems({ errors }: { errors: CompanionProblem[] }) {
  if (errors.length === 0) return <>-</>;
  return (
    <>
      {companionErrorLines(errors).map((line, i) => (
        <div key={i} style={WRAP_WORDS}>
          {line}
        </div>
      ))}
    </>
  );
}

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
  // The tenant view: which run of the pipeline ("Process again" starts a new one).
  attempt?: number;
  // Both views, on the Text step's result: its page counts.
  text_pages?: TextPages | null;
}

/** What the confirmation says before documents are processed again. */
export function reprocessBody(statuses: string[]): ReactNode {
  const { count, ready } = reprocessSummary(statuses);
  return (
    <SpaceBetween size="s">
      <Box>
        {count === 1 ? "This document" : `These ${count} documents`} will go through every processing step again.
        The results they have now stay in place until the new ones are complete.
      </Box>
      {ready > 0 && (
        <Alert type="warning">
          {ready === 1 ? "1 document is" : `${ready} documents are`} Ready. Processing a Ready document again runs
          the AI steps again, which costs money.
        </Alert>
      )}
    </SpaceBetween>
  );
}

export interface ReprocessOutcome {
  started: number;
  refused: { name: string; reason: string }[];
}

/** Ask for each document in turn; one refusal does not stop the rest. */
export async function reprocessAll(api: Api, documents: { id: string; file_name: string }[]): Promise<ReprocessOutcome> {
  const outcome: ReprocessOutcome = { started: 0, refused: [] };
  for (const d of documents) {
    try {
      await call(api.POST("/api/documents/{document_id}/reprocess", { params: { path: { document_id: d.id } } }));
      outcome.started += 1;
    } catch (caught) {
      outcome.refused.push({ name: d.file_name, reason: messageFor(caught) });
    }
  }
  return outcome;
}

export function ReprocessResult({ outcome, onDismiss }: { outcome: ReprocessOutcome; onDismiss: () => void }) {
  return (
    <Alert type={outcome.refused.length ? "warning" : "success"} dismissible onDismiss={onDismiss}>
      {outcome.started === 1 ? "1 document was" : `${outcome.started} documents were`} queued to be processed again.
      {outcome.refused.length > 0 && (
        <ul>
          {outcome.refused.map((r) => (
            <li key={r.name}>
              <span style={WRAP_NAME}>{r.name}</span>: {r.reason}
            </li>
          ))}
        </ul>
      )}
    </Alert>
  );
}

function result(e: StepEvent): string {
  const words = resultText(e.status);
  // The Text step says what it did with the pages, the unread count included.
  if (e.step === "text" && e.text_pages) return `${words} \u00b7 ${textPagesText(e.text_pages)}`;
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
  // Once a document has been processed again, each step says which attempt it belongs to,
  // and only the latest attempt's steps decide the note below.
  const latest = Math.max(1, ...events.map((e) => e.attempt ?? 1));
  const current = events.filter((e) => (e.attempt ?? 1) === latest);
  return (
    <SpaceBetween size="s">
      <Table
        variant="embedded"
        items={events}
        wrapLines
        header={<Header variant="h3">Pipeline steps</Header>}
        empty={<Box textAlign="center">No steps yet.</Box>}
        columnDefinitions={[
          ...(latest > 1 ? [{ id: "attempt", header: "Attempt", cell: (e: StepEvent) => e.attempt ?? 1 }] : []),
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
      {laterStepsPending(documentStatus, current) && (
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

// Size and Duration are hidden by default on narrower windows, and Companion file at every
// width (each is in the row's panel, and Preferences shows any of them). Companion file
// joined them on 2026-11-25: with the selection checkboxes, the File name column had no room
// left, at 1280 px or at 1800.
export const TENANT_COLUMNS: ColumnChoice[] = [
  { id: "received", label: "Received" },
  { id: "batch", label: "Batch" },
  { id: "file_name", label: "File name", alwaysVisible: true },
  { id: "pages", label: "Pages" },
  { id: "size", label: "Size", hiddenWhenNarrow: true },
  { id: "companion", label: "Companion file", hiddenByDefault: true },
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

/** The companion file's values (step 9), as the tenant's own data. */
export function CompanionValues({ companion }: { companion: NonNullable<DocumentDetail["companion"]> }) {
  const text = (value: string | number | null | undefined) => (value === null || value === undefined || value === "" ? "-" : value);
  return (
    <SpaceBetween size="s">
      <Header variant="h3" description="From the companion file, kept beside what is read from the PDF.">
        Companion file values
      </Header>
      <KeyValuePairs
        columns={3}
        items={[
          { label: "Application ID", value: <span style={WRAP_NAME}>{text(companion.application_ref)}</span> },
          { label: "Loan number", value: <span style={WRAP_NAME}>{text(companion.loan_number)}</span> },
          { label: "Cosigner", value: <span style={WRAP_WORDS}>{text(companion.cosigner_name)}</span> },
          { label: "Package created", value: text(companion.package_created_on) },
          { label: "Package completed", value: text(companion.package_completed_on) },
          { label: "Loan term (months)", value: text(companion.loan_term_months) },
          { label: "Maturity date", value: text(companion.loan_maturity_date) },
          { label: "Interest rate (%)", value: text(companion.loan_interest_rate) },
        ]}
      />
      {companion.signers.length > 0 && (
        <Table
          variant="embedded"
          items={companion.signers}
          wrapLines
          header={<Header variant="h3">Signers</Header>}
          columnDefinitions={[
            { id: "name", header: "Name", cell: (s) => <span style={WRAP_WORDS}>{text(s.name)}</span> },
            { id: "email", header: "Email", cell: (s) => <span style={WRAP_NAME}>{text(s.email)}</span> },
            { id: "phone", header: "Phone", cell: (s) => text(s.phone) },
            { id: "location", header: "Location", cell: (s) => <span style={WRAP_WORDS}>{text(s.location)}</span> },
          ]}
        />
      )}
      {companion.labels.length > 0 && (
        <Table
          variant="embedded"
          items={companion.labels}
          wrapLines
          header={<Header variant="h3">Other values</Header>}
          columnDefinitions={[
            { id: "key", header: "Key", cell: (l) => <span style={WRAP_NAME}>{l.key}</span> },
            { id: "value", header: "Value", cell: (l) => <span style={WRAP_NAME}>{String(l.value)}</span> },
          ]}
        />
      )}
    </SpaceBetween>
  );
}

function DocumentPanel({
  id,
  canAct,
  onDismiss,
  onChanged,
}: {
  id: string | null;
  // A tenant admin: process again, mark as reviewed, attach or replace the companion file.
  canAct: boolean;
  onDismiss: () => void;
  onChanged: () => void;
}) {
  const { api } = useSession();
  // What was loaded, and for which document: a panel opened on another document shows nothing
  // until its own detail arrives.
  const [loaded, setLoaded] = useState<{ id: string; detail?: DocumentDetail; error?: string } | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const fetchDetail = useCallback(
    (documentId: string) =>
      call(api.GET("/api/documents/{document_id}", { params: { path: { document_id: documentId } } })).then(
        (detail) => ({ id: documentId, detail }),
        (caught: unknown) => ({ id: documentId, error: messageFor(caught) }),
      ),
    [api],
  );
  useEffect(() => {
    if (!id) return;
    void fetchDetail(id).then(setLoaded);
  }, [fetchDetail, id]);
  const load = useCallback(async () => {
    if (id) setLoaded(await fetchDetail(id));
  }, [fetchDetail, id]);
  const current = loaded?.id === id ? loaded : null;
  const detail = current?.detail ?? null;
  const error = current?.error ?? linkError;
  const setError = setLinkError;
  // A companion file on its way: look again until its scan has decided.
  const waiting = detail?.companion_files.some((f) => f.status === "registered") ?? false;
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => void load(), COMPANION_POLL_MS);
    return () => window.clearInterval(timer);
  }, [waiting, load]);

  /** Run one of the admin's actions, then show what the document looks like now. */
  const act = async (run: () => Promise<string>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setNotice(await run());
      await load();
      onChanged();
    } catch (caught) {
      setError(caught instanceof Error && !("code" in caught) ? caught.message : messageFor(caught));
    } finally {
      setBusy(false);
    }
  };

  const processAgain = () => {
    if (!detail) return;
    setConfirm({
      title: "Process this document again?",
      body: reprocessBody([detail.status]),
      action: "Process again",
      run: () =>
        act(async () => {
          const started = await call(api.POST("/api/documents/{document_id}/reprocess", { params: { path: { document_id: detail.id } } }));
          return `Queued to be processed again (attempt ${started.attempt}).`;
        }),
    });
  };

  const markReviewed = () =>
    act(async () => {
      if (!detail) return "";
      await call(api.POST("/api/documents/{document_id}/reviewed", { params: { path: { document_id: detail.id } } }));
      return "Marked as reviewed. The document is Ready.";
    });

  const attachCompanion = (file: File) =>
    act(async () => {
      if (!detail) return "";
      const registered = await call(
        api.POST("/api/documents/{document_id}/companion", {
          params: { path: { document_id: detail.id } },
          body: { name: file.name, size_bytes: file.size, sha256: await sha256Hex(file) },
        }),
      );
      try {
        await putFile(registered.upload, file, () => undefined);
      } catch {
        throw new Error("The file could not be uploaded. Try again in a few minutes.");
      }
      return "Uploaded. It is scanned and checked first; its values replace the current ones once it is accepted.";
    });

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
        {notice && (
          <Alert type="success" dismissible onDismiss={() => setNotice(null)}>
            {notice}
          </Alert>
        )}
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
                // The current attempt's: a document processed again is timed from then.
                { label: detail.attempt > 1 ? `Duration (attempt ${detail.attempt})` : "Duration", value: formatDuration(detail.duration_seconds) },
              ]}
            />
            <SpaceBetween direction="horizontal" size="xs">
              {detail.pdf_available && (
                <Button iconName="external" iconAlign="right" loading={opening} onClick={() => void openPdf()}>
                  Open PDF
                </Button>
              )}
              {canAct && canReprocess(detail.status) && (
                <Button disabled={busy} onClick={processAgain}>
                  Process again
                </Button>
              )}
              {canAct && detail.status === "needs_review" && (
                <Button disabled={busy} onClick={() => void markReviewed()}>
                  Mark as reviewed
                </Button>
              )}
              {canAct && COMPANION_REPLACEABLE.includes(detail.status) && (
                <>
                  <input
                    ref={fileInput}
                    type="file"
                    accept=".json,application/json"
                    hidden
                    aria-label="Companion file"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      event.target.value = "";
                      if (file) void attachCompanion(file);
                    }}
                  />
                  <Button disabled={busy || waiting} loading={busy} iconName="upload" onClick={() => fileInput.current?.click()}>
                    {detail.has_companion ? "Replace companion file" : "Attach companion file"}
                  </Button>
                </>
              )}
            </SpaceBetween>
            <Steps events={detail.events} documentStatus={detail.status} />
            {detail.companion && <CompanionValues companion={detail.companion} />}
            {detail.companion_files.length > 0 && (
              <Table
                variant="embedded"
                items={detail.companion_files}
                wrapLines
                header={<Header variant="h3">Companion file uploads</Header>}
                columnDefinitions={[
                  { id: "name", header: "File", cell: (f) => <span style={WRAP_NAME}>{f.name}</span> },
                  { id: "status", header: "Status", cell: (f) => companionFileText(f) },
                  {
                    id: "problems",
                    header: "Problems",
                    // Plain words, as on the Upload screen; never the stored codes.
                    cell: (f) => <CompanionProblems errors={f.errors} />,
                  },
                  {
                    id: "uploaded",
                    header: "Uploaded",
                    cell: (f) => <span style={ONE_LINE}>{formatShortTime(f.registered_at)}</span>,
                  },
                ]}
              />
            )}
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
      <Confirm
        request={confirm}
        busy={busy}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          const request = confirm;
          setConfirm(null);
          void request?.run();
        }}
      />
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
  const { api, me } = useSession();
  const isAdmin = me.role === "tenant-admin";
  const [selected, setSelected] = useState<DocumentRow[]>([]);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [reprocessing, setReprocessing] = useState(false);
  const [outcome, setOutcome] = useState<ReprocessOutcome | null>(null);
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

  const processAgain = () => {
    const documents = selected.filter((d) => canReprocess(d.status));
    if (!documents.length) return;
    setConfirm({
      title: documents.length === 1 ? "Process this document again?" : `Process ${documents.length} documents again?`,
      body: reprocessBody(documents.map((d) => d.status)),
      action: "Process again",
      run: async () => {
        setReprocessing(true);
        try {
          setOutcome(await reprocessAll(api, documents));
          setSelected([]);
          await reload();
        } finally {
          setReprocessing(false);
        }
      },
    });
  };

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
      minWidth: FILE_NAME_MIN_WIDTH,
      cell: (d) => (
        <Link
          href="#"
          onFollow={(event) => {
            event.preventDefault();
            setOpen(d.id);
          }}
        >
          <span style={FILE_NAME} title={d.file_name}>
            {d.file_name}
          </span>
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
            <SpaceBetween direction="horizontal" size="xs">
              {isAdmin && (
                <Button disabled={!selected.some((d) => canReprocess(d.status))} loading={reprocessing} onClick={processAgain}>
                  Process again
                </Button>
              )}
              <Button iconName="download" loading={exporting} onClick={() => void exportCsv()}>
                Export CSV
              </Button>
            </SpaceBetween>
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
        {outcome && <ReprocessResult outcome={outcome} onDismiss={() => setOutcome(null)} />}
        <Table
          // Tenant admins select documents to process again; one that cannot be is not
          // selectable.
          selectionType={isAdmin ? "multi" : undefined}
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
          isItemDisabled={(d) => !canReprocess(d.status)}
          ariaLabels={{
            selectionGroupLabel: "Documents to process again",
            allItemsSelectionLabel: () => "Select every document on this page",
            itemSelectionLabel: (_, d) => `Select ${d.file_name}`,
          }}
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
      <DocumentPanel id={open} canAct={isAdmin} onDismiss={() => setOpen(null)} onChanged={() => void reload()} />
      <Confirm
        request={confirm}
        busy={reprocessing}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          const request = confirm;
          setConfirm(null);
          void request?.run();
        }}
      />
    </ContentLayout>
  );
}
