import { describe, expect, it, vi } from "vitest";

import { companionErrorText, matchText } from "./pages/Upload";
import { anyInProgress, errorText, formatBytes, formatDuration } from "./pages/ingestionFormat";
import { chunks, classify, COMPANION_MAX_BYTES, localProblem, putFile, runLimited, sha256Hex } from "./upload";

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
  it("accepts a PDF and a companion file with this tenant's ID", async () => {
    expect(await localProblem(file("a.pdf", "%PDF-1.7"), TENANT)).toBeNull();
    expect(await localProblem(file("a.json", JSON.stringify({ TenantID: TENANT })), TENANT)).toBeNull();
    expect(await localProblem(file("a.json", JSON.stringify({ tenantid: TENANT.toUpperCase() })), TENANT)).toBeNull();
  });

  it("explains what is wrong", async () => {
    expect(await localProblem(file("a.docx", "x"), TENANT)).toMatch(/Only PDF/);
    expect(await localProblem(file("a.pdf", ""), TENANT)).toMatch(/empty/);
    expect(await localProblem(file("a.pdf", "x", 100 * 1024 * 1024 + 1), TENANT)).toMatch(/100 MB/);
    expect(await localProblem(file("a.json", "{}", COMPANION_MAX_BYTES + 1), TENANT)).toMatch(/64 KB/);
    expect(await localProblem(file("a.json", "{not json"), TENANT)).toMatch(/not valid JSON/);
    expect(await localProblem(file("a.json", "[1]"), TENANT)).toMatch(/one JSON object/);
    expect(await localProblem(file("a.json", JSON.stringify({ TenantID: "other" })), TENANT)).toMatch(/TenantID/);
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

  it("formats sizes, durations and error codes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(null)).toBe("-");
    expect(formatDuration(42)).toBe("0:42");
    expect(formatDuration(125)).toBe("2:05");
    expect(formatDuration(3720)).toBe("1:02:00");
    expect(errorText("pdf_encrypted")).toBe("The PDF is password-protected.");
    expect(errorText("something_new")).toBe("something new");
  });

  it("refreshes while anything is in progress", () => {
    expect(anyInProgress([{ status: "ready" }, { status: "processing" }])).toBe(true);
    expect(anyInProgress([{ status: "ready" }, { status: "duplicate" }])).toBe(false);
  });
});
