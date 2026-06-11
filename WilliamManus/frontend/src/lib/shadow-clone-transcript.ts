import { safeJsonParse } from '@/components/thread/utils';
import type { ParsedContent, ParsedMetadata, UnifiedMessage } from '@/components/thread/types';

import type { ShadowCloneActivityEnvelope } from '@/lib/shadow-clone-panel';

export interface ShadowCloneTranscriptState {
  messages: UnifiedMessage[];
  streamingTextContent: string;
  streamingReasoningContent: string;
  streamingToolCall: ParsedContent | null;
  lastSequence: number;
  latestLabel: string | null;
}

export const createEmptyShadowCloneTranscriptState = (): ShadowCloneTranscriptState => ({
  messages: [],
  streamingTextContent: '',
  streamingReasoningContent: '',
  streamingToolCall: null,
  lastSequence: -1,
  latestLabel: null,
});

export const hasShadowCloneTranscriptLiveActivity = (
  transcriptState: ShadowCloneTranscriptState,
): boolean =>
  Boolean(
    transcriptState.streamingTextContent ||
      transcriptState.streamingReasoningContent ||
      transcriptState.streamingToolCall,
  );

const stringify = (value: unknown): string => {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const buildToolCallPayload = (activity: ShadowCloneActivityEnvelope, metadata: ParsedMetadata, content: ParsedContent): ParsedContent | null => {
  const candidates = Array.isArray(metadata.tool_calls) && metadata.tool_calls.length > 0
    ? metadata.tool_calls
    : Array.isArray(content.tool_calls)
      ? content.tool_calls
      : [];
  if (!candidates.length) return null;

  const toolCall = candidates[0] || {};
  const toolName = String(toolCall?.function?.name || content.name || 'Using Tool');
  let toolArguments = toolCall?.function?.arguments;
  if (toolArguments && typeof toolArguments !== 'string') {
    toolArguments = stringify(toolArguments);
  }

  return {
    role: 'assistant',
    status_type: 'tool_call_chunk',
    name: toolName,
    arguments: typeof toolArguments === 'string' ? toolArguments : '',
    xml_tag_name: toolName,
    tool_index: toolCall?.index,
    id: typeof toolCall?.id === 'string' ? toolCall.id : undefined,
  };
};

const sameStreamingToolCall = (
  left: ParsedContent | null,
  right: ParsedContent | null,
): boolean => {
  if (left === right) return true;
  if (!left || !right) return false;

  return (
    left.role === right.role &&
    left.status_type === right.status_type &&
    left.name === right.name &&
    left.arguments === right.arguments &&
    left.xml_tag_name === right.xml_tag_name &&
    left.tool_index === right.tool_index &&
    left.id === right.id
  );
};

const withAdvancedLastSequence = (
  transcriptState: ShadowCloneTranscriptState,
  sequence: number,
): ShadowCloneTranscriptState =>
  Number.isFinite(sequence) && sequence > transcriptState.lastSequence
    ? {
        ...transcriptState,
        lastSequence: sequence,
    }
    : transcriptState;

const nextLastSequence = (
  transcriptState: ShadowCloneTranscriptState,
  sequence: number,
): number =>
  Number.isFinite(sequence)
    ? Math.max(transcriptState.lastSequence, sequence)
    : transcriptState.lastSequence;

const mergeStreamingTextChunk = (
  previousText: string,
  incomingText: string,
): string => {
  if (!incomingText) {
    return previousText;
  }
  if (!previousText || incomingText === previousText) {
    return incomingText;
  }
  if (incomingText.startsWith(previousText)) {
    return incomingText;
  }
  return `${previousText}${incomingText}`;
};

const isStaleActivityForCurrentTranscriptChannel = (
  transcriptState: ShadowCloneTranscriptState,
  sequence: number,
  streamStatus: string,
  messageType: UnifiedMessage['type'],
  content: ParsedContent,
): boolean => {
  if (!Number.isFinite(sequence) || sequence > transcriptState.lastSequence) {
    return false;
  }

  if (streamStatus === 'reasoning_chunk') {
    return true;
  }
  if (streamStatus === 'chunk') {
    const incomingText = typeof content.content === 'string' ? content.content : '';
    return !(
      incomingText &&
      (
        !transcriptState.streamingTextContent ||
        incomingText.startsWith(transcriptState.streamingTextContent)
      )
    );
  }
  if (streamStatus === 'tool_call_chunk') {
    return true;
  }
  if (messageType === 'tool') {
    return true;
  }
  if (messageType === 'assistant' && streamStatus === 'complete') {
    return transcriptState.latestLabel === '已生成回复' ||
      transcriptState.latestLabel === '已生成工具调用';
  }

  return false;
};

const buildMessageId = (subtaskId: string, messageType: UnifiedMessage['type'], sequence: number, content: ParsedContent): string => {
  if (messageType === 'tool' && typeof content.tool_call_id === 'string' && content.tool_call_id) {
    return `shadow-clone:${subtaskId}:tool:${content.tool_call_id}`;
  }
  if (messageType === 'assistant' && Array.isArray(content.tool_calls) && content.tool_calls[0]?.id) {
    return `shadow-clone:${subtaskId}:assistant-tool:${content.tool_calls[0].id}`;
  }
  return `shadow-clone:${subtaskId}:${messageType}:${sequence}`;
};

const upsertMessage = (messages: UnifiedMessage[], nextMessage: UnifiedMessage): UnifiedMessage[] => {
  const existingIndex = messages.findIndex((message) => message.message_id === nextMessage.message_id);
  if (existingIndex === -1) {
    if (messages.length === 0) {
      return [nextMessage];
    }

    const lastMessage = messages[messages.length - 1];
    if ((lastMessage?.sequence || 0) <= (nextMessage.sequence || 0)) {
      return messages.concat(nextMessage);
    }

    return messages
      .concat(nextMessage)
      .sort((left, right) => (left.sequence || 0) - (right.sequence || 0));
  }

  const currentMessage = messages[existingIndex];
  if (
    currentMessage.sequence === nextMessage.sequence &&
    currentMessage.thread_id === nextMessage.thread_id &&
    currentMessage.type === nextMessage.type &&
    currentMessage.role === nextMessage.role &&
    currentMessage.is_llm_message === nextMessage.is_llm_message &&
    currentMessage.content === nextMessage.content &&
    currentMessage.metadata === nextMessage.metadata &&
    currentMessage.created_at === nextMessage.created_at &&
    currentMessage.updated_at === nextMessage.updated_at
  ) {
    return messages;
  }

  const next = messages.slice();
  next[existingIndex] = nextMessage;
  return next;
};

const buildUnifiedMessage = (
  subtaskId: string,
  activity: ShadowCloneActivityEnvelope,
  messageType: UnifiedMessage['type'],
  metadata: ParsedMetadata,
  content: ParsedContent,
): UnifiedMessage => {
  const sequence = Number(activity.sequence ?? Date.now());
  const timestamp = activity.updated_at || activity.created_at || new Date().toISOString();
  return {
    sequence: Number.isFinite(sequence) ? sequence : undefined,
    message_id: buildMessageId(subtaskId, messageType, sequence, content),
    thread_id: `shadow-clone:${subtaskId}`,
    type: messageType,
    role: messageType === 'tool' ? 'tool' : 'assistant',
    is_llm_message: messageType !== 'tool',
    content: JSON.stringify(content),
    metadata: JSON.stringify({
      ...metadata,
      stream_status: 'complete',
      shadow_clone_subtask_id: subtaskId,
      shadow_clone_activity_relay: true,
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};

const shouldPromoteToolResultToAssistantMessage = (
  metadata: ParsedMetadata,
  content: ParsedContent,
): boolean =>
  metadata.activity_owner === 'claude_sdk_subagent' &&
  String(content.tool_name || '').trim().toLowerCase() === 'agent' &&
  typeof content.result === 'string' &&
  content.result.trim().length > 0;

const buildAssistantMessageFromToolResult = (
  subtaskId: string,
  activity: ShadowCloneActivityEnvelope,
  metadata: ParsedMetadata,
  content: ParsedContent,
): UnifiedMessage => {
  const sequence = Number(activity.sequence ?? Date.now());
  const timestamp = activity.updated_at || activity.created_at || new Date().toISOString();
  const resultText = String(content.result || '');
  return {
    sequence: Number.isFinite(sequence) ? sequence : undefined,
    message_id: `shadow-clone:${subtaskId}:assistant-tool-result:${content.tool_call_id || sequence}`,
    thread_id: `shadow-clone:${subtaskId}`,
    type: 'assistant',
    role: 'assistant',
    is_llm_message: true,
    content: JSON.stringify({
      role: 'assistant',
      content: resultText,
    }),
    metadata: JSON.stringify({
      ...metadata,
      stream_status: 'complete',
      shadow_clone_subtask_id: subtaskId,
      shadow_clone_activity_relay: true,
      shadow_clone_promoted_tool_result: true,
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};

export const applyShadowCloneTranscriptActivity = (
  transcriptState: ShadowCloneTranscriptState,
  activity: ShadowCloneActivityEnvelope,
): ShadowCloneTranscriptState => {
  const sequence = Number(activity.sequence ?? -1);
  const subtaskId = String(activity.subtask_id || '');
  const content = (typeof activity.content === 'string'
    ? safeJsonParse<ParsedContent>(activity.content, { content: activity.content })
    : (activity.content || {})) as ParsedContent;
  const metadata = (activity.metadata && typeof activity.metadata === 'object'
    ? activity.metadata
    : {}) as ParsedMetadata;
  const streamStatus = String(metadata.stream_status || '').trim();
  const messageType = String(activity.message_type || 'assistant').trim() as UnifiedMessage['type'];
  if (
    isStaleActivityForCurrentTranscriptChannel(
      transcriptState,
      sequence,
      streamStatus,
      messageType,
      content,
    )
  ) {
    return transcriptState;
  }

  if (streamStatus === 'reasoning_chunk') {
    const nextReasoningContent =
      typeof content.reasoning_content === 'string'
        ? content.reasoning_content
        : transcriptState.streamingReasoningContent;
    if (
      nextReasoningContent === transcriptState.streamingReasoningContent &&
      transcriptState.latestLabel === '正在推理'
    ) {
      return withAdvancedLastSequence(transcriptState, sequence);
    }

    return {
      ...transcriptState,
      streamingReasoningContent: nextReasoningContent,
      lastSequence: nextLastSequence(transcriptState, sequence),
      latestLabel: '正在推理',
    };
  }

  if (streamStatus === 'tool_call_chunk') {
    const nextToolCall = buildToolCallPayload(activity, metadata, content);
    if (
      sameStreamingToolCall(nextToolCall, transcriptState.streamingToolCall) &&
      transcriptState.streamingTextContent === '' &&
      transcriptState.latestLabel === '正在调用工具'
    ) {
      return withAdvancedLastSequence(transcriptState, sequence);
    }

    return {
      ...transcriptState,
      streamingToolCall: nextToolCall,
      streamingTextContent: transcriptState.streamingTextContent,
      lastSequence: nextLastSequence(transcriptState, sequence),
      latestLabel: '正在调用工具',
    };
  }

  if (streamStatus === 'chunk') {
    const incomingTextContent =
      typeof content.content === 'string'
        ? content.content
        : '';
    const nextTextContent = mergeStreamingTextChunk(
      transcriptState.streamingTextContent,
      incomingTextContent,
    );
    if (
      nextTextContent === transcriptState.streamingTextContent &&
      transcriptState.latestLabel === '正在输出回复'
    ) {
      return withAdvancedLastSequence(transcriptState, sequence);
    }

    return {
      ...transcriptState,
      streamingTextContent: nextTextContent,
      lastSequence: nextLastSequence(transcriptState, sequence),
      latestLabel: '正在输出回复',
    };
  }

  if (messageType === 'tool' && typeof content.tool_name === 'string') {
    if (shouldPromoteToolResultToAssistantMessage(metadata, content)) {
      return {
        ...transcriptState,
        messages: upsertMessage(
          transcriptState.messages,
          buildAssistantMessageFromToolResult(subtaskId, activity, metadata, content),
        ),
        streamingToolCall: null,
        streamingTextContent: '',
        lastSequence: nextLastSequence(transcriptState, sequence),
        latestLabel: '已生成回复',
      };
    }

    return {
      ...transcriptState,
      messages: upsertMessage(
        transcriptState.messages,
        buildUnifiedMessage(subtaskId, activity, 'tool', metadata, content),
      ),
      streamingToolCall: null,
      lastSequence: nextLastSequence(transcriptState, sequence),
      latestLabel: `已完成 ${content.tool_name}`,
    };
  }

  if (messageType === 'assistant' && streamStatus === 'complete') {
    const completeContent = {
      ...content,
      content:
        typeof content.content === 'string'
          ? mergeStreamingTextChunk(
              transcriptState.streamingTextContent,
              content.content,
            )
          : transcriptState.streamingTextContent || content.content,
    } as ParsedContent;
    const mergedContent = {
      ...completeContent,
      reasoning_content:
        typeof completeContent.reasoning_content === 'string' &&
        completeContent.reasoning_content
          ? completeContent.reasoning_content
          : transcriptState.streamingReasoningContent || undefined,
    } as ParsedContent;

    return {
      messages: upsertMessage(
        transcriptState.messages,
        buildUnifiedMessage(subtaskId, activity, 'assistant', metadata, mergedContent),
      ),
      streamingTextContent: '',
      streamingReasoningContent: '',
      streamingToolCall: null,
      lastSequence: nextLastSequence(transcriptState, sequence),
      latestLabel: Array.isArray(content.tool_calls) && content.tool_calls.length > 0
        ? '已生成工具调用'
        : '已生成回复',
    };
  }

  return {
    ...transcriptState,
    lastSequence: nextLastSequence(transcriptState, sequence),
  };
};

export const applyShadowCloneTranscriptMessage = (
  transcriptState: ShadowCloneTranscriptState,
  message: UnifiedMessage,
  scopeId: string = 'main',
): ShadowCloneTranscriptState => {
  const content = safeJsonParse<ParsedContent>(message.content, {} as ParsedContent);
  const metadata = safeJsonParse<ParsedMetadata>(message.metadata, {} as ParsedMetadata);

  return applyShadowCloneTranscriptActivity(transcriptState, {
    subtask_id: scopeId,
    sequence: message.sequence,
    role:
      typeof content.role === 'string'
        ? content.role
        : typeof message.role === 'string'
          ? message.role
          : 'assistant',
    message_type: message.type,
    content,
    metadata,
    created_at: message.created_at,
    updated_at: message.updated_at,
  });
};

export const upsertSyntheticCompleteTranscriptMessage = (
  messages: UnifiedMessage[],
  subtaskId: string,
  resultText: string,
  timestamp?: string,
): UnifiedMessage[] => {
  const nextMessage: UnifiedMessage = {
    message_id: `shadow-clone:${subtaskId}:assistant-complete`,
    thread_id: `shadow-clone:${subtaskId}`,
    type: 'assistant',
    role: 'assistant',
    is_llm_message: true,
    content: JSON.stringify({
      role: 'assistant',
      content: resultText,
    }),
    metadata: JSON.stringify({
      stream_status: 'complete',
      shadow_clone_subtask_id: subtaskId,
      shadow_clone_synthetic: true,
    }),
    created_at: timestamp || new Date().toISOString(),
    updated_at: timestamp || new Date().toISOString(),
  };

  const hasEquivalentAssistantMessage = messages.some((message) => {
    if (message.type !== 'assistant') return false;
    const parsed = safeJsonParse<ParsedContent>(message.content, {} as ParsedContent);
    return typeof parsed.content === 'string' && parsed.content.trim() === resultText.trim();
  });

  if (hasEquivalentAssistantMessage) {
    return messages;
  }

  return upsertMessage(messages, nextMessage);
};
