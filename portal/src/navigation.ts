/**
 * Screens per role (BUILD_SPEC.md section 11). Build step 5 has Tenants and Users; the
 * others arrive in later steps and are not shown until they exist (owner decision D7).
 * The API enforces every permission; the portal only hides what a role cannot do.
 */

export type Role = "tenant-admin" | "tenant-user" | "master-admin" | "master-user";

export interface Screen {
  id: "home" | "tenants" | "users";
  title: string;
  path: string;
}

export const SCREENS: Record<Screen["id"], Screen> = {
  home: { id: "home", title: "Home", path: "/" },
  tenants: { id: "tenants", title: "Tenants", path: "/tenants" },
  users: { id: "users", title: "Users", path: "/users" },
};

const BY_ROLE: Record<Role, Screen["id"][]> = {
  // Ask, Upload, Ingestion history, Fraud review and Settings arrive in steps 6 to 8.
  "tenant-admin": ["home", "users"],
  "tenant-user": ["home"],
  // Ingestion history and Audit history arrive later.
  "master-admin": ["tenants"],
  "master-user": ["tenants"],
};

export function isRole(value: string): value is Role {
  return value in BY_ROLE;
}

export function screensFor(role: string): Screen[] {
  return isRole(role) ? BY_ROLE[role].map((id) => SCREENS[id]) : [];
}

/** Where a role lands after sign-in. */
export function landingFor(role: string): string {
  return screensFor(role)[0]?.path ?? "/";
}

export function canSee(role: string, path: string): boolean {
  return screensFor(role).some((screen) => screen.path === path);
}
