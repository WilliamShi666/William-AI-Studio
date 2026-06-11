import React from 'react';
import type { Project } from '@/lib/api';
import { Message as BaseApiMessageType } from '@/lib/api';

// Define a type for the params to make React.use() work properly
export type ThreadParams = {
  threadId: string;
  projectId: string;
};

// Unified Message Interface matching the backend/database schema
export interface UnifiedMessage {
  sequence?: number;
  message_id: string | null; // Can be null for transient stream events (chunks, unsaved statuses)
  thread_id: string;
  project_id?: string; // Add project_id field from real data
  type: 'user' | 'assistant' | 'tool' | 'system' | 'status' | 'browser_state'; // Add 'system' if used
  role?: string; // Add role field from real data  
  is_llm_message: boolean;
  content: string; // ALWAYS a JSON string from the backend
  metadata: string; // ALWAYS a JSON string from the backend
  created_at: string; // ISO timestamp string
  updated_at: string; // ISO timestamp string
  agent_id?: string | null; // ID of the agent associated with this message
  agent_version_id?: string | null; // Version of the agent
  agents?: {
    name: string;
    profile_image_url?: string;
  }; // Agent information from join
}

// Helper type for parsed content - structure depends on message.type
export interface ParsedContent {
  role?: 'user' | 'assistant' | 'tool' | 'system'; // From the JSON string in content
  content?: any; // Can be string, object, etc. after parsing
  reasoning_content?: string; // Gemini/Anthropic 思考/推理内容
  tool_calls?: any[]; // For native tool calls
  tool_call_id?: string; // For tool results
  name?: string; // For tool results
  status_type?: string; // For status messages
  [key: string]: any; // Allow other properties
}

// Helper type for parsed metadata
export interface ParsedMetadata {
  stream_status?:
    | 'chunk'
    | 'complete'
    | 'reasoning_chunk'
    | 'tool_call_chunk'
    | 'tool_result_chunk'
    | 'shadow_clone_planning_started'
    | 'shadow_clone_environment_preparing'
    | 'shadow_clone_environment_ready'
    | 'shadow_clone_environment_recovering'
    | 'shadow_clone_proposed'
    | 'subagent_started'
    | 'subagent_completed'
    | 'subagent_failed'
    | 'subagent_activity'
    | 'heartbeat'
    | 'shadow_clone_aggregating'
    | 'shadow_clone_complete'
    | 'shadow_clone_v2_started'
    | 'shadow_clone_v2_plan_created'
    | 'shadow_clone_v2_execution_completed'
    | 'shadow_clone_v2_execution_failed';
  shadow_clone_phase?: 'planning' | 'execution' | 'aggregate';
  live_activity?: {
    scope?: 'shadow_clone_main' | 'main_agent';
    phase?: 'planning' | 'confirming' | 'execution' | 'aggregate' | 'completed';
    reason?: string | null;
    subtask_id?: string | null;
    epoch?: number;
    updated_at?: string | null;
  };
  thread_run_id?: string;
  tool_index?: number;
  assistant_message_id?: string; // Link tool results/statuses back
  linked_tool_result_message_id?: string; // Link status to tool result
  parsing_details?: any;
  tool_calls?: any[]; // Tool calls from streaming
  [key: string]: any; // Allow other properties
}

// Extend the base Message type with the expected database fields
export interface ApiMessageType extends Omit<BaseApiMessageType, 'type'> {
  message_id?: string;
  thread_id?: string;
  is_llm_message?: boolean;
  metadata?: string;
  created_at?: string;
  updated_at?: string;
  // Allow 'type' to be potentially wider than the base type
  type?: string;
}

// Re-export existing types
export type { Project };
