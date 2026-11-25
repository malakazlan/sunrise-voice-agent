// @vitest-environment node
import { describe, expect, it } from "vitest";

import { builtFiles } from "./build-output";

// The fixture layer (mock/) serves `npm run dev:mock` and the tests over HTTP; nothing of it may
// reach the production build: not its data, not its module, not its server.
const MARKERS = [
  "1d2a4c3e8f00a1b2c3d4e5f6", // the tail of every fixture call id
  "csrf-fixture",
  "mock-password",
  "Metformin 500 mg at Walgreens Oak Ave",
  "5550107788", // a made-up callback number the mock reveals
  "mock/fixtures",
  "mock/server",
  "MOCK_SCENARIO",
];

describe("the fixture layer", () => {
  it("is not in the production build", () => {
    const files = builtFiles();
    expect(files.size).toBeGreaterThan(0);
    const found = [...files].flatMap(([name, content]) =>
      MARKERS.filter((marker) => content.includes(marker)).map((marker) => `${name}: ${marker}`),
    );
    expect(found).toEqual([]);
  });
});
