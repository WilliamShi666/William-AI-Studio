const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'useAgentStream.ts'), 'utf8');

assert.match(
  source,
  /buildSyntheticTerminalAssistantMessage/,
  'useAgentStream should be able to synthesize a visible assistant message from streamed text',
);

const finalizeStreamStart = source.indexOf('const finalizeStream = useCallback');
assert.notEqual(finalizeStreamStart, -1, 'finalizeStream callback should exist');
const finalizeStreamEnd = source.indexOf('// --- Stream Callback Handlers ---', finalizeStreamStart);
assert.notEqual(finalizeStreamEnd, -1, 'finalizeStream block should end before stream callback handlers');
const finalizeStreamBlock = source.slice(finalizeStreamStart, finalizeStreamEnd);

assert.ok(
  finalizeStreamBlock.indexOf('promoteTerminalStreamingContent(finalStatus, runId);') <
    finalizeStreamBlock.indexOf('clearStreamingTextContent();'),
  'terminal status finalization must promote visible streamed text before clearing ephemeral chunks',
);

const finalAssistantStart = source.indexOf('} else if (isFinalAssistantStreamStatus(streamStatus)) {');
assert.notEqual(finalAssistantStart, -1, 'final assistant stream-status branch should exist');
const finalAssistantEnd = source.indexOf("} else if (streamStatus === 'tool_call_chunk')", finalAssistantStart);
assert.notEqual(finalAssistantEnd, -1, 'final assistant branch should end before tool_call_chunk branch');
const finalAssistantBlock = source.slice(finalAssistantStart, finalAssistantEnd);

assert.match(
  finalAssistantBlock,
  /const finalMessageWillEmit =[\s\S]*shouldEmitFinalMessageToThread[\s\S]*finalMessage\.message_id[\s\S]*!isEmptyAssistantMessage\(finalMessage\)/,
  'final assistant branch should explicitly decide whether the final message will become visible in the thread',
);
assert.match(
  finalAssistantBlock,
  /const finalMessageWillBeVisible =[\s\S]*finalMessageWillEmit[\s\S]*finalMessageRenderableText/s,
  'final assistant branch should distinguish emitted messages from user-visible assistant text',
);
assert.match(
  finalAssistantBlock,
  /if\s*\(\s*shouldEmitFinalMessageToThread\s*&&\s*!finalMessageWillBeVisible\s*\)\s*\{\s*promoteFinalAssistantStreamingContent\(finalMessage,\s*ownership\.runId\);/s,
  'final assistant branch must synthesize a visible fallback before clearing chunks when the final message would not be visible',
);
assert.ok(
  finalAssistantBlock.indexOf('promoteFinalAssistantStreamingContent(finalMessage, ownership.runId);') <
    finalAssistantBlock.indexOf('clearStreamingTextContent();'),
  'final assistant fallback promotion must happen before clearing streaming chunks',
);

console.log('useAgentStream terminal promotion contract passed');
