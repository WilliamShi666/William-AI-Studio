import type { UserRole } from './api/auth';

export type ModeId = 'v1' | 'v2' | 'roys';

export type ViewId = 'dashboard' | 'knowledge' | 'chat' | 'qa' | 'retrieval' | 'debate' | 'settings' | 'admin';

export interface ModeConfig {
  id: ModeId;
  label: string;
  badge: string;
  description: string;
  brandName: string;
  theme: 'dark' | 'light';
  allowedViews: ViewId[];
  dataVersion: 'v1' | 'v2';
  readOnly: boolean;
}

export const MODE_SEQUENCE: ModeId[] = ['v1', 'v2', 'roys'];

export const MODE_CONFIG: Record<ModeId, ModeConfig> = {
  v1: {
    id: 'v1',
    label: 'VLM模式 v1.0.0',
    badge: '1.0',
    description: '支持快速模式和精确模式 (VLM)',
    brandName: '多模态RAG系统',
    theme: 'dark',
    allowedViews: ['dashboard', 'knowledge', 'chat', 'qa', 'retrieval', 'debate', 'settings'],
    dataVersion: 'v1',
    readOnly: false,
  },
  v2: {
    id: 'v2',
    label: 'OCR模式 v2.0.0',
    badge: '2.0',
    description: 'MinerU · DeepSeek-OCR · PaddleOCR-VL',
    brandName: '多模态RAG系统',
    theme: 'light',
    allowedViews: ['dashboard', 'knowledge', 'chat', 'qa', 'retrieval', 'debate', 'settings'],
    dataVersion: 'v2',
    readOnly: false,
  },
  roys: {
    id: 'roys',
    label: 'Roys国际学科助教军团',
    badge: 'R',
    description: '国际学科助教',
    brandName: 'Roys乐亦思国际学科助教军团',
    theme: 'dark',
    allowedViews: ['dashboard', 'chat', 'qa', 'debate', 'settings'],
    dataVersion: 'v2',
    readOnly: true,
  },
};

export const getFirstAllowedView = (mode: ModeId): ViewId => {
  return MODE_CONFIG[mode].allowedViews[0];
};

export const getAccessibleModes = (role: UserRole): ModeId[] => {
  return role === 'admin' ? MODE_SEQUENCE : ['roys'];
};

export const canAccessAdminPanel = (role: UserRole): boolean => {
  return role === 'admin';
};

export const isModeUsingV2Data = (mode: ModeId): boolean => {
  return MODE_CONFIG[mode].dataVersion === 'v2';
};

export const isModeReadOnly = (mode: ModeId): boolean => {
  return MODE_CONFIG[mode].readOnly;
};

export const isDarkThemeMode = (mode: ModeId): boolean => {
  return MODE_CONFIG[mode].theme === 'dark';
};
