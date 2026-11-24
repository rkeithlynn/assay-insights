import { describe, expect, it } from "vitest";

import { errorCodeText, errorText } from "./ingestionFormat";

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
