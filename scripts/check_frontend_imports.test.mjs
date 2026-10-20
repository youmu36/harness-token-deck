import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { checkSource } from './check_frontend_imports.mjs';

const root = path.resolve('frontend/src');
function check(file, source) { return checkSource(path.join(root, file), source, root); }

test('rejects relative, aliased, re-exported and dynamic forbidden imports', () => {
  for (const code of [
    "import X from '../features/wallet/X.vue'",
    "export { X } from '@/infra/bridge'",
    "const x = import('../infra/bridge')",
  ]) assert.equal(check('shared/query.ts', code).length, 1);
});
test('checks Vue scripts, bridge access and direct pywebview access', () => {
  assert.equal(check('ui/Test.vue', '<script setup>window.pywebview.api.call()</script>').length, 1);
  assert.equal(check('features/wallet/run.ts', "window['pywebview'].api.call()").length, 1);
  assert.equal(check('features/wallet/run.ts', "import x from '../../infra/bridge/desktop'").length, 1);
  assert.equal(check('features/wallet/run.ts', "import x from '../../infra/bridge'").length, 0);
  assert.equal(check('infra/bridge/desktop.ts', 'window.pywebview.api.call()').length, 0);
});
