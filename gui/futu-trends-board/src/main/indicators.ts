// 技术指标计算服务 - 主进程版本
import { getConfig } from './configManager'
import { getParamsDB } from './paramsDB'
import type { KlineData, IndicatorResult } from '../types'
import type { IndicatorParams } from './paramsDB'

// 重新导出类型供主进程使用
export type { KlineData, IndicatorResult }

/**
 * 计算ATR (Average True Range)
 */
export function calculateATR(
  high: number[],
  low: number[],
  close: number[],
  period: number = 14
): number[] {
  const tr: number[] = []
  const atr: number[] = []

  for (let i = 0; i < high.length; i++) {
    if (i === 0) {
      tr.push(high[i] - low[i])
    } else {
      const hl = high[i] - low[i]
      const hc = Math.abs(high[i] - close[i - 1])
      const lc = Math.abs(low[i] - close[i - 1])
      tr.push(Math.max(hl, hc, lc))
    }
  }

  for (let i = 0; i < tr.length; i++) {
    if (i < period - 1) {
      atr.push(tr.slice(0, i + 1).reduce((sum, val) => sum + val, 0) / (i + 1))
    } else {
      atr.push(tr.slice(i - period + 1, i + 1).reduce((sum, val) => sum + val, 0) / period)
    }
  }

  return atr
}

/**
 * 计算EMA (Exponential Moving Average)
 */
export function calculateEMA(data: number[], period: number): number[] {
  const ema: number[] = []
  const multiplier = 2 / (period + 1)

  for (let i = 0; i < data.length; i++) {
    if (i === 0) {
      ema.push(data[i])
    } else {
      ema.push((data[i] - ema[i - 1]) * multiplier + ema[i - 1])
    }
  }

  return ema
}

/**
 * 计算KD随机指标
 */
export function calculateKD(
  high: number[],
  low: number[],
  close: number[],
  kPeriod: number = 14,
  dPeriod: number = 3,
  oversold: number = 20,
  overbought: number = 80
): { k: number[]; d: number[]; oversold: number; overbought: number } {
  const k: number[] = []
  const d: number[] = []

  for (let i = 0; i < close.length; i++) {
    const start = Math.max(0, i - kPeriod + 1)
    const end = i + 1
    const sliceHigh = high.slice(start, end)
    const sliceLow = low.slice(start, end)

    const highest = Math.max(...sliceHigh)
    const lowest = Math.min(...sliceLow)

    if (highest === lowest) {
      k.push(50)
    } else {
      k.push((100 * (close[i] - lowest)) / (highest - lowest))
    }
  }

  for (let i = 0; i < k.length; i++) {
    const start = Math.max(0, i - dPeriod + 1)
    const end = i + 1
    const sliceK = k.slice(start, end)
    d.push(sliceK.reduce((sum, val) => sum + val, 0) / sliceK.length)
  }

  return { k, d, oversold, overbought }
}

/**
 * 计算MACD指标 (基于ATR标准化)
 */
export function calculateMACD(
  close: number[],
  high: number[],
  low: number[],
  fastPeriod: number = 12,
  slowPeriod: number = 26,
  signalPeriod: number = 9
): { macd: number[]; signal: number[] } {
  const atr = calculateATR(high, low, close, slowPeriod)
  const emaFast = calculateEMA(close, fastPeriod)
  const emaSlow = calculateEMA(close, slowPeriod)

  const vmacd: number[] = []
  for (let i = 0; i < Math.min(emaFast.length, emaSlow.length, atr.length); i++) {
    if (atr[i] !== 0) {
      vmacd.push((100 * (emaFast[i] - emaSlow[i])) / atr[i])
    } else {
      vmacd.push(0)
    }
  }

  const signal = calculateEMA(vmacd, signalPeriod)

  return { macd: vmacd, signal }
}

/**
 * 计算RSI指标
 */
export function calculateRSI(
  close: number[], 
  period: number = 14,
  oversold: number = 30,
  overbought: number = 70
): { values: number[]; oversold: number; overbought: number } {
  const rsi: number[] = []
  const gains: number[] = []
  const losses: number[] = []

  for (let i = 0; i < close.length; i++) {
    if (i === 0) {
      gains.push(0)
      losses.push(0)
    } else {
      const change = close[i] - close[i - 1]
      gains.push(change > 0 ? change : 0)
      losses.push(change < 0 ? -change : 0)
    }
  }

  for (let i = 0; i < gains.length; i++) {
    if (i < period - 1) {
      rsi.push(50)
    } else {
      const avgGain =
        gains.slice(i - period + 1, i + 1).reduce((sum, val) => sum + val, 0) / period
      const avgLoss =
        losses.slice(i - period + 1, i + 1).reduce((sum, val) => sum + val, 0) / period

      if (avgLoss === 0) {
        rsi.push(100)
      } else {
        const rs = avgGain / avgLoss
        rsi.push(100 - 100 / (1 + rs))
      }
    }
  }

  return { values: rsi, oversold, overbought }
}

/**
 * 从参数数据库获取指标参数
 * @param stockCode - 股票代码
 * @param indicatorType - 指标类型 (MACD, KD, RSI)
 * @param dbPath - 数据库路径
 * @returns 指标参数或null
 */
async function getIndicatorParams(
  stockCode: string,
  indicatorType: 'MACD' | 'KD' | 'RSI',
  dbPath?: string
): Promise<IndicatorParams | null> {
  if (!dbPath) {
    return null
  }

  try {
    const db = getParamsDB(dbPath)
    const stockParams = await db.getStockParams(stockCode)

    if (!stockParams || !stockParams.best_params) {
      console.warn(`[Indicators] ${indicatorType} parameters not found for ${stockCode}`)
      return null
    }

    return stockParams.best_params as IndicatorParams
  } catch (error) {
    console.error(`[Indicators] Error getting ${indicatorType} params for ${stockCode}:`, error)
    return null
  }
}

/**
 * 计算所有技术指标
 * @param klineData - K线数据
 * @param stockCode - 股票代码（用于从数据库读取参数）
 */
export async function calculateIndicators(
  klineData: KlineData[],
  stockCode?: string
): Promise<IndicatorResult> {
  const time = klineData.map((d) => d.time)
  const close = klineData.map((d) => d.close)
  const high = klineData.map((d) => d.high)
  const low = klineData.map((d) => d.low)

  // 从配置获取参数
  const config = getConfig()
  const emaPeriod = config.EMA_PERIOD || 20

  // 获取数据库路径
  const macdDbPath = config.MACD_PARAMS_DB
  const kdDbPath = config.KD_PARAMS_DB
  const rsiDbPath = config.RSI_PARAMS_DB

  // 计算 EMA（不需要从数据库读取）
  const ema = calculateEMA(close, emaPeriod)

  // 初始化结果
  let macd: number[] = []
  let signal: number[] = []
  let k: number[] = []
  let d: number[] = []
  let kdOversold = 20
  let kdOverbought = 80
  let rsiValues: number[] = []
  let rsiOversold = 30
  let rsiOverbought = 70

  // 计算 MACD（尝试从数据库读取参数）
  if (stockCode && macdDbPath) {
    const macdParams = await getIndicatorParams(stockCode, 'MACD', macdDbPath)
    if (macdParams) {
      const fastPeriod = macdParams.fast_period || 12
      const slowPeriod = macdParams.slow_period || 26
      const signalPeriod = macdParams.signal_period || 9
      const result = calculateMACD(close, high, low, fastPeriod, slowPeriod, signalPeriod)
      macd = result.macd
      signal = result.signal
      console.log(`[Indicators] Using MACD params from DB for ${stockCode}:`, {
        fast: fastPeriod,
        slow: slowPeriod,
        signal: signalPeriod
      })
    } else {
      // 使用默认参数
      const result = calculateMACD(close, high, low)
      macd = result.macd
      signal = result.signal
    }
  } else {
    // 使用默认参数
    const result = calculateMACD(close, high, low)
    macd = result.macd
    signal = result.signal
  }

  // 计算 KD（尝试从数据库读取参数）
  if (stockCode && kdDbPath) {
    const kdParams = await getIndicatorParams(stockCode, 'KD', kdDbPath)
    if (kdParams) {
      const kPeriod = kdParams.k_period || 14
      const dPeriod = kdParams.d_period || 3
      kdOversold = kdParams.oversold || 20
      kdOverbought = kdParams.overbought || 80
      const result = calculateKD(high, low, close, kPeriod, dPeriod, kdOversold, kdOverbought)
      k = result.k
      d = result.d
      console.log(`[Indicators] Using KD params from DB for ${stockCode}:`, {
        k: kPeriod,
        d: dPeriod,
        oversold: kdOversold,
        overbought: kdOverbought
      })
    } else {
      // 使用默认参数
      const result = calculateKD(high, low, close)
      k = result.k
      d = result.d
      kdOversold = result.oversold
      kdOverbought = result.overbought
    }
  } else {
    // 使用默认参数
    const result = calculateKD(high, low, close)
    k = result.k
    d = result.d
    kdOversold = result.oversold
    kdOverbought = result.overbought
  }

  // 计算 RSI（尝试从数据库读取参数）
  if (stockCode && rsiDbPath) {
    const rsiParams = await getIndicatorParams(stockCode, 'RSI', rsiDbPath)
    if (rsiParams) {
      const rsiPeriod = rsiParams.rsi_period || 14
      rsiOversold = rsiParams.oversold || 30
      rsiOverbought = rsiParams.overbought || 70
      const result = calculateRSI(close, rsiPeriod, rsiOversold, rsiOverbought)
      rsiValues = result.values
      console.log(`[Indicators] Using RSI params from DB for ${stockCode}:`, {
        period: rsiPeriod,
        oversold: rsiOversold,
        overbought: rsiOverbought
      })
    } else {
      // 使用默认参数
      const result = calculateRSI(close)
      rsiValues = result.values
      rsiOversold = result.oversold
      rsiOverbought = result.overbought
    }
  } else {
    // 使用默认参数
    const result = calculateRSI(close)
    rsiValues = result.values
    rsiOversold = result.oversold
    rsiOverbought = result.overbought
  }

  return {
    time,
    ema,
    // 保持扁平格式（向后兼容）
    k,
    d,
    macd,
    signal,
    rsi: rsiValues,
    // 新增：嵌套对象格式（与第一版兼容，包含超买超卖线）
    kd: {
      k,
      d,
      oversold: kdOversold,
      overbought: kdOverbought
    },
    rsi_indicator: {
      values: rsiValues,
      oversold: rsiOversold,
      overbought: rsiOverbought
    },
    macd_indicator: {
      vmacd: macd,
      signal: signal,
      hist: macd.map((m, i) => m - signal[i])
    }
  }
}
