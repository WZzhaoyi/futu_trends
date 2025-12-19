import { createApp } from 'vue';
import App from './App.vue';
import naive from 'naive-ui';
import { initApiPort, updateApiPort } from './config';

const app = createApp(App);
app.use(naive);

// 初始化 API 端口
if (typeof window !== 'undefined' && window.electronAPI) {
  // 获取初始端口
  initApiPort()
    .then(() => {
      console.log('[Main] API port initialized');
    })
    .catch((error) => {
      console.error('[Main] Failed to initialize API port:', error);
    });
  
  // 监听端口变化（从 Electron 主进程）
  window.electronAPI.onApiPortChanged((port: number) => {
    updateApiPort(port);
    console.log(`[Main] API port changed to: ${port}`);
  });
}

app.mount('#app');

