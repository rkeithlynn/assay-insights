import { describe, expect, it } from "vitest";

import type { TenantOut, UserOut } from "../api/client";
import { tenantActions } from "./Tenants";
import { actionsFor, RESEND_EXPLANATION } from "./Users";

function user(overrides: Partial<UserOut>): UserOut {
  return {
    id: "u1",
    name: "Ada",
    email: "ada@example.test",
    role: "tenant-user",
    status: "active",
    last_sign_in_at: null,
    ...overrides,
  };
}

function tenant(overrides: Partial<TenantOut>): TenantOut {
  return {
    id: "t1",
    name: "Tenant",
    kind: "business",
    status: "active",
    users: 0,
    documents: 0,
    created_at: "2026-11-23T00:00:00Z",
    ...overrides,
  };
}

const ids = (items: ReturnType<typeof actionsFor>) => items.map((i) => ("id" in i ? i.id : ""));

describe("user actions", () => {
  it("offers MFA reset for tenant users only", () => {
    expect(ids(actionsFor(user({ role: "tenant-user" }), "me"))).toContain("reset-mfa");
    expect(ids(actionsFor(user({ role: "tenant-admin" }), "me"))).not.toContain("reset-mfa");
  });

  it("never offers an admin a change of their own role or a self-disable", () => {
    const self = ids(actionsFor(user({ id: "me", role: "tenant-admin" }), "me"));
    expect(self).not.toContain("make-user");
    expect(self).not.toContain("disable");
  });

  it("offers resend for invited users and reset password for active ones", () => {
    expect(ids(actionsFor(user({ status: "invited" }), "me"))).toContain("resend");
    expect(ids(actionsFor(user({ status: "invited" }), "me"))).not.toContain("reset-password");
    expect(ids(actionsFor(user({ status: "active" }), "me"))).toContain("reset-password");
    expect(ids(actionsFor(user({ status: "disabled" }), "me"))).toContain("enable");
  });
});

describe("invited users", () => {
  it("are offered a resent invitation with its reason, never a password reset", () => {
    const items = actionsFor(user({ status: "invited" }), "me");
    expect(ids(items)).not.toContain("reset-password");
    const resend = items.find((i) => "id" in i && i.id === "resend");
    expect(resend).toMatchObject({ text: "Resend invitation", secondaryText: RESEND_EXPLANATION });
    expect(RESEND_EXPLANATION).toMatch(/Forgot password can't reach them until they first sign in/);
  });
});

describe("tenant actions", () => {
  it("follows the tenant's status, and never touches the platform tenant", () => {
    expect(tenantActions(tenant({ status: "failed" }))).toEqual(["retry"]);
    expect(tenantActions(tenant({ status: "active" }))).toEqual(["suspend"]);
    expect(tenantActions(tenant({ status: "suspended" }))).toEqual(["reactivate"]);
    expect(tenantActions(tenant({ status: "provisioning" }))).toEqual([]);
    expect(tenantActions(tenant({ kind: "platform" }))).toEqual([]);
  });
});
