'use client';

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  ExternalLink,
  OctagonX,
  Pause,
  X,
} from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import {
  useShadowCloneStore,
  type ShadowCloneSubtask,
  type ShadowCloneSubtaskStatus,
} from '@/lib/stores/shadow-clone-store';
import type { ShadowClonePanelToolCall } from '@/lib/shadow-clone-panel';
import { ShadowCloneRunningSpinner } from './shadow-clone-running-spinner';

interface ShadowCloneProgressProps {
  agentRunId: string | null;
  onSubtaskSelect?: () => void;
}

interface ShadowCloneNotice {
  tone: 'warning' | 'error';
  text: string;
}

const EMPTY_TOOL_CALLS: readonly ShadowClonePanelToolCall[] = [];

const ACTIVE_SYNC_PHASES = new Set(['running', 'recovering', 'aggregating']);
const AUTHORITATIVE_SYNC_INTERVAL_MS = 4000;
const STALE_SYNC_THRESHOLD_MS = 12000;

const isClosedShadowClonePhase = (phase: string): boolean =>
  phase === 'completed' || phase === 'cancelled' || phase === 'timeout';

const STATUS_META: Record<
  ShadowCloneSubtaskStatus,
  {
    label: string;
    badgeClassName: string;
    icon: React.ComponentType<{ className?: string }>;
    iconClassName: string;
    ringClassName: string;
    cardClassName: string;
  }
> = {
  pending: {
    label: '等待中',
    badgeClassName: 'bg-slate-100 text-slate-600',
    icon: CircleDashed,
    iconClassName: 'text-slate-400',
    ringClassName: 'border-slate-200 bg-slate-50',
    cardClassName: 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/80',
  },
  running: {
    label: '运行中',
    badgeClassName: 'bg-blue-50 text-blue-700',
    icon: CircleDashed,
    iconClassName: 'text-blue-600',
    ringClassName: 'border-blue-200 bg-blue-50 shadow-[0_0_0_6px_rgba(59,130,246,0.08)]',
    cardClassName:
      'border-blue-200 bg-gradient-to-br from-blue-50/90 via-white to-sky-50/70 shadow-[0_18px_36px_-30px_rgba(59,130,246,0.65)]',
  },
  completed: {
    label: '已完成',
    badgeClassName: 'bg-emerald-50 text-emerald-700',
    icon: CheckCircle2,
    iconClassName: 'text-emerald-600',
    ringClassName: 'border-emerald-200 bg-emerald-50',
    cardClassName:
      'border-emerald-200/80 bg-gradient-to-br from-emerald-50/90 via-white to-emerald-50/60 hover:border-emerald-300',
  },
  failed: {
    label: '异常',
    badgeClassName: 'bg-red-50 text-red-700',
    icon: OctagonX,
    iconClassName: 'text-red-600',
    ringClassName: 'border-red-200 bg-red-50',
    cardClassName:
      'border-red-200/80 bg-gradient-to-br from-red-50/90 via-white to-red-50/60 hover:border-red-300',
  },
  cancelled: {
    label: '已停止',
    badgeClassName: 'bg-slate-100 text-slate-700',
    icon: Pause,
    iconClassName: 'text-slate-600',
    ringClassName: 'border-slate-200 bg-slate-100',
    cardClassName:
      'border-slate-200 bg-gradient-to-br from-slate-50/95 via-white to-slate-100/70 hover:border-slate-300',
  },
  timeout: {
    label: '已超时',
    badgeClassName: 'bg-amber-50 text-amber-700',
    icon: AlertTriangle,
    iconClassName: 'text-amber-600',
    ringClassName: 'border-amber-200 bg-amber-50',
    cardClassName:
      'border-amber-200/80 bg-gradient-to-br from-amber-50/90 via-white to-amber-50/65 hover:border-amber-300',
  },
  denied: {
    label: '已跳过',
    badgeClassName: 'bg-slate-100 text-slate-600',
    icon: Pause,
    iconClassName: 'text-slate-500',
    ringClassName: 'border-slate-200 bg-slate-100',
    cardClassName:
      'border-slate-200 bg-gradient-to-br from-slate-50/95 via-white to-slate-100/60 hover:border-slate-300',
  },
};

const truncateText = (value: string | null | undefined, maxLength = 92): string => {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .replace(/[<>]/g, '')
    .trim();

  if (!normalized) {
    return '';
  }

  if (normalized.length <= maxLength) {
    return normalized;
  }

  return `${normalized.slice(0, maxLength - 1)}…`;
};

const summarizeToolCall = (toolCall: {
  assistantCall?: { name?: string; content?: string };
  toolResult?: { content?: string };
}): string => {
  const toolName = String(toolCall.assistantCall?.name || '工具').trim();
  const toolResult = String(toolCall.toolResult?.content || '').trim();
  const toolArguments = truncateText(toolCall.assistantCall?.content, 72);

  if (toolResult === 'STREAMING') {
    return `正在执行 ${toolName}`;
  }

  if (toolResult) {
    return `${toolName}: ${truncateText(toolResult, 72)}`;
  }

  if (toolArguments) {
    return `${toolName}: ${toolArguments}`;
  }

  return `调用 ${toolName}`;
};

const ACTIVE_RECOVERY_PHASES = new Set([
  'wake_requested',
  'wake_dispatched',
  'replacement_requested',
  'replacement_started',
]);

const isActiveRecoveryPhase = (phase: string | null | undefined): boolean =>
  ACTIVE_RECOVERY_PHASES.has(String(phase || '').trim());

const getSubtaskVisualStatus = (
  subtask: ShadowCloneSubtask,
): ShadowCloneSubtaskStatus =>
  isActiveRecoveryPhase(subtask.recovery_phase) &&
  subtask.status !== 'completed' &&
  subtask.status !== 'cancelled' &&
  subtask.status !== 'timeout' &&
  subtask.status !== 'denied'
    ? 'running'
    : subtask.status;

const getRecoveryLabel = (subtask: ShadowCloneSubtask): string | null => {
  switch (String(subtask.recovery_phase || '').trim()) {
    case 'wake_requested':
      return '恢复中';
    case 'wake_dispatched':
      return '已唤醒';
    case 'replacement_requested':
      return '准备换人';
    case 'replacement_started':
      return '替补接管';
    case 'replacement_succeeded':
      return '替补完成';
    default:
      return null;
  }
};

const getRecoveryHeadline = (
  subtask: ShadowCloneSubtask,
  latestLabel: string | null | undefined,
): string | null => {
  const recoveryPhase = String(subtask.recovery_phase || '').trim();
  const recoveryReason = truncateText(
    subtask.recovery_reason || subtask.error || subtask.result_summary,
    72,
  );

  switch (recoveryPhase) {
    case 'wake_requested':
      return truncateText(
        recoveryReason || latestLabel || '系统正在唤醒原子智能体，准备沿用原上下文继续执行',
        72,
      );
    case 'wake_dispatched':
      return truncateText(
        latestLabel || '已向原子智能体发送恢复指令，等待继续执行',
        72,
      );
    case 'replacement_requested':
      return truncateText(
        recoveryReason || '原子智能体暂未恢复，正在准备替补接管',
        72,
      );
    case 'replacement_started':
      return truncateText(
        latestLabel || '替补子智能体已接管，正在根据交接摘要继续任务',
        72,
      );
    case 'recovery_exhausted':
      return truncateText(
        recoveryReason || '恢复尝试已耗尽，等待上层进一步处理',
        72,
      );
    default:
      return null;
  }
};

const getHeadline = (
  subtask: ShadowCloneSubtask,
  latestLabel: string | null | undefined,
): string => {
  const recoveryHeadline = getRecoveryHeadline(subtask, latestLabel);
  if (recoveryHeadline) {
    return recoveryHeadline;
  }

  if (subtask.status === 'failed') {
    return truncateText(subtask.error || subtask.result_summary || '执行出现异常', 72);
  }

  if (subtask.status === 'completed') {
    return truncateText(subtask.result_summary || latestLabel || '任务已完成', 72);
  }

  if (subtask.status === 'cancelled') {
    return truncateText(
      subtask.result_summary || subtask.error || '任务已停止，已生成结果仍可查看',
      72,
    );
  }

  if (subtask.status === 'timeout') {
    return truncateText(
      subtask.result_summary || subtask.error || '任务因超时结束',
      72,
    );
  }

  if (subtask.status === 'denied') {
    return truncateText(
      subtask.result_summary || subtask.error || '任务被跳过，未进入执行',
      72,
    );
  }

  if (subtask.status === 'running') {
    return truncateText(
      latestLabel || subtask.result_summary || subtask.task_description || '正在处理子任务',
      72,
    );
  }

  return truncateText(
    latestLabel || subtask.task_description || '等待主智能体调度',
    72,
  );
};

const buildProgressNotice = ({
  environmentStatus,
  environmentLastError,
  lastError,
}: {
  environmentStatus: string | null;
  environmentLastError: string | null;
  lastError: string | null;
}): ShadowCloneNotice | null => {
  if (environmentStatus === 'recovering') {
    return {
      tone: 'warning',
      text:
        environmentLastError || '共享环境恢复中，当前继续保留最近一次成功状态。',
    };
  }

  if (environmentStatus === 'failed') {
    return {
      tone: 'error',
      text: environmentLastError || '共享环境出现异常，正在等待修复。',
    };
  }

  if (lastError) {
    return {
      tone: 'warning',
      text: `状态同步短暂异常，当前展示最近一次成功同步的数据。${lastError}`,
    };
  }

  return null;
};

const readActiveRecoverySummary = (subtasks: ShadowCloneSubtask[]): string | null => {
  const recoveringSubtask = subtasks.find((item) =>
    isActiveRecoveryPhase(item.recovery_phase),
  );
  if (!recoveringSubtask) {
    return null;
  }

  const roleLabel = recoveringSubtask.role || recoveringSubtask.id;
  switch (String(recoveringSubtask.recovery_phase || '').trim()) {
    case 'wake_requested':
      return `正在唤醒 ${roleLabel}，优先保留原上下文继续执行`;
    case 'wake_dispatched':
      return `已向 ${roleLabel} 发送恢复指令，等待恢复执行`;
    case 'replacement_requested':
      return `原 ${roleLabel} 暂未恢复，正在整理交接信息`;
    case 'replacement_started':
      return `替补已接管 ${roleLabel} 的任务，正在延续执行`;
    default:
      return null;
  }
};

function StatusIndicator({
  status,
  isActive,
}: {
  status: ShadowCloneSubtaskStatus;
  isActive: boolean;
}) {
  const meta = STATUS_META[status];
  const Icon = meta.icon;

  return (
    <div
      className={cn(
        'flex h-11 w-11 items-center justify-center rounded-full border-2 transition-all duration-200',
        meta.ringClassName,
        isActive && 'scale-[1.03] shadow-[0_0_0_4px_rgba(37,99,235,0.12)]',
      )}
    >
      {status === 'running' ? (
        <ShadowCloneRunningSpinner size="sm" className="text-blue-600" />
      ) : (
        <Icon className={cn('h-4 w-4', meta.iconClassName)} />
      )}
    </div>
  );
}

const ProgressSubtaskCard = React.memo(function ProgressSubtaskCard({
  subtaskId,
  onSelect,
}: {
  subtaskId: string;
  onSelect: (subtaskId: string) => void;
}) {
  const { subtask, toolCalls, latestLabel, isActive } = useShadowCloneStore(
    useShallow((state) => ({
      subtask: state.subtasks.find((item) => item.id === subtaskId) || null,
      toolCalls:
        state.subtaskPanelStates[subtaskId]?.toolCalls ?? EMPTY_TOOL_CALLS,
      latestLabel: state.subtaskPanelStates[subtaskId]?.latestLabel || null,
      isActive: state.activeSubtaskId === subtaskId,
    })),
  );

  const recentActivity = React.useMemo(
    () =>
      toolCalls
        .slice(-5)
        .map(summarizeToolCall)
        .filter(Boolean)
        .slice(-5)
        .reverse(),
    [toolCalls],
  );

  const headline = React.useMemo(() => {
    if (!subtask) {
      return '';
    }

    return getHeadline(subtask, latestLabel);
  }, [latestLabel, subtask]);

  if (!subtask) {
    return null;
  }

  const visualStatus = getSubtaskVisualStatus(subtask);
  const statusMeta = STATUS_META[visualStatus];
  const recoveryLabel = getRecoveryLabel(subtask);
  const displayTitle = subtask.role || subtask.id;
  const secondaryLabel =
    subtask.role && subtask.role !== subtask.id ? subtask.id : null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={() => onSelect(subtask.id)}
          className={cn(
            'group relative min-h-[104px] w-full cursor-pointer rounded-2xl border px-3 py-3 text-left transition-all duration-200',
            'hover:-translate-y-0.5 hover:shadow-[0_18px_36px_-30px_rgba(15,23,42,0.35)]',
            statusMeta.cardClassName,
            isActive && 'border-blue-300 shadow-[0_0_0_4px_rgba(37,99,235,0.12)]',
          )}
        >
          <ExternalLink className="absolute right-3 top-3 h-3.5 w-3.5 text-slate-300 transition-colors duration-200 group-hover:text-slate-500" />

          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1 space-y-2">
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-semibold text-slate-900">
                    {displayTitle}
                  </span>
                  {secondaryLabel && (
                    <span className="rounded-full bg-white/80 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                      {secondaryLabel}
                    </span>
                  )}
                </div>
                <p className="truncate text-xs font-medium text-slate-600">
                  {headline || '暂无状态更新'}
                </p>
              </div>

              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium',
                    statusMeta.badgeClassName,
                  )}
                >
                  {recoveryLabel || statusMeta.label}
                </span>
                {latestLabel && visualStatus === 'running' && (
                  <span className="truncate text-[11px] text-blue-600">
                    {truncateText(latestLabel, 32)}
                  </span>
                )}
              </div>

              {recentActivity.length > 0 && (
                <div className="space-y-1">
                  {recentActivity.slice(0, 2).map((entry, index) => (
                    <div
                      key={`${subtask.id}-visible-activity-${index}`}
                      data-testid="shadow-clone-recent-tool-call"
                      className="truncate rounded-lg bg-white/75 px-2 py-1 text-[11px] leading-4 text-slate-500"
                    >
                      {entry}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="shrink-0 pt-0.5">
              <StatusIndicator status={visualStatus} isActive={isActive} />
            </div>
          </div>
        </button>
      </TooltipTrigger>

      <TooltipContent
        side="top"
        align="start"
        sideOffset={10}
        className="z-[95] w-[min(360px,calc(100vw-1.5rem))] rounded-2xl border border-slate-200 bg-white p-0 text-left shadow-[0_24px_60px_-28px_rgba(15,23,42,0.45)]"
      >
        <div className="border-b border-slate-200/80 px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-900">
                {displayTitle}
              </p>
              {secondaryLabel && (
                <p className="mt-1 text-xs text-slate-500">{secondaryLabel}</p>
              )}
            </div>
            <span
              className={cn(
                'inline-flex rounded-full px-2.5 py-1 text-[11px] font-medium',
                statusMeta.badgeClassName,
              )}
            >
              {statusMeta.label}
            </span>
          </div>
        </div>

        <div className="space-y-3 px-4 py-3">
          <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                当前状态
              </div>
              <p className="text-xs leading-5 text-slate-600">
                {headline || '暂无状态更新'}
              </p>
            </div>

          {(subtask.recovery_phase || subtask.recovery_handoff_summary) && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                恢复进度
              </div>
              <p className="text-xs leading-5 text-slate-600">
                {getRecoveryHeadline(subtask, latestLabel) ||
                  subtask.recovery_reason ||
                  '系统正在处理中断后的恢复流程'}
              </p>
              {subtask.recovery_handoff_summary && (
                <p className="mt-2 text-xs leading-5 text-slate-500">
                  {truncateText(subtask.recovery_handoff_summary, 180)}
                </p>
              )}
            </div>
          )}

          <div>
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">
              任务描述
            </div>
            <p className="text-xs leading-5 text-slate-600">
              {subtask.task_description || '暂无任务描述'}
            </p>
          </div>

          {recentActivity.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                最近日志
              </div>
              <div className="space-y-1.5">
                {recentActivity.map((entry, index) => (
                  <div
                    key={`${subtask.id}-activity-${index}`}
                    className="rounded-xl bg-slate-50 px-2.5 py-2 text-xs leading-5 text-slate-600"
                  >
                    {entry}
                  </div>
                ))}
              </div>
            </div>
          )}

          {(subtask.result_summary || subtask.error) && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                结果摘要
              </div>
              <p
                className={cn(
                  'text-xs leading-5',
                  subtask.status === 'failed'
                    ? 'text-red-600'
                    : subtask.status === 'timeout'
                      ? 'text-amber-700'
                      : 'text-slate-600',
                )}
              >
                {subtask.error || subtask.result_summary}
              </p>
            </div>
          )}

          <div className="flex items-center justify-between border-t border-slate-200/80 pt-3 text-[11px] text-slate-400">
            <span>点击卡片进入完整子智能体视图</span>
            <ExternalLink className="h-3.5 w-3.5" />
          </div>
        </div>
      </TooltipContent>
    </Tooltip>
  );
});

const ProgressSubtaskGrid = React.memo(function ProgressSubtaskGrid({
  subtaskIds,
  onSelect,
}: {
  subtaskIds: string[];
  onSelect: (subtaskId: string) => void;
}) {
  return (
    <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
      {subtaskIds.map((subtaskId) => (
        <ProgressSubtaskCard
          key={subtaskId}
          subtaskId={subtaskId}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
});

export function ShadowCloneProgress({
  agentRunId,
  onSubtaskSelect,
}: ShadowCloneProgressProps) {
  const currentRunId = useShadowCloneStore((state) => state.currentRunId);
  const phase = useShadowCloneStore((state) => state.phase);
  const subtasks = useShadowCloneStore((state) => state.subtasks);
  const lastHeartbeatAt = useShadowCloneStore((state) => state.lastHeartbeatAt);
  const lastSyncedAt = useShadowCloneStore((state) => state.lastSyncedAt);
  const resetRuntime = useShadowCloneStore((state) => state.resetRuntime);
  const isSyncing = useShadowCloneStore((state) => state.isSyncing);
  const lastError = useShadowCloneStore((state) => state.lastError);
  const environmentStatus = useShadowCloneStore((state) => state.environmentStatus);
  const environmentLastError = useShadowCloneStore(
    (state) => state.environmentLastError,
  );
  const syncRunData = useShadowCloneStore((state) => state.syncRunData);

  const [manuallyCollapsedRunKey, setManuallyCollapsedRunKey] = useState<string | null>(null);
  const [expandedCompletionKey, setExpandedCompletionKey] = useState<string | null>(null);
  const isBoundRun = !currentRunId || currentRunId === agentRunId;

  const shouldShow =
    isBoundRun &&
    subtasks.length > 0 &&
    (
      phase === 'running' ||
      phase === 'recovering' ||
      phase === 'aggregating' ||
      phase === 'completed' ||
      phase === 'cancelled' ||
      phase === 'timeout'
    );

  useEffect(() => {
    if (!shouldShow || !agentRunId || !ACTIVE_SYNC_PHASES.has(phase) || !isBoundRun) return;
    void syncRunData(agentRunId);
    const interval = window.setInterval(() => {
      void syncRunData(agentRunId);
    }, AUTHORITATIVE_SYNC_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [agentRunId, isBoundRun, shouldShow, phase, syncRunData]);

  useEffect(() => {
    if (
      !shouldShow ||
      !agentRunId ||
      !ACTIVE_SYNC_PHASES.has(phase) ||
      isSyncing ||
      !isBoundRun
    ) {
      return;
    }
    const freshnessAnchor = lastHeartbeatAt || lastSyncedAt;
    if (!freshnessAnchor) {
      return;
    }
    if (Date.now() - freshnessAnchor < STALE_SYNC_THRESHOLD_MS) {
      return;
    }
    void syncRunData(agentRunId);
  }, [
    agentRunId,
    isSyncing,
    lastHeartbeatAt,
    lastSyncedAt,
    phase,
    shouldShow,
    syncRunData,
    isBoundRun,
  ]);

  const completedCount = subtasks.filter((item) => item.status === 'completed').length;
  const failedCount = subtasks.filter((item) => item.status === 'failed').length;
  const cancelledCount = subtasks.filter((item) => item.status === 'cancelled').length;
  const timeoutCount = subtasks.filter((item) => item.status === 'timeout').length;
  const deniedCount = subtasks.filter((item) => item.status === 'denied').length;
  const runningCount = subtasks.filter((item) => item.status === 'running').length;
  const waitingCount = subtasks.filter((item) => item.status === 'pending').length;
  const total = subtasks.length;
  const resolvedCount =
    completedCount + failedCount + cancelledCount + timeoutCount + deniedCount;
  const progressValue = total > 0 ? (resolvedCount / total) * 100 : 0;
  const allCompleted = total > 0 && resolvedCount === total;
  const heartbeatText = lastHeartbeatAt
    ? new Date(lastHeartbeatAt).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '暂无';
  const surfacedNotice = buildProgressNotice({
    environmentStatus,
    environmentLastError,
    lastError,
  });
  const activeRecoverySummary = readActiveRecoverySummary(subtasks);
  const subtaskIdsKey = React.useMemo(
    () => JSON.stringify(subtasks.map((subtask) => subtask.id)),
    [subtasks],
  );
  const subtaskIds = React.useMemo<string[]>(
    () => JSON.parse(subtaskIdsKey) as string[],
    [subtaskIdsKey],
  );

  const handleSubtaskSelect = React.useCallback(
    (_subtaskId: string) => {
      onSubtaskSelect?.();
    },
    [onSubtaskSelect],
  );

  if (!shouldShow) return null;

  const currentRunKey = agentRunId || `shadow-clone-${total}`;
  const completionKey =
    isClosedShadowClonePhase(phase) && total > 0
      ? `${currentRunKey}:${resolvedCount}:${total}`
      : null;
  const isCollapsed =
    manuallyCollapsedRunKey === currentRunKey ||
    Boolean(completionKey && expandedCompletionKey !== completionKey);

  const handleExpand = () => {
    setManuallyCollapsedRunKey(null);
    if (completionKey) {
      setExpandedCompletionKey(completionKey);
    }
  };

  const handleCollapse = () => {
    setManuallyCollapsedRunKey(currentRunKey);
  };

  const handleClose = () => {
    setManuallyCollapsedRunKey(null);
    setExpandedCompletionKey(null);
    resetRuntime();
  };

  const statusSummary =
    phase === 'cancelled'
      ? '影分身执行已停止，已产出的结果仍可继续查看'
      : phase === 'timeout'
      ? '影分身执行超时结束，已产出的结果仍可继续查看'
      : phase === 'recovering'
      ? activeRecoverySummary ||
        (environmentStatus === 'recovering'
          ? surfacedNotice?.text || '共享环境恢复中，准备重新启动当前层任务'
          : '系统正在恢复中断的子任务执行')
      : phase === 'aggregating'
      ? '主智能体正在汇总最终报告'
      : environmentStatus === 'failed'
        ? surfacedNotice?.text || '共享环境出现异常，正在等待修复'
      : allCompleted
        ? failedCount > 0
          ? `全部 ${total} 个任务已结束，其中 ${failedCount} 个异常`
          : cancelledCount > 0 || timeoutCount > 0 || deniedCount > 0
            ? `全部 ${total} 个任务已结束`
            : `全部 ${total} 个任务已完成`
        : runningCount > 0
          ? `${runningCount} 个子任务正在执行`
          : waitingCount > 0
            ? `${waitingCount} 个子任务等待调度`
            : '正在同步影分身状态';

  if (isCollapsed) {
    return (
      <div className="mb-3 overflow-hidden rounded-2xl border border-slate-200 bg-white/95 shadow-[0_18px_40px_-28px_rgba(15,23,42,0.35)] backdrop-blur-sm">
        <div className="h-1.5 bg-slate-100">
          <div
            className="h-full rounded-full bg-gradient-to-r from-blue-600 via-sky-500 to-emerald-500 transition-all duration-300"
            style={{ width: `${progressValue}%` }}
          />
        </div>
        <div className="flex items-center justify-between gap-3 px-4 py-3">
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className="bg-blue-600 text-white hover:bg-blue-600">
                影分身模式
              </Badge>
              <span className="text-sm font-semibold text-slate-900">
                {resolvedCount}/{total} 已结束
              </span>
              {isSyncing && (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-blue-700">
                  <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
                  同步中
                </span>
              )}
            </div>
            <p className="truncate text-xs leading-5 text-slate-500">{statusSummary}</p>
          </div>

          <div className="flex items-center gap-2">
            <span className="hidden text-xs text-slate-400 sm:inline">心跳：{heartbeatText}</span>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 rounded-xl px-2 text-xs text-slate-600 transition-all duration-200 hover:bg-slate-100 hover:text-slate-900"
              onClick={handleExpand}
            >
              <ChevronDown className="mr-1 h-3.5 w-3.5 rotate-180" />
              展开
            </Button>
            {isClosedShadowClonePhase(phase) && (
              <Button
                variant="ghost"
                size="sm"
                className="h-8 rounded-xl px-2 text-xs text-slate-600 transition-all duration-200 hover:bg-slate-100 hover:text-slate-900"
                onClick={handleClose}
              >
                <X className="mr-1 h-3.5 w-3.5" />
                关闭
              </Button>
            )}
          </div>
        </div>

        {surfacedNotice && (
          <div
            className={cn(
              'border-t px-4 py-2 text-xs',
              surfacedNotice.tone === 'error'
                ? 'border-red-100 bg-red-50 text-red-700'
                : 'border-amber-100 bg-amber-50 text-amber-800',
            )}
          >
            {surfacedNotice.text}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="mb-3 overflow-hidden rounded-3xl border border-slate-200 bg-white/95 shadow-[0_18px_40px_-28px_rgba(15,23,42,0.35)] backdrop-blur-sm">
      <div className="h-1.5 bg-slate-100">
        <div
          className="h-full rounded-full bg-gradient-to-r from-blue-600 via-sky-500 to-emerald-500 transition-all duration-300"
          style={{ width: `${progressValue}%` }}
        />
      </div>

      <div className="border-b border-slate-200/80 px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className="bg-blue-600 text-white hover:bg-blue-600">
                影分身模式
              </Badge>
              <span className="text-sm font-semibold text-slate-900">
                {resolvedCount}/{total} 已结束
              </span>
              <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                看板视图
              </span>
              {isSyncing && (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-blue-700">
                  <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
                  同步中
                </span>
              )}
            </div>
            <p className="text-xs leading-5 text-slate-500">{statusSummary}</p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="text-right text-xs text-slate-500">
              <div>心跳：{heartbeatText}</div>
              {phase === 'aggregating' && (
                <div className="mt-1 inline-flex items-center gap-1 font-medium text-blue-700">
                  <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
                  正在汇总最终报告
                </div>
              )}
              {phase === 'recovering' && (
                <div className="mt-1 inline-flex items-center gap-1 font-medium text-amber-700">
                  <ShadowCloneRunningSpinner size="xs" className="text-amber-600" />
                  {activeRecoverySummary || '正在恢复共享环境'}
                </div>
              )}
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 rounded-xl px-2 text-xs text-slate-600 transition-all duration-200 hover:bg-slate-100 hover:text-slate-900"
              onClick={handleCollapse}
            >
              <ChevronDown className="mr-1 h-3.5 w-3.5" />
              收起
            </Button>
            {isClosedShadowClonePhase(phase) && (
              <Button
                variant="ghost"
                size="sm"
                className="h-8 rounded-xl px-2 text-xs text-slate-600 transition-all duration-200 hover:bg-slate-100 hover:text-slate-900"
                onClick={handleClose}
              >
                <X className="mr-1 h-3.5 w-3.5" />
                关闭
              </Button>
            )}
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded-full bg-blue-50 px-2.5 py-1 font-medium text-blue-700">
            运行中 {runningCount}
          </span>
          <span className="rounded-full bg-slate-100 px-2.5 py-1 font-medium text-slate-600">
            等待中 {waitingCount}
          </span>
          <span className="rounded-full bg-emerald-50 px-2.5 py-1 font-medium text-emerald-700">
            已完成 {completedCount}
          </span>
          {failedCount > 0 && (
            <span className="rounded-full bg-red-50 px-2.5 py-1 font-medium text-red-700">
              异常 {failedCount}
            </span>
          )}
          {cancelledCount > 0 && (
            <span className="rounded-full bg-slate-100 px-2.5 py-1 font-medium text-slate-700">
              已停止 {cancelledCount}
            </span>
          )}
          {timeoutCount > 0 && (
            <span className="rounded-full bg-amber-50 px-2.5 py-1 font-medium text-amber-700">
              已超时 {timeoutCount}
            </span>
          )}
          {deniedCount > 0 && (
            <span className="rounded-full bg-slate-100 px-2.5 py-1 font-medium text-slate-600">
              已跳过 {deniedCount}
            </span>
          )}
        </div>
      </div>

      <div className="max-h-[19rem] overflow-y-auto px-3 py-3">
        <ProgressSubtaskGrid
          subtaskIds={subtaskIds}
          onSelect={handleSubtaskSelect}
        />
      </div>

      {surfacedNotice && (
        <div
          className={cn(
            'border-t px-4 py-2 text-xs',
            surfacedNotice.tone === 'error'
              ? 'border-red-100 bg-red-50 text-red-700'
              : 'border-amber-100 bg-amber-50 text-amber-800',
          )}
        >
          {surfacedNotice.text}
        </div>
      )}
    </div>
  );
}
