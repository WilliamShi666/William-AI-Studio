export const getAuthToken = (): string | null => {
  if (typeof window === 'undefined') {
    return null;
  }

  const directToken = localStorage.getItem('auth_token');
  if (directToken) {
    return directToken;
  }

  const cookieToken = document.cookie
    .split(';')
    .map((entry) => entry.trim())
    .find((entry) => entry.startsWith('auth_token='))
    ?.split('=')[1];
  if (cookieToken) {
    try {
      localStorage.setItem('auth_token', cookieToken);
    } catch {
      // ignore storage failures
    }
    return cookieToken;
  }

  const storedSession = localStorage.getItem('auth_session');
  if (!storedSession) {
    return null;
  }

  try {
    const parsed = JSON.parse(storedSession) as { access_token?: string };
    return parsed?.access_token ?? null;
  } catch {
    return null;
  }
};

export type UserRole = 'admin' | 'user';

export interface TokenPayload {
  sub: string;
  role?: UserRole;
  exp?: number;
  iat?: number;
  type?: string;
}

const decodeBase64Url = (input: string): string => {
  const normalized = input.replace(/-/g, '+').replace(/_/g, '/');
  const padded = normalized + '==='.slice((normalized.length + 3) % 4);
  return atob(padded);
};

export const parseJwtPayload = (token: string): TokenPayload | null => {
  try {
    const base64Payload = token.split('.')[1];
    if (!base64Payload) return null;
    const payload = JSON.parse(decodeBase64Url(base64Payload));
    return payload as TokenPayload;
  } catch {
    return null;
  }
};

/**
 * Check if a JWT token is expired
 * @param token - JWT token string (optional, uses stored token if not provided)
 * @returns true if token is expired or missing, false if valid
 */
export const isTokenExpired = (token?: string | null): boolean => {
  const resolvedToken = token ?? getAuthToken();
  if (!resolvedToken) {
    return true; // No token means effectively expired
  }

  const payload = parseJwtPayload(resolvedToken);
  if (!payload?.exp) {
    return false; // No expiration claim, assume valid
  }

  const now = Date.now() / 1000;
  return now >= payload.exp;
};

/**
 * Clear all auth-related storage (for logout or expired session)
 */
export const clearAuthSession = (): void => {
  if (typeof window === 'undefined') return;

  try {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('auth_session');
    document.cookie = 'auth_token=; Path=/; Max-Age=0';
  } catch {
    // Ignore storage errors
  }
};

/**
 * Redirect to the main login page with optional return URL
 * @param returnPath - Path to return to after login (optional)
 */
export const redirectToLogin = (returnPath?: string): void => {
  if (typeof window === 'undefined') return;

  clearAuthSession();

  // Build login URL - main site is at root, login page is /auth
  const loginUrl = new URL('/auth', window.location.origin);

  if (returnPath) {
    loginUrl.searchParams.set('returnUrl', returnPath);
  } else {
    // Default: return to current tutor path
    loginUrl.searchParams.set('returnUrl', window.location.pathname);
  }

  window.location.href = loginUrl.toString();
};

export const getUserRole = (): UserRole => {
  const token = getAuthToken();
  if (!token) return 'user';
  const payload = parseJwtPayload(token);
  return payload?.role === 'admin' ? 'admin' : 'user';
};

export const appendAuthTokenToUrl = (url: string, token?: string | null): string => {
  const resolvedToken = token ?? getAuthToken();
  if (!resolvedToken) {
    return url;
  }

  try {
    const base = typeof window !== 'undefined' ? window.location.origin : 'http://localhost';
    const parsed = new URL(url, base);
    if (!parsed.searchParams.has('token')) {
      parsed.searchParams.set('token', resolvedToken);
    }
    return parsed.toString();
  } catch {
    return url;
  }
};

export const withAuthHeaders = (init: RequestInit = {}): RequestInit => {
  const token = getAuthToken();
  if (!token) {
    return init;
  }

  const headers = new Headers(init.headers || {});
  if (!headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  return { ...init, headers };
};

/**
 * Options for authFetch
 */
export interface AuthFetchOptions {
  /** If true, skip automatic redirect to login on 401 response */
  skipAuthRedirect?: boolean;
}

/**
 * Fetch wrapper that adds auth headers and handles 401 responses
 * @param input - Fetch input (URL or Request)
 * @param init - Fetch init options
 * @param options - Additional options for auth handling
 * @returns Fetch response
 */
export const authFetch = async (
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: AuthFetchOptions = {}
): Promise<Response> => {
  const response = await fetch(input, withAuthHeaders(init));

  // Handle 401 Unauthorized - session expired or invalid
  if (response.status === 401 && !options.skipAuthRedirect) {
    console.warn('Received 401 Unauthorized, redirecting to login');
    redirectToLogin();
  }

  return response;
};
