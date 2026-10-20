import fs from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { checkContrast, contrast } from './check_contrast.mjs';

const css = fs.readFileSync(new URL('../frontend/src/ui/tokens.css', import.meta.url), 'utf8');
test('WCAG reference ratios', () => {
  assert.equal(contrast('#000000', '#ffffff'), 21);
  assert.equal(contrast('#202c3a', '#202c3a'), 1);
});
test('a lowered contrast and a missing theme both fail the gate', () => {
  const background = checkContrast(css).themes.light.bg;
  const faded = checkContrast(css.replace(/--text:\s*#[0-9a-f]{6};/i, `--text: ${background};`));
  assert.ok(faded.errors.some(error => error.startsWith('light: --text / --bg')));
  const missing = checkContrast(css.replace("html[data-theme='dark']", 'html[data-preview]'));
  assert.ok(missing.errors.includes('Missing dark theme'));
});
test('missing required color tokens fail explicitly', () => {
  assert.ok(checkContrast(css.replace(/--cache-loss:\s*#[0-9a-f]{6};/i, '')).errors.some(error => error.includes('--cache-loss')));
});
