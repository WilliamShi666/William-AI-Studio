const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'useAgentStream.ts'), 'utf8');

for (const eventName of [
  'subagent_activity',
  'shadow_clone_v2_started',
  'shadow_clone_v2_plan_created',
  'shadow_clone_v2_execution_completed',
  'shadow_clone_v2_execution_failed',
  'shadow_clone_v2_projection',
]) {
  assert.match(
    source,
    new RegExp(`'${eventName}'`),
    `useAgentStream should route ${eventName} lifecycle events into the Shadow Clone store`,
  );
}

const lifecycleMatch = /if\s*\(\s*isShadowCloneLifecycleEvent\s*&&\s*topLevelLifecycleStatus\s*\)\s*\{/.exec(source);
const lifecycleBlockStart = lifecycleMatch ? lifecycleMatch.index : -1;
assert.notEqual(
  lifecycleBlockStart,
  -1,
  'Shadow Clone lifecycle route block should exist',
);
const lifecycleBlock = source.slice(lifecycleBlockStart, lifecycleBlockStart + 500);
assert.match(
  lifecycleBlock,
  /shadowCloneStore\.handleSSEEvent\(/,
  'Shadow Clone lifecycle events should update shadow clone store before normal assistant rendering',
);
assert.match(
  lifecycleBlock,
  /topLevelLifecycleStatus/,
  'Shadow Clone lifecycle routing should dispatch by the top-level event type, not metadata.stream_status',
);
assert.match(
  lifecycleBlock,
  /message\s+as\s+unknown\s+as\s+ParsedContent/,
  'Shadow Clone lifecycle routing should pass the full event envelope so subagent_activity keeps subtask_id and metadata',
);
assert.match(
  lifecycleBlock,
  /return;/,
  'Shadow Clone lifecycle events should return before normal assistant rendering',
);

assert.match(
  source,
  /event_cursor\?:\s*string;/,
  'useAgentStream should type Shadow Clone V2 event_cursor for append-order replay',
);
assert.match(
  source,
  /lastEventCursorRef/,
  'useAgentStream should track append-order event cursors separately from logical event_index',
);
assert.match(
  source,
  /fromEventId:\s*reconnectFromEventId/,
  'useAgentStream should reconnect V2 streams with from_event_id when available',
);
assert.match(
  source,
  /persistCursor\(ownership\.runId,\s*message\.event_index,\s*eventCursor\)/,
  'useAgentStream should persist event_cursor with event_index',
);
assert.match(
  source,
  /previousEventIndex\s*>=\s*eventIndex\s*&&\s*!\(\s*eventCursor\s*&&\s*eventCursor\s*!==\s*lastPersistedEventCursorRef\.current\s*\)/,
  'useAgentStream should allow cursor-only persistence updates for lower-sequence V2 events',
);
assert.match(
  source,
  /isV2TerminalSummary/,
  'useAgentStream should detect V2 terminal summary before duplicate cursor filtering',
);
assert.match(
  source,
  /!isV2TerminalSummary\s*&&\s*\(\s*!eventCursor\s*\|\|\s*eventCursor\s*===\s*lastEventCursorRef\.current\s*\)/,
  'useAgentStream should allow V2 terminal summary through duplicate cursor filtering',
);

assert.match(
  source,
  /const isShadowCloneLifecycleEvent =[\s\S]*Boolean\(topLevelLifecycleStatus\)[\s\S]*SHADOW_CLONE_EVENTS\.has\(streamStatus\)/,
  'top-level Shadow Clone lifecycle events such as subagent_activity must route to the store even when metadata.stream_status is chunk or complete',
);

console.log('useAgentStream Shadow Clone V2 lifecycle routing contract passed');
