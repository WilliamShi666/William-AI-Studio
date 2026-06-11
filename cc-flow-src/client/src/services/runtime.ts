declare global {
  interface Window {
    claudeCodeUI?: {
      apiBaseUrl?: string;
      wsBaseUrl?: string;
      platform?: string;
    };
  }
}

export function getApiBaseUrl(): string {
  return window.claudeCodeUI?.apiBaseUrl || "/api";
}

export function getWsBaseUrl(): string {
  if (window.claudeCodeUI?.wsBaseUrl) {
    return window.claudeCodeUI.wsBaseUrl;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

export function buildWsUrl(path: string, query?: URLSearchParams): string {
  const normalizedBase = getWsBaseUrl().replace(/\/$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const suffix = query && query.toString() ? `?${query.toString()}` : "";
  return `${normalizedBase}${normalizedPath}${suffix}`;
}

export {};
