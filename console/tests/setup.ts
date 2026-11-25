import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Without vitest's globals, Testing Library does not unmount between tests on its own.
afterEach(() => {
  cleanup();
});
