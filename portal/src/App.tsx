/** The signed-in portal: MFA setup first if required, otherwise the shell and its screens. */

import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { canSee, landingFor } from "./navigation";
import { Audit } from "./pages/Audit";
import { Home } from "./pages/Home";
import { MfaSetup } from "./pages/MfaSetup";
import { Settings } from "./pages/Settings";
import { Tenants } from "./pages/Tenants";
import { Users } from "./pages/Users";
import { useSession } from "./session";
import { Shell } from "./Shell";

export function App() {
  const { me } = useSession();
  // A tenant-admin without MFA can use nothing else until it is set up (the API refuses
  // every other route with mfa_required).
  if (me.mfa_required) return <MfaSetup />;
  const role = me.role;
  const landing = landingFor(role);
  return (
    <BrowserRouter>
      <Shell>
        <Routes>
          {canSee(role, "/") && <Route path="/" element={<Home />} />}
          {canSee(role, "/tenants") && <Route path="/tenants" element={<Tenants />} />}
          {canSee(role, "/users") && <Route path="/users" element={<Users />} />}
          {canSee(role, "/settings") && <Route path="/settings" element={<Settings />} />}
          {canSee(role, "/audit") && <Route path="/audit" element={<Audit />} />}
          <Route path="*" element={<Navigate to={landing} replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
