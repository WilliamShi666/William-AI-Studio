'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { GitBranch, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { safeJsonParse } from '@/components/thread/utils';
import type { ParsedContent } from '@/components/thread/types';
import { useShadowCloneStore } from '@/lib/stores/shadow-clone-store';
import { cn } from '@/lib/utils';
import {
  ShadowCloneMonitorRows,
  ShadowCloneMonitorTable,
  type ShadowCloneMonitorRow,
} from './shadow-clone-main-monitor';

interface ShadowCloneConfirmationProps {
  agentRunId: string | null;
}

const truncateConfirmationText = (
  value: string | null | undefined,
  maxLength = 96,
): string => {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!normalized) return '';
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength - 1)}...`;
};

export function ShadowCloneConfirmation({
  agentRunId,
}: ShadowCloneConfirmationProps) {
  const phase = useShadowCloneStore((state) => state.phase);
  const subtasks = useShadowCloneStore((state) => state.subtasks);
  const dependencies = useShadowCloneStore((state) => state.dependencies);
  const environmentStatus = useShadowCloneStore((state) => state.environmentStatus);
  const environmentReady = useShadowCloneStore((state) => state.environmentReady);
  const environmentLastError = useShadowCloneStore((state) => state.environmentLastError);
  const environmentConfirmationReady = useShadowCloneStore(
    (state) => state.environmentConfirmationReady,
  );
  const mainTranscriptState = useShadowCloneStore(
    (state) => state.mainTranscriptState,
  );
  const confirmProposal = useShadowCloneStore((state) => state.confirmProposal);
  const denyProposal = useShadowCloneStore((state) => state.denyProposal);
  const resetRuntime = useShadowCloneStore((state) => state.resetRuntime);

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isAwaitingExecutionStart, setIsAwaitingExecutionStart] = useState(false);

  const isPlanning = phase === 'planning';
  const isPreparing =
    subtasks.length > 0 &&
    (
      phase === 'preparing' ||
      environmentStatus === 'pending' ||
      environmentStatus === 'preparing' ||
      (!environmentReady && phase === 'confirming')
    );
  const isRecovering = environmentStatus === 'recovering';
  const isEnvironmentFailed = environmentStatus === 'failed';
  const isStartingExecution =
    isAwaitingExecutionStart && phase === 'confirming' && subtasks.length > 0;
  const isConfirming =
    (
      phase === 'confirming' ||
      phase === 'preparing' ||
      isPreparing ||
      isRecovering ||
      isEnvironmentFailed
    ) &&
    subtasks.length > 0;
  const canConfirm =
    Boolean(agentRunId) &&
    environmentReady &&
    environmentConfirmationReady &&
    !isRecovering &&
    !isEnvironmentFailed &&
    !isStartingExecution;
  const statusLabel = isPlanning
    ? '规划中'
    : isEnvironmentFailed
      ? '环境异常'
      : isRecovering
        ? '恢复中'
        : isPreparing
          ? '环境准备中'
          : isStartingExecution
            ? '启动中'
            : '等待确认';
  const statusDescription = isPlanning
    ? '主智能体正在拆解任务并生成影分身执行方案，实时推理会持续刷新。'
    : isEnvironmentFailed
      ? environmentLastError || '共享环境未准备完成，系统正在等待修复或重试。'
      : isRecovering
        ? environmentLastError || '共享环境恢复中，恢复完成后会重新开放批准'
        : isPreparing
          ? '正在预热共享沙箱环境，环境就绪后即可批准执行'
          : isStartingExecution
            ? '已发送批准请求，等待共享环境开始执行'
            : '共享环境已就绪，可以批准并开始并行执行';
  const rowActionLabel = isPlanning
    ? '主智能体正在校准子任务边界'
    : isEnvironmentFailed
      ? '共享环境异常，等待恢复后再启动'
      : isRecovering
        ? '共享环境恢复中，暂不启动任务'
        : isPreparing
          ? '共享环境准备中，等待开放批准'
          : isStartingExecution
            ? '批准已提交，等待执行层启动'
            : canConfirm
              ? '共享环境就绪，批准后即可启动'
              : '等待共享环境开放确认';
  const rowVisualStatus = isEnvironmentFailed
    ? 'failed'
    : isPlanning || isPreparing || isRecovering || isStartingExecution
      ? 'running'
      : 'pending';

  useEffect(() => {
    if (phase !== 'confirming') {
      setIsAwaitingExecutionStart(false);
    }
  }, [phase]);

  const hasDependencies = dependencies.length > 0;
  const estimatedMinutes = useMemo(() => {
    const estimate = Math.max(1, Math.ceil(subtasks.length * 1.5));
    return `${estimate}-${estimate + 3} 分钟`;
  }, [subtasks.length]);

  const latestTranscriptSnapshot = useMemo(() => {
    for (let index = mainTranscriptState.messages.length - 1; index >= 0; index -= 1) {
      const message = mainTranscriptState.messages[index];
      if (message?.type !== 'assistant') continue;
      const parsedContent = safeJsonParse<ParsedContent>(
        message.content,
        {} as ParsedContent,
      );
      const reasoning =
        typeof parsedContent.reasoning_content === 'string'
          ? parsedContent.reasoning_content
          : '';
      const text =
        typeof parsedContent.content === 'string' ? parsedContent.content : '';
      if (reasoning.trim() || text.trim()) {
        return { reasoning, text };
      }
    }

    return {
      reasoning: '',
      text: '',
    };
  }, [mainTranscriptState.messages]);

  const planningReasoning =
    mainTranscriptState.streamingReasoningContent ||
    latestTranscriptSnapshot.reasoning;
  const planningText =
    mainTranscriptState.streamingTextContent || latestTranscriptSnapshot.text;
  const hasPlanningTranscript =
    planningReasoning.trim().length > 0 || planningText.trim().length > 0;
  const confirmationRows = useMemo<ShadowCloneMonitorRow[]>(
    () =>
      subtasks.map((subtask, index) => ({
        id: subtask.id,
        index,
        role: truncateConfirmationText(subtask.role || subtask.id, 48),
        taskSummary: truncateConfirmationText(
          subtask.task_description || subtask.id || '暂无任务描述',
          120,
        ),
        detail: truncateConfirmationText(rowActionLabel, 92),
        status: rowVisualStatus,
        visualStatus: rowVisualStatus,
      })),
    [rowActionLabel, rowVisualStatus, subtasks],
  );

  const transcriptContent = hasPlanningTranscript ? (
    <div className="rounded-3xl border border-blue-100 bg-blue-50/70 p-5">
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span>{isPlanning ? '主智能体实时分析中' : '主智能体实时规划说明'}</span>
        <span className="min-w-0 truncate">{mainTranscriptState.latestLabel || '同步后端输出中'}</span>
      </div>

      <div className="max-h-64 space-y-4 overflow-y-auto pr-2 md:max-h-72 lg:max-h-[20rem]">
        {planningReasoning ? (
          <div className="rounded-2xl border border-blue-100 bg-white/85 p-4 shadow-sm">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-blue-600">
              推理过程
            </div>
            <p className="whitespace-pre-wrap break-words text-[15px] leading-7 text-slate-600">
              {planningReasoning}
            </p>
          </div>
        ) : null}

        {planningText ? (
          <div className="rounded-2xl border border-blue-100 bg-white/85 p-4 shadow-sm">
            <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-blue-600">
              规划输出
            </div>
            <p className="whitespace-pre-wrap break-words text-[15px] leading-7 text-slate-700">
              {planningText}
            </p>
          </div>
        ) : null}
      </div>
    </div>
  ) : null;

  const handleConfirm = async () => {
    if (!agentRunId) {
      toast.error('缺少运行标识，请重试。');
      return;
    }

    setIsSubmitting(true);
    try {
      await confirmProposal(agentRunId);
      setIsAwaitingExecutionStart(true);
      toast.success('已批准影分身执行计划');
    } catch (error) {
      console.error('[ShadowClone] confirm failed:', error);
      toast.error('批准影分身计划失败');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDeny = async () => {
    if (!agentRunId) {
      resetRuntime();
      return;
    }

    setIsSubmitting(true);
    try {
      await denyProposal(agentRunId);
      toast.message('已拒绝影分身计划，将回退到常规模式。');
    } catch (error) {
      console.error('[ShadowClone] deny failed:', error);
      toast.error('拒绝影分身计划失败');
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!isPlanning && !isConfirming) {
    return null;
  }

  return (
    <div className="mb-3 overflow-hidden rounded-3xl border border-slate-200 bg-white p-0 shadow-[0_24px_60px_-28px_rgba(15,23,42,0.35)]">
      <div className="border-b border-slate-200/80 px-6 py-5">
        <div className="flex items-center gap-2 text-slate-900">
          <GitBranch className="h-4 w-4 text-blue-600" />
          <h3 className="text-base font-semibold">
            {isPlanning ? '影分身执行规划中' : '影分身执行计划'}
          </h3>
        </div>
        <p className="pt-1 text-sm leading-6 text-slate-500">
          {isPlanning
            ? '主智能体正在分析是否启用影分身模式，并实时输出规划过程。'
            : (
              <>
                主智能体建议将当前请求拆分为 <strong>{subtasks.length}</strong>{' '}
                个子任务并行执行，预计耗时 {estimatedMinutes}。
              </>
            )}
        </p>
      </div>

      <div className="space-y-4 px-6 py-5">
        {transcriptContent}

        <div className="overflow-hidden rounded-3xl border border-slate-200 bg-slate-50/80">
          <div className="border-b border-slate-200/80 px-4 py-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <p className="text-sm font-semibold text-slate-900">{statusLabel}</p>
                <p className="mt-1 text-xs leading-5 text-slate-500">{statusDescription}</p>
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-full bg-slate-100 px-2.5 py-1 font-medium text-slate-600 whitespace-nowrap">
                {isPlanning ? `草案 ${subtasks.length}` : `子任务 ${subtasks.length}`}
              </span>
              <span className="rounded-full bg-blue-50 px-2.5 py-1 font-medium text-blue-700 whitespace-nowrap">
                状态 {statusLabel}
              </span>
              <span className="rounded-full bg-slate-100 px-2.5 py-1 font-medium text-slate-600 whitespace-nowrap">
                {hasDependencies
                  ? `依赖 ${dependencies.length}`
                  : isPlanning
                    ? '依赖分析中'
                    : '可并行执行'}
              </span>
              <span
                className={cn(
                  'rounded-full px-2.5 py-1 font-medium whitespace-nowrap',
                  !isPlanning && canConfirm
                    ? 'bg-emerald-50 text-emerald-700'
                    : 'bg-amber-50 text-amber-700',
                )}
              >
                {isPlanning ? '主智能体实时分析中' : `环境 ${canConfirm ? 'ready' : '未就绪'}`}
              </span>
            </div>

            {environmentLastError ? (
              <div className="mt-3 rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 break-words">
                {environmentLastError}
              </div>
            ) : null}
          </div>

          <ShadowCloneMonitorTable
            isZh={true}
            viewportClassName="max-h-80 bg-white"
            emptyState={
              <div className="flex items-start gap-3 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-blue-100 bg-blue-50">
                  <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-slate-900">
                    正在生成并行拆解草案
                  </div>
                  <p className="mt-1 text-xs leading-5 text-slate-500">
                    当前先展示主智能体的实时推理。待任务边界和依赖关系稳定后，会在这里出现可批准的子任务列表。
                  </p>
                </div>
              </div>
            }
          >
            {confirmationRows.length > 0 ? (
              <ShadowCloneMonitorRows rows={confirmationRows} isZh={true} />
            ) : undefined}
          </ShadowCloneMonitorTable>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs leading-5 text-slate-500">
          <span>
            {hasDependencies
              ? `依赖关系：${dependencies.length} 条`
              : isPlanning
                ? '依赖关系分析中'
                : '无依赖，可直接并行执行'}
          </span>
          <span>
            {isPlanning
              ? '规划完成后才会开放批准入口'
              : isEnvironmentFailed
                ? '环境异常时不会发出批准请求'
                : isRecovering
                  ? '恢复完成后才会重新开放批准'
                  : isPreparing
                    ? '环境就绪后可立即启动并行执行'
                    : isStartingExecution
                      ? '批准已提交，正在等待任务层启动'
                      : '批准后将进入并行执行阶段'}
          </span>
        </div>
      </div>

      {!isPlanning ? (
        <div className="flex flex-col-reverse gap-2 border-t border-slate-200/80 px-6 py-4 sm:flex-row sm:items-center sm:justify-end">
          <Button
            type="button"
            variant="outline"
            className="rounded-xl border-slate-200 text-slate-700 transition-all duration-200 hover:border-slate-300 hover:bg-slate-50"
            onClick={handleDeny}
            disabled={isSubmitting}
          >
            保持常规模式
          </Button>
          <Button
            type="button"
            className="rounded-xl bg-blue-600 text-white transition-all duration-200 hover:bg-blue-700"
            onClick={handleConfirm}
            disabled={isSubmitting || isStartingExecution || !canConfirm}
          >
            {isSubmitting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin text-white" />
                提交中
              </>
            ) : isEnvironmentFailed ? (
              '环境异常'
            ) : isRecovering ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin text-white" />
                恢复中
              </>
            ) : isPreparing ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin text-white" />
                环境准备中
              </>
            ) : isStartingExecution ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin text-white" />
                启动中
              </>
            ) : (
              '批准并执行'
            )}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
