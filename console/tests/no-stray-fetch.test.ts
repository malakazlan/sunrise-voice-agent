// @vitest-environment node
import { describe, expect, it } from "vitest";

import { filesUsing } from "./source-files";

// Only the fetch wrapper talks to the network, and only the stream client opens a stream.
const FETCH = /\bfetch\s*\(|\b(?:globalThis|window|self)\s*\.\s*fetch\b/;
const EVENT_SOURCE = /\bEventSource\b/;
const OTHER_NETWORK = /\bXMLHttpRequest\b|\bWebSocket\b|\bsendBeacon\b|\bimportScripts\b/;

describe("network access", () => {
  it("the patterns see what they are looking for", () => {
    const sources = (code: string) => new Map([["sample.ts", code]]);
    for (const code of ["await fetch(url)", "await globalThis.fetch(url)", "const call = window.fetch"]) {
      expect(filesUsing(FETCH, sources(code)), code).toEqual(["sample.ts"]);
    }
    expect(filesUsing(FETCH, sources("// fetch(url) in a comment\nconst x = 1"))).toEqual([]);
    expect(filesUsing(EVENT_SOURCE, sources("new EventSource(url)"))).toEqual(["sample.ts"]);
    expect(filesUsing(OTHER_NETWORK, sources("navigator.sendBeacon(url)"))).toEqual(["sample.ts"]);
  });

  it("fetch is called only by the fetch wrapper", () => {
    expect(filesUsing(FETCH)).toEqual(["lib/api/client.ts"]);
  });

  it("EventSource is used only by the stream client", () => {
    expect(filesUsing(EVENT_SOURCE)).toEqual(["lib/api/stream.ts"]);
  });

  it("no other way to reach the network is used", () => {
    expect(filesUsing(OTHER_NETWORK)).toEqual([]);
  });
});
