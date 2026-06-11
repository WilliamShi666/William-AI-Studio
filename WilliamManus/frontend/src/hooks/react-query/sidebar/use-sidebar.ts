'use client';

import { createMutationHook } from "@/hooks/use-query";
import { getSidebarProjects, getSidebarThreads, Project, Thread } from "@/lib/api";
import { createQueryHook } from '@/hooks/use-query';
import { threadKeys } from "./keys";
import { projectKeys } from "./keys";
import { deleteThread } from "../threads/utils";

export const useProjects = createQueryHook(
  projectKeys.lists(),
  async () => {
    const data = await getSidebarProjects();
    return data as Project[];
  },
  {
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  }
);

export const useThreads = createQueryHook(
  threadKeys.lists(),
  async () => {
    const data = await getSidebarThreads();
    return data as Thread[];
  },
  {
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  }
);

interface DeleteThreadVariables {
  threadId: string;
  sandboxId?: string;
  isNavigateAway?: boolean;
}

export const useDeleteThread = createMutationHook(
  async ({ threadId, sandboxId }: DeleteThreadVariables) => {
    return await deleteThread(threadId, sandboxId);
  },
  {
    onSuccess: () => {
    },
  }
);

interface DeleteMultipleThreadsVariables {
  threadIds: string[];
  threadSandboxMap?: Record<string, string>;
  onProgress?: (completed: number, total: number) => void;
}

const DELETE_THREADS_CONCURRENCY_LIMIT = 2;

export const useDeleteMultipleThreads = createMutationHook(
  async ({ threadIds, threadSandboxMap, onProgress }: DeleteMultipleThreadsVariables) => {
    let completedCount = 0;
    const results: Array<{ success: boolean; threadId: string; error?: unknown }> = [];

    for (let index = 0; index < threadIds.length; index += DELETE_THREADS_CONCURRENCY_LIMIT) {
      const batch = threadIds.slice(index, index + DELETE_THREADS_CONCURRENCY_LIMIT);
      const batchResults = await Promise.all(
        batch.map(async (threadId) => {
          try {
            const sandboxId = threadSandboxMap?.[threadId];
            await deleteThread(threadId, sandboxId);
            completedCount++;
            onProgress?.(completedCount, threadIds.length);
            return { success: true, threadId };
          } catch (error) {
            return { success: false, threadId, error };
          }
        }),
      );
      results.push(...batchResults);
    }
    
    return {
      successful: results.filter(r => r.success).map(r => r.threadId),
      failed: results.filter(r => !r.success).map(r => r.threadId),
    };
  },
  {
    onSuccess: () => {
    },
  }
);

export type ThreadWithProject = {
  threadId: string;
  projectId: string;
  projectName: string;
  url: string;
  updatedAt: string;
};

export const processThreadsWithProjects = (
  threads: Thread[],
  projects: Project[]
): ThreadWithProject[] => {
  const projectsById = new Map<string, Project>();
  projects.forEach((project) => {
    projectsById.set(project.id, project);
  });

  const threadsWithProjects: ThreadWithProject[] = [];

  for (const thread of threads) {
    const projectId = thread.project_id;
    if (!projectId) continue;

    const project = projectsById.get(projectId);
    if (!project) {
      continue;
    }
    let displayName = project.name || 'Unnamed Project';
    if (thread.metadata?.is_workflow_execution && thread.metadata?.workflow_run_name) {
      displayName = thread.metadata.workflow_run_name;
    }

    threadsWithProjects.push({
      threadId: thread.id || thread.thread_id, // 适配后端返回的字段名
      projectId: projectId,
      projectName: displayName,
      url: `/projects/${projectId}/thread/${thread.id || thread.thread_id}`,
      updatedAt:
        thread.updated_at || project.updated_at || new Date().toISOString(),
    });
  }

  return sortThreads(threadsWithProjects);
};

export const sortThreads = (
  threadsList: ThreadWithProject[],
): ThreadWithProject[] => {
  return [...threadsList].sort((a, b) => {
    return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();
  });
};
