import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/components/AuthProvider';
import {
  fetchSandboxFileContent,
  listSandboxFilesResponse,
  normalizeFileDeliveryPath,
  type FileInfo,
  type SandboxFileListDiagnostics,
  type SandboxFileListResponse,
} from '@/lib/api';

// Re-export FileCache utilities for compatibility
export { FileCache } from '@/hooks/use-cached-file';

/**
 * Normalize a file path to ensure consistent caching
 */
function normalizePath(path: string, rootPath?: string): string {
  return normalizeFileDeliveryPath(path, rootPath);
}

/**
 * Generate React Query keys for file operations
 */
export const fileQueryKeys = {
  all: ['files'] as const,
  contents: () => [...fileQueryKeys.all, 'content'] as const,
  content: (sandboxId: string, path: string, contentType: string) => 
    [...fileQueryKeys.contents(), sandboxId, normalizePath(path), contentType] as const,
  directories: () => [...fileQueryKeys.all, 'directory'] as const,
  directory: (sandboxId: string, path: string) => 
    [...fileQueryKeys.directories(), sandboxId, normalizePath(path)] as const,
};

/**
 * Determine content type from file path
 */
function getContentTypeFromPath(path: string): 'text' | 'blob' | 'json' {
  if (!path) return 'text';
  
  const ext = path.toLowerCase().split('.').pop() || '';
  
  // Binary file extensions
  if (/^(xlsx|xls|docx|doc|pptx|ppt|pdf|png|jpg|jpeg|gif|bmp|webp|svg|ico|zip|exe|dll|bin|dat|obj|o|so|dylib|mp3|mp4|avi|mov|wmv|flv|wav|ogg)$/.test(ext)) {
    return 'blob';
  }
  
  // JSON files
  if (ext === 'json') return 'json';
  
  // Default to text
  return 'text';
}

/**
 * Get MIME type from file path
 */
function getMimeTypeFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || '';
  
  switch (ext) {
    case 'xlsx': return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
    case 'xls': return 'application/vnd.ms-excel';
    case 'docx': return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    case 'doc': return 'application/msword';
    case 'pptx': return 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
    case 'ppt': return 'application/vnd.ms-powerpoint';
    case 'pdf': return 'application/pdf';
    case 'png': return 'image/png';
    case 'jpg': 
    case 'jpeg': return 'image/jpeg';
    case 'gif': return 'image/gif';
    case 'svg': return 'image/svg+xml';
    case 'zip': return 'application/zip';
    default: return 'application/octet-stream';
  }
}

/**
 * Fetch file content with proper error handling and content type detection
 */
export async function fetchFileContent(
  sandboxId: string,
  filePath: string,
  contentType: 'text' | 'blob' | 'json',
  token: string,
  rootPath?: string,
): Promise<string | Blob | any> {
  const normalizedPath = normalizePath(filePath, rootPath);

  const result = await fetchSandboxFileContent(sandboxId, normalizedPath, {
    accessToken: token,
    responseType: contentType,
  });

  if (contentType === 'blob' && result.data instanceof Blob) {
    const blob = result.data;
    const expectedMimeType = getMimeTypeFromPath(filePath);
    if (expectedMimeType !== blob.type && expectedMimeType !== 'application/octet-stream') {
      return new Blob([blob], { type: expectedMimeType });
    }

    return blob;
  }

  return result.data;
}

/**
 * Legacy compatibility function for getCachedFile
 */
export async function getCachedFile(
  sandboxId: string,
  filePath: string,
  options: {
    contentType?: 'text' | 'blob' | 'json';
    force?: boolean;
    token?: string;
    rootPath?: string;
  } = {}
): Promise<any> {
  const normalizedPath = normalizePath(filePath, options.rootPath);
  const detectedContentType = getContentTypeFromPath(filePath);
  const effectiveContentType = options.contentType || detectedContentType;
  
  if (!options.token) {
    throw new Error('Authentication token required');
  }
  
  return fetchFileContent(
    sandboxId,
    normalizedPath,
    effectiveContentType,
    options.token,
    options.rootPath,
  );
}

/**
 * Hook for fetching file content with React Query
 * Returns raw content - components create blob URLs as needed
 */
export function useFileContentQuery(
  sandboxId?: string,
  filePath?: string,
  options: {
    contentType?: 'text' | 'blob' | 'json';
    enabled?: boolean;
    staleTime?: number;
    gcTime?: number;
    rootPath?: string;
  } = {}
) {
  const { session } = useAuth();
  
  const normalizedPath = filePath ? normalizePath(filePath, options.rootPath) : null;
  const detectedContentType = filePath ? getContentTypeFromPath(filePath) : 'text';
  const effectiveContentType = options.contentType || detectedContentType;
  
  const queryResult = useQuery({
    queryKey: sandboxId && normalizedPath ? 
      fileQueryKeys.content(sandboxId, normalizedPath, effectiveContentType) : [],
    queryFn: async () => {
      if (!sandboxId || !normalizedPath) {
        throw new Error('Missing required parameters');
      }
      
      return fetchFileContent(
        sandboxId,
        normalizedPath,
        effectiveContentType,
        session?.access_token || '',
        options.rootPath,
      );
    },
    enabled: Boolean(sandboxId && normalizedPath && (options.enabled !== false)),
    staleTime: options.staleTime || (effectiveContentType === 'blob' ? 5 * 60 * 1000 : 2 * 60 * 1000), // 5min for blobs, 2min for text
    gcTime: options.gcTime || 10 * 60 * 1000, // 10 minutes
    retry: (failureCount, error: any) => {
      // Don't retry on auth errors
      if (error?.message?.includes('401') || error?.message?.includes('403')) {
        return false;
      }
      return failureCount < 3;
    },
  });
  
  const queryClient = useQueryClient();
  
  // Refresh function
  const refreshCache = React.useCallback(async () => {
    if (!sandboxId || !filePath) return null;
    
    const normalizedPath = normalizePath(filePath, options.rootPath);
    const queryKey = fileQueryKeys.content(sandboxId, normalizedPath, effectiveContentType);
    
    await queryClient.invalidateQueries({ queryKey });
    const newData = queryClient.getQueryData(queryKey);
    return newData || null;
  }, [sandboxId, filePath, effectiveContentType, options.rootPath, queryClient]);
  
  return {
    ...queryResult,
    refreshCache,
    // Legacy compatibility methods
    getCachedFile: () => Promise.resolve(queryResult.data),
    getFromCache: () => queryResult.data,
    cache: new Map(), // Legacy compatibility - empty map
  };
}

/**
 * Hook for fetching directory listings
 */
export function useDirectoryQuery(
  sandboxId?: string,
  directoryPath?: string,
  options: {
    enabled?: boolean;
    staleTime?: number;
    rootPath?: string;
  } = {}
) {
  const normalizedPath = directoryPath
    ? normalizePath(directoryPath, options.rootPath)
    : null;

  const queryResult = useQuery({
    queryKey: sandboxId && normalizedPath ? 
      fileQueryKeys.directory(sandboxId, normalizedPath) : [],
    queryFn: async (): Promise<SandboxFileListResponse> => {
      if (!sandboxId || !normalizedPath) {
        throw new Error('Missing required parameters');
      }
      return await listSandboxFilesResponse(sandboxId, normalizedPath);
    },
    enabled: Boolean(sandboxId && normalizedPath && (options.enabled !== false)),
    staleTime: options.staleTime || 30 * 1000, // 30 seconds for directory listings
    gcTime: 5 * 60 * 1000, // 5 minutes
    retry: 2,
  });

  return {
    ...queryResult,
    data: queryResult.data?.files,
    diagnostics: queryResult.data?.diagnostics ?? null,
    response: queryResult.data,
  } as Omit<typeof queryResult, 'data'> & {
    data: FileInfo[] | undefined;
    diagnostics: SandboxFileListDiagnostics | null;
    response: SandboxFileListResponse | undefined;
  };
}

/**
 * Hook for preloading multiple files
 */
export function useFilePreloader() {
  const queryClient = useQueryClient();
  const { session } = useAuth();
  
  const preloadFiles = React.useCallback(async (
    sandboxId: string,
    filePaths: string[]
  ): Promise<void> => {
    if (!session?.access_token) {
      console.warn('Cannot preload files: No authentication token available');
      return;
    }
    
    const uniquePaths = [...new Set(filePaths)];
    
    const preloadPromises = uniquePaths.map(async (path) => {
      const normalizedPath = normalizePath(path);
      const contentType = getContentTypeFromPath(path);
      
      // Check if already cached
      const queryKey = fileQueryKeys.content(sandboxId, normalizedPath, contentType);
      const existingData = queryClient.getQueryData(queryKey);
      
      if (existingData) {
        return existingData;
      }
      
      // Prefetch the file
      return queryClient.prefetchQuery({
        queryKey,
        queryFn: () => fetchFileContent(sandboxId, normalizedPath, contentType, session.access_token!),
        staleTime: contentType === 'blob' ? 5 * 60 * 1000 : 2 * 60 * 1000,
      });
    });
    
    await Promise.all(preloadPromises);
  }, [queryClient, session]);
  
  return { preloadFiles };
}

/**
 * Compatibility hook that mimics the old useCachedFile API
 */
export function useCachedFile<T = string>(
  sandboxId?: string,
  filePath?: string,
  options: {
    expiration?: number;
    contentType?: 'json' | 'text' | 'blob' | 'arrayBuffer' | 'base64';
    processFn?: (data: any) => T;
  } = {}
) {
  // Map old contentType values to new ones
  const mappedContentType = React.useMemo(() => {
    switch (options.contentType) {
      case 'json': return 'json';
      case 'blob':
      case 'arrayBuffer':
      case 'base64': return 'blob';
      case 'text':
      default: return 'text';
    }
  }, [options.contentType]);
  
  const query = useFileContentQuery(sandboxId, filePath, {
    contentType: mappedContentType,
    staleTime: options.expiration,
  });
  
  // Process data if processFn is provided
  const processedData = React.useMemo(() => {
    if (!query.data || !options.processFn) {
      return query.data as T;
    }
    
    try {
      return options.processFn(query.data);
    } catch (error) {
      console.error('Error processing file data:', error);
      return null;
    }
  }, [query.data, options]);
  
  return {
    data: processedData,
    isLoading: query.isLoading,
    error: query.error,
    refreshCache: query.refreshCache,
    // Legacy compatibility methods
    getCachedFile: () => Promise.resolve(processedData),
    getFromCache: () => processedData,
    cache: new Map(), // Legacy compatibility - empty map
  };
}
