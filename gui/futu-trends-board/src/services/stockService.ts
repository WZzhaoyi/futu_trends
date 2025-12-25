// 股票数据服务 - 渲染进程 IPC 包装层
// 所有数据处理和计算都在主进程完成

import type { Stock, KlineData, IndicatorResult, ChartData, StockListResult } from '../types'

// 重新导出类型供渲染进程使用
export type { Stock, KlineData, IndicatorResult, ChartData, StockListResult }

/**
 * 获取股票列表（通过 IPC 从主进程获取）
 */
export async function getStockList(): Promise<Stock[]> {
  if (typeof window === 'undefined' || !window.electronAPI) {
    throw new Error('Electron API not available')
  }

  const result = await window.electronAPI.getStockList()
  console.log(`[StockService] Loaded ${result.stocks.length} stocks from ${result.source}`)
  
  return result.stocks
}

/**
 * 获取图表数据（包含 K线 + 指标，通过 IPC 从主进程获取）
 */
export async function getChartData(stockCode: string, maxCount?: number): Promise<ChartData> {
  if (typeof window === 'undefined' || !window.electronAPI) {
    throw new Error('Electron API not available')
  }

  console.log(`[StockService] Requesting chart data for ${stockCode}`)
  const chartData = await window.electronAPI.getChartData(stockCode, maxCount)
  console.log(`[StockService] Received chart data:`, {
    stock: chartData.stock.code,
    klineCount: chartData.klines.length,
    hasIndicators: {
      ema: !!chartData.indicators.ema,
      macd: !!chartData.indicators.macd,
      kd: !!chartData.indicators.k,
      rsi: !!chartData.indicators.rsi
    }
  })
  
  return chartData
}
