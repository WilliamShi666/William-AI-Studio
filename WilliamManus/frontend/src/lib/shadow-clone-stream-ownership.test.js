import test from 'node:test';
import assert from 'node:assert/strict';

import {
  deriveShadowCloneStreamOwnership,
  resolveShadowCloneCanonicalRouting,
} from './shadow-clone-stream-ownership.ts';

test('planning phase uses shadow clone transcript as the single live owner', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: 'planning',
    storePhase: 'planning',
    liveActivityScope: 'shadow_clone_main',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 0,
    semanticCategory: 'operational',
  });

  assert.equal(ownership.isPlanningPhase, true);
  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, true);
  assert.equal(ownership.shouldSuppressLocalPlanningPresentation, true);
  assert.equal(ownership.shouldSuppressLocalPresentation, true);
  assert.equal(ownership.shouldEmitFinalMessageToThread, false);
});

test('aggregate phase still suppresses local presentation but keeps final thread emission', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: 'aggregate',
    storePhase: 'aggregating',
    liveActivityScope: 'main_agent',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 3,
    semanticCategory: 'operational',
  });

  assert.equal(ownership.isPlanningPhase, false);
  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, true);
  assert.equal(ownership.shouldSuppressLocalMainTailPresentation, true);
  assert.equal(ownership.shouldSuppressLocalPresentation, true);
  assert.equal(ownership.shouldEmitFinalMessageToThread, false);
});

test('main-agent tail mirroring without explicit stream phase still suppresses local presentation', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: null,
    storePhase: 'running',
    liveActivityScope: 'main_agent',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 2,
    semanticCategory: 'operational',
  });

  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, true);
  assert.equal(ownership.shouldSuppressLocalMainTailPresentation, true);
  assert.equal(ownership.shouldSuppressLocalPresentation, true);
  assert.equal(ownership.shouldEmitFinalMessageToThread, false);
});

test('ordinary non-shadow-clone assistant output stays on the normal thread surface', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: null,
    storePhase: 'idle',
    liveActivityScope: null,
    currentRunId: null,
    ownershipRunId: 'run-1',
    subtaskCount: 0,
    semanticCategory: 'user_facing',
  });

  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, false);
  assert.equal(ownership.shouldSuppressLocalPresentation, false);
  assert.equal(ownership.shouldEmitFinalMessageToThread, true);
});

test('canonical planning-like user-facing prose is owned by the thread', () => {
  for (const canonicalUiPhase of [
    'planning',
    'preparing_environment',
    'confirming',
  ]) {
    const ownership = deriveShadowCloneStreamOwnership({
      streamPhase: 'planning',
      storePhase: 'planning',
      liveActivityScope: 'shadow_clone_main',
      currentRunId: 'run-1',
      ownershipRunId: 'run-1',
      subtaskCount: 0,
      semanticCategory: 'user_facing',
      canonicalActivityOwner: 'shadow_clone',
      canonicalUiPhase,
    });

    assert.equal(ownership.prosePresentationOwner, 'thread');
    assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, false);
    assert.equal(ownership.shouldSuppressLocalPlanningPresentation, false);
    assert.equal(ownership.shouldSuppressLocalPresentation, false);
    assert.equal(ownership.shouldEmitFinalMessageToThread, true);
  }
});

test('canonical main-agent planning narrative stays user-facing instead of becoming operational progress', () => {
  const route = resolveShadowCloneCanonicalRouting({
    activityOwner: 'main_agent',
    uiPhase: 'planning',
    phaseReason: 'shadow_clone_v2_main_agent_planning_output',
  });
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: route.streamPhase,
    storePhase: 'planning',
    liveActivityScope: route.liveActivity?.scope ?? null,
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 1,
    semanticCategory: 'user_facing',
    hasCanonicalContract: route.hasCanonicalContract,
    canonicalActivityOwner: route.activityOwner,
    canonicalUiPhase: route.uiPhase,
  });

  assert.equal(route.activityOwner, 'main_agent');
  assert.equal(route.uiPhase, 'planning');
  assert.equal(ownership.prosePresentationOwner, 'thread');
  assert.equal(ownership.shouldSuppressLocalPresentation, false);
  assert.equal(ownership.shouldEmitFinalMessageToThread, true);
});

test('aggregate narrative remains locally rendered even when shadow clone owns the operational tail', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: 'aggregate',
    storePhase: 'aggregating',
    liveActivityScope: 'main_agent',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 3,
    semanticCategory: 'user_facing',
    canonicalActivityOwner: 'main_agent',
    canonicalUiPhase: 'main_agent_continuation',
  });

  assert.equal(ownership.prosePresentationOwner, 'thread');
  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, false);
  assert.equal(ownership.shouldSuppressLocalMainTailPresentation, false);
  assert.equal(ownership.shouldSuppressLocalPresentation, false);
  assert.equal(ownership.shouldEmitFinalMessageToThread, true);
});

test('main-agent narrative is not blanked by shadow clone tail ownership metadata alone', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: null,
    storePhase: 'completed',
    liveActivityScope: 'main_agent',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 2,
    semanticCategory: 'user_facing',
  });

  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, true);
  assert.equal(ownership.shouldSuppressLocalMainTailPresentation, false);
  assert.equal(ownership.shouldSuppressLocalPresentation, false);
  assert.equal(ownership.shouldEmitFinalMessageToThread, true);
});

test('canonical main-agent continuation keeps prose in thread and disables monitor transcript mirroring', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: 'aggregate',
    storePhase: 'aggregating',
    liveActivityScope: 'main_agent',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 3,
    semanticCategory: 'user_facing',
    canonicalActivityOwner: 'main_agent',
    canonicalUiPhase: 'main_agent_continuation',
  });

  assert.equal(ownership.prosePresentationOwner, 'thread');
  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, false);
  assert.equal(ownership.shouldSuppressLocalPresentation, false);
  assert.equal(ownership.shouldEmitFinalMessageToThread, true);
});

test('canonical shadow clone operational events keep prose ownership on the monitor surface', () => {
  const ownership = deriveShadowCloneStreamOwnership({
    streamPhase: 'planning',
    storePhase: 'planning',
    liveActivityScope: 'shadow_clone_main',
    currentRunId: 'run-1',
    ownershipRunId: 'run-1',
    subtaskCount: 0,
    semanticCategory: 'operational',
    canonicalActivityOwner: 'shadow_clone',
    canonicalUiPhase: 'confirming',
  });

  assert.equal(ownership.prosePresentationOwner, 'shadow_clone_monitor');
  assert.equal(ownership.shouldMirrorToShadowCloneMainTranscript, false);
  assert.equal(ownership.shouldSuppressLocalPresentation, true);
  assert.equal(ownership.shouldEmitFinalMessageToThread, false);
});

test('canonical routing maps main-agent continuation into aggregate live activity without legacy fallback guessing', () => {
  const route = resolveShadowCloneCanonicalRouting({
    activityOwner: 'main_agent',
    uiPhase: 'main_agent_continuation',
    phaseReason: 'subtasks_finished',
  });

  assert.equal(route.hasCanonicalContract, true);
  assert.equal(route.activityOwner, 'main_agent');
  assert.equal(route.uiPhase, 'main_agent_continuation');
  assert.deepEqual(route.liveActivity, {
    scope: 'main_agent',
    phase: 'aggregate',
    reason: 'subtasks_finished',
  });
  assert.equal(route.streamPhase, 'aggregate');
});

test('canonical completion with owner none clears live activity instead of falling back to stale legacy scope', () => {
  const route = resolveShadowCloneCanonicalRouting({
    activityOwner: 'none',
    uiPhase: 'completed',
    phaseReason: 'final_summary_delivered',
  });

  assert.equal(route.hasCanonicalContract, true);
  assert.equal(route.activityOwner, 'none');
  assert.equal(route.uiPhase, 'completed');
  assert.equal(route.liveActivity, null);
  assert.equal(route.streamPhase, null);
});
