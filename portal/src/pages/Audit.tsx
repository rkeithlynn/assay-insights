/**
 * Audit history (both master roles; BUILD_SPEC.md section 11). Newest first, 50 per page,
 * server-side sort and filter, CSV export. Columns: time, tenant, actor, role, action,
 * target, outcome. Filters: date range (last 7 days by default), tenant, category, actor's
 * email. A row opens the before and after values, the IP address and the browser.
 *
 * Every filter is sent to the API as a query parameter; nothing here builds a query.
 * The API redacts anything secret in the before and after values before sending them.
 */

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import ContentLayout from "@cloudscape-design/components/content-layout";
import DateRangePicker, { type DateRangePickerProps } from "@cloudscape-design/components/date-range-picker";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import { useCallback, useMemo, useState } from "react";

import { type AuditEvent, type AuditPage, call, errorFrom } from "../api/client";
import { messageFor } from "../errors";
import { useSession } from "../session";
import { useLoad } from "../useLoad";

const PAGE_SIZE = 50;
const DEFAULT_RANGE: DateRangePickerProps.RelativeOption = {
  type: "relative",
  amount: 7,
  unit: "day",
  key: "previous-7-days",
};
const RELATIVE_OPTIONS: DateRangePickerProps.RelativeOption[] = [
  { key: "previous-1-day", amount: 1, unit: "day", type: "relative" },
  DEFAULT_RANGE,
  { key: "previous-30-days", amount: 30, unit: "day", type: "relative" },
  { key: "previous-90-days", amount: 90, unit: "day", type: "relative" },
];
const CATEGORIES: SelectProps.Option[] = [
  { value: "", label: "All categories" },
  { value: "auth", label: "Sign-in and sign-out" },
  { value: "user", label: "User changes" },
  { value: "tenant", label: "Tenant changes" },
  { value: "audit", label: "Audit exports" },
];

type Category = "auth" | "user" | "tenant" | "audit";
type Sort = "at" | "-at" | "tenant" | "-tenant" | "actor" | "-actor" | "action" | "-action" | "category" | "-category";

const UNIT_MS: Record<DateRangePickerProps.TimeUnit, number> = {
  second: 1000,
  minute: 60_000,
  hour: 3_600_000,
  day: 86_400_000,
  week: 604_800_000,
  month: 2_592_000_000,
  year: 31_536_000_000,
};

/** The API's `from` and `to` for a picker value (undefined: the API's last 7 days). */
export function rangeOf(value: DateRangePickerProps.Value | null, now = new Date()): { from?: string; to?: string } {
  if (!value) return {};
  if (value.type === "relative") {
    return { from: new Date(now.getTime() - value.amount * UNIT_MS[value.unit]).toISOString(), to: now.toISOString() };
  }
  return { from: new Date(value.startDate).toISOString(), to: new Date(value.endDate).toISOString() };
}

interface Filters {
  range: DateRangePickerProps.Value | null;
  tenantId: string;
  category: string;
  actor: string;
}

const SORT_FIELDS: Record<string, string> = {
  at: "at",
  tenant: "tenant",
  actor: "actor",
  action: "action",
  category: "category",
};

function query(filters: Filters, sort: Sort, page: number) {
  const { from, to } = rangeOf(filters.range);
  return {
    from,
    to,
    tenant_id: filters.tenantId || undefined,
    category: (filters.category || undefined) as Category | undefined,
    actor_email: filters.actor.trim() || undefined,
    sort,
    page,
    page_size: PAGE_SIZE,
  };
}

const EMPTY = "-";

function isEmpty(value: unknown): boolean {
  return (
    value === null ||
    value === undefined ||
    (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0)
  );
}

/**
 * The Before and After panes' text, or null when the event has neither (a sign-out, for
 * example), so the panes are left out. One empty side shows a dash, never "null".
 */
export function beforeAfter(detail: AuditEvent["detail"]): { before: string; after: string } | null {
  const before: unknown = detail?.before;
  const after: unknown = detail?.after;
  if (isEmpty(before) && isEmpty(after)) return null;
  const text = (value: unknown) => (isEmpty(value) ? EMPTY : JSON.stringify(value, null, 2));
  return { before: text(before), after: text(after) };
}

function EventDetail({ event, onDismiss }: { event: AuditEvent | null; onDismiss: () => void }) {
  const values = event ? beforeAfter(event.detail) : null;
  return (
    <Modal visible={event !== null} onDismiss={onDismiss} header="Audit event" size="large">
      {event && (
        <SpaceBetween size="l">
          <KeyValuePairs
            columns={3}
            items={[
              { label: "Time", value: new Date(event.at).toLocaleString() },
              { label: "Tenant", value: event.tenant_name },
              { label: "Outcome", value: event.outcome },
              { label: "Actor", value: event.actor_email ?? "System" },
              { label: "Role", value: event.actor_role ?? "-" },
              { label: "Action", value: `${event.category} / ${event.action}` },
              { label: "Target", value: event.target_label ?? event.target_id ?? "-" },
              { label: "IP address", value: event.ip_address ?? "-" },
              { label: "Browser", value: event.user_agent ?? "-" },
            ]}
          />
          {values && (
            <ColumnLayout columns={2} variant="text-grid">
              {(["before", "after"] as const).map((side) => (
                <div key={side}>
                  <Box variant="awsui-key-label">{side === "before" ? "Before" : "After"}</Box>
                  <Box variant="code">
                    <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>{values[side]}</pre>
                  </Box>
                </div>
              ))}
            </ColumnLayout>
          )}
          {event.detail && Object.keys(event.detail).some((k) => k !== "before" && k !== "after") && (
            <div>
              <Box variant="awsui-key-label">Other details</Box>
              <Box variant="code">
                <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
                  {JSON.stringify(
                    Object.fromEntries(Object.entries(event.detail).filter(([k]) => k !== "before" && k !== "after")),
                    null,
                    2,
                  )}
                </pre>
              </Box>
            </div>
          )}
        </SpaceBetween>
      )}
    </Modal>
  );
}

export function Audit() {
  const { api } = useSession();
  const [draft, setDraft] = useState<Filters>({ range: DEFAULT_RANGE, tenantId: "", category: "", actor: "" });
  const [filters, setFilters] = useState<Filters>(draft);
  const [sort, setSort] = useState<Sort>("-at");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<AuditEvent | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const fetchTenants = useCallback(async () => (await call(api.GET("/api/admin/tenants"))).tenants, [api]);
  const { data: tenants } = useLoad(fetchTenants);
  const fetchPage = useCallback(
    async (): Promise<AuditPage> => {
      const result = await call(api.GET("/api/admin/audit", { params: { query: query(filters, sort, page) } }));
      // The route also answers CSV, but only when asked for it.
      if (typeof result === "string") throw new Error("expected JSON");
      return result;
    },
    [api, filters, sort, page],
  );
  const { data, error, reload } = useLoad(fetchPage);

  const tenantOptions = useMemo<SelectProps.Option[]>(
    () => [{ value: "", label: "All tenants" }, ...(tenants ?? []).map((t) => ({ value: t.id, label: t.name }))],
    [tenants],
  );

  const exportCsv = async () => {
    setExporting(true);
    setExportError(null);
    try {
      const { data: text, response } = await api.GET("/api/admin/audit", {
        params: { query: { ...query(filters, sort, 1), format: "csv" } },
        parseAs: "text",
      });
      if (!response.ok || typeof text !== "string") throw errorFrom(response.status, null);
      const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? "audit.csv";
      link.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setExportError(messageFor(caught));
    } finally {
      setExporting(false);
    }
  };

  const sortField = sort.replace(/^-/, "");
  const columns: TableProps.ColumnDefinition<AuditEvent>[] = [
    { id: "at", header: "Time", sortingField: "at", cell: (e) => new Date(e.at).toLocaleString(), isRowHeader: true },
    { id: "tenant", header: "Tenant", sortingField: "tenant", cell: (e) => e.tenant_name },
    { id: "actor", header: "Actor", sortingField: "actor", cell: (e) => e.actor_email ?? "System" },
    { id: "role", header: "Role", cell: (e) => e.actor_role ?? "-" },
    {
      id: "action",
      header: "Action",
      sortingField: "action",
      cell: (e) => (
        <Link
          onFollow={(event) => {
            event.preventDefault();
            setOpen(e);
          }}
          href="#"
        >
          {e.action}
        </Link>
      ),
    },
    { id: "target", header: "Target", cell: (e) => e.target_label ?? e.target_id ?? "-" },
    {
      id: "outcome",
      header: "Outcome",
      cell: (e) => (
        <StatusIndicator type={e.outcome === "success" ? "success" : "error"}>{e.outcome}</StatusIndicator>
      ),
    },
  ];

  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Sign-ins, sign-outs, user changes and tenant changes across all tenants."
          actions={
            <Button iconName="download" loading={exporting} onClick={() => void exportCsv()}>
              Export CSV
            </Button>
          }
        >
          Audit history
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
          variant="container"
          loading={data === null && !error}
          loadingText="Loading audit history"
          items={data?.events ?? []}
          trackBy="id"
          columnDefinitions={columns}
          sortingColumn={{ sortingField: SORT_FIELDS[sortField] ?? "at" }}
          sortingDescending={sort.startsWith("-")}
          onSortingChange={({ detail }) => {
            const field = detail.sortingColumn.sortingField ?? "at";
            setSort(((detail.isDescending ? "-" : "") + field) as Sort);
            setPage(1);
          }}
          onRowClick={({ detail }) => setOpen(detail.item)}
          empty={<Box textAlign="center">No events match these filters.</Box>}
          header={
            <Header counter={data ? `(${data.total})` : undefined}>Events</Header>
          }
          filter={
            <form
              onSubmit={(event) => {
                event.preventDefault();
                setFilters(draft);
                setPage(1);
              }}
            >
              <SpaceBetween direction="horizontal" size="s" alignItems="end">
                <FormField label="Date range">
                  <DateRangePicker
                    value={draft.range}
                    relativeOptions={RELATIVE_OPTIONS}
                    isValidRange={(range) =>
                      range?.type === "absolute" && (!range.startDate || !range.endDate)
                        ? { valid: false, errorMessage: "Choose a start and an end." }
                        : { valid: true }
                    }
                    rangeSelectorMode="default"
                    onChange={({ detail }) => setDraft({ ...draft, range: detail.value })}
                    placeholder="Last 7 days"
                  />
                </FormField>
                <FormField label="Tenant">
                  <Select
                    selectedOption={tenantOptions.find((o) => o.value === draft.tenantId) ?? tenantOptions[0] ?? null}
                    options={tenantOptions}
                    onChange={({ detail }) => setDraft({ ...draft, tenantId: detail.selectedOption.value ?? "" })}
                  />
                </FormField>
                <FormField label="Category">
                  <Select
                    selectedOption={CATEGORIES.find((o) => o.value === draft.category) ?? CATEGORIES[0] ?? null}
                    options={CATEGORIES}
                    onChange={({ detail }) => setDraft({ ...draft, category: detail.selectedOption.value ?? "" })}
                  />
                </FormField>
                <FormField label="Actor's email">
                  <Input
                    value={draft.actor}
                    type="search"
                    placeholder="Contains"
                    onChange={({ detail }) => setDraft({ ...draft, actor: detail.value })}
                  />
                </FormField>
                <Button formAction="submit">Apply</Button>
              </SpaceBetween>
            </form>
          }
          pagination={
            <Pagination currentPageIndex={page} pagesCount={pages} onChange={({ detail }) => setPage(detail.currentPageIndex)} />
          }
        />
      </SpaceBetween>
      <EventDetail event={open} onDismiss={() => setOpen(null)} />
    </ContentLayout>
  );
}
