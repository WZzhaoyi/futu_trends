// 主进程日志管理器
import { app } from 'electron'
import * as fs from 'fs'
import * as path from 'path'

class Logger {
  private logDir: string
  private logFile: string
  private logStream: fs.WriteStream | null = null

  constructor() {
    // 在用户数据目录创建日志文件夹
    this.logDir = path.join(app.getPath('userData'), 'logs')
    
    // 确保日志目录存在
    if (!fs.existsSync(this.logDir)) {
      fs.mkdirSync(this.logDir, { recursive: true })
    }

    // 日志文件名：futu-trends-YYYY-MM-DD.log
    const today = new Date().toISOString().split('T')[0]
    this.logFile = path.join(this.logDir, `futu-trends-${today}.log`)
    
    // 创建写入流
    this.logStream = fs.createWriteStream(this.logFile, { flags: 'a' })
    
    // 初始化日志记录
    this.log('info', '='.repeat(80))
    this.log('info', `应用启动 - ${new Date().toLocaleString('zh-CN')}`)
    this.log('info', `应用版本: ${app.getVersion()}`)
    this.log('info', `Electron版本: ${process.versions.electron}`)
    this.log('info', `Node版本: ${process.versions.node}`)
    this.log('info', `运行模式: ${app.isPackaged ? '生产环境(打包)' : '开发环境'}`)
    this.log('info', `用户数据目录: ${app.getPath('userData')}`)
    this.log('info', `日志文件: ${this.logFile}`)
    this.log('info', '='.repeat(80))
  }

  private formatMessage(level: string, message: string, ...args: any[]): string {
    const timestamp = new Date().toISOString()
    const formattedArgs = args.map(arg => {
      if (typeof arg === 'object') {
        try {
          return JSON.stringify(arg, null, 2)
        } catch {
          return String(arg)
        }
      }
      return String(arg)
    }).join(' ')
    
    const fullMessage = formattedArgs ? `${message} ${formattedArgs}` : message
    return `[${timestamp}] [${level.toUpperCase()}] ${fullMessage}`
  }

  private writeToFile(message: string): void {
    if (this.logStream && !this.logStream.destroyed) {
      this.logStream.write(message + '\n')
    }
  }

  public log(level: 'info' | 'warn' | 'error' | 'debug', message: string, ...args: any[]): void {
    const formatted = this.formatMessage(level, message, ...args)
    
    // 写入文件
    this.writeToFile(formatted)
    
    // 同时输出到控制台（开发环境）
    if (!app.isPackaged) {
      switch (level) {
        case 'error':
          console.error(formatted)
          break
        case 'warn':
          console.warn(formatted)
          break
        case 'debug':
          console.debug(formatted)
          break
        default:
          console.log(formatted)
      }
    }
  }

  public info(message: string, ...args: any[]): void {
    this.log('info', message, ...args)
  }

  public warn(message: string, ...args: any[]): void {
    this.log('warn', message, ...args)
  }

  public error(message: string, ...args: any[]): void {
    this.log('error', message, ...args)
  }

  public debug(message: string, ...args: any[]): void {
    this.log('debug', message, ...args)
  }

  public getLogPath(): string {
    return this.logFile
  }

  public getLogDir(): string {
    return this.logDir
  }

  public close(): void {
    if (this.logStream && !this.logStream.destroyed) {
      this.log('info', '应用关闭')
      this.logStream.end()
      this.logStream = null
    }
  }

  // 清理旧日志文件（保留最近 7 天）
  public cleanOldLogs(daysToKeep: number = 7): void {
    try {
      const files = fs.readdirSync(this.logDir)
      const now = Date.now()
      const maxAge = daysToKeep * 24 * 60 * 60 * 1000

      files.forEach(file => {
        if (file.startsWith('futu-trends-') && file.endsWith('.log')) {
          const filePath = path.join(this.logDir, file)
          const stats = fs.statSync(filePath)
          const age = now - stats.mtime.getTime()

          if (age > maxAge) {
            fs.unlinkSync(filePath)
            this.info(`清理旧日志文件: ${file}`)
          }
        }
      })
    } catch (error) {
      this.error('清理旧日志文件失败:', error)
    }
  }
}

// 单例模式
let loggerInstance: Logger | null = null

export function initLogger(): Logger {
  if (!loggerInstance) {
    loggerInstance = new Logger()
    // 清理旧日志
    loggerInstance.cleanOldLogs(7)
  }
  return loggerInstance
}

export function getLogger(): Logger {
  if (!loggerInstance) {
    throw new Error('Logger not initialized. Call initLogger() first.')
  }
  return loggerInstance
}

export function closeLogger(): void {
  if (loggerInstance) {
    loggerInstance.close()
    loggerInstance = null
  }
}

// 辅助函数：将参数转换为可读字符串
function formatArg(arg: any): string {
  if (typeof arg === 'string') {
    return arg
  }
  if (typeof arg === 'object' && arg !== null) {
    try {
      // 对于 Error 对象，显示完整堆栈
      if (arg instanceof Error) {
        return `${arg.name}: ${arg.message}\n${arg.stack || ''}`
      }
      // 对于普通对象，格式化 JSON
      return JSON.stringify(arg, null, 2)
    } catch {
      return String(arg)
    }
  }
  return String(arg)
}

// 拦截全局 console 方法（仅在打包环境）
export function interceptConsole(logger: Logger): void {
  if (app.isPackaged) {
    const originalLog = console.log
    const originalWarn = console.warn
    const originalError = console.error
    const originalDebug = console.debug

    console.log = (...args: any[]) => {
      logger.info(args.map(formatArg).join(' '))
      originalLog.apply(console, args)
    }

    console.warn = (...args: any[]) => {
      logger.warn(args.map(formatArg).join(' '))
      originalWarn.apply(console, args)
    }

    console.error = (...args: any[]) => {
      logger.error(args.map(formatArg).join(' '))
      originalError.apply(console, args)
    }

    console.debug = (...args: any[]) => {
      logger.debug(args.map(formatArg).join(' '))
      originalDebug.apply(console, args)
    }

    logger.info('Console 输出已拦截并重定向到日志文件')
  }
}

