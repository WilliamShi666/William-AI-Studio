import test from 'node:test';
import assert from 'node:assert/strict';

import {
  getRightPanelMode,
  RIGHT_PANEL_MODE,
  shouldSuppressPrimaryThreadActivityForRightPanel,
} from './shadow-clone-right-panel-mode.ts';

test('closed confirmation surface does not suppress the primary-thread activity cue', () => {
  const rightPanelMode = getRightPanelMode({
    phase: 'planning',
    viewScope: 'shadow_clone_main',
    activeSubtaskId: null,
    hasActiveSubtask: false,
    subtaskCount: 1,
    liveActivityScope: 'shadow_clone_main',
    agentStatus: 'running',
  });

  assert.equal(rightPanelMode.kind, RIGHT_PANEL_MODE.SHADOW_CLONE_CONFIRMATION);
  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode,
      isSidePanelOpen: false,
    }),
    false,
  );
});

test('open confirmation or monitor surfaces suppress the primary-thread activity cue', () => {
  const confirmationMode = getRightPanelMode({
    phase: 'confirming',
    viewScope: 'shadow_clone_main',
    activeSubtaskId: null,
    hasActiveSubtask: false,
    subtaskCount: 1,
    liveActivityScope: 'shadow_clone_main',
    agentStatus: 'running',
  });
  const mainMonitorMode = getRightPanelMode({
    phase: 'running',
    viewScope: 'shadow_clone_main',
    activeSubtaskId: null,
    hasActiveSubtask: false,
    subtaskCount: 1,
    liveActivityScope: 'shadow_clone_main',
    agentStatus: 'running',
  });

  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode: confirmationMode,
      isSidePanelOpen: true,
    }),
    true,
  );
  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode: mainMonitorMode,
      isSidePanelOpen: true,
    }),
    true,
  );
});

test('open teammate rows enter subagent inspection and suppress the primary-thread activity cue', () => {
  const rightPanelMode = getRightPanelMode({
    phase: 'running',
    viewScope: 'shadow_clone_subagent',
    activeSubtaskId: 'subtask-1',
    hasActiveSubtask: true,
    subtaskCount: 2,
    liveActivityScope: 'shadow_clone_main',
    agentStatus: 'running',
  });

  assert.deepEqual(rightPanelMode, {
    kind: RIGHT_PANEL_MODE.SUBAGENT_INSPECTION,
    subtaskId: 'subtask-1',
  });
  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode,
      isSidePanelOpen: true,
    }),
    true,
  );
});

test('clicked subagent keeps inspection available after the main run becomes idle', () => {
  const rightPanelMode = getRightPanelMode({
    phase: 'running',
    viewScope: 'shadow_clone_subagent',
    activeSubtaskId: 'toolu-claude-1',
    hasActiveSubtask: true,
    subtaskCount: 1,
    liveActivityScope: 'shadow_clone_main',
    agentStatus: 'idle',
  });

  assert.deepEqual(rightPanelMode, {
    kind: RIGHT_PANEL_MODE.SUBAGENT_INSPECTION,
    subtaskId: 'toolu-claude-1',
  });
});

test('clicked subagent keeps the main monitor available before the synced subtask list catches up', () => {
  const rightPanelMode = getRightPanelMode({
    phase: 'running',
    viewScope: 'shadow_clone_subagent',
    activeSubtaskId: 'toolu-claude-2',
    hasActiveSubtask: false,
    subtaskCount: 3,
    liveActivityScope: 'shadow_clone_main',
    agentStatus: 'running',
  });

  assert.deepEqual(rightPanelMode, {
    kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR,
  });
});

test('completed shadow clone runs keep the team monitor available for replay when subtasks exist', () => {
  const rightPanelMode = getRightPanelMode({
    phase: 'completed',
    viewScope: 'shadow_clone_main',
    activeSubtaskId: null,
    hasActiveSubtask: false,
    subtaskCount: 2,
    liveActivityScope: null,
    agentStatus: 'idle',
  });

  assert.deepEqual(rightPanelMode, {
    kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR,
  });
});

test('errored shadow clone runs keep the team monitor available when subtasks exist', () => {
  const rightPanelMode = getRightPanelMode({
    phase: 'error',
    viewScope: 'shadow_clone_main',
    activeSubtaskId: null,
    hasActiveSubtask: false,
    subtaskCount: 2,
    liveActivityScope: null,
    agentStatus: 'idle',
  });

  assert.deepEqual(rightPanelMode, {
    kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR,
  });
});
