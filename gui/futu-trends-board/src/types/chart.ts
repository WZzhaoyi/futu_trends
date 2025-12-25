// 图表相关类型定义 - 已迁移到 types/index.ts
// 此文件保留用于向后兼容

export type { Stock, KlineData, IndicatorResult, ChartData } from './index'

export interface Indicators {
  time: string[];
  ema?: number[];
  k?: number[];
  d?: number[];
  macd?: number[];
  signal?: number[];
  rsi?: number[];
}
