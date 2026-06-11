const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'shadow-clone-main-monitor.tsx'), 'utf8');

assert.match(
  source,
  /selectSubtask/,
  'ShadowCloneMainMonitor should wire row clicks to the shadow clone store selection action',
);

const handlerStart = source.indexOf('const handleSelectSubtask = React.useCallback');
assert.notEqual(handlerStart, -1, 'ShadowCloneMainMonitor should define a row select handler');
const handlerBlock = source.slice(handlerStart, handlerStart + 500);

assert.doesNotMatch(
  handlerBlock,
  /\(_subtaskId: string\) => \{\}/,
  'ShadowCloneMainMonitor row select handler must not be a no-op',
);
assert.match(
  handlerBlock,
  /selectSubtask\(/,
  'ShadowCloneMainMonitor row select handler should call selectSubtask',
);
assert.match(
  source,
  /data-testid="shadow-clone-monitor-row"/,
  'ShadowCloneMainMonitor rows need a stable selector for E2E subagent panel assertions',
);
assert.match(
  source,
  /data-subtask-id=\{row\.id\}/,
  'ShadowCloneMainMonitor rows should expose the stable subtask id for click-through E2E tests',
);

assert.match(
  source,
  /data-agent-name=\{row\.agentName \|\| undefined\}/,
  'ShadowCloneMainMonitor rows should expose stable agent names for pre-broadcast identity E2E assertions',
);
assert.match(
  source,
  /data-agent-id=\{row\.agentId \|\| undefined\}/,
  'ShadowCloneMainMonitor rows should expose stable agent ids for pre-broadcast identity E2E assertions',
);

console.log('ShadowCloneMainMonitor row selection contract passed');
