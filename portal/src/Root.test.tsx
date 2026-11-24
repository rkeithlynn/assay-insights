import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  GET: vi.fn(),
  POST: vi.fn(),
}));
const manager = vi.hoisted(() => ({
  signinRedirectCallback: vi.fn(),
  getUser: vi.fn(),
  signinSilent: vi.fn(),
  removeUser: vi.fn(),
  revokeTokens: vi.fn(),
  signinRedirect: vi.fn(),
  events: { addUserLoaded: vi.fn() },
}));

vi.mock("./api/client", async (actual) => ({
  ...(await actual<typeof import("./api/client")>()),
  apiClient: () => api,
}));
vi.mock("./auth", async (actual) => ({
  ...(await actual<typeof import("./auth")>()),
  userManager: () => manager,
}));
vi.mock("./config", async (actual) => ({
  ...(await actual<typeof import("./config")>()),
  loadConfig: () => Promise.resolve({}),
  poolFor: () => ({ kind: "tenant", region: "us-east-1", host: "portal.example.test" }),
}));
vi.mock("./App", () => ({ App: () => <p>signed in</p> }));

import { Root } from "./Root";

const ME = {
  mfa_required: false,
  pool: "tenant",
  role: "tenant-user",
  tenant: { id: "00000000-0000-4000-8000-000000000001", name: "Example Tenant" },
  user: { id: "00000000-0000-4000-8000-000000000002", name: "Ada", email: "ada@example.test" },
};
const USER = { expired: false, access_token: "token", refresh_token: "refresh" };
const ok = (data: unknown) => Promise.resolve({ data, response: { ok: true, status: 200 } });

const sessionStarts = () => api.POST.mock.calls.filter(([path]) => path === "/api/auth/session-started");

beforeEach(() => {
  api.GET.mockImplementation(() => ok(ME));
  api.POST.mockImplementation(() => ok(undefined));
  manager.signinRedirectCallback.mockResolvedValue(USER);
  manager.getUser.mockResolvedValue(USER);
});

afterEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("session start", () => {
  it("is reported once after a sign-in redirect", async () => {
    window.history.replaceState(null, "", "/auth/callback?code=abc&state=xyz");
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    expect(manager.signinRedirectCallback).toHaveBeenCalledOnce();
    expect(sessionStarts()).toHaveLength(1);
    // The callback's code leaves the address bar.
    expect(window.location.pathname).toBe("/");
  });

  it("is not reported when a signed-in tab reloads", async () => {
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    expect(manager.getUser).toHaveBeenCalledOnce();
    expect(sessionStarts()).toHaveLength(0);
  });

  it("is not reported when the account check refuses the user", async () => {
    window.history.replaceState(null, "", "/auth/callback?code=abc&state=xyz");
    api.GET.mockImplementation(() =>
      Promise.resolve({ error: { detail: "tenant_suspended" }, response: { ok: false, status: 403 } }),
    );
    render(<Root />);
    expect(await screen.findByText("Access refused")).toBeInTheDocument();
    expect(sessionStarts()).toHaveLength(0);
  });

  it("never holds up the sign-in when the report fails", async () => {
    window.history.replaceState(null, "", "/auth/callback?code=abc&state=xyz");
    api.POST.mockImplementation(() => Promise.reject(new TypeError("network down")));
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    expect(sessionStarts()).toHaveLength(1);
  });
});
