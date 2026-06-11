'use client';

import * as React from 'react';
import { useRouter } from 'next/navigation';
import {
  BadgeCheck,
  Bell,
  ChevronDown,
  ChevronsUpDown,
  Command,
  CreditCard,
  Key,
  LogOut,
  Plus,
  Settings,
  User,
  AudioWaveform,
  Sun,
  Moon,
  KeyRound,
  Plug,
  Languages,
} from 'lucide-react';
import { useAccounts } from '@/hooks/use-accounts';
import NewTeamForm from '@/components/basejump/new-team-form';
import { toast } from 'sonner';
import { useAuth } from '@/components/AuthProvider';

import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from '@/components/ui/sidebar';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { useTheme } from 'next-themes';
import { isBillingUiEnabled, isLocalMode } from '@/lib/config';
import { useFeatureFlag } from '@/lib/feature-flags';
import { useLanguage } from '@/contexts/LanguageContext';

export function NavUserWithTeams({
  user,
}: {
  user: {
    name: string;
    email: string;
    avatar: string;
  };
}) {
  const router = useRouter();
  const { isMobile } = useSidebar();
  const { data: accounts } = useAccounts();
  const [showNewTeamDialog, setShowNewTeamDialog] = React.useState(false);
  const { theme, setTheme } = useTheme();
  const { signOut } = useAuth();
  const { enabled: customAgentsEnabled, loading: flagLoading } = useFeatureFlag("custom_agents");
  const { language, setLanguage, t } = useLanguage();
  const billingUiEnabled = isBillingUiEnabled();

  // Prepare personal account and team accounts
  const personalAccount = React.useMemo(
    () => accounts?.find((account) => account.personal_account),
    [accounts],
  );
  const teamAccounts = React.useMemo(
    () => accounts?.filter((account) => !account.personal_account),
    [accounts],
  );

  // Create a default list of teams with logos for the UI (will show until real data loads)
  const defaultTeams = [
    {
      name: personalAccount?.name || 'Personal Account',
      logo: Command,
      plan: 'Personal',
      account_id: personalAccount?.account_id,
      slug: personalAccount?.slug,
      personal_account: true,
    },
    ...(teamAccounts?.map((team) => ({
      name: team.name,
      logo: AudioWaveform,
      plan: 'Team',
      account_id: team.account_id,
      slug: team.slug,
      personal_account: false,
    })) || []),
  ];

  // Use the first team or first entry in defaultTeams as activeTeam
  const [activeTeam, setActiveTeam] = React.useState(defaultTeams[0]);

  // Update active team when accounts load
  React.useEffect(() => {
    if (accounts?.length) {
      const currentTeam = accounts.find(
        (account) => account.account_id === activeTeam.account_id,
      );
      if (currentTeam) {
        setActiveTeam({
          name: currentTeam.name,
          logo: currentTeam.personal_account ? Command : AudioWaveform,
          plan: currentTeam.personal_account ? 'Personal' : 'Team',
          account_id: currentTeam.account_id,
          slug: currentTeam.slug,
          personal_account: currentTeam.personal_account,
        });
      } else {
        // If current team not found, set first available account as active
        const firstAccount = accounts[0];
        setActiveTeam({
          name: firstAccount.name,
          logo: firstAccount.personal_account ? Command : AudioWaveform,
          plan: firstAccount.personal_account ? 'Personal' : 'Team',
          account_id: firstAccount.account_id,
          slug: firstAccount.slug,
          personal_account: firstAccount.personal_account,
        });
      }
    }
  }, [accounts, activeTeam.account_id]);

  // Handle team selection
  const handleTeamSelect = (team) => {
    setActiveTeam(team);

    // Navigate to the appropriate dashboard
    if (team.personal_account) {
      router.push('/dashboard');
    } else {
      router.push(`/${team.slug}`);
    }
  };

  const handleLogout = async () => {
    console.log('🚀 Starting logout process...');
    try {
      // Step 1: 获取存储的token
      const accessToken = localStorage.getItem('access_token');
      const refreshToken = localStorage.getItem('refresh_token');
      
      // Step 2: 调用后端logout API
      if (accessToken) {
        try {
          await fetch('/api/auth/logout', {
            method: 'POST',
            headers: {
              'Authorization': `Bearer ${accessToken}`,
              'Content-Type': 'application/json'
            },
            body: JSON.stringify({
              refresh_token: refreshToken
            })
          });
        } catch (error) {
          console.error('Backend logout failed:', error);
          // 即使后端失败，也要继续清除前端token
        }
      }
      
      // Step 3: 使用AuthProvider的signOut方法（这会调用AuthClient.signOut并清除auth_session）
      console.log('🔑 Calling AuthProvider signOut...');
      await signOut();
      
      // Step 4: 清除所有其他本地存储
      console.log('🧹 Clearing local storage...');
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      localStorage.removeItem('auth_session'); // 确保清除AuthClient的session
      localStorage.removeItem('auth_token');
      document.cookie = 'auth_token=; Path=/; Max-Age=0';
      sessionStorage.clear();
      
      // Step 5: 显示成功消息，让系统自动处理跳转
      console.log('✅ Logout completed, system will handle redirect...');
      toast.success('已成功退出登录');
      
      // 不需要手动跳转，layout-content.tsx会检测到user状态变化并自动跳转
      // 系统会在下一次渲染时自动跳转到认证页面
      
    } catch (error) {
      console.error('Logout error:', error);
      // 即使出错也要清除本地数据并跳转
      try {
        await signOut();
      } catch (signOutError) {
        console.error('SignOut also failed:', signOutError);
      }
      
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      localStorage.removeItem('auth_session'); // AuthClient使用的session key
      localStorage.removeItem('auth_token');
      document.cookie = 'auth_token=; Path=/; Max-Age=0';
      sessionStorage.clear();
      toast.error('退出登录时出现问题，但已清除本地数据');
      
      // 不需要手动跳转，系统会自动检测到认证状态变化并跳转
    }
  };

  const getInitials = (name: string) => {
    return name
      .split(' ')
      .map((part) => part.charAt(0))
      .join('')
      .toUpperCase()
      .substring(0, 2);
  };

  if (!activeTeam) {
    return null;
  }

  return (
    <Dialog open={showNewTeamDialog} onOpenChange={setShowNewTeamDialog}>
      <SidebarMenu>
        <SidebarMenuItem>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <SidebarMenuButton
                size="lg"
                tooltip={t('sidebar.userMenu')}
                className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
              >
                <Avatar className="h-8 w-8 rounded-lg group-data-[collapsible=icon]:h-6 group-data-[collapsible=icon]:w-6">
                  <AvatarImage src={user.avatar} alt={user.name} />
                  <AvatarFallback className="rounded-lg">
                    {getInitials(user.name)}
                  </AvatarFallback>
                </Avatar>
                <div className="grid flex-1 text-left text-sm leading-tight group-data-[collapsible=icon]:hidden">
                  <span className="truncate font-medium">{user.name}</span>
                  <span className="truncate text-xs">{user.email}</span>
                </div>
                <ChevronsUpDown className="ml-auto size-4 group-data-[collapsible=icon]:hidden" />
              </SidebarMenuButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
              side={isMobile ? 'bottom' : 'top'}
              align="start"
              sideOffset={4}
            >
              <DropdownMenuLabel className="p-0 font-normal">
                <div className="flex items-center gap-2 px-1.5 py-1.5 text-left text-sm">
                  <Avatar className="h-8 w-8 rounded-lg">
                    <AvatarImage src={user.avatar} alt={user.name} />
                    <AvatarFallback className="rounded-lg">
                      {getInitials(user.name)}
                    </AvatarFallback>
                  </Avatar>
                  <div className="grid flex-1 text-left text-sm leading-tight">
                    <span className="truncate font-medium">{user.name}</span>
                    <span className="truncate text-xs">{user.email}</span>
                  </div>
                </div>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />

              {/* Teams Section */}
              {personalAccount && (
                <>
                  <DropdownMenuLabel className="text-muted-foreground text-xs">
                    {t('menu.personalAccount')}
                  </DropdownMenuLabel>
                  <DropdownMenuItem
                    key={personalAccount.account_id}
                    onClick={() =>
                      handleTeamSelect({
                        name: personalAccount.name,
                        logo: Command,
                        plan: 'Personal',
                        account_id: personalAccount.account_id,
                        slug: personalAccount.slug,
                        personal_account: true,
                      })
                    }
                    className="gap-2 p-2"
                  >
                    <div className="flex size-6 items-center justify-center rounded-xs border">
                      <Command className="size-4 shrink-0" />
                    </div>
                    {personalAccount.name}
                    <DropdownMenuShortcut>⌘1</DropdownMenuShortcut>
                  </DropdownMenuItem>
                </>
              )}

              {teamAccounts?.length > 0 && (
                <>
                  <DropdownMenuLabel className="text-muted-foreground text-xs mt-2">
                    {t('menu.teams')}
                  </DropdownMenuLabel>
                  {teamAccounts.map((team, index) => (
                    <DropdownMenuItem
                      key={team.account_id}
                      onClick={() =>
                        handleTeamSelect({
                          name: team.name,
                          logo: AudioWaveform,
                          plan: 'Team',
                          account_id: team.account_id,
                          slug: team.slug,
                          personal_account: false,
                        })
                      }
                      className="gap-2 p-2"
                    >
                      <div className="flex size-6 items-center justify-center rounded-xs border">
                        <AudioWaveform className="size-4 shrink-0" />
                      </div>
                      {team.name}
                      <DropdownMenuShortcut>⌘{index + 2}</DropdownMenuShortcut>
                    </DropdownMenuItem>
                  ))}
                </>
              )}

              {/* <DropdownMenuSeparator />
              <DialogTrigger asChild>
                <DropdownMenuItem 
                  className="gap-2 p-2"
                  onClick={() => {
                    setShowNewTeamDialog(true)
                  }}
                >
                  <div className="bg-background flex size-6 items-center justify-center rounded-md border">
                    <Plus className="size-4" />
                  </div>
                  <div className="text-muted-foreground font-medium">Add team</div>
                </DropdownMenuItem>
              </DialogTrigger> */}
              <DropdownMenuSeparator />

              {/* User Settings Section */}
              <DropdownMenuGroup>
                {billingUiEnabled && (
                  <DropdownMenuItem 
                    onClick={() => toast.info(t('common.comingSoon'))}
                  >
                    <CreditCard className="h-4 w-4" />
                    {t('menu.billing')}
                  </DropdownMenuItem>
                )}
                {!flagLoading && customAgentsEnabled && (
                  <DropdownMenuItem 
                    onClick={() => toast.info(t('common.comingSoon'))}
                  >
                    <Plug className="h-4 w-4" />
                    {t('menu.integrations')}
                  </DropdownMenuItem>
                )}
                {!flagLoading && customAgentsEnabled && (
                  <DropdownMenuItem 
                    onClick={() => toast.info(t('common.comingSoon'))}
                  >
                    <Key className="h-4 w-4" />
                    {language === 'zh' ? `${t('menu.apiKeys')} (管理员)` : `${t('menu.apiKeys')} (Admin)`}
                  </DropdownMenuItem>
                )}
                {isLocalMode() && (
                  <DropdownMenuItem 
                    onClick={() => toast.info(t('common.comingSoon'))}
                  >
                    <KeyRound className="h-4 w-4" />
                    {t('menu.envManager')}
                  </DropdownMenuItem>
                )}
                {/* <DropdownMenuItem asChild>
                  <Link href="/settings">
                    <Settings className="mr-2 h-4 w-4" />
                    Settings
                  </Link>
                </DropdownMenuItem> */}
                <DropdownMenuItem
                  onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
                >
                  <div className="flex items-center gap-2">
                    <Sun className="h-4 w-4 rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
                    <Moon className="absolute h-4 w-4 rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
                    <span>{t('menu.theme')}</span>
                  </div>
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={() => setLanguage(language === 'zh' ? 'en' : 'zh')}
                >
                  <Languages className="h-4 w-4" />
                  {language === 'zh' ? t('menu.switchToEnglish') : t('menu.switchToChinese')}
                </DropdownMenuItem>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuItem className='text-destructive focus:text-destructive focus:bg-destructive/10' onClick={handleLogout}>
                <LogOut className="h-4 w-4 text-destructive" />
                {t('menu.logout')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </SidebarMenuItem>
      </SidebarMenu>

      <DialogContent className="sm:max-w-[425px] border-subtle dark:border-white/10 bg-card-bg dark:bg-background-secondary rounded-2xl shadow-custom">
        <DialogHeader>
          <DialogTitle className="text-foreground">
            {t('dialog.createTeamTitle')}
          </DialogTitle>
          <DialogDescription className="text-foreground/70">
            {t('dialog.createTeamDescription')}
          </DialogDescription>
        </DialogHeader>
        <NewTeamForm />
      </DialogContent>
    </Dialog>
  );
}
