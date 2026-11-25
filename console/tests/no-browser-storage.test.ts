// @vitest-environment node
import { describe, expect, it } from "vitest";

import { filesUsing, namesUsed } from "./source-files";

// Nothing the API returns is kept in the browser or written to the console log (the query cache
// lives in memory only), and no code writes raw HTML into the page. No file is allowed any of
// these names, under any alias.
const FORBIDDEN = [
  "console",
  "localStorage",
  "sessionStorage",
  "indexedDB",
  "document.cookie",
  "cookieStore",
  "caches",
  "dangerouslySetInnerHTML",
];

describe("browser storage, the console log and raw HTML", () => {
  it("the scan sees a use however it is written", () => {
    const uses: [string, string][] = [
      ["console", "console.log(x)"],
      ["console", "const log = console;\nlog.info(x)"],
      ["console", "const { debug } = console"],
      ["console", 'globalThis["console"].error(x)'],
      ["localStorage", "const store = window.localStorage;\nstore.setItem('k', v)"],
      ["sessionStorage", "sessionStorage.getItem('k')"],
      ["indexedDB", "indexedDB.open('db')"],
      ["document.cookie", "document.cookie = 'a=b'"],
      ["document.cookie", 'const jar = document;\njar["cookie"] = "a=b"'],
      ["cookieStore", "await cookieStore.set('a', 'b')"],
      ["caches", "await caches.open('c')"],
      ["dangerouslySetInnerHTML", "export const P = () => <p dangerouslySetInnerHTML={{ __html: h }} />"],
      ["dangerouslySetInnerHTML", "createElement('p', { dangerouslySetInnerHTML: { __html: h } })"],
    ];
    for (const [name, code] of uses) {
      expect(namesUsed(code).has(name), code).toBe(true);
    }
    expect(namesUsed("// console.log(x)\nexport const P = () => <p>the console</p>").has("console")).toBe(
      false,
    );
  });

  for (const name of FORBIDDEN) {
    it(`no file uses ${name}`, () => {
      expect(filesUsing(name)).toEqual([]);
    });
  }
});
