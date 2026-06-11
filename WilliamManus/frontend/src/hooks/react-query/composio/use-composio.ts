'use client';

import { useMutation, useQueryClient, useQuery, useInfiniteQuery } from '@tanstack/react-query';
import { 
  composioApi, 
  type ComposioToolkitsResponse,
  type CompositoCategoriesResponse,
  type CreateComposioProfileRequest,
  type CreateComposioProfileResponse,
  type DetailedComposioToolkitResponse,
  type ComposioToolsResponse,
} from './utils';
import { composioKeys } from './keys';
import { toast } from 'sonner';

export const useComposioCategories = () => {
  // Commented out composio categories API request
  return useQuery({
    queryKey: composioKeys.categories(),
    queryFn: async (): Promise<CompositoCategoriesResponse> => {
      // Return empty response instead of making API call
      return { success: true, categories: [], total: 0 };
      
      // const result = await composioApi.getCategories();
      // return result;
    },
    staleTime: 10 * 60 * 1000,
    retry: 2,
    enabled: false, // Disable the query entirely
  });
};

export const useComposioToolkits = (search?: string, category?: string) => {
  // Commented out composio toolkits API request
  return useQuery({
    queryKey: composioKeys.toolkits(search, category),
    queryFn: async (): Promise<ComposioToolkitsResponse> => {
      // Return empty response instead of making API call
      return { 
        success: true, 
        toolkits: [], 
        total_items: 0, 
        total_pages: 0, 
        current_page: 1, 
        next_cursor: undefined, 
        has_more: false 
      };
      
      // const result = await composioApi.getToolkits(search, category);
      // return result;
    },
    staleTime: 5 * 60 * 1000, 
    retry: 2,
    enabled: false, // Disable the query entirely
  });
};

export const useComposioToolkitsInfinite = (search?: string, category?: string) => {
  // Commented out composio toolkits infinite API request
  return useInfiniteQuery({
    queryKey: ['composio', 'toolkits', 'infinite', search, category],
    queryFn: async ({ pageParam }): Promise<ComposioToolkitsResponse> => {
      // Return empty response instead of making API call
      return { 
        success: true, 
        toolkits: [], 
        total_items: 0, 
        total_pages: 0, 
        current_page: 1, 
        next_cursor: undefined, 
        has_more: false 
      };
      
      // const result = await composioApi.getToolkits(search, category, pageParam);
      // return result;
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => {
      return lastPage.next_cursor || undefined;
    },
    staleTime: 5 * 60 * 1000,
    retry: 2,
    enabled: false, // Disable the query entirely
  });
};

export const useComposioToolkitIcon = (toolkitSlug: string, options?: { enabled?: boolean }) => {
  // Commented out composio toolkit icon API request
  return useQuery({
    queryKey: ['composio', 'toolkit-icon', toolkitSlug],
    queryFn: async (): Promise<{ success: boolean; icon_url?: string }> => {
      // Return empty response instead of making API call
      return { success: true, icon_url: undefined };
      
      // const result = await composioApi.getToolkitIcon(toolkitSlug);
      // return result;
    },
    enabled: false, // Disable the query entirely
    staleTime: 60 * 60 * 1000,
    retry: 2,
  });
};

export const useComposioToolkitDetails = (toolkitSlug: string, options?: { enabled?: boolean }) => {
  // Commented out composio toolkit details API request
  return useQuery({
    queryKey: ['composio', 'toolkit-details', toolkitSlug],
    queryFn: async (): Promise<DetailedComposioToolkitResponse> => {
      // Return empty response instead of making API call
      return { 
        success: true, 
        toolkit: {
          slug: '',
          name: '',
          description: '',
          logo: '',
          tags: [],
          auth_schemes: [],
          categories: [],
          auth_config_details: [],
          base_url: ''
        }
      };
      
      // const result = await composioApi.getToolkitDetails(toolkitSlug);
      // return result;
    },
    enabled: false, // Disable the query entirely
    staleTime: 10 * 60 * 1000,
    retry: 2,
  });
};

export const useComposioTools = (toolkitSlug: string, options?: { enabled?: boolean; limit?: number }) => {
  // Commented out composio tools API request
  return useQuery({
    queryKey: ['composio', 'tools', toolkitSlug, options?.limit],
    queryFn: async (): Promise<ComposioToolsResponse> => {
      // Return empty response instead of making API call
      return { 
        success: true, 
        tools: [], 
        total_items: 0, 
        current_page: 1, 
        total_pages: 0, 
        next_cursor: undefined 
      };
      
      // const result = await composioApi.getTools(toolkitSlug, options?.limit);
      // return result;
    },
    enabled: false, // Disable the query entirely
    staleTime: 10 * 60 * 1000,
    retry: 2,
  });
};

export const useCreateComposioProfile = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: async (request: CreateComposioProfileRequest): Promise<CreateComposioProfileResponse> => {
      return await composioApi.createProfile(request);
    },
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({ queryKey: composioKeys.profiles.all() });
      toast.success(`Connected to ${variables.profile_name}!`);
      
      // If there's a redirect URL, open it automatically
      if (data.redirect_url) {
        window.open(data.redirect_url, '_blank', 'width=600,height=700,resizable=yes,scrollbars=yes');
      }
    },
    onError: (error) => {
      console.error('Failed to create Composio profile:', error);
      toast.error(error.message || 'Failed to create profile');
    },
  });
};

export const useInvalidateComposioQueries = () => {
  const queryClient = useQueryClient();
  
  return () => {
    queryClient.invalidateQueries({ queryKey: composioKeys.all });
  };
}; 