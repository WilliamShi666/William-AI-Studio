import { useQuery } from '@tanstack/react-query';
import { composioApi, type ComposioProfile, type ComposioToolkitGroup, type ComposioMcpUrlResponse } from './utils';
import { composioKeys } from './keys';

export const useComposioProfiles = (params?: { toolkit_slug?: string; is_active?: boolean }) => {
  // Commented out composio profiles API request
  return useQuery({
    queryKey: composioKeys.profiles.list(params),
    queryFn: () => {
      // Return empty array instead of making API call
      return Promise.resolve([]);
      // return composioApi.getProfiles(params);
    },
    staleTime: 5 * 60 * 1000,
    enabled: false, // Disable the query entirely
  });
};

// Hook to get a single profile
export const useComposioProfile = (profileId: string, enabled = true) => {
  // Commented out composio profile API request
  return useQuery({
    queryKey: composioKeys.profiles.detail(profileId),
    queryFn: async () => {
      // Return null instead of making API call
      return null;
      
      // const profiles = await composioApi.getProfiles();
      // return profiles.find(p => p.profile_id === profileId) || null;
    },
    enabled: false, // Disable the query entirely
    staleTime: 5 * 60 * 1000,
  });
};

export const useComposioCredentialsProfiles = () => {
  // Commented out composio credentials profiles API request
  return useQuery({
    queryKey: composioKeys.profiles.all(),
    queryFn: () => {
      // Return empty array instead of making API call
      return Promise.resolve([]);
      // return composioApi.getCredentialsProfiles();
    },
    staleTime: 5 * 60 * 1000,
    enabled: false, // Disable the query entirely
  });
};

export const useComposioMcpUrl = (profileId: string, enabled = false) => {
  // Commented out composio MCP URL API request
  return useQuery({
    queryKey: composioKeys.profiles.mcpConfig(profileId),
    queryFn: () => {
      // Return empty response instead of making API call
      return Promise.resolve({ url: '', success: false });
      // return composioApi.getMcpUrl(profileId);
    },
    enabled: false, // Disable the query entirely
    staleTime: 0,
    gcTime: 0,
  });
}; 