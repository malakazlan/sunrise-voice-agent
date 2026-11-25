// @vitest-environment node
import { describe, expect, it } from "vitest";

import { phoneForReading } from "@/lib/format";

describe("a callback number shown for reading", () => {
  it("groups ten digits three, three, four", () => {
    expect(phoneForReading("5552013344")).toBe("555 201 3344");
    expect(phoneForReading("555 201 3344")).toBe("555 201 3344");
    expect(phoneForReading(" 555 2013344 ")).toBe("555 201 3344");
  });

  it("leaves anything else exactly as the API sent it", () => {
    for (const value of [
      "+1 555 201 3344",
      "(555) 201-3344",
      "555-201-3344",
      "555201334",
      "55520133445",
      "ext 12",
    ]) {
      expect(phoneForReading(value), value).toBe(value);
    }
  });
});
