import React, { forwardRef, useEffect, useState } from 'react';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Square, Loader2, ArrowUp } from 'lucide-react';
import { cn } from '@/lib/utils';
import { UploadedFile } from './chat-input';
import { FileUploadHandler } from './file-upload-handler';
import { VoiceRecorder } from './voice-recorder';
import { UnifiedConfigMenu } from './unified-config-menu';
import { canAccessModel, SubscriptionStatus } from './_use-model-selection';
import { isBillingUiEnabled, isLocalMode } from '@/lib/config';
import { useFeatureFlag } from '@/lib/feature-flags';
import { TooltipContent } from '@/components/ui/tooltip';
import { Tooltip } from '@/components/ui/tooltip';
import { TooltipProvider, TooltipTrigger } from '@radix-ui/react-tooltip';
import { BillingModal } from '@/components/billing/billing-modal';
import { handleFiles } from './file-upload-handler';
import { ShadowCloneToggle } from './shadow-clone-toggle';

interface MessageInputProps {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  onSubmit: (e: React.FormEvent) => void;
  onTranscription: (text: string) => void;
  placeholder: string;
  loading: boolean;
  disabled: boolean;
  isAgentRunning: boolean;
  onStopAgent?: () => void;
  isDraggingOver: boolean;
  uploadedFiles: UploadedFile[];

  fileInputRef: React.RefObject<HTMLInputElement>;
  isUploading: boolean;
  hasPendingAttachments: boolean;
  attachmentStatusMessage?: string;
  sandboxId?: string;
  preparedProjectId?: string;
  setPreparedProjectId: React.Dispatch<React.SetStateAction<string | undefined>>;
  setPendingFiles: React.Dispatch<React.SetStateAction<File[]>>;
  setUploadedFiles: React.Dispatch<React.SetStateAction<UploadedFile[]>>;
  setIsUploading: React.Dispatch<React.SetStateAction<boolean>>;
  hideAttachments?: boolean;
  messages?: any[]; // Add messages prop
  isLoggedIn?: boolean;

  selectedModel: string;
  onModelChange: (model: string) => void;
  modelOptions: any[];
  subscriptionStatus: SubscriptionStatus;
  canAccessModel: (modelId: string) => boolean;
  refreshCustomModels?: () => void;
  selectedAgentId?: string;
  onAgentSelect?: (agentId: string | undefined) => void;
  enableAdvancedConfig?: boolean;
  hideAgentSelection?: boolean;
  isSunaAgent?: boolean;
  enableShadowCloneUI?: boolean;
  shadowCloneMainModel?: string;
  shadowCloneSubagentModel?: string;
  onShadowCloneMainModelChange: (model?: string) => void;
  onShadowCloneSubagentModelChange: (model?: string) => void;
  resolveModelId: (modelId: string) => string;
}

export const MessageInput = forwardRef<HTMLTextAreaElement, MessageInputProps>(
  (
    {
      value,
      onChange,
      onSubmit,
      onTranscription,
      placeholder,
      loading,
      disabled,
      isAgentRunning,
      onStopAgent,
      isDraggingOver,
      uploadedFiles,

      fileInputRef,
      isUploading,
      hasPendingAttachments,
      attachmentStatusMessage,
      sandboxId,
      preparedProjectId,
      setPreparedProjectId,
      setPendingFiles,
      setUploadedFiles,
      setIsUploading,
      hideAttachments = false,
      messages = [],
      isLoggedIn = true,

      selectedModel,
      onModelChange,
      modelOptions,
      subscriptionStatus,
      canAccessModel,
      refreshCustomModels,

      selectedAgentId,
      onAgentSelect,
      enableAdvancedConfig = false,
      hideAgentSelection = false,
      isSunaAgent,
      enableShadowCloneUI = false,
      shadowCloneMainModel,
      shadowCloneSubagentModel,
      onShadowCloneMainModelChange,
      onShadowCloneSubagentModelChange,
      resolveModelId,
    },
    ref,
  ) => {
    const billingUiEnabled = isBillingUiEnabled();
    const [billingModalOpen, setBillingModalOpen] = useState(false);
    const [mounted, setMounted] = useState(false);
    const { enabled: customAgentsEnabled, loading: flagsLoading } = useFeatureFlag('custom_agents');

    useEffect(() => {
      setMounted(true);
    }, []);

    useEffect(() => {
      const textarea = ref as React.RefObject<HTMLTextAreaElement>;
      if (!textarea.current) return;

      const adjustHeight = () => {
        const el = textarea.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.maxHeight = '200px';
        el.style.overflowY = el.scrollHeight > 200 ? 'auto' : 'hidden';

        const newHeight = Math.min(el.scrollHeight, 200);
        el.style.height = `${newHeight}px`;
      };

      adjustHeight();

      window.addEventListener('resize', adjustHeight);
      return () => window.removeEventListener('resize', adjustHeight);
    }, [value, ref]);

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        if (
          (value.trim() || uploadedFiles.length > 0) &&
          !loading &&
          (!disabled || isAgentRunning) &&
          (!hasPendingAttachments || isAgentRunning)
        ) {
          onSubmit(e as unknown as React.FormEvent);
        }
      }
    };

    const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      if (!e.clipboardData) return;
      const items = Array.from(e.clipboardData.items);
      const imageFiles: File[] = [];
      for (const item of items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const file = item.getAsFile();
          if (file) imageFiles.push(file);
        }
      }
      if (imageFiles.length > 0) {
        e.preventDefault();
        handleFiles(
          imageFiles,
          sandboxId,
          preparedProjectId,
          setPreparedProjectId,
          setPendingFiles,
          setUploadedFiles,
          setIsUploading,
          messages,
        );
      }
    };

    const renderDropdown = () => {
      const showAdvancedFeatures = isLoggedIn && (enableAdvancedConfig || (customAgentsEnabled && !flagsLoading));
      // Don't render dropdown components until after hydration to prevent ID mismatches
      if (!mounted) {
        return <div className="flex items-center gap-2 h-8" />; // Placeholder with same height
      }
      // Unified compact menu for both logged and non-logged (non-logged shows only models subset via menu trigger)
      return (
        <div className="flex items-center gap-2">
          <UnifiedConfigMenu
            isLoggedIn={isLoggedIn}
            selectedAgentId={showAdvancedFeatures && !hideAgentSelection ? selectedAgentId : undefined}
            onAgentSelect={showAdvancedFeatures && !hideAgentSelection ? onAgentSelect : undefined}
            selectedModel={selectedModel}
            onModelChange={onModelChange}
            modelOptions={modelOptions}
            subscriptionStatus={subscriptionStatus}
            canAccessModel={canAccessModel}
            refreshCustomModels={refreshCustomModels}
          />
        </div>
      );
    }

    return (
      <div className="relative flex flex-col w-full h-full gap-2 justify-between">

        <div className="flex flex-col gap-1 px-2">
          <Textarea
            ref={ref}
            value={value}
            onChange={onChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={placeholder}
            className={cn(
              'w-full bg-transparent dark:bg-transparent border-none shadow-none focus-visible:ring-0 px-0.5 pb-6 pt-4 !text-[15px] min-h-[36px] max-h-[200px] overflow-y-auto resize-none',
              isDraggingOver ? 'opacity-40' : '',
            )}
            disabled={loading || (disabled && !isAgentRunning)}
            rows={1}
          />
        </div>

        {attachmentStatusMessage && (
          <div className="px-2 text-xs text-muted-foreground">
            {attachmentStatusMessage}
          </div>
        )}


        <div className="flex items-center justify-between mt-0 mb-1 px-2">
          <div className="flex items-center gap-3">
            {!hideAttachments && (
              <FileUploadHandler
                ref={fileInputRef}
                loading={loading}
                disabled={disabled}
                isAgentRunning={isAgentRunning}
                isUploading={isUploading}
                sandboxId={sandboxId}
                preparedProjectId={preparedProjectId}
                setPreparedProjectId={setPreparedProjectId}
                setPendingFiles={setPendingFiles}
                setUploadedFiles={setUploadedFiles}
                setIsUploading={setIsUploading}
                messages={messages}
                isLoggedIn={isLoggedIn}
              />
            )}

            {enableShadowCloneUI && (
              <ShadowCloneToggle
                disabled={loading || isAgentRunning || (disabled && !isAgentRunning)}
                modelOptions={modelOptions}
                shadowCloneMainModel={shadowCloneMainModel}
                shadowCloneSubagentModel={shadowCloneSubagentModel}
                onShadowCloneMainModelChange={onShadowCloneMainModelChange}
                onShadowCloneSubagentModelChange={onShadowCloneSubagentModelChange}
                resolveModelId={resolveModelId}
              />
            )}

          </div>

          {/* {subscriptionStatus === 'no_subscription' && !isLocalMode() &&
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger>
                  <p role='button' className='text-sm text-amber-500 hidden sm:block cursor-pointer' onClick={() => setBillingModalOpen(true)}>Upgrade for more usage</p>
                </TooltipTrigger>
                <TooltipContent>
                  <p>The free tier is severely limited by the amount of usage. Upgrade to experience the full power of Roys Alpha.</p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          } */}

          <div className='flex items-center gap-2'>
            {renderDropdown()}
            {billingUiEnabled && (
              <BillingModal
                open={billingModalOpen}
                onOpenChange={setBillingModalOpen}
                returnUrl={typeof window !== 'undefined' ? window.location.href : '/'}
              />
            )}

            {isLoggedIn && <VoiceRecorder
              onTranscription={onTranscription}
              disabled={loading || (disabled && !isAgentRunning)}
            />}

            <Button
              type="submit"
              onClick={isAgentRunning && onStopAgent ? onStopAgent : onSubmit}
              size="sm"
              className={cn(
                'w-8 h-8 flex-shrink-0 self-end rounded-xl bg-zinc-900 text-white hover:bg-zinc-800 dark:bg-primary dark:text-primary-foreground dark:hover:bg-primary/90',
                (!value.trim() && uploadedFiles.length === 0 && !isAgentRunning) ||
                  loading ||
                  (hasPendingAttachments && !isAgentRunning) ||
                  (disabled && !isAgentRunning)
                  ? 'opacity-50'
                  : '',
              )}
              disabled={
                (!value.trim() && uploadedFiles.length === 0 && !isAgentRunning) ||
                loading ||
                (hasPendingAttachments && !isAgentRunning) ||
                (disabled && !isAgentRunning)
              }
            >
              {loading ? (
                <Loader2 className="h-5 w-5 animate-spin" />
              ) : isAgentRunning ? (
                <div className="min-h-[14px] min-w-[14px] w-[14px] h-[14px] rounded-sm bg-current" />
              ) : (
                <ArrowUp className="h-5 w-5" />
              )}
            </Button>
          </div>
        </div>
        {/* {subscriptionStatus === 'no_subscription' && !isLocalMode() &&
          <div className='sm:hidden absolute -bottom-8 left-0 right-0 flex justify-center'>
            <p className='text-xs text-amber-500 px-2 py-1'>
              Upgrade for better performance
            </p>
          </div>
        } */}
      </div>
    );
  },
);

MessageInput.displayName = 'MessageInput';
