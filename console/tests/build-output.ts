import { existsSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { ROOT, walkFiles } from "./source-files";

// What `next build` wrote. These tests read it, so they run after the build (CI builds first);
// without one they fail rather than pass on nothing.
export const NEXT = join(ROOT, ".next");
// The build's caches and records of how it ran are not served or shipped.
const NOT_SHIPPED = ["cache", "trace", "trace-build", "diagnostics", "types", "dev"];

export function builtFiles(under = ""): Map<string, string> {
  const directory = join(NEXT, under);
  if (!existsSync(join(NEXT, "BUILD_ID")) || !existsSync(directory)) {
    throw new Error("no build output: run `npm run build` before these tests");
  }
  const files = walkFiles(directory).filter((path) => {
    const top = relative(NEXT, path).split(sep)[0];
    return !NOT_SHIPPED.includes(top);
  });
  return new Map(
    files.map((path) => [relative(NEXT, path).split(sep).join("/"), readFileSync(path, "latin1")]),
  );
}
