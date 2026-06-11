import React, { useRef, useState, useCallback, useEffect, useMemo } from 'react';
import { CircleDashed, CheckCircle, AlertTriangle, Brain } from 'lucide-react';
import { UnifiedMessage, ParsedContent, ParsedMetadata } from '@/components/thread/types';
import { FileAttachmentGrid } from '@/components/thread/file-attachment';
import { useFilePreloader, useDirectoryQuery } from '@/hooks/react-query/files';
import { useAuth } from '@/components/AuthProvider';
import { Project } from '@/lib/api';
import {
    extractPrimaryParam,
    getToolIcon,
    getUserFriendlyToolName,
    safeJsonParse,
} from '@/components/thread/utils';
import { KortixLogo } from '@/components/sidebar/kortix-logo';
import { AgentLoader } from './loader';
import { FileWritingIndicator } from './FileWritingIndicator';
import { AgentAvatar, AgentName } from './agent-avatar';
import { parseXmlToolCalls, isNewXmlFormat } from '@/components/thread/tool-views/xml-parser';
import { ShowToolStream } from './ShowToolStream';
import { ComposioUrlDetector } from './composio-url-detector';
import { HIDE_STREAMING_XML_TAGS } from '@/components/thread/utils';
import { Reasoning, ReasoningTrigger, ReasoningContent } from '@/components/home/ui/reasoning';
import { Markdown } from '@/components/ui/markdown';
import { useLanguage } from '@/contexts/LanguageContext';
import { TaskFilesSummary, type TaskFileEntry } from './TaskFilesSummary';
import { normalizeWorkspaceAttachmentPaths } from '@/lib/workspace-attachments';

const TASK_FILE_TOOL_NAMES = new Set(['write_file', 'upload_file']);

const normalizeToolName = (toolName: string) =>
    toolName.replace(/-/g, '_').toLowerCase();

const normalizeWorkspacePath = (path: string) => {
    const trimmed = path.trim();
    if (!trimmed) return '';
    if (trimmed.startsWith('/workspace')) return trimmed;
    return `/workspace/${trimmed.replace(/^\/+/, '')}`;
};

const coerceBytes = (value: unknown): number | undefined => {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string') {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) return parsed;
    }
    return undefined;
};

const readShadowCloneFileArtifacts = (metadata: unknown): TaskFileEntry[] => {
    if (!metadata || typeof metadata !== 'object') return [];
    const rawArtifacts = (metadata as Record<string, unknown>).shadow_clone_file_artifacts;
    if (!Array.isArray(rawArtifacts)) return [];

    const seen = new Set<string>();
    return rawArtifacts
        .map((artifact) => {
            if (!artifact || typeof artifact !== 'object') return null;
            const rawPath = (artifact as Record<string, unknown>).path;
            const path = normalizeWorkspaceAttachmentPaths(rawPath)[0] || '';
            if (!path || seen.has(path)) return null;
            seen.add(path);
            return {
                path,
                bytes: coerceBytes((artifact as Record<string, unknown>).bytes),
            };
        })
        .filter((artifact): artifact is TaskFileEntry => Boolean(artifact));
};

type MessageGroup = {
    type: 'user' | 'assistant_group';
    messages: UnifiedMessage[];
    key: string;
};

type GroupedMessagesState = {
    finalGroupedMessages: MessageGroup[];
    groupRunIdByIndex: Map<number, string>;
    lastGroupIndexByRun: Map<string, number>;
};

const isShadowCloneCardSystemMessage = (message: UnifiedMessage): boolean => {
    if (message.type !== 'system') {
        return false;
    }

    const metadata = safeJsonParse<ParsedMetadata | Record<string, unknown>>(
        message.metadata,
        {},
    );
    return (
        Boolean(metadata) &&
        typeof metadata === 'object' &&
        metadata.synthetic_kind === 'shadow_clone_card'
    );
};

const buildGroupedMessagesState = ({
    displayMessages,
    readOnly,
    streamingTextContent,
    streamingReasoningContent,
    streamingToolCall,
    streamHookStatus,
    streamingText,
    isStreamingText,
    assistantRunByMessageId,
}: {
    displayMessages: UnifiedMessage[];
    readOnly: boolean;
    streamingTextContent: string;
    streamingReasoningContent?: string;
    streamingToolCall?: any;
    streamHookStatus: string;
    streamingText: string;
    isStreamingText: boolean;
    assistantRunByMessageId: Map<string, string>;
}): GroupedMessagesState => {
    const groupedMessages: MessageGroup[] = [];
    let currentGroup: MessageGroup | null = null;
    let assistantGroupCounter = 0;

    displayMessages.forEach((message, index) => {
        const messageType = message.type;
        const key = message.message_id || `msg-${index}`;

        if (isShadowCloneCardSystemMessage(message)) {
            return;
        }

        if (messageType === 'user') {
            if (currentGroup) {
                groupedMessages.push(currentGroup);
                currentGroup = null;
            }
            groupedMessages.push({ type: 'user', messages: [message], key });
            return;
        }

        if (
            messageType === 'assistant' ||
            messageType === 'tool' ||
            messageType === 'browser_state'
        ) {
            const canAddToExistingGroup =
                currentGroup &&
                currentGroup.type === 'assistant_group' &&
                (() => {
                    if (messageType === 'assistant') {
                        const lastAssistantMsg = currentGroup.messages.findLast(
                            (groupedMessage) => groupedMessage.type === 'assistant',
                        );
                        if (!lastAssistantMsg) return true;

                        return message.agent_id === lastAssistantMsg.agent_id;
                    }

                    return true;
                })();

            if (canAddToExistingGroup) {
                currentGroup?.messages.push(message);
            } else {
                if (currentGroup) {
                    groupedMessages.push(currentGroup);
                }
                assistantGroupCounter += 1;
                currentGroup = {
                    type: 'assistant_group',
                    messages: [message],
                    key: `assistant-group-${assistantGroupCounter}`,
                };
            }
            return;
        }

        if (messageType !== 'status' && currentGroup) {
            groupedMessages.push(currentGroup);
            currentGroup = null;
        }
    });

    if (currentGroup) {
        groupedMessages.push(currentGroup);
    }

    const mergedGroups: MessageGroup[] = [];
    let currentMergedGroup: MessageGroup | null = null;

    groupedMessages.forEach((group) => {
        if (group.type === 'assistant_group') {
            if (currentMergedGroup?.type === 'assistant_group') {
                currentMergedGroup.messages.push(...group.messages);
            } else {
                if (currentMergedGroup) {
                    mergedGroups.push(currentMergedGroup);
                }
                currentMergedGroup = {
                    ...group,
                    messages: [...group.messages],
                };
            }
            return;
        }

        if (currentMergedGroup) {
            mergedGroups.push(currentMergedGroup);
            currentMergedGroup = null;
        }
        mergedGroups.push(group);
    });

    if (currentMergedGroup) {
        mergedGroups.push(currentMergedGroup);
    }

    const finalGroupedMessages = mergedGroups.map((group) => ({
        ...group,
        messages: [...group.messages],
    }));
    const syntheticTimestamp = new Date().toISOString();

    const appendStreamingContent = (content: string, isPlayback = false) => {
        // Deduplication: skip if content already present in the last emitted assistant message.
        // This prevents double-rendering when textContent and messages[] hold the same content
        // (e.g., when the stream hook emits a message without clearing streaming text).
        if (content) {
            for (let i = finalGroupedMessages.length - 1; i >= 0; i--) {
                const group = finalGroupedMessages[i];
                if (group.type === 'assistant_group') {
                    const lastMsg = group.messages[group.messages.length - 1];
                    const isSyntheticStreaming =
                        lastMsg?.message_id === 'streamingTextContent' ||
                        lastMsg?.message_id === 'playbackStreamingText';
                    if (lastMsg?.content === content && !isSyntheticStreaming) {
                        return;
                    }
                    break;
                }
            }
        }

        const messageId = isPlayback ? 'playbackStreamingText' : 'streamingTextContent';
        const metadata = isPlayback ? 'playbackStreamingText' : 'streamingTextContent';
        const keySuffix = isPlayback ? 'playback-streaming' : 'streaming';
        const syntheticMessage: UnifiedMessage = {
            content,
            type: 'assistant',
            message_id: messageId,
            metadata,
            created_at: syntheticTimestamp,
            updated_at: syntheticTimestamp,
            is_llm_message: true,
            thread_id: messageId,
            sequence: Infinity,
        };
        const lastGroup = finalGroupedMessages.at(-1);

        if (!lastGroup || lastGroup.type === 'user') {
            assistantGroupCounter += 1;
            finalGroupedMessages.push({
                type: 'assistant_group',
                messages: [syntheticMessage],
                key: `assistant-group-${assistantGroupCounter}-${keySuffix}`,
            });
            return;
        }

        if (lastGroup.type === 'assistant_group') {
            const lastMessage = lastGroup.messages[lastGroup.messages.length - 1];
            if (lastMessage?.message_id !== messageId) {
                lastGroup.messages.push(syntheticMessage);
            }
        }
    };

    const shouldEnsureStreamingGroup =
        !readOnly &&
        !streamingTextContent &&
        (streamingReasoningContent || streamingToolCall) &&
        (streamHookStatus === 'streaming' || streamHookStatus === 'connecting');
    if (shouldEnsureStreamingGroup) {
        appendStreamingContent('', false);
    }

    if (streamingTextContent) {
        appendStreamingContent(streamingTextContent, false);
    }

    if (readOnly && streamingText && isStreamingText) {
        appendStreamingContent(streamingText, true);
    }

    const groupRunIdByIndex = new Map<number, string>();
    const lastGroupIndexByRun = new Map<string, number>();

    finalGroupedMessages.forEach((group, index) => {
        if (group.type !== 'assistant_group') return;

        let runId: string | null = null;

        for (let messageIndex = group.messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
            const message = group.messages[messageIndex];
            if (message.type !== 'assistant') continue;

            if (message.message_id) {
                const mappedRunId = assistantRunByMessageId.get(message.message_id);
                if (mappedRunId) {
                    runId = mappedRunId;
                    break;
                }
            }

            const metadata = safeJsonParse<ParsedMetadata | string>(message.metadata, {});
            if (
                metadata &&
                typeof metadata === 'object' &&
                typeof metadata.thread_run_id === 'string'
            ) {
                runId = metadata.thread_run_id;
                break;
            }
        }

        if (runId) {
            groupRunIdByIndex.set(index, runId);
            lastGroupIndexByRun.set(runId, index);
        }
    });

    return {
        finalGroupedMessages,
        groupRunIdByIndex,
        lastGroupIndexByRun,
    };
};


// Helper function to render attachments (keeping original implementation for now)
export function renderAttachments(attachments: string[], fileViewerHandler?: (filePath?: string, filePathList?: string[]) => void, sandboxId?: string, project?: Project) {
    if (!attachments || attachments.length === 0) return null;

    // Filter out empty strings and check if we have any valid attachments
    const validAttachments = attachments.filter(attachment => attachment && attachment.trim() !== '');
    if (validAttachments.length === 0) return null;

    return <FileAttachmentGrid
        attachments={validAttachments}
        onFileClick={fileViewerHandler}
        showPreviews={true}
        sandboxId={sandboxId}
        project={project}
    />;
}

// Render Markdown content while preserving XML tags that should be displayed as tool calls
export function renderMarkdownContent(
    content: string,
    handleToolClick: (assistantMessageId: string | null, toolName: string) => void,
    messageId: string | null,
    fileViewerHandler?: (filePath?: string, filePathList?: string[]) => void,
    sandboxId?: string,
    project?: Project,
    debugMode?: boolean
) {
    // If in debug mode, just display raw content in a pre tag
    if (debugMode) {
        return (
            <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto p-2 border border-border rounded-md bg-muted/30 text-foreground">
                {content}
            </pre>
        );
    }

    if (isNewXmlFormat(content)) {
        const contentParts: React.ReactNode[] = [];
        let lastIndex = 0;

        // Find all function_calls blocks
        const functionCallsRegex = /<function_calls>([\s\S]*?)<\/function_calls>/gi;
        let match: RegExpExecArray | null = null;

        while ((match = functionCallsRegex.exec(content)) !== null) {
            // Add text before the function_calls block
            if (match.index > lastIndex) {
                const textBeforeBlock = content.substring(lastIndex, match.index);
                if (textBeforeBlock.trim()) {
                    contentParts.push(
                        <ComposioUrlDetector key={`md-${lastIndex}`} content={textBeforeBlock} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words" />
                    );
                }
            }

            // Parse the tool calls in this block
            const toolCalls = parseXmlToolCalls(match[0]);

            toolCalls.forEach((toolCall, index) => {
                const toolName = toolCall.functionName.replace(/_/g, '-');

                if (toolName === 'ask') {
                    // Handle ask tool specially - extract text and attachments
                    const askText = toolCall.parameters.text || '';
                    const attachments = toolCall.parameters.attachments || [];

                    // Convert single attachment to array for consistent handling
                    const attachmentArray = normalizeWorkspaceAttachmentPaths(attachments);

                    // Render ask tool content with attachment UI
                    contentParts.push(
                        <div key={`ask-${match.index}-${index}`} className="space-y-3">
                            <ComposioUrlDetector content={askText} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words [&>:first-child]:mt-0 prose-headings:mt-3" />
                            {renderAttachments(attachmentArray, fileViewerHandler, sandboxId, project)}
                        </div>
                    );
                } else if (toolName === 'complete') {
                    // Handle complete tool specially - extract text and attachments
                    const completeText = toolCall.parameters.text || '';
                    const attachments = toolCall.parameters.attachments || '';

                    // Convert single attachment to array for consistent handling
                    const attachmentArray = normalizeWorkspaceAttachmentPaths(attachments);

                    // Render complete tool content with attachment UI
                    contentParts.push(
                        <div key={`complete-${match.index}-${index}`} className="space-y-3">
                            <ComposioUrlDetector content={completeText} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words [&>:first-child]:mt-0 prose-headings:mt-3" />
                            {renderAttachments(attachmentArray, fileViewerHandler, sandboxId, project)}
                        </div>
                    );
                } else {
                    const IconComponent = getToolIcon(toolName);

                    // Extract primary parameter for display
                    let paramDisplay = '';
                    if (toolCall.parameters.file_path) {
                        paramDisplay = toolCall.parameters.file_path;
                    } else if (toolCall.parameters.command) {
                        paramDisplay = toolCall.parameters.command;
                    } else if (toolCall.parameters.query) {
                        paramDisplay = toolCall.parameters.query;
                    } else if (toolCall.parameters.url) {
                        paramDisplay = toolCall.parameters.url;
                    }

                    contentParts.push(
                        <div
                            key={`tool-${match.index}-${index}`}
                            className="my-1"
                        >
                            <button
                                onClick={() => handleToolClick(messageId, toolName)}
                                className="inline-flex items-center gap-1.5 py-1 px-1 pr-1.5 text-xs text-muted-foreground bg-muted hover:bg-muted/80 rounded-lg transition-colors cursor-pointer border border-neutral-200 dark:border-neutral-700/50"
                            >
                                <div className='border-2 bg-gradient-to-br from-neutral-200 to-neutral-300 dark:from-neutral-700 dark:to-neutral-800 flex items-center justify-center p-0.5 rounded-sm border-neutral-400/20 dark:border-neutral-600'>
                                    <IconComponent className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                                </div>
                                <span className="font-mono text-xs text-foreground">{getUserFriendlyToolName(toolName)}</span>
                                {paramDisplay && <span className="ml-1 text-muted-foreground truncate max-w-[200px]" title={paramDisplay}>{paramDisplay}</span>}
                            </button>
                        </div>
                    );
                }
            });

            lastIndex = match.index + match[0].length;
        }

        // Add any remaining text after the last function_calls block
        if (lastIndex < content.length) {
            const remainingText = content.substring(lastIndex);
            if (remainingText.trim()) {
                contentParts.push(
                    <ComposioUrlDetector key={`md-${lastIndex}`} content={remainingText} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words" />
                );
            }
        }

        return contentParts.length > 0 ? contentParts : <ComposioUrlDetector content={content} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words" />;
    }

    // Fall back to old XML format handling
    const xmlRegex = /<(?!inform\b)([a-zA-Z\-_]+)(?:\s+[^>]*)?>(?:[\s\S]*?)<\/\1>|<(?!inform\b)([a-zA-Z\-_]+)(?:\s+[^>]*)?\/>/g;
    let lastIndex = 0;
    const contentParts: React.ReactNode[] = [];
    let match: RegExpExecArray | null = null;

    // If no XML tags found, just return the full content as markdown
    if (!content.match(xmlRegex)) {
        return <ComposioUrlDetector content={content} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words" />;
    }

    while ((match = xmlRegex.exec(content)) !== null) {
        // Add text before the tag as markdown
        if (match.index > lastIndex) {
            const textBeforeTag = content.substring(lastIndex, match.index);
            contentParts.push(
                <ComposioUrlDetector key={`md-${lastIndex}`} content={textBeforeTag} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none inline-block mr-1 break-words" />
            );
        }

        const rawXml = match[0];
        const toolName = match[1] || match[2];
        const toolCallKey = `tool-${match.index}`;

        if (toolName === 'ask') {
            // Extract attachments from the XML attributes
            const attachmentsMatch = rawXml.match(/attachments=["']([^"']*)["']/i);
            const attachments = attachmentsMatch
                ? normalizeWorkspaceAttachmentPaths(attachmentsMatch[1])
                : [];

            // Extract content from the ask tag
            const contentMatch = rawXml.match(/<ask[^>]*>([\s\S]*?)<\/ask>/i);
            const askContent = contentMatch ? contentMatch[1] : '';

            // Render <ask> tag content with attachment UI (using the helper)
            contentParts.push(
                <div key={`ask-${match.index}`} className="space-y-3">
                    <ComposioUrlDetector content={askContent} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words [&>:first-child]:mt-0 prose-headings:mt-3" />
                    {renderAttachments(attachments, fileViewerHandler, sandboxId, project)}
                </div>
            );
        } else if (toolName === 'complete') {
            // Extract attachments from the XML attributes
            const attachmentsMatch = rawXml.match(/attachments=["']([^"']*)["']/i);
            const attachments = attachmentsMatch
                ? normalizeWorkspaceAttachmentPaths(attachmentsMatch[1])
                : [];

            // Extract content from the complete tag
            const contentMatch = rawXml.match(/<complete[^>]*>([\s\S]*?)<\/complete>/i);
            const completeContent = contentMatch ? contentMatch[1] : '';

            // Render <complete> tag content with attachment UI (using the helper)
            contentParts.push(
                <div key={`complete-${match.index}`} className="space-y-3">
                    <ComposioUrlDetector content={completeContent} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words [&>:first-child]:mt-0 prose-headings:mt-3" />
                    {renderAttachments(attachments, fileViewerHandler, sandboxId, project)}
                </div>
            );
        } else {
            const IconComponent = getToolIcon(toolName);
            const paramDisplay = extractPrimaryParam(toolName, rawXml);

            // Render tool button as a clickable element
            contentParts.push(
                <div
                    key={toolCallKey}
                    className="my-1"
                >
                    <button
                        onClick={() => handleToolClick(messageId, toolName)}
                        className="inline-flex items-center gap-1.5 py-1 px-1 pr-1.5 text-xs text-muted-foreground bg-muted hover:bg-muted/80 rounded-lg transition-colors cursor-pointer border border-neutral-200 dark:border-neutral-700/50"
                    >
                        <div className='border-2 bg-gradient-to-br from-neutral-200 to-neutral-300 dark:from-neutral-700 dark:to-neutral-800 flex items-center justify-center p-0.5 rounded-sm border-neutral-400/20 dark:border-neutral-600'>
                            <IconComponent className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                        </div>
                        <span className="font-mono text-xs text-foreground">{getUserFriendlyToolName(toolName)}</span>
                        {paramDisplay && <span className="ml-1 text-muted-foreground truncate max-w-[200px]" title={paramDisplay}>{paramDisplay}</span>}
                    </button>
                </div>
            );
        }
        lastIndex = xmlRegex.lastIndex;
    }

    // Add text after the last tag
    if (lastIndex < content.length) {
        contentParts.push(
            <ComposioUrlDetector key={`md-${lastIndex}`} content={content.substring(lastIndex)} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none break-words" />
        );
    }

    return contentParts;
}

export interface ThreadContentProps {
    messages: UnifiedMessage[];
    streamingTextContent?: string;
    streamingReasoningContent?: string; // 流式思考/推理内容
    streamingToolCall?: any;
    agentStatus: 'idle' | 'running' | 'connecting' | 'disconnecting' | 'error';
    handleToolClick: (assistantMessageId: string | null, toolName: string) => void;
    handleOpenFileViewer: (filePath?: string, filePathList?: string[]) => void;
    readOnly?: boolean;
    visibleMessages?: UnifiedMessage[]; // For playback mode
    streamingText?: string; // For playback mode
    isStreamingText?: boolean; // For playback mode
    currentToolCall?: any; // For playback mode
    streamHookStatus?: string; // Add this prop
    sandboxId?: string; // Add sandboxId prop
    project?: Project; // Add project prop
    debugMode?: boolean; // Add debug mode parameter
    isPreviewMode?: boolean;
    agentName?: string;
    agentAvatar?: React.ReactNode;
    emptyStateComponent?: React.ReactNode; // Add custom empty state component prop
    threadMetadata?: any; // Add thread metadata prop
    scrollContainerRef?: React.RefObject<HTMLDivElement>; // Add scroll container ref prop
    agentMetadata?: any; // Add agent metadata prop
    agentData?: any; // Add full agent data prop
    isWritingFile?: boolean; // Whether agent is thinking or working
    suppressAgentActivity?: boolean;
    showPendingAssistantLoader?: boolean;
    onShadowCloneSubtaskSelect?: () => void;
}

export const ThreadContent: React.FC<ThreadContentProps> = ({
    messages,
    streamingTextContent = "",
    streamingReasoningContent = "",
    streamingToolCall,
    agentStatus,
    handleToolClick,
    handleOpenFileViewer,
    readOnly = false,
    visibleMessages,
    streamingText = "",
    isStreamingText = false,
    currentToolCall,
    streamHookStatus = "idle",
    sandboxId,
    project,
    debugMode = false,
    isPreviewMode = false,
    agentName = 'Roys Alpha',
    agentAvatar = <KortixLogo size={16} />,
    emptyStateComponent,
    threadMetadata,
    scrollContainerRef,
    agentMetadata,
    agentData,
    isWritingFile = false,
    suppressAgentActivity = false,
    showPendingAssistantLoader = false,
    onShadowCloneSubtaskSelect,
}) => {
    const messagesContainerRef = useRef<HTMLDivElement>(null);
    const latestMessageRef = useRef<HTMLDivElement>(null);
    const contentRef = useRef<HTMLDivElement>(null);
    const [shouldJustifyToTop, setShouldJustifyToTop] = useState(false);
    const { session } = useAuth();
    const { t } = useLanguage();

    // React Query file preloader
    const { preloadFiles } = useFilePreloader();

    const { data: workspaceEntries = [], refetch: refetchWorkspaceEntries } = useDirectoryQuery(
        sandboxId,
        '/workspace',
        {
            enabled: Boolean(sandboxId && session?.access_token),
            staleTime: 10 * 1000,
        },
    );

    const previousAgentStatusRef = useRef(agentStatus);

    useEffect(() => {
        const prevStatus = previousAgentStatusRef.current;
        const wasRunning = prevStatus === 'running' || prevStatus === 'connecting' || prevStatus === 'disconnecting';
        const isRunning = agentStatus === 'running' || agentStatus === 'connecting' || agentStatus === 'disconnecting';

        if (wasRunning && !isRunning) {
            refetchWorkspaceEntries();
        }

        previousAgentStatusRef.current = agentStatus;
    }, [agentStatus, refetchWorkspaceEntries]);

    const workspaceFiles = useMemo(() => {
        if (!workspaceEntries.length) return [];

        return workspaceEntries
            .filter((entry) => !entry.is_dir)
            .map((entry) => ({
                path: normalizeWorkspacePath(entry.path),
                bytes: coerceBytes(entry.size),
            }))
            .filter((entry) => entry.path);
    }, [workspaceEntries]);

    const containerClassName = isPreviewMode
        ? "flex-1 overflow-y-auto scrollbar-thin scrollbar-track-secondary/0 scrollbar-thumb-primary/10 scrollbar-thumb-rounded-full hover:scrollbar-thumb-primary/10 py-4 pb-0"
        : "flex-1 overflow-y-auto scrollbar-thin scrollbar-track-secondary/0 scrollbar-thumb-primary/10 scrollbar-thumb-rounded-full hover:scrollbar-thumb-primary/10 py-4 pb-0 bg-card/80 dark:bg-card/90 backdrop-blur supports-[backdrop-filter]:bg-card/60";

    // In playback mode, we use visibleMessages instead of messages
    // 过滤拆分的assistant消息，只显示主消息
    const filteredMessages = useMemo(() => messages.filter(message => {
        if (message.type === 'assistant' && message.metadata) {
            try {
                const metadata = JSON.parse(message.metadata);
                if (metadata.split_for_frontend === true && metadata.tool_index > 0) {
                    return false;
                }
            } catch {
                return true;
            }
        }
        return true;
    }), [messages]);

    const displayMessages = useMemo(
        () => (readOnly && visibleMessages ? visibleMessages : filteredMessages),
        [filteredMessages, readOnly, visibleMessages],
    );

    const { runFilesById, assistantRunByMessageId, messageFileArtifactsById } = useMemo(() => {
        const sourceMessages = readOnly && visibleMessages ? visibleMessages : messages;
        const assistantRunByMessageId = new Map<string, string>();
        const messageFileArtifactsById = new Map<string, TaskFileEntry[]>();

        sourceMessages.forEach((message) => {
            if (message.type !== 'assistant' || !message.message_id) return;

            const metadata = safeJsonParse<ParsedMetadata | string>(message.metadata, {});
            if (metadata && typeof metadata === 'object' && typeof metadata.thread_run_id === 'string') {
                assistantRunByMessageId.set(message.message_id, metadata.thread_run_id);
            }
            const shadowCloneFileArtifacts = readShadowCloneFileArtifacts(metadata);
            if (shadowCloneFileArtifacts.length > 0) {
                messageFileArtifactsById.set(message.message_id, shadowCloneFileArtifacts);
            }
        });

        const runFilesById = new Map<string, TaskFileEntry[]>();
        const runPathIndexById = new Map<string, Map<string, number>>();

        sourceMessages.forEach((message) => {
            if (message.type !== 'tool') return;

            const metadata = safeJsonParse<ParsedMetadata | string>(message.metadata, {});
            let runId: string | undefined;

            if (metadata && typeof metadata === 'object') {
                if (typeof metadata.thread_run_id === 'string') {
                    runId = metadata.thread_run_id;
                } else if (typeof metadata.assistant_message_id === 'string') {
                    runId = assistantRunByMessageId.get(metadata.assistant_message_id);
                }
            }

            if (!runId) return;

            const parsedContent = safeJsonParse<Record<string, any> | string>(message.content, {});
            if (!parsedContent || typeof parsedContent !== 'object') return;

            const toolNameRaw = typeof parsedContent.tool_name === 'string' ? parsedContent.tool_name : '';
            const normalizedToolName = normalizeToolName(toolNameRaw);
            if (!TASK_FILE_TOOL_NAMES.has(normalizedToolName)) return;

            const result = parsedContent.result;
            const rawPath =
                typeof result?.path === 'string'
                    ? result.path
                    : typeof parsedContent.path === 'string'
                        ? parsedContent.path
                        : '';

            if (!rawPath) return;

            const normalizedPath = normalizeWorkspacePath(rawPath);
            if (!normalizedPath || normalizedPath.endsWith('/')) return;

            const bytes = coerceBytes(result?.bytes);

            if (!runFilesById.has(runId)) {
                runFilesById.set(runId, []);
                runPathIndexById.set(runId, new Map());
            }

            const fileList = runFilesById.get(runId)!;
            const pathIndex = runPathIndexById.get(runId)!;
            const existingIndex = pathIndex.get(normalizedPath);

            if (existingIndex === undefined) {
                pathIndex.set(normalizedPath, fileList.length);
                fileList.push({ path: normalizedPath, bytes });
            } else if (bytes !== undefined && fileList[existingIndex].bytes === undefined) {
                fileList[existingIndex].bytes = bytes;
            }
        });

        return { runFilesById, assistantRunByMessageId, messageFileArtifactsById };
    }, [messages, readOnly, visibleMessages]);

    const isActiveRunStreaming =
        (!readOnly &&
            (agentStatus === 'running' ||
                agentStatus === 'connecting' ||
                agentStatus === 'disconnecting' ||
                streamHookStatus === 'streaming' ||
                streamHookStatus === 'connecting')) ||
        (readOnly && isStreamingText);
    
    // Helper function to get agent info robustly
    const agentInfo = useMemo(() => {
        const agentBuilderName = t('agent.builder');
        if (threadMetadata?.is_agent_builder) {
            return {
                name: agentBuilderName,
                avatar: (
                    <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                        <span className="text-lg">🤖</span>
                    </div>
                )
            };
        }

        const isSunaDefaultAgent = agentMetadata?.is_suna_default || false;

        const recentAssistantWithAgent = [...displayMessages].reverse().find(msg =>
            msg.type === 'assistant' && msg.agents?.name
        );

        if (recentAssistantWithAgent?.agents?.name === 'Agent Builder') {
            return {
                name: agentBuilderName,
                avatar: (
                    <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                        <span className="text-lg">🤖</span>
                    </div>
                )
            };
        }

        if (agentData && !isSunaDefaultAgent) {
            const profileUrl = agentData.profile_image_url;
            const avatar = profileUrl ? (
                <img src={profileUrl} alt={agentData.name || agentName} className="h-5 w-5 rounded object-cover" />
            ) : agentData.avatar ? (
                <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                    <span className="text-lg">{agentData.avatar}</span>
                </div>
            ) : (
                <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                    <KortixLogo size={16} />
                </div>
            );
            return {
                name: agentData.name || agentName,
                avatar
            };
        }

        if (recentAssistantWithAgent?.agents?.name) {
            const isSunaAgent = recentAssistantWithAgent.agents.name === 'Suna' || isSunaDefaultAgent;
            const resolvedAgentName = recentAssistantWithAgent.agents.name === 'Suna'
                ? (agentName && agentName !== 'Suna' ? agentName : t('agent.default'))
                : recentAssistantWithAgent.agents.name;
            // Prefer profile image if available on the agent payload
            const profileUrl = (recentAssistantWithAgent as any)?.agents?.profile_image_url;
            const avatar = profileUrl && !isSunaDefaultAgent ? (
                <img src={profileUrl} alt={resolvedAgentName} className="h-5 w-5 rounded object-cover" />
            ) : !isSunaDefaultAgent ? (
                <>
                    {isSunaAgent ? (
                        <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                            <KortixLogo size={16} />
                        </div>
                    ) : (
                        <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                            <span className="text-lg">{recentAssistantWithAgent.agents.name.charAt(0).toUpperCase()}</span>
                        </div>
                    )}
                </>
            ) : (
                <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                    <KortixLogo size={16} />
                </div>
            );
            return {
                name: resolvedAgentName,
                avatar
            };
        }

        if (isSunaDefaultAgent) {
            return {
                name: agentName && agentName !== 'Suna' ? agentName : t('agent.default'),
                avatar: (
                    <div className="h-5 w-5 flex items-center justify-center rounded text-xs">
                        <KortixLogo size={16} />
                    </div>
                )
            };
        }

        return {
            name: agentName || 'Roys Alpha',
            avatar: agentAvatar
        };
    }, [threadMetadata, displayMessages, agentName, agentAvatar, agentMetadata, agentData, t]);

    // Simplified scroll handler - flex-column-reverse handles positioning
    const handleScroll = useCallback(() => {
        // No scroll logic needed with flex-column-reverse
    }, []);

    // No scroll-to-bottom needed with flex-column-reverse

    // No auto-scroll needed with flex-column-reverse - CSS handles it

    // Smart justify-content based on content height
    useEffect(() => {
        const checkContentHeight = () => {
            const container = (scrollContainerRef || messagesContainerRef).current;
            const content = contentRef.current;
            if (!container || !content) return;

            const containerHeight = container.clientHeight;
            const contentHeight = content.scrollHeight;
            setShouldJustifyToTop(contentHeight <= containerHeight);
        };

        checkContentHeight();
        const resizeObserver = new ResizeObserver(checkContentHeight);
        if (contentRef.current) resizeObserver.observe(contentRef.current);
        const containerRef = (scrollContainerRef || messagesContainerRef).current;
        if (containerRef) resizeObserver.observe(containerRef);

        return () => resizeObserver.disconnect();
    }, [displayMessages, streamingTextContent, agentStatus, scrollContainerRef]);

    // Preload all message attachments when messages change or sandboxId is provided
    React.useEffect(() => {
        if (!sandboxId) return;

        // Extract all file attachments from messages
        const allAttachments: string[] = [];

        displayMessages.forEach(message => {
            if (message.type === 'user') {
                try {
                    const content = typeof message.content === 'string' ? message.content : '';
                    const attachmentsMatch = content.match(/\[Uploaded File: (.*?)\]/g);
                    if (attachmentsMatch) {
                        attachmentsMatch.forEach(match => {
                            const pathMatch = match.match(/\[Uploaded File: (.*?)\]/);
                            if (pathMatch && pathMatch[1]) {
                                allAttachments.push(pathMatch[1]);
                            }
                        });
                    }
                } catch (e) {
                    console.error('Error parsing message attachments:', e);
                }
            }
        });

        // Use React Query preloading if we have attachments AND a valid token
        if (allAttachments.length > 0 && session?.access_token) {
            // Preload files with React Query in background
            preloadFiles(sandboxId, allAttachments).catch(err => {
                console.error('React Query preload failed:', err);
            });
        }
    }, [displayMessages, sandboxId, session?.access_token, preloadFiles]);

    const shouldShowEmpty = displayMessages.length === 0 && !streamingTextContent && !streamingToolCall &&
        !streamingText && !currentToolCall && agentStatus === 'idle';
    const { finalGroupedMessages, groupRunIdByIndex, lastGroupIndexByRun } = useMemo(
        () =>
            buildGroupedMessagesState({
                displayMessages,
                readOnly,
                streamingTextContent,
                streamingReasoningContent,
                streamingToolCall,
                streamHookStatus,
                streamingText,
                isStreamingText,
                assistantRunByMessageId,
            }),
        [
            assistantRunByMessageId,
            displayMessages,
            isStreamingText,
            readOnly,
            streamHookStatus,
            streamingReasoningContent,
            streamingText,
            streamingTextContent,
            streamingToolCall,
        ],
    );

    return (
        <>
            {shouldShowEmpty ? (
                // Render empty state outside scrollable container
                <div className="flex-1 min-h-[60vh] flex items-center justify-center bg-card/80 dark:bg-card/90 backdrop-blur supports-[backdrop-filter]:bg-card/60 dark:bg-gradient-to-b dark:from-card/80 dark:via-card/90 dark:to-muted/40">
                    {emptyStateComponent || (
                        <div className="text-center text-muted-foreground">
                            {readOnly ? "No messages to display." : "Send a message to start."}
                        </div>
                    )}
                </div>
            ) : (
                // Render scrollable content container with column-reverse
                <div
                    ref={scrollContainerRef || messagesContainerRef}
                    className={`${containerClassName} flex flex-col-reverse ${shouldJustifyToTop ? 'justify-end min-h-full' : ''}`}
                    onScroll={handleScroll}
                >
                    <div ref={contentRef} className="mx-auto min-w-0 w-full max-w-3xl px-4 pb-44 md:px-6">
                        <div className="space-y-8 min-w-0">
                            {finalGroupedMessages.map((group, groupIndex) => {
                                    if (group.type === 'user') {
                                        const message = group.messages[0];
                                        const messageContent = (() => {
                                            try {
                                                const parsed = safeJsonParse<ParsedContent>(message.content, { content: message.content });
                                                return parsed.content || message.content;
                                            } catch {
                                                return message.content;
                                            }
                                        })();

                                        // In debug mode, display raw message content
                                        if (debugMode) {
                                            return (
                                                <div key={group.key} className="flex justify-end">
                                                    <div className="flex max-w-[85%] rounded-2xl bg-card px-4 py-3 break-words overflow-hidden">
                                                        <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto min-w-0 flex-1">
                                                            {message.content}
                                                        </pre>
                                                    </div>
                                                </div>
                                            );
                                        }

                                        // Extract attachments from the message content
                                        const attachmentsMatch = messageContent.match(/\[Uploaded File: (.*?)\]/g);
                                        const attachments = attachmentsMatch
                                            ? attachmentsMatch.map((match: string) => {
                                                const pathMatch = match.match(/\[Uploaded File: (.*?)\]/);
                                                return pathMatch ? pathMatch[1] : null;
                                            }).filter(Boolean)
                                            : [];

                                        // Remove attachment info from the message content
                                        const cleanContent = messageContent.replace(/\[Uploaded File: .*?\]/g, '').trim();

                                        const userJSX = (
                                            <div
                                                key={group.key}
                                                className="flex justify-end"
                                                data-testid="thread-user-message-group"
                                            >
                                                <div className="flex max-w-[85%] rounded-3xl rounded-br-lg bg-card border px-4 py-3 break-words overflow-hidden">
                                                    <div className="space-y-3 min-w-0 flex-1">
                                                        {cleanContent && (
                                                            <ComposioUrlDetector content={cleanContent} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none [&>:first-child]:mt-0 prose-headings:mt-3 break-words overflow-wrap-anywhere" />
                                                        )}

                                                        {/* Use the helper function to render user attachments */}
                                                        {renderAttachments(attachments as string[], handleOpenFileViewer, sandboxId, project)}
                                                    </div>
                                                </div>
                                            </div>
                                        );
                                        
                                        return userJSX;
                                    } else if (group.type === 'assistant_group') {
                                        // Get agent_id from the first assistant message in this group
                                        const firstAssistantMsg = group.messages.find(m => m.type === 'assistant');
                                        const groupAgentId = firstAssistantMsg?.agent_id;
                                        const groupRunId = groupRunIdByIndex.get(groupIndex);
                                        const taskFiles = groupRunId ? runFilesById.get(groupRunId) : undefined;
                                        const workspaceFileCount = workspaceFiles.length;
                                        const shouldPreferWorkspaceFiles =
                                            workspaceFileCount > 0 &&
                                            (workspaceFileCount <= 5 || !taskFiles || taskFiles.length === 0);
                                        const summaryFiles = shouldPreferWorkspaceFiles
                                            ? workspaceFiles
                                            : taskFiles;
                                        const summaryTitle = shouldPreferWorkspaceFiles
                                            ? 'Workspace files'
                                            : 'Files created';
                                        const viewAllLabel = shouldPreferWorkspaceFiles
                                            ? 'View all files in workspace'
                                            : 'View all files in this task';
                                        const isLastGroupForRun = groupRunId
                                            ? lastGroupIndexByRun.get(groupRunId) === groupIndex
                                            : false;
                                        const isLastGroup = groupIndex === finalGroupedMessages.length - 1;
                                        const suppressTaskFiles = isActiveRunStreaming && isLastGroup;
                                        const shouldShowTaskFiles = Boolean(
                                            groupRunId &&
                                                isLastGroupForRun &&
                                                summaryFiles &&
                                                summaryFiles.length > 0 &&
                                                !suppressTaskFiles,
                                        );
                                        

                                        
                                        const assistantJSX = (
                                            <div
                                                key={group.key}
                                                ref={groupIndex === finalGroupedMessages.length - 1 ? latestMessageRef : null}
                                                data-testid="thread-assistant-message-group"
                                            >
                                                <div className="flex flex-col gap-2">
                                                    <div className="flex items-center">
                                                        <div className="rounded-md flex items-center justify-center relative">
                                                            {groupAgentId ? (
                                                                <AgentAvatar agentId={groupAgentId} size={20} className="h-5 w-5" />
                                                            ) : (
                                                                agentInfo.avatar
                                                            )}
                                                        </div>
                                                        <p className='ml-2 text-sm text-muted-foreground'>
                                                            {groupAgentId ? (
                                                                <AgentName agentId={groupAgentId} fallback={agentInfo.name} />
                                                            ) : (
                                                                agentInfo.name
                                                            )}
                                                        </p>
                                                    </div>

                                                    {/* Message content - ALL messages in the group */}
                                                    <div className="flex max-w-[90%] text-sm break-words overflow-hidden">
                                                        <div className="space-y-2 min-w-0 flex-1">
                                                            {(() => {
                                                                // In debug mode, just show raw messages content
                                                                if (debugMode) {
                                                                    return group.messages.map((message, msgIndex) => {
                                                                        const msgKey = message.message_id || `raw-msg-${msgIndex}`;
                                                                        return (
                                                                            <div key={msgKey} className="mb-4">
                                                                                <div className="text-xs font-medium text-muted-foreground mb-1">
                                                                                    Type: {message.type} | ID: {message.message_id || 'no-id'}
                                                                                </div>
                                                                                <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto p-2 border border-border rounded-md bg-muted/30">
                                                                                    {JSON.stringify(message.content, null, 2)}
                                                                                </pre>
                                                                                {message.metadata && message.metadata !== '{}' && (
                                                                                    <div className="mt-2">
                                                                                        <div className="text-xs font-medium text-muted-foreground mb-1">
                                                                                            Metadata:
                                                                                        </div>
                                                                                        <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto p-2 border border-border rounded-md bg-muted/30">
                                                                                            {JSON.stringify(message.metadata, null, 2)}
                                                                                        </pre>
                                                                                    </div>
                                                                                )}
                                                                            </div>
                                                                        );
                                                                    });
                                                                }

                                                                const toolResultsMap = new Map<string | null, UnifiedMessage[]>();
                                                                group.messages.forEach(msg => {
                                                                    if (msg.type === 'tool') {
                                                                        const meta = safeJsonParse<ParsedMetadata>(msg.metadata, {});
                                                                        const assistantId = meta.assistant_message_id || null;
                                                                        if (!toolResultsMap.has(assistantId)) {
                                                                            toolResultsMap.set(assistantId, []);
                                                                        }
                                                                        toolResultsMap.get(assistantId)?.push(msg);
                                                                    }
                                                                });

                                                                const elements: React.ReactNode[] = [];
                                                                let assistantMessageCount = 0; // Move this outside the loop

                                                                group.messages.forEach((message, msgIndex) => {
                                                                    // Phase E (defense-in-depth): handle subagent_activity and tool messages
                                                                    // that may contain tool call info, in addition to assistant messages
                                                                    if (message.type === 'assistant' || message.type === 'subagent_activity' || message.type === 'tool') {
                                                                        const parsedContent = safeJsonParse<ParsedContent>(message.content, {});
                                                                        const msgKey = message.message_id || `submsg-assistant-${msgIndex}`;

                                                                        // 🔧 检查是否包含 tool_calls
                                                                        const hasToolCalls = parsedContent.tool_calls && Array.isArray(parsedContent.tool_calls) && parsedContent.tool_calls.length > 0;
                                                                        const hasTextContent = parsedContent.content && parsedContent.content.trim() !== '';

                                                                        // 如果只有 tool_calls 没有文本内容，渲染工具调用卡片
                                                                        if (hasToolCalls && !hasTextContent) {
                                                                            // 为每个工具调用渲染卡片
                                                                            parsedContent.tool_calls.forEach((toolCall: any, toolIndex: number) => {
                                                                                const toolName = toolCall.function?.name || toolCall.name || 'unknown-tool';
                                                                                const toolArgs = toolCall.function?.arguments || toolCall.arguments || '{}';
                                                                                
                                                                                // 尝试解析参数获取显示信息
                                                                                let paramDisplay = '';
                                                                                try {
                                                                                    const args = typeof toolArgs === 'string' ? JSON.parse(toolArgs) : toolArgs;
                                                                                    // 提取第一个参数作为预览
                                                                                    const firstKey = Object.keys(args)[0];
                                                                                    if (firstKey && args[firstKey]) {
                                                                                        const val = String(args[firstKey]);
                                                                                        paramDisplay = val.length > 30 ? val.substring(0, 30) + '...' : val;
                                                                                    }
                                                                                } catch (e) {
                                                                                    // 参数解析失败，忽略
                                                                                }
                                                                                
                                                                                const IconComponent = getToolIcon(toolName);
                                                                                
                                                                                elements.push(
                                                                                    <div
                                                                                        key={`${msgKey}-tool-${toolIndex}`}
                                                                                        className={assistantMessageCount > 0 ? "mt-2" : ""}
                                                                                    >
                                                                                        <button
                                                                                            onClick={() => handleToolClick(message.message_id, toolName)}
                                                                                            className="inline-flex items-center gap-1.5 py-1 px-1 pr-1.5 text-xs text-muted-foreground bg-muted hover:bg-muted/80 rounded-lg transition-colors cursor-pointer border border-neutral-200 dark:border-neutral-700/50"
                                                                                        >
                                                                                            <div className='border-2 bg-gradient-to-br from-neutral-200 to-neutral-300 dark:from-neutral-700 dark:to-neutral-800 flex items-center justify-center p-0.5 rounded-sm border-neutral-400/20 dark:border-neutral-600'>
                                                                                                <IconComponent className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
                                                                                            </div>
                                                                                            <span className="font-mono text-xs text-foreground">{getUserFriendlyToolName(toolName)}</span>
                                                                                            {paramDisplay && <span className="ml-1 text-muted-foreground truncate max-w-[200px]" title={paramDisplay}>{paramDisplay}</span>}
                                                                                        </button>
                                                                                    </div>
                                                                                );
                                                                                
                                                                                assistantMessageCount++;
                                                                            });
                                                                            return; // 跳过后续的文本渲染
                                                                        }

                                                                        // 区分流式消息和历史消息的处理逻辑
                                                                        let finalContent;
                                                                        if (msgKey.includes('streaming') || msgKey.includes('playback')) {
                                                                            // 流式消息 may be a plain text chunk rather than a JSON
                                                                            // {content} payload. Keep the synthetic assistant message
                                                                            // visible even if the external stream status lags behind.
                                                                            finalContent = parsedContent.content || message.content;
                                                                        } else {
                                                                            // 历史消息：只使用解析后的 content，不回退到原始 JSON
                                                                            finalContent = parsedContent.content;
                                                                        }

                                                                        // 渲染思考/推理内容 (如果存在)
                                                                        const reasoningText = parsedContent.reasoning_content;
                                                                        if (reasoningText && reasoningText.trim()) {
                                                                            elements.push(
                                                                                <div key={`${msgKey}-reasoning`} className={assistantMessageCount > 0 ? "mt-4 mb-3" : "mb-3"}>
                                                                                    <Reasoning>
                                                                                        <ReasoningTrigger className="text-xs text-muted-foreground/70 hover:text-muted-foreground">
                                                                                            <span className="flex items-center gap-1.5">
                                                                                                <Brain className="h-3.5 w-3.5" />
                                                                                                思考过程
                                                                                            </span>
                                                                                        </ReasoningTrigger>
                                                                                        <ReasoningContent className="mt-2">
                                                                                            <div className="text-sm text-muted-foreground/60 prose prose-sm dark:prose-invert bg-muted/30 rounded-md p-3 border border-border/50">
                                                                                                <Markdown>{reasoningText}</Markdown>
                                                                                            </div>
                                                                                        </ReasoningContent>
                                                                                    </Reasoning>
                                                                                </div>
                                                                            );
                                                                        }

                                                                        if (!finalContent || finalContent.trim() === '') return;

                                                                        const renderedContent = renderMarkdownContent(
                                                                            finalContent,
                                                                            handleToolClick,
                                                                            message.message_id,
                                                                            handleOpenFileViewer,
                                                                            sandboxId,
                                                                            project,
                                                                            debugMode
                                                                        );
                                                                        const shadowCloneFileArtifacts =
                                                                            message.message_id
                                                                                ? messageFileArtifactsById.get(message.message_id) || []
                                                                                : [];

                                                                        elements.push(
                                                                            <div key={msgKey} className={assistantMessageCount > 0 ? "mt-4" : ""}>
                                                                                <div className="prose prose-sm dark:prose-invert chat-markdown max-w-none [&>:first-child]:mt-0 prose-headings:mt-3 break-words overflow-hidden">
                                                                                    {renderedContent}
                                                                                </div>
                                                                                {shadowCloneFileArtifacts.length > 0 && (
                                                                                    <TaskFilesSummary
                                                                                        files={shadowCloneFileArtifacts}
                                                                                        onOpenFileViewer={handleOpenFileViewer}
                                                                                        title="Files created"
                                                                                        viewAllLabel="View all files in this task"
                                                                                    />
                                                                                )}
                                                                            </div>
                                                                        );

                                                                        assistantMessageCount++; // Increment after adding the element
                                                                    }
                                                                });

                                                                return elements;
                                                            })()}

                                                            {groupIndex === finalGroupedMessages.length - 1 &&
                                                              !readOnly &&
                                                              (
                                                                streamHookStatus === 'streaming' ||
                                                                streamHookStatus === 'connecting' ||
                                                                (streamHookStatus === 'completed' && Boolean(streamingReasoningContent))
                                                              ) && (
                                                                <div className="mt-2">
                                                                    {/* 流式思考/推理内容显示 */}
                                                                    {streamingReasoningContent && (
                                                                        <div className="mb-3">
                                                                            <Reasoning open={true}>
                                                                                <ReasoningTrigger className="text-xs text-muted-foreground/70 hover:text-muted-foreground">
                                                                                    <span className="flex items-center gap-1.5">
                                                                                        <Brain className={`h-3.5 w-3.5 ${streamHookStatus === 'completed' ? '' : 'animate-pulse'}`} />
                                                                                        {streamHookStatus === 'completed' ? '思考完成' : '正在思考...'}
                                                                                    </span>
                                                                                </ReasoningTrigger>
                                                                                <ReasoningContent className="mt-2">
                                                                                    <div className="text-sm text-muted-foreground/60 prose prose-sm dark:prose-invert bg-muted/30 rounded-md p-3 border border-border/50">
                                                                                        <Markdown>{streamingReasoningContent}</Markdown>
                                                                                        {/* 流式光标动画 */}
                                                                                        {(streamHookStatus === 'streaming' || streamHookStatus === 'connecting') && (
                                                                                            <span className="inline-block h-4 w-0.5 bg-muted-foreground/40 ml-0.5 -mb-1 animate-pulse" />
                                                                                        )}
                                                                                    </div>
                                                                                </ReasoningContent>
                                                                            </Reasoning>
                                                                        </div>
                                                                    )}
                                                                    {streamingToolCall && (streamHookStatus === 'streaming' || streamHookStatus === 'connecting') && (
                                                                        <div className="mb-3">
                                                                            <div className="animate-shimmer inline-flex items-center gap-1.5 py-1.5 px-3 text-xs font-medium text-muted-foreground bg-muted/40 rounded-md border border-border/60">
                                                                                <CircleDashed className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0 animate-spin animation-duration-2000" />
                                                                                <span className="font-mono text-xs text-muted-foreground">
                                                                                    {getUserFriendlyToolName(streamingToolCall.name || 'Using Tool')}
                                                                                </span>
                                                                            </div>
                                                                        </div>
                                                                    )}
                                                                    {(() => {
                                                                        // In debug mode, show raw streaming content
                                                                        if (debugMode && streamingTextContent) {
                                                                            return (
                                                                                <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto p-2 border border-border rounded-md bg-muted/30">
                                                                                    {streamingTextContent}
                                                                                </pre>
                                                                            );
                                                                        }

                                                                        let detectedTag: string | null = null;
                                                                        let tagStartIndex = -1;
                                                                        if (streamingTextContent) {
                                                                            // First check for new format
                                                                            const functionCallsIndex = streamingTextContent.indexOf('<function_calls>');
                                                                            if (functionCallsIndex !== -1) {
                                                                                detectedTag = 'function_calls';
                                                                                tagStartIndex = functionCallsIndex;
                                                                            } else {
                                                                                // Fall back to old format detection
                                                                                for (const tag of HIDE_STREAMING_XML_TAGS) {
                                                                                    const openingTagPattern = `<${tag}`;
                                                                                    const index = streamingTextContent.indexOf(openingTagPattern);
                                                                                    if (index !== -1) {
                                                                                        detectedTag = tag;
                                                                                        tagStartIndex = index;
                                                                                        break;
                                                                                    }
                                                                                }
                                                                            }
                                                                        }


                                                                        const textToRender = streamingTextContent || '';
                                                                        const textBeforeTag = detectedTag ? textToRender.substring(0, tagStartIndex) : textToRender;
                                                                        const showCursor =
                                                                          (streamHookStatus ===
                                                                            'streaming' ||
                                                                            streamHookStatus ===
                                                                              'connecting') &&
                                                                          !detectedTag;

                                                                        return (
                                                                            <>
                                                                                {textBeforeTag && (
                                                                                    <ComposioUrlDetector content={textBeforeTag} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none [&>:first-child]:mt-0 prose-headings:mt-3 break-words overflow-wrap-anywhere" />
                                                                                )}
                                                                                {showCursor && (
                                                                                    <span className="inline-block h-4 w-0.5 bg-primary ml-0.5 -mb-1 animate-pulse" />
                                                                                )}

                                                                                {detectedTag && (
                                                                                    <ShowToolStream
                                                                                        content={textToRender.substring(tagStartIndex)}
                                                                                        messageId={visibleMessages && visibleMessages.length > 0 ? visibleMessages[visibleMessages.length - 1].message_id : "playback-streaming"}
                                                                                        onToolClick={handleToolClick}
                                                                                        showExpanded={true}
                                                                                        startTime={Date.now()}
                                                                                    />
                                                                                )}


                                                                            </>
                                                                        );
                                                                    })()}
                                                                </div>
                                                            )}

                                                            {/* For playback mode, show streaming text and tool calls */}
                                                            {readOnly && groupIndex === finalGroupedMessages.length - 1 && isStreamingText && (
                                                                <div className="mt-2">
                                                                    {(() => {
                                                                        let detectedTag: string | null = null;
                                                                        let tagStartIndex = -1;
                                                                        if (streamingText) {
                                                                            // First check for new format
                                                                            const functionCallsIndex = streamingText.indexOf('<function_calls>');
                                                                            if (functionCallsIndex !== -1) {
                                                                                detectedTag = 'function_calls';
                                                                                tagStartIndex = functionCallsIndex;
                                                                            } else {
                                                                                // Fall back to old format detection
                                                                                for (const tag of HIDE_STREAMING_XML_TAGS) {
                                                                                    const openingTagPattern = `<${tag}`;
                                                                                    const index = streamingText.indexOf(openingTagPattern);
                                                                                    if (index !== -1) {
                                                                                        detectedTag = tag;
                                                                                        tagStartIndex = index;
                                                                                        break;
                                                                                    }
                                                                                }
                                                                            }
                                                                        }

                                                                        const textToRender = streamingText || '';
                                                                        const textBeforeTag = detectedTag ? textToRender.substring(0, tagStartIndex) : textToRender;
                                                                        const showCursor = isStreamingText && !detectedTag;

                                                                        return (
                                                                            <>
                                                                                {/* In debug mode, show raw streaming content */}
                                                                                {debugMode && streamingText ? (
                                                                                    <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto p-2 border border-border rounded-md bg-muted/30">
                                                                                        {streamingText}
                                                                                    </pre>
                                                                                ) : (
                                                                                    <>
                                                                                        {textBeforeTag && (
                                                                                            <ComposioUrlDetector content={textBeforeTag} className="text-sm prose prose-sm dark:prose-invert chat-markdown max-w-none [&>:first-child]:mt-0 prose-headings:mt-3 break-words overflow-wrap-anywhere" />
                                                                                        )}
                                                                                        {showCursor && (
                                                                                            <span className="inline-block h-4 w-0.5 bg-primary ml-0.5 -mb-1 animate-pulse" />
                                                                                        )}

                                                                                        {detectedTag && (
                                                                                            <ShowToolStream
                                                                                                content={textToRender.substring(tagStartIndex)}
                                                                                                messageId="streamingTextContent"
                                                                                                onToolClick={handleToolClick}
                                                                                                showExpanded={true}
                                                                                                startTime={Date.now()} // Tool just started now
                                                                                            />
                                                                                        )}
                                                                                    </>
                                                                                )}
                                                                            </>
                                                                        );
                                                                    })()}
                                                                </div>
                                                            )}

                                                            {shouldShowTaskFiles && summaryFiles && (
                                                                <TaskFilesSummary
                                                                    files={summaryFiles}
                                                                    onOpenFileViewer={handleOpenFileViewer}
                                                                    title={summaryTitle}
                                                                    viewAllLabel={viewAllLabel}
                                                                />
                                                            )}
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        );
                                        
                                        return assistantJSX;
                                    }
                                    return null;
                                })}
                            {(!readOnly &&
                                !streamingTextContent &&
                                !suppressAgentActivity &&
                                (showPendingAssistantLoader ||
                                  ((agentStatus === 'running' || agentStatus === 'connecting' || agentStatus === 'disconnecting') &&
                                    (messages.length === 0 || messages[messages.length - 1].type === 'user'))) &&
                                (
                                    <div ref={latestMessageRef} className='w-full h-22 rounded'>
                                        <div className="flex flex-col gap-2">
                                            {/* Logo positioned above the loader */}
                                            <div className="flex items-center">
                                                <div className="rounded-md flex items-center justify-center">
                                                    {agentInfo.avatar}
                                                </div>
                                                <p className='ml-2 text-sm text-muted-foreground'>
                                                    {agentInfo.name}
                                                </p>
                                            </div>

                                            {/* Loader content */}
                                            <div className="space-y-2 w-full h-12">
                                                <AgentLoader />
                                            </div>
                                        </div>
                                    </div>
                                ))}
                            {/* 📝 File Writing Indicator — isWritingFile is NOT gated on suppressAgentActivity per SCV2 parity spec.
                                Each panel independently manages its own indicator; right-panel state must not suppress the left-panel blue bar. */}
                            {!readOnly && (
                              <>
                                {/* Blue bar: always shown when agent is working, regardless of panel state */}
                                {isWritingFile && (
                                    <div ref={latestMessageRef} className='w-full rounded'>
                                        <div className="flex flex-col gap-2">
                                            <div className="flex items-center">
                                                <div className="rounded-md flex items-center justify-center">
                                                    {agentInfo.avatar}
                                                </div>
                                                <p className='ml-2 text-sm text-muted-foreground'>
                                                    {agentInfo.name}
                                                </p>
                                            </div>
                                            <FileWritingIndicator />
                                        </div>
                                    </div>
                                )}
                                {/* Tool call indicator: respects suppression for message routing */}
                                {Boolean(streamingToolCall) && !suppressAgentActivity && (
                                    <div ref={latestMessageRef} className='w-full rounded'>
                                        <div className="flex flex-col gap-2">
                                            <div className="flex items-center">
                                                <div className="rounded-md flex items-center justify-center">
                                                    {agentInfo.avatar}
                                                </div>
                                                <p className='ml-2 text-sm text-muted-foreground'>
                                                    {agentInfo.name}
                                                </p>
                                            </div>
                                            <FileWritingIndicator />
                                        </div>
                                    </div>
                                )}
                              </>
                            )}
                            {readOnly && currentToolCall && (
                                <div ref={latestMessageRef}>
                                    <div className="flex flex-col gap-2">
                                        {/* Logo positioned above the tool call */}
                                        <div className="flex justify-start">
                                            <div className="rounded-md flex items-center justify-center">
                                                {agentInfo.avatar}
                                            </div>
                                            <p className='ml-2 text-sm text-muted-foreground'>
                                                {agentInfo.name}
                                            </p>
                                        </div>

                                        {/* Tool call content */}
                                        <div className="space-y-2">
                                            <div className="animate-shimmer inline-flex items-center gap-1.5 py-1.5 px-3 text-xs font-medium text-muted-foreground bg-muted/40 rounded-md border border-border/60">
                                                <CircleDashed className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0 animate-spin animation-duration-2000" />
                                                <span className="font-mono text-xs text-muted-foreground">
                                                    {currentToolCall.name || 'Using Tool'}
                                                </span>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* For playback mode - Show streaming indicator if no messages yet */}
                            {readOnly && visibleMessages && visibleMessages.length === 0 && isStreamingText && (
                                <div ref={latestMessageRef}>
                                    <div className="flex flex-col gap-2">
                                        {/* Logo positioned above the streaming indicator */}
                                        <div className="flex justify-start">
                                            <div className="rounded-md flex items-center justify-center">
                                                {agentInfo.avatar}
                                            </div>
                                            <p className='ml-2 text-sm text-muted-foreground'>
                                                {agentInfo.name}
                                            </p>
                                        </div>

                                        {/* Streaming indicator content */}
                                        <div className="max-w-[90%] px-4 py-3 text-sm">
                                            <div className="flex items-center gap-1.5 py-1">
                                                <div className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50 animate-pulse" />
                                                <div className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50 animate-pulse delay-150" />
                                                <div className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50 animate-pulse delay-300" />
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            )}
                            <div className="!h-48" />
                        </div>
                    </div>
                </div>
            )}

            {/* No scroll button needed with flex-column-reverse */}
        </>
    );
};

export default ThreadContent; 
