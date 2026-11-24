import { describe, expect, it } from "vitest";

import { canSee, landingFor, navigationWidth, SCREENS, screensFor } from "./navigation";

describe("navigation", () => {
  it("shows each role only its screens", () => {
    expect(screensFor("tenant-admin").map((s) => s.id)).toEqual(["home", "upload", "ingestion", "users", "settings"]);
    expect(screensFor("tenant-user").map((s) => s.id)).toEqual(["home", "ingestion"]);
    expect(screensFor("master-admin").map((s) => s.id)).toEqual(["tenants", "ingestion", "audit"]);
    expect(screensFor("master-user").map((s) => s.id)).toEqual(["tenants", "ingestion", "audit"]);
    expect(screensFor("someone-else")).toEqual([]);
  });

  it("never shows tenant screens to master roles or the reverse", () => {
    expect(canSee("master-admin", "/users")).toBe(false);
    expect(canSee("tenant-user", "/users")).toBe(false);
    expect(canSee("tenant-admin", "/tenants")).toBe(false);
    expect(canSee("tenant-admin", "/audit")).toBe(false);
    expect(canSee("tenant-user", "/settings")).toBe(false);
    expect(canSee("master-user", "/settings")).toBe(false);
    // Build step 6: only a tenant admin uploads; everyone sees ingestion history.
    expect(canSee("tenant-user", "/upload")).toBe(false);
    expect(canSee("master-admin", "/upload")).toBe(false);
    for (const role of ["tenant-admin", "tenant-user", "master-admin", "master-user"]) {
      expect(canSee(role, "/ingestion")).toBe(true);
    }
  });

  it("lands master roles on Tenants and tenant roles on Home", () => {
    expect(landingFor("master-user")).toBe("/tenants");
    expect(landingFor("tenant-admin")).toBe("/");
  });

  it("sizes the navigation to its longest title, within Cloudscape's limits", () => {
    // "Ingestion history" (build step 6) is the longest title on both portals.
    expect(navigationWidth(screensFor("tenant-admin"))).toBe(218);
    expect(navigationWidth(screensFor("master-admin"))).toBe(218);
    expect(navigationWidth([SCREENS.home, SCREENS.users])).toBe(150);
    expect(navigationWidth([])).toBe(150);
    expect(navigationWidth([{ ...SCREENS.home, title: "x".repeat(60) }])).toBe(280);
  });
});
