/**
 * 验证工具函数
 */

/**
 * 验证股票代码是否有效
 * @param code - 股票代码
 * @returns 是否为有效代码
 */
export function isValidCode(code: string | null | undefined): boolean {
  return !!code && code !== 'undefined' && code.trim() !== '';
}

