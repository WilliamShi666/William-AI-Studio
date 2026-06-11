import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { ToolCallInput } from '@/components/thread/tool-call-side-panel';
import { UnifiedMessage, ParsedMetadata, StreamingToolCall, AgentStatus } from '../_types';
import { safeJsonParse } from '@/components/thread/utils';
import { ParsedContent } from '@/components/thread/types';
import { extractToolName } from '@/components/thread/tool-views/xml-parser';
import { useIsMobile } from '@/hooks/use-mobile';
import {
  mergeNormalizedWriteFileArgs,
  normalizeWriteFileArgs,
  stringifyNormalizedWriteFileArgs,
} from '@/lib/write-file-stream';

interface UseToolCallsReturn {
  toolCalls: ToolCallInput[];
  setToolCalls: React.Dispatch<React.SetStateAction<ToolCallInput[]>>;
  currentToolIndex: number;
  setCurrentToolIndex: React.Dispatch<React.SetStateAction<number>>;
  isSidePanelOpen: boolean;
  setIsSidePanelOpen: React.Dispatch<React.SetStateAction<boolean>>;
  autoOpenedPanel: boolean;
  setAutoOpenedPanel: React.Dispatch<React.SetStateAction<boolean>>;
  externalNavIndex: number | undefined;
  setExternalNavIndex: React.Dispatch<React.SetStateAction<number | undefined>>;
  handleToolClick: (clickedAssistantMessageId: string | null, clickedToolName: string) => void;
  handleStreamingToolCall: (toolCall: StreamingToolCall | null) => void;
  toggleSidePanel: () => void;
  handleSidePanelNavigate: (newIndex: number) => void;
  userClosedPanelRef: React.MutableRefObject<boolean>;
}

const STREAMING_RESULT_CONTENT = 'STREAMING';
const STREAMING_MATCH_WINDOW_MS = 30_000;

type HistoricalToolState = {
  toolPairs: ToolCallInput[];
  messageIdToIndex: Map<string, number>;
  assistantIndexByMessageId: Map<string, number>;
};

type ToolMessageMatch = {
  index: number;
  message: UnifiedMessage;
};

function parseToolContent(content: unknown): {
  toolName: string;
  parameters: Record<string, unknown>;
  result: Record<string, unknown> | null;
} | null {
  try {
    const parsed =
      typeof content === 'string' ? safeJsonParse<Record<string, unknown> | string>(content, content) : content;

    if (parsed && typeof parsed === 'object') {
      const parsedRecord = parsed as Record<string, unknown>;

      if ('tool_name' in parsedRecord || 'xml_tag_name' in parsedRecord) {
        return {
          toolName:
            (typeof parsedRecord['tool_name'] === 'string' && parsedRecord['tool_name']) ||
            (typeof parsedRecord['xml_tag_name'] === 'string' && parsedRecord['xml_tag_name']) ||
            'unknown',
          parameters:
            parsedRecord['parameters'] && typeof parsedRecord['parameters'] === 'object'
              ? (parsedRecord['parameters'] as Record<string, unknown>)
              : {},
          result:
            parsedRecord['result'] && typeof parsedRecord['result'] === 'object'
              ? (parsedRecord['result'] as Record<string, unknown>)
              : null,
        };
      }

      if ('content' in parsedRecord && parsedRecord['content'] && typeof parsedRecord['content'] === 'object') {
        const innerContent = parsedRecord['content'] as Record<string, unknown>;
        if ('tool_name' in innerContent || 'xml_tag_name' in innerContent) {
          return {
            toolName:
              (typeof innerContent['tool_name'] === 'string' && innerContent['tool_name']) ||
              (typeof innerContent['xml_tag_name'] === 'string' && innerContent['xml_tag_name']) ||
              'unknown',
            parameters:
              innerContent['parameters'] && typeof innerContent['parameters'] === 'object'
                ? (innerContent['parameters'] as Record<string, unknown>)
                : {},
            result:
              innerContent['result'] && typeof innerContent['result'] === 'object'
                ? (innerContent['result'] as Record<string, unknown>)
                : null,
          };
        }
      }
    }
  } catch {
    return null;
  }

  return null;
}

function getAssistantDisplayContent(message: UnifiedMessage): string {
  try {
    const parsed = safeJsonParse<ParsedContent>(message.content, {});
    if (typeof parsed.content === 'string') {
      return parsed.content;
    }
  } catch {
    return message.content;
  }

  return message.content;
}

function getHistoricalToolName(assistantMessage: UnifiedMessage, resultMessage: UnifiedMessage): string {
  const parsedToolContent = parseToolContent(resultMessage.content);
  if (parsedToolContent?.toolName) {
    return parsedToolContent.toolName.replace(/_/g, '-').toLowerCase();
  }

  const assistantContent = getAssistantDisplayContent(assistantMessage);
  const extractedToolName = extractToolName(assistantContent);
  if (extractedToolName) {
    return extractedToolName;
  }

  const parsedAssistantContent = safeJsonParse<{
    tool_calls?: Array<{ function?: { name?: string }; name?: string }>;
  }>(assistantMessage.content, {});
  const firstToolCall = parsedAssistantContent.tool_calls?.[0];
  const rawName = firstToolCall?.function?.name || firstToolCall?.name;

  return rawName ? rawName.replace(/_/g, '-').toLowerCase() : 'unknown';
}

function getHistoricalToolSuccess(resultMessage: UnifiedMessage): boolean {
  const parsedToolContent = parseToolContent(resultMessage.content);
  if (parsedToolContent?.result && typeof parsedToolContent.result === 'object') {
    return parsedToolContent.result.success !== false;
  }

  const toolResultContent = (() => {
    try {
      const parsed = safeJsonParse<ParsedContent>(resultMessage.content, {});
      return parsed.content || resultMessage.content;
    } catch {
      return resultMessage.content;
    }
  })();

  if (typeof toolResultContent !== 'string') {
    return true;
  }

  const explicitSuccessMatch = toolResultContent.match(
    /ToolResult\s*\(\s*success\s*=\s*(True|False|true|false)/i,
  );
  if (explicitSuccessMatch) {
    return explicitSuccessMatch[1].toLowerCase() === 'true';
  }

  const normalizedContent = toolResultContent.toLowerCase();
  return !(
    normalizedContent.includes('failed') ||
    normalizedContent.includes('error') ||
    normalizedContent.includes('failure')
  );
}

function getTimestampValue(timestamp?: string, fallback = 0): number {
  const parsed = Date.parse(timestamp || '');
  return Number.isFinite(parsed) ? parsed : fallback;
}

function isSameToolCall(a: ToolCallInput, b: ToolCallInput): boolean {
  return (
    a.assistantCall.name === b.assistantCall.name &&
    a.assistantCall.content === b.assistantCall.content &&
    a.assistantCall.timestamp === b.assistantCall.timestamp &&
    a.toolResult?.content === b.toolResult?.content &&
    a.toolResult?.isSuccess === b.toolResult?.isSuccess &&
    a.toolResult?.timestamp === b.toolResult?.timestamp
  );
}

function hasHistoricalReplacement(streamingItem: ToolCallInput, historicalItem: ToolCallInput): boolean {
  if (historicalItem.assistantCall.name !== streamingItem.assistantCall.name) {
    return false;
  }

  if (historicalItem.assistantCall.content === streamingItem.assistantCall.content) {
    return true;
  }

  const streamingTime = getTimestampValue(streamingItem.assistantCall.timestamp);
  const historicalTime = getTimestampValue(historicalItem.assistantCall.timestamp);

  return (
    streamingTime > 0 &&
    historicalTime > 0 &&
    Math.abs(historicalTime - streamingTime) < STREAMING_MATCH_WINDOW_MS
  );
}

function mergeHistoricalAndStreamingToolCalls(
  previousToolCalls: ToolCallInput[],
  historicalToolPairs: ToolCallInput[],
): ToolCallInput[] {
  const streamingItems = previousToolCalls.filter(
    (toolCall) => toolCall.toolResult?.content === STREAMING_RESULT_CONTENT,
  );
  const remainingStreamingItems = streamingItems.filter(
    (streamingItem) =>
      !historicalToolPairs.some((historicalItem) =>
        hasHistoricalReplacement(streamingItem, historicalItem),
      ),
  );

  const mergedToolCalls = [...historicalToolPairs, ...remainingStreamingItems];

  if (
    previousToolCalls.length === mergedToolCalls.length &&
    previousToolCalls.every((toolCall, index) => isSameToolCall(toolCall, mergedToolCalls[index]))
  ) {
    return previousToolCalls;
  }

  return mergedToolCalls;
}

function buildHistoricalToolState(messages: UnifiedMessage[]): HistoricalToolState {
  const assistantMessages: UnifiedMessage[] = [];
  const assistantToolCallIdsByMessageId = new Map<string, string[]>();
  const assistantIndexByMessageId = new Map<string, number>();
  const firstToolMatchByAssistantId = new Map<string, ToolMessageMatch>();
  const firstToolMatchByToolCallId = new Map<string, ToolMessageMatch>();

  messages.forEach((message, index) => {
    if (message.type === 'assistant' && message.message_id) {
      assistantIndexByMessageId.set(message.message_id, assistantMessages.length);
      assistantMessages.push(message);

      const parsedAssistantContent = safeJsonParse<{
        tool_calls?: Array<{ id?: string }>;
      }>(message.content, {});
      const toolCallIds = Array.isArray(parsedAssistantContent.tool_calls)
        ? parsedAssistantContent.tool_calls
            .map((toolCall) => toolCall?.id)
            .filter((toolCallId): toolCallId is string => Boolean(toolCallId))
        : [];

      assistantToolCallIdsByMessageId.set(message.message_id, toolCallIds);
      return;
    }

    if (message.type !== 'tool') {
      return;
    }

    const metadata = safeJsonParse<ParsedMetadata>(message.metadata, {});
    if (
      typeof metadata.assistant_message_id === 'string' &&
      !firstToolMatchByAssistantId.has(metadata.assistant_message_id)
    ) {
      firstToolMatchByAssistantId.set(metadata.assistant_message_id, { index, message });
    }

    const parsedToolContent = safeJsonParse<{ tool_call_id?: string }>(message.content, {});
    if (
      typeof parsedToolContent.tool_call_id === 'string' &&
      !firstToolMatchByToolCallId.has(parsedToolContent.tool_call_id)
    ) {
      firstToolMatchByToolCallId.set(parsedToolContent.tool_call_id, { index, message });
    }
  });

  const toolPairs: ToolCallInput[] = [];
  const messageIdToIndex = new Map<string, number>();

  assistantMessages.forEach((assistantMessage) => {
    if (!assistantMessage.message_id) {
      return;
    }

    let matchedToolMessage = firstToolMatchByAssistantId.get(assistantMessage.message_id);
    const toolCallIds = assistantToolCallIdsByMessageId.get(assistantMessage.message_id) || [];

    toolCallIds.forEach((toolCallId) => {
      const candidate = firstToolMatchByToolCallId.get(toolCallId);
      if (!candidate) {
        return;
      }

      if (!matchedToolMessage || candidate.index < matchedToolMessage.index) {
        matchedToolMessage = candidate;
      }
    });

    if (!matchedToolMessage) {
      return;
    }

    const toolIndex = toolPairs.length;
    toolPairs.push({
      assistantCall: {
        name: getHistoricalToolName(assistantMessage, matchedToolMessage.message),
        content: assistantMessage.content,
        timestamp: assistantMessage.created_at,
      },
      toolResult: {
        content: matchedToolMessage.message.content,
        isSuccess: getHistoricalToolSuccess(matchedToolMessage.message),
        timestamp: matchedToolMessage.message.created_at,
      },
    });
    messageIdToIndex.set(assistantMessage.message_id, toolIndex);
  });

  return {
    toolPairs,
    messageIdToIndex,
    assistantIndexByMessageId,
  };
}

export function useToolCalls(
  messages: UnifiedMessage[],
  setLeftSidebarOpen: (open: boolean) => void,
  agentStatus?: AgentStatus,
): UseToolCallsReturn {
  const [toolCalls, setToolCalls] = useState<ToolCallInput[]>([]);
  const [currentToolIndex, setCurrentToolIndex] = useState<number>(0);
  const [isSidePanelOpen, setIsSidePanelOpen] = useState(false);
  const [autoOpenedPanel, setAutoOpenedPanel] = useState(false);
  const [externalNavIndex, setExternalNavIndex] = useState<number | undefined>(undefined);
  const toolCallsRef = useRef<ToolCallInput[]>([]);
  const userClosedPanelRef = useRef(false);
  const userNavigatedRef = useRef(false);
  const assistantMessageToToolIndex = useRef<Map<string, number>>(new Map());
  const assistantMessageFallbackIndex = useRef<Map<string, number>>(new Map());
  const isMobile = useIsMobile();

  const historicalToolState = useMemo(() => buildHistoricalToolState(messages), [messages]);
  const historicalToolPairs = historicalToolState.toolPairs;

  useEffect(() => {
    toolCallsRef.current = toolCalls;
  }, [toolCalls]);

  useEffect(() => {
    assistantMessageToToolIndex.current = historicalToolState.messageIdToIndex;
    assistantMessageFallbackIndex.current = historicalToolState.assistantIndexByMessageId;
  }, [
    historicalToolState.assistantIndexByMessageId,
    historicalToolState.messageIdToIndex,
  ]);

  const toggleSidePanel = useCallback(() => {
    setIsSidePanelOpen((prevIsOpen) => {
      const nextIsOpen = !prevIsOpen;
      if (!nextIsOpen) {
        userClosedPanelRef.current = true;
      }
      if (nextIsOpen) {
        setLeftSidebarOpen(false);
      }
      return nextIsOpen;
    });
  }, [setLeftSidebarOpen]);

  const handleSidePanelNavigate = useCallback((newIndex: number) => {
    setCurrentToolIndex(newIndex);
    userNavigatedRef.current = true;
  }, []);

  const focusToolIndex = useCallback((toolIndex: number) => {
    setExternalNavIndex(toolIndex);
    setCurrentToolIndex(toolIndex);
    setIsSidePanelOpen(true);

    setTimeout(() => {
      setExternalNavIndex(undefined);
    }, 100);
  }, []);

  useEffect(() => {
    let cancelled = false;

    queueMicrotask(() => {
      if (cancelled) {
        return;
      }

      setToolCalls((previousToolCalls) =>
        mergeHistoricalAndStreamingToolCalls(previousToolCalls, historicalToolPairs),
      );
    });

    return () => {
      cancelled = true;
    };
  }, [historicalToolPairs]);

  useEffect(() => {
    if (historicalToolPairs.length === 0) {
      return;
    }

    let cancelled = false;

    queueMicrotask(() => {
      if (cancelled) {
        return;
      }

      const latestHistoricalIndex = historicalToolPairs.length - 1;

      if (agentStatus === 'running' && !userNavigatedRef.current) {
        setCurrentToolIndex(latestHistoricalIndex);
        return;
      }

      if (isSidePanelOpen && !userClosedPanelRef.current && !userNavigatedRef.current) {
        setCurrentToolIndex(latestHistoricalIndex);
        return;
      }

      if (!isSidePanelOpen && !autoOpenedPanel && !userClosedPanelRef.current && !isMobile) {
        setCurrentToolIndex(latestHistoricalIndex);
        setIsSidePanelOpen(true);
        setAutoOpenedPanel(true);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [
    agentStatus,
    autoOpenedPanel,
    historicalToolPairs.length,
    isMobile,
    isSidePanelOpen,
  ]);

  useEffect(() => {
    if (agentStatus === 'idle') {
      userNavigatedRef.current = false;
    }
  }, [agentStatus]);

  useEffect(() => {
    if (!autoOpenedPanel || isSidePanelOpen) {
      return;
    }

    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) {
        setAutoOpenedPanel(false);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [autoOpenedPanel, isSidePanelOpen]);

  const handleToolClick = useCallback(
    (clickedAssistantMessageId: string | null, clickedToolName: string) => {
      void clickedToolName;

      if (!clickedAssistantMessageId) {
        toast.warning('Cannot view details: Assistant message ID is missing.');
        return;
      }

      userClosedPanelRef.current = false;
      userNavigatedRef.current = true;

      const mappedToolIndex = assistantMessageToToolIndex.current.get(clickedAssistantMessageId);
      if (mappedToolIndex !== undefined) {
        focusToolIndex(mappedToolIndex);
        return;
      }

      const fallbackAssistantIndex = assistantMessageFallbackIndex.current.get(
        clickedAssistantMessageId,
      );
      if (
        fallbackAssistantIndex !== undefined &&
        fallbackAssistantIndex >= 0 &&
        fallbackAssistantIndex < toolCallsRef.current.length
      ) {
        focusToolIndex(fallbackAssistantIndex);
        return;
      }

      toast.info('Could not find details for this tool call.');
    },
    [focusToolIndex],
  );

  const handleStreamingToolCall = useCallback((toolCall: StreamingToolCall | null) => {
    if (!toolCall || userClosedPanelRef.current) {
      return;
    }

    const rawToolName = toolCall.name || toolCall.xml_tag_name || 'Unknown Tool';
    const toolName = rawToolName.replace(/_/g, '-').toLowerCase();
    const isWriteFileTool = toolName === 'write-file' || toolName === 'write_file';
    const rawArguments = toolCall.arguments;

    let toolArguments = '';
    let parsedArguments: Record<string, unknown> | null = null;

    if (typeof rawArguments === 'object' && rawArguments !== null) {
      parsedArguments = rawArguments as Record<string, unknown>;
      toolArguments = JSON.stringify(rawArguments);
    } else if (typeof rawArguments === 'string') {
      toolArguments = rawArguments;
      if (rawArguments.trim().startsWith('{')) {
        try {
          parsedArguments = JSON.parse(rawArguments) as Record<string, unknown>;
        } catch {
          parsedArguments = null;
        }
      }
    }

    const normalizedWriteFileArgs = isWriteFileTool ? normalizeWriteFileArgs(rawArguments) : null;
    if (normalizedWriteFileArgs) {
      toolArguments =
        stringifyNormalizedWriteFileArgs(normalizedWriteFileArgs) || toolArguments;
      parsedArguments = {
        ...(parsedArguments || {}),
        ...normalizedWriteFileArgs,
      };
    }

    let formattedContent = toolArguments;

    if (toolName.includes('command') && !toolArguments.includes('<execute-command>')) {
      const command = parsedArguments?.command || toolArguments;
      formattedContent = `<execute-command>${command}</execute-command>`;
    } else if (
      toolName.includes('file') ||
      toolName === 'create-file' ||
      toolName === 'delete-file' ||
      toolName === 'full-file-rewrite' ||
      toolName === 'edit-file' ||
      toolName === 'write-file' ||
      toolName === 'write_file'
    ) {
      const fileOpTags = [
        'create-file',
        'delete-file',
        'full-file-rewrite',
        'edit-file',
        'write-file',
        'write_file',
        'create_file',
        'delete_file',
        'edit_file',
      ];
      const normalizedToolName = toolName.replace(/_/g, '-');
      const matchingTag = fileOpTags.find(
        (tag) => toolName === tag || normalizedToolName === tag.replace(/_/g, '-'),
      );
      const fileArgs = normalizedWriteFileArgs || parsedArguments;

      if (matchingTag && fileArgs) {
        if (normalizedWriteFileArgs && normalizedToolName === 'write-file') {
          formattedContent =
            stringifyNormalizedWriteFileArgs(normalizedWriteFileArgs) || toolArguments;
        } else {
          const fileArgsRecord = fileArgs as Record<string, unknown>;
          const filePath =
            String(
              fileArgsRecord['file_path'] ||
                fileArgsRecord['path'] ||
                fileArgsRecord['target_file'] ||
                '',
            );
          const fileContents = String(
            fileArgsRecord['file_contents'] ||
              fileArgsRecord['content'] ||
              fileArgsRecord['file_contents_delta'] ||
              '',
          );
          const xmlTag =
            matchingTag === 'write-file' || matchingTag === 'write_file'
              ? 'create-file'
              : matchingTag.replace(/_/g, '-');

          if (xmlTag === 'edit-file') {
            formattedContent = `<${xmlTag} target_file="${filePath}">${fileContents}</${xmlTag}>`;
          } else if (xmlTag === 'delete-file') {
            formattedContent = `<${xmlTag} file_path="${filePath}"></${xmlTag}>`;
          } else {
            formattedContent = `<${xmlTag} file_path="${filePath}">${fileContents}</${xmlTag}>`;
          }
        }
      } else if (
        matchingTag &&
        normalizedToolName !== 'write-file' &&
        !toolArguments.includes(`<${matchingTag}>`) &&
        !toolArguments.includes('file_path=') &&
        !toolArguments.includes('target_file=')
      ) {
        const filePath = toolArguments.trim();
        if (filePath && !filePath.startsWith('<')) {
          formattedContent =
            matchingTag === 'edit-file'
              ? `<${matchingTag} target_file="${filePath}">`
              : `<${matchingTag} file_path="${filePath}">`;
        } else {
          formattedContent = `<${matchingTag}>${toolArguments}</${matchingTag}>`;
        }
      } else if (matchingTag) {
        formattedContent = toolArguments;
      }
    }

    const now = new Date().toISOString();
    const existingStreamingIndex = toolCallsRef.current.findIndex(
      (existingToolCall) =>
        existingToolCall.assistantCall.name === toolName &&
        existingToolCall.toolResult?.content === STREAMING_RESULT_CONTENT,
    );
    const nextFocusedIndex =
      existingStreamingIndex >= 0 ? existingStreamingIndex : toolCallsRef.current.length;

    const newToolCall: ToolCallInput = {
      assistantCall: {
        name: toolName,
        content: formattedContent,
        timestamp: now,
      },
      toolResult: {
        content: STREAMING_RESULT_CONTENT,
        isSuccess: true,
        timestamp: now,
      },
    };

    setToolCalls((previousToolCalls) => {
      const currentStreamingIndex = previousToolCalls.findIndex(
        (existingToolCall) =>
          existingToolCall.assistantCall.name === toolName &&
          existingToolCall.toolResult?.content === STREAMING_RESULT_CONTENT,
      );

      if (currentStreamingIndex >= 0) {
        const updatedToolCalls = [...previousToolCalls];
        let nextContent = formattedContent;

        if (isWriteFileTool) {
          const mergedArgs = mergeNormalizedWriteFileArgs(
            updatedToolCalls[currentStreamingIndex].assistantCall.content,
            formattedContent,
          );
          if (mergedArgs) {
            nextContent =
              stringifyNormalizedWriteFileArgs(mergedArgs) ||
              updatedToolCalls[currentStreamingIndex].assistantCall.content ||
              formattedContent;
          }
        }

        updatedToolCalls[currentStreamingIndex] = {
          ...updatedToolCalls[currentStreamingIndex],
          assistantCall: {
            ...updatedToolCalls[currentStreamingIndex].assistantCall,
            content: nextContent,
          },
          toolResult: {
            content: STREAMING_RESULT_CONTENT,
            isSuccess: true,
            timestamp: now,
          },
        };
        return updatedToolCalls;
      }

      return [...previousToolCalls, newToolCall];
    });

    if (!userNavigatedRef.current) {
      setCurrentToolIndex(nextFocusedIndex);
    }

    setIsSidePanelOpen(true);
  }, []);

  return {
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
  };
}
