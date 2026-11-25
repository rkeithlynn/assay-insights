import { describe, expect, it } from "vitest";

import {
  canReprocess,
  COMPANION_REPLACEABLE,
  companionFileText,
  errorCodeText,
  errorText,
  fileProblemText,
  laterStepsPending,
  reprocessSummary,
  resultText,
  stepText,
} from "./ingestionFormat";

describe("resultText", () => {
  it("shows plain words, never the stored codes", () => {
    expect(resultText("continue")).toBe("Passed");
    expect(resultText("pdf_url")).toBe("Opened");
    expect(resultText("clean")).toBe("Clean");
    expect(resultText("scan_timeout")).toBe("Timed out");
    expect(resultText("on_hold")).toBe("On hold");
  });

  it("turns an unknown code into words", () => {
    expect(resultText("something_new")).toBe("Something new");
  });
});

describe("laterStepsPending (until steps 5 to 10 exist)", () => {
  const now = new Date("2026-11-25T12:00:00Z").getTime();
  const fingerprint = (at: string) => ({ step: "fingerprint", status: "continue", at });
  const old = fingerprint("2026-11-24T15:02:09Z");
  const fresh = fingerprint("2026-11-25T11:58:00Z");
  const inspected = [fresh, { step: "inspect", status: "continue", at: "2026-11-25T11:59:30Z" }];

  it("is shown for a processing document that passed inspection", () => {
    expect(laterStepsPending("processing", inspected, now)).toBe(true);
  });
  it("is shown for one that passed fingerprint before inspection existed", () => {
    expect(laterStepsPending("processing", [old], now)).toBe(true);
    expect(laterStepsPending("processing", [{ step: "fingerprint", status: "continue" }], now)).toBe(true);
  });
  it("is not shown while a document waits for its task or the task runs", () => {
    expect(laterStepsPending("processing", [fresh], now)).toBe(false);
    expect(laterStepsPending("processing", [old, { step: "worker", status: "running" }], now)).toBe(false);
    expect(laterStepsPending("processing", [old, { step: "inspect", status: "running" }], now)).toBe(false);
  });
  it("is not shown otherwise", () => {
    expect(laterStepsPending("processing", [{ step: "start", status: "started" }], now)).toBe(false);
    expect(laterStepsPending("duplicate", [{ step: "fingerprint", status: "duplicate" }], now)).toBe(false);
    expect(laterStepsPending("ready", inspected, now)).toBe(false);
    expect(laterStepsPending("on_hold", [fresh, { step: "inspect", status: "on_hold" }], now)).toBe(false);
  });
});

describe("the processing task's steps", () => {
  it("have plain names and results", () => {
    expect(stepText("inspect")).toBe("Inspection");
    expect(stepText("worker")).toBe("Processing task");
    expect(resultText("running")).toBe("Running");
    expect(resultText("error")).toBe("Failed");
    expect(errorText("inspect_failed")).toBe("The inspection step failed.");
    expect(errorText("worker_failed")).toBe("The processing task step failed.");
    expect(errorText("claim_failed")).toBe("The scheduling step failed.");
    expect(errorText("pdf_unreadable")).toBe("The PDF could not be opened. Check the file and upload it again.");
  });
});

describe("errorText", () => {
  it("names the step that failed, in plain words", () => {
    expect(errorText("fingerprint_failed")).toBe("The fingerprint step failed.");
  });

  it("says why a file is on hold and what to do", () => {
    expect(errorText("pdf_encrypted")).toBe(
      "This PDF is password-protected and can't be processed. Remove the password and upload it again.",
    );
    expect(errorText("scan_unsupported")).toMatch(/password-protected\. Remove the password and upload it again\.$/);
    for (const code of ["scan_failed", "scan_access_denied", "scan_timeout", "companion_not_scanned"]) {
      expect(errorText(code)).toMatch(/ in a new batch\.$/);
    }
    expect(fileProblemText("scan_unsupported", "companion")).toBe(
      "This companion file can't be scanned. Remove it from the batch and add it again.",
    );
    expect(fileProblemText("scan_unsupported", "pdf")).toBe(errorText("scan_unsupported"));
  });

  it("keeps the fixed texts and the generic failure", () => {
    expect(errorText("pipeline_error")).toBe("Processing failed.");
    expect(errorText(null)).toBe("");
  });

  it("does not invent a step it does not know", () => {
    expect(errorText("mystery_failed")).toBe("mystery failed");
  });
});

describe("errorCodeText (master admins)", () => {
  it("shows the code and the error type", () => {
    expect(errorCodeText("fingerprint_failed", "AccessDenied")).toBe("fingerprint_failed · AccessDenied");
    expect(errorCodeText("fingerprint_failed", null)).toBe("fingerprint_failed");
    expect(errorCodeText(null, null)).toBe("");
  });

  it("always shows the generic code of an older row, and says the step was not recorded", () => {
    expect(errorCodeText("pipeline_error", null)).toBe("pipeline_error · the failed step was not recorded");
    expect(errorCodeText("pipeline_error", "States.Timeout")).toBe("pipeline_error · States.Timeout");
  });
});

describe("process again and the companion file (build step 6, second half)", () => {
  it("offers Process again only for a document that may run again", () => {
    expect(["failed", "ready", "needs_review", "processing"].every(canReprocess)).toBe(true);
    expect(["quarantined", "on_hold", "duplicate", "discarded", "uploaded", "scanning"].some(canReprocess)).toBe(false);
  });

  it("counts the Ready documents, whose AI steps cost money to run again", () => {
    expect(reprocessSummary(["ready", "failed", "ready", "processing"])).toEqual({ count: 4, ready: 2 });
    expect(reprocessSummary(["failed"])).toEqual({ count: 1, ready: 0 });
  });

  it("allows a companion file only once the document has been processed", () => {
    expect(COMPANION_REPLACEABLE).toEqual(["ready", "needs_review", "processing"]);
  });

  it("says how each companion upload stands, in plain words", () => {
    const file = (status: string, current = false, replacement = true) => ({ status, current, replacement });
    expect(companionFileText(file("clean", true))).toBe("In use");
    expect(companionFileText(file("registered"))).toBe("Waiting for its scan");
    expect(companionFileText(file("invalid"))).toBe("Rejected: not valid");
    expect(companionFileText(file("infected"))).toBe("Quarantined: malware found");
    expect(companionFileText(file("clean", false, false))).toBe("Replaced");
  });

  it("names the new steps and results in plain words", () => {
    expect(stepText("companion")).toBe("Companion file");
    expect(stepText("review")).toBe("Review");
    expect(resultText("stored")).toBe("Stored");
    expect(resultText("none")).toBe("No companion file");
    expect(resultText("reviewed")).toBe("Marked as reviewed");
    expect(errorText("companion_failed")).toBe("The companion file step failed.");
  });
});
