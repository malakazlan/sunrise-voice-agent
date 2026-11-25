// @vitest-environment node
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { namesUsed, ROOT } from "./source-files";

describe("next.config.ts", () => {
  // With cacheComponents on, a page the user leaves stays alive, hidden, so callback details shown
  // on it would outlive leaving it.
  it("does not turn on cacheComponents", () => {
    const source = readFileSync(join(ROOT, "next.config.ts"), "utf-8");

    expect(namesUsed(source, "next.config.ts").has("cacheComponents")).toBe(false);
  });
});
