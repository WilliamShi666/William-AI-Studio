'use client';

import Link from 'next/link';
import { SubmitButton } from '@/components/ui/submit-button';
import { Input } from '@/components/ui/input';
import { useMediaQuery } from '@/hooks/use-media-query';
import { useState, useEffect, Suspense } from 'react';
import { signIn, signUp, forgotPassword } from './actions';
import { useSearchParams, useRouter } from 'next/navigation';
import {
  ArrowLeft,
  X,
  CheckCircle,
  AlertCircle,
  MailCheck,
  Loader2,
} from 'lucide-react';
import { useAuth } from '@/components/AuthProvider';
import { getPostAuthRedirect } from '@/lib/auth/access';
import { toast } from 'sonner';
import { useFeatureFlag } from '@/lib/feature-flags';
import { KortixLogo } from '@/components/sidebar/kortix-logo';

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Ripple } from '@/components/ui/ripple';


function LoginContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoading } = useAuth();
  const mode = searchParams.get('mode');
  const returnUrl = searchParams.get('returnUrl');
  const message = searchParams.get('message');
  const { enabled: customAgentsEnabled } = useFeatureFlag("custom_agents");

  const isSignUp = mode === 'signup';
  const isMobile = useMediaQuery('(max-width: 768px)');
  const [mounted, setMounted] = useState(false);

  // 移除认证方法追踪，简化认证流程

  useEffect(() => {
    if (!isLoading && user) {
      router.push(getPostAuthRedirect(user, returnUrl));
    }
  }, [user, isLoading, router, returnUrl]);

  const isSuccessMessage =
    message &&
    (message.includes('Check your email') ||
      message.includes('Account created') ||
      message.includes('success'));

  // Registration success state
  const [registrationSuccess, setRegistrationSuccess] =
    useState(!!isSuccessMessage);
  const [registrationEmail, setRegistrationEmail] = useState('');

  // const [forgotPasswordOpen, setForgotPasswordOpen] = useState(false);
  // const [forgotPasswordEmail, setForgotPasswordEmail] = useState('');
  // const [forgotPasswordStatus, setForgotPasswordStatus] = useState<{
  //   success?: boolean;
  //   message?: string;
  // }>({});

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (isSuccessMessage) {
      setRegistrationSuccess(true);
    }
  }, [isSuccessMessage]);

  const handleSignIn = async (prevState: any, formData: FormData) => {
    if (returnUrl) {
      formData.append('returnUrl', returnUrl);
    } else {
      formData.append('returnUrl', '/');
    }
    const result = await signIn(prevState, formData);

    if (
      result &&
      typeof result === 'object' &&
      'success' in result &&
      result.success &&
      'redirectTo' in result
      ) {
      // 如果有认证数据，保存到客户端
      if ('authData' in result && result.authData && typeof result.authData === 'object') {
        try {
          const authData = result.authData as {
            access_token: string;
            refresh_token: string;
            user: any;
            expires_at: number;
          };
          // 保存认证信息到localStorage
          localStorage.setItem('auth_session', JSON.stringify({
            access_token: authData.access_token,
            refresh_token: authData.refresh_token,
            user: authData.user,
            expires_at: authData.expires_at
          }));
          localStorage.setItem('auth_token', authData.access_token);
          const secureFlag = window.location.protocol === 'https:' ? '; Secure' : '';
          document.cookie = `auth_token=${authData.access_token}; Path=/; SameSite=Lax${secureFlag}`;
        } catch (error) {
          console.error('Failed to save auth data:', error);
        }
      }
      
      window.location.href = result.redirectTo as string;
      return null;
    }

    if (result && typeof result === 'object' && 'message' in result) {
      toast.error('Login failed', {
        description: result.message as string,
        duration: 5000,
      });
      return {};
    }

    return result;
  };

  const handleSignUp = async (prevState: any, formData: FormData) => {
    const email = formData.get('email') as string;
    setRegistrationEmail(email);

    if (returnUrl) {
      formData.append('returnUrl', returnUrl);
    }

    // Add origin for email redirects
    formData.append('origin', window.location.origin);

    const result = await signUp(prevState, formData);

    // Check for success and redirectTo properties (direct login case)
    if (
      result &&
      typeof result === 'object' &&
      'success' in result &&
      result.success &&
      'redirectTo' in result
    ) {
      // 如果有认证数据，保存到客户端
      if ('authData' in result && result.authData && typeof result.authData === 'object') {
        try {
          const authData = result.authData as {
            access_token: string;
            refresh_token: string;
            user: any;
            expires_at: number;
          };
          // 保存认证信息到localStorage
          localStorage.setItem('auth_session', JSON.stringify({
            access_token: authData.access_token,
            refresh_token: authData.refresh_token,
            user: authData.user,
            expires_at: authData.expires_at
          }));
          localStorage.setItem('auth_token', authData.access_token);
          const secureFlag = window.location.protocol === 'https:' ? '; Secure' : '';
          document.cookie = `auth_token=${authData.access_token}; Path=/; SameSite=Lax${secureFlag}`;
        } catch (error) {
          console.error('Failed to save auth data:', error);
        }
      }
      
      // Use window.location for hard navigation to avoid stale state
      window.location.href = result.redirectTo as string;
      return null; // Return null to prevent normal form action completion
    }

    // Check if registration was successful
    if (result && typeof result === 'object' && 'message' in result) {
      const resultMessage = result.message as string;
      
      // 检查是否是注册成功，需要跳转到登录
      if ('redirectToLogin' in result && result.redirectToLogin) {
        toast.success('注册成功！', {
          description: '您现在可以使用您的凭据登录。',
          duration: 5000,
        });
        
        // 跳转到登录页面
        const params = new URLSearchParams(window.location.search);
        params.delete('mode'); // 移除signup模式
        if (returnUrl) {
          params.set('returnUrl', returnUrl);
        }
        
        const newUrl = window.location.pathname + 
          (params.toString() ? '?' + params.toString() : '');
        
        window.history.pushState({ path: newUrl }, '', newUrl);
        window.location.reload(); // 刷新页面切换到登录模式
        
        return {};
      } else {
        // 其他错误消息
        toast.error('注册失败', {
          description: resultMessage,
          duration: 5000,
        });
        return {};
      }
    }

    return result;
  };

  // const handleForgotPassword = async (e: React.FormEvent<HTMLFormElement>) => {
  //   e.preventDefault();

  //   setForgotPasswordStatus({});

  //   if (!forgotPasswordEmail || !forgotPasswordEmail.includes('@')) {
  //     setForgotPasswordStatus({
  //       success: false,
  //       message: '请输入有效的邮箱地址',
  //     });
  //     return;
  //   }

  //   const formData = new FormData();
  //   formData.append('email', forgotPasswordEmail);
  //   formData.append('origin', window.location.origin);

  //   const result = await forgotPassword(null, formData);

  //   setForgotPasswordStatus(result);
  // };

  const resetRegistrationSuccess = () => {
    setRegistrationSuccess(false);
    // Remove message from URL and set mode to signin
    const params = new URLSearchParams(window.location.search);
    params.delete('message');
    params.set('mode', 'signin');

    const newUrl =
      window.location.pathname +
      (params.toString() ? '?' + params.toString() : '');

    window.history.pushState({ path: newUrl }, '', newUrl);

    router.refresh();
  };

  // Show loading spinner while checking auth state
  if (isLoading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  // Registration success view
  if (registrationSuccess) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center p-4">
        <div className="w-full max-w-md mx-auto">
          <div className="text-center">
            <div className="bg-green-50 dark:bg-green-950/20 rounded-full p-4 mb-6 inline-flex">
              <MailCheck className="h-12 w-12 text-green-500 dark:text-green-400" />
            </div>

            <h1 className="text-3xl font-semibold text-foreground mb-4">
              查看您的邮箱
            </h1>

            <p className="text-muted-foreground mb-2">
              我们已将确认链接发送至：
            </p>

            <p className="text-lg font-medium mb-6">
              {registrationEmail || 'your email address'}
            </p>

            <div className="bg-green-50 dark:bg-green-950/20 border border-green-100 dark:border-green-900/50 rounded-lg p-4 mb-8">
              <p className="text-sm text-green-800 dark:text-green-400">
                点击邮件中的链接激活您的账户。如果您没有看到邮件，请检查垃圾邮件文件夹。
              </p>
            </div>

            <div className="flex flex-col sm:flex-row gap-3 justify-center items-center">
              <Link
                href="/"
                className="flex h-11 items-center justify-center px-6 text-center rounded-lg border border-border bg-background hover:bg-accent transition-colors"
              >
                返回首页
              </Link>
              <button
                onClick={resetRegistrationSuccess}
                className="flex h-11 items-center justify-center px-6 text-center rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                返回登录
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
      <div className="min-h-screen bg-background relative landing-page-text">
        <div className="absolute top-6 left-6 z-10">
          <Link href="/" className="flex items-center">
            <KortixLogo size={28} />
          </Link>
        </div>
        <div className="flex min-h-screen">
          <div className="relative flex-1 flex items-center justify-center p-4 lg:p-8">
            <div className="absolute top-6 right-10 z-10">
              <Link
                href="/"
                className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors mb-4"
              >
                <ArrowLeft className="h-4 w-4" />
                返回首页
              </Link>
            </div>
            <div className="w-full max-w-sm">
              <div className="mb-4 flex items-center flex-col gap-3 sm:gap-4 justify-center">

                <h1 className="text-xl sm:text-2xl font-semibold text-foreground text-center leading-tight">
                  {isSignUp ? '创建您的账户' : '登录您的账户'}
                </h1>
              </div>
            {/* 移除第三方登录，只保留邮箱密码登录 */}
            <form className="space-y-3">
              <Input
                id="email"
                name="email"
                type="email"
                placeholder="邮箱地址"
                className="h-10 rounded-lg"
                required
              />
              <Input
                id="password"
                name="password"
                type="password"
                placeholder="密码"
                className="h-10 rounded-lg"
                required
              />
              {isSignUp && (
                <Input
                  id="confirmPassword"
                  name="confirmPassword"
                  type="password"
                  placeholder="确认密码"
                  className="h-10 rounded-lg"
                  required
                />
              )}
              <div className="pt-2">
                <div className="relative">
                  <SubmitButton
                    formAction={isSignUp ? handleSignUp : handleSignIn}
                    className="w-full h-10 bg-primary text-primary-foreground hover:bg-primary/90 transition-colors rounded-lg"
                    pendingText={isSignUp ? "正在创建账户..." : "正在登录..."}
                  >
                    {isSignUp ? '创建账户' : '登录'}
                  </SubmitButton>
                </div>
              </div>
            </form>
            
            <div className="mt-4 space-y-3 text-center text-sm">
              {/* {!isSignUp && (
                <button
                  type="button"
                  onClick={() => setForgotPasswordOpen(true)}
                  className="text-primary hover:underline"
                >
                  忘记密码？
                </button>
              )} */}
              
              <div>
                <Link
                  href={isSignUp 
                    ? `/auth${returnUrl ? `?returnUrl=${returnUrl}` : ''}`
                    : `/auth?mode=signup${returnUrl ? `&returnUrl=${returnUrl}` : ''}`
                  }
                  className="text-muted-foreground hover:text-foreground transition-colors"
                >
                  {isSignUp 
                    ? '已有账户？登录' 
                    : "没有账户？注册"
                  }
                </Link>
              </div>
            </div>
          </div>
        </div>
        <div className="hidden lg:flex flex-1 items-center justify-center bg-sidebar relative overflow-hidden">
          <div className="absolute inset-0">
            <Ripple />
          </div>
        </div>
      </div>
      {/* <Dialog open={forgotPasswordOpen} onOpenChange={setForgotPasswordOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <div className="flex items-center justify-between">
              <DialogTitle>重置密码</DialogTitle>
            </div>
            <DialogDescription>
              输入您的邮箱地址，我们将发送重置密码链接。
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleForgotPassword} className="space-y-4">
            <Input
              id="forgot-password-email"
              type="email"
              placeholder="邮箱地址"
              value={forgotPasswordEmail}
              onChange={(e) => setForgotPasswordEmail(e.target.value)}
              className="h-11 rounded-xl"
              required
            />
            {forgotPasswordStatus.message && (
              <div
                className={`p-3 rounded-md flex items-center gap-3 ${
                  forgotPasswordStatus.success
                    ? 'bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-900/50 text-green-800 dark:text-green-400'
                    : 'bg-destructive/10 border border-destructive/20 text-destructive'
                }`}
              >
                {forgotPasswordStatus.success ? (
                  <CheckCircle className="h-4 w-4 flex-shrink-0" />
                ) : (
                  <AlertCircle className="h-4 w-4 flex-shrink-0" />
                )}
                <span className="text-sm">{forgotPasswordStatus.message}</span>
              </div>
            )}
            <DialogFooter className="gap-2">
              <button
                type="button"
                onClick={() => setForgotPasswordOpen(false)}
                className="h-10 px-4 border border-border bg-background hover:bg-accent transition-colors rounded-md"
              >
                取消
              </button>
              <button
                type="submit"
                className="h-10 px-4 bg-primary text-primary-foreground hover:bg-primary/90 transition-colors rounded-md"
              >
                发送重置链接
              </button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog> */}
    </div>
  );
}

export default function Login() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen bg-background flex items-center justify-center">
          <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin"></div>
        </div>
      }
    >
      <LoginContent />
    </Suspense>
  );
}
