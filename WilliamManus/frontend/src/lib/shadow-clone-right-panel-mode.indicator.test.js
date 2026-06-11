import test from 'node:test';
import assert from 'node:assert/strict';

import { RIGHT_PANEL_MODE, shouldSuppressPrimaryThreadActivityForRightPanel } from './shadow-clone-right-panel-mode.ts';

// Per SCV2 parity spec (FR-011): indicator suppression was intentionally removed.
// The shouldSuppressPrimaryThreadActivityForRightPanel function is now used for
// message routing only, not for indicator gating.

test('shouldSuppressPrimaryThreadActivityForRightPanel returns false when side panel is closed', () => {
  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode: { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR },
      isSidePanelOpen: false,
    }),
    false,
  );
});

test('shouldSuppressPrimaryThreadActivityForRightPanel returns true for SHADOW_CLONE_MAIN_MONITOR with panel open', () => {
  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode: { kind: RIGHT_PANEL_MODE.SHADOW_CLONE_MAIN_MONITOR },
      isSidePanelOpen: true,
    }),
    true,
  );
});

test('shouldSuppressPrimaryThreadActivityForRightPanel returns false for NORMAL mode', () => {
  assert.equal(
    shouldSuppressPrimaryThreadActivityForRightPanel({
      rightPanelMode: { kind: RIGHT_PANEL_MODE.NORMAL },
      isSidePanelOpen: true,
    }),
    false,
  );
});
