'use client';

import React, {
  useCallback,
  useDeferredValue,
  useEffect,
  useRef,
  useState,
} from 'react';
import { useSearchParams } from 'next/navigation';
import { ArrowLeft } from 'lucide-react';
import {
  BillingError,
  AgentRunLimitError,
  getMessages,
  type ImageMediaRef,
  type ShadowCloneMode,
} from '@/lib/api';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { ChatInput } from '@/components/thread/chat-input/chat-input';
import { useSidebar } from '@/components/ui/sidebar';
import {
  useAgentStream,
  type AgentStreamStatusContext,
} from '@/hooks/useAgentStream';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/use-mobile';
import { isBillingUiEnabled, isLocalMode } from '@/lib/config';
import { ThreadContent } from '@/components/thread/content/ThreadContent';
import { ThreadSkeleton } from '@/components/thread/content/ThreadSkeleton';
import {
  getRightPanelMode,
  isRightPanelInspectionMode,
  RIGHT_PANEL_MODE,
  shouldAutoOpenRightPanelMode,
  shouldSuppressPrimaryThreadActivityForRightPanel,
} from '@/lib/shadow-clone-right-panel-mode';
import { useAddUserMessageMutation } from '@/hooks/react-query/threads/use-messages';
import { useStartAgentMutation } from '@/hooks/react-query/threads/use-agent-run';
import { useSubscription } from '@/hooks/react-query/subscriptions/use-subscriptions';
import { SubscriptionStatus } from '@/components/thread/chat-input/_use-model-selection';

import { UnifiedMessage, ApiMessageType, ToolCallInput } from '../_types';
import { useThreadData, useToolCalls, useBilling, useKeyboardShortcuts } from '../_hooks';
import { ThreadError, UpgradeDialog, ThreadLayout } from '../_components';
import { useThreadAgent, useAgents } from '@/hooks/react-query/agents/use-agents';
import { AgentRunLimitDialog } from '@/components/thread/agent-run-limit-dialog';
import { useAgentSelection } from '@/lib/stores/agent-selection-store';
import { useQueryClient } from '@tanstack/react-query';
import { threadKeys } from '@/hooks/react-query/threads/keys';
import { useLanguage } from '@/contexts/LanguageContext';
import { safeJsonParse } from '@/components/thread/utils';
import { isBenignAgentNotRunningError } from '@/lib/stream-errors';
import { useShadowCloneStore } from '@/lib/stores/shadow-clone-store';
import {
  mergeNormalizedWriteFileArgs,
  normalizeWriteFileArgs,
  stringifyNormalizedWriteFileArgs,
} from '@/lib/write-file-stream';
import {
  hasAssistantMessageForRun,
  shouldApplyTerminalStatus,
  shouldRetryTerminalMessageRefetch,
  shouldShowCompletionHint,
} from './agent-stream-run-guard';
import {
  mergeThreadMessages,
  normalizeThreadMessages,
} from '../_hooks/thread-message-merge';

const messageTimestamp = (message: UnifiedMessage): number => {
  const parsed = Date.parse(message.created_at || message.updated_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
};

const assistantMessageText = (message: UnifiedMessage): string => {
  const parsed = safeJsonParse<{ content?: unknown }>(message.content, {});
  if (typeof parsed.content === 'string') {
    return parsed.content.trim();
  }
  return typeof message.content === 'string' ? message.content.trim() : '';
};

const shadowCloneSyntheticMainFinalKey = (message: UnifiedMessage): string | null => {
  if (message.message_id !== 'shadow-clone:main:assistant-complete') {
    return null;
  }
  const text = assistantMessageText(message);
  return text ? `assistant:${text}` : null;
};

const mergeShadowCloneMainTranscriptMessages = (
  threadMessages: UnifiedMessage[],
  shadowCloneMainMessages: UnifiedMessage[],
): UnifiedMessage[] => {
  if (!shadowCloneMainMessages.length) {
    return threadMessages;
  }

  const canonicalAssistantContentKeys = new Map<string, number>();
  threadMessages
    .filter((message) => message.type === 'assistant')
    .forEach((message) => {
      const text = assistantMessageText(message);
      if (!text) return;
      const key = `assistant:${text}`;
      const timestamp = messageTimestamp(message);
      const existingTimestamp = canonicalAssistantContentKeys.get(key);
      if (existingTimestamp === undefined || timestamp > existingTimestamp) {
        canonicalAssistantContentKeys.set(key, timestamp);
      }
    });
  const filteredShadowCloneMainMessages = shadowCloneMainMessages.filter((message) => {
    const syntheticFinalKey = shadowCloneSyntheticMainFinalKey(message);
    if (!syntheticFinalKey) {
      return true;
    }
    const canonicalTimestamp = canonicalAssistantContentKeys.get(syntheticFinalKey);
    if (canonicalTimestamp === undefined) {
      return true;
    }
    const syntheticFinalTimestamp = messageTimestamp(message);
    return !(canonicalTimestamp >= syntheticFinalTimestamp);
  });
  if (!filteredShadowCloneMainMessages.length) {
    return threadMessages;
  }

  const byId = new Map<string, { message: UnifiedMessage; order: number }>();
  [...threadMessages, ...filteredShadowCloneMainMessages].forEach((message, order) => {
    const key = message.message_id || `${message.thread_id}:${message.sequence}:${order}`;
    byId.set(key, { message, order });
  });

  return Array.from(byId.values())
    .sort((left, right) => {
      const timeDelta = messageTimestamp(left.message) - messageTimestamp(right.message);
      if (timeDelta !== 0) return timeDelta;
      const leftSequence = Number(left.message.sequence || 0);
      const rightSequence = Number(right.message.sequence || 0);
      if (leftSequence !== rightSequence) return leftSequence - rightSequence;
      return left.order - right.order;
    })
    .map((entry) => entry.message);
};

const DUPLICATE_USER_WINDOW_MS = 5000;
const THREAD_PAGE_DEBUG_ENABLED =
  process.env.NEXT_PUBLIC_AGENT_STREAM_DEBUG === 'true';

const logThreadDebug = (...args: unknown[]) => {
  if (!THREAD_PAGE_DEBUG_ENABLED) return;
  console.log(...args);
};

const normalizeUserMessageContent = (content: string): string => {
  const parsed = safeJsonParse<{ content?: string } | string>(content, content);
  if (typeof parsed === 'string') {
    return parsed;
  }
  if (parsed && typeof parsed === 'object' && typeof parsed.content === 'string') {
    return parsed.content;
  }
  return content;
};

const isDuplicateUserMessage = (incoming: UnifiedMessage, existing: UnifiedMessage): boolean => {
  if (incoming.type !== 'user' || existing.type !== 'user') {
    return false;
  }

  const incomingText = normalizeUserMessageContent(incoming.content || '').trim();
  const existingText = normalizeUserMessageContent(existing.content || '').trim();
  if (!incomingText || !existingText || incomingText !== existingText) {
    return false;
  }

  const incomingTime = new Date(incoming.created_at || Date.now()).getTime();
  const existingTime = new Date(existing.created_at || 0).getTime();
  if (!Number.isFinite(incomingTime) || !Number.isFinite(existingTime)) {
    return false;
  }

  return Math.abs(incomingTime - existingTime) <= DUPLICATE_USER_WINDOW_MS;
};

// 🔧 修复React Hooks规则 - 提取内部组件
function ThreadPageContent({
  projectId,
  threadId,
}: {
  projectId: string;
  threadId: string;
}) {
  const isMobile = useIsMobile();
  const { t } = useLanguage();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const billingUiEnabled = isBillingUiEnabled();

  // State
  const [newMessage, setNewMessage] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [fileViewerOpen, setFileViewerOpen] = useState(false);
  const [fileToView, setFileToView] = useState<string | null>(null);
  const [filePathList, setFilePathList] = useState<string[] | undefined>(undefined);
  const [showUpgradeDialog, setShowUpgradeDialog] = useState(false);
  const [debugMode, setDebugMode] = useState(false);
  const [initialPanelOpenAttempted, setInitialPanelOpenAttempted] = useState(false);
  // Use Zustand store for agent selection persistence
  const { 
    selectedAgentId, 
    setSelectedAgent, 
    initializeFromAgents,
    getCurrentAgent,
    isSunaAgent 
  } = useAgentSelection();
  
  const { data: agentsResponse } = useAgents();
  const agents = React.useMemo(() => agentsResponse?.agents || [], [agentsResponse?.agents]);
  const [isSidePanelAnimating, setIsSidePanelAnimating] = useState(false);
  const [userInitiatedRun, setUserInitiatedRun] = useState(false);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [showAgentLimitDialog, setShowAgentLimitDialog] = useState(false);
  const [agentLimitData, setAgentLimitData] = useState<{
    runningCount: number;
    runningThreadIds: string[];
  } | null>(null);
  const [showCompletionHint, setShowCompletionHint] = useState(false);
  const [pendingAssistantLoaderRunId, setPendingAssistantLoaderRunId] = useState<string | null>(null);
  const streamDebugEnabled = THREAD_PAGE_DEBUG_ENABLED;
  const resetShadowCloneRuntime = useShadowCloneStore((state) => state.resetRuntime);
  const clearShadowCloneActiveSubtask = useShadowCloneStore((state) => state.clearActiveSubtask);
  const returnToShadowCloneMain = useShadowCloneStore((state) => state.returnToMainView);
  const shadowClonePhase = useShadowCloneStore((state) => state.phase);
  const shadowCloneCurrentRunId = useShadowCloneStore((state) => state.currentRunId);
  const shadowCloneSubtaskCount = useShadowCloneStore((state) => state.subtasks.length);
  const shadowCloneLiveScope = useShadowCloneStore(
    (state) => state.liveActivity?.scope || null,
  );
  const shadowCloneViewScope = useShadowCloneStore((state) => state.viewScope);
  const activeShadowCloneSubtaskId = useShadowCloneStore((state) => state.activeSubtaskId);
  const activeShadowCloneSubtask = useShadowCloneStore((state) =>
    state.activeSubtaskId
      ? state.subtasks.find((item) => item.id === state.activeSubtaskId) || null
      : null,
  );
  const activeShadowCloneTranscript = useShadowCloneStore((state) =>
    state.activeSubtaskId
      ? state.subtaskTranscriptStates[state.activeSubtaskId] || null
      : null,
  );
  const shadowCloneMainTranscript = useShadowCloneStore((state) => state.mainTranscriptState);
  const syncShadowCloneRunData = useShadowCloneStore((state) => state.syncRunData);
  const deferredShadowCloneViewScope = useDeferredValue(shadowCloneViewScope);
  const deferredActiveShadowCloneSubtaskId = useDeferredValue(activeShadowCloneSubtaskId);
  const deferredActiveShadowCloneSubtask = useDeferredValue(activeShadowCloneSubtask);
  const deferredActiveShadowCloneTranscript = useDeferredValue(activeShadowCloneTranscript);
  const deferredShadowCloneMainTranscript = useDeferredValue(shadowCloneMainTranscript);

  // Refs - simplified for flex-column-reverse
  const latestMessageRef = useRef<HTMLDivElement>(null);
  const initialLayoutAppliedRef = useRef(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const hasSeenRunRef = useRef(false);
  const activeRunIdRef = useRef<string | null>(null);
  const lastTerminalRunIdRef = useRef<string | null>(null);
  const currentRunStartedAtRef = useRef<number>(0);
  const lastTerminalRefetchKeyRef = useRef<string | null>(null);
  const terminalMessageRefetchAttemptRef = useRef(0);

  useEffect(() => {
    resetShadowCloneRuntime();
    setPendingAssistantLoaderRunId(null);
  }, [threadId, resetShadowCloneRuntime]);

  const logCompletionHintTransition = useCallback((
    action: 'show' | 'hide',
    reason: string,
    details?: Record<string, unknown>,
  ) => {
    if (!streamDebugEnabled) return;
    console.log('💡 [completionHint]', {
      action,
      reason,
      isSending,
      userInitiatedRun,
      activeRunId: activeRunIdRef.current,
      terminalRunId: lastTerminalRunIdRef.current,
      ...(details ?? {}),
      timestamp: Date.now(),
    });
  }, [
    isSending,
    streamDebugEnabled,
    userInitiatedRun,
  ]);

  // Sidebar
  const { state: leftSidebarState, setOpen: setLeftSidebarOpen } = useSidebar();

  // Custom hooks
  const {
    messages,
    setMessages,
    project,
    sandboxId,
    projectName,
    agentRunId,
    setAgentRunId,
    agentStatus,
    setAgentStatus,
    isLoading,
    error,
    initialLoadCompleted,
    threadQuery,
    messagesQuery,
    projectQuery,
    agentRunsQuery,
  } = useThreadData(threadId, projectId);
  const activeProject = project?.id === projectId ? project : null;
  const activeSandboxId = activeProject
    ? activeProject.file_delivery_source?.browseSandboxId ||
      sandboxId
    : null;
  const activeProjectName = activeProject ? projectName : '';

  // 简化调试信息
  React.useEffect(() => {
    logThreadDebug('🔍 [ThreadPage] messages状态:', {
      length: messages?.length,
      types: messages?.reduce((acc, msg) => {
        acc[msg.type] = (acc[msg.type] || 0) + 1;
        return acc;
      }, {} as Record<string, number>)
    });
  }, [messages]);

  const rightPanelMode = React.useMemo(
    () =>
      getRightPanelMode({
        phase: shadowClonePhase,
        viewScope: shadowCloneViewScope,
        activeSubtaskId: activeShadowCloneSubtaskId,
        hasActiveSubtask: Boolean(activeShadowCloneSubtask),
        subtaskCount: shadowCloneSubtaskCount,
        liveActivityScope: shadowCloneLiveScope,
        agentStatus,
      }),
    [
      activeShadowCloneSubtask,
      activeShadowCloneSubtaskId,
      agentStatus,
      shadowCloneLiveScope,
      shadowClonePhase,
      shadowCloneSubtaskCount,
      shadowCloneViewScope,
    ],
  );
  const deferredRightPanelMode = React.useMemo(
    () =>
      getRightPanelMode({
        phase: shadowClonePhase,
        viewScope: deferredShadowCloneViewScope,
        activeSubtaskId: deferredActiveShadowCloneSubtaskId,
        hasActiveSubtask: Boolean(deferredActiveShadowCloneSubtask),
        subtaskCount: shadowCloneSubtaskCount,
        liveActivityScope: shadowCloneLiveScope,
        agentStatus,
      }),
    [
      agentStatus,
      deferredActiveShadowCloneSubtask,
      deferredActiveShadowCloneSubtaskId,
      deferredShadowCloneViewScope,
      shadowCloneLiveScope,
      shadowClonePhase,
      shadowCloneSubtaskCount,
    ],
  );
  const isSubagentInspectionMode = isRightPanelInspectionMode(rightPanelMode);
  const isDeferredSubagentInspectionMode =
    isRightPanelInspectionMode(deferredRightPanelMode);
  const shouldAutoOpenShadowClonePanel =
    shouldAutoOpenRightPanelMode(rightPanelMode);
  const activeShadowCloneName =
    deferredActiveShadowCloneSubtask?.role || deferredActiveShadowCloneSubtask?.id || 'Subagent';

  const {
    toolCalls,
    setToolCalls,
    currentToolIndex,
    setCurrentToolIndex,
    isSidePanelOpen,
    setIsSidePanelOpen,
    autoOpenedPanel,
    setAutoOpenedPanel,
    externalNavIndex,
    setExternalNavIndex,
    handleToolClick,
    handleStreamingToolCall,
    toggleSidePanel,
    handleSidePanelNavigate,
    userClosedPanelRef,
  } = useToolCalls(messages, setLeftSidebarOpen, agentStatus);

  const handleReturnToShadowCloneMain = useCallback(() => {
    clearShadowCloneActiveSubtask();
    returnToShadowCloneMain();
    setIsSidePanelOpen(true);
    userClosedPanelRef.current = false;
  }, [
    clearShadowCloneActiveSubtask,
    returnToShadowCloneMain,
    setIsSidePanelOpen,
    userClosedPanelRef,
  ]);

  const handleToolClickWithPanelFocus = useCallback((assistantMessageId: string | null, toolName: string) => {
    if (isSubagentInspectionMode) {
      setIsSidePanelOpen(true);
      userClosedPanelRef.current = false;
      return;
    }

    clearShadowCloneActiveSubtask();
    handleToolClick(assistantMessageId, toolName);
  }, [
    clearShadowCloneActiveSubtask,
    handleToolClick,
    isSubagentInspectionMode,
    setIsSidePanelOpen,
    userClosedPanelRef,
  ]);

  const handleSidePanelClose = useCallback(() => {
    setIsSidePanelOpen(false);
    userClosedPanelRef.current = true;
    setAutoOpenedPanel(true);
  }, [
    setAutoOpenedPanel,
    setIsSidePanelOpen,
    userClosedPanelRef,
  ]);

  const handleSidePanelToggle = useCallback(() => {
    if (isSidePanelOpen) {
      handleSidePanelClose();
      return;
    }

    toggleSidePanel();
  }, [handleSidePanelClose, isSidePanelOpen, toggleSidePanel]);
  
  // 调试toolCalls状态
  React.useEffect(() => {
    logThreadDebug('🔧 [ThreadPage] toolCalls状态:', {
      length: toolCalls.length,
      toolCalls: toolCalls,
      currentIndex: currentToolIndex,
      isSidePanelOpen: isSidePanelOpen
    });
  }, [toolCalls, currentToolIndex, isSidePanelOpen]);
  
  // 🎯 专注调试：agentStatus 变化源头
  const prevAgentStatusRef = React.useRef(agentStatus);
  React.useEffect(() => {
    if (prevAgentStatusRef.current !== agentStatus) {
      logThreadDebug('🚨 [agentStatus] CHANGED:', {
        from: prevAgentStatusRef.current,
        to: agentStatus,
        timestamp: Date.now()
      });
      prevAgentStatusRef.current = agentStatus;
    }
  }, [agentStatus]);

  const {
    showBillingAlert,
    setShowBillingAlert,
    billingData,
    setBillingData,
    checkBillingLimits,
    billingStatusQuery,
  } = useBilling(project?.account_id, agentStatus, initialLoadCompleted);

  // Keyboard shortcuts
  useKeyboardShortcuts({
    isSidePanelOpen,
    setIsSidePanelOpen,
    leftSidebarState,
    setLeftSidebarOpen,
    userClosedPanelRef,
  });

  const addUserMessageMutation = useAddUserMessageMutation();
  const startAgentMutation = useStartAgentMutation();
  const { data: threadAgentData } = useThreadAgent(threadId);
  const agent = threadAgentData?.agent;
  const resolvedAgentName = agent?.name === 'Suna' ? t('agent.default') : agent?.name;
  const workflowId = threadQuery.data?.metadata?.workflow_id;

  useEffect(() => {
    queryClient.invalidateQueries({ queryKey: threadKeys.agentRuns(threadId) });
    queryClient.invalidateQueries({ queryKey: threadKeys.messages(threadId) });
  }, [threadId, queryClient]);

  useEffect(() => {
    if (!agentRunId) {
      return;
    }

    const shouldBootstrapShadowCloneState =
      (shadowClonePhase === 'idle' || shadowCloneSubtaskCount === 0) &&
      (!shadowCloneCurrentRunId || shadowCloneCurrentRunId === agentRunId);

    if (!shouldBootstrapShadowCloneState) {
      return;
    }

    void syncShadowCloneRunData(agentRunId);
  }, [
    agentRunId,
    shadowCloneCurrentRunId,
    shadowClonePhase,
    shadowCloneSubtaskCount,
    syncShadowCloneRunData,
  ]);

  useEffect(() => {
    if (agents.length > 0) {
      const threadAgentId = threadAgentData?.agent?.agent_id;
      initializeFromAgents(agents, threadAgentId);
    }
  }, [threadAgentData, agents, initializeFromAgents]);

  const { data: subscriptionData } = useSubscription();
  const subscriptionStatus: SubscriptionStatus = subscriptionData?.status === 'active'
    ? 'active'
    : 'no_subscription';


  const handleProjectRenamed = useCallback((newName: string) => {
  }, []);

  // scrollToBottom for flex-column-reverse layout
  const scrollToBottom = useCallback(() => {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, []);

  const handleNewMessageFromStream = useCallback((message: UnifiedMessage) => {
    if (!message.message_id) {
      console.warn(
        `[STREAM HANDLER] Received message is missing ID: Type=${message.type}`,
      );
    }

    setMessages((prev) => {
      const messageExists = prev.some(
        (m) => m.message_id === message.message_id,
      );
      if (messageExists) {
        return prev.map((m) =>
          m.message_id === message.message_id ? message : m,
        );
      } else {
        if (message.type === 'user' && prev.some((m) => isDuplicateUserMessage(message, m))) {
          return prev;
        }
        // If this is a user message, replace any optimistic user message with temp ID
        if (message.type === 'user') {
          const normalizedIncoming = normalizeUserMessageContent(message.content || '');
          const optimisticIndex = prev.findIndex(m =>
            m.type === 'user' &&
            m.message_id?.startsWith('temp-') &&
            normalizeUserMessageContent(m.content || '') === normalizedIncoming
          );
          if (optimisticIndex !== -1) {
            // Replace the optimistic message with the real one
            return prev.map((m, index) =>
              index === optimisticIndex ? message : m
            );
          }
        }
        return [...prev, message];
      }
    });

    if (message.type === 'tool') {
      setAutoOpenedPanel(false);
    }
  }, [setMessages, setAutoOpenedPanel]);

  const handleStreamStatusChange = useCallback((
    hookStatus: string,
    statusContext?: AgentStreamStatusContext,
  ) => {
    const statusRunId = statusContext?.runId ?? null;
    const currentActiveRunId = activeRunIdRef.current;
    const isTerminalStatus = [
      'idle',
      'completed',
      'stopped',
      'agent_not_running',
      'error',
      'failed',
    ].includes(hookStatus);

    logThreadDebug('🌊 [handleStreamStatusChange] Received status update:', {
      hookStatus,
      statusRunId,
      statusEpoch: statusContext?.epoch,
      source: statusContext?.source,
      currentActiveRunId,
    });

    if (isTerminalStatus) {
      const shouldApplyStatus = shouldApplyTerminalStatus({
        statusRunId,
        activeRunId: currentActiveRunId,
        isSending,
        userInitiatedRun,
        agentStatus,
      });

      if (!shouldApplyStatus) {
        logThreadDebug(
          '⏭️ [handleStreamStatusChange] Ignoring terminal status for non-active run',
          {
            hookStatus,
            statusRunId,
            currentActiveRunId,
          },
        );
        return;
      }
    }

    if (statusRunId && (hookStatus === 'connecting' || hookStatus === 'streaming')) {
      activeRunIdRef.current = statusRunId;
    }

    switch (hookStatus) {
      case 'idle':
      case 'completed':
      case 'stopped':
      case 'agent_not_running':
      case 'error':
      case 'failed':
        lastTerminalRunIdRef.current = statusRunId ?? currentActiveRunId ?? null;
        if (
          hookStatus === 'stopped' ||
          hookStatus === 'agent_not_running' ||
          hookStatus === 'error' ||
          hookStatus === 'failed'
        ) {
          setPendingAssistantLoaderRunId(null);
        }
        logThreadDebug('🌊 [handleStreamStatusChange] Setting agentStatus to IDLE');
        setAgentStatus((current) => {
          if (current !== 'idle') {
            logThreadDebug('✅ [handleStreamStatusChange] Changed agentStatus to IDLE');
            return 'idle';
          }
          logThreadDebug('⏭️ [handleStreamStatusChange] Skipped - already IDLE');
          return current;
        });
        setAgentRunId(null);
        activeRunIdRef.current = null;
        setAutoOpenedPanel(false);

        // No scroll needed with flex-column-reverse
        break;
      case 'connecting':
        logThreadDebug('🌊 [handleStreamStatusChange] Setting agentStatus to CONNECTING');
        setAgentStatus((current) => {
          if (current !== 'connecting') {
            logThreadDebug('✅ [handleStreamStatusChange] Changed agentStatus to CONNECTING');
            return 'connecting';
          }
          logThreadDebug('⏭️ [handleStreamStatusChange] Skipped - already CONNECTING');
          return current;
        });
        break;
      case 'streaming':
        logThreadDebug('🌊 [handleStreamStatusChange] Setting agentStatus to RUNNING (streaming)');
        setAgentStatus((current) => {
          if (current !== 'running') {
            logThreadDebug('✅ [handleStreamStatusChange] Changed agentStatus to RUNNING');
            return 'running';
          }
          logThreadDebug('⏭️ [handleStreamStatusChange] Skipped - already RUNNING');
          return current;
        });
        break;
    }
  }, [
    agentStatus,
    isSending,
    userInitiatedRun,
    setAgentStatus,
    setAgentRunId,
    setAutoOpenedPanel,
    setPendingAssistantLoaderRunId,
  ]);

  const handleStreamError = useCallback((errorMessage: string) => {
    console.error(`[PAGE] Stream hook error: ${errorMessage}`);
    const normalized = errorMessage.toLowerCase();
    const isQwenTimeout =
      normalized.includes('no new chunks') ||
      normalized.includes('stalled without effective progress');
    if (
      !normalized.includes('not found') &&
      !isBenignAgentNotRunningError(errorMessage)
    ) {
      const displayMessage = isQwenTimeout
        ? '模型响应暂时超时，已结束本次运行。你可以继续对话或重试。'
        : `${t('thread.streamError')}: ${errorMessage}`;
      toast.error(displayMessage);
    }
  }, [t]);

  const reconcileCanonicalMessagesAfterTerminal = useCallback(
    async (reason: string) => {
      try {
        const canonicalMessages = await getMessages(threadId);
        const unifiedMessages = normalizeThreadMessages(
          canonicalMessages,
          threadId,
        ) as UnifiedMessage[];

        queryClient.setQueryData(threadKeys.messages(threadId), canonicalMessages);
        setMessages((prev) => mergeThreadMessages(
          prev,
          unifiedMessages,
        ) as UnifiedMessage[]);
        logThreadDebug('✅ [terminalReconcile] canonical messages merged into chat panel', {
          reason,
          threadId,
          fetchedCount: canonicalMessages.length,
        });
      } catch (error) {
        console.error('[terminalReconcile] Failed to merge canonical messages:', error);
      }
    },
    [queryClient, setMessages, threadId],
  );

  const scheduleCanonicalMessageReconciliation = useCallback(
    (reason: string, delaysMs: number[]) => {
      const timers = delaysMs.map((delayMs) =>
        window.setTimeout(() => {
          void reconcileCanonicalMessagesAfterTerminal(
            `${reason}:${delayMs}`,
          );
          void agentRunsQuery.refetch();
          void messagesQuery.refetch();
        }, delayMs),
      );

      window.setTimeout(() => {
        timers.forEach((timer) => window.clearTimeout(timer));
      }, Math.max(...delaysMs) + 1000);
    },
    [
      agentRunsQuery,
      messagesQuery,
      reconcileCanonicalMessagesAfterTerminal,
    ],
  );

  const handleStreamClose = useCallback((finalStatus: string) => {
    logThreadDebug('Stream closed - triggering direct canonical message reconciliation');
    scheduleCanonicalMessageReconciliation(
      finalStatus || 'closed',
      [0, 500, 1500, 3000],
    );
  }, [
    scheduleCanonicalMessageReconciliation,
  ]);

  const {
    status: streamHookStatus,
    textContent: streamingTextContent,
    reasoningContent: streamingReasoningContent,
    toolCall: streamingToolCall,
    error: streamError,
    agentRunId: currentHookRunId,
    isWritingFile,
    startStreaming,
    stopStreaming,
  } = useAgentStream(
    {
      onMessage: handleNewMessageFromStream,
      onStatusChange: handleStreamStatusChange,
      onError: handleStreamError,
      onClose: handleStreamClose,
    },
    threadId,
    setMessages,
  );

  // Subagent inspection streaming state (SCV2 parity spec).
  // Uses Zustand selector to read per-subagent inspection stream state.
  const activeInspectionSubtaskId = isDeferredSubagentInspectionMode && deferredActiveShadowCloneSubtask
    ? deferredActiveShadowCloneSubtask.id
    : null;
  const subagentInspectionState = useShadowCloneStore(
    React.useCallback(
      (state) => (activeInspectionSubtaskId ? state.inspectionStreamStates[activeInspectionSubtaskId] ?? null : null),
      [activeInspectionSubtaskId],
    ),
  );
  const subagentStreamText = subagentInspectionState?.textContent ?? '';
  const subagentStreamReasoning = subagentInspectionState?.reasoningContent ?? '';
  const subagentStreamStatus = subagentInspectionState?.status ?? 'idle';
  const subagentIsWritingFile = subagentInspectionState?.isWritingFile ?? false;

  const effectiveThreadMessages = isDeferredSubagentInspectionMode
    ? deferredActiveShadowCloneTranscript?.messages || []
    : mergeShadowCloneMainTranscriptMessages(
        messages,
        deferredShadowCloneMainTranscript?.messages || [],
      );
  const effectiveStreamingTextContent = isDeferredSubagentInspectionMode
    ? (subagentStreamText || deferredActiveShadowCloneTranscript?.streamingTextContent || '')
    : streamingTextContent;
  const effectiveStreamingReasoningContent = isDeferredSubagentInspectionMode
    ? (subagentStreamReasoning || deferredActiveShadowCloneTranscript?.streamingReasoningContent || '')
    : streamingReasoningContent;
  const effectiveStreamingToolCall = isDeferredSubagentInspectionMode
    ? deferredActiveShadowCloneTranscript?.streamingToolCall || null
    : streamingToolCall;
  const effectiveThreadAgentStatus: 'idle' | 'running' | 'connecting' | 'disconnecting' | 'error' = isDeferredSubagentInspectionMode
    ? deferredActiveShadowCloneSubtask?.status === 'running'
      ? 'running'
      : deferredActiveShadowCloneSubtask?.status === 'pending'
        ? 'connecting'
        : deferredActiveShadowCloneSubtask?.status === 'failed'
          ? 'error'
          : 'idle'
    : agentStatus;
  const hasSelectedSubagentLiveTranscript = Boolean(
    isDeferredSubagentInspectionMode &&
      (
        effectiveStreamingTextContent.trim() ||
        effectiveStreamingReasoningContent.trim() ||
        effectiveStreamingToolCall
      )
  );
  const shouldRenderSelectedSubagentStreamingState =
    hasSelectedSubagentLiveTranscript ||
    effectiveThreadAgentStatus === 'running' ||
    effectiveThreadAgentStatus === 'connecting';
  const effectiveThreadStreamHookStatus = isDeferredSubagentInspectionMode
    ? (subagentStreamStatus !== 'idle' ? subagentStreamStatus : shouldRenderSelectedSubagentStreamingState ? 'streaming' : 'idle')
    : streamHookStatus;
  const hasPendingAssistantOutput = pendingAssistantLoaderRunId
    ? hasAssistantMessageForRun({
        messages,
        terminalRunId: pendingAssistantLoaderRunId,
        activeRunId: pendingAssistantLoaderRunId,
        runStartedAt: currentRunStartedAtRef.current,
      })
    : false;
  const hasPendingStreamingFeedback = Boolean(
    streamingTextContent.trim() ||
      streamingReasoningContent.trim() ||
      streamingToolCall
  );
  const showPendingAssistantLoader =
    !isDeferredSubagentInspectionMode &&
    !hasPendingStreamingFeedback &&
    (
      isSending ||
      (
        Boolean(pendingAssistantLoaderRunId) &&
        !hasPendingAssistantOutput
      )
    );

  useEffect(() => {
    if (!pendingAssistantLoaderRunId) {
      return;
    }

    const timeout = window.setTimeout(() => {
      setPendingAssistantLoaderRunId((currentRunId) =>
        currentRunId === pendingAssistantLoaderRunId ? null : currentRunId,
      );
    }, 125000);

    return () => window.clearTimeout(timeout);
  }, [pendingAssistantLoaderRunId]);

  useEffect(() => {
    if (pendingAssistantLoaderRunId && hasPendingAssistantOutput) {
      setPendingAssistantLoaderRunId(null);
    }
  }, [hasPendingAssistantOutput, pendingAssistantLoaderRunId]);

  const effectiveThreadAgentName = isDeferredSubagentInspectionMode
    ? deferredActiveShadowCloneSubtask?.role || deferredActiveShadowCloneSubtask?.id || 'Subagent'
    : resolvedAgentName;
  // Decoupled suppression per SCV2 frontend parity spec:
  // - suppressPrimaryThreadMessages: controls which agent messages route to
  //   left panel (keeps existing shadow-clone message routing to right panel).
  // - suppressPrimaryThreadIndicator: controls FileWritingIndicator (blue bar).
  //   Must NOT be gated on right panel state — each panel independently
  //   manages its own indicator per FR-011.
  const shouldSuppressPrimaryThreadMessages =
    !isDeferredSubagentInspectionMode &&
    (
      showCompletionHint ||
      shouldSuppressPrimaryThreadActivityForRightPanel({
        rightPanelMode,
        isSidePanelOpen,
      })
    );
  const shouldSuppressPrimaryThreadIndicator =
    isDeferredSubagentInspectionMode; // Only suppress when left panel shows subagent content
  const shouldUsePrimaryToolPanelLivePath = true;
  const shouldTrackPrimaryStreamingText = isSidePanelOpen;

  // activeRunIdRef is kept in sync via direct assignments at each
  // setAgentRunId() call site — no useEffect needed.

  useEffect(() => {
    const isRunActive =
      isSending ||
      userInitiatedRun ||
      agentStatus === 'running' ||
      agentStatus === 'connecting' ||
      agentStatus === 'disconnecting' ||
      streamHookStatus === 'connecting' ||
      streamHookStatus === 'streaming';

    if (isRunActive) {
      hasSeenRunRef.current = true;
      if (showCompletionHint) {
        logCompletionHintTransition('hide', 'active_run_signals', {
          streamHookStatus,
        });
        setShowCompletionHint(false);
      }
      return;
    }

    const isTerminalStatus = [
      'completed',
      'stopped',
      'failed',
      'error',
      'agent_not_running',
    ].includes(streamHookStatus);

    const terminalRunId = lastTerminalRunIdRef.current;
    const activeRunId = activeRunIdRef.current;
    const runStartedAt = currentRunStartedAtRef.current;
    const hasAssistantMessageAfterRunStart = hasAssistantMessageForRun({
      messages,
      terminalRunId,
      activeRunId,
      runStartedAt,
    });
    const hasStreamingContent = Boolean(
      streamingTextContent.trim() ||
        streamingReasoningContent.trim() ||
        streamingToolCall
    );
    const shouldShowHint = shouldShowCompletionHint({
      terminalRunId,
      activeRunId,
      hasSeenRun: hasSeenRunRef.current,
      hasAssistantMessageAfterRunStart,
      hasStreamingContent,
      finalStatus: streamHookStatus,
    });

    if (isTerminalStatus && terminalRunId) {
      const refetchKey = `${terminalRunId}:${streamHookStatus}`;
      if (lastTerminalRefetchKeyRef.current !== refetchKey) {
        lastTerminalRefetchKeyRef.current = refetchKey;
        terminalMessageRefetchAttemptRef.current = 0;
      }

      if (hasAssistantMessageAfterRunStart) {
        terminalMessageRefetchAttemptRef.current = 0;
      } else {
        const attempt = terminalMessageRefetchAttemptRef.current;
        const shouldRetryRefetch = shouldRetryTerminalMessageRefetch({
          terminalRunId,
          activeRunId,
          isTerminalStatus,
          hasAssistantMessageAfterRunStart,
          attempt,
          maxAttempts: 8,
        });

        if (shouldRetryRefetch) {
          terminalMessageRefetchAttemptRef.current = attempt + 1;
          const retryDelayMs = attempt === 0 ? 0 : Math.min(500 * attempt, 2000);
          const retryTimer = window.setTimeout(() => {
            void messagesQuery.refetch();
            void agentRunsQuery.refetch();
          }, retryDelayMs);
          return () => window.clearTimeout(retryTimer);
        }
      }
    }

    if (!shouldShowHint) {
      if (showCompletionHint) {
        logCompletionHintTransition('hide', isTerminalStatus ? 'terminal_has_output_or_mismatch' : 'not_terminal_or_run_not_seen', {
          streamHookStatus,
          hasSeenRun: hasSeenRunRef.current,
          hasAssistantMessageAfterRunStart,
          hasStreamingContent,
        });
        setShowCompletionHint(false);
      }
      return;
    }

    if (!showCompletionHint) {
      logCompletionHintTransition('show', 'terminal_output_visible_post_completion', {
        streamHookStatus,
        hasAssistantMessageAfterRunStart,
        hasStreamingContent,
      });
      setShowCompletionHint(true);
    }
  }, [
    agentStatus,
    agentRunsQuery,
    currentHookRunId,
    isSending,
    messages,
    messagesQuery,
    streamHookStatus,
    showCompletionHint,
    streamingReasoningContent,
    streamingTextContent,
    streamingToolCall,
    userInitiatedRun,
    agentRunId,
    logCompletionHintTransition,
  ]);

  const handleSubmitMessage = useCallback(
    async (
      message: string,
      options?: {
        model_name?: string;
        enable_thinking?: boolean;
        reasoning_effort?: string;
        shadow_clone_mode?: ShadowCloneMode;
        shadow_clone_main_model?: string;
        shadow_clone_subagent_model?: string;
        media_refs?: ImageMediaRef[];
      },
    ) => {
      if (!message.trim()) return;
      hasSeenRunRef.current = true;
      currentRunStartedAtRef.current = Date.now();
      setShowCompletionHint(false);
      lastTerminalRunIdRef.current = null;
      lastTerminalRefetchKeyRef.current = null;
      terminalMessageRefetchAttemptRef.current = 0;
      activeRunIdRef.current = null;
      userClosedPanelRef.current = false;
      resetShadowCloneRuntime();
      setIsSending(true);

      let savedMessageId: string | null = null;
      const userPayload = JSON.stringify({ role: 'user', content: message });
      const optimisticUserMessage: UnifiedMessage = {
        message_id: `temp-${Date.now()}`,
        thread_id: threadId,
        type: 'user',
        is_llm_message: false,
        content: userPayload,
        metadata: '{}',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };

      setMessages((prev) => [...prev, optimisticUserMessage]);
      setNewMessage('');

      try {
        // Step 1: Save user message first (must complete before agent starts)
        // This eliminates the race condition where agent starts before message is saved
        logThreadDebug('📝 [handleSubmitMessage] Saving user message...');
        const savedMessage = await addUserMessageMutation.mutateAsync({
          threadId,
          message,
          media_refs: options?.media_refs,
        });
        const normalizedMessage: UnifiedMessage = {
          message_id: savedMessage.message_id || optimisticUserMessage.message_id,
          thread_id: savedMessage.thread_id || threadId,
          type: (savedMessage.type || 'user') as UnifiedMessage['type'],
          is_llm_message: Boolean(savedMessage.is_llm_message),
          content: savedMessage.content || userPayload,
          metadata: savedMessage.metadata || '{}',
          created_at: savedMessage.created_at || optimisticUserMessage.created_at,
          updated_at: savedMessage.updated_at || savedMessage.created_at || optimisticUserMessage.updated_at,
          agent_id: savedMessage.agent_id,
          agents: savedMessage.agents,
        };
        savedMessageId = normalizedMessage.message_id;
        setMessages((prev) => {
          let replaced = false;
          const next = prev.map((m) => {
            if (m.message_id === optimisticUserMessage.message_id) {
              replaced = true;
              return normalizedMessage;
            }
            return m;
          });
          if (!replaced) {
            return [...next, normalizedMessage];
          }
          return next;
        });
        logThreadDebug('✅ [handleSubmitMessage] User message saved successfully');

        // Step 2: Start agent after message is confirmed saved
        logThreadDebug('🚀 [handleSubmitMessage] Starting agent...');
        let agentResult;
        try {
          agentResult = await startAgentMutation.mutateAsync({
            threadId,
            options: {
              ...options,
              agent_id: selectedAgentId
            }
          });
        } catch (error) {
          // Handle specific agent start errors
          if (error instanceof BillingError) {
            setBillingData({
              currentUsage: error.detail.currentUsage as number | undefined,
              limit: error.detail.limit as number | undefined,
              message: error.detail.message || 'Monthly usage limit reached. Please upgrade.',
              accountId: project?.account_id || null
            });
            setShowBillingAlert(true);
            const idsToRemove = [optimisticUserMessage.message_id, savedMessageId].filter(Boolean);
            setMessages(prev => prev.filter(m => !idsToRemove.includes(m.message_id)));
            return;
          }

          if (error instanceof AgentRunLimitError) {
            const { running_thread_ids, running_count } = error.detail;
            setAgentLimitData({
              runningCount: running_count,
              runningThreadIds: running_thread_ids,
            });
            setShowAgentLimitDialog(true);
            const idsToRemove = [optimisticUserMessage.message_id, savedMessageId].filter(Boolean);
            setMessages(prev => prev.filter(m => !idsToRemove.includes(m.message_id)));
            return;
          }

          throw new Error(`${t('thread.startFailed')}: ${error instanceof Error ? error.message : error}`);
        }

        const newAgentRunId = agentResult.agent_run_id;
        if (!newAgentRunId) {
          throw new Error(`${t('thread.startFailed')}: missing agent run id`);
        }

        logThreadDebug('✅ [handleSubmitMessage] Agent started successfully:', newAgentRunId);
        setUserInitiatedRun(true);
        setAgentRunId(newAgentRunId);
        activeRunIdRef.current = newAgentRunId;
        currentRunStartedAtRef.current = Date.now();
        lastTerminalRunIdRef.current = null;
        lastTerminalRefetchKeyRef.current = null;
        terminalMessageRefetchAttemptRef.current = 0;
        // 立即设置为 running 状态，因为用户刚刚发起了新的对话
        setAgentStatus('running');
        setPendingAssistantLoaderRunId(newAgentRunId);
        startStreaming(newAgentRunId);
        setUserInitiatedRun(false);
        scheduleCanonicalMessageReconciliation(
          `run-start:${newAgentRunId}`,
          [1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000],
        );

      } catch (err) {
        console.error('Error sending message or starting agent:', err);
        if (!(err instanceof BillingError) && !(err instanceof AgentRunLimitError)) {
          toast.error(err instanceof Error ? err.message : t('common.operationFailed'));
        }
        const idsToRemove = [optimisticUserMessage.message_id, savedMessageId].filter(Boolean);
        setMessages((prev) => prev.filter((m) => !idsToRemove.includes(m.message_id)));
      } finally {
        setIsSending(false);
      }
    },
    [
      threadId,
      project?.account_id,
      selectedAgentId,
      addUserMessageMutation,
      startAgentMutation,
      setMessages,
      setBillingData,
      setShowBillingAlert,
      setAgentRunId,
      setAgentStatus,
      setShowCompletionHint,
      startStreaming,
      t,
      resetShadowCloneRuntime,
      userClosedPanelRef,
    ],
  );

  const handleStopAgent = useCallback(async () => {
    setAgentStatus('disconnecting');
    await stopStreaming();
  }, [stopStreaming, setAgentStatus]);

  const handleOpenFileViewer = useCallback((filePath?: string, filePathList?: string[]) => {
    if (!activeSandboxId) {
      toast.error(t('thread.sandboxNotReady'));
      return;
    }

    if (filePath) {
      setFileToView(filePath);
    } else {
      setFileToView(null);
    }
    setFilePathList(filePathList);
    setFileViewerOpen(true);
  }, [activeSandboxId, t]);

  useEffect(() => {
    setFileViewerOpen(false);
    setFileToView(null);
    setFilePathList(undefined);
  }, [threadId, projectId]);

  const toolViewAssistant = useCallback(
    (assistantContent?: string, toolContent?: string) => {
      if (!assistantContent) return null;

      return (
        <div className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">
            {t('thread.assistantMessage')}
          </div>
          <div className="rounded-md border bg-muted/50 p-3">
            <div className="text-xs prose prose-xs dark:prose-invert chat-markdown max-w-none">{assistantContent}</div>
          </div>
        </div>
      );
    },
    [t],
  );

  const toolViewResult = useCallback(
    (toolContent?: string, isSuccess?: boolean) => {
      if (!toolContent) return null;

      return (
        <div className="space-y-1">
          <div className="flex justify-between items-center">
            <div className="text-xs font-medium text-muted-foreground">
              {t('thread.toolResult')}
            </div>
            <div
              className={`px-2 py-0.5 rounded-full text-xs ${isSuccess
                ? 'bg-green-50 text-green-700 dark:bg-green-900 dark:text-green-300'
                : 'bg-red-50 text-red-700 dark:bg-red-900 dark:text-red-300'
                }`}
            >
              {isSuccess ? t('thread.toolSuccess') : t('thread.toolFailed')}
            </div>
          </div>
          <div className="rounded-md border bg-muted/50 p-3">
            <div className="text-xs prose prose-xs dark:prose-invert chat-markdown max-w-none">{toolContent}</div>
          </div>
        </div>
      );
    },
    [t],
  );

  // Effects
  useEffect(() => {
    if (!initialLayoutAppliedRef.current) {
      setLeftSidebarOpen(false);
      initialLayoutAppliedRef.current = true;
    }
  }, [setLeftSidebarOpen]);

  useEffect(() => {
    if (initialLoadCompleted && !initialPanelOpenAttempted) {
      setInitialPanelOpenAttempted(true);

      // Only auto-open on desktop, not mobile
      if (!isMobile) {
        if (toolCalls.length > 0) {
          setIsSidePanelOpen(true);
          setCurrentToolIndex(toolCalls.length - 1);
        } else {
          if (messages.length > 0) {
            setIsSidePanelOpen(true);
          }
        }
      }
    }
  }, [initialPanelOpenAttempted, messages, toolCalls, initialLoadCompleted, setIsSidePanelOpen, setCurrentToolIndex, isMobile]);

  useEffect(() => {
    if (!initialLoadCompleted || isMobile || !shouldAutoOpenShadowClonePanel) {
      return;
    }
    if (userClosedPanelRef.current || isSidePanelOpen) {
      return;
    }

    setLeftSidebarOpen(false);
    setIsSidePanelOpen(true);
  }, [
    initialLoadCompleted,
    isMobile,
    isSidePanelOpen,
    setIsSidePanelOpen,
    setLeftSidebarOpen,
    shouldAutoOpenShadowClonePanel,
    userClosedPanelRef,
  ]);

  useEffect(() => {
    // Start streaming if user initiated a run (don't wait for initialLoadCompleted for first-time users)
    if (
      agentRunId &&
      agentRunId !== currentHookRunId &&
      userInitiatedRun &&
      agentStatus === 'running'
    ) {
      logThreadDebug('🚀 [ThreadPage] Starting stream for user-initiated run:', agentRunId);
      startStreaming(agentRunId);
      setUserInitiatedRun(false); // Reset flag after starting
    }
    // Also start streaming if this is from page load with recent active runs
    else if (
      agentRunId &&
      agentRunId !== currentHookRunId &&
      initialLoadCompleted &&
      !userInitiatedRun &&
      agentStatus === 'running'
    ) {
      logThreadDebug('🚀 [ThreadPage] Starting stream for active run:', agentRunId);
      startStreaming(agentRunId);
    }
  }, [
    agentRunId,
    startStreaming,
    currentHookRunId,
    initialLoadCompleted,
    userInitiatedRun,
    agentStatus,
  ]);

  // No auto-scroll needed with flex-column-reverse

  // No intersection observer needed with flex-column-reverse

  // SEO title update
  useEffect(() => {
    if (projectName) {
      document.title = `Roys Alpha`;

      const metaDescription = document.querySelector(
        'meta[name="description"]',
      );
      if (metaDescription) {
        metaDescription.setAttribute(
          'content',
          `${projectName} - Interactive agent conversation powered by Roys Alpha`,
        );
      }

      const ogTitle = document.querySelector('meta[property="og:title"]');
      if (ogTitle) {
        ogTitle.setAttribute('content', `${projectName} | Roys Alpha`);
      }

      const ogDescription = document.querySelector(
        'meta[property="og:description"]',
      );
      if (ogDescription) {
        ogDescription.setAttribute(
          'content',
          `Interactive AI conversation for ${projectName}`,
        );
      }
    }
  }, [projectName]);

  useEffect(() => {
    // 🚫 禁用debug模式参数
    // const debugParam = searchParams.get('debug');
    // setDebugMode(debugParam === 'true');
    setDebugMode(false); // 强制禁用debug模式
  }, [searchParams]);

  const hasCheckedUpgradeDialog = useRef(false);

  useEffect(() => {
    if (!billingUiEnabled) {
      setShowUpgradeDialog(false);
      return;
    }
    if (initialLoadCompleted && subscriptionData && !hasCheckedUpgradeDialog.current) {
      hasCheckedUpgradeDialog.current = true;
      const hasSeenUpgradeDialog = localStorage.getItem('upgrade_dialog_displayed');
      const isFreeTier = subscriptionStatus === 'no_subscription';
      if (!hasSeenUpgradeDialog && isFreeTier && !isLocalMode()) {
        setShowUpgradeDialog(true);
      }
    }
  }, [billingUiEnabled, subscriptionData, subscriptionStatus, initialLoadCompleted]);

  const handleDismissUpgradeDialog = () => {
    setShowUpgradeDialog(false);
    localStorage.setItem('upgrade_dialog_displayed', 'true');
  };

  useEffect(() => {
    logThreadDebug('🌊 [streamingToolCall] useEffect triggered:', {
      shouldUsePrimaryToolPanelLivePath,
      hasStreamingToolCall: !!streamingToolCall,
      toolCall: streamingToolCall,
      timestamp: Date.now()
    });

    if (!shouldUsePrimaryToolPanelLivePath) {
      return;
    }

    if (streamingToolCall) {
      logThreadDebug('🔄 [streamingToolCall] Calling handleStreamingToolCall with:', streamingToolCall);
      handleStreamingToolCall(streamingToolCall);
    }
  }, [
    handleStreamingToolCall,
    shouldUsePrimaryToolPanelLivePath,
    streamingToolCall,
  ]);

  const [latestStreamingText, setLatestStreamingText] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (shouldTrackPrimaryStreamingText) {
      return;
    }

    setLatestStreamingText((previousValue) =>
      previousValue === undefined ? previousValue : undefined,
    );
  }, [shouldTrackPrimaryStreamingText]);

  // Extract streaming text from tool call chunk for real-time file content display
  useEffect(() => {
    if (!shouldTrackPrimaryStreamingText) return;
    if (!streamingToolCall) return;
    // Extract arguments from the tool call - this is the JSON string with file_path, file_contents, etc.
    if (streamingToolCall.arguments) {
      const rawArguments =
        typeof streamingToolCall.arguments === 'string'
          ? streamingToolCall.arguments
          : JSON.stringify(streamingToolCall.arguments);
      logThreadDebug('🔄 [page.tsx] streamingText extracted:', {
        toolName: streamingToolCall.name,
        argumentsLength: rawArguments.length,
        argumentsPreview: rawArguments.substring(0, 150),
      });

      const normalizedIncomingWriteArgs = normalizeWriteFileArgs(rawArguments);
      if (normalizedIncomingWriteArgs) {
        setLatestStreamingText((previousValue) => {
          const mergedWriteArgs = mergeNormalizedWriteFileArgs(
            previousValue,
            rawArguments,
          );
          const nextValue =
            stringifyNormalizedWriteFileArgs(mergedWriteArgs) || rawArguments;
          return previousValue === nextValue ? previousValue : nextValue;
        });
        return;
      }

      setLatestStreamingText((previousValue) =>
        previousValue === rawArguments ? previousValue : rawArguments,
      );
    }
  }, [shouldTrackPrimaryStreamingText, streamingToolCall]);

  const streamingText = shouldTrackPrimaryStreamingText
    ? latestStreamingText
    : undefined;

  useEffect(() => {
    setIsSidePanelAnimating(true);
    const timer = setTimeout(() => setIsSidePanelAnimating(false), 200); // Match transition duration
    return () => clearTimeout(timer);
  }, [isSidePanelOpen]);

  // Scroll detection for show/hide scroll-to-bottom button
  useEffect(() => {
    const handleScroll = () => {
      if (!scrollContainerRef.current) return;

      const scrollTop = scrollContainerRef.current.scrollTop;
      const scrollHeight = scrollContainerRef.current.scrollHeight;
      const clientHeight = scrollContainerRef.current.clientHeight;
      const threshold = 100;

      // With flex-column-reverse, scrollTop becomes NEGATIVE when scrolling up
      // Show button when scrollTop < -threshold (scrolled up enough from bottom)
      const shouldShow = scrollTop < -threshold && scrollHeight > clientHeight;
      setShowScrollToBottom(shouldShow);
    };

    const scrollContainer = scrollContainerRef.current;
    if (scrollContainer) {
      scrollContainer.addEventListener('scroll', handleScroll, { passive: true });
      // Check initial state
      setTimeout(() => handleScroll(), 100);

      return () => {
        scrollContainer.removeEventListener('scroll', handleScroll);
      };
    }
  }, [messages, initialLoadCompleted]);

  if (!initialLoadCompleted || isLoading) {
    return <ThreadSkeleton isSidePanelOpen={isSidePanelOpen} />;
  }

  if (error) {
    return (
      <ThreadLayout
        threadId={threadId}
        projectName={activeProjectName}
        projectId={activeProject?.id || ''}
        project={activeProject}
        sandboxId={activeSandboxId}
        isSidePanelOpen={isSidePanelOpen}
        onToggleSidePanel={handleSidePanelToggle}
        onViewFiles={handleOpenFileViewer}
        fileViewerOpen={fileViewerOpen}
        setFileViewerOpen={setFileViewerOpen}
        fileToView={fileToView}
        filePathList={filePathList}
        toolCalls={toolCalls}
        messages={messages as ApiMessageType[]}
        externalNavIndex={externalNavIndex}
        agentStatus={agentStatus}
        currentToolIndex={currentToolIndex}
        onSidePanelNavigate={handleSidePanelNavigate}
        onSidePanelClose={handleSidePanelClose}
        renderAssistantMessage={toolViewAssistant}
        renderToolResult={toolViewResult}
        isLoading={!initialLoadCompleted || isLoading}
        showBillingAlert={billingUiEnabled && showBillingAlert}
        billingData={billingData}
        onDismissBilling={() => setShowBillingAlert(false)}
        debugMode={debugMode}
        isMobile={isMobile}
        initialLoadCompleted={initialLoadCompleted}
        agentName={agent && agent.name}
      >
        <ThreadError error={error} />
      </ThreadLayout>
    );
  }

  return (
    <>
      <ThreadLayout
        threadId={threadId}
        projectName={activeProjectName}
        projectId={activeProject?.id || ''}
        project={activeProject}
        sandboxId={activeSandboxId}
        isSidePanelOpen={isSidePanelOpen}
        onToggleSidePanel={handleSidePanelToggle}
        onProjectRenamed={handleProjectRenamed}
        onViewFiles={handleOpenFileViewer}
        fileViewerOpen={fileViewerOpen}
        setFileViewerOpen={setFileViewerOpen}
        fileToView={fileToView}
        filePathList={filePathList}
        toolCalls={toolCalls}
        messages={messages as ApiMessageType[]}
        externalNavIndex={externalNavIndex}
        agentStatus={agentStatus}
        currentToolIndex={currentToolIndex}
        onSidePanelNavigate={handleSidePanelNavigate}
        onSidePanelClose={handleSidePanelClose}
        renderAssistantMessage={toolViewAssistant}
        renderToolResult={toolViewResult}
        isLoading={!initialLoadCompleted || isLoading}
        showBillingAlert={billingUiEnabled && showBillingAlert}
        billingData={billingData}
        onDismissBilling={() => setShowBillingAlert(false)}
        debugMode={debugMode}
        isMobile={isMobile}
        initialLoadCompleted={initialLoadCompleted}
        agentName={agent && agent.name}
        disableInitialAnimation={!initialLoadCompleted && toolCalls.length > 0}
        streamingText={streamingText}
      >
        {/* {workflowId && (
          <div className="px-4 pt-4">
            <WorkflowInfo workflowId={workflowId} />
          </div>
        )} */}
        {isDeferredSubagentInspectionMode && (
          <div className="px-4 pt-4">
            <div className="flex items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white/95 px-4 py-3 shadow-[0_12px_30px_-24px_rgba(15,23,42,0.35)] backdrop-blur-sm">
              <div className="flex min-w-0 items-center gap-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleReturnToShadowCloneMain}
                  className="h-8 gap-1.5 rounded-xl px-2.5 text-xs text-slate-600 hover:text-slate-900"
                  title="返回主视图 / Back to main view"
                >
                  <ArrowLeft className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">
                    返回主视图 / Back to main view
                  </span>
                </Button>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-900">
                    当前正在查看 {' '}
                    <span className="inline-flex max-w-full items-center rounded-md bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-900 align-middle">
                      {activeShadowCloneName}
                    </span>
                    {' '}
                    的实时输出 / live output
                  </p>
                  <p className="mt-1 text-xs leading-5 text-slate-500">
                    点击左上角按钮返回主视图和右侧团队成员列表。
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}

        <ThreadContent
          messages={effectiveThreadMessages}
          streamingTextContent={effectiveStreamingTextContent}
          streamingReasoningContent={effectiveStreamingReasoningContent}
          streamingToolCall={effectiveStreamingToolCall}
          agentStatus={effectiveThreadAgentStatus}
          handleToolClick={handleToolClickWithPanelFocus}
          handleOpenFileViewer={handleOpenFileViewer}
          readOnly={false}
          streamHookStatus={effectiveThreadStreamHookStatus}
          sandboxId={activeSandboxId}
          project={activeProject}
          debugMode={debugMode}
          agentName={effectiveThreadAgentName}
          agentAvatar={undefined}
          agentMetadata={isDeferredSubagentInspectionMode ? undefined : agent?.metadata}
          agentData={isDeferredSubagentInspectionMode ? undefined : agent}
          scrollContainerRef={scrollContainerRef}
          isWritingFile={
            isDeferredSubagentInspectionMode
              ? subagentIsWritingFile
              : shouldSuppressPrimaryThreadIndicator
                ? false
                : isWritingFile
          }
          suppressAgentActivity={shouldSuppressPrimaryThreadMessages}
          showPendingAssistantLoader={showPendingAssistantLoader}
          onShadowCloneSubtaskSelect={() => {}}
        />


        <div
          className={cn(
            "fixed bottom-0 z-10 bg-gradient-to-t from-background via-background/90 to-transparent px-4 pt-8",
            isSidePanelAnimating ? "" : "transition-[left,right] duration-200 ease-in-out",
            leftSidebarState === 'expanded' ? 'left-[72px] md:left-[256px]' : 'left-[72px]',
            isSidePanelOpen && !isMobile ? 'right-[90%] sm:right-[450px] md:right-[500px] lg:right-[550px] xl:right-[650px]' : 'right-0',
            isMobile ? 'left-0 right-0' : ''
          )}>
          <div className={cn(
            "mx-auto",
            isMobile ? "w-full" : "max-w-3xl"
          )}>
            {showCompletionHint && (
              <div
                className="mb-2 rounded-md border bg-muted/70 px-3 py-2 text-xs text-muted-foreground"
                role="status"
                aria-live="polite"
              >
                {t('thread.completionHint')}
              </div>
            )}
            {!isSubagentInspectionMode && (
              <ChatInput
                value={newMessage}
                onChange={setNewMessage}
                onSubmit={handleSubmitMessage}
                placeholder={t('chat.placeholder')}
                loading={isSending}
                disabled={isSending || agentStatus === 'running' || agentStatus === 'connecting' || agentStatus === 'disconnecting'}
                isAgentRunning={agentStatus === 'running' || agentStatus === 'connecting' || agentStatus === 'disconnecting'}
                onStopAgent={handleStopAgent}
                autoFocus={!isLoading}
                onFileBrowse={handleOpenFileViewer}
                projectId={projectId}
                sandboxId={activeSandboxId || undefined}
                messages={messages}
                agentName={resolvedAgentName}
                selectedAgentId={selectedAgentId}
                onAgentSelect={setSelectedAgent}
                toolCalls={toolCalls}
                toolCallIndex={currentToolIndex}
                showToolPreview={!isSidePanelOpen && toolCalls.length > 0}
                onExpandToolPreview={() => {
                  clearShadowCloneActiveSubtask();
                  setIsSidePanelOpen(true);
                  userClosedPanelRef.current = false;
                }}
                defaultShowSnackbar={false}
                showScrollToBottomIndicator={showScrollToBottom}
                onScrollToBottom={scrollToBottom}
                enableShadowCloneUI={true}
              />
            )}
          </div>
        </div>
      </ThreadLayout>

      {billingUiEnabled && (
        <UpgradeDialog
          open={showUpgradeDialog}
          onOpenChange={setShowUpgradeDialog}
          onDismiss={handleDismissUpgradeDialog}
        />
      )}

      {agentLimitData && (
        <AgentRunLimitDialog
          open={showAgentLimitDialog}
          onOpenChange={setShowAgentLimitDialog}
          runningCount={agentLimitData.runningCount}
          runningThreadIds={agentLimitData.runningThreadIds}
          projectId={projectId}
        />
      )}
    </>
  );
}

// 🎯 主组件 - 只负责参数验证，避免条件性调用hooks
export default function ThreadPage({
  params,
}: {
  params: Promise<{
    projectId: string;
    threadId: string;
  }>;
}) {
  const unwrappedParams = React.use(params);
  const { projectId, threadId } = unwrappedParams;

  // 确保参数存在后再进行后续逻辑
  if (!projectId || !threadId) {
    return <ThreadSkeleton isSidePanelOpen={false} />;
  }

  // 参数有效，渲染主要内容
  return <ThreadPageContent projectId={projectId} threadId={threadId} />;
} 
