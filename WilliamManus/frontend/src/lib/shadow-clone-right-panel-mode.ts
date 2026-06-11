'use client';

import type {
  ShadowCloneLiveScope,
  ShadowClonePhase,
  ShadowCloneViewScope,
} from '@/lib/stores/shadow-clone-store';

const ACTIVE_AGENT_PANEL_STATUSES = new Set(['running', 'connecting']);

export const RIGHT_PANEL_MODE = {
  NORMAL: 'NORMAL',
  SHADOW_CLONE_CONFIRMATION: 'SHADOW_CLONE_CONFIRMATION',
  SHADOW_CLONE_MAIN_MONITOR: 'SHADOW_CLONE_MAIN_MONITOR',
  SHADOW_CLONE_HANDOFF: 'SHADOW_CLONE_HANDOFF',
  SUBAGENT_INSPECTION: 'SUBAGENT_INSPECTION',
} as const;

export type RightPanelMode =
  | { kind: typeof RIGHT_PANEL_MODE.NORMAL }
  | { kind: typeof RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION }
  | { kind: typeof RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR }
  | { kind: typeof RIGHT_PANEL_MODE.SHADOW_CLONE_HANDOFF }
  | {
      kind: typeof RIGHT_PANEL_MODE.SUBAGENT_INSPECTION;
      subtaskId: string;
    };

export interface RightPanelModeInput {
  phase: ShadowClonePhase;
  viewScope?: ShadowCloneViewScope | null;
  activeSubtaskId?: string | null;
  hasActiveSubtask: boolean;
  subtaskCount: number;
  liveActivityScope?: ShadowCloneLiveScope | null;
  agentStatus: string;
}

export const isRightPanelAgentActive = (agentStatus: string): boolean =>
  ACTIVE_AGENT_PANEL_STATUSES.has(agentStatus);

export const getRightPanelMode = ({
  phase,
  viewScope,
  activeSubtaskId,
  hasActiveSubtask,
  subtaskCount,
  liveActivityScope,
  agentStatus,
}: RightPanelModeInput): RightPanelMode => {
  if (viewScope === 'shadow_clone_subagent' && activeSubtaskId) {
    return hasActiveSubtask
      ? {
          kind: RIGHT_PANEL_MODE.SUBAGENT_INSPECTION,
          subtaskId: activeSubtaskId,
        }
      : { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR };
  }

  if (
    phase === 'planning' ||
    phase === 'preparing' ||
    phase === 'confirming' ||
    phase === 'recovering'
  ) {
    return { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION };
  }

  if (subtaskCount === 0) {
    return { kind: RIGHT_PANEL_MODE.NORMAL };
  }

  const isAgentActive = isRightPanelAgentActive(agentStatus);

  if (liveActivityScope === 'main_agent' && isAgentActive) {
    return { kind: RIGHT_PANEL_MODE.NORMAL };
  }

  if (
    phase === 'aggregating' ||
    phase === 'completed' ||
    phase === 'error' ||
    phase === 'cancelled' ||
    phase === 'timeout'
  ) {
    return isAgentActive
      ? { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_HANDOFF }
      : { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR };
  }

  if (
    phase === 'running' ||
    (liveActivityScope === 'shadow_clone_main' && isAgentActive)
  ) {
    return { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR };
  }

  return { kind: RIGHT_PANEL_MODE.NORMAL };
};

export const isRightPanelInspectionMode = (
  rightPanelMode: RightPanelMode,
): rightPanelMode is Extract<
  RightPanelMode,
  { kind: typeof RIGHT_PANEL_MODE.SUBAGENT_INSPECTION }
> => rightPanelMode.kind === RIGHT_PANEL_MODE.SUBAGENT_INSPECTION;

export const isRightPanelWorkspaceMode = (
  rightPanelMode: RightPanelMode,
): boolean =>
  rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION ||
  rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR ||
  rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_HANDOFF ||
  rightPanelMode.kind === RIGHT_PANEL_MODE.SUBAGENT_INSPECTION;

export const shouldAutoOpenRightPanelMode = (
  rightPanelMode: RightPanelMode,
): boolean =>
  rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION ||
  rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR;

export const shouldSuppressPrimaryThreadActivityForRightPanel = ({
  rightPanelMode,
  isSidePanelOpen,
}: {
  rightPanelMode: RightPanelMode;
  isSidePanelOpen: boolean;
}): boolean =>
  isSidePanelOpen &&
  (rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION ||
    rightPanelMode.kind === RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR ||
    rightPanelMode.kind === RIGHT_PANEL_MODE.SUBAGENT_INSPECTION);

// SCV2 parity spec (FR-011): the FileWritingIndicator (blue bar) MUST NOT
// be suppressed based on right panel state. Each panel independently manages
// its own indicator. The indicator gating was intentionally removed from
// shouldSuppressPrimaryThreadActivityForRightPanel (now used for message
// routing only). Do not reintroduce indicator suppression here.
