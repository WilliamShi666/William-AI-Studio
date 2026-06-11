'use client';

import { useState, useEffect, useRef, Fragment, useCallback } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import {
  File,
  Folder,
  Upload,
  Download,
  ChevronRight,
  Home,
  ChevronLeft,
  Loader,
  AlertTriangle,
  FileText,
  ChevronDown,
  Archive,
  Copy,
  Check,
} from 'lucide-react';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  FileRenderer,
} from '@/components/file-renderers';
import {
  DEFAULT_FILE_DELIVERY_ROOT_PATH,
  createFileActionOperationId,
  downloadSandboxFile,
  downloadSandboxArchive,
  type FileActionPhaseEvent,
  listSandboxFiles,
  normalizeFileDeliveryPath,
  normalizeFileDeliveryRootPath,
  SandboxArchiveDownloadError,
  type FileInfo,
  Project,
} from '@/lib/api';
import { toast } from 'sonner';
import { createClient } from '@/lib/supabase/client';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import {
  useDirectoryQuery,
  useFileContentQuery,
  FileCache
} from '@/hooks/react-query/files';
import { classifySandboxPathFromEntries } from '@/lib/sandbox-path-kind';
import { normalizeFilenameToNFC } from '@/lib/utils/unicode';

// Define API_URL
const API_URL = process.env.NEXT_PUBLIC_BACKEND_URL || '';

interface FileViewerModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sandboxId: string;
  initialFilePath?: string | null;
  project?: Project;
  filePathList?: string[];
}

interface ArchiveDownloadErrorState {
  title: string;
  message: string;
  status?: number;
  kind: 'authorization' | 'source_identity' | 'empty_artifact_set' | 'not_found' | 'server' | 'unknown';
  sandboxId: string | null;
  archiveSelection:
    | 'run_file_delivery_source'
    | 'project_sandbox'
    | 'current_sandbox_fallback'
    | 'unavailable';
  operationId?: string;
  requestId?: string | null;
  responseSource?: string | null;
  identitySource?: string | null;
  sandboxAvailable?: boolean | null;
  outcome?: string | null;
  fallback?: boolean | null;
}

interface PreparedDownloadState {
  url: string;
  fileName: string;
  kind: 'file' | 'archive';
  operationId: string;
  requestId: string | null;
  responseSource: string | null;
  identitySource: string | null;
  sandboxAvailable: boolean | null;
  outcome: string | null;
  fallback: boolean | null;
}

type DownloadPhase =
  | 'clicked'
  | 'request_dispatched'
  | 'response_ok'
  | 'blob_ready'
  | 'save_attempted'
  | 'manual_link_visible'
  | 'manual_link_clicked';

interface DownloadDiagnosticsState {
  kind: 'file' | 'archive';
  operationId: string;
  currentPhase: DownloadPhase;
  phaseHistory: DownloadPhase[];
  requestId: string | null;
  responseSource: string | null;
  identitySource: string | null;
  sandboxAvailable: boolean | null;
  outcome: string | null;
  fallback: boolean | null;
  sandboxId: string | null;
  archiveSelection:
    | 'run_file_delivery_source'
    | 'project_sandbox'
    | 'current_sandbox_fallback'
    | 'unavailable';
  fileName: string | null;
  errorMessage: string | null;
}

function describeArchiveDownloadError(
  error: unknown,
  options: {
    sandboxId: string | null;
    archiveSelection:
      | 'run_file_delivery_source'
      | 'project_sandbox'
      | 'current_sandbox_fallback'
      | 'unavailable';
  },
): ArchiveDownloadErrorState {
  if (error instanceof SandboxArchiveDownloadError) {
    switch (error.kind) {
      case 'authorization':
        return {
          title: 'Archive download was denied',
          message: error.detail || 'The backend rejected access to this archive request.',
          status: error.status,
          kind: error.kind,
          sandboxId: error.sandboxId,
          archiveSelection: options.archiveSelection,
          operationId: error.operationId,
          requestId: error.requestId,
          responseSource: error.responseSource,
          identitySource: error.identitySource,
          sandboxAvailable: error.sandboxAvailable,
          outcome: error.outcome,
          fallback: error.fallback,
        };
      case 'empty_artifact_set':
        return {
          title: 'No downloadable files were found',
          message:
            error.detail ||
            'The backend could not find any persisted files for this archive request.',
          status: error.status,
          kind: error.kind,
          sandboxId: error.sandboxId,
          archiveSelection: options.archiveSelection,
          operationId: error.operationId,
          requestId: error.requestId,
          responseSource: error.responseSource,
          identitySource: error.identitySource,
          sandboxAvailable: error.sandboxAvailable,
          outcome: error.outcome,
          fallback: error.fallback,
        };
      case 'source_identity':
        return {
          title: 'Archive source could not be resolved',
          message:
            error.detail ||
            'The backend could not resolve the sandbox or artifact source for this archive request.',
          status: error.status,
          kind: error.kind,
          sandboxId: error.sandboxId,
          archiveSelection: options.archiveSelection,
          operationId: error.operationId,
          requestId: error.requestId,
          responseSource: error.responseSource,
          identitySource: error.identitySource,
          sandboxAvailable: error.sandboxAvailable,
          outcome: error.outcome,
          fallback: error.fallback,
        };
      case 'not_found':
        return {
          title: 'Archive source was not found',
          message: error.detail || 'The backend could not find the requested archive source.',
          status: error.status,
          kind: error.kind,
          sandboxId: error.sandboxId,
          archiveSelection: options.archiveSelection,
          operationId: error.operationId,
          requestId: error.requestId,
          responseSource: error.responseSource,
          identitySource: error.identitySource,
          sandboxAvailable: error.sandboxAvailable,
          outcome: error.outcome,
          fallback: error.fallback,
        };
      case 'server':
        return {
          title: 'Archive download failed on the server',
          message: error.detail || 'The backend failed while preparing the archive.',
          status: error.status,
          kind: error.kind,
          sandboxId: error.sandboxId,
          archiveSelection: options.archiveSelection,
          operationId: error.operationId,
          requestId: error.requestId,
          responseSource: error.responseSource,
          identitySource: error.identitySource,
          sandboxAvailable: error.sandboxAvailable,
          outcome: error.outcome,
          fallback: error.fallback,
        };
      case 'unknown':
      default:
        return {
          title: 'Archive download failed',
          message: error.detail || 'The backend returned an unexpected archive error.',
          status: error.status,
          kind: error.kind,
          sandboxId: error.sandboxId,
          archiveSelection: options.archiveSelection,
          operationId: error.operationId,
          requestId: error.requestId,
          responseSource: error.responseSource,
          identitySource: error.identitySource,
          sandboxAvailable: error.sandboxAvailable,
          outcome: error.outcome,
          fallback: error.fallback,
        };
    }
  }

  const fallbackMessage =
    error instanceof Error && error.message
      ? error.message
      : 'The archive request failed before a file could be downloaded.';
  return {
    title: 'Archive download failed',
    message: fallbackMessage,
    kind: 'unknown',
    sandboxId: options.sandboxId,
    archiveSelection: options.archiveSelection,
  };
}

function formatBooleanDiagnostic(value: boolean | null | undefined): string {
  if (value == null) return 'unknown';
  return value ? 'yes' : 'no';
}

export function FileViewerModal({
  open,
  onOpenChange,
  sandboxId,
  initialFilePath,
  project,
  filePathList,
}: FileViewerModalProps) {
  // Safely handle initialFilePath to ensure it's a string or null
  const safeInitialFilePath = typeof initialFilePath === 'string' ? initialFilePath : null;
  const normalizedSandboxId = sandboxId.trim() ? sandboxId.trim() : null;
  const fileDeliverySource = project?.file_delivery_source ?? {
    browseSandboxId: normalizedSandboxId,
    archiveSandboxId: normalizedSandboxId,
    browseRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
    archiveRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
    identitySource: normalizedSandboxId ? 'sandbox_prop_fallback' : 'unavailable',
    archiveSelection: normalizedSandboxId
      ? 'current_sandbox_fallback'
      : 'unavailable',
    downloadMode: 'server_archive' as const,
  };
  const browseSandboxId = fileDeliverySource.browseSandboxId;
  const archiveSandboxId = fileDeliverySource.archiveSandboxId;
  const [resolvedBrowseSandboxId, setResolvedBrowseSandboxId] = useState<string | null>(null);
  const activeBrowseSandboxId = resolvedBrowseSandboxId ?? browseSandboxId;
  const uploadSandboxId = project?.file_delivery_source
    ? activeBrowseSandboxId
    : normalizedSandboxId;
  const browseRootPath = normalizeFileDeliveryRootPath(
    fileDeliverySource.browseRootPath,
  );
  const archiveRootPath = normalizeFileDeliveryRootPath(
    fileDeliverySource.archiveRootPath,
  );

  // File navigation state
  const [currentPath, setCurrentPath] = useState(browseRootPath);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  // Add navigation state for file list mode
  const [currentFileIndex, setCurrentFileIndex] = useState<number>(-1);
  const isFileListMode = Boolean(filePathList && filePathList.length > 0);

  // Use React Query for directory listing
  const {
    data: files = [],
    diagnostics: listDiagnostics,
    isLoading: isLoadingFiles,
    error: filesError,
    refetch: refetchFiles
  } = useDirectoryQuery(activeBrowseSandboxId, currentPath, {
    enabled: open && !!activeBrowseSandboxId,
    staleTime: 30 * 1000, // 30 seconds
    rootPath: browseRootPath,
  });
  const effectiveBrowseSandboxId = activeBrowseSandboxId;
  const effectiveIdentitySource =
    listDiagnostics?.identitySource ?? fileDeliverySource.identitySource;

  useEffect(() => {
    if (open) {
      refetchFiles();
    }
  }, [open, refetchFiles]);

  useEffect(() => {
    const nextResolvedBrowseSandboxId =
      listDiagnostics?.resolvedSandboxId?.trim() || null;
    if (!nextResolvedBrowseSandboxId) {
      return;
    }

    setResolvedBrowseSandboxId((currentBrowseSandboxId) => {
      return currentBrowseSandboxId === nextResolvedBrowseSandboxId
        ? currentBrowseSandboxId
        : nextResolvedBrowseSandboxId;
    });
  }, [listDiagnostics?.resolvedSandboxId]);

  // Add a navigation lock to prevent race conditions
  const currentNavigationRef = useRef<string | null>(null);

  // File content state
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [textContentForRenderer, setTextContentForRenderer] = useState<
    string | null
  >(null);
  const [blobUrlForRenderer, setBlobUrlForRenderer] = useState<string | null>(
    null,
  );
  const [contentError, setContentError] = useState<string | null>(null);

  // Use the React Query hook for the selected file instead of useCachedFile
  const {
    data: cachedFileContent,
    isLoading: isCachedFileLoading,
    error: cachedFileError,
  } = useFileContentQuery(
    effectiveBrowseSandboxId,
    selectedFilePath || undefined,
    {
      // Auto-detect content type consistently with other components
      enabled: open && !!selectedFilePath,
      staleTime: 5 * 60 * 1000, // 5 minutes
      rootPath: browseRootPath,
    }
  );

  // Utility state
  const [isUploading, setIsUploading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // State to track if initial path has been processed
  const [initialPathProcessed, setInitialPathProcessed] = useState(false);

  // Project state
  const [projectWithSandbox, setProjectWithSandbox] = useState<
    Project | undefined
  >(project);

  // Add state for PDF export
  const [isExportingPdf, setIsExportingPdf] = useState(false);
  const markdownRef = useRef<HTMLDivElement>(null);

  // Add a ref to track active download URLs
  const activeDownloadUrls = useRef<Set<string>>(new Set());

  // Add state for download all functionality
  const [isDownloadingAll, setIsDownloadingAll] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<{
    current: number;
    total: number;
    currentFile: string;
  } | null>(null);
  const [archiveDownloadError, setArchiveDownloadError] =
    useState<ArchiveDownloadErrorState | null>(null);
  const [preparedDownload, setPreparedDownload] =
    useState<PreparedDownloadState | null>(null);
  const [downloadDiagnostics, setDownloadDiagnostics] =
    useState<DownloadDiagnosticsState | null>(null);

  // Add state for copy functionality
  const [isCopyingPath, setIsCopyingPath] = useState(false);
  const [isCopyingContent, setIsCopyingContent] = useState(false);

  const clearPreparedDownload = useCallback(() => {
    setPreparedDownload((current) => {
      if (current) {
        URL.revokeObjectURL(current.url);
        activeDownloadUrls.current.delete(current.url);
      }

      return null;
    });
  }, []);

  const updateDownloadDiagnostics = useCallback((
    operationId: string,
    update: Partial<Omit<DownloadDiagnosticsState, 'operationId' | 'kind' | 'phaseHistory' | 'currentPhase'>> & {
      phase?: DownloadPhase;
    },
  ) => {
    setDownloadDiagnostics((current) => {
      if (!current || current.operationId !== operationId) {
        return current;
      }

      const nextPhase = update.phase ?? current.currentPhase;
      const nextPhaseHistory =
        update.phase && current.phaseHistory[current.phaseHistory.length - 1] !== update.phase
          ? [...current.phaseHistory, update.phase]
          : current.phaseHistory;

      return {
        ...current,
        ...update,
        currentPhase: nextPhase,
        phaseHistory: nextPhaseHistory,
      };
    });
  }, []);

  const beginDownloadDiagnostics = useCallback((
    kind: 'file' | 'archive',
    sourceSandboxId: string | null,
  ) => {
    const operationId = createFileActionOperationId(kind === 'archive' ? 'archive' : 'download');
    setDownloadDiagnostics({
      kind,
      operationId,
      currentPhase: 'clicked',
      phaseHistory: ['clicked'],
      requestId: null,
      responseSource: null,
      identitySource: null,
      sandboxAvailable: null,
      outcome: null,
      fallback: null,
      sandboxId: sourceSandboxId,
      archiveSelection: fileDeliverySource.archiveSelection,
      fileName: null,
      errorMessage: null,
    });
    return operationId;
  }, [fileDeliverySource.archiveSelection]);

  const handleTransferPhase = useCallback((
    operationId: string,
    event: FileActionPhaseEvent,
  ) => {
    updateDownloadDiagnostics(operationId, {
      phase: event.phase as DownloadPhase,
      requestId: event.requestId ?? null,
      responseSource: event.responseSource ?? null,
      identitySource: event.identitySource ?? null,
      sandboxAvailable: event.sandboxAvailable ?? null,
      outcome: event.outcome ?? null,
      fallback: event.fallback ?? null,
    });
  }, [updateDownloadDiagnostics]);

  // Setup project with sandbox URL if not provided directly
  useEffect(() => {
    setProjectWithSandbox(project);
  }, [project, sandboxId]);

  useEffect(() => {
    currentNavigationRef.current = null;
    setCurrentPath(browseRootPath);
    setIsInitialLoad(true);
    setResolvedBrowseSandboxId(null);
    setCurrentFileIndex(-1);
    setSelectedFilePath(null);
    setTextContentForRenderer(null);
    setBlobUrlForRenderer((current) => {
      if (current && !activeDownloadUrls.current.has(current)) {
        URL.revokeObjectURL(current);
      }
      return null;
    });
    setContentError(null);
    setInitialPathProcessed(false);
    setArchiveDownloadError(null);
    setDownloadDiagnostics(null);
    clearPreparedDownload();
  }, [archiveSandboxId, browseRootPath, browseSandboxId, clearPreparedDownload]);

  // Keep paths anchored to the resolved browse root for this file-delivery source.
  const normalizePath = useCallback((path: unknown): string => {
    return normalizeFileDeliveryPath(path, browseRootPath);
  }, [browseRootPath]);

  const downloadBlob = useCallback((
    blob: Blob,
    fileName: string,
    options: {
      kind: 'file' | 'archive';
      operationId: string;
      requestId: string | null;
      responseSource: string | null;
      identitySource: string | null;
      sandboxAvailable: boolean | null;
      outcome: string | null;
      fallback: boolean | null;
    },
  ) => {
    updateDownloadDiagnostics(options.operationId, {
      phase: 'save_attempted',
      fileName,
      requestId: options.requestId,
      responseSource: options.responseSource,
      identitySource: options.identitySource,
      sandboxAvailable: options.sandboxAvailable,
      outcome: options.outcome,
      fallback: options.fallback,
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);

    activeDownloadUrls.current.add(url);
    setPreparedDownload((current) => {
      if (current) {
        URL.revokeObjectURL(current.url);
        activeDownloadUrls.current.delete(current.url);
      }

      updateDownloadDiagnostics(options.operationId, {
        phase: 'manual_link_visible',
        fileName,
        requestId: options.requestId,
        responseSource: options.responseSource,
        identitySource: options.identitySource,
        sandboxAvailable: options.sandboxAvailable,
        outcome: options.outcome,
        fallback: options.fallback,
      });
      return {
        url,
        fileName,
        kind: options.kind,
        operationId: options.operationId,
        requestId: options.requestId,
        responseSource: options.responseSource,
        identitySource: options.identitySource,
        sandboxAvailable: options.sandboxAvailable,
        outcome: options.outcome,
        fallback: options.fallback,
      };
    });
  }, [updateDownloadDiagnostics]);

  // Function to download all files as a zip
  const handleDownloadAll = useCallback(async () => {
    if (isDownloadingAll) return;

    const operationId = beginDownloadDiagnostics('archive', archiveSandboxId);

    try {
      setIsDownloadingAll(true);
      setArchiveDownloadError(null);
      setDownloadProgress({ current: 0, total: 0, currentFile: 'Requesting server archive...' });

      if (!archiveSandboxId) {
        const missingSourceError = describeArchiveDownloadError(
          new Error('No archive download source is available for this workspace.'),
          {
            sandboxId: null,
            archiveSelection: fileDeliverySource.archiveSelection,
          },
        );
        updateDownloadDiagnostics(operationId, {
          errorMessage: missingSourceError.message,
        });
        setArchiveDownloadError(missingSourceError);
        toast.error(missingSourceError.title);
        return;
      }

      const archiveResult = await downloadSandboxArchive(archiveSandboxId, {
        root_path: archiveRootPath,
        include_hidden: false,
        continue_on_error: true,
      }, {
        operationId,
        onPhase: (event) => handleTransferPhase(operationId, event),
      });

      updateDownloadDiagnostics(operationId, {
        requestId: archiveResult.requestId,
        responseSource: archiveResult.responseSource,
        identitySource: archiveResult.identitySource,
        sandboxAvailable: archiveResult.sandboxAvailable,
        outcome: archiveResult.outcome,
        fallback: archiveResult.fallback,
        fileName: archiveResult.filename,
      });
      downloadBlob(archiveResult.blob, archiveResult.filename, {
        kind: 'archive',
        operationId: archiveResult.operationId,
        requestId: archiveResult.requestId,
        responseSource: archiveResult.responseSource,
        identitySource: archiveResult.identitySource,
        sandboxAvailable: archiveResult.sandboxAvailable,
        outcome: archiveResult.outcome,
        fallback: archiveResult.fallback,
      });

      const { total, succeeded, failed } = archiveResult.summary;
      if (total > 0 && failed > 0) {
        toast.warning(
          `Archive prepared: ${succeeded}/${total} files packaged, ${failed} failed. If the browser blocks the save, use the manual link below.`,
        );
      } else {
        toast.success(
          `Archive prepared: ${succeeded || total} files packaged. If the browser blocks the save, use the manual link below.`,
        );
      }
    } catch (error) {
      const describedError = describeArchiveDownloadError(error, {
        sandboxId: archiveSandboxId,
        archiveSelection: fileDeliverySource.archiveSelection,
      });
      updateDownloadDiagnostics(operationId, {
        requestId: describedError.requestId ?? null,
        responseSource: describedError.responseSource ?? null,
        identitySource: describedError.identitySource ?? null,
        sandboxAvailable: describedError.sandboxAvailable ?? null,
        outcome: describedError.outcome ?? null,
        fallback: describedError.fallback ?? null,
        errorMessage: describedError.message,
      });
      setArchiveDownloadError(describedError);
      toast.error(
        describedError.status
          ? `${describedError.title} (${describedError.status})`
          : describedError.title,
      );
    } finally {
      setIsDownloadingAll(false);
      setDownloadProgress(null);
    }
  }, [
    beginDownloadDiagnostics,
    archiveRootPath,
    archiveSandboxId,
    downloadBlob,
    fileDeliverySource.archiveSelection,
    handleTransferPhase,
    isDownloadingAll,
    updateDownloadDiagnostics,
  ]);

  // Helper function to check if a value is a Blob (type-safe version of instanceof)
  const isBlob = (value: any): value is Blob => {
    return value instanceof Blob;
  };

  // Helper function to clear the selected file
  const clearSelectedFile = useCallback(() => {
    setSelectedFilePath(null);
    setTextContentForRenderer(null); // Clear derived text content
    setBlobUrlForRenderer(null); // Clear derived blob URL
    setContentError(null);
    // Only reset file list mode index when not in file list mode
    if (!isFileListMode) {
      setCurrentFileIndex(-1);
    }
  }, [isFileListMode]);

  // Core file opening function
  const openFile = useCallback(
    async (file: FileInfo) => {
      if (file.is_dir) {
        // For directories, just navigate to that folder
        const normalizedPath = normalizePath(file.path);

        // Clear selected file when navigating
        clearSelectedFile();

        // Update path state - must happen after clearing selection
        setCurrentPath(normalizedPath);
        return;
      }

      // Skip if already selected
      if (selectedFilePath === file.path) {
        return;
      }

      // Clear previous state and set selected file
      clearSelectedFile();
      setSelectedFilePath(file.path);

      // Only reset file index if we're NOT in file list mode or the file is not in the list
      if (!isFileListMode || !filePathList?.includes(file.path)) {
        setCurrentFileIndex(-1);
      }

      // The useFileContentQuery hook will automatically handle loading the content
      // No need to manually fetch here - React Query will handle it
    },
    [
      selectedFilePath,
      clearSelectedFile,
      normalizePath,
      isFileListMode,
      filePathList,
    ],
  );

  // Load files when modal opens or path changes - Refined
  useEffect(() => {
    if (!open || !activeBrowseSandboxId) {
      return; // Don't load if modal is closed or no sandbox ID
    }

    // Skip repeated loads for the same path
    if (isLoadingFiles && currentNavigationRef.current === currentPath) {
      return;
    }

    // Track current navigation
    currentNavigationRef.current = currentPath;

    // React Query handles the loading state automatically

    // After the first load, set isInitialLoad to false
    if (isInitialLoad) {
      setIsInitialLoad(false);
    }

    // Handle any loading errors
    if (filesError) {
      toast.error('Failed to load files');
    }
  }, [activeBrowseSandboxId, currentPath, filesError, isInitialLoad, isLoadingFiles, open]);

  // Helper function to navigate to a folder
  const navigateToFolder = useCallback(
    (folder: FileInfo) => {
      if (!folder.is_dir) return;

      // Ensure the path is properly normalized
      const normalizedPath = normalizePath(folder.path);

      // Clear selected file when navigating
      clearSelectedFile();

      // Update path state - must happen after clearing selection
      setCurrentPath(normalizedPath);
    },
    [normalizePath, clearSelectedFile],
  );

  // Navigate to a specific path in the breadcrumb
  const navigateToBreadcrumb = useCallback(
    (path: string) => {
      const normalizedPath = normalizePath(path);

      // Clear selected file and set path
      clearSelectedFile();
      setCurrentPath(normalizedPath);
    },
    [normalizePath, clearSelectedFile],
  );

  // Helper function to navigate to home
  const navigateHome = useCallback(() => {
    clearSelectedFile();
    setCurrentPath(browseRootPath);
  }, [browseRootPath, clearSelectedFile]);

  // Function to generate breadcrumb segments from a path
  const getBreadcrumbSegments = useCallback(
    (path: string) => {
      // Ensure we're working with a normalized path
      const normalizedPath = normalizePath(path);

      const cleanPath =
        normalizedPath === browseRootPath
          ? ''
          : normalizedPath.startsWith(`${browseRootPath}/`)
            ? normalizedPath.slice(browseRootPath.length + 1)
            : normalizedPath.replace(/^\/+/, '');
      if (!cleanPath) return [];

      const parts = cleanPath.split('/').filter(Boolean);
      let currentPath = browseRootPath;

      return parts.map((part, index) => {
        currentPath = `${currentPath}/${part}`;
        return {
          name: part,
          path: currentPath,
          isLast: index === parts.length - 1,
        };
      });
    },
    [browseRootPath, normalizePath],
  );

  // Add a helper to directly interact with the raw cache
  const _directlyAccessCache = useCallback(
    (filePath: string): {
      found: boolean;
      content: any;
      contentType: string;
    } => {
      // Normalize the path for consistent cache key
      const normalizedPath = normalizePath(filePath);

      // Detect the appropriate content type based on file extension
      const detectedContentType = FileCache.getContentTypeFromPath(filePath);

      // Create cache key with detected content type
      const cacheKey = `${effectiveBrowseSandboxId}:${normalizedPath}:${detectedContentType}`;

      if (FileCache.has(cacheKey)) {
        const cachedContent = FileCache.get(cacheKey);
        return { found: true, content: cachedContent, contentType: detectedContentType };
      }

      return { found: false, content: null, contentType: detectedContentType };
    },
    [effectiveBrowseSandboxId, normalizePath],
  );

  const isDirectoryReadError = useCallback((errorMessage: string) => {
    return /is a directory/i.test(errorMessage);
  }, []);

  const sleep = useCallback((ms: number) => {
    return new Promise<void>((resolve) => {
      setTimeout(resolve, ms);
    });
  }, []);

  const resolveInitialPathEntry = useCallback(
    async (targetPath: string): Promise<FileInfo | null> => {
      const normalizedTargetPath = normalizePath(targetPath);

      if (normalizedTargetPath === browseRootPath) {
        return {
          name: browseRootPath.split('/').filter(Boolean).at(-1) || 'workspace',
          path: browseRootPath,
          is_dir: true,
          size: 0,
          mod_time: new Date().toISOString(),
        };
      }

      const buildDirectoryEntry = (): FileInfo => {
        const folderName = normalizedTargetPath.split('/').pop() || 'workspace';
        return {
          name: folderName,
          path: normalizedTargetPath,
          is_dir: true,
          size: 0,
          mod_time: new Date().toISOString(),
        };
      };

      const resolveFromParentListing = async (): Promise<FileInfo | null> => {
        const lastSlashIndex = normalizedTargetPath.lastIndexOf('/');
        const parentPath =
          lastSlashIndex > 0
            ? normalizedTargetPath.substring(0, lastSlashIndex)
            : browseRootPath;
        const targetName =
          lastSlashIndex >= 0 ? normalizedTargetPath.substring(lastSlashIndex + 1) : normalizedTargetPath;

        if (!effectiveBrowseSandboxId) {
          return null;
        }

        const siblingEntries = await listSandboxFiles(effectiveBrowseSandboxId, parentPath);
        const matchedEntry = siblingEntries.find((entry) => {
          const normalizedEntryPath = normalizePath(entry.path);
          return normalizedEntryPath === normalizedTargetPath || entry.name === targetName;
        });

        if (!matchedEntry) {
          return null;
        }

        return {
          ...matchedEntry,
          path: normalizePath(matchedEntry.path),
        };
      };

      // Probe twice to reduce eventual-consistency races after freshly created folders.
      const attemptDelays = [0, 200];
      for (const delayMs of attemptDelays) {
        if (delayMs > 0) {
          await sleep(delayMs);
        }

        try {
          const parentMatch = await resolveFromParentListing();
          if (parentMatch) {
            return parentMatch;
          }
        } catch {
          // Keep probing; parent listing can fail transiently while the sandbox FS catches up.
        }

        try {
          if (!effectiveBrowseSandboxId) {
            return null;
          }

          await listSandboxFiles(effectiveBrowseSandboxId, normalizedTargetPath);
          return buildDirectoryEntry();
        } catch {
          // Not a resolvable directory yet; retry/backoff handled by the loop.
        }
      }

      return null;
    },
    [browseRootPath, effectiveBrowseSandboxId, normalizePath, sleep],
  );

  const resolveFileListEntry = useCallback(
    async (targetPath: string): Promise<{
      entry: FileInfo | null;
      kind: 'file' | 'directory' | 'unknown';
      normalizedPath: string;
    }> => {
      const knownMatch = classifySandboxPathFromEntries(targetPath, files, normalizePath);
      if (knownMatch.entry) {
        return {
          entry: knownMatch.entry,
          kind: knownMatch.kind,
          normalizedPath: knownMatch.normalizedPath,
        };
      }

      const resolvedEntry = await resolveInitialPathEntry(targetPath);
      if (!resolvedEntry) {
        return {
          entry: null,
          kind: 'unknown',
          normalizedPath: knownMatch.normalizedPath,
        };
      }

      return {
        entry: resolvedEntry,
        kind: resolvedEntry.is_dir ? 'directory' : 'file',
        normalizedPath: normalizePath(resolvedEntry.path),
      };
    },
    [files, normalizePath, resolveInitialPathEntry],
  );

  const openFileListEntryByIndex = useCallback(
    async (
      index: number,
      mode: 'direct' | 'file_navigation' = 'direct',
    ): Promise<'file' | 'directory' | 'unknown' | 'out_of_bounds'> => {
      if (!isFileListMode || !filePathList || index < 0 || index >= filePathList.length) {
        return 'out_of_bounds';
      }

      const filePath = filePathList[index];
      const lastSlashIndex = filePath.lastIndexOf('/');
      const directoryPath =
        lastSlashIndex > 0 ? filePath.substring(0, lastSlashIndex) : browseRootPath;
      const fileName = filePath.split('/').pop() || filePath;
      const resolvedPath = await resolveFileListEntry(filePath);

      if (!resolvedPath.entry) {
        if (mode === 'direct') {
          clearSelectedFile();
          setContentError(null);
          setCurrentFileIndex(-1);
          setCurrentPath(normalizePath(directoryPath));
          toast.warning(`Unable to verify file type for ${fileName}. Browse folders to open it safely.`);
        }
        return 'unknown';
      }

      if (resolvedPath.entry.is_dir) {
        if (mode === 'direct') {
          setCurrentFileIndex(-1);
          openFile(resolvedPath.entry);
        }
        return 'directory';
      }

      setCurrentPath(normalizePath(directoryPath));
      setCurrentFileIndex(index);
      openFile(resolvedPath.entry);
      return 'file';
    },
    [
      isFileListMode,
      filePathList,
      browseRootPath,
      resolveFileListEntry,
      clearSelectedFile,
      normalizePath,
      openFile,
    ],
  );

  // Navigation functions for file list mode
  const navigatePrevious = useCallback(() => {
    if (!isFileListMode || !filePathList || currentFileIndex <= 0) {
      return;
    }

    void (async () => {
      for (let index = currentFileIndex - 1; index >= 0; index -= 1) {
        const outcome = await openFileListEntryByIndex(index, 'file_navigation');
        if (outcome === 'file') {
          return;
        }
      }
    })();
  }, [currentFileIndex, isFileListMode, filePathList, openFileListEntryByIndex]);

  const navigateNext = useCallback(() => {
    if (!isFileListMode || !filePathList || currentFileIndex >= filePathList.length - 1) {
      return;
    }

    void (async () => {
      for (let index = currentFileIndex + 1; index < filePathList.length; index += 1) {
        const outcome = await openFileListEntryByIndex(index, 'file_navigation');
        if (outcome === 'file') {
          return;
        }
      }
    })();
  }, [currentFileIndex, isFileListMode, filePathList, openFileListEntryByIndex]);

  // Handle initial file path - Runs ONLY ONCE on open if initialFilePath is provided
  useEffect(() => {
    if (!open) {
      setInitialPathProcessed(false);
      return;
    }

    if (!safeInitialFilePath || initialPathProcessed) {
      return;
    }

    let cancelled = false;

    const handleInitialPath = async () => {
      // If we're in file list mode, find the index and navigate to it
      if (isFileListMode && filePathList) {
        const normalizedInitialPath = normalizePath(safeInitialFilePath);
        const index = filePathList.findIndex((path) => normalizePath(path) === normalizedInitialPath);
        if (index !== -1) {
          await openFileListEntryByIndex(index, 'direct');
          setInitialPathProcessed(true);
          return;
        }
      }

      // Resolve file/folder type from parent directory metadata first.
      const fullPath = normalizePath(safeInitialFilePath);
      const lastSlashIndex = fullPath.lastIndexOf('/');
      const directoryPath =
        lastSlashIndex > 0 ? fullPath.substring(0, lastSlashIndex) : browseRootPath;
      const fileName =
        lastSlashIndex >= 0 ? fullPath.substring(lastSlashIndex + 1) : '';

      try {
        const initialEntry = await resolveInitialPathEntry(fullPath);
        if (cancelled) return;

        if (initialEntry) {
          if (initialEntry.is_dir) {
            openFile(initialEntry);
          } else {
            if (currentPath !== directoryPath) {
              setCurrentPath(directoryPath);
            }
            openFile(initialEntry);
          }
          setInitialPathProcessed(true);
          return;
        }
      } catch (error) {
        console.warn('[FileViewerModal] Failed to resolve initial path metadata', error);
      }

      if (cancelled) return;

      // Unknown path type: avoid forcing file preview to prevent directory misclassification.
      clearSelectedFile();
      setContentError(null);
      if (currentPath !== directoryPath) {
        setCurrentPath(directoryPath);
      }
      toast.warning(`Unable to verify file type for ${fileName || fullPath}. Browse folders to open it safely.`);
      setInitialPathProcessed(true);
    };

    handleInitialPath();

    return () => {
      cancelled = true;
    };
  }, [
    open,
    safeInitialFilePath,
    initialPathProcessed,
    isFileListMode,
    filePathList,
    normalizePath,
    openFileListEntryByIndex,
    resolveInitialPathEntry,
    openFile,
    clearSelectedFile,
    browseRootPath,
    currentPath,
  ]);

  // Effect to handle cached file content updates
  useEffect(() => {
    if (!selectedFilePath) return;

    // Handle errors
    if (cachedFileError) {
      const errorMessage = cachedFileError.message || '';
      if (isDirectoryReadError(errorMessage)) {
        const normalizedDirectoryPath = normalizePath(selectedFilePath);
        clearSelectedFile();
        setContentError(null);
        setCurrentPath(normalizedDirectoryPath);
        toast.info('Opened folder view because the selected path is a directory.');
        return;
      }

      setContentError(`Failed to load file: ${cachedFileError.message}`);
      return;
    }

    // Handle successful content
    if (cachedFileContent !== null && !isCachedFileLoading) {
      // Check file type to determine proper handling
      const isImageFile = FileCache.isImageFile(selectedFilePath);
      const isPdfFile = FileCache.isPdfFile(selectedFilePath);
      const extension = selectedFilePath.split('.').pop()?.toLowerCase();
      const isOfficeFile = ['xlsx', 'xls', 'docx', 'doc', 'pptx', 'ppt'].includes(extension || '');
      const isBinaryFile = isImageFile || isPdfFile || isOfficeFile;

      // Handle content based on type and file extension
      if (typeof cachedFileContent === 'string') {
        if (cachedFileContent.startsWith('blob:')) {
          // It's already a blob URL
          setTextContentForRenderer(null);
          setBlobUrlForRenderer(cachedFileContent);
        } else if (isBinaryFile) {
          // Binary files should not be displayed as text, even if they come as strings
          setTextContentForRenderer(null);
          setBlobUrlForRenderer(null);
          setContentError('Binary file received in incorrect format. Please try refreshing.');
        } else {
          // Actual text content for text files
          setTextContentForRenderer(cachedFileContent);
          setBlobUrlForRenderer(null);
        }
      } else if (isBlob(cachedFileContent)) {
        // Create blob URL for binary content
        const url = URL.createObjectURL(cachedFileContent);
        setBlobUrlForRenderer(url);
        setTextContentForRenderer(null);
      } else if (typeof cachedFileContent === 'object') {
        // convert to json string if file_contents is a object
        const jsonString = JSON.stringify(cachedFileContent, null, 2);
        setTextContentForRenderer(jsonString);
        setBlobUrlForRenderer(null);
      }
      else {
        // Unknown content type
        setTextContentForRenderer(null);
        setBlobUrlForRenderer(null);
        setContentError('Unknown content type received.');
      }
    }
  }, [
    selectedFilePath,
    cachedFileContent,
    isCachedFileLoading,
    cachedFileError,
    isDirectoryReadError,
    normalizePath,
    clearSelectedFile,
  ]);

  // Modify the cleanup effect to respect active downloads
  useEffect(() => {
    const activeDownloadUrlsSnapshot = activeDownloadUrls.current;

    return () => {
      if (blobUrlForRenderer && !isDownloading && !activeDownloadUrlsSnapshot.has(blobUrlForRenderer)) {
        URL.revokeObjectURL(blobUrlForRenderer);
      }
    };
  }, [blobUrlForRenderer, isDownloading]);

  useEffect(() => {
    return () => {
      clearPreparedDownload();
    };
  }, [clearPreparedDownload]);

  // Keyboard navigation
  useEffect(() => {
    if (!open || !isFileListMode) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        navigatePrevious();
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        navigateNext();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open, isFileListMode, navigatePrevious, navigateNext]);

  // Handle modal close
  const handleOpenChange = useCallback(
    (open: boolean) => {
      if (!open) {
        // Only revoke if not downloading and not an active download URL
        if (blobUrlForRenderer && !isDownloading && !activeDownloadUrls.current.has(blobUrlForRenderer)) {
          URL.revokeObjectURL(blobUrlForRenderer);
        }

        clearSelectedFile();
        setCurrentPath(browseRootPath);
        // React Query will handle clearing the files data
        setInitialPathProcessed(false);
        setIsInitialLoad(true);
        setCurrentFileIndex(-1); // Reset file index

        // Reset download all state
        setIsDownloadingAll(false);
        setDownloadProgress(null);
        setArchiveDownloadError(null);
        setDownloadDiagnostics(null);
        clearPreparedDownload();
      }
      onOpenChange(open);
    },
    [
      blobUrlForRenderer,
      browseRootPath,
      clearPreparedDownload,
      clearSelectedFile,
      isDownloading,
      onOpenChange,
      setIsInitialLoad,
    ],
  );

  // Helper to check if file is markdown
  const isMarkdownFile = useCallback((filePath: string | null) => {
    return filePath ? filePath.toLowerCase().endsWith('.md') : false;
  }, []);

  // Copy functions
  const copyToClipboard = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      return false;
    }
  }, []);

  const handleCopyPath = useCallback(async () => {
    if (!textContentForRenderer) return;
    
    setIsCopyingPath(true);
    const success = await copyToClipboard(textContentForRenderer);
    if (success) {
      toast.success('File content copied to clipboard');
    } else {
      toast.error('Failed to copy file content');
    }
    setTimeout(() => setIsCopyingPath(false), 500);
  }, [textContentForRenderer, copyToClipboard]);

  const handleCopyContent = useCallback(async () => {
    if (!textContentForRenderer) return;
    
    setIsCopyingContent(true);
    const success = await copyToClipboard(textContentForRenderer);
    if (success) {
      toast.success('File content copied to clipboard');
    } else {
      toast.error('Failed to copy file content');
    }
    setTimeout(() => setIsCopyingContent(false), 500);
  }, [textContentForRenderer, copyToClipboard]);

  // Handle PDF export for markdown files
  const handleExportPdf = useCallback(
    async (orientation: 'portrait' | 'landscape' = 'portrait') => {
      if (
        !selectedFilePath ||
        isExportingPdf ||
        !isMarkdownFile(selectedFilePath)
      )
        return;

      setIsExportingPdf(true);

      try {
        // Use the ref to access the markdown content directly
        if (!markdownRef.current) {
          throw new Error('Markdown content not found');
        }

        // Create a standalone document for printing
        const printWindow = window.open('', '_blank');
        if (!printWindow) {
          throw new Error(
            'Unable to open print window. Please check if popup blocker is enabled.',
          );
        }

        // Get the base URL for resolving relative URLs
        const _baseUrl = window.location.origin;

        // Generate HTML content
        const fileName = selectedFilePath.split('/').pop() || 'document';
        const pdfName = fileName.replace(/\.md$/, '');

        // Extract content
        const markdownContent = markdownRef.current.innerHTML;

        // Generate a full HTML document with controlled styles
        const htmlContent = `
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="UTF-8">
          <title>${pdfName}</title>
          <style>
            @media print {
              @page { 
                size: ${orientation === 'landscape' ? 'A4 landscape' : 'A4'};
                margin: 15mm;
              }
              body {
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
              }
            }
            body {
              font-family: 'Helvetica', 'Arial', sans-serif;
              font-size: 12pt;
              color: #333;
              line-height: 1.5;
              padding: 20px;
              max-width: 100%;
              margin: 0 auto;
              background: white;
            }
            h1 { font-size: 24pt; margin-top: 20pt; margin-bottom: 12pt; }
            h2 { font-size: 20pt; margin-top: 18pt; margin-bottom: 10pt; }
            h3 { font-size: 16pt; margin-top: 16pt; margin-bottom: 8pt; }
            h4, h5, h6 { font-weight: bold; margin-top: 12pt; margin-bottom: 6pt; }
            p { margin: 8pt 0; }
            pre, code {
              font-family: 'Courier New', monospace;
              background-color: #f5f5f5;
              border-radius: 3pt;
              padding: 2pt 4pt;
              font-size: 10pt;
            }
            pre {
              padding: 8pt;
              margin: 8pt 0;
              overflow-x: auto;
              white-space: pre-wrap;
            }
            code {
              white-space: pre-wrap;
            }
            img {
              max-width: 100%;
              height: auto;
            }
            a {
              color: #0066cc;
              text-decoration: underline;
            }
            ul, ol {
              padding-left: 20pt;
              margin: 8pt 0;
            }
            blockquote {
              margin: 8pt 0;
              padding-left: 12pt;
              border-left: 4pt solid #ddd;
              color: #666;
            }
            table {
              border-collapse: collapse;
              width: 100%;
              margin: 12pt 0;
            }
            th, td {
              border: 1pt solid #ddd;
              padding: 6pt;
              text-align: left;
            }
            th {
              background-color: #f5f5f5;
              font-weight: bold;
            }
            /* Syntax highlighting basic styles */
            .hljs-keyword, .hljs-selector-tag { color: #569cd6; }
            .hljs-literal, .hljs-number { color: #b5cea8; }
            .hljs-string { color: #ce9178; }
            .hljs-comment { color: #6a9955; }
            .hljs-attribute, .hljs-attr { color: #9cdcfe; }
            .hljs-function, .hljs-name { color: #dcdcaa; }
            .hljs-title.class_ { color: #4ec9b0; }
            .markdown-content pre { background-color: #f8f8f8; }
          </style>
        </head>
        <body>
          <div class="markdown-content">
            ${markdownContent}
          </div>
          <script>
            // Remove any complex CSS variables or functions that might cause issues
            document.querySelectorAll('[style]').forEach(el => {
              const style = el.getAttribute('style');
              if (style && (style.includes('oklch') || style.includes('var(--') || style.includes('hsl('))) {
                // Replace complex color values with simple ones or remove them
                el.setAttribute('style', style
                  .replace(/color:.*?(;|$)/g, 'color: #333;')
                  .replace(/background-color:.*?(;|$)/g, 'background-color: transparent;')
                );
              }
            });
            
            // Print automatically when loaded
            window.onload = () => {
              setTimeout(() => {
                window.print();
                setTimeout(() => window.close(), 500);
              }, 300);
            };
          </script>
        </body>
        </html>
      `;

        // Write the HTML content to the new window
        printWindow.document.open();
        printWindow.document.write(htmlContent);
        printWindow.document.close();

        toast.success('PDF export initiated. Check your print dialog.');
      } catch (error) {
        toast.error(
          `Failed to export PDF: ${error instanceof Error ? error.message : String(error)}`,
        );
      } finally {
        setIsExportingPdf(false);
      }
    },
    [selectedFilePath, isExportingPdf, isMarkdownFile],
  );

  // Handle file download - streamlined for performance
  const handleDownload = async () => {
    if (!selectedFilePath || isDownloading) return;

    const operationId = beginDownloadDiagnostics('file', effectiveBrowseSandboxId);

    try {
      setIsDownloading(true);
      if (!effectiveBrowseSandboxId) {
        throw new Error('No file download source is available for this workspace.');
      }

      const downloadResult = await downloadSandboxFile(
        effectiveBrowseSandboxId,
        selectedFilePath,
        {
          operationId,
          onPhase: (event) => handleTransferPhase(operationId, event),
        },
      );
      updateDownloadDiagnostics(operationId, {
        requestId: downloadResult.requestId,
        responseSource: downloadResult.responseSource,
        fallback: downloadResult.fallback,
        fileName: downloadResult.filename,
      });
      downloadBlob(downloadResult.blob, downloadResult.filename, {
        kind: 'file',
        operationId: downloadResult.operationId,
        requestId: downloadResult.requestId,
        responseSource: downloadResult.responseSource,
        identitySource: null,
        sandboxAvailable: null,
        outcome: null,
        fallback: downloadResult.fallback,
      });
      toast.info(
        'File download prepared. If your browser does not save it automatically, use the manual link below.',
      );

    } catch (error) {
      updateDownloadDiagnostics(operationId, {
        errorMessage: error instanceof Error ? error.message : String(error),
      });
      toast.error(`Failed to download file: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setIsDownloading(false);
    }
  };

  // Handle file upload - Define after helpers
  const handleUpload = useCallback(() => {
    if (fileInputRef.current) {
      fileInputRef.current.click();
    }
  }, []);



  // Process uploaded file - Define after helpers
  const processUpload = useCallback(
    async (event: React.ChangeEvent<HTMLInputElement>) => {
      if (!event.target.files || event.target.files.length === 0) return;

      const file = event.target.files[0];
      setIsUploading(true);

      try {
        // Normalize filename to NFC
        const normalizedName = normalizeFilenameToNFC(file.name);
        const uploadPath = `${currentPath}/${normalizedName}`;

        const formData = new FormData();
        // If the filename was normalized, append with the normalized name in the field name
        // The server will use the path parameter for the actual filename
        formData.append('file', file, normalizedName);
        formData.append('path', uploadPath);

        const supabase = createClient();
        const {
          data: { session },
        } = await supabase.auth.getSession();

        if (!session?.access_token) {
          throw new Error('No access token available');
        }

        if (!uploadSandboxId) {
          throw new Error('No upload source is available for this workspace.');
        }

        const response = await fetch(
          `${API_URL}/sandboxes/${uploadSandboxId}/files`,
          {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${session.access_token}`,
            },
            body: formData,
          },
        );

        if (!response.ok) {
          const error = await response.text();
          throw new Error(error || 'Upload failed');
        }

        // Reload the file list using React Query
        await refetchFiles();

        toast.success(`Uploaded: ${normalizedName}`);
      } catch (error) {
        toast.error(
          `Upload failed: ${error instanceof Error ? error.message : String(error)}`,
        );
      } finally {
        setIsUploading(false);
        if (event.target) event.target.value = '';
      }
    },
    [currentPath, refetchFiles, uploadSandboxId],
  );

  const activePreparedDiagnostics =
    preparedDownload && downloadDiagnostics?.operationId === preparedDownload.operationId
      ? downloadDiagnostics
      : null;
  const activeArchiveErrorDiagnostics =
    archiveDownloadError && downloadDiagnostics?.operationId === archiveDownloadError.operationId
      ? downloadDiagnostics
      : null;
  const hasBrowseSource = Boolean(effectiveBrowseSandboxId);
  const directoryErrorMessage =
    filesError instanceof Error
      ? filesError.message
      : filesError
        ? String(filesError)
        : null;

  // Reset file list mode when modal opens without filePathList
  useEffect(() => {
    if (open && !filePathList) {
      setCurrentFileIndex(-1);
    }
  }, [open, filePathList]);

  // --- Render --- //
  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-[90vw] md:max-w-[1200px] w-[95vw] h-[90vh] max-h-[900px] flex flex-col p-0 gap-0 overflow-hidden">
        <DialogHeader className="px-4 py-2 border-b flex-shrink-0 flex flex-row gap-4 items-center">
          <DialogTitle className="text-lg font-semibold">
            Workspace Files
          </DialogTitle>

          {/* Download progress display */}
          {downloadProgress && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <div className="flex items-center gap-1">
                <Loader className="h-3 w-3 animate-spin" />
                <span>
                  {downloadProgress.total > 0
                    ? `${downloadProgress.current}/${downloadProgress.total}`
                    : 'Preparing...'
                  }
                </span>
              </div>
              <span className="max-w-[200px] truncate">
                {downloadProgress.currentFile}
              </span>
            </div>
          )}

          <div className="flex items-center gap-2">
            {/* Navigation arrows for file list mode */}
            {(() => {
              return isFileListMode && selectedFilePath && filePathList && filePathList.length > 1 && currentFileIndex >= 0;
            })() && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={navigatePrevious}
                    disabled={currentFileIndex <= 0}
                    className="h-8 w-8 p-0"
                    title="Previous file (←)"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </Button>
                  <div className="text-xs text-muted-foreground px-1">
                    {currentFileIndex + 1} / {(filePathList?.length || 0)}
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={navigateNext}
                    disabled={currentFileIndex >= (filePathList?.length || 0) - 1}
                    className="h-8 w-8 p-0"
                    title="Next file (→)"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </>
              )}
          </div>
        </DialogHeader>

        {archiveDownloadError ? (
          <div className="mx-4 mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-950">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-700" />
              <div className="min-w-0">
                <p className="font-medium">{archiveDownloadError.title}</p>
                <p className="mt-1 break-words text-amber-900/90">
                  {archiveDownloadError.message}
                </p>
                <p className="mt-2 text-xs text-amber-900/75">
                  {archiveDownloadError.status
                    ? `HTTP ${archiveDownloadError.status} `
                    : ''}
                  {archiveDownloadError.sandboxId
                    ? `archive source sandbox: ${archiveDownloadError.sandboxId}`
                    : 'archive source sandbox: unavailable'}
                  {' · '}
                  selection: {archiveDownloadError.archiveSelection}
                  {archiveDownloadError.operationId
                    ? ` · request: ${archiveDownloadError.operationId}`
                    : ''}
                </p>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-amber-900/75">
                  {archiveDownloadError.requestId ? (
                    <span className="break-all">backend request: {archiveDownloadError.requestId}</span>
                  ) : null}
                  {archiveDownloadError.responseSource ? (
                    <span>source: {archiveDownloadError.responseSource}</span>
                  ) : null}
                  {archiveDownloadError.identitySource ? (
                    <span>identity: {archiveDownloadError.identitySource}</span>
                  ) : null}
                  {archiveDownloadError.outcome ? (
                    <span>outcome: {archiveDownloadError.outcome}</span>
                  ) : null}
                  <span>
                    sandbox available: {formatBooleanDiagnostic(archiveDownloadError.sandboxAvailable)}
                  </span>
                  <span>fallback: {formatBooleanDiagnostic(archiveDownloadError.fallback)}</span>
                  {activeArchiveErrorDiagnostics ? (
                    <span>phase: {activeArchiveErrorDiagnostics.currentPhase}</span>
                  ) : null}
                </div>
                {activeArchiveErrorDiagnostics ? (
                  <p className="mt-1 break-words text-xs text-amber-900/70">
                    phases: {activeArchiveErrorDiagnostics.phaseHistory.join(' -> ')}
                  </p>
                ) : null}
              </div>
            </div>
          </div>
        ) : null}

        {!archiveDownloadError && downloadDiagnostics?.errorMessage ? (
          <div className="mx-4 mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-950">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-700" />
              <div className="min-w-0">
                <p className="font-medium">
                  {downloadDiagnostics.kind === 'archive'
                    ? 'Archive download failed'
                    : 'File download failed'}
                </p>
                <p className="mt-1 break-words text-amber-900/90">
                  {downloadDiagnostics.errorMessage}
                </p>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-amber-900/75">
                  <span className="break-all">request: {downloadDiagnostics.operationId}</span>
                  {downloadDiagnostics.requestId ? (
                    <span className="break-all">backend request: {downloadDiagnostics.requestId}</span>
                  ) : null}
                  {downloadDiagnostics.responseSource ? (
                    <span>source: {downloadDiagnostics.responseSource}</span>
                  ) : null}
                  {downloadDiagnostics.identitySource ? (
                    <span>identity: {downloadDiagnostics.identitySource}</span>
                  ) : null}
                  {downloadDiagnostics.outcome ? (
                    <span>outcome: {downloadDiagnostics.outcome}</span>
                  ) : null}
                  <span>
                    sandbox available: {formatBooleanDiagnostic(downloadDiagnostics.sandboxAvailable)}
                  </span>
                  <span>fallback: {formatBooleanDiagnostic(downloadDiagnostics.fallback)}</span>
                  <span>phase: {downloadDiagnostics.currentPhase}</span>
                </div>
                <p className="mt-1 break-words text-xs text-amber-900/70">
                  phases: {downloadDiagnostics.phaseHistory.join(' -> ')}
                </p>
              </div>
            </div>
          </div>
        ) : null}

        {preparedDownload ? (
          <div className="mx-4 mt-3 rounded-lg border border-sky-300 bg-sky-50 px-3 py-3 text-sm text-sky-950">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="font-medium">
                  {preparedDownload.kind === 'archive'
                    ? 'Archive download prepared'
                    : 'File download prepared'}
                </p>
                <p className="mt-1 break-words text-sky-900/90">
                  If your browser did not start the save automatically, use the manual link below.
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-sky-900/80">
                  <a
                    href={preparedDownload.url}
                    download={preparedDownload.fileName}
                    className="font-medium underline underline-offset-2"
                    onClick={() =>
                      updateDownloadDiagnostics(preparedDownload.operationId, {
                        phase: 'manual_link_clicked',
                      })
                    }
                  >
                    Download {preparedDownload.fileName}
                  </a>
                  <span className="break-all">
                    request: {preparedDownload.operationId}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-sky-900/80">
                  {preparedDownload.requestId ? (
                    <span className="break-all">backend request: {preparedDownload.requestId}</span>
                  ) : null}
                  {preparedDownload.responseSource ? (
                    <span>source: {preparedDownload.responseSource}</span>
                  ) : null}
                  {preparedDownload.identitySource ? (
                    <span>identity: {preparedDownload.identitySource}</span>
                  ) : null}
                  {preparedDownload.outcome ? (
                    <span>outcome: {preparedDownload.outcome}</span>
                  ) : null}
                  <span>
                    sandbox available: {formatBooleanDiagnostic(preparedDownload.sandboxAvailable)}
                  </span>
                  <span>fallback: {formatBooleanDiagnostic(preparedDownload.fallback)}</span>
                  {activePreparedDiagnostics ? (
                    <span>phase: {activePreparedDiagnostics.currentPhase}</span>
                  ) : null}
                </div>
                {activePreparedDiagnostics ? (
                  <p className="mt-1 break-words text-xs text-sky-900/75">
                    phases: {activePreparedDiagnostics.phaseHistory.join(' -> ')}
                  </p>
                ) : null}
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={clearPreparedDownload}
                className="h-8 flex-shrink-0"
              >
                Dismiss
              </Button>
            </div>
          </div>
        ) : null}

        {/* Navigation Bar */}
        <div className="px-4 py-2 border-b flex items-center gap-2">
          <Button
            variant="ghost"
            size="icon"
            onClick={navigateHome}
            className="h-8 w-8"
            title="Go to home directory"
          >
            <Home className="h-4 w-4" />
          </Button>

          <div className="flex items-center overflow-x-auto flex-1 min-w-0 scrollbar-hide whitespace-nowrap">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-sm font-medium min-w-fit flex-shrink-0"
              onClick={navigateHome}
            >
              home
            </Button>

            {currentPath !== browseRootPath && (
              <>
                {getBreadcrumbSegments(currentPath).map((segment) => (
                  <Fragment key={segment.path}>
                    <ChevronRight className="h-4 w-4 mx-1 text-muted-foreground opacity-50 flex-shrink-0" />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-sm font-medium truncate max-w-[200px]"
                      onClick={() => navigateToBreadcrumb(segment.path)}
                    >
                      {segment.name}
                    </Button>
                  </Fragment>
                ))}
              </>
            )}

            {selectedFilePath && (
              <>
                <ChevronRight className="h-4 w-4 mx-1 text-muted-foreground opacity-50 flex-shrink-0" />
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium truncate">
                    {selectedFilePath.split('/').pop()}
                  </span>
                </div>
              </>
            )}
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            {selectedFilePath && (
              <>
                {/* Copy content button - only show for text files */}
                {textContentForRenderer && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleCopyContent}
                    disabled={isCopyingContent || isCachedFileLoading}
                    className="h-8 gap-1"
                  >
                    {isCopyingContent ? (
                      <Check className="h-4 w-4" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                    <span className="hidden sm:inline">Copy</span>
                  </Button>
                )}

                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDownload}
                  disabled={isDownloading || isCachedFileLoading}
                  className="h-8 gap-1"
                >
                  {isDownloading ? (
                    <Loader className="h-4 w-4 animate-spin" />
                  ) : (
                    <Download className="h-4 w-4" />
                  )}
                  <span className="hidden sm:inline">Download</span>
                </Button>

                {/* Replace the Export as PDF button with a dropdown */}
                {isMarkdownFile(selectedFilePath) && (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={
                          isExportingPdf ||
                          isCachedFileLoading ||
                          contentError !== null
                        }
                        className="h-8 gap-1"
                      >
                        {isExportingPdf ? (
                          <Loader className="h-4 w-4 animate-spin" />
                        ) : (
                          <FileText className="h-4 w-4" />
                        )}
                        <span className="hidden sm:inline">Export as PDF</span>
                        <ChevronDown className="h-3 w-3 ml-1" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem
                        onClick={() => handleExportPdf('portrait')}
                        className="flex items-center gap-2 cursor-pointer"
                      >
                        <span className="rotate-90">⬌</span> Portrait
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        onClick={() => handleExportPdf('landscape')}
                        className="flex items-center gap-2 cursor-pointer"
                      >
                        <span>⬌</span> Landscape
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}
              </>
            )}

            {!selectedFilePath && (
              <>
                {/* Download All button - only show when in home directory */}
                {currentPath === browseRootPath && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleDownloadAll}
                    disabled={isDownloadingAll || isLoadingFiles}
                    className="h-8 gap-1"
                  >
                    {isDownloadingAll ? (
                      <Loader className="h-4 w-4 animate-spin" />
                    ) : (
                      <Archive className="h-4 w-4" />
                    )}
                    <span className="hidden sm:inline">Download All</span>
                  </Button>
                )}

                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleUpload}
                  disabled={isUploading || !uploadSandboxId}
                  className="h-8 gap-1"
                >
                  {isUploading ? (
                    <Loader className="h-4 w-4 animate-spin" />
                  ) : (
                    <Upload className="h-4 w-4" />
                  )}
                  <span className="hidden sm:inline">Upload</span>
                </Button>
              </>
            )}

            <input
              type="file"
              ref={fileInputRef}
              className="hidden"
              onChange={processUpload}
              disabled={isUploading}
            />
          </div>
        </div>

        {!selectedFilePath ? (
          <div className="border-b bg-muted/30 px-4 py-2 text-xs text-muted-foreground">
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              <span>browse sandbox: {effectiveBrowseSandboxId ?? 'none'}</span>
              <span>archive sandbox: {archiveSandboxId ?? 'none'}</span>
              <span>identity: {effectiveIdentitySource || 'unknown'}</span>
              <span>selection: {fileDeliverySource.archiveSelection}</span>
              <span>browse root: {browseRootPath}</span>
              <span>archive root: {archiveRootPath}</span>
              {listDiagnostics?.sandboxAvailable !== undefined ? (
                <span>
                  sandbox available:{' '}
                  {formatBooleanDiagnostic(listDiagnostics.sandboxAvailable)}
                </span>
              ) : null}
            </div>
          </div>
        ) : null}

        {/* Content Area */}
        <div className="flex-1 overflow-hidden">
          {selectedFilePath ? (
            /* File Viewer */
            <div className="h-full w-full overflow-auto">
              {isCachedFileLoading ? (
                <div className="h-full w-full flex flex-col items-center justify-center">
                  <Loader className="h-8 w-8 animate-spin text-primary mb-3" />
                  <p className="text-sm text-muted-foreground">
                    Loading file{selectedFilePath ? `: ${selectedFilePath.split('/').pop()}` : '...'}
                  </p>
                  <p className="text-xs text-muted-foreground/70 mt-1">
                    {(() => {
                      // Normalize the path for consistent cache checks
                      if (!selectedFilePath) return "Preparing...";

                      const normalizedPath = normalizePath(selectedFilePath);

                      // Detect the appropriate content type based on file extension
                      const detectedContentType = FileCache.getContentTypeFromPath(normalizedPath);

                      // Check for cache with the correct content type
                      const isCached = FileCache.has(`${effectiveBrowseSandboxId}:${normalizedPath}:${detectedContentType}`);

                      return isCached
                        ? "Using cached version"
                        : "Fetching from server";
                    })()}
                  </p>
                </div>
              ) : contentError ? (
                <div className="h-full w-full flex items-center justify-center p-4">
                  <div className="max-w-md p-6 text-center border rounded-lg bg-muted/10">
                    <AlertTriangle className="h-10 w-10 text-orange-500 mx-auto mb-4" />
                    <h3 className="text-lg font-medium mb-2">
                      Error Loading File
                    </h3>
                    <p className="text-sm text-muted-foreground mb-4">
                      {contentError}
                    </p>
                    <div className="flex justify-center gap-3">
                      <Button
                        onClick={() => {
                          setContentError(null);
                          openFile({
                            path: selectedFilePath,
                            name: selectedFilePath.split('/').pop() || '',
                            is_dir: false,
                            size: 0,
                            mod_time: new Date().toISOString(),
                          } as FileInfo);
                        }}
                      >
                        Retry
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => {
                          clearSelectedFile();
                        }}
                      >
                        Back to Files
                      </Button>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="h-full w-full relative">
                  {(() => {
                    // Safety check: don't render text content for binary files
                    const isImageFile = FileCache.isImageFile(selectedFilePath);
                    const isPdfFile = FileCache.isPdfFile(selectedFilePath);
                    const extension = selectedFilePath?.split('.').pop()?.toLowerCase();
                    const isOfficeFile = ['xlsx', 'xls', 'docx', 'doc', 'pptx', 'ppt'].includes(extension || '');
                    const isBinaryFile = isImageFile || isPdfFile || isOfficeFile;

                    // For binary files, only render if we have a blob URL
                    if (isBinaryFile && !blobUrlForRenderer) {
                      return (
                        <div className="h-full w-full flex items-center justify-center">
                          <div className="text-sm text-muted-foreground">
                            Loading {isPdfFile ? 'PDF' : isImageFile ? 'image' : 'file'}...
                          </div>
                        </div>
                      );
                    }

                    return (
                      <FileRenderer
                        key={selectedFilePath}
                        content={isBinaryFile ? null : textContentForRenderer}
                        binaryUrl={blobUrlForRenderer}
                        fileName={selectedFilePath}
                        className="h-full w-full"
                        project={projectWithSandbox}
                        markdownRef={
                          isMarkdownFile(selectedFilePath) ? markdownRef : undefined
                        }
                        onDownload={handleDownload}
                        isDownloading={isDownloading}
                      />
                    );
                  })()}
                </div>
              )}
            </div>
          ) : (
            /* File Explorer */
            <div className="h-full w-full">
              {!hasBrowseSource ? (
                <div className="h-full w-full flex items-center justify-center p-4">
                  <div className="max-w-md rounded-lg border bg-muted/10 p-6 text-center">
                    <AlertTriangle className="mx-auto mb-4 h-10 w-10 text-orange-500" />
                    <h3 className="mb-2 text-lg font-medium">
                      No browse source is available
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      This thread does not currently expose a sandbox to browse.
                    </p>
                    <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                      <p>archive sandbox: {archiveSandboxId ?? 'none'}</p>
                      <p>identity source: {effectiveIdentitySource || 'unknown'}</p>
                      <p>browse root: {browseRootPath}</p>
                    </div>
                  </div>
                </div>
              ) : isLoadingFiles ? (
                <div className="h-full w-full flex items-center justify-center">
                  <Loader className="h-6 w-6 animate-spin text-primary" />
                </div>
              ) : directoryErrorMessage ? (
                <div className="h-full w-full flex items-center justify-center p-4">
                  <div className="max-w-md rounded-lg border bg-muted/10 p-6 text-center">
                    <AlertTriangle className="mx-auto mb-4 h-10 w-10 text-orange-500" />
                    <h3 className="mb-2 text-lg font-medium">
                      Failed to load directory
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      {directoryErrorMessage}
                    </p>
                    <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                      <p>browse sandbox: {effectiveBrowseSandboxId ?? 'none'}</p>
                      <p>identity source: {effectiveIdentitySource || 'unknown'}</p>
                      <p>browse root: {browseRootPath}</p>
                      <p>selection: {fileDeliverySource.archiveSelection}</p>
                    </div>
                  </div>
                </div>
              ) : files.length === 0 ? (
                <div className="h-full w-full flex flex-col items-center justify-center">
                  <Folder className="h-12 w-12 mb-2 text-muted-foreground opacity-30" />
                  <p className="text-sm text-muted-foreground">
                    Directory is empty for the current file-delivery source
                  </p>
                  <div className="mt-3 space-y-1 text-center text-xs text-muted-foreground">
                    <p>browse sandbox: {effectiveBrowseSandboxId ?? 'none'}</p>
                    <p>archive sandbox: {archiveSandboxId ?? 'none'}</p>
                    <p>identity source: {effectiveIdentitySource || 'unknown'}</p>
                    <p>selection: {fileDeliverySource.archiveSelection}</p>
                    <p>browse root: {browseRootPath}</p>
                    {listDiagnostics?.sandboxAvailable !== undefined ? (
                      <p>
                        sandbox available:{' '}
                        {formatBooleanDiagnostic(listDiagnostics.sandboxAvailable)}
                      </p>
                    ) : null}
                  </div>
                </div>
              ) : (
                <ScrollArea className="h-full w-full p-2">
                  <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3 p-4">
                    {files.map((file) => (
                      <button
                        key={file.path}
                        className={`flex flex-col items-center p-3 rounded-2xl border hover:bg-muted/50 transition-colors ${selectedFilePath === file.path
                          ? 'bg-muted border-primary/20'
                          : ''
                          }`}
                        onClick={() => {
                          if (file.is_dir) {
                            navigateToFolder(file);
                          } else {
                            openFile(file);
                          }
                        }}
                      >
                        <div className="w-12 h-12 flex items-center justify-center mb-1">
                          {file.is_dir ? (
                            <Folder className="h-9 w-9 text-blue-500" />
                          ) : (
                            <File className="h-8 w-8 text-muted-foreground" />
                          )}
                        </div>
                        <span className="text-xs text-center font-medium truncate max-w-full">
                          {file.name}
                        </span>
                      </button>
                    ))}
                  </div>
                </ScrollArea>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
