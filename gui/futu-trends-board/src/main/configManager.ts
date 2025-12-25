// 配置管理器 - 读取和管理ini配置文件
import * as fs from 'fs'
import * as path from 'path'
import { parse, stringify } from 'ini'
import { app } from 'electron'

export interface AppConfig {
  // 数据源配置
  DATA_SOURCE?: string // futu, yfinance, akshare
  FUTU_HOST?: string
  FUTU_PORT?: number
  FUTU_WS_PORT?: number
  FUTU_WS_KEY?: string
  FUTU_API_KEY?: string
  FUTU_GROUP?: string
  FUTU_CODE_LIST?: string
  FUTU_PUSH_TYPE?: string

  // 技术指标配置
  EMA_PERIOD?: number
  KD_PARAMS_DB?: string
  MACD_PARAMS_DB?: string
  RSI_PARAMS_DB?: string

  // 其他配置
  DATA_DIR?: string
  DARK_MODE?: boolean
  PROXY?: string
}

let currentConfig: AppConfig | null = null
let currentConfigPath: string | null = null

/**
 * 解析ini配置文件
 */
export function parseConfigFile(filePath: string): AppConfig {
  try {
    if (!fs.existsSync(filePath)) {
      console.warn(`[ConfigManager] Config file not found: ${filePath}`)
      return {}
    }

    const content = fs.readFileSync(filePath, 'utf-8')
    const parsed = parse(content)

    // 提取CONFIG节的内容
    const configSection = parsed.CONFIG || parsed

    const appConfig: AppConfig = {}

    // 解析配置项
    if (configSection.DATA_SOURCE) {
      appConfig.DATA_SOURCE = configSection.DATA_SOURCE
    }

    if (configSection.FUTU_HOST) {
      appConfig.FUTU_HOST = configSection.FUTU_HOST
    }

    if (configSection.FUTU_PORT) {
      appConfig.FUTU_PORT = parseInt(configSection.FUTU_PORT, 10)
    }

    if (configSection.FUTU_WS_PORT) {
      appConfig.FUTU_WS_PORT = parseInt(configSection.FUTU_WS_PORT, 10)
    }

    if (configSection.FUTU_WS_KEY) {
      appConfig.FUTU_WS_KEY = configSection.FUTU_WS_KEY
    }

    if (configSection.FUTU_API_KEY) {
      appConfig.FUTU_API_KEY = configSection.FUTU_API_KEY
    }

    if (configSection.FUTU_GROUP) {
      appConfig.FUTU_GROUP = configSection.FUTU_GROUP
    }

    if (configSection.FUTU_CODE_LIST) {
      appConfig.FUTU_CODE_LIST = configSection.FUTU_CODE_LIST
    }

    if (configSection.FUTU_PUSH_TYPE) {
      appConfig.FUTU_PUSH_TYPE = configSection.FUTU_PUSH_TYPE
    }

    if (configSection.EMA_PERIOD) {
      appConfig.EMA_PERIOD = parseInt(configSection.EMA_PERIOD, 10)
    }

    if (configSection.KD_PARAMS_DB) {
      appConfig.KD_PARAMS_DB = configSection.KD_PARAMS_DB
    }

    if (configSection.MACD_PARAMS_DB) {
      appConfig.MACD_PARAMS_DB = configSection.MACD_PARAMS_DB
    }

    if (configSection.RSI_PARAMS_DB) {
      appConfig.RSI_PARAMS_DB = configSection.RSI_PARAMS_DB
    }

    if (configSection.DATA_DIR) {
      appConfig.DATA_DIR = configSection.DATA_DIR
    }

    if (configSection.DARK_MODE !== undefined) {
      appConfig.DARK_MODE = configSection.DARK_MODE === 'True' || configSection.DARK_MODE === 'true'
    }

    if (configSection.PROXY) {
      appConfig.PROXY = configSection.PROXY
    }

    console.log(`[ConfigManager] Config loaded from: ${filePath}`)
    return appConfig
  } catch (error) {
    console.error(`[ConfigManager] Failed to parse config file: ${filePath}`, error)
    return {}
  }
}

/**
 * 加载配置文件
 */
export function loadConfig(filePath?: string): AppConfig {
  if (filePath) {
    currentConfigPath = path.isAbsolute(filePath) ? filePath : path.resolve(filePath)
  } else {
    // 尝试查找默认配置文件
    const possiblePaths = [
      path.join(process.cwd(), 'config.ini'),
      path.join(process.cwd(), 'env', 'signal_electron.ini'),
      path.join(process.cwd(), '..', 'env', 'signal_electron.ini'),
      path.join(__dirname, '../../config.ini'),
      path.join(__dirname, '../../../env/signal_electron.ini'),
    ]

    for (const possiblePath of possiblePaths) {
      if (fs.existsSync(possiblePath)) {
        currentConfigPath = possiblePath
        break
      }
    }
  }

  if (currentConfigPath) {
    currentConfig = parseConfigFile(currentConfigPath)
  } else {
    console.warn('[ConfigManager] No config file found, using defaults')
    currentConfig = {}
  }

  return currentConfig || {}
}

/**
 * 获取当前配置
 */
export function getConfig(): AppConfig {
  return currentConfig || {}
}

/**
 * 获取当前配置文件路径
 */
export function getConfigPath(): string | null {
  return currentConfigPath
}

/**
 * 保存配置到文件
 * @param config 配置对象
 * @param filePath 可选的文件路径，不提供则保存到当前配置文件或默认位置
 */
export function saveConfig(config: AppConfig, filePath?: string): string {
  try {
    // 确定保存路径
    let targetPath = filePath || currentConfigPath
    
    if (!targetPath) {
      // 如果没有当前配置路径，使用用户数据目录
      const userDataPath = app.getPath('userData')
      targetPath = path.join(userDataPath, 'config.ini')
      console.log('[ConfigManager] No current config path, using default:', targetPath)
    }

    // 确保目录存在
    const dir = path.dirname(targetPath)
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true })
    }

    // 转换配置为 ini 格式
    const iniData = {
      CONFIG: {
        DATA_SOURCE: config.DATA_SOURCE || 'yfinance',
        FUTU_HOST: config.FUTU_HOST || '127.0.0.1',
        FUTU_PORT: config.FUTU_PORT || 11111,
        FUTU_WS_PORT: config.FUTU_WS_PORT || 33334,
        FUTU_WS_KEY: config.FUTU_WS_KEY || '',
        FUTU_GROUP: config.FUTU_GROUP || '',
        FUTU_CODE_LIST: config.FUTU_CODE_LIST || '',
        FUTU_PUSH_TYPE: config.FUTU_PUSH_TYPE || 'K_DAY',
        EMA_PERIOD: config.EMA_PERIOD || 240,
        KD_PARAMS_DB: config.KD_PARAMS_DB || '',
        MACD_PARAMS_DB: config.MACD_PARAMS_DB || '',
        RSI_PARAMS_DB: config.RSI_PARAMS_DB || '',
        PROXY: config.PROXY || '',
        DATA_DIR: config.DATA_DIR || './data/detect',
        DARK_MODE: config.DARK_MODE ? 'True' : 'False'
      }
    }

    // 写入文件
    const iniContent = stringify(iniData)
    fs.writeFileSync(targetPath, iniContent, 'utf-8')

    console.log('[ConfigManager] Config saved to:', targetPath)

    // 更新当前配置和路径
    currentConfig = config
    currentConfigPath = targetPath

    return targetPath
  } catch (error) {
    console.error('[ConfigManager] Failed to save config:', error)
    throw error
  }
}
