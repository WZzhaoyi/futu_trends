/**
 * 应用配置常量
 */

// API 配置
let API_PORT = 8001;
export let API_BASE = `http://127.0.0.1:${API_PORT}`;
export const API_TIMEOUT = 5000;
export const MAX_KLINE_COUNT = 1000;

/**
 * 更新 API 端口
 * @param port - 新的端口号
 */
export function updateApiPort(port: number): void {
  API_PORT = port;
  API_BASE = `http://127.0.0.1:${port}`;
  console.log(`[Config] API base updated to: ${API_BASE}`);
}

/**
 * 初始化 API 端口（从 Electron 获取）
 */
export async function initApiPort(): Promise<void> {
  if (typeof window !== 'undefined' && window.electronAPI) {
    try {
      const port = await window.electronAPI.getApiPort();
      if (port) {
        updateApiPort(port);
      }
    } catch (error) {
      console.error('[Config] Failed to get API port:', error);
    }
  }
}

// 图表配置
export const CHART_COLORS = {
  // K线颜色
  CANDLESTICK_UP: 'rgba(255, 82, 82, 1)',
  CANDLESTICK_DOWN: 'rgba(0, 168, 67, 1)',
  
  // 成交量颜色
  VOLUME_UP: 'rgba(0, 168, 67, 0.5)',
  VOLUME_DOWN: 'rgba(255, 82, 82, 0.5)',
  VOLUME_BASE: 'rgba(76, 175, 80, 0.5)',
  
  // EMA 颜色
  EMA: 'rgba(224, 82, 211, 0.8)',
  
  // MACD 颜色
  MACD_LINE: 'rgba(33, 150, 243, 1)',
  MACD_SIGNAL: 'rgba(255, 152, 0, 1)',
  MACD_HIST_BASE: 'rgba(38, 166, 154, 1)',
  // MACD 柱状图颜色（根据趋势变化）
  MACD_HIST_POSITIVE_UP: 'rgba(255,82,82,1)',      // 正值且上升：深红色
  MACD_HIST_POSITIVE_DOWN: 'rgba(255,205,210,1)',  // 正值但下降：浅红色
  MACD_HIST_NEGATIVE_UP: 'rgba(178,223,219,1)',    // 负值但上升：浅绿色
  MACD_HIST_NEGATIVE_DOWN: 'rgba(38,166,154,1)',   // 负值且下降：深绿色
  
  // KD 颜色
  KD_K: 'rgba(33, 150, 243, 1)',
  KD_D: 'rgba(255, 152, 0, 1)',
  KD_OVERBOUGHT: 'rgba(255, 82, 82, 0.3)',
  KD_OVERSOLD: 'rgba(0, 168, 67, 0.3)',
  
  // RSI 颜色
  RSI: 'rgba(255, 152, 0, 1)',
  RSI_OVERBOUGHT: 'rgba(255, 82, 82, 0.3)',
  RSI_OVERSOLD: 'rgba(0, 168, 67, 0.3)',
} as const;

// 图表样式配置
export const CHART_STYLES = {
  BACKGROUND: '#191919',
  TEXT: '#ffffff',
  GRID: '#333333',
  PANE_STRETCH_FACTOR: 0.5, // 副图窗格高度比例
} as const;

// 重试配置
export const RETRY_CONFIG = {
  MAX_RETRIES: 3,
  RETRY_DELAY: 1000,
  SERVICE_CHECK_RETRIES: 10,
  SERVICE_CHECK_DELAY: 500,
} as const;

