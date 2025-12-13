/**
 * 时间处理工具函数
 */
import { Time } from 'lightweight-charts';

/**
 * 将时间字符串转换为 TradingView Lightweight Charts 格式
 * @param timeStr - 时间字符串（ISO 格式或时间戳）
 * @returns TradingView Time 格式
 */
export function parseTime(timeStr: string): Time {
  // 尝试解析为 ISO 格式
  const date = new Date(timeStr);
  if (!isNaN(date.getTime())) {
    // 返回时间戳（秒）
    return Math.floor(date.getTime() / 1000) as Time;
  }
  
  // 尝试作为时间戳（秒）
  const timestamp = parseInt(timeStr, 10);
  if (!isNaN(timestamp)) {
    return timestamp as Time;
  }
  
  // 如果都失败，返回原始字符串（让库处理）
  return timeStr as Time;
}

