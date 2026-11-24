import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";

import markUrl from "@assets/assay-insights-mark.svg";

import type { Me } from "./api/client";
import { type Session, SessionContext } from "./session";
import { LOGO_ALT, PRODUCT_NAME, Shell } from "./Shell";

function me(pool: Me["pool"], role: string): Me {
  return {
    mfa_required: false,
    pool,
    role,
    tenant: { id: "00000000-0000-4000-8000-000000000001", name: "Example Tenant" },
    user: { id: "00000000-0000-4000-8000-000000000002", name: "Ada", email: "ada@example.test" },
  };
}

function Where() {
  return <p>screen at {useLocation().pathname}</p>;
}

function renderShell(who: Me, at: string) {
  const session = { me: who, signOut: () => Promise.resolve() } as unknown as Session;
  return render(
    <SessionContext.Provider value={session}>
      <MemoryRouter initialEntries={[at]}>
        <Shell>
          <Where />
        </Shell>
      </MemoryRouter>
    </SessionContext.Provider>,
  );
}

describe("top banner", () => {
  // Both hosts render the same shell: the tenant portal (tenant pool) and the admin portal
  // (platform pool), whose home screens differ.
  it.each([
    { host: "tenant portal", who: me("tenant", "tenant-admin"), home: "/" },
    { host: "admin portal", who: me("platform", "master-admin"), home: "/tenants" },
  ])("shows the logo next to the product name, linked to home ($host)", async ({ who, home }) => {
    renderShell(who, "/users");

    // The visible bar is the banner landmark (Cloudscape's aria-hidden measuring copy is not).
    const banner = screen.getByRole("banner");
    const logo = within(banner).getByRole("img", { name: LOGO_ALT });
    expect(logo).toHaveAttribute("src", markUrl);
    expect(logo).toHaveAttribute("alt", LOGO_ALT);

    const link = logo.closest("a");
    expect(link).not.toBeNull();
    expect(link).toHaveAttribute("href", home);
    // The product name sits in the same link. Cloudscape drops it when the bar is too narrow
    // (and jsdom has no layout, so always here), keeping it in its measuring copy only.
    const titles = within(document.getElementById("top-nav") as HTMLElement).getAllByText(PRODUCT_NAME);
    expect(titles.length).toBeGreaterThan(0);
    for (const title of titles) expect(title.closest("a")).toHaveAttribute("href", home);

    await userEvent.click(logo);
    expect(screen.getByText(`screen at ${home}`)).toBeInTheDocument();
  });
});
