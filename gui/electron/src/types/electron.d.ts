export interface ElectronAPI {
  openChartWindow: (code: string) => Promise<number>;
  setWindowTitle: (title: string) => Promise<void>;
  getApiPort: () => Promise<number>;
  onApiPortChanged: (callback: (port: number) => void) => void;
}

declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}

