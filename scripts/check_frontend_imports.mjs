import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const defaultRoot = fileURLToPath(new URL('../frontend/src/', import.meta.url));
const inside = (name, prefix) => name === prefix || name.startsWith(`${prefix}/`);

export function checkSource(filename, content, root = defaultRoot) {
  const errors = [];
  const relative = path.relative(root, filename).replaceAll('\\', '/');
  const script = filename.endsWith('.vue')
    ? [...content.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n')
    : content;
  const source = ts.createSourceFile(filename, script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  function checkImport(specifier, node) {
    let target = specifier;
    if (specifier.startsWith('.')) {
      target = path.relative(root, path.resolve(path.dirname(filename), specifier)).replaceAll('\\', '/');
    } else if (specifier.startsWith('@/')) {
      target = specifier.slice(2);
    }
    if ((inside(relative, 'shared') || inside(relative, 'ui')) &&
        (inside(target, 'features') || inside(target, 'infra'))) {
      fail(node, 'shared/ui cannot import features or infra');
    }
    if (inside(relative, 'features') && inside(target, 'infra') &&
        target !== 'infra/bridge' && target !== 'infra/bridge/index' && target !== 'infra/bridge/index.ts') {
      fail(node, 'features must access infra through the bridge entry point');
    }
  }
  function fail(node, message) {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
    errors.push(`${relative}:${line + 1}: ${message}`);
  }
  function visit(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
        node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      checkImport(node.moduleSpecifier.text, node);
    }
    if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
        (ts.isIdentifier(node.expression) && node.expression.text === 'require'))) {
      if (node.arguments[0] && ts.isStringLiteral(node.arguments[0])) checkImport(node.arguments[0].text, node);
    }
    const direct = ts.isPropertyAccessExpression(node) && node.name.text === 'pywebview';
    const indexed = ts.isElementAccessExpression(node) && ts.isStringLiteral(node.argumentExpression) &&
      node.argumentExpression.text === 'pywebview';
    if ((direct || indexed) && relative !== 'infra/bridge/desktop.ts') {
      fail(node, 'only infra/bridge/desktop.ts may access pywebview');
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return errors;
}

export function check(root = defaultRoot) {
  const errors = [];
  function walk(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(filename);
      else if (/\.(vue|ts|tsx|js|mjs)$/.test(entry.name)) {
        errors.push(...checkSource(filename, fs.readFileSync(filename, 'utf8'), root));
      }
    }
  }
  walk(root);
  return errors;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const errors = check();
  for (const error of errors) console.error(error);
  console.log(`Frontend architecture: ${errors.length} violation(s)`);
  process.exitCode = errors.length ? 1 : 0;
}
