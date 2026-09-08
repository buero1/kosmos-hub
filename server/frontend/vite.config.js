import { defineConfig } from 'vite';
import { resolve } from 'node:path';

export default defineConfig({
  build: {
    emptyOutDir: false,
    lib: {
      entry: resolve(import.meta.dirname, 'jodit-editor.js'),
      formats: ['iife'],
      name: 'KosmosJoditEditor',
      fileName: () => 'jodit-editor.js',
      cssFileName: 'jodit-editor',
    },
    outDir: resolve(import.meta.dirname, '../app/static'),
  },
});
