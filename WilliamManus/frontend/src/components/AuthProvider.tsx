'use client';

import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  ReactNode,
} from 'react';
import { authClient, User, Session } from '@/lib/auth/client';
import { userCannotAccessRoysAlpha } from '@/lib/auth/access';
import { useRouter } from 'next/navigation';

type AuthContextType = {
  supabase: any;
  session: Session | null;
  user: User | null;
  isLoading: boolean;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthContextType | undefined>(undefined);

const PUBLIC_PATHS = new Set(['/', '/legal', '/invitation']);
const PUBLIC_PREFIXES = ['/auth', '/share', '/api', '/_next'];
const PUBLIC_FILE_REGEX =
  /\.(?:svg|png|jpg|jpeg|gif|webp|ico|css|js|map|txt|xml)$/i;

function isPublicPath(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) {
    return true;
  }

  if (PUBLIC_FILE_REGEX.test(pathname)) {
    return true;
  }

  return PUBLIC_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [session, setSession] = useState<Session | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();

  const redirectToAuthIfProtected = useCallback(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const { pathname, search } = window.location;
    if (isPublicPath(pathname)) {
      return;
    }

    router.replace(
      `/auth?returnUrl=${encodeURIComponent(`${pathname}${search}`)}`,
    );
  }, [router]);

  const redirectToHomeIfProtected = useCallback(() => {
    if (typeof window === 'undefined') {
      return;
    }

    if (isPublicPath(window.location.pathname)) {
      return;
    }

    router.replace('/');
  }, [router]);

  useEffect(() => {
    let isActive = true;

    const initializeSession = async () => {
      try {
        const {
          data: { session: currentSession },
          error,
        } = await authClient.instance.getSession();

        if (!isActive) {
          return;
        }

        if (currentSession && !error) {
          setSession(currentSession);
          setUser(currentSession.user);

          if (userCannotAccessRoysAlpha(currentSession.user)) {
            redirectToHomeIfProtected();
          }

          const {
            data: { user: verifiedUser },
            error: verifyError,
          } = await authClient.instance.getUser({ strict: true });

          if (!isActive) {
            return;
          }

          if (verifyError === 'Session invalid') {
            setSession(null);
            setUser(null);
            redirectToAuthIfProtected();
            return;
          }

          if (verifiedUser) {
            setUser(verifiedUser);
            if (userCannotAccessRoysAlpha(verifiedUser)) {
              redirectToHomeIfProtected();
            }
          }
        } else {
          setSession(null);
          setUser(null);
          redirectToAuthIfProtected();
        }
      } catch (error) {
        console.error('Failed to get initial session:', error);
      } finally {
        if (isActive) {
          setIsLoading(false);
        }
      }
    };

    initializeSession();

    // 监听认证状态变化
    const { data: { subscription } } = authClient.instance.onAuthStateChange(
      (_event, newSession) => {
        if (!isActive) {
          return;
        }

        setSession(newSession);
        setUser(newSession?.user ?? null);
        if (newSession?.user && userCannotAccessRoysAlpha(newSession.user)) {
          redirectToHomeIfProtected();
        }
        setIsLoading(false);
      },
    );

    return () => {
      isActive = false;
      subscription.unsubscribe();
    };
  }, [redirectToAuthIfProtected, redirectToHomeIfProtected]);

  const signOut = async () => {
    try {
      await authClient.instance.signOut();
      setSession(null);
      setUser(null);
    } catch (error) {
      console.error('❌ Error signing out:', error);
    }
  };

  const value = {
    supabase: authClient.instance, // 提供认证客户端而不是数据库客户端
    session,
    user,
    isLoading,
    signOut,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
