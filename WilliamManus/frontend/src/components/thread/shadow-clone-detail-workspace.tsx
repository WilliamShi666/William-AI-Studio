'use client';

import React from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  CircleDashed,
  OctagonX,
  PanelsTopLeft,
  Pause,
  Radar,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import {
  useShadowCloneStore,
  type ShadowCloneTranscriptState,
  type ShadowCloneSubtask,
  type ShadowCloneSubtaskStatus,
} from '@/lib/stores/shadow-clone-store';
import { ShadowCloneRunningSpinner } from './shadow-clone-running-spinner';

const STATUS_CONFIG: Record<
  ShadowCloneSubtaskStatus,
  {
    label: string;
    badgeClassName: string;
    icon: React.ComponentType<{ className?: string }>;
    iconClassName: string;
  }
> = {
  pending: {
    label: '等待中',
    badgeClassName: 'bg-slate-100 text-slate-600',
    icon: CircleDashed,
    iconClassName: 'text-slate-400',
  },
  running: {
    label: '运行中',
    badgeClassName: 'bg-blue-50 text-blue-700',
    icon: CircleDashed,
    iconClassName: 'text-blue-600',
  },
  completed: {
    label: '已完成',
    badgeClassName: 'bg-emerald-50 text-emerald-700',
    icon: CheckCircle2,
    iconClassName: 'text-emerald-600',
  },
  failed: {
    label: '失败',
    badgeClassName: 'bg-red-50 text-red-700',
    icon: OctagonX,
    iconClassName: 'text-red-600',
  },
  cancelled: {
    label: '已停止',
    badgeClassName: 'bg-slate-100 text-slate-700',
    icon: Pause,
    iconClassName: 'text-slate-600',
  },
  timeout: {
    label: '已超时',
    badgeClassName: 'bg-amber-50 text-amber-700',
    icon: AlertTriangle,
    iconClassName: 'text-amber-600',
  },
  denied: {
    label: '已跳过',
    badgeClassName: 'bg-slate-100 text-slate-600',
    icon: Pause,
    iconClassName: 'text-slate-500',
  },
};

const getDetailBody = (subtask: ShadowCloneSubtask | undefined, fullResult?: string) => {
  if (!subtask) {
    return '请选择一个子任务，右侧将展示该 Subagent 的运行详情与结果摘录。';
  }

  if (fullResult) {
    return fullResult;
  }

  if (subtask.result_summary) {
    return subtask.result_summary;
  }

  if (subtask.status === 'running') {
    return '子任务正在运行中，结果摘录会在生成后同步到这里。';
  }

  if (subtask.status === 'pending') {
    return '子任务正在排队等待执行。';
  }

  if (subtask.status === 'cancelled') {
    return '子任务已停止，已生成的结果摘录会继续保留在这里。';
  }

  if (subtask.status === 'timeout') {
    return '子任务因超时结束，当前仅保留已生成的结果摘录。';
  }

  if (subtask.status === 'denied') {
    return '子任务被跳过，未进入实际执行。';
  }

  if (subtask.error) {
    return subtask.error;
  }

  return '当前暂无可展示的详细结果。';
};

const readLiveTranscriptText = (
  transcriptState: ShadowCloneTranscriptState | undefined,
): string => {
  if (!transcriptState) return '';

  const liveParts = [
    transcriptState.streamingReasoningContent,
    transcriptState.streamingToolCall
      ? JSON.stringify(transcriptState.streamingToolCall, null, 2)
      : '',
    transcriptState.streamingTextContent,
  ].filter((part) => typeof part === 'string' && part.trim());
  if (liveParts.length > 0) {
    return liveParts.join('\n\n');
  }

  return transcriptState.messages
    .map((message) => message.content)
    .filter((content) => typeof content === 'string' && content.trim())
    .join('\n\n');
};

export function ShadowCloneDetailWorkspace() {
  const phase = useShadowCloneStore((state) => state.phase);
  const currentRunId = useShadowCloneStore((state) => state.currentRunId);
  const subtasks = useShadowCloneStore((state) => state.subtasks);
  const activeSubtaskId = useShadowCloneStore((state) => state.activeSubtaskId);
  const detailResults = useShadowCloneStore((state) => state.detailResults);
  const detailLoadingId = useShadowCloneStore((state) => state.detailLoadingId);
  const detailError = useShadowCloneStore((state) => state.detailError);
  const activeTranscriptState = useShadowCloneStore((state) =>
    state.activeSubtaskId
      ? state.subtaskTranscriptStates[state.activeSubtaskId]
      : undefined,
  );
  const isSyncing = useShadowCloneStore((state) => state.isSyncing);
  const lastHeartbeatAt = useShadowCloneStore((state) => state.lastHeartbeatAt);
  const setActiveSubtask = useShadowCloneStore((state) => state.setActiveSubtask);

  const activeSubtask = subtasks.find((item) => item.id === activeSubtaskId) || null;
  const activeTranscriptText = readLiveTranscriptText(activeTranscriptState);
  const completedCount = subtasks.filter((item) => item.status === 'completed').length;
  const runningCount = subtasks.filter((item) => item.status === 'running').length;
  const failedCount = subtasks.filter((item) => item.status === 'failed').length;
  const heartbeatText = lastHeartbeatAt
    ? new Date(lastHeartbeatAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '暂无';
  const handleReturnToMainView = React.useCallback(() => {
    setActiveSubtask(null);
  }, [setActiveSubtask]);

  const handleSelectSubtask = (subtaskId: string) => {
    setActiveSubtask(null);
  };

  return (
    <div className="flex h-full flex-col bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(248,250,252,0.98)_100%)]">
      <div className="border-b border-slate-200/80 px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
              <Radar className="h-4 w-4 text-blue-600" />
              影分身深度监控台
            </div>
            <p className="text-xs leading-5 text-slate-500">
              选中左侧子任务后，这里会联动展示详细状态、结果摘录与完整输出。
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleReturnToMainView}
            className="h-8 rounded-xl border-slate-200 px-3 text-xs text-slate-700 hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900"
          >
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            返回主视图
          </Button>
          <div className="rounded-2xl border border-slate-200 bg-white px-3 py-2 text-right shadow-sm">
            <div className="text-[11px] uppercase tracking-[0.18em] text-slate-400">状态</div>
            <div className="mt-1 text-sm font-semibold text-slate-900">{phase === 'aggregating' ? '汇总中' : '监控中'}</div>
            <div className="mt-1 text-[11px] text-slate-500">心跳：{heartbeatText}</div>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-3 gap-3">
          <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">运行中</div>
            <div className="mt-2 text-2xl font-semibold text-blue-700">{runningCount}</div>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">已完成</div>
            <div className="mt-2 text-2xl font-semibold text-emerald-700">{completedCount}</div>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
            <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">异常</div>
            <div className="mt-2 text-2xl font-semibold text-red-600">{failedCount}</div>
          </div>
        </div>
      </div>

      <div className="grid min-h-0 flex-1 gap-4 overflow-hidden px-5 py-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.95fr)]">
        <section className="min-h-0 overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-[0_18px_40px_-28px_rgba(15,23,42,0.35)]">
          <div className="flex items-center justify-between border-b border-slate-200/80 px-4 py-3">
            <div>
              <h3 className="text-sm font-semibold text-slate-900">任务总览</h3>
              <p className="mt-1 text-xs text-slate-500">点击任意子任务，右侧详情将实时联动。</p>
            </div>
            <div className="inline-flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-600">
              <PanelsTopLeft className="h-3.5 w-3.5" />
              {isSyncing ? '同步中…' : `${subtasks.length} 个子任务`}
            </div>
          </div>

          <div className="min-h-0 overflow-auto">
            <table className="min-w-full border-separate border-spacing-0 text-sm">
              <thead className="sticky top-0 z-10 bg-slate-50/95 backdrop-blur-sm">
                <tr className="text-left text-xs uppercase tracking-[0.16em] text-slate-500">
                  <th className="px-4 py-3 font-medium">序号</th>
                  <th className="px-4 py-3 font-medium">任务主题</th>
                  <th className="px-4 py-3 font-medium">角色</th>
                  <th className="px-4 py-3 font-medium">状态标签</th>
                </tr>
              </thead>
              <tbody>
                {subtasks.map((subtask, index) => {
                  const statusConfig = STATUS_CONFIG[subtask.status];
                  const StatusIcon = statusConfig.icon;
                  const isActive = subtask.id === activeSubtaskId;

                  return (
                    <tr
                      key={subtask.id}
                      onClick={() => handleSelectSubtask(subtask.id)}
                      className={cn(
                        'cursor-pointer transition-all duration-200',
                        isActive ? 'bg-blue-50/70' : 'hover:bg-slate-50',
                      )}
                    >
                      <td className="border-b border-slate-100 px-4 py-3 align-top text-slate-500">
                        {String(index + 1).padStart(2, '0')}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 align-top">
                        <div className="max-w-[280px] space-y-1">
                          <div className="font-medium text-slate-900">{subtask.id}</div>
                          <p className="line-clamp-2 text-xs leading-5 text-slate-500">
                            {subtask.task_description || '暂无任务描述'}
                          </p>
                        </div>
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 align-top text-slate-600">
                        {subtask.role || 'subagent'}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 align-top">
                        <span
                          className={cn(
                            'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium',
                            statusConfig.badgeClassName,
                          )}
                        >
                          {subtask.status === 'running' ? (
                            <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
                          ) : (
                            <StatusIcon className={cn('h-3.5 w-3.5', statusConfig.iconClassName)} />
                          )}
                          {statusConfig.label}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="min-h-0 overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-[0_18px_40px_-28px_rgba(15,23,42,0.35)]">
          <div className="border-b border-slate-200/80 px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-slate-900">任务详情</h3>
                <p className="mt-1 text-xs text-slate-500">
                  {activeSubtask ? '查看选中子任务的状态、摘要与完整输出。' : '从左侧选择任务后，这里会展示详细内容。'}
                </p>
              </div>
              {activeSubtask && (() => {
                const statusConfig = STATUS_CONFIG[activeSubtask.status];
                const StatusIcon = statusConfig.icon;
                return (
                  <span
                    className={cn(
                      'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium',
                      statusConfig.badgeClassName,
                    )}
                  >
                    {activeSubtask.status === 'running' ? (
                      <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
                    ) : (
                      <StatusIcon className={cn('h-3.5 w-3.5', statusConfig.iconClassName)} />
                    )}
                    {statusConfig.label}
                  </span>
                );
              })()}
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto px-4 py-4">
            <div className="rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
              <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">当前任务</div>
              <div className="mt-2 text-sm font-semibold text-slate-900">
                {activeSubtask?.id || '尚未选中任务'}
              </div>
              <p className="mt-2 text-xs leading-6 text-slate-500">
                {activeSubtask?.task_description || '点击左侧任意子任务卡片，查看该任务的详细监控内容。'}
              </p>
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-2xl border border-slate-200 bg-white p-4">
                <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">角色</div>
                <div className="mt-2 text-sm font-medium text-slate-900">{activeSubtask?.role || '—'}</div>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-white p-4">
                <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">结果状态</div>
                <div className="mt-2 text-sm font-medium text-slate-900">
                  {activeSubtask?.status ? STATUS_CONFIG[activeSubtask.status].label : '未选择'}
                </div>
              </div>
            </div>

            <div className="min-h-[240px] rounded-3xl border border-slate-200 bg-slate-950/[0.02] p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">监控输出</div>
                  <div className="mt-1 text-sm font-medium text-slate-900">完整结果 / 运行摘录</div>
                </div>
                {detailLoadingId === activeSubtask?.id && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">
                    <ShadowCloneRunningSpinner size="xs" className="text-blue-600" />
                    加载中
                  </span>
                )}
              </div>

              <div className="mt-4 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                <pre className="max-h-[340px] overflow-auto whitespace-pre-wrap break-words text-xs leading-6 text-slate-700">
                  {getDetailBody(
                    activeSubtask || undefined,
                    activeSubtask
                      ? detailResults[activeSubtask.id] || activeTranscriptText
                      : undefined,
                  )}
                </pre>
              </div>

              {detailError && (
                <div className="mt-3 rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {detailError}
                </div>
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
