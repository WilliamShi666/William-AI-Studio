'use client';

import { Project } from '@/lib/api';
import { getUserFriendlyToolName, safeJsonParse } from '@/components/thread/utils';
import React from 'react';
import { Slider } from '@/components/ui/slider';
import { Skeleton } from '@/components/ui/skeleton';
import { ApiMessageType } from '@/components/thread/types';
import { CircleDashed, X, ChevronLeft, ChevronRight, Computer, Radio, Maximize2, Minimize2, Copy, Check, ArrowLeft } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/use-mobile';
import { Button } from '@/components/ui/button';
import { ToolView } from './tool-views/wrapper';
import { motion, AnimatePresence } from 'framer-motion';
import { toast } from 'sonner';
import {
  Drawer,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import { useLanguage } from '@/contexts/LanguageContext';
import {
  useShadowCloneStore,
} from '@/lib/stores/shadow-clone-store';
import {
  getRightPanelMode,
  isRightPanelAgentActive,
  isRightPanelInspectionMode,
  isRightPanelWorkspaceMode,
  RIGHT_PANEL_MODE,
} from '@/lib/shadow-clone-right-panel-mode';
import { ShadowCloneConfirmation } from './shadow-clone-confirmation';
import { ShadowCloneMainMonitor } from './shadow-clone-main-monitor';
import {
  mergeNormalizedWriteFileArgs,
  stringifyNormalizedWriteFileArgs,
} from '@/lib/write-file-stream';

export interface ToolCallInput {
  assistantCall: {
    content?: string;
    name?: string;
    timestamp?: string;
  };
  toolResult?: {
    content?: string;
    isSuccess?: boolean;
    timestamp?: string;
  };
  messages?: ApiMessageType[];
}

interface ToolCallSidePanelProps {
  isOpen: boolean;
  onClose: () => void;
  toolCalls: ToolCallInput[];
  currentIndex: number;
  onNavigate: (newIndex: number) => void;
  externalNavigateToIndex?: number;
  messages?: ApiMessageType[];
  agentStatus: string;
  project?: Project;
  renderAssistantMessage?: (
    assistantContent?: string,
    toolContent?: string,
  ) => React.ReactNode;
  renderToolResult?: (
    toolContent?: string,
    isSuccess?: boolean,
  ) => React.ReactNode;
  isLoading?: boolean;
  agentName?: string;
  onFileClick?: (filePath: string) => void;
  disableInitialAnimation?: boolean;
  streamingText?: string; // Live JSON arguments from tool call chunk for streaming file content display
}

interface ToolCallSnapshot {
  id: string;
  toolCall: ToolCallInput;
  index: number;
  timestamp: number;
}

const FLOATING_LAYOUT_ID = 'tool-panel-float';
const CONTENT_LAYOUT_ID = 'tool-panel-content';

// Helper function to generate the computer title
const getComputerTitle = (agentName?: string, isZh = false): string => {
  if (agentName) {
    return isZh ? `${agentName} 的电脑` : `${agentName}'s Computer`;
  }
  return isZh ? 'Roys Alpha 的电脑' : "Roys Alpha's Computer";
};

// Reusable header component for the tool panel
interface PanelHeaderProps {
  agentName?: string;
  onClose: () => void;
  onReturnToMainView?: () => void;
  showReturnToMainView?: boolean;
  isStreaming?: boolean;
  variant?: 'drawer' | 'desktop' | 'motion';
  showMinimize?: boolean;
  hasToolResult?: boolean;
  layoutId?: string;
  isZh?: boolean;
}

const PanelHeader: React.FC<PanelHeaderProps> = ({
  agentName,
  onClose,
  onReturnToMainView,
  showReturnToMainView = false,
  isStreaming = false,
  variant = 'desktop',
  showMinimize = false,
  hasToolResult = false,
  layoutId,
  isZh = false,
}) => {
  const title = getComputerTitle(agentName, isZh);
  const minimizeLabel = isZh ? '最小化为浮窗预览' : 'Minimize to floating preview';
  const closeLabel = isZh ? '关闭' : 'Close';
  const returnLabel = isZh ? '返回主视图' : 'Back to main view';
  const runningLabel = isZh ? '运行中' : 'Running';
  const showReturnAction = showReturnToMainView && typeof onReturnToMainView === 'function';
  const renderReturnButton = (className: string) =>
    showReturnAction ? (
      <Button
        variant="ghost"
        size="sm"
        onClick={onReturnToMainView}
        className={cn('h-8 gap-1.5 rounded-xl px-2.5 text-xs text-zinc-600 hover:text-zinc-900', className)}
        title={returnLabel}
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">{returnLabel}</span>
      </Button>
    ) : null;

  if (variant === 'drawer') {
    return (
      <DrawerHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            {renderReturnButton('shrink-0')}
            <DrawerTitle className="truncate text-lg font-medium">
              {title}
            </DrawerTitle>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="h-8 w-8 shrink-0"
            title={minimizeLabel}
          >
            <Minimize2 className="h-4 w-4" />
          </Button>
        </div>
      </DrawerHeader>
    );
  }

  if (variant === 'motion') {
    return (
      <motion.div
        layoutId={layoutId}
        className="h-14 px-4 flex items-center"
      >
        <div className="flex items-center justify-between w-full">
          <motion.div layoutId="tool-icon" className="ml-2 flex min-w-0 items-center gap-2">
            {renderReturnButton('shrink-0')}
            <h2 className="truncate text-lg font-medium text-zinc-900 dark:text-zinc-100">
              {title}
            </h2>
          </motion.div>

          {hasToolResult && !isStreaming && (
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                onClick={onClose}
                className="h-8 w-8 ml-1"
                title={minimizeLabel}
              >
                <Minimize2 className="h-4 w-4" />
              </Button>
            </div>
          )}

          {isStreaming && (
            <div className="flex items-center gap-2">
              <div className="px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400 flex items-center gap-1.5">
                <CircleDashed className="h-3 w-3 animate-spin" />
                <span>{runningLabel}</span>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={onClose}
                className="h-8 w-8 ml-1"
                title={minimizeLabel}
              >
                <Minimize2 className="h-4 w-4" />
              </Button>
            </div>
          )}

          {!hasToolResult && !isStreaming && (
            <Button
              variant="ghost"
              size="icon"
              onClick={onClose}
              className="h-8 w-8"
              title={minimizeLabel}
            >
              <Minimize2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </motion.div>
    );
  }

  return (
    <div className="h-14 px-4 flex items-center">
      <div className="flex items-center justify-between w-full">
        <div className="ml-2 flex min-w-0 items-center gap-2">
          {renderReturnButton('shrink-0')}
          <h2 className="truncate text-lg font-medium text-zinc-900 dark:text-zinc-100">
            {title}
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {isStreaming && (
            <div className="px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400 flex items-center gap-1.5">
              <CircleDashed className="h-3 w-3 animate-spin" />
              <span>{runningLabel}</span>
            </div>
          )}
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="h-8 w-8"
            title={showMinimize ? minimizeLabel : closeLabel}
          >
            {showMinimize ? <Minimize2 className="h-4 w-4" /> : <X className="h-4 w-4" />}
          </Button>
        </div>
      </div>
    </div>
  );
};

interface ShadowCloneWorkspaceHeaderProps {
  onClose: () => void;
  onReturnToMainView?: () => void;
  showReturnToMainView?: boolean;
  isZh?: boolean;
}

const ShadowCloneWorkspaceHeader: React.FC<ShadowCloneWorkspaceHeaderProps> = ({
  onClose,
  onReturnToMainView,
  showReturnToMainView = false,
  isZh = false,
}) => {
  const minimizeLabel = isZh ? '最小化为浮窗预览' : 'Minimize to floating preview';
  const returnLabel = isZh ? '返回主视图' : 'Back to main view';
  const showReturnAction = showReturnToMainView && typeof onReturnToMainView === 'function';

  return (
    <div className="flex items-center justify-between gap-2 px-3 pb-2 pt-3">
      <div className="flex min-w-0 items-center gap-2">
        {showReturnAction && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onReturnToMainView}
            className="h-8 gap-1.5 rounded-xl px-2.5 text-xs text-zinc-600 hover:text-zinc-900"
            title={returnLabel}
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">{returnLabel}</span>
          </Button>
        )}
      </div>
      <Button
        variant="ghost"
        size="icon"
        onClick={onClose}
        className="h-8 w-8 rounded-xl text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
        title={minimizeLabel}
      >
        <Minimize2 className="h-4 w-4" />
      </Button>
    </div>
  );
};

interface ShadowCloneHandoffProps {
  agentStatus: string;
  isZh?: boolean;
}

const ShadowCloneHandoff: React.FC<ShadowCloneHandoffProps> = ({
  agentStatus,
  isZh = false,
}) => {
  const isActive = isRightPanelAgentActive(agentStatus);
  const title = isZh ? '正在切回主智能体' : 'Handing back to the main agent';
  const description = isZh
    ? '影分身执行已结束。主智能体恢复后的实时工具活动会继续显示在这里；若没有新的工具调用，面板会回到常规状态。'
    : 'Shadow Clone execution has wrapped. Main-agent tool activity will continue here if more tools run; otherwise the panel returns to normal.';
  const statusLabel = isActive
    ? isZh
      ? '正在交接'
      : 'Handoff in progress'
    : isZh
      ? '已交接'
      : 'Handoff complete';

  return (
    <div className="flex h-full items-center justify-center px-4 pb-4">
      <div className="w-full max-w-xl rounded-3xl border border-slate-200 bg-white/95 p-6 shadow-[0_24px_60px_-28px_rgba(15,23,42,0.35)]">
        <div className="flex items-center gap-3">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-blue-100 bg-blue-50 text-blue-600">
            {isActive ? (
              <CircleDashed className="h-5 w-5 animate-spin" />
            ) : (
              <Check className="h-5 w-5" />
            )}
          </div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-slate-900">{title}</p>
            <p className="mt-1 text-sm leading-6 text-slate-500">{description}</p>
          </div>
        </div>

        <div className="mt-4 inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium text-slate-600">
          {statusLabel}
        </div>
      </div>
    </div>
  );
};

export function ToolCallSidePanel({
  isOpen,
  onClose,
  toolCalls,
  currentIndex,
  onNavigate,
  messages,
  agentStatus,
  project,
  isLoading = false,
  externalNavigateToIndex,
  agentName,
  onFileClick,
  disableInitialAnimation,
  streamingText,
}: ToolCallSidePanelProps) {
  const { language } = useLanguage();
  const isZh = language === 'zh';
  const shadowClonePhase = useShadowCloneStore((state) => state.phase);
  const shadowCloneCurrentRunId = useShadowCloneStore((state) => state.currentRunId);
  const shadowCloneLiveActivity = useShadowCloneStore((state) => state.liveActivity);
  const shadowCloneViewScope = useShadowCloneStore((state) => state.viewScope);
  const activeSubtaskId = useShadowCloneStore((state) => state.activeSubtaskId);
  const shadowCloneSubtasks = useShadowCloneStore((state) => state.subtasks);
  const returnToShadowCloneMain = useShadowCloneStore((state) => state.returnToMainView);
  const shadowClonePanelState = useShadowCloneStore((state) =>
    activeSubtaskId ? state.subtaskPanelStates[activeSubtaskId] || null : null,
  );
  const activeShadowCloneSubtask = useShadowCloneStore((state) =>
    state.subtasks.find((item) => item.id === activeSubtaskId) || null,
  );
  const rightPanelMode = React.useMemo(
    () =>
      getRightPanelMode({
        phase: shadowClonePhase,
        viewScope: shadowCloneViewScope,
        activeSubtaskId,
        hasActiveSubtask: Boolean(activeShadowCloneSubtask),
        subtaskCount: shadowCloneSubtasks.length,
        liveActivityScope: shadowCloneLiveActivity?.scope,
        agentStatus,
      }),
    [
      activeShadowCloneSubtask,
      activeSubtaskId,
      agentStatus,
      shadowCloneLiveActivity?.scope,
      shadowClonePhase,
      shadowCloneSubtasks.length,
      shadowCloneViewScope,
    ],
  );
  const isShadowCloneContextActive = isRightPanelInspectionMode(rightPanelMode);
  const effectiveToolCalls = React.useMemo(
    () => (isShadowCloneContextActive ? (shadowClonePanelState?.toolCalls || []) : toolCalls),
    [isShadowCloneContextActive, shadowClonePanelState?.toolCalls, toolCalls],
  );
  const effectiveStreamingText = React.useMemo(
    () => (isShadowCloneContextActive ? shadowClonePanelState?.streamingText || '' : streamingText || ''),
    [isShadowCloneContextActive, shadowClonePanelState?.streamingText, streamingText],
  );
  const effectiveAgentStatus = React.useMemo(() => {
    if (!isShadowCloneContextActive) {
      return agentStatus;
    }

    if (activeShadowCloneSubtask?.status === 'running') {
      return 'running';
    }

    if (activeShadowCloneSubtask?.status === 'pending') {
      return 'connecting';
    }

    return 'idle';
  }, [isShadowCloneContextActive, activeShadowCloneSubtask?.status, agentStatus]);
  const isShadowCloneConfirmationActive =
    rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION;
  const isShadowCloneMainMonitorActive =
    rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR;
  const isShadowCloneHandoffActive =
    rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_HANDOFF;
  const isShadowCloneWorkspaceActive = isRightPanelWorkspaceMode(rightPanelMode);
  const shadowCloneSelectedToolPanelTestId = isShadowCloneContextActive
    ? 'shadow-clone-selected-subagent-tool-panel'
    : undefined;
  const labels = React.useMemo(() => ({
    running: isZh ? '运行中' : 'Running',
    liveUpdates: isZh ? '实时更新' : 'Live updates',
    latestTool: isZh ? '最新工具' : 'Latest tool',
    jumpToLive: isZh ? '跳到实时' : 'Jump to live',
    jumpToLatest: isZh ? '跳到最新' : 'Jump to latest',
    noToolActivity: isZh ? '暂无工具活动' : 'No tool activity',
    noToolActivityDescription: isZh
      ? '工具调用和计算机操作执行时会显示在这里。'
      : "Tool calls and computer interactions will appear here when they're being executed.",
    toolRunning: isZh ? '工具运行中' : 'Tool is running',
    toolFallback: isZh ? '工具' : 'Tool',
    prev: isZh ? '上一项' : 'Prev',
    next: isZh ? '下一项' : 'Next',
    copySuccess: isZh ? '内容已复制到剪贴板' : 'File content copied to clipboard',
    copyFailed: isZh ? '复制内容失败' : 'Failed to copy file content',
  }), [isZh]);
  const [dots, setDots] = React.useState('');
  const [internalIndex, setInternalIndex] = React.useState(0);
  const [navigationMode, setNavigationMode] = React.useState<'live' | 'manual'>('live');
  const hasInitializedSnapshotsRef = React.useRef(false);
  const lastPanelContextRef = React.useRef('main-agent');

  // Add copy functionality state
  const [isCopyingContent, setIsCopyingContent] = React.useState(false);

  const isMobile = useIsMobile();
  const panelContextKey = isShadowCloneContextActive
    ? `shadow-clone:${rightPanelMode.subtaskId}`
    : isShadowCloneWorkspaceActive
      ? `shadow-clone:${rightPanelMode.kind}`
      : 'main-agent';
  const toolCallSnapshots = React.useMemo(
    () =>
      effectiveToolCalls.map((toolCall, index) => ({
        id: `${index}-${toolCall.assistantCall.timestamp || toolCall.toolResult?.timestamp || 'no-timestamp'}`,
        toolCall,
        index,
        timestamp:
          Date.parse(toolCall.assistantCall.timestamp || toolCall.toolResult?.timestamp || '') ||
          index,
      })),
    [effectiveToolCalls],
  );

  const handleClose = React.useCallback(() => {
    if (isShadowCloneContextActive) {
      returnToShadowCloneMain();
    }
    onClose();
  }, [isShadowCloneContextActive, onClose, returnToShadowCloneMain]);

  React.useEffect(() => {
    const didContextChange = lastPanelContextRef.current !== panelContextKey;

    if (didContextChange) {
      lastPanelContextRef.current = panelContextKey;
      hasInitializedSnapshotsRef.current = toolCallSnapshots.length > 0;
      setNavigationMode('live');
      setInternalIndex(toolCallSnapshots.length > 0 ? toolCallSnapshots.length - 1 : 0);
      return;
    }

    if (toolCallSnapshots.length === 0) {
      hasInitializedSnapshotsRef.current = false;
      setInternalIndex(0);
      return;
    }

    if (!hasInitializedSnapshotsRef.current) {
      hasInitializedSnapshotsRef.current = true;
      setInternalIndex(toolCallSnapshots.length - 1);
    }
  }, [panelContextKey, toolCallSnapshots.length]);

  // 简化的索引同步，完全避免 toolCallSnapshots 依赖
  React.useEffect(() => {
    if (isShadowCloneContextActive) return;
    setInternalIndex(currentIndex);
  }, [currentIndex, isShadowCloneContextActive]);

  React.useEffect(() => {
    if (!isShadowCloneContextActive || toolCallSnapshots.length === 0) return;
    if (navigationMode === 'live') {
      setInternalIndex(toolCallSnapshots.length - 1);
    } else if (internalIndex >= toolCallSnapshots.length) {
      setInternalIndex(toolCallSnapshots.length - 1);
    }
  }, [
    isShadowCloneContextActive,
    toolCallSnapshots.length,
    navigationMode,
    internalIndex,
  ]);

  const safeInternalIndex = Math.min(internalIndex, Math.max(0, toolCallSnapshots.length - 1));
  const currentSnapshot = toolCallSnapshots[safeInternalIndex];
  const currentToolCall = currentSnapshot?.toolCall;
  const totalCalls = toolCallSnapshots.length;
  const snapshotIndexById = React.useMemo(
    () => new Map(toolCallSnapshots.map((snapshot, index) => [snapshot.id, index])),
    [toolCallSnapshots],
  );

  const completedToolCalls = React.useMemo(
    () =>
      toolCallSnapshots.filter(
        (snapshot) =>
          snapshot.toolCall.toolResult?.content &&
          snapshot.toolCall.toolResult.content !== 'STREAMING',
      ),
    [toolCallSnapshots],
  );
  const totalCompletedCalls = completedToolCalls.length;

  const streamingFileHint = React.useMemo(() => {
    if (!effectiveStreamingText || typeof effectiveStreamingText !== 'string') {
      return null;
    }

    const parsed = safeJsonParse<Record<string, any> | null>(effectiveStreamingText, null);
    if (parsed && typeof parsed === 'object') {
      const filePath = parsed.file_path || parsed.path || parsed.target_file || null;
      const fileContents =
        parsed.file_contents ?? parsed.content ?? parsed.file_contents_delta ?? null;
      if (typeof fileContents === 'string' || typeof filePath === 'string') {
        return {
          filePath: typeof filePath === 'string' ? filePath : null,
          hasFileContents: typeof fileContents === 'string' && fileContents.length > 0,
        };
      }

      if (typeof parsed.task === 'string') {
        const taskPathMatch = parsed.task.match(/\/workspace\/[^\s"'`]+/i);
        const hasWriteFile = /write[_-]?file/i.test(parsed.task);
        if (taskPathMatch && hasWriteFile) {
          return { filePath: taskPathMatch[0], hasFileContents: false };
        }
      }
    }

    const hasWriteFile = /write[_-]?file/i.test(effectiveStreamingText);
    const pathMatch = effectiveStreamingText.match(/\/workspace\/[^\s"'`]+/i);
    if (hasWriteFile && pathMatch) {
      return { filePath: pathMatch[0], hasFileContents: false };
    }

    return null;
  }, [effectiveStreamingText]);

  const streamingSnapshots = React.useMemo(
    () =>
      toolCallSnapshots.filter(
        (snapshot) => snapshot.toolCall.toolResult?.content === 'STREAMING',
      ),
    [toolCallSnapshots],
  );
  const latestStreamingSnapshot = streamingSnapshots[streamingSnapshots.length - 1];
  const liveWriteFileContent = React.useMemo(() => {
    const snapshotContent = latestStreamingSnapshot?.toolCall?.assistantCall?.content;
    const mergedWriteFileArgs = mergeNormalizedWriteFileArgs(
      snapshotContent,
      effectiveStreamingText,
    );

    return (
      stringifyNormalizedWriteFileArgs(mergedWriteFileArgs) ||
      snapshotContent ||
      effectiveStreamingText
    );
  }, [
    effectiveStreamingText,
    latestStreamingSnapshot?.toolCall?.assistantCall?.content,
  ]);
  const {
    displayToolCall,
    displayIndex,
    displayTotalCalls,
    isCurrentToolStreaming,
  } = React.useMemo(() => {
    let nextToolCall = currentToolCall;
    let nextIndex = safeInternalIndex;
    let nextTotalCalls = totalCalls;

    if (
      navigationMode === 'live' &&
      effectiveAgentStatus === 'running' &&
      latestStreamingSnapshot
    ) {
      nextToolCall = latestStreamingSnapshot.toolCall;
      nextIndex = latestStreamingSnapshot.index;
    }

    if (
      navigationMode === 'live' &&
      effectiveAgentStatus === 'running' &&
      streamingFileHint
    ) {
      nextToolCall = {
        assistantCall: {
          name: 'write-file',
          content: liveWriteFileContent,
          timestamp:
            latestStreamingSnapshot?.toolCall?.assistantCall?.timestamp ||
            new Date().toISOString(),
        },
        toolResult: {
          content: 'STREAMING',
          isSuccess: true,
          timestamp: new Date().toISOString(),
        },
      };
    }

    const nextIsCurrentToolStreaming =
      nextToolCall?.toolResult?.content === 'STREAMING';

    if (!nextIsCurrentToolStreaming) {
      const completedIndex = completedToolCalls.findIndex(
        (snapshot) => snapshot.id === currentSnapshot?.id,
      );
      if (completedIndex >= 0) {
        nextIndex = completedIndex;
        nextTotalCalls = totalCompletedCalls;
      }
    }

    return {
      displayToolCall: nextToolCall,
      displayIndex: nextIndex,
      displayTotalCalls: nextTotalCalls,
      isCurrentToolStreaming: nextIsCurrentToolStreaming,
    };
  }, [
    completedToolCalls,
    currentSnapshot?.id,
    currentToolCall,
    effectiveAgentStatus,
    latestStreamingSnapshot,
    liveWriteFileContent,
    navigationMode,
    safeInternalIndex,
    streamingFileHint,
    totalCalls,
    totalCompletedCalls,
  ]);

  const toolViewStreamingText = React.useMemo(() => {
    const currentToolName = displayToolCall?.assistantCall?.name || '';
    const isWriteFileTool = /write[_-]?file/i.test(currentToolName);

    if (!isWriteFileTool) {
      return effectiveStreamingText;
    }

    if (typeof liveWriteFileContent === 'string' && liveWriteFileContent.trim()) {
      return liveWriteFileContent;
    }

    if (
      typeof displayToolCall?.assistantCall?.content === 'string' &&
      displayToolCall.assistantCall.content.trim()
    ) {
      return displayToolCall.assistantCall.content;
    }

    return effectiveStreamingText;
  }, [
    displayToolCall,
    effectiveStreamingText,
    liveWriteFileContent,
  ]);

  const isStreaming = displayToolCall?.toolResult?.content === 'STREAMING';

  // Extract actual success value from tool content with fallbacks
  const getActualSuccess = (toolCall: any): boolean => {
    const content = toolCall?.toolResult?.content;
    if (!content) return toolCall?.toolResult?.isSuccess ?? true;

    const safeParse = (data: any) => {
      try { return typeof data === 'string' ? JSON.parse(data) : data; }
      catch { return null; }
    };

    const parsed = safeParse(content);
    if (!parsed) return toolCall?.toolResult?.isSuccess ?? true;

    if (parsed.content) {
      const inner = safeParse(parsed.content);
      if (inner?.tool_execution?.result?.success !== undefined) {
        return inner.tool_execution.result.success;
      }
    }
    const success = parsed.tool_execution?.result?.success ??
      parsed.result?.success ??
      parsed.success;

    return success !== undefined ? success : (toolCall?.toolResult?.isSuccess ?? true);
  };

  const isSuccess = isStreaming ? true : getActualSuccess(displayToolCall);

  // Copy functions
  const copyToClipboard = React.useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.error('Failed to copy text: ', err);
      return false;
    }
  }, []);

  const handleCopyContent = React.useCallback(async () => {
    const toolContent = displayToolCall?.toolResult?.content;
    if (!toolContent || toolContent === 'STREAMING') return;

    // Try to extract file content from tool result
    let fileContent = '';

    // If the tool result is JSON, try to extract file content
    try {
      const parsed = JSON.parse(toolContent);
      if (parsed.content && typeof parsed.content === 'string') {
        fileContent = parsed.content;
      } else if (parsed.file_content && typeof parsed.file_content === 'string') {
        fileContent = parsed.file_content;
      } else if (parsed.result && typeof parsed.result === 'string') {
        fileContent = parsed.result;
      } else if (parsed.toolOutput && typeof parsed.toolOutput === 'string') {
        fileContent = parsed.toolOutput;
      } else {
        // If no string content found, stringify the object
        fileContent = JSON.stringify(parsed, null, 2);
      }
    } catch (e) {
      // If it's not JSON, use the content as is
      fileContent = typeof toolContent === 'string' ? toolContent : JSON.stringify(toolContent, null, 2);
    }

    setIsCopyingContent(true);
    const success = await copyToClipboard(fileContent);
    if (success) {
      toast.success(labels.copySuccess);
    } else {
      toast.error(labels.copyFailed);
    }
    setTimeout(() => setIsCopyingContent(false), 500);
  }, [displayToolCall?.toolResult?.content, copyToClipboard, labels]);

  const internalNavigate = React.useCallback((newIndex: number, source: string = 'internal') => {
    if (newIndex < 0 || newIndex >= totalCalls) return;

    const isNavigatingToLatest = newIndex === totalCalls - 1;
    setInternalIndex(newIndex);

    if (isNavigatingToLatest) {
      setNavigationMode('live');
    } else {
      setNavigationMode('manual');
    }

    if (source === 'user_explicit' && !isShadowCloneContextActive) {
      onNavigate(newIndex);
    }
  }, [totalCalls, onNavigate, isShadowCloneContextActive]);

  const isLiveMode = navigationMode === 'live';
  const showJumpToLive = navigationMode === 'manual' && effectiveAgentStatus === 'running';
  const showJumpToLatest = navigationMode === 'manual' && effectiveAgentStatus !== 'running';

  const navigateToPrevious = React.useCallback(() => {
    // 🔧 修复：在streaming状态下也允许导航到之前的任务
    if (displayIndex > 0) {
      // 如果当前正在streaming，直接使用toolCallSnapshots而不是completedToolCalls
      const targetSnapshots = isCurrentToolStreaming ? toolCallSnapshots : completedToolCalls;
      const targetIndex = displayIndex - 1;
      const targetSnapshot = targetSnapshots[targetIndex];

      if (targetSnapshot) {
        const actualIndex = snapshotIndexById.get(targetSnapshot.id);
        if (actualIndex !== undefined) {
          setNavigationMode('manual');
          internalNavigate(actualIndex, 'user_explicit');
        }
      }
    }
  }, [displayIndex, isCurrentToolStreaming, completedToolCalls, internalNavigate, snapshotIndexById, toolCallSnapshots]);

  const navigateToNext = React.useCallback(() => {
    // 🔧 修复：在streaming状态下也允许导航到后面的任务
    if (displayIndex < displayTotalCalls - 1) {
      // 如果当前正在streaming，直接使用toolCallSnapshots而不是completedToolCalls
      const targetSnapshots = isCurrentToolStreaming ? toolCallSnapshots : completedToolCalls;
      const targetIndex = displayIndex + 1;
      const targetSnapshot = targetSnapshots[targetIndex];

      if (targetSnapshot) {
        const actualIndex = snapshotIndexById.get(targetSnapshot.id);
        if (actualIndex !== undefined) {
          // 如果导航到最后一个，进入live模式
          const isLastTool = targetIndex === targetSnapshots.length - 1;
          if (isLastTool) {
            setNavigationMode('live');
          } else {
            setNavigationMode('manual');
          }
          internalNavigate(actualIndex, 'user_explicit');
        }
      }
    }
  }, [displayIndex, displayTotalCalls, isCurrentToolStreaming, completedToolCalls, internalNavigate, snapshotIndexById, toolCallSnapshots]);

  const jumpToLive = React.useCallback(() => {
    setNavigationMode('live');
    internalNavigate(totalCalls - 1, 'user_explicit');
  }, [totalCalls, internalNavigate]);

  const jumpToLatest = React.useCallback(() => {
    setNavigationMode('manual');
    internalNavigate(totalCalls - 1, 'user_explicit');
  }, [totalCalls, internalNavigate]);

  const renderStatusButton = React.useCallback(() => {
    const baseClasses = "flex items-center justify-center gap-1.5 px-2 py-0.5 rounded-full w-[116px]";
    const dotClasses = "w-1.5 h-1.5 rounded-full";
    const textClasses = "text-xs font-medium";

    if (isLiveMode) {
      if (effectiveAgentStatus === 'running') {
        return (
          <div className={`${baseClasses} bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800`}>
            <div className={`${dotClasses} bg-green-500 animate-pulse`} />
            <span className={`${textClasses} text-green-700 dark:text-green-400`}>{labels.liveUpdates}</span>
          </div>
        );
      } else {
        return (
          <div className={`${baseClasses} bg-neutral-50 dark:bg-neutral-900/20 border border-neutral-200 dark:border-neutral-800`}>
            <div className={`${dotClasses} bg-neutral-500`} />
            <span className={`${textClasses} text-neutral-700 dark:text-neutral-400`}>{labels.latestTool}</span>
          </div>
        );
      }
    } else {
      if (effectiveAgentStatus === 'running') {
        return (
          <div
            className={`${baseClasses} bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 hover:bg-green-100 dark:hover:bg-green-900/30 transition-colors cursor-pointer`}
            onClick={jumpToLive}
          >
            <div className={`${dotClasses} bg-green-500 animate-pulse`} />
            <span className={`${textClasses} text-green-700 dark:text-green-400`}>{labels.jumpToLive}</span>
          </div>
        );
      } else {
        return (
          <div
            className={`${baseClasses} bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 hover:bg-blue-100 dark:hover:bg-blue-900/30 transition-colors cursor-pointer`}
            onClick={jumpToLatest}
          >
            <div className={`${dotClasses} bg-blue-500`} />
            <span className={`${textClasses} text-blue-700 dark:text-blue-400`}>{labels.jumpToLatest}</span>
          </div>
        );
      }
    }
  }, [isLiveMode, effectiveAgentStatus, jumpToLive, jumpToLatest, labels]);

  const handleSliderChange = React.useCallback(([newValue]: [number]) => {
    // 🔧 修复：在streaming状态下也允许通过滑动条导航
    const targetSnapshots = isCurrentToolStreaming ? toolCallSnapshots : completedToolCalls;
    const targetSnapshot = targetSnapshots[newValue];

    if (targetSnapshot) {
      const actualIndex = snapshotIndexById.get(targetSnapshot.id);
      if (actualIndex !== undefined) {
        const isLastTool = newValue === targetSnapshots.length - 1;
        if (isLastTool) {
          setNavigationMode('live');
        } else {
          setNavigationMode('manual');
        }

        internalNavigate(actualIndex, 'user_explicit');
      }
    }
  }, [isCurrentToolStreaming, completedToolCalls, internalNavigate, snapshotIndexById, toolCallSnapshots]);

  React.useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      // Close panel with Cmd/Ctrl+I
      if ((event.metaKey || event.ctrlKey) && event.key === 'i') {
        event.preventDefault();
        handleClose();
        return;
      }

      if (isShadowCloneWorkspaceActive) {
        return;
      }

      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        navigateToPrevious();
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        navigateToNext();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    isOpen,
    handleClose,
    isShadowCloneWorkspaceActive,
    navigateToPrevious,
    navigateToNext,
  ]);

  React.useEffect(() => {
    if (!isOpen) return;
    const handleSidebarToggle = (event: CustomEvent) => {
      if (event.detail.expanded) {
        handleClose();
      }
    };

    window.addEventListener(
      'sidebar-left-toggled',
      handleSidebarToggle as EventListener,
    );
    return () =>
      window.removeEventListener(
        'sidebar-left-toggled',
        handleSidebarToggle as EventListener,
      );
  }, [isOpen, handleClose]);

  React.useEffect(() => {
    if (isShadowCloneWorkspaceActive) {
      return;
    }
    if (externalNavigateToIndex !== undefined && externalNavigateToIndex >= 0 && externalNavigateToIndex < totalCalls) {
      internalNavigate(externalNavigateToIndex, 'external_click');
    }
  }, [externalNavigateToIndex, internalNavigate, isShadowCloneWorkspaceActive, totalCalls]);

  React.useEffect(() => {
    if (!isStreaming) return;
    const interval = setInterval(() => {
      setDots((prev) => {
        if (prev === '...') return '';
        return prev + '.';
      });
    }, 500);

    return () => clearInterval(interval);
  }, [isStreaming]);

  if (!isOpen) {
    return null;
  }

  if (isLoading) {
    if (isMobile) {
      return (
        <Drawer open={isOpen} onOpenChange={(open) => !open && handleClose()}>
          <DrawerContent
          className="h-[85vh] tool-panel"
          data-testid={shadowCloneSelectedToolPanelTestId}
          data-subtask-id={isShadowCloneContextActive ? activeSubtaskId || undefined : undefined}
        >
            <PanelHeader
              isZh={isZh}
              agentName={agentName}
              onClose={handleClose}
              variant="drawer"
            />

            <div className="flex-1 p-4 overflow-auto">
              <div className="space-y-4">
                <Skeleton className="h-8 w-32" />
                <Skeleton className="h-20 w-full rounded-md" />
                <Skeleton className="h-40 w-full rounded-md" />
                <Skeleton className="h-20 w-full rounded-md" />
              </div>
            </div>
          </DrawerContent>
        </Drawer>
      );
    }

    return (
      <div className="fixed inset-0 z-30 pointer-events-none">
        <div className="p-4 h-full flex items-stretch justify-end pointer-events-auto">
          <div className="border rounded-2xl flex flex-col shadow-2xl bg-background w-[90%] sm:w-[450px] md:w-[500px] lg:w-[550px] xl:w-[650px]">
            <div className="flex-1 flex flex-col overflow-hidden">
              <div className="flex flex-col h-full">
                <PanelHeader
                  isZh={isZh}
                  agentName={agentName}
                  onClose={handleClose}
                  showMinimize={true}
                />
                <div className="flex-1 p-4 overflow-auto">
                  <div className="space-y-4">
                    <Skeleton className="h-8 w-32" />
                    <Skeleton className="h-20 w-full rounded-md" />
                    <Skeleton className="h-40 w-full rounded-md" />
                    <Skeleton className="h-20 w-full rounded-md" />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const renderContent = () => {
    if (isShadowCloneConfirmationActive) {
      return (
        <div className="flex h-full flex-col">
          <ShadowCloneWorkspaceHeader isZh={isZh} onClose={handleClose} />
          <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">
            <ShadowCloneConfirmation agentRunId={shadowCloneCurrentRunId} />
          </div>
        </div>
      );
    }

    if (isShadowCloneMainMonitorActive) {
      return (
        <div className="flex h-full flex-col">
          <ShadowCloneWorkspaceHeader isZh={isZh} onClose={handleClose} />
          <div className="min-h-0 flex-1 overflow-hidden">
            <ShadowCloneMainMonitor isZh={isZh} messages={messages} />
          </div>
        </div>
      );
    }

    if (isShadowCloneContextActive) {
      // Subagent inspection intentionally falls through to the normal tool-call
      // renderer below. The left chat panel owns the selected subagent transcript;
      // this right panel owns the selected subagent's bash/write/tool activity.
    }

    if (isShadowCloneHandoffActive) {
      return (
        <div className="flex h-full flex-col">
          <ShadowCloneWorkspaceHeader isZh={isZh} onClose={handleClose} />
          <div className="min-h-0 flex-1 overflow-hidden">
            <ShadowCloneHandoff agentStatus={agentStatus} isZh={isZh} />
          </div>
        </div>
      );
    }

    if (!displayToolCall && toolCallSnapshots.length === 0) {
      return (
        <div className="flex flex-col h-full">
          {!isMobile && (
            <PanelHeader
              isZh={isZh}
              agentName={isShadowCloneContextActive ? activeShadowCloneSubtask?.role || activeShadowCloneSubtask?.id || agentName : agentName}
              onClose={handleClose}
              onReturnToMainView={returnToShadowCloneMain}
              showReturnToMainView={isShadowCloneContextActive}
            />
          )}
          <div className="flex flex-col items-center justify-center flex-1 p-8">
            <div className="flex flex-col items-center space-y-4 max-w-sm text-center">
              <div className="relative">
                <div className="w-16 h-16 bg-zinc-100 dark:bg-zinc-800 rounded-full flex items-center justify-center">
                  <Computer className="h-8 w-8 text-zinc-400 dark:text-zinc-500" />
                </div>
                <div className="absolute -bottom-1 -right-1 w-6 h-6 bg-zinc-200 dark:bg-zinc-700 rounded-full flex items-center justify-center">
                  <div className="w-2 h-2 bg-zinc-400 dark:text-zinc-500 rounded-full"></div>
                </div>
              </div>
              <div className="space-y-2">
                <h3 className="text-lg font-medium text-zinc-900 dark:text-zinc-100">
                  {isShadowCloneContextActive && effectiveAgentStatus === 'running'
                    ? labels.toolRunning
                    : labels.noToolActivity}
                </h3>
                <p className="text-sm text-zinc-500 dark:text-zinc-400 leading-relaxed">
                  {isShadowCloneContextActive && effectiveAgentStatus === 'running'
                    ? (isZh
                        ? '所选影分身正在执行任务，实时工具活动会在到达后显示在这里。'
                        : 'The selected shadow clone is running. Live tool activity will appear here as it arrives.')
                    : labels.noToolActivityDescription}
                </p>
              </div>
            </div>
          </div>
        </div>
      );
    }

    if (!displayToolCall && toolCallSnapshots.length > 0) {
      const firstStreamingTool = toolCallSnapshots.find(s => s.toolCall.toolResult?.content === 'STREAMING');
      if (firstStreamingTool && totalCompletedCalls === 0) {
        const toolLabel = getUserFriendlyToolName(
          firstStreamingTool.toolCall.assistantCall.name || labels.toolFallback,
        );
        const runningDescription = isZh
          ? `${toolLabel} 正在执行，完成后会在这里显示结果。`
          : `${toolLabel} is currently executing. Results will appear here when complete.`;
        return (
          <div className="flex flex-col h-full">
            {!isMobile && (
              <PanelHeader
                isZh={isZh}
                agentName={isShadowCloneContextActive ? activeShadowCloneSubtask?.role || activeShadowCloneSubtask?.id || agentName : agentName}
                onClose={handleClose}
                isStreaming={true}
                onReturnToMainView={returnToShadowCloneMain}
                showReturnToMainView={isShadowCloneContextActive}
              />
            )}
            {isMobile && (
              <div className="px-4 pb-2">
                <div className="flex items-center justify-center">
                  <div className="px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400 flex items-center gap-1.5">
                    <CircleDashed className="h-3 w-3 animate-spin" />
                    <span>{labels.running}</span>
                  </div>
                </div>
              </div>
            )}
            <div className="flex flex-col items-center justify-center flex-1 p-8">
              <div className="flex flex-col items-center space-y-4 max-w-sm text-center">
                <div className="relative">
                  <div className="w-16 h-16 bg-blue-50 dark:bg-blue-900/20 rounded-full flex items-center justify-center">
                    <CircleDashed className="h-8 w-8 text-blue-500 dark:text-blue-400 animate-spin" />
                  </div>
                </div>
                <div className="space-y-2">
                  <h3 className="text-lg font-medium text-zinc-900 dark:text-zinc-100">
                    {labels.toolRunning}
                  </h3>
                  <p className="text-sm text-zinc-500 dark:text-zinc-400 leading-relaxed">
                    {runningDescription}
                  </p>
                </div>
              </div>
            </div>
          </div>
        );
      }

      return (
        <div className="flex flex-col h-full">
          {!isMobile && (
            <PanelHeader
              isZh={isZh}
              agentName={isShadowCloneContextActive ? activeShadowCloneSubtask?.role || activeShadowCloneSubtask?.id || agentName : agentName}
              onClose={handleClose}
              onReturnToMainView={returnToShadowCloneMain}
              showReturnToMainView={isShadowCloneContextActive}
            />
          )}
          <div className="flex-1 p-4 overflow-auto">
            <div className="space-y-4">
              <Skeleton className="h-8 w-32" />
              <Skeleton className="h-20 w-full rounded-md" />
            </div>
          </div>
        </div>
      );
    }

    const toolView = (
      <ToolView
        name={displayToolCall.assistantCall.name}
        assistantContent={displayToolCall.assistantCall.content}
        toolContent={displayToolCall.toolResult?.content}
        assistantTimestamp={displayToolCall.assistantCall.timestamp}
        toolTimestamp={displayToolCall.toolResult?.timestamp}
        isSuccess={isSuccess}
        isStreaming={isStreaming}
        project={project}
        messages={messages}
        agentStatus={effectiveAgentStatus}
        currentIndex={displayIndex}
        totalCalls={displayTotalCalls}
        onFileClick={onFileClick}
        streamingText={toolViewStreamingText}
      />
    );

    return (
      <div className="flex flex-col h-full">
        {!isMobile && (
          <PanelHeader
            isZh={isZh}
            agentName={isShadowCloneContextActive ? activeShadowCloneSubtask?.role || activeShadowCloneSubtask?.id || agentName : agentName}
            onClose={handleClose}
            isStreaming={isStreaming}
            variant="motion"
            hasToolResult={!!displayToolCall.toolResult?.content}
            layoutId={CONTENT_LAYOUT_ID}
            onReturnToMainView={returnToShadowCloneMain}
            showReturnToMainView={isShadowCloneContextActive}
          />
        )}

        <div className="flex-1 overflow-auto scrollbar-thin scrollbar-thumb-zinc-300 dark:scrollbar-thumb-zinc-700 scrollbar-track-transparent">
          {toolView}
        </div>
      </div>
    );
  };

  // Mobile version - use drawer
  if (isMobile) {
    return (
      <Drawer open={isOpen} onOpenChange={(open) => !open && handleClose()}>
        <DrawerContent
          className="h-[85vh] tool-panel"
          data-testid={shadowCloneSelectedToolPanelTestId}
          data-subtask-id={isShadowCloneContextActive ? activeSubtaskId || undefined : undefined}
        >
          {isShadowCloneWorkspaceActive ? (
            <DrawerHeader className="sr-only">
              <DrawerTitle>
                {isZh ? '影分身工作区' : 'Shadow Clone workspace'}
              </DrawerTitle>
            </DrawerHeader>
          ) : (
            <PanelHeader
              isZh={isZh}
              agentName={agentName}
              onClose={handleClose}
              variant="drawer"
            />
          )}

          <div className="flex-1 flex flex-col overflow-hidden">
            {renderContent()}
          </div>

          {!isShadowCloneWorkspaceActive &&
            (displayTotalCalls > 1 || (isCurrentToolStreaming && totalCompletedCalls > 0)) && (
            <div className="border-t border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-3">
              <div className="flex items-center justify-between">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={navigateToPrevious}
                  disabled={displayIndex <= 0}
                  className="h-8 px-2.5 text-xs"
                >
                  <ChevronLeft className="h-3.5 w-3.5 mr-1" />
                  <span>{labels.prev}</span>
                </Button>

                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-zinc-600 dark:text-zinc-400 font-medium tabular-nums min-w-[44px]">
                    {displayIndex + 1}/{displayTotalCalls}
                  </span>
                  {renderStatusButton()}
                </div>

                <Button
                  variant="outline"
                  size="sm"
                  onClick={navigateToNext}
                  disabled={displayIndex >= displayTotalCalls - 1}
                  className="h-8 px-2.5 text-xs"
                >
                  <span>{labels.next}</span>
                  <ChevronRight className="h-3.5 w-3.5 ml-1" />
                </Button>
              </div>
            </div>
          )}
        </DrawerContent>
      </Drawer>
    );
  }

  // Desktop version - use fixed panel
  return (
    <AnimatePresence mode="wait">
      {isOpen && (
        <motion.div
          key="sidepanel"
          layoutId={FLOATING_LAYOUT_ID}
          initial={disableInitialAnimation ? { opacity: 1 } : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{
            opacity: { duration: disableInitialAnimation ? 0 : 0.15 },
            layout: {
              type: "spring",
              stiffness: 400,
              damping: 35
            }
          }}
          data-testid={shadowCloneSelectedToolPanelTestId}
          data-subtask-id={isShadowCloneContextActive ? activeSubtaskId || undefined : undefined}
          className="fixed top-0 right-3 bottom-3 tool-panel
           flex flex-col z-30
           w-[40vw] sm:w-[450px] md:w-[500px] lg:w-[550px] xl:w-[645px]
           bg-sidebar/90 backdrop-blur-xl dark:bg-card
           border border-border/60 dark:border-[oklch(1_0_0/10%)]
           rounded-3xl
           shadow-[0_20px_60px_-12px_oklch(0.4_0.015_75/15%)]
           dark:shadow-none"
          style={{
            overflow: 'hidden',
          }}
        >
          <div className="flex-1 flex flex-col overflow-hidden bg-transparent">
            {renderContent()}
          </div>
          {!isShadowCloneWorkspaceActive &&
            (displayTotalCalls > 1 || (isCurrentToolStreaming && totalCompletedCalls > 0)) && (
            <div className="border-t border-border/50 dark:border-[oklch(1_0_0/8%)] bg-muted/30 dark:bg-[oklch(0.22_0.018_250/50%)] backdrop-blur-sm px-4 py-3">
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={navigateToPrevious}
                    disabled={displayIndex <= 0}
                    className="h-7 w-7 text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </Button>
                  <span className="text-xs text-zinc-600 dark:text-zinc-400 font-medium tabular-nums px-1 min-w-[44px] text-center">
                    {displayIndex + 1}/{displayTotalCalls}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={navigateToNext}
                    disabled={displayIndex >= displayTotalCalls - 1}
                    className="h-7 w-7 text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>

                <div className="flex-1 relative">
                  <Slider
                    min={0}
                    max={displayTotalCalls - 1}
                    step={1}
                    value={[displayIndex]}
                    onValueChange={handleSliderChange}
                    className="w-full [&>span:first-child]:h-1.5 [&>span:first-child]:bg-zinc-200 dark:[&>span:first-child]:bg-zinc-800 [&>span:first-child>span]:bg-zinc-500 dark:[&>span:first-child>span]:bg-zinc-400 [&>span:first-child>span]:h-1.5"
                  />
                </div>

                <div className="flex items-center gap-1.5">
                  {renderStatusButton()}
                </div>
              </div>
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
