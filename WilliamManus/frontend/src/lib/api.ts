import { createClient } from '@/lib/supabase/client';
import { handleApiError } from './error-handler';
import posthog from 'posthog-js';

// Get backend URL from environment variables (may be empty in some deployments)
const RAW_API_URL = process.env.NEXT_PUBLIC_BACKEND_URL || '';

/**
 * Ensure we always have an absolute API base URL.
 * Falls back to the current origin with `/api` when the env var is missing.
 */
export function getApiBaseUrl(): string {
  if (RAW_API_URL) return RAW_API_URL.replace(/\/$/, '');

  if (typeof window !== 'undefined') {
    return `${window.location.origin}/api`;
  }

  // SSR fallback; Next will rewrite to the backend or the same origin
  return '/api';
}

export function buildApiUrl(path: string): URL {
  const base = getApiBaseUrl();
  // Ensure there is exactly one slash between base and path
  const normalizedBase = base.endsWith('/') ? base : `${base}/`;
  const normalizedPath = path.startsWith('/') ? path.slice(1) : path;
  if (normalizedBase.startsWith('/')) {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost';
    return new URL(`${normalizedBase}${normalizedPath}`, origin);
  }
  return new URL(`${normalizedBase}${normalizedPath}`);
}

// Default API URL used across this module
const API_URL = getApiBaseUrl();

// Set to keep track of agent runs that are known to be non-running.
// Size-bounded to prevent unbounded memory growth over long sessions.
const MAX_NON_RUNNING_CACHE = 200;
const nonRunningAgentRuns = new Set<string>();

function addToNonRunning(agentRunId: string) {
  nonRunningAgentRuns.add(agentRunId);
  if (nonRunningAgentRuns.size > MAX_NON_RUNNING_CACHE) {
    // Prune oldest entries (approximate LRU via Set iteration order)
    const entries = nonRunningAgentRuns.values();
    let toRemove = nonRunningAgentRuns.size - MAX_NON_RUNNING_CACHE;
    while (toRemove > 0) {
      const first = entries.next();
      if (first.done) break;
      nonRunningAgentRuns.delete(first.value);
      toRemove--;
    }
  }
}

interface StreamAgentCallbacks {
  onMessage: (content: string) => void;
  onError: (error: Error | string) => void;
  onClose: () => void;
  onEventIndex?: (index: number) => void;
  onEventCursor?: (cursor: string) => void;
}

// Map to keep track of active EventSource streams per run.
const activeStreams = new Map<string, EventSource>();

// Custom error for billing issues
export class BillingError extends Error {
  status: number;
  detail: { message: string; [key: string]: any }; // Allow other properties in detail

  constructor(
    status: number,
    detail: { message: string; [key: string]: any },
    message?: string,
  ) {
    super(message || detail.message || `Billing Error: ${status}`);
    this.name = 'BillingError';
    this.status = status;
    this.detail = detail;

    // Set the prototype explicitly.
    Object.setPrototypeOf(this, BillingError.prototype);
  }
}

// Custom error for agent run limit exceeded
export class AgentRunLimitError extends Error {
  status: number;
  detail: { 
    message: string;
    running_thread_ids: string[];
    running_count: number;
  };

  constructor(
    status: number,
    detail: { 
      message: string;
      running_thread_ids: string[];
      running_count: number;
      [key: string]: any;
    },
    message?: string,
  ) {
    super(message || detail.message || `Agent Run Limit Exceeded: ${status}`);
    this.name = 'AgentRunLimitError';
    this.status = status;
    this.detail = detail;

    // Set the prototype explicitly.
    Object.setPrototypeOf(this, AgentRunLimitError.prototype);
  }
}

export class AgentCountLimitError extends Error {
  status: number;
  detail: { 
    message: string;
    current_count: number;
    limit: number;
    tier_name: string;
    error_code: string;
  };

  constructor(
    status: number,
    detail: { 
      message: string;
      current_count: number;
      limit: number;
      tier_name: string;
      error_code: string;
      [key: string]: any;
    },
    message?: string,
  ) {
    super(message || detail.message || `Agent Count Limit Exceeded: ${status}`);
    this.name = 'AgentCountLimitError';
    this.status = status;
    this.detail = detail;
    Object.setPrototypeOf(this, AgentCountLimitError.prototype);
  }
}

export class NoAccessTokenAvailableError extends Error {
  constructor(message?: string, options?: { cause?: Error }) {
    super(message || 'No access token available', options);
  }
  name = 'NoAccessTokenAvailableError';
}

export class ShadowCloneAdminAccessError extends Error {
  status: number;

  constructor(status: number, message?: string) {
    super(message || 'Shadow Clone admin access denied');
    this.name = 'ShadowCloneAdminAccessError';
    this.status = status;
    Object.setPrototypeOf(this, ShadowCloneAdminAccessError.prototype);
  }
}

export class ShadowCloneAdminEndpointNotFoundError extends Error {
  constructor(message?: string) {
    super(message || 'Shadow Clone admin config endpoint not found');
    this.name = 'ShadowCloneAdminEndpointNotFoundError';
    Object.setPrototypeOf(this, ShadowCloneAdminEndpointNotFoundError.prototype);
  }
}

export interface ShadowCloneModelOverrides {
  shadow_clone_main_model?: string;
  shadow_clone_subagent_model?: string;
}

export interface ShadowCloneAdminModelConfig {
  main_model_name: string | null;
  subagent_model_name: string | null;
}

const SHADOW_CLONE_ADMIN_MODEL_CONFIG_PATH = '/admin/shadow-clone-model-config';

async function getAccessTokenOrThrow(): Promise<string> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    throw new NoAccessTokenAvailableError();
  }

  return session.access_token;
}

function normalizeShadowCloneAdminModelConfig(
  payload: any,
): ShadowCloneAdminModelConfig | null {
  if (!payload || typeof payload !== 'object') {
    return null;
  }

  const mainModelName =
    typeof payload?.main_model_name === 'string'
      ? payload.main_model_name
      : typeof payload?.shadow_clone_main_model === 'string'
        ? payload.shadow_clone_main_model
        : null;
  const subagentModelName =
    typeof payload?.subagent_model_name === 'string'
      ? payload.subagent_model_name
      : typeof payload?.shadow_clone_subagent_model === 'string'
        ? payload.shadow_clone_subagent_model
        : null;

  return {
    main_model_name: mainModelName,
    subagent_model_name: subagentModelName,
  };
}

export const getShadowCloneAdminModelConfig = async (): Promise<ShadowCloneAdminModelConfig> => {
  const accessToken = await getAccessTokenOrThrow();
  const response = await fetch(`${API_URL}${SHADOW_CLONE_ADMIN_MODEL_CONFIG_PATH}`, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
    cache: 'no-store',
  });

  if (response.status === 401 || response.status === 403) {
    throw new ShadowCloneAdminAccessError(response.status);
  }

  if (response.status === 404) {
    throw new ShadowCloneAdminEndpointNotFoundError();
  }

  if (!response.ok) {
    throw new Error(
      `Failed to load Shadow Clone admin config: ${response.status} ${response.statusText}`,
    );
  }

  const payload = await response.json().catch(() => null);
  const config = normalizeShadowCloneAdminModelConfig(payload);
  if (!config) {
    throw new Error('Shadow Clone admin config response is invalid');
  }
  return config;
};

export const updateShadowCloneAdminModelConfig = async (
  config: {
    main_model_name: string;
    subagent_model_name: string;
  },
): Promise<ShadowCloneAdminModelConfig> => {
  const accessToken = await getAccessTokenOrThrow();
  const response = await fetch(`${API_URL}${SHADOW_CLONE_ADMIN_MODEL_CONFIG_PATH}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(config),
  });

  if (response.status === 401 || response.status === 403) {
    throw new ShadowCloneAdminAccessError(response.status);
  }

  if (response.status === 404) {
    throw new ShadowCloneAdminEndpointNotFoundError();
  }

  if (!response.ok) {
    throw new Error(
      `Failed to update Shadow Clone admin config: ${response.status} ${response.statusText}`,
    );
  }

  if (response.status === 204) {
    return config;
  }

  const payload = await response.json().catch(() => null);
  return normalizeShadowCloneAdminModelConfig(payload) || config;
};

// Type Definitions (moved from potential separate file for clarity)
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
  file_delivery_source?: ProjectFileDeliverySource;
  is_public?: boolean; // Flag to indicate if the project is public
  [key: string]: any; // Allow additional properties to handle database fields
};

type ProjectSandbox = Project['sandbox'];

export const DEFAULT_FILE_DELIVERY_ROOT_PATH = '/workspace';

export interface RunFileDeliverySource {
  agentRunId: string;
  browseSandboxId: string | null;
  archiveSandboxId: string | null;
  browseRootPath: string;
  archiveRootPath: string;
  identitySource: string;
  bindingState?: string | null;
  environmentStatus?: string | null;
  environmentReady?: boolean | null;
  executionEpoch?: number | null;
  manifestSandboxId?: string | null;
  terminalStatus?: string | null;
}

function normalizeOptionalString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmedValue = value.trim();
  return trimmedValue ? trimmedValue : null;
}

function normalizeOptionalBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function normalizeOptionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function normalizeAgentRunMetadata(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== 'string' || !value.trim()) {
    return {};
  }
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

export function normalizeFileDeliveryRootPath(path?: string | null): string {
  const normalizedPath = normalizeOptionalString(path);
  if (!normalizedPath) return DEFAULT_FILE_DELIVERY_ROOT_PATH;

  const prefixedPath = normalizedPath.startsWith('/')
    ? normalizedPath
    : `/${normalizedPath}`;

  if (prefixedPath === '/') return prefixedPath;

  return prefixedPath.replace(/\/+$/, '') || '/';
}

export function normalizeFileDeliveryPath(
  path: unknown,
  rootPath?: string | null,
): string {
  const normalizedRootPath = normalizeFileDeliveryRootPath(rootPath);
  const normalizedPath = normalizeOptionalString(path);

  if (!normalizedPath) {
    return normalizedRootPath;
  }

  const joinWithRoot = (basePath: string, relativePath: string): string => {
    const trimmedRelativePath = relativePath.replace(/^\/+/, '');
    if (!trimmedRelativePath) {
      return basePath;
    }

    if (basePath === '/') {
      return `/${trimmedRelativePath}`;
    }

    return `${basePath}/${trimmedRelativePath}`;
  };

  if (normalizedPath.startsWith('/')) {
    const normalizedAbsolutePath = normalizeFileDeliveryRootPath(normalizedPath);

    if (
      normalizedAbsolutePath === normalizedRootPath ||
      normalizedAbsolutePath.startsWith(`${normalizedRootPath}/`)
    ) {
      return normalizedAbsolutePath;
    }

    const shouldRebaseDefaultWorkspacePath =
      normalizedRootPath !== DEFAULT_FILE_DELIVERY_ROOT_PATH &&
      normalizedRootPath.startsWith(`${DEFAULT_FILE_DELIVERY_ROOT_PATH}/`) &&
      (normalizedAbsolutePath === DEFAULT_FILE_DELIVERY_ROOT_PATH ||
        normalizedAbsolutePath.startsWith(
          `${DEFAULT_FILE_DELIVERY_ROOT_PATH}/`,
        ));

    if (shouldRebaseDefaultWorkspacePath) {
      const relativeWorkspacePath = normalizedAbsolutePath
        .slice(DEFAULT_FILE_DELIVERY_ROOT_PATH.length)
        .replace(/^\/+/, '');

      return joinWithRoot(normalizedRootPath, relativeWorkspacePath);
    }

    return normalizedAbsolutePath;
  }

  if (
    normalizedPath === normalizedRootPath ||
    normalizedPath.startsWith(`${normalizedRootPath}/`)
  ) {
    return normalizedPath;
  }

  return joinWithRoot(normalizedRootPath, normalizedPath);
}

function normalizeRunFileDeliverySource(
  rawSource: unknown,
  fallbackAgentRunId?: string | null,
): RunFileDeliverySource | null {
  if (!rawSource || typeof rawSource !== 'object') {
    return null;
  }

  const source = rawSource as Record<string, unknown>;
  const browseSandboxId = normalizeOptionalString(
    source.browseSandboxId ?? source.browse_sandbox_id,
  );
  const archiveSandboxId = normalizeOptionalString(
    source.archiveSandboxId ?? source.archive_sandbox_id,
  );
  const identitySource =
    normalizeOptionalString(source.identitySource ?? source.identity_source) ||
    'unknown';
  const agentRunId =
    normalizeOptionalString(source.agentRunId ?? source.agent_run_id) ||
    normalizeOptionalString(fallbackAgentRunId) ||
    '';

  return {
    agentRunId,
    browseSandboxId,
    archiveSandboxId,
    browseRootPath: normalizeFileDeliveryRootPath(
      normalizeOptionalString(source.browseRootPath ?? source.browse_root_path),
    ),
    archiveRootPath: normalizeFileDeliveryRootPath(
      normalizeOptionalString(source.archiveRootPath ?? source.archive_root_path),
    ),
    identitySource,
    bindingState: normalizeOptionalString(
      source.bindingState ?? source.binding_state,
    ),
    environmentStatus: normalizeOptionalString(
      source.environmentStatus ?? source.environment_status,
    ),
    environmentReady: normalizeOptionalBoolean(
      source.environmentReady ?? source.environment_ready,
    ),
    executionEpoch: normalizeOptionalNumber(
      source.executionEpoch ?? source.execution_epoch,
    ),
    manifestSandboxId: normalizeOptionalString(
      source.manifestSandboxId ?? source.manifest_sandbox_id,
    ),
    terminalStatus: normalizeOptionalString(
      source.terminalStatus ?? source.terminal_status,
    ),
  };
}

export interface ProjectFileDeliverySource {
  agentRunId?: string | null;
  browseSandboxId: string | null;
  archiveSandboxId: string | null;
  browseRootPath: string;
  archiveRootPath: string;
  identitySource: string;
  archiveSelection:
    | 'thread_workspace'
    | 'run_file_delivery_source'
    | 'project_sandbox'
    | 'current_sandbox_fallback'
    | 'unavailable';
  downloadMode: 'server_archive';
}

export function resolveThreadWorkspaceFileDeliverySource(
  threadId: string,
): ProjectFileDeliverySource {
  const normalizedThreadId = normalizeOptionalString(threadId) || '';
  const syntheticId = normalizedThreadId
    ? `thread-workspace:${normalizedThreadId}`
    : null;
  return {
    agentRunId: 'thread-workspace',
    browseSandboxId: syntheticId,
    archiveSandboxId: syntheticId,
    browseRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
    archiveRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
    identitySource: syntheticId ? 'thread_workspace_artifacts' : 'unavailable',
    archiveSelection: syntheticId ? 'thread_workspace' : 'unavailable',
    downloadMode: 'server_archive',
  };
}

export function resolveProjectFileDeliverySource(
  project?: Pick<Project, 'sandbox'> | null,
  currentSandboxId?: string | null,
): ProjectFileDeliverySource {
  const normalizedCurrentSandboxId =
    typeof currentSandboxId === 'string' && currentSandboxId.trim()
      ? currentSandboxId.trim()
      : null;
  const normalizedProjectSandboxId =
    typeof project?.sandbox?.id === 'string' && project.sandbox.id.trim()
      ? project.sandbox.id.trim()
      : null;

  if (normalizedProjectSandboxId) {
    return {
      browseSandboxId: normalizedCurrentSandboxId ?? normalizedProjectSandboxId,
      archiveSandboxId: normalizedProjectSandboxId,
      browseRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
      archiveRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
      identitySource: 'project_sandbox',
      archiveSelection: 'project_sandbox',
      downloadMode: 'server_archive',
    };
  }

  if (normalizedCurrentSandboxId) {
    return {
      browseSandboxId: normalizedCurrentSandboxId,
      archiveSandboxId: normalizedCurrentSandboxId,
      browseRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
      archiveRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
      identitySource: 'current_sandbox_fallback',
      archiveSelection: 'current_sandbox_fallback',
      downloadMode: 'server_archive',
    };
  }

  return {
    browseSandboxId: null,
    archiveSandboxId: null,
    browseRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
    archiveRootPath: DEFAULT_FILE_DELIVERY_ROOT_PATH,
    identitySource: 'unavailable',
    archiveSelection: 'unavailable',
    downloadMode: 'server_archive',
  };
}

export function resolveFileDeliverySource(
  runSource?: RunFileDeliverySource | null,
  project?: Pick<Project, 'sandbox'> | null,
  currentSandboxId?: string | null,
): ProjectFileDeliverySource {
  if (runSource?.browseSandboxId || runSource?.archiveSandboxId) {
    return {
      agentRunId: runSource.agentRunId || null,
      browseSandboxId: runSource.browseSandboxId,
      archiveSandboxId: runSource.archiveSandboxId,
      browseRootPath: normalizeFileDeliveryRootPath(runSource.browseRootPath),
      archiveRootPath: normalizeFileDeliveryRootPath(runSource.archiveRootPath),
      identitySource: runSource.identitySource || 'run_file_delivery_source',
      archiveSelection: 'run_file_delivery_source',
      downloadMode: 'server_archive',
    };
  }

  return resolveProjectFileDeliverySource(project, currentSandboxId);
}

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

export type Thread = {
  thread_id?: string; // 原有格式
  id?: string; // 新后端格式
  account_id: string | null;
  project_id?: string | null;
  name?: string | null; // 后端返回的name字段
  status?: string; // 后端返回的status字段
  metadata?: any; // 元数据
  is_public?: boolean;
  created_at: string;
  updated_at: string;
  [key: string]: any; // Allow additional properties to handle database fields
};

export type Message = {
  role: string;
  content: string;
  type: string;
  message_id?: string;
  thread_id?: string;
  is_llm_message?: boolean;
  metadata?: string;
  created_at?: string;
  updated_at?: string;
  agent_id?: string;
  agent_version_id?: string;
  agents?: {
    name: string;
    avatar?: string;
    avatar_color?: string;
    profile_image_url?: string;
  };
  author?: string;
  event_id?: string;
};

export type AgentRun = {
  id: string;
  thread_id: string;
  status: 'running' | 'completed' | 'stopped' | 'error';
  started_at: string;
  completed_at: string | null;
  responses: Message[];
  error: string | null;
  metadata?: Record<string, unknown>;
  file_delivery_source?: RunFileDeliverySource | null;
};

export type ToolCall = {
  name: string;
  arguments: Record<string, unknown>;
};

export type ShadowCloneMode = 'off' | 'on' | 'auto';

export interface ShadowCloneStatusSubagent {
  status:
    | 'pending'
    | 'running'
    | 'completed'
    | 'failed'
    | 'cancelled'
    | 'timeout'
    | 'denied';
  role?: string;
  result_summary?: string;
  started_at?: string;
  finished_at?: string;
  created_at?: string;
  attempt_index?: number;
  failure_class?: string | null;
  last_error?: string | null;
  recovery?: {
    mode?: string | null;
    phase?: string | null;
    reason?: string | null;
    wake_attempts?: number;
    replacement_attempts?: number;
    replacement_context_id?: string | null;
    handoff_summary?: string | null;
    updated_at?: string | null;
  };
}

export interface ShadowCloneStatusProposalSubtask {
  id: string;
  role?: string;
  task_description?: string;
}

export interface ShadowCloneStatusDependency {
  from_id: string;
  to_id: string;
}

export interface ShadowCloneRecoverySubtask {
  subtask_id: string;
  role?: string | null;
  error?: string | null;
  failure_class?: string | null;
  attempt_index?: number;
}

export interface ShadowCloneRecoveryStatus {
  pending?: boolean;
  kind?: string | null;
  message?: string | null;
  requested_at?: string | null;
  scope?: string | null;
  failed_subtasks?: ShadowCloneRecoverySubtask[];
  retryable_subtasks?: string[];
  decision?: string | null;
  decision_at?: string | null;
}

export interface ShadowCloneLiveActivity {
  scope?: 'shadow_clone_main' | 'main_agent';
  phase?: 'planning' | 'confirming' | 'execution' | 'aggregate' | 'completed';
  reason?: string | null;
  subtask_id?: string | null;
  epoch?: number;
  updated_at?: string | null;
}

export type ShadowCloneActivityOwner =
  | 'shadow_clone'
  | 'main_agent'
  | 'none';

export type ShadowCloneUiPhase =
  | 'planning'
  | 'preparing_environment'
  | 'confirming'
  | 'subagents_running'
  | 'recovering'
  | 'main_agent_continuation'
  | 'aggregating'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timeout'
  | 'denied';

export interface ShadowCloneStatusResponse {
  status:
    | 'pending'
    | 'confirming'
    | 'running'
    | 'aggregating'
    | 'completed'
    | 'failed'
    | 'cancelled'
    | 'denied'
    | 'timeout'
    | 'not_found';
  mode?: ShadowCloneMode;
  total?: number;
  completed?: number;
  failed?: number;
  running?: number;
  updated_at?: string;
  sandbox?: {
    id?: string;
    type?: string;
    binding_state?: string;
    environment_status?: string;
    environment_ready?: boolean;
  };
  environment?: {
    status?: 'pending' | 'preparing' | 'ready' | 'recovering' | 'failed';
    ready?: boolean;
    last_error?: string | null;
    prepared_at?: string | null;
    manifest?: Record<string, unknown>;
  };
  proposal?: {
    subtasks?: ShadowCloneStatusProposalSubtask[];
    dependencies?: ShadowCloneStatusDependency[];
  };
  recovery?: ShadowCloneRecoveryStatus;
  activity_owner?: ShadowCloneActivityOwner;
  ui_phase?: ShadowCloneUiPhase;
  phase_reason?: string | null;
  live_activity?: ShadowCloneLiveActivity;
  subagents?: Record<string, ShadowCloneStatusSubagent>;
}

export interface ShadowCloneResultSummary {
  subtask_id: string;
  role: string;
  status:
    | 'pending'
    | 'running'
    | 'completed'
    | 'failed'
    | 'cancelled'
    | 'timeout'
    | 'denied';
  summary: string;
  submitted_at: string;
  attempt_index?: number;
  failure_class?: string | null;
}

export interface ShadowCloneResultsResponse {
  results: ShadowCloneResultSummary[];
  count: number;
}

export interface ShadowCloneFullResultResponse {
  subtask_id: string;
  result: string;
}

export interface InitiateAgentResponse {
  thread_id: string;
  agent_run_id: string;
}

export interface PreparedAttachment {
  attachment_id: string;
  name: string;
  original_name?: string | null;
  path: string;
  size: number;
  content_type?: string | null;
  kind?: string | null;
  mime_type?: string | null;
  filename?: string | null;
  sha256?: string | null;
}

export interface ImageMediaRef {
  kind: 'image';
  path: string;
  mime_type: string;
  filename: string;
  sha256?: string;
}

export interface PrepareAttachmentsResponse {
  project_id: string;
  attachments: PreparedAttachment[];
}

export interface HealthCheckResponse {
  status: string;
  timestamp: string;
  instance_id: string;
}

export interface FileInfo {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  mod_time: string;
  permissions?: string;
}

export interface SandboxFileListDiagnostics {
  identitySource?: string;
  sandboxAvailable?: boolean | null;
  resolvedSandboxId?: string | null;
}

export interface SandboxFileListResponse {
  files: FileInfo[];
  diagnostics?: SandboxFileListDiagnostics;
}

export interface SandboxArchiveDownloadRequest {
  root_path?: string;
  paths?: string[];
  include_hidden?: boolean;
  continue_on_error?: boolean;
}

export interface SandboxArchiveSummary {
  total: number;
  succeeded: number;
  failed: number;
}

export interface SandboxArchiveDownloadResult {
  blob: Blob;
  filename: string;
  summary: SandboxArchiveSummary;
  operationId: string;
  requestId: string | null;
  responseSource: string | null;
  identitySource: string | null;
  sandboxAvailable: boolean | null;
  outcome: string | null;
  fallback: boolean | null;
}

export type SandboxArchiveFailureKind =
  | 'authorization'
  | 'source_identity'
  | 'empty_artifact_set'
  | 'not_found'
  | 'server'
  | 'unknown';

export class SandboxArchiveDownloadError extends Error {
  status: number;
  detail: string;
  sandboxId: string;
  kind: SandboxArchiveFailureKind;
  operationId: string;
  requestId: string | null;
  responseSource: string | null;
  identitySource: string | null;
  sandboxAvailable: boolean | null;
  outcome: string | null;
  fallback: boolean | null;

  constructor(
    sandboxId: string,
    status: number,
    detail: string,
    kind: SandboxArchiveFailureKind,
    operationId: string,
    requestId: string | null,
    responseSource: string | null,
    identitySource: string | null,
    sandboxAvailable: boolean | null,
    outcome: string | null,
    fallback: boolean | null,
    message?: string,
  ) {
    super(message || detail || `Sandbox archive download failed (${status})`);
    this.name = 'SandboxArchiveDownloadError';
    this.status = status;
    this.detail = detail;
    this.sandboxId = sandboxId;
    this.kind = kind;
    this.operationId = operationId;
    this.requestId = requestId;
    this.responseSource = responseSource;
    this.identitySource = identitySource;
    this.sandboxAvailable = sandboxAvailable;
    this.outcome = outcome;
    this.fallback = fallback;
    Object.setPrototypeOf(this, SandboxArchiveDownloadError.prototype);
  }
}

export type FileActionKind = 'list' | 'read' | 'download' | 'archive';
export type FileActionTransferPhase =
  | 'request_dispatched'
  | 'response_ok'
  | 'blob_ready';

export const FILE_ACTION_OPERATION_ID_HEADER = 'X-Client-Operation-Id';

export interface FileActionPhaseEvent {
  phase: FileActionTransferPhase;
  operationId: string;
  status?: number;
  requestId?: string | null;
  responseSource?: string | null;
  identitySource?: string | null;
  sandboxAvailable?: boolean | null;
  outcome?: string | null;
  fallback?: boolean | null;
}

export interface FileActionResponseMetadata {
  requestId: string | null;
  responseSource: string | null;
  identitySource: string | null;
  sandboxAvailable: boolean | null;
  outcome: string | null;
  fallback: boolean | null;
}

interface FileActionRequestOptions {
  accessToken?: string | null;
  operationId?: string;
  onPhase?: (event: FileActionPhaseEvent) => void;
}

export interface SandboxFileContentResult {
  data: string | Blob | unknown;
  filename: string;
  contentType: string | null;
  operationId: string;
  requestId: string | null;
  responseSource: string | null;
  fallback: boolean | null;
}

export interface SandboxFileDownloadResult {
  blob: Blob;
  filename: string;
  contentType: string;
  operationId: string;
  requestId: string | null;
  responseSource: string | null;
  fallback: boolean | null;
}

export type WorkflowExecution = {
  id: string;
  workflow_id: string;
  workflow_name: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  started_at: string | null;
  completed_at: string | null;
  result: any;
  error: string | null;
};

export type WorkflowExecutionLog = {
  id: string;
  execution_id: string;
  node_id: string;
  node_name: string;
  node_type: string;
  started_at: string;
  completed_at: string | null;
  status: 'running' | 'completed' | 'failed';
  input_data: any;
  output_data: any;
  error: string | null;
};

// Workflow Types
export type Workflow = {
  id: string;
  name: string;
  description: string;
  status: 'draft' | 'active' | 'paused' | 'disabled' | 'archived';
  project_id: string;
  account_id: string;
  definition: {
    name: string;
    description: string;
    nodes: any[];
    edges: any[];
    variables?: Record<string, any>;
  };
  created_at: string;
  updated_at: string;
};

export type WorkflowNode = {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: any;
};

export type WorkflowEdge = {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
};

// Project APIs
export const getProjects = async (): Promise<Project[]> => {
  try {
    const supabase = createClient();

    // Get the current user's ID to filter projects
    const { data: userData, error: userError } = await supabase.auth.getUser();
    if (userError) {
      console.error('Error getting current user:', userError);
      return [];
    }

    // If no user is logged in, return an empty array
    if (!userData.user) {
      return [];
    }

    // Query only projects where account_id matches the current user's ID
    const { data, error } = await supabase
      .from('projects')
      .select('*')
      .eq('account_id', userData.user.id);

    if (error) {
      // Handle permission errors specifically
      if (
        error.code === '42501' &&
        error.message.includes('has_role_on_account')
      ) {
        console.error(
          'Permission error: User does not have proper account access',
        );
        return []; // Return empty array instead of throwing
      }
      throw error;
    }

    // Map database fields to our Project type
    const mappedProjects: Project[] = (data || []).map((project) => ({
      id: project.project_id,
      name: project.name || '',
      description: project.description || '',
      account_id: project.account_id,
      created_at: project.created_at,
      updated_at: project.updated_at,
      sandbox: (() => {
        // Handle sandbox as JSON string from API
        if (typeof project.sandbox === 'string') {
          try {
            return JSON.parse(project.sandbox);
          } catch (e) {
            console.warn('Failed to parse sandbox JSON string:', project.sandbox);
            return {
              id: '',
              pass: '',
              vnc_preview: '',
              sandbox_url: '',
            };
          }
        }
        // Handle sandbox as object (fallback)
        return project.sandbox || {
          id: '',
          pass: '',
          vnc_preview: '',
          sandbox_url: '',
        };
      })(),
    }));

    return mappedProjects;
  } catch (err) {
    console.error('Error fetching projects:', err);
    handleApiError(err, { operation: 'load projects', resource: 'projects' });
    // Return empty array for permission errors to avoid crashing the UI
    return [];
  }
};

// 专门用于侧边栏的项目获取函数
export const getSidebarProjects = async (): Promise<Project[]> => {
  try {
    const supabase = createClient();
    const { data: { session } } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    const response = await fetch(`${API_URL}/sidebar/projects`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error('Failed to fetch sidebar projects:', response.status, errorText);
      return []; // Return empty array for errors to avoid crashing the UI
    }

    const data = await response.json();
    // 确保返回的是数组格式
    if (Array.isArray(data)) {
      return data;
    } else {
      console.warn('Backend returned non-array data for sidebar projects:', data);
      return [];
    }
  } catch (err) {
    console.error('Error fetching sidebar projects:', err);
    handleApiError(err, { operation: 'load sidebar projects', resource: 'sidebar projects' });
    // Return empty array for permission errors to avoid crashing the UI
    return [];
  }
};

export const getProject = async (projectId: string): Promise<Project> => {
  const supabase = createClient();

  try {
    const { data, error } = await supabase
      .from('projects')
      .select('*')
      .eq('project_id', projectId)
      .single();

    if (error) {
      // Handle the specific "no rows returned" error from Supabase
      if (error.code === 'PGRST116') {
        throw new Error(`Project not found or not accessible: ${projectId}`);
      }
      throw error;
    }
    const {
      data: { session },
    } = await supabase.auth.getSession();
    const initialSandbox = parseProjectSandbox(data.sandbox);
    const ensuredSandbox = initialSandbox.id
      ? await ensureProjectSandboxActive(projectId, session?.access_token)
      : null;
    const mergedSandbox = {
      ...initialSandbox,
      ...(ensuredSandbox || {}),
    };

    // Map database fields to our Project type
    const mappedProject: Project = {
      id: data.project_id,
      name: data.name || '',
      description: data.description || '',
      account_id: data.account_id,
      is_public: data.is_public || false,
      created_at: data.created_at,
      sandbox: mergedSandbox,
    };

    return mappedProject;
  } catch (error) {
    console.error(`Error fetching project ${projectId}:`, error);
    handleApiError(error, { operation: 'load project', resource: `project ${projectId}` });
    throw error;
  }
};

export const createProject = async (
  projectData: { name: string; description: string },
  accountId?: string,
): Promise<Project> => {
  const supabase = createClient();

  // If accountId is not provided, we'll need to get the user's ID
  if (!accountId) {
    const { data: userData, error: userError } = await supabase.auth.getUser();

    if (userError) throw userError;
    if (!userData.user)
      throw new Error('You must be logged in to create a project');

    // In Basejump, the personal account ID is the same as the user ID
    accountId = userData.user.id;
  }

  const { data, error } = await supabase
    .from('projects')
    .insert({
      name: projectData.name,
      description: projectData.description || null,
      account_id: accountId,
    })
    .select()
    .single();

  if (error) {
    handleApiError(error, { operation: 'create project', resource: 'project' });
    throw error;
  }

  const project = {
    id: data.project_id,
    name: data.name,
    description: data.description || '',
    account_id: data.account_id,
    created_at: data.created_at,
    sandbox: { id: '', pass: '', vnc_preview: '' },
  };
  return project;
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
    handleApiError(error, { operation: 'update project', resource: `project ${projectId}` });
    throw error;
  }

  if (!updatedData) {
    const noDataError = new Error('No data returned from update');
    handleApiError(noDataError, { operation: 'update project', resource: `project ${projectId}` });
    throw noDataError;
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
  const project = {
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
  return project;
};

export const deleteProject = async (projectId: string): Promise<void> => {
  const supabase = createClient();
  const { error } = await supabase
    .from('projects')
    .delete()
    .eq('project_id', projectId);

  if (error) {
    handleApiError(error, { operation: 'delete project', resource: `project ${projectId}` });
    throw error;
  }
};

// Thread APIs
export const getThreads = async (projectId?: string): Promise<Thread[]> => {
  const supabase = createClient();

  // Get the current user's ID to filter threads
  const { data: userData, error: userError } = await supabase.auth.getUser();
  if (userError) {
    console.error('Error getting current user:', userError);
    return [];
  }

  // If no user is logged in, return an empty array
  if (!userData.user) {
    return [];
  }

  let query = supabase.from('threads').select('*');

  // Always filter by the current user's account ID
  query = query.eq('account_id', userData.user.id);

  if (projectId) {
    query = query.eq('project_id', projectId);
  }

  const { data, error } = await query;

  if (error) {
    handleApiError(error, { operation: 'load threads', resource: projectId ? `threads for project ${projectId}` : 'threads' });
    throw error;
  }

  const mappedThreads: Thread[] = (data || [])
    .filter((thread) => {
      const metadata = thread.metadata || {};
      return !metadata.is_agent_builder;
    })
    .map((thread) => ({
      thread_id: thread.thread_id,
      account_id: thread.account_id,
      project_id: thread.project_id,
      created_at: thread.created_at,
      updated_at: thread.updated_at,
      metadata: thread.metadata,
    }));
  return mappedThreads;
};

// 专门用于侧边栏的线程获取函数
export const getSidebarThreads = async (projectId?: string): Promise<Thread[]> => {
  try {
    const supabase = createClient();
    const { data: { session } } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    const url = buildApiUrl('/sidebar/threads');
    if (projectId) {
      url.searchParams.append('project_id', projectId);
    }

    const response = await fetch(url.toString(), {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error('Failed to fetch sidebar threads:', response.status, errorText);
      return [];
    }

    const data = await response.json();
    // 确保返回的是数组格式
    if (Array.isArray(data)) {
      return data;
    } else {
      console.warn('Backend returned non-array data for sidebar threads:', data);
      return [];
    }
  } catch (error) {
    console.error('Error getting sidebar threads:', error);
    handleApiError(error, { operation: 'load sidebar threads', resource: projectId ? `sidebar threads for project ${projectId}` : 'sidebar threads' });
    return [];
  }
};

export const getThread = async (threadId: string): Promise<Thread> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
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
    handleApiError(error, { operation: 'load thread', resource: `thread ${threadId}` });
    throw error;
  }
};

export const createThread = async (projectId: string): Promise<Thread> => {
  const supabase = createClient();

  // If user is not logged in, redirect to login
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    throw new Error('You must be logged in to create a thread');
  }

  const { data, error } = await supabase
    .from('threads')
    .insert({
      project_id: projectId,
      account_id: user.id, // Use the current user's ID as the account ID
    })
    .select()
    .single();

  if (error) {
    handleApiError(error, { operation: 'create thread', resource: 'thread' });
    throw error;
  }
  return data;
};

export const addUserMessage = async (
  threadId: string,
  content: string,
  options?: {
    media_refs?: ImageMediaRef[];
  },
): Promise<Message> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    // Call backend API to add user message
    const response = await fetch(`${API_URL}/threads/${threadId}/messages`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify({
        content: content,
        type: 'user',
        ...(options?.media_refs && options.media_refs.length > 0
          ? { media_refs: options.media_refs }
          : {}),
      }),
      cache: 'no-store',
    });

    if (!response.ok) {
      throw new Error(
        `Error adding user message: ${response.statusText} (${response.status})`,
      );
    }

    const data = await response.json();
    const normalizedContent = typeof data.content === 'string'
      ? data.content
      : JSON.stringify(data.content ?? { role: 'user', content });
    const mappedMessage: Message = {
      message_id: data.message_id || data.id,
      thread_id: data.thread_id || threadId,
      type: data.type || 'user',
      role: data.role || data.type || 'user',
      is_llm_message: data.is_llm_message ?? false,
      content: normalizedContent,
      metadata: data.metadata
        ? typeof data.metadata === 'string'
          ? data.metadata
          : JSON.stringify(data.metadata)
        : '{}',
      created_at: data.created_at || new Date().toISOString(),
      updated_at: data.updated_at || data.created_at || new Date().toISOString(),
      agent_id: data.agent_id,
      agent_version_id: data.agent_version_id,
      agents: data.agents,
      author: data.author,
      event_id: data.event_id,
    };

    console.log('✅ [addUserMessage] User message sent to backend successfully');
    return mappedMessage;
  } catch (error: any) {
    console.error('❌ [addUserMessage] Error adding user message:', error);
    handleApiError(error, { operation: 'add message', resource: 'message' });
    throw error;
  }
};

export const getMessages = async (threadId: string): Promise<Message[]> => {
  try {
    console.log('🚀 [getMessages] API调用开始:', {
      threadId,
      API_URL,
      timestamp: new Date().toISOString()
    });
    
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    const apiUrl = `${API_URL}/threads/${threadId}/messages`;
    console.log('🌐 [getMessages] 调用API URL:', apiUrl);

    const response = await fetch(apiUrl, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    });

    if (!response.ok) {
      throw new Error(
        `Error getting messages: ${response.statusText} (${response.status})`,
      );
    }

    const data = await response.json();
    console.log('📡 [getMessages] API响应数据:', {
      status: response.status,
      statusText: response.statusText,
      responseType: typeof data,
      isArray: Array.isArray(data),
      dataKeys: data ? Object.keys(data) : null,
      data: JSON.stringify(data, null, 2)
    });
    
    let messages: any[] = [];
    
    // 提取消息数组
    if (Array.isArray(data)) {
      messages = data;
      console.log('📨 [getMessages] 使用直接数组格式');
    } else if (data && Array.isArray(data.messages)) {
      messages = data.messages;
      console.log('📨 [getMessages] 使用data.messages格式');
    } else if (data && Array.isArray(data.data)) {
      messages = data.data;
      console.log('📨 [getMessages] 使用data.data格式');
    } else {
      console.warn('Messages API returned unexpected format:', data);
      return [];
    }
    
    console.log('📊 [getMessages] 提取的消息统计:', {
      totalCount: messages.length,
      messageTypes: messages.reduce((acc: any, msg: any) => {
        acc[msg.type] = (acc[msg.type] || 0) + 1;
        return acc;
      }, {}),
      toolMessages: messages.filter((msg: any) => msg.type === 'tool').length
    });
    
    // 映射后端数据格式到前端期望的格式
    const mappedMessages = messages.map((msg: any) => ({
      message_id: msg.message_id || msg.id,
      thread_id: msg.thread_id,
      type: msg.type, // 'user' | 'assistant' 等
      role: msg.type, // 添加role字段
      is_llm_message: msg.is_llm_message || false,
      content: typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content),
      metadata: msg.metadata ? (typeof msg.metadata === 'string' ? msg.metadata : JSON.stringify(msg.metadata)) : '{}',
      created_at: msg.created_at,
      updated_at: msg.updated_at || msg.created_at, // 如果没有updated_at，使用created_at
      agent_id: msg.agent_id,
      agent_version_id: msg.agent_version_id,
      agents: msg.agents,
      author: msg.author, // 保留原有字段
      event_id: msg.event_id, // 保留原有字段
    }));
    
    console.log('🔍 [getMessages] 原始数据:', messages);
    console.log('🔍 [getMessages] 映射后数据:', mappedMessages);
    
    return mappedMessages;
  } catch (error) {
    handleApiError(error, { operation: 'load messages', resource: `messages for thread ${threadId}` });
    throw error;
  }
};

// Agent APIs
export const startAgent = async (
  threadId: string,
  options?: {
    model_name?: string;
    enable_thinking?: boolean;
    reasoning_effort?: string;
    stream?: boolean;
    agent_id?: string; // Optional again
    shadow_clone_mode?: ShadowCloneMode;
    shadow_clone_main_model?: string;
    shadow_clone_subagent_model?: string;
  },
): Promise<{ agent_run_id: string }> => {
  try {
    const accessToken = await getAccessTokenOrThrow();

    // Check if backend URL is configured
    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    const defaultOptions = {
      model_name: 'claude-3-7-sonnet-latest',
      enable_thinking: false,
      reasoning_effort: 'low',
      stream: true,
    };

    const finalOptions = { ...defaultOptions, ...options };

    const body: any = {
      model_name: finalOptions.model_name,
      enable_thinking: finalOptions.enable_thinking,
      reasoning_effort: finalOptions.reasoning_effort,
      stream: finalOptions.stream,
    };
    
    // Only include agent_id if it's provided
    if (finalOptions.agent_id) {
      body.agent_id = finalOptions.agent_id;
    }
    if (finalOptions.shadow_clone_mode) {
      body.shadow_clone_mode = finalOptions.shadow_clone_mode;
    }
    if (finalOptions.shadow_clone_main_model) {
      body.shadow_clone_main_model = finalOptions.shadow_clone_main_model;
    }
    if (finalOptions.shadow_clone_subagent_model) {
      body.shadow_clone_subagent_model = finalOptions.shadow_clone_subagent_model;
    }

    const response = await fetch(`${API_URL}/thread/${threadId}/agent/start`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      if (response.status === 402) {
        try {
          const errorData = await response.json();
          const detail = errorData?.detail || { message: 'Payment Required' };
          if (typeof detail.message !== 'string') {
            detail.message = 'Payment Required';
          }
          throw new BillingError(response.status, detail);
        } catch (parseError) {
          throw new BillingError(
            response.status,
            { message: 'Payment Required' },
            `Error starting agent: ${response.statusText} (402)`,
          );
        }
      }

      if (response.status === 429) {
          const errorData = await response.json();
          const detail = errorData?.detail || { 
            message: 'Too many agent runs running',
            running_thread_ids: [],
            running_count: 0,
          };
          if (typeof detail.message !== 'string') {
            detail.message = 'Too many agent runs running';
          }
          if (!Array.isArray(detail.running_thread_ids)) {
            detail.running_thread_ids = [];
          }
          if (typeof detail.running_count !== 'number') {
            detail.running_count = 0;
          }
          throw new AgentRunLimitError(response.status, detail);
      }

      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `[API] Error starting agent: ${response.status} ${response.statusText}`,
      );
      throw new Error(
        `Error starting agent: ${response.statusText} (${response.status})`,
      );
    }

    const result = await response.json();
    return result;
  } catch (error) {
    if (error instanceof BillingError || error instanceof AgentRunLimitError) {
      throw error;
    }

    if (error instanceof NoAccessTokenAvailableError) {
      throw error;
    }

    console.error('[API] Failed to start agent:', error);
    
    if (
      error instanceof TypeError &&
      error.message.includes('Failed to fetch')
    ) {
      const networkError = new Error(
        `Cannot connect to backend server. Please check your internet connection and make sure the backend is running.`,
      );
      handleApiError(networkError, { operation: 'start agent', resource: 'AI assistant' });
      throw networkError;
    }

    handleApiError(error, { operation: 'start agent', resource: 'AI assistant' });
    throw error;
  }
};

export const confirmShadowClone = async (
  agentRunId: string,
): Promise<{ status: string }> => {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    throw new NoAccessTokenAvailableError();
  }

  const response = await fetch(
    `${API_URL}/agent-run/${agentRunId}/shadow-clone/confirm`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    },
  );

  if (!response.ok) {
    throw new Error(
      `Error confirming shadow clone: ${response.statusText} (${response.status})`,
    );
  }

  return response.json();
};

export const denyShadowClone = async (
  agentRunId: string,
  reason?: string,
): Promise<{ status: string; reason?: string | null }> => {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    throw new NoAccessTokenAvailableError();
  }

  const response = await fetch(
    `${API_URL}/agent-run/${agentRunId}/shadow-clone/deny`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify({ reason: reason || null }),
      cache: 'no-store',
    },
  );

  if (!response.ok) {
    throw new Error(
      `Error denying shadow clone: ${response.statusText} (${response.status})`,
    );
  }

  return response.json();
};

export const getShadowCloneStatus = async (
  agentRunId: string,
): Promise<ShadowCloneStatusResponse> => {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    throw new NoAccessTokenAvailableError();
  }

  const response = await fetch(
    `${API_URL}/agent-run/${agentRunId}/shadow-clone/status`,
    {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    },
  );

  if (!response.ok) {
    throw new Error(
      `Error getting shadow clone status: ${response.statusText} (${response.status})`,
    );
  }

  return response.json();
};

export const getShadowCloneResults = async (
  agentRunId: string,
): Promise<ShadowCloneResultsResponse> => {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    throw new NoAccessTokenAvailableError();
  }

  const response = await fetch(
    `${API_URL}/agent-run/${agentRunId}/shadow-clone/results`,
    {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    },
  );

  if (!response.ok) {
    throw new Error(
      `Error getting shadow clone results: ${response.statusText} (${response.status})`,
    );
  }

  return response.json();
};

export const getShadowCloneFullResult = async (
  agentRunId: string,
  subtaskId: string,
): Promise<ShadowCloneFullResultResponse> => {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    throw new NoAccessTokenAvailableError();
  }

  const response = await fetch(
    `${API_URL}/agent-run/${agentRunId}/shadow-clone/results/${subtaskId}`,
    {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    },
  );

  if (!response.ok) {
    throw new Error(
      `Error getting shadow clone full result: ${response.statusText} (${response.status})`,
    );
  }

  return response.json();
};

export const stopAgent = async (agentRunId: string): Promise<void> => {
  addToNonRunning(agentRunId);
  const existingStream = activeStreams.get(agentRunId);
  if (existingStream) {
    existingStream.close();
    activeStreams.delete(agentRunId);
  }

  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    const authError = new NoAccessTokenAvailableError();
    handleApiError(authError, { operation: 'stop agent', resource: 'AI assistant' });
    throw authError;
  }

  const response = await fetch(`${API_URL}/agent-run/${agentRunId}/stop`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${session.access_token}`,
    },
    cache: 'no-store',
  });

  posthog.capture('task_abandoned', { agentRunId });

  if (!response.ok) {
    const stopError = new Error(`Error stopping agent: ${response.statusText}`);
    handleApiError(stopError, { operation: 'stop agent', resource: 'AI assistant' });
    throw stopError;
  }
};

export const getAgentStatus = async (agentRunId: string): Promise<AgentRun> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      console.error('[API] No access token available for getAgentStatus');
      throw new NoAccessTokenAvailableError();
    }

    const url = `${API_URL}/agent-run/${agentRunId}`;
    const response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `[API] Error getting agent status: ${response.status} ${response.statusText}`,
        errorText,
      );
      if (response.status === 404) {
        addToNonRunning(agentRunId);
      }

      throw new Error(
        `Error getting agent status: ${response.statusText} (${response.status})`,
      );
    }

    return response.json();
  } catch (error) {
    console.error('[API] Failed to get agent status:', error);
    handleApiError(error, { operation: 'get agent status', resource: 'AI assistant status', silent: true });
    throw error;
  }
};

export const getAgentRuns = async (threadId: string): Promise<AgentRun[]> => {
  const normalizeAgentRun = (rawRun: unknown): AgentRun | null => {
    if (!rawRun || typeof rawRun !== 'object') {
      return null;
    }

    const run = rawRun as Record<string, unknown>;
    const runId =
      normalizeOptionalString(run.id) ||
      normalizeOptionalString(run.agent_run_id) ||
      '';
    const status =
      run.status === 'running' ||
      run.status === 'completed' ||
      run.status === 'stopped' ||
      run.status === 'error'
        ? run.status
        : 'error';

    return {
      id: runId,
      thread_id: normalizeOptionalString(run.thread_id) || '',
      status,
      started_at: normalizeOptionalString(run.started_at) || '',
      completed_at: normalizeOptionalString(run.completed_at),
      responses: Array.isArray(run.responses) ? (run.responses as Message[]) : [],
      error: normalizeOptionalString(run.error),
      metadata: normalizeAgentRunMetadata(run.metadata),
      file_delivery_source: normalizeRunFileDeliverySource(
        run.file_delivery_source,
        runId,
      ),
    };
  };

  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    const response = await fetch(`${API_URL}/thread/${threadId}/agent-runs`, {
      headers: {
        Authorization: `Bearer ${session.access_token}`,
      },
      cache: 'no-store',
    });

    if (!response.ok) {
      throw new Error(`Error getting agent runs: ${response.statusText}`);
    }

    const data = await response.json();
    
    // 确保返回的是数组格式
    if (Array.isArray(data)) {
      return data
        .map((run) => normalizeAgentRun(run))
        .filter((run): run is AgentRun => Boolean(run));
    } else if (data && Array.isArray(data.agent_runs)) {
      return data.agent_runs
        .map((run: unknown) => normalizeAgentRun(run))
        .filter((run: AgentRun | null): run is AgentRun => Boolean(run));
    } else if (data && Array.isArray(data.data)) {
      return data.data
        .map((run: unknown) => normalizeAgentRun(run))
        .filter((run: AgentRun | null): run is AgentRun => Boolean(run));
    } else {
      console.warn('Agent runs API returned unexpected format:', data);
      return [];
    }
  } catch (error) {
    if (error instanceof NoAccessTokenAvailableError) {
      throw error;
    }

    console.error('Failed to get agent runs:', error);
    handleApiError(error, { operation: 'load agent runs', resource: 'conversation history' });
    throw error;
  }
};

export const streamAgent = (
  agentRunId: string,
  callbacks: StreamAgentCallbacks,
  options?: {
    fromIndex?: number;
    fromEventId?: string;
  },
): (() => void) => {
  // Note: nonRunningAgentRuns is populated when SSE endpoint returns "not found" error.
  // This prevents repeated connection attempts to non-existent agent runs.
  // We keep this check but removed the pre-connection status check to allow
  // streaming for fast-completing agents.
  if (nonRunningAgentRuns.has(agentRunId)) {
    console.log(`[streamAgent] Agent run ${agentRunId} is in nonRunningAgentRuns set, skipping connection`);
    setTimeout(() => {
      callbacks.onError(`Agent run ${agentRunId} is not running`);
      callbacks.onClose();
    }, 0);

    return () => {};
  }

  const existingStream = activeStreams.get(agentRunId);
  if (existingStream) {
    existingStream.close();
    activeStreams.delete(agentRunId);
  }

  let cancelled = false;
  let setupEventSource: EventSource | null = null;

  try {
    const setupStream = async () => {
      if (cancelled) return;
      // Skip the pre-connection status check - connect to SSE regardless of status.
      // The SSE endpoint will handle completed agents by sending initial messages
      // from Redis then closing. This ensures we get streaming messages even for
      // fast-completing agents.
      console.log(`[streamAgent] Setting up SSE stream for ${agentRunId} (skipping status check)`);

      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();
      if (cancelled) return;

      if (!session?.access_token) {
        if (cancelled) return;
        const authError = new NoAccessTokenAvailableError();
        callbacks.onError(authError);
        callbacks.onClose();
        return;
      }

      const url = new URL(`${API_URL}/agent-run/${agentRunId}/stream`);
      url.searchParams.append('token', session.access_token);
      if (typeof options?.fromIndex === 'number' && options.fromIndex >= 0) {
        url.searchParams.append('from_index', String(options.fromIndex));
      }
      if (typeof options?.fromEventId === 'string' && options.fromEventId.trim()) {
        url.searchParams.append('from_event_id', options.fromEventId.trim());
      }

      const eventSource = new EventSource(url.toString());
      setupEventSource = eventSource;
      if (cancelled) {
        eventSource.close();
        setupEventSource = null;
        return;
      }

      activeStreams.set(agentRunId, eventSource);
      setupEventSource = null;

      const deleteIfCurrent = () => {
        const stream = activeStreams.get(agentRunId);
        if (stream === eventSource) {
          activeStreams.delete(agentRunId);
        }
      };

      eventSource.onopen = () => {
      };

      eventSource.onmessage = (event) => {
        if (cancelled) return;
        try {
          const rawData = event.data;
          let jsonData: any | null = null;
          try {
            jsonData = JSON.parse(rawData);
          } catch {
            jsonData = null;
          }

          if (typeof jsonData?.event_index === 'number') {
            callbacks.onEventIndex?.(jsonData.event_index);
          }
          const eventCursor =
            typeof jsonData?.event_cursor === 'string'
              ? jsonData.event_cursor
              : typeof jsonData?.metadata?.event_cursor === 'string'
                ? jsonData.metadata.event_cursor
                : typeof jsonData?.event_id === 'string'
                  ? jsonData.event_id
                  : null;
          if (eventCursor) {
            callbacks.onEventCursor?.(eventCursor);
          }

          if (jsonData?.type === 'ping') return;

          // Skip empty messages
          if (!rawData || rawData.trim() === '') {
            return;
          }

          // Check for error status messages
          if (jsonData?.status === 'error') {
            console.error(`[STREAM] Error status received for ${agentRunId}:`, jsonData);
            
            // Pass the error message to the callback
            callbacks.onError(jsonData.message || 'Unknown error occurred');
            
            // Don't close the stream for error status messages as they may continue
            return;
          }

          // Check for "Agent run not found" error
          if (
            rawData.includes('Agent run') &&
            rawData.includes('not found in active runs')
          ) {
            // Notify about the error
            callbacks.onError('Agent run not found in active runs');

            // Clean up
            eventSource.close();
            deleteIfCurrent();
            callbacks.onClose();

            return;
          }

          // Check for completion messages
          if (jsonData?.type === 'status' && jsonData?.status === 'completed') {
            // Notify about the message
            callbacks.onMessage(rawData);

            // Clean up
            eventSource.close();
            deleteIfCurrent();
            callbacks.onClose();

            return;
          }

          // Check for thread run end message
          if (jsonData?.type === 'status' && jsonData?.status === 'thread_run_end') {
            // Notify about the message
            callbacks.onMessage(rawData);
            return;
          }

          // For all other messages, just pass them through
          callbacks.onMessage(rawData);
        } catch (error) {
          console.error(`[STREAM] Error handling message:`, error);
          callbacks.onError(error instanceof Error ? error : String(error));
        }
      };

      eventSource.onerror = () => {
        if (cancelled) {
          eventSource.close();
          deleteIfCurrent();
          return;
        }
        // Always close and let the hook reconnect with an updated from_index cursor.
        eventSource.close();
        deleteIfCurrent();

        getAgentStatus(agentRunId)
          .then(() => {
            if (cancelled) return;
            callbacks.onClose();
          })
          .catch((err) => {
            if (cancelled) return;
            console.error(
              `[STREAM] Error checking agent status after stream error:`,
              err,
            );

            const errMsg = err instanceof Error ? err.message : String(err);
            const isNotFoundErr =
              errMsg.includes('not found') ||
              errMsg.includes('404') ||
              errMsg.includes('does not exist');

            if (isNotFoundErr) {
              addToNonRunning(agentRunId);
            }

            callbacks.onClose();
          });
      };
    };

    // Start the stream setup
    void setupStream().catch((error) => {
      if (cancelled) return;
      console.error(`[STREAM] Error setting up stream for ${agentRunId}:`, error);
      callbacks.onError(error instanceof Error ? error : String(error));
      callbacks.onClose();
    });

    // Return a cleanup function
    return () => {
      cancelled = true;
      if (setupEventSource) {
        setupEventSource.close();
        setupEventSource = null;
      }
      const stream = activeStreams.get(agentRunId);
      if (stream) {
        stream.close();
        activeStreams.delete(agentRunId);
      }
    };
  } catch (error) {
    console.error(`[STREAM] Error setting up stream for ${agentRunId}:`, error);
    callbacks.onError(error instanceof Error ? error : String(error));
    callbacks.onClose();
    return () => {};
  }
};

// Sandbox API Functions
export const createSandboxFile = async (
  sandboxId: string,
  filePath: string,
  content: string,
): Promise<void> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    // Use FormData to handle both text and binary content more reliably
    const formData = new FormData();
    formData.append('path', filePath);

    // Create a Blob from the content string and append as a file
    const blob = new Blob([content], { type: 'application/octet-stream' });
    formData.append('file', blob, filePath.split('/').pop() || 'file');

    const headers: Record<string, string> = {};
    if (session?.access_token) {
      headers['Authorization'] = `Bearer ${session.access_token}`;
    }

    const response = await fetch(`${API_URL}/sandboxes/${sandboxId}/files`, {
      method: 'POST',
      headers,
      body: formData,
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error creating sandbox file: ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error creating sandbox file: ${response.statusText} (${response.status})`,
      );
    }

    const result = await response.json();
    return result;
  } catch (error) {
    console.error('Failed to create sandbox file:', error);
    handleApiError(error, { operation: 'create file', resource: `file ${filePath}` });
    throw error;
  }
};

// Fallback method for legacy support using JSON
export const createSandboxFileJson = async (
  sandboxId: string,
  filePath: string,
  content: string,
): Promise<void> => {
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

    const response = await fetch(
      `${API_URL}/sandboxes/${sandboxId}/files/json`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          path: filePath,
          content: content,
        }),
      },
    );

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error creating sandbox file (JSON): ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error creating sandbox file: ${response.statusText} (${response.status})`,
      );
    }

    const result = await response.json();
    return result;
  } catch (error) {
    console.error('Failed to create sandbox file with JSON:', error);
    handleApiError(error, { operation: 'create file', resource: `file ${filePath}` });
    throw error;
  }
};

// Helper function to normalize file paths with Unicode characters
function normalizePathWithUnicode(path: string): string {
  try {
    // Replace escaped Unicode sequences with actual characters
    return path.replace(/\\u([0-9a-fA-F]{4})/g, (_, hexCode) => {
      return String.fromCharCode(parseInt(hexCode, 16));
    });
  } catch (e) {
    console.error('Error processing Unicode escapes in path:', e);
    return path;
  }
}

function parsePositiveIntHeader(value: string | null): number {
  const parsed = value ? parseInt(value, 10) : NaN;
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function parseNullableBooleanHeader(value: string | null): boolean | null {
  if (value == null) return null;
  const normalized = value.trim().toLowerCase();
  if (normalized === '1' || normalized === 'true') return true;
  if (normalized === '0' || normalized === 'false') return false;
  return null;
}

function readArchiveResponseMetadata(headers: Headers): FileActionResponseMetadata {
  return {
    requestId: headers.get('x-request-id'),
    responseSource: headers.get('x-archive-response-source'),
    identitySource: headers.get('x-archive-identity-source'),
    sandboxAvailable: parseNullableBooleanHeader(headers.get('x-archive-sandbox-available')),
    outcome: headers.get('x-archive-outcome'),
    fallback: parseNullableBooleanHeader(headers.get('x-archive-fallback')),
  };
}

function readWorkspaceResponseMetadata(headers: Headers): FileActionResponseMetadata {
  return {
    requestId: headers.get('x-request-id'),
    responseSource: headers.get('x-workspace-response-source'),
    identitySource: null,
    sandboxAvailable: null,
    outcome: null,
    fallback: parseNullableBooleanHeader(headers.get('x-workspace-fallback')),
  };
}

function emitFileActionPhase(
  options: FileActionRequestOptions,
  event: FileActionPhaseEvent,
): void {
  options.onPhase?.(event);
}

function createOperationEntropy(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) {
    return randomUuid;
  }

  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createFileActionOperationId(action: FileActionKind): string {
  return `file-${action}-${createOperationEntropy()}`;
}

async function resolveFileActionAccessToken(
  accessToken?: string | null,
): Promise<string | null> {
  if (typeof accessToken === 'string') {
    return accessToken || null;
  }

  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  return session?.access_token || null;
}

function buildFileActionHeaders(
  action: FileActionKind,
  accessToken?: string | null,
  headers: Record<string, string> = {},
  operationId?: string,
): { headers: Record<string, string>; operationId: string } {
  const resolvedOperationId = operationId || createFileActionOperationId(action);
  const resolvedHeaders: Record<string, string> = {
    ...headers,
    [FILE_ACTION_OPERATION_ID_HEADER]: resolvedOperationId,
  };

  if (accessToken) {
    resolvedHeaders.Authorization = `Bearer ${accessToken}`;
  }

  return {
    headers: resolvedHeaders,
    operationId: resolvedOperationId,
  };
}

function normalizeMimeType(contentTypeHeader: string | null, fallback: string): string {
  const normalized = contentTypeHeader?.split(';')[0]?.trim();
  return normalized || fallback;
}

function getPathFilename(path: string, fallback = 'file'): string {
  const segments = path.split('/').filter(Boolean);
  return segments.at(-1) || fallback;
}

async function fetchSandboxFileResponse(
  sandboxId: string,
  path: string,
  action: Extract<FileActionKind, 'read' | 'download'>,
  options: FileActionRequestOptions = {},
): Promise<{
  response: Response;
  normalizedPath: string;
  operationId: string;
  metadata: FileActionResponseMetadata;
}> {
  const accessToken = await resolveFileActionAccessToken(options.accessToken);
  const normalizedPath = normalizePathWithUnicode(path);
  const url = buildApiUrl(`/sandboxes/${sandboxId}/files/content`);
  url.searchParams.set('path', normalizedPath);

  const { headers, operationId } = buildFileActionHeaders(
    action,
    accessToken,
    {},
    options.operationId,
  );

  emitFileActionPhase(options, {
    phase: 'request_dispatched',
    operationId,
  });
  const response = await fetch(url.toString(), {
    headers,
  });
  const metadata = readWorkspaceResponseMetadata(response.headers);

  if (!response.ok) {
    const errorText = await response
      .text()
      .catch(() => 'No error details available');
    throw new Error(
      `Error ${action === 'download' ? 'downloading' : 'getting'} sandbox file content: ${response.statusText} (${response.status}) [request ${operationId}] ${errorText}`,
    );
  }

  emitFileActionPhase(options, {
    phase: 'response_ok',
    operationId,
    status: response.status,
    ...metadata,
  });
  return {
    response,
    normalizedPath,
    operationId,
    metadata,
  };
}

function parseApiDetailMessage(errorText: string): string {
  if (!errorText) return 'No error details available';

  try {
    const parsed = JSON.parse(errorText);
    if (parsed?.detail?.message) {
      return String(parsed.detail.message);
    }
    if (typeof parsed?.detail === 'string') {
      return parsed.detail;
    }
    if (typeof parsed?.message === 'string') {
      return parsed.message;
    }
  } catch {
    // Keep the raw response body when the backend does not return JSON.
  }

  return errorText;
}

function classifySandboxArchiveFailure(
  status: number,
  detailMessage: string,
): SandboxArchiveFailureKind {
  const normalizedDetail = detailMessage.toLowerCase();

  if (
    status === 401 ||
    status === 403 ||
    normalizedDetail.includes('forbidden') ||
    normalizedDetail.includes('not authorized') ||
    normalizedDetail.includes('access denied')
  ) {
    return 'authorization';
  }

  if (
    normalizedDetail.includes('no files found') ||
    normalizedDetail.includes('empty artifact') ||
    normalizedDetail.includes('empty archive')
  ) {
    return 'empty_artifact_set';
  }

  if (
    normalizedDetail.includes('sandbox unavailable') ||
    normalizedDetail.includes('sandbox lease') ||
    normalizedDetail.includes('sandbox not found') ||
    normalizedDetail.includes('archive sandbox unavailable') ||
    normalizedDetail.includes('resolved sandbox')
  ) {
    return 'source_identity';
  }

  if (status === 404 || normalizedDetail.includes('not found')) {
    return 'not_found';
  }

  if (status >= 500) {
    return 'server';
  }

  return 'unknown';
}

function parseFilenameFromContentDisposition(headerValue: string | null, fallback: string): string {
  if (!headerValue) return fallback;

  const utf8Match = headerValue.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch (error) {
      console.warn('Failed to decode UTF-8 filename from header:', error);
    }
  }

  const quotedMatch = headerValue.match(/filename=\"([^\"]+)\"/i);
  if (quotedMatch?.[1]) return quotedMatch[1];

  const plainMatch = headerValue.match(/filename=([^;]+)/i);
  if (plainMatch?.[1]) return plainMatch[1].trim();

  return fallback;
}

function normalizeSandboxFileListDiagnostics(
  rawDiagnostics: unknown,
): SandboxFileListDiagnostics | undefined {
  if (!rawDiagnostics || typeof rawDiagnostics !== 'object') {
    return undefined;
  }

  const diagnostics = rawDiagnostics as Record<string, unknown>;
  const normalizedDiagnostics: SandboxFileListDiagnostics = {
    identitySource:
      normalizeOptionalString(
        diagnostics.identitySource ?? diagnostics.identity_source,
      ) || undefined,
    sandboxAvailable:
      normalizeOptionalBoolean(
        diagnostics.sandboxAvailable ?? diagnostics.sandbox_available,
      ) ?? undefined,
    resolvedSandboxId: normalizeOptionalString(
      diagnostics.resolvedSandboxId ?? diagnostics.resolved_sandbox_id,
    ),
  };

  if (
    normalizedDiagnostics.identitySource === undefined &&
    normalizedDiagnostics.sandboxAvailable === undefined &&
    normalizedDiagnostics.resolvedSandboxId === null
  ) {
    return undefined;
  }

  return normalizedDiagnostics;
}

function normalizeSandboxFileListResponse(
  payload: unknown,
): SandboxFileListResponse {
  if (Array.isArray(payload)) {
    return {
      files: payload as FileInfo[],
    };
  }

  if (!payload || typeof payload !== 'object') {
    return { files: [] };
  }

  const response = payload as Record<string, unknown>;

  return {
    files: Array.isArray(response.files) ? (response.files as FileInfo[]) : [],
    diagnostics: normalizeSandboxFileListDiagnostics(response.diagnostics),
  };
}

export const listSandboxFilesResponse = async (
  sandboxId: string,
  path: string,
  options: FileActionRequestOptions = {},
): Promise<SandboxFileListResponse> => {
  try {
    const accessToken = await resolveFileActionAccessToken(options.accessToken);
    const url = buildApiUrl(`/sandboxes/${sandboxId}/files`);

    // Normalize the path to handle Unicode escape sequences
    const normalizedPath = normalizePathWithUnicode(path);

    // Properly encode the path parameter for UTF-8 support
    url.searchParams.set('path', normalizedPath);

    const { headers, operationId } = buildFileActionHeaders(
      'list',
      accessToken,
      {},
      options.operationId,
    );

    const response = await fetch(url.toString(), {
      headers,
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error listing sandbox files: ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error listing sandbox files: ${response.statusText} (${response.status}) [request ${operationId}]`,
      );
    }

    const data = await response.json();
    return normalizeSandboxFileListResponse(data);
  } catch (error) {
    console.error('Failed to list sandbox files:', error);
    // handleApiError(error, { operation: 'list files', resource: `directory ${path}` });
    throw error;
  }
};

export const listSandboxFiles = async (
  sandboxId: string,
  path: string,
  options: FileActionRequestOptions = {},
): Promise<FileInfo[]> => {
  const response = await listSandboxFilesResponse(sandboxId, path, options);
  return response.files;
};

export const downloadSandboxArchive = async (
  sandboxId: string,
  request: SandboxArchiveDownloadRequest = {},
  options: FileActionRequestOptions = {},
): Promise<SandboxArchiveDownloadResult> => {
  try {
    const accessToken = await resolveFileActionAccessToken(options.accessToken);
    const url = buildApiUrl(`/sandboxes/${sandboxId}/files/archive`);

    const payload: SandboxArchiveDownloadRequest = {
      ...request,
      root_path: request.root_path ? normalizePathWithUnicode(request.root_path) : request.root_path,
      paths: request.paths?.map((item) => normalizePathWithUnicode(item)),
    };

    const { headers, operationId } = buildFileActionHeaders(
      'archive',
      accessToken,
      {
        'Content-Type': 'application/json',
      },
      options.operationId,
    );

    emitFileActionPhase(options, {
      phase: 'request_dispatched',
      operationId,
    });
    const response = await fetch(url.toString(), {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
    const metadata = readArchiveResponseMetadata(response.headers);

    if (!response.ok) {
      const errorText = await response.text().catch(() => 'No error details available');
      const detailMessage = parseApiDetailMessage(errorText);
      const failureKind = classifySandboxArchiveFailure(response.status, detailMessage);

      throw new SandboxArchiveDownloadError(
        sandboxId,
        response.status,
        detailMessage,
        failureKind,
        operationId,
        metadata.requestId,
        metadata.responseSource,
        metadata.identitySource,
        metadata.sandboxAvailable,
        metadata.outcome,
        metadata.fallback,
        `Error downloading sandbox archive: ${detailMessage} (${response.status})`,
      );
    }

    emitFileActionPhase(options, {
      phase: 'response_ok',
      operationId,
      status: response.status,
      ...metadata,
    });
    const filename = parseFilenameFromContentDisposition(
      response.headers.get('content-disposition'),
      `workspace-${sandboxId}.zip`,
    );

    const summary: SandboxArchiveSummary = {
      total: parsePositiveIntHeader(response.headers.get('x-archive-total')),
      succeeded: parsePositiveIntHeader(response.headers.get('x-archive-succeeded')),
      failed: parsePositiveIntHeader(response.headers.get('x-archive-failed')),
    };

    const blob = await response.blob();
    emitFileActionPhase(options, {
      phase: 'blob_ready',
      operationId,
      status: response.status,
      ...metadata,
    });

    return {
      blob,
      filename,
      summary,
      operationId,
      requestId: metadata.requestId,
      responseSource: metadata.responseSource,
      identitySource: metadata.identitySource,
      sandboxAvailable: metadata.sandboxAvailable,
      outcome: metadata.outcome,
      fallback: metadata.fallback,
    };
  } catch (error) {
    console.error('Failed to download sandbox archive:', error);
    throw error;
  }
};

export const fetchSandboxFileContent = async (
  sandboxId: string,
  path: string,
  options: FileActionRequestOptions & {
    responseType?: 'auto' | 'text' | 'blob' | 'json';
  } = {},
): Promise<SandboxFileContentResult> => {
  try {
    const { response, normalizedPath, operationId, metadata } = await fetchSandboxFileResponse(
      sandboxId,
      path,
      'read',
      options,
    );
    const contentType = response.headers.get('content-type');
    const filename = parseFilenameFromContentDisposition(
      response.headers.get('content-disposition'),
      getPathFilename(normalizedPath),
    );
    const responseType = options.responseType || 'auto';

    let data: string | Blob | unknown;
    if (responseType === 'json') {
      data = await response.json();
    } else if (responseType === 'blob') {
      data = await response.blob();
    } else if (
      responseType === 'text' ||
      (responseType === 'auto' &&
        ((contentType && contentType.includes('text')) ||
          contentType?.includes('application/json')))
    ) {
      data = await response.text();
    } else {
      data = await response.blob();
    }

    return {
      data,
      filename,
      contentType,
      operationId,
      requestId: metadata.requestId,
      responseSource: metadata.responseSource,
      fallback: metadata.fallback,
    };
  } catch (error) {
    console.error('Failed to get sandbox file content:', error);
    handleApiError(error, { operation: 'load file content', resource: `file ${path}` });
    throw error;
  }
};

export const getSandboxFileContent = async (
  sandboxId: string,
  path: string,
): Promise<string | Blob> => {
  const result = await fetchSandboxFileContent(sandboxId, path, {
    responseType: 'auto',
  });
  return result.data as string | Blob;
};

export const downloadSandboxFile = async (
  sandboxId: string,
  path: string,
  options: FileActionRequestOptions = {},
): Promise<SandboxFileDownloadResult> => {
  try {
    const { response, normalizedPath, operationId, metadata } = await fetchSandboxFileResponse(
      sandboxId,
      path,
      'download',
      options,
    );
    const filename = parseFilenameFromContentDisposition(
      response.headers.get('content-disposition'),
      getPathFilename(normalizedPath),
    );
    const contentType = normalizeMimeType(
      response.headers.get('content-type'),
      'application/octet-stream',
    );
    const blob = await response.blob();
    const normalizedBlob =
      blob.type === contentType ? blob : new Blob([blob], { type: contentType });
    emitFileActionPhase(options, {
      phase: 'blob_ready',
      operationId,
      status: response.status,
      ...metadata,
    });

    return {
      blob: normalizedBlob,
      filename,
      contentType,
      operationId,
      requestId: metadata.requestId,
      responseSource: metadata.responseSource,
      fallback: metadata.fallback,
    };
  } catch (error) {
    console.error('Failed to download sandbox file:', error);
    handleApiError(error, { operation: 'download file', resource: `file ${path}` });
    throw error;
  }
};

// Function to get public projects
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
    handleApiError(err, { operation: 'load public projects', resource: 'public projects' });
    return [];
  }
};


export const initiateAgent = async (
  formData: FormData,
): Promise<InitiateAgentResponse> => {
  try {
    const accessToken = await getAccessTokenOrThrow();

    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    const response = await fetch(`${API_URL}/agent/initiate`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
      body: formData,
      cache: 'no-store',
    });

    if (!response.ok) {
      // Check for 402 Payment Required first
      if (response.status === 402) {
        try {
          const errorData = await response.json();
          // Ensure detail exists and has a message property
          const detail = errorData?.detail || { message: 'Payment Required' };
          if (typeof detail.message !== 'string') {
            detail.message = 'Payment Required'; // Default message if missing
          }
          throw new BillingError(response.status, detail);
        } catch (parseError) {
          // Handle cases where parsing fails or the structure isn't as expected
          throw new BillingError(
            response.status,
            { message: 'Payment Required' },
            `Error initiating agent: ${response.statusText} (402)`,
          );
        }
      }

      // Check for 429 Too Many Requests (Agent Run Limit)
      if (response.status === 429) {
          const errorData = await response.json();
          // Ensure detail exists and has required properties
          const detail = errorData?.detail || { 
            message: 'Too many agent runs running',
            running_thread_ids: [],
            running_count: 0,
          };
          if (typeof detail.message !== 'string') {
            detail.message = 'Too many agent runs running';
          }
          if (!Array.isArray(detail.running_thread_ids)) {
            detail.running_thread_ids = [];
          }
          if (typeof detail.running_count !== 'number') {
            detail.running_count = 0;
          }
          throw new AgentRunLimitError(response.status, detail);
      }

      // Handle other errors
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      
      console.error(
        `[API] Error initiating agent: ${response.status} ${response.statusText}`,
        errorText,
      );
    
      if (response.status === 401) {
        throw new Error('Authentication error: Please sign in again');
      } else if (response.status >= 500) {
        throw new Error('Server error: Please try again later');
      }
    
      throw new Error(
        `Error initiating agent: ${response.statusText} (${response.status})`,
      );
    }

    const result = await response.json();
    return result;
  } catch (error) {
    // Rethrow BillingError and AgentRunLimitError instances directly
    if (error instanceof BillingError || error instanceof AgentRunLimitError) {
      throw error;
    }

    console.error('[API] Failed to initiate agent:', error);

    if (
      error instanceof TypeError &&
      error.message.includes('Failed to fetch')
    ) {
      const networkError = new Error(
        `Cannot connect to backend server. Please check your internet connection and make sure the backend is running.`,
      );
      handleApiError(networkError, { operation: 'initiate agent', resource: 'AI assistant' });
      throw networkError;
    }
    handleApiError(error, { operation: 'initiate agent' });
    throw error;
  }
};

export const prepareAgentAttachments = async (
  formData: FormData,
): Promise<PrepareAttachmentsResponse> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    if (!API_URL) {
      throw new Error(
        'Backend URL is not configured. Set NEXT_PUBLIC_BACKEND_URL in your environment.',
      );
    }

    const response = await fetch(`${API_URL}/agent/attachments/prepare`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${session.access_token}`,
      },
      body: formData,
      cache: 'no-store',
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      throw new Error(
        `Failed to prepare attachments: ${response.status} ${response.statusText}${errorText ? ` - ${errorText}` : ''}`,
      );
    }

    return response.json();
  } catch (err) {
    handleApiError(err, { operation: 'prepare attachments', resource: 'attachments' });
    throw err;
  }
};

export const checkApiHealth = async (): Promise<HealthCheckResponse> => {
  try {
    const response = await fetch(`${API_URL}/health`, {
      cache: 'no-store',
    });

    if (!response.ok) {
      throw new Error(`API health check failed: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    throw error;
  }
};

// Billing API Types
export interface CreateCheckoutSessionRequest {
  price_id: string;
  success_url: string;
  cancel_url: string;
  referral_id?: string;
  commitment_type?: 'monthly' | 'yearly' | 'yearly_commitment';
}

export interface CreatePortalSessionRequest {
  return_url: string;
}

export interface SubscriptionStatus {
  status: string; // Includes 'active', 'trialing', 'past_due', 'scheduled_downgrade', 'no_subscription'
  plan_name?: string;
  price_id?: string;
  current_period_end?: string; // ISO datetime string
  cancel_at_period_end?: boolean;
  trial_end?: string; // ISO datetime string
  minutes_limit?: number;
  cost_limit?: number;
  current_usage?: number;
  // Fields for scheduled changes
  has_schedule?: boolean;
  scheduled_plan_name?: string;
  scheduled_price_id?: string;
  scheduled_change_date?: string; // ISO datetime string
  // Subscription data for frontend components
  subscription_id?: string;
  subscription?: {
    id: string;
    status: string;
    cancel_at_period_end: boolean;
    current_period_end: number; // timestamp
  };
}

export interface CommitmentInfo {
  has_commitment: boolean;
  commitment_type?: string;
  months_remaining?: number;
  can_cancel: boolean;
  commitment_end_date?: string;
}

// Interface for user subscription details from Stripe
export interface UserSubscriptionResponse {
  subscription?: {
    id: string;
    status: string;
    current_period_end: number;
    current_period_start: number;
    cancel_at_period_end: boolean;
    cancel_at?: number;
    items: {
      data: Array<{
        id: string;
        price: {
          id: string;
          unit_amount: number;
          currency: string;
          recurring: {
            interval: string;
            interval_count: number;
          };
        };
        quantity: number;
      }>;
    };
    metadata: {
      [key: string]: string;
    };
  };
  price_id?: string;
  plan_name?: string;
  status?: string;
  has_schedule?: boolean;
  scheduled_price_id?: string;
  current_period_end?: number;
  current_period_start?: number;
  cancel_at_period_end?: boolean;
  cancel_at?: number;
  customer_email?: string;
  usage?: {
    total_usage: number;
    limit: number;
  };
}

// Usage log entry interface
export interface UsageLogEntry {
  message_id: string;
  thread_id: string;
  created_at: string;
  content: {
    usage: {
      prompt_tokens: number;
      completion_tokens: number;
    };
    model: string;
  };
  total_tokens: number;
  estimated_cost: number;
  project_id: string;
}

// Usage logs response interface
export interface UsageLogsResponse {
  logs: UsageLogEntry[];
  has_more: boolean;
  message?: string;
}

export interface BillingStatusResponse {
  can_run: boolean;
  message: string;
  subscription: {
    price_id: string;
    plan_name: string;
    minutes_limit?: number;
  };
}

export interface Model {
  id: string;
  display_name: string;
  short_name?: string;
  requires_subscription?: boolean;
  is_available?: boolean;
  input_cost_per_million_tokens?: number | null;
  output_cost_per_million_tokens?: number | null;
  max_tokens?: number | null;
}

export interface AvailableModelsResponse {
  models: Model[];
  subscription_tier: string;
  total_models: number;
}

export interface CreateCheckoutSessionResponse {
  status:
    | 'upgraded'
    | 'downgrade_scheduled'
    | 'checkout_created'
    | 'no_change'
    | 'new'
    | 'updated'
    | 'scheduled'
    | 'commitment_created'
    | 'commitment_blocks_downgrade';
  subscription_id?: string;
  schedule_id?: string;
  session_id?: string;
  url?: string;
  effective_date?: string;
  message?: string;
  details?: {
    is_upgrade?: boolean;
    effective_date?: string;
    current_price?: number;
    new_price?: number;
    commitment_end_date?: string;
    months_remaining?: number;
    invoice?: {
      id: string;
      status: string;
      amount_due: number;
      amount_paid: number;
    };
  };
}

export interface CancelSubscriptionResponse {
  success: boolean;
  status: 'cancelled_at_period_end' | 'commitment_prevents_cancellation';
  message: string;
  details?: {
    subscription_id?: string;
    cancellation_effective_date?: string;
    current_period_end?: number;
    access_until?: string;
    months_remaining?: number;
    commitment_end_date?: string;
    can_cancel_after?: string;
  };
}

export interface ReactivateSubscriptionResponse {
  success: boolean;
  status: 'reactivated' | 'not_cancelled';
  message: string;
  details?: {
    subscription_id?: string;
    next_billing_date?: string;
  };
}

// Billing API Functions
export const createCheckoutSession = async (
  request: CreateCheckoutSessionRequest,
): Promise<CreateCheckoutSessionResponse> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }
    
    
    const requestBody = { ...request, tolt_referral: window.tolt_referral };
    
    const response = await fetch(`${API_URL}/billing/create-checkout-session`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error creating checkout session: ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error creating checkout session: ${response.statusText} (${response.status})`,
      );
    }

    const data = await response.json();
    switch (data.status) {
      case 'upgraded':
      case 'updated':
      case 'downgrade_scheduled':
      case 'scheduled':
      case 'no_change':
        return data;
      case 'new':
      case 'checkout_created':
        if (!data.url) {
          throw new Error('No checkout URL provided');
        }
        return data;
      default:
        console.warn(
          'Unexpected status from createCheckoutSession:',
          data.status,
        );
        return data;
    }
  } catch (error) {
    console.error('Failed to create checkout session:', error);
    handleApiError(error, { operation: 'create checkout session', resource: 'billing' });
    throw error;
  }
};


export const createPortalSession = async (
  request: CreatePortalSessionRequest,
): Promise<{ url: string }> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    const response = await fetch(`${API_URL}/billing/create-portal-session`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error creating portal session: ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error creating portal session: ${response.statusText} (${response.status})`,
      );
    }

    return response.json();
  } catch (error) {
    console.error('Failed to create portal session:', error);
    handleApiError(error, { operation: 'create portal session', resource: 'billing portal' });
    throw error;
  }
};


export const getSubscription = async (): Promise<SubscriptionStatus> => {
  // 暂时不调用后端API，直接返回默认的无订阅状态
  console.log('Billing API temporarily disabled - returning default subscription status');
  return {
    status: 'no_subscription',
    plan_name: undefined,
    price_id: undefined,
    current_period_end: undefined,
    cancel_at_period_end: undefined,
    trial_end: undefined,
    minutes_limit: undefined,
    cost_limit: undefined,
    current_usage: 0
  };
};

export const getSubscriptionCommitment = async (subscriptionId: string): Promise<CommitmentInfo> => {
  // 暂时不调用后端API，直接返回默认的承诺信息
  console.log('Billing commitment API temporarily disabled - returning default commitment info');
  return {
    has_commitment: false,
    commitment_type: undefined,
    months_remaining: undefined,
    can_cancel: true,
    commitment_end_date: undefined
  };
};

export const getAvailableModels = async (): Promise<AvailableModelsResponse> => {
  // 暂时不调用后端API，直接返回默认的模型列表
  console.log('Available models API temporarily disabled - returning default models');
  return {
    models: [
      {
        id: 'openrouter/anthropic/claude-sonnet-4.5',
        short_name: 'openrouter/anthropic/claude-sonnet-4.5',
        display_name: 'Claude Sonnet 4.5',
        created_by: 'OpenRouter',
        model_id: 'openrouter/anthropic/claude-sonnet-4.5',
        requires_subscription: false,
        vision_enabled: true,
        input_cost_per_million_tokens: 0.000003000,
        output_cost_per_million_tokens: 0.000015000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'openrouter/google/gemini-3.1-pro-preview',
        short_name: 'openrouter/google/gemini-3.1-pro-preview',
        display_name: 'Gemini 3.1 Pro (Preview)',
        created_by: 'OpenRouter',
        model_id: 'openrouter/google/gemini-3.1-pro-preview',
        requires_subscription: false,
        vision_enabled: true,
        input_cost_per_million_tokens: 0.000001250,
        output_cost_per_million_tokens: 0.000010000,
        supports_thinking: false,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'openrouter/google/gemini-3-flash-preview',
        short_name: 'openrouter/google/gemini-3-flash-preview',
        display_name: 'Gemini 3 Flash (Preview)',
        created_by: 'OpenRouter',
        model_id: 'openrouter/google/gemini-3-flash-preview',
        requires_subscription: false,
        vision_enabled: true,
        input_cost_per_million_tokens: 0.000000150,
        output_cost_per_million_tokens: 0.000000600,
        supports_thinking: false,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'deepseek-chat',
        short_name: 'deepseek-chat',
        display_name: 'DeepSeek Chat',
        created_by: 'DeepSeek',
        model_id: 'deepseek-chat',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000000150,
        output_cost_per_million_tokens: 0.000000600,
        supports_thinking: false,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'openrouter/moonshotai/kimi-k2.5',
        short_name: 'openrouter/moonshotai/kimi-k2.5',
        display_name: 'Kimi K2.5',
        created_by: 'OpenRouter',
        model_id: 'moonshotai/kimi-k2.5',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000004000,
        output_cost_per_million_tokens: 0.000021000,
        supports_thinking: true,
        max_thinking_tokens: 32768,
        is_available: true
      },
      {
        id: 'kimi-k2.5',
        short_name: 'kimi-k2.5',
        display_name: 'Kimi K2.5 (Moonshot Official)',
        created_by: 'Moonshot',
        model_id: 'kimi-k2.5',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000004000,
        output_cost_per_million_tokens: 0.000021000,
        supports_thinking: true,
        max_thinking_tokens: 32768,
        is_available: true
      },
      {
        id: 'openrouter/moonshotai/kimi-k2.6',
        short_name: 'openrouter/moonshotai/kimi-k2.6',
        display_name: 'Kimi K2.6 (OpenRouter)',
        created_by: 'OpenRouter',
        model_id: 'moonshotai/kimi-k2.6',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000004000,
        output_cost_per_million_tokens: 0.000021000,
        supports_thinking: true,
        max_thinking_tokens: 32768,
        is_available: true
      },
      {
        id: 'kimi-k2.6',
        short_name: 'kimi-k2.6',
        display_name: 'Kimi K2.6 (Moonshot Official)',
        created_by: 'Moonshot',
        model_id: 'kimi-k2.6',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000004000,
        output_cost_per_million_tokens: 0.000021000,
        supports_thinking: true,
        max_thinking_tokens: 32768,
        is_available: true
      },
      {
        id: 'openrouter/xiaomi/mimo-v2.5-pro',
        short_name: 'openrouter/xiaomi/mimo-v2.5-pro',
        display_name: 'MiMo V2.5 Pro (OpenRouter)',
        created_by: 'OpenRouter',
        model_id: 'xiaomi/mimo-v2.5-pro',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'openrouter/deepseek/deepseek-v4-pro',
        short_name: 'openrouter/deepseek/deepseek-v4-pro',
        display_name: 'DeepSeek V4 Pro (OpenRouter)',
        created_by: 'OpenRouter',
        model_id: 'deepseek/deepseek-v4-pro',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'openrouter/deepseek/deepseek-v4-flash',
        short_name: 'openrouter/deepseek/deepseek-v4-flash',
        display_name: 'DeepSeek V4 Flash (OpenRouter)',
        created_by: 'OpenRouter',
        model_id: 'deepseek/deepseek-v4-flash',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'deepseek-v4-pro-high',
        short_name: 'deepseek-v4-pro-high',
        display_name: 'DeepSeek V4 Pro High (Official)',
        created_by: 'DeepSeek',
        model_id: 'deepseek-v4-pro-high',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'deepseek-v4-pro-max',
        short_name: 'deepseek-v4-pro-max',
        display_name: 'DeepSeek V4 Pro Max (Official)',
        created_by: 'DeepSeek',
        model_id: 'deepseek-v4-pro-max',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'deepseek-v4-flash-high',
        short_name: 'deepseek-v4-flash-high',
        display_name: 'DeepSeek V4 Flash High (Official)',
        created_by: 'DeepSeek',
        model_id: 'deepseek-v4-flash-high',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'deepseek-v4-flash-max',
        short_name: 'deepseek-v4-flash-max',
        display_name: 'DeepSeek V4 Flash Max (Official)',
        created_by: 'DeepSeek',
        model_id: 'deepseek-v4-flash-max',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'openrouter/minimax/minimax-m2.1',
        short_name: 'openrouter/minimax/minimax-m2.1',
        display_name: 'Minimax M2.1',
        created_by: 'OpenRouter',
        model_id: 'openrouter/minimax/minimax-m2.1',
        requires_subscription: false,
        vision_enabled: false,
        input_cost_per_million_tokens: 0.000001000,
        output_cost_per_million_tokens: 0.000003000,
        supports_thinking: false,
        max_thinking_tokens: 0,
        is_available: true
      },
      {
        id: 'dashscope/qwen3.5-plus',
        short_name: 'dashscope/qwen3.5-plus',
        display_name: 'Qwen 3.5 Plus (DashScope)',
        created_by: 'DashScope',
        model_id: 'dashscope/qwen3.5-plus',
        requires_subscription: false,
        vision_enabled: true,
        input_cost_per_million_tokens: 0.000000130,
        output_cost_per_million_tokens: 0.000000600,
        supports_thinking: true,
        max_thinking_tokens: 32768,
        is_available: true
      },
      {
        id: 'openrouter/z-ai/glm-4.7',
        short_name: 'openrouter/z-ai/glm-4.7',
        display_name: 'GLM 4.7',
        created_by: 'OpenRouter',
        model_id: 'openrouter/z-ai/glm-4.7',
        requires_subscription: false,
        vision_enabled: true,
        input_cost_per_million_tokens: 0.000000400,
        output_cost_per_million_tokens: 0.000001500,
        supports_thinking: true,
        max_thinking_tokens: 0,
        is_available: true
      }
    ],
    subscription_tier: 'free',
    total_models: 18
  };
};


export const checkBillingStatus = async (): Promise<BillingStatusResponse> => {
  // 暂时不调用后端API，直接返回默认的计费状态
  console.log('Billing status API temporarily disabled - returning default status');
  return {
    status: 'healthy',
    message: 'Billing system is temporarily disabled',
    limits: {
      daily_cost_limit: 1000,
      monthly_cost_limit: 10000,
      daily_usage: 0,
      monthly_usage: 0
    },
    subscription: {
      status: 'no_subscription',
      plan_name: 'Free',
      current_period_end: null
    }
  };
};

export const cancelSubscription = async (): Promise<CancelSubscriptionResponse> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    const response = await fetch(`${API_URL}/billing/cancel-subscription`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error cancelling subscription: ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error cancelling subscription: ${response.statusText} (${response.status})`,
      );
    }

    return response.json();
  } catch (error) {
    console.error('Failed to cancel subscription:', error);
    handleApiError(error, { operation: 'cancel subscription', resource: 'subscription' });
    throw error;
  }
};

// 🔧 添加缺失的 reactivateSubscription 函数
export const reactivateSubscription = async (): Promise<ReactivateSubscriptionResponse> => {
  try {
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();

    if (!session?.access_token) {
      throw new NoAccessTokenAvailableError();
    }

    const response = await fetch(`${API_URL}/billing/reactivate-subscription`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
    });

    if (!response.ok) {
      const errorText = await response
        .text()
        .catch(() => 'No error details available');
      console.error(
        `Error reactivating subscription: ${response.status} ${response.statusText}`,
        errorText,
      );
      throw new Error(
        `Error reactivating subscription: ${response.statusText} (${response.status})`,
      );
    }

    return response.json();
  } catch (error) {
    console.error('Failed to reactivate subscription:', error);
    handleApiError(error, { operation: 'reactivate subscription', resource: 'subscription' });
    throw error;
  }
};

// Transcription API Types
export interface TranscriptionResponse {
  text: string;
}

// Transcription API Functions
export const transcribeAudio = async (audioFile: File): Promise<TranscriptionResponse> => {
  // 暂时不调用后端API，直接返回模拟的转录结果
  console.log('Transcription API temporarily disabled - returning mock transcription');
  return {
    text: "This is a mock transcription result. Please implement the actual transcription API."
  };
};
