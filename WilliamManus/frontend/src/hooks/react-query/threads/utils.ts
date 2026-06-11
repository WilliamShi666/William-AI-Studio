import { createClient } from "@/lib/supabase/client";

const API_URL = process.env.NEXT_PUBLIC_BACKEND_URL;
const DELETE_THREAD_TIMEOUT_MS = 10000;

export type Thread = {
    thread_id: string;
    account_id: string | null;
    project_id?: string | null;
    is_public?: boolean;
    created_at: string;
    updated_at: string;
    metadata?: {
      workflow_id?: string;
      workflow_name?: string;
      workflow_run_name?: string;
      is_workflow_execution?: boolean;
      agent_id?: string;
      is_agent_builder?: boolean;
      [key: string]: any;
    };
    [key: string]: any;
  };
  
export type Project = {
    id: string;
    name: string;
    description: string;
    account_id: string;
    created_at: string;
    updated_at?: string;
    sandbox: {
      vnc_preview?: string;
      sandbox_url?: string;
      id?: string;
      pass?: string;
    };
    is_public?: boolean;
    [key: string]: any;
  };

type ProjectSandbox = Project['sandbox'];

function parseProjectSandbox(rawSandbox: unknown): ProjectSandbox {
  if (typeof rawSandbox === 'string') {
    try {
      return parseProjectSandbox(JSON.parse(rawSandbox));
    } catch (error) {
      console.warn('Failed to parse project sandbox JSON:', rawSandbox);
      return {
        id: '',
        pass: '',
        vnc_preview: '',
        sandbox_url: '',
      };
    }
  }

  if (!rawSandbox || typeof rawSandbox !== 'object') {
    return {
      id: '',
      pass: '',
      vnc_preview: '',
      sandbox_url: '',
    };
  }

  const sandbox = rawSandbox as Record<string, unknown>;
  return {
    id: typeof sandbox.id === 'string' ? sandbox.id : '',
    pass: typeof sandbox.pass === 'string' ? sandbox.pass : '',
    vnc_preview: typeof sandbox.vnc_preview === 'string' ? sandbox.vnc_preview : '',
    sandbox_url: typeof sandbox.sandbox_url === 'string' ? sandbox.sandbox_url : '',
  };
}

async function ensureProjectSandboxActive(
  projectId: string,
  accessToken?: string | null,
): Promise<Partial<ProjectSandbox> | null> {
  if (!API_URL) {
    return null;
  }

  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (accessToken) {
      headers.Authorization = `Bearer ${accessToken}`;
    }

    const response = await fetch(
      `${API_URL}/project/${projectId}/sandbox/ensure-active`,
      {
        method: 'POST',
        headers,
      },
    );

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.warn(
        `Failed to ensure sandbox is active: ${response.status} ${response.statusText}`,
        errorText,
      );
      return null;
    }

    const payload = await response.json();
    const ensuredSandbox = parseProjectSandbox(payload?.sandbox);
    const resolvedId =
      typeof payload?.resolved_sandbox_id === 'string'
        ? payload.resolved_sandbox_id
        : typeof payload?.sandbox_id === 'string'
          ? payload.sandbox_id
          : ensuredSandbox.id;

    return {
      ...ensuredSandbox,
      id: resolvedId || ensuredSandbox.id || '',
    };
  } catch (sandboxError) {
    console.warn('Failed to ensure sandbox is active:', sandboxError);
    return null;
  }
}
  

  export const getThread = async (threadId: string): Promise<Thread> => {
    try {
      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();

      if (!session?.access_token) {
        throw new Error('No access token available');
      }

      if (!API_URL) {
        throw new Error(
          'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
        );
      }

      const response = await fetch(`${API_URL}/threads/${threadId}`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${session.access_token}`,
        },
        cache: 'no-store',
      });

      if (!response.ok) {
        throw new Error(
          `Error getting thread: ${response.statusText} (${response.status})`,
        );
      }

      const data = await response.json();
      return data;
    } catch (error) {
      throw error;
    }
  };

export const updateThread = async (
    threadId: string,
    data: Partial<Thread>,
  ): Promise<Thread> => {
    const supabase = createClient();
  
    const updateData = { ...data };
  
    // Update the thread
    const { data: updatedThread, error } = await supabase
      .from('threads')
      .update(updateData)
      .eq('thread_id', threadId)
      .select()
      .single();
  
    if (error) {
      console.error('Error updating thread:', error);
      throw new Error(`Error updating thread: ${error.message}`);
    }
  
    return updatedThread;
  };

export const toggleThreadPublicStatus = async (
    threadId: string,
    isPublic: boolean,
  ): Promise<Thread> => {
    return updateThread(threadId, { is_public: isPublic });
};

const deleteSandbox = async (sandboxId: string): Promise<void> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };

    if (session?.access_token) {
      headers['Authorization'] = `Bearer ${session.access_token}`;
    }

    const response = await fetch(`${API_URL}/sandboxes/${sandboxId}`, {
      method: 'DELETE',
      headers,
    });

    if (!response.ok) {
      console.warn('Failed to delete sandbox, continuing with thread deletion');
    }
  } catch (error) {
    console.warn('Error deleting sandbox, continuing with thread deletion:', error);
  }
};

export const deleteThread = async (threadId: string, sandboxId?: string): Promise<void> => {
  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  const abortController = new AbortController();

  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new Error('No access token available');
    }

    if (!API_URL) {
      throw new Error('Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.');
    }

    // Call backend API to delete thread
    // delete_sandbox=true if sandboxId is provided
    const deleteSandboxParam = !!sandboxId;
    const url = `${API_URL}/threads/${threadId}?delete_sandbox=${deleteSandboxParam}`;
    timeoutHandle = setTimeout(() => {
      abortController.abort();
    }, DELETE_THREAD_TIMEOUT_MS);

    const response = await fetch(url, {
      method: 'DELETE',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      signal: abortController.signal,
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(
        errorData.detail || `Error deleting thread: ${response.statusText} (${response.status})`,
      );
    }

    const result = await response.json();
    console.log('Thread deleted successfully:', result);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('Thread deletion request timed out. Cleanup continues in background.');
    }
    console.error('Error deleting thread:', error);
    throw error;
  } finally {
    if (timeoutHandle) {
      clearTimeout(timeoutHandle);
    }
  }
};
  

export const getPublicProjects = async (): Promise<Project[]> => {
    try {
      const supabase = createClient();
  
      // Query for threads that are marked as public
      const { data: publicThreads, error: threadsError } = await supabase
        .from('threads')
        .select('project_id')
        .eq('is_public', true);
  
      if (threadsError) {
        console.error('Error fetching public threads:', threadsError);
        return [];
      }
  
      // If no public threads found, return empty array
      if (!publicThreads?.length) {
        return [];
      }
  
      // Extract unique project IDs from public threads
      const publicProjectIds = [
        ...new Set(publicThreads.map((thread) => thread.project_id)),
      ].filter(Boolean);
  
      // If no valid project IDs, return empty array
      if (!publicProjectIds.length) {
        return [];
      }
  
      // Get the projects that have public threads
      const { data: projects, error: projectsError } = await supabase
        .from('projects')
        .select('*')
        .in('project_id', publicProjectIds);
  
      if (projectsError) {
        console.error('Error fetching public projects:', projectsError);
        return [];
      }
  
      // Map database fields to our Project type
      const mappedProjects: Project[] = (projects || []).map((project) => ({
        id: project.project_id,
        name: project.name || '',
        description: project.description || '',
        account_id: project.account_id,
        created_at: project.created_at,
        updated_at: project.updated_at,
        sandbox: project.sandbox || {
          id: '',
          pass: '',
          vnc_preview: '',
          sandbox_url: '',
        },
        is_public: true, // Mark these as public projects
      }));
  
      return mappedProjects;
    } catch (err) {
      console.error('Error fetching public projects:', err);
      return [];
    }
  };



  export const getProject = async (projectId: string): Promise<Project> => {
    try {
      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();

      if (!session?.access_token) {
        throw new Error('No access token available');
      }

      if (!API_URL) {
        throw new Error(
          'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
        );
      }

      const response = await fetch(`${API_URL}/projects/${projectId}`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${session.access_token}`,
        },
        cache: 'no-store',
      });

      if (!response.ok) {
        if (response.status === 404) {
          throw new Error(`Project not found or not accessible: ${projectId}`);
        }
        throw new Error(
          `Error getting project: ${response.statusText} (${response.status})`,
        );
      }

      const data = await response.json();
      const initialSandbox = parseProjectSandbox(data.sandbox);
      const ensuredSandbox = initialSandbox.id
        ? await ensureProjectSandboxActive(projectId, session?.access_token)
        : null;
      const mergedSandbox = {
        ...initialSandbox,
        ...(ensuredSandbox || {}),
      };
      
      // Map to Project type
      const mappedProject: Project = {
        id: data.project_id || data.id,
        name: data.name || '',
        description: data.description || '',
        account_id: data.account_id,
        is_public: data.is_public || false,
        created_at: data.created_at,
        updated_at: data.updated_at,
        sandbox: mergedSandbox,
      };

      return mappedProject;
    } catch (error) {
      console.error(`Error fetching project ${projectId}:`, error);
      throw error;
    }
  };


  export const updateProject = async (
    projectId: string,
    data: Partial<Project>,
  ): Promise<Project> => {
    const supabase = createClient();
    // Sanity check to avoid update errors
    if (!projectId || projectId === '') {
      console.error('Attempted to update project with invalid ID:', projectId);
      throw new Error('Cannot update project: Invalid project ID');
    }
  
    const { data: updatedData, error } = await supabase
      .from('projects')
      .update(data)
      .eq('project_id', projectId)
      .select()
      .single();
  
    if (error) {
      console.error('Error updating project:', error);
      throw error;
    }
  
    if (!updatedData) {
      throw new Error('No data returned from update');
    }
  
    // Dispatch a custom event to notify components about the project change
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('project-updated', {
          detail: {
            projectId,
            updatedData: {
              id: updatedData.project_id,
              name: updatedData.name,
              description: updatedData.description,
            },
          },
        }),
      );
    }
  
    // Return formatted project data - use same mapping as getProject
    return {
      id: updatedData.project_id,
      name: updatedData.name,
      description: updatedData.description || '',
      account_id: updatedData.account_id,
      created_at: updatedData.created_at,
      sandbox: updatedData.sandbox || {
        id: '',
        pass: '',
        vnc_preview: '',
        sandbox_url: '',
      },
    };
  };
