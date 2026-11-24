/**
 * Screens per role (BUILD_SPEC.md section 11). Build step 5 has Tenants and Users; build step
 * 6 adds Upload and Ingestion history. The others arrive in later steps and are not shown
 * until they exist (owner decision D7).
 * The API enforces every permission; the portal only hides what a role cannot do.
 */

export type Role = "tenant-admin" | "tenant-user" | "master-admin" | "master-user";

export interface Screen {
  id: "home" | "upload" | "ingestion" | "tenants" | "users" | "settings" | "audit";
  title: string;
  path: string;
}

export const SCREENS: Record<Screen["id"], Screen> = {
  home: { id: "home", title: "Home", path: "/" },
  upload: { id: "upload", title: "Upload", path: "/upload" },
  // The tenant view and the master view (all tenants) share the path; the pool decides.
  ingestion: { id: "ingestion", title: "Ingestion history", path: "/ingestion" },
  tenants: { id: "tenants", title: "Tenants", path: "/tenants" },
  users: { id: "users", title: "Users", path: "/users" },
  settings: { id: "settings", title: "Settings", path: "/settings" },
  audit: { id: "audit", title: "Audit history", path: "/audit" },
};

const BY_ROLE: Record<Role, Screen["id"][]> = {
  // Ask and Fraud review arrive in steps 7 and 8.
  "tenant-admin": ["home", "upload", "ingestion", "users", "settings"],
  "tenant-user": ["home", "ingestion"],
  "master-admin": ["tenants", "ingestion", "audit"],
  "master-user": ["tenants", "ingestion", "audit"],
};

export function isRole(value: string): value is Role {
  return value in BY_ROLE;
}

export function screensFor(role: string): Screen[] {
  return isRole(role) ? BY_ROLE[role].map((id) => SCREENS[id]) : [];
}

// The navigation's width (px) for a list of screens: room for the longest title, the
// panel's padding and the collapse button, no more. Cloudscape's default is 280 px. The
// estimate per character suits Open Sans at 14 px; the rendered checks (portal/e2e) verify
// that no title is cut off or runs under the collapse button.
const NAV_PX_PER_CHAR = 7.5;
const NAV_CHROME_PX = 90;
const NAV_MIN_PX = 150;
const NAV_MAX_PX = 280;

export function navigationWidth(screens: Screen[]): number {
  const longest = Math.max(0, ...screens.map((screen) => screen.title.length));
  const width = Math.ceil(longest * NAV_PX_PER_CHAR + NAV_CHROME_PX);
  return Math.min(NAV_MAX_PX, Math.max(NAV_MIN_PX, width));
}

/** Where a role lands after sign-in. */
export function landingFor(role: string): string {
  return screensFor(role)[0]?.path ?? "/";
}

export function canSee(role: string, path: string): boolean {
  return screensFor(role).some((screen) => screen.path === path);
}
