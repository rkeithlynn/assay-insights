/** Labels and formats shared by the Upload screen and both ingestion histories. */

import type { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";

import type { DocumentStatus } from "../api/client";

type Indicator = StatusIndicatorProps.Type;

export const DOCUMENT_STATUS: Record<DocumentStatus, { type: Indicator; label: string }> = {
  uploaded: { type: "pending", label: "Uploaded" },
  scanning: { type: "in-progress", label: "Scanning" },
  processing: { type: "in-progress", label: "Processing" },
  ready: { type: "success", label: "Ready" },
  needs_review: { type: "warning", label: "Needs review" },
  quarantined: { type: "error", label: "Quarantined" },
  on_hold: { type: "warning", label: "On hold" },
  failed: { type: "error", label: "Failed" },
  duplicate: { type: "info", label: "Duplicate" },
  discarded: { type: "stopped", label: "Discarded" },
};

/** Statuses that still change by themselves: the history refreshes while any row has one. */
export const IN_PROGRESS: ReadonlySet<DocumentStatus> = new Set(["uploaded", "scanning", "processing"]);

export function anyInProgress(rows: { status: DocumentStatus }[]): boolean {
  return rows.some((row) => IN_PROGRESS.has(row.status));
}

export const STATUS_OPTIONS = (Object.keys(DOCUMENT_STATUS) as DocumentStatus[]).map((status) => ({
  value: status,
  label: DOCUMENT_STATUS[status].label,
}));

/**
 * A compact time for a table cell: no year when it is this year's (the row's panel shows the
 * full time). `seconds` for Audit history, where they matter.
 */
export function formatShortTime(value: string, { seconds = false, now = new Date() } = {}): string {
  const date = new Date(value);
  return date.toLocaleString(undefined, {
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
  });
}

/** The first 8 characters of an ID: enough to tell rows apart; the panel shows it all. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Minutes and seconds (`2:05`), or hours too (`1:02:00`). */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  const s = String(seconds % 60).padStart(2, "0");
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}:${s}`;
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${s}`;
}

/** Plain words for the error codes a document or a pipeline step carries. */
export const ERROR_TEXT: Record<string, string> = {
  threats_found: "Malware was found; the file is quarantined.",
  scan_unsupported: "The malware scan could not read this file.",
  scan_failed: "The malware scan failed.",
  scan_access_denied: "The malware scan could not reach this file.",
  scan_timeout: "The malware scan did not finish.",
  size_mismatch: "The stored file's size differs from what was registered.",
  checksum_mismatch: "The stored file differs from what was registered.",
  version_missing: "The scanned version of the file was not found.",
  companion_invalid: "Its companion file is not valid.",
  companion_mismatch: "Its companion file differs from what was registered.",
  companion_infected: "Malware was found in its companion file.",
  companion_not_scanned: "Its companion file could not be scanned.",
  companion_unmatched: "No PDF in the batch has this name.",
  not_a_pdf: "The file is not a PDF.",
  pdf_encrypted: "The PDF is password-protected.",
  not_uploaded: "The file was never uploaded.",
  removed: "Removed from the batch.",
  timeout: "Processing took too long.",
  throttled: "The service was busy; processing stopped.",
  pipeline_error: "Processing failed.",
};

const STEP_FAILED = /^([a-z0-9]+)_failed$/;

/** Plain words for a code. A pipeline step's failure (`fingerprint_failed`) names the step. */
export function errorText(code: string | null | undefined): string {
  if (!code) return "";
  const known = ERROR_TEXT[code];
  if (known) return known;
  const step = STEP_FAILED.exec(code)?.[1];
  const name = step === undefined ? undefined : STEP_TEXT[step];
  if (name) return `The ${name.toLowerCase()} step failed.`;
  return code.replaceAll("_", " ");
}

/** For master admins: the code itself, and a failed step's error type (an error's name). */
export function errorCodeText(code: string | null | undefined, errorType?: string | null): string {
  return [code, errorType].filter(Boolean).join(" · ");
}

/** Plain words for a pipeline step's result (`document_events.status`). */
export const RESULT_TEXT: Record<string, string> = {
  // Malware scan
  clean: "Clean",
  infected: "Malware found",
  unsupported: "Could not be scanned",
  mismatch: "File differs",
  invalid: "Not valid",
  scan_timeout: "Timed out",
  // Queue and execution
  queued: "Queued",
  started: "Started",
  // Fingerprint
  continue: "Passed",
  duplicate: "Duplicate",
  on_hold: "On hold",
  // Companion file, failure
  quarantined: "Quarantined",
  failed: "Failed",
  removed: "Removed",
  // PDF opened
  pdf_url: "Opened",
};

export function resultText(status: string): string {
  const known = RESULT_TEXT[status];
  if (known) return known;
  const words = status.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Build step 6, first half only: no step after fingerprint exists yet, so a document that
 * passed it stays Processing. TODO(build step 6, second half): remove this and its note.
 */
export function laterStepsPending(status: string, events: { step: string; status: string }[]): boolean {
  return status === "processing" && events.some((e) => e.step === "fingerprint" && e.status === "continue");
}

/** A pipeline step's name, for the step list. */
export const STEP_TEXT: Record<string, string> = {
  scan: "Malware scan",
  queued: "Queued",
  start: "Started",
  fingerprint: "Fingerprint",
  companion: "Companion file",
  failed: "Failed",
  viewed: "PDF opened",
};

export function stepText(step: string): string {
  return STEP_TEXT[step] ?? step;
}
