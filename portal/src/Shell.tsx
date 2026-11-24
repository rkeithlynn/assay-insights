/**
 * The shell (BUILD_SPEC.md section 11): Cloudscape app layout. Top bar with the logo, the
 * tenant name, a "Development" tag and the user menu (Change password, Sign out).
 *
 * The banner's logo is the mark next to the product name, both in one link to the role's
 * home screen. It is the banner's own version of the mark (`assay-insights-mark-banner.svg`):
 * the favicon's navy tile is the banner's colour and would vanish into it, so the banner
 * version is the artwork alone, cropped to fill Cloudscape's 32 px logo height (24 px on a
 * narrow screen). It declares its width and height: without them Cloudscape sizes the
 * identity as if the image had no width, and cuts the product name to "Assay Insi...".
 * Imported, so Vite bundles it with the portal (served from the portal's own origin or
 * inlined as a data: URL, both already in the CSP's img-src).
 *
 * The navigation is as wide as the role's own items need (`navigationWidth`), not
 * Cloudscape's 280 px: room for the longest title and the collapse button, no more.
 */

import AppLayout from "@cloudscape-design/components/app-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import { type ReactNode, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import markUrl from "@assets/assay-insights-mark-banner.svg";

import { navigationWidth, screensFor } from "./navigation";
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
        navigationWidth={navigationWidth(screens)}
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
