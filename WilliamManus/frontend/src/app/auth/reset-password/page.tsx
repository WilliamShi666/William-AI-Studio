'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useSearchParams } from 'next/navigation';
import { useState, useEffect, Suspense } from 'react';
import { AlertCircle, ArrowLeft, CheckCircle } from 'lucide-react';

import { Input } from '@/components/ui/input';
import { SubmitButton } from '@/components/ui/submit-button';
import { resetPassword } from '../actions';

function ResetPasswordContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const code = searchParams.get('code');

  const [resetSuccess, setResetSuccess] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Check if code is present in URL
  useEffect(() => {
    if (!code) {
      setErrorMessage(
        '重置代码无效或缺失。请重新请求密码重置链接。',
      );
    }
  }, [code]);

  const handleResetPassword = async (prevState: any, formData: FormData) => {
    if (!code) {
      return { message: '重置代码无效' };
    }

    const result = await resetPassword(prevState, formData);

    if (
      result &&
      typeof result === 'object' &&
      'success' in result &&
      result.success
    ) {
      setResetSuccess(true);
      return result;
    }

    return result;
  };

  if (resetSuccess) {
    return (
      <main className="flex flex-col items-center justify-center min-h-screen w-full">
        <div className="w-full divide-y divide-border">
          <section className="w-full relative overflow-hidden">
            <div className="relative flex flex-col items-center w-full px-6">
              <div className="absolute inset-x-1/4 top-0 h-[600px] md:h-[800px] -z-20 bg-background rounded-b-xl"></div>

              {/* Success content */}
              <div className="relative z-10 pt-24 pb-8 max-w-xl mx-auto h-full w-full flex flex-col gap-2 items-center justify-center">
                <div className="flex flex-col items-center text-center">
                  <div className="bg-green-50 dark:bg-green-950/20 rounded-full p-4 mb-6">
                    <CheckCircle className="h-12 w-12 text-green-500 dark:text-green-400" />
                  </div>

                  <h1 className="text-3xl md:text-4xl lg:text-5xl font-medium tracking-tighter text-center text-balance text-primary mb-4">
                    密码重置完成
                  </h1>

                  <p className="text-base md:text-lg text-center text-muted-foreground font-medium text-balance leading-relaxed tracking-tight max-w-md mb-6">
                    您的密码已成功更新。您现在可以使用新密码登录。
                  </p>

                  <div className="flex flex-col sm:flex-row gap-4 w-full max-w-sm">
                    <Link
                      href="/auth"
                      className="flex h-12 items-center justify-center w-full text-center rounded-full bg-primary text-primary-foreground hover:bg-primary/90 transition-all shadow-md"
                    >
                      前往登录
                    </Link>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </div>
      </main>
    );
  }

  return (
    <main className="flex flex-col items-center justify-center min-h-screen w-full">
      <div className="w-full divide-y divide-border">
        <section className="w-full relative overflow-hidden">
          <div className="relative flex flex-col items-center w-full px-6">
            <div className="absolute inset-x-1/4 top-0 h-[600px] md:h-[800px] -z-20 bg-background rounded-b-xl"></div>

            {/* Header content */}
            <div className="relative z-10 pt-24 pb-8 max-w-md mx-auto h-full w-full flex flex-col gap-2 items-center justify-center">
              <Link
                href="/auth"
                className="group border border-border/50 bg-background hover:bg-accent/20 rounded-full text-sm h-8 px-3 flex items-center gap-2 transition-all duration-200 shadow-sm mb-6"
              >
                <ArrowLeft className="h-4 w-4 text-muted-foreground" />
                <span className="font-medium text-muted-foreground text-xs tracking-wide">
                  返回登录
                </span>
              </Link>

              <h1 className="text-3xl md:text-4xl lg:text-5xl font-medium tracking-tighter text-center text-balance text-primary">
                重置密码
              </h1>
              <p className="text-base md:text-lg text-center text-muted-foreground font-medium text-balance leading-relaxed tracking-tight mt-2 mb-6">
                为您的账户创建新密码
              </p>
            </div>
          </div>

          {/* Form card */}
          <div className="relative z-10 flex justify-center px-6 pb-24">
            <div className="w-full max-w-md rounded-xl bg-[#F3F4F6] dark:bg-[#F9FAFB]/[0.02] border border-border p-8">
              {errorMessage && (
                <div className="mb-6 p-4 rounded-lg flex items-center gap-3 bg-secondary/10 border border-secondary/20 text-secondary">
                  <AlertCircle className="h-5 w-5 flex-shrink-0 text-secondary" />
                  <span className="text-sm font-medium">{errorMessage}</span>
                </div>
              )}

              {!errorMessage && (
                <form className="space-y-4">
                  <div>
                    <Input
                      id="password"
                      name="password"
                      type="password"
                      placeholder="新密码"
                      className="h-12 rounded-full bg-background border-border"
                      required
                    />
                  </div>

                  <div>
                    <Input
                      id="confirmPassword"
                      name="confirmPassword"
                      type="password"
                      placeholder="确认新密码"
                      className="h-12 rounded-full bg-background border-border"
                      required
                    />
                  </div>

                  <div className="space-y-4 pt-4">
                    <SubmitButton
                      formAction={handleResetPassword}
                      className="w-full h-12 rounded-full bg-primary text-primary-foreground hover:bg-primary/90 transition-all shadow-md"
                      pendingText="正在更新密码..."
                    >
                        重置密码
                    </SubmitButton>
                  </div>
                </form>
              )}

              {errorMessage && (
                <div className="mt-6 flex justify-center">
                  <Link
                    href="/auth"
                    className="flex h-12 px-6 items-center justify-center text-center rounded-full bg-primary text-primary-foreground hover:bg-primary/90 transition-all shadow-md"
                  >
                    返回登录
                  </Link>
                </div>
              )}
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}

export default function ResetPassword() {
  return (
    <Suspense
      fallback={
        <main className="flex flex-col items-center justify-center min-h-screen w-full">
          <div className="w-12 h-12 rounded-full border-4 border-primary border-t-transparent animate-spin"></div>
        </main>
      }
    >
      <ResetPasswordContent />
    </Suspense>
  );
}
