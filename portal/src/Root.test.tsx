import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
const redirectTo = vi.hoisted(() => vi.fn());
vi.mock("./auth", async (actual) => ({
  ...(await actual<typeof import("./auth")>()),
  userManager: () => manager,
  redirectTo,
}));
vi.mock("./config", async (actual) => ({
  ...(await actual<typeof import("./config")>()),
  loadConfig: () => Promise.resolve({}),
  poolFor: () => ({ kind: "tenant", region: "us-east-1", host: "portal.example.test", idleTimeoutMinutes: 30 }),
}));
vi.mock("./App", async () => {
  const { useSession } = await import("./session");
  return {
    App: () => {
      const { signOut } = useSession();
      return (
        <>
          <p>signed in</p>
          <button onClick={() => void signOut()}>Sign out</button>
        </>
      );
    },
  };
});

import { IDLE_NOTE_KEY, Root, SIGNED_OUT_ELSEWHERE, signedOutIdle } from "./Root";
import { SIGN_OUT_CHANNEL } from "./signout";

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

// Another tab of the same portal, as the browser would see it.
let otherTab: BroadcastChannel;
let heardByOtherTab: unknown[];

beforeEach(() => {
  api.GET.mockImplementation(() => ok(ME));
  api.POST.mockImplementation(() => ok(undefined));
  manager.signinRedirectCallback.mockResolvedValue(USER);
  manager.getUser.mockResolvedValue(USER);
  manager.revokeTokens.mockResolvedValue(undefined);
  manager.removeUser.mockResolvedValue(undefined);
  heardByOtherTab = [];
  otherTab = new BroadcastChannel(SIGN_OUT_CHANNEL);
  otherTab.onmessage = (event: MessageEvent<unknown>) => heardByOtherTab.push(event.data);
});

afterEach(() => {
  otherTab.close();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

const posts = (path: string) => api.POST.mock.calls.filter(([p]) => p === path);
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

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

describe("sign-out across tabs", () => {
  it("tells the portal's other tabs, and the tab that signed out ignores its own message", async () => {
    render(<Root />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await vi.waitFor(() => expect(redirectTo).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(heardByOtherTab).toEqual([{ type: "signed-out", reason: "user" }]));
    // This tab: one sign-out row, its own refresh token revoked once, then Cognito's logout.
    expect(posts("/api/auth/signed-out")).toHaveLength(1);
    expect(manager.revokeTokens).toHaveBeenCalledExactlyOnceWith(["refresh_token"]);
    expect(String(redirectTo.mock.calls[0]?.[0])).toContain("/logout?");
    await settle();
    expect(manager.revokeTokens).toHaveBeenCalledOnce();
  });

  it("ends this tab's session when another tab signs out", async () => {
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    otherTab.postMessage({ type: "signed-out" });
    expect(await screen.findByText(SIGNED_OUT_ELSEWHERE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    // Its own refresh token revoked and its tokens cleared...
    expect(manager.revokeTokens).toHaveBeenCalledExactlyOnceWith(["refresh_token"]);
    expect(manager.removeUser).toHaveBeenCalledOnce();
    // ...but no second sign-out row, and no trip to Cognito's logout from this tab.
    expect(posts("/api/auth/signed-out")).toHaveLength(0);
    expect(redirectTo).not.toHaveBeenCalled();
  });

  it("still ends this tab's session if the revocation fails", async () => {
    manager.revokeTokens.mockRejectedValue(new Error("offline"));
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    otherTab.postMessage({ type: "signed-out" });
    expect(await screen.findByText(SIGNED_OUT_ELSEWHERE)).toBeInTheDocument();
    expect(manager.removeUser).toHaveBeenCalledOnce();
  });

  it("says why when another tab signed out for inactivity", async () => {
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    otherTab.postMessage({ type: "signed-out", reason: "idle_timeout" });
    expect(await screen.findByText(signedOutIdle(30))).toBeInTheDocument();
    expect(posts("/api/auth/signed-out")).toHaveLength(0);
  });

  it.each([{ type: "something-else" }, "signed-out", null, 42])("ignores any other message (%j)", async (data) => {
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    otherTab.postMessage(data);
    await settle();
    expect(screen.getByText("signed in")).toBeInTheDocument();
    expect(manager.revokeTokens).not.toHaveBeenCalled();
    expect(manager.removeUser).not.toHaveBeenCalled();
  });
});

describe("idle timeout", () => {
  afterEach(() => {
    vi.useRealTimers();
    window.sessionStorage.clear();
  });

  it("warns 2 minutes before, then signs out as the button does, recorded as idle", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(28 * 60_000);
    expect(await screen.findByText("Are you still there?")).toBeInTheDocument();
    expect(posts("/api/auth/signed-out")).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(2 * 60_000);
    await vi.waitFor(() => expect(redirectTo).toHaveBeenCalledOnce());
    expect(posts("/api/auth/signed-out")).toEqual([
      ["/api/auth/signed-out", { body: { reason: "idle_timeout", idle_minutes: 30 } }],
    ]);
    await vi.waitFor(() => expect(heardByOtherTab).toEqual([{ type: "signed-out", reason: "idle_timeout" }]));
    expect(manager.revokeTokens).toHaveBeenCalledExactlyOnceWith(["refresh_token"]);
    // The sign-in screen this tab comes back to will say why.
    expect(window.sessionStorage.getItem(IDLE_NOTE_KEY)).toBe("30");
  });

  it("Stay signed in counts as activity", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Root />);
    expect(await screen.findByText("signed in")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(29 * 60_000);
    await userEvent.click(await screen.findByRole("button", { name: "Stay signed in" }));
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(posts("/api/auth/signed-out")).toHaveLength(0);
    expect(redirectTo).not.toHaveBeenCalled();
  });

  it("shows the reason on the sign-in screen after an idle sign-out", async () => {
    window.sessionStorage.setItem(IDLE_NOTE_KEY, "30");
    manager.getUser.mockResolvedValue(null);
    render(<Root />);
    expect(await screen.findByText(signedOutIdle(30))).toBeInTheDocument();
    expect(window.sessionStorage.getItem(IDLE_NOTE_KEY)).toBeNull();
  });
});
