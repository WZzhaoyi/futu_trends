// 应用配置类型定义（与主进程保持一致）
export interface AppConfig {
  DATA_SOURCE?: string
  FUTU_HOST?: string
  FUTU_PORT?: number
  FUTU_WS_PORT?: number
  FUTU_WS_KEY?: string
  FUTU_API_KEY?: string
  FUTU_GROUP?: string
  FUTU_CODE_LIST?: string
  FUTU_PUSH_TYPE?: string
  EMA_PERIOD?: number
  KD_PARAMS_DB?: string
  MACD_PARAMS_DB?: string
  RSI_PARAMS_DB?: string
  DATA_DIR?: string
  DARK_MODE?: boolean
  PROXY?: string
}

/**
 * 从主进程获取配置（统一入口，不缓存）
 */
async function getConfigFromMain(): Promise<AppConfig> {
  if (typeof window !== 'undefined' && window.electronAPI) {
    try {
      return await window.electronAPI.getConfig();
    } catch (error) {
      console.warn('[Config] Failed to get config from main process', error);
      return {};
    }
  }
  return {};
}


/**
 * 获取配置（异步，从主进程获取最新配置）
 */
export async function getAppConfig(): Promise<AppConfig> {
  return await getConfigFromMain();
}

/**
 * 重新加载配置
 */
export async function reloadAppConfig(configPath?: string): Promise<AppConfig> {
  if (typeof window !== 'undefined' && window.electronAPI) {
    try {
      return await window.electronAPI.reloadConfig(configPath);
    } catch (error) {
      console.warn('[Config] Failed to reload config', error);
      return await getConfigFromMain();
    }
  }
  return {};
}

// 配置辅助函数（从主进程获取）
export const API_CONFIG = {
  async getFUTU_WS_URL(): Promise<string> {
    const config = await getConfigFromMain();
    if (config.FUTU_WS_PORT && config.FUTU_HOST) {
      return `ws://${config.FUTU_HOST}:${config.FUTU_WS_PORT}`;
    }
    return 'ws://127.0.0.1:33333';
  },
  async getFUTU_PASSWORD(): Promise<string> {
    const config = await getConfigFromMain();
    return config.FUTU_WS_KEY || config.FUTU_API_KEY || '';
  },
  AKTOOLS_BASE_URL: 'http://localhost:8080/api',
};

export const CHART_CONFIG = {
  async getMAX_KLINE_COUNT(): Promise<number> {
    const config = await getConfigFromMain();
    return config.EMA_PERIOD || 200;
  },
  DEFAULT_KLINE_PERIOD: '1d',
};

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

export const CHART_STYLES = {
  BACKGROUND: '#191919',
  TEXT: '#ffffff',
  GRID: '#333333',
  PANE_STRETCH_FACTOR: 0.5, // 副图窗格高度比例
} as const;
