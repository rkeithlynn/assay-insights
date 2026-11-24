/**
 * The shell (BUILD_SPEC.md section 11): Cloudscape app layout. Top bar with the logo, the
 * tenant name, a "Development" tag and the user menu (Change password, Sign out).
 */

import AppLayout from "@cloudscape-design/components/app-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import { type ReactNode, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import logoUrl from "@assets/assay-insights-logo.svg";

import { screensFor } from "./navigation";
import { ChangePassword } from "./pages/ChangePassword";
import { useSession } from "./session";

export function Shell({ children }: { children: ReactNode }) {
  const { me, signOut } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [changingPassword, setChangingPassword] = useState(false);
  const screens = screensFor(me.role);

  return (
    <>
      <div id="top-nav" style={{ position: "sticky", top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{
            href: screens[0]?.path ?? "/",
            logo: { src: logoUrl, alt: "Assay Insights" },
            onFollow: (event) => {
              event.preventDefault();
              void navigate(screens[0]?.path ?? "/");
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
