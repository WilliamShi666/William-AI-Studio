import test from 'node:test';
import assert from 'node:assert/strict';

import {
  extractSubagentStreamingText,
  extractSubagentReasoningText,
  buildSubagentToolCallMessage,
  isSubagentChunkEvent,
  isSubagentCompleteWithToolCalls,
} from './useAgentStream-scv2-routing.ts';

// ── isSubagentChunkEvent ──

test('isSubagentChunkEvent returns true for subagent_activity chunk', () => {
  assert.equal(isSubagentChunkEvent('subagent_activity', 'chunk'), true);
});

test('isSubagentChunkEvent returns true for subagent_activity reasoning_chunk', () => {
  assert.equal(isSubagentChunkEvent('subagent_activity', 'reasoning_chunk'), true);
});

test('isSubagentChunkEvent returns false for other types', () => {
  assert.equal(isSubagentChunkEvent('assistant', 'chunk'), false);
  assert.equal(isSubagentChunkEvent('status', 'chunk'), false);
  assert.equal(isSubagentChunkEvent('subagent_activity', 'complete'), false);
  assert.equal(isSubagentChunkEvent('subagent_activity', 'tool_call_chunk'), false);
});

// ── extractSubagentStreamingText ──

test('extractSubagentStreamingText extracts text from subagent_activity content', () => {
  const content = { content: 'Hello World' };
  assert.equal(extractSubagentStreamingText(content), 'Hello World');
});

test('extractSubagentStreamingText returns empty for missing content', () => {
  assert.equal(extractSubagentStreamingText({}), '');
  assert.equal(extractSubagentStreamingText(null), '');
  assert.equal(extractSubagentStreamingText({ content: '' }), '');
});

test('extractSubagentStreamingText handles nested content.content', () => {
  // Claude SDK triple-nested structure
  const content = { content: { content: 'Nested Hello' } };
  assert.equal(extractSubagentStreamingText(content), 'Nested Hello');
});

test('extractSubagentStreamingText returns empty for non-string content', () => {
  assert.equal(extractSubagentStreamingText({ content: 42 }), '');
  assert.equal(extractSubagentStreamingText({ content: null }), '');
});

// ── extractSubagentReasoningText ──

test('extractSubagentReasoningText extracts reasoning from content', () => {
  const content = { reasoning_content: 'I think...' };
  assert.equal(extractSubagentReasoningText(content), 'I think...');
});

test('extractSubagentReasoningText handles nested content.reasoning_content', () => {
  const content = { content: { reasoning_content: 'Deep thought' } };
  assert.equal(extractSubagentReasoningText(content), 'Deep thought');
});

// ── isSubagentCompleteWithToolCalls ──

test('isSubagentCompleteWithToolCalls detects tool_calls in complete event', () => {
  const content = {
    role: 'assistant',
    content: '',
    tool_calls: [{ id: 'tc1', function: { name: 'write_file', arguments: '{}' } }],
  };
  assert.equal(isSubagentCompleteWithToolCalls('subagent_activity', 'complete', content), true);
});

test('isSubagentCompleteWithToolCalls returns false for chunk events', () => {
  const content = { content: 'streaming text' };
  assert.equal(isSubagentCompleteWithToolCalls('subagent_activity', 'chunk', content), false);
});

test('isSubagentCompleteWithToolCalls returns false without tool_calls', () => {
  const content = { role: 'assistant', content: 'Hello' };
  assert.equal(isSubagentCompleteWithToolCalls('subagent_activity', 'complete', content), false);
});

test('isSubagentCompleteWithToolCalls returns false for non-subagent types', () => {
  const content = { role: 'assistant', content: '', tool_calls: [{ id: 'x' }] };
  assert.equal(isSubagentCompleteWithToolCalls('assistant', 'complete', content), false);
});

// ── buildSubagentToolCallMessage ──

test('buildSubagentToolCallMessage creates assistant message from subagent tool calls', () => {
  const content = {
    role: 'assistant',
    content: '',
    tool_calls: [
      {
        id: 'toolu_abc',
        type: 'function',
        function: { name: 'write_file', arguments: '{"file_path": "/test.py"}' },
      },
    ],
  };
  const metadata = {
    thread_run_id: 'run-123',
    subtask_id: 'task-abc',
    stream_status: 'complete',
  };

  const message = buildSubagentToolCallMessage({
    content,
    metadata,
    threadId: 'thread-1',
    messageId: 'msg-1',
    sequence: 5,
    timestamp: '2025-01-01T00:00:00Z',
  });

  assert.equal(message.type, 'assistant');
  assert.equal(message.is_llm_message, true);
  assert.equal(message.thread_id, 'thread-1');
  assert.equal(message.message_id, 'msg-1');

  const parsed = JSON.parse(message.content);
  assert.equal(parsed.role, 'assistant');
  assert.equal(parsed.content, '');
  assert.equal(parsed.tool_calls.length, 1);
  assert.equal(parsed.tool_calls[0].function.name, 'write_file');
});

test('buildSubagentToolCallMessage returns null for empty tool_calls', () => {
  const content = { role: 'assistant', content: 'Hello' };
  const result = buildSubagentToolCallMessage({
    content,
    metadata: {},
    threadId: 't1',
    messageId: 'm1',
    sequence: 1,
    timestamp: '2025-01-01',
  });
  assert.equal(result, null);
});

test('buildSubagentToolCallMessage preserves multiple tool calls', () => {
  const content = {
    role: 'assistant',
    content: '',
    tool_calls: [
      { id: 't1', function: { name: 'read_file', arguments: '{}' } },
      { id: 't2', function: { name: 'write_file', arguments: '{}' } },
    ],
  };
  const message = buildSubagentToolCallMessage({
    content,
    metadata: {},
    threadId: 't1',
    messageId: 'm1',
    sequence: 1,
    timestamp: '2025-01-01',
  });
  const parsed = JSON.parse(message.content);
  assert.equal(parsed.tool_calls.length, 2);
});
