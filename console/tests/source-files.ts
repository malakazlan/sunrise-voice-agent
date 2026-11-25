import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = fileURLToPath(new URL("..", import.meta.url));

// The code that ships: everything under these, and the proxy, less the tests.
const SHIPPED = ["app", "components", "lib"];

function walk(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

export function shippedSources(): Map<string, string> {
  const files = [...SHIPPED.flatMap((name) => walk(join(ROOT, name))), join(ROOT, "proxy.ts")]
    .filter((path) => /\.(ts|tsx)$/.test(path) && !/\.test\.tsx?$/.test(path))
    .sort();
  return new Map(
    files.map((path) => [relative(ROOT, path).split(sep).join("/"), readFileSync(path, "utf-8")]),
  );
}

// Comments name things the code does not do; only code counts.
export function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

export function filesUsing(pattern: RegExp, sources = shippedSources()): string[] {
  return [...sources].filter(([, source]) => pattern.test(withoutComments(source))).map(([name]) => name);
}

export function walkFiles(directory: string): string[] {
  return walk(directory);
}
