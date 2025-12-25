import { electronAPI } from '@electron-toolkit/preload'
import { contextBridge, ipcRenderer } from 'electron';

// Custom APIs for renderer
const api = {
  openChartWindow: (stockCode: string) => ipcRenderer.invoke('open-chart-window', stockCode),
  setWindowTitle: (title: string) => ipcRenderer.invoke('set-window-title', title),
  getConfig: () => ipcRenderer.invoke('get-config'),
  selectConfigFile: () => ipcRenderer.invoke('select-config-file'),
  reloadConfig: (configPath?: string) => ipcRenderer.invoke('reload-config', configPath),
  saveConfig: (config: any) => ipcRenderer.invoke('save-config', config),
  getStockList: () => ipcRenderer.invoke('get-stock-list'),
  getChartData: (stockCode: string, maxCount?: number) => 
    ipcRenderer.invoke('get-chart-data', stockCode, maxCount),
  // 日志相关 API
  getLogPath: () => ipcRenderer.invoke('get-log-path'),
  openLogDir: () => ipcRenderer.invoke('open-log-dir')
}

// Use `contextBridge` APIs to expose Electron APIs to
// renderer only if context isolation is enabled, otherwise
// just add to the DOM global.
if (process.contextIsolated) {
  try {
    contextBridge.exposeInMainWorld('electron', electronAPI)
    contextBridge.exposeInMainWorld('electronAPI', api)
    contextBridge.exposeInMainWorld('api', api)
  } catch (error) {
    console.error(error)
  }
} else {
  // @ts-ignore (define in dts)
  window.electron = electronAPI
  // @ts-ignore (define in dts)
  window.electronAPI = api
  // @ts-ignore (define in dts)
  window.api = api
}
