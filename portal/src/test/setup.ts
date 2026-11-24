import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest runs without globals, so Testing Library cannot register its own cleanup: unmount
// every render after each test, or the next test sees two of everything.
afterEach(cleanup);
