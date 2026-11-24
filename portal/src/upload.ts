/**
 * Uploading files into a batch (BUILD_SPEC.md sections 8 and 10, build step 6).
 *
 * 1. Each file is checked here first, so most mistakes show at once: its type (PDF, or a JSON
 *    companion file), its size, and for a companion file that it is a JSON object whose
 *    `TenantID`, if any, is this organization's. The API checks again, and the pipeline
 *    validates every companion file properly once it is scanned.
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

/** Why this file cannot be added, in plain words, or null. */
export async function localProblem(file: File, tenantId: string): Promise<string | null> {
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
  if (file.size > COMPANION_MAX_BYTES) return "A companion file may be at most 64 KB.";
  let parsed: unknown;
  try {
    parsed = JSON.parse(await file.text());
  } catch {
    return "The companion file is not valid JSON.";
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return "The companion file must be one JSON object.";
  }
  const entry = Object.entries(parsed).find(([key]) => key.toLowerCase() === "tenantid");
  if (entry && String(entry[1]).toLowerCase() !== tenantId.toLowerCase()) {
    return "The companion file's TenantID is not this organization's ID.";
  }
  return null;
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

export function chunks<T>(items: T[], size = MAX_FILES_PER_REQUEST): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}
