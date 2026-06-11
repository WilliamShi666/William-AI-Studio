'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useThreadQuery } from '@/hooks/react-query/threads/use-threads';
import { ThreadSkeleton } from '@/components/thread/content/ThreadSkeleton';

interface RedirectPageProps {
  threadId: string;
}

export function RedirectPage({ threadId }: RedirectPageProps) {
  const router = useRouter();
  const threadQuery = useThreadQuery(threadId);
  const projectId = threadQuery.data?.project_id;

  useEffect(() => {
    if (!projectId) {
      return;
    }
    router.replace(`/projects/${projectId}/thread/${threadId}`);
  }, [projectId, router, threadId]);

  useEffect(() => {
    if (!threadQuery.isError) {
      return;
    }
    router.replace('/dashboard');
  }, [router, threadQuery.isError]);

  if (threadQuery.isError) {
    return null;
  }
  return <ThreadSkeleton isSidePanelOpen={false} />;
}
