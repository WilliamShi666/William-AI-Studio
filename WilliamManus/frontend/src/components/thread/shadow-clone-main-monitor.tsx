'use client';

import React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  HardDrive,
  OctagonX,
  Pause,
  Play,
  Radio,
  RefreshCcw,
} from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';

import { safeJsonParse } from '@/components/thread/utils';
import type { ApiMessageType } from '@/components/thread/types';
import { Button } from '@/components/ui/button';
import { Slider } from '@/components/ui/slider';
import { cn } from '@/lib/utils';
import {
  useShadowCloneStore,
  type ShadowCloneLiveActivity,
  type ShadowClonePhase,
  type ShadowCloneSubtask,
  type ShadowCloneSubtaskStatus,
} from '@/lib/stores/shadow-clone-store';
import { ShadowCloneRunningSpinner } from './shadow-clone-running-spinner';

const ACTIVE_PHASES: ShadowClonePhase[] = [
  'preparing',
  'confirming',
  'recovering',
  'running',
  'aggregating',
  'completed',
  'cancelled',
  'timeout',
];

const STATUS_META: Record<
  ShadowCloneSubtaskStatus,
  {
    badge: string;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    badgeClassName: string;
    iconClassName: string;
    dotClassName: string;
    rowAccentClassName: string;
  }
> = {
  pending: {
    badge: 'WAIT',
    label: 'Pending',
    icon: CircleDashed,
    badgeClassName: 'border-slate-200 bg-slate-100 text-slate-600',
    iconClassName: 'text-slate-400',
    dotClassName: 'bg-slate-400',
    rowAccentClassName: 'hover:bg-slate-100/90',
  },
  running: {
    badge: 'BUSY',
    label: 'Running',
    icon: CircleDashed,
    badgeClassName: 'border-blue-200 bg-blue-50 text-blue-700',
    iconClassName: 'text-blue-600',
    dotClassName: 'bg-blue-500 shadow-[0_0_0_4px_rgba(59,130,246,0.14)]',
    rowAccentClassName: 'bg-blue-50/65 hover:bg-blue-50/90',
  },
  completed: {
    badge: 'DONE',
    label: 'Completed',
    icon: CheckCircle2,
    badgeClassName: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    iconClassName: 'text-emerald-600',
    dotClassName: 'bg-emerald-500',
    rowAccentClassName: 'hover:bg-emerald-50/45',
  },
  failed: {
    badge: 'FAIL',
    label: 'Failed',
    icon: OctagonX,
    badgeClassName: 'border-red-200 bg-red-50 text-red-700',
    iconClassName: 'text-red-600',
    dotClassName: 'bg-red-500 shadow-[0_0_0_4px_rgba(239,68,68,0.12)]',
    rowAccentClassName: 'bg-red-50/60 hover:bg-red-50/80',
  },
  cancelled: {
    badge: 'STOP',
    label: 'Stopped',
    icon: Pause,
    badgeClassName: 'border-slate-200 bg-slate-100 text-slate-700',
    iconClassName: 'text-slate-600',
    dotClassName: 'bg-slate-500',
    rowAccentClassName: 'hover:bg-slate-100/90',
  },
  timeout: {
    badge: 'TIME',
    label: 'Timed out',
    icon: AlertTriangle,
    badgeClassName: 'border-amber-200 bg-amber-50 text-amber-700',
    iconClassName: 'text-amber-600',
    dotClassName: 'bg-amber-500',
    rowAccentClassName: 'bg-amber-50/55 hover:bg-amber-50/80',
  },
  denied: {
    badge: 'SKIP',
    label: 'Skipped',
    icon: Pause,
    badgeClassName: 'border-slate-200 bg-slate-100 text-slate-600',
    iconClassName: 'text-slate-500',
    dotClassName: 'bg-slate-400',
    rowAccentClassName: 'hover:bg-slate-100/85',
  },
};

interface ShadowCloneMainMonitorProps {
  isZh?: boolean;
  messages?: ApiMessageType[];
}

export interface ShadowCloneMonitorRow {
  id: string;
  index: number;
  role: string;
  taskSummary: string;
  detail: string;
  status: ShadowCloneSubtaskStatus;
  visualStatus: ShadowCloneSubtaskStatus;
  agentName?: string;
  agentId?: string;
}

interface MonitorSummaryModel {
  taskSummary: string;
  footer: string;
  completedCount: number;
  failedCount: number;
  resolvedCount: number;
  totalCount: number;
  progressValue: number;
  lastUpdatedLabel: string;
  liveLabel: string;
  syncLabel: string;
  syncTone: 'live' | 'syncing' | 'stale' | 'error';
  environmentLabel: string;
  continuityLabel: string | null;
  preparedLabel: string | null;
  refreshLabel: string | null;
  environmentNotice: {
    tone: 'warning' | 'error';
    text: string;
  } | null;
}

interface FrozenMonitorModel extends MonitorSummaryModel {
  rows: ShadowCloneMonitorRow[];
}

interface MonitorRowDetailSnapshot {
  streamingToolName: string;
  streamingTextContent: string;
  streamingReasoningContent: string;
  transcriptLatestLabel: string | null;
  panelLatestLabel: string | null;
}

interface MainTranscriptSnapshot {
  streamingToolName: string;
  streamingTextContent: string;
  streamingReasoningContent: string;
}

interface MainAgentLivePanelModel {
  eyebrow: string;
  title: string;
  detail: string;
  isLive: boolean;
}

type StreamingToolCallSnapshotSource =
  | {
      name?: string;
      xml_tag_name?: string;
    }
  | null
  | undefined;

type TranscriptSnapshotSource =
  | {
      streamingToolCall?: StreamingToolCallSnapshotSource;
      streamingTextContent?: string;
      streamingReasoningContent?: string;
      latestLabel?: string | null;
    }
  | undefined;

type PanelSnapshotSource =
  | {
      latestLabel?: string | null;
    }
  | undefined;

const truncate = (value: string | null | undefined, maxLength = 96): string => {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!normalized) return '';
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength - 1)}...`;
};

const truncateTail = (
  value: string | null | undefined,
  maxLength = 220,
): string => {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!normalized) return '';
  if (normalized.length <= maxLength) return normalized;
  return `...${normalized.slice(normalized.length - maxLength + 3)}`;
};

const ACTIVE_RECOVERY_PHASES = new Set([
  'wake_requested',
  'wake_dispatched',
  'replacement_requested',
  'replacement_started',
]);

const isActiveRecoveryPhase = (phase: string | null | undefined): boolean =>
  ACTIVE_RECOVERY_PHASES.has(String(phase || '').trim());

const getVisualSubtaskStatus = (
  subtask: ShadowCloneSubtask,
): ShadowCloneSubtaskStatus =>
  isActiveRecoveryPhase(subtask.recovery_phase) &&
  subtask.status !== 'completed' &&
  subtask.status !== 'cancelled' &&
  subtask.status !== 'timeout' &&
  subtask.status !== 'denied'
    ? 'running'
    : subtask.status;

const describeSubtaskRecovery = (
  subtask: ShadowCloneSubtask,
  isZh: boolean,
): string | null => {
  const recoveryPhase = String(subtask.recovery_phase || '').trim();
  const reason = truncate(
    subtask.recovery_reason || subtask.error || subtask.result_summary,
    92,
  );

  switch (recoveryPhase) {
    case 'wake_requested':
      return (
        reason ||
        (isZh
          ? '正在唤醒原子智能体，优先保留已有上下文继续执行'
          : 'Waking the original subagent to preserve its context.')
      );
    case 'wake_dispatched':
      return isZh
        ? '已向原子智能体发送恢复指令，等待继续执行'
        : 'Recovery instructions were delivered to the original subagent.';
    case 'replacement_requested':
      return (
        reason ||
        (isZh
          ? '原子智能体暂未恢复，正在整理失败原因和交接摘要'
          : 'The original subagent did not recover; preparing the handoff.')
      );
    case 'replacement_started':
      return isZh
        ? '替补子智能体已接管，正在根据交接摘要继续任务'
        : 'A replacement subagent has taken over with the handoff context.';
    case 'replacement_succeeded':
      return isZh
        ? '替补子智能体已完成接管任务'
        : 'The replacement subagent completed the takeover.';
    case 'recovery_exhausted':
      return (
        reason ||
        (isZh
          ? '恢复尝试已耗尽，等待更高层进一步处理'
          : 'Recovery attempts were exhausted.')
      );
    default:
      return null;
  }
};

const readRecoveryLiveLabel = (
  subtasks: ShadowCloneSubtask[],
  isZh: boolean,
): string | null => {
  const recoveringSubtask = subtasks.find((item) =>
    isActiveRecoveryPhase(item.recovery_phase),
  );
  if (!recoveringSubtask) {
    return null;
  }

  const recoveryPhase = String(recoveringSubtask.recovery_phase || '').trim();
  if (
    recoveryPhase === 'replacement_requested' ||
    recoveryPhase === 'replacement_started'
  ) {
    return isZh ? '替补接管中' : 'Replacement takeover';
  }
  return isZh ? '唤醒原分身' : 'Waking original clone';
};

const readMessageText = (content: string | null | undefined): string => {
  if (!content) return '';
  const parsed = safeJsonParse<{ content?: string } | string>(content, content);
  if (typeof parsed === 'string') {
    return parsed;
  }
  if (parsed && typeof parsed === 'object' && typeof parsed.content === 'string') {
    return parsed.content;
  }
  return '';
};

const pickRunningTask = (
  messages: ApiMessageType[] | undefined,
  isZh: boolean,
): string => {
  const lastUserMessage = (messages || [])
    .filter((message) => message?.type === 'user')
    .at(-1);
  const text = truncate(readMessageText(lastUserMessage?.content), 120);
  if (text) return text;
  return isZh ? '正在执行影分身任务' : 'Shadow Clone task in progress';
};

const formatToolName = (value: string | null | undefined): string =>
  String(value || '')
    .trim()
    .replace(/[_-]+/g, ' ');

const readStreamingToolName = (
  streamingToolCall: StreamingToolCallSnapshotSource,
): string =>
  typeof streamingToolCall?.name === 'string'
    ? streamingToolCall.name
    : typeof streamingToolCall?.xml_tag_name === 'string'
      ? streamingToolCall.xml_tag_name
      : '';

const buildMonitorRowDetailSnapshot = (
  transcriptState: TranscriptSnapshotSource,
  panelState: PanelSnapshotSource,
): MonitorRowDetailSnapshot => ({
  streamingToolName: readStreamingToolName(transcriptState?.streamingToolCall),
  streamingTextContent:
    typeof transcriptState?.streamingTextContent === 'string'
      ? transcriptState.streamingTextContent
      : '',
  streamingReasoningContent:
    typeof transcriptState?.streamingReasoningContent === 'string'
      ? transcriptState.streamingReasoningContent
      : '',
  transcriptLatestLabel: transcriptState?.latestLabel || null,
  panelLatestLabel: panelState?.latestLabel || null,
});

const buildMainTranscriptSnapshot = (
  transcriptState: TranscriptSnapshotSource,
): MainTranscriptSnapshot => ({
  streamingToolName: readStreamingToolName(transcriptState?.streamingToolCall),
  streamingTextContent:
    typeof transcriptState?.streamingTextContent === 'string'
      ? transcriptState.streamingTextContent
      : '',
  streamingReasoningContent:
    typeof transcriptState?.streamingReasoningContent === 'string'
      ? transcriptState.streamingReasoningContent
      : '',
});

const formatPreparedAt = (value: string | null | undefined, isZh: boolean): string | null => {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleTimeString(isZh ? 'zh-CN' : 'en-US', {
    hour: '2-digit',
    minute: '2-digit',
  });
};

const formatEnvironmentLabel = (
  environmentStatus: string | null,
  environmentReady: boolean,
  environmentConfirmationReady: boolean,
  isZh: boolean,
): string => {
  switch (environmentStatus) {
    case 'preparing':
      return isZh ? '环境准备中' : 'Preparing environment';
    case 'recovering':
      return isZh ? '环境恢复中' : 'Recovering environment';
    case 'failed':
      return isZh ? '环境异常' : 'Environment failed';
    case 'ready':
      if (environmentReady && environmentConfirmationReady) {
        return isZh ? '环境已就绪' : 'Environment ready';
      }
      if (environmentReady) {
        return isZh ? '环境已就绪，等待确认开放' : 'Environment ready, confirmation gated';
      }
      return isZh ? '环境待就绪' : 'Environment pending';
    default:
      return environmentReady
        ? isZh
          ? '环境在线'
          : 'Environment live'
        : isZh
          ? '环境待就绪'
          : 'Environment pending';
  }
};

const formatContinuityLabel = (
  sandboxType: string | null,
  sandboxBindingState: string | null,
  sandboxId: string | null,
  isZh: boolean,
): string | null => {
  if (!sandboxType && !sandboxBindingState && !sandboxId) {
    return null;
  }
  const parts = [
    sandboxType ? sandboxType : isZh ? 'sandbox' : 'sandbox',
    sandboxBindingState,
    sandboxId ? sandboxId.slice(0, 8) : null,
  ].filter(Boolean);
  return parts.join(' · ');
};

const readRefreshReason = (
  environmentManifest: Record<string, unknown> | null,
  isZh: boolean,
): string | null => {
  const refreshReason =
    environmentManifest && typeof environmentManifest.refresh_reason === 'string'
      ? environmentManifest.refresh_reason
      : null;
  if (!refreshReason) {
    return null;
  }
  const normalized = refreshReason.replace(/_/g, ' ');
  return isZh ? `已刷新: ${normalized}` : `Refreshed: ${normalized}`;
};

const readSyncModel = ({
  isSyncing,
  lastError,
  lastHeartbeatAt,
  lastSyncedAt,
  nowMs,
  isZh,
}: {
  isSyncing: boolean;
  lastError: string | null;
  lastHeartbeatAt: number | null;
  lastSyncedAt: number | null;
  nowMs: number;
  isZh: boolean;
}): { syncLabel: string; syncTone: 'live' | 'syncing' | 'stale' | 'error' } => {
  if (lastError) {
    return {
      syncLabel: isZh ? '同步异常' : 'Sync failed',
      syncTone: 'error',
    };
  }
  if (isSyncing) {
    return {
      syncLabel: isZh ? '同步中' : 'Syncing',
      syncTone: 'syncing',
    };
  }
  const freshnessAnchor = lastHeartbeatAt || lastSyncedAt;
  if (!freshnessAnchor) {
    return {
      syncLabel: isZh ? '等待首个心跳' : 'Waiting for live signal',
      syncTone: 'stale',
    };
  }
  const ageMs = Math.max(0, nowMs - freshnessAnchor);
  if (ageMs > 30000) {
    return {
      syncLabel: isZh ? `信号偏旧 ${Math.round(ageMs / 1000)}s` : `Stale ${Math.round(ageMs / 1000)}s`,
      syncTone: 'stale',
    };
  }
  return {
    syncLabel: isZh ? '实时在线' : 'Live',
    syncTone: 'live',
  };
};

const buildEnvironmentNotice = ({
  environmentStatus,
  environmentLastError,
  lastError,
  isZh,
}: {
  environmentStatus: string | null;
  environmentLastError: string | null;
  lastError: string | null;
  isZh: boolean;
}): MonitorSummaryModel['environmentNotice'] => {
  if (environmentStatus === 'recovering') {
    return {
      tone: 'warning',
      text:
        environmentLastError ||
        (isZh
          ? '共享环境恢复中，当前继续保留最近一次可用状态。'
          : 'The shared environment is recovering while the latest available state stays visible.'),
    };
  }

  if (environmentStatus === 'failed') {
    return {
      tone: 'error',
      text:
        environmentLastError ||
        (isZh
          ? '共享环境出现异常，系统正在等待修复。'
          : 'The shared environment failed and is waiting for remediation.'),
    };
  }

  if (lastError) {
    return {
      tone: 'warning',
      text: isZh
        ? `状态同步短暂异常，当前展示最近一次成功同步的数据。${lastError}`
        : `State sync hit a transient issue. Showing the latest successful snapshot. ${lastError}`,
    };
  }

  return null;
};

const pickSubtaskDetail = (
  subtask: ShadowCloneSubtask,
  detailSnapshot: MonitorRowDetailSnapshot,
  isZh: boolean,
): string => {
  const {
    streamingToolName,
    streamingTextContent,
    streamingReasoningContent,
    transcriptLatestLabel,
    panelLatestLabel,
  } = detailSnapshot;
  if (streamingToolName) {
    return truncate(
      isZh
        ? `正在调用 ${formatToolName(streamingToolName)}`
        : `Calling ${formatToolName(streamingToolName)}`,
      92,
    );
  }

  if (streamingTextContent.trim()) {
    return truncate(streamingTextContent, 92);
  }

  if (streamingReasoningContent.trim()) {
    return truncate(streamingReasoningContent, 92);
  }

  const latestLabel = transcriptLatestLabel || panelLatestLabel;
  if (latestLabel) {
    return truncate(latestLabel, 92);
  }

  const recoveryDetail = describeSubtaskRecovery(subtask, isZh);
  if (recoveryDetail) {
    return recoveryDetail;
  }

  if (subtask.result_summary) {
    return truncate(subtask.result_summary, 92);
  }

  if (subtask.error) {
    return truncate(subtask.error, 92);
  }

  switch (subtask.status) {
    case 'pending':
      return isZh ? '等待主 Agent 调度' : 'Waiting for the main agent to dispatch';
    case 'running':
      return isZh ? '正在处理当前子任务' : 'Processing the assigned subtask';
    case 'completed':
      return isZh ? '子任务已完成' : 'Subtask completed';
    case 'failed':
      return isZh ? '子任务执行失败' : 'Subtask failed';
    case 'cancelled':
      return isZh ? '子任务已停止，已生成结果会继续保留' : 'Subtask was stopped and generated output remains available';
    case 'timeout':
      return isZh ? '子任务执行超时结束' : 'Subtask timed out';
    case 'denied':
      return isZh ? '子任务被跳过，未进入实际执行' : 'Subtask was skipped before execution';
    default:
      return '';
  }
};

const pickFooterText = (
  phase: ShadowClonePhase,
  liveActivity: ShadowCloneLiveActivity | null,
  subtasks: ShadowCloneSubtask[],
  mainTranscriptSnapshot: MainTranscriptSnapshot,
  isZh: boolean,
): string => {
  const recoverySubtask =
    (liveActivity?.subtask_id
      ? subtasks.find((item) => item.id === liveActivity.subtask_id) || null
      : null) ||
    subtasks.find((item) => isActiveRecoveryPhase(item.recovery_phase)) ||
    null;
  const recoveryReason = String(liveActivity?.reason || '').trim();

  if (
    recoverySubtask &&
    (
      phase === 'recovering' ||
      recoveryReason === 'subagent_recovering' ||
      recoveryReason === 'subagent_wake_sent' ||
      recoveryReason === 'subagent_resumed' ||
      recoveryReason === 'subagent_replacement_started' ||
      recoveryReason === 'subagent_replacement_completed'
    )
  ) {
    const roleLabel = recoverySubtask.role || recoverySubtask.id;
    switch (String(recoverySubtask.recovery_phase || '').trim()) {
      case 'wake_requested':
      case 'wake_dispatched':
        return isZh
          ? `协调器: 正在唤醒 ${roleLabel}，优先保留原上下文和已有执行轨迹。`
          : `Coordinator: Waking ${roleLabel} first to preserve the existing context and execution trace.`;
      case 'replacement_requested':
        return isZh
          ? `协调器: ${roleLabel} 暂未恢复，正在整理失败原因和交接摘要。`
          : `Coordinator: ${roleLabel} did not recover cleanly, so handoff context is being prepared.`;
      case 'replacement_started':
        return isZh
          ? `协调器: 替补已接管 ${roleLabel} 的任务，并带着交接摘要继续执行。`
          : `Coordinator: A replacement has taken over ${roleLabel}'s task with the handoff summary.`;
      default:
        break;
    }
  }

  const {
    streamingToolName,
    streamingTextContent,
    streamingReasoningContent,
  } = mainTranscriptSnapshot;
  if (streamingToolName) {
    return truncate(
      isZh
        ? `Main Agent: 正在调用 ${formatToolName(streamingToolName)}`
        : `Main Agent: Calling ${formatToolName(streamingToolName)}`,
      128,
    );
  }

  if (streamingTextContent.trim()) {
    return truncate(`Main Agent: ${streamingTextContent}`, 128);
  }

  if (streamingReasoningContent.trim()) {
    return truncate(`Main Agent: ${streamingReasoningContent}`, 128);
  }

  switch (phase) {
    case 'preparing':
      return isZh
        ? 'Main Agent: 正在准备共享沙箱环境，确保技能与依赖可用。'
        : 'Main Agent: Preparing the shared sandbox environment before confirmation.';
    case 'confirming':
      return isZh
        ? 'Main Agent: 正在等待你的确认，以启动影分身执行。'
        : 'Main Agent: Waiting for confirmation before dispatching clones.';
    case 'recovering':
      return isZh
        ? '协调器: 正在恢复共享环境并准备重启当前任务层。'
        : 'Coordinator: Recovering the shared environment before restarting this layer.';
    case 'running':
      return isZh
        ? 'Main Agent: 正在监督影分身执行并等待结果回传。'
        : 'Main Agent: Supervising subagents and waiting for results.';
    case 'aggregating':
      return isZh
        ? 'Main Agent: 正在整合各影分身产出的内容草稿。'
        : 'Main Agent: Aggregating outputs from each clone.';
    case 'completed':
      return isZh
        ? 'Main Agent: 已完成影分身结果整合。'
        : 'Main Agent: Finished consolidating clone outputs.';
    case 'cancelled':
      return isZh
        ? 'Main Agent: 影分身执行已停止，已生成的结果会继续保留。'
        : 'Main Agent: Shadow Clone execution was stopped, and generated results remain available.';
    case 'timeout':
      return isZh
        ? 'Main Agent: 影分身执行因超时而结束，已生成的结果会继续保留。'
        : 'Main Agent: Shadow Clone execution timed out, and generated results remain available.';
    default:
      return isZh ? 'Main Agent: 待命中。' : 'Main Agent: Standing by.';
  }
};

const buildMainAgentLivePanelModel = (
  phase: ShadowClonePhase,
  mainTranscriptSnapshot: MainTranscriptSnapshot,
  isZh: boolean,
): MainAgentLivePanelModel | null => {
  const {
    streamingToolName,
    streamingTextContent,
    streamingReasoningContent,
  } = mainTranscriptSnapshot;

  if (streamingToolName) {
    return {
      eyebrow: isZh ? 'Main Agent Live' : 'Main Agent Live',
      title: isZh
        ? `正在调用 ${formatToolName(streamingToolName)}`
        : `Calling ${formatToolName(streamingToolName)}`,
      detail: '',
      isLive: true,
    };
  }

  if (streamingTextContent.trim()) {
    return {
      eyebrow: isZh ? 'Main Agent Live' : 'Main Agent Live',
      title: isZh ? '实时输出' : 'Live output',
      detail: truncateTail(streamingTextContent, 260),
      isLive: true,
    };
  }

  if (streamingReasoningContent.trim()) {
    return {
      eyebrow: isZh ? 'Main Agent Live' : 'Main Agent Live',
      title: isZh ? '实时思考' : 'Live reasoning',
      detail: truncateTail(streamingReasoningContent, 240),
      isLive: phase !== 'completed',
    };
  }

  if (phase === 'aggregating') {
    return {
      eyebrow: isZh ? 'Main Agent Live' : 'Main Agent Live',
      title: isZh ? '正在整合影分身结果' : 'Aggregating clone outputs',
      detail: isZh
        ? '轻量实时视图已接管主 Agent 收尾阶段，完整历史消息会在最终结果稳定后显示。'
        : 'The lightweight live view is handling the main-agent tail while the final transcript settles.',
      isLive: true,
    };
  }

  if (phase === 'completed') {
    return {
      eyebrow: isZh ? 'Main Agent Live' : 'Main Agent Live',
      title: isZh ? '正在完成最后收尾' : 'Finalizing the tail',
      detail: isZh
        ? '保留实时收尾视图直到最终消息稳定写入线程。'
        : 'Keeping the live tail visible until the final message is safely committed to the thread.',
      isLive: false,
    };
  }

  if (phase === 'cancelled' || phase === 'timeout') {
    return {
      eyebrow: isZh ? 'Main Agent Live' : 'Main Agent Live',
      title:
        phase === 'cancelled'
          ? isZh
            ? '执行已停止'
            : 'Execution stopped'
          : isZh
            ? '执行已超时'
            : 'Execution timed out',
      detail:
        phase === 'cancelled'
          ? isZh
            ? '停止请求已生效，已生成的结果和文件仍然保留。'
            : 'The stop request took effect, and generated results/files remain available.'
          : isZh
            ? '本次执行因超时结束，已生成的结果和文件仍然保留。'
            : 'This run ended due to timeout, and generated results/files remain available.',
      isLive: false,
    };
  }

  return null;
};

export const buildShadowCloneMonitorRow = (
  subtask: ShadowCloneSubtask,
  index: number,
  detailSnapshot: MonitorRowDetailSnapshot,
  isZh: boolean,
): ShadowCloneMonitorRow => ({
  id: subtask.id,
  index,
  role: subtask.agent_name || subtask.role || subtask.id || (isZh ? `影分身 ${index + 1}` : `Clone ${index + 1}`),
  taskSummary: truncate(subtask.task_description || subtask.id, 120),
  detail: pickSubtaskDetail(subtask, detailSnapshot, isZh),
  status: subtask.status,
  visualStatus: getVisualSubtaskStatus(subtask),
  agentName: subtask.agent_name,
  agentId: subtask.agent_id,
});

function MonitorRowButton({
  row,
  isZh,
  onSelect,
}: {
  row: ShadowCloneMonitorRow;
  isZh: boolean;
  onSelect?: (subtaskId: string) => void;
}) {
  const statusMeta = STATUS_META[row.visualStatus];
  const StatusIcon = statusMeta.icon;
  const isInteractive = typeof onSelect === 'function';
  const statusLabel = isZh
    ? row.visualStatus === 'pending'
      ? '等待中'
      : row.visualStatus === 'running'
        ? '运行中'
        : row.visualStatus === 'completed'
          ? '已完成'
          : row.visualStatus === 'failed'
            ? '异常'
            : row.visualStatus === 'cancelled'
              ? '已停止'
              : row.visualStatus === 'timeout'
                ? '已超时'
                : '已跳过'
    : statusMeta.label;

  return (
    <tr
      data-testid="shadow-clone-monitor-row"
      data-subtask-id={row.id}
      data-agent-name={row.agentName || undefined}
      data-agent-id={row.agentId || undefined}
      role={isInteractive ? 'button' : undefined}
      tabIndex={isInteractive ? 0 : undefined}
      onClick={isInteractive ? () => onSelect?.(row.id) : undefined}
      onKeyDown={
        isInteractive
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onSelect?.(row.id);
              }
            }
          : undefined
      }
      title={`${row.role} · ${row.taskSummary}${row.detail ? ` · ${row.detail}` : ''}`}
      className={cn(
        'border-b border-slate-200/70 transition-colors duration-150',
        isInteractive && 'cursor-pointer',
        isInteractive &&
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500/30',
        row.index % 2 === 0 ? 'bg-white' : 'bg-slate-50/55',
        statusMeta.rowAccentClassName,
      )}
    >
      <td className="px-4 py-1.5 align-middle">
        <span className="block text-[12px] font-semibold tabular-nums text-slate-500">
          {row.index + 1}.
        </span>
      </td>
      <td className="max-w-0 px-3 py-1.5 align-middle">
        <div className="flex min-w-0 items-center gap-2">
          <span className={cn('h-2.5 w-2.5 shrink-0 rounded-full', statusMeta.dotClassName)} />
          <span className="block min-w-0 flex-1 truncate whitespace-nowrap text-[12px] font-semibold text-slate-900">
            {row.role}
          </span>
        </div>
      </td>
      <td className="max-w-0 py-1.5 pl-1 pr-3 align-middle">
        <span className="block max-w-full truncate whitespace-nowrap text-[12px] font-medium text-slate-600">
          {row.taskSummary}
        </span>
      </td>
      <td className="max-w-0 px-3 py-1.5 align-middle">
        <span className="block max-w-full truncate whitespace-nowrap text-[12px] text-slate-500">
          {row.detail}
        </span>
      </td>
      <td className="py-1.5 pl-2 pr-5 align-middle">
        <div className="flex justify-end">
          <div
            className={cn(
              'flex h-6 w-6 items-center justify-center rounded-full border border-slate-200 bg-white/95',
              row.visualStatus === 'running' && 'border-blue-200 bg-blue-50/95',
              row.visualStatus === 'completed' && 'border-emerald-200 bg-emerald-50/95',
              row.visualStatus === 'failed' && 'border-red-200 bg-red-50/95',
              row.visualStatus === 'cancelled' && 'border-slate-200 bg-slate-100/95',
              row.visualStatus === 'timeout' && 'border-amber-200 bg-amber-50/95',
              row.visualStatus === 'denied' && 'border-slate-200 bg-slate-100/95',
            )}
            aria-label={statusLabel}
          >
            {row.visualStatus === 'running' ? (
              <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
            ) : (
              <StatusIcon className={cn('h-3.5 w-3.5', statusMeta.iconClassName)} />
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}

const LiveMonitorRow = React.memo(function LiveMonitorRow({
  subtaskId,
  index,
  isZh,
  onSelect,
}: {
  subtaskId: string;
  index: number;
  isZh: boolean;
  onSelect: (subtaskId: string) => void;
}) {
  const {
    subtask,
    streamingToolName,
    streamingTextContent,
    streamingReasoningContent,
    transcriptLatestLabel,
    panelLatestLabel,
  } = useShadowCloneStore(
    useShallow((state) => ({
      subtask: state.subtasks.find((item) => item.id === subtaskId) || null,
      streamingToolName: readStreamingToolName(
        state.subtaskTranscriptStates[subtaskId]?.streamingToolCall,
      ),
      streamingTextContent:
        state.subtaskTranscriptStates[subtaskId]?.streamingTextContent || '',
      streamingReasoningContent:
        state.subtaskTranscriptStates[subtaskId]?.streamingReasoningContent || '',
      transcriptLatestLabel:
        state.subtaskTranscriptStates[subtaskId]?.latestLabel || null,
      panelLatestLabel: state.subtaskPanelStates[subtaskId]?.latestLabel || null,
    })),
  );

  const detailSnapshot = React.useMemo<MonitorRowDetailSnapshot>(
    () => ({
      streamingToolName,
      streamingTextContent,
      streamingReasoningContent,
      transcriptLatestLabel,
      panelLatestLabel,
    }),
    [
      panelLatestLabel,
      streamingReasoningContent,
      streamingTextContent,
      streamingToolName,
      transcriptLatestLabel,
    ],
  );

  const row = React.useMemo(
    () =>
      subtask
        ? buildShadowCloneMonitorRow(subtask, index, detailSnapshot, isZh)
        : null,
    [detailSnapshot, index, isZh, subtask],
  );

  if (!row) {
    return null;
  }

  return <MonitorRowButton row={row} isZh={isZh} onSelect={onSelect} />;
});

const LiveMonitorRows = React.memo(function LiveMonitorRows({
  subtaskIds,
  isZh,
  onSelect,
}: {
  subtaskIds: string[];
  isZh: boolean;
  onSelect: (subtaskId: string) => void;
}) {
  return (
    <>
      {subtaskIds.map((subtaskId, index) => (
        <LiveMonitorRow
          key={subtaskId}
          subtaskId={subtaskId}
          index={index}
          isZh={isZh}
          onSelect={onSelect}
        />
      ))}
    </>
  );
});

export const ShadowCloneMonitorRows = React.memo(function ShadowCloneMonitorRows({
  rows,
  isZh,
  onSelect,
}: {
  rows: ShadowCloneMonitorRow[];
  isZh: boolean;
  onSelect?: (subtaskId: string) => void;
}) {
  return (
    <>
      {rows.map((row) => (
        <MonitorRowButton
          key={row.id}
          row={row}
          isZh={isZh}
          onSelect={onSelect}
        />
      ))}
    </>
  );
});

interface ShadowCloneMonitorTableProps {
  isZh: boolean;
  children?: React.ReactNode;
  emptyState?: React.ReactNode;
  viewportClassName?: string;
}

export const ShadowCloneMonitorTable = React.memo(function ShadowCloneMonitorTable({
  isZh,
  children,
  emptyState,
  viewportClassName,
}: ShadowCloneMonitorTableProps) {
  return (
    <div className={cn('min-h-0 flex-1 overflow-auto', viewportClassName)}>
      <table className="min-w-[38rem] w-full table-fixed border-collapse">
        <thead className="sticky top-0 z-10 bg-slate-50/95 backdrop-blur-sm">
          <tr className="border-b border-slate-200/80 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">
            <th className="w-12 px-4 py-2 text-left">{isZh ? '#' : '#'}</th>
            <th className="w-[26%] px-3 py-2 text-left">{isZh ? '影分身' : 'Clone'}</th>
            <th className="w-[38%] py-2 pl-1 pr-3 text-left">{isZh ? '任务' : 'Task'}</th>
            <th className="w-[24%] px-3 py-2 text-left">
              {isZh ? '当前行动' : 'Current action'}
            </th>
            <th className="w-14 py-2 pl-2 pr-5 text-right">{isZh ? '状态' : 'State'}</th>
          </tr>
        </thead>
        <tbody>
          {children || emptyState ? (
            children || (
              <tr className="border-b border-slate-200/70 bg-white">
                <td colSpan={5} className="px-4 py-5 align-middle">
                  {emptyState}
                </td>
              </tr>
            )
          ) : null}
        </tbody>
      </table>
    </div>
  );
});

export function ShadowCloneMainMonitor({
  isZh = true,
  messages,
}: ShadowCloneMainMonitorProps) {
  const phase = useShadowCloneStore((state) => state.phase);
  const currentRunId = useShadowCloneStore((state) => state.currentRunId);
  const subtasks = useShadowCloneStore((state) => state.subtasks);
  const pendingCount = useShadowCloneStore((state) => state.pendingCount);
  const lastHeartbeatAt = useShadowCloneStore((state) => state.lastHeartbeatAt);
  const lastSyncedAt = useShadowCloneStore((state) => state.lastSyncedAt);
  const isSyncing = useShadowCloneStore((state) => state.isSyncing);
  const lastError = useShadowCloneStore((state) => state.lastError);
  const mainTranscriptSnapshot = useShadowCloneStore(
    useShallow((state) =>
      buildMainTranscriptSnapshot(state.mainTranscriptState),
    ),
  );
  const liveActivity = useShadowCloneStore((state) => state.liveActivity);
  const environmentStatus = useShadowCloneStore((state) => state.environmentStatus);
  const environmentReady = useShadowCloneStore((state) => state.environmentReady);
  const environmentLastError = useShadowCloneStore((state) => state.environmentLastError);
  const environmentPreparedAt = useShadowCloneStore((state) => state.environmentPreparedAt);
  const environmentConfirmationReady = useShadowCloneStore(
    (state) => state.environmentConfirmationReady,
  );
  const environmentManifest = useShadowCloneStore((state) => state.environmentManifest);
  const sandboxId = useShadowCloneStore((state) => state.sandboxId);
  const sandboxType = useShadowCloneStore((state) => state.sandboxType);
  const sandboxBindingState = useShadowCloneStore((state) => state.sandboxBindingState);
  const selectSubtask = useShadowCloneStore((state) => state.selectSubtask);

  const [isLive, setIsLive] = React.useState(true);
  const [frozenModel, setFrozenModel] = React.useState<FrozenMonitorModel | null>(null);
  const [nowMs, setNowMs] = React.useState(() => Date.now());

  React.useEffect(() => {
    const interval = window.setInterval(() => {
      setNowMs(Date.now());
    }, 5000);
    return () => window.clearInterval(interval);
  }, []);

  React.useEffect(() => {
    if (!ACTIVE_PHASES.includes(phase)) {
      setIsLive(true);
      setFrozenModel(null);
    }
  }, [phase]);

  const liveModel = React.useMemo<MonitorSummaryModel>(() => {
    const completedCount = subtasks.filter((item) => item.status === 'completed').length;
    const failedCount = subtasks.filter((item) => item.status === 'failed').length;
    const resolvedCount = subtasks.filter(
      (item) =>
        item.status === 'completed' ||
        item.status === 'failed' ||
        item.status === 'cancelled' ||
        item.status === 'timeout' ||
        item.status === 'denied',
    ).length;
    const totalCount = subtasks.length;
    const progressValue = totalCount > 0 ? (resolvedCount / totalCount) * 100 : 0;
    const lastUpdatedLabel = lastHeartbeatAt
      ? new Date(lastHeartbeatAt).toLocaleTimeString(isZh ? 'zh-CN' : 'en-US', {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
      : isZh
        ? '刚刚'
        : 'Just now';
    const syncModel = readSyncModel({
      isSyncing,
      lastError,
      lastHeartbeatAt,
      lastSyncedAt,
      nowMs,
      isZh,
    });
    const environmentLabel = formatEnvironmentLabel(
      environmentStatus,
      environmentReady,
      environmentConfirmationReady,
      isZh,
    );
    const continuityLabel = formatContinuityLabel(
      sandboxType,
      sandboxBindingState,
      sandboxId,
      isZh,
    );
    const preparedLabel = formatPreparedAt(environmentPreparedAt, isZh);
    const refreshLabel = readRefreshReason(environmentManifest, isZh);

    return {
      taskSummary: pickRunningTask(messages, isZh),
      footer: pickFooterText(
        phase,
        liveActivity,
        subtasks,
        mainTranscriptSnapshot,
        isZh,
      ),
      completedCount,
      failedCount,
      resolvedCount,
      totalCount,
      progressValue,
      lastUpdatedLabel,
      liveLabel:
        phase === 'preparing'
          ? isZh
            ? '环境准备中'
            : 'Preparing environment'
          : phase === 'confirming'
          ? isZh
            ? '等待确认'
            : 'Awaiting confirmation'
          : phase === 'recovering'
            ? readRecoveryLiveLabel(subtasks, isZh) || (isZh ? '恢复中' : 'Recovering')
          : phase === 'aggregating'
            ? isZh
              ? '整合中'
              : 'Aggregating'
            : phase === 'completed'
              ? isZh
                ? '已完成'
                : 'Completed'
              : phase === 'cancelled'
                ? isZh
                  ? '已停止'
                  : 'Stopped'
                : phase === 'timeout'
                  ? isZh
                    ? '已超时'
                    : 'Timed out'
              : pendingCount > 0
                ? isZh
                  ? `待启动 ${pendingCount}`
                  : `${pendingCount} pending`
                : isZh
                  ? '实时中'
                  : 'Live',
      syncLabel: syncModel.syncLabel,
      syncTone: syncModel.syncTone,
      environmentLabel,
      continuityLabel,
      preparedLabel,
      refreshLabel,
      environmentNotice: buildEnvironmentNotice({
        environmentStatus,
        environmentLastError,
        lastError,
        isZh,
      }),
    };
  }, [
    environmentConfirmationReady,
    environmentLastError,
    environmentManifest,
    environmentPreparedAt,
    environmentReady,
    environmentStatus,
    isSyncing,
    isZh,
    lastError,
    lastHeartbeatAt,
    lastSyncedAt,
    liveActivity,
    mainTranscriptSnapshot,
    messages,
    nowMs,
    pendingCount,
    phase,
    sandboxBindingState,
    sandboxId,
    sandboxType,
    subtasks,
  ]);

  const liveSubtaskIdsKey = React.useMemo(
    () => JSON.stringify(subtasks.map((subtask) => subtask.id)),
    [subtasks],
  );
  const liveSubtaskIds = React.useMemo<string[]>(
    () => JSON.parse(liveSubtaskIdsKey) as string[],
    [liveSubtaskIdsKey],
  );

  const buildFrozenModel = React.useCallback((): FrozenMonitorModel => {
    const snapshotState = useShadowCloneStore.getState();

    return {
      ...liveModel,
      rows: snapshotState.subtasks.map((subtask, index) =>
        buildShadowCloneMonitorRow(
          subtask,
          index,
          buildMonitorRowDetailSnapshot(
            snapshotState.subtaskTranscriptStates[subtask.id],
            snapshotState.subtaskPanelStates[subtask.id],
          ),
          isZh,
        ),
      ),
    };
  }, [isZh, liveModel]);

  const model = isLive ? liveModel : frozenModel || liveModel;
  const mainAgentLivePanel = React.useMemo(
    () => buildMainAgentLivePanelModel(phase, mainTranscriptSnapshot, isZh),
    [isZh, mainTranscriptSnapshot, phase],
  );

  const handleToggleLive = React.useCallback(() => {
    if (isLive) {
      setFrozenModel(buildFrozenModel());
      setIsLive(false);
      return;
    }
    setFrozenModel(null);
    setIsLive(true);
  }, [buildFrozenModel, isLive]);

  const handleJumpToLive = React.useCallback(() => {
    setFrozenModel(null);
    setIsLive(true);
  }, []);

  const handleSelectSubtask = React.useCallback(
    (subtaskId: string) => {
      if (!currentRunId) {
        return;
      }
      void selectSubtask(currentRunId, subtaskId);
    },
    [currentRunId, selectSubtask],
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(248,250,252,0.98)_45%,rgba(241,245,249,0.98)_100%)]">
      <div className="border-b border-slate-200/80 px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-[11px] uppercase tracking-[0.24em] text-slate-400">
              {isZh ? 'Running Task' : 'Running Task'}
            </p>
            <p className="mt-2 text-sm font-medium leading-6 text-slate-900">
              {model.taskSummary}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="icon"
              onClick={handleToggleLive}
              className="h-9 w-9 rounded-xl border-slate-200 bg-white/90 text-slate-700 shadow-sm transition-all duration-200 hover:border-slate-300 hover:bg-white"
            >
              {isLive ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            </Button>
            <div className="rounded-2xl border border-slate-200 bg-white/90 px-3 py-2 text-right shadow-sm">
              <p className="text-[11px] uppercase tracking-[0.18em] text-slate-400">
                {isZh ? 'Progress' : 'Progress'}
              </p>
              <p className="mt-1 text-sm font-semibold text-slate-900">
                {model.resolvedCount} / {model.totalCount}
              </p>
            </div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
          <span
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 whitespace-nowrap',
              model.syncTone === 'live' && 'border-emerald-200 bg-emerald-50 text-emerald-700',
              model.syncTone === 'syncing' && 'border-blue-200 bg-blue-50 text-blue-700',
              model.syncTone === 'stale' && 'border-amber-200 bg-amber-50 text-amber-700',
              model.syncTone === 'error' && 'border-red-200 bg-red-50 text-red-700',
            )}
          >
            <RefreshCcw className="h-3 w-3" />
            {model.syncLabel}
          </span>
          <span
            className={cn(
              'rounded-full border px-2.5 py-1 whitespace-nowrap',
              environmentReady
                ? 'border-sky-200 bg-sky-50 text-sky-700'
                : 'border-slate-200 bg-white/80 text-slate-600',
            )}
          >
            {model.environmentLabel}
          </span>
          {model.continuityLabel ? (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white/80 px-2.5 py-1 text-slate-600 whitespace-nowrap">
              <HardDrive className="h-3 w-3" />
              {model.continuityLabel}
            </span>
          ) : null}
          {model.refreshLabel ? (
            <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-700 whitespace-nowrap">
              {model.refreshLabel}
            </span>
          ) : null}
          {model.preparedLabel ? (
            <span className="rounded-full border border-slate-200 bg-white/80 px-2.5 py-1 whitespace-nowrap">
              {isZh ? `准备于 ${model.preparedLabel}` : `Prepared ${model.preparedLabel}`}
            </span>
          ) : null}
          <span className="rounded-full border border-slate-200 bg-white/80 px-2.5 py-1 whitespace-nowrap">
            {isZh ? `已完成 ${model.completedCount}` : `${model.completedCount} complete`}
          </span>
          <span className="rounded-full border border-slate-200 bg-white/80 px-2.5 py-1 whitespace-nowrap">
            {isZh ? `异常 ${model.failedCount}` : `${model.failedCount} failed`}
          </span>
          <span className="rounded-full border border-slate-200 bg-white/80 px-2.5 py-1 whitespace-nowrap">
            {isZh ? `更新于 ${model.lastUpdatedLabel}` : `Updated ${model.lastUpdatedLabel}`}
          </span>
        </div>
        {model.environmentNotice ? (
          <div
            className={cn(
              'mt-3 flex items-start gap-2 rounded-2xl px-3 py-2 text-[12px]',
              model.environmentNotice.tone === 'error'
                ? 'border border-red-200 bg-red-50/80 text-red-700'
                : 'border border-amber-200 bg-amber-50/90 text-amber-800',
            )}
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{model.environmentNotice.text}</span>
          </div>
        ) : null}
        {mainAgentLivePanel ? (
          <div className="mt-3 rounded-[24px] border border-slate-900/5 bg-slate-950 px-4 py-3 text-slate-100 shadow-[0_12px_30px_-24px_rgba(15,23,42,0.65)]">
            <div className="flex items-start gap-3">
              <span
                className={cn(
                  'mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full',
                  mainAgentLivePanel.isLive
                    ? 'bg-emerald-400 shadow-[0_0_0_4px_rgba(74,222,128,0.16)]'
                    : 'bg-slate-500',
                )}
              />
              <div className="min-w-0 flex-1">
                <p className="text-[11px] uppercase tracking-[0.18em] text-slate-400">
                  {mainAgentLivePanel.eyebrow}
                </p>
                <p className="mt-1 text-sm font-semibold text-slate-100">
                  {mainAgentLivePanel.title}
                </p>
                {mainAgentLivePanel.detail ? (
                  <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-slate-300">
                    {mainAgentLivePanel.detail}
                  </p>
                ) : null}
              </div>
            </div>
          </div>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 px-4 py-4">
        <div className="flex h-full min-h-0 flex-col rounded-[28px] border border-slate-200/90 bg-white/90 shadow-[0_20px_50px_-30px_rgba(15,23,42,0.35)]">
          <div className="flex items-center justify-between border-b border-slate-200/80 px-4 py-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.16em] text-slate-400">
                {isZh ? 'Subjects' : 'Subjects'}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {isZh
                  ? '点击任意影分身即可进入该子任务的实时检查视图。'
                  : 'Click any row to inspect that clone in real time.'}
              </p>
            </div>

            <div
              className={cn(
                'inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium whitespace-nowrap',
                isLive
                  ? 'border-red-200 bg-red-50 text-red-700'
                  : 'border-slate-200 bg-slate-100 text-slate-600',
              )}
            >
              <Radio className="h-3.5 w-3.5" />
              {isLive ? model.liveLabel : isZh ? '已暂停' : 'Paused'}
            </div>
          </div>

          <ShadowCloneMonitorTable isZh={isZh}>
            {isLive
              ? (
                <LiveMonitorRows
                  subtaskIds={liveSubtaskIds}
                  isZh={isZh}
                  onSelect={handleSelectSubtask}
                />
              )
              : (
                <ShadowCloneMonitorRows
                  rows={frozenModel?.rows || []}
                  isZh={isZh}
                  onSelect={handleSelectSubtask}
                />
              )}
          </ShadowCloneMonitorTable>

          <div className="border-t border-slate-200/80 px-4 py-4">
            <div className="flex justify-center">
              <Button
                type="button"
                variant="outline"
                onClick={handleJumpToLive}
                className="h-9 rounded-xl border-slate-200 bg-white px-4 text-sm text-slate-700 shadow-sm transition-all duration-200 hover:border-slate-300 hover:bg-slate-50"
              >
                {isZh ? 'Jump to Live' : 'Jump to Live'}
              </Button>
            </div>

            <div className="mt-4 flex items-center gap-3">
              <div className="flex items-center gap-1 text-slate-400">
                <span className="text-xs font-medium">|&lt;</span>
                <span className="text-xs font-medium">&gt;|</span>
              </div>

              <div className="flex-1">
                <Slider
                  min={0}
                  max={100}
                  step={1}
                  value={[Math.round(model.progressValue)]}
                  onValueChange={() => undefined}
                  className="w-full [&>span:first-child]:h-1.5 [&>span:first-child]:bg-slate-200 [&>span:first-child>span]:bg-gradient-to-r [&>span:first-child>span]:from-blue-600 [&>span:first-child>span]:via-sky-500 [&>span:first-child>span]:to-emerald-500 [&>span:first-child>span]:h-1.5"
                />
              </div>

              <div
                className={cn(
                  'inline-flex items-center gap-1.5 text-xs font-medium',
                  isLive ? 'text-red-600' : 'text-slate-500',
                )}
              >
                <Radio className="h-3.5 w-3.5" />
                {isLive ? model.liveLabel : isZh ? '已暂停' : 'Paused'}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="border-t border-slate-200/80 bg-[linear-gradient(180deg,rgba(15,23,42,0.88)_0%,rgba(15,23,42,0.95)_100%)] px-4 py-3 text-xs text-slate-100">
        <span className="font-medium text-slate-200">Main Agent:</span>{' '}
        <span className="text-slate-300">{model.footer.replace(/^Main Agent:\s*/i, '')}</span>
      </div>
    </div>
  );
}
