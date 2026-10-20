import { expect, test, vi } from 'vitest';
import { createTheme, initialTheme } from '../src/app/theme';
test('system theme responds live while explicit themes stay fixed, and listener is disposed', () => {
  let listener: (() => void) | undefined;
  const media = { matches: false, addEventListener: vi.fn((_: string, fn: () => void) => { listener = fn; }), removeEventListener: vi.fn() };
  const theme = createTheme('system', document, media as unknown as MediaQueryList);
  expect(theme.resolved.value).toBe('light'); media.matches = true; listener!();
  expect(document.documentElement.dataset.theme).toBe('dark');
  theme.set('light'); media.matches = false; listener!(); media.matches = true; listener!();
  expect(document.documentElement.dataset.theme).toBe('light');
  theme.set('system'); expect(document.documentElement.dataset.theme).toBe('dark');
  theme.dispose(); expect(media.removeEventListener).toHaveBeenCalledWith('change', listener);
  expect(initialTheme('#/settings?theme=dark&logPath=example')).toBe('dark');
});
