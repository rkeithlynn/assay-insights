import { describe, expect, it, vi } from "vitest";

import { matchText, pdfFor, rejectedCompanions } from "./pages/Upload";
import { anyInProgress, companionErrorLines, companionErrorText, errorText, formatBytes, formatDuration } from "./pages/ingestionFormat";
import {
  chunks,
  classify,
  COMPANION_MAX_BYTES,
  companionWarnings,
  localProblem,
  putFile,
  runLimited,
  sha256Hex,
} from "./upload";

const TENANT = "7f3a91c2-0000-4000-8000-000000000001";

function file(name: string, body: string, size?: number): File {
  const f = new File([body], name);
  if (size !== undefined) Object.defineProperty(f, "size", { value: size });
  return f;
}

describe("classify", () => {
  it("knows PDFs and companion files by extension, and pairs them by base name", () => {
    expect(classify("Smith-Package.PDF")).toEqual({ kind: "pdf", base: "smith-package" });
    expect(classify("smith-package.json")).toEqual({ kind: "companion", base: "smith-package" });
    expect(classify("notes.txt")).toBeNull();
    expect(classify(".pdf")).toBeNull();
    expect(classify("no-extension")).toBeNull();
  });
});

describe("localProblem", () => {
  it("accepts a PDF and any companion file of the right size", () => {
    expect(localProblem(file("a.pdf", "%PDF-1.7"))).toBeNull();
    expect(localProblem(file("a.json", JSON.stringify({ TenantID: TENANT })))).toBeNull();
    // A companion file is never left out for its content (owner decision A1).
    expect(localProblem(file("a.json", "{not json"))).toBeNull();
    expect(localProblem(file("a.json", JSON.stringify({ TenantID: "other" })))).toBeNull();
  });

  it("explains what keeps a file out", () => {
    expect(localProblem(file("a.docx", "x"))).toMatch(/Only PDF/);
    expect(localProblem(file("a.pdf", ""))).toMatch(/empty/);
    expect(localProblem(file("a.pdf", "x", 100 * 1024 * 1024 + 1))).toMatch(/100 MB/);
    expect(localProblem(file("a.json", "{}", COMPANION_MAX_BYTES + 1))).toMatch(/64 KB/);
  });
});

describe("companionWarnings", () => {
  it("is empty for a file with this tenant's ID", async () => {
    expect(await companionWarnings(file("a.json", JSON.stringify({ TenantID: TENANT })), TENANT)).toEqual([]);
    expect(await companionWarnings(file("a.json", JSON.stringify({ tenantid: TENANT.toUpperCase() })), TENANT)).toEqual([]);
  });

  it("lists every problem it can see at once", async () => {
    const body = JSON.stringify({ TenantID: "other", Note: { a: 1 }, List: [1] });
    expect(await companionWarnings(file("a.json", body), TENANT)).toEqual([
      "TenantID is not this organization's ID",
      "Note must be text, a number or true/false",
      "List must be text, a number or true/false",
    ]);
    expect(await companionWarnings(file("a.json", "{not json"), TENANT)).toEqual(["The file is not valid JSON"]);
    expect(await companionWarnings(file("a.json", "[1]"), TENANT)).toEqual(["The file must be one JSON object"]);
  });
});

describe("sha256Hex", () => {
  it("is the file's hex SHA-256", async () => {
    expect(await sha256Hex(new Blob(["hello"]))).toBe("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
  });
});

describe("putFile", () => {
  it("sends exactly the signed headers and reports progress", async () => {
    const sent: Record<string, string> = {};
    const fake = {
      upload: {} as { onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void },
      status: 200,
      open: vi.fn(),
      setRequestHeader: (name: string, value: string) => {
        sent[name] = value;
      },
      send() {
        fake.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 });
        fake.onload?.();
      },
      onload: undefined as undefined | (() => void),
      onerror: undefined as undefined | (() => void),
    };
    const progress: number[] = [];
    await putFile(
      { url: "https://b.s3.us-east-1.amazonaws.com/inbox/x/y?sig", method: "PUT", headers: { "Content-Type": "application/pdf", "x-amz-checksum-sha256": "abc=" } },
      new Blob(["0123456789"]),
      (p) => progress.push(p),
      () => fake as unknown as XMLHttpRequest,
    );
    expect(fake.open).toHaveBeenCalledWith("PUT", "https://b.s3.us-east-1.amazonaws.com/inbox/x/y?sig");
    expect(sent).toEqual({ "Content-Type": "application/pdf", "x-amz-checksum-sha256": "abc=" });
    expect(progress).toEqual([0.5, 1]);
  });

  it("fails when S3 refuses the upload", async () => {
    const fake = {
      upload: {},
      status: 403,
      open: () => undefined,
      setRequestHeader: () => undefined,
      send() {
        fake.onload?.();
      },
      onload: undefined as undefined | (() => void),
    };
    await expect(
      putFile({ url: "u", method: "PUT", headers: {} }, new Blob(["x"]), () => undefined, () => fake as unknown as XMLHttpRequest),
    ).rejects.toThrow(/403/);
  });
});

describe("runLimited and chunks", () => {
  it("never runs more than the limit at once, and runs every task", async () => {
    let running = 0;
    let peak = 0;
    const tasks = Array.from({ length: 7 }, (_, i) => async () => {
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((r) => setTimeout(r, 1));
      running -= 1;
      if (i === 3) throw new Error("one fails");
      return i;
    });
    const results = await runLimited(tasks, 3);
    expect(peak).toBeLessThanOrEqual(3);
    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(6);
  });

  it("registers at most 50 files at a time", () => {
    expect(chunks(Array.from({ length: 120 }, (_, i) => i)).map((c) => c.length)).toEqual([50, 50, 20]);
  });
});

describe("labels", () => {
  it("names a companion file's problems by key, never by value", () => {
    expect(companionErrorText([{ code: "tenant_mismatch", key: "TenantID" }])).toBe("TenantID is not this organization's ID");
    expect(companionErrorText([{ code: "not_json", key: null }])).toBe("The file is not valid JSON");
  });

  it("says whether a file has its pair", () => {
    const base = { id: "1", name: "a.pdf", size_bytes: 1, status: "clean", error_code: null, errors: [], document_id: null, registered_at: "", scanned_at: null };
    expect(matchText({ ...base, kind: "pdf", matched: true })).toBe("Companion file matched");
    expect(matchText({ ...base, kind: "pdf", matched: false })).toBe("No companion file");
    expect(matchText({ ...base, kind: "companion", matched: false })).toBe("No PDF with this name");
  });

  it("flags a PDF whose companion file was rejected (owner decision A1)", () => {
    const base = { size_bytes: 1, error_code: null, errors: [], document_id: null, registered_at: "", scanned_at: null, matched: true };
    const pdf = { ...base, id: "1", name: "Smith.pdf", kind: "pdf" as const, status: "clean" };
    const bad = { ...base, id: "2", name: "smith.json", kind: "companion" as const, status: "invalid" };
    const other = { ...base, id: "3", name: "jones.pdf", kind: "pdf" as const, status: "clean" };
    const files = [pdf, bad, other];
    expect(matchText(pdf, files)).toBe("Companion rejected");
    expect(matchText(bad, files)).toBe("Rejected");
    expect(matchText(other, files)).toBe("Companion file matched");
    expect(rejectedCompanions(files)).toEqual([bad]);
    expect(pdfFor(bad, files)).toBe(pdf);
  });

  it("lists every companion problem on its own line (owner decision A2)", () => {
    expect(
      companionErrorLines([
        { code: "tenant_mismatch", key: "TenantID" },
        { code: "must_be_a_date", key: "CreateDate" },
      ]),
    ).toEqual(["TenantID is not this organization's ID", "CreateDate must be a date (YYYY-MM-DD)"]);
  });

  it("formats sizes, durations and error codes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(null)).toBe("-");
    expect(formatDuration(42)).toBe("0:42");
    expect(formatDuration(125)).toBe("2:05");
    expect(formatDuration(3720)).toBe("1:02:00");
    expect(errorText("pdf_encrypted")).toBe(
      "This PDF is password-protected and can't be processed. Remove the password and upload it again.",
    );
    expect(errorText("something_new")).toBe("something new");
  });

  it("refreshes while anything is in progress", () => {
    expect(anyInProgress([{ status: "ready" }, { status: "processing" }])).toBe(true);
    expect(anyInProgress([{ status: "ready" }, { status: "duplicate" }])).toBe(false);
  });
});
