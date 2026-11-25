// @vitest-environment node
import { describe, expect, it } from "vitest";

import { builtFiles } from "./build-output";

// What the browser is given (.next/static) loads nothing from another origin: the fonts are
// bundled, no stylesheet imports or points at a remote URL, and the only origins named in the
// scripts are these, which appear in error messages, documentation links and XML namespaces
// and are never requested. A new one fails this test until someone has looked at it.
const NAMED_NOT_LOADED = new Set([
  "http://www.w3.org",
  "https://nextjs.org",
  "https://react.dev",
  "https://github.com",
  "https://bit.ly",
  "http://json-schema.org",
  "https://json-schema.org",
  "https://redux.js.org",
  "https://redux-toolkit.js.org",
  // Placeholder bases for parsing relative URLs in framework code.
  "http://n",
  "https://a",
  "https://x",
]);
const ORIGIN = /https?:\/\/[A-Za-z0-9.-]+(?::\d+)?/g;
const CSS_URL = /url\(\s*['"]?([^'")]*)/g;

describe("remote origins in the build", () => {
  const files = builtFiles("static");

  it("no stylesheet loads from another origin", () => {
    const css = [...files].filter(([name]) => name.endsWith(".css"));
    expect(css.length).toBeGreaterThan(0);
    const remote = css.flatMap(([name, content]) => [
      ...(/@import/.test(content) ? [`${name}: @import`] : []),
      ...[...content.matchAll(CSS_URL)]
        .map((match) => match[1])
        .filter((url) => /^(?:[a-z]+:)?\/\//i.test(url) && !url.startsWith("data:"))
        .map((url) => `${name}: ${url}`),
    ]);
    expect(remote).toEqual([]);
  });

  it("the fonts are bundled", () => {
    expect([...files.keys()].filter((name) => name.endsWith(".woff2")).length).toBeGreaterThanOrEqual(3);
  });

  it("no script names an origin other than the known, never-requested ones", () => {
    const scripts = [...files].filter(([name]) => name.endsWith(".js"));
    expect(scripts.length).toBeGreaterThan(0);
    const unknown = scripts.flatMap(([name, content]) =>
      [...new Set(content.match(ORIGIN) ?? [])]
        .filter((origin) => !NAMED_NOT_LOADED.has(origin))
        .map((origin) => `${name}: ${origin}`),
    );
    expect(unknown).toEqual([]);
  });
});
