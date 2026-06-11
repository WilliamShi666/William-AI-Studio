import test from 'node:test';
import assert from 'node:assert/strict';

import { isBackendLogContent, isBackendLogMessage } from './useAgentStream.ts';

// ── isBackendLogContent ──

test('isBackendLogContent returns true for Python traceback line', () => {
  assert.equal(isBackendLogContent('Traceback (most recent call last):'), true);
  assert.equal(isBackendLogContent('  File "/app/main.py", line 42, in handle'), true);
});

test('isBackendLogContent returns true for Redis error', () => {
  assert.equal(isBackendLogContent('redis.exceptions.ConnectionError: Connection refused'), true);
  assert.equal(isBackendLogContent('redis.commands.core.SetCommand'), true);
});

test('isBackendLogContent returns true for timestamped log lines', () => {
  assert.equal(isBackendLogContent('2025-06-08 12:34:56,789 | ERROR | Something failed'), true);
  assert.equal(isBackendLogContent('[2025-06-08 12:34:56] ERROR: Failed to connect'), true);
});

test('isBackendLogContent returns true for raise statement', () => {
  assert.equal(isBackendLogContent('  raise ValueError("invalid input")'), true);
});

test('isBackendLogContent returns true for root logger output', () => {
  assert.equal(isBackendLogContent('ERROR:root:Failed to parse configuration'), true);
  assert.equal(isBackendLogContent('WARNING:root:Deprecated API called'), true);
});

test('isBackendLogContent returns false for natural language agent output', () => {
  assert.equal(isBackendLogContent('I will now analyze the file for you.'), false);
  assert.equal(isBackendLogContent('We found three issues in the codebase:'), false);
  assert.equal(isBackendLogContent('The main problem is in the authentication module.'), false);
  assert.equal(isBackendLogContent('Here is a summary of what I did:'), false);
  assert.equal(isBackendLogContent('Based on your request, I have completed the following tasks:'), false);
  assert.equal(isBackendLogContent('Let me explain the changes I made.'), false);
  assert.equal(isBackendLogContent('Next, I will run the tests to verify.'), false);
  assert.equal(isBackendLogContent('It appears that the configuration is outdated.'), false);
});

test('isBackendLogContent returns false for empty or whitespace', () => {
  assert.equal(isBackendLogContent(''), false);
  assert.equal(isBackendLogContent('   '), false);
  assert.equal(isBackendLogContent(null), false);
  assert.equal(isBackendLogContent(undefined), false);
});

// ── isBackendLogMessage ──

test('isBackendLogMessage detects traceback in parsed object', () => {
  const msg = { role: 'assistant', content: 'Traceback (most recent call last):\n  File "/app/main.py", line 42' };
  assert.equal(isBackendLogMessage(msg), true);
});

test('isBackendLogMessage detects log in plain string', () => {
  assert.equal(isBackendLogMessage('ERROR:root:Failed to connect to database'), true);
});

test('isBackendLogMessage returns false for normal agent message', () => {
  const msg = { role: 'assistant', content: 'I have successfully written the file. Here is a summary:' };
  assert.equal(isBackendLogMessage(msg), false);
});

test('isBackendLogMessage handles non-string content gracefully', () => {
  const msg = { role: 'assistant', content: 42 };
  assert.equal(isBackendLogMessage(msg), false);
});

test('isBackendLogMessage handles null content', () => {
  const msg = { role: 'assistant', content: null };
  assert.equal(isBackendLogMessage(msg), false);
});
