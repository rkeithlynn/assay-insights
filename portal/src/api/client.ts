/**
 * The API client, generated from the API's OpenAPI schema (BUILD_SPEC.md section 10):
 * `uv run scripts/export_openapi.py`, then `npm run gen:api`. Same origin (`/api/*` goes to
 * the API through CloudFront), so no CORS and no credentials in cookies: the access token
 * goes in the Authorization header.
 */

import createClient, { type Middleware } from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Me = Schemas["Me"];
export type UserOut = Schemas["UserOut"];
export type TenantOut = Schemas["TenantOut"];
export type AdminOut = Schemas["AdminOut"];
export type AuditEvent = Schemas["AuditEvent"];
export type AuditPage = Schemas["AuditPage"];
export type TenantModels = Schemas["TenantModels"];
export type SettingsModels = Schemas["SettingsModels"];
export type CatalogModel = Schemas["CatalogModel"];
// Build step 6: batches, uploads and ingestion history.
export type BatchOut = Schemas["BatchOut"];
export type BatchSummary = Schemas["BatchSummary"];
export type FileOut = Schemas["FileOut"];
export type RegisteredFile = Schemas["RegisteredFile"];
export type DocumentRow = Schemas["DocumentRow"];
export type DocumentPage = Schemas["DocumentPage"];
export type DocumentDetail = Schemas["DocumentDetail"];
export type DocumentStatus = DocumentRow["status"];
export type IngestionRow = Schemas["IngestionRow"];
export type IngestionPage = Schemas["IngestionPage"];
export type IngestionDetail = Schemas["IngestionDetail"];

export type Api = ReturnType<typeof createClient<paths>>;

/** An API error: the HTTP status and the API's short code (`detail`). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

export function errorFrom(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return new ApiError(status, detail);
  if (status === 422) return new ApiError(status, "invalid_input");
  return new ApiError(status, `http_${status}`);
}

export function apiClient(token: () => string | null): Api {
  const auth: Middleware = {
    onRequest({ request }) {
      const value = token();
      if (value) request.headers.set("Authorization", `Bearer ${value}`);
      return request;
    },
  };
  const client = createClient<paths>({ baseUrl: "" });
  client.use(auth);
  return client;
}

/** Unwrap an openapi-fetch result: the data, or an ApiError. */
export async function call<T>(
  promise: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await promise;
  if (!response.ok) throw errorFrom(response.status, error);
  return data as T;
}
