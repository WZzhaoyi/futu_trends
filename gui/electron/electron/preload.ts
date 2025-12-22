import { contextBridge, ipcRenderer } from 'electron';

// 定义 Electron API 类型
export interface ElectronAPI {
  openChartWindow: (code: string) => Promise<number>;
  setWindowTitle: (title: string) => Promise<void>;
  getApiPort: () => Promise<number>;
  onApiPortChanged: (callback: (port: number) => void) => void;
  selectConfigFile: () => Promise<string | null>;
  restartBackend: (configPath?: string | null) => Promise<{ success: boolean; message: string }>;
}

// 暴露安全的 API 给渲染进程
contextBridge.exposeInMainWorld('electronAPI', {
  openChartWindow: (code: string) => ipcRenderer.invoke('open-chart-window', code),
  setWindowTitle: (title: string) => ipcRenderer.invoke('set-window-title', title),
  getApiPort: () => ipcRenderer.invoke('get-api-port'),
  onApiPortChanged: (callback: (port: number) => void) => {
    ipcRenderer.on('api-port-changed', (_, port: number) => callback(port));
  },
  selectConfigFile: () => ipcRenderer.invoke('select-config-file'),
  restartBackend: (configPath?: string | null) => ipcRenderer.invoke('restart-backend', configPath),
} as ElectronAPI);

// 类型声明（在 src/types/electron.d.ts 中）
declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}

