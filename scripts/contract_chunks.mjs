// Partition Ajv's generated dependency graph. Shared DTO validators have one owner.
import ts from 'typescript';

export function contractChunks(source, groups) {
  const tree = ts.createSourceFile('validators.js', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const declarations = new Map();
  for (const statement of tree.statements) {
    if (ts.isExpressionStatement(statement)) continue; // "use strict"
    const name = ts.isFunctionDeclaration(statement) ? statement.name?.text
      : ts.isVariableStatement(statement) && statement.declarationList.declarations.length === 1
        ? statement.declarationList.declarations[0].name.getText(tree) : undefined;
    if (!name || !/^\w+$/.test(name) || declarations.has(name)) throw new Error('Unexpected Ajv declaration');
    declarations.set(name, { code: statement.getText(tree).replace(/^export\s+/, ''), statement, dependencies: new Set(), owners: new Set() });
  }
  for (const declaration of declarations.values()) {
    const visit = node => {
      if (ts.isIdentifier(node) && declarations.has(node.text)) declaration.dependencies.add(node.text);
      ts.forEachChild(node, visit);
    };
    visit(declaration.statement);
  }
  for (const [group, roots] of Object.entries(groups)) {
    const visit = name => {
      const declaration = declarations.get(name);
      if (!declaration) throw new Error(`Missing Ajv declaration: ${name}`);
      if (declaration.owners.has(group)) return;
      declaration.owners.add(group);
      for (const dependency of declaration.dependencies) visit(dependency);
    };
    for (const root of roots) visit(root);
  }
  const owner = declaration => declaration.owners.size > 1 ? 'shared' : [...declaration.owners][0];
  const output = {};
  for (const group of ['shared', ...Object.keys(groups)]) {
    const members = [...declarations].filter(([, declaration]) => owner(declaration) === group);
    const imports = new Set();
    for (const [, declaration] of members) {
      for (const dependency of declaration.dependencies) {
        if (owner(declarations.get(dependency)) !== group) imports.add(dependency);
      }
    }
    output[group] = (imports.size ? `import { ${[...imports].sort().join(', ')} } from './validators.shared.js';\n` : '')
      + members.map(([, declaration]) => declaration.code.replace(/^(const|let|var|function)\b/, 'export $1')).join('\n') + '\n';
  }
  return output;
}
