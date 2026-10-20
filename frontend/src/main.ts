import { createApp } from 'vue';
import App from './App.vue';
import './ui/base.css';
import { createBridge, installEvents } from './infra/bridge';
import { BridgeKey } from './app/ports';
import { createTheme, initialTheme } from './app/theme';

// Resolve the boot theme before mounting; Shell owns the persisted theme thereafter.
const bootTheme = createTheme(initialTheme(window.location.hash));

const bridge = await createBridge();
const uninstallEvents = installEvents(bridge);
const app = createApp(App);
app.provide(BridgeKey, bridge);
app.mount('#app');
bootTheme.dispose();
if (import.meta.hot) import.meta.hot.dispose(() => {
  uninstallEvents();
  bridge.dispose();
  app.unmount();
});
