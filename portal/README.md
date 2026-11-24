# portal

React + TypeScript + Vite + Cloudscape single-page app (BUILD_SPEC.md section 11). One app
serves both hosts; the host decides the user pool (`src/config.ts`), and `GET /api/me`
decides the navigation (`src/navigation.ts`).

- Sign-in: managed login, authorization code flow with PKCE (`src/auth.ts`, oidc-client-ts).
  Tokens live in sessionStorage only.
- `config.json` (pool and client IDs, login domains, hosts) is written by Pulumi into the
  portal bucket; the code holds no identifiers.
- The API client is generated: `uv run scripts/export_openapi.py` (repository root), then
  `npm run gen:api` here. Commit `src/api/openapi.json` and `src/api/schema.d.ts`.
- Change password and MFA setup call Cognito directly with the user's access token
  (`src/cognito.ts`); passwords never reach the API.

Built and uploaded by `scripts/build_portal.py` during `pulumi up` (in `portal/Dockerfile`,
Node 24, `npm ci --ignore-scripts`). Locally (PowerShell, in `portal/`):

    npm ci --ignore-scripts
    npm run typecheck; npm run lint; npm test; npm run build

Dependencies are exact versions. Add or bump one only with a release at least two weeks old,
resolved inside the pinned Node image with `npm install --before=<date two weeks ago>`.
