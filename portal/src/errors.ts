/** Plain-language messages for the API's error codes (`detail`). */

import { ApiError } from "./api/client";

const MESSAGES: Record<string, string> = {
  user_exists: "A user with that email address is already in this organization.",
  email_in_use: "That email address is already used by another organization.",
  last_admin: "The organization must keep at least one admin who is not disabled.",
  cannot_change_self: "You cannot change your own role or disable yourself.",
  wrong_state: "The user is not in a state that allows this. Refresh the list and try again.",
  user_not_found: "That user no longer exists. Refresh the list.",
  tenant_not_found: "That tenant no longer exists. Refresh the list.",
  not_failed: "Only a tenant whose provisioning failed can be retried.",
  status_change_not_allowed: "The tenant's status does not allow that change.",
  first_admin_unknown: "The tenant's first admin is not on record, so it cannot be retried.",
  cognito_failed: "The sign-in service did not respond. Try again in a minute.",
  forbidden: "You do not have permission to do that.",
  mfa_required: "Set up multi-factor authentication to continue.",
  tenant_suspended: "Your organization's access is suspended. Contact Assay Insights.",
  tenant_not_active: "Your organization is not active yet.",
  user_disabled: "Your account is disabled. Contact your organization's admin.",
  account_not_found: "No account was found for this sign-in.",
  role_mismatch: "Your role has changed. Sign in again.",
  session_revoked: "Your session has ended. Sign in again.",
  invalid_token: "Your session has ended. Sign in again.",
  invalid_input: "Some of the values are not valid.",
  model_not_available: "That model is not available to your organization.",
  model_not_in_catalog: "That model is not in the catalog or not approved for that job.",
  invalid_range: "Choose a date range of up to 13 months, with the start before the end.",
  // Build step 6: batches and uploads.
  batch_already_open: "You already have an open batch. Close or discard it first.",
  batch_not_open: "That batch is no longer open. Refresh the page.",
  batch_not_found: "That batch no longer exists. Refresh the page.",
  not_batch_owner: "Only the admin who opened a batch can change it.",
  batch_not_ready: "The batch cannot close yet. See the files marked below.",
  file_not_found: "That file is no longer in the batch. Refresh the page.",
  unsupported_file_type: "Only PDF files and their JSON companion files can be uploaded.",
  invalid_file_name: "A file name may not be empty or contain slashes or control characters.",
  file_too_large: "A PDF may be at most 100 MB and a companion file at most 64 KB.",
  empty_file: "That file is empty.",
  duplicate_file_name: "The batch already has a file with that name.",
  too_many_pdfs: "A batch may hold at most 200 PDFs.",
  too_many_companion_files: "A batch may hold at most one companion file per PDF.",
  too_many_files_in_request: "Add at most 50 files at a time.",
  no_files: "Choose at least one file.",
  storage_unavailable: "Storage did not respond. Try again in a minute.",
  pdf_not_available: "This document's PDF is not available to view.",
  document_not_found: "That document was not found.",
  // Build step 6, second half: process again, mark as reviewed, the companion file.
  not_reprocessable: "This document cannot be processed again.",
  attempt_running: "This document is being processed now. Try again when it has finished.",
  not_needs_review: "This document is not waiting for review. Refresh the page.",
  companion_not_allowed: "A companion file cannot be attached to this document.",
  companion_pending: "A companion file for this document is already on its way. Wait for it to finish.",
};

/** Codes after which the user must sign in again. */
export const SIGN_IN_AGAIN = new Set(["role_mismatch", "session_revoked", "invalid_token"]);

export function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return MESSAGES[error.code] ?? `The request failed (${error.code}).`;
  }
  return "Something went wrong. Check your connection and try again.";
}
