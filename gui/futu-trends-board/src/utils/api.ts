// API错误处理和重试机制工具
export interface RetryOptions {
  maxRetries?: number;
  delay?: number;
  backoff?: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public statusCode?: number,
    public originalError?: any
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * 默认重试选项
 */
const DEFAULT_RETRY_OPTIONS: Required<RetryOptions> = {
  maxRetries: 3,
  delay: 1000,
  backoff: 2
};

/**
 * 延迟函数
 */
const delay = (ms: number): Promise<void> => {
  return new Promise(resolve => setTimeout(resolve, ms));
};

/**
 * 带重试机制的异步函数执行器
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  options: RetryOptions = {}
): Promise<T> {
  const opts = { ...DEFAULT_RETRY_OPTIONS, ...options };
  let lastError: any;

  for (let attempt = 0; attempt <= opts.maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;

      // 如果是最后一次尝试，直接抛出错误
      if (attempt === opts.maxRetries) {
        break;
      }

      // 检查是否是可重试的错误
      if (!isRetryableError(error)) {
        break;
      }

      // 计算延迟时间
      const delayTime = opts.delay * Math.pow(opts.backoff, attempt);
      console.warn(`[withRetry] Attempt ${attempt + 1} failed, retrying in ${delayTime}ms:`, error);

      await delay(delayTime);
    }
  }

  throw lastError;
}

/**
 * 判断错误是否可重试
 */
function isRetryableError(error: any): boolean {
  // 网络错误
  if (error.code === 'NETWORK_ERROR' || error.code === 'TIMEOUT') {
    return true;
  }

  // HTTP状态码
  if (error.statusCode) {
    // 5xx 服务器错误
    if (error.statusCode >= 500) {
      return true;
    }
    // 429 请求过于频繁
    if (error.statusCode === 429) {
      return true;
    }
  }

  // 富途API错误
  if (error.retType && error.retType !== 0) {
    // 某些富途错误码可以重试
    return true;
  }

  // Yahoo Finance 限流错误
  if (error.message && error.message.includes('rate limit')) {
    return true;
  }

  return false;
}

/**
 * 获取错误消息
 */
export function getErrorMessage(error: any): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  if (error.response) {
    // HTTP响应错误
    const status = error.response.status;
    const statusText = error.response.statusText;
    return `HTTP ${status}: ${statusText}`;
  }

  if (error.message) {
    return error.message;
  }

  if (typeof error === 'string') {
    return error;
  }

  return 'Unknown error occurred';
}

/**
 * 创建API错误
 */
export function createApiError(message: string, statusCode?: number, originalError?: any): ApiError {
  return new ApiError(message, statusCode, originalError);
}

/**
 * 检查服务是否就绪（用于兼容旧接口）
 */
export async function checkServiceReady(): Promise<boolean> {
  // 在纯JS版本中，总是返回true
  return true;
}

/**
 * 兼容旧版本的requestWithRetry函数
 */
export async function requestWithRetry<T>(fn: () => Promise<T>): Promise<T> {
  return withRetry(fn, { maxRetries: 2, delay: 500 });
}
