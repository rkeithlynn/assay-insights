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
  // On hold: why, and what to do (owner, 2026-11-24). The malware scan reports only that it
  // could not scan the file (UNSUPPORTED); for a PDF here that is almost always a password,
  // but the scan does not say so, hence "most likely".
  scan_unsupported:
    "This PDF can't be scanned, most likely because it is password-protected. Remove the password and upload it again.",
  scan_failed: "The malware scan failed. Upload the file again in a new batch.",
  scan_access_denied: "The malware scan could not read the stored file. Upload it again in a new batch.",
  scan_timeout: "The malware scan did not finish within 30 minutes. Upload the file again in a new batch.",
  size_mismatch: "The stored file's size differs from what was registered.",
  checksum_mismatch: "The stored file differs from what was registered.",
  version_missing: "The scanned version of the file was not found.",
  companion_invalid: "Its companion file is not valid.",
  companion_mismatch: "Its companion file differs from what was registered.",
  companion_infected: "Malware was found in its companion file.",
  companion_not_scanned:
    "Its companion file couldn't be scanned, so the PDF is on hold. Upload the PDF and a new companion file in a new batch.",
  companion_unmatched: "No PDF in the batch has this name.",
  not_a_pdf: "The file is not a PDF.",
  pdf_unreadable: "The PDF could not be opened. Check the file and upload it again.",
  pdf_encrypted: "This PDF is password-protected and can't be processed. Remove the password and upload it again.",
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

/** A file's problem on the Upload screen. A companion file that can't be scanned is not a
 * PDF, so it gets its own words. */
export function fileProblemText(code: string | null | undefined, kind: string): string {
  if (kind === "companion" && code === "scan_unsupported") {
    return "This companion file can't be scanned. Remove it from the batch and add it again.";
  }
  return errorText(code);
}

// Rows written before step-level codes (build step 6, 2026-11-24) carry only this one.
const GENERIC_FAILURE = "pipeline_error";

/** For master admins: always the stored code, and a failed step's error type (an error's
 * name). A generic failure with no error type says the step was not recorded. */
export function errorCodeText(code: string | null | undefined, errorType?: string | null): string {
  if (code === GENERIC_FAILURE && !errorType) return `${code} · the failed step was not recorded`;
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
  // The processing task's progress: under way, or stopped by an error.
  running: "Running",
  error: "Failed",
  // Fingerprint, inspection
  continue: "Passed",
  duplicate: "Duplicate",
  on_hold: "On hold",
  // Companion file, failure
  quarantined: "Quarantined",
  failed: "Failed",
  removed: "Removed",
  // PDF opened
  pdf_url: "Opened",
  // The companion file's values (step 9), and a review.
  stored: "Stored",
  none: "No companion file",
  reviewed: "Marked as reviewed",
};

export function resultText(status: string): string {
  const known = RESULT_TEXT[status];
  if (known) return known;
  const words = status.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// A document that passed fingerprint before the inspection step existed has no task to
// wait for. One uploaded since is waiting for its task, for a few minutes at most.
const NO_TASK_COMING_MS = 10 * 60 * 1000;

/**
 * Until steps 5 to 10 exist: a document that passed every step there is stays Processing.
 * That is one that passed inspection, or (uploaded before inspection existed) one that
 * passed fingerprint a while ago with nothing since.
 * TODO(build step 6, second half, stage 4): remove this and its note.
 */
export function laterStepsPending(
  status: string,
  events: { step: string; status: string; at?: string }[],
  now: number = Date.now(),
): boolean {
  if (status !== "processing") return false;
  if (events.some((e) => e.step === "inspect" && e.status === "continue")) return true;
  const fingerprint = events.find((e) => e.step === "fingerprint" && e.status === "continue");
  if (!fingerprint || events.some((e) => e.step === "worker" || e.step === "inspect")) return false;
  return fingerprint.at === undefined || now - new Date(fingerprint.at).getTime() > NO_TASK_COMING_MS;
}

/** A pipeline step's name, for the step list. */
export const STEP_TEXT: Record<string, string> = {
  scan: "Malware scan",
  queued: "Queued",
  start: "Started",
  fingerprint: "Fingerprint",
  // The processing task (build step 6, second half): waiting for it, the task itself, and
  // its first step, which opens the PDF.
  claim: "Scheduling",
  worker: "Processing task",
  inspect: "Inspection",
  companion: "Companion file",
  review: "Review",
  failed: "Failed",
  viewed: "PDF opened",
};

/** "Process again" (tenant admins): the statuses the API accepts. A `processing` document is
 * accepted only once its attempt has ended; the API says so if it has not. */
export const REPROCESSABLE: readonly string[] = ["failed", "ready", "needs_review", "processing"];
/** A companion file may be attached or replaced once the document has been processed. */
export const COMPANION_REPLACEABLE: readonly string[] = ["ready", "needs_review", "processing"];

export function canReprocess(status: string): boolean {
  return REPROCESSABLE.includes(status);
}

/** What the confirmation says before documents are processed again. Processing a Ready
 * document again runs the AI steps again, which costs money: the dialog says how many. */
export function reprocessSummary(statuses: string[]): { count: number; ready: number } {
  return { count: statuses.length, ready: statuses.filter((s) => s === "ready").length };
}

/** How a companion upload of a document stands, in plain words. */
export function companionFileText(file: { status: string; current: boolean; replacement: boolean }): string {
  if (file.current) return "In use";
  switch (file.status) {
    case "registered":
      return "Waiting for its scan";
    case "clean":
      return file.replacement ? "Checked" : "Replaced";
    case "invalid":
      return "Rejected: not valid";
    case "infected":
      return "Quarantined: malware found";
    case "mismatch":
      return "Rejected: the file changed during upload";
    case "not_uploaded":
      return "Not uploaded";
    default:
      return "Could not be scanned";
  }
}

export function stepText(step: string): string {
  return STEP_TEXT[step] ?? step;
}
