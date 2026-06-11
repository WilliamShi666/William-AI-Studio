import React, { useState, useRef, useCallback, useEffect } from 'react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import {
  ChatInput,
  ChatInputHandles
} from '@/components/thread/chat-input/chat-input';
import { ThreadContent } from '@/components/thread/content/ThreadContent';
import { UnifiedMessage } from '@/components/thread/types';
import { useInitiateAgentWithInvalidation } from '@/hooks/react-query/dashboard/use-initiate-agent';
import { useAgentStream } from '@/hooks/useAgentStream';
import { useAddUserMessageMutation } from '@/hooks/react-query/threads/use-messages';
import { useStartAgentMutation, useStopAgentMutation } from '@/hooks/react-query/threads/use-agent-run';
import {
  BillingError,
  type ImageMediaRef,
  type PreparedAttachment,
  type ShadowCloneMode,
} from '@/lib/api';
import { normalizeFilenameToNFC } from '@/lib/utils/unicode';
import { KortixLogo } from '../sidebar/kortix-logo';
import { useLanguage } from '@/contexts/LanguageContext';
import { isBenignAgentNotRunningError } from '@/lib/stream-errors';

interface Agent {
  agent_id: string;
  name: string;
  description?: string;
  system_prompt: string;
  configured_mcps: Array<{ name: string; qualifiedName: string; config: any; enabledTools?: string[] }>;
  agentpress_tools: Record<string, { enabled: boolean; description: string }>;
  is_default: boolean;
  created_at?: string;
  updated_at?: string;
  profile_image_url?: string;
}

interface AgentPreviewProps {
  agent: Agent;
  agentMetadata?: {
    is_suna_default?: boolean;
  };
}

export const AgentPreview = ({ agent, agentMetadata }: AgentPreviewProps) => {
  const { language, t } = useLanguage();
  const displayName = agent.name === 'Suna' ? t('agent.default') : agent.name;
  const [messages, setMessages] = useState<UnifiedMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [threadId, setThreadId] = useState<string | null>(null);
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [agentStatus, setAgentStatus] = useState<'idle' | 'running' | 'connecting' | 'disconnecting' | 'error'>('idle');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [hasStartedConversation, setHasStartedConversation] = useState(false);

  const isSunaAgent = agentMetadata?.is_suna_default || false;

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<ChatInputHandles>(null);

  const getAgentStyling = () => {
    return {
      avatar: '🤖',
      color: '#6366f1',
    };
  };

  const { avatar, color } = getAgentStyling();

  const agentAvatarComponent = React.useMemo(() => {
    if (isSunaAgent) {
      return <KortixLogo size={16} />;
    }
    if (agent.profile_image_url) {
      return (
        <img 
          src={agent.profile_image_url} 
          alt={agent.name}
          className="h-4 w-4 rounded-sm object-cover"
        />
      );
    }
    if (avatar) {
      return <div className="text-base leading-none">{avatar}</div>;
    }
    return <KortixLogo size={16} />;
  }, [agent.profile_image_url, agent.name, avatar, isSunaAgent]);

  const initiateAgentMutation = useInitiateAgentWithInvalidation();
  const addUserMessageMutation = useAddUserMessageMutation();
  const startAgentMutation = useStartAgentMutation();
  const stopAgentMutation = useStopAgentMutation();

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleNewMessageFromStream = useCallback((message: UnifiedMessage) => {
    setMessages((prev) => {
      if (message.type === 'user') {
        const optimisticIndex = prev.findIndex((m) =>
          m.message_id?.startsWith('temp-') &&
          m.type === 'user' &&
          m.content === message.content
        );
        if (optimisticIndex !== -1) {
          const next = [...prev];
          next[optimisticIndex] = message;
          return next;
        }
      }
      const messageExists = prev.some((m) => m.message_id === message.message_id);
      if (messageExists) {
        return prev.map((m) => m.message_id === message.message_id ? message : m);
      } else {
        return [...prev, message];
      }
    });
  }, []);

  const handleStreamStatusChange = useCallback((hookStatus: string) => {
    switch (hookStatus) {
      case 'idle':
      case 'completed':
      case 'stopped':
      case 'agent_not_running':
      case 'error':
      case 'failed':
        setAgentStatus('idle');
        setAgentRunId(null);
        break;
      case 'connecting':
        setAgentStatus('connecting');
        break;
      case 'streaming':
        setAgentStatus('running');
        break;
    }
  }, []);

  const handleStreamError = useCallback((errorMessage: string) => {
    console.error(`[PREVIEW] Stream error: ${errorMessage}`);
    const normalized = errorMessage.toLowerCase();
    if (!normalized.includes('not found') &&
      !isBenignAgentNotRunningError(errorMessage)) {
      toast.error(`Stream Error: ${errorMessage}`);
    }
  }, []);

  const handleStreamClose = useCallback(() => {
  }, []);

  const {
    status: streamHookStatus,
    textContent: streamingTextContent,
    reasoningContent: streamingReasoningContent,
    toolCall: streamingToolCall,
    error: streamError,
    agentRunId: currentHookRunId,
    startStreaming,
    stopStreaming,
  } = useAgentStream(
    {
      onMessage: handleNewMessageFromStream,
      onStatusChange: handleStreamStatusChange,
      onError: handleStreamError,
      onClose: handleStreamClose,
    },
    threadId,
    setMessages,
  );

  useEffect(() => {
    if (agentRunId && agentRunId !== currentHookRunId && threadId) {
      startStreaming(agentRunId);
    }
  }, [agentRunId, startStreaming, currentHookRunId, threadId]);

  useEffect(() => {
    if (streamingTextContent) {
      scrollToBottom();
    }
  }, [streamingTextContent]);

  const handleSubmitFirstMessage = async (
    message: string,
    options?: {
      model_name?: string;
      enable_thinking?: boolean;
      reasoning_effort?: string;
      agent_id?: string;
      stream?: boolean;
      enable_context_manager?: boolean;
      shadow_clone_mode?: ShadowCloneMode;
      project_id?: string;
      prepared_attachments?: PreparedAttachment[];
    },
  ) => {
    const preparedAttachments = options?.prepared_attachments ?? [];
    const hasPreparedAttachments = preparedAttachments.length > 0;
    if (
      !message.trim() &&
      !hasPreparedAttachments &&
      !chatInputRef.current?.getPendingFiles().length
    ) return;

    setIsSubmitting(true);
    setHasStartedConversation(true);

    try {
      const files = hasPreparedAttachments
        ? []
        : chatInputRef.current?.getPendingFiles() || [];

      const formData = new FormData();
      formData.append('prompt', message);
      formData.append('agent_id', agent.agent_id);

      if (options?.project_id) {
        formData.append('project_id', options.project_id);
      }
      if (hasPreparedAttachments) {
        formData.append(
          'prepared_attachments_json',
          JSON.stringify(preparedAttachments),
        );
      }

      files.forEach((file) => {
        const normalizedName = normalizeFilenameToNFC(file.name);
        formData.append('files', file, normalizedName);
      });

      if (options?.model_name) formData.append('model_name', options.model_name);
      formData.append('enable_thinking', String(options?.enable_thinking ?? false));
      formData.append('reasoning_effort', options?.reasoning_effort ?? 'low');
      formData.append('stream', String(options?.stream ?? true));
      formData.append('enable_context_manager', String(options?.enable_context_manager ?? false));

      const result = await initiateAgentMutation.mutateAsync(formData);

      if (result.thread_id) {
        setThreadId(result.thread_id);
        if (result.agent_run_id) {
          setAgentRunId(result.agent_run_id);
        } else {
          try {
            const agentResult = await startAgentMutation.mutateAsync({
              threadId: result.thread_id,
              options
            });
            setAgentRunId(agentResult.agent_run_id);
          } catch (startError) {
            console.error('[PREVIEW] Error starting agent manually:', startError);
            toast.error('Failed to start agent');
          }
        }
        const preparedAttachmentMarkers = preparedAttachments
          .map((attachment) => `[Uploaded File: ${attachment.path}]`)
          .join('\n');
        const displayMessage = preparedAttachmentMarkers
          ? (message
              ? `${message}\n\n${preparedAttachmentMarkers}`
              : preparedAttachmentMarkers)
          : message;
        const userPayload = JSON.stringify({ role: 'user', content: displayMessage });
        const userMessage: UnifiedMessage = {
          message_id: `user-${Date.now()}`,
          thread_id: result.thread_id,
          type: 'user',
          is_llm_message: false,
          content: userPayload,
          metadata: '{}',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        setMessages([userMessage]);
      }

      chatInputRef.current?.clearPendingFiles();
      setInputValue('');
    } catch (error: any) {
      console.error('[PREVIEW] Error during initiation:', error);
      if (error instanceof BillingError) {
        toast.error('Billing limit reached. Please upgrade your plan.');
      } else {
        toast.error('Failed to start conversation');
      }
      setHasStartedConversation(false);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmitMessage = useCallback(
    async (
      message: string,
      options?: {
        model_name?: string;
        enable_thinking?: boolean;
        reasoning_effort?: string;
        shadow_clone_mode?: ShadowCloneMode;
        media_refs?: ImageMediaRef[];
      },
    ) => {
      if (!message.trim() || !threadId) return;
      setIsSubmitting(true);

      let savedMessageId: string | null = null;
      const userPayload = JSON.stringify({ role: 'user', content: message });
      const optimisticUserMessage: UnifiedMessage = {
        message_id: `temp-${Date.now()}`,
        thread_id: threadId,
        type: 'user',
        is_llm_message: false,
        content: userPayload,
        metadata: '{}',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };

      setMessages((prev) => [...prev, optimisticUserMessage]);
      setInputValue('');

      try {
        const savedMessage = await addUserMessageMutation.mutateAsync({
          threadId,
          message,
          media_refs: options?.media_refs,
        });
        const normalizedMessage: UnifiedMessage = {
          message_id: savedMessage.message_id || optimisticUserMessage.message_id,
          thread_id: savedMessage.thread_id || threadId,
          type: (savedMessage.type || 'user') as UnifiedMessage['type'],
          is_llm_message: Boolean(savedMessage.is_llm_message),
          content: savedMessage.content || userPayload,
          metadata: savedMessage.metadata || '{}',
          created_at: savedMessage.created_at || optimisticUserMessage.created_at,
          updated_at: savedMessage.updated_at || savedMessage.created_at || optimisticUserMessage.updated_at,
          agent_id: savedMessage.agent_id,
          agents: savedMessage.agents,
        };
        savedMessageId = normalizedMessage.message_id;
        setMessages((prev) => {
          let replaced = false;
          const next = prev.map((m) => {
            if (m.message_id === optimisticUserMessage.message_id) {
              replaced = true;
              return normalizedMessage;
            }
            return m;
          });
          if (!replaced) {
            return [...next, normalizedMessage];
          }
          return next;
        });

        let agentResult;
        try {
          agentResult = await startAgentMutation.mutateAsync({
            threadId,
            options,
          });
        } catch (error: any) {
          if (error instanceof BillingError) {
            toast.error('Billing limit reached. Please upgrade your plan.');
            const idsToRemove = [optimisticUserMessage.message_id, savedMessageId].filter(Boolean);
            setMessages(prev => prev.filter(m => !idsToRemove.includes(m.message_id)));
            return;
          }
          throw new Error(`Failed to start agent: ${error?.message || error}`);
        }
        setAgentRunId(agentResult.agent_run_id);

      } catch (err) {
        console.error('[PREVIEW] Error sending message:', err);
        toast.error(err instanceof Error ? err.message : 'Operation failed');
        const idsToRemove = [optimisticUserMessage.message_id, savedMessageId].filter(Boolean);
        setMessages((prev) => prev.filter((m) => !idsToRemove.includes(m.message_id)));
      } finally {
        setIsSubmitting(false);
      }
    },
    [threadId, addUserMessageMutation, startAgentMutation],
  );

  const handleStopAgent = useCallback(async () => {
    setAgentStatus('disconnecting');
    await stopStreaming();

    if (agentRunId) {
      try {
        await stopAgentMutation.mutateAsync(agentRunId);
      } catch (error) {
        console.error('[PREVIEW] Error stopping agent:', error);
      }
    }
  }, [stopStreaming, agentRunId, stopAgentMutation]);

  const handleToolClick = useCallback((assistantMessageId: string | null, toolName: string) => {
    toast.info(`Tool: ${toolName} (Preview mode - tool details not available)`);
  }, []);


  return (
    <div className="h-full flex flex-col bg-muted dark:bg-muted/30">
      <div className="flex-shrink-0 flex items-center gap-3 px-8 py-8">
        <div className="flex-1">
        </div>
        <Badge variant="highlight" className="text-sm">{t('home.previewMode')}</Badge>
      </div>
      <div className="flex-1 overflow-hidden">
        <div className="h-full overflow-y-auto scrollbar-hide">
          <ThreadContent
            messages={messages}
            streamingTextContent={streamingTextContent}
            streamingToolCall={streamingToolCall}
            agentStatus={agentStatus}
            handleToolClick={handleToolClick}
            handleOpenFileViewer={() => { }}
            streamHookStatus={streamHookStatus}
            isPreviewMode={true}
            agentName={agent.name}
            agentAvatar={agentAvatarComponent}
            agentMetadata={agentMetadata}
            agentData={agent}
            emptyStateComponent={
              <div className="flex flex-col items-center text-center text-muted-foreground/80">
                <div className="flex w-20 aspect-square items-center justify-center rounded-2xl bg-muted-foreground/10 p-4 mb-4">
                  {isSunaAgent ? (
                    <KortixLogo size={36} />
                  ) : agent.profile_image_url ? (
                    <img 
                      src={agent.profile_image_url} 
                      alt={agent.name}
                      className="w-12 h-12 rounded-xl object-cover"
                    />
                  ) : (
                    <div className="text-4xl">{avatar}</div>
                  )}
                </div>
                <p className='w-[60%] text-2xl mb-3'>
                  {t('home.startConversation')}{' '}
                  <span className='text-primary/80 font-semibold'>{displayName}</span>
                  {t('home.startConversationSuffix')}
                </p>
                <p className='w-[70%] text-sm text-muted-foreground/60'>{t('home.testAgentHint')}</p>
              </div>
            }
          />
          <div ref={messagesEndRef} />
        </div>
      </div>
      <div className="flex-shrink-0">
        <div className="px-8 md:pb-4">
          <ChatInput
            ref={chatInputRef}
            onSubmit={threadId ? handleSubmitMessage : handleSubmitFirstMessage}
            loading={isSubmitting}
            placeholder={language === 'zh'
              ? `给${displayName || t('agent.default')}发消息...`
              : `Message ${displayName || 'agent'}...`}
            value={inputValue}
            onChange={setInputValue}
            disabled={isSubmitting}
            isAgentRunning={agentStatus === 'running' || agentStatus === 'connecting' || agentStatus === 'disconnecting'}
            onStopAgent={handleStopAgent}
            agentName={agent.name}
            hideAttachments={false}
            bgColor='bg-muted-foreground/10'
            selectedAgentId={agent.agent_id}
            onAgentSelect={() => {
              toast.info("You can only test the agent you are currently configuring");
            }}
          />
        </div>
      </div>
    </div>
  );
};
