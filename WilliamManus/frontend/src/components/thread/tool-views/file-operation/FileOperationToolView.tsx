import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import {
  ExternalLink,
  Loader2,
  Code,
  Eye,
  File,
  Copy,
  Check,
} from 'lucide-react';
import {
  extractFilePath,
  extractFileContent,
  extractStreamingFileContent,
  formatTimestamp,
  getToolTitle,
  extractToolData,
} from '../utils';
import {
  MarkdownRenderer,
  processUnicodeContent,
} from '@/components/file-renderers/markdown-renderer';
import { CsvRenderer } from '@/components/file-renderers/csv-renderer';
import { cn } from '@/lib/utils';
import { CodeBlockCode } from '@/components/ui/code-block';
import { constructHtmlPreviewUrl } from '@/lib/utils/url';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";

import {
  getLanguageFromFileName,
  getOperationType,
  getOperationConfigs,
  getFileIcon,
  processFilePath,
  getFileName,
  getFileExtension,
  isFileType,
  hasLanguageHighlighting,
  splitContentIntoLines,
  extractPartialFileContent,
} from './_utils';
import { ToolViewProps } from '../types';
import { GenericToolView } from '../GenericToolView';
import { LoadingState } from '../shared/LoadingState';
import { toast } from 'sonner';
import { useAuth } from '@/components/AuthProvider';
import { fetchFileContent } from '@/hooks/react-query/files/use-file-queries';
import {
  mergeNormalizedWriteFileArgs,
  normalizeWriteFileArgs,
} from '@/lib/write-file-stream';

const SCROLL_VIEWPORT_SELECTOR = '[data-radix-scroll-area-viewport]';
const SCROLL_BOTTOM_THRESHOLD_PX = 24;

const getScrollViewport = (
  scrollContainer: HTMLDivElement | null,
): HTMLElement | null => {
  if (!scrollContainer) return null;
  return scrollContainer.querySelector(
    SCROLL_VIEWPORT_SELECTOR,
  ) as HTMLElement | null;
};

const isViewportNearBottom = (
  viewport: HTMLElement,
  thresholdPx = SCROLL_BOTTOM_THRESHOLD_PX,
): boolean => {
  const distanceFromBottom =
    viewport.scrollHeight - (viewport.scrollTop + viewport.clientHeight);
  return distanceFromBottom <= thresholdPx;
};

const scrollViewportToBottom = (viewport: HTMLElement): void => {
  viewport.scrollTo({
    top: viewport.scrollHeight,
    behavior: 'auto',
  });
};

const getViewportContentNode = (viewport: HTMLElement): HTMLElement | null => {
  const contentNode = viewport.firstElementChild;
  return contentNode instanceof HTMLElement ? contentNode : null;
};

export function FileOperationToolView({
  assistantContent,
  toolContent,
  assistantTimestamp,
  toolTimestamp,
  isSuccess = true,
  isStreaming = false,
  name,
  project,
  streamingText,
}: ToolViewProps) {
  // Add copy functionality state
  const [isCopyingContent, setIsCopyingContent] = useState(false);
  const [streamedFileContent, setStreamedFileContent] = useState<string | null>(null);
  const { session } = useAuth();

  // Refs for auto-scrolling during streaming
  const [sourceScrollContainer, setSourceScrollContainer] =
    useState<HTMLDivElement | null>(null);
  const [previewScrollContainer, setPreviewScrollContainer] =
    useState<HTMLDivElement | null>(null);
  const shouldAutoFollowSourceRef = useRef(true);
  const shouldAutoFollowPreviewRef = useRef(true);
  const hasUserScrolledSourceRef = useRef(false);
  const hasUserScrolledPreviewRef = useRef(false);

  const handleSourceScrollRef = useCallback((node: HTMLDivElement | null) => {
    setSourceScrollContainer(node);
  }, []);

  const handlePreviewScrollRef = useCallback((node: HTMLDivElement | null) => {
    setPreviewScrollContainer(node);
  }, []);

  // Copy functions
  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.error('Failed to copy text: ', err);
      return false;
    }
  };

  const handleCopyContent = async () => {
    if (!fileContent) return;

    setIsCopyingContent(true);
    const success = await copyToClipboard(fileContent);
    if (success) {
      toast.success('File content copied to clipboard');
    } else {
      toast.error('Failed to copy file content');
    }
    setTimeout(() => setIsCopyingContent(false), 500);
  };

  const operation = getOperationType(name, assistantContent);
  const configs = getOperationConfigs();
  const config = configs[operation];
  const Icon = config.icon;

  // Use useMemo to ensure extraction runs reactively when streamingText changes
  // This fixes the React timing issue where extraction ran before streamingText was set
  const { filePath: extractedFilePath, fileContent: extractedFileContent } = useMemo(() => {
    let extractedFilePath: string | null = null;
    let extractedFileContent: string | null = null;

    // First, try to extract from tool data
    const assistantToolData = extractToolData(assistantContent);
    const toolToolData = extractToolData(toolContent);

    if (assistantToolData.toolResult) {
      extractedFilePath = assistantToolData.filePath;
      extractedFileContent = assistantToolData.fileContent;
    } else if (toolToolData.toolResult) {
      extractedFilePath = toolToolData.filePath;
      extractedFileContent = toolToolData.fileContent;
    }

    if (!extractedFilePath) {
      extractedFilePath = extractFilePath(assistantContent);
    }

    const mergedWriteFileArgs =
      operation !== 'delete'
        ? mergeNormalizedWriteFileArgs(
            mergeNormalizedWriteFileArgs(assistantContent, toolContent),
            streamingText,
          )
        : null;

    if (mergedWriteFileArgs) {
      const mergedFilePath = mergedWriteFileArgs.file_path;
      const mergedFileContents =
        typeof mergedWriteFileArgs.file_contents === 'string'
          ? mergedWriteFileArgs.file_contents
          : mergedWriteFileArgs.file_contents_delta;
      const isSameFile =
        !extractedFilePath || !mergedFilePath || mergedFilePath === extractedFilePath;

      if (mergedFileContents !== undefined && mergedFileContents !== null && isSameFile) {
        if (
          typeof extractedFileContent !== 'string' ||
          mergedFileContents.length >= extractedFileContent.length
        ) {
          extractedFileContent = mergedFileContents;
        }
      }

      if (!extractedFilePath && mergedFilePath) {
        extractedFilePath = mergedFilePath;
      }
    }

    // PRIORITY 1: Extract file content from streaming JSON arguments (tool_call_chunk)
    // This provides real-time streaming display of file content as it's being written
    const shouldUseStreamingText =
      !!streamingText && operation !== 'delete' && (isStreaming || !extractedFileContent);
    if (shouldUseStreamingText) {
      console.log('📄 [FileOperationToolView] useMemo: Extracting from streamingText');
      const normalizedStreamingArgs = normalizeWriteFileArgs(streamingText);
      if (normalizedStreamingArgs) {
        const parsedFilePath = normalizedStreamingArgs.file_path;
        const parsedFileContents =
          normalizedStreamingArgs.file_contents !== undefined &&
          normalizedStreamingArgs.file_contents !== null
            ? normalizedStreamingArgs.file_contents
            : normalizedStreamingArgs.file_contents_delta;
        const isSameFile =
          !extractedFilePath || !parsedFilePath || parsedFilePath === extractedFilePath;

        if (parsedFileContents !== undefined && parsedFileContents !== null && isSameFile) {
          if (
            typeof extractedFileContent !== 'string' ||
            parsedFileContents.length >= extractedFileContent.length
          ) {
            extractedFileContent = parsedFileContents;
          }
        }
        if (!extractedFilePath && parsedFilePath) {
          extractedFilePath = parsedFilePath;
        }
      }

      try {
        const parsed =
          typeof streamingText === 'string' ? JSON.parse(streamingText) : streamingText;
        const parsedFilePath = parsed.file_path || parsed.path;
        const parsedFileContents =
          parsed.file_contents !== undefined && parsed.file_contents !== null
            ? parsed.file_contents
            : parsed.content ?? parsed.file_contents_delta;
        const isSameFile =
          !extractedFilePath || !parsedFilePath || parsedFilePath === extractedFilePath;

        console.log('📄 [FileOperationToolView] useMemo: Parsed streamingText:', {
          hasFileContents: parsed.file_contents !== undefined,
          fileContentsLength: parsed.file_contents?.length,
          hasContent: parsed.content !== undefined,
          filePath: parsed.file_path,
          isSameFile,
        });

        if (parsedFileContents !== undefined && parsedFileContents !== null && isSameFile) {
          if (
            typeof extractedFileContent !== 'string' ||
            parsedFileContents.length >= extractedFileContent.length
          ) {
            extractedFileContent = parsedFileContents;
          }
          console.log('✅ [FileOperationToolView] useMemo: Set fileContent from streamingText:', {
            contentLength: extractedFileContent?.length,
            contentPreview: extractedFileContent?.substring(0, 100),
          });
        } else if (!isSameFile) {
          console.log('⚠️ [FileOperationToolView] useMemo: Streaming text file_path mismatch, skipping', {
            extractedFilePath,
            parsedFilePath,
          });
        }

        if (!extractedFilePath && parsedFilePath) {
          extractedFilePath = parsedFilePath;
          console.log('✅ [FileOperationToolView] useMemo: Extracted file_path:', extractedFilePath);
        }
      } catch (e) {
        console.log('⚠️ [FileOperationToolView] useMemo: JSON parse failed, trying partial extraction:', e instanceof Error ? e.message : e);
        if (typeof streamingText === 'string') {
          // JSON incomplete - use regex extraction for partial JSON
          const partialContent = extractPartialFileContent(streamingText, operation);
          if (partialContent && (!extractedFileContent || extractedFileContent.length === 0)) {
            extractedFileContent = partialContent;
            console.log('✅ [FileOperationToolView] useMemo: Extracted partial content:', {
              contentLength: partialContent?.length,
            });
          }

          // Also try to extract file_path from partial JSON
          if (!extractedFilePath) {
            const partialArgs = normalizeWriteFileArgs(streamingText);
            if (partialArgs?.file_path) {
              extractedFilePath = partialArgs.file_path;
              console.log('✅ [FileOperationToolView] useMemo: Extracted file_path from partial:', extractedFilePath);
            }
          }
        }
      }
    }

    if (!extractedFilePath && typeof streamingText === 'string') {
      const workspacePathMatch = streamingText.match(/\/workspace\/[^\s"'`]+/i);
      if (workspacePathMatch) {
        extractedFilePath = workspacePathMatch[0];
        console.log('✅ [FileOperationToolView] useMemo: Extracted workspace path from streamingText:', extractedFilePath);
      }
    }

    // PRIORITY 2: Fall back to extracting from assistantContent (XML format or tool result)
    if (!extractedFileContent && operation !== 'delete') {
      extractedFileContent = isStreaming
        ? extractStreamingFileContent(
            assistantContent,
            operation === 'create' ? 'create-file' : operation === 'edit' ? 'edit-file' : 'full-file-rewrite',
          ) || ''
        : extractFileContent(
            assistantContent,
            operation === 'create' ? 'create-file' : operation === 'edit' ? 'edit-file' : 'full-file-rewrite',
          );
    }

    console.log('📄 [FileOperationToolView] useMemo: Final extraction result:', {
      hasFilePath: !!extractedFilePath,
      hasFileContent: !!extractedFileContent,
      fileContentLength: extractedFileContent?.length,
      isStreaming,
      hasStreamingText: !!streamingText,
    });

    return { filePath: extractedFilePath, fileContent: extractedFileContent };
  }, [isStreaming, streamingText, assistantContent, toolContent, operation]);

  useEffect(() => {
    setStreamedFileContent(null);
  }, [extractedFilePath]);

  const filePath = extractedFilePath;
  const hasExtractedContent =
    typeof extractedFileContent === 'string' && extractedFileContent.length > 0;
  const fileContent = streamedFileContent ?? extractedFileContent;

  // Debug log for render
  if (isStreaming) {
    console.log('📄 [FileOperationToolView] Render:', {
      isStreaming,
      hasStreamingText: !!streamingText,
      hasFileContent: !!fileContent,
      fileContentLength: fileContent?.length,
    });
  }

  useEffect(() => {
    const sandboxId = project?.sandbox?.id;
    if (!isStreaming || !filePath || !sandboxId || !session?.access_token) return;
    if (hasExtractedContent) return;

    let cancelled = false;
    const poll = async () => {
      try {
        const content = await fetchFileContent(
          sandboxId,
          filePath,
          'text',
          session.access_token,
        );
        if (!cancelled && typeof content === 'string') {
          setStreamedFileContent((prev) => (prev === content ? prev : content));
        }
      } catch (error) {
        // Ignore transient errors during streaming writes
      }
    };

    poll();
    const interval = setInterval(poll, 1000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [isStreaming, filePath, project?.sandbox?.id, session?.access_token, hasExtractedContent]);

  const toolTitle = getToolTitle(name || `file-${operation}`);
  const processedFilePath = processFilePath(filePath);
  const fileName = getFileName(processedFilePath);
  const fileExtension = getFileExtension(fileName);

  const isMarkdown = isFileType.markdown(fileExtension);
  const isHtml = isFileType.html(fileExtension);
  const isCsv = isFileType.csv(fileExtension);

  const language = getLanguageFromFileName(fileName);
  const hasHighlighting = hasLanguageHighlighting(language);
  const contentLines = splitContentIntoLines(fileContent);

  const htmlPreviewUrl =
    isHtml && project?.sandbox?.sandbox_url && processedFilePath
      ? constructHtmlPreviewUrl(project.sandbox.sandbox_url, processedFilePath)
      : undefined;

  const FileIcon = getFileIcon(fileName);

  useEffect(() => {
    const viewport = getScrollViewport(sourceScrollContainer);
    if (!viewport) return;

    const updateAutoFollow = () => {
      hasUserScrolledSourceRef.current = true;
      shouldAutoFollowSourceRef.current = isViewportNearBottom(viewport);
    };

    viewport.addEventListener('scroll', updateAutoFollow, { passive: true });

    return () => {
      viewport.removeEventListener('scroll', updateAutoFollow);
    };
  }, [sourceScrollContainer]);

  useEffect(() => {
    const viewport = getScrollViewport(previewScrollContainer);
    if (!viewport) return;

    const updateAutoFollow = () => {
      hasUserScrolledPreviewRef.current = true;
      shouldAutoFollowPreviewRef.current = isViewportNearBottom(viewport);
    };

    viewport.addEventListener('scroll', updateAutoFollow, { passive: true });

    return () => {
      viewport.removeEventListener('scroll', updateAutoFollow);
    };
  }, [previewScrollContainer]);

  useEffect(() => {
    if (!isStreaming) return;

    shouldAutoFollowSourceRef.current = true;
    shouldAutoFollowPreviewRef.current = true;
    hasUserScrolledSourceRef.current = false;
    hasUserScrolledPreviewRef.current = false;
  }, [isStreaming, filePath]);

  // Auto-scroll source view while streaming only when user is pinned near bottom.
  useEffect(() => {
    if (!isStreaming || !fileContent || !shouldAutoFollowSourceRef.current) return;

    const viewport = getScrollViewport(sourceScrollContainer);
    if (!viewport) return;

    requestAnimationFrame(() => {
      if (!shouldAutoFollowSourceRef.current) return;
      scrollViewportToBottom(viewport);
    });
  }, [isStreaming, fileContent, sourceScrollContainer]);

  // Content can still resize after React commit; keep pinned users at the bottom.
  useEffect(() => {
    if (!isStreaming || !sourceScrollContainer || typeof ResizeObserver === 'undefined') return;

    const viewport = getScrollViewport(sourceScrollContainer);
    if (!viewport) return;

    const contentNode = getViewportContentNode(viewport);
    if (!contentNode) return;

    let frameId = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frameId);
      frameId = requestAnimationFrame(() => {
        if (!shouldAutoFollowSourceRef.current) return;
        scrollViewportToBottom(viewport);
      });
    });

    observer.observe(contentNode);

    return () => {
      cancelAnimationFrame(frameId);
      observer.disconnect();
    };
  }, [isStreaming, sourceScrollContainer]);

  // Auto-scroll preview view while streaming only when user is pinned near bottom.
  useEffect(() => {
    if (!isStreaming || !fileContent || !shouldAutoFollowPreviewRef.current) return;

    const viewport = getScrollViewport(previewScrollContainer);
    if (!viewport) return;

    requestAnimationFrame(() => {
      if (!shouldAutoFollowPreviewRef.current) return;
      scrollViewportToBottom(viewport);
    });
  }, [isStreaming, fileContent, previewScrollContainer]);

  // Mirror source resize-follow behavior for preview content.
  useEffect(() => {
    if (!isStreaming || !previewScrollContainer || typeof ResizeObserver === 'undefined') return;

    const viewport = getScrollViewport(previewScrollContainer);
    if (!viewport) return;

    const contentNode = getViewportContentNode(viewport);
    if (!contentNode) return;

    let frameId = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frameId);
      frameId = requestAnimationFrame(() => {
        if (!shouldAutoFollowPreviewRef.current) return;
        scrollViewportToBottom(viewport);
      });
    });

    observer.observe(contentNode);

    return () => {
      cancelAnimationFrame(frameId);
      observer.disconnect();
    };
  }, [isStreaming, previewScrollContainer]);

  if (!isStreaming && !processedFilePath && !fileContent) {
    return (
      <GenericToolView
        name={name || `file-${operation}`}
        assistantContent={assistantContent}
        toolContent={toolContent}
        assistantTimestamp={assistantTimestamp}
        toolTimestamp={toolTimestamp}
        isSuccess={isSuccess}
        isStreaming={isStreaming}
      />
    );
  }

  const renderFilePreview = () => {
    if (!fileContent) {
      return (
        <div className="flex items-center justify-center h-full p-12">
          <div className="text-center">
            <FileIcon className="h-12 w-12 mx-auto mb-4 text-zinc-400" />
            <p className="text-sm text-zinc-500 dark:text-zinc-400">No content to preview</p>
          </div>
        </div>
      );
    }

    if (isHtml && htmlPreviewUrl) {
      return (
        <div className="flex flex-col h-[calc(100vh-16rem)]">
          <iframe
            src={htmlPreviewUrl}
            title={`HTML Preview of ${fileName}`}
            className="flex-grow border-0"
            sandbox="allow-same-origin allow-scripts"
          />
        </div>
      );
    }

    if (isMarkdown) {
      return (
        <div className="p-1 py-0 prose dark:prose-invert prose-zinc max-w-none">
          <MarkdownRenderer
            content={processUnicodeContent(fileContent)}
          />
        </div>
      );
    }

    if (isCsv) {
      return (
        <div className="h-full w-full p-4">
          <div className="h-[calc(100vh-17rem)] w-full bg-muted/20 border rounded-xl overflow-auto">
            <CsvRenderer content={processUnicodeContent(fileContent)} />
          </div>
        </div>
      );
    }

    return (
      <div className="p-4">
        <div className='w-full h-full bg-muted/20 border rounded-xl px-4 py-2 pb-6'>
          <pre className="text-sm font-mono text-zinc-800 dark:text-zinc-300 whitespace-pre-wrap break-words">
            {processUnicodeContent(fileContent)}
          </pre>
        </div>
      </div>
    );
  };

  const renderDeleteOperation = () => (
    <div className="flex flex-col items-center justify-center h-full py-12 px-6 tool-panel-surface">
      <div className={cn("w-20 h-20 rounded-full flex items-center justify-center mb-6", config.bgColor)}>
        <Icon className={cn("h-10 w-10", config.color)} />
      </div>
      <h3 className="text-xl font-semibold mb-6 text-zinc-900 dark:text-zinc-100">
        File Deleted
      </h3>
      <div className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg p-4 w-full max-w-md text-center mb-4 shadow-sm">
        <code className="text-sm font-mono text-zinc-700 dark:text-zinc-300 break-all">
          {processedFilePath || 'Unknown file path'}
        </code>
      </div>
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        This file has been permanently removed
      </p>
    </div>
  );

  const renderSourceCode = () => {
    if (!fileContent) {
      return (
        <div className="flex items-center justify-center h-full p-12">
          <div className="text-center">
            <FileIcon className="h-12 w-12 mx-auto mb-4 text-zinc-400" />
            <p className="text-sm text-zinc-500 dark:text-zinc-400">No source code to display</p>
          </div>
        </div>
      );
    }

    const shouldUseSyntaxHighlight = hasHighlighting && !isStreaming;

    if (shouldUseSyntaxHighlight) {
      return (
        <div className="relative">
          <div className="absolute left-0 top-0 bottom-0 w-12 border-r border-zinc-200 dark:border-zinc-800 z-10 flex flex-col bg-zinc-50 dark:bg-zinc-900">
            {contentLines.map((_, idx) => (
              <div
                key={idx}
                className="h-6 text-right pr-3 text-xs font-mono text-zinc-500 dark:text-zinc-500 select-none"
              >
                {idx + 1}
              </div>
            ))}
          </div>
          <div className="pl-12">
            <CodeBlockCode
              code={processUnicodeContent(fileContent)}
              language={language}
              className="text-xs"
            />
          </div>
        </div>
      );
    }

    return (
      <div className="min-w-full table">
        {contentLines.map((line, idx) => (
          <div
            key={idx}
            className={cn("table-row transition-colors", config.hoverColor)}
          >
            <div className="table-cell text-right pr-3 pl-6 py-0.5 text-xs font-mono text-zinc-500 dark:text-zinc-500 select-none w-12 border-r border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900">
              {idx + 1}
            </div>
            <div className="table-cell pl-3 py-0.5 pr-4 text-xs font-mono whitespace-pre-wrap text-zinc-800 dark:text-zinc-300">
              {processUnicodeContent(line) || ' '}
            </div>
          </div>
        ))}
        <div className="table-row h-4"></div>
      </div>
    );
  };

  return (
    <Card className="flex border shadow-none border-t border-b-0 border-x-0 p-0 rounded-none flex-col h-full overflow-hidden bg-card">
      <Tabs defaultValue={isMarkdown || isHtml ? 'preview' : 'code'} className="w-full h-full">
        <CardHeader className="h-14 bg-zinc-50/80 dark:bg-zinc-900/80 backdrop-blur-sm border-b p-2 px-4 space-y-2 mb-0">
          <div className="flex flex-row items-center justify-between">
            <div className="flex items-center gap-2">
              <div className={cn("relative p-2 rounded-lg border", config.gradientBg, config.borderColor)}>
                <Icon className={cn("h-5 w-5", config.color)} />
              </div>
              <div>
                <CardTitle className="text-base font-medium text-zinc-900 dark:text-zinc-100">
                  {toolTitle}
                </CardTitle>
              </div>
            </div>
            <div className='flex items-center gap-2'>
              {isHtml && htmlPreviewUrl && !isStreaming && (
                <Button variant="outline" size="sm" className="h-8 text-xs bg-white dark:bg-muted/50 hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-none" asChild>
                  <a href={htmlPreviewUrl} target="_blank" rel="noopener noreferrer">
                    <ExternalLink className="h-3.5 w-3.5 mr-1.5" />
                    Open in Browser
                  </a>
                </Button>
              )}
              {/* Copy button - only show when there's file content */}
              {fileContent && !isStreaming && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleCopyContent}
                  disabled={isCopyingContent}
                  className="h-8 text-xs bg-white dark:bg-muted/50 hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-none"
                  title="Copy file content"
                >
                  {isCopyingContent ? (
                    <Check className="h-3.5 w-3.5 mr-1.5" />
                  ) : (
                    <Copy className="h-3.5 w-3.5 mr-1.5" />
                  )}
                  <span className="hidden sm:inline">Copy</span>
                </Button>
              )}
              <TabsList className="h-8 bg-muted/50 border border-border/50 p-0.5 gap-1">
                <TabsTrigger
                  value="code"
                  className="flex items-center gap-1.5 px-4 py-2 text-xs font-medium transition-all [&[data-state=active]]:bg-white [&[data-state=active]]:dark:bg-primary/10 [&[data-state=active]]:text-foreground hover:bg-background/50 text-muted-foreground shadow-none"
                >
                  <Code className="h-3.5 w-3.5" />
                  Source
                </TabsTrigger>
                <TabsTrigger
                  value="preview"
                  className="flex items-center gap-1.5 px-4 py-2 text-xs font-medium transition-all [&[data-state=active]]:bg-white [&[data-state=active]]:dark:bg-primary/10 [&[data-state=active]]:text-foreground hover:bg-background/50 text-muted-foreground shadow-none"
                >
                  <Eye className="h-3.5 w-3.5" />
                  Preview
                </TabsTrigger>
              </TabsList>
            </div>
          </div>
        </CardHeader>

        <CardContent className="p-0 -my-2 h-full flex-1 overflow-hidden relative">
          <TabsContent value="code" className="flex-1 h-full mt-0 p-0 overflow-hidden">
            <ScrollArea ref={handleSourceScrollRef} className="h-full w-full min-h-0">
              {isStreaming && !fileContent ? (
                <LoadingState
                  icon={Icon}
                  iconColor={config.color}
                  bgColor={config.bgColor}
                  title={config.progressMessage}
                  filePath={processedFilePath || 'Processing file...'}
                  subtitle="Please wait while the file is being processed"
                  showProgress={false}
                />
              ) : operation === 'delete' ? (
                <div className="flex flex-col items-center justify-center h-full py-12 px-6">
                  <div className={cn("w-20 h-20 rounded-full flex items-center justify-center mb-6", config.bgColor)}>
                    <Icon className={cn("h-10 w-10", config.color)} />
                  </div>
                  <h3 className="text-xl font-semibold mb-6 text-zinc-900 dark:text-zinc-100">
                    Delete Operation
                  </h3>
                  <div className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg p-4 w-full max-w-md text-center">
                    <code className="text-sm font-mono text-zinc-700 dark:text-zinc-300 break-all">
                      {processedFilePath || 'Unknown file path'}
                    </code>
                  </div>
                </div>
              ) : (
                renderSourceCode()
              )}
            </ScrollArea>
          </TabsContent>

          <TabsContent value="preview" className="w-full flex-1 h-full mt-0 p-0 overflow-hidden">
            <ScrollArea ref={handlePreviewScrollRef} className="h-full w-full min-h-0">
              {isStreaming && !fileContent ? (
                <LoadingState
                  icon={Icon}
                  iconColor={config.color}
                  bgColor={config.bgColor}
                  title={config.progressMessage}
                  filePath={processedFilePath || 'Processing file...'}
                  subtitle="Please wait while the file is being processed"
                  showProgress={false}
                />
              ) : operation === 'delete' ? (
                renderDeleteOperation()
              ) : (
                renderFilePreview()
              )}
              {isStreaming && fileContent && (
                <div className="sticky bottom-4 right-4 float-right mr-4 mb-4">
                  <Badge className="bg-blue-500/90 text-white border-none shadow-lg animate-pulse">
                    <Loader2 className="h-3 w-3 animate-spin mr-1" />
                    Streaming...
                  </Badge>
                </div>
              )}
            </ScrollArea>
          </TabsContent>
        </CardContent>

        <div className="px-4 py-2 h-10 bg-gradient-to-r from-zinc-50/90 to-zinc-100/90 dark:from-zinc-900/90 dark:to-zinc-800/90 backdrop-blur-sm border-t border-zinc-200 dark:border-zinc-800 flex justify-between items-center gap-4">
          <div className="h-full flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
            <Badge variant="outline" className="py-0.5 h-6">
              <FileIcon className="h-3 w-3" />
              {hasHighlighting ? language.toUpperCase() : fileExtension.toUpperCase() || 'TEXT'}
            </Badge>
          </div>

          <div className="text-xs text-zinc-500 dark:text-zinc-400">
            {toolTimestamp && !isStreaming
              ? formatTimestamp(toolTimestamp)
              : assistantTimestamp
                ? formatTimestamp(assistantTimestamp)
                : ''}
          </div>
        </div>
      </Tabs>
    </Card>
  );
}
