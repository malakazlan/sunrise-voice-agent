import { existsSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { ROOT, walkFiles } from "./source-files";

// What `next build` wrote. These tests read it, so they run after the build (CI builds first);
// without one they fail rather than pass on nothing.
export const NEXT = join(ROOT, ".next");
// The build's caches and records of how it ran are not served or shipped.
const NOT_SHIPPED = ["cache", "trace", "trace-build", "diagnostics", "types", "dev"];
// The standalone server's copies of installed packages (31 MB of third-party code, and most of
// the read): the console's own code, the fixture layer included if it ever leaked, is traced
// into .next/server and .next/standalone/.next, never into these.
const PACKAGE_COPIES = "standalone/node_modules/";

export function builtFiles(under = ""): Map<string, string> {
  const directory = join(NEXT, under);
  if (!existsSync(join(NEXT, "BUILD_ID")) || !existsSync(directory)) {
    throw new Error("no build output: run `npm run build` before these tests");
  }
  const files = walkFiles(directory).filter((path) => {
    const name = relative(NEXT, path).split(sep).join("/");
    return !NOT_SHIPPED.includes(name.split("/")[0]) && !name.startsWith(PACKAGE_COPIES);
  });
  return new Map(
    files.map((path) => [relative(NEXT, path).split(sep).join("/"), readFileSync(path, "latin1")]),
  );
}
