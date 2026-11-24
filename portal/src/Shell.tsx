/**
 * The shell (BUILD_SPEC.md section 11): Cloudscape app layout. Top bar with the logo, the
 * tenant name, a "Development" tag and the user menu (Change password, Sign out).
 *
 * The banner's logo is the square mark next to the product name, both in one link to the
 * role's home screen. The mark is imported, so Vite bundles it with the portal (served from
 * the portal's own origin or inlined as a data: URL, both already in the CSP's img-src).
 * Cloudscape draws the logo at most 32 px high (24 px on a narrow screen); the mark is drawn
 * for favicon sizes, so it stays legible there. The full wordmark would be too small.
 */

import AppLayout from "@cloudscape-design/components/app-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import { type ReactNode, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import markUrl from "@assets/assay-insights-mark.svg";

import { screensFor } from "./navigation";
import { ChangePassword } from "./pages/ChangePassword";
import { useSession } from "./session";

export const PRODUCT_NAME = "Assay Insights";
export const LOGO_ALT = "Assay Insights logo";

export function Shell({ children }: { children: ReactNode }) {
  const { me, signOut } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [changingPassword, setChangingPassword] = useState(false);
  const screens = screensFor(me.role);
  const home = screens[0]?.path ?? "/";

  return (
    <>
      <div id="top-nav" style={{ position: "sticky", top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{
            href: home,
            title: PRODUCT_NAME,
            logo: { src: markUrl, alt: LOGO_ALT },
            onFollow: (event) => {
              event.preventDefault();
              void navigate(home);
            },
          }}
          utilities={[
            {
              type: "button",
              text: "Development",
              iconName: "status-info",
              ariaLabel: "Development environment",
              disableUtilityCollapse: true,
            },
            { type: "button", text: me.tenant.name, ariaLabel: `Organization: ${me.tenant.name}` },
            {
              type: "menu-dropdown",
              text: me.user.name,
              description: me.user.email,
              iconName: "user-profile",
              items: [
                { id: "change-password", text: "Change password" },
                { id: "sign-out", text: "Sign out" },
              ],
              onItemClick: ({ detail }) => {
                if (detail.id === "change-password") setChangingPassword(true);
                if (detail.id === "sign-out") void signOut();
              },
            },
          ]}
        />
      </div>
      <AppLayout
        headerSelector="#top-nav"
        toolsHide
        navigation={
          <SideNavigation
            activeHref={location.pathname}
            items={screens.map((screen) => ({ type: "link", text: screen.title, href: screen.path }))}
            onFollow={(event) => {
              event.preventDefault();
              void navigate(event.detail.href);
            }}
          />
        }
        content={children}
      />
      <ChangePassword visible={changingPassword} onDismiss={() => setChangingPassword(false)} />
    </>
  );
}
