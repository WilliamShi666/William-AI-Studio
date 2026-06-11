import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const helperUrl = pathToFileURL(path.join(testDir, 'useAgentStream-final-status.js'));

const { isFinalAssistantStreamStatus } = await import(helperUrl.href);

test('complete, completed, final, and done stream statuses are final assistant statuses', () => {
  assert.equal(isFinalAssistantStreamStatus('complete'), true);
  assert.equal(isFinalAssistantStreamStatus('completed'), true);
  assert.equal(isFinalAssistantStreamStatus('final'), true);
  assert.equal(isFinalAssistantStreamStatus('done'), true);
});

test('chunk stream status is not a final assistant status', () => {
  assert.equal(isFinalAssistantStreamStatus('chunk'), false);
});

test('reasoning_chunk stream status is not a final assistant status', () => {
  assert.equal(isFinalAssistantStreamStatus('reasoning_chunk'), false);
});

test('tool_call_chunk stream status is not a final assistant status', () => {
  assert.equal(isFinalAssistantStreamStatus('tool_call_chunk'), false);
});

test('tool_result_chunk stream status is not a final assistant status', () => {
  assert.equal(isFinalAssistantStreamStatus('tool_result_chunk'), false);
});

test('edge cases: undefined, null, and empty string are not final', () => {
  assert.equal(isFinalAssistantStreamStatus(undefined), false);
  assert.equal(isFinalAssistantStreamStatus(null), false);
  assert.equal(isFinalAssistantStreamStatus(''), false);
});

test('unknown status values are not final', () => {
  assert.equal(isFinalAssistantStreamStatus('unknown_status'), false);
  assert.equal(isFinalAssistantStreamStatus('processing'), false);
  assert.equal(isFinalAssistantStreamStatus('streaming'), false);
});
