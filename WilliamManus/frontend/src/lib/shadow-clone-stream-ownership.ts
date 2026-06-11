export type ShadowCloneStreamPhase = 'planning' | 'execution' | 'aggregate' | null;

export type ShadowCloneLiveScope = 'shadow_clone_main' | 'main_agent' | null;
export type ShadowCloneSemanticCategory = 'user_facing' | 'operational';
export type ShadowCloneCanonicalActivityOwner =
  | 'shadow_clone'
  | 'main_agent'
  | 'none';
export type ShadowCloneCanonicalUiPhase =
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
export type ShadowCloneProsePresentationOwner =
  | 'thread'
  | 'shadow_clone_monitor';

export interface ShadowCloneResolvedLiveActivity {
  scope: Exclude<ShadowCloneLiveScope, null>;
  phase: 'planning' | 'confirming' | 'execution' | 'aggregate';
  reason: string | null;
}

interface ResolveShadowCloneCanonicalRoutingInput {
  activityOwner?: unknown;
  uiPhase?: unknown;
  phaseReason?: unknown;
}

interface ShadowCloneCanonicalRouting {
  hasCanonicalContract: boolean;
  activityOwner: ShadowCloneCanonicalActivityOwner | null;
  uiPhase: ShadowCloneCanonicalUiPhase | null;
  phaseReason: string | null;
  liveActivity: ShadowCloneResolvedLiveActivity | null;
  streamPhase: ShadowCloneStreamPhase;
}

interface ShadowCloneStreamOwnershipInput {
  streamPhase: ShadowCloneStreamPhase;
  storePhase: string | null | undefined;
  liveActivityScope: ShadowCloneLiveScope;
  currentRunId: string | null | undefined;
  ownershipRunId: string | null | undefined;
  subtaskCount: number;
  semanticCategory?: ShadowCloneSemanticCategory;
  hasCanonicalContract?: boolean;
  canonicalActivityOwner?: ShadowCloneCanonicalActivityOwner | null;
  canonicalUiPhase?: ShadowCloneCanonicalUiPhase | null;
}

interface ShadowCloneStreamOwnership {
  isPlanningPhase: boolean;
  hasCanonicalContract: boolean;
  prosePresentationOwner: ShadowCloneProsePresentationOwner;
  shouldMirrorToShadowCloneMainTranscript: boolean;
  shouldSuppressLocalPlanningPresentation: boolean;
  shouldSuppressLocalMainTailPresentation: boolean;
  shouldSuppressLocalPresentation: boolean;
  shouldEmitFinalMessageToThread: boolean;
}

const SHADOW_CLONE_CANONICAL_ACTIVITY_OWNERS =
  ['shadow_clone', 'main_agent', 'none'] as const;
const SHADOW_CLONE_CANONICAL_UI_PHASES = [
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
] as const;

const normalizeCanonicalActivityOwner = (
  value: unknown,
): ShadowCloneCanonicalActivityOwner | null =>
  SHADOW_CLONE_CANONICAL_ACTIVITY_OWNERS.find((item) => item === value) || null;

const normalizeCanonicalUiPhase = (
  value: unknown,
): ShadowCloneCanonicalUiPhase | null =>
  SHADOW_CLONE_CANONICAL_UI_PHASES.find((item) => item === value) || null;

const normalizeCanonicalPhaseReason = (value: unknown): string | null =>
  typeof value === 'string' && value.trim().length > 0 ? value : null;

const inferCanonicalActivityOwner = (
  uiPhase: ShadowCloneCanonicalUiPhase,
): ShadowCloneCanonicalActivityOwner => {
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

const mapCanonicalUiPhaseToStreamPhase = (
  uiPhase: ShadowCloneCanonicalUiPhase,
): ShadowCloneStreamPhase => {
  switch (uiPhase) {
    case 'planning':
    case 'preparing_environment':
    case 'confirming':
      return 'planning';
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

const mapCanonicalUiPhaseToLivePhase = (
  uiPhase: ShadowCloneCanonicalUiPhase,
): ShadowCloneResolvedLiveActivity['phase'] | null => {
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

export const resolveShadowCloneCanonicalRouting = ({
  activityOwner,
  uiPhase,
  phaseReason,
}: ResolveShadowCloneCanonicalRoutingInput): ShadowCloneCanonicalRouting => {
  const normalizedUiPhase = normalizeCanonicalUiPhase(uiPhase);
  const normalizedActivityOwner =
    normalizeCanonicalActivityOwner(activityOwner) ||
    (normalizedUiPhase ? inferCanonicalActivityOwner(normalizedUiPhase) : null);
  const normalizedPhaseReason = normalizeCanonicalPhaseReason(phaseReason);
  const hasCanonicalContract =
    normalizedActivityOwner !== null || normalizedUiPhase !== null;

  if (!hasCanonicalContract) {
    return {
      hasCanonicalContract: false,
      activityOwner: null,
      uiPhase: null,
      phaseReason: null,
      liveActivity: null,
      streamPhase: null,
    };
  }

  const livePhase = normalizedUiPhase
    ? mapCanonicalUiPhaseToLivePhase(normalizedUiPhase)
    : null;
  const liveActivity: ShadowCloneResolvedLiveActivity | null =
    normalizedActivityOwner === 'none' || !livePhase
      ? null
      : {
          scope:
            normalizedActivityOwner === 'shadow_clone'
              ? 'shadow_clone_main'
              : 'main_agent',
          phase: livePhase,
          reason: normalizedPhaseReason,
        };

  return {
    hasCanonicalContract: true,
    activityOwner: normalizedActivityOwner,
    uiPhase: normalizedUiPhase,
    phaseReason: normalizedPhaseReason,
    liveActivity,
    streamPhase: normalizedUiPhase
      ? mapCanonicalUiPhaseToStreamPhase(normalizedUiPhase)
      : null,
  };
};

export const deriveShadowCloneStreamOwnership = ({
  streamPhase,
  storePhase,
  liveActivityScope,
  currentRunId,
  ownershipRunId,
  subtaskCount,
  semanticCategory = 'operational',
  hasCanonicalContract = false,
  canonicalActivityOwner = null,
  canonicalUiPhase = null,
}: ShadowCloneStreamOwnershipInput): ShadowCloneStreamOwnership => {
  const isPlanningPhase = streamPhase === 'planning';
  const isUserFacingCategory = semanticCategory === 'user_facing';
  const canonicalPlanningLikePhase =
    canonicalUiPhase === 'planning' ||
    canonicalUiPhase === 'preparing_environment' ||
    canonicalUiPhase === 'confirming';
  const prefersCanonicalContract =
    hasCanonicalContract ||
    canonicalActivityOwner !== null ||
    canonicalUiPhase !== null;
  const isMainTailPhase =
    storePhase === 'aggregating' || storePhase === 'completed';
  const shouldMirrorMainAgentTailTranscript =
    Boolean(currentRunId) &&
    currentRunId === ownershipRunId &&
    subtaskCount > 0 &&
    (liveActivityScope === 'main_agent' || isMainTailPhase);
  const legacyProsePresentationOwner: ShadowCloneProsePresentationOwner =
    !isUserFacingCategory &&
    (isPlanningPhase ||
      shouldMirrorMainAgentTailTranscript ||
      streamPhase === 'aggregate')
      ? 'shadow_clone_monitor'
      : 'thread';
  const canonicalShadowCloneOwnsProse =
    canonicalActivityOwner === 'shadow_clone' && !isUserFacingCategory;
  const prosePresentationOwner: ShadowCloneProsePresentationOwner =
    prefersCanonicalContract
      ? canonicalShadowCloneOwnsProse
        ? 'shadow_clone_monitor'
        : 'thread'
      : legacyProsePresentationOwner;
  const shouldMirrorToShadowCloneMainTranscript = prefersCanonicalContract
    ? prosePresentationOwner === 'shadow_clone_monitor' && isUserFacingCategory
    : Boolean(streamPhase) || shouldMirrorMainAgentTailTranscript;
  const shouldSuppressLocalPlanningPresentation =
    prosePresentationOwner === 'shadow_clone_monitor' &&
    (prefersCanonicalContract ? canonicalPlanningLikePhase : isPlanningPhase);
  const shouldSuppressLocalMainTailPresentation =
    prosePresentationOwner === 'shadow_clone_monitor' &&
    !shouldSuppressLocalPlanningPresentation;

  return {
    isPlanningPhase,
    hasCanonicalContract: prefersCanonicalContract,
    prosePresentationOwner,
    shouldMirrorToShadowCloneMainTranscript,
    shouldSuppressLocalPlanningPresentation,
    shouldSuppressLocalMainTailPresentation,
    shouldSuppressLocalPresentation:
      prosePresentationOwner === 'shadow_clone_monitor',
    shouldEmitFinalMessageToThread: prosePresentationOwner === 'thread',
  };
};
