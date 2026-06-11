'use client';

import React, {
  useState,
  useRef,
  useEffect,
  forwardRef,
  useImperativeHandle,
} from 'react';
import { useAgents } from '@/hooks/react-query/agents/use-agents';
import { useAgentSelection } from '@/lib/stores/agent-selection-store';

import { Card, CardContent } from '@/components/ui/card';
import { handleFiles } from './file-upload-handler';
import { MessageInput } from './message-input';
import { AttachmentGroup } from '../attachment-group';
import { useModelSelection } from './_use-model-selection';
import { useFileDelete } from '@/hooks/react-query/files';
import { useQueryClient } from '@tanstack/react-query';
import { ToolCallInput } from './floating-tool-preview';
import { ChatSnack } from './chat-snack';
import { Brain, Zap, Workflow, Database, ArrowDown } from 'lucide-react';
import { useComposioToolkitIcon } from '@/hooks/react-query/composio/use-composio';
import { Skeleton } from '@/components/ui/skeleton';

import { IntegrationsRegistry } from '@/components/agents/integrations-registry';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useSubscriptionWithStreaming } from '@/hooks/react-query/subscriptions/use-subscriptions';
import { isBillingUiEnabled, isLocalMode } from '@/lib/config';
import { BillingModal } from '@/components/billing/billing-modal';
import { toast } from 'sonner';
import posthog from 'posthog-js';
import { useLanguage } from '@/contexts/LanguageContext';
import { useShadowCloneStore } from '@/lib/stores/shadow-clone-store';
import type { PreparedAttachment, ShadowCloneMode } from '@/lib/api';
import { buildUploadedImageMediaRefs, type ImageMediaRef } from './media-refs';

export interface ChatInputHandles {
  getPendingFiles: () => File[];
  clearPendingFiles: () => void;
}

export interface ChatInputProps {
  onSubmit: (
    message: string,
      options?: {
        model_name?: string;
        enable_thinking?: boolean;
        reasoning_effort?: string;
        agent_id?: string;
        shadow_clone_mode?: ShadowCloneMode;
        shadow_clone_main_model?: string;
        shadow_clone_subagent_model?: string;
        project_id?: string;
        prepared_attachments?: PreparedAttachment[];
        media_refs?: ImageMediaRef[];
      },
  ) => void | Promise<void>;
  placeholder?: string;
  loading?: boolean;
  disabled?: boolean;
  isAgentRunning?: boolean;
  onStopAgent?: () => void;
  autoFocus?: boolean;
  value?: string;
  onChange?: (value: string) => void;
  onFileBrowse?: () => void;
  projectId?: string;
  sandboxId?: string;
  hideAttachments?: boolean;
  selectedAgentId?: string;
  onAgentSelect?: (agentId: string | undefined) => void;
  agentName?: string;
  messages?: any[];
  bgColor?: string;
  toolCalls?: ToolCallInput[];
  toolCallIndex?: number;
  showToolPreview?: boolean;
  onExpandToolPreview?: () => void;
  isLoggedIn?: boolean;
  enableAdvancedConfig?: boolean;
  onConfigureAgent?: (agentId: string) => void;
  hideAgentSelection?: boolean;
  defaultShowSnackbar?: 'tokens' | 'upgrade' | false;
  showToLowCreditUsers?: boolean;
  agentMetadata?: {
    is_suna_default?: boolean;
  };
  showScrollToBottomIndicator?: boolean;
  onScrollToBottom?: () => void;
  enableShadowCloneUI?: boolean;
}

export interface UploadedFile {
  clientId: string;
  name: string;
  path: string;
  size: number;
  type: string;
  localUrl?: string;
  status?: 'preparing' | 'uploading' | 'ready' | 'error';
  preparedAttachment?: PreparedAttachment;
  errorMessage?: string;
}

const EMPTY_AGENTS: any[] = [];

function getPromptLengthBucket(length: number): string {
  if (length === 0) {
    return '0';
  }
  if (length <= 100) {
    return '1-100';
  }
  if (length <= 500) {
    return '101-500';
  }
  if (length <= 2000) {
    return '501-2000';
  }
  return '2000+';
}


export const ChatInput = forwardRef<ChatInputHandles, ChatInputProps>(
  (
    {
      onSubmit,
      placeholder,
      loading = false,
      disabled = false,
      isAgentRunning = false,
      onStopAgent,
      autoFocus = true,
      value: controlledValue,
      onChange: controlledOnChange,
      onFileBrowse,
      projectId,
      sandboxId,
      hideAttachments = false,
      selectedAgentId,
      onAgentSelect,
      agentName,
      messages = [],
      bgColor = 'bg-card',
      toolCalls = [],
      toolCallIndex = 0,
      showToolPreview = false,
      onExpandToolPreview,
      isLoggedIn = true,
      enableAdvancedConfig = false,
      onConfigureAgent,
      hideAgentSelection = false,
      defaultShowSnackbar = false,
      showToLowCreditUsers = true,
      agentMetadata,
      showScrollToBottomIndicator = false,
      onScrollToBottom,
      enableShadowCloneUI = false,
    },
    ref,
  ) => {
    const isControlled =
      controlledValue !== undefined && controlledOnChange !== undefined;
    const { t } = useLanguage();
    const billingUiEnabled = isBillingUiEnabled();

    const [uncontrolledValue, setUncontrolledValue] = useState('');
    const value = isControlled ? controlledValue : uncontrolledValue;

    const isSunaAgent = agentMetadata?.is_suna_default || false;
    const shadowCloneMode = useShadowCloneStore((state) => state.mode);

    const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
    const [pendingFiles, setPendingFiles] = useState<File[]>([]);
    const [isUploading, setIsUploading] = useState(false);
    const [preparedProjectId, setPreparedProjectId] = useState<string | undefined>(projectId);
    const [isDraggingOver, setIsDraggingOver] = useState(false);
    const [shadowCloneMainModel, setShadowCloneMainModel] = useState<string | undefined>(undefined);
    const [shadowCloneSubagentModel, setShadowCloneSubagentModel] = useState<string | undefined>(undefined);

    const [registryDialogOpen, setRegistryDialogOpen] = useState(false);
    const [showSnackbarState, setShowSnackbarState] = useState(defaultShowSnackbar);
    const [userDismissedUsage, setUserDismissedUsage] = useState(false);
    const [billingModalOpen, setBillingModalOpen] = useState(false);

    const {
      selectedModel,
      setSelectedModel: handleModelChange,
      subscriptionStatus,
      allModels: modelOptions,
      canAccessModel,
      getActualModelId,
      refreshCustomModels,
    } = useModelSelection();

    const { data: subscriptionData } = useSubscriptionWithStreaming(isAgentRunning);
    const deleteFileMutation = useFileDelete();
    const queryClient = useQueryClient();

    // Fetch integration icons only when logged in and advanced config UI is in use
    const shouldFetchIcons = isLoggedIn && !!enableAdvancedConfig;
    const { data: googleDriveIcon } = useComposioToolkitIcon('googledrive', { enabled: shouldFetchIcons });
    const { data: slackIcon } = useComposioToolkitIcon('slack', { enabled: shouldFetchIcons });
    const { data: notionIcon } = useComposioToolkitIcon('notion', { enabled: shouldFetchIcons });

    // Show usage preview logic:
    // - Always show to free users when showToLowCreditUsers is true
    // - For paid users, only show when they're at 70% or more of their cost limit (30% or below remaining)
    const shouldShowUsage = billingUiEnabled && !isLocalMode() && subscriptionData && showToLowCreditUsers && (() => {
      // Free users: always show
      if (subscriptionStatus === 'no_subscription') {
        return true;
      }

      // Paid users: only show when at 70% or more of cost limit
      const currentUsage = subscriptionData.current_usage || 0;
      const costLimit = subscriptionData.cost_limit || 0;

      if (costLimit === 0) return false; // No limit set

      return currentUsage >= (costLimit * 0.7); // 70% or more used (30% or less remaining)
    })();

    const showSnackbar =
      !billingUiEnabled
        ? false
        : shouldShowUsage &&
            defaultShowSnackbar !== false &&
            !userDismissedUsage &&
            (showSnackbarState === false || showSnackbarState === defaultShowSnackbar)
          ? 'upgrade'
          : !shouldShowUsage
            ? false
            : showSnackbarState;

    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const { data: agentsResponse } = useAgents({}, { enabled: isLoggedIn });
    const agents = agentsResponse?.agents ?? EMPTY_AGENTS;
    const resolvedPlaceholder = placeholder || t('chat.placeholder');

    const { initializeFromAgents } = useAgentSelection();
    useImperativeHandle(ref, () => ({
      getPendingFiles: () => pendingFiles,
      clearPendingFiles: () => {
        setPendingFiles([]);
        setUploadedFiles((prev) => {
          prev.forEach((file) => {
            if (file.status === 'ready' && file.localUrl) {
              URL.revokeObjectURL(file.localUrl);
            }
          });
          return prev.filter((file) => file.status !== 'ready');
        });
        setPreparedProjectId(undefined);
      },
    }));

    const readyPreparedAttachments = uploadedFiles
      .filter(
        (file) => file.status === 'ready' && file.preparedAttachment,
      )
      .map((file) => file.preparedAttachment as PreparedAttachment);
    const readyUploadedFiles = uploadedFiles.filter((file) => file.status === 'ready');
    const effectivePreparedProjectId = preparedProjectId || projectId;
    const hasBlockingAttachmentWork =
      isUploading ||
      uploadedFiles.some(
        (file) => file.status === 'preparing' || file.status === 'uploading',
      );
    const hasAttachedFiles = readyUploadedFiles.length > 0 || pendingFiles.length > 0;
    const attachmentStatusMessage = hasBlockingAttachmentWork
      ? 'Attachments are still being prepared.'
      : undefined;

    useEffect(() => {
      if (agents.length > 0 && !onAgentSelect) {
        initializeFromAgents(agents);
      }
    }, [agents, onAgentSelect, initializeFromAgents]);



    useEffect(() => {
      if (autoFocus && textareaRef.current) {
        textareaRef.current.focus();
      }
    }, [autoFocus]);
    const handleSubmit = async (e: React.FormEvent) => {
      e.preventDefault();
      if (
        (!value.trim() && !hasAttachedFiles) ||
        loading ||
        (disabled && !isAgentRunning) ||
        (!isAgentRunning && hasBlockingAttachmentWork)
      )
        return;

      if (isAgentRunning && onStopAgent) {
        onStopAgent();
        return;
      }

      let message = value;
      const shouldUsePreparedAttachments =
        !sandboxId && readyPreparedAttachments.length > 0;
      const imageMediaRefs = buildUploadedImageMediaRefs(readyUploadedFiles);
      const normalizedShadowCloneMainModel = shadowCloneMainModel
        ? getActualModelId(shadowCloneMainModel)
        : undefined;
      const normalizedShadowCloneSubagentModel = shadowCloneSubagentModel
        ? getActualModelId(shadowCloneSubagentModel)
        : undefined;

      if (readyUploadedFiles.length > 0) {
        const fileInfo = readyUploadedFiles
          .map((file) => `[Uploaded File: ${file.path}]`)
          .join('\n');
        message = message ? `${message}\n\n${fileInfo}` : fileInfo;
      }

      let baseModelName = getActualModelId(selectedModel);
      const promptCharacterCount = value.trim().length;

      // 检测是否为 Gemini 3 / Kimi K2.5（官方或 OpenRouter）模型 - 自动启用思考模式
      const normalizedModelName = baseModelName.toLowerCase();
      const isGemini3Model = normalizedModelName.includes('gemini-3');
      const isKimiK25Model =
        normalizedModelName === 'kimi-k2.5' ||
        normalizedModelName === 'openrouter/moonshotai/kimi-k2.5' ||
        normalizedModelName === 'ppio/moonshotai/kimi-k2.5';
      const isReasoningPreferredModel =
        normalizedModelName === 'openrouter/minimax/minimax-m2.7' ||
        normalizedModelName === 'openrouter/xiaomi/mimo-v2-pro' ||
        normalizedModelName === 'deepseek-v4-pro-high' ||
        normalizedModelName === 'deepseek-v4-pro-max' ||
        normalizedModelName === 'deepseek-v4-flash-high' ||
        normalizedModelName === 'deepseek-v4-flash-max';

      // Gemini 3 / Kimi K2.5 / OpenRouter reasoning models 自动启用思考。
      // 当前 openai-proxy 的 gpt-5.4 chat/completions 路由不会透出可分离 reasoning chunk，
      // 因此不要在这里默认开启隐藏 reasoning 模式，避免前后端对“可见思考流”的预期不一致。
      let thinkingEnabled = isGemini3Model || isKimiK25Model || isReasoningPreferredModel;
      if (selectedModel.endsWith('-thinking')) {
        baseModelName = getActualModelId(selectedModel.replace(/-thinking$/, ''));
        thinkingEnabled = true;
      }

      posthog.capture('task_prompt_submitted', {
        attachment_count: readyUploadedFiles.length,
        has_text: promptCharacterCount > 0,
        prepared_attachment_count: readyPreparedAttachments.length,
        prompt_length_bucket: getPromptLengthBucket(promptCharacterCount),
        shadow_clone_mode: enableShadowCloneUI ? shadowCloneMode : 'disabled',
        thinking_enabled: thinkingEnabled,
      });

      try {
        await Promise.resolve(
          onSubmit(message, {
            agent_id: selectedAgentId,
            model_name: baseModelName,
            enable_thinking: thinkingEnabled,
            reasoning_effort: thinkingEnabled ? 'high' : 'low',
            ...(enableShadowCloneUI
              ? { shadow_clone_mode: shadowCloneMode }
              : {}),
            ...(enableShadowCloneUI && normalizedShadowCloneMainModel
              ? { shadow_clone_main_model: normalizedShadowCloneMainModel }
              : {}),
            ...(enableShadowCloneUI && normalizedShadowCloneSubagentModel
              ? { shadow_clone_subagent_model: normalizedShadowCloneSubagentModel }
              : {}),
            ...(shouldUsePreparedAttachments
              ? {
                  project_id: effectivePreparedProjectId,
                  prepared_attachments: readyPreparedAttachments,
                }
              : {}),
            ...(imageMediaRefs.length > 0
              ? {
                  media_refs: imageMediaRefs,
                }
              : {}),
          }),
        );

        if (!isControlled) {
          setUncontrolledValue('');
        }
      } catch (error) {
        console.error('Chat input submit failed:', error);
      }
    };

    const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const newValue = e.target.value;
      if (isControlled) {
        controlledOnChange(newValue);
      } else {
        setUncontrolledValue(newValue);
      }
    };

    const handleTranscription = (transcribedText: string) => {
      const currentValue = isControlled ? controlledValue : uncontrolledValue;
      const newValue = currentValue ? `${currentValue} ${transcribedText}` : transcribedText;

      if (isControlled) {
        controlledOnChange(newValue);
      } else {
        setUncontrolledValue(newValue);
      }
    };

    const removeUploadedFile = async (index: number) => {
      const fileToRemove = uploadedFiles[index];

      // Clean up local URL if it exists
      if (fileToRemove.localUrl) {
        URL.revokeObjectURL(fileToRemove.localUrl);
      }

      // Remove from local state immediately for responsive UI
      setUploadedFiles((prev) => prev.filter((_, i) => i !== index));
      if (!sandboxId) {
        setPendingFiles([]);
      }

      // Check if file is referenced in existing chat messages before deleting from server
      const isFileUsedInChat = messages.some(message => {
        const content = typeof message.content === 'string' ? message.content : '';
        return content.includes(`[Uploaded File: ${fileToRemove.path}]`);
      });

      // Only delete from server if file is not referenced in chat history
      if (sandboxId && fileToRemove.path && !isFileUsedInChat) {
        deleteFileMutation.mutate({
          sandboxId,
          filePath: fileToRemove.path,
        }, {
          onError: (error) => {
            console.error('Failed to delete file from server:', error);
          }
        });
      } else {
        // File exists in chat history, don't delete from server
      }
    };

    const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDraggingOver(true);
    };

    const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDraggingOver(false);
    };



    return (
      <div className="mx-auto w-full max-w-4xl relative">
        <div className="relative">
          <ChatSnack
            toolCalls={toolCalls}
            toolCallIndex={toolCallIndex}
            onExpandToolPreview={onExpandToolPreview}
            agentName={agentName}
            showToolPreview={showToolPreview}
            showUsagePreview={showSnackbar}
            subscriptionData={subscriptionData}
            onCloseUsage={() => { setShowSnackbarState(false); setUserDismissedUsage(true); }}
            onOpenUpgrade={billingUiEnabled ? () => setBillingModalOpen(true) : undefined}
            isVisible={showToolPreview || !!showSnackbar}
          />

          {/* Scroll to bottom button */}
          {showScrollToBottomIndicator && onScrollToBottom && (
            <button
              onClick={onScrollToBottom}
              className={`absolute cursor-pointer right-3 z-50 w-8 h-8 rounded-full bg-card border border-border transition-all duration-200 hover:scale-105 flex items-center justify-center ${showToolPreview || !!showSnackbar ? '-top-12' : '-top-5'
                }`}
              title={t('chat.scrollToBottom')}
            >
              <ArrowDown className="w-4 h-4 text-muted-foreground" />
            </button>
          )}
          <Card
            className={`-mb-2 shadow-none w-full max-w-4xl mx-auto bg-transparent border-none overflow-visible ${enableAdvancedConfig && selectedAgentId ? '' : 'rounded-3xl'} relative z-10`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setIsDraggingOver(false);
              if (fileInputRef.current && e.dataTransfer.files.length > 0) {
                const files = Array.from(e.dataTransfer.files);
                handleFiles(
                  files,
                  sandboxId,
                  effectivePreparedProjectId,
                  setPreparedProjectId,
                  setPendingFiles,
                  setUploadedFiles,
                  setIsUploading,
                  messages,
                  queryClient,
                );
              }
            }}
          >
            <div className="w-full text-sm flex flex-col justify-between items-start rounded-lg">
              <CardContent className={`w-full p-1.5 pb-2 ${bgColor} border dark:border-transparent rounded-3xl shadow-none`}>
                <AttachmentGroup
                  files={uploadedFiles || []}
                  sandboxId={sandboxId}
                  onRemove={removeUploadedFile}
                  layout="inline"
                  maxHeight="216px"
                  showPreviews={true}
                />
                <MessageInput
                  ref={textareaRef}
                  value={value}
                  onChange={handleChange}
                  onSubmit={handleSubmit}
                  onTranscription={handleTranscription}
                  placeholder={resolvedPlaceholder}
                  loading={loading}
                  disabled={disabled}
                  isAgentRunning={isAgentRunning}
                  onStopAgent={onStopAgent}
                  isDraggingOver={isDraggingOver}
                  uploadedFiles={uploadedFiles}

                  fileInputRef={fileInputRef}
                  isUploading={isUploading}
                  hasPendingAttachments={hasBlockingAttachmentWork}
                  attachmentStatusMessage={attachmentStatusMessage}
                  sandboxId={sandboxId}
                  preparedProjectId={effectivePreparedProjectId}
                  setPreparedProjectId={setPreparedProjectId}
                  setPendingFiles={setPendingFiles}
                  setUploadedFiles={setUploadedFiles}
                  setIsUploading={setIsUploading}
                  hideAttachments={hideAttachments}
                  messages={messages}

                  selectedModel={selectedModel}
                  onModelChange={handleModelChange}
                  modelOptions={modelOptions}
                  subscriptionStatus={subscriptionStatus}
                  canAccessModel={canAccessModel}
                  refreshCustomModels={refreshCustomModels}
                  isLoggedIn={isLoggedIn}

                  selectedAgentId={selectedAgentId}
                  onAgentSelect={onAgentSelect}
                  hideAgentSelection={hideAgentSelection}
                  enableShadowCloneUI={enableShadowCloneUI}
                  shadowCloneMainModel={shadowCloneMainModel}
                  shadowCloneSubagentModel={shadowCloneSubagentModel}
                  onShadowCloneMainModelChange={setShadowCloneMainModel}
                  onShadowCloneSubagentModelChange={setShadowCloneSubagentModel}
                  resolveModelId={getActualModelId}
                />
              </CardContent>
            </div>
          </Card>

          {enableAdvancedConfig && selectedAgentId && (
            <div className="w-full max-w-4xl mx-auto -mt-12 relative z-20">
              <div className="bg-gradient-to-b from-transparent via-transparent to-muted/30 pt-8 pb-2 px-4 rounded-b-3xl border border-t-0 border-border/50 transition-all duration-300 ease-out">
                <div className="flex items-center justify-between gap-1 overflow-x-auto scrollbar-none relative">
                  <button
                    onClick={() => toast.info(t('common.comingSoon'))}
                    className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground transition-all duration-200 px-2.5 py-1.5 rounded-lg hover:bg-muted/50 border border-transparent hover:border-border/30 flex-shrink-0 cursor-pointer relative pointer-events-auto"
                  >
                    <div className="flex items-center -space-x-0.5">
                      {googleDriveIcon?.icon_url && slackIcon?.icon_url && notionIcon?.icon_url ? (
                        <>
                          <div className="w-4 h-4 bg-white dark:bg-muted border border-border rounded-full flex items-center justify-center shadow-sm">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={googleDriveIcon.icon_url} className="w-2.5 h-2.5" alt="Google Drive" />
                          </div>
                          <div className="w-4 h-4 bg-white dark:bg-muted border border-border rounded-full flex items-center justify-center shadow-sm">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={slackIcon.icon_url} className="w-2.5 h-2.5" alt="Slack" />
                          </div>
                          <div className="w-4 h-4 bg-white dark:bg-muted border border-border rounded-full flex items-center justify-center shadow-sm">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={notionIcon.icon_url} className="w-2.5 h-2.5" alt="Notion" />
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="w-4 h-4 bg-white dark:bg-muted border border-border rounded-full flex items-center justify-center shadow-sm">
                            <Skeleton className="w-2.5 h-2.5 rounded" />
                          </div>
                          <div className="w-4 h-4 bg-white dark:bg-muted border border-border rounded-full flex items-center justify-center shadow-sm">
                            <Skeleton className="w-2.5 h-2.5 rounded" />
                          </div>
                          <div className="w-4 h-4 bg-white dark:bg-muted border border-border rounded-full flex items-center justify-center shadow-sm">
                            <Skeleton className="w-2.5 h-2.5 rounded" />
                          </div>
                        </>
                      )}
                    </div>
                    <span className="text-xs font-medium">{t('agent.integrations')}</span>
                  </button>
                  <button
                    onClick={() => toast.info(t('common.comingSoon'))}
                    className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground transition-all duration-200 px-2.5 py-1.5 rounded-lg hover:bg-muted/50 border border-transparent hover:border-border/30 flex-shrink-0 cursor-pointer relative pointer-events-auto"
                  >
                    <Brain className="h-3.5 w-3.5 flex-shrink-0" />
                    <span className="text-xs font-medium">{t('agent.instructions')}</span>
                  </button>
                  <button
                    onClick={() => toast.info(t('common.comingSoon'))}
                    className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground transition-all duration-200 px-2.5 py-1.5 rounded-lg hover:bg-muted/50 border border-transparent hover:border-border/30 flex-shrink-0 cursor-pointer relative pointer-events-auto"
                  >
                    <Database className="h-3.5 w-3.5 flex-shrink-0" />
                    <span className="text-xs font-medium">{t('agent.knowledgeBase')}</span>
                  </button>
                  <button
                    onClick={() => toast.info(t('common.comingSoon'))}
                    className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground transition-all duration-200 px-2.5 py-1.5 rounded-lg hover:bg-muted/50 border border-transparent hover:border-border/30 flex-shrink-0 cursor-pointer relative pointer-events-auto"
                  >
                    <Zap className="h-3.5 w-3.5 flex-shrink-0" />
                    <span className="text-xs font-medium">{t('agent.triggers')}</span>
                  </button>
                  <button
                    onClick={() => toast.info(t('common.comingSoon'))}
                    className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground transition-all duration-200 px-2.5 py-1.5 rounded-lg hover:bg-muted/50 border border-transparent hover:border-border/30 flex-shrink-0 cursor-pointer relative pointer-events-auto"
                  >
                    <Workflow className="h-3.5 w-3.5 flex-shrink-0" />
                    <span className="text-xs font-medium">{t('agent.scripts')}</span>
                  </button>
                </div>
              </div>
            </div>
          )}

          <Dialog open={registryDialogOpen} onOpenChange={setRegistryDialogOpen}>
            <DialogContent className="p-0 max-w-6xl h-[90vh] overflow-hidden">
              <DialogHeader className="sr-only">
                <DialogTitle>Integrations</DialogTitle>
              </DialogHeader>
              <IntegrationsRegistry
                showAgentSelector={true}
                selectedAgentId={selectedAgentId}
                onAgentChange={onAgentSelect}
                onToolsSelected={(profileId, selectedTools, appName, appSlug) => {
                  // Save to workflow or perform other action here
                }}
              />
            </DialogContent>
          </Dialog>
          {billingUiEnabled && (
            <BillingModal
              open={billingModalOpen}
              onOpenChange={setBillingModalOpen}
            />
          )}
        </div>
      </div>
    );
  },
);

ChatInput.displayName = 'ChatInput';
