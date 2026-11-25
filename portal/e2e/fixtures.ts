/**
 * The rendered portal, signed in, with a stand-in API (build step 5).
 *
 * The real production build (`vite build`) is served by `vite preview`; nothing here runs
 * against AWS. `/config.json` and every `/api/*` call are answered by Playwright, every other
 * request is refused, and the user is signed in by putting a stored oidc-client-ts user into
 * sessionStorage, exactly where the portal keeps it after a real sign-in. The tokens are
 * placeholders: the portal never reads them, and the API that would is not here.
 *
 * The two hosts are two origins of the same server, so each has its own sessionStorage and
 * the portal picks the pool from the host, as it does behind CloudFront: the tenant portal is
 * `localhost`, the admin portal `127.0.0.1`.
 */

import type { BrowserContext, Page, Route } from "@playwright/test";

export const PORT = 4173;
export const HOSTS = { tenant: `localhost:${PORT}`, admin: `127.0.0.1:${PORT}` } as const;
const REGION = "us-east-1";
const POOLS = {
  tenant: { userPoolId: "us-east-1_TENANTPOOL", clientId: "tenantclient", loginUrl: "https://tenant.auth.example.test" },
  platform: { userPoolId: "us-east-1_PLATFORMPOOL", clientId: "platformclient", loginUrl: "https://admin.auth.example.test" },
} as const;

export type Persona = "tenant-admin" | "tenant-user" | "master-admin" | "master-user";

const TENANT_ID = "00000000-0000-4000-8000-0000000000a1";
const PLATFORM_ID = "00000000-0000-4000-8000-0000000000e1";
const HAIKU = "00000000-0000-4000-8000-00000000c001";
const SONNET = "00000000-0000-4000-8000-00000000c002";
const OPUS = "00000000-0000-4000-8000-00000000c003";

export function hostFor(persona: Persona): string {
  return persona.startsWith("tenant") ? HOSTS.tenant : HOSTS.admin;
}

function me(persona: Persona, mfaRequired: boolean) {
  const tenant = persona.startsWith("tenant")
    ? { id: TENANT_ID, name: "Infinity Loans" }
    : { id: PLATFORM_ID, name: "Assay Insights, LLC" };
  return {
    mfa_required: mfaRequired,
    pool: persona.startsWith("tenant") ? "tenant" : "platform",
    role: persona,
    tenant,
    user: { id: "00000000-0000-4000-8000-0000000000u0", name: "Ada Lovelace", email: "ada@example.test" },
  };
}

const ROLES = ["tenant-admin", "tenant-user"] as const;
const STATUSES = ["active", "active", "invited", "disabled"] as const;
const USERS = Array.from({ length: 9 }, (_, i) => ({
  id: `00000000-0000-4000-8000-0000000001${String(i).padStart(2, "0")}`,
  name: `User Number ${i + 1}`,
  email: `user${i + 1}@example.test`,
  role: ROLES[i % 2],
  status: STATUSES[i % 4],
  last_sign_in_at: i % 4 === 2 ? null : "2026-11-24T15:04:05Z",
}));

const TENANTS = [
  { id: PLATFORM_ID, name: "Assay Insights, LLC", kind: "platform", status: "active", users: 2, documents: 0 },
  { id: TENANT_ID, name: "Infinity Loans", kind: "business", status: "active", users: 9, documents: 120 },
  { id: "00000000-0000-4000-8000-0000000000a2", name: "Isolation Test A", kind: "business", status: "active", users: 1, documents: 0 },
  { id: "00000000-0000-4000-8000-0000000000a3", name: "Isolation Test B", kind: "business", status: "suspended", users: 1, documents: 0 },
  { id: "00000000-0000-4000-8000-0000000000a4", name: "Northwind Credit", kind: "business", status: "failed", users: 0, documents: 0 },
].map((t) => ({ ...t, created_at: "2026-11-23T10:00:00Z" }));

const CATALOG = [
  { id: HAIKU, name: "Claude Haiku", profile_id: "us.example.haiku", jobs: ["ingest", "query"], price_in: "1.000000", price_out: "5.000000" },
  { id: SONNET, name: "Claude Sonnet", profile_id: "us.example.sonnet", jobs: ["ingest", "query"], price_in: "3.000000", price_out: "15.000000" },
  { id: OPUS, name: "Claude Opus", profile_id: "us.example.opus", jobs: ["query"], price_in: "15.000000", price_out: "75.000000" },
];

// Long, realistic addresses and the longest action names: the table must still fit at 1280 px.
// Actor addresses up to about 33 characters (the longest here) fit at 1280 px on one line; a longer
// target wraps.
const ACTORS = ["compliance@northwindcredit.com", "alexandra.richardson@infinity.com", "ada@example.test"];
const LONG_EMAILS = [
  "alexandra.richardson-whitfield@infinityloans.example",
  "operations.compliance.team@northwindcredit.example",
  "ada@example.test",
];
const AUDIT_EVENTS = Array.from({ length: 12 }, (_, i) => ({
  id: `00000000-0000-4000-8000-0000000002${String(i).padStart(2, "0")}`,
  at: new Date(Date.UTC(2026, 10, 24, 15, 59 - i)).toISOString(),
  tenant_id: TENANT_ID,
  tenant_name: i % 3 === 1 ? "Assay Insights, LLC" : "Infinity Loans",
  actor_email: i % 5 === 4 ? null : (ACTORS[i % 3] ?? null),
  actor_role: i % 5 === 4 ? null : "tenant-admin",
  category: i % 2 ? "auth" : "user",
  action: ["temporary_password_accepted", "session_started", "models_available_changed", "mfa_enabled"][i % 4],
  target_type: "user",
  target_id: USERS[i % 9]?.id ?? null,
  target_label: LONG_EMAILS[(i + 1) % 3] ?? null,
  outcome: "success",
  detail: { pool: "tenant" },
  ip_address: "198.51.100.7",
  user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/142.0",
}));

// Build step 6: an open batch, ingestion history for both views. Long names on purpose: the
// tables must still fit at 1280 px.
const BATCH_ID = "cfd08356-aac4-4dfe-a3f5-0000000003b1";
// Realistic IDs: the table shows each one's first 8 characters.
const DOC = (i: number) => `${(0x3f2d2180 + i * 0x1111).toString(16)}-2e99-4991-8cee-0000000004${String(i).padStart(2, "0")}`;
const batchFile = (i: number, name: string, kind: "pdf" | "companion", status: string, extra = {}) => ({
  id: `00000000-0000-4000-8000-0000000005${String(i).padStart(2, "0")}`,
  name,
  kind,
  size_bytes: kind === "pdf" ? 2_400_000 + i * 1000 : 812,
  status,
  error_code: null,
  errors: [],
  matched: false,
  document_id: kind === "pdf" ? DOC(i) : null,
  registered_at: "2026-11-24T15:00:00Z",
  scanned_at: status === "registered" ? null : "2026-11-24T15:01:00Z",
  ...extra,
});
const BATCH = {
  id: BATCH_ID,
  name: "October applications, Atlanta branch",
  status: "open",
  opened_by: "Ada Lovelace",
  mine: true,
  opened_at: "2026-11-24T14:55:00Z",
  closed_at: null,
  closes_by: "2026-11-25T14:55:00Z",
  pdf_count: 4,
  files: [
    batchFile(1, "smith-package-final-signed-2026-10-28.pdf", "pdf", "clean", { matched: true }),
    batchFile(2, "smith-package-final-signed-2026-10-28.json", "companion", "clean", { matched: true }),
    batchFile(3, "jones-application.pdf", "pdf", "clean"),
    batchFile(4, "garcia-package.pdf", "pdf", "clean", { matched: true }),
    // Every problem at once (owner decision A2).
    batchFile(5, "garcia-package.json", "companion", "invalid", {
      matched: true,
      error_code: "companion_invalid",
      errors: [
        { code: "tenant_mismatch", key: "TenantID" },
        { code: "must_be_a_date", key: "CreateDate" },
        { code: "must_be_a_whole_number", key: "LoanTerm" },
      ],
    }),
    batchFile(6, "eicar-test.pdf", "pdf", "infected", { error_code: "threats_found" }),
    // On hold, with its reason and what to do.
    batchFile(7, "password-protected-statement.pdf", "pdf", "unsupported", { error_code: "scan_unsupported" }),
  ],
  // Only the rejected companion file stands in the way, so Close opens its dialog.
  problems: [{ file_id: "00000000-0000-4000-8000-000000000505", code: "companion_invalid" }],
};
const DOC_STATUSES = ["ready", "processing", "duplicate", "quarantined", "on_hold", "failed", "needs_review", "scanning"];
const DOCUMENTS = Array.from({ length: 10 }, (_, i) => ({
  id: DOC(i),
  received_at: new Date(Date.UTC(2026, 10, 24, 15, 50 - i)).toISOString(),
  batch_id: BATCH_ID,
  batch_name: i % 2 ? "October applications, Atlanta branch" : "Weekly upload",
  file_name: i % 3 ? `application-package-${i}.pdf` : "smith-package-final-signed-2026-10-28-with-cosigner.pdf",
  page_count: 12 + i,
  size_bytes: 2_400_000 + i * 150_000,
  has_companion: i % 2 === 0,
  status: DOC_STATUSES[i % DOC_STATUSES.length],
  error_code: i % 8 === 4 ? "pdf_encrypted" : null,
  forms_found: i % 4,
  completed_at: i % 8 === 1 ? null : new Date(Date.UTC(2026, 10, 24, 15, 52 - i)).toISOString(),
  duration_seconds: i % 8 === 1 ? null : 120 + i,
  uploaded_by: "Alexandra Richardson-Whitfield",
}));
const EVENTS = [
  { step: "scan", status: "clean", at: "2026-11-24T15:01:00Z", error_code: null, viewer: null, attempt: 1 },
  { step: "queued", status: "queued", at: "2026-11-24T15:02:00Z", error_code: null, viewer: null, attempt: 1 },
  { step: "start", status: "started", at: "2026-11-24T15:02:05Z", error_code: null, viewer: null, attempt: 1 },
  { step: "fingerprint", status: "continue", at: "2026-11-24T15:02:09Z", error_code: null, viewer: null, attempt: 1 },
  // The processing task's first step (build step 6, second half): it opened the PDF.
  { step: "inspect", status: "continue", at: "2026-11-24T15:03:20Z", error_code: null, viewer: null, attempt: 1 },
  // Step 9 (build step 6, second half): the companion file's values were stored.
  { step: "companion", status: "stored", at: "2026-11-24T15:03:30Z", error_code: null, viewer: null, attempt: 1 },
  // Who opened it: sent to tenant admins only. A long address, to check it wraps.
  { step: "viewed", status: "pdf_url", at: "2026-11-24T15:10:00Z", error_code: null, viewer: "alexandra.richardson-whitfield@infinityloans.example", attempt: 1 },
];
const MASTER_ROWS = DOCUMENTS.map((d, i) => ({
  document_id: d.id,
  tenant_id: TENANT_ID,
  tenant_name: i % 2 ? "Infinity Loans" : "Northwind Credit",
  batch_id: d.batch_id,
  // The first row (opened in the detail check) failed before step-level codes existed.
  status: i === 0 ? "failed" : d.status,
  error_code: i === 0 ? "pipeline_error" : d.error_code,
  page_count: d.page_count,
  size_bytes: d.size_bytes,
  has_companion: d.has_companion,
  forms_found: d.forms_found,
  received_at: d.received_at,
  completed_at: d.completed_at,
  duration_seconds: d.duration_seconds,
}));

function apiResponse(persona: Persona, mfaRequired: boolean, method: string, path: string): unknown {
  if (path === "/api/me") return me(persona, mfaRequired);
  if (method !== "GET") return null;
  if (path === "/api/users") return { users: USERS };
  if (path === "/api/settings/models") {
    return {
      available: { ingest: CATALOG.slice(0, 2), query: CATALOG },
      selected: { ingest: HAIKU, query: SONNET },
    };
  }
  if (path === "/api/admin/tenants") return { tenants: TENANTS };
  if (/^\/api\/admin\/tenants\/[^/]+\/admins$/.test(path)) {
    return {
      admins: [
        { id: USERS[0]?.id, name: "User Number 1", email: "user1@example.test", role: "tenant-admin", status: "active", mfa_resettable: true },
      ],
    };
  }
  if (/^\/api\/admin\/tenants\/[^/]+\/models$/.test(path)) {
    return { catalog: CATALOG, available: { ingest: [HAIKU, SONNET], query: [HAIKU, SONNET, OPUS] }, selected: { ingest: HAIKU, query: SONNET } };
  }
  if (path === "/api/batches") {
    const summary = { id: BATCH.id, name: BATCH.name, status: BATCH.status, opened_by: BATCH.opened_by, mine: true };
    return { batches: [{ ...summary, opened_at: BATCH.opened_at, closed_at: null, pdf_count: BATCH.pdf_count }] };
  }
  if (path === `/api/batches/${BATCH_ID}`) return BATCH;
  if (path === "/api/documents") {
    return { documents: DOCUMENTS, total: DOCUMENTS.length, page: 1, page_size: 50, start: "2026-11-17T16:00:00Z", end: "2026-11-24T16:00:00Z" };
  }
  if (/^\/api\/documents\/[^/]+$/.test(path)) {
    const row = DOCUMENTS[0];
    return {
      ...row,
      // Passed fingerprint, so the panel shows the "later steps not available yet" note.
      status: "processing",
      duplicate_of: null,
      pdf_available: true,
      attempt: 1,
      // The companion file's values, with long ones to check they wrap, and its uploads.
      companion: {
        version: 2,
        application_ref: "APP-2026-00123",
        loan_number: null,
        cosigner_name: "Bartholomew Featherstonehaugh-Montgomery",
        package_created_on: "2026-10-28",
        package_completed_on: "2026-11-02",
        loan_term_months: 360,
        loan_maturity_date: "2056-12-01",
        loan_interest_rate: 6.125,
        signers: [
          { name: "Alexandra Richardson-Whitfield", email: "alexandra.richardson-whitfield@infinityloans.example", phone: "+14045550100", location: "Atlanta, GA" },
        ],
        labels: [
          { key: "Branch", value: "Atlanta" },
          { key: "UnderwritingQueueAssignmentReference", value: "a-long-value-without-any-spaces-in-it-to-check-wrapping-0123456789" },
        ],
      },
      companion_files: [
        { id: "00000000-0000-4000-8000-000000000a02", name: "smith-package-corrected.json", status: "clean", error_code: null, errors: [], replacement: true, registered_at: "2026-11-24T15:20:00Z", applied_at: "2026-11-24T15:21:00Z", current: true },
        { id: "00000000-0000-4000-8000-000000000a01", name: "smith-package.json", status: "clean", error_code: null, errors: [], replacement: false, registered_at: "2026-11-24T15:00:00Z", applied_at: "2026-11-24T15:03:30Z", current: false },
      ],
      events: EVENTS,
      parts: [
        { start_page: 1, end_page: 4, form_type: "LoanApplication", method: "rule", confidence: null },
        { start_page: 5, end_page: 12, form_type: "GeneralForm", method: "model", confidence: 0.82 },
      ],
    };
  }
  if (path === "/api/admin/ingestion") {
    return { documents: MASTER_ROWS, total: MASTER_ROWS.length, page: 1, page_size: 50, start: "2026-11-17T16:00:00Z", end: "2026-11-24T16:00:00Z" };
  }
  if (/^\/api\/admin\/ingestion\/[^/]+$/.test(path)) {
    return {
      document_id: MASTER_ROWS[0]?.document_id,
      events: [
        // The master view never has `viewed` events.
        ...EVENTS.filter((e) => e.step !== "viewed").map((e) => ({ step: e.step, status: e.status, at: e.at, error_code: e.error_code, error_type: null })),
        // Both kinds of failure, to check both fit: a row from before step-level codes, and one after.
        { step: "failed", status: "failed", at: "2026-11-24T15:03:40Z", error_code: "pipeline_error", error_type: null },
        { step: "failed", status: "failed", at: "2026-11-24T15:04:40Z", error_code: "fingerprint_failed", error_type: "Lambda.TooManyRequestsException" },
        // The processing task's own marks and codes: a step that stopped, and why.
        { step: "inspect", status: "error", at: "2026-11-24T15:05:10Z", error_code: null, error_type: "TssRequestError" },
        { step: "failed", status: "failed", at: "2026-11-24T15:05:12Z", error_code: "inspect_failed", error_type: "TssRequestError" },
      ],
    };
  }
  if (path === "/api/admin/audit") {
    return { events: AUDIT_EVENTS, total: AUDIT_EVENTS.length, page: 1, page_size: 50, start: "2026-11-17T16:00:00Z", end: "2026-11-24T16:00:00Z" };
  }
  return undefined;
}

/** The stored user oidc-client-ts reads at start-up (its `user:<authority>:<client>` key). */
function storedUser(persona: Persona): { key: string; value: string } {
  const pool = persona.startsWith("tenant") ? POOLS.tenant : POOLS.platform;
  const authority = `https://cognito-idp.${REGION}.amazonaws.com/${pool.userPoolId}`;
  const user = {
    id_token: "placeholder.id.token",
    access_token: "placeholder.access.token",
    refresh_token: "placeholder-refresh-token",
    token_type: "Bearer",
    scope: "openid email profile aws.cognito.signin.user.admin",
    profile: { sub: "placeholder-sub", iss: authority, aud: pool.clientId, exp: 4102444800, iat: 1795500000 },
    expires_at: 4102444800,
  };
  return { key: `oidc.user:${authority}:${pool.clientId}`, value: JSON.stringify(user) };
}

/** Serve config.json and the API for this persona, refuse everything off the portal's server. */
export async function signIn(
  context: BrowserContext,
  persona: Persona,
  { mfaRequired = false, signedIn = true } = {},
): Promise<void> {
  if (signedIn) {
    const { key, value } = storedUser(persona);
    await context.addInitScript(
      ([k, v]) => {
        window.sessionStorage.setItem(k, v);
      },
      [key, value] as const,
    );
  }
  await context.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    const local = url.host === HOSTS.tenant || url.host === HOSTS.admin;
    if (!local) return route.abort("blockedbyclient");
    if (url.pathname === "/config.json") {
      return route.fulfill({
        json: { region: REGION, hosts: { tenant: HOSTS.tenant, admin: HOSTS.admin }, pools: POOLS },
      });
    }
    if (url.pathname.startsWith("/api/")) {
      const body = apiResponse(persona, mfaRequired, route.request().method(), url.pathname);
      if (body === undefined) return route.fulfill({ status: 404, json: { detail: "not_found" } });
      if (body === null) return route.fulfill({ status: 204 });
      return route.fulfill({ json: body });
    }
    return route.continue();
  });
}

export async function open(page: Page, persona: Persona, path: string): Promise<void> {
  await page.goto(`http://${hostFor(persona)}${path}`);
}
