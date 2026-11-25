import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

export const ROOT = fileURLToPath(new URL("..", import.meta.url));

// Everything under console/ that is code, wherever it is, except what never ships: the
// dependencies, the build, the fixture layer, the tests and the dev scripts.
const NOT_SHIPPED = new Set(["node_modules", ".next", "mock", "tests", "scripts"]);
const CODE = /\.(?:ts|tsx|js|jsx|mjs|cjs|mts|cts)$/;
const TEST = /\.test\.[a-z]+$/;

function walk(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return NOT_SHIPPED.has(entry.name) ? [] : walk(path);
    }
    return [path];
  });
}

// Every file under a directory, nothing left out.
export function walkFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walkFiles(path) : [path];
  });
}

export function shippedSources(): Map<string, string> {
  const files = walk(ROOT)
    .filter((path) => CODE.test(path) && !TEST.test(path))
    .sort();
  return new Map(
    files.map((path) => [relative(ROOT, path).split(sep).join("/"), readFileSync(path, "utf-8")]),
  );
}

function scriptKind(name: string): ts.ScriptKind {
  if (/\.(?:tsx|jsx)$/.test(name)) return ts.ScriptKind.TSX;
  if (/\.(?:js|mjs|cjs)$/.test(name)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

// Every name the code uses, read from its syntax tree, so comments and page text do not count and
// a renamed or quoted use still does: each identifier (an alias `const f = fetch` names `fetch`), each
// string that is exactly a name (`window["fetch"]`), each JSX attribute, and `document.cookie`
// (any `.cookie` or `["cookie"]`).
export function namesUsed(source: string, name = "sample.tsx"): Set<string> {
  const tree = ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true, scriptKind(name));
  const names = new Set<string>();
  const visit = (node: ts.Node) => {
    if (ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) {
      names.add(node.text);
    } else if (ts.isStringLiteralLike(node)) {
      names.add(node.text);
    }
    if (
      (ts.isPropertyAccessExpression(node) && node.name.text === "cookie") ||
      (ts.isElementAccessExpression(node) &&
        ts.isStringLiteralLike(node.argumentExpression) &&
        node.argumentExpression.text === "cookie")
    ) {
      names.add("document.cookie");
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  return names;
}

export function filesUsing(name: string, sources = shippedSources()): string[] {
  return [...sources].filter(([file, source]) => namesUsed(source, file).has(name)).map(([file]) => file);
}
