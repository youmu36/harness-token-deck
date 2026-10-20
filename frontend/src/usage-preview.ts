import { createApp } from 'vue';
import UsagePreview from './app/UsagePreview.vue';
import { BridgeKey } from './app/ports';
import { unwrap } from './app/errors';
import { createMockBridge } from './infra/bridge/mock';
import './ui/base.css';
const bridge = createMockBridge();
const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
createApp(UsagePreview, { initialPreferences }).provide(BridgeKey, bridge).mount('#app');
