const assert = require('node:assert/strict');

function shouldProcessV2CursorEvent(message, state) {
  const eventCursor =
    typeof message.event_cursor === 'string'
      ? message.event_cursor
      : typeof message.event_id === 'string'
        ? message.event_id
        : null;
  const isTerminalSummary = message?.metadata?.terminal_summary === true;

  if (typeof message.event_index === 'number') {
    if (message.event_index <= state.lastEventIndex) {
      if (
        !isTerminalSummary &&
        (!eventCursor || eventCursor === state.lastEventCursor)
      ) {
        return false;
      }
    }
    state.lastEventIndex = message.event_index;
    if (eventCursor) {
      state.lastEventCursor = eventCursor;
    }
  }
  return true;
}

{
  const state = { lastEventIndex: 10, lastEventCursor: '1001-0' };
  const terminalSummary = {
    type: 'shadow_clone_v2_projection',
    event_index: 10,
    event_cursor: '1001-0',
    metadata: { terminal_summary: true },
  };
  assert.equal(
    shouldProcessV2CursorEvent(terminalSummary, state),
    true,
    'terminal summary with same event_index/event_cursor must bypass duplicate gate',
  );
}

{
  const state = { lastEventIndex: 10, lastEventCursor: '1001-0' };
  const duplicateNonSummary = {
    type: 'shadow_clone_v2_projection',
    event_index: 10,
    event_cursor: '1001-0',
    metadata: {},
  };
  assert.equal(
    shouldProcessV2CursorEvent(duplicateNonSummary, state),
    false,
    'non-summary duplicate should still be ignored',
  );
}

console.log('useAgentStream V2 cursor behavior contract passed');
