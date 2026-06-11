import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  type AgentRun,
  Project,
  resolveFileDeliverySource,
  resolveThreadWorkspaceFileDeliverySource,
} from '@/lib/api';
import { useThreadQuery } from '@/hooks/react-query/threads/use-threads';
import { useMessagesQuery } from '@/hooks/react-query/threads/use-messages';
import { useProjectQuery } from '@/hooks/react-query/threads/use-project';
import { useAgentRunsQuery } from '@/hooks/react-query/threads/use-agent-run';
import { useShadowCloneStore } from '@/lib/stores/shadow-clone-store';
import { ApiMessageType, UnifiedMessage, AgentStatus } from '../_types';
import { shouldAdoptLatestRunningRun } from '../[threadId]/agent-stream-run-guard';
import { areThreadMessagesEqual, mergeThreadMessages, normalizeThreadMessages } from './thread-message-merge';
import {
  getAgentRunSortTimestamp,
  selectPreferredAgentRun,
  selectPreferredFileDeliveryRun,
  selectShadowCloneBootstrapRun,
} from './file-delivery-run-selection';

const THREAD_DATA_DEBUG_ENABLED =
  process.env.NEXT_PUBLIC_AGENT_STREAM_DEBUG === 'true';

const logThreadDataDebug = (...args: unknown[]) => {
  if (!THREAD_DATA_DEBUG_ENABLED) return;
  console.log(...args);
};

interface UseThreadDataReturn {
  messages: UnifiedMessage[];
  setMessages: React.Dispatch<React.SetStateAction<UnifiedMessage[]>>;
  project: Project | null;
  sandboxId: string | null;
  projectName: string;
  agentRunId: string | null;
  setAgentRunId: React.Dispatch<React.SetStateAction<string | null>>;
  agentStatus: AgentStatus;
  setAgentStatus: React.Dispatch<React.SetStateAction<AgentStatus>>;
  isLoading: boolean;
  error: string | null;
  initialLoadCompleted: boolean;
  threadQuery: ReturnType<typeof useThreadQuery>;
  messagesQuery: ReturnType<typeof useMessagesQuery>;
  projectQuery: ReturnType<typeof useProjectQuery>;
  agentRunsQuery: ReturnType<typeof useAgentRunsQuery>;
}

export function useThreadData(threadId: string, projectId: string): UseThreadDataReturn {
  const [messages, setMessages] = useState<UnifiedMessage[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [sandboxId, setSandboxId] = useState<string | null>(null);
  const [projectName, setProjectName] = useState<string>('');
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [agentStatus, setAgentStatus] = useState<AgentStatus>('idle');
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // 🎯 添加流式保护标志
  const isStreamingOrRecentlyStreamedRef = useRef(false);
  
  const initialLoadCompleted = useRef<boolean>(false);
  const messagesLoadedRef = useRef(false);
  const agentRunsCheckedRef = useRef(false);
  const hasInitiallyScrolled = useRef<boolean>(false);
  const previousThreadIdRef = useRef<string | null>(null);
  

  const threadQuery = useThreadQuery(threadId);
  const messagesQuery = useMessagesQuery(threadId);

  // 调试React Query状态
  console.log('🔎 [useThreadData] messagesQuery完整状态:', {
    data: messagesQuery.data,
    status: messagesQuery.status,
    isLoading: messagesQuery.isLoading,
    isFetching: messagesQuery.isFetching,
    isError: messagesQuery.isError,
    error: messagesQuery.error,
    enabled: !!threadId,
    threadId
  });
  const projectQuery = useProjectQuery(projectId);
  const agentRunsQuery = useAgentRunsQuery(threadId);
  
  // 🎯 监听agentStatus变化，在开始/结束时刷新项目数据
  const prevAgentStatusForProjectRef = useRef<AgentStatus>('idle');
  useEffect(() => {
    const currentStatus = agentStatus;
    const prevStatus = prevAgentStatusForProjectRef.current;
    
    // 🔍 调试：始终显示状态变化
    if (prevStatus !== currentStatus) {
      console.log(`🔄 [useThreadData] AgentStatus变化: ${prevStatus} → ${currentStatus}`);
    }
    
    const startingRun =
      (prevStatus === 'idle' || prevStatus === 'error') &&
      (currentStatus === 'running' || currentStatus === 'connecting');
    const endingRun =
      (prevStatus === 'running' || prevStatus === 'connecting') &&
      (currentStatus === 'idle' || currentStatus === 'error');

    if (startingRun) {
      console.log('🚀 [useThreadData] Agent开始运行，立即刷新项目数据获取VNC配置');
      projectQuery.refetch();
    } else if (endingRun) {
      console.log('✅ [useThreadData] Agent结束运行，刷新项目数据同步沙盒状态');
      projectQuery.refetch();
    }
    
    prevAgentStatusForProjectRef.current = currentStatus;
  }, [agentStatus, projectQuery]);
  
  // 🎯 管理流式保护标志
  useEffect(() => {
    if (agentStatus === 'running' || agentStatus === 'connecting') {
      console.log('🛡️ [useThreadData] 启用流式保护 - agentStatus:', agentStatus);
      isStreamingOrRecentlyStreamedRef.current = true;
    } else if (agentStatus === 'idle') {
      // 延迟清除保护标志，给消息状态稳定一些时间
      console.log('⏰ [useThreadData] 5秒后清除流式保护');
      setTimeout(() => {
        console.log('🔓 [useThreadData] 清除流式保护');
        isStreamingOrRecentlyStreamedRef.current = false;
      }, 5000); // 增加到5秒
    }
  }, [agentStatus]);
  
  // (debug logs removed)

  useEffect(() => {
    if (previousThreadIdRef.current === threadId) {
      return;
    }

    previousThreadIdRef.current = threadId;
    agentRunsCheckedRef.current = false;
    messagesLoadedRef.current = false;
    initialLoadCompleted.current = false;
    hasInitiallyScrolled.current = false;
    isStreamingOrRecentlyStreamedRef.current = false;

    setMessages([]);
    setProject(null);
    setSandboxId(null);
    setProjectName('');
    setAgentRunId(null);
    setAgentStatus('idle');
    setError(null);
    setIsLoading(true);
  }, [threadId]);

  useEffect(() => {
    let isMounted = true;

    async function initializeData() {
      if (!initialLoadCompleted.current) setIsLoading(true);
      setError(null);
      try {
        if (!threadId) throw new Error('Thread ID is required');

        if (threadQuery.isError) {
          throw new Error('Failed to load thread data: ' + threadQuery.error);
        }
        if (!isMounted) return;

        if (projectQuery.data) {
          const rawSandboxId =
            typeof projectQuery.data.sandbox === 'string'
              ? projectQuery.data.sandbox
              : projectQuery.data.sandbox?.id || null;
          const activeAgentRun = selectPreferredFileDeliveryRun(
            agentRunsQuery.data,
            agentRunId,
            threadId,
          );
          const fileDeliverySource =
            activeAgentRun?.file_delivery_source?.identitySource === 'thread_workspace_artifacts'
              ? resolveThreadWorkspaceFileDeliverySource(threadId)
              : resolveFileDeliverySource(
                  activeAgentRun?.file_delivery_source ?? null,
                  projectQuery.data,
                  rawSandboxId,
                );
          const normalizedProject: Project = {
            ...projectQuery.data,
            file_delivery_source: fileDeliverySource,
          };

          setProject(normalizedProject);
          setSandboxId(fileDeliverySource.browseSandboxId || null);
          setProjectName(normalizedProject.name || '');
        }

        if (messagesQuery.data && isMounted) {
          const unifiedMessages = normalizeThreadMessages(messagesQuery.data, threadId) as UnifiedMessage[];
          setMessages((prev) => {
            const mergedMessages = mergeThreadMessages(prev, unifiedMessages) as UnifiedMessage[];
            if (areThreadMessagesEqual(prev, mergedMessages)) {
              return prev;
            }
            return mergedMessages;
          });
          messagesLoadedRef.current = true;

          if (!hasInitiallyScrolled.current) {
            hasInitiallyScrolled.current = true;
          }
        }

        if (agentRunsQuery.data && isMounted) {
          const shouldProcessInitialAgentRuns = !agentRunsCheckedRef.current;
          if (shouldProcessInitialAgentRuns) {
            logThreadDataDebug('🔍 [useThreadData] Processing agent runs:', {
              total: agentRunsQuery.data.length,
              statuses: agentRunsQuery.data.map(r => ({ id: r.id, status: r.status }))
            });
            agentRunsCheckedRef.current = true;
          }
          
          // Check for any running agents - only connect streams to RUNNING agents.
          // Completed Shadow Clone V2 runs still need a one-shot status bootstrap
          // so historical reloads can restore the team list and subagent panels.
          const runningRuns = agentRunsQuery.data.filter(r => r.status === 'running');
          const shadowCloneBootstrapRun = selectShadowCloneBootstrapRun(
            agentRunsQuery.data,
            agentRunId,
            threadId,
          );
          if (shouldProcessInitialAgentRuns) {
            logThreadDataDebug('🏃 [useThreadData] Running agent runs:', runningRuns.length);
            logThreadDataDebug('🧬 [useThreadData] Shadow Clone bootstrap run:', shadowCloneBootstrapRun?.id || null);
          }
          
          if (runningRuns.length > 0) {
            const latestRunning =
              selectPreferredAgentRun(runningRuns, agentRunId, threadId) ?? runningRuns[0];
            const shouldAdoptRunningRun =
              shouldProcessInitialAgentRuns ||
              shouldAdoptLatestRunningRun({
                latestRunningRunId: latestRunning.id,
                currentAgentRunId: agentRunId,
                agentStatus,
                isStreamingOrRecentlyStreamed: isStreamingOrRecentlyStreamedRef.current,
              });

            if (shouldAdoptRunningRun) {
              logThreadDataDebug('✅ [useThreadData] Found running agent:', latestRunning.id);
              setAgentRunId(latestRunning.id);
              void useShadowCloneStore.getState().syncRunData(latestRunning.id);
              setAgentStatus((current) => {
                if (current !== 'running') {
                  logThreadDataDebug('✅ [useThreadData] Changed agentStatus to RUNNING');
                  return 'running';
                }
                return current;
              });
            }
          } else if (shadowCloneBootstrapRun && shouldProcessInitialAgentRuns) {
            logThreadDataDebug(
              '🧬 [useThreadData] Restoring completed Shadow Clone V2 state:',
              shadowCloneBootstrapRun.id,
            );
            void useShadowCloneStore.getState().syncRunData(
              shadowCloneBootstrapRun.id,
              { force: true },
            );
            setAgentStatus((current) => (current === 'idle' ? current : 'idle'));
            setAgentRunId(null);
          } else if (shouldProcessInitialAgentRuns) {
            const hasActiveRunSignals =
              isStreamingOrRecentlyStreamedRef.current ||
              agentStatus === 'running' ||
              agentStatus === 'connecting' ||
              Boolean(agentRunId);

            if (hasActiveRunSignals) {
              console.log(
                '⏭️ [useThreadData] Skip idle reset because active stream signals still exist',
                {
                  isStreamingOrRecentlyStreamed: isStreamingOrRecentlyStreamedRef.current,
                  agentStatus,
                  agentRunId,
                },
              );
            } else {
              // For historical conversations, don't set any agentRunId
              console.log('💤 [useThreadData] No running agents found - this is likely a historical conversation');
              setAgentStatus((current) => {
                if (current !== 'idle') {
                  console.log('✅ [useThreadData] Changed agentStatus to IDLE');
                  return 'idle';
                }
                return current;
              });
              // Explicitly clear any previous agentRunId to prevent streaming attempts
              setAgentRunId(null);
            }
          }
        }

        if (threadQuery.data && messagesQuery.data && agentRunsQuery.data) {
          initialLoadCompleted.current = true;
          setIsLoading(false);
          // Removed time-based final check to avoid incorrectly forcing idle while a stream is active
        }

      } catch (err) {
        console.error('Error loading thread data:', err);
        if (isMounted) {
          const errorMessage =
            err instanceof Error ? err.message : 'Failed to load thread';
          setError(errorMessage);
          toast.error(errorMessage);
          setIsLoading(false);
        }
      }
    }

    if (threadId) {
      initializeData();
    }

    return () => {
      isMounted = false;
    };
  }, [
    threadId,
    threadQuery.data,
    threadQuery.isError,
    threadQuery.error,
    projectQuery.data,
    messagesQuery.data,
    agentRunsQuery.data,
    agentRunId,
    agentStatus,
  ]);

  // Message updates are merged in initializeData whenever messagesQuery.data changes.

  return {
    messages,
    setMessages,
    project,
    sandboxId,
    projectName,
    agentRunId,
    setAgentRunId,
    agentStatus,
    setAgentStatus,
    isLoading,
    error,
    initialLoadCompleted: initialLoadCompleted.current,
    threadQuery,
    messagesQuery,
    projectQuery,
    agentRunsQuery,
  };
} 
