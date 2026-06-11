const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'useAgentStream.ts'), 'utf8');

assert.match(
  source,
  /resolveClaudeSDKSubagentRoute/,
  'useAgentStream should resolve Claude SDK subagent routing metadata',
);

const routeBlockStart = source.indexOf('if (claudeSDKSubagentRoute) {');
assert.notEqual(routeBlockStart, -1, 'Claude SDK subagent route block should exist');
const switchStart = source.indexOf('switch (message.type)', routeBlockStart);
assert.notEqual(switchStart, -1, 'message switch should follow Claude route block');
const routeBlock = source.slice(routeBlockStart, switchStart);
assert.match(
  routeBlock,
  /shadowCloneStore\.handleSSEEvent\(/,
  'Claude SDK subagent route should update shadow clone store',
);
assert.match(
  routeBlock,
  /return;/,
  'Claude SDK subagent-routed events should return before the normal message switch so they do not render in the main thread',
);

console.log('useAgentStream Claude subagent routing contract passed');
