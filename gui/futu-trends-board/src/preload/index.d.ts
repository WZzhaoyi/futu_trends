import { ElectronAPI } from '@electron-toolkit/preload'
import { AppConfig } from 'src/main/configManager'
import type { Stock, KlineData, IndicatorResult, ChartData, StockListResult } from '../types'

export interface ConfigFileResult {
  path: string
  config: AppConfig
}

// 重新导出类型供渲染进程使用
export type { Stock, KlineData, IndicatorResult, ChartData, StockListResult }

declare global {
  interface Window {
    electron: ElectronAPI
    electronAPI: {
      openChartWindow: (stockCode: string) => Promise<number>
      setWindowTitle: (title: string) => Promise<void>
      getConfig: () => Promise<AppConfig>
      selectConfigFile: () => Promise<ConfigFileResult | null>
      reloadConfig: (configPath?: string) => Promise<AppConfig>
      getStockList: () => Promise<StockListResult>
      getChartData: (stockCode: string, maxCount?: number) => Promise<ChartData>
    }
    api: unknown
  }
}
