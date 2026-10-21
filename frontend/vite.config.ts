import { fileURLToPath, URL } from 'node:url';
import vue from '@vitejs/plugin-vue';
import { configDefaults, defineConfig } from 'vitest/config';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  plugins: [vue()],
  build: {
    outDir: '../backend/src/htd/web',
    emptyOutDir: true,
  },
  test: {
    environment: 'happy-dom',
    include: ['tests/**/*.test.ts'],
    exclude: [...configDefaults.exclude, 'tests/dst/**'],
  },
});
