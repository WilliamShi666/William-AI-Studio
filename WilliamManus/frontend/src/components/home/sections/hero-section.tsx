'use client';
import { HeroVideoSection } from '@/components/home/sections/hero-video-section';
import { siteConfig } from '@/lib/home';
import { ArrowRight, Github, X, AlertCircle, Square } from 'lucide-react';
import { FlickeringGrid } from '@/components/home/ui/flickering-grid';
import { useMediaQuery } from '@/hooks/use-media-query';
import { useState, useEffect, useRef, FormEvent } from 'react';
import { useScroll } from 'motion/react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/components/AuthProvider';
import { userCannotAccessRoysAlpha } from '@/lib/auth/access';
import {
  BillingError,
  AgentRunLimitError,
  type PreparedAttachment,
  type ShadowCloneMode,
} from '@/lib/api';
import { useInitiateAgentMutation } from '@/hooks/react-query/dashboard/use-initiate-agent';
import { generateThreadName } from '@/lib/actions/threads';
import { useAgents } from '@/hooks/react-query/agents/use-agents';

import { BillingErrorAlert } from '@/components/billing/usage-limit-alert';
import { useBillingError } from '@/hooks/useBillingError';
import { useAccounts } from '@/hooks/use-accounts';
import { isBillingUiEnabled, isLocalMode, config } from '@/lib/config';
import { toast } from 'sonner';
import { useModal } from '@/hooks/use-modal-store';
import { normalizeFilenameToNFC } from '@/lib/utils/unicode';
import { createQueryHook } from '@/hooks/use-query';
import { agentKeys } from '@/hooks/react-query/agents/keys';
import { getAgents } from '@/hooks/react-query/agents/utils';
import { AgentRunLimitDialog } from '@/components/thread/agent-run-limit-dialog';
import { useLanguage } from '@/contexts/LanguageContext';
import { ParticleField } from '@/components/home/ui/particle-field';
import type { ChatInputHandles } from '@/components/thread/chat-input/chat-input';



// Constant for localStorage key to ensure consistency
const PENDING_PROMPT_KEY = 'pendingAgentPrompt';



export function HeroSection() {
  const billingUiEnabled = isBillingUiEnabled();
  const { hero } = siteConfig;
  const tablet = useMediaQuery('(max-width: 1024px)');
  const [mounted, setMounted] = useState(false);
  const [isScrolling, setIsScrolling] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const scrollTimeout = useRef<NodeJS.Timeout | null>(null);
  const { scrollY } = useScroll();
  const [inputValue, setInputValue] = useState('');
  const [selectedAgentId, setSelectedAgentId] = useState<string | undefined>();
  const router = useRouter();
  const { user, isLoading } = useAuth();
  const { billingError, handleBillingError, clearBillingError } =
    useBillingError();
  const { data: accounts } = useAccounts();
  const personalAccount = accounts?.find((account) => account.personal_account);
  const { onOpen } = useModal();
  const { language, t } = useLanguage();
  const isZh = language === 'zh';
  const initiateAgentMutation = useInitiateAgentMutation();
  const chatInputRef = useRef<ChatInputHandles>(null);
  const [showAgentLimitDialog, setShowAgentLimitDialog] = useState(false);
  const [agentLimitData, setAgentLimitData] = useState<{
    runningCount: number;
    runningThreadIds: string[];
  } | null>(null);
  const [showLoginPrompt, setShowLoginPrompt] = useState(false);
  const [pendingProjectName, setPendingProjectName] = useState('');
  const [pendingTargetUrl, setPendingTargetUrl] = useState('');
  const [portalOrigin, setPortalOrigin] = useState<string>('');

  // Fetch agents for selection
  const { data: agentsResponse } = createQueryHook(
    agentKeys.list({
      limit: 100,
      sort_by: 'name',
      sort_order: 'asc'
    }),
    () => getAgents({
      limit: 100,
      sort_by: 'name',
      sort_order: 'asc'
    }),
    {
      enabled: !!user && !isLoading,
      staleTime: 5 * 60 * 1000,
      gcTime: 10 * 60 * 1000,
    }
  )();

  const agents = agentsResponse?.agents || [];



  useEffect(() => {
    setMounted(true);
  }, []);

  // Set portal origin for navigation (preserves current port for dev/prod separation)
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const { protocol, hostname, port } = window.location;
    if (protocol && hostname) {
      // 保留端口号，确保开发模式(3001)和生产模式(3000/80)分离
      const portSuffix = port ? `:${port}` : '';
      setPortalOrigin(`${protocol}//${hostname}${portSuffix}`);
    }
  }, []);

  // Detect when scrolling is active to reduce animation complexity
  useEffect(() => {
    const unsubscribe = scrollY.on('change', () => {
      setIsScrolling(true);

      // Clear any existing timeout
      if (scrollTimeout.current) {
        clearTimeout(scrollTimeout.current);
      }

      // Set a new timeout
      scrollTimeout.current = setTimeout(() => {
        setIsScrolling(false);
      }, 300); // Wait 300ms after scroll stops
    });

    return () => {
      unsubscribe();
      if (scrollTimeout.current) {
        clearTimeout(scrollTimeout.current);
      }
    };
  }, [scrollY]);

  // Handle project card click
  const handleProjectClick = (targetUrl: string, projectName: string) => {
    if (!user && !isLoading) {
      // User not logged in, show prompt
      setPendingProjectName(projectName);
      setPendingTargetUrl(targetUrl);
      setShowLoginPrompt(true);
      return;
    }

    if (targetUrl === '/dashboard' && user && userCannotAccessRoysAlpha(user)) {
      toast.error(
        isZh
          ? '您没有访问该项目的权限。'
          : 'You do not have permission to access this project.',
      );
      return;
    }

    // User is logged in, navigate using full URL to ensure Nginx routing (port 80)
    const fullUrl = portalOrigin ? `${portalOrigin}${targetUrl}` : targetUrl;
    window.location.href = fullUrl;
  };

  // Handle ChatInput submission
  const handleChatInputSubmit = async (
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
    }
  ) => {
    const preparedAttachments = options?.prepared_attachments ?? [];
    const hasPreparedAttachments = preparedAttachments.length > 0;
    if (
      (!message.trim() &&
        !hasPreparedAttachments &&
        !chatInputRef.current?.getPendingFiles().length) ||
      isSubmitting
    ) return;

    // If user is not logged in, save prompt and redirect to login
    if (!user && !isLoading) {
      localStorage.setItem(PENDING_PROMPT_KEY, message.trim());
      router.push(`/auth?returnUrl=${encodeURIComponent('/dashboard')}`);
      return;
    }

    // User is logged in, create the agent with files like dashboard does
    const files = hasPreparedAttachments
      ? []
      : chatInputRef.current?.getPendingFiles() || [];
    setIsSubmitting(true);
    try {
      localStorage.removeItem(PENDING_PROMPT_KEY);

      const formData = new FormData();
      formData.append('prompt', message);

      // Add selected agent if one is chosen
      if (selectedAgentId) {
        formData.append('agent_id', selectedAgentId);
      }

      if (options?.project_id) {
        formData.append('project_id', options.project_id);
      }
      if (hasPreparedAttachments) {
        formData.append(
          'prepared_attachments_json',
          JSON.stringify(preparedAttachments),
        );
      }

      // Add files if any
      files.forEach((file) => {
        const normalizedName = normalizeFilenameToNFC(file.name);
        formData.append('files', file, normalizedName);
      });

      if (options?.model_name) formData.append('model_name', options.model_name);

      // 检测是否为 Gemini 3 模型 - 自动启用思考模式
      const isGemini3Model = options?.model_name?.toLowerCase().includes('gemini-3') ?? false;
      const thinkingEnabled = isGemini3Model || (options?.enable_thinking ?? false);

      formData.append('enable_thinking', String(thinkingEnabled));
      formData.append('reasoning_effort', thinkingEnabled ? 'high' : 'low');
      formData.append('stream', 'true');
      formData.append('enable_context_manager', 'false');

      const result = await initiateAgentMutation.mutateAsync(formData);

      if (result.thread_id) {
        router.push(`/agents/${result.thread_id}`);
      } else {
        throw new Error(t('common.operationFailed'));
      }

      chatInputRef.current?.clearPendingFiles();
      setInputValue('');
    } catch (error: any) {
      if (error instanceof BillingError) {
        if (billingUiEnabled) {
          onOpen("paymentRequiredDialog");
        } else {
          toast.error(t('notice.unavailableDescription'));
        }
      } else if (error instanceof AgentRunLimitError) {
        const { running_thread_ids, running_count } = error.detail;
        
        setAgentLimitData({
          runningCount: running_count,
          runningThreadIds: running_thread_ids,
        });
        setShowAgentLimitDialog(true);
      } else {
        const isConnectionError =
          error instanceof TypeError &&
          error.message.includes('Failed to fetch');
        if (!isLocalMode() || isConnectionError) {
          toast.error(
            error.message || (isZh
              ? '创建代理失败，请稍后重试。'
              : 'Failed to create agent. Please try again.'),
          );
        }
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <section
      id="hero"
      className="w-full relative z-0 min-h-screen overflow-hidden bg-[#061a32] text-white"
    >
      <ParticleField />
      <div className="absolute inset-0 -z-30 [background:radial-gradient(circle_at_1px_1px,rgba(255,255,255,0.08)_1px,transparent_0)] [background-size:42px_42px]" />
      <div className="absolute inset-0 -z-20 bg-[radial-gradient(circle_at_18%_18%,rgba(90,165,255,0.28),transparent_42%),radial-gradient(circle_at_82%_12%,rgba(88,200,255,0.22),transparent_38%),radial-gradient(circle_at_50%_70%,rgba(70,140,235,0.22),transparent_60%)]" />
      <div
        className="hidden sm:block pointer-events-none absolute left-0 bottom-0 h-[100vh] sm:h-[110vh] md:h-[120vh] w-[85%] sm:w-[75%] md:w-[66%] lg:w-[60%] -z-10 overflow-hidden"
        style={{
          WebkitMaskImage:
            'radial-gradient(170% 155% at 0% 100%, rgba(0,0,0,1) 0%, rgba(0,0,0,0.88) 30%, rgba(0,0,0,0.5) 55%, rgba(0,0,0,0.18) 70%, transparent 86%), linear-gradient(to right, rgba(0,0,0,1) 0%, rgba(0,0,0,0.92) 45%, rgba(0,0,0,0.45) 65%, rgba(0,0,0,0.15) 78%, transparent 90%), linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0.82) 38%, rgba(0,0,0,0.35) 60%, transparent 84%)',
          maskImage:
            'radial-gradient(170% 155% at 0% 100%, rgba(0,0,0,1) 0%, rgba(0,0,0,0.88) 30%, rgba(0,0,0,0.5) 55%, rgba(0,0,0,0.18) 70%, transparent 86%), linear-gradient(to right, rgba(0,0,0,1) 0%, rgba(0,0,0,0.92) 45%, rgba(0,0,0,0.45) 65%, rgba(0,0,0,0.15) 78%, transparent 90%), linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0.82) 38%, rgba(0,0,0,0.35) 60%, transparent 84%)',
          WebkitMaskRepeat: 'no-repeat',
          maskRepeat: 'no-repeat',
          WebkitMaskSize: '100% 100%',
          maskSize: '100% 100%',
          WebkitMaskComposite: 'source-in, source-in',
          maskComposite: 'intersect, intersect',
        }}
      >
        {mounted && (
          <FlickeringGrid
            className="h-full w-full [filter:drop-shadow(0_0_26px_rgba(150,110,255,0.6))]"
            squareSize={tablet ? 2 : 2.5}
            gridGap={tablet ? 2 : 2.5}
            color="rgb(232, 226, 255)"
            maxOpacity={tablet ? 0.22 : 0.32}
            flickerChance={isScrolling ? 0.006 : (tablet ? 0.02 : 0.035)} // Lower performance impact on mobile
          />
        )}
      </div>
      <div
        className="hidden sm:block pointer-events-none absolute right-0 bottom-0 h-[100vh] sm:h-[110vh] md:h-[120vh] w-[85%] sm:w-[75%] md:w-[66%] lg:w-[60%] -z-10 overflow-hidden"
        style={{
          WebkitMaskImage:
            'radial-gradient(170% 155% at 100% 100%, rgba(0,0,0,1) 0%, rgba(0,0,0,0.88) 30%, rgba(0,0,0,0.5) 55%, rgba(0,0,0,0.18) 70%, transparent 86%), linear-gradient(to left, rgba(0,0,0,1) 0%, rgba(0,0,0,0.92) 45%, rgba(0,0,0,0.45) 65%, rgba(0,0,0,0.15) 78%, transparent 90%), linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0.82) 38%, rgba(0,0,0,0.35) 60%, transparent 84%)',
          maskImage:
            'radial-gradient(170% 155% at 100% 100%, rgba(0,0,0,1) 0%, rgba(0,0,0,0.88) 30%, rgba(0,0,0,0.5) 55%, rgba(0,0,0,0.18) 70%, transparent 86%), linear-gradient(to left, rgba(0,0,0,1) 0%, rgba(0,0,0,0.92) 45%, rgba(0,0,0,0.45) 65%, rgba(0,0,0,0.15) 78%, transparent 90%), linear-gradient(to top, rgba(0,0,0,1) 0%, rgba(0,0,0,0.82) 38%, rgba(0,0,0,0.35) 60%, transparent 84%)',
          WebkitMaskRepeat: 'no-repeat',
          maskRepeat: 'no-repeat',
          WebkitMaskSize: '100% 100%',
          maskSize: '100% 100%',
          WebkitMaskComposite: 'source-in, source-in',
          maskComposite: 'intersect, intersect',
        }}
      >
        {mounted && (
          <FlickeringGrid
            className="h-full w-full [filter:drop-shadow(0_0_26px_rgba(150,110,255,0.6))]"
            squareSize={tablet ? 2 : 2.5}
            gridGap={tablet ? 2 : 2.5}
            color="rgb(232, 226, 255)"
            maxOpacity={tablet ? 0.22 : 0.32}
            flickerChance={isScrolling ? 0.006 : (tablet ? 0.02 : 0.035)} // Lower performance impact on mobile
          />
        )}
      </div>
      <div className="relative flex flex-col items-center w-full px-4 sm:px-6">

        <div className="relative z-10 pt-16 sm:pt-24 md:pt-32 mx-auto h-full w-full max-w-6xl flex flex-col items-center justify-center">
          {/* <p className="border border-border bg-accent rounded-full text-sm h-8 px-3 flex items-center gap-2">
            {hero.badgeIcon}
            {hero.badge}
          </p> */}

          {/* <Link
            href={hero.githubUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="group border border-border/50 bg-background hover:bg-accent/20 hover:border-secondary/40 rounded-full text-sm h-8 px-3 flex items-center gap-2 transition-all duration-300 shadow-sm hover:shadow-md hover:scale-105 hover:-translate-y-0.5"
          >
            {hero.badgeIcon}
            <span className="font-medium text-muted-foreground text-xs tracking-wide group-hover:text-primary transition-colors duration-300">
              {hero.badge}
            </span>
            <span className="inline-flex items-center justify-center size-3.5 rounded-full bg-muted/30 group-hover:bg-secondary/30 transition-colors duration-300">
              <svg
                width="8"
                height="8"
                viewBox="0 0 24 24"
                fill="none"
                xmlns="http://www.w3.org/2000/svg"
                className="text-muted-foreground group-hover:text-primary"
              >
                <path
                  d="M7 17L17 7M17 7H8M17 7V16"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>
          </Link> */}
          <div className="flex flex-col items-center justify-center gap-3 sm:gap-4 pt-8 sm:pt-12 max-w-4xl mx-auto">
            <h1 className="text-3xl md:text-4xl lg:text-5xl xl:text-6xl font-semibold tracking-tighter text-center px-2 whitespace-nowrap drop-shadow-[0_8px_28px_rgba(31,99,255,0.35)]">
              <span className="text-white">William老师的</span>
              <span className="ml-2 bg-gradient-to-r from-[#79d0ff] via-[#4b8bff] to-[#163b9a] bg-clip-text text-transparent">
                AI梦工厂
              </span>
            </h1>
            <p className="text-base md:text-lg text-center text-[#c5cfdf] font-medium text-balance leading-relaxed tracking-tight max-w-2xl px-2">
              和您的AI团队并肩作战！
            </p>
          </div>

          {/* Navigation Cards Section */}
          <div className="flex flex-col md:flex-row items-stretch w-full max-w-5xl mx-auto gap-4 md:gap-6 px-4 sm:px-6 mt-12">
            {/* Left Card - RoysLegion */}
            <div
              onClick={() => handleProjectClick('/tutor/', 'RoysLegion')}
              className="flex-1 p-6 md:p-8 rounded-2xl border border-white/10 bg-white/5 backdrop-blur-sm hover:bg-white/10 hover:border-[#6bb6ff]/60 transition-all duration-300 cursor-pointer group shadow-lg shadow-[rgba(17,24,39,0.35)]"
            >
              <div className="flex flex-col h-full">
                <h3 className="text-xl md:text-2xl font-semibold text-white group-hover:text-[#8ad4ff] transition-colors mb-3">
                  RoysLegion
                </h3>
                <p className="text-sm md:text-base text-[#c1cadd] leading-relaxed">
                  乐亦思国际学科助教军团，多学科AI助教如千军万马解答疑难
                </p>
                <div className="mt-auto pt-4">
                  <span className="inline-flex items-center text-sm text-[#7bbdff] group-hover:text-[#9dd7ff] group-hover:translate-x-1 transition-all">
                    进入项目 →
                  </span>
                </div>
              </div>
            </div>

            <div
              onClick={() => handleProjectClick('/dashboard', 'Roys Alpha')}
              className="flex-1 p-6 md:p-8 rounded-2xl border border-white/10 bg-white/5 backdrop-blur-sm hover:bg-white/10 hover:border-[#6bb6ff]/60 transition-all duration-300 cursor-pointer group shadow-lg shadow-[rgba(17,24,39,0.35)]"
            >
              <div className="flex flex-col h-full">
                <h3 className="text-xl md:text-2xl font-semibold text-white group-hover:text-[#8ad4ff] transition-colors mb-3">
                  Roys Alpha
                </h3>
                <p className="text-sm md:text-base text-[#c1cadd] leading-relaxed">
                  乐亦思Manus类通用智能体，自主帮您搞定多类任务
                </p>
                <div className="mt-auto pt-4">
                  <span className="inline-flex items-center text-sm text-[#7bbdff] group-hover:text-[#9dd7ff] group-hover:translate-x-1 transition-all">
                    进入项目 →
                  </span>
                </div>
              </div>
            </div>

            {/* Right Card - Claude Code UI */}
            <Link
              href="/claude-code-ui"
              className="flex-1 p-6 md:p-8 rounded-2xl border border-[#d97757]/30 bg-[linear-gradient(135deg,rgba(251,249,246,0.12),rgba(217,119,87,0.08))] backdrop-blur-sm hover:bg-white/10 hover:border-[#d97757]/70 transition-all duration-300 cursor-pointer group shadow-lg shadow-[rgba(217,119,87,0.14)]"
            >
              <div className="flex flex-col h-full">
                <h3 className="text-xl md:text-2xl font-semibold text-white group-hover:text-[#f0c7b2] transition-colors mb-3">
                  Claude Code UI
                </h3>
                <p className="text-sm md:text-base text-[#d9d1c7] leading-relaxed">
                  把 Claude Code 的运行轨迹变成可审计、可回放、可下载的玻璃匣子。
                </p>
                <div className="mt-auto pt-4">
                  <span className="inline-flex items-center text-sm text-[#f0a07a] group-hover:text-[#ffd6c2] group-hover:translate-x-1 transition-all">
                    了解并下载源码 →
                  </span>
                </div>
              </div>
            </Link>
          </div>

        </div>

      </div>
        <div className="mb-8 sm:mb-16 sm:mt-32 mx-auto"></div>



      {/* Add Billing Error Alert here */}
      {billingUiEnabled && (
        <BillingErrorAlert
          message={billingError?.message}
          currentUsage={billingError?.currentUsage}
          limit={billingError?.limit}
          accountId={personalAccount?.account_id}
          onDismiss={clearBillingError}
          isOpen={!!billingError}
        />
      )}

      {agentLimitData && (
        <AgentRunLimitDialog
          open={showAgentLimitDialog}
          onOpenChange={setShowAgentLimitDialog}
          runningCount={agentLimitData.runningCount}
          runningThreadIds={agentLimitData.runningThreadIds}
          projectId={undefined} // Hero section doesn't have a specific project context
        />
      )}

      {/* Login Prompt Dialog */}
      {showLoginPrompt && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => setShowLoginPrompt(false)}
          />
          <div className="relative bg-background border border-border rounded-xl p-6 max-w-md mx-4 shadow-xl">
            <h3 className="text-lg font-semibold text-primary mb-2">
              需要登录
            </h3>
            <p className="text-muted-foreground mb-6">
              请先登录以访问「{pendingProjectName}」功能
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowLoginPrompt(false)}
                className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-accent transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => {
                  setShowLoginPrompt(false);
                  // Build full URL and pass as returnUrl to redirect after login
                  const fullUrl = portalOrigin ? `${portalOrigin}${pendingTargetUrl}` : pendingTargetUrl;
                  router.push(`/auth?returnUrl=${encodeURIComponent(fullUrl)}`);
                }}
                className="px-4 py-2 text-sm bg-secondary text-secondary-foreground rounded-lg hover:bg-secondary/80 transition-colors"
              >
                前往登录
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
