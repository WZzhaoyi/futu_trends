// 主进程数据服务层 - 处理所有数据源调用和计算
import YahooFinance from 'yahoo-finance2'
import { getFutuApi } from 'futu-sdk'
import { Qot_Common } from 'futu-proto'
import { getConfig } from './configManager'
import { calculateIndicators } from './indicators'
import type { Stock, KlineData, ChartData, IndicatorResult, StockListResult } from '../types'
import iconv from 'iconv-lite'
import axios from 'axios'

// 创建 Yahoo Finance 实例（v3 API）
// 可选配置：suppressNotices、validation 等
// 注意：通过 electron-vite 配置将 yahoo-finance2 打包，确保 ES 模块默认导出被正确处理
const yahooFinance = new YahooFinance({
  // 禁用通知提示
  suppressNotices: ['yahooSurvey'],
  // 开启验证（推荐，默认已开启）
  validation: {
    logErrors: true
  }
})

// 重新导出类型供主进程使用
export type { Stock, KlineData, ChartData, IndicatorResult, StockListResult }

/**
 * 辅助函数：处理 PowerShell 中文输出
 * 在 Windows PowerShell 中，需要将 UTF-8 字符串转换为 GBK 编码才能正确显示
 */
function logToConsole(message: string): void {
  if (process.platform === 'win32' && process.env.TERM_PROGRAM !== 'vscode') {
    // 在 Windows PowerShell 中，尝试使用 GBK 编码输出
    try {
      const gbkBuffer = iconv.encode(message, 'gbk')
      process.stdout.write(gbkBuffer)
      process.stdout.write('\n')
    } catch {
      // 如果转换失败，使用默认输出
      console.log(message)
    }
  } else {
    // 其他环境直接输出 UTF-8
    console.log(message)
  }
}

/**
 * 从富途获取股票列表
 */
export async function getFutuStockList(): Promise<StockListResult> {
  const config = getConfig()
  console.log('[DataService] Getting stock list with config:', {
    FUTU_GROUP: config.FUTU_GROUP || 'not set',
    FUTU_HOST: config.FUTU_HOST || 'not set',
    FUTU_WS_PORT: config.FUTU_WS_PORT || 'not set',
    FUTU_WS_KEY: config.FUTU_WS_KEY || 'not set',
    FUTU_CODE_LIST: config.FUTU_CODE_LIST ? `${config.FUTU_CODE_LIST.split(',').length} codes` : 'not set'
  })

  const stocks: Stock[] = []

  // 方法1: 从富途分组获取
  if (config?.FUTU_GROUP && config.FUTU_WS_KEY && config.FUTU_HOST && config.FUTU_WS_PORT) {
    console.log('[DataService] Attempting to load stocks from Futu group:', config.FUTU_GROUP)
    console.log('[DataService] WebSocket connection details:', {
      host: config.FUTU_HOST,
      port: config.FUTU_WS_PORT,
      hasKey: !!config.FUTU_WS_KEY,
      keyLength: config.FUTU_WS_KEY?.length || 0
    })
    
    try {
      const wsUrl = `ws://${config.FUTU_HOST}:${config.FUTU_WS_PORT}`
      console.log('[DataService] Connecting to WebSocket:', wsUrl)
      
      const { webRequest, webSocket } = getFutuApi(wsUrl, config.FUTU_WS_KEY)
      console.log('[DataService] WebSocket connection established')

      try {
        console.log('[DataService] Requesting GetUserSecurity for group:', config.FUTU_GROUP)
        const { staticInfoList } = await webRequest.GetUserSecurity({
          groupName: config.FUTU_GROUP
        })
        console.log('[DataService] GetUserSecurity response received, items count:', staticInfoList?.length || 0)

        if (staticInfoList && staticInfoList.length > 0) {
          const getFutuMarketName = (market: Qot_Common.QotMarket): 'SH' | 'SZ' | 'HK' | 'US' | '' => {
            const marketMap: Record<number, 'SH' | 'SZ' | 'HK' | 'US'> = {
              [Qot_Common.QotMarket.QotMarket_CNSH_Security]: 'SH',
              [Qot_Common.QotMarket.QotMarket_CNSZ_Security]: 'SZ',
              [Qot_Common.QotMarket.QotMarket_US_Security]: 'US',
              [Qot_Common.QotMarket.QotMarket_HK_Security]: 'HK'
            }
            return marketMap[market] || ''
          }

          for (const item of staticInfoList) {
            const basic = item.basic
            if (basic && basic.security) {
              const marketName = getFutuMarketName(basic.security.market)
              if (marketName) {
                stocks.push({
                  code: `${marketName}.${basic.security.code}`,
                  name: basic.name || basic.security.code || '',
                  market: marketName
                })
              }
            }
          }
          console.log(`[DataService] Successfully loaded ${stocks.length} stocks from Futu group`)
          return { stocks, source: 'futu_group' }
        }
      } finally {
        try {
          webSocket.close()
          console.log('[DataService] WebSocket closed')
        } catch (closeError) {
          console.warn('[DataService] Error closing WebSocket:', closeError)
        }
      }
    } catch (error) {
      const errorDetails = {
        message: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : undefined,
        name: error instanceof Error ? error.name : undefined,
        wsUrl: `ws://${config.FUTU_HOST}:${config.FUTU_WS_PORT}`,
        isPackaged: process.env.NODE_ENV === 'production' || !process.env.ELECTRON_RENDERER_URL
      }
      console.error('[DataService] Failed to get stocks from Futu group:', errorDetails)
      console.error('[DataService] Full error:', error)
    }
  }

  // 方法2: 从代码列表获取
  if (config?.FUTU_CODE_LIST && config.FUTU_CODE_LIST.length > 0) {
    console.log('[DataService] Loading stocks from FUTU_CODE_LIST')
    const codes = config.FUTU_CODE_LIST.split(',')
      .map((code) => code.trim())
      .filter((code) => code.length > 0)

    for (const code of codes) {
      // 解析代码格式如 "SH.600000"
      const parts = code.split('.')
      if (parts.length === 2) {
        const market = parts[0].toUpperCase() as 'SH' | 'SZ' | 'HK' | 'US'
        if (['SH', 'SZ', 'HK', 'US'].includes(market)) {
          stocks.push({
            code: code,
            name: code,
            market: market
          })
        } else {
          console.warn(`[DataService] Unsupported market in code: ${code}`)
        }
      } else {
        console.warn(`[DataService] Invalid code format: ${code}`)
      }
    }
    console.log(`[DataService] Loaded ${stocks.length} stocks from FUTU_CODE_LIST`)
    return { stocks, source: 'code_list' }
  }

  if (stocks.length === 0) {
    throw new Error('No stocks configured. Please set FUTU_GROUP or FUTU_CODE_LIST in config file.')
  }

  return { stocks, source: 'unknown' }
}

/**
 * 从Yahoo Finance获取K线数据
 * @param stock 股票对象
 * @param maxCount 最大K线数量
 */
export async function getYahooKlineData(stock: Stock, maxCount: number = 1200): Promise<KlineData[]> {
  try {
    // 转换为 Yahoo Finance 格式的 symbol
    const yahooSymbol = stockToYahooSymbol(stock)
    
    console.log('[DataService] Fetching Yahoo kline:', {
      stock: stock.code,
      market: stock.market,
      yahooSymbol,
      maxCount
    })

    const endDate = new Date()
    const startDate = new Date()
    startDate.setDate(endDate.getDate() - maxCount)

    const queryOptions = {
      period1: startDate,
      period2: endDate,
      interval: '1d' as const
    }

    // Yahoo Finance API: 使用 chart() 替代已废弃的 historical()
    // 参考: https://github.com/gadicc/yahoo-finance2/issues/795
    const result = await yahooFinance.chart(yahooSymbol, queryOptions)

    if (!result || !result.quotes || !Array.isArray(result.quotes) || result.quotes.length === 0) {
      throw new Error(`No data returned for ${stock.code}`)
    }

    const klines = result.quotes.map((item) => ({
      time: new Date(item.date).toISOString().split('T')[0],
      open: item.open ?? 0,
      high: item.high ?? 0,
      low: item.low ?? 0,
      close: item.close ?? 0,
      volume: item.volume ?? 0
    }))

    console.log(`[DataService] Successfully fetched ${klines.length} klines from Yahoo for ${stock.code}`)
    return klines
  } catch (error) {
    console.error(`[DataService] Failed to fetch kline data for ${stock.code} from Yahoo:`, error)
    throw error
  }
}

/**
 * 从 AkShare (通过 AkTools HTTP API) 获取K线数据
 * @param stock 股票对象
 * @param maxCount 最大K线数量
 */
export async function getAkShareKlineData(stock: Stock, maxCount: number = 1200): Promise<KlineData[]> {
  const config = getConfig()
  
  if (!config.AKTOOLS_HOST || !config.AKTOOLS_PORT) {
    console.warn('[DataService] AkTools connection parameters not configured')
    return []
  }

  try {
    // 解析股票代码
    const parts = stock.code.split('.')
    if (parts.length !== 2) {
      throw new Error(`Invalid stock code format: ${stock.code}`)
    }
    
    const [market, code] = parts
    
    // AkShare 只支持A股（SH和SZ市场）
    if (market !== 'SH' && market !== 'SZ') {
      console.warn(`[DataService] AkShare only supports A-share markets (SH/SZ), got: ${market}`)
      return []
    }

    // 计算日期范围
    const endDate = new Date()
    const startDate = new Date()
    // 多请求一些天数以确保获取足够的交易日数据
    const daysNeeded = Math.ceil((maxCount * 7) / 5 * 1.5)
    startDate.setDate(endDate.getDate() - daysNeeded)

    // 格式化日期为 YYYYMMDD
    const formatDate = (date: Date): string => {
      const year = date.getFullYear()
      const month = String(date.getMonth() + 1).padStart(2, '0')
      const day = String(date.getDate()).padStart(2, '0')
      return `${year}${month}${day}`
    }

    const startDateStr = formatDate(startDate)
    const endDateStr = formatDate(endDate)

    console.log('[DataService] Fetching AkShare kline:', {
      stock: stock.code,
      market,
      code,
      startDate: startDateStr,
      endDate: endDateStr,
      maxCount
    })

    // 构建 AkTools API URL
    // 使用 stock_zh_a_hist 接口获取A股历史数据
    const aktoolsUrl = `http://${config.AKTOOLS_HOST}:${config.AKTOOLS_PORT}/api/public/stock_zh_a_hist`
    const params = {
      symbol: code,  // 只传代码，不带市场前缀
      period: 'daily',  // 日K线
      start_date: startDateStr,
      end_date: endDateStr,
      adjust: 'qfq'  // 前复权
    }

    console.log('[DataService] Requesting AkTools API:', aktoolsUrl, params)

    // 发送HTTP请求
    const response = await axios.get(aktoolsUrl, {
      params,
      timeout: 30000  // 30秒超时
    })

    if (!response.data || !Array.isArray(response.data)) {
      console.warn('[DataService] Invalid response from AkTools:', response.data)
      return []
    }

    console.log('[DataService] Received data from AkTools:', {
      totalRecords: response.data.length,
      sample: response.data[0]
    })

    // 转换数据格式
    // AkShare 返回的数据格式：
    // { 日期: '2021-11-09', 开盘: 3014.70, 收盘: 3024.46, 最高: 3042.33, 最低: 2990.33, 成交量: 512595, 成交额: 895105984, ... }
    const klines: KlineData[] = response.data
      .filter((item: any) => {
        // 过滤掉无效数据
        return item && item['日期'] && item['收盘']
      })
      .map((item: any) => ({
        time: item['日期'],  // 日期格式: YYYY-MM-DD
        open: parseFloat(item['开盘'] || item['收盘']),
        high: parseFloat(item['最高'] || item['收盘']),
        low: parseFloat(item['最低'] || item['收盘']),
        close: parseFloat(item['收盘']),
        volume: parseInt(item['成交量'] || 0, 10)
      }))

    // 按时间排序并限制数量
    const sortedKlines = klines.sort((a, b) => a.time.localeCompare(b.time))
    const limitedKlines = sortedKlines.slice(-maxCount)

    console.log(`[DataService] Successfully fetched ${limitedKlines.length} klines from AkShare for ${stock.code}`)
    
    if (limitedKlines.length > 0) {
      console.log('[DataService] Sample kline data:', {
        first: limitedKlines[0],
        last: limitedKlines[limitedKlines.length - 1]
      })
    }

    return limitedKlines
  } catch (error) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    console.error(`[DataService] Failed to fetch kline data from AkShare for ${stock.code}:`, {
      error: errorMsg,
      stack: error instanceof Error ? error.stack : undefined,
      aktoolsUrl: `http://${config.AKTOOLS_HOST}:${config.AKTOOLS_PORT}`
    })
    return []
  }
}

/**
 * 市场相关的辅助函数
 */

/**
 * 将市场代码转换为富途市场枚举
 * @param marketStr 市场代码如 "SH", "SZ", "HK", "US"
 */
function marketToFutuMarket(marketStr: 'SH' | 'SZ' | 'HK' | 'US'): Qot_Common.QotMarket {
  const marketMap: Record<string, Qot_Common.QotMarket> = {
    'SH': Qot_Common.QotMarket.QotMarket_CNSH_Security,
    'SZ': Qot_Common.QotMarket.QotMarket_CNSZ_Security,
    'HK': Qot_Common.QotMarket.QotMarket_HK_Security,
    'US': Qot_Common.QotMarket.QotMarket_US_Security
  }
  return marketMap[marketStr]
}

function ktypeToFutuKLType(ktype: string): Qot_Common.KLType {
  const ktypeMap: Record<string, Qot_Common.KLType> = {
    'K_DAY': Qot_Common.KLType.KLType_Day,
    'K_WEEK': Qot_Common.KLType.KLType_Week,
    'K_MONTH': Qot_Common.KLType.KLType_Month
  }
  return ktypeMap[ktype] || Qot_Common.KLType.KLType_Day
}

/**
 * 将 Stock 转换为 Yahoo Finance 使用的 symbol
 * @param stock 股票对象
 * @returns Yahoo Finance symbol
 */
function stockToYahooSymbol(stock: Stock): string {
  const parts = stock.code.split('.')
  if (parts.length !== 2) {
    return stock.code
  }

  const [market, code] = parts

  // 转换映射
  const symbolMap: Record<string, (code: string) => string> = {
    'SH': (code) => `${code}.SS`,  // 上海 -> .SS
    'SZ': (code) => `${code}.SZ`,  // 深圳 -> .SZ
    'HK': (code) => code.padStart(5, '0'), // 香港 -> 补齐5位，如 00700
    'US': (code) => code  // 美股直接使用代码
  }

  const converter = symbolMap[market.toUpperCase()]
  return converter ? converter(code) : stock.code
}

/**
 * 获取市场的显示名称
 */
export function getMarketDisplayName(market: 'SH' | 'SZ' | 'HK' | 'US'): string {
  const displayNames: Record<string, string> = {
    'SH': '上海证券交易所',
    'SZ': '深圳证券交易所',
    'HK': '香港证券交易所',
    'US': '美国市场'
  }
  return displayNames[market] || market
}

/**
 * 判断是否是中国A股市场
 */
export function isChineseAShare(market: 'SH' | 'SZ' | 'HK' | 'US'): boolean {
  return market === 'SH' || market === 'SZ'
}

/**
 * 判断市场是否支持富途数据源
 */
export function supportsFutuDataSource(_market: 'SH' | 'SZ' | 'HK' | 'US'): boolean {
  // 所有市场都支持富途，但美股通常使用 Yahoo Finance 更方便
  return true
}

/**
 * 从富途OpenD获取K线数据
 * @param stock 股票对象
 * @param maxCount 最大K线数量
 */
export async function getFutuKlineData(stock: Stock, maxCount: number = 1200): Promise<KlineData[]> {
  const config = getConfig()
  
  if (!config.FUTU_WS_KEY || !config.FUTU_HOST || !config.FUTU_WS_PORT) {
    console.warn('[DataService] Futu connection parameters not configured')
    return []
  }

  try {
    // 解析股票代码：提取市场和代码部分
    const parts = stock.code.split('.')
    if (parts.length !== 2) {
      throw new Error(`Invalid stock code format: ${stock.code}`)
    }
    
    const [, code] = parts
    const market = marketToFutuMarket(stock.market)
    const ktype = config.FUTU_PUSH_TYPE || 'K_DAY'
    
    // 计算时间范围（多请求一些天数以确保获取足够的交易日数据）
    const endDate = new Date()
    const startDate = new Date()
    // 假设平均每周5个交易日，多请求50%的时间以确保数据充足
    const daysNeeded = Math.ceil((maxCount * 7) / 5 * 1.5)
    startDate.setDate(endDate.getDate() - daysNeeded)

    console.log('[DataService] Fetching Futu kline:', {
      stock: stock.code,
      market: stock.market,
      futuMarket: Qot_Common.QotMarket[market],
      code,
      startDate: startDate.toISOString().split('T')[0],
      endDate: endDate.toISOString().split('T')[0],
      maxCount
    })

    const wsUrl = `ws://${config.FUTU_HOST}:${config.FUTU_WS_PORT}`
    console.log('[DataService] Connecting to WebSocket for kline data:', wsUrl)
    
    let webRequest: any
    let webSocket: any
    
    try {
      const apiResult = getFutuApi(wsUrl, config.FUTU_WS_KEY)
      webRequest = apiResult.webRequest
      webSocket = apiResult.webSocket
      console.log('[DataService] WebSocket connection established for kline data')
    } catch (connectionError) {
      console.error('[DataService] Failed to establish WebSocket connection:', {
        error: connectionError instanceof Error ? connectionError.message : String(connectionError),
        wsUrl,
        isPackaged: process.env.NODE_ENV === 'production' || !process.env.ELECTRON_RENDERER_URL
      })
      throw connectionError
    }

    try {
      // 请求历史K线数据
      console.log('[DataService] Requesting RequestHistoryKL:', {
        security: { market: Qot_Common.QotMarket[market], code },
        klType: ktype,
        beginTime: startDate.toISOString().split('T')[0],
        endTime: endDate.toISOString().split('T')[0],
        maxAckKLNum: maxCount
      })
      const result = await webRequest.RequestHistoryKL({
        security: {
          market:marketToFutuMarket(stock.market),
          code:code
        },
        klType: ktypeToFutuKLType(ktype), // 默认日K
        rehabType: Qot_Common.RehabType.RehabType_Forward, // 前复权
        beginTime: startDate.toISOString().split('T')[0],
        endTime: endDate.toISOString().split('T')[0],
        maxAckKLNum: maxCount // 最多返回的K线数量
      })

      if (!result || !result.klList || result.klList.length === 0) {
        console.warn('[DataService] No kline data returned from Futu for:', stock.code)
        return []
      }

      // 调试：查看原始数据格式
      console.log('[DataService] Sample Futu kline data:', {
        firstItem: result.klList[0],
        totalCount: result.klList.length
      })

      // 转换为标准格式
      const klines: KlineData[] = result.klList
        .filter((kl: any) => {
          // 过滤掉空数据点
          if (kl.isBlank) {
            return false
          }
          // 过滤掉没有时间的数据
          if (!kl.time || kl.time.trim() === '') {
            console.warn('[DataService] Skipping kline with empty time:', kl)
            return false
          }
          return true
        })
        .map((kl: any) => {
          // 处理 volume：根据 Futu API 文档，可能是字符串、数字或 Long 类型
          let volume = 0
          if (typeof kl.volume === 'string') {
            // 字符串类型，如 "58625939"
            volume = parseInt(kl.volume, 10) || 0
          } else if (typeof kl.volume === 'number') {
            volume = kl.volume
          } else if (kl.volume && typeof kl.volume.toNumber === 'function') {
            // Long 类型
            volume = kl.volume.toNumber()
          }

          // 处理时间格式
          // Futu API 返回格式："2021-08-05 00:00:00"
          let time = kl.time.trim()
          
          // 判断是否为日K线级别（日K、周K、月K保留日期，其他保留完整时间）
          const isDayLevelKline = ['K_DAY', 'K_WEEK', 'K_MONTH'].includes(ktype)
          
          if (isDayLevelKline) {
            // 日K线级别：只保留日期部分 "YYYY-MM-DD"
            if (time.includes(' ')) {
              time = time.split(' ')[0]
            }
            if (time.includes('T')) {
              time = time.split('T')[0]
            }
          }
          // 分钟K线级别：保留完整时间 "YYYY-MM-DD HH:mm:ss"（parseTime 会处理）

          return {
            time,
            open: kl.openPrice || 0,
            high: kl.highPrice || 0,
            low: kl.lowPrice || 0,
            close: kl.closePrice || 0,
            volume
          }
        })

      // 验证数据格式
      if (klines.length > 0) {
        console.log('[DataService] Sample converted kline:', {
          first: klines[0],
          last: klines[klines.length - 1]
        })
      }

      // 按时间排序并限制数量
      const sortedKlines = klines.sort((a, b) => a.time.localeCompare(b.time))
      const limitedKlines = sortedKlines.slice(-maxCount)

      console.log(`[DataService] Successfully fetched ${limitedKlines.length} klines from Futu for ${stock.code}`)
      return limitedKlines
    } finally {
      try {
        if (webSocket) {
          webSocket.close()
          console.log('[DataService] WebSocket closed for kline data')
        }
      } catch (closeError) {
        console.warn('[DataService] Error closing WebSocket:', closeError)
      }
    }
  } catch (error) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    const errorDetails = {
      message: errorMsg,
      stack: error instanceof Error ? error.stack : undefined,
      stock: stock.code,
      wsUrl: `ws://${config.FUTU_HOST}:${config.FUTU_WS_PORT}`,
      isPackaged: process.env.NODE_ENV === 'production' || !process.env.ELECTRON_RENDERER_URL,
      configLoaded: !!config.FUTU_WS_KEY && !!config.FUTU_HOST && !!config.FUTU_WS_PORT
    }
    logToConsole(`[DataService] 获取富途K线数据失败 ${stock.code}: ${errorMsg}`)
    console.error(`[DataService] Failed to fetch kline data from Futu for ${stock.code}:`, errorDetails)
    console.error(`[DataService] Full error:`, error)
    return []
  }
}

/**
 * 获取完整的图表数据（K线 + 指标）
 */
export async function getChartData(stockCode: string, maxCount: number=1200): Promise<ChartData> {
  console.log('[DataService] Getting chart data for:', stockCode)

  // 1. 获取股票信息
  const { stocks } = await getFutuStockList()
  const stock = stocks.find((s) => s.code === stockCode)

  if (!stock) {
    throw new Error(`Stock not found: ${stockCode}`)
  }

  // 2. 确定K线数量
  const config = getConfig()
  const klineCount = maxCount || 1200
  const dataSource = config.DATA_SOURCE || 'futu'

  // 3. 获取K线数据（传递完整的 Stock 对象）
  let klines: KlineData[] = []

  // 根据数据源区分
  if (dataSource === 'futu') {
    klines = await getFutuKlineData(stock, klineCount)
  } else if (dataSource === 'yfinance') {
    klines = await getYahooKlineData(stock, klineCount)
  } else if (dataSource === 'akshare') {
    klines = await getAkShareKlineData(stock, klineCount)
  } else {
    throw new Error(`Unsupported data source: ${dataSource}`)
  }

  if (klines.length === 0) {
    throw new Error(`No kline data available for ${stockCode}`)
  }

  // 4. 计算指标（传递股票代码以从数据库读取参数）
  console.log('[DataService] Calculating indicators with params from database...')
  const indicators = await calculateIndicators(klines, stock.code)

  console.log('[DataService] Chart data ready:', {
    stock: stock.code,
    klineCount: klines.length,
    hasIndicators: {
      ema: !!indicators.ema,
      macd: !!indicators.macd,
      kd: !!indicators.k,
      rsi: !!indicators.rsi
    }
  })

  return {
    stock,
    klines,
    indicators
  }
}

