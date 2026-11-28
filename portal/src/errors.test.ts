import { describe, expect, it } from "vitest";

import { ApiError } from "./api/client";
import { messageFor } from "./errors";

describe("messageFor", () => {
  it("says a refused Process again may be about a few minutes ago, not only about now", () => {
    // The API refuses while an attempt runs, and for a few minutes after its last step: until
    // the pipeline sets a final status, a finished document still reads as processing. So the
    // message must not claim that something is running, nor tell the user to wait for an end
    // they cannot see (owner, 2026-11-27).
    const message = messageFor(new ApiError(409, "attempt_running"));
    expect(message).toBe("This document is being processed, or was a few minutes ago. Try again shortly.");
    expect(message).not.toContain("when it has finished");
  });

  it("shows plain words for a code it knows and the code for one it does not", () => {
    expect(messageFor(new ApiError(409, "not_reprocessable"))).toBe("This document cannot be processed again.");
    expect(messageFor(new ApiError(500, "something_new"))).toBe("The request failed (something_new).");
    expect(messageFor(new Error("offline"))).toBe("Something went wrong. Check your connection and try again.");
  });
});
