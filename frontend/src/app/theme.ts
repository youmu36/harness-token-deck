import { computed, ref } from 'vue';
export type Theme = 'light' | 'dark' | 'system';

export function createTheme(initial: Theme = 'system', doc: Document = document, media = window.matchMedia('(prefers-color-scheme: dark)')) {
  const preference = ref<Theme>(initial);
  const systemDark = ref(media.matches);
  const resolved = computed(() => preference.value === 'system' ? systemDark.value ? 'dark' : 'light' : preference.value);
  const apply = () => { doc.documentElement.dataset.theme = resolved.value; };
  const changed = () => { systemDark.value = media.matches; apply(); };
  const set = (value: Theme) => { preference.value = value; apply(); };
  media.addEventListener('change', changed);
  apply();
  return { preference, resolved, set, dispose: () => media.removeEventListener('change', changed) };
}

export function initialTheme(hash: string): Theme {
  const value = new URLSearchParams(hash.split('?')[1]).get('theme');
  return value === 'light' || value === 'dark' ? value : 'system';
}
