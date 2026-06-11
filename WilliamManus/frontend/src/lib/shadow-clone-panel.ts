import { safeJsonParse } from '@/components/thread/utils';

export interface ShadowClonePanelToolCall {
  assistantCall: {
    name?: string;
    content?: string;
    timestamp?: string;
    toolCallId?: string | null;
  };
  toolResult?: {
    content?: string;
    isSuccess?: boolean;
    timestamp?: string;
    toolCallId?: string | null;
  };
  messages?: any[];
}

export interface ShadowCloneActivityEnvelope {
  subtask_id: string;
  sequence?: number;
  role?: string;
  message_type?: string;
  content?: Record<string, any> | string | null;
  metadata?: Record<string, any> | null;
  created_at?: string;
  updated_at?: string;
}

export interface ShadowClonePanelState {
  toolCalls: ShadowClonePanelToolCall[];
  streamingText: string;
  latestLabel: string | null;
  lastSequence: number;
}

export const createEmptyShadowClonePanelState = (): ShadowClonePanelState => ({
  toolCalls: [],
  streamingText: '',
  latestLabel: null,
  lastSequence: -1,
});

const normalizeToolName = (value: string | null | undefined): string =>
  String(value || 'unknown')
    .trim()
    .replace(/_/g, '-')
    .toLowerCase();

const stringifyToolPayload = (value: unknown): string => {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const asObject = (value: unknown): Record<string, any> => {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, any>;
  }
  if (typeof value === 'string') {
    return safeJsonParse<Record<string, any>>(value, {});
  }
  return {};
};

const isFileOperationTool = (toolName: string): boolean => {
  const normalized = normalizeToolName(toolName);
  return (
    normalized.includes('file') ||
    normalized === 'write' ||
    normalized === 'create-file' ||
    normalized === 'delete-file' ||
    normalized === 'full-file-rewrite' ||
    normalized === 'edit-file' ||
    normalized === 'write-file'
  );
};

const buildFormattedAssistantContent = (
  toolName: string,
  rawArguments: unknown,
): { formattedContent: string; rawArgumentString: string } => {
  const normalizedToolName = normalizeToolName(toolName);
  const rawArgumentString =
    typeof rawArguments === 'string'
      ? rawArguments
      : stringifyToolPayload(rawArguments);
  const argumentsObj = asObject(rawArguments);

  if (
    normalizedToolName.includes('command') &&
    !rawArgumentString.includes('<execute-command>')
  ) {
    const command = argumentsObj.command || rawArgumentString;
    return {
      formattedContent: `<execute-command>${command}</execute-command>`,
      rawArgumentString,
    };
  }

  if (isFileOperationTool(normalizedToolName)) {
    const filePath =
      argumentsObj.file_path ||
      argumentsObj.path ||
      argumentsObj.target_file ||
      argumentsObj.file ||
      '';
    const fileContents =
      argumentsObj.file_contents_delta ||
      argumentsObj.file_contents ||
      argumentsObj.content ||
      '';
    const xmlTag =
      normalizedToolName === 'write-file' || normalizedToolName === 'write'
        ? 'create-file'
        : normalizedToolName;

    if (xmlTag === 'edit-file') {
      return {
        formattedContent: `<${xmlTag} target_file="${filePath}">${fileContents}</${xmlTag}>`,
        rawArgumentString,
      };
    }

    if (xmlTag === 'delete-file') {
      return {
        formattedContent: `<${xmlTag} file_path="${filePath}"></${xmlTag}>`,
        rawArgumentString,
      };
    }

    return {
      formattedContent: `<${xmlTag} file_path="${filePath}">${fileContents}</${xmlTag}>`,
      rawArgumentString,
    };
  }

  return {
    formattedContent: rawArgumentString,
    rawArgumentString,
  };
};

const upsertStreamingToolCall = (
  toolCalls: ShadowClonePanelToolCall[],
  nextToolCall: ShadowClonePanelToolCall,
): ShadowClonePanelToolCall[] => {
  const nextId = nextToolCall.assistantCall.toolCallId;
  const nextName = normalizeToolName(nextToolCall.assistantCall.name);
  const existingIndex = toolCalls.findIndex((item) => {
    const existingId = item.assistantCall.toolCallId;
    if (nextId && existingId) {
      return existingId === nextId;
    }
    return (
      normalizeToolName(item.assistantCall.name) === nextName &&
      item.toolResult?.content === 'STREAMING'
    );
  });

  if (existingIndex === -1) {
    return toolCalls.concat(nextToolCall);
  }

  const currentToolCall = toolCalls[existingIndex];
  const mergedToolCall: ShadowClonePanelToolCall = {
    ...currentToolCall,
    assistantCall: {
      ...currentToolCall.assistantCall,
      ...nextToolCall.assistantCall,
    },
    toolResult: {
      ...currentToolCall.toolResult,
      ...nextToolCall.toolResult,
    },
  };

  if (
    currentToolCall.assistantCall.name === mergedToolCall.assistantCall.name &&
    currentToolCall.assistantCall.content === mergedToolCall.assistantCall.content &&
    currentToolCall.assistantCall.timestamp === mergedToolCall.assistantCall.timestamp &&
    currentToolCall.assistantCall.toolCallId === mergedToolCall.assistantCall.toolCallId &&
    currentToolCall.toolResult?.content === mergedToolCall.toolResult?.content &&
    currentToolCall.toolResult?.isSuccess === mergedToolCall.toolResult?.isSuccess &&
    currentToolCall.toolResult?.timestamp === mergedToolCall.toolResult?.timestamp &&
    currentToolCall.toolResult?.toolCallId === mergedToolCall.toolResult?.toolCallId
  ) {
    return toolCalls;
  }

  const updated = toolCalls.slice();
  updated[existingIndex] = mergedToolCall;
  return updated;
};

const finalizeToolCall = (
  toolCalls: ShadowClonePanelToolCall[],
  toolName: string,
  toolCallId: string | null | undefined,
  resultText: string,
  timestamp?: string,
): ShadowClonePanelToolCall[] => {
  const normalizedToolName = normalizeToolName(toolName);
  const next = toolCalls.slice();
  let targetIndex = -1;

  if (toolCallId) {
    targetIndex = next.findIndex(
      (item) => item.assistantCall.toolCallId === toolCallId,
    );
  }

  if (targetIndex === -1) {
    for (let index = next.length - 1; index >= 0; index -= 1) {
      const item = next[index];
      if (
        normalizeToolName(item.assistantCall.name) === normalizedToolName &&
        item.toolResult?.content === 'STREAMING'
      ) {
        targetIndex = index;
        break;
      }
    }
  }

  if (targetIndex === -1) {
    return next.concat({
      assistantCall: {
        name: normalizedToolName,
        content: '',
        timestamp,
        toolCallId: toolCallId || undefined,
      },
      toolResult: {
        content: resultText,
        isSuccess: true,
        timestamp,
        toolCallId: toolCallId || undefined,
      },
    });
  }

  const currentToolCall = next[targetIndex];
  const nextToolCall: ShadowClonePanelToolCall = {
    ...currentToolCall,
    toolResult: {
      content: resultText,
      isSuccess: true,
      timestamp,
      toolCallId: toolCallId || undefined,
    },
  };

  if (
    currentToolCall.toolResult?.content === nextToolCall.toolResult?.content &&
    currentToolCall.toolResult?.isSuccess === nextToolCall.toolResult?.isSuccess &&
    currentToolCall.toolResult?.timestamp === nextToolCall.toolResult?.timestamp &&
    currentToolCall.toolResult?.toolCallId === nextToolCall.toolResult?.toolCallId
  ) {
    return toolCalls;
  }

  next[targetIndex] = nextToolCall;
  return next;
};

export const applyShadowCloneActivity = (
  panelState: ShadowClonePanelState,
  activity: ShadowCloneActivityEnvelope,
): ShadowClonePanelState => {
  const sequence = Number(activity.sequence ?? -1);
  if (Number.isFinite(sequence) && sequence <= panelState.lastSequence) {
    return panelState;
  }

  const content =
    typeof activity.content === 'string'
      ? safeJsonParse<Record<string, any>>(activity.content, {
          content: activity.content,
        })
      : activity.content || {};
  const metadata =
    activity.metadata && typeof activity.metadata === 'object'
      ? activity.metadata
      : {};
  const streamStatus = String(metadata.stream_status || '').trim();
  const messageType = String(activity.message_type || '').trim();
  const timestamp = activity.updated_at || activity.created_at || new Date().toISOString();

  if (messageType === 'assistant' && Array.isArray(content.tool_calls) && content.tool_calls.length > 0) {
    const toolCall = content.tool_calls[0] || {};
    const toolName = normalizeToolName(toolCall?.function?.name || content.name || 'unknown');
    const toolCallId = toolCall?.id ? String(toolCall.id) : null;
    const rawArguments = toolCall?.function?.arguments;
    const { formattedContent, rawArgumentString } = buildFormattedAssistantContent(
      toolName,
      rawArguments,
    );

    return {
      toolCalls: upsertStreamingToolCall(panelState.toolCalls, {
        assistantCall: {
          name: toolName,
          content: formattedContent,
          timestamp,
          toolCallId,
        },
        toolResult: {
          content: 'STREAMING',
          isSuccess: true,
          timestamp,
          toolCallId,
        },
      }),
      streamingText: isFileOperationTool(toolName) ? rawArgumentString : panelState.streamingText,
      latestLabel: `正在执行 ${toolName}`,
      lastSequence: Number.isFinite(sequence) ? sequence : panelState.lastSequence,
    };
  }

  if (messageType === 'tool' && typeof content.tool_name === 'string') {
    const toolName = normalizeToolName(content.tool_name);
    const toolCallId =
      typeof content.tool_call_id === 'string' ? content.tool_call_id : null;
    const resultText = stringifyToolPayload(content.result);

    return {
      toolCalls: finalizeToolCall(
        panelState.toolCalls,
        toolName,
        toolCallId,
        resultText,
        timestamp,
      ),
      streamingText: isFileOperationTool(toolName) ? '' : panelState.streamingText,
      latestLabel: `已完成 ${toolName}`,
      lastSequence: Number.isFinite(sequence) ? sequence : panelState.lastSequence,
    };
  }

  if (
    messageType === 'assistant' &&
    streamStatus === 'complete' &&
    typeof content.content === 'string' &&
    content.content.trim()
  ) {
    return {
      ...panelState,
      latestLabel: '已生成阶段结论',
      lastSequence: Number.isFinite(sequence) ? sequence : panelState.lastSequence,
    };
  }

  return {
    ...panelState,
    lastSequence: Number.isFinite(sequence) ? sequence : panelState.lastSequence,
  };
};

export const buildShadowCloneCompleteToolCall = (
  resultText: string,
  timestamp?: string,
): ShadowClonePanelToolCall => ({
  assistantCall: {
    name: 'complete',
    content: 'subagent_final_result',
    timestamp,
    toolCallId: 'shadow-clone-complete',
  },
  toolResult: {
    content: resultText,
    isSuccess: true,
    timestamp,
    toolCallId: 'shadow-clone-complete',
  },
});
