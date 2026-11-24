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
const PLATFORM_ID = "00000000-0000-4000-8000-0000000000f1";
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
