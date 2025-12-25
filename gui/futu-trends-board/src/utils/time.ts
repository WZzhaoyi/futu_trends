// 时间处理工具函数

/**
 * 将日期字符串解析为 lightweight-charts 需要的时间格式
 * 
 * 自动检测时间格式：
 * - 如果只有日期（YYYY-MM-DD）-> BusinessDay 格式（用于日K线）
 * - 如果包含时分秒（YYYY-MM-DD HH:mm:ss）-> UTCTimestamp（用于分钟K线）
 * 
 * @param timeStr - 时间字符串、时间戳或Date对象
 * @param useTimestamp - 强制使用时间戳模式，默认 false（自动检测）
 * @returns BusinessDay 格式或 UTCTimestamp
 */
export function parseTime(
  timeStr: string | number | Date,
  useTimestamp?: boolean
): { year: number; month: number; day: number } | number {
  // 日期字符串处理
  if (typeof timeStr === 'string' && timeStr.includes('-')) {
    // 自动检测：是否包含时分秒
    const hasTime = (timeStr.includes(' ') || (timeStr.includes('T') && timeStr.length > 10))
    const shouldUseTimestamp = useTimestamp !== undefined ? useTimestamp : hasTime
    
    if (shouldUseTimestamp && hasTime) {
      // 分钟K线模式：返回 UTC 时间戳（秒）
      const isoTime = timeStr.replace(' ', 'T')
      const date = new Date(isoTime.includes('Z') ? isoTime : isoTime + 'Z')
      return Math.floor(date.getTime() / 1000) as number
    } else {
      // 日K线模式：返回 BusinessDay 格式（只有日期）
      const dateOnly = timeStr.split(/[ T]/)[0]
      const date = new Date(dateOnly + 'T00:00:00Z')
      return {
        year: date.getUTCFullYear(),
        month: date.getUTCMonth() + 1,
        day: date.getUTCDate(),
      }
    }
  }

  // 时间戳处理
  if (typeof timeStr === 'number') {
    const shouldUseTimestamp = useTimestamp !== undefined ? useTimestamp : false
    if (shouldUseTimestamp) {
      // 分钟K线模式：直接返回时间戳
      return timeStr as number
    } else {
      // 日K线模式：转换为 BusinessDay
      const date = new Date(timeStr * 1000)
      return {
        year: date.getUTCFullYear(),
        month: date.getUTCMonth() + 1,
        day: date.getUTCDate(),
      }
    }
  }

  // Date对象处理
  if (timeStr instanceof Date) {
    const shouldUseTimestamp = useTimestamp !== undefined ? useTimestamp : false
    if (shouldUseTimestamp) {
      // 分钟K线模式：返回时间戳
      return Math.floor(timeStr.getTime() / 1000) as number
    } else {
      // 日K线模式：返回 BusinessDay
      return {
        year: timeStr.getUTCFullYear(),
        month: timeStr.getUTCMonth() + 1,
        day: timeStr.getUTCDate(),
      }
    }
  }

  throw new Error(`Unsupported time format: ${String(timeStr)}`)
}

interface TimeObject {
  year: number
  month: number
  day: number
}

/**
 * 格式化时间显示
 */
export function formatTime(time: TimeObject | string | number): string {
  if (typeof time === 'object' && time !== null && 'year' in time && 'month' in time && 'day' in time) {
    const timeObj = time as TimeObject;
    return `${timeObj.year}-${String(timeObj.month).padStart(2, '0')}-${String(timeObj.day).padStart(2, '0')}`;
  }
  return String(time);
}
