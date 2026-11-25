// @vitest-environment node
import { describe, expect, it } from "vitest";

import { filesUsing } from "./source-files";

// Nothing the API returns is kept in the browser or written to the console log: the query cache
// lives in memory only.
const STORAGE =
  /\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|\bdocument\s*\.\s*cookie\b|\bcookieStore\b|\bcaches\s*\./;
const CONSOLE_LOG = /\bconsole\s*\.\s*\w+/;

describe("browser storage and the console log", () => {
  it("the patterns see what they are looking for", () => {
    const sources = (code: string) => new Map([["sample.ts", code]]);
    for (const code of [
      "localStorage.setItem('k', v)",
      "window.sessionStorage.getItem('k')",
      "indexedDB.open('db')",
      "document.cookie = 'a=b'",
      "await cookieStore.set('a', 'b')",
      "await caches.open('c')",
    ]) {
      expect(filesUsing(STORAGE, sources(code)), code).toEqual(["sample.ts"]);
    }
    for (const code of ["console.log(x)", "console.error(error)", "console . table(rows)"]) {
      expect(filesUsing(CONSOLE_LOG, sources(code)), code).toEqual(["sample.ts"]);
    }
  });

  it("no app code uses browser storage", () => {
    expect(filesUsing(STORAGE)).toEqual([]);
  });

  it("no app code writes to the console", () => {
    expect(filesUsing(CONSOLE_LOG)).toEqual([]);
  });
});
