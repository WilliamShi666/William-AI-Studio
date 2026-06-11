'use client';

import { useSubscription } from '@/hooks/react-query/subscriptions/use-subscriptions';
import { useState, useEffect, useMemo } from 'react';
import { isLocalMode } from '@/lib/config';
import { useAvailableModels } from '@/hooks/react-query/subscriptions/use-model';

export const STORAGE_KEY_MODEL = 'suna-preferred-model-v3';
export const STORAGE_KEY_CUSTOM_MODELS = 'customModels';
// 默认模型设为 DeepSeek 官方 V4 Pro High，避免前端默认落到 OpenRouter 或本地 ollama。
export const DEFAULT_PREMIUM_MODEL_ID = 'deepseek-v4-pro-high';
export const DEFAULT_FREE_MODEL_ID = 'deepseek-v4-pro-high';
export const DEFAULT_LOCAL_CHAT_MODEL_ID = 'deepseek-v4-pro-high';

export type SubscriptionStatus = 'no_subscription' | 'active';

export interface ModelOption {
  id: string;
  label: string;
  requiresSubscription: boolean;
  description?: string;
  top?: boolean;
  isCustom?: boolean;
  priority?: number;
  recommended?: boolean;
}

export interface CustomModel {
  id: string;
  label: string;
}

// SINGLE SOURCE OF TRUTH for all model data - aligned with backend constants
export const MODELS = {
  // 🎯 默认显示的四个主要模型
  // Local models (available in local mode only)
  'ollama': {
    tier: 'local',
    priority: 110,
    recommended: true,
    lowQuality: false,
    localOnly: true
  },
  // Premium OpenAI GPT-4o
  'gpt-4o': {
    tier: 'premium',
    priority: 105,
    recommended: true,
    lowQuality: false
  },
  // Default free Gemini model
  'gemini-3.1-pro-preview': {
    tier: 'free',
    priority: 104,
    recommended: true,
    lowQuality: false
  },
  'openrouter/google/gemini-3.1-pro-preview': {
    tier: 'free',
    priority: 103,
    recommended: false,
    lowQuality: false
  },
  'openrouter/google/gemini-3-flash-preview': {
    tier: 'free',
    priority: 102,
    recommended: false,
    lowQuality: false
  },
  'openrouter/z-ai/glm-4.7': {
    tier: 'free',
    priority: 101,
    recommended: false,
    lowQuality: false
  },
  'openrouter/z-ai/glm-5.1': {
    tier: 'free',
    priority: 102,
    recommended: true,
    lowQuality: false
  },
  'openrouter/z-ai/glm-5': {
    tier: 'free',
    priority: 100,
    recommended: false,
    lowQuality: false
  },
  // Claude Sonnet 4.5 - OpenRouter
  'openrouter/anthropic/claude-sonnet-4.5': {
    tier: 'free',
    priority: 107,
    recommended: true,
    lowQuality: false
  },
  // Kimi K2.5 - PPIO OpenAI-compatible route (legacy hidden alias)
  'ppio/moonshotai/kimi-k2.5': {
    tier: 'free',
    priority: 90,
    recommended: false,
    lowQuality: false
  },
  // Kimi K2.5 - Moonshot official route
  'kimi-k2.5': {
    tier: 'free',
    priority: 105,
    recommended: true,
    lowQuality: false
  },
  // Kimi K2.5 - OpenRouter endpoint
  'openrouter/moonshotai/kimi-k2.5': {
    tier: 'free',
    priority: 106,
    recommended: true,
    lowQuality: false
  },
  // Kimi K2.6 - Moonshot official route
  'kimi-k2.6': {
    tier: 'free',
    priority: 109,
    recommended: true,
    lowQuality: false
  },
  // Kimi K2.6 - OpenRouter endpoint
  'openrouter/moonshotai/kimi-k2.6': {
    tier: 'free',
    priority: 110,
    recommended: true,
    lowQuality: false
  },
  // Minimax M2.1 - OpenRouter
  'openrouter/minimax/minimax-m2.1': {
    tier: 'free',
    priority: 99,
    recommended: false,
    lowQuality: false
  },
  // Minimax M2.5 - OpenRouter
  'openrouter/minimax/minimax-m2.5': {
    tier: 'free',
    priority: 98,
    recommended: false,
    lowQuality: false
  },
  'openrouter/minimax/minimax-m2.7': {
    tier: 'free',
    priority: 99,
    recommended: true,
    lowQuality: false
  },
  'openrouter/xiaomi/mimo-v2-pro': {
    tier: 'free',
    priority: 101,
    recommended: true,
    lowQuality: false
  },
  'openrouter/xiaomi/mimo-v2.5-pro': {
    tier: 'free',
    priority: 108,
    recommended: true,
    lowQuality: false
  },
  'openrouter/deepseek/deepseek-v4-pro': {
    tier: 'free',
    priority: 108,
    recommended: true,
    lowQuality: false
  },
  'openrouter/deepseek/deepseek-v4-flash': {
    tier: 'free',
    priority: 107,
    recommended: true,
    lowQuality: false
  },
  'deepseek-v4-pro-high': {
    tier: 'free',
    priority: 108,
    recommended: true,
    lowQuality: false
  },
  'deepseek-v4-pro-max': {
    tier: 'free',
    priority: 107,
    recommended: true,
    lowQuality: false
  },
  'deepseek-v4-flash-high': {
    tier: 'free',
    priority: 106,
    recommended: true,
    lowQuality: false
  },
  'deepseek-v4-flash-max': {
    tier: 'free',
    priority: 105,
    recommended: true,
    lowQuality: false
  },
  'dashscope/minimax-m2.5': {
    tier: 'free',
    priority: 88,
    recommended: false,
    lowQuality: false
  },
  // Qwen 3.5 Plus - DashScope (Aliyun)
  'dashscope/qwen3.5-plus': {
    tier: 'free',
    priority: 97,
    recommended: false,
    lowQuality: false
  },
  'openrouter/qwen/qwen3.6-plus': {
    tier: 'free',
    priority: 103,
    recommended: true,
    lowQuality: false
  },
  // Volcengine Doubao Seed 2.0 models
  'volcengine/doubao-seed-2-0-pro-260215': {
    tier: 'free',
    priority: 96,
    recommended: false,
    lowQuality: false
  },
  'volcengine/doubao-seed-2-0-code-preview-260215': {
    tier: 'free',
    priority: 95,
    recommended: false,
    lowQuality: false
  },
  'mystery-model': {
    tier: 'free',
    priority: 108,
    recommended: true,
    lowQuality: false
  },
  // Free tier models (available to all users)
  'deepseek-chat': {
    tier: 'free',
    priority: 100,
    recommended: false,
    lowQuality: false
  },

  // 🔽 其他模型暂时注释，不在默认列表中显示
  /*
  'claude-sonnet-4': {
    tier: 'premium',
    priority: 99,
    recommended: true,
    lowQuality: false
  },
  // OpenAI models
  'gpt-4o': {
    tier: 'premium',
    priority: 98,
    recommended: true,
    lowQuality: false
  },
  'gpt-4o-mini': {
    tier: 'free',
    priority: 95,
    recommended: false,
    lowQuality: false
  },
  'gpt-3.5-turbo': {
    tier: 'free',
    priority: 85,
    recommended: false,
    lowQuality: true
  },

  // 'gemini-flash-2.5': {
  //   tier: 'free',
  //   priority: 70,
  //   recommended: false,
  //   lowQuality: false
  // },
  // 'qwen3': {
  //   tier: 'free',
  //   priority: 60,
  //   recommended: false,
  //   lowQuality: false
  // },

  // Premium/Paid tier models (require subscription) - except specific free models
  'moonshotai/kimi-k2': {
    tier: 'free',
    priority: 96,
    recommended: false,
    lowQuality: false
  },
  'grok-4': {
    tier: 'premium',
    priority: 94,
    recommended: false,
    lowQuality: false
  },
  'sonnet-3.7': {
    tier: 'premium',
    priority: 93,
    recommended: false,
    lowQuality: false
  },
  'google/gemini-2.5-pro': {
    tier: 'premium',
    priority: 96,
    recommended: false,
    lowQuality: false
  },
  'sonnet-3.5': {
    tier: 'premium',
    priority: 90,
    recommended: false,
    lowQuality: false
  },
  'gpt-5-mini': {
    tier: 'premium',
    priority: 98,
    recommended: false,
    lowQuality: false
  },
  'gemini-2.5-flash:thinking': {
    tier: 'premium',
    priority: 84,
    recommended: false,
    lowQuality: false
  },
  // 'deepseek/deepseek-chat-v3-0324': {
  //   tier: 'free',
  //   priority: 75,
  //   recommended: false,
  //   lowQuality: false
  // },
  */
};

// Canonical model IDs used across frontend and backend aliases.
const MODEL_ID_CANONICAL_MAP: Record<string, string> = {
  'anthropic/claude-sonnet-4.5': 'openrouter/anthropic/claude-sonnet-4.5',
  'google/gemini-3.1-pro-preview': 'openrouter/google/gemini-3.1-pro-preview',
  'google/gemini-3-flash-preview': 'openrouter/google/gemini-3-flash-preview',
  'minimax/minimax-m2.1': 'openrouter/minimax/minimax-m2.1',
  'minimax/minimax-m2.5': 'openrouter/minimax/minimax-m2.5',
  'minimax/minimax-m2.7': 'openrouter/minimax/minimax-m2.7',
  'xiaomi/mimo-v2-pro': 'openrouter/xiaomi/mimo-v2-pro',
  'openrouter/xiaomi/mimo-v2-pro': 'openrouter/xiaomi/mimo-v2-pro',
  'xiaomi/mimo-v2.5-pro': 'openrouter/xiaomi/mimo-v2.5-pro',
  'mimo-v2.5-pro': 'openrouter/xiaomi/mimo-v2.5-pro',
  'openrouter/xiaomi/mimo-v2.5-pro': 'openrouter/xiaomi/mimo-v2.5-pro',
  'openrouter/minimax/minimax-m2.7': 'openrouter/minimax/minimax-m2.7',
  'dashscope/minimax-m2.5': 'dashscope/minimax-m2.5',
  'minimax-m2.5': 'dashscope/minimax-m2.5',
  'minimax/m2.5': 'dashscope/minimax-m2.5',
  'MiniMax-M2.5': 'dashscope/minimax-m2.5',
  'openai-proxy/gpt-5.4': 'mystery-model',
  'gpt-5.4': 'mystery-model',
  'mystery-model': 'mystery-model',
  'glm-4.7': 'openrouter/z-ai/glm-4.7',
  'glm-5.1': 'openrouter/z-ai/glm-5.1',
  'glm-5': 'openrouter/z-ai/glm-5',
  'z-ai/glm-4.7': 'openrouter/z-ai/glm-4.7',
  'z-ai/glm-5.1': 'openrouter/z-ai/glm-5.1',
  'z-ai/glm-5': 'openrouter/z-ai/glm-5',
  'qwen/qwen3.6-plus': 'openrouter/qwen/qwen3.6-plus',
  'qwen3.6-plus': 'openrouter/qwen/qwen3.6-plus',
  'openrouter/qwen/qwen3.6-plus': 'openrouter/qwen/qwen3.6-plus',
  'qwen/qwen3.5-plus': 'dashscope/qwen3.5-plus',
  'qwen3.5-plus': 'dashscope/qwen3.5-plus',
  'openrouter/qwen/qwen3.5-plus': 'dashscope/qwen3.5-plus',
  'dashscope/qwen3.5-plus': 'dashscope/qwen3.5-plus',
  // Legacy Qwen aliases -> transparently route to qwen3.5-plus.
  'qwen/qwen3.5-397b-a17b': 'dashscope/qwen3.5-plus',
  'qwen3.5-397b-a17b': 'dashscope/qwen3.5-plus',
  'openrouter/qwen/qwen3.5-397b-a17b': 'dashscope/qwen3.5-plus',
  'dashscope/qwen3.5-397b-a17b': 'dashscope/qwen3.5-plus',
  'openrouter/deepseek/deepseek-chat': 'deepseek-chat',
  'deepseek/deepseek-chat': 'deepseek-chat',
  'deepseek/deepseek-v4-pro': 'deepseek-v4-pro-high',
  'deepseek-v4-pro-openrouter': 'openrouter/deepseek/deepseek-v4-pro',
  'openrouter/deepseek/deepseek-v4-pro': 'openrouter/deepseek/deepseek-v4-pro',
  'deepseek/deepseek-v4-flash': 'deepseek-v4-flash-high',
  'deepseek-v4-flash-openrouter': 'openrouter/deepseek/deepseek-v4-flash',
  'openrouter/deepseek/deepseek-v4-flash': 'openrouter/deepseek/deepseek-v4-flash',
  'deepseek-v4-pro': 'deepseek-v4-pro-high',
  'deepseek/deepseek-v4-pro-high': 'deepseek-v4-pro-high',
  'deepseek-v4-pro-high': 'deepseek-v4-pro-high',
  'deepseek/deepseek-v4-pro-max': 'deepseek-v4-pro-max',
  'deepseek-v4-pro-max': 'deepseek-v4-pro-max',
  'deepseek-v4-flash': 'deepseek-v4-flash-high',
  'deepseek/deepseek-v4-flash-high': 'deepseek-v4-flash-high',
  'deepseek-v4-flash-high': 'deepseek-v4-flash-high',
  'deepseek/deepseek-v4-flash-max': 'deepseek-v4-flash-max',
  'deepseek-v4-flash-max': 'deepseek-v4-flash-max',
  'ppio/moonshotai/kimi-k2.5': 'openrouter/moonshotai/kimi-k2.5',
  'moonshot/kimi-k2.5': 'kimi-k2.5',
  'moonshotai/kimi-k2.5': 'kimi-k2.5',
  'openrouter/moonshotai/kimi-k2.5': 'openrouter/moonshotai/kimi-k2.5',
  'moonshot/kimi-k2.6': 'kimi-k2.6',
  'moonshotai/kimi-k2.6': 'kimi-k2.6',
  'kimi/kimi-k2.6': 'kimi-k2.6',
  'kimi-k2.6': 'kimi-k2.6',
  'openrouter/moonshotai/kimi-k2.6': 'openrouter/moonshotai/kimi-k2.6',
  'doubao-seed-2-0-pro-260215': 'volcengine/doubao-seed-2-0-pro-260215',
  'doubao-seed-2-0-code-preview-260215': 'volcengine/doubao-seed-2-0-code-preview-260215',
};

const CURATED_MODELS: Array<{ id: string; label: string; recommended: boolean }> = [
  {
    id: 'openrouter/google/gemini-3.1-pro-preview',
    label: 'Gemini 3.1 Pro (Preview)',
    recommended: false,
  },
  {
    id: 'openrouter/google/gemini-3-flash-preview',
    label: 'Gemini 3 Flash (Preview)',
    recommended: false,
  },
  {
    id: 'openrouter/moonshotai/kimi-k2.5',
    label: 'Kimi K2.5',
    recommended: true,
  },
  {
    id: 'kimi-k2.5',
    label: 'Kimi K2.5 (Moonshot Official)',
    recommended: true,
  },
  {
    id: 'openrouter/moonshotai/kimi-k2.6',
    label: 'Kimi K2.6 (OpenRouter)',
    recommended: true,
  },
  {
    id: 'kimi-k2.6',
    label: 'Kimi K2.6 (Moonshot Official)',
    recommended: true,
  },
  {
    id: 'openrouter/minimax/minimax-m2.1',
    label: 'Minimax M2.1',
    recommended: false,
  },
  {
    id: 'openrouter/minimax/minimax-m2.5',
    label: 'Minimax M2.5 (OpenRouter)',
    recommended: true,
  },
  {
    id: 'openrouter/minimax/minimax-m2.7',
    label: 'MiniMax M2.7',
    recommended: true,
  },
  {
    id: 'openrouter/xiaomi/mimo-v2-pro',
    label: 'Mimo V2 Pro',
    recommended: true,
  },
  {
    id: 'openrouter/xiaomi/mimo-v2.5-pro',
    label: 'MiMo V2.5 Pro (OpenRouter)',
    recommended: true,
  },
  {
    id: 'openrouter/deepseek/deepseek-v4-pro',
    label: 'DeepSeek V4 Pro (OpenRouter)',
    recommended: true,
  },
  {
    id: 'openrouter/deepseek/deepseek-v4-flash',
    label: 'DeepSeek V4 Flash (OpenRouter)',
    recommended: true,
  },
  {
    id: 'deepseek-v4-pro-high',
    label: 'DeepSeek V4 Pro High (Official)',
    recommended: true,
  },
  {
    id: 'deepseek-v4-pro-max',
    label: 'DeepSeek V4 Pro Max (Official)',
    recommended: true,
  },
  {
    id: 'deepseek-v4-flash-high',
    label: 'DeepSeek V4 Flash High (Official)',
    recommended: true,
  },
  {
    id: 'deepseek-v4-flash-max',
    label: 'DeepSeek V4 Flash Max (Official)',
    recommended: true,
  },
  {
    id: 'dashscope/minimax-m2.5',
    label: 'Minimax M2.5 (阿里云 DashScope)',
    recommended: false,
  },
  {
    id: 'openrouter/qwen/qwen3.6-plus',
    label: 'Qwen 3.6 Plus',
    recommended: true,
  },
  {
    id: 'dashscope/qwen3.5-plus',
    label: 'Qwen 3.5 Plus (多模态)',
    recommended: true,
  },
  {
    id: 'openrouter/z-ai/glm-4.7',
    label: 'GLM 4.7',
    recommended: true,
  },
  {
    id: 'openrouter/z-ai/glm-5.1',
    label: 'GLM 5.1',
    recommended: true,
  },
  {
    id: 'openrouter/z-ai/glm-5',
    label: 'GLM 5',
    recommended: true,
  },
  {
    id: 'volcengine/doubao-seed-2-0-code-preview-260215',
    label: 'seed-2-0-code-preview-260215',
    recommended: false,
  },
  {
    id: 'mystery-model',
    label: '神秘模型',
    recommended: true,
  },
];

const CURATED_MODEL_IDS = CURATED_MODELS.map(model => model.id);
const CURATED_MODEL_LABELS = Object.fromEntries(
  CURATED_MODELS.map(model => [model.id, model.label]),
);
const CURATED_MODEL_RECOMMENDED = Object.fromEntries(
  CURATED_MODELS.map(model => [model.id, model.recommended]),
);

export const toCanonicalModelId = (modelId: string): string => {
  return MODEL_ID_CANONICAL_MAP[modelId] || modelId;
};

const SHADOW_CLONE_OFFICIAL_KIMI_MODEL_IDS = new Set([
  'kimi-k2.5',
  'moonshot/kimi-k2.5',
  'moonshotai/kimi-k2.5',
  'kimi/kimi-k2.5',
  'kimi-k2.6',
  'moonshot/kimi-k2.6',
  'moonshotai/kimi-k2.6',
  'kimi/kimi-k2.6',
]);

const SHADOW_CLONE_OFFICIAL_DEEPSEEK_MODEL_IDS = new Set([
  'deepseek-v4-pro-high',
  'deepseek-v4-pro-max',
  'deepseek-v4-flash-high',
  'deepseek-v4-flash-max',
]);

export const isShadowCloneSelectableModelId = (modelId: string): boolean => {
  const normalizedModelId = String(modelId || '').trim().toLowerCase();
  if (!normalizedModelId) {
    return false;
  }

  if (SHADOW_CLONE_OFFICIAL_KIMI_MODEL_IDS.has(normalizedModelId)) {
    return true;
  }

  if (SHADOW_CLONE_OFFICIAL_DEEPSEEK_MODEL_IDS.has(normalizedModelId)) {
    return true;
  }

  if (!normalizedModelId.startsWith('openrouter/')) {
    return false;
  }

  if (
    normalizedModelId.includes('qwen3.5-plus') ||
    normalizedModelId.includes('qwen3.5-397b-a17b')
  ) {
    return false;
  }

  return true;
};

// Helper to check if a user can access a model based on subscription status
export const canAccessModel = (
  subscriptionStatus: SubscriptionStatus,
  requiresSubscription: boolean,
): boolean => {
  if (isLocalMode()) {
    return true;
  }
  return subscriptionStatus === 'active' || !requiresSubscription;
};

// Helper to format a model name for display
export const formatModelName = (name: string): string => {
  return name
    .split('-')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
};

// Add openrouter/ prefix to custom models
export const getPrefixedModelId = (modelId: string, isCustom: boolean): string => {
  if (isCustom && !modelId.startsWith('openrouter/')) {
    return `openrouter/${modelId}`;
  }
  return modelId;
};

// Helper to get custom models from localStorage
export const getCustomModels = (): CustomModel[] => {
  if (!isLocalMode() || typeof window === 'undefined') return [];

  try {
    const storedModels = localStorage.getItem(STORAGE_KEY_CUSTOM_MODELS);
    if (!storedModels) return [];

    const parsedModels = JSON.parse(storedModels);
    if (!Array.isArray(parsedModels)) return [];

    return parsedModels
      .filter((model: any) =>
        model && typeof model === 'object' &&
        typeof model.id === 'string' &&
        typeof model.label === 'string');
  } catch (e) {
    console.error('Error parsing custom models:', e);
    return [];
  }
};

// Helper to save model preference to localStorage safely
const saveModelPreference = (modelId: string): void => {
  try {
    localStorage.setItem(STORAGE_KEY_MODEL, modelId);
  } catch (error) {
    console.warn('Failed to save model preference to localStorage:', error);
  }
};

const getDefaultModelId = (subscriptionStatus: SubscriptionStatus): string =>
  isLocalMode()
    ? DEFAULT_LOCAL_CHAT_MODEL_ID
    : subscriptionStatus === 'active'
      ? DEFAULT_PREMIUM_MODEL_ID
      : DEFAULT_FREE_MODEL_ID;

export const useModelSelection = () => {
  const [selectedModel, setSelectedModel] = useState(DEFAULT_FREE_MODEL_ID);
  const [customModels, setCustomModels] = useState<CustomModel[]>([]);
  const [hasInitialized, setHasInitialized] = useState(false);

  const { data: subscriptionData } = useSubscription();
  const { data: modelsData, isLoading: isLoadingModels } = useAvailableModels({
    refetchOnMount: false,
  });

  const subscriptionStatus: SubscriptionStatus = subscriptionData?.status === 'active'
    ? 'active'
    : 'no_subscription';

  // Function to refresh custom models from localStorage
  const refreshCustomModels = () => {
    if (isLocalMode() && typeof window !== 'undefined') {
      const freshCustomModels = getCustomModels();
      setCustomModels(freshCustomModels);
    }
  };

  // Load custom models from localStorage
  useEffect(() => {
    refreshCustomModels();
  }, []);

  // Generate model options list with consistent structure
  const MODEL_OPTIONS = useMemo(() => {
    let models = [];

    // Curated model list shown in chat input.
    if (!modelsData?.models || isLoadingModels) {
      models = CURATED_MODELS.map(model => ({
        id: model.id,
        label: model.label,
        requiresSubscription: false,
        priority: MODELS[model.id]?.priority || 0,
        recommended: model.recommended,
      }));
    } else {
      // Keep only curated models from API response.
      models = modelsData.models
        .filter(model => {
          const shortName = toCanonicalModelId(model.short_name || model.id);
          const fullName = toCanonicalModelId(model.id);
          return CURATED_MODEL_IDS.includes(shortName) || CURATED_MODEL_IDS.includes(fullName);
        })
        .map(model => {
          const shortName = model.short_name || model.id;
          const canonicalModelId = toCanonicalModelId(shortName);
          const displayName = model.display_name || shortName;

          // Format the display label
          let cleanLabel = displayName;
          if (cleanLabel.includes('/')) {
            cleanLabel = cleanLabel.split('/').pop() || cleanLabel;
          }

          cleanLabel = cleanLabel
            .replace(/-/g, ' ')
            .split(' ')
            .map(word => word.charAt(0).toUpperCase() + word.slice(1))
            .join(' ');

          // 不再为OpenRouter模型添加前缀，直接显示模型名

          // Get model data from our central MODELS constant
          const modelData = MODELS[canonicalModelId] || MODELS[shortName] || {};
          const isPremium = model?.requires_subscription || modelData.tier === 'premium' || false;

          return {
            id: canonicalModelId,
            label: CURATED_MODEL_LABELS[canonicalModelId] || cleanLabel,
            requiresSubscription: isPremium,
            top: modelData.priority >= 90, // Mark high-priority models as "top"
            priority: modelData.priority || 0,
            lowQuality: modelData.lowQuality || false,
            recommended: CURATED_MODEL_RECOMMENDED[canonicalModelId] ?? modelData.recommended ?? false,
          };
        });
    }

    // Deduplicate aliases and force curated models to stay visible even
    // when backend /available-models omits a model temporarily.
    const mergedModels = new Map<string, any>();
    models.forEach(model => {
      if (!mergedModels.has(model.id)) {
        mergedModels.set(model.id, model);
      }
    });

    CURATED_MODELS.forEach(model => {
      if (!mergedModels.has(model.id)) {
        mergedModels.set(model.id, {
          id: model.id,
          label: model.label,
          requiresSubscription: false,
          priority: MODELS[model.id]?.priority || 0,
          recommended: model.recommended,
        });
      }
    });

    models = Array.from(mergedModels.values());

    // Add custom models if in local mode
    if (isLocalMode() && customModels.length > 0) {
      const customModelOptions = customModels.map(model => ({
        id: model.id,
        label: model.label || formatModelName(model.id),
        requiresSubscription: false,
        top: false,
        isCustom: true,
        priority: 30, // Low priority by default
        lowQuality: false,
        recommended: false
      }));

      models = [...models, ...customModelOptions];
    }

    // Sort models consistently in one place:
    // 1. First by recommended (recommended first)
    // 2. Then by priority (higher first)
    // 3. Finally by name (alphabetical)
    const sortedModels = models.sort((a, b) => {
      // First by recommended status
      if (a.recommended !== b.recommended) {
        return a.recommended ? -1 : 1;
      }

      // Then by priority (higher first)
      if (a.priority !== b.priority) {
        return b.priority - a.priority;
      }

      // Finally by name
      return a.label.localeCompare(b.label);
    });
    return sortedModels;
  }, [modelsData, isLoadingModels, customModels]);

  // Get filtered list of models the user can access (no additional sorting)
  const availableModels = useMemo(() => {
    return isLocalMode()
      ? MODEL_OPTIONS
      : MODEL_OPTIONS.filter(model =>
          canAccessModel(subscriptionStatus, model.requiresSubscription)
        );
  }, [MODEL_OPTIONS, subscriptionStatus]);

  // Initialize selected model from localStorage ONLY ONCE
  useEffect(() => {
    if (typeof window === 'undefined' || hasInitialized) return;
    try {
      const savedModelRaw = localStorage.getItem(STORAGE_KEY_MODEL);
      const savedModel = savedModelRaw ? toCanonicalModelId(savedModelRaw) : null;

      // If we have a saved model, validate it's still available and accessible
      if (savedModel) {
        // Wait for models to load before validating
        if (isLoadingModels) {
          return;
        }

        const modelOption = MODEL_OPTIONS.find(option => option.id === savedModel);
        const isCustomModel = isLocalMode() && customModels.some(model => model.id === savedModelRaw);

        // Check if saved model is still valid and accessible
        if (modelOption || isCustomModel) {
          const isAccessible = isLocalMode() ||
            canAccessModel(subscriptionStatus, modelOption?.requiresSubscription ?? false);

          if (isAccessible) {
            const activeModelId = isCustomModel ? savedModelRaw || savedModel : savedModel;
            setSelectedModel(activeModelId);
            saveModelPreference(activeModelId);
            setHasInitialized(true);
            return;
          }
        }
      }

      // Fallback to a backend-supported chat model. In LOCAL env this app may
      // still be connected to production backends where bare "ollama" is not
      // configured, so only send ollama when the user explicitly selected it.
      const defaultModel = getDefaultModelId(subscriptionStatus);
      setSelectedModel(defaultModel);
      saveModelPreference(defaultModel);
      setHasInitialized(true);

    } catch (error) {
      console.warn('Failed to load preferences from localStorage:', error);
      const defaultModel = getDefaultModelId(subscriptionStatus);
      setSelectedModel(defaultModel);
      saveModelPreference(defaultModel);
      setHasInitialized(true);
    }
  }, [subscriptionStatus, MODEL_OPTIONS, isLoadingModels, customModels, hasInitialized]);

  // Handle model selection change
  const handleModelChange = (modelId: string) => {
    // Refresh custom models from localStorage to ensure we have the latest
    if (isLocalMode()) {
      refreshCustomModels();
    }

    const canonicalModelId = toCanonicalModelId(modelId);

    // First check if it's a custom model in local mode
    const isCustomModel =
      isLocalMode() &&
      customModels.some(model => model.id === modelId || model.id === canonicalModelId);

    // Then check if it's in standard MODEL_OPTIONS
    const modelOption = MODEL_OPTIONS.find(option => option.id === canonicalModelId);

    // Check if model exists in either custom models or standard options
    if (!modelOption && !isCustomModel) {
      console.warn('Model not found in options:', modelId, MODEL_OPTIONS, isCustomModel, customModels);

      // Reset to a backend-supported default when the selected model is not
      // found. Do not poison later chat submissions with bare "ollama" unless
      // that was an explicit selectable model.
      const defaultModel = getDefaultModelId(subscriptionStatus);
      setSelectedModel(defaultModel);
      saveModelPreference(defaultModel);
      return;
    }

    // Check access permissions (except for custom models in local mode)
    if (
      !isCustomModel &&
      !isLocalMode() &&
      !canAccessModel(subscriptionStatus, modelOption?.requiresSubscription ?? false)
    ) {
      console.warn('Model not accessible:', modelId);
      return;
    }

    const activeModelId = isCustomModel ? modelId : canonicalModelId;
    setSelectedModel(activeModelId);
    saveModelPreference(activeModelId);
  };

  // Get the actual model ID to send to the backend
  const getActualModelId = (modelId: string): string => {
    // For ollama local model, always return "ollama" as the backend model name
    if (modelId === 'ollama') {
      return 'ollama';
    }

    // Canonicalize model aliases before sending to backend.
    const canonicalModelId = toCanonicalModelId(modelId);
    if (canonicalModelId === 'mystery-model') {
      return 'openai-proxy/gpt-5.4';
    }
    return canonicalModelId;
  };

  return {
    selectedModel,
    setSelectedModel: (modelId: string) => {
      handleModelChange(modelId);
    },
    subscriptionStatus,
    availableModels,
    allModels: MODEL_OPTIONS,  // Already pre-sorted
    customModels,
    getActualModelId,
    refreshCustomModels,
    canAccessModel: (modelId: string) => {
      if (isLocalMode()) return true;
      const canonicalModelId = toCanonicalModelId(modelId);
      const model = MODEL_OPTIONS.find(m => m.id === canonicalModelId);
      return model ? canAccessModel(subscriptionStatus, model.requiresSubscription) : false;
    },
    isSubscriptionRequired: (modelId: string) => {
      const canonicalModelId = toCanonicalModelId(modelId);
      return MODEL_OPTIONS.find(m => m.id === canonicalModelId)?.requiresSubscription || false;
    }
  };
};

// Export the hook but not any sorting logic - sorting is handled internally
