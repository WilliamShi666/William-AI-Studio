'use client';

import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import type { UnifiedMessage } from '@/components/thread/types';
import {
  confirmShadowClone,
  denyShadowClone,
  getShadowCloneFullResult,
  getShadowCloneResults,
  getShadowCloneStatus,
  type ShadowCloneActivityOwner,
  type ShadowCloneLiveActivity as ApiShadowCloneLiveActivity,
    type ShadowCloneStatusDependency,
  type ShadowCloneStatusProposalSubtask,
  type ShadowCloneResultSummary,
  type ShadowCloneStatusResponse,
  type ShadowCloneUiPhase,
} from '@/lib/api';
import {
  applyShadowCloneActivity,
  buildShadowCloneCompleteToolCall,
  createEmptyShadowClonePanelState,
  type ShadowCloneActivityEnvelope,
  type ShadowClonePanelState,
  type ShadowClonePanelToolCall,
} from '@/lib/shadow-clone-panel';
import {
  applyShadowCloneTranscriptActivity,
  applyShadowCloneTranscriptMessage,
  createEmptyShadowCloneTranscriptState,
  type ShadowCloneTranscriptState,
  upsertSyntheticCompleteTranscriptMessage,
} from '@/lib/shadow-clone-transcript';

export type ShadowCloneMode = 'off' | 'auto' | 'on';
export type ShadowClonePhase =
  | 'idle'
  | 'planning'
  | 'preparing'
  | 'confirming'
  | 'recovering'
  | 'running'
  | 'aggregating'
  | 'completed'
  | 'cancelled'
  | 'timeout'
  | 'denied'
  | 'error';
export type ShadowCloneEnvironmentStatus =
  | 'pending'
  | 'preparing'
  | 'ready'
  | 'recovering'
  | 'failed';
export type ShadowCloneSubtaskStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timeout'
  | 'denied';
export type ShadowCloneViewScope =
  | 'shadow_clone_main'
  | 'shadow_clone_subagent';
export type ShadowCloneLiveScope = 'shadow_clone_main' | 'main_agent';
export type ShadowCloneLivePhase =
  | 'planning'
  | 'confirming'
  | 'execution'
  | 'aggregate'
  | 'completed';
export type ShadowCloneLiveActivity = Required<
  Pick<ApiShadowCloneLiveActivity, 'scope' | 'phase'>
> &
  Pick<ApiShadowCloneLiveActivity, 'reason' | 'subtask_id' | 'epoch' | 'updated_at'>;

interface ShadowCloneSyncOptions {
  force?: boolean;
}

export interface ShadowCloneSubtask {
  id: string;
  role: string;
  task_description: string;
  status: ShadowCloneSubtaskStatus;
  agent_name?: string;
  agent_id?: string;
  agent_status?: string;
  idle_since?: string | null;
  idle_expires_at?: string | null;
  wake_reason?: string | null;
  wake_message_id?: string | null;
  shutdown_reason?: string | null;
  source?: 'claude_sdk';
  error?: string;
  result_summary?: string;
  submitted_at?: string;
  started_at?: string;
  finished_at?: string;
  attempt_index?: number;
  failure_class?: string | null;
  recovery_mode?: string | null;
  recovery_phase?: string | null;
  recovery_reason?: string | null;
  recovery_handoff_summary?: string | null;
  replacement_context_id?: string | null;
}

export interface SubagentInspectionStreamState {
  status: string;
  textContent: string;
  reasoningContent: string;
  isWritingFile: boolean;
  messages: UnifiedMessage[];
}

interface ShadowCloneDependency {
  from_id: string;
  to_id: string;
}

interface ShadowCloneStoreState {
  mode: ShadowCloneMode;
  modeExplicitlySet: boolean;
  phase: ShadowClonePhase;
  currentRunId: string | null;
  environmentStatus: ShadowCloneEnvironmentStatus | null;
  environmentReady: boolean;
  environmentLastError: string | null;
  environmentPreparedAt: string | null;
  environmentConfirmationReady: boolean;
  environmentManifest: Record<string, unknown> | null;
  sandboxId: string | null;
  sandboxType: string | null;
  sandboxBindingState: string | null;
  liveActivity: ShadowCloneLiveActivity | null;
  viewScope: ShadowCloneViewScope;
  activeSubtaskId: string | null;
  subtasks: ShadowCloneSubtask[];
  dependencies: ShadowCloneDependency[];
  pendingCount: number;
  lastHeartbeatAt: number | null;
  lastError: string | null;
  detailResults: Record<string, string>;
  detailError: string | null;
  detailLoadingId: string | null;
  subtaskPanelStates: Record<string, ShadowClonePanelState>;
  mainTranscriptState: ShadowCloneTranscriptState;
  subtaskTranscriptStates: Record<string, ShadowCloneTranscriptState>;
  // Per-subagent inspection streaming state (SCV2 parity spec FR-005–008)
  inspectionStreamStates: Record<string, SubagentInspectionStreamState>;
  isSyncing: boolean;
  lastSyncedAt: number | null;
  setMode: (mode: ShadowCloneMode) => void;
  setActiveSubtask: (subtaskId: string | null) => void;
  clearActiveSubtask: () => void;
  returnToMainView: () => void;
  resetRuntime: () => void;
  applyLiveActivity: (
    activity: ApiShadowCloneLiveActivity | null | undefined,
    agentRunId?: string | null,
  ) => void;
  applyMainTranscriptMessage: (
    message: UnifiedMessage,
    agentRunId?: string | null,
    liveActivity?: unknown,
  ) => void;
  handleSSEEvent: (
    streamStatus: string,
    content: any,
    liveActivity?: unknown,
    agentRunId?: string | null,
  ) => void;
  syncRunData: (agentRunId: string, options?: ShadowCloneSyncOptions) => Promise<void>;
  selectSubtask: (agentRunId: string, subtaskId: string) => Promise<void>;
  fetchSubtaskResult: (agentRunId: string, subtaskId: string) => Promise<void>;
  confirmProposal: (agentRunId: string) => Promise<void>;
  denyProposal: (agentRunId: string, reason?: string) => Promise<void>;
  // Subagent inspection streaming (SCV2 parity spec)
  initSubagentInspection: (subtaskId: string) => void;
  applySubagentStreamEvent: (subtaskId: string, event: { status?: string; textContent?: string; reasoningContent?: string; isWritingFile?: boolean; message?: UnifiedMessage }) => void;
  clearSubagentInspection: (subtaskId: string) => void;
}

const resetRuntimeState = {
  phase: 'idle' as ShadowClonePhase,
  currentRunId: null as string | null,
  environmentStatus: null as ShadowCloneEnvironmentStatus | null,
  environmentReady: false,
  environmentLastError: null as string | null,
  environmentPreparedAt: null as string | null,
  environmentConfirmationReady: false,
  environmentManifest: null as Record<string, unknown> | null,
  sandboxId: null as string | null,
  sandboxType: null as string | null,
  sandboxBindingState: null as string | null,
  liveActivity: null as ShadowCloneLiveActivity | null,
  viewScope: 'shadow_clone_main' as ShadowCloneViewScope,
  activeSubtaskId: null as string | null,
  subtasks: [] as ShadowCloneSubtask[],
  dependencies: [] as ShadowCloneDependency[],
  pendingCount: 0,
  lastHeartbeatAt: null as number | null,
  lastError: null as string | null,
  detailResults: {} as Record<string, string>,
  detailError: null as string | null,
  detailLoadingId: null as string | null,
  subtaskPanelStates: {} as Record<string, ShadowClonePanelState>,
  mainTranscriptState: createEmptyShadowCloneTranscriptState(),
  subtaskTranscriptStates: {} as Record<string, ShadowCloneTranscriptState>,
  inspectionStreamStates: {} as Record<string, SubagentInspectionStreamState>,
  isSyncing: false,
  lastSyncedAt: null as number | null,
};

const resetRuntimeStateWithHistoricalV2Transcripts = (
  state: Pick<ShadowCloneStoreState, 'subtaskTranscriptStates'>,
) => ({
  ...resetRuntimeState,
  subtaskTranscriptStates: retainV2ProjectionOutputTranscriptMessages(
    state.subtaskTranscriptStates,
  ),
});

const derivePendingCount = (subtasks: ShadowCloneSubtask[]): number =>
  subtasks.reduce(
    (count, subtask) => (subtask.status === 'pending' ? count + 1 : count),
    0,
  );

const deriveViewScope = (
  activeSubtaskId: string | null,
): ShadowCloneViewScope =>
  activeSubtaskId ? 'shadow_clone_subagent' : 'shadow_clone_main';

const isTerminalShadowClonePhase = (
  phase: ShadowClonePhase,
): boolean =>
  phase === 'completed' ||
  phase === 'cancelled' ||
  phase === 'timeout' ||
  phase === 'denied' ||
  phase === 'error';

const isPostExecutionShadowClonePhase = (
  phase: ShadowClonePhase,
): boolean => phase === 'aggregating' || isTerminalShadowClonePhase(phase);

const reconcileActiveSubtaskId = (
  activeSubtaskId: string | null,
  subtasks: ShadowCloneSubtask[],
): string | null =>
  activeSubtaskId && subtasks.some((item) => item.id === activeSubtaskId)
    ? activeSubtaskId
    : null;

const preserveInspectionSubtaskId = (
  activeSubtaskId: string | null,
  viewScope: ShadowCloneViewScope,
  subtasks: ShadowCloneSubtask[],
  shouldPreserveMissingSubtask: boolean,
): string | null => {
  if (viewScope === 'shadow_clone_subagent' && activeSubtaskId) {
    if (
      shouldPreserveMissingSubtask ||
      subtasks.some((item) => item.id === activeSubtaskId)
    ) {
      return activeSubtaskId;
    }
  }
  return reconcileActiveSubtaskId(activeSubtaskId, subtasks);
};

const normalizeSubtaskStatus = (
  value: unknown,
  fallback: ShadowCloneSubtaskStatus = 'pending',
): ShadowCloneSubtaskStatus => {
  switch (String(value || '').trim().toLowerCase()) {
    case 'pending':
    case 'starting':
      return 'pending';
    case 'running':
    case 'working':
    case 'in_progress':
      return 'running';
    case 'idle':
    case 'completed':
      return 'completed';
    case 'failed':
    case 'error':
      return 'failed';
    case 'cancelled':
    case 'stopped':
      return 'cancelled';
    case 'timeout':
      return 'timeout';
    case 'denied':
      return 'denied';
    default:
      return fallback;
  }
};

const SHADOW_CLONE_STORE_VERSION = 1;

type ShadowClonePersistedState = Pick<
  ShadowCloneStoreState,
  'mode' | 'modeExplicitlySet'
>;

const isShadowCloneMode = (value: unknown): value is ShadowCloneMode =>
  value === 'off' || value === 'auto' || value === 'on';

const normalizePersistedShadowClonePreference = (
  persistedState: unknown,
): ShadowClonePersistedState => {
  const persisted =
    persistedState && typeof persistedState === 'object'
      ? (persistedState as Partial<ShadowClonePersistedState>)
      : {};
  const mode = isShadowCloneMode(persisted.mode) ? persisted.mode : null;
  const modeExplicitlySet =
    typeof persisted.modeExplicitlySet === 'boolean'
      ? persisted.modeExplicitlySet
      : null;

  if (mode && modeExplicitlySet !== null) {
    return {
      mode,
      modeExplicitlySet,
    };
  }

  if (mode === 'on' || mode === 'off') {
    return {
      mode,
      modeExplicitlySet: true,
    };
  }

  return {
    mode: 'off',
    modeExplicitlySet: false,
  };
};

const coercePersistedShadowCloneState = (
  persistedState: unknown,
): Record<string, unknown> =>
  persistedState && typeof persistedState === 'object'
    ? (persistedState as Record<string, unknown>)
    : {};

const normalizePhase = (
  status: ShadowCloneStatusResponse['status'] | undefined,
  fallback: ShadowClonePhase,
  environmentStatus?: ShadowCloneEnvironmentStatus,
): ShadowClonePhase => {
  switch (status) {
    case 'aggregating':
      return fallback === 'completed' ? 'completed' : 'aggregating';
    case 'completed':
      return 'completed';
    case 'cancelled':
      return 'cancelled';
    case 'timeout':
      return 'timeout';
    case 'denied':
      return 'denied';
    case 'failed':
      return 'error';
    default:
      break;
  }

  const isTerminalOrPostExecutionPhase =
    isPostExecutionShadowClonePhase(fallback);

  switch (environmentStatus) {
    case 'preparing':
      if (isTerminalOrPostExecutionPhase) {
        return fallback;
      }
      if (fallback === 'recovering') {
        return 'recovering';
      }
      return fallback === 'running' ? 'running' : 'preparing';
    case 'recovering':
      return isTerminalOrPostExecutionPhase ? fallback : 'recovering';
    case 'ready':
      if (isTerminalOrPostExecutionPhase) {
        return fallback;
      }
      if (status === 'running' || fallback === 'running' || fallback === 'recovering') {
        return 'running';
      }
      return 'confirming';
    default:
      break;
  }

  switch (status) {
    case 'confirming':
      return fallback === 'idle' || fallback === 'planning'
        ? 'confirming'
        : fallback;
    case 'pending':
      return fallback === 'idle' ? 'planning' : fallback;
    case 'running':
      return isPostExecutionShadowClonePhase(fallback) ||
        fallback === 'recovering'
        ? fallback
        : 'running';
    case 'not_found':
    default:
      return fallback;
  }
};

const SHADOW_CLONE_LIVE_SCOPES: ShadowCloneLiveScope[] = [
  'shadow_clone_main',
  'main_agent',
];

const SHADOW_CLONE_LIVE_PHASES: ShadowCloneLivePhase[] = [
  'planning',
  'confirming',
  'execution',
  'aggregate',
  'completed',
];

const SHADOW_CLONE_CANONICAL_ACTIVITY_OWNERS: ShadowCloneActivityOwner[] = [
  'shadow_clone',
  'main_agent',
  'none',
];

const SHADOW_CLONE_CANONICAL_UI_PHASES: ShadowCloneUiPhase[] = [
  'planning',
  'preparing_environment',
  'confirming',
  'subagents_running',
  'recovering',
  'main_agent_continuation',
  'aggregating',
  'completed',
  'failed',
  'cancelled',
  'timeout',
  'denied',
];

const buildLiveActivity = (
  scope: ShadowCloneLiveScope,
  phase: ShadowCloneLivePhase,
  reason?: string | null,
  epoch?: number,
): ShadowCloneLiveActivity => ({
  scope,
  phase,
  reason: reason || null,
  subtask_id: null,
  epoch,
  updated_at: null,
});

const normalizeLiveActivity = (
  value: unknown,
): ShadowCloneLiveActivity | null => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }

  const candidate = value as Record<string, unknown>;
  const scope = SHADOW_CLONE_LIVE_SCOPES.find(
    (item) => candidate.scope === item,
  );
  const phase = SHADOW_CLONE_LIVE_PHASES.find(
    (item) => candidate.phase === item,
  );

  if (!scope || !phase) {
    return null;
  }

  return {
    scope,
    phase,
    reason:
      typeof candidate.reason === 'string' && candidate.reason.trim().length > 0
        ? candidate.reason
        : null,
    subtask_id:
      typeof candidate.subtask_id === 'string' && candidate.subtask_id.trim().length > 0
        ? candidate.subtask_id
        : null,
    epoch:
      typeof candidate.epoch === 'number' && Number.isFinite(candidate.epoch)
        ? candidate.epoch
        : undefined,
    updated_at:
      typeof candidate.updated_at === 'string' && candidate.updated_at.trim().length > 0
        ? candidate.updated_at
        : null,
  };
};

const resolveLiveActivity = (
  provided: unknown,
  fallback: ShadowCloneLiveActivity | null,
): ShadowCloneLiveActivity | null =>
  normalizeLiveActivity(provided) || fallback;

const resolveStableLiveActivity = (
  provided: unknown,
  fallback: ShadowCloneLiveActivity | null,
  current: ShadowCloneLiveActivity | null,
): ShadowCloneLiveActivity | null => {
  const next = resolveLiveActivity(provided, fallback);
  return sameLiveActivity(next, current) ? current : next;
};

const sameLiveActivity = (
  left: ShadowCloneLiveActivity | null,
  right: ShadowCloneLiveActivity | null,
): boolean =>
  left?.scope === right?.scope &&
  left?.phase === right?.phase &&
  left?.reason === right?.reason &&
  left?.subtask_id === right?.subtask_id &&
  left?.epoch === right?.epoch;

const normalizeCanonicalActivityOwner = (
  value: unknown,
): ShadowCloneActivityOwner | null =>
  SHADOW_CLONE_CANONICAL_ACTIVITY_OWNERS.find((item) => item === value) || null;

const normalizeCanonicalUiPhase = (
  value: unknown,
): ShadowCloneUiPhase | null =>
  SHADOW_CLONE_CANONICAL_UI_PHASES.find((item) => item === value) || null;

const inferCanonicalActivityOwnerFromUiPhase = (
  uiPhase: ShadowCloneUiPhase,
): ShadowCloneActivityOwner => {
  switch (uiPhase) {
    case 'planning':
    case 'preparing_environment':
    case 'confirming':
    case 'subagents_running':
    case 'recovering':
      return 'shadow_clone';
    case 'main_agent_continuation':
    case 'aggregating':
      return 'main_agent';
    case 'completed':
    case 'failed':
    case 'cancelled':
    case 'timeout':
    case 'denied':
    default:
      return 'none';
  }
};

const mapCanonicalUiPhaseToStorePhase = (
  uiPhase: ShadowCloneUiPhase,
): ShadowClonePhase => {
  switch (uiPhase) {
    case 'planning':
      return 'planning';
    case 'preparing_environment':
      return 'preparing';
    case 'confirming':
      return 'confirming';
    case 'subagents_running':
      return 'running';
    case 'recovering':
      return 'recovering';
    case 'main_agent_continuation':
    case 'aggregating':
      return 'aggregating';
    case 'completed':
      return 'completed';
    case 'denied':
      return 'denied';
    case 'failed':
      return 'error';
    case 'cancelled':
      return 'cancelled';
    case 'timeout':
      return 'timeout';
    default:
      return 'idle';
  }
};

const mapCanonicalUiPhaseToLivePhase = (
  uiPhase: ShadowCloneUiPhase,
): ShadowCloneLivePhase | null => {
  switch (uiPhase) {
    case 'planning':
    case 'preparing_environment':
      return 'planning';
    case 'confirming':
      return 'confirming';
    case 'subagents_running':
    case 'recovering':
      return 'execution';
    case 'main_agent_continuation':
    case 'aggregating':
      return 'aggregate';
    case 'completed':
    case 'failed':
    case 'cancelled':
    case 'timeout':
    case 'denied':
    default:
      return null;
  }
};

const normalizeCanonicalPhaseReason = (value: unknown): string | null =>
  typeof value === 'string' && value.trim().length > 0 ? value : null;

const normalizeCanonicalStatusContract = (
  statusPayload: ShadowCloneStatusResponse,
): {
  phase: ShadowClonePhase;
  liveActivity: ShadowCloneLiveActivity | null;
} | null => {
  const uiPhase = normalizeCanonicalUiPhase(statusPayload.ui_phase);
  if (!uiPhase) {
    return null;
  }

  const activityOwner =
    normalizeCanonicalActivityOwner(statusPayload.activity_owner) ||
    inferCanonicalActivityOwnerFromUiPhase(uiPhase);
  const livePhase = mapCanonicalUiPhaseToLivePhase(uiPhase);
  const phaseReason = normalizeCanonicalPhaseReason(statusPayload.phase_reason);

  return {
    phase: mapCanonicalUiPhaseToStorePhase(uiPhase),
    liveActivity:
      activityOwner === 'none' || !livePhase
        ? null
        : buildLiveActivity(
            activityOwner === 'shadow_clone' ? 'shadow_clone_main' : 'main_agent',
            livePhase,
            phaseReason,
          ),
  };
};

const inferLiveActivityFromStatus = (
  statusPayload: ShadowCloneStatusResponse,
  subtasks: ShadowCloneSubtask[],
): ShadowCloneLiveActivity | null => {
  const explicit = normalizeLiveActivity(statusPayload.live_activity);
  if (explicit) {
    return explicit;
  }

  const hasShadowCloneContext =
    subtasks.length > 0 ||
    Boolean(statusPayload.proposal?.subtasks && statusPayload.proposal.subtasks.length > 0);

  if (!hasShadowCloneContext) {
    return null;
  }

  switch (statusPayload.status) {
    case 'pending':
      return buildLiveActivity('shadow_clone_main', 'planning', 'status_pending');
    case 'confirming':
      return buildLiveActivity('shadow_clone_main', 'confirming', 'status_confirming');
    case 'running':
      return buildLiveActivity('shadow_clone_main', 'execution', 'status_running');
    case 'aggregating':
      return buildLiveActivity('main_agent', 'aggregate', 'status_aggregating');
    case 'completed':
    case 'denied':
    case 'failed':
      return buildLiveActivity('main_agent', 'completed', 'status_completed');
    case 'cancelled':
    case 'timeout':
      return null;
    default:
      return null;
  }
};

const readEnvironmentStatus = (
  statusPayload: ShadowCloneStatusResponse,
): ShadowCloneEnvironmentStatus | null =>
  typeof statusPayload.environment?.status === 'string'
    ? statusPayload.environment.status
    : typeof statusPayload.sandbox?.environment_status === 'string'
      ? (statusPayload.sandbox.environment_status as ShadowCloneEnvironmentStatus)
      : null;

const readEnvironmentReady = (
  statusPayload: ShadowCloneStatusResponse,
): boolean => {
  if (typeof statusPayload.environment?.ready === 'boolean') {
    return statusPayload.environment.ready;
  }
  if (typeof statusPayload.sandbox?.environment_ready === 'boolean') {
    return statusPayload.sandbox.environment_ready;
  }
  return readEnvironmentStatus(statusPayload) === 'ready';
};

const readEnvironmentLastError = (
  statusPayload: ShadowCloneStatusResponse,
): string | null =>
  typeof statusPayload.environment?.last_error === 'string' &&
  statusPayload.environment.last_error.trim().length > 0
    ? statusPayload.environment.last_error
    : null;

const readEnvironmentPreparedAt = (
  statusPayload: ShadowCloneStatusResponse,
): string | null =>
  typeof statusPayload.environment?.prepared_at === 'string' &&
  statusPayload.environment.prepared_at.trim().length > 0
    ? statusPayload.environment.prepared_at
    : null;

const readEnvironmentConfirmationReady = (
  statusPayload: ShadowCloneStatusResponse,
): boolean => {
  const manifest = statusPayload.environment?.manifest;
  if (
    manifest &&
    typeof manifest === 'object' &&
    'confirmation_ready' in manifest &&
    typeof manifest.confirmation_ready === 'boolean'
  ) {
    return manifest.confirmation_ready;
  }
  return readEnvironmentReady(statusPayload);
};

const readRecoveryPending = (
  statusPayload: ShadowCloneStatusResponse,
): boolean => statusPayload.recovery?.pending === true;

const ACTIVE_SUBAGENT_RECOVERY_PHASES = new Set([
  'wake_requested',
  'wake_dispatched',
  'replacement_requested',
  'replacement_started',
]);

const hasActiveSubagentRecovery = (
  statusPayload: ShadowCloneStatusResponse,
): boolean =>
  Object.values(statusPayload.subagents || {}).some((subagent) =>
    ACTIVE_SUBAGENT_RECOVERY_PHASES.has(
      String(subagent?.recovery?.phase || '').trim(),
    ),
  );

const AUTHORITATIVE_SYNC_STALENESS_MS = 4000;

const readEnvironmentManifest = (
  statusPayload: ShadowCloneStatusResponse,
): Record<string, unknown> | null =>
  statusPayload.environment?.manifest &&
  typeof statusPayload.environment.manifest === 'object'
    ? (statusPayload.environment.manifest as Record<string, unknown>)
    : null;

const readSandboxId = (
  statusPayload: ShadowCloneStatusResponse,
): string | null =>
  typeof statusPayload.sandbox?.id === 'string' && statusPayload.sandbox.id.trim().length > 0
    ? statusPayload.sandbox.id
    : null;

const readSandboxType = (
  statusPayload: ShadowCloneStatusResponse,
): string | null =>
  typeof statusPayload.sandbox?.type === 'string' && statusPayload.sandbox.type.trim().length > 0
    ? statusPayload.sandbox.type
    : null;

const readSandboxBindingState = (
  statusPayload: ShadowCloneStatusResponse,
): string | null =>
  typeof statusPayload.sandbox?.binding_state === 'string' &&
  statusPayload.sandbox.binding_state.trim().length > 0
    ? statusPayload.sandbox.binding_state
    : null;

const mapProposalSubtask = (
  item: ShadowCloneStatusProposalSubtask,
): ShadowCloneSubtask => ({
  id: String(item?.id || ''),
  role: String(item?.role || ''),
  task_description: String(item?.task_description || ''),
  status: 'pending',
});

const mapDependency = (
  item: ShadowCloneStatusDependency,
): ShadowCloneDependency => ({
  from_id: String(item?.from_id || ''),
  to_id: String(item?.to_id || ''),
});

const buildPlannedSubtasks = (
  statusPayload: ShadowCloneStatusResponse,
): ShadowCloneSubtask[] => {
  const proposalSubtasks = Array.isArray(statusPayload.proposal?.subtasks)
    ? statusPayload.proposal.subtasks.map(mapProposalSubtask)
    : [];
  if (proposalSubtasks.length > 0) {
    return proposalSubtasks;
  }
  return Object.entries(statusPayload.subagents || {}).map(([subtaskId, subagent]) => ({
    id: subtaskId,
    role: subagent.role ? String(subagent.role) : '',
    task_description: '',
    status: 'pending',
  }));
};

const toNonEmptyString = (value: unknown): string =>
  typeof value === 'string' ? value.trim() : '';

const mapV2SubtaskPayload = (
  item: any,
  fallbackStatus: ShadowCloneSubtaskStatus,
): (Partial<ShadowCloneSubtask> & { id: string }) | null => {
  if (!item || typeof item !== 'object') {
    return null;
  }

  const id = toNonEmptyString(item.id) ||
    toNonEmptyString(item.subtask_id) ||
    toNonEmptyString(item.task_id);
  if (!id) {
    return null;
  }

  return {
    id,
    agent_name:
      toNonEmptyString(item.agent_name) ||
      toNonEmptyString(item.owner_agent) ||
      toNonEmptyString(item.metadata?.agent_name) ||
      undefined,
    agent_id: toNonEmptyString(item.agent_id) || undefined,
    agent_status: toNonEmptyString(item.agent_status) || undefined,
    idle_since:
      typeof item.idle_since === 'string' ? item.idle_since : undefined,
    idle_expires_at:
      typeof item.idle_expires_at === 'string' ? item.idle_expires_at : undefined,
    wake_reason:
      typeof item.wake_reason === 'string' ? item.wake_reason : undefined,
    wake_message_id:
      typeof item.wake_message_id === 'string' ? item.wake_message_id : undefined,
    shutdown_reason:
      typeof item.shutdown_reason === 'string' ? item.shutdown_reason : undefined,
    role:
      toNonEmptyString(item.role) ||
      toNonEmptyString(item.agent_role) ||
      toNonEmptyString(item.agent_name),
    task_description:
      toNonEmptyString(item.task_description) ||
      toNonEmptyString(item.description),
    status: normalizeSubtaskStatus(item.status, fallbackStatus),
    result_summary:
      typeof item.result_summary === 'string'
        ? item.result_summary
        : typeof item.summary === 'string'
          ? item.summary
          : undefined,
    started_at: typeof item.started_at === 'string' ? item.started_at : undefined,
    finished_at: typeof item.finished_at === 'string' ? item.finished_at : undefined,
    error:
      typeof item.error === 'string'
        ? item.error
        : typeof item.last_error === 'string'
          ? item.last_error
          : undefined,
    failure_class:
      typeof item.failure_class === 'string' ? item.failure_class : undefined,
  };
};

const readV2SubtasksFromContent = (
  content: any,
  fallbackStatus: ShadowCloneSubtaskStatus,
): (Partial<ShadowCloneSubtask> & { id: string })[] => {
  if (!content || typeof content !== 'object') {
    return [];
  }

  const arraySource = Array.isArray(content.subtasks)
    ? content.subtasks
    : Array.isArray(content.proposal?.subtasks)
      ? content.proposal.subtasks
      : Array.isArray(content.tasks)
        ? content.tasks
        : null;
  if (arraySource) {
    return arraySource
      .map((item: any) => mapV2SubtaskPayload(item, fallbackStatus))
      .filter((item: any): item is Partial<ShadowCloneSubtask> & { id: string } =>
        Boolean(item),
      );
  }

  if (content.tasks && typeof content.tasks === 'object') {
    return Object.entries(content.tasks)
      .map(([id, payload]) =>
        mapV2SubtaskPayload(
          {
            id,
            ...(payload && typeof payload === 'object' ? payload : {}),
          },
          fallbackStatus,
        ),
      )
      .filter((item): item is Partial<ShadowCloneSubtask> & { id: string } =>
        Boolean(item),
      );
  }

  if (content.subagents && typeof content.subagents === 'object') {
    return Object.entries(content.subagents)
      .map(([id, payload]) =>
        mapV2SubtaskPayload(
          {
            id,
            ...(payload && typeof payload === 'object' ? payload : {}),
          },
          fallbackStatus,
        ),
      )
      .filter((item): item is Partial<ShadowCloneSubtask> & { id: string } =>
        Boolean(item),
      );
  }

  return [];
};

const mapAgentLifecycleToSubtaskStatus = (
  value: unknown,
): ShadowCloneSubtaskStatus => {
  switch (String(value || '').trim().toLowerCase()) {
    case 'working':
    case 'running':
      return 'running';
    case 'failed':
      return 'failed';
    case 'closed':
    case 'shutting_down':
    case 'idle':
      return 'completed';
    case 'starting':
    default:
      return 'pending';
  }
};

const readProjectionAgents = (projection: any): Record<string, any> => {
  const agents =
    projection?.agents && typeof projection.agents === 'object'
      ? projection.agents
      : projection?.team?.members && typeof projection.team.members === 'object'
        ? projection.team.members
        : {};
  return agents as Record<string, any>;
};

const normalizeV2AgentIdentity = (value: unknown): string =>
  toNonEmptyString(value).toLowerCase();

const readProjectionAgentName = (entryName: string, agent: any): string =>
  toNonEmptyString(agent?.agent_name) || entryName;

const resolveProjectionAgentEntryForSubtask = (
  subtask: Partial<ShadowCloneSubtask> & { id: string },
  agents: Record<string, any>,
): [string, any] | null => {
  const requestedAgentName = normalizeV2AgentIdentity(subtask.agent_name);
  if (requestedAgentName) {
    const match = Object.entries(agents).find(([entryName, agent]) =>
      normalizeV2AgentIdentity(entryName) === requestedAgentName ||
      normalizeV2AgentIdentity(agent?.agent_name) === requestedAgentName,
    );
    if (match) {
      return match;
    }
  }

  const requestedAgentId = normalizeV2AgentIdentity(subtask.agent_id);
  if (requestedAgentId) {
    const match = Object.entries(agents).find(([_entryName, agent]) =>
      normalizeV2AgentIdentity(agent?.agent_id) === requestedAgentId,
    );
    if (match) {
      return match;
    }
  }

  const taskId = toNonEmptyString(subtask.id);
  if (!taskId) {
    return null;
  }
  const taskMatches = Object.entries(agents).filter(([_entryName, agent]) => {
    const currentTaskIds = Array.isArray(agent?.current_task_ids)
      ? agent.current_task_ids
      : [];
    return currentTaskIds.some((currentTaskId: unknown) =>
      toNonEmptyString(currentTaskId) === taskId,
    );
  });
  return taskMatches.length === 1 ? taskMatches[0] : null;
};

const applyAgentMetadataToSubtask = (
  subtask: Partial<ShadowCloneSubtask> & { id: string },
  agents: Record<string, any>,
): Partial<ShadowCloneSubtask> & { id: string } => {
  const agentEntry = resolveProjectionAgentEntryForSubtask(subtask, agents);
  const agentName = agentEntry
    ? readProjectionAgentName(agentEntry[0], agentEntry[1])
    : subtask.agent_name || '';
  const agent = agentEntry ? agentEntry[1] : null;
  if (!agent || typeof agent !== 'object') {
    return subtask;
  }
  return {
    ...subtask,
    role: subtask.role || toNonEmptyString(agent.role) || agentName,
    agent_name: agentName,
    agent_id: toNonEmptyString(agent.agent_id) || subtask.agent_id,
    agent_status: toNonEmptyString(agent.status) || subtask.agent_status,
    idle_since:
      typeof agent.idle_since === 'string' ? agent.idle_since : subtask.idle_since,
    idle_expires_at:
      typeof agent.idle_expires_at === 'string'
        ? agent.idle_expires_at
        : subtask.idle_expires_at,
    wake_reason:
      typeof agent.wake_reason === 'string' ? agent.wake_reason : subtask.wake_reason,
    wake_message_id:
      typeof agent.wake_message_id === 'string'
        ? agent.wake_message_id
        : subtask.wake_message_id,
    shutdown_reason:
      typeof agent.shutdown_reason === 'string'
        ? agent.shutdown_reason
        : subtask.shutdown_reason,
  };
};

const buildAssignedProjectionAgentIdentities = (
  taskRows: (Partial<ShadowCloneSubtask> & { id: string })[],
): Set<string> => {
  const identities = new Set<string>();
  for (const item of taskRows) {
    const agentId = normalizeV2AgentIdentity(item.agent_id);
    const agentName = normalizeV2AgentIdentity(item.agent_name);
    if (agentId) {
      identities.add(`id:${agentId}`);
    }
    if (agentName) {
      identities.add(`name:${agentName}`);
    }
  }
  return identities;
};

const projectionAgentIdentityKeys = (
  entryName: string,
  agent: any,
): string[] => {
  const keys: string[] = [];
  const agentId = normalizeV2AgentIdentity(agent?.agent_id);
  const agentName = normalizeV2AgentIdentity(readProjectionAgentName(entryName, agent));
  if (agentId) {
    keys.push(`id:${agentId}`);
  }
  if (agentName) {
    keys.push(`name:${agentName}`);
  }
  return keys;
};

const buildV2ProjectionSubtasks = (
  projection: any,
  fallbackStatus: ShadowCloneSubtaskStatus,
): (Partial<ShadowCloneSubtask> & { id: string })[] => {
  const agents = readProjectionAgents(projection);
  const taskRows = readV2SubtasksFromContent(projection, fallbackStatus).map((item) =>
    applyAgentMetadataToSubtask(item, agents),
  );
  const assignedAgents = buildAssignedProjectionAgentIdentities(taskRows);
  const syntheticAgentRows = Object.entries(agents)
    .filter(([agentName, agent]) =>
      projectionAgentIdentityKeys(agentName, agent).every(
        (identityKey) => !assignedAgents.has(identityKey),
      ),
    )
    .map(([agentName, agent]) => ({
      id: `agent:${agentName}`,
      role: toNonEmptyString(agent?.role) || agentName,
      task_description:
        String(agent?.status || '').trim().toLowerCase() === 'idle'
          ? `Idle teammate ${agentName}`
          : `Team member ${agentName}`,
      status: mapAgentLifecycleToSubtaskStatus(agent?.status),
      agent_name: agentName,
      agent_id: toNonEmptyString(agent?.agent_id) || undefined,
      agent_status: toNonEmptyString(agent?.status) || undefined,
      idle_since:
        typeof agent?.idle_since === 'string' ? agent.idle_since : undefined,
      idle_expires_at:
        typeof agent?.idle_expires_at === 'string'
          ? agent.idle_expires_at
          : undefined,
      wake_reason:
        typeof agent?.wake_reason === 'string' ? agent.wake_reason : undefined,
      wake_message_id:
        typeof agent?.wake_message_id === 'string'
          ? agent.wake_message_id
          : undefined,
      shutdown_reason:
        typeof agent?.shutdown_reason === 'string'
          ? agent.shutdown_reason
          : undefined,
    }));
  return taskRows.concat(syntheticAgentRows);
};

const readV2FinalOutputFromProjection = (
  projection: any,
): { content: string; created_at?: string } | null => {
  const finalOutput = projection?.final_output;

  if (finalOutput && typeof finalOutput === 'object') {
    const content = toNonEmptyString(finalOutput.content);
    if (!content) {
      return null;
    }
    return {
      content,
      created_at:
        typeof finalOutput.created_at === 'string'
          ? finalOutput.created_at
          : undefined,
    };
  }

  const content = toNonEmptyString(finalOutput);
  return content ? { content } : null;
};

const readV2ProjectionMessages = (projection: any): any[] => {
  const messages = projection?.messages;
  if (Array.isArray(messages)) {
    return messages;
  }
  if (messages && typeof messages === 'object') {
    return Object.entries(messages).map(([id, payload]) => ({
      message_id: id,
      ...(payload && typeof payload === 'object' ? payload : {}),
    }));
  }
  return [];
};

const readV2ProjectionTranscripts = (projection: any): any[] => {
  const transcripts = projection?.transcripts;
  if (Array.isArray(transcripts)) {
    return transcripts;
  }
  if (transcripts && typeof transcripts === 'object') {
    return Object.entries(transcripts).map(([id, payload]) => ({
      message_id: id,
      ...(payload && typeof payload === 'object' ? payload : {}),
    }));
  }
  return [];
};

const readV2ProjectionToolCalls = (projection: any): any[] => {
  const toolCalls = projection?.tool_calls;
  if (Array.isArray(toolCalls)) {
    return toolCalls;
  }
  if (toolCalls && typeof toolCalls === 'object') {
    return Object.entries(toolCalls).map(([id, payload]) => ({
      tool_call_id: id,
      ...(payload && typeof payload === 'object' ? payload : {}),
    }));
  }
  return [];
};

const readMailboxAgentName = (value: unknown): string => {
  const text = toNonEmptyString(value);
  if (!text) {
    return '';
  }
  return text.includes('@') ? text.split('@')[0] || '' : text;
};

const buildAgentSubtaskIndex = (
  subtasks: ShadowCloneSubtask[],
): Map<string, string[]> => {
  const index = new Map<string, string[]>();
  for (const subtask of subtasks) {
    const agentName = toNonEmptyString(subtask.agent_name);
    if (!agentName) {
      continue;
    }
    index.set(agentName, (index.get(agentName) || []).concat(subtask.id));
  }
  return index;
};

const buildTaskIdSet = (subtasks: ShadowCloneSubtask[]): Set<string> =>
  new Set(subtasks.map((subtask) => subtask.id));

const normalizeProjectionToolName = (value: unknown): string =>
  String(value || 'unknown')
    .trim()
    .replace(/_/g, '-')
    .toLowerCase();

const normalizeWorkspaceArtifactPath = (value: unknown): string => {
  const path = toNonEmptyString(value);
  if (!path) {
    return '';
  }
  if (path.startsWith('/workspace/')) {
    return path.replace(/\/{2,}/g, '/');
  }
  if (path.startsWith('workspace/')) {
    return `/${path}`.replace(/\/{2,}/g, '/');
  }
  if (path.startsWith('/')) {
    return path.replace(/\/{2,}/g, '/');
  }
  return `/workspace/${path.replace(/^\.?\//, '')}`.replace(/\/{2,}/g, '/');
};

const readProjectionToolPath = (toolCall: any): string => {
  const argumentCandidates = [
    toolCall?.arguments?.path,
    toolCall?.arguments?.file_path,
    toolCall?.arguments_summary?.path,
    toolCall?.arguments_summary?.file_path,
    toolCall?.result?.path,
    toolCall?.path,
    toolCall?.file_path,
  ];

  for (const candidate of argumentCandidates) {
    const normalized = normalizeWorkspaceArtifactPath(candidate);
    if (normalized && !normalized.endsWith('/')) {
      return normalized;
    }
  }

  const resultSummary = toNonEmptyString(toolCall?.result_summary);
  const resultMatch = resultSummary.match(/\/workspace\/[^\s"'`),;]+/);
  return normalizeWorkspaceArtifactPath(resultMatch?.[0]);
};

type ShadowCloneFileArtifact = {
  path: string;
  tool_call_id?: string;
  tool_name?: string;
  task_id?: string;
  agent_name?: string;
  bytes?: number;
};

const isProjectionFileArtifactTool = (toolName: unknown): boolean => {
  const normalized = normalizeProjectionToolName(toolName).replace(/-/g, '_');
  return normalized === 'write_file' || normalized === 'upload_file';
};

const buildProjectionFileArtifactsBySubtask = (
  projection: any,
  subtasks: ShadowCloneSubtask[],
): Map<string, ShadowCloneFileArtifact[]> => {
  const projectedToolCalls = readV2ProjectionToolCalls(projection);
  const taskIds = buildTaskIdSet(subtasks);
  const agentSubtaskIndex = buildAgentSubtaskIndex(subtasks);
  const artifactsBySubtask = new Map<string, ShadowCloneFileArtifact[]>();
  const seenBySubtask = new Map<string, Set<string>>();

  for (const toolCall of projectedToolCalls) {
    if (!isProjectionFileArtifactTool(toolCall?.tool_name)) {
      continue;
    }
    const status = toNonEmptyString(toolCall?.status).toLowerCase();
    if (status && status !== 'completed') {
      continue;
    }
    const path = readProjectionToolPath(toolCall);
    if (!path) {
      continue;
    }
    const subtaskIds = resolveProjectionToolCallSubtaskIds(
      toolCall,
      subtasks,
      taskIds,
      agentSubtaskIndex,
    );
    for (const subtaskId of subtaskIds) {
      if (!seenBySubtask.has(subtaskId)) {
        seenBySubtask.set(subtaskId, new Set());
      }
      const seen = seenBySubtask.get(subtaskId)!;
      if (seen.has(path)) {
        continue;
      }
      seen.add(path);
      const bytes =
        typeof toolCall?.result?.bytes === 'number' && Number.isFinite(toolCall.result.bytes)
          ? toolCall.result.bytes
          : undefined;
      artifactsBySubtask.set(
        subtaskId,
        (artifactsBySubtask.get(subtaskId) || []).concat({
          path,
          tool_call_id:
            toNonEmptyString(toolCall?.tool_call_id) ||
            toNonEmptyString(toolCall?.id) ||
            undefined,
          tool_name: toNonEmptyString(toolCall?.tool_name) || undefined,
          task_id: toNonEmptyString(toolCall?.task_id) || undefined,
          agent_name: toNonEmptyString(toolCall?.agent_name) || undefined,
          ...(bytes !== undefined ? { bytes } : {}),
        }),
      );
    }
  }

  return artifactsBySubtask;
};

const stringifyProjectionSummary = (value: unknown): string => {
  if (typeof value === 'string') {
    return value.trim();
  }
  if (value == null) {
    return '';
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const buildProjectionToolArgumentSummary = (toolCall: any): string => {
  const parts: string[] = [];
  const summary = stringifyProjectionSummary(toolCall?.arguments_summary);
  if (summary) {
    parts.push(summary);
  }
  if (toolCall?.arguments_redacted === true) {
    parts.push('arguments redacted');
  }
  if (toolCall?.arguments_size_bytes != null) {
    parts.push(`arguments size: ${toolCall.arguments_size_bytes} bytes`);
  }
  return parts.join('\n');
};

const buildProjectionToolResult = (
  toolCall: any,
): { content?: string; isSuccess?: boolean } | undefined => {
  const status = String(toolCall?.status || '').trim().toLowerCase();
  if (status === 'running' || status === 'pending') {
    return {
      content: 'STREAMING',
      isSuccess: true,
    };
  }
  if (status === 'failed') {
    const errorType = toNonEmptyString(toolCall?.error_type);
    const error = toNonEmptyString(toolCall?.error);
    return {
      content: [errorType, error].filter(Boolean).join(': ') || 'Tool call failed.',
      isSuccess: false,
    };
  }
  if (status === 'completed') {
    return {
      content:
        toNonEmptyString(toolCall?.result_summary) ||
        toNonEmptyString(toolCall?.result) ||
        'Tool call completed.',
      isSuccess: true,
    };
  }
  return undefined;
};

const buildProjectionPanelToolCall = (
  toolCall: any,
): ShadowClonePanelToolCall | null => {
  const toolCallId =
    toNonEmptyString(toolCall?.tool_call_id) || toNonEmptyString(toolCall?.id);
  if (!toolCallId) {
    return null;
  }
  const timestamp =
    toNonEmptyString(toolCall?.completed_at) ||
    toNonEmptyString(toolCall?.failed_at) ||
    toNonEmptyString(toolCall?.started_at) ||
    toNonEmptyString(toolCall?.updated_at) ||
    undefined;
  const toolResult = buildProjectionToolResult(toolCall);
  return {
    assistantCall: {
      name: normalizeProjectionToolName(toolCall?.tool_name),
      content: buildProjectionToolArgumentSummary(toolCall),
      timestamp,
      toolCallId,
    },
    ...(toolResult
      ? {
          toolResult: {
            ...toolResult,
            timestamp,
            toolCallId,
          },
        }
      : {}),
  };
};

const upsertProjectionPanelToolCall = (
  toolCalls: ShadowClonePanelToolCall[],
  nextToolCall: ShadowClonePanelToolCall,
): ShadowClonePanelToolCall[] => {
  const nextId = nextToolCall.assistantCall.toolCallId;
  const existingIndex = toolCalls.findIndex(
    (toolCall) => toolCall.assistantCall.toolCallId === nextId,
  );
  if (existingIndex === -1) {
    return toolCalls.concat(nextToolCall);
  }
  if (samePanelToolCall(toolCalls[existingIndex], nextToolCall)) {
    return toolCalls;
  }
  return toolCalls.map((toolCall, index) =>
    index === existingIndex ? nextToolCall : toolCall,
  );
};

const resolveProjectionToolCallSubtaskIds = (
  toolCall: any,
  subtasks: ShadowCloneSubtask[],
  taskIds: Set<string>,
  agentSubtaskIndex: Map<string, string[]>,
): string[] => {
  const subtaskIds = new Set<string>();
  const toolCallId =
    toNonEmptyString(toolCall?.tool_call_id) || toNonEmptyString(toolCall?.id);
  const directTaskId = toNonEmptyString(toolCall?.task_id);
  if (directTaskId && taskIds.has(directTaskId)) {
    subtaskIds.add(directTaskId);
  }

  if (toolCallId) {
    for (const subtask of subtasks) {
      const rawTask = subtask as ShadowCloneSubtask & {
        tool_call_ids?: unknown;
      };
      const toolCallIds = Array.isArray(rawTask.tool_call_ids)
        ? rawTask.tool_call_ids.map((item) => String(item))
        : [];
      if (toolCallIds.includes(toolCallId)) {
        subtaskIds.add(subtask.id);
      }
    }
  }

  const agentName = toNonEmptyString(toolCall?.agent_name);
  if (agentName) {
    for (const subtaskId of agentSubtaskIndex.get(agentName) || []) {
      subtaskIds.add(subtaskId);
    }
  }

  return Array.from(subtaskIds);
};

const applyV2ProjectionToolCallsToPanelStates = (
  states: Record<string, ShadowClonePanelState>,
  projection: any,
  subtasks: ShadowCloneSubtask[],
): Record<string, ShadowClonePanelState> => {
  const projectedToolCalls = readV2ProjectionToolCalls(projection);
  if (projectedToolCalls.length === 0 || subtasks.length === 0) {
    return states;
  }

  const taskIds = buildTaskIdSet(subtasks);
  const agentSubtaskIndex = buildAgentSubtaskIndex(subtasks);
  let nextStates = states;

  for (const rawToolCall of projectedToolCalls) {
    const panelToolCall = buildProjectionPanelToolCall(rawToolCall);
    if (!panelToolCall) {
      continue;
    }
    const subtaskIds = resolveProjectionToolCallSubtaskIds(
      rawToolCall,
      subtasks,
      taskIds,
      agentSubtaskIndex,
    );
    for (const subtaskId of subtaskIds) {
      const currentPanelState = ensurePanelState(nextStates, subtaskId);
      const nextToolCalls = upsertProjectionPanelToolCall(
        currentPanelState.toolCalls,
        panelToolCall,
      );
      if (nextToolCalls === currentPanelState.toolCalls) {
        continue;
      }
      nextStates = {
        ...nextStates,
        [subtaskId]: {
          ...currentPanelState,
          toolCalls: nextToolCalls,
          latestLabel:
            panelToolCall.toolResult?.content === 'STREAMING'
              ? `正在执行 ${panelToolCall.assistantCall.name || 'tool'}`
              : currentPanelState.latestLabel || '已同步工具调用',
        },
      };
    }
  }

  return nextStates;
};

const resolveProjectionMessageSubtaskIds = (
  message: any,
  agentSubtaskIndex: Map<string, string[]>,
): string[] => {
  const candidateAgentNames = [
    readMailboxAgentName(message?.recipient),
    readMailboxAgentName(message?.recipient_agent),
    readMailboxAgentName(message?.to),
    readMailboxAgentName(message?.sender),
    readMailboxAgentName(message?.sender_agent),
    readMailboxAgentName(message?.from),
  ].filter(Boolean);
  const subtaskIds = new Set<string>();
  for (const agentName of candidateAgentNames) {
    for (const subtaskId of agentSubtaskIndex.get(agentName) || []) {
      subtaskIds.add(subtaskId);
    }
  }
  return Array.from(subtaskIds);
};

const readV2ProjectionMessageMetadata = (
  message: UnifiedMessage,
): Record<string, any> => {
  try {
    const metadata = JSON.parse(message.metadata || '{}');
    return metadata && typeof metadata === 'object' ? metadata : {};
  } catch {
    return {};
  }
};

const isV2ProjectionTranscriptMessage = (message: UnifiedMessage): boolean =>
  readV2ProjectionMessageMetadata(message).shadow_clone_v2_projection_message === true;

const isV2ProjectionOutputTranscriptMessage = (
  message: UnifiedMessage,
): boolean =>
  readV2ProjectionMessageMetadata(message)
    .shadow_clone_v2_projection_transcript === true;


const projectionTranscriptAgentKey = (value: unknown): string =>
  toNonEmptyString(value).toLowerCase();

const projectionTranscriptMessageKey = (message: UnifiedMessage): string => {
  const metadata = readV2ProjectionMessageMetadata(message);
  return (
    toNonEmptyString(metadata.v2_transcript_message_id) ||
    toNonEmptyString(metadata.original_message_id) ||
    toNonEmptyString(message.message_id)
  );
};

const buildHistoricalProjectionTranscriptsByAgent = (
  states: Record<string, ShadowCloneTranscriptState>,
): Map<string, UnifiedMessage[]> => {
  const byAgent = new Map<string, UnifiedMessage[]>();
  const seenByAgent = new Map<string, Set<string>>();

  for (const transcriptState of Object.values(states)) {
    for (const message of transcriptState.messages) {
      if (!isV2ProjectionOutputTranscriptMessage(message)) {
        continue;
      }
      const metadata = readV2ProjectionMessageMetadata(message);
      const agentKey = projectionTranscriptAgentKey(metadata.agent_name);
      if (!agentKey) {
        continue;
      }
      const messageKey = projectionTranscriptMessageKey(message);
      if (!messageKey) {
        continue;
      }
      const seen = seenByAgent.get(agentKey) || new Set<string>();
      if (seen.has(messageKey)) {
        continue;
      }
      seen.add(messageKey);
      seenByAgent.set(agentKey, seen);
      byAgent.set(agentKey, (byAgent.get(agentKey) || []).concat(message));
    }
  }

  return byAgent;
};

const cloneHistoricalProjectionTranscriptForSubtask = (
  message: UnifiedMessage,
  subtaskId: string,
): UnifiedMessage => {
  const metadata = readV2ProjectionMessageMetadata(message);
  const originalSubtaskId = toNonEmptyString(metadata.shadow_clone_subtask_id);
  const originalMessageId = toNonEmptyString(message.message_id);
  return {
    ...message,
    message_id: `shadow-clone:${subtaskId}:v2-agent-history:${projectionTranscriptMessageKey(message) || originalMessageId}`,
    thread_id: `shadow-clone:${subtaskId}`,
    metadata: JSON.stringify({
      ...metadata,
      shadow_clone_subtask_id: subtaskId,
      shadow_clone_v2_agent_history: true,
      original_shadow_clone_subtask_id: originalSubtaskId || undefined,
      original_message_id: originalMessageId || undefined,
    }),
  };
};

const appendHistoricalAgentTranscriptMessages = (
  states: Record<string, ShadowCloneTranscriptState>,
  historicalByAgent: Map<string, UnifiedMessage[]>,
  subtasks: ShadowCloneSubtask[],
): Record<string, ShadowCloneTranscriptState> => {
  let nextStates = states;

  for (const subtask of subtasks) {
    const agentKey = projectionTranscriptAgentKey(subtask.agent_name);
    if (!agentKey) {
      continue;
    }
    const historicalMessages = historicalByAgent.get(agentKey) || [];
    if (historicalMessages.length === 0) {
      continue;
    }
    const currentState = nextStates[subtask.id] || createEmptyShadowCloneTranscriptState();
    const existingKeys = new Set(
      currentState.messages
        .filter(isV2ProjectionOutputTranscriptMessage)
        .map(projectionTranscriptMessageKey)
        .filter(Boolean),
    );
    for (const historicalMessage of historicalMessages) {
      const historicalKey = projectionTranscriptMessageKey(historicalMessage);
      if (!historicalKey || existingKeys.has(historicalKey)) {
        continue;
      }
      nextStates = appendV2ProjectionTranscriptMessage(
        nextStates,
        subtask.id,
        cloneHistoricalProjectionTranscriptForSubtask(historicalMessage, subtask.id),
      );
      existingKeys.add(historicalKey);
    }
  }

  return nextStates;
};

const stripV2ProjectionTranscriptMessages = (
  states: Record<string, ShadowCloneTranscriptState>,
): Record<string, ShadowCloneTranscriptState> =>
  Object.entries(states).reduce<Record<string, ShadowCloneTranscriptState>>(
    (nextStates, [subtaskId, transcriptState]) => {
      const messages = transcriptState.messages.filter(
        (message) =>
          !isV2ProjectionTranscriptMessage(message) &&
          !isV2ProjectionOutputTranscriptMessage(message),
      );
      const hasLiveContent = Boolean(
        transcriptState.streamingTextContent ||
          transcriptState.streamingReasoningContent ||
          transcriptState.streamingToolCall,
      );
      if (messages.length === 0 && !hasLiveContent) {
        return nextStates;
      }
      return {
        ...nextStates,
        [subtaskId]: {
          ...transcriptState,
          messages,
          latestLabel:
            messages.length === 0 && transcriptState.latestLabel === '已同步队友消息'
              ? null
              : transcriptState.latestLabel,
        },
      };
    },
    {},
  );

const retainV2ProjectionOutputTranscriptMessages = (
  states: Record<string, ShadowCloneTranscriptState>,
): Record<string, ShadowCloneTranscriptState> =>
  Object.entries(states).reduce<Record<string, ShadowCloneTranscriptState>>(
    (nextStates, [subtaskId, transcriptState]) => {
      const messages = transcriptState.messages.filter(
        isV2ProjectionOutputTranscriptMessage,
      );
      if (messages.length === 0) {
        return nextStates;
      }
      return {
        ...nextStates,
        [subtaskId]: {
          ...createEmptyShadowCloneTranscriptState(),
          messages,
          lastSequence: messages.reduce(
            (maxSequence, message) => Math.max(maxSequence, message.sequence || 0),
            0,
          ),
          latestLabel: '已同步队友消息',
        },
      };
    },
    {},
  );

const buildV2ProjectionTranscriptMessage = (
  subtaskId: string,
  message: any,
  sequence: number,
): UnifiedMessage => {
  const messageId =
    toNonEmptyString(message?.message_id) ||
    toNonEmptyString(message?.id) ||
    `projection-message-${sequence}`;
  const sender =
    toNonEmptyString(message?.sender) ||
    toNonEmptyString(message?.sender_agent) ||
    toNonEmptyString(message?.from) ||
    undefined;
  const recipient =
    toNonEmptyString(message?.recipient) ||
    toNonEmptyString(message?.recipient_agent) ||
    toNonEmptyString(message?.to) ||
    undefined;
  const status = toNonEmptyString(message?.status) || undefined;
  const summary =
    toNonEmptyString(message?.summary) ||
    toNonEmptyString(message?.subject) ||
    undefined;
  const text =
    toNonEmptyString(message?.text) ||
    toNonEmptyString(message?.content) ||
    toNonEmptyString(message?.body) ||
    summary ||
    'Mailbox message';
  const timestamp =
    toNonEmptyString(message?.created_at) ||
    toNonEmptyString(message?.sent_at) ||
    toNonEmptyString(message?.updated_at) ||
    new Date().toISOString();

  return {
    sequence,
    message_id: `shadow-clone:${subtaskId}:v2-projection-message:${messageId}`,
    thread_id: `shadow-clone:${subtaskId}`,
    type: 'assistant',
    role: 'assistant',
    is_llm_message: true,
    content: JSON.stringify({
      role: 'assistant',
      content: text,
      sender,
      recipient,
      status,
      summary,
      priority: toNonEmptyString(message?.priority) || undefined,
      control: message?.control,
    }),
    metadata: JSON.stringify({
      stream_status: 'complete',
      shadow_clone_subtask_id: subtaskId,
      shadow_clone_v2_projection_message: true,
      mailbox_message_id: messageId,
      sender,
      recipient,
      status,
      summary,
      text,
      priority: toNonEmptyString(message?.priority) || undefined,
      control_type: toNonEmptyString(message?.control?.type) || undefined,
      read_at: toNonEmptyString(message?.read_at) || undefined,
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};

const resolveProjectionTranscriptSubtaskIds = (
  transcript: any,
  subtasks: ShadowCloneSubtask[],
  taskIds: Set<string>,
  agentSubtaskIndex: Map<string, string[]>,
): string[] => {
  const subtaskIds = new Set<string>();
  const taskId = toNonEmptyString(transcript?.task_id);
  if (taskId && taskIds.has(taskId)) {
    subtaskIds.add(taskId);
  }

  const agentName = toNonEmptyString(transcript?.agent_name);
  if (agentName) {
    for (const subtaskId of agentSubtaskIndex.get(agentName) || []) {
      subtaskIds.add(subtaskId);
    }
  }

  return Array.from(subtaskIds);
};

const buildV2ProjectionOutputTranscriptMessage = (
  subtaskId: string,
  transcript: any,
  sequence: number,
  fileArtifacts: ShadowCloneFileArtifact[] = [],
): UnifiedMessage | null => {
  const content = toNonEmptyString(transcript?.content);
  if (!content) {
    return null;
  }
  const messageId =
    toNonEmptyString(transcript?.message_id) ||
    toNonEmptyString(transcript?.id) ||
    `projection-transcript-${sequence}`;
  const timestamp =
    toNonEmptyString(transcript?.created_at) ||
    toNonEmptyString(transcript?.updated_at) ||
    new Date().toISOString();
  const agentName = toNonEmptyString(transcript?.agent_name) || undefined;
  const sourceEventType =
    toNonEmptyString(transcript?.source_event_type) || undefined;
  const streamStatus = toNonEmptyString(transcript?.stream_status) || 'complete';
  const visibleContent =
    agentName && !content.startsWith(`${agentName}\n\n`)
      ? `${agentName}\n\n${content}`
      : content;

  return {
    sequence,
    message_id: `shadow-clone:${subtaskId}:v2-projection-transcript:${messageId}`,
    thread_id: `shadow-clone:${subtaskId}`,
    type: 'assistant',
    role: 'assistant',
    is_llm_message: true,
    content: JSON.stringify({
      role: 'assistant',
      content: visibleContent,
      agent_name: agentName,
      stream_status: streamStatus,
    }),
    metadata: JSON.stringify({
      stream_status: streamStatus,
      shadow_clone_subtask_id: subtaskId,
      shadow_clone_v2_projection_transcript: true,
      v2_transcript_message_id: messageId,
      agent_name: agentName,
      source_event_type: sourceEventType,
      ...(fileArtifacts.length > 0
        ? { shadow_clone_file_artifacts: fileArtifacts }
        : {}),
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};

const appendV2ProjectionTranscriptMessage = (
  states: Record<string, ShadowCloneTranscriptState>,
  subtaskId: string,
  message: UnifiedMessage,
): Record<string, ShadowCloneTranscriptState> => {
  const currentState = states[subtaskId] || createEmptyShadowCloneTranscriptState();
  const messages = currentState.messages
    .concat(message)
    .sort((left, right) => {
      const sequenceDelta = (left.sequence || 0) - (right.sequence || 0);
      if (sequenceDelta !== 0) {
        return sequenceDelta;
      }
      const leftTime = Date.parse(left.created_at || left.updated_at || '');
      const rightTime = Date.parse(right.created_at || right.updated_at || '');
      if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
        return leftTime - rightTime;
      }
      return String(left.message_id || '').localeCompare(String(right.message_id || ''));
    });
  return {
    ...states,
    [subtaskId]: {
      ...currentState,
      messages,
      lastSequence: Math.max(currentState.lastSequence, message.sequence || 0),
      latestLabel: currentState.latestLabel || '已同步队友消息',
    },
  };
};

const applyV2ProjectionMessagesToTranscriptStates = (
  states: Record<string, ShadowCloneTranscriptState>,
  projection: any,
  subtasks: ShadowCloneSubtask[],
  eventIndex?: unknown,
): Record<string, ShadowCloneTranscriptState> => {
  const projectionMessages = readV2ProjectionMessages(projection);
  const projectionTranscripts = readV2ProjectionTranscripts(projection);
  const agentSubtaskIndex = buildAgentSubtaskIndex(subtasks);
  const taskIds = buildTaskIdSet(subtasks);
  const baseSequence = Number(eventIndex);
  const historicalByAgent = buildHistoricalProjectionTranscriptsByAgent(states);
  const fileArtifactsBySubtask = buildProjectionFileArtifactsBySubtask(
    projection,
    subtasks,
  );
  let nextStates = stripV2ProjectionTranscriptMessages(states);

  projectionMessages.forEach((message, index) => {
    const subtaskIds = resolveProjectionMessageSubtaskIds(message, agentSubtaskIndex);
    const sequence = Number.isFinite(baseSequence) ? baseSequence + index : index;
    for (const subtaskId of subtaskIds) {
      nextStates = appendV2ProjectionTranscriptMessage(
        nextStates,
        subtaskId,
        buildV2ProjectionTranscriptMessage(subtaskId, message, sequence),
      );
    }
  });

  projectionTranscripts.forEach((transcript, index) => {
    if (!toNonEmptyString(transcript?.content)) {
      return;
    }
    const subtaskIds = resolveProjectionTranscriptSubtaskIds(
      transcript,
      subtasks,
      taskIds,
      agentSubtaskIndex,
    );
    for (const subtaskId of subtaskIds) {
      const sequence = Number.isFinite(baseSequence)
        ? baseSequence + projectionMessages.length + index
        : projectionMessages.length + index;
      const scopedMessage = buildV2ProjectionOutputTranscriptMessage(
        subtaskId,
        transcript,
        sequence,
        fileArtifactsBySubtask.get(subtaskId) || [],
      );
      if (!scopedMessage) {
        continue;
      }
      nextStates = appendV2ProjectionTranscriptMessage(
        nextStates,
        subtaskId,
        scopedMessage,
      );
    }
  });

  nextStates = appendHistoricalAgentTranscriptMessages(
    nextStates,
    historicalByAgent,
    subtasks,
  );

  return nextStates;
};

const readV2DependenciesFromContent = (
  content: any,
): ShadowCloneDependency[] =>
  Array.isArray(content?.dependencies)
    ? content.dependencies.map(mapDependency)
    : Array.isArray(content?.proposal?.dependencies)
      ? content.proposal.dependencies.map(mapDependency)
      : [];

const markUnresolvedSubtasksCompleted = (
  subtasks: ShadowCloneSubtask[],
): ShadowCloneSubtask[] =>
  subtasks.map((subtask) =>
    isTerminalSubtaskStatus(subtask.status)
      ? subtask
      : {
          ...subtask,
          status: 'completed',
          error: undefined,
        },
  );

const upsertSubtask = (
  subtasks: ShadowCloneSubtask[],
  patch: Partial<ShadowCloneSubtask> & { id: string },
): ShadowCloneSubtask[] => {
  const index = subtasks.findIndex((item) => item.id === patch.id);
  if (index === -1) {
    return subtasks.concat({
      id: patch.id,
      role: patch.role || '',
      task_description: patch.task_description || '',
      status: normalizeSubtaskStatus(patch.status),
      agent_name: patch.agent_name,
      agent_id: patch.agent_id,
      agent_status: patch.agent_status,
      idle_since: patch.idle_since,
      idle_expires_at: patch.idle_expires_at,
      wake_reason: patch.wake_reason,
      wake_message_id: patch.wake_message_id,
      shutdown_reason: patch.shutdown_reason,
      source: patch.source,
      error: patch.error,
      result_summary: patch.result_summary,
      submitted_at: patch.submitted_at,
      started_at: patch.started_at,
      finished_at: patch.finished_at,
      attempt_index: patch.attempt_index,
      failure_class: patch.failure_class,
      recovery_mode: patch.recovery_mode,
      recovery_phase: patch.recovery_phase,
      recovery_reason: patch.recovery_reason,
      recovery_handoff_summary: patch.recovery_handoff_summary,
      replacement_context_id: patch.replacement_context_id,
    });
  }

  const next = subtasks.slice();
  const nextSubtask: ShadowCloneSubtask = {
    ...next[index],
    ...patch,
    role: patch.role ?? next[index].role,
    task_description: patch.task_description ?? next[index].task_description,
    status:
      patch.status !== undefined
        ? normalizeSubtaskStatus(patch.status, next[index].status)
        : next[index].status,
    source: patch.source ?? next[index].source,
    agent_name: patch.agent_name ?? next[index].agent_name,
    agent_id: patch.agent_id ?? next[index].agent_id,
    agent_status: patch.agent_status ?? next[index].agent_status,
    idle_since: 'idle_since' in patch ? patch.idle_since : next[index].idle_since,
    idle_expires_at:
      'idle_expires_at' in patch
        ? patch.idle_expires_at
        : next[index].idle_expires_at,
    wake_reason:
      'wake_reason' in patch ? patch.wake_reason : next[index].wake_reason,
    wake_message_id:
      'wake_message_id' in patch
        ? patch.wake_message_id
        : next[index].wake_message_id,
    shutdown_reason:
      'shutdown_reason' in patch
        ? patch.shutdown_reason
        : next[index].shutdown_reason,
    result_summary: patch.result_summary ?? next[index].result_summary,
    submitted_at: patch.submitted_at ?? next[index].submitted_at,
    started_at: patch.started_at ?? next[index].started_at,
    finished_at: patch.finished_at ?? next[index].finished_at,
    error: 'error' in patch ? patch.error : next[index].error,
    attempt_index:
      'attempt_index' in patch ? patch.attempt_index : next[index].attempt_index,
    failure_class:
      'failure_class' in patch ? patch.failure_class : next[index].failure_class,
    recovery_mode:
      'recovery_mode' in patch ? patch.recovery_mode : next[index].recovery_mode,
    recovery_phase:
      'recovery_phase' in patch ? patch.recovery_phase : next[index].recovery_phase,
    recovery_reason:
      'recovery_reason' in patch ? patch.recovery_reason : next[index].recovery_reason,
    recovery_handoff_summary:
      'recovery_handoff_summary' in patch
        ? patch.recovery_handoff_summary
        : next[index].recovery_handoff_summary,
    replacement_context_id:
      'replacement_context_id' in patch
        ? patch.replacement_context_id
        : next[index].replacement_context_id,
  };
  const current = next[index];
  if (
    current.id === nextSubtask.id &&
    current.role === nextSubtask.role &&
    current.task_description === nextSubtask.task_description &&
    current.status === nextSubtask.status &&
    current.source === nextSubtask.source &&
    current.agent_name === nextSubtask.agent_name &&
    current.agent_id === nextSubtask.agent_id &&
    current.agent_status === nextSubtask.agent_status &&
    current.idle_since === nextSubtask.idle_since &&
    current.idle_expires_at === nextSubtask.idle_expires_at &&
    current.wake_reason === nextSubtask.wake_reason &&
    current.wake_message_id === nextSubtask.wake_message_id &&
    current.shutdown_reason === nextSubtask.shutdown_reason &&
    current.error === nextSubtask.error &&
    current.result_summary === nextSubtask.result_summary &&
    current.submitted_at === nextSubtask.submitted_at &&
    current.started_at === nextSubtask.started_at &&
    current.finished_at === nextSubtask.finished_at &&
    current.attempt_index === nextSubtask.attempt_index &&
    current.failure_class === nextSubtask.failure_class &&
    current.recovery_mode === nextSubtask.recovery_mode &&
    current.recovery_phase === nextSubtask.recovery_phase &&
    current.recovery_reason === nextSubtask.recovery_reason &&
    current.recovery_handoff_summary === nextSubtask.recovery_handoff_summary &&
    current.replacement_context_id === nextSubtask.replacement_context_id
  ) {
    return subtasks;
  }
  next[index] = nextSubtask;
  return next;
};

const mergeRuntimeData = (
  subtasks: ShadowCloneSubtask[],
  statusPayload: ShadowCloneStatusResponse,
  summaryRows: ShadowCloneResultSummary[],
): ShadowCloneSubtask[] => {
  let next = subtasks.slice();

  for (const [subtaskId, subagent] of Object.entries(statusPayload.subagents || {})) {
    next = upsertSubtask(next, {
      id: subtaskId,
      role: subagent.role ? String(subagent.role) : undefined,
      status: normalizeSubtaskStatus(subagent.status),
      result_summary:
        typeof subagent.result_summary === 'string'
          ? subagent.result_summary
          : undefined,
      started_at:
        typeof subagent.started_at === 'string' ? subagent.started_at : undefined,
      finished_at:
        typeof subagent.finished_at === 'string' ? subagent.finished_at : undefined,
      error:
        typeof subagent.last_error === 'string' ? subagent.last_error : undefined,
      attempt_index:
        typeof subagent.attempt_index === 'number' ? subagent.attempt_index : undefined,
      failure_class:
        typeof subagent.failure_class === 'string' ? subagent.failure_class : undefined,
      recovery_mode:
        typeof subagent.recovery?.mode === 'string' ? subagent.recovery.mode : undefined,
      recovery_phase:
        typeof subagent.recovery?.phase === 'string' ? subagent.recovery.phase : undefined,
      recovery_reason:
        typeof subagent.recovery?.reason === 'string' ? subagent.recovery.reason : undefined,
      recovery_handoff_summary:
        typeof subagent.recovery?.handoff_summary === 'string'
          ? subagent.recovery.handoff_summary
          : undefined,
      replacement_context_id:
        typeof subagent.recovery?.replacement_context_id === 'string'
          ? subagent.recovery.replacement_context_id
          : undefined,
    });
  }

  for (const row of summaryRows) {
    next = upsertSubtask(next, {
      id: row.subtask_id,
      role: row.role || undefined,
      status: normalizeSubtaskStatus(row.status),
      result_summary: row.summary || undefined,
      submitted_at: row.submitted_at || undefined,
      attempt_index:
        typeof row.attempt_index === 'number' ? row.attempt_index : undefined,
      failure_class:
        typeof row.failure_class === 'string' ? row.failure_class : undefined,
    });
  }

  return next;
};

const hasLocalSubtaskActivity = (
  state: Pick<ShadowCloneStoreState, 'subtaskTranscriptStates' | 'subtaskPanelStates'>,
  subtaskId: string,
): boolean =>
  Boolean(state.subtaskTranscriptStates[subtaskId]) ||
  Boolean(state.subtaskPanelStates[subtaskId]);

const shouldPreserveLocalSubtask = (
  state: Pick<ShadowCloneStoreState, 'subtaskTranscriptStates' | 'subtaskPanelStates'>,
  subtask: ShadowCloneSubtask,
): boolean =>
  subtask.source === 'claude_sdk' || hasLocalSubtaskActivity(state, subtask.id);

const mergeLocalSubtaskLedger = (
  authoritativeSubtasks: ShadowCloneSubtask[],
  localSubtasks: ShadowCloneSubtask[],
  state: Pick<ShadowCloneStoreState, 'subtaskTranscriptStates' | 'subtaskPanelStates'>,
): ShadowCloneSubtask[] => {
  let next = authoritativeSubtasks;
  for (const localSubtask of localSubtasks) {
    if (
      next.some((subtask) => subtask.id === localSubtask.id) ||
      !shouldPreserveLocalSubtask(state, localSubtask)
    ) {
      continue;
    }
    next = upsertSubtask(next, localSubtask);
  }
  return next;
};

const ensurePanelState = (
  states: Record<string, ShadowClonePanelState>,
  subtaskId: string,
): ShadowClonePanelState => states[subtaskId] || createEmptyShadowClonePanelState();

const samePanelToolCall = (
  left: ShadowClonePanelToolCall,
  right: ShadowClonePanelToolCall,
): boolean =>
  left.assistantCall.name === right.assistantCall.name &&
  left.assistantCall.content === right.assistantCall.content &&
  left.assistantCall.toolCallId === right.assistantCall.toolCallId &&
  left.toolResult?.content === right.toolResult?.content &&
  left.toolResult?.isSuccess === right.toolResult?.isSuccess &&
  left.toolResult?.toolCallId === right.toolResult?.toolCallId;

const samePanelStateIgnoringSequence = (
  left: ShadowClonePanelState,
  right: ShadowClonePanelState,
): boolean =>
  left.streamingText === right.streamingText &&
  left.latestLabel === right.latestLabel &&
  left.toolCalls.length === right.toolCalls.length &&
  left.toolCalls.every((toolCall, index) =>
    samePanelToolCall(toolCall, right.toolCalls[index]),
  );

const stabilizePlanningPanelState = (
  currentPanelState: ShadowClonePanelState,
  nextPanelState: ShadowClonePanelState,
  activity: ShadowCloneActivityEnvelope,
): ShadowClonePanelState => {
  const metadata =
    activity.metadata && typeof activity.metadata === 'object'
      ? activity.metadata
      : {};
  const streamStatus = String(metadata.stream_status || '').trim();
  if (
    (streamStatus === 'reasoning_chunk' ||
      streamStatus === 'chunk' ||
      streamStatus === 'tool_call_chunk') &&
    samePanelStateIgnoringSequence(currentPanelState, nextPanelState)
  ) {
    if (nextPanelState.lastSequence === currentPanelState.lastSequence) {
      return currentPanelState;
    }

    return {
      ...currentPanelState,
      lastSequence: nextPanelState.lastSequence,
    };
  }

  return nextPanelState;
};

const ensureTranscriptState = (
  states: Record<string, ShadowCloneTranscriptState>,
  subtaskId: string,
): ShadowCloneTranscriptState =>
  states[subtaskId] || createEmptyShadowCloneTranscriptState();

const isTerminalSubtaskStatus = (
  status: ShadowCloneSubtaskStatus | undefined,
): boolean =>
  status === 'completed' ||
  status === 'failed' ||
  status === 'cancelled' ||
  status === 'timeout' ||
  status === 'denied';

const upsertTranscriptMessageById = (
  messages: UnifiedMessage[],
  nextMessage: UnifiedMessage,
): UnifiedMessage[] => {
  const existingIndex = messages.findIndex(
    (message) => message.message_id === nextMessage.message_id,
  );
  if (existingIndex === -1) {
    return messages.concat(nextMessage);
  }

  const existingMessage = messages[existingIndex];
  if (
    existingMessage.content === nextMessage.content &&
    existingMessage.metadata === nextMessage.metadata &&
    existingMessage.updated_at === nextMessage.updated_at
  ) {
    return messages;
  }

  const next = messages.slice();
  next[existingIndex] = nextMessage;
  return next;
};

const buildShadowCloneV2SubagentStartedTranscriptMessage = ({
  agentRunId,
  subtaskId,
  agentName,
  role,
  taskDescription,
  timestamp,
}: {
  agentRunId: string | null | undefined;
  subtaskId: string;
  agentName?: string;
  role?: string;
  taskDescription?: string;
  timestamp: string;
}): UnifiedMessage => {
  const runId = agentRunId || 'unknown-run';
  const roleText = role || agentName || '团队成员';
  const taskText = taskDescription
    ? `任务：${taskDescription}`
    : `角色：${roleText}`;
  return {
    sequence: 0,
    message_id: `shadow-clone-v2-progress:${runId}:${subtaskId}:started`,
    thread_id: `shadow-clone:${subtaskId}`,
    type: 'assistant',
    role: 'assistant',
    is_llm_message: false,
    content: JSON.stringify({
      role: 'assistant',
      content: `[系统进度] ${roleText} 已收到任务，开始执行。${taskText}`,
    }),
    metadata: JSON.stringify({
      stream_status: 'complete',
      source: 'shadow_clone_v2',
      agent_run_id: runId,
      agent_name: agentName || undefined,
      role: role || undefined,
      shadow_clone_subtask_id: subtaskId,
      subtask_id: subtaskId,
      shadow_clone_system_progress: true,
      message_kind: 'orchestration_progress',
      phase_reason: 'shadow_clone_v2_subagent_started_progress',
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};

const buildShadowCloneV2MainProjectionProgressMessage = ({
  agentRunId,
  sourceEventType,
  eventIndex,
  projection,
}: {
  agentRunId: string | null | undefined;
  sourceEventType: string;
  eventIndex: number;
  projection: any;
}): UnifiedMessage | null => {
  const runId = agentRunId || 'unknown-run';
  const timestamp = toNonEmptyString(projection?.updated_at) || new Date().toISOString();
  const members = projection?.team?.members;
  const memberCount = members && typeof members === 'object'
    ? Object.keys(members).length
    : 0;
  const tasks = projection?.tasks;
  const taskCount = tasks && typeof tasks === 'object'
    ? Object.keys(tasks).length
    : 0;
  let text = '';

  switch (sourceEventType) {
    case 'run_started':
      text = '我已收到请求，正在创建团队并规划任务。';
      break;
    case 'team_created':
      text = `我已创建 ${memberCount} 个团队成员，正在继续规划和分配任务。`;
      break;
    case 'task_created':
      text = `我正在分配任务，目前已生成 ${taskCount} 个任务。`;
      break;
    case 'task_claimed':
      text = '已有团队成员收到任务并开始执行，我正在监督进度并等待结果回传。';
      break;
    case 'tool_call_started':
      text = '团队成员正在调用工具执行任务，我会持续跟踪工具结果。';
      break;
    case 'run_completed':
      text = '团队成员结果已回传，正在完成最终汇总。';
      break;
    default:
      return null;
  }

  return {
    sequence: Number.isFinite(eventIndex) ? eventIndex : undefined,
    message_id: `shadow-clone-v2-main-progress:${runId}:${sourceEventType}:${eventIndex}`,
    thread_id: 'shadow-clone:main',
    type: 'assistant',
    role: 'assistant',
    is_llm_message: false,
    content: JSON.stringify({
      role: 'assistant',
      content: `[系统进度] ${text}`,
    }),
    metadata: JSON.stringify({
      stream_status: 'complete',
      source: 'shadow_clone_v2',
      agent_run_id: runId,
      shadow_clone_subtask_id: 'main',
      shadow_clone_system_progress: true,
      message_kind: 'orchestration_progress',
      phase_reason: 'shadow_clone_v2_projection_progress',
      source_event_type: sourceEventType,
      event_index: eventIndex,
      subagent_count: memberCount,
      task_count: taskCount,
    }),
    created_at: timestamp,
    updated_at: timestamp,
  };
};

const buildShadowCloneV2ProjectionSubagentProgressMessages = ({
  agentRunId,
  sourceEventType,
  eventIndex,
  projection,
}: {
  agentRunId: string | null | undefined;
  sourceEventType: string;
  eventIndex: number;
  projection: any;
}): UnifiedMessage[] => {
  if (sourceEventType !== 'task_claimed' && sourceEventType !== 'tool_call_started') {
    return [];
  }

  const runId = agentRunId || 'unknown-run';
  const timestamp = toNonEmptyString(projection?.updated_at) || new Date().toISOString();
  const tasks = projection?.tasks && typeof projection.tasks === 'object'
    ? Object.values(projection.tasks) as any[]
    : [];
  const activeTasks = tasks.filter((task) => {
    const status = toNonEmptyString(task?.status).toLowerCase();
    if (status === 'in_progress' || status === 'running') {
      return true;
    }

    if (sourceEventType !== 'task_claimed' && sourceEventType !== 'tool_call_started') {
      return false;
    }

    return Boolean(
      toNonEmptyString(task?.owner_agent) ||
      toNonEmptyString(task?.agent_name) ||
      toNonEmptyString(task?.agent_id),
    );
  });

  return activeTasks
    .map((task) => {
      const subtaskId = toNonEmptyString(task?.id || task?.task_id);
      if (!subtaskId) return null;
      const agentName = toNonEmptyString(task?.agent_name || task?.owner_agent || task?.metadata?.agent_name);
      const role = toNonEmptyString(task?.role || task?.subject);
      const text = sourceEventType === 'task_claimed'
        ? `${role || agentName || '团队成员'} 已收到任务，开始执行。`
        : `${role || agentName || '团队成员'} 正在调用工具推进任务。`;

      return {
        sequence: Number.isFinite(eventIndex) ? eventIndex : undefined,
        message_id: `shadow-clone-v2-progress:${runId}:${subtaskId}:${sourceEventType}:${eventIndex}`,
        thread_id: `shadow-clone:${subtaskId}`,
        type: 'assistant' as const,
        role: 'assistant',
        is_llm_message: false,
        content: JSON.stringify({
          role: 'assistant',
          content: `[系统进度] ${text}`,
        }),
        metadata: JSON.stringify({
          stream_status: 'complete',
          source: 'shadow_clone_v2',
          agent_run_id: runId,
          agent_name: agentName || undefined,
          role: role || undefined,
          shadow_clone_subtask_id: subtaskId,
          subtask_id: subtaskId,
          shadow_clone_system_progress: true,
          message_kind: 'orchestration_progress',
          phase_reason: 'shadow_clone_v2_projection_subagent_progress',
          source_event_type: sourceEventType,
          event_index: eventIndex,
        }),
        created_at: timestamp,
        updated_at: timestamp,
      } satisfies UnifiedMessage;
    })
    .filter((message): message is UnifiedMessage => Boolean(message));
};

const applyProjectionSubagentProgressMessages = (
  states: Record<string, ShadowCloneTranscriptState>,
  messages: UnifiedMessage[],
): Record<string, ShadowCloneTranscriptState> => {
  let nextStates = states;
  for (const message of messages) {
    const metadata = readV2ProjectionMessageMetadata(message);
    const subtaskId = toNonEmptyString(metadata.shadow_clone_subtask_id);
    if (!subtaskId) continue;

    const currentState = ensureTranscriptState(nextStates, subtaskId);
    const nextMessages = upsertTranscriptMessageById(
      currentState.messages,
      message,
    );
    if (nextMessages === currentState.messages) {
      continue;
    }

    nextStates = {
      ...nextStates,
      [subtaskId]: {
        ...currentState,
        messages: nextMessages,
        latestLabel: sourceEventLabel(metadata.source_event_type) || currentState.latestLabel,
      },
    };
  }
  return nextStates;
};

const sourceEventLabel = (sourceEventType: unknown): string | null => {
  switch (toNonEmptyString(sourceEventType)) {
    case 'task_claimed':
      return '正在执行任务';
    case 'tool_call_started':
      return '正在调用工具';
    default:
      return null;
  }
};

const upsertSyntheticCompleteToolCall = (
  toolCalls: ShadowClonePanelToolCall[],
  resultText: string,
  timestamp?: string,
): ShadowClonePanelToolCall[] => {
  const syntheticCall = buildShadowCloneCompleteToolCall(resultText, timestamp);
  const existingIndex = toolCalls.findIndex(
    (item) => item.assistantCall.toolCallId === 'shadow-clone-complete',
  );

  if (existingIndex === -1) {
    return toolCalls.concat(syntheticCall);
  }

  const next = toolCalls.slice();
  next[existingIndex] = syntheticCall;
  return next;
};

const markSyntheticCompleteTranscriptAsV2History = (
  messages: UnifiedMessage[],
  subtaskId: string,
  agentName?: string,
): UnifiedMessage[] =>
  messages.map((message) => {
    if (message.message_id !== `shadow-clone:${subtaskId}:assistant-complete`) {
      return message;
    }
    const metadata = readV2ProjectionMessageMetadata(message);
    return {
      ...message,
      metadata: JSON.stringify({
        ...metadata,
        shadow_clone_subtask_id: subtaskId,
        shadow_clone_v2_projection_transcript: true,
        v2_transcript_message_id:
          toNonEmptyString(metadata.v2_transcript_message_id) ||
          toNonEmptyString(message.message_id),
        agent_name: agentName || toNonEmptyString(metadata.agent_name) || undefined,
        source_event_type:
          toNonEmptyString(metadata.source_event_type) ||
          'selected_subtask_full_result',
      }),
    };
  });

let shadowCloneSyncSequence = 0;
const latestShadowCloneSyncByRun = new Map<string, number>();
const inFlightShadowCloneSyncByRun = new Map<string, Promise<void>>();

export const useShadowCloneStore = create<ShadowCloneStoreState>()(
  persist(
    (set, get) => ({
      mode: 'off',
      modeExplicitlySet: false,
      ...resetRuntimeState,

      setMode: (mode) =>
        set({
          mode,
          modeExplicitlySet: true,
        }),

      setActiveSubtask: (subtaskId) =>
        set({
          activeSubtaskId: subtaskId,
          viewScope: deriveViewScope(subtaskId),
        }),

      clearActiveSubtask: () =>
        set({
          activeSubtaskId: null,
          viewScope: deriveViewScope(null),
          detailError: null,
        }),

      returnToMainView: () =>
        set({
          activeSubtaskId: null,
          viewScope: deriveViewScope(null),
          detailError: null,
        }),

      resetRuntime: () => set(resetRuntimeState),

      applyLiveActivity: (activity, agentRunId) =>
        set((state) => {
          const nextLiveActivity =
            activity === undefined
              ? state.liveActivity
              : normalizeLiveActivity(activity);
          const nextRunId = agentRunId ?? state.currentRunId;

          if (
            sameLiveActivity(nextLiveActivity, state.liveActivity) &&
            nextRunId === state.currentRunId
          ) {
            return state;
          }

          return {
            liveActivity: nextLiveActivity,
            currentRunId: nextRunId,
          };
        }),

      applyMainTranscriptMessage: (message, agentRunId, liveActivity) =>
        set((state) => {
          const nextTranscriptState = applyShadowCloneTranscriptMessage(
            state.mainTranscriptState,
            message,
            'main',
          );
          const nextPhase = state.phase === 'idle' ? 'planning' : state.phase;
          const nextRunId = agentRunId ?? state.currentRunId;
          const nextLiveActivity =
            liveActivity === undefined
              ? state.liveActivity
              : resolveStableLiveActivity(liveActivity, null, state.liveActivity);

          if (
            nextTranscriptState === state.mainTranscriptState &&
            nextPhase === state.phase &&
            nextRunId === state.currentRunId &&
            nextLiveActivity === state.liveActivity
          ) {
            return state;
          }

          return {
            mainTranscriptState: nextTranscriptState,
            phase: nextPhase,
            currentRunId: nextRunId,
            liveActivity: nextLiveActivity,
          };
        }),

      handleSSEEvent: (streamStatus, content, liveActivity, agentRunId) => {
        const eventLiveActivity = normalizeLiveActivity(
          liveActivity === undefined ? content?.live_activity : liveActivity,
        );

        switch (streamStatus) {
          case 'shadow_clone_planning_started': {
            set((state) => ({
              ...(
                agentRunId &&
                state.currentRunId === agentRunId
                  ? {
                      currentRunId: agentRunId,
                      phase:
                        state.phase === 'idle'
                          ? 'planning'
                          : state.phase,
                    }
                  : {
                      ...resetRuntimeStateWithHistoricalV2Transcripts(state),
                      currentRunId: agentRunId ?? null,
                      phase: 'planning',
                    }
              ),
              liveActivity: resolveStableLiveActivity(
                eventLiveActivity,
                buildLiveActivity('shadow_clone_main', 'planning', 'planning_started'),
                state.liveActivity,
              ),
              lastError: null,
            }));
            return;
          }

          case 'shadow_clone_v2_started': {
            const resolvedRunId =
              agentRunId ||
              (typeof content?.agent_run_id === 'string' ? content.agent_run_id : null);

            set((state) => ({
              ...(
                resolvedRunId &&
                state.currentRunId === resolvedRunId
                  ? {
                      currentRunId: resolvedRunId,
                      phase:
                        state.phase === 'idle'
                          ? 'planning'
                          : state.phase,
                    }
                  : {
                      ...resetRuntimeStateWithHistoricalV2Transcripts(state),
                      currentRunId: resolvedRunId,
                      phase: 'planning',
                    }
              ),
              liveActivity: resolveStableLiveActivity(
                eventLiveActivity,
                buildLiveActivity('shadow_clone_main', 'planning', 'shadow_clone_v2_started'),
                state.liveActivity,
              ),
              lastError: null,
            }));
            return;
          }

          case 'shadow_clone_environment_preparing': {
            set((state) => ({
              phase: normalizePhase(undefined, state.phase, 'preparing'),
              environmentStatus: 'preparing',
              environmentReady: false,
              environmentConfirmationReady: false,
              environmentLastError: null,
              environmentManifest: state.environmentManifest,
              liveActivity: resolveStableLiveActivity(
                eventLiveActivity,
                buildLiveActivity('shadow_clone_main', 'planning', 'environment_preparing'),
                state.liveActivity,
              ),
              lastError: null,
            }));
            return;
          }

          case 'shadow_clone_environment_ready': {
            const manifest =
              content?.environment && typeof content.environment === 'object'
                ? content.environment
                : {};
            const confirmationReady =
              typeof manifest?.confirmation_ready === 'boolean'
                ? manifest.confirmation_ready
                : true;
            set((state) => ({
              phase: normalizePhase(undefined, state.phase, 'ready'),
              environmentStatus: 'ready',
              environmentReady: true,
              environmentConfirmationReady: confirmationReady,
              environmentManifest:
                manifest && typeof manifest === 'object'
                  ? (manifest as Record<string, unknown>)
                  : state.environmentManifest,
              environmentPreparedAt:
                typeof manifest?.prepared_at === 'string' ? manifest.prepared_at : state.environmentPreparedAt,
              sandboxId:
                manifest?.sandbox &&
                typeof manifest.sandbox === 'object' &&
                typeof (manifest.sandbox as { id?: unknown }).id === 'string'
                  ? String((manifest.sandbox as { id?: unknown }).id)
                  : state.sandboxId,
              sandboxType:
                manifest?.sandbox &&
                typeof manifest.sandbox === 'object' &&
                typeof (manifest.sandbox as { type?: unknown }).type === 'string'
                  ? String((manifest.sandbox as { type?: unknown }).type)
                  : state.sandboxType,
              sandboxBindingState:
                manifest?.sandbox &&
                typeof manifest.sandbox === 'object' &&
                typeof (manifest.sandbox as { binding_state?: unknown }).binding_state === 'string'
                  ? String((manifest.sandbox as { binding_state?: unknown }).binding_state)
                  : state.sandboxBindingState,
              environmentLastError: null,
              liveActivity: resolveStableLiveActivity(
                eventLiveActivity,
                buildLiveActivity('shadow_clone_main', 'confirming', 'environment_ready'),
                state.liveActivity,
              ),
              lastError: null,
            }));
            return;
          }

          case 'shadow_clone_environment_recovering': {
            set((state) => ({
              phase: normalizePhase(undefined, state.phase, 'recovering'),
              environmentStatus: 'recovering',
              environmentReady: false,
              environmentConfirmationReady: false,
              environmentLastError:
                typeof content?.message === 'string' ? content.message : state.environmentLastError,
              environmentManifest: state.environmentManifest,
              liveActivity: resolveStableLiveActivity(
                eventLiveActivity,
                buildLiveActivity('shadow_clone_main', 'execution', 'environment_recovering'),
                state.liveActivity,
              ),
              lastError: null,
            }));
            return;
          }

          case 'shadow_clone_proposed': {
            const subtasks = Array.isArray(content?.subtasks)
              ? content.subtasks.map((item: any) => ({
                  id: String(item?.id || ''),
                  role: String(item?.role || ''),
                  task_description: String(item?.task_description || ''),
                  status: 'pending' as ShadowCloneSubtaskStatus,
                }))
              : [];
            const dependencies = Array.isArray(content?.dependencies)
              ? content.dependencies
              : [];

            set((state) => {
              let nextSubtasks = state.subtasks.slice();
              for (const subtask of subtasks) {
                nextSubtasks = upsertSubtask(nextSubtasks, {
                  ...subtask,
                  status: 'pending',
                });
              }

              const activeSubtaskId = reconcileActiveSubtaskId(
                state.activeSubtaskId,
                nextSubtasks,
              );

              return {
                phase: normalizePhase('confirming', state.phase),
                viewScope: deriveViewScope(null),
                activeSubtaskId,
                subtasks: nextSubtasks,
                liveActivity: resolveStableLiveActivity(
                  eventLiveActivity,
                  buildLiveActivity('shadow_clone_main', 'confirming', 'proposal_created'),
                  state.liveActivity,
                ),
                dependencies,
                pendingCount: derivePendingCount(nextSubtasks),
                lastError: null,
                detailError: null,
              };
            });
            return;
          }

          case 'shadow_clone_v2_plan_created': {
            const incomingSubtasks = readV2SubtasksFromContent(content, 'pending');
            const dependencies = readV2DependenciesFromContent(content);
            const resolvedRunId =
              agentRunId ||
              (typeof content?.agent_run_id === 'string' ? content.agent_run_id : null);

            set((state) => {
              let nextSubtasks = state.subtasks.slice();
              for (const subtask of incomingSubtasks) {
                nextSubtasks = upsertSubtask(nextSubtasks, subtask);
              }

              const activeSubtaskId = reconcileActiveSubtaskId(
                state.activeSubtaskId,
                nextSubtasks,
              );

              return {
                currentRunId: resolvedRunId ?? state.currentRunId,
                phase: isTerminalShadowClonePhase(state.phase)
                  ? state.phase
                  : 'running',
                viewScope: deriveViewScope(activeSubtaskId),
                activeSubtaskId,
                subtasks: nextSubtasks,
                dependencies,
                pendingCount: derivePendingCount(nextSubtasks),
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity(
                        'shadow_clone_main',
                        'execution',
                        'shadow_clone_v2_plan_created',
                      ),
                      state.liveActivity,
                    ),
                lastError: null,
                detailError: activeSubtaskId ? state.detailError : null,
              };
            });
            return;
          }

          case 'shadow_clone_v2_projection': {
            const projection =
              content?.projection && typeof content.projection === 'object'
                ? content.projection
                : content;
            const incomingSubtasks = buildV2ProjectionSubtasks(
              projection,
              normalizeSubtaskStatus(projection?.status, 'pending'),
            );
            const dependencies = readV2DependenciesFromContent(projection);
            const finalOutput = readV2FinalOutputFromProjection(projection);
            const resolvedRunId =
              agentRunId ||
              (typeof content?.agent_run_id === 'string'
                ? content.agent_run_id
                : typeof projection?.agent_run_id === 'string'
                  ? projection.agent_run_id
                  : null);

            set((state) => {
              const baseState =
                resolvedRunId && state.currentRunId !== resolvedRunId
                  ? { ...state, ...resetRuntimeState }
                  : state;
              let nextSubtasks: ShadowCloneSubtask[] = [];
              for (const subtask of incomingSubtasks) {
                nextSubtasks = upsertSubtask(nextSubtasks, subtask);
              }
              const canonicalContract = normalizeCanonicalStatusContract(
                projection as ShadowCloneStatusResponse,
              );
              const nextPhase = canonicalContract
                ? canonicalContract.phase
                : normalizePhase(
                    projection?.status,
                    baseState.phase,
                    readEnvironmentStatus(projection as ShadowCloneStatusResponse) ||
                      undefined,
                  );
              const activeSubtaskId = preserveInspectionSubtaskId(
                baseState.activeSubtaskId,
                baseState.viewScope,
                nextSubtasks,
                nextPhase === 'running' || nextPhase === 'recovering',
              );
              const liveActivity = canonicalContract
                ? resolveStableLiveActivity(
                    projection?.live_activity,
                    canonicalContract.liveActivity,
                    baseState.liveActivity,
                  )
                : resolveStableLiveActivity(
                    projection?.live_activity,
                    inferLiveActivityFromStatus(
                      projection as ShadowCloneStatusResponse,
                      nextSubtasks,
                    ),
                    baseState.liveActivity,
                  );
              const transcriptSourceStates =
                resolvedRunId && state.currentRunId !== resolvedRunId
                  ? state.subtaskTranscriptStates
                  : baseState.subtaskTranscriptStates;
              const sourceEventType = toNonEmptyString(content?.v2_event_type);
              const eventIndex = Number(content?.event_index);
              const projectionSubagentProgressMessages =
                buildShadowCloneV2ProjectionSubagentProgressMessages({
                  agentRunId: resolvedRunId ?? baseState.currentRunId,
                  sourceEventType,
                  eventIndex,
                  projection,
                });
              const subtaskTranscriptStates = applyProjectionSubagentProgressMessages(
                applyV2ProjectionMessagesToTranscriptStates(
                  transcriptSourceStates,
                  projection,
                  nextSubtasks,
                  content?.event_index,
                ),
                projectionSubagentProgressMessages,
              );
              const mainProgressMessage = buildShadowCloneV2MainProjectionProgressMessage({
                agentRunId: resolvedRunId ?? baseState.currentRunId,
                sourceEventType,
                eventIndex,
                projection,
              });
              const subtaskPanelStates = applyV2ProjectionToolCallsToPanelStates(
                baseState.subtaskPanelStates,
                projection,
                nextSubtasks,
              );

              return {
                currentRunId: resolvedRunId ?? baseState.currentRunId,
                phase: nextPhase,
                viewScope: deriveViewScope(activeSubtaskId),
                activeSubtaskId,
                subtasks: nextSubtasks,
                dependencies,
                pendingCount: derivePendingCount(nextSubtasks),
                liveActivity,
                mainTranscriptState: {
                  ...baseState.mainTranscriptState,
                  messages: (() => {
                    const withProgress = mainProgressMessage
                      ? upsertTranscriptMessageById(
                          baseState.mainTranscriptState.messages,
                          mainProgressMessage,
                        )
                      : baseState.mainTranscriptState.messages;
                    return finalOutput
                      ? upsertSyntheticCompleteTranscriptMessage(
                          withProgress,
                          'main',
                          finalOutput.content,
                          finalOutput.created_at,
                        )
                      : withProgress;
                  })(),
                },
                subtaskTranscriptStates,
                subtaskPanelStates,
                lastError: null,
                detailError: activeSubtaskId ? baseState.detailError : null,
              };
            });
            return;
          }

          case 'subagent_started': {
            const subtaskId = String(content?.subtask_id || '');
            if (!subtaskId) return;
            set((state) => {
              const currentSubtask =
                state.subtasks.find((item) => item.id === subtaskId) || null;
              const isClaudeSDKSource =
                String(content?.source || '').trim() === 'claude_sdk';
              const isShadowCloneV2Source =
                String(content?.source || '').trim() === 'shadow_clone_v2';
              const incomingRole =
                typeof content?.role === 'string' ? String(content.role).trim() : '';
              const incomingTaskDescription =
                typeof content?.task_description === 'string'
                  ? String(content.task_description).trim()
                  : '';
              const nextRole =
                isClaudeSDKSource && incomingRole === 'teammate' && currentSubtask?.role
                  ? undefined
                  : incomingRole || undefined;
              const nextTaskDescription =
                isClaudeSDKSource && !incomingTaskDescription && currentSubtask?.task_description
                  ? undefined
                  : incomingTaskDescription || undefined;
              const incomingAgentName =
                typeof content?.agent_name === 'string'
                  ? String(content.agent_name).trim()
                  : '';
              const nextStatus = currentSubtask
                ? isTerminalSubtaskStatus(currentSubtask.status)
                  ? currentSubtask.status
                  : currentSubtask.status === 'running' ||
                      isClaudeSDKSource ||
                      isShadowCloneV2Source
                    ? 'running'
                    : 'pending'
                : isClaudeSDKSource || isShadowCloneV2Source
                  ? 'running'
                  : 'pending';
              const nextSubtasks = upsertSubtask(state.subtasks, {
                id: subtaskId,
                role: nextRole,
                task_description: nextTaskDescription,
                source: isClaudeSDKSource ? 'claude_sdk' : undefined,
                // Claude SDK subagents stream tool invocation before child activity.
                // Treat SDK-owned starts as visibly active, but preserve terminal states
                // and never downgrade a subtask that is already running.
                status: nextStatus,
                error: undefined,
                attempt_index:
                  typeof content?.attempt_index === 'number'
                    ? content.attempt_index
                    : undefined,
                failure_class: undefined,
              });
              const currentTranscriptState = ensureTranscriptState(
                state.subtaskTranscriptStates,
                subtaskId,
              );
              const startedProgressMessage = isShadowCloneV2Source
                ? buildShadowCloneV2SubagentStartedTranscriptMessage({
                    agentRunId: agentRunId ?? state.currentRunId,
                    subtaskId,
                    agentName: incomingAgentName || undefined,
                    role: nextRole || currentSubtask?.role,
                    taskDescription:
                      nextTaskDescription || currentSubtask?.task_description,
                    timestamp:
                      typeof content?.created_at === 'string'
                        ? String(content.created_at)
                        : new Date().toISOString(),
                  })
                : null;
              const nextTranscriptState = startedProgressMessage
                ? {
                    ...currentTranscriptState,
                    messages: upsertTranscriptMessageById(
                      currentTranscriptState.messages,
                      startedProgressMessage,
                    ),
                  }
                : currentTranscriptState;

              return {
                phase:
                  isPostExecutionShadowClonePhase(state.phase)
                    ? state.phase
                    : 'running',
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity(
                        'shadow_clone_main',
                        'execution',
                        'subagent_started',
                      ),
                      state.liveActivity,
                    ),
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
                subtaskTranscriptStates:
                  nextTranscriptState === currentTranscriptState
                    ? state.subtaskTranscriptStates
                    : {
                        ...state.subtaskTranscriptStates,
                        [subtaskId]: nextTranscriptState,
                      },
              };
            });
            return;
          }

          case 'subagent_activity': {
            const subtaskId = String(content?.subtask_id || '');
            if (!subtaskId) return;
            set((state) => {
              const currentSubtask =
                state.subtasks.find((item) => item.id === subtaskId) || null;
              const currentPanelState = ensurePanelState(
                state.subtaskPanelStates,
                subtaskId,
              );
              const currentTranscriptState = ensureTranscriptState(
                state.subtaskTranscriptStates,
                subtaskId,
              );
              const activity = content as ShadowCloneActivityEnvelope;
              const nextPanelState = stabilizePlanningPanelState(
                currentPanelState,
                applyShadowCloneActivity(
                  currentPanelState,
                  activity,
                ),
                activity,
              );
              const nextTranscriptState = applyShadowCloneTranscriptActivity(
                currentTranscriptState,
                activity,
              );
              const nextSubtasks = upsertSubtask(state.subtasks, {
                id: subtaskId,
                role:
                  typeof content?.role === 'string'
                    ? String(content.role)
                    : typeof content?.agent_name === 'string'
                      ? String(content.agent_name)
                    : undefined,
                status: isTerminalSubtaskStatus(currentSubtask?.status)
                  ? currentSubtask.status
                  : 'running',
                error:
                  isTerminalSubtaskStatus(currentSubtask?.status)
                    ? currentSubtask?.error
                    : undefined,
              });
              const nextLiveActivity = resolveStableLiveActivity(
                eventLiveActivity,
                buildLiveActivity('shadow_clone_main', 'execution', 'subagent_activity'),
                state.liveActivity,
              );
              const stableLiveActivity = isTerminalShadowClonePhase(state.phase)
                ? state.liveActivity
                : nextLiveActivity;

              if (
                nextPanelState === currentPanelState &&
                nextTranscriptState === currentTranscriptState &&
                nextSubtasks === state.subtasks &&
                stableLiveActivity === state.liveActivity
              ) {
                return state;
              }

              return {
                liveActivity: stableLiveActivity,
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
                subtaskPanelStates:
                  nextPanelState === currentPanelState
                    ? state.subtaskPanelStates
                    : {
                        ...state.subtaskPanelStates,
                        [subtaskId]: nextPanelState,
                      },
                subtaskTranscriptStates:
                  nextTranscriptState === currentTranscriptState
                    ? state.subtaskTranscriptStates
                    : {
                        ...state.subtaskTranscriptStates,
                        [subtaskId]: nextTranscriptState,
                      },
              };
            });
            return;
          }

          case 'subagent_completed': {
            const subtaskId = String(content?.subtask_id || '');
            if (!subtaskId) return;
            set((state) => {
              const nextSubtasks = upsertSubtask(state.subtasks, {
                id: subtaskId,
                status: 'completed',
                error: undefined,
                result_summary:
                  typeof content?.result_summary === 'string'
                    ? content.result_summary
                    : undefined,
                attempt_index:
                  typeof content?.attempt_index === 'number'
                    ? content.attempt_index
                    : undefined,
                failure_class: undefined,
              });

              return {
                phase: isPostExecutionShadowClonePhase(state.phase)
                  ? state.phase
                  : 'running',
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity(
                        'shadow_clone_main',
                        'execution',
                        'subagent_completed',
                      ),
                      state.liveActivity,
                    ),
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
              };
            });
            return;
          }

          case 'subagent_failed': {
            const subtaskId = String(content?.subtask_id || '');
            if (!subtaskId) return;
            set((state) => {
              const nextSubtasks = upsertSubtask(state.subtasks, {
                id: subtaskId,
                status: 'failed',
                error:
                  typeof content?.error === 'string' ? content.error : 'failed',
                attempt_index:
                  typeof content?.attempt_index === 'number'
                    ? content.attempt_index
                    : undefined,
                failure_class:
                  typeof content?.failure_class === 'string'
                    ? content.failure_class
                    : undefined,
              });

              return {
                phase: isPostExecutionShadowClonePhase(state.phase)
                  ? state.phase
                  : 'running',
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity(
                        'shadow_clone_main',
                        'execution',
                        'subagent_failed',
                      ),
                      state.liveActivity,
                    ),
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
              };
            });
            return;
          }

          case 'shadow_clone_subagent_recovering':
          case 'shadow_clone_subagent_wake_sent':
          case 'shadow_clone_subagent_replacement_completed':
          case 'shadow_clone_subagent_recovery_exhausted': {
            const subtaskId = String(content?.subtask_id || '');
            if (!subtaskId) return;
            set((state) => {
              const nextSubtasks = upsertSubtask(state.subtasks, {
                id: subtaskId,
                attempt_index:
                  typeof content?.attempt_index === 'number'
                    ? content.attempt_index
                    : undefined,
                failure_class:
                  typeof content?.failure_class === 'string'
                    ? content.failure_class
                    : undefined,
                recovery_mode:
                  typeof content?.recovery_mode === 'string'
                    ? content.recovery_mode
                    : undefined,
                recovery_phase:
                  typeof content?.recovery_phase === 'string'
                    ? content.recovery_phase
                    : undefined,
                recovery_reason:
                  typeof content?.message === 'string' ? content.message : undefined,
                recovery_handoff_summary:
                  typeof content?.handoff_summary === 'string'
                    ? content.handoff_summary
                    : undefined,
                replacement_context_id:
                  typeof content?.replacement_context_id === 'string'
                    ? content.replacement_context_id
                    : undefined,
              });

              return {
                phase:
                  isPostExecutionShadowClonePhase(state.phase)
                    ? state.phase
                    : 'recovering',
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity('shadow_clone_main', 'execution', streamStatus),
                      state.liveActivity,
                    ),
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
              };
            });
            return;
          }

          case 'shadow_clone_subagent_resumed':
          case 'shadow_clone_subagent_replacement_started': {
            const subtaskId = String(content?.subtask_id || '');
            if (!subtaskId) return;
            set((state) => {
              const nextSubtasks = upsertSubtask(state.subtasks, {
                id: subtaskId,
                status: 'running',
                error: undefined,
                attempt_index:
                  typeof content?.attempt_index === 'number'
                    ? content.attempt_index
                    : undefined,
                failure_class: undefined,
                recovery_mode:
                  typeof content?.recovery_mode === 'string'
                    ? content.recovery_mode
                    : undefined,
                recovery_phase:
                  typeof content?.recovery_phase === 'string'
                    ? content.recovery_phase
                    : undefined,
                recovery_reason:
                  typeof content?.message === 'string' ? content.message : undefined,
                recovery_handoff_summary:
                  typeof content?.handoff_summary === 'string'
                    ? content.handoff_summary
                    : undefined,
                replacement_context_id:
                  typeof content?.replacement_context_id === 'string'
                    ? content.replacement_context_id
                    : undefined,
              });

              return {
                phase:
                  isPostExecutionShadowClonePhase(state.phase)
                    ? state.phase
                    : 'running',
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity('shadow_clone_main', 'execution', streamStatus),
                      state.liveActivity,
                    ),
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
              };
            });
            return;
          }

          case 'heartbeat': {
            set((state) => ({
              pendingCount: derivePendingCount(state.subtasks),
              lastHeartbeatAt: Date.now(),
              liveActivity: isTerminalShadowClonePhase(state.phase)
                ? state.liveActivity
                : resolveStableLiveActivity(
                    eventLiveActivity,
                    buildLiveActivity('shadow_clone_main', 'execution', 'heartbeat'),
                    state.liveActivity,
                  ),
            }));
            return;
          }

          case 'shadow_clone_aggregating':
            set((state) => {
              const pinnedSubtask = preserveInspectionSubtaskId(
                state.activeSubtaskId,
                state.viewScope,
                state.subtasks,
                false,
              );

              return {
                phase: isTerminalShadowClonePhase(state.phase)
                  ? state.phase
                  : 'aggregating',
                viewScope: deriveViewScope(pinnedSubtask),
                activeSubtaskId: pinnedSubtask,
                detailError: pinnedSubtask ? state.detailError : null,
                liveActivity: isTerminalShadowClonePhase(state.phase)
                  ? state.liveActivity
                  : resolveStableLiveActivity(
                      eventLiveActivity,
                      buildLiveActivity('main_agent', 'aggregate', 'aggregate_started'),
                      state.liveActivity,
                    ),
              };
            });
            return;

          case 'shadow_clone_complete':
            set((state) => {
              const pinnedSubtask = preserveInspectionSubtaskId(
                state.activeSubtaskId,
                state.viewScope,
                state.subtasks,
                false,
              );

              return {
                phase: isTerminalShadowClonePhase(state.phase)
                  ? state.phase
                  : 'completed',
                viewScope: deriveViewScope(pinnedSubtask),
                activeSubtaskId: pinnedSubtask,
                pendingCount: 0,
                detailError: pinnedSubtask ? state.detailError : null,
                liveActivity: resolveStableLiveActivity(
                  eventLiveActivity,
                  null,
                  state.liveActivity,
                ),
              };
            });
            return;

          case 'shadow_clone_v2_execution_completed':
            set((state) => {
              const incomingSubtasks = readV2SubtasksFromContent(content, 'completed');
              let nextSubtasks = state.subtasks.slice();
              for (const subtask of incomingSubtasks) {
                nextSubtasks = upsertSubtask(nextSubtasks, {
                  ...subtask,
                  status: 'completed',
                  error: undefined,
                });
              }
              if (incomingSubtasks.length === 0) {
                nextSubtasks = markUnresolvedSubtasksCompleted(nextSubtasks);
              }
              const pinnedSubtask = preserveInspectionSubtaskId(
                state.activeSubtaskId,
                state.viewScope,
                nextSubtasks,
                false,
              );

              return {
                phase: isTerminalShadowClonePhase(state.phase)
                  ? state.phase
                  : 'completed',
                viewScope: deriveViewScope(pinnedSubtask),
                activeSubtaskId: pinnedSubtask,
                subtasks: nextSubtasks,
                pendingCount: 0,
                detailError: pinnedSubtask ? state.detailError : null,
                liveActivity: resolveStableLiveActivity(
                  eventLiveActivity,
                  null,
                  state.liveActivity,
                ),
              };
            });
            return;

          case 'shadow_clone_v2_execution_failed':
            set((state) => {
              const incomingSubtasks = readV2SubtasksFromContent(content, 'failed');
              let nextSubtasks = state.subtasks.slice();
              for (const subtask of incomingSubtasks) {
                nextSubtasks = upsertSubtask(nextSubtasks, subtask);
              }

              const failedTaskId =
                toNonEmptyString(content?.failed_task_id) ||
                toNonEmptyString(content?.subtask_id) ||
                toNonEmptyString(content?.task_id);
              if (failedTaskId) {
                nextSubtasks = upsertSubtask(nextSubtasks, {
                  id: failedTaskId,
                  status: 'failed',
                  error:
                    typeof content?.error === 'string'
                      ? content.error
                      : 'Shadow Clone V2 subagent failed.',
                });
              }

              const pinnedSubtask = preserveInspectionSubtaskId(
                state.activeSubtaskId,
                state.viewScope,
                nextSubtasks,
                false,
              );

              return {
                phase: 'error',
                viewScope: deriveViewScope(pinnedSubtask),
                activeSubtaskId: pinnedSubtask,
                subtasks: nextSubtasks,
                pendingCount: derivePendingCount(nextSubtasks),
                detailError: pinnedSubtask ? state.detailError : null,
                liveActivity: resolveStableLiveActivity(
                  eventLiveActivity,
                  null,
                  state.liveActivity,
                ),
                lastError:
                  typeof content?.error === 'string'
                    ? content.error
                    : 'Shadow Clone V2 execution failed.',
              };
            });
            return;

          default:
            return;
        }
      },

      syncRunData: async (agentRunId, options) => {
        const force = options?.force === true;
        const existingSync = inFlightShadowCloneSyncByRun.get(agentRunId);
        if (existingSync && !force) {
          return existingSync;
        }

        const syncPromise = (async () => {
          const syncId = ++shadowCloneSyncSequence;
          latestShadowCloneSyncByRun.set(agentRunId, syncId);

          const stateBeforeSync = get();
          if (
            !force &&
            stateBeforeSync.currentRunId &&
            stateBeforeSync.currentRunId !== agentRunId
          ) {
            return;
          }

          set((state) => ({
            isSyncing:
              !state.currentRunId || state.currentRunId === agentRunId
                ? true
                : state.isSyncing,
            lastError: null,
            currentRunId:
              state.currentRunId === null || state.currentRunId === agentRunId
                ? agentRunId
                : state.currentRunId,
          }));

          try {
            const [statusPayload, resultsPayload] = await Promise.all([
              getShadowCloneStatus(agentRunId),
              getShadowCloneResults(agentRunId),
            ]);

            if (latestShadowCloneSyncByRun.get(agentRunId) !== syncId) {
              return;
            }

            if (
              get().currentRunId &&
              get().currentRunId !== agentRunId &&
              !force
            ) {
              return;
            }

            set((state) => {
              if (
                latestShadowCloneSyncByRun.get(agentRunId) !== syncId ||
                (state.currentRunId && state.currentRunId !== agentRunId && !force)
              ) {
                return state;
              }

              const v2ProjectionSubtasks = buildV2ProjectionSubtasks(
                statusPayload,
                normalizeSubtaskStatus(statusPayload.status, 'pending'),
              );
              const plannedSubtasks = v2ProjectionSubtasks.length > 0
                ? v2ProjectionSubtasks
                : buildPlannedSubtasks(statusPayload);
              const subtasks = mergeRuntimeData(
                plannedSubtasks,
                statusPayload,
                resultsPayload.results || [],
              );
              const shouldPreserveLocalClaudeSDKLedger =
                state.subtasks.length > 0 &&
                state.subtasks.some((subtask) =>
                  shouldPreserveLocalSubtask(state, subtask),
                ) &&
                (
                  statusPayload.status === 'not_found' ||
                  normalizeCanonicalUiPhase(statusPayload.ui_phase) === 'subagents_running' ||
                  (
                    subtasks.length === 0 &&
                    state.subtasks.some((subtask) => subtask.source === 'claude_sdk')
                  )
                );
              const effectiveSubtasks = shouldPreserveLocalClaudeSDKLedger
                ? mergeLocalSubtaskLedger(subtasks, state.subtasks, state)
                : subtasks;
              const dependencies = Array.isArray(statusPayload.proposal?.dependencies)
                ? statusPayload.proposal.dependencies.map(mapDependency)
                : [];
              const pendingCount = derivePendingCount(effectiveSubtasks);
              const detailLoadingId =
                state.detailLoadingId &&
                effectiveSubtasks.some((item) => item.id === state.detailLoadingId)
                  ? state.detailLoadingId
                  : null;
              const canonicalContract = normalizeCanonicalStatusContract(statusPayload);
              const nextPhase = canonicalContract
                ? canonicalContract.phase
                : readRecoveryPending(statusPayload) ||
                    hasActiveSubagentRecovery(statusPayload)
                  ? 'recovering'
                  : normalizePhase(
                      statusPayload.status,
                      state.phase,
                      readEnvironmentStatus(statusPayload) || undefined,
                    );
              const shouldPreserveMissingInspectionSubtask =
                nextPhase === 'running' || nextPhase === 'recovering';
              const activeSubtaskId = preserveInspectionSubtaskId(
                state.activeSubtaskId,
                state.viewScope,
                effectiveSubtasks,
                shouldPreserveMissingInspectionSubtask,
              );
              const liveActivity = canonicalContract
                ? resolveStableLiveActivity(
                    canonicalContract.liveActivity,
                    null,
                    state.liveActivity,
                  )
                : resolveStableLiveActivity(
                      statusPayload.live_activity,
                      inferLiveActivityFromStatus(statusPayload, effectiveSubtasks),
                      state.liveActivity,
                    );
              const subtaskTranscriptStates =
                applyV2ProjectionMessagesToTranscriptStates(
                  state.subtaskTranscriptStates,
                  statusPayload,
                  effectiveSubtasks,
                );
              const subtaskPanelStates = applyV2ProjectionToolCallsToPanelStates(
                state.subtaskPanelStates,
                statusPayload,
                effectiveSubtasks,
              );

              return {
                currentRunId: agentRunId,
                phase: nextPhase,
                environmentStatus: readEnvironmentStatus(statusPayload),
                environmentReady: readEnvironmentReady(statusPayload),
                environmentConfirmationReady: readEnvironmentConfirmationReady(
                  statusPayload,
                ),
                environmentPreparedAt: readEnvironmentPreparedAt(statusPayload),
                environmentLastError: readEnvironmentLastError(statusPayload),
                environmentManifest: readEnvironmentManifest(statusPayload),
                sandboxId: readSandboxId(statusPayload),
                sandboxType: readSandboxType(statusPayload),
                sandboxBindingState: readSandboxBindingState(statusPayload),
                liveActivity,
                subtasks: effectiveSubtasks,
                dependencies,
                pendingCount,
                isSyncing: false,
                lastSyncedAt: Date.now(),
                activeSubtaskId,
                detailLoadingId,
                detailError: activeSubtaskId ? state.detailError : null,
                viewScope: deriveViewScope(activeSubtaskId),
                subtaskTranscriptStates,
                subtaskPanelStates,
                lastError: null,
              };
            });

            const activeSubtaskId = get().activeSubtaskId;
            const activeSubtask = get().subtasks.find(
              (item) => item.id === activeSubtaskId,
            );
            if (
              activeSubtaskId &&
              activeSubtask &&
              (activeSubtask.status === 'completed' ||
                activeSubtask.status === 'failed') &&
              !get().detailResults[activeSubtaskId]
            ) {
              void get().fetchSubtaskResult(agentRunId, activeSubtaskId);
            }
          } catch (error) {
            if (latestShadowCloneSyncByRun.get(agentRunId) !== syncId) {
              return;
            }
            if (get().currentRunId && get().currentRunId !== agentRunId) {
              return;
            }
            set({
              isSyncing: false,
              lastError:
                error instanceof Error ? error.message : 'Failed to sync shadow clone state',
            });
          }
        })();

        inFlightShadowCloneSyncByRun.set(agentRunId, syncPromise);

        return syncPromise.finally(() => {
          if (inFlightShadowCloneSyncByRun.get(agentRunId) === syncPromise) {
            inFlightShadowCloneSyncByRun.delete(agentRunId);
          }
        });
      },

      selectSubtask: async (agentRunId, subtaskId) => {
        set({
          activeSubtaskId: subtaskId,
          viewScope: deriveViewScope(subtaskId),
          detailError: null,
          currentRunId: agentRunId,
        });

        const state = get();
        const selected = state.subtasks.find((item) => item.id === subtaskId);
        const shouldSync =
          state.currentRunId !== agentRunId ||
          state.subtasks.length === 0 ||
          !selected ||
          (Boolean(state.lastSyncedAt) &&
            Date.now() - Number(state.lastSyncedAt) > AUTHORITATIVE_SYNC_STALENESS_MS);

        if (shouldSync) {
          void get().syncRunData(agentRunId);
        }

        if (
          selected &&
          (selected.status === 'completed' || selected.status === 'failed')
        ) {
          void get().fetchSubtaskResult(agentRunId, subtaskId);
        }
      },

      fetchSubtaskResult: async (agentRunId, subtaskId) => {
        if (get().detailResults[subtaskId]) {
          return;
        }

        set({ detailLoadingId: subtaskId, detailError: null, currentRunId: agentRunId });

        try {
          const response = await getShadowCloneFullResult(agentRunId, subtaskId);
          if (get().currentRunId && get().currentRunId !== agentRunId) {
            return;
          }

          set((state) => {
            const timestamp = new Date().toISOString();
            const selectedSubtask =
              state.subtasks.find((item) => item.id === subtaskId) || null;
            const currentTranscriptState = ensureTranscriptState(
              state.subtaskTranscriptStates,
              subtaskId,
            );
            const syntheticMessages = upsertSyntheticCompleteTranscriptMessage(
              currentTranscriptState.messages,
              subtaskId,
              response.result,
              timestamp,
            );

            return {
              detailResults: {
                ...state.detailResults,
                [subtaskId]: response.result,
              },
              subtaskPanelStates: {
                ...state.subtaskPanelStates,
                [subtaskId]: {
                  ...ensurePanelState(state.subtaskPanelStates, subtaskId),
                  toolCalls: upsertSyntheticCompleteToolCall(
                    ensurePanelState(state.subtaskPanelStates, subtaskId).toolCalls,
                    response.result,
                    timestamp,
                  ),
                },
              },
              subtaskTranscriptStates: {
                ...state.subtaskTranscriptStates,
                [subtaskId]: {
                  ...currentTranscriptState,
                  messages: markSyntheticCompleteTranscriptAsV2History(
                    syntheticMessages,
                    subtaskId,
                    selectedSubtask?.agent_name,
                  ),
                },
              },
              detailLoadingId:
                state.detailLoadingId === subtaskId ? null : state.detailLoadingId,
            };
          });
        } catch (error) {
          if (get().currentRunId && get().currentRunId !== agentRunId) {
            return;
          }

          const message =
            error instanceof Error
              ? error.message
              : 'Failed to load shadow clone detail';

          if (message.includes('(404)')) {
            set((state) => ({
              detailLoadingId:
                state.detailLoadingId === subtaskId ? null : state.detailLoadingId,
            }));
            return;
          }

          set((state) => ({
            detailLoadingId:
              state.detailLoadingId === subtaskId ? null : state.detailLoadingId,
            detailError: message,
          }));
        }
      },

      confirmProposal: async (agentRunId) => {
        const state = get();
        if (!state.environmentReady || !state.environmentConfirmationReady) {
          throw new Error(
            state.environmentLastError ||
              '共享环境尚未准备完成，请等待环境就绪后再批准执行。',
          );
        }
        await confirmShadowClone(agentRunId);
        set({
          currentRunId: agentRunId,
          lastError: null,
          detailError: null,
        });
      },

      denyProposal: async (agentRunId, reason) => {
        await denyShadowClone(agentRunId, reason);
        set({
          phase: 'denied',
          liveActivity: null,
          activeSubtaskId: null,
          viewScope: deriveViewScope(null),
          pendingCount: 0,
        });
      },

      // ── Subagent Inspection Streaming (SCV2 parity spec) ──

      initSubagentInspection: (subtaskId) => {
        set((state) => ({
          inspectionStreamStates: {
            ...state.inspectionStreamStates,
            [subtaskId]: {
              status: 'connecting',
              textContent: '',
              reasoningContent: '',
              isWritingFile: false,
              messages: [],
            },
          },
        }));
      },

      applySubagentStreamEvent: (subtaskId, event) => {
        set((state) => {
          const current = state.inspectionStreamStates[subtaskId];
          if (!current) return state;
          const updated = { ...current };
          if (event.status !== undefined) updated.status = event.status;
          if (event.isWritingFile !== undefined) updated.isWritingFile = event.isWritingFile;
          if (event.textContent !== undefined) {
            // REPLACE, not append: backend sends accumulated (full) text per chunk, not deltas
            updated.textContent = event.textContent;
          }
          if (event.reasoningContent !== undefined) {
            // REPLACE, not append: backend sends accumulated (full) text per chunk, not deltas
            updated.reasoningContent = event.reasoningContent;
          }
          if (event.message) {
            updated.messages = [...current.messages, event.message];
          }
          return {
            inspectionStreamStates: {
              ...state.inspectionStreamStates,
              [subtaskId]: updated,
            },
          };
        });
      },

      clearSubagentInspection: (subtaskId) => {
        set((state) => {
          if (!state.inspectionStreamStates[subtaskId]) return state;
          const next = { ...state.inspectionStreamStates };
          delete next[subtaskId];
          return { inspectionStreamStates: next };
        });
      },
    }),
    {
      name: 'shadow-clone-store',
      version: SHADOW_CLONE_STORE_VERSION,
      migrate: (persistedState) => ({
        ...coercePersistedShadowCloneState(persistedState),
        ...normalizePersistedShadowClonePreference(persistedState),
      }),
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        mode: state.mode,
        modeExplicitlySet: state.modeExplicitlySet,
      }),
    },
  ),
);
