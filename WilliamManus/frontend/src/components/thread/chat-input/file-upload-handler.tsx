'use client';

import React, { forwardRef, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Paperclip, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { createClient } from '@/lib/supabase/client';
import { useQueryClient } from '@tanstack/react-query';
import { fileQueryKeys } from '@/hooks/react-query/files/use-file-queries';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { UploadedFile } from './chat-input';
import { normalizeFilenameToNFC } from '@/lib/utils/unicode';
import { prepareAgentAttachments } from '@/lib/api';

const API_URL = process.env.NEXT_PUBLIC_BACKEND_URL || '';

const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024;

const createClientId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `upload-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
};

const getUploadErrorMessage = (error: unknown, fallback: string): string => {
  if (typeof error === 'string') {
    return error;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
};

const filterAllowedFiles = (files: File[]): File[] =>
  files.filter((file) => {
    if (file.size > MAX_FILE_SIZE_BYTES) {
      toast.error(`文件大小超过50MB限制: ${file.name}`);
      return false;
    }
    return true;
  });

const createPlaceholderFile = (
  file: File,
  status: NonNullable<UploadedFile['status']>,
  options?: {
    localPreview?: boolean;
  },
): UploadedFile => {
  const normalizedName = normalizeFilenameToNFC(file.name);

  return {
    clientId: createClientId(),
    name: normalizedName,
    path: `/workspace/${normalizedName}`,
    size: file.size,
    type: file.type || 'application/octet-stream',
    localUrl: options?.localPreview ? URL.createObjectURL(file) : undefined,
    status,
  };
};

const handleLocalFiles = (
  files: File[],
  preparedProjectId: string | undefined,
  setPreparedProjectId: React.Dispatch<React.SetStateAction<string | undefined>>,
  setPendingFiles: React.Dispatch<React.SetStateAction<File[]>>,
  setUploadedFiles: React.Dispatch<React.SetStateAction<UploadedFile[]>>,
  setIsUploading: React.Dispatch<React.SetStateAction<boolean>>,
) => {
  const filteredFiles = filterAllowedFiles(files);
  if (filteredFiles.length === 0) {
    return;
  }

  const placeholderFiles = filteredFiles.map((file) =>
    createPlaceholderFile(file, 'preparing', { localPreview: true }),
  );
  const clientIds = new Set(placeholderFiles.map((file) => file.clientId));

  setPendingFiles((prev) => [...prev, ...filteredFiles]);
  setUploadedFiles((prev) => [...prev, ...placeholderFiles]);
  setIsUploading(true);

  void (async () => {
    try {
      const formData = new FormData();
      if (preparedProjectId) {
        formData.append('project_id', preparedProjectId);
      }

      filteredFiles.forEach((file) => {
        const normalizedName = normalizeFilenameToNFC(file.name);
        formData.append('files', file, normalizedName);
      });

      const result = await prepareAgentAttachments(formData);
      setPreparedProjectId(result.project_id);

      const preparedAttachments = result.attachments;
      setUploadedFiles((prev) =>
        prev.map((item) => {
          const placeholderIndex = placeholderFiles.findIndex(
            (placeholder) => placeholder.clientId === item.clientId,
          );
          if (placeholderIndex === -1) {
            return item;
          }

          const preparedAttachment = preparedAttachments[placeholderIndex];
          if (!preparedAttachment) {
            return {
              ...item,
              status: 'error',
              errorMessage: '文件准备失败，请重试。',
            };
          }

          return {
            ...item,
            name: preparedAttachment.name,
            path: preparedAttachment.path,
            size: preparedAttachment.size,
            type:
              preparedAttachment.content_type ||
              item.type ||
              'application/octet-stream',
            status: 'ready',
            preparedAttachment,
            errorMessage: undefined,
          };
        }),
      );

      preparedAttachments.forEach((attachment) => {
        toast.success(`文件已准备完成: ${attachment.name}`);
      });
    } catch (error) {
      const errorMessage = getUploadErrorMessage(
        error,
        '文件准备失败，请稍后重试。',
      );
      setUploadedFiles((prev) =>
        prev.map((item) =>
          clientIds.has(item.clientId)
            ? {
                ...item,
                status: 'error',
                errorMessage,
              }
            : item,
        ),
      );
      toast.error(errorMessage);
    } finally {
      setIsUploading(false);
    }
  })();
};

const uploadFiles = async (
  files: File[],
  sandboxId: string,
  setUploadedFiles: React.Dispatch<React.SetStateAction<UploadedFile[]>>,
  setIsUploading: React.Dispatch<React.SetStateAction<boolean>>,
  messages: any[] = [], // Add messages parameter to check for existing files
  queryClient?: any, // Add queryClient parameter for cache invalidation
) => {
  const filteredFiles = filterAllowedFiles(files);
  if (filteredFiles.length === 0) {
    return;
  }

  const placeholders = filteredFiles.map((file) =>
    createPlaceholderFile(file, 'uploading'),
  );
  setUploadedFiles((prev) => [...prev, ...placeholders]);

  try {
    setIsUploading(true);

    for (const [index, file] of filteredFiles.entries()) {
      const placeholder = placeholders[index];
      try {
        const normalizedName = normalizeFilenameToNFC(file.name);
        const uploadPath = `/workspace/${normalizedName}`;

        const isFileInChat = messages.some(message => {
          const content = typeof message.content === 'string' ? message.content : '';
          return content.includes(`[Uploaded File: ${uploadPath}]`);
        });

        const formData = new FormData();
        formData.append('file', file, normalizedName);
        formData.append('path', uploadPath);

        const supabase = createClient();
        const {
          data: { session },
        } = await supabase.auth.getSession();

        if (!session?.access_token) {
          throw new Error('缺少登录凭证');
        }

        const response = await fetch(`${API_URL}/sandboxes/${sandboxId}/files`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${session.access_token}`,
          },
          body: formData,
        });

        if (!response.ok) {
          throw new Error('文件上传失败');
        }

        if (isFileInChat && queryClient) {
          ['text', 'blob', 'json'].forEach(contentType => {
            const queryKey = fileQueryKeys.content(sandboxId, uploadPath, contentType);
            queryClient.removeQueries({ queryKey });
          });

          const directoryPath = uploadPath.substring(0, uploadPath.lastIndexOf('/'));
          queryClient.invalidateQueries({
            queryKey: fileQueryKeys.directory(sandboxId, directoryPath),
          });
        }

        setUploadedFiles((prev) =>
          prev.map((item) =>
            item.clientId === placeholder.clientId
              ? {
                  ...item,
                  path: uploadPath,
                  status: 'ready',
                  errorMessage: undefined,
                }
              : item,
          ),
        );

        toast.success(`Attachment uploaded: ${normalizedName}`);
      } catch (error) {
        const errorMessage = getUploadErrorMessage(
          error,
          'Attachment could not be uploaded.',
        );
        setUploadedFiles((prev) =>
          prev.map((item) =>
            item.clientId === placeholder.clientId
              ? {
                  ...item,
                  status: 'error',
                  errorMessage,
                }
              : item,
          ),
        );
        toast.error(`${placeholder.name}: ${errorMessage}`);
      }
    }
  } catch (error) {
    console.error('File upload failed:', error);
    const errorMessage = getUploadErrorMessage(error, 'Attachment upload failed.');
    setUploadedFiles((prev) =>
      prev.map((item) =>
        item.status === 'uploading'
          ? {
              ...item,
              status: 'error',
              errorMessage,
            }
          : item,
      ),
    );
    toast.error(errorMessage);
  } finally {
    setIsUploading(false);
  }
};

const handleFiles = async (
  files: File[],
  sandboxId: string | undefined,
  preparedProjectId: string | undefined,
  setPreparedProjectId: React.Dispatch<React.SetStateAction<string | undefined>>,
  setPendingFiles: React.Dispatch<React.SetStateAction<File[]>>,
  setUploadedFiles: React.Dispatch<React.SetStateAction<UploadedFile[]>>,
  setIsUploading: React.Dispatch<React.SetStateAction<boolean>>,
  messages: any[] = [], // Add messages parameter
  queryClient?: any, // Add queryClient parameter
) => {
  if (sandboxId) {
    // If we have a sandboxId, upload files directly
    await uploadFiles(files, sandboxId, setUploadedFiles, setIsUploading, messages, queryClient);
  } else {
    // Otherwise, prepare them in artifact storage before the first send
    handleLocalFiles(
      files,
      preparedProjectId,
      setPreparedProjectId,
      setPendingFiles,
      setUploadedFiles,
      setIsUploading,
    );
  }
};

interface FileUploadHandlerProps {
  loading: boolean;
  disabled: boolean;
  isAgentRunning: boolean;
  isUploading: boolean;
  sandboxId?: string;
  preparedProjectId?: string;
  setPreparedProjectId: React.Dispatch<React.SetStateAction<string | undefined>>;
  setPendingFiles: React.Dispatch<React.SetStateAction<File[]>>;
  setUploadedFiles: React.Dispatch<React.SetStateAction<UploadedFile[]>>;
  setIsUploading: React.Dispatch<React.SetStateAction<boolean>>;
  messages?: any[]; // Add messages prop
  isLoggedIn?: boolean;
}

export const FileUploadHandler = forwardRef<
  HTMLInputElement,
  FileUploadHandlerProps
>(
  (
    {
      loading,
      disabled,
      isAgentRunning,
      isUploading,
      sandboxId,
      preparedProjectId,
      setPreparedProjectId,
      setPendingFiles,
      setUploadedFiles,
      setIsUploading,
      messages = [],
      isLoggedIn = true,
    },
    ref,
  ) => {
    const queryClient = useQueryClient();
    // Clean up object URLs when component unmounts
    useEffect(() => {
      return () => {
        // Clean up any object URLs to avoid memory leaks
        setUploadedFiles(prev => {
          prev.forEach(file => {
            if (file.localUrl) {
              URL.revokeObjectURL(file.localUrl);
            }
          });
          return prev;
        });
      };
    }, []);

    const handleFileUpload = () => {
      if (ref && 'current' in ref && ref.current) {
        ref.current.click();
      }
    };

    const processFileUpload = async (
      event: React.ChangeEvent<HTMLInputElement>,
    ) => {
      if (!event.target.files || event.target.files.length === 0) return;

      const files = Array.from(event.target.files);
      // Use the helper function instead of the static method
      await handleFiles(
        files,
        sandboxId,
        preparedProjectId,
        setPreparedProjectId,
        setPendingFiles,
        setUploadedFiles,
        setIsUploading,
        messages,
        queryClient,
      );

      event.target.value = '';
    };

    return (
      <>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-block">
                <Button
                  type="button"
                  onClick={handleFileUpload}
                  variant="outline"
                  size="sm"
                  className="h-8 px-3 py-2 bg-transparent border border-border rounded-xl text-muted-foreground hover:text-foreground hover:bg-accent/50 flex items-center gap-2"
                  disabled={
                    !isLoggedIn || loading || (disabled && !isAgentRunning) || isUploading
                  }
                >
                  {isUploading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Paperclip className="h-4 w-4" />
                  )}
                  <span className="text-sm">附加</span>
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top">
              <p>{isLoggedIn ? '附加文件' : '请登录以附加文件'}</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <input
          type="file"
          ref={ref}
          className="hidden"
          onChange={processFileUpload}
          multiple
        />
      </>
    );
  },
);

FileUploadHandler.displayName = 'FileUploadHandler';
export { handleFiles, handleLocalFiles, uploadFiles };
