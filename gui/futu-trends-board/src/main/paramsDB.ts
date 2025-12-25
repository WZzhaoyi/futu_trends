// 参数数据库管理类 - 支持 SQLite 和 MongoDB
import initSqlJs, { Database as SqlJsDatabase } from 'sql.js'
import { MongoClient, Db, Collection } from 'mongodb'
import * as fs from 'fs'
import * as path from 'path'

/**
 * 股票参数数据类型
 */
export interface StockParams {
  stock_code: string
  best_params: Record<string, any>
  meta_info?: Record<string, any>
  performance?: Record<string, any>
  last_updated?: string
  source_file?: string
}

/**
 * 指标参数类型
 */
export interface IndicatorParams {
  // MACD参数
  fast_period?: number
  slow_period?: number
  signal_period?: number
  
  // KD参数
  k_period?: number
  d_period?: number
  oversold?: number
  overbought?: number
  
  // RSI参数
  rsi_period?: number
}

/**
 * 参数数据库管理类
 * 支持 SQLite (sql.js) 和 MongoDB 两种数据库
 */
export class ParamsDB {
  private dbUri: string
  private dbType: 'sqlite' | 'mongodb'
  private sqliteDb?: SqlJsDatabase
  private mongoClient?: MongoClient
  private mongoDb?: Db
  private paramsCollection?: Collection
  private sqlInitialized: boolean = false

  constructor(dbUri: string) {
    this.dbUri = dbUri

    if (dbUri.startsWith('sqlite:///')) {
      this.dbType = 'sqlite'
      const dbPath = dbUri.replace('sqlite:///', '')
      
      // 检查文件是否存在
      if (!fs.existsSync(dbPath)) {
        throw new Error(`SQLite database not found: ${dbPath}`)
      }
      
      // sql.js 需要异步初始化，在第一次使用时初始化
    } else if (dbUri.startsWith('mongodb://') || dbUri.startsWith('mongodb+srv://')) {
      this.dbType = 'mongodb'
      // MongoDB 连接将在第一次使用时初始化（延迟连接）
    } else {
      throw new Error(`Unsupported database URI format: ${dbUri}`)
    }
  }

  /**
   * 初始化 SQLite 数据库（sql.js）
   */
  private async initSqlite(): Promise<void> {
    if (this.sqlInitialized && this.sqliteDb) {
      return // 已经初始化
    }

    const dbPath = this.dbUri.replace('sqlite:///', '')
    
    // 初始化 sql.js
    const SQL = await initSqlJs({
      // 指定 wasm 文件路径
      locateFile: (file) => {
        // 开发环境：从 node_modules 读取
        // 生产环境：从 out/main 读取（由构建脚本复制）
        const devPath = path.join(__dirname, '../../node_modules/sql.js/dist', file)
        const prodPath = path.join(__dirname, file)
        
        if (fs.existsSync(prodPath)) {
          console.log(`[ParamsDB] Using wasm from: ${prodPath}`)
          return prodPath
        } else if (fs.existsSync(devPath)) {
          console.log(`[ParamsDB] Using wasm from: ${devPath}`)
          return devPath
        } else {
          console.warn(`[ParamsDB] WASM file not found, using default: ${file}`)
          return file
        }
      }
    })

    // 读取数据库文件
    const buffer = fs.readFileSync(dbPath)
    this.sqliteDb = new SQL.Database(buffer)
    this.sqlInitialized = true
    
    console.log('[ParamsDB] SQLite database initialized with sql.js')
  }

  /**
   * 初始化 MongoDB 连接
   */
  private async initMongo(): Promise<void> {
    if (this.mongoDb) {
      return // 已经初始化
    }

    const options = {
      serverSelectionTimeoutMS: 30000,
      connectTimeoutMS: 20000,
      socketTimeoutMS: 30000,
      retryWrites: true,
      retryReads: true,
    }

    this.mongoClient = new MongoClient(this.dbUri, options)
    await this.mongoClient.connect()

    // 从 URI 中提取数据库名称
    const dbName = this.dbUri.split('/').pop()?.split('?')[0] || 'futu_trends'
    this.mongoDb = this.mongoClient.db(dbName)
    this.paramsCollection = this.mongoDb.collection('strategy_params')
  }

  /**
   * 获取股票参数
   * @param stockCode - 股票代码
   * @returns 股票参数，如果未找到返回 null
   */
  async getStockParams(stockCode: string): Promise<StockParams | null> {
    try {
      if (this.dbType === 'sqlite') {
        await this.initSqlite()
        return this.getStockParamsFromSQLite(stockCode)
      } else {
        await this.initMongo()
        return await this.getStockParamsFromMongo(stockCode)
      }
    } catch (error) {
      console.error(`[ParamsDB] Error getting params for ${stockCode}:`, error)
      return null
    }
  }

  /**
   * 从 SQLite 获取参数（使用 sql.js）
   */
  private getStockParamsFromSQLite(stockCode: string): StockParams | null {
    if (!this.sqliteDb) {
      return null
    }

    try {
      const stmt = this.sqliteDb.prepare(
        'SELECT best_params, meta_info, performance, last_updated, source_file FROM stock_params WHERE stock_code = ?'
      )
      stmt.bind([stockCode])
      
      if (stmt.step()) {
        const row = stmt.getAsObject()
        stmt.free()

        return {
          stock_code: stockCode,
          best_params: JSON.parse(row.best_params as string),
          meta_info: row.meta_info ? JSON.parse(row.meta_info as string) : undefined,
          performance: row.performance ? JSON.parse(row.performance as string) : undefined,
          last_updated: row.last_updated as string,
          source_file: row.source_file as string
        }
      }
      
      stmt.free()
      return null
    } catch (error) {
      console.error(`[ParamsDB] SQLite error for ${stockCode}:`, error)
      return null
    }
  }

  /**
   * 从 MongoDB 获取参数
   */
  private async getStockParamsFromMongo(stockCode: string): Promise<StockParams | null> {
    if (!this.paramsCollection) {
      return null
    }

    try {
      const doc = await this.paramsCollection.findOne({ stock_code: stockCode })

      if (!doc) {
        return null
      }

      return {
        stock_code: stockCode,
        best_params: doc.best_params,
        meta_info: doc.meta_info,
        performance: doc.performance,
        last_updated: doc.last_updated?.toISOString(),
        source_file: doc.source_file
      }
    } catch (error) {
      console.error(`[ParamsDB] MongoDB error for ${stockCode}:`, error)
      return null
    }
  }

  /**
   * 关闭数据库连接
   */
  async close(): Promise<void> {
    if (this.sqliteDb) {
      this.sqliteDb.close()
      this.sqliteDb = undefined
      this.sqlInitialized = false
    }
    if (this.mongoClient) {
      await this.mongoClient.close()
    }
  }
}

/**
 * 创建参数数据库实例（带缓存）
 */
const dbCache = new Map<string, ParamsDB>()

export function getParamsDB(dbUri: string): ParamsDB {
  if (!dbCache.has(dbUri)) {
    dbCache.set(dbUri, new ParamsDB(dbUri))
  }
  return dbCache.get(dbUri)!
}

/**
 * 清理所有数据库连接
 */
export async function closeAllParamsDB(): Promise<void> {
  for (const db of dbCache.values()) {
    await db.close()
  }
  dbCache.clear()
}

