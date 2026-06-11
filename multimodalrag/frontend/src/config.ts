/**
 * 前端环境配置
 * 从环境变量读取API地址，如果没有则使用默认值
 */

const resolveUrl = (port: string, envValue?: string) => {
  const explicit = (envValue || '').trim();
  if (explicit) {
    return explicit;
  }

  if (typeof window !== 'undefined' && window.location?.hostname) {
    return `${window.location.protocol}//${window.location.hostname}:${port}`;
  }

  return `http://localhost:${port}`;
};

const resolveApiBase = (envValue?: string) => {
  const explicit = (envValue || '').trim();
  if (explicit) {
    return explicit;
  }

  if (typeof window !== 'undefined' && window.location?.origin) {
    return `${window.location.origin}/api`;
  }

  return 'http://localhost:8002/api';
};

export const config = {
  // Milvus API 服务地址
  milvusApiUrl: resolveUrl('8000', import.meta.env.VITE_MILVUS_API_URL),

  // Chat 对话服务地址
  chatApiUrl: resolveUrl('8501', import.meta.env.VITE_CHAT_API_URL),

  // Multi-agent debate 服务地址
  debateApiUrl: resolveUrl('8602', import.meta.env.VITE_DEBATE_API_URL),

  // PDF 提取服务地址
  extractionApiUrl: resolveUrl('8006', import.meta.env.VITE_EXTRACTION_API_URL),

  // 切分服务地址
  chunkApiUrl: resolveUrl('8001', import.meta.env.VITE_CHUNK_API_URL),

  // 主站后端 API（管理员面板等）
  mainApiUrl: resolveApiBase(import.meta.env.VITE_MAIN_API_URL),
} as const;

// 导出便捷方法
export const getApiUrl = (service: keyof typeof config): string => {
  return config[service];
};
