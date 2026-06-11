const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'tool-call-side-panel.tsx'), 'utf8');

assert.match(
  source,
  /shadow-clone-selected-subagent-tool-panel/,
  'ToolCallSidePanel should expose a stable selected-subagent tool panel test id',
);
assert.match(
  source,
  /data-subtask-id=\{isShadowCloneContextActive \? activeSubtaskId \|\| undefined : undefined\}/,
  'Selected subagent tool panel should bind the active subtask id for scoped E2E assertions',
);
assert.match(
  source,
  /isRightPanelInspectionMode\(rightPanelMode\)/,
  'Selected subagent tool panel selector should only activate in inspection mode',
);

console.log('ToolCallSidePanel selected subagent selector contract passed');
