/**
 * 图表相关类型定义
 */

export interface KlineData {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface MacdIndicator {
  vmacd: number[];
  signal: number[];
  hist: number[];
}

export interface KdIndicator {
  k: number[];
  d: number[];
  oversold: number;
  overbought: number;
}

export interface RsiIndicator {
  values: number[];
  oversold: number;
  overbought: number;
}

export interface Indicators {
  time?: string[];
  ema?: number[];
  macd?: MacdIndicator;
  kd?: KdIndicator;
  rsi?: RsiIndicator;
}

export interface Stock {
  code: string;
  name: string;
}

