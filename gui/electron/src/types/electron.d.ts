export interface ElectronAPI {
  openChartWindow: (code: string) => Promise<number>;
  setWindowTitle: (title: string) => Promise<void>;
  getApiPort: () => Promise<number>;
  onApiPortChanged: (callback: (port: number) => void) => void;
  selectConfigFile: () => Promise<string | null>;
  restartBackend: (configPath?: string | null) => Promise<{ success: boolean; message: string }>;
}

declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}

