'use client';

import * as React from 'react';
import Link from 'next/link';
import { Bot, Menu, Store, Plus, Zap, ChevronRight, Loader2, GraduationCap, Home } from 'lucide-react';

import { NavAgents } from '@/components/sidebar/nav-agents';
import { NavUserWithTeams } from '@/components/sidebar/nav-user-with-teams';
import { KortixLogo } from '@/components/sidebar/kortix-logo';
// import { CTACard } from '@/components/sidebar/cta';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarTrigger,
  useSidebar,
} from '@/components/ui/sidebar';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { NewAgentDialog } from '@/components/agents/new-agent-dialog';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { createClient } from '@/lib/supabase/client';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { Button } from '@/components/ui/button';
import { useIsMobile } from '@/hooks/use-mobile';
import { cn } from '@/lib/utils';
import { usePathname, useSearchParams } from 'next/navigation';
import { useFeatureFlags } from '@/lib/feature-flags';
import posthog from 'posthog-js';
import { authClient } from '@/lib/auth/client';
import { useLanguage } from '@/contexts/LanguageContext';
// Floating mobile menu button component
function FloatingMobileMenuButton() {
  const { setOpenMobile, openMobile } = useSidebar();
  const isMobile = useIsMobile();
  const { t } = useLanguage();

  if (!isMobile || openMobile) return null;

  return (
    <div className="fixed top-6 left-4 z-50 md:hidden">
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            onClick={() => setOpenMobile(true)}
            size="icon"
            className="h-12 w-12 rounded-full bg-primary text-primary-foreground shadow-lg hover:bg-primary/90 transition-all duration-200 hover:scale-105 active:scale-95 touch-manipulation"
            aria-label={t('sidebar.openMenu')}
          >
            <Menu className="h-5 w-5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {t('sidebar.openMenu')}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

export function SidebarLeft({
  ...props
}: React.ComponentProps<typeof Sidebar>) {
  const { state, setOpen, setOpenMobile, toggleSidebar } = useSidebar();
  const isMobile = useIsMobile();
  const { language, t } = useLanguage();
  const isZh = language === 'zh';
  const isCollapsed = state === 'collapsed' && !isMobile;
  const [user, setUser] = useState<{
    name: string;
    email: string;
    avatar: string;
  }>({
    name: t('common.loading'),
    email: t('common.loading'),
    avatar: '',
  });

  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { flags, loading: flagsLoading } = useFeatureFlags(['custom_agents', 'agent_marketplace']);
  const customAgentsEnabled = flags.custom_agents;
  const marketplaceEnabled = flags.agent_marketplace;
  const [showNewAgentDialog, setShowNewAgentDialog] = useState(false);
  const [portalOrigin, setPortalOrigin] = useState('');

  // Close mobile menu on page navigation
  useEffect(() => {
    if (isMobile) {
      setOpenMobile(false);
    }
  }, [pathname, searchParams, isMobile, setOpenMobile]);

  
  useEffect(() => {
    const fetchUserData = async () => {
      try {
        const { data: { user: userData }, error } = await authClient.instance.getUser();
        
        if (userData && !error) {
          setUser({
            name: userData.name || userData.email?.split('@')[0] || 'User',
            email: userData.email || '',
            avatar: userData.metadata?.avatar_url || '',
          });
        }
      } catch (error) {
        console.error('Failed to fetch user data:', error);
        // 设置默认用户信息
        setUser({
          name: isZh ? '用户' : 'User',
          email: 'user@example.com',
          avatar: '',
        });
      }
    };

    fetchUserData();
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'b') {
        event.preventDefault();
        setOpen(!state.startsWith('expanded'));
        window.dispatchEvent(
          new CustomEvent('sidebar-left-toggled', {
            detail: { expanded: !state.startsWith('expanded') },
          }),
        );
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [state, setOpen]);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const { protocol, hostname } = window.location;
    if (protocol && hostname) {
      setPortalOrigin(`${protocol}//${hostname}`);
    }
  }, []);

  const tutorHref = portalOrigin ? `${portalOrigin}/tutor/` : '/tutor';
  const homeHref = portalOrigin ? `${portalOrigin}/` : '/';




  return (
    <Sidebar
      collapsible="icon"
      className={cn(
        // 边框和背景
        'border-r-0',
        // 毛玻璃效果
        'bg-sidebar/80 backdrop-blur-xl',
        'supports-[backdrop-filter]:bg-sidebar/80',
        // 隐藏滚动条
        '[&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]',
      )}
      {...props}
    >
      <SidebarHeader
        className={cn('py-2', isCollapsed ? 'pl-5 pr-3' : 'pl-4 pr-2')}
      >
        <div
          className={cn(
            'flex h-[40px] w-full items-center',
            isCollapsed && 'relative group/sidebar-header',
          )}
        >
          <Tooltip>
            <TooltipTrigger asChild>
              <Link
                href="/dashboard"
                className={cn(
                  'flex min-w-0 items-center transition-opacity duration-200',
                  isCollapsed
                    ? 'gap-0 group-hover/sidebar-header:opacity-0'
                    : 'gap-2',
                )}
                onClick={(e) => {
                  if (isCollapsed && !isMobile) {
                    e.preventDefault();
                    toggleSidebar();
                  } else if (isMobile) {
                    setOpenMobile(false);
                  }
                }}
              >
                <KortixLogo size={isCollapsed ? 16 : 24} />
                <span
                  className={cn(
                    'font-semibold text-lg whitespace-nowrap overflow-hidden transition-[max-width,opacity,transform] duration-200',
                    isCollapsed
                      ? 'max-w-0 opacity-0 -translate-x-2'
                      : 'max-w-[12rem] opacity-100 translate-x-0',
                  )}
                >
                  Roys Alpha
                </span>
              </Link>
            </TooltipTrigger>
            <TooltipContent side="right" hidden={!isCollapsed}>
              {t('sidebar.home')}
            </TooltipContent>
          </Tooltip>
          {!isMobile && (
            <div
              className={cn(
                'flex items-center',
                isCollapsed
                  ? 'absolute left-0 top-1/2 z-10 -translate-y-1/2 opacity-0 pointer-events-none transition-opacity duration-200 group-hover/sidebar-header:opacity-100 group-hover/sidebar-header:pointer-events-auto'
                  : 'ml-auto gap-2',
              )}
            >
              {isCollapsed ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={toggleSidebar}
                      aria-label={t('sidebar.expand')}
                      className="h-8 w-8"
                    >
                      <ChevronRight className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="right">{t('sidebar.expand')}</TooltipContent>
                </Tooltip>
              ) : (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <SidebarTrigger className="h-8 w-8" />
                  </TooltipTrigger>
                  <TooltipContent>{t('sidebar.toggle')}</TooltipContent>
                </Tooltip>
              )}
            </div>
          )}
        </div>
      </SidebarHeader>
      <SidebarContent className="[&::-webkit-scrollbar]:hidden [-ms-overflow-style:'none'] [scrollbar-width:'none']">
        <SidebarGroup>
          <Link href="/dashboard">
            <SidebarMenuButton 
              tooltip={t('sidebar.newTask')}
              className={cn('touch-manipulation', {
                'bg-accent text-accent-foreground font-medium': pathname === '/dashboard',
              })} 
              onClick={() => {
                posthog.capture('new_task_clicked');
                if (isMobile) setOpenMobile(false);
              }}
            >
              <Plus className="h-4 w-4 mr-1" />
              <span className="flex items-center justify-between w-full">
                {t('sidebar.newTask')}
              </span>
            </SidebarMenuButton>
          </Link>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton asChild tooltip={t('sidebar.tutor')}>
                <a
                  href={tutorHref}
                  onClick={() => {
                    if (isMobile) setOpenMobile(false);
                  }}
                >
                  <GraduationCap className="h-4 w-4 mr-1" />
                  <span>{t('sidebar.tutor')}</span>
                </a>
              </SidebarMenuButton>
            </SidebarMenuItem>
            <SidebarMenuItem>
              <SidebarMenuButton asChild tooltip={t('sidebar.home')}>
                <a
                  href={homeHref}
                  onClick={() => {
                    if (isMobile) setOpenMobile(false);
                  }}
                >
                  <Home className="h-4 w-4 mr-1" />
                  <span>{t('sidebar.home')}</span>
                </a>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
          {!flagsLoading && customAgentsEnabled && (
            <SidebarMenu>
              <Collapsible
                defaultOpen={pathname?.includes('/agents')}
                className="group/collapsible"
              >
                <SidebarMenuItem>
                  <CollapsibleTrigger asChild>
                    <SidebarMenuButton
                      tooltip={t('sidebar.agents')}
                      onClick={() => {
                        if (state === 'collapsed') {
                          setOpen(true);
                        }
                      }}
                    >
                      <Bot className="h-4 w-4 mr-1" />
                      <span>{t('sidebar.agents')}</span>
                      <ChevronRight className="ml-auto transition-transform duration-200 group-data-[state=open]/collapsible:rotate-90" />
                    </SidebarMenuButton>
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <SidebarMenuSub>
                      <SidebarMenuSubItem>
                        <SidebarMenuSubButton 
                          className="cursor-pointer pl-3 touch-manipulation"
                          onClick={() => {
                            toast.info(t('common.comingSoon'));
                            if (isMobile) setOpenMobile(false);
                          }}
                        >
                          <span>{t('sidebar.explore')}</span>
                        </SidebarMenuSubButton>
                      </SidebarMenuSubItem>
                      <SidebarMenuSubItem>
                        <SidebarMenuSubButton 
                          className="cursor-pointer pl-3 touch-manipulation"
                          onClick={() => {
                            toast.info(t('common.comingSoon'));
                            if (isMobile) setOpenMobile(false);
                          }}
                        >
                          <span>{t('sidebar.myAgents')}</span>
                        </SidebarMenuSubButton>
                      </SidebarMenuSubItem>
                      <SidebarMenuSubItem>
                        <SidebarMenuSubButton 
                          onClick={() => {
                            toast.info(t('common.comingSoon'));
                            if (isMobile) setOpenMobile(false);
                          }}
                          className="cursor-pointer pl-3 touch-manipulation"
                        >
                          <span>{t('sidebar.newAgent')}</span>
                        </SidebarMenuSubButton>
                      </SidebarMenuSubItem>
                    </SidebarMenuSub>
                  </CollapsibleContent>
                </SidebarMenuItem>
              </Collapsible>
            </SidebarMenu>
          )}

        </SidebarGroup>
        <NavAgents />
      </SidebarContent>
      {/* {state !== 'collapsed' && (
        <div className="px-3 py-2">
          <CTACard />
        </div>
      )} */}
      <SidebarFooter>
        <NavUserWithTeams user={user} />
      </SidebarFooter>
      <NewAgentDialog 
        open={showNewAgentDialog} 
        onOpenChange={setShowNewAgentDialog}
      />
    </Sidebar>
  );
}

// Export the floating button so it can be used in the layout
export { FloatingMobileMenuButton };
