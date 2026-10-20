import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const tokenFile = fileURLToPath(new URL('../frontend/src/ui/tokens.css', import.meta.url));
const backgrounds = ['bg', 'surface', 'surface-2'];
export const pairs = [
  ...['text', 'text-muted', 'text-faint', 'accent', 'money'].flatMap(fg => backgrounds.map(bg => [fg, bg, 4.5])),
  ['text', 'accent-soft', 4.5], ['accent', 'accent-soft', 4.5], ['accent-contrast', 'accent', 4.5],
  ...['ok', 'warn', 'danger', 'info'].flatMap(fg => [
    ...backgrounds.map(bg => [fg, bg, 4.5]), [fg, `${fg}-soft`, 4.5],
  ]),
  ...['cache-bust', 'cache-loss', 'chart-1', 'chart-2', 'chart-3', 'chart-4']
    .flatMap(fg => backgrounds.map(bg => [fg, bg, 3])),
];

function luminance(hex) {
  const channels = hex.slice(1).match(/../g).map(channel => {
    const value = parseInt(channel, 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

export function contrast(foreground, background) {
  const a = luminance(foreground), b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

export function checkContrast(css) {
  const blocks = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  const themes = {};
  const errors = [];
  let count = 0;
  let minimumText = Infinity;
  let minimumGraphic = Infinity;
  for (const theme of ['light', 'dark']) {
    const block = blocks.find(([ , selector]) => new RegExp(`html\\[data-theme=['"]${theme}['"]\\]`).test(selector));
    if (!block) { errors.push(`Missing ${theme} theme`); continue; }
    const tokens = Object.fromEntries([...block[2].matchAll(/--([\w-]+)\s*:\s*(#[0-9a-f]{6})\s*;/gi)].map(([, name, color]) => [name, color]));
    themes[theme] = tokens;
    for (const [fg, bg, threshold] of pairs) {
      if (!tokens[fg] || !tokens[bg]) { errors.push(`${theme}: missing --${fg} or --${bg}`); continue; }
      const ratio = contrast(tokens[fg], tokens[bg]);
      count++;
      if (threshold === 4.5) minimumText = Math.min(minimumText, ratio);
      else minimumGraphic = Math.min(minimumGraphic, ratio);
      if (ratio < threshold) errors.push(`${theme}: --${fg} / --${bg} = ${ratio.toFixed(3)} < ${threshold}`);
    }
  }
  return { errors, count, minimumText, minimumGraphic, themes };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = checkContrast(fs.readFileSync(tokenFile, 'utf8'));
  result.errors.forEach(error => console.error(error));
  console.log(`Contrast: ${result.count} pairs, ${result.errors.length} failure(s); text min ${result.minimumText.toFixed(3)}, graphics min ${result.minimumGraphic.toFixed(3)}`);
  process.exitCode = result.errors.length ? 1 : 0;
}
