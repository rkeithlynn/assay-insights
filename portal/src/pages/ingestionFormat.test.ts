import { describe, expect, it } from "vitest";

import { errorCodeText, errorText, laterStepsPending, resultText } from "./ingestionFormat";

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

describe("laterStepsPending (first half of build step 6 only)", () => {
  const passed = [{ step: "fingerprint", status: "continue" }];
  it("is shown for a processing document that passed fingerprint", () => {
    expect(laterStepsPending("processing", passed)).toBe(true);
  });
  it("is not shown otherwise", () => {
    expect(laterStepsPending("processing", [{ step: "start", status: "started" }])).toBe(false);
    expect(laterStepsPending("duplicate", [{ step: "fingerprint", status: "duplicate" }])).toBe(false);
    expect(laterStepsPending("ready", passed)).toBe(false);
  });
});

describe("errorText", () => {
  it("names the step that failed, in plain words", () => {
    expect(errorText("fingerprint_failed")).toBe("The fingerprint step failed.");
  });

  it("keeps the fixed texts and the generic failure", () => {
    expect(errorText("pdf_encrypted")).toBe("The PDF is password-protected.");
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
});
