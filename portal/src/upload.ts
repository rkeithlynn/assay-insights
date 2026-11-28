/**
 * Uploading files into a batch (BUILD_SPEC.md sections 8 and 10, build step 6).
 *
 * 1. Each file is checked here first: its type (PDF, or a JSON companion file), name and size
 *    (a file that fails is not added). A companion file's content is only looked at, never a
 *    reason to leave it out: it is uploaded, the pipeline validates it fully once it is
 *    scanned, and an invalid one is shown as rejected until it is dealt with.
 * 2. Its SHA-256 is computed in the browser and registered with its name and size. The
 *    upload link the API returns is signed for exactly that size, type and hash, so S3
 *    accepts only this file's content under it.
 * 3. The file is sent straight to the tenant's bucket with that link (a PUT with the headers
 *    the API returned), with progress. The file's name never leaves this page except to the
 *    API; the bucket key is an opaque ID.
 */

export const PDF_MAX_BYTES = 100 * 1024 * 1024;
export const COMPANION_MAX_BYTES = 64 * 1024;
export const MAX_FILES_PER_REQUEST = 50;
// Uploads in flight at once.
export const PARALLEL_UPLOADS = 3;

export type Kind = "pdf" | "companion";

export interface Classified {
  kind: Kind;
  /** The name without its extension, lower-cased: how a companion file finds its PDF. */
  base: string;
}

const EXTENSIONS: Record<string, Kind> = { pdf: "pdf", json: "companion" };

export function classify(name: string): Classified | null {
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return null;
  const kind = EXTENSIONS[name.slice(dot + 1).toLowerCase()];
  const base = name.slice(0, dot).trim().toLowerCase();
  return kind && base ? { kind, base } : null;
}

/**
 * Why this file cannot be added at all, in plain words, or null: its type, name or size, which
 * the API refuses at registration too. A companion file's content is not checked here; see
 * `companionWarnings`.
 */
export function localProblem(file: File): string | null {
  const classified = classify(file.name);
  if (!classified) return "Only PDF files and their JSON companion files can be uploaded.";
  // Slashes, backslashes and control characters (U+0000 to U+001F, U+007F).
  // eslint-disable-next-line no-control-regex
  if (/[/\\\u0000-\u001f\u007f]/.test(file.name)) {
    return "The file name may not contain slashes or control characters.";
  }
  if (file.size === 0) return "The file is empty.";
  if (classified.kind === "pdf") {
    return file.size > PDF_MAX_BYTES ? "A PDF may be at most 100 MB." : null;
  }
  return file.size > COMPANION_MAX_BYTES ? "A companion file may be at most 64 KB." : null;
}

/**
 * What this page can already see is wrong with a companion file's content, every problem at
 * once (owner decision A1/A2, 2026-11-24). The file is still added: it is uploaded and the
 * pipeline checks it fully after its scan, and that complete list replaces these. An invalid
 * companion file keeps the batch from closing until it is fixed, removed, or its PDF goes on
 * without it.
 */
export async function companionWarnings(file: File, tenantId: string): Promise<string[]> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(await file.text());
  } catch {
    return ["The file is not valid JSON"];
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return ["The file must be one JSON object"];
  }
  const warnings: string[] = [];
  for (const [key, value] of Object.entries(parsed)) {
    if (key.toLowerCase() === "tenantid" && String(value).toLowerCase() !== tenantId.toLowerCase()) {
      warnings.push(`${key} is not this organization's ID`);
    } else if (value !== null && typeof value === "object") {
      warnings.push(`${key} must be text, a number or true/false`);
    }
  }
  return warnings;
}

export async function sha256Hex(file: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export interface UploadLink {
  url: string;
  method: "PUT";
  headers: Record<string, string>;
}

/**
 * Send the file with its presigned link. `onProgress` gets 0 to 1. Uses XMLHttpRequest, the
 * only browser API that reports upload progress. Resolves when S3 has stored it.
 */
export function putFile(
  link: UploadLink,
  file: Blob,
  onProgress: (fraction: number) => void,
  request: () => XMLHttpRequest = () => new XMLHttpRequest(),
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = request();
    xhr.open(link.method, link.url);
    for (const [name, value] of Object.entries(link.headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve();
      } else {
        reject(new Error(`upload refused (${xhr.status})`));
      }
    };
    xhr.onerror = () => reject(new Error("upload failed"));
    xhr.send(file);
  });
}

/** Run `tasks` with at most `limit` in flight; every task runs, failures included. */
export async function runLimited<T>(tasks: (() => Promise<T>)[], limit = PARALLEL_UPLOADS): Promise<PromiseSettledResult<T>[]> {
  const results: PromiseSettledResult<T>[] = [];
  let next = 0;
  const worker = async () => {
    while (next < tasks.length) {
      const index = next++;
      const task = tasks[index];
      if (!task) continue;
      try {
        results[index] = { status: "fulfilled", value: await task() };
      } catch (reason) {
        results[index] = { status: "rejected", reason };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
  return results;
}

/**
 * What the screen says about an addition: how many files the page was handed and how many of
 * them are now in the batch (owner, 2026-11-27, after two selections arrived short with
 * nothing on the screen to show it). `added` is null while the addition is still running.
 */
export function additionText(chosen: number, added: number | null): string {
  if (chosen === 0) return "No file was chosen.";
  const files = chosen === 1 ? "1 file chosen" : `${chosen} files chosen`;
  if (added === null) return `${files}: adding ${chosen === 1 ? "it" : "them"}.`;
  return added === chosen ? `${files}: ${added} added.` : `${files}: ${added} added, ${chosen - added} not added.`;
}

/** How the API will name a file: it stores the name without surrounding spaces. */
function registeredName(name: string): string {
  return name.trim();
}

export interface Matched<F, E> {
  /** Each registered entry with the file of its name; no file if the answer named none of ours. */
  pairs: { entry: E; file: F | undefined }[];
  /** The files the answer did not mention. */
  unregistered: F[];
}

/**
 * Pair the API's registered entries with the files they were registered for, by name, never
 * by position: a link is signed for one file's size and hash, so each file must go to the
 * link registered under its own name whatever order the answer comes in. Two files of one
 * name are paired in the order they were sent.
 */
export function matchRegistered<F extends { name: string }, E extends { name: string }>(files: F[], entries: E[]): Matched<F, E> {
  const waiting = new Map<string, F[]>();
  for (const file of files) {
    const key = registeredName(file.name);
    waiting.set(key, [...(waiting.get(key) ?? []), file]);
  }
  const pairs = entries.map((entry) => ({ entry, file: waiting.get(registeredName(entry.name))?.shift() }));
  return { pairs, unregistered: [...waiting.values()].flat() };
}

export function chunks<T>(items: T[], size = MAX_FILES_PER_REQUEST): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}
