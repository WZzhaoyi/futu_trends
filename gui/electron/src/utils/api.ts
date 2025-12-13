/**
 * API 工具函数
 */
import axios, { AxiosError } from 'axios';
import { API_BASE, RETRY_CONFIG } from '../config';

/**
 * 检查后端服务是否就绪
 * @param maxRetries - 最大重试次数
 * @param retryDelay - 重试延迟（毫秒）
 * @returns 服务是否就绪
 */
export async function checkServiceReady(
  maxRetries: number = RETRY_CONFIG.SERVICE_CHECK_RETRIES,
  retryDelay: number = RETRY_CONFIG.SERVICE_CHECK_DELAY
): Promise<boolean> {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await axios.get(`${API_BASE}/`, { 
        timeout: 1000 
      });
      if (response.data?.status === 'ok') {
        return true;
      }
    } catch (error) {
      // 服务未就绪，继续重试
    }
    
    if (i < maxRetries - 1) {
      await new Promise(resolve => setTimeout(resolve, retryDelay));
    }
  }
  
  return false;
}

/**
 * 带重试的 API 请求
 * @param requestFn - 请求函数
 * @param maxRetries - 最大重试次数
 * @param retryDelay - 重试延迟（毫秒）
 * @returns 请求结果
 */
export async function requestWithRetry<T>(
  requestFn: () => Promise<T>,
  maxRetries: number = RETRY_CONFIG.MAX_RETRIES,
  retryDelay: number = RETRY_CONFIG.RETRY_DELAY
): Promise<T> {
  let lastError: any = null;
  
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await requestFn();
    } catch (error) {
      lastError = error;
      if (i < maxRetries - 1) {
        await new Promise(resolve => setTimeout(resolve, retryDelay));
      }
    }
  }
  
  throw lastError;
}

/**
 * 获取友好的错误消息
 * @param error - 错误对象
 * @returns 错误消息
 */
export function getErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const axiosError = error as AxiosError;
    
    if (axiosError.response?.status === 404) {
      return 'Resource not found';
    }
    
    if (axiosError.code === 'ECONNREFUSED' || axiosError.code === 'ERR_NETWORK') {
      return 'Unable to connect to backend service, please check if the service is running';
    }
    
    if (axiosError.response?.status) {
      return `Request failed (${axiosError.response.status})`;
    }
  }
  
  if (error instanceof Error) {
    return error.message;
  }
  
  return 'Unknown error, please try again later';
}

