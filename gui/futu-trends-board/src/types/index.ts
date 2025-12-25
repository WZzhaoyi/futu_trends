// 统一的类型定义文件 - 避免重复定义

/**
 * 股票信息
 */
export interface Stock {
  code: string  // 统一格式：市场.代码，如 "SH.600000"
  name: string
  market: 'SH' | 'SZ' | 'HK' | 'US'
}

/**
 * K线数据
 */
export interface KlineData {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

/**
 * MACD 指标数据
 */
export interface MacdIndicator {
  vmacd: number[]
  signal: number[]
  hist: number[]
}

/**
 * KD 指标数据（包含超买超卖线）
 */
export interface KdIndicator {
  k: number[]
  d: number[]
  oversold: number
  overbought: number
}

/**
 * RSI 指标数据（包含超买超卖线）
 */
export interface RsiIndicator {
  values: number[]
  oversold: number
  overbought: number
}

/**
 * 技术指标计算结果
 */
export interface IndicatorResult {
  time: string[]
  ema?: number[]
  k?: number[]
  d?: number[]
  macd?: number[]
  signal?: number[]
  rsi?: number[]
  // 新增：嵌套对象格式（与第一版兼容）
  kd?: KdIndicator
  rsi_indicator?: RsiIndicator
  macd_indicator?: MacdIndicator
}

/**
 * 完整的图表数据（K线 + 指标）
 */
export interface ChartData {
  stock: Stock
  klines: KlineData[]
  indicators: IndicatorResult
}

/**
 * 股票列表获取结果
 */
export interface StockListResult {
  stocks: Stock[]
  source: 'futu_group' | 'code_list' | 'unknown'
}

