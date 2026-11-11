import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.vscodeclone.app',
  appName: 'VS Code Clone',
  webDir: 'www',
  backgroundColor: '#1e1e1e',
  android: {
    allowMixedContent: true
  },
  server: {
    androidScheme: 'https'
  }
};

export default config;
