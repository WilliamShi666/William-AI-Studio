/**
 * API 配置 - 支持 v1.0 和 v2.0 版本
 * 使用环境变量动态配置后端服务地址
 */

import { config } from '../config';

export interface ExtractionMethod {
  id: string;
  label: string;
  description: string;
}

export interface APIVersion {
  extraction: {
    endpoint: string;
    uploadEndpoint: string;
    methods: ExtractionMethod[];
  };
  chunking: {
    endpoint: string;
    methods: string[];
  };
  retrieval: {
    endpoint: string;
  };
  chat: {
    endpoint: string;
  };
}

export const API_CONFIG: Record<'v1' | 'v2', APIVersion> = {
  // ============ v1.0 配置 ============
  v1: {
    extraction: {
      endpoint: `${config.extractionApiUrl}/extract`,
      uploadEndpoint: `${config.extractionApiUrl}/api/v1/files/upload`,
      methods: [
        {
          id: 'fast',
          label: '快速模式 (PyMuPDF4LLM)',
          description: '适合简单文档，速度快'
        },
        {
          id: 'vlm',
          label: '精确模式 (VLM)',
          description: '支持复杂布局，使用视觉语言模型'
        }
      ]
    },
    chunking: {
      endpoint: `${config.chunkApiUrl}/chunk`,
      methods: ['header_recursive', 'markdown_only']
    },
    retrieval: {
      endpoint: config.milvusApiUrl
    },
    chat: {
      endpoint: config.chatApiUrl
    }
  },

  // ============ v2.0 配置 (OCR 增强版) ============
  v2: {
    extraction: {
      endpoint: `${config.extractionApiUrl}/extract/v2`,
      uploadEndpoint: `${config.extractionApiUrl}/api/v2/files/upload`,
      methods: [
        {
          id: 'mineru',
          label: 'MinerU',
          description: '高质量 PDF 解析，支持复杂版面结构'
        },
        {
          id: 'deepseek',
          label: 'DeepSeek-OCR',
          description: 'AI驱动的OCR，支持复杂场景和多语言'
        },
        {
          id: 'paddleocr_vl',
          label: 'PaddleOCR-VL',
          description: '视觉语言模型OCR，高精度文字识别'
        }
      ]
    },
    chunking: {
      endpoint: `${config.chunkApiUrl}/chunk/v2`,
      methods: ['ocr_aware', 'layout_based']
    },
    retrieval: {
      endpoint: `${config.milvusApiUrl}/v2`
    },
    chat: {
      endpoint: `${config.chatApiUrl}/v2`
    }
  }
};

/**
 * 根据版本获取 API 配置
 */
export const getAPIConfig = (isV2: boolean): APIVersion => {
  return isV2 ? API_CONFIG.v2 : API_CONFIG.v1;
};

/**
 * 获取提取方法配置
 */
export const getExtractionMethods = (isV2: boolean): ExtractionMethod[] => {
  return getAPIConfig(isV2).extraction.methods;
};

/**
 * 获取上传端点
 */
export const getUploadEndpoint = (isV2: boolean): string => {
  return getAPIConfig(isV2).extraction.uploadEndpoint;
};

/**
 * 获取默认提取方法
 */
export const getDefaultExtractionMethod = (isV2: boolean): string => {
  return isV2 ? 'mineru' : 'fast';
};

