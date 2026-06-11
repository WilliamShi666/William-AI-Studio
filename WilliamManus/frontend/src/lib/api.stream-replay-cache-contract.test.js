const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'api.ts'), 'utf8');

const getAgentStatusMatch = source.match(
  /export const getAgentStatus = async[\s\S]*?\n\};\n\nexport const getAgentRuns/,
);
assert.ok(getAgentStatusMatch, 'expected getAgentStatus implementation in api.ts');
assert.doesNotMatch(
  getAgentStatusMatch[0],
  /if\s*\(\s*data\.status\s*!==\s*['"]running['"]\s*\)\s*\{\s*addToNonRunning\(agentRunId\);/s,
  'completed/stopped runs must remain replayable; only true not-found responses should enter nonRunningAgentRuns',
);

const completedStatusMatch = source.match(
  /if\s*\(\s*jsonData\?\.type\s*===\s*['"]status['"][\s\S]*?jsonData\?\.status\s*===\s*['"]completed['"][\s\S]*?return;\s*\n\s*\}/,
);
assert.ok(completedStatusMatch, 'expected completed status branch in streamAgent');
assert.doesNotMatch(
  completedStatusMatch[0],
  /addToNonRunning\(agentRunId\)/,
  'normal completed SSE status must not poison future terminal replay attempts',
);

const activeRunsNotFoundMatch = source.match(
  /if\s*\(\s*rawData\.includes\(['"]Agent run['"]\)[\s\S]*?rawData\.includes\(['"]not found in active runs['"]\)[\s\S]*?return;\s*\n\s*\}/,
);
assert.ok(activeRunsNotFoundMatch, 'expected active-runs-not-found branch in streamAgent');
assert.doesNotMatch(
  activeRunsNotFoundMatch[0],
  /addToNonRunning\(agentRunId\)/,
  'active-run registry misses are not durable 404s and must not disable completed-run replay',
);

const eventSourceErrorStart = source.indexOf('eventSource.onerror = () => {');
assert.notEqual(eventSourceErrorStart, -1, 'expected EventSource onerror handler in streamAgent');
const eventSourceErrorEnd = source.indexOf('// Start the stream setup', eventSourceErrorStart);
assert.notEqual(eventSourceErrorEnd, -1, 'expected EventSource onerror handler to end before stream setup');
const eventSourceErrorBlock = source.slice(eventSourceErrorStart, eventSourceErrorEnd);
assert.doesNotMatch(
  eventSourceErrorBlock,
  /if\s*\(\s*status\.status\s*!==\s*['"]running['"]\s*\)\s*\{\s*addToNonRunning\(agentRunId\);/s,
  'EventSource close/error follow-up must not cache completed/stopped runs as non-replayable',
);

assert.match(
  source,
  /fromEventId\?:\s*string;/,
  'streamAgent should accept an append-order fromEventId replay cursor',
);
assert.match(
  source,
  /url\.searchParams\.append\(['"]from_event_id['"],\s*options\.fromEventId\.trim\(\)\)/,
  'streamAgent should pass from_event_id to the backend SSE endpoint when available',
);
assert.match(
  source,
  /callbacks\.onEventCursor\?\.\(eventCursor\)/,
  'streamAgent should surface event_cursor/metadata.event_cursor/event_id to the hook',
);

const getAgentRunsMatch = source.match(
  /export const getAgentRuns = async[\s\S]*?\n\};\n\nexport const streamAgent/,
);
assert.ok(getAgentRunsMatch, 'expected getAgentRuns implementation in api.ts');
assert.match(
  getAgentRunsMatch[0],
  /metadata:\s*normalizeAgentRunMetadata\(run\.metadata\)/,
  'getAgentRuns must preserve agent run metadata so completed Shadow Clone V2 runs can bootstrap replay UI',
);

console.log('stream replay cache contract passed');
