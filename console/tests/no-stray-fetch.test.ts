// @vitest-environment node
import { describe, expect, it } from "vitest";

import { filesUsing, namesUsed, shippedSources } from "./source-files";

// Only the fetch wrapper talks to the network, and only the stream client opens a stream. Each
// allowed file is named; any other file that so much as names `fetch` or `EventSource` fails.
const ALLOWED: Record<string, string[]> = {
  fetch: ["lib/api/client.ts"],
  EventSource: ["lib/api/stream.ts"],
  XMLHttpRequest: [],
  WebSocket: [],
  sendBeacon: [],
  importScripts: [],
};

describe("network access", () => {
  it("the scan sees a use however it is written", () => {
    for (const code of [
      "await fetch(url)",
      "const get = fetch;\nget(url)",
      "const { fetch: get } = globalThis",
      'window["fetch"](url)',
      "export default async () => (await globalThis.fetch(url)).json()",
    ]) {
      expect(namesUsed(code).has("fetch"), code).toBe(true);
    }
    expect(namesUsed("const Source = EventSource;\nnew Source(url)").has("EventSource")).toBe(true);
    expect(namesUsed("// fetch(url) in a comment\nconst x = 1").has("fetch")).toBe(false);
    expect(namesUsed("export const P = () => <p>fetch the console</p>").has("fetch")).toBe(false);
  });

  it("the scan reads every kind of code file in every folder that ships", () => {
    const files = [...shippedSources().keys()];
    expect(files).toEqual(expect.arrayContaining(["proxy.ts", "next.config.ts", "eslint.config.mjs"]));
    expect(files.some((file) => /^(?:node_modules|\.next|mock|tests|scripts)\//.test(file))).toBe(false);
    expect(files.some((file) => /\.test\./.test(file))).toBe(false);
  });

  for (const [name, allowed] of Object.entries(ALLOWED)) {
    it(`${name} is named only in ${allowed.length === 0 ? "no file" : allowed.join(", ")}`, () => {
      expect(filesUsing(name)).toEqual(allowed);
    });
  }
});
