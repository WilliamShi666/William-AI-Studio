// 简化的错误处理模块
// 移除Supabase依赖，提供基本的错误处理功能

export interface DatabaseError {
  message: string;
  code?: string;
  details?: any;
}

export interface ApiError extends Error {
  status?: number;
  code?: string;
  details?: any;
}

// 模拟错误类型常量
export const ERROR_CODES = {
  PGRST116: 'PGRST116', // 原Supabase的"no rows returned"错误
  NETWORK_ERROR: 'NETWORK_ERROR',
  UNAUTHORIZED: 'UNAUTHORIZED',
  FORBIDDEN: 'FORBIDDEN',
  NOT_FOUND: 'NOT_FOUND'
} as const;

// 处理数据库错误
export function handleDatabaseError(error: any): DatabaseError {
  if (!error) {
    return { message: 'Unknown database error' };
  }

  // 统一错误格式
  return {
    message: error.message || 'Database operation failed',
    code: error.code || 'UNKNOWN_ERROR',
    details: error.details || error
  };
}

// 处理API错误
export function handleApiError(error: any): ApiError {
  if (!error) {
    const apiError = new Error('Unknown API error') as ApiError;
    apiError.status = 500;
    return apiError;
  }

  const apiError = new Error(error.message || 'API request failed') as ApiError;
  apiError.status = error.status || error.statusCode || 500;
  apiError.code = error.code || 'UNKNOWN_ERROR';
  apiError.details = error.details || error;

  return apiError;
}

// 检查是否是特定类型的错误
export function isNoRowsError(error: any): boolean {
  return error?.code === ERROR_CODES.PGRST116 || 
         error?.message?.includes('no rows returned') ||
         error?.message?.includes('No rows found');
}

export function isNetworkError(error: any): boolean {
  return error?.code === ERROR_CODES.NETWORK_ERROR ||
         error?.message?.includes('Failed to fetch') ||
         error?.message?.includes('Network error');
}

export function isUnauthorizedError(error: any): boolean {
  return error?.status === 401 || 
         error?.code === ERROR_CODES.UNAUTHORIZED ||
         error?.message?.includes('Unauthorized');
}

// 格式化错误消息用于显示
export function formatErrorMessage(error: any): string {
  if (typeof error === 'string') {
    return error;
  }

  if (error?.message) {
    return error.message;
  }

  return 'An unexpected error occurred';
}

// 日志错误
export function logError(error: any, context?: string) {
  const formattedError = handleApiError(error);
  console.error(
    `[${context || 'ERROR'}]`,
    formattedError.message,
    formattedError.details
  );
} 