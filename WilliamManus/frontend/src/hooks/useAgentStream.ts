import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  streamAgent,
  getAgentStatus,
  stopAgent,
  AgentRun,
  getMessages,
} from '@/lib/api';
import { toast } from 'sonner';
import {
  UnifiedMessage,
  ParsedContent,
  ParsedMetadata,
} from '@/components/thread/types';
import { safeJsonParse } from '@/components/thread/utils';
import { isFinalAssistantStreamStatus } from './useAgentStream-final-status';
import {
  isBenignAgentNotRunningError,
  isBenignPostTerminalStreamError,
  isLikelyStreamConnectionError,
} from '@/lib/stream-errors';
import {
  mergeNormalizedWriteFileArgs,
  normalizeWriteFileArgs,
  stringifyNormalizedWriteFileArgs,
} from '@/lib/write-file-stream';
import {
  useShadowCloneStore,
  type ShadowCloneLiveActivity,
} from '@/lib/stores/shadow-clone-store';
import {
  deriveShadowCloneStreamOwnership,
  resolveShadowCloneCanonicalRouting,
} from '@/lib/shadow-clone-stream-ownership';
import { resolveClaudeSDKSubagentRoute } from '@/lib/claude-sdk-subagent-routing';
import {
  buildSyntheticTerminalAssistantMessage,
  getRenderableAssistantText,
} from './useAgentStream-terminal-reconciliation';
import {
  isSubagentChunkEvent,
  isSubagentCompleteWithToolCalls,
  extractSubagentStreamingText,
  extractSubagentReasoningText,
  buildSubagentToolCallMessage,
} from './useAgentStream-scv2-routing';

const normalizeReasoningForCompare = (value: string): string =>
  value.replace(/\r\n/g, '\n').replace(/[ \t]+\n/g, '\n').trim();

interface StreamContentChunk {
  content: string;
  sequence?: number;
}

const joinOrderedStreamChunks = (chunks: StreamContentChunk[]): string => {
  if (chunks.length === 0) return '';
  if (chunks.length === 1) return chunks[0]?.content || '';

  let requiresSort = false;
  let previousSequence = chunks[0]?.sequence ?? 0;

  for (let index = 1; index < chunks.length; index += 1) {
    const sequence = chunks[index]?.sequence ?? 0;
    if (sequence < previousSequence) {
      requiresSort = true;
      break;
    }
    previousSequence = sequence;
  }

  const source = requiresSort
    ? chunks.slice().sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0))
    : chunks;

  return source.reduce((combined, chunk) => combined + chunk.content, '');
};

const createShadowCloneFallbackLiveActivity = (
  phase: 'planning' | 'execution' | 'aggregate',
): ShadowCloneLiveActivity => ({
  scope: phase === 'planning' ? 'shadow_clone_main' : 'main_agent',
  phase,
  reason: 'stream_route_metadata',
});

const buildShadowCloneLiveActivitySignature = (
  activity: Partial<ShadowCloneLiveActivity> | null,
  runId: string | null | undefined,
): string =>
  [
    runId ?? '',
    activity?.scope ?? 'null',
    activity?.phase ?? 'null',
    activity?.reason ?? '',
    activity?.subtask_id ?? '',
    activity?.epoch ?? '',
  ].join('|');

const STREAM_TOOL_DEBUG_ENABLED =
  process.env.NEXT_PUBLIC_AGENT_STREAM_DEBUG === 'true';
const WRITE_TOOLCALL_THROTTLE_MS = 100;
const COMPLETION_REASONING_GRACE_MS = 1200;
const STREAM_CURSOR_STORAGE_KEY_PREFIX = 'agent-stream-cursor:';
const STREAM_CURSOR_PERSIST_THROTTLE_MS = 750;
const STREAM_CURSOR_PERSIST_MIN_DELTA = 25;
const SHADOW_CLONE_EVENTS = new Set([
  'shadow_clone_planning_started',
  'shadow_clone_environment_preparing',
  'shadow_clone_environment_ready',
  'shadow_clone_environment_recovering',
  'shadow_clone_proposed',
  'subagent_started',
  'subagent_completed',
  'subagent_failed',
  'shadow_clone_subagent_recovering',
  'shadow_clone_subagent_wake_sent',
  'shadow_clone_subagent_resumed',
  'shadow_clone_subagent_replacement_started',
  'shadow_clone_subagent_replacement_completed',
  'shadow_clone_subagent_recovery_exhausted',
  'subagent_activity',
  'heartbeat',
  'shadow_clone_aggregating',
  'shadow_clone_complete',
  'shadow_clone_v2_started',
  'shadow_clone_v2_plan_created',
  'shadow_clone_v2_execution_completed',
  'shadow_clone_v2_execution_failed',
  'shadow_clone_v2_projection',
]);

const debugToolStreamLog = (...args: unknown[]) => {
  if (!STREAM_TOOL_DEBUG_ENABLED) return;
  console.log(...args);
};

const debugStreamLifecycleLog = (
  event: string,
  payload?: Record<string, unknown>,
) => {
  if (!STREAM_TOOL_DEBUG_ENABLED) return;
  console.log('🧭 [useAgentStream]', {
    event,
    ...(payload ?? {}),
  });
};

interface PersistedStreamCursor {
  threadId: string;
  runId: string;
  eventIndex: number;
  eventCursor?: string;
  updatedAt: number;
}

const getStreamCursorStorage = (): Storage | null => {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
};

const getStreamCursorStorageKey = (threadId: string, runId: string): string =>
  `${STREAM_CURSOR_STORAGE_KEY_PREFIX}${threadId}:${runId}`;

const readPersistedStreamCursor = (
  threadId: string,
  runId: string,
): PersistedStreamCursor | null => {
  const storage = getStreamCursorStorage();
  if (!storage) return null;

  const rawValue = storage.getItem(getStreamCursorStorageKey(threadId, runId));
  if (!rawValue) return null;

  try {
    const parsed = JSON.parse(rawValue) as PersistedStreamCursor | number;
    if (typeof parsed === 'number') {
      return parsed >= 0
        ? { threadId, runId, eventIndex: parsed, updatedAt: Date.now() }
        : null;
    }
    const eventIndex =
      typeof parsed?.eventIndex === 'number' ? parsed.eventIndex : null;
    if (eventIndex === null || eventIndex < 0) return null;
    return {
      threadId,
      runId,
      eventIndex,
      eventCursor:
        typeof parsed?.eventCursor === 'string' && parsed.eventCursor.trim()
          ? parsed.eventCursor
          : undefined,
      updatedAt:
        typeof parsed?.updatedAt === 'number' ? parsed.updatedAt : Date.now(),
    };
  } catch {
    const fallbackIndex = Number(rawValue);
    if (Number.isFinite(fallbackIndex) && fallbackIndex >= 0) {
      return { threadId, runId, eventIndex: fallbackIndex, updatedAt: Date.now() };
    }
  }

  storage.removeItem(getStreamCursorStorageKey(threadId, runId));
  return null;
};

const persistStreamCursorValue = (
  threadId: string,
  runId: string,
  eventIndex: number,
  eventCursor?: string | null,
): void => {
  const storage = getStreamCursorStorage();
  if (!storage || eventIndex < 0) return;

  const payload: PersistedStreamCursor = {
    threadId,
    runId,
    eventIndex,
    ...(eventCursor ? { eventCursor } : {}),
    updatedAt: Date.now(),
  };
  storage.setItem(
    getStreamCursorStorageKey(threadId, runId),
    JSON.stringify(payload),
  );
};

const clearPersistedStreamCursor = (threadId: string, runId: string): void => {
  const storage = getStreamCursorStorage();
  if (!storage) return;
  storage.removeItem(getStreamCursorStorageKey(threadId, runId));
};

const isWriteFileToolName = (toolName: string): boolean => {
  const normalized = toolName.toLowerCase();
  return (
    normalized === 'write_file' ||
    normalized === 'write-file' ||
    normalized === 'writefile' ||
    (normalized.includes('write') && normalized.includes('file'))
  );
};

interface StreamToolCallEntry {
  id?: string;
  index?: number;
  function?: {
    name?: string;
    arguments?: unknown;
  };
}

const getStreamToolCalls = (toolCalls: unknown): StreamToolCallEntry[] => {
  if (!Array.isArray(toolCalls)) return [];

  return toolCalls.filter(
    (toolCall): toolCall is StreamToolCallEntry =>
      Boolean(toolCall) && typeof toolCall === 'object',
  );
};

const getStreamToolCallName = (toolCall: StreamToolCallEntry | null | undefined): string =>
  typeof toolCall?.function?.name === 'string' ? toolCall.function.name : '';

const getStreamToolCallArguments = (
  toolCall: StreamToolCallEntry | null | undefined,
): unknown => toolCall?.function?.arguments;

const getStreamToolCallId = (
  toolCall: StreamToolCallEntry | null | undefined,
): string | undefined =>
  typeof toolCall?.id === 'string' && toolCall.id ? toolCall.id : undefined;

const getStreamToolCallIndex = (
  toolCall: StreamToolCallEntry | null | undefined,
): number | undefined =>
  typeof toolCall?.index === 'number' ? toolCall.index : undefined;

const selectRelevantStreamToolCall = (
  toolCalls: StreamToolCallEntry[],
): StreamToolCallEntry | null => {
  if (!toolCalls.length) return null;

  return (
    toolCalls.find((toolCall) =>
      isWriteFileToolName(getStreamToolCallName(toolCall)),
    ) || toolCalls[0]
  );
};

const streamToolCallsMatch = (
  candidate: StreamToolCallEntry,
  reference: StreamToolCallEntry,
): boolean => {
  const candidateId = getStreamToolCallId(candidate);
  const referenceId = getStreamToolCallId(reference);
  if (candidateId && referenceId) {
    return candidateId === referenceId;
  }

  const candidateIndex = getStreamToolCallIndex(candidate);
  const referenceIndex = getStreamToolCallIndex(reference);
  if (candidateIndex !== undefined && referenceIndex !== undefined) {
    return candidateIndex === referenceIndex;
  }

  const candidateName = getStreamToolCallName(candidate);
  const referenceName = getStreamToolCallName(reference);
  return Boolean(candidateName && referenceName && candidateName === referenceName);
};

const getWriteFileArgsScore = (rawArguments: unknown): number => {
  const normalizedArgs = normalizeWriteFileArgs(rawArguments);
  if (!normalizedArgs) return -1;

  const fullContentLength = normalizedArgs.file_contents?.length ?? 0;
  const deltaLength = normalizedArgs.file_contents_delta?.length ?? 0;
  return (
    (normalizedArgs.file_path ? 1000 : 0) +
    Math.max(fullContentLength, deltaLength) * 4 +
    (typeof normalizedArgs.delta_index === 'number' ? 20 : 0)
  );
};

const selectWriteFileArguments = (
  contentArguments: unknown,
  metadataArguments: unknown,
): unknown => {
  const mergedArgs =
    mergeNormalizedWriteFileArgs(contentArguments, metadataArguments) ||
    mergeNormalizedWriteFileArgs(metadataArguments, contentArguments);
  if (mergedArgs) {
    return stringifyNormalizedWriteFileArgs(mergedArgs);
  }

  const contentScore = getWriteFileArgsScore(contentArguments);
  const metadataScore = getWriteFileArgsScore(metadataArguments);
  if (contentScore > metadataScore) {
    return contentArguments ?? metadataArguments;
  }

  return metadataArguments ?? contentArguments;
};

const mapAgentStatus = (backendStatus: string): string => {
  switch (backendStatus) {
    case 'completed':
      return 'completed';
    case 'stopped':
      return 'stopped';
    case 'failed':
      return 'failed';
    default:
      return 'error';
  }
};


interface ApiMessageType {
  message_id?: string;
  thread_id?: string;
  type: string;
  is_llm_message?: boolean;
  content: string;
  metadata?: string;
  created_at?: string;
  updated_at?: string;
  agent_id?: string;
  agents?: {
    name: string;
    avatar?: string;
    avatar_color?: string;
  };
}

interface StreamEventMessage extends UnifiedMessage {
  event_index?: number;
  event_cursor?: string;
  event_id?: string;
  non_progress?: boolean;
  status?: string;
}

// Define the structure returned by the hook
export interface UseAgentStreamResult {
  status: string;
  textContent: string;
  reasoningContent: string; // 思考/推理内容
  toolCall: ParsedContent | null;
  error: string | null;
  agentRunId: string | null; // Expose the currently managed agentRunId
  isWritingFile: boolean; // Whether agent is thinking or working
  startStreaming: (runId: string) => void;
  stopStreaming: () => Promise<void>;
}

export type AgentStreamStatusSource = 'stream' | 'terminal' | 'lifecycle';

export interface AgentStreamStatusContext {
  status: string;
  runId: string | null;
  ownerRunId: string | null;
  epoch: number;
  source: AgentStreamStatusSource;
}

interface StreamOwnershipContext {
  runId: string;
  epoch: number;
}

// Define the callbacks the hook consumer can provide
export interface AgentStreamCallbacks {
  onMessage: (message: UnifiedMessage) => void; // Callback for complete messages
  onStatusChange?: (
    status: string,
    context?: AgentStreamStatusContext,
  ) => void; // Optional: Notify on internal status changes
  onError?: (error: string) => void; // Optional: Notify on errors
  onClose?: (finalStatus: string) => void; // Optional: Notify when streaming definitively ends
  onAssistantStart?: () => void; // Optional: Notify when assistant starts streaming
  onAssistantChunk?: (chunk: { content: string }) => void; // Optional: Notify on each assistant message chunk
  onReasoningChunk?: (chunk: { content: string }) => void; // Optional: Notify on each reasoning chunk
  onToolCallChunk?: (message: UnifiedMessage) => void; // Optional: Notify on tool call chunks
}

// Helper function to map API messages to UnifiedMessages
const mapApiMessagesToUnified = (
  messagesData: ApiMessageType[] | null | undefined,
  currentThreadId: string,
): UnifiedMessage[] => {
  return (messagesData || [])
    .filter((msg) => msg.type !== 'status')
    .map((msg: ApiMessageType) => ({
      message_id: msg.message_id || null,
      thread_id: msg.thread_id || currentThreadId,
      type: (msg.type || 'system') as UnifiedMessage['type'],
      is_llm_message: Boolean(msg.is_llm_message),
      content: msg.content || '',
      metadata: msg.metadata || '{}',
      created_at: msg.created_at || new Date().toISOString(),
      updated_at: msg.updated_at || new Date().toISOString(),
      agent_id: (msg as any).agent_id,
      agents: (msg as any).agents,
    }));
};

const isEmptyAssistantMessage = (message: UnifiedMessage): boolean => {
  if (message.type !== 'assistant') return false;

  const parsed = safeJsonParse<ParsedContent | string>(message.content, {});
  let textContent: string | undefined;
  let reasoningText: string | undefined;
  let toolCalls: unknown[] | undefined;

  if (typeof parsed === 'string') {
    textContent = parsed;
  } else if (parsed && typeof parsed === 'object') {
    if (typeof parsed.content === 'string') {
      textContent = parsed.content;
    }
    if (typeof parsed.reasoning_content === 'string') {
      reasoningText = parsed.reasoning_content;
    }
    if (Array.isArray(parsed.tool_calls)) {
      toolCalls = parsed.tool_calls;
    }
  }

  const hasToolCalls = Array.isArray(toolCalls) && toolCalls.length > 0;
  const hasReasoning = typeof reasoningText === 'string' && reasoningText.trim().length > 0;
  const trimmedText = typeof textContent === 'string' ? textContent.trim() : '';
  const hasText = trimmedText.length > 0 && trimmedText !== '(empty message)';

  return !hasToolCalls && !hasReasoning && !hasText;
};

const hasUserFacingAssistantNarrative = (
  parsedContent: ParsedContent | string | null | undefined,
): boolean => {
  if (typeof parsedContent === 'string') {
    const trimmedText = parsedContent.trim();
    return trimmedText.length > 0 && trimmedText !== '(empty message)';
  }

  if (!parsedContent || typeof parsedContent !== 'object') {
    return false;
  }

  const textContent =
    typeof parsedContent.content === 'string'
      ? parsedContent.content
      : typeof parsedContent.content === 'number'
        ? String(parsedContent.content)
        : '';
  const reasoningContent =
    typeof parsedContent.reasoning_content === 'string'
      ? parsedContent.reasoning_content
      : '';
  const trimmedText = textContent.trim();

  return (
    reasoningContent.trim().length > 0 ||
    (trimmedText.length > 0 && trimmedText !== '(empty message)')
  );
};

const getShadowCloneSemanticCategory = ({
  messageType,
  streamStatus,
  parsedContent,
}: {
  messageType: UnifiedMessage['type'];
  streamStatus?: string;
  parsedContent: ParsedContent;
}): 'user_facing' | 'operational' => {
  if (messageType !== 'assistant') {
    return 'operational';
  }

  if (streamStatus === 'reasoning_chunk' || streamStatus === 'chunk') {
    return 'user_facing';
  }

  if (isFinalAssistantStreamStatus(streamStatus) || !streamStatus) {
    return hasUserFacingAssistantNarrative(parsedContent)
      ? 'user_facing'
      : 'operational';
  }

  return 'operational';
};

export const BACKEND_LOG_PATTERNS = [
  /^Traceback\s*\(most recent call last\):/im,
  /^\s*File\s+"[^"]+",\s+line\s+\d+/im,
  /redis\.exceptions\./i,
  /^(ERROR|WARNING|INFO|DEBUG):root:/im,
  /^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.]\d{3}\s+\|/im,  // timestamped log lines
  /^\s*raise\s+\w+/im,
  /\[\d{4}-\d{2}-\d{2}.*?\]\s*(ERROR|WARN|INFO|DEBUG)/i,          // logfmt/bracket timestamps
  /^redis\.commands\./i,
];

export const isBackendLogContent = (text: string): boolean => {
  if (!text || !text.trim()) return false;
  const trimmed = text.trim();
  // Quick rejection: if the text looks like natural language (starts with
  // common sentence patterns), skip pattern matching
  if (/^(I|We|You|The|This|Here|Let|Now|It|That|Our|Your|Based|Following|First|Next)\s/i.test(trimmed)) {
    return false;
  }
  return BACKEND_LOG_PATTERNS.some((pattern) => pattern.test(trimmed));
};

export const isBackendLogMessage = (parsedContent: ParsedContent | string): boolean => {
  if (typeof parsedContent === 'string') {
    return isBackendLogContent(parsedContent);
  }
  if (parsedContent && typeof parsedContent === 'object') {
    const textContent = typeof parsedContent.content === 'string' ? parsedContent.content : '';
    return isBackendLogContent(textContent);
  }
  return false;
};

export function useAgentStream(
  callbacks: AgentStreamCallbacks,
  threadId: string,
  setMessages: (messages: UnifiedMessage[]) => void,
): UseAgentStreamResult {
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<string>('idle');
  const [textContent, setTextContent] = useState<StreamContentChunk[]>([]);
  const [reasoningContent, setReasoningContent] = useState<StreamContentChunk[]>([]); // 思考/推理内容
  const [toolCall, setToolCall] = useState<ParsedContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isWritingFile, setIsWritingFile] = useState<boolean>(false);
  const indicatorStartRef = useRef<number | null>(null);
  const indicatorHideTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const completionReasoningTimerRef = useRef<NodeJS.Timeout | null>(null);
  const completionReasoningHoldRef = useRef<string>('');
  const minIndicatorMs = 800;

  const streamCleanupRef = useRef<(() => void) | null>(null);
  const isMountedRef = useRef<boolean>(true);
  const currentRunIdRef = useRef<string | null>(null); // Ref to track the run ID being processed
  const streamEpochRef = useRef<number>(0);
  const activeStreamEpochRef = useRef<number>(0);
  const threadIdRef = useRef(threadId); // Ref to hold the current threadId
  const isStartingStreamRef = useRef<boolean>(false);
  const guardGenerationRef = useRef<number>(0);
  const pendingStartRetryRef = useRef<{ runId: string; attempts: number; timer: ReturnType<typeof setTimeout> | null } | null>(null);
  const reconnectAttemptsRef = useRef<number>(0);
  const reconnectTimerRef = useRef<NodeJS.Timeout | null>(null);
  const startStreamingRef = useRef<((runId: string) => void) | null>(null);
  const stopDetectionTimerRef = useRef<NodeJS.Timeout | null>(null); // 1秒超时定时器
  const lastToolCompletionTimeRef = useRef<number | null>(null); // V2: 工具完成时间
  const hasTextAfterToolRef = useRef<boolean>(false); // V2: 工具后是否有文本
  const idleCheckTimerRef = useRef<NodeJS.Timeout | null>(null);
  const fileStreamBuffersRef = useRef<Map<string, { content: string; filePath?: string; lastDeltaIndex?: number }>>(
    new Map(),
  );
  const lastShadowCloneLiveActivitySignatureRef = useRef<string | null>(null);
  const pendingWriteToolCallRef = useRef<{
    payload: ParsedContent | null;
    message: UnifiedMessage | null;
  }>({
    payload: null,
    message: null,
  });
  const writeToolCallFlushTimerRef = useRef<NodeJS.Timeout | null>(null);
  const lastWriteToolCallFlushAtRef = useRef<number>(0);
  const reasoningSequenceSetRef = useRef<Set<number>>(new Set());
  const lastEventIndexRef = useRef<number>(-1);
  const lastEventCursorRef = useRef<string | null>(null);
  const lastPersistedEventIndexRef = useRef<number>(-1);
  const lastPersistedEventCursorRef = useRef<string | null>(null);
  const lastCursorPersistAtRef = useRef<number>(0);
  const emittedMessageSignaturesRef = useRef<Map<string, string>>(new Map());
  const visibleAssistantRunIdsRef = useRef<Set<string>>(new Set());
  const surfacedTerminalErrorSignatureRef = useRef<string | null>(null);
  const setMessagesRef = useRef(setMessages); // Ref to hold the setMessages function
  const statusRef = useRef(status);
  const agentRunIdRef = useRef(agentRunId);
  const textContentRef = useRef(textContent);
  const reasoningContentRef = useRef(reasoningContent);
  const terminalPromotionTextRef = useRef<string>('');
  const terminalPromotionReasoningRef = useRef<string>('');
  const idleCheckDelayMs = 30000;
  const maxReconnectAttempts = 3;
  const reconnectBaseDelayMs = 1000;

  const startToolIndicator = useCallback(() => {
    indicatorStartRef.current = Date.now();
    if (indicatorHideTimeoutRef.current) {
      clearTimeout(indicatorHideTimeoutRef.current);
      indicatorHideTimeoutRef.current = null;
    }
    setIsWritingFile(true);
  }, []);

  const stopToolIndicator = useCallback(() => {
    const startedAt = indicatorStartRef.current;
    if (!startedAt) {
      setIsWritingFile(false);
      return;
    }

    const elapsed = Date.now() - startedAt;
    const remaining = minIndicatorMs - elapsed;
    if (remaining <= 0) {
      indicatorStartRef.current = null;
      setIsWritingFile(false);
      return;
    }

    if (indicatorHideTimeoutRef.current) {
      clearTimeout(indicatorHideTimeoutRef.current);
    }
    indicatorHideTimeoutRef.current = setTimeout(() => {
      indicatorStartRef.current = null;
      setIsWritingFile(false);
      indicatorHideTimeoutRef.current = null;
    }, remaining);
  }, [minIndicatorMs]);

  const resetToolIndicator = useCallback(() => {
    if (indicatorHideTimeoutRef.current) {
      clearTimeout(indicatorHideTimeoutRef.current);
      indicatorHideTimeoutRef.current = null;
    }
    indicatorStartRef.current = null;
    setIsWritingFile(false);
  }, []);

  const surfaceTerminalFailure = useCallback(
    (ownership: StreamOwnershipContext, errorMessage: string) => {
      const signature = `${ownership.runId ?? 'unknown'}:${ownership.epoch}:${errorMessage}`;
      if (surfacedTerminalErrorSignatureRef.current === signature) {
        console.info('[useAgentStream] Suppressing duplicate terminal error surface:', signature);
        return;
      }

      surfacedTerminalErrorSignatureRef.current = signature;
      setError(errorMessage);
      if (callbacks.onError) {
        callbacks.onError(errorMessage);
        return;
      }
      toast.error(errorMessage, { duration: 15000 });
    },
    [callbacks],
  );

  const resetWriteToolCallThrottle = useCallback(() => {
    if (writeToolCallFlushTimerRef.current) {
      clearTimeout(writeToolCallFlushTimerRef.current);
      writeToolCallFlushTimerRef.current = null;
    }
    pendingWriteToolCallRef.current = { payload: null, message: null };
    lastWriteToolCallFlushAtRef.current = 0;
  }, []);

  const clearCompletionReasoningTimer = useCallback(() => {
    if (completionReasoningTimerRef.current) {
      clearTimeout(completionReasoningTimerRef.current);
      completionReasoningTimerRef.current = null;
    }
  }, []);

  const clearStreamingTextContent = useCallback(() => {
    textContentRef.current = [];
    terminalPromotionTextRef.current = '';
    setTextContent((prev) => (prev.length === 0 ? prev : []));
  }, []);

  const clearStreamingReasoningContent = useCallback(() => {
    reasoningContentRef.current = [];
    terminalPromotionReasoningRef.current = '';
    setReasoningContent((prev) => (prev.length === 0 ? prev : []));
  }, []);

  const scheduleCompletionReasoningClear = useCallback(
    (runId: string | null) => {
      clearCompletionReasoningTimer();
      if (!runId) {
        completionReasoningHoldRef.current = '';
        clearStreamingReasoningContent();
        return;
      }

      completionReasoningTimerRef.current = setTimeout(() => {
        completionReasoningTimerRef.current = null;
        if (
          !isMountedRef.current ||
          (currentRunIdRef.current && currentRunIdRef.current !== runId)
        ) {
          return;
        }
        completionReasoningHoldRef.current = '';
        clearStreamingReasoningContent();
      }, COMPLETION_REASONING_GRACE_MS);
    },
    [clearCompletionReasoningTimer, clearStreamingReasoningContent],
  );

  const flushWriteToolCall = useCallback(() => {
    if (writeToolCallFlushTimerRef.current) {
      clearTimeout(writeToolCallFlushTimerRef.current);
      writeToolCallFlushTimerRef.current = null;
    }

    const { payload, message } = pendingWriteToolCallRef.current;
    if (!payload) {
      return;
    }

    pendingWriteToolCallRef.current = { payload: null, message: null };
    if (!isMountedRef.current) {
      return;
    }
    lastWriteToolCallFlushAtRef.current = Date.now();
    setToolCall(payload);
    if (message) {
      callbacks.onToolCallChunk?.(message);
    }
  }, [callbacks]);

  const applyShadowCloneLiveActivityIfNeeded = useCallback(
    (
      store: ReturnType<typeof useShadowCloneStore.getState>,
      activity: Partial<ShadowCloneLiveActivity> | null,
      runId: string | null | undefined,
    ) => {
      const signature = buildShadowCloneLiveActivitySignature(activity, runId);
      if (lastShadowCloneLiveActivitySignatureRef.current === signature) {
        return;
      }
      lastShadowCloneLiveActivitySignatureRef.current = signature;
      store.applyLiveActivity(activity, runId);
    },
    [],
  );

  const enqueueWriteToolCall = useCallback(
    (payload: ParsedContent, message: UnifiedMessage) => {
      pendingWriteToolCallRef.current = { payload, message };
      const now = Date.now();
      const elapsed = now - lastWriteToolCallFlushAtRef.current;
      if (
        lastWriteToolCallFlushAtRef.current === 0 ||
        elapsed >= WRITE_TOOLCALL_THROTTLE_MS
      ) {
        flushWriteToolCall();
        return;
      }

      if (writeToolCallFlushTimerRef.current) {
        return;
      }
      const waitMs = Math.max(0, WRITE_TOOLCALL_THROTTLE_MS - elapsed);
      writeToolCallFlushTimerRef.current = setTimeout(() => {
        flushWriteToolCall();
      }, waitMs);
    },
    [flushWriteToolCall],
  );

  const orderedTextContent = useMemo(
    () => joinOrderedStreamChunks(textContent),
    [textContent],
  );
  const orderedReasoningContent = useMemo(
    () => joinOrderedStreamChunks(reasoningContent),
    [reasoningContent],
  );

  // Update refs whenever state changes
  useEffect(() => {
    statusRef.current = status;
  }, [status]);
  
  useEffect(() => {
    agentRunIdRef.current = agentRunId;
  }, [agentRunId]);
  
  useEffect(() => {
    textContentRef.current = textContent;
  }, [textContent]);

  useEffect(() => {
    reasoningContentRef.current = reasoningContent;
  }, [reasoningContent]);

  const persistCursor = useCallback(
    (
      runId: string | null,
      eventIndex: number,
      eventCursor?: string | null,
      options?: { force?: boolean },
    ) => {
      if (!runId || eventIndex < 0) return;

      const force = Boolean(options?.force);
      const previousEventIndex = lastPersistedEventIndexRef.current;
      const cursorOnlyUpdate =
        Boolean(eventCursor) &&
        eventCursor !== lastPersistedEventCursorRef.current;
      const now = Date.now();

      if (!force) {
        if (
          previousEventIndex >= eventIndex &&
          !(eventCursor && eventCursor !== lastPersistedEventCursorRef.current)
        ) {
          return;
        }
        if (
          !cursorOnlyUpdate &&
          previousEventIndex >= 0 &&
          eventIndex - previousEventIndex < STREAM_CURSOR_PERSIST_MIN_DELTA &&
          now - lastCursorPersistAtRef.current < STREAM_CURSOR_PERSIST_THROTTLE_MS
        ) {
          return;
        }
      }

      persistStreamCursorValue(threadId, runId, eventIndex, eventCursor);
      lastPersistedEventIndexRef.current = eventIndex;
      lastPersistedEventCursorRef.current = eventCursor || null;
      lastCursorPersistAtRef.current = now;
    },
    [threadId],
  );

  const clearPersistedCursor = useCallback(
    (runId: string | null) => {
      if (!runId) return;
      clearPersistedStreamCursor(threadId, runId);
      if (currentRunIdRef.current === runId || agentRunIdRef.current === runId) {
        lastPersistedEventIndexRef.current = -1;
        lastCursorPersistAtRef.current = 0;
      }
    },
    [threadId],
  );

  const invalidateStreamOwnership = useCallback((reason: string) => {
    streamEpochRef.current += 1;
    activeStreamEpochRef.current = streamEpochRef.current;
    debugStreamLifecycleLog('invalidate_stream_ownership', {
      reason,
      nextEpoch: activeStreamEpochRef.current,
      currentRunId: currentRunIdRef.current,
    });
  }, []);

  const isOwnershipActive = useCallback(
    (
      ownership: StreamOwnershipContext,
      event: string,
    ): boolean => {
      if (!isMountedRef.current) return false;

      if (ownership.epoch !== activeStreamEpochRef.current) {
        debugStreamLifecycleLog('ignore_stale_epoch_callback', {
          event,
          callbackRunId: ownership.runId,
          callbackEpoch: ownership.epoch,
          activeEpoch: activeStreamEpochRef.current,
          currentRunId: currentRunIdRef.current,
          status: statusRef.current,
        });
        return false;
      }

      if (
        currentRunIdRef.current &&
        ownership.runId !== currentRunIdRef.current
      ) {
        debugStreamLifecycleLog('ignore_stale_run_callback', {
          event,
          callbackRunId: ownership.runId,
          callbackEpoch: ownership.epoch,
          activeEpoch: activeStreamEpochRef.current,
          currentRunId: currentRunIdRef.current,
          status: statusRef.current,
        });
        return false;
      }

      return true;
    },
    [],
  );

  // 清理定时器
  useEffect(() => {
    const fileStreamBuffers = fileStreamBuffersRef.current;
    const emittedMessageSignatures = emittedMessageSignaturesRef.current;

    return () => {
      if (stopDetectionTimerRef.current) {
        clearTimeout(stopDetectionTimerRef.current);
        stopDetectionTimerRef.current = null;
      }
      if (idleCheckTimerRef.current) {
        clearTimeout(idleCheckTimerRef.current);
        idleCheckTimerRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (indicatorHideTimeoutRef.current) {
        clearTimeout(indicatorHideTimeoutRef.current);
        indicatorHideTimeoutRef.current = null;
      }
      if (completionReasoningTimerRef.current) {
        clearTimeout(completionReasoningTimerRef.current);
        completionReasoningTimerRef.current = null;
      }
      completionReasoningHoldRef.current = '';
      fileStreamBuffers.clear();
      lastEventIndexRef.current = -1;
      lastPersistedEventIndexRef.current = -1;
      lastPersistedEventCursorRef.current = null;
      lastEventCursorRef.current = null;
      lastCursorPersistAtRef.current = 0;
      lastShadowCloneLiveActivitySignatureRef.current = null;
      emittedMessageSignatures.clear();
      visibleAssistantRunIdsRef.current.clear();
      terminalPromotionTextRef.current = '';
      terminalPromotionReasoningRef.current = '';
    };
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const flushCursor = () => {
      const runId = currentRunIdRef.current ?? agentRunIdRef.current;
      if (!runId || lastEventIndexRef.current < 0) return;
      persistCursor(runId, lastEventIndexRef.current, lastEventCursorRef.current, {
        force: true,
      });
    };

    window.addEventListener('pagehide', flushCursor);
    window.addEventListener('beforeunload', flushCursor);
    return () => {
      window.removeEventListener('pagehide', flushCursor);
      window.removeEventListener('beforeunload', flushCursor);
      flushCursor();
    };
  }, [persistCursor]);

  // On thread change, ensure any existing stream is cleaned up to avoid stale subscriptions
  useEffect(() => {
    const previousThreadId = threadIdRef.current;
    if (previousThreadId && previousThreadId !== threadId) {
      if (streamCleanupRef.current) {
        // Close the existing stream for the previous thread
        streamCleanupRef.current();
        streamCleanupRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      invalidateStreamOwnership('thread_changed');
      setStatus('idle');
      clearStreamingTextContent();
      clearStreamingReasoningContent();
      clearCompletionReasoningTimer();
      completionReasoningHoldRef.current = '';
      setToolCall(null);
      setAgentRunId(null);
      currentRunIdRef.current = null;
      reconnectAttemptsRef.current = 0;
      reasoningSequenceSetRef.current.clear();
      fileStreamBuffersRef.current.clear();
      lastEventIndexRef.current = -1;
      lastEventCursorRef.current = null;
      lastPersistedEventIndexRef.current = -1;
      lastPersistedEventCursorRef.current = null;
      lastCursorPersistAtRef.current = 0;
      lastShadowCloneLiveActivitySignatureRef.current = null;
      emittedMessageSignaturesRef.current.clear();
      visibleAssistantRunIdsRef.current.clear();
      terminalPromotionTextRef.current = '';
      terminalPromotionReasoningRef.current = '';
    }
    threadIdRef.current = threadId;
  }, [
    clearCompletionReasoningTimer,
    clearStreamingReasoningContent,
    clearStreamingTextContent,
    threadId,
    invalidateStreamOwnership,
  ]);

  useEffect(() => {
    setMessagesRef.current = setMessages;
  }, [setMessages]);

  const normalizeTerminalStatus = (statusValue: string): string | null => {
    const normalized = statusValue.toLowerCase();
    if (normalized === 'completed') return 'completed';
    if (normalized === 'stopped') return 'stopped';
    if (normalized === 'failed') return 'failed';
    if (normalized === 'error') return 'failed';
    if (statusValue === 'END_STREAM') return 'completed';
    if (statusValue === 'ERROR') return 'failed';
    if (statusValue === 'STOP') return 'stopped';
    return null;
  };

  // Internal function to update status and notify consumer
  const updateStatus = useCallback(
    (
      newStatus: string,
      options?: {
        runId?: string | null;
        ownerRunId?: string | null;
        epoch?: number;
        source?: AgentStreamStatusSource;
      },
    ) => {
      if (isMountedRef.current) {
        if (
          typeof options?.epoch === 'number' &&
          options.epoch !== activeStreamEpochRef.current
        ) {
          debugStreamLifecycleLog('ignore_stale_status_update', {
            newStatus,
            requestedEpoch: options.epoch,
            activeEpoch: activeStreamEpochRef.current,
            requestedRunId: options.runId ?? null,
            ownerRunId: options.ownerRunId ?? null,
          });
          return;
        }
        const resolvedOwnerRunId =
          options?.ownerRunId ?? currentRunIdRef.current ?? agentRunId ?? null;
        const resolvedRunId =
          options?.runId ?? resolvedOwnerRunId;
        const statusContext: AgentStreamStatusContext = {
          status: newStatus,
          runId: resolvedRunId,
          ownerRunId: resolvedOwnerRunId,
          epoch: options?.epoch ?? activeStreamEpochRef.current,
          source: options?.source ?? 'stream',
        };
        debugStreamLifecycleLog('status_update', {
          newStatus,
          runId: resolvedRunId,
          ownerRunId: resolvedOwnerRunId,
          epoch: statusContext.epoch,
          source: statusContext.source,
          statusBefore: statusRef.current,
        });
        setStatus(newStatus);
        callbacks.onStatusChange?.(newStatus, statusContext);
        if (newStatus === 'error' && error) {
          callbacks.onError?.(error);
        }
        if (
          [
            'completed',
            'stopped',
            'failed',
            'error',
            'agent_not_running',
          ].includes(newStatus)
        ) {
          callbacks.onClose?.(newStatus);
        }
      }
    },
    [agentRunId, callbacks, error],
  ); // Include error dependency

  const emitMessage = useCallback(
    (message: UnifiedMessage) => {
      // Per SCV2 parity spec FR-004: filter backend log content from
      // user-facing chat panel. Assistant messages whose content matches
      // traceback/Redis/raw-log patterns are suppressed.
      if (message.type === 'assistant') {
        try {
          const parsed = typeof message.content === 'string'
            ? JSON.parse(message.content)
            : message.content;
          if (isBackendLogMessage(parsed)) {
            debugStreamLifecycleLog('suppress_backend_log_message', {
              messageId: message.message_id,
            });
            return;
          }
        } catch {
          // Non-JSON content — check as plain text
          if (isBackendLogContent(String(message.content ?? ''))) {
            return;
          }
        }
      }

      if (!message.message_id) {
        callbacks.onMessage(message);
        return;
      }

      const signature = `${message.updated_at ?? ''}|${message.content}|${message.metadata ?? ''}`;
      const previousSignature = emittedMessageSignaturesRef.current.get(message.message_id);
      if (previousSignature === signature) {
        return;
      }

      emittedMessageSignaturesRef.current.set(message.message_id, signature);
      callbacks.onMessage(message);
    },
    [callbacks],
  );

  const promoteTerminalStreamingContent = useCallback(
    (finalStatus: string, runId: string | null) => {
      if (
        !runId ||
        ![
          'completed',
          'stopped',
          'failed',
          'error',
          'agent_not_running',
        ].includes(finalStatus)
      ) {
        return;
      }

      if (visibleAssistantRunIdsRef.current.has(runId)) {
        return;
      }

      const streamedText =
        terminalPromotionTextRef.current ||
        joinOrderedStreamChunks(textContentRef.current);
      if (!streamedText.trim()) {
        return;
      }

      const streamedReasoning =
        terminalPromotionReasoningRef.current ||
        joinOrderedStreamChunks(reasoningContentRef.current);
      const syntheticMessage = buildSyntheticTerminalAssistantMessage({
        runId,
        threadId,
        textContent: streamedText,
        reasoningContent: streamedReasoning,
      }) as UnifiedMessage;

      visibleAssistantRunIdsRef.current.add(runId);
      emitMessage(syntheticMessage);
    },
    [emitMessage, threadId],
  );

  const promoteFinalAssistantStreamingContent = useCallback(
    (finalMessage: UnifiedMessage, runId: string | null) => {
      if (!runId || visibleAssistantRunIdsRef.current.has(runId)) {
        return false;
      }

      const finalMessageText = getRenderableAssistantText(finalMessage);
      const streamedText =
        terminalPromotionTextRef.current ||
        joinOrderedStreamChunks(textContentRef.current) ||
        (finalMessageText === '(empty message)' ? '' : finalMessageText);
      if (!streamedText.trim()) {
        return false;
      }

      const streamedReasoning =
        terminalPromotionReasoningRef.current ||
        joinOrderedStreamChunks(reasoningContentRef.current);
      const syntheticMessage = buildSyntheticTerminalAssistantMessage({
        runId,
        threadId,
        textContent: streamedText,
        reasoningContent: streamedReasoning,
      }) as UnifiedMessage;

      visibleAssistantRunIdsRef.current.add(runId);
      emitMessage(syntheticMessage);
      return true;
    },
    [emitMessage, threadId],
  );

  // Function to handle finalization of a stream (completion, stop, error)
  const finalizeStream = useCallback(
    (
      finalStatus: string,
      options?: {
        runId?: string | null;
        epoch?: number;
        reason?: string;
      },
    ) => {
      if (!isMountedRef.current) return;

      const runId = options?.runId ?? currentRunIdRef.current ?? agentRunId ?? null;
      const activeEpoch = activeStreamEpochRef.current;

      if (
        typeof options?.epoch === 'number' &&
        options.epoch !== activeEpoch
      ) {
        debugStreamLifecycleLog('ignore_stale_finalize_epoch', {
          finalStatus,
          requestedRunId: runId,
          requestedEpoch: options.epoch,
          activeEpoch,
          reason: options.reason,
          currentRunId: currentRunIdRef.current,
        });
        return;
      }

      if (runId && currentRunIdRef.current && runId !== currentRunIdRef.current) {
        debugStreamLifecycleLog('ignore_stale_finalize_run', {
          finalStatus,
          requestedRunId: runId,
          currentRunId: currentRunIdRef.current,
          epoch: options?.epoch ?? activeEpoch,
          reason: options.reason,
        });
        return;
      }

      if (streamCleanupRef.current) {
        streamCleanupRef.current();
        streamCleanupRef.current = null;
      }
      if (idleCheckTimerRef.current) {
        clearTimeout(idleCheckTimerRef.current);
        idleCheckTimerRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconnectAttemptsRef.current = 0;

      promoteTerminalStreamingContent(finalStatus, runId);

      // Reset streaming-specific state
      clearStreamingTextContent();
      const shouldPreserveCompletionReasoning =
        finalStatus === 'completed' &&
        Boolean(
          completionReasoningHoldRef.current.trim() ||
            reasoningContentRef.current.length > 0,
        );
      if (shouldPreserveCompletionReasoning) {
        scheduleCompletionReasoningClear(runId);
      } else {
        clearCompletionReasoningTimer();
        completionReasoningHoldRef.current = '';
        clearStreamingReasoningContent();
      }
      setToolCall(null);
      resetWriteToolCallThrottle();
      reasoningSequenceSetRef.current.clear();
      fileStreamBuffersRef.current.clear();
      lastEventIndexRef.current = -1;
      lastEventCursorRef.current = null;
      lastPersistedEventIndexRef.current = -1;
      lastPersistedEventCursorRef.current = null;
      lastCursorPersistAtRef.current = 0;
      resetToolIndicator();

      // Update status and clear run ID
      updateStatus(finalStatus, {
        runId,
        ownerRunId: runId,
        source: 'terminal',
        epoch: options?.epoch ?? activeEpoch,
      });
      setAgentRunId(null);
      currentRunIdRef.current = null;
      invalidateStreamOwnership(`finalize:${finalStatus}:${options?.reason ?? 'none'}`);

      // Message refetch disabled - optimistic messages will handle updates

      // If the run was stopped or completed, try to get final status to update nonRunning set (keep this)
      if (
        runId &&
        (finalStatus === 'completed' ||
          finalStatus === 'stopped' ||
          finalStatus === 'failed' ||
          finalStatus === 'error' ||
          finalStatus === 'agent_not_running')
      ) {
        clearPersistedCursor(runId);
        getAgentStatus(runId).catch((err) => {
        });
      }
    },
    [
      agentRunId,
      clearCompletionReasoningTimer,
      clearPersistedCursor,
      clearStreamingReasoningContent,
      clearStreamingTextContent,
      scheduleCompletionReasoningClear,
      invalidateStreamOwnership,
      promoteTerminalStreamingContent,
      resetToolIndicator,
      resetWriteToolCallThrottle,
      updateStatus,
    ],
  );

  const scheduleIdleCheck = useCallback(
    (ownership: StreamOwnershipContext) => {
      if (idleCheckTimerRef.current) {
        clearTimeout(idleCheckTimerRef.current);
      }
      idleCheckTimerRef.current = setTimeout(async () => {
        if (!isOwnershipActive(ownership, 'idle_check_timer_fired')) return;
        try {
          const latest = await getAgentStatus(ownership.runId);
          if (!isOwnershipActive(ownership, 'idle_check_status_resolved')) return;
          if (latest.status !== 'running') {
            finalizeStream(mapAgentStatus(latest.status) || 'agent_not_running', {
              runId: ownership.runId,
              epoch: ownership.epoch,
              reason: 'idle_check_non_running_status',
            });
            return;
          }
        } catch (err) {
          console.error('[useAgentStream] Idle status check failed:', err);
        }
        scheduleIdleCheck(ownership);
      }, idleCheckDelayMs);
    },
    [finalizeStream, idleCheckDelayMs, isOwnershipActive],
  );

  const scheduleReconnect = useCallback(
    (ownership: StreamOwnershipContext) => {
      if (!isOwnershipActive(ownership, 'schedule_reconnect')) return;
      if (reconnectTimerRef.current) return;

      if (reconnectAttemptsRef.current >= maxReconnectAttempts) {
        finalizeStream('error', {
          runId: ownership.runId,
          epoch: ownership.epoch,
          reason: 'max_reconnect_attempts_exceeded',
        });
        return;
      }

      reconnectAttemptsRef.current += 1;
      const delay = reconnectBaseDelayMs * reconnectAttemptsRef.current;
      updateStatus('connecting', {
        runId: ownership.runId,
        ownerRunId: ownership.runId,
        source: 'lifecycle',
        epoch: ownership.epoch,
      });

      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        if (!isOwnershipActive(ownership, 'reconnect_timer_fire')) return;
        startStreamingRef.current?.(ownership.runId);
      }, delay);
    },
    [
      finalizeStream,
      isOwnershipActive,
      updateStatus,
      maxReconnectAttempts,
      reconnectBaseDelayMs,
    ],
  );

  const captureTerminalPromotionChunk = useCallback(
    (target: 'text' | 'reasoning', chunk: string) => {
      const normalizedChunk = String(chunk || '');
      if (!normalizedChunk.trim()) return;

      const ref =
        target === 'text'
          ? terminalPromotionTextRef
          : terminalPromotionReasoningRef;
      const previous = ref.current;
      if (!previous) {
        ref.current = normalizedChunk;
        return;
      }

      if (previous === normalizedChunk || previous.includes(normalizedChunk)) {
        return;
      }

      if (
        normalizedChunk.length >= previous.length &&
        normalizedChunk.includes(previous)
      ) {
        ref.current = normalizedChunk;
        return;
      }

      ref.current = previous + normalizedChunk;
    },
    [],
  );

  // --- Stream Callback Handlers ---

  const handleStreamMessage = useCallback(
    (rawData: string, ownership: StreamOwnershipContext) => {
      if (!isOwnershipActive(ownership, 'on_message')) return;
      debugStreamLifecycleLog('on_message_callback', {
        runId: ownership.runId,
        epoch: ownership.epoch,
        currentRunId: currentRunIdRef.current,
        status: statusRef.current,
      });

      let processedData = rawData;
      if (processedData.startsWith('data: ')) {
        processedData = processedData.substring(6).trim();
      }
      if (!processedData) return;

      if (STREAM_TOOL_DEBUG_ENABLED) {
        try {
          const debugParsed = JSON.parse(processedData);
          const debugMetadata =
            typeof debugParsed.metadata === 'string'
              ? JSON.parse(debugParsed.metadata)
              : debugParsed.metadata;
          if (debugMetadata?.stream_status === 'tool_call_chunk') {
            debugToolStreamLog('📨 [useAgentStream] Incoming tool_call_chunk', {
              type: debugParsed.type,
              sequence: debugParsed.sequence,
              toolName: debugMetadata?.tool_calls?.[0]?.function?.name,
            });
          }
        } catch (_err) {
          // ignore debug parse failures
        }
      }

      // --- Early exit for non-JSON completion messages ---
      if (
        processedData ===
        '{"type": "status", "status": "completed", "message": "Agent run completed successfully"}'
      ) {
        finalizeStream('completed', {
          runId: ownership.runId,
          epoch: ownership.epoch,
          reason: 'non_json_completed_payload',
        });
        return;
      }
      if (
        processedData.includes('Run data not available for streaming') ||
        processedData.includes('Stream ended with status: completed')
      ) {
        finalizeStream('completed', {
          runId: ownership.runId,
          epoch: ownership.epoch,
          reason: 'non_json_stream_end_payload',
        });
        return;
      }

      // --- Check for terminal status / error messages first ---
      let earlyJsonData: unknown = null;
      try {
        earlyJsonData = JSON.parse(processedData);
        const candidate = earlyJsonData as {
          type?: string;
          status?: string;
          content?: unknown;
          message?: unknown;
        };
        const isRawShadowCloneLifecycleCandidate =
          typeof candidate.type === 'string' &&
          SHADOW_CLONE_EVENTS.has(candidate.type);
        const hasAssistantContent =
          candidate.type === 'assistant' &&
          candidate.content !== undefined &&
          candidate.content !== null &&
          String(candidate.content).trim().length > 0;
        if (
          candidate.status &&
          !hasAssistantContent &&
          !isRawShadowCloneLifecycleCandidate
        ) {
          const terminalStatus = normalizeTerminalStatus(String(candidate.status));
          if (terminalStatus) {
            if (terminalStatus === 'failed') {
              const errorMessage = candidate.message || 'Unknown error occurred';
              surfaceTerminalFailure(ownership, String(errorMessage));
            }
            finalizeStream(terminalStatus, {
              runId: ownership.runId,
              epoch: ownership.epoch,
              reason: 'json_terminal_status',
            });
            return;
          }
        }
      } catch (jsonError) {
        // Not JSON or could not parse as JSON, continue processing
      }

      // --- Process JSON messages ---
      const message = safeJsonParse(processedData, null) as StreamEventMessage | null;
      if (!message) {
        console.warn(
          '[useAgentStream] Failed to parse streamed message:',
          processedData,
        );
        return;
      }

      if (typeof message.event_index === 'number') {
        const messageType = String(
          (message as { type?: unknown }).type || '',
        );
        const metadataValue = (message as { metadata?: unknown }).metadata;
        const eventCursor =
          typeof message.event_cursor === 'string'
            ? message.event_cursor
            : typeof message.event_id === 'string'
              ? message.event_id
              : null;
        const isV2TerminalSummary =
          messageType === 'shadow_clone_v2_projection' &&
          typeof metadataValue === 'object' &&
          metadataValue !== null &&
          (metadataValue as Record<string, unknown>).terminal_summary === true;
        if (message.event_index <= lastEventIndexRef.current) {
          if (
            !isV2TerminalSummary &&
            (!eventCursor || eventCursor === lastEventCursorRef.current)
          ) {
            return;
          }
        }
        lastEventIndexRef.current = message.event_index;
        if (eventCursor) {
          lastEventCursorRef.current = eventCursor;
        }
        persistCursor(ownership.runId, message.event_index, eventCursor);
      }

      const topLevelLifecycleStatus =
        typeof message.type === 'string' &&
        SHADOW_CLONE_EVENTS.has(message.type)
          ? message.type
          : undefined;
      const parsedContent = topLevelLifecycleStatus && message.content === undefined
        ? (message as unknown as ParsedContent)
        : safeJsonParse<ParsedContent>(message.content, {});
      const parsedMetadata = safeJsonParse<ParsedMetadata>(
        message.metadata,
        {},
      );
      const streamStatus =
        typeof parsedMetadata.stream_status === 'string'
          ? parsedMetadata.stream_status
          : topLevelLifecycleStatus;
      const canonicalShadowCloneRouting = resolveShadowCloneCanonicalRouting({
        activityOwner:
          parsedMetadata.activity_owner ?? parsedContent.activity_owner,
        uiPhase: parsedMetadata.ui_phase ?? parsedContent.ui_phase,
        phaseReason:
          parsedMetadata.phase_reason ?? parsedContent.phase_reason,
      });
      const shadowCloneSemanticCategory = getShadowCloneSemanticCategory({
        messageType: message.type,
        streamStatus,
        parsedContent,
      });
      const isNonProgressMessage = message.non_progress === true;

      const messageStatus = (message as { status?: string }).status;
      if (message.type === 'status' && messageStatus) {
        const terminalStatus = normalizeTerminalStatus(messageStatus);
        if (terminalStatus) {
          finalizeStream(terminalStatus, {
            runId: ownership.runId,
            epoch: ownership.epoch,
            reason: 'message_terminal_status',
          });
          return;
        }
      }

      if (!isNonProgressMessage) {
        (window as any).lastStreamMessage = Date.now(); // Keep track of productive stream traffic
        scheduleIdleCheck(ownership);
      }

      // Update status to streaming only when the stream shows productive progress.
      if (!isNonProgressMessage && statusRef.current !== 'streaming') {
        updateStatus('streaming', {
          runId: ownership.runId,
          ownerRunId: ownership.runId,
          epoch: ownership.epoch,
          source: 'stream',
        });
      }

      const appendReasoningChunk = (rawReasoning: unknown): boolean => {
        if (rawReasoning === undefined || rawReasoning === null) {
          return false;
        }

        const reasoningChunk =
          typeof rawReasoning === 'string'
            ? rawReasoning
            : String(rawReasoning ?? '');

        if (!reasoningChunk.trim()) {
          return false;
        }
        captureTerminalPromotionChunk('reasoning', reasoningChunk);

        let changed = false;
        setReasoningContent((prev) => {
          const prevCombined = joinOrderedStreamChunks(prev);

          const normalizedPrev = normalizeReasoningForCompare(prevCombined);
          const normalizedChunk = normalizeReasoningForCompare(reasoningChunk);

          if (message.sequence !== undefined) {
            if (reasoningSequenceSetRef.current.has(message.sequence)) {
              return prev;
            }
            reasoningSequenceSetRef.current.add(message.sequence);
          }

          if (normalizedPrev) {
            if (normalizedChunk === normalizedPrev) {
              return prev;
            }
            if (
              normalizedChunk.length >= normalizedPrev.length &&
              normalizedChunk.includes(normalizedPrev)
            ) {
              changed = true;
              return [
                {
                  sequence: message.sequence,
                  content: reasoningChunk,
                },
              ];
            }
            if (normalizedPrev.includes(normalizedChunk)) {
              return prev;
            }
          }

          if (message.sequence !== undefined) {
            const existingIndex = prev.findIndex(
              (item) => item.sequence === message.sequence,
            );
            if (existingIndex !== -1) {
              const next = prev.slice();
              if (next[existingIndex]?.content === reasoningChunk) {
                return prev;
              }
              changed = true;
              next[existingIndex] = {
                sequence: message.sequence,
                content: reasoningChunk,
              };
              return next;
            }
          }

          changed = true;
          return prev.concat({
            sequence: message.sequence,
            content: reasoningChunk,
          });
        });

        if (changed) {
          callbacks.onReasoningChunk?.({ content: reasoningChunk });
        }
        return changed;
      };

      const shadowCloneStore = useShadowCloneStore.getState();
      const shadowClonePhase =
        canonicalShadowCloneRouting.hasCanonicalContract
          ? canonicalShadowCloneRouting.streamPhase
          : parsedMetadata.shadow_clone_phase === 'planning' ||
              parsedMetadata.shadow_clone_phase === 'execution' ||
              parsedMetadata.shadow_clone_phase === 'aggregate'
            ? parsedMetadata.shadow_clone_phase
            : null;
      const {
        shouldMirrorToShadowCloneMainTranscript,
        shouldSuppressLocalMainTailPresentation:
          shouldSuppressLocalMainTailPresentationState,
        shouldSuppressLocalPresentation:
          shouldSuppressLocalShadowClonePresentationState,
        shouldEmitFinalMessageToThread,
      } = deriveShadowCloneStreamOwnership({
        streamPhase: shadowClonePhase,
        storePhase: shadowCloneStore.phase,
        liveActivityScope: shadowCloneStore.liveActivity?.scope ?? null,
        currentRunId: shadowCloneStore.currentRunId,
        ownershipRunId: ownership.runId,
        subtaskCount: shadowCloneStore.subtasks.length,
        semanticCategory: shadowCloneSemanticCategory,
        hasCanonicalContract: canonicalShadowCloneRouting.hasCanonicalContract,
        canonicalActivityOwner: canonicalShadowCloneRouting.activityOwner,
        canonicalUiPhase: canonicalShadowCloneRouting.uiPhase,
      });
      const shadowCloneLiveActivity =
        canonicalShadowCloneRouting.hasCanonicalContract
          ? canonicalShadowCloneRouting.liveActivity
          : parsedMetadata.live_activity && typeof parsedMetadata.live_activity === 'object'
            ? parsedMetadata.live_activity
            : shadowClonePhase
              ? createShadowCloneFallbackLiveActivity(shadowClonePhase)
              : shouldSuppressLocalMainTailPresentationState
                ? shadowCloneStore.liveActivity ?? undefined
                : undefined;
      const isShadowCloneLifecycleEvent =
        Boolean(topLevelLifecycleStatus) ||
        (
          typeof streamStatus === 'string' &&
          SHADOW_CLONE_EVENTS.has(streamStatus)
        );
      const claudeSDKSubagentRoute = resolveClaudeSDKSubagentRoute({
        messageType: message.type,
        sequence: message.sequence,
        parsedContent,
        parsedMetadata,
      });
      const mirrorShadowCloneMainTranscriptMessage = (
        nextMessage: UnifiedMessage,
      ) => {
        if (!shouldMirrorToShadowCloneMainTranscript) {
          return;
        }
        shadowCloneStore.applyMainTranscriptMessage(
          nextMessage,
          ownership.runId,
          shadowCloneLiveActivity,
        );
      };

      if (
        !isShadowCloneLifecycleEvent &&
        !shouldMirrorToShadowCloneMainTranscript &&
        shadowCloneLiveActivity !== undefined
      ) {
        applyShadowCloneLiveActivityIfNeeded(
          shadowCloneStore,
          shadowCloneLiveActivity,
          ownership.runId,
        );
      }

      if (claudeSDKSubagentRoute) {
        shadowCloneStore.handleSSEEvent(
          claudeSDKSubagentRoute.streamStatus,
          claudeSDKSubagentRoute.content,
          claudeSDKSubagentRoute.liveActivity,
          ownership.runId,
        );

        // Feed live data to Subagent inspection view (SCV2 parity spec)
        const sdkSubtaskId = claudeSDKSubagentRoute.content.subtask_id;
        if (typeof sdkSubtaskId === 'string' && sdkSubtaskId) {
          shadowCloneStore.applySubagentStreamEvent(sdkSubtaskId, {
            status: 'streaming',
            textContent: extractSubagentStreamingText(parsedContent),
            reasoningContent: extractSubagentReasoningText(parsedContent),
          });
        }

        // Phase A: Track isWritingFile for subagent inspection blue bar
        // NOTE: Do NOT route subagent chunks to left panel (setTextContent/setReasoningContent).
        // Each agent's chat panel only displays its own output (FR-001).
        // Subagent text lives in inspectionStreamStates → read by page.tsx when inspecting.
        if (isSubagentChunkEvent('subagent_activity', streamStatus)) {
          const subagentText = extractSubagentStreamingText(parsedContent);
          if (subagentText && typeof sdkSubtaskId === 'string' && sdkSubtaskId) {
            // New text arrived — reset blue bar and idle timer
            shadowCloneStore.applySubagentStreamEvent(sdkSubtaskId, { isWritingFile: false });
            if (stopDetectionTimerRef.current) {
              clearTimeout(stopDetectionTimerRef.current);
              stopDetectionTimerRef.current = null;
            }
            // After 1s of no new text, show blue bar in inspection view
            stopDetectionTimerRef.current = setTimeout(() => {
              shadowCloneStore.applySubagentStreamEvent(sdkSubtaskId, { isWritingFile: true });
            }, 1000);
          }
        }

        // Phase D: Convert subagent tool-call complete events to assistant messages (R4 gray button fix)
        if (isSubagentCompleteWithToolCalls('subagent_activity', streamStatus, parsedContent)) {
          // Clear idle timer to prevent blue bar flash after subagent completion
          if (stopDetectionTimerRef.current) {
            clearTimeout(stopDetectionTimerRef.current);
            stopDetectionTimerRef.current = null;
          }
          const syntheticMsg = buildSubagentToolCallMessage({
            content: parsedContent,
            metadata: parsedMetadata as Record<string, unknown>,
            threadId,
            messageId: `scv2-subagent-tool-${sdkSubtaskId || ownership.runId}-${message.sequence ?? 0}`,
            sequence: message.sequence ?? 0,
            timestamp: message.created_at || new Date().toISOString(),
          });
          if (syntheticMsg) {
            emitMessage(syntheticMsg as unknown as UnifiedMessage);
          }
        }

        return;
      }

      if (
        isShadowCloneLifecycleEvent &&
        topLevelLifecycleStatus
      ) {
        shadowCloneStore.handleSSEEvent(
          topLevelLifecycleStatus,
          message as unknown as ParsedContent,
          shadowCloneLiveActivity,
          ownership.runId,
        );

        // Feed live data to Subagent inspection for native subagent_activity events
        const nativeSubtaskId = typeof parsedContent.subtask_id === 'string'
          ? parsedContent.subtask_id
          : typeof (parsedContent as Record<string, unknown>).subtask_id === 'string'
            ? (parsedContent as Record<string, unknown>).subtask_id as string
            : '';
        if (nativeSubtaskId && (topLevelLifecycleStatus as string) === 'subagent_activity') {
          const activityContent = typeof parsedContent.content === 'object' && parsedContent.content
            ? (parsedContent.content as Record<string, unknown>)
            : {};
          shadowCloneStore.applySubagentStreamEvent(nativeSubtaskId, {
            status: parsedContent.status === 'complete' ? 'completed' : 'streaming',
            textContent: typeof activityContent.content === 'string' ? activityContent.content : extractSubagentStreamingText(parsedContent),
            reasoningContent: typeof activityContent.reasoning_content === 'string' ? activityContent.reasoning_content : extractSubagentReasoningText(parsedContent),
          });
        }

        // Phase A: Track isWritingFile for subagent inspection blue bar
        // NOTE: Do NOT route subagent chunks to left panel (setTextContent/setReasoningContent).
        // Each agent's chat panel only displays its own output (FR-001).
        // Subagent text lives in inspectionStreamStates → read by page.tsx when inspecting.
        if (isSubagentChunkEvent(topLevelLifecycleStatus, streamStatus)) {
          const subagentText = extractSubagentStreamingText(parsedContent);
          if (subagentText && nativeSubtaskId) {
            // New text arrived — reset blue bar and idle timer
            shadowCloneStore.applySubagentStreamEvent(nativeSubtaskId, { isWritingFile: false });
            if (stopDetectionTimerRef.current) {
              clearTimeout(stopDetectionTimerRef.current);
              stopDetectionTimerRef.current = null;
            }
            // After 1s of no new text, show blue bar in inspection view
            stopDetectionTimerRef.current = setTimeout(() => {
              shadowCloneStore.applySubagentStreamEvent(nativeSubtaskId, { isWritingFile: true });
            }, 1000);
          }
        }

        // Phase D: Convert subagent tool-call complete events to assistant messages (R4 gray button fix)
        if (isSubagentCompleteWithToolCalls(topLevelLifecycleStatus, streamStatus, parsedContent)) {
          if (stopDetectionTimerRef.current) {
            clearTimeout(stopDetectionTimerRef.current);
            stopDetectionTimerRef.current = null;
          }
          const syntheticMsg = buildSubagentToolCallMessage({
            content: parsedContent,
            metadata: parsedMetadata as Record<string, unknown>,
            threadId,
            messageId: `scv2-native-tool-${nativeSubtaskId || ownership.runId}-${message.sequence ?? 0}`,
            sequence: message.sequence ?? 0,
            timestamp: message.created_at || new Date().toISOString(),
          });
          if (syntheticMsg) {
            emitMessage(syntheticMsg as unknown as UnifiedMessage);
          }
        }

        return;
      }

      switch (message.type) {
        case 'assistant':
          // 处理思考/推理内容 chunk
          if (streamStatus === 'reasoning_chunk') {
            mirrorShadowCloneMainTranscriptMessage(message);
            if (shouldSuppressLocalShadowClonePresentationState) {
              return;
            }
            const reasoningChanged = appendReasoningChunk(
              parsedContent.reasoning_content,
            );
            if (reasoningChanged) {
              return;
            }
          } else if (
            streamStatus === 'chunk' &&
            parsedContent.content
          ) {
            mirrorShadowCloneMainTranscriptMessage(message);
            if (shouldSuppressLocalShadowClonePresentationState) {
              return;
            }
            appendReasoningChunk(parsedContent.reasoning_content);
            const rawChunkContent = parsedContent.content;
            const chunkContent =
              typeof rawChunkContent === 'string'
                ? rawChunkContent
                : String(rawChunkContent ?? '');

            if (!chunkContent.trim()) {
              return;
            }
            captureTerminalPromotionChunk('text', chunkContent);

            // 🎯 V2: 标记工具完成后有文本输出
            if (lastToolCompletionTimeRef.current) {
              hasTextAfterToolRef.current = true;
            }

            // 清除旧的定时器
            if (stopDetectionTimerRef.current) {
              clearTimeout(stopDetectionTimerRef.current);
              stopDetectionTimerRef.current = null;
            }

            // 设置新的1秒定时器
            stopDetectionTimerRef.current = setTimeout(() => {
              // 🎯 V2: 智能冷却期检查
              const timeSinceToolCompletion = lastToolCompletionTimeRef.current
                ? Date.now() - lastToolCompletionTimeRef.current
                : Infinity;

              // 显示条件：
              // 1. 距离工具完成 > 5秒（不在冷却期） OR
              // 2. 工具完成后没有文本输出（连续工具调用场景）
              const shouldShow = timeSinceToolCompletion > 5000 || !hasTextAfterToolRef.current;

              if (!isWritingFile && shouldShow) {
                startToolIndicator();
                debugToolStreamLog('[FileWriting] Indicator ON (1s timeout)', {
                  cooldown: timeSinceToolCompletion < 5000,
                  hasTextAfterTool: hasTextAfterToolRef.current
                });
              }
            }, 1000);

            setTextContent((prev) => {
              const currentTotalLength = prev.reduce((sum, item) => sum + item.content.length, 0);
              const newContentLength = chunkContent.length;

              if (message.sequence !== undefined) {
                const existingIndex = prev.findIndex((item) => item.sequence === message.sequence);
                if (existingIndex !== -1) {
                  if (prev[existingIndex]?.content === chunkContent) {
                    return prev;
                  }
                  const next = prev.slice();
                  next[existingIndex] = {
                    sequence: message.sequence,
                    content: chunkContent,
                  };
                  return next;
                }
              }

              const previousCombined = joinOrderedStreamChunks(prev);

              if (
                previousCombined.length > 0 &&
                chunkContent.length >= previousCombined.length &&
                chunkContent.includes(previousCombined)
              ) {
                return [
                  {
                    sequence: message.sequence,
                    content: chunkContent,
                  },
                ];
              }

              if (
                message.sequence !== undefined &&
                message.sequence > 10 &&
                newContentLength >= currentTotalLength &&
                newContentLength > 20
              ) {
                debugToolStreamLog(
                  `🔄 [useAgentStream] Detected complete chunk (seq: ${message.sequence}, new: ${newContentLength}, prev: ${currentTotalLength}), replacing`,
                );
                return [
                  {
                    sequence: message.sequence,
                    content: chunkContent,
                  },
                ];
              }

              const lastChunk = prev.length > 0 ? prev[prev.length - 1] : null;
              if (
                lastChunk &&
                lastChunk.content === chunkContent &&
                (lastChunk.sequence === message.sequence || message.sequence === undefined)
              ) {
                return prev;
              }

              return prev.concat({
                sequence: message.sequence,
                content: chunkContent,
              });
            });
            callbacks.onAssistantChunk?.({ content: chunkContent });
          } else if (isFinalAssistantStreamStatus(streamStatus)) {
            // 清除停止检测定时器
            if (stopDetectionTimerRef.current) {
              clearTimeout(stopDetectionTimerRef.current);
              stopDetectionTimerRef.current = null;
            }

            // 收到完整消息，清空 streaming chunks
            debugToolStreamLog('✅ [useAgentStream] Complete message received');
            let finalMessage = message;
            let finalizedContent = parsedContent;
            const reasoningSnapshot = reasoningContentRef.current;
            const hasReasoning =
              Array.isArray(reasoningSnapshot) && reasoningSnapshot.length > 0;
            const streamedReasoningText = hasReasoning
              ? joinOrderedStreamChunks(reasoningSnapshot)
              : '';
            const reasoningText = shouldSuppressLocalShadowClonePresentationState
              ? shadowCloneStore.mainTranscriptState.streamingReasoningContent ||
                streamedReasoningText
              : streamedReasoningText;

            if (
              reasoningText.trim() &&
              parsedContent &&
              typeof parsedContent === 'object' &&
              !parsedContent.reasoning_content
            ) {
              const mergedContent = {
                ...parsedContent,
                reasoning_content: reasoningText,
              };
              finalMessage = {
                ...message,
                content: JSON.stringify(mergedContent),
              };
              finalizedContent = mergedContent;
            }
            const finalReasoningText =
              typeof finalizedContent?.reasoning_content === 'string'
                ? finalizedContent.reasoning_content
                : reasoningText;
            const finalMessageWillEmit = Boolean(
              shouldEmitFinalMessageToThread &&
                finalMessage.message_id &&
                !isEmptyAssistantMessage(finalMessage),
            );
            const finalMessageRenderableText =
              getRenderableAssistantText(finalMessage).trim();
            const finalMessageWillBeVisible = Boolean(
              finalMessageWillEmit &&
                finalMessageRenderableText &&
                finalMessageRenderableText !== '(empty message)',
            );
            if (shouldEmitFinalMessageToThread && !finalMessageWillBeVisible) {
              promoteFinalAssistantStreamingContent(finalMessage, ownership.runId);
            }
            if (!shouldSuppressLocalShadowClonePresentationState) {
              clearStreamingTextContent();
              setToolCall(null);
              stopToolIndicator(); // 确保指示器关闭
              if (finalReasoningText.trim()) {
                completionReasoningHoldRef.current = finalReasoningText;
                setReasoningContent([
                  {
                    sequence: message.sequence,
                    content: finalReasoningText,
                  },
                ]);
                scheduleCompletionReasoningClear(ownership.runId);
              } else {
                clearCompletionReasoningTimer();
                completionReasoningHoldRef.current = '';
                clearStreamingReasoningContent();
              }
            } else {
              // Per SCV2 parity spec: indicator state managed independently from
              // presentation routing. Ensure indicator stops even when local
              // presentation is suppressed (content routed to right panel).
              stopToolIndicator();
            }
            mirrorShadowCloneMainTranscriptMessage(finalMessage);
            if (finalMessageWillEmit) {
              if (finalMessageWillBeVisible) {
                visibleAssistantRunIdsRef.current.add(ownership.runId);
              }
              // Phase B: SCV2 main agent — trigger indicator cycle before emitting
              // to give visual feedback that the agent "worked" (R1 workaround)
              if (
                parsedMetadata.shadow_clone_mode &&
                !shouldSuppressLocalShadowClonePresentationState
              ) {
                startToolIndicator();
                setTimeout(() => { stopToolIndicator(); }, 800);
              }
              emitMessage(finalMessage);
            }
          } else if (streamStatus === 'tool_call_chunk') {
            // 清除停止检测定时器
            if (stopDetectionTimerRef.current) {
              clearTimeout(stopDetectionTimerRef.current);
              stopDetectionTimerRef.current = null;
            }

            // tool_call_chunk is pre-execution planning only.
            // Keep write indicator aligned with tool_started/tool_completed/tool_failed.
            debugToolStreamLog(
              '[FileWriting] tool_call_chunk received (indicator waits for tool_started)',
            );

            mirrorShadowCloneMainTranscriptMessage(message);
            if (shouldSuppressLocalShadowClonePresentationState) {
              return;
            }

            // 处理工具调用chunk
            const metadataToolCalls = getStreamToolCalls(parsedMetadata.tool_calls);
            const contentToolCalls = getStreamToolCalls(parsedContent.tool_calls);
            const toolCalls = [...metadataToolCalls, ...contentToolCalls];
            if (toolCalls.length > 0) {
              const selectedToolCall = selectRelevantStreamToolCall(toolCalls);
              if (!selectedToolCall) {
                clearStreamingTextContent();
                return;
              }

              const metadataToolCall =
                metadataToolCalls.find((toolCall) =>
                  streamToolCallsMatch(toolCall, selectedToolCall),
                ) || null;
              const contentToolCall =
                contentToolCalls.find((toolCall) =>
                  streamToolCallsMatch(toolCall, selectedToolCall),
                ) || null;

              const toolName =
                getStreamToolCallName(metadataToolCall) ||
                getStreamToolCallName(contentToolCall) ||
                '';
              const metadataArguments = getStreamToolCallArguments(metadataToolCall);
              const contentArguments = getStreamToolCallArguments(contentToolCall);
              const rawArguments = isWriteFileToolName(toolName)
                ? selectWriteFileArguments(contentArguments, metadataArguments)
                : (metadataArguments ?? contentArguments);
              let toolArguments = rawArguments;

              // Check for file writing tools - support multiple naming conventions
              const isFileWriteTool = isWriteFileToolName(toolName);
              
              if (isFileWriteTool) {
                const parsedArgs = normalizeWriteFileArgs(rawArguments) || {};
                const streamToolCallId =
                  getStreamToolCallId(metadataToolCall) ??
                  getStreamToolCallId(contentToolCall);

                const filePath = parsedArgs.file_path || undefined;
                const delta =
                  typeof parsedArgs.file_contents_delta === 'string'
                    ? parsedArgs.file_contents_delta
                    : null;
                const fullContent =
                  typeof parsedArgs.file_contents === 'string'
                    ? parsedArgs.file_contents
                    : null;
                const deltaIndex =
                  typeof parsedArgs.delta_index === 'number'
                    ? parsedArgs.delta_index
                    : message.sequence;
                const pathBufferKey = filePath
                  ? `write_file:path:${filePath}`
                  : null;
                const toolIdBufferKey = streamToolCallId
                  ? `write_file:id:${streamToolCallId}`
                  : null;
                const runBufferKey = parsedMetadata.thread_run_id
                  ? `write_file:run:${parsedMetadata.thread_run_id}`
                  : null;
                const primaryBufferKeys = [
                  pathBufferKey,
                  toolIdBufferKey,
                ].filter((value): value is string => Boolean(value));
                const lookupBufferKeys = primaryBufferKeys.length
                  ? [
                      ...primaryBufferKeys,
                      ...(runBufferKey ? [runBufferKey] : []),
                    ]
                  : (runBufferKey ? [runBufferKey] : []);
                const debugBufferKey =
                  primaryBufferKeys[0] ||
                  runBufferKey ||
                  'write_file:unkeyed';

                debugToolStreamLog('📝 [useAgentStream] write_file chunk details:', {
                  filePath,
                  hasDelta: delta !== null,
                  deltaLength: delta?.length,
                  hasFullContent: fullContent !== null,
                  fullContentLength: fullContent?.length,
                  deltaIndex,
                  bufferKey: debugBufferKey,
                  lookupBufferKeys,
                  rawArgumentsType: typeof rawArguments,
                  rawArgumentsPreview:
                    typeof rawArguments === 'string'
                      ? rawArguments.substring(0, 100)
                      : JSON.stringify(rawArguments).substring(0, 100),
                });

                const shouldReset = delta !== null && deltaIndex === 0;
                let buffer = lookupBufferKeys
                  .map((key) => fileStreamBuffersRef.current.get(key))
                  .find((candidate) => candidate);
                if (!buffer || shouldReset) {
                  buffer = { content: '', filePath };
                }

                const isDuplicateDelta =
                  delta !== null &&
                  typeof deltaIndex === 'number' &&
                  buffer.lastDeltaIndex === deltaIndex;

                if (!isDuplicateDelta) {
                  if (fullContent !== null) {
                    buffer.content =
                      shouldReset || fullContent.length >= buffer.content.length
                        ? fullContent
                        : buffer.content;
                  } else if (delta !== null) {
                    buffer.content += delta;
                  }
                }
                if (filePath && !buffer.filePath) {
                  buffer.filePath = filePath;
                }
                if (typeof deltaIndex === 'number') {
                  buffer.lastDeltaIndex = deltaIndex;
                }
                if (runBufferKey && primaryBufferKeys.length > 0) {
                  fileStreamBuffersRef.current.delete(runBufferKey);
                }
                const keysToStore = primaryBufferKeys.length
                  ? primaryBufferKeys
                  : (runBufferKey ? [runBufferKey] : [debugBufferKey]);
                for (const key of keysToStore) {
                  fileStreamBuffersRef.current.set(key, buffer);
                }

                const mergedArgs: Record<string, any> = {
                  file_path: buffer.filePath || filePath,
                  file_contents: buffer.content,
                };
                if (delta !== null) mergedArgs.file_contents_delta = delta;
                if (typeof parsedArgs.delta_index === 'number') mergedArgs.delta_index = parsedArgs.delta_index;

                debugToolStreamLog('📝 [useAgentStream] write_file buffer updated:', {
                  bufferContentLength: buffer.content?.length,
                  bufferFilePath: buffer.filePath,
                  mergedArgsKeys: Object.keys(mergedArgs),
                  file_contents_length: mergedArgs.file_contents?.length,
                });

                toolArguments =
                  stringifyNormalizedWriteFileArgs(mergedArgs) ||
                  JSON.stringify(mergedArgs);
                debugToolStreamLog(
                  '📝 [useAgentStream] write_file aggregated arguments length:',
                  typeof toolArguments === 'string'
                    ? toolArguments.length
                    : undefined,
                );
              }

              if (toolArguments && typeof toolArguments !== 'string') {
                try {
                  toolArguments = JSON.stringify(toolArguments);
                } catch (err) {
                  toolArguments = String(toolArguments);
                }
              }

              const toolCallPayload = {
                role: 'assistant' as const,
                status_type: 'tool_call_chunk',
                name: toolName,
                arguments: toolArguments, // JSON string with aggregated content for streaming
                xml_tag_name: toolName,
                tool_index:
                  getStreamToolCallIndex(metadataToolCall) ??
                  getStreamToolCallIndex(contentToolCall),
                id:
                  getStreamToolCallId(metadataToolCall) ??
                  getStreamToolCallId(contentToolCall),
              };
              
              if (isFileWriteTool) {
                enqueueWriteToolCall(toolCallPayload as ParsedContent, message);
              } else {
                resetWriteToolCallThrottle();
                setToolCall(toolCallPayload);
                callbacks.onToolCallChunk?.(message);
              }
            }
            // 清空streaming text以避免重复显示
            clearStreamingTextContent();
          } else if (!streamStatus) {
            // Handle non-chunked assistant messages if needed
            if (!isEmptyAssistantMessage(message)) {
              mirrorShadowCloneMainTranscriptMessage(message);
              if (shouldSuppressLocalShadowClonePresentationState) {
                return;
              }
              if (!shouldSuppressLocalMainTailPresentationState) {
                callbacks.onAssistantStart?.();
              }
              clearStreamingTextContent();
              setToolCall(null);
              stopToolIndicator();
              if (message.message_id) emitMessage(message);
            }
          }
          break;
        case 'tool':
          // 清除停止检测定时器
          if (stopDetectionTimerRef.current) {
            clearTimeout(stopDetectionTimerRef.current);
            stopDetectionTimerRef.current = null;
          }

          resetWriteToolCallThrottle();
          setToolCall(null); // Clear any streaming tool call
          stopToolIndicator(); // Reset file writing indicator

          // 🎯 V2：记录工具完成时间 + 重置文本标志
          lastToolCompletionTimeRef.current = Date.now();
          hasTextAfterToolRef.current = false; // 重置：等待后续是否有文本
          debugToolStreamLog('[FileWriting] Indicator OFF, cooldown tracking started');

          mirrorShadowCloneMainTranscriptMessage(message);
          if (shouldSuppressLocalShadowClonePresentationState) {
            break;
          }

          if (message.message_id) emitMessage(message);
          break;
        case 'status':
          switch (parsedContent.status_type) {
            case 'tool_call_chunk':
              // 🔧 检测到工具调用，立即清空 streaming text
              // 避免显示包含重复内容的 streaming chunks
              debugToolStreamLog(
                '🔄 [useAgentStream] Tool call detected, clearing streaming text immediately',
              );
              if (!shouldSuppressLocalShadowClonePresentationState) {
                clearStreamingTextContent();
              }
              break;
            case 'tool_started':
              if (shouldSuppressLocalShadowClonePresentationState) {
                break;
              }
              resetWriteToolCallThrottle();
              if (!isWritingFile) {
                startToolIndicator();
              }
              setToolCall({
                role: 'assistant' as const,
                status_type: 'tool_started',
                name: parsedContent.function_name,
                arguments: parsedContent.arguments,
                xml_tag_name: parsedContent.xml_tag_name,
                tool_index: parsedContent.tool_index,
              });
              break;
            case 'tool_completed':
            case 'tool_failed':
            case 'tool_error': {
              // Per SCV2 parity spec: indicator state managed independently from
              // presentation routing. Always stop the indicator and record
              // timing even when local presentation is suppressed.
              stopToolIndicator();
              fileStreamBuffersRef.current.clear();
              lastToolCompletionTimeRef.current = Date.now();
              hasTextAfterToolRef.current = false;

              if (shouldSuppressLocalShadowClonePresentationState) {
                break;
              }
              resetWriteToolCallThrottle();
              const shouldClearActiveTool =
                parsedContent.tool_index === undefined ||
                toolCall?.tool_index === parsedContent.tool_index ||
                (
                  typeof parsedContent.function_name === 'string' &&
                  parsedContent.function_name.length > 0 &&
                  toolCall?.name === parsedContent.function_name
                );
              if (
                shouldClearActiveTool
              ) {
                setToolCall(null);
              }
              break;
            }
            case 'thread_run_end':
              if (shouldSuppressLocalShadowClonePresentationState) {
                break;
              }
              resetWriteToolCallThrottle();
              setToolCall(null);
              stopToolIndicator();
              fileStreamBuffersRef.current.clear();
              break;
            case 'finish':
              // Optional: Handle finish reasons like 'xml_tool_limit_reached'
              // Don't finalize here, wait for thread_run_end or completion message
              if (shouldSuppressLocalShadowClonePresentationState) {
                break;
              }
              resetWriteToolCallThrottle();
              setToolCall(null);
              stopToolIndicator();
              break;
            case 'error':
              setError(parsedContent.message || 'Agent run failed');
              finalizeStream('error', {
                runId: ownership.runId,
                epoch: ownership.epoch,
                reason: 'status_message_error',
              });
              break;
            // Ignore thread_run_start, assistant_response_start etc. for now
            default:
              // console.debug('[useAgentStream] Received unhandled status type:', parsedContent.status_type);
              break;
          }
          break;
        case 'user':
        case 'system':
          // Handle other message types if necessary, e.g., if backend sends historical context
          if (message.message_id) emitMessage(message);
          break;
        default:
          console.warn(
            '[useAgentStream] Unhandled message type:',
            message.type,
          );
      }
    },
    [
      toolCall,
      applyShadowCloneLiveActivityIfNeeded,
      callbacks,
      captureTerminalPromotionChunk,
      clearCompletionReasoningTimer,
      clearStreamingReasoningContent,
      clearStreamingTextContent,
      emitMessage,
      finalizeStream,
      isOwnershipActive,
      promoteFinalAssistantStreamingContent,
      scheduleCompletionReasoningClear,
      startToolIndicator,
      stopToolIndicator,
      updateStatus,
      scheduleIdleCheck,
      enqueueWriteToolCall,
      persistCursor,
      resetWriteToolCallThrottle,
      isWritingFile,
      surfaceTerminalFailure,
    ],
  );

  const handleStreamError = useCallback(
    (err: Error | string | Event, ownership: StreamOwnershipContext) => {
      if (!isOwnershipActive(ownership, 'on_error')) return;
      debugStreamLifecycleLog('on_error_callback', {
        runId: ownership.runId,
        epoch: ownership.epoch,
        currentRunId: currentRunIdRef.current,
        status: statusRef.current,
      });

      // Extract error message
      let errorMessage = 'Unknown streaming error';
      if (typeof err === 'string') {
        errorMessage = err;
      } else if (err instanceof Error) {
        errorMessage = err.message;
      } else if (err instanceof Event && err.type === 'error') {
        // Standard EventSource errors don't have much detail, might need status check
        errorMessage = 'Stream connection error';
      }

      const runId = ownership.runId;

      if (isBenignAgentNotRunningError(errorMessage)) {
        console.info(
          '[useAgentStream] Suppressing benign not-running stream error:',
          errorMessage,
        );
        finalizeStream('agent_not_running', {
          runId,
          epoch: ownership.epoch,
          reason: 'benign_not_running_error',
        });
        return;
      }

      if (isBenignPostTerminalStreamError(errorMessage, statusRef.current)) {
        console.info(
          '[useAgentStream] Suppressing post-terminal stream connection error:',
          errorMessage,
        );
        return;
      }

      if (isLikelyStreamConnectionError(errorMessage) && runId) {
        // EventSource emits a generic "connection error" before we can determine
        // whether the run is actually terminal; defer user-facing error handling
        // to handleStreamClose() where we check backend run status.
        console.warn(
          '[useAgentStream] Deferring transient stream connection error handling:',
          errorMessage,
        );
        setError(errorMessage);
        return;
      }

      console.error('[useAgentStream] Streaming error:', errorMessage, err);
      setError(errorMessage);
      callbacks.onError?.(errorMessage);

    },
    [callbacks, finalizeStream, isOwnershipActive],
  );

  const handleStreamClose = useCallback(
    (ownership: StreamOwnershipContext) => {
      if (!isOwnershipActive(ownership, 'on_close')) return;
      debugStreamLifecycleLog('on_close_callback', {
        runId: ownership.runId,
        epoch: ownership.epoch,
        currentRunId: currentRunIdRef.current,
        status: statusRef.current,
      });

      const runId = ownership.runId;
      if (!runId) {
        console.warn('[useAgentStream] Stream closed but no active agentRunId.');
        // If status was streaming, something went wrong, finalize as error
        if (
          statusRef.current === 'streaming' ||
          statusRef.current === 'connecting'
        ) {
          finalizeStream('error', {
            runId: null,
            epoch: ownership.epoch,
            reason: 'close_without_runid_in_streaming_state',
          });
        } else if (
          statusRef.current !== 'idle' &&
          statusRef.current !== 'completed' &&
          statusRef.current !== 'stopped' &&
          statusRef.current !== 'agent_not_running'
        ) {
          // If in some other state, just go back to idle if no runId
          finalizeStream('idle', {
            runId: null,
            epoch: ownership.epoch,
            reason: 'close_without_runid_non_terminal_state',
          });
        }
        return;
      }

      // Immediately check the agent status when the stream closes unexpectedly
      // This covers cases where the agent finished but the final message wasn't received,
      // or if the agent errored out on the backend.
      if (streamCleanupRef.current) {
        streamCleanupRef.current();
        streamCleanupRef.current = null;
      }
      getAgentStatus(runId)
        .then((agentStatus) => {
          if (!isOwnershipActive(ownership, 'on_close_status_resolved')) return;

          if (agentStatus.status === 'running') {
            scheduleReconnect(ownership);
            toast.warning('Stream disconnected. Reconnecting...');
          } else {
            // Map backend terminal status to hook terminal status
            const finalStatus = mapAgentStatus(agentStatus.status);
            finalizeStream(finalStatus, {
              runId,
              epoch: ownership.epoch,
              reason: 'close_status_terminal',
            });
          }
        })
        .catch((err) => {
          if (!isOwnershipActive(ownership, 'on_close_status_error')) return;

          const errorMessage = err instanceof Error ? err.message : String(err);
          console.error(
            `[useAgentStream] Error checking agent status for ${runId} after stream close: ${errorMessage}`,
          );

          const isRunUnavailableError =
            isBenignAgentNotRunningError(errorMessage) ||
            errorMessage.includes('not found') ||
            errorMessage.includes('404') ||
            errorMessage.includes('does not exist');

          if (isRunUnavailableError) {
            // Revert to agent_not_running for this specific case
            finalizeStream('agent_not_running', {
              runId,
              epoch: ownership.epoch,
              reason: 'close_status_not_found',
            });
          } else {
            // For other errors checking status, finalize with generic error
            finalizeStream('error', {
              runId,
              epoch: ownership.epoch,
              reason: 'close_status_error',
            });
          }
        });
    },
    [finalizeStream, isOwnershipActive, scheduleReconnect],
  );

  // --- Effect to manage the stream lifecycle ---
  useEffect(() => {
    isMountedRef.current = true;

    // Cleanup function - be more conservative about stream cleanup
    return () => {
      invalidateStreamOwnership('hook_unmount');
      isMountedRef.current = false;
      clearCompletionReasoningTimer();
      completionReasoningHoldRef.current = '';
      resetWriteToolCallThrottle();
      
      // Don't automatically cleanup streams on navigation
      // Only set mounted flag to false to prevent new operations
      // Streams will be cleaned up when they naturally complete or on explicit stop

      if (pendingStartRetryRef.current?.timer) {
        clearTimeout(pendingStartRetryRef.current.timer);
        pendingStartRetryRef.current = null;
      }
    };
  }, [clearCompletionReasoningTimer, invalidateStreamOwnership, resetWriteToolCallThrottle]); // Mount/unmount cleanup

  // --- Public Functions ---

  const startStreaming = useCallback(
    async (runId: string) => {
      if (!isMountedRef.current) return;
      if (isStartingStreamRef.current) {
        console.warn('[useAgentStream] startStreaming blocked by guard, queueing retry for %s', runId);
        if (pendingStartRetryRef.current) {
          pendingStartRetryRef.current.runId = runId;
          if (pendingStartRetryRef.current.timer) {
            clearTimeout(pendingStartRetryRef.current.timer);
          }
        } else {
          pendingStartRetryRef.current = { runId, attempts: 0, timer: null };
        }
        const entry = pendingStartRetryRef.current;
        entry.timer = setTimeout(() => {
          if (!isMountedRef.current) return;
          entry.attempts += 1;
          const retryRunId = entry.runId;
          if (entry.attempts > 5) {
            console.error('[useAgentStream] Force-resetting stuck isStartingStreamRef guard');
            isStartingStreamRef.current = false;
            guardGenerationRef.current += 1;
          }
          pendingStartRetryRef.current = null;
          startStreamingRef.current?.(retryRunId);
        }, Math.min(100 * entry.attempts, 500));
        return;
      }
      isStartingStreamRef.current = true;
      const guardGen = guardGenerationRef.current;
      guardGenerationRef.current = guardGen + 1;
      const myGuardGen = guardGenerationRef.current;
      let ownership: StreamOwnershipContext | null = null;
      try {
        if (
          currentRunIdRef.current === runId &&
          streamCleanupRef.current &&
          (statusRef.current === 'connecting' || statusRef.current === 'streaming')
        ) {
          return;
        }

        const isReconnect = currentRunIdRef.current === runId;
        const nextEpoch = streamEpochRef.current + 1;
        streamEpochRef.current = nextEpoch;
        activeStreamEpochRef.current = nextEpoch;
        const streamOwnership: StreamOwnershipContext = {
          runId,
          epoch: nextEpoch,
        };
        ownership = streamOwnership;
        debugStreamLifecycleLog('start_streaming', {
          runId,
          epoch: nextEpoch,
          isReconnect,
          statusBefore: statusRef.current,
          currentRunId: currentRunIdRef.current,
        });

        // Clean up any previous stream
        if (streamCleanupRef.current) {
          streamCleanupRef.current();
          streamCleanupRef.current = null;
        }
        if (reconnectTimerRef.current) {
          clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = null;
        }

        if (!isReconnect) {
          // Reset state for a new run only. Reconnect keeps existing progress.
          clearCompletionReasoningTimer();
          completionReasoningHoldRef.current = '';
          clearStreamingTextContent();
          clearStreamingReasoningContent();
          setToolCall(null);
          resetWriteToolCallThrottle();
          resetToolIndicator();
          reasoningSequenceSetRef.current.clear();
          fileStreamBuffersRef.current.clear();
          lastEventIndexRef.current = -1;
          lastEventCursorRef.current = null;
          lastPersistedEventIndexRef.current = -1;
          lastPersistedEventCursorRef.current = null;
          lastCursorPersistAtRef.current = 0;
          lastShadowCloneLiveActivitySignatureRef.current = null;
          lastToolCompletionTimeRef.current = null; // V2
          hasTextAfterToolRef.current = false; // V2
        }

        setError(null);
        surfacedTerminalErrorSignatureRef.current = null;
        if (stopDetectionTimerRef.current) {
          clearTimeout(stopDetectionTimerRef.current);
          stopDetectionTimerRef.current = null;
        }
        setAgentRunId(runId);
        currentRunIdRef.current = runId; // Set the ref immediately
        updateStatus('connecting', {
          runId,
          ownerRunId: runId,
          source: 'lifecycle',
          epoch: streamOwnership.epoch,
        });

        // Connect to SSE regardless of agent status - the SSE endpoint will handle
        // completed agents by sending initial messages from Redis then closing.
        // This ensures we get streaming messages even for fast-completing agents.
        // Previously, we checked status here which caused streaming to fail for
        // agents that completed quickly (before the frontend could connect).
        debugStreamLifecycleLog('connecting_to_sse', {
          runId,
          reason: 'skip_preconnection_status_check',
        });

        // Proceed to create the stream
        let reconnectFromIndex =
          lastEventIndexRef.current >= 0 ? lastEventIndexRef.current + 1 : undefined;
        let reconnectFromEventId =
          lastEventCursorRef.current && lastEventCursorRef.current.trim()
            ? lastEventCursorRef.current
            : undefined;
        if (reconnectFromIndex === undefined) {
          const persistedCursor = readPersistedStreamCursor(threadId, runId);
          if (persistedCursor !== null) {
            lastEventIndexRef.current = persistedCursor.eventIndex;
            lastEventCursorRef.current = persistedCursor.eventCursor ?? null;
            lastPersistedEventIndexRef.current = persistedCursor.eventIndex;
            lastPersistedEventCursorRef.current =
              persistedCursor.eventCursor ?? null;
            lastCursorPersistAtRef.current = Date.now();
            reconnectFromIndex = persistedCursor.eventIndex + 1;
            reconnectFromEventId = persistedCursor.eventCursor;
            debugStreamLifecycleLog('resume_from_persisted_cursor', {
              runId,
              persistedEventIndex: persistedCursor.eventIndex,
              persistedEventCursor: persistedCursor.eventCursor,
              reconnectFromIndex,
              reconnectFromEventId,
            });
          }
        }
        const cleanup = streamAgent(
          runId,
          {
            onMessage: (data) => {
              // Ignore messages if threadId changed while the EventSource stayed open
              if (threadIdRef.current !== threadId) return;
              handleStreamMessage(data, streamOwnership);
            },
            onError: (err) => {
              if (threadIdRef.current !== threadId) return;
              handleStreamError(err, streamOwnership);
            },
            onClose: () => {
              if (threadIdRef.current !== threadId) return;
              handleStreamClose(streamOwnership);
            },
          },
          {
            fromIndex: reconnectFromIndex,
            fromEventId: reconnectFromEventId,
          },
        );
        streamCleanupRef.current = cleanup;
        reconnectAttemptsRef.current = 0;
        scheduleIdleCheck(streamOwnership);
        // If no message arrives within 5s, probe agent liveness and force-reconnect
        // if the agent is running but we see no traffic.
        setTimeout(async () => {
          if (!isOwnershipActive(streamOwnership, '5s_liveness_timer_fired')) return;
          if (statusRef.current === 'streaming') return;
          if (lastEventIndexRef.current >= 0) return;
          try {
            const latest = await getAgentStatus(runId);
            if (!isOwnershipActive(streamOwnership, '5s_liveness_status_resolved')) return;
            if (latest.status !== 'running') {
              finalizeStream(mapAgentStatus(latest.status) || 'agent_not_running', {
                runId,
                epoch: streamOwnership.epoch,
                reason: '5s_liveness_non_running',
              });
            } else {
              console.warn('[useAgentStream] Agent running but no messages in 5s, forcing reconnect');
              if (streamCleanupRef.current) {
                streamCleanupRef.current();
                streamCleanupRef.current = null;
              }
              scheduleReconnect(streamOwnership);
            }
          } catch {
            if (isOwnershipActive(streamOwnership, '5s_liveness_status_error')) {
              scheduleReconnect(streamOwnership);
            }
          }
        }, 5000);
        // Status will be updated to 'streaming' by the first message received in handleStreamMessage
        // If for some reason no message arrives shortly, verify liveness again to avoid zombie state
        setTimeout(async () => {
          if (!isOwnershipActive(streamOwnership, 'initial_liveness_check_timer_fired')) return;
          if (statusRef.current === 'streaming') return; // Already streaming
          try {
            const latest = await getAgentStatus(runId);
            if (!isOwnershipActive(streamOwnership, 'initial_liveness_check_status_resolved')) return;
            if (latest.status !== 'running') {
              finalizeStream(mapAgentStatus(latest.status) || 'agent_not_running', {
                runId,
                epoch: streamOwnership.epoch,
                reason: 'initial_liveness_check_non_running_status',
              });
            }
          } catch {
            // ignore
          }
        }, 1500);
      } catch (err) {
        if (!isMountedRef.current) return; // Check mount status after async call

        const errorMessage = err instanceof Error ? err.message : String(err);
        console.error(
          `[useAgentStream] Error initiating stream for ${runId}: ${errorMessage}`,
        );
        setError(errorMessage);

        const isRunUnavailableError =
          isBenignAgentNotRunningError(errorMessage) ||
          errorMessage.includes('not found') ||
          errorMessage.includes('404') ||
          errorMessage.includes('does not exist');

        finalizeStream(isRunUnavailableError ? 'agent_not_running' : 'error', {
          runId,
          epoch: ownership?.epoch,
          reason: 'start_streaming_exception',
        });
      } finally {
        if (guardGenerationRef.current === myGuardGen) {
          isStartingStreamRef.current = false;
        }
      }
    },
    [
      updateStatus,
      finalizeStream,
      handleStreamMessage,
      handleStreamError,
      handleStreamClose,
      scheduleIdleCheck,
      isOwnershipActive,
      clearCompletionReasoningTimer,
      clearStreamingReasoningContent,
      clearStreamingTextContent,
      resetWriteToolCallThrottle,
      resetToolIndicator,
      threadId,
    ],
  ); // Add dependencies

  useEffect(() => {
    startStreamingRef.current = startStreaming;
  }, [startStreaming]);

  const stopStreaming = useCallback(async () => {
    if (!isMountedRef.current || !agentRunId) return;

    const runIdToStop = agentRunId;

    // Immediately update status and clean up stream
    finalizeStream('stopped', {
      runId: runIdToStop,
      reason: 'manual_stop',
    });

    try {
      await stopAgent(runIdToStop);
      toast.success('Agent stopped.');
      // finalizeStream already called getAgentStatus implicitly if needed
    } catch (err) {
      // Don't revert status here, as the user intended to stop. Just log error.
      const errorMessage = err instanceof Error ? err.message : String(err);
      console.error(
        `[useAgentStream] Error sending stop request for ${runIdToStop}: ${errorMessage}`,
      );
      toast.error(`Failed to stop agent: ${errorMessage}`);
    }
  }, [agentRunId, finalizeStream]); // Add dependencies

  return {
    status,
    textContent: orderedTextContent,
    reasoningContent: orderedReasoningContent, // 思考/推理内容
    toolCall,
    error,
    agentRunId,
    isWritingFile,
    startStreaming,
    stopStreaming,
  };
}
