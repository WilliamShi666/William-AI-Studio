import type { ParsedContent, ParsedMetadata } from '@/components/thread/types';

export interface ClaudeSDKSubagentRoute {
  streamStatus: 'subagent_started' | 'subagent_activity';
  content: Record<string, unknown>;
  liveActivity: {
    scope: 'shadow_clone_main';
    phase: 'execution';
    reason: 'subagent_started' | 'subagent_activity';
    subtask_id: string;
  };
}

const parseToolArguments = (value: unknown): Record<string, unknown> => {
  if (!value) return {};
  if (typeof value === 'object') return value as Record<string, unknown>;
  if (typeof value !== 'string') return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
};

const firstToolCall = (
  parsedContent: ParsedContent,
  parsedMetadata: ParsedMetadata,
): Record<string, any> | null => {
  const candidates = Array.isArray(parsedMetadata.tool_calls) && parsedMetadata.tool_calls.length > 0
    ? parsedMetadata.tool_calls
    : Array.isArray(parsedContent.tool_calls)
      ? parsedContent.tool_calls
      : [];
  return candidates[0] || null;
};

const trimTaskDescription = (value: unknown): string => {
  const raw = typeof value === 'string' ? value.trim() : '';
  if (!raw) return '';
  return raw.length > 180 ? `${raw.slice(0, 177)}...` : raw;
};

const resolveClaudeSDKSubagentRouteImpl = ({
  messageType,
  sequence,
  parsedContent,
  parsedMetadata,
}: {
  messageType: string;
  sequence?: number;
  parsedContent: ParsedContent;
  parsedMetadata: ParsedMetadata;
}): ClaudeSDKSubagentRoute | null => {
  if (parsedMetadata.activity_owner !== 'claude_sdk_subagent') {
    return null;
  }

  const toolCall = firstToolCall(parsedContent, parsedMetadata);
  const subtaskId = String(
    parsedMetadata.subagent_tool_call_id ||
      parsedMetadata.parent_tool_use_id ||
      toolCall?.id ||
      '',
  ).trim();
  if (!subtaskId) {
    return null;
  }

  const toolName = String(toolCall?.function?.name || parsedContent.name || '').trim();
  const toolArgs = parseToolArguments(toolCall?.function?.arguments);
  const agentName = String(
    toolArgs.agent || toolArgs.subagent_type || toolArgs.subagentType || '',
  ).trim();
  const isAgentToolStart =
    messageType === 'assistant' &&
    parsedMetadata.stream_status === 'tool_call_chunk' &&
    (toolName === 'Agent' || toolName === 'Task') &&
    Boolean(parsedMetadata.subagent_tool_call_id);

  if (isAgentToolStart) {
    return {
      streamStatus: 'subagent_started',
      content: {
        subtask_id: subtaskId,
        source: 'claude_sdk',
        role: agentName || 'teammate',
        task_description: trimTaskDescription(toolArgs.prompt || toolArgs.description),
        sequence,
        message_type: messageType,
        content: parsedContent,
        metadata: parsedMetadata,
      },
      liveActivity: {
        scope: 'shadow_clone_main',
        phase: 'execution',
        reason: 'subagent_started',
        subtask_id: subtaskId,
      },
    };
  }

  return {
    streamStatus: 'subagent_activity',
    content: {
      subtask_id: subtaskId,
      source: 'claude_sdk',
      role: agentName || undefined,
      sequence,
      message_type: messageType,
      content: parsedContent,
      metadata: parsedMetadata,
    },
    liveActivity: {
      scope: 'shadow_clone_main',
      phase: 'execution',
      reason: 'subagent_activity',
      subtask_id: subtaskId,
    },
  };
};


export const resolveClaudeSDKSubagentRoute = resolveClaudeSDKSubagentRouteImpl;
