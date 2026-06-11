/**
 * SCV2 subagent event routing utilities.
 *
 * Extracted from useAgentStream to enable unit testing of the logic that
 * routes subagent_activity chunk/complete events to the left panel's
 * streaming state and tool-call message history.
 */

interface SubagentContent {
  role?: string;
  content?: unknown;
  reasoning_content?: unknown;
  tool_calls?: unknown[];
  [key: string]: unknown;
}

interface BuildToolCallMessageParams {
  content: SubagentContent;
  metadata: Record<string, unknown>;
  threadId: string;
  messageId: string;
  sequence: number;
  timestamp: string;
}

// ── Event type checks ──

export const isSubagentChunkEvent = (
  topLevelType: string,
  streamStatus: string,
): boolean =>
  topLevelType === 'subagent_activity' &&
  (streamStatus === 'chunk' || streamStatus === 'reasoning_chunk');

export const isSubagentCompleteWithToolCalls = (
  topLevelType: string,
  streamStatus: string,
  content: SubagentContent,
): boolean => {
  if (topLevelType !== 'subagent_activity' || streamStatus !== 'complete') {
    return false;
  }
  const toolCalls = content.tool_calls;
  return Array.isArray(toolCalls) && toolCalls.length > 0;
};

// ── Content extraction ──

/** Extract streaming text from subagent_activity content, handling nested structures */
export const extractSubagentStreamingText = (
  content: SubagentContent | null | undefined,
): string => {
  if (!content || typeof content !== 'object') return '';

  // Direct content field (most common: { content: "Hello" })
  if (typeof content.content === 'string' && content.content) {
    return content.content;
  }

  // Nested: content.content.content (Claude SDK triple-nested)
  if (
    typeof content.content === 'object' &&
    content.content !== null &&
    typeof (content.content as SubagentContent).content === 'string'
  ) {
    return (content.content as SubagentContent).content as string;
  }

  return '';
};

/** Extract reasoning/thinking text from subagent_activity content */
export const extractSubagentReasoningText = (
  content: SubagentContent | null | undefined,
): string => {
  if (!content || typeof content !== 'object') return '';

  // Direct reasoning_content field
  if (typeof content.reasoning_content === 'string' && content.reasoning_content) {
    return content.reasoning_content;
  }

  // Nested: content.content.reasoning_content
  if (
    typeof content.content === 'object' &&
    content.content !== null &&
    typeof (content.content as SubagentContent).reasoning_content === 'string'
  ) {
    return (content.content as SubagentContent).reasoning_content as string;
  }

  return '';
};

// ── Synthetic message builder (Phase D: gray button fix) ──

/**
 * Build a synthetic type: "assistant" message from a subagent_activity event
 * that contains tool_calls. This allows the ThreadContent rendering loop
 * (line 1170-1221) to render the gray tool-call button, matching regular mode.
 */
export const buildSubagentToolCallMessage = (
  params: BuildToolCallMessageParams,
): Record<string, unknown> | null => {
  const { content, metadata, threadId, messageId, sequence, timestamp } = params;
  const toolCalls = content.tool_calls;

  if (!Array.isArray(toolCalls) || toolCalls.length === 0) {
    return null;
  }

  return {
    sequence,
    message_id: messageId,
    thread_id: threadId,
    type: 'assistant',
    is_llm_message: true,
    content: JSON.stringify({
      role: 'assistant',
      content: '',
      tool_calls: toolCalls,
    }),
    metadata: JSON.stringify({
      ...metadata,
      stream_status: 'complete',
      _synthetic_from: 'subagent_activity',
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};
