/**
 * 应用配置常量
 */

// API 配置
let API_PORT = 8001;
export let API_BASE = `http://127.0.0.1:${API_PORT}`;
export const API_TIMEOUT = 5000;
export const MAX_KLINE_COUNT = 1000;

// API 端口变化监听器
type PortChangeListener = (port: number, apiBase: string) => void;
const portChangeListeners: PortChangeListener[] = [];

/**
 * 获取当前 API 端口
 * @returns 当前端口号
 */
export function getApiPort(): number {
  return API_PORT;
}

/**
 * 获取当前 API 基础地址（动态获取，确保总是最新值）
 * @returns API 基础地址
 */
export function getApiBase(): string {
  return `http://127.0.0.1:${API_PORT}`;
}

/**
 * 构建完整的 API URL
 * @param path - API 路径（以 / 开头）
 * @returns 完整的 API URL
 */
export function getApiUrl(path: string): string {
  const base = getApiBase();
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  return `${base}${cleanPath}`;
}

/**
 * 添加端口变化监听器
 * @param listener - 监听器函数
 * @returns 取消监听的函数
 */
export function onApiPortChanged(listener: PortChangeListener): () => void {
  portChangeListeners.push(listener);
  return () => {
    const index = portChangeListeners.indexOf(listener);
    if (index > -1) {
      portChangeListeners.splice(index, 1);
    }
  };
}

/**
 * 更新 API 端口
 * @param port - 新的端口号
 */
export function updateApiPort(port: number): void {
  if (port === API_PORT) {
    return; // 端口未变化，无需更新
  }
  
  const oldPort = API_PORT;
  API_PORT = port;
  API_BASE = `http://127.0.0.1:${port}`;
  
  console.log(`[Config] API port updated from ${oldPort} to ${port}, API base: ${API_BASE}`);
  
  // 通知所有监听器
  portChangeListeners.forEach(listener => {
    try {
      listener(port, API_BASE);
    } catch (error) {
      console.error('[Config] Error in port change listener:', error);
    }
  });
}

/**
 * 初始化 API 端口（从 Electron 获取）
 */
export async function initApiPort(): Promise<void> {
  if (typeof window !== 'undefined' && window.electronAPI) {
    try {
      const port = await window.electronAPI.getApiPort();
      if (port && port !== API_PORT) {
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

