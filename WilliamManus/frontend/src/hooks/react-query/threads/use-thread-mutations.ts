'use client';

import { createMutationHook } from '@/hooks/use-query';
import { 
  createThread, 
  addUserMessage,
  type ImageMediaRef,
} from '@/lib/api';
import { toast } from 'sonner';

export const useCreateThread = createMutationHook(
  ({ projectId }: { projectId: string }) => createThread(projectId),
  {
    onSuccess: () => {
      toast.success('Thread created successfully');
    },
    errorContext: {
      operation: 'create thread',
      resource: 'thread'
    }
  }
);

export const useAddUserMessage = createMutationHook(
  ({
    threadId,
    content,
    media_refs,
  }: {
    threadId: string;
    content: string;
    media_refs?: ImageMediaRef[];
  }) => addUserMessage(threadId, content, { media_refs }),
  {
    errorContext: {
      operation: 'add message',
      resource: 'message'
    }
  }
); 
