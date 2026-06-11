'use client';

import React, { useMemo, useState } from 'react';
import {
  Bot,
  Check,
  ChevronDown,
  GitBranch,
  Loader2,
  Sparkles,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { useShadowCloneStore, type ShadowCloneMode } from '@/lib/stores/shadow-clone-store';
import {
  getShadowCloneAdminModelConfig,
  ShadowCloneAdminAccessError,
  ShadowCloneAdminEndpointNotFoundError,
  type ShadowCloneAdminModelConfig,
  updateShadowCloneAdminModelConfig,
} from '@/lib/api';
import { toast } from 'sonner';
import { isShadowCloneSelectableModelId } from './_use-model-selection';

interface ShadowCloneToggleProps {
  disabled?: boolean;
  modelOptions?: Array<{
    id: string;
    label?: string;
  }>;
  shadowCloneMainModel?: string;
  shadowCloneSubagentModel?: string;
  onShadowCloneMainModelChange?: (model?: string) => void;
  onShadowCloneSubagentModelChange?: (model?: string) => void;
  resolveModelId?: (modelId: string) => string;
}

const MODE_OPTIONS: Array<{
  value: ShadowCloneMode;
  shortLabel: string;
  title: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  {
    value: 'on',
    shortLabel: '影分身',
    title: '影分身模式 (ON)',
    description: '开启影分身模式，适用于多线程并行任务',
    icon: GitBranch,
  },
  {
    value: 'off',
    shortLabel: '常规',
    title: '常规模式',
    description: '单智能体进行任务',
    icon: Bot,
  },
  {
    value: 'auto',
    shortLabel: '智能',
    title: '智能 (AUTO)',
    description: '由智能体自行判断任务复杂度并决定是否开启分身',
    icon: Sparkles,
  },
];

const MODE_THEME: Record<
  ShadowCloneMode,
  {
    triggerClassName: string;
    iconClassName: string;
  }
> = {
  on: {
    triggerClassName:
      'border-blue-200 bg-blue-50 text-blue-700 hover:border-blue-300 hover:bg-blue-100/80',
    iconClassName: 'text-blue-600',
  },
  off: {
    triggerClassName:
      'border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50',
    iconClassName: 'text-slate-500',
  },
  auto: {
    triggerClassName:
      'border-sky-200 bg-sky-50 text-sky-700 hover:border-sky-300 hover:bg-sky-100/80',
    iconClassName: 'text-sky-600',
  },
};

const GLOBAL_DEFAULT_OPTION = '__shadow_clone_global_default__';

export function ShadowCloneToggle({
  disabled = false,
  modelOptions = [],
  shadowCloneMainModel,
  shadowCloneSubagentModel,
  onShadowCloneMainModelChange,
  onShadowCloneSubagentModelChange,
  resolveModelId = (modelId) => modelId,
}: ShadowCloneToggleProps) {
  const mode = useShadowCloneStore((state) => state.mode);
  const setMode = useShadowCloneStore((state) => state.setMode);
  const [open, setOpen] = useState(false);
  const [adminConfigStatus, setAdminConfigStatus] = useState<
    'idle' | 'loading' | 'ready' | 'unavailable'
  >('idle');
  const [globalConfig, setGlobalConfig] = useState<ShadowCloneAdminModelConfig | null>(null);
  const [isPromoting, setIsPromoting] = useState(false);

  const currentOption = MODE_OPTIONS.find((option) => option.value === mode) || MODE_OPTIONS[1];
  const currentTheme = MODE_THEME[mode];
  const CurrentIcon = currentOption.icon;
  const effectiveMainModel =
    shadowCloneMainModel || globalConfig?.main_model_name || '';
  const effectiveSubagentModel =
    shadowCloneSubagentModel || globalConfig?.subagent_model_name || '';
  const effectiveMainModelSupported =
    !effectiveMainModel || isShadowCloneSelectableModelId(effectiveMainModel);
  const effectiveSubagentModelSupported =
    !effectiveSubagentModel || isShadowCloneSelectableModelId(effectiveSubagentModel);
  const canPromote =
    adminConfigStatus === 'ready' &&
    !!effectiveMainModel &&
    !!effectiveSubagentModel &&
    effectiveMainModelSupported &&
    effectiveSubagentModelSupported &&
    (effectiveMainModel !== globalConfig?.main_model_name ||
      effectiveSubagentModel !== globalConfig?.subagent_model_name);
  const selectOptions = useMemo(() => {
    const mergedOptions = new Map<
      string,
      { id: string; label: string; disabled?: boolean }
    >();

    modelOptions.forEach((option) => {
      if (!option?.id) {
        return;
      }
      if (!isShadowCloneSelectableModelId(option.id)) {
        return;
      }
      mergedOptions.set(option.id, {
        id: option.id,
        label: option.label || option.id,
      });
    });

    [
      globalConfig?.main_model_name,
      globalConfig?.subagent_model_name,
      shadowCloneMainModel,
      shadowCloneSubagentModel,
    ].forEach((modelId) => {
      if (!modelId || mergedOptions.has(modelId)) {
        return;
      }
      const supported = isShadowCloneSelectableModelId(modelId);
      mergedOptions.set(modelId, {
        id: modelId,
        label: supported
          ? modelId
          : `${modelId} (legacy unsupported for Shadow Clone)`,
        disabled: !supported,
      });
    });

    return Array.from(mergedOptions.values());
  }, [
    globalConfig?.main_model_name,
    globalConfig?.subagent_model_name,
    modelOptions,
    shadowCloneMainModel,
    shadowCloneSubagentModel,
  ]);

  const loadAdminConfig = async () => {
    setAdminConfigStatus('loading');

    try {
      const config = await getShadowCloneAdminModelConfig();
      setGlobalConfig(config);
      setAdminConfigStatus('ready');
    } catch (error) {
      if (
        error instanceof ShadowCloneAdminAccessError ||
        error instanceof ShadowCloneAdminEndpointNotFoundError
      ) {
        setAdminConfigStatus('unavailable');
        return;
      }
      console.error('Failed to load Shadow Clone admin config:', error);
      setAdminConfigStatus('unavailable');
    }
  };

  const handlePromote = async () => {
    if (!effectiveMainModel || !effectiveSubagentModel) {
      return;
    }

    setIsPromoting(true);
    try {
      const nextConfig = await updateShadowCloneAdminModelConfig({
        main_model_name: resolveModelId(effectiveMainModel),
        subagent_model_name: resolveModelId(effectiveSubagentModel),
      });
      setGlobalConfig(nextConfig);
      toast.success('Shadow Clone global defaults updated');
    } catch (error) {
      console.error('Failed to promote Shadow Clone model defaults:', error);
      toast.error(
        error instanceof Error
          ? error.message
          : 'Failed to update Shadow Clone global defaults',
      );
    } finally {
      setIsPromoting(false);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (nextOpen && (adminConfigStatus !== 'ready' || !globalConfig)) {
          void loadAdminConfig();
        }
        if (!nextOpen && adminConfigStatus !== 'ready') {
          setAdminConfigStatus('idle');
        }
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className={cn(
            'inline-flex h-9 items-center gap-2 rounded-xl border px-3 text-xs font-medium shadow-sm transition-all duration-200',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-200/80',
            currentTheme.triggerClassName,
            disabled && 'cursor-not-allowed opacity-60',
          )}
          aria-label={`Shadow clone mode: ${mode}`}
          aria-expanded={open}
        >
          <CurrentIcon className={cn('h-3.5 w-3.5', currentTheme.iconClassName)} />
          <span>{currentOption.shortLabel}</span>
          <ChevronDown
            className={cn(
              'h-3.5 w-3.5 text-slate-400 transition-transform duration-200',
              open && 'rotate-180',
            )}
          />
        </button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        side="top"
        sideOffset={10}
        className="z-[90] max-h-[min(75vh,32rem)] w-[min(320px,calc(100vw-1.5rem))] overflow-y-auto rounded-2xl border border-slate-200 bg-white p-2 shadow-[0_20px_50px_-24px_rgba(15,23,42,0.45)]"
      >
        <div className="px-2 pb-2 pt-1 text-xs font-medium text-slate-500">
          选择当前任务执行模式
        </div>
        <div className="space-y-1">
          {MODE_OPTIONS.map((option) => {
            const OptionIcon = option.icon;
            const selected = option.value === mode;

            return (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  setMode(option.value);
                  setOpen(false);
                }}
                className={cn(
                  'group flex w-full cursor-pointer items-start gap-3 rounded-2xl border border-transparent px-3 py-3 text-left transition-all duration-200',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-200/80',
                  selected
                    ? 'border-blue-200 bg-blue-50/80'
                    : 'hover:border-slate-200 hover:bg-slate-50',
                )}
              >
                <div
                  className={cn(
                    'mt-0.5 flex h-8 w-8 items-center justify-center rounded-xl border transition-colors duration-200',
                    selected
                      ? 'border-blue-200 bg-white text-blue-600'
                      : 'border-slate-200 bg-white text-slate-500 group-hover:text-slate-700',
                  )}
                >
                  <OptionIcon className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-semibold text-slate-900">
                      {option.title}
                    </span>
                    {selected && <Check className="h-4 w-4 text-blue-600" />}
                  </div>
                  <p className="text-xs leading-5 text-slate-500">
                    {option.description}
                  </p>
                </div>
              </button>
            );
          })}
        </div>
        {adminConfigStatus === 'loading' && (
          <div className="mt-3 flex items-center gap-2 border-t border-slate-100 px-2 pb-1 pt-3 text-xs text-slate-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>Checking admin controls…</span>
          </div>
        )}
        {adminConfigStatus === 'ready' && globalConfig && (
          <div className="mt-3 border-t border-slate-100 px-2 pt-3">
            <div className="mb-2 flex items-center justify-between gap-3">
              <div>
                <div className="text-xs font-semibold text-slate-900">Advanced</div>
                <p className="text-[11px] leading-4 text-slate-500">
                  Per-run Shadow Clone model overrides for admins.
                </p>
                <p className="mt-1 text-[11px] leading-4 text-slate-500">
                  Shadow Clone supports Kimi, official DeepSeek V4 variants, and OpenRouter-backed models.
                </p>
              </div>
            </div>
            <div className="space-y-2">
              <label className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
                Main Agent
              </label>
              <select
                value={shadowCloneMainModel ?? GLOBAL_DEFAULT_OPTION}
                onChange={(event) =>
                  onShadowCloneMainModelChange?.(
                    event.target.value === GLOBAL_DEFAULT_OPTION
                      ? undefined
                      : event.target.value,
                  )
                }
                className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 outline-none transition-colors focus:border-blue-300"
              >
                <option value={GLOBAL_DEFAULT_OPTION}>
                  {globalConfig.main_model_name
                    ? `Use global default (${globalConfig.main_model_name}${
                        isShadowCloneSelectableModelId(globalConfig.main_model_name)
                          ? ''
                          : ', legacy unsupported'
                      })`
                    : 'Use system default'}
                </option>
                {selectOptions.map((option) => (
                  <option
                    key={`main-${option.id}`}
                    value={option.id}
                    disabled={Boolean(option.disabled)}
                  >
                    {option.label}
                  </option>
                ))}
              </select>

              <label className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
                Subagent
              </label>
              <select
                value={shadowCloneSubagentModel ?? GLOBAL_DEFAULT_OPTION}
                onChange={(event) =>
                  onShadowCloneSubagentModelChange?.(
                    event.target.value === GLOBAL_DEFAULT_OPTION
                      ? undefined
                      : event.target.value,
                  )
                }
                className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 outline-none transition-colors focus:border-blue-300"
              >
                <option value={GLOBAL_DEFAULT_OPTION}>
                  {globalConfig.subagent_model_name
                    ? `Use global default (${globalConfig.subagent_model_name}${
                        isShadowCloneSelectableModelId(globalConfig.subagent_model_name)
                          ? ''
                          : ', legacy unsupported'
                      })`
                    : 'Use system default'}
                </option>
                {selectOptions.map((option) => (
                  <option
                    key={`subagent-${option.id}`}
                    value={option.id}
                    disabled={Boolean(option.disabled)}
                  >
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="mt-3 flex items-center justify-between gap-3">
              <p className="text-[11px] leading-4 text-slate-500">
                Next run pair:
                {' '}
                <span className="font-medium text-slate-700">
                  {effectiveMainModel || 'unset'}
                </span>
                {' / '}
                <span className="font-medium text-slate-700">
                  {effectiveSubagentModel || 'unset'}
                </span>
              </p>
              {(!effectiveMainModelSupported || !effectiveSubagentModelSupported) && (
                <p className="text-[11px] leading-4 text-amber-600">
                  Legacy unsupported pair selected. Choose Kimi, DeepSeek V4, or an OpenRouter model.
                </p>
              )}
              <button
                type="button"
                onClick={handlePromote}
                disabled={disabled || isPromoting || !canPromote}
                className={cn(
                  'inline-flex items-center rounded-xl border px-3 py-2 text-[11px] font-medium transition-colors',
                  canPromote && !disabled && !isPromoting
                    ? 'border-slate-300 bg-slate-900 text-white hover:bg-slate-800'
                    : 'cursor-not-allowed border-slate-200 bg-slate-100 text-slate-400',
                )}
              >
                {isPromoting ? 'Promoting…' : 'Promote Global Default'}
              </button>
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
