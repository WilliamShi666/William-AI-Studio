import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const helperUrl = pathToFileURL(path.join(testDir, 'thread-message-merge.js'));

const {
  normalizeThreadMessages,
  mergeThreadMessages,
  areThreadMessagesEqual,
} = await import(helperUrl.href);

test('normalizeThreadMessages keeps assistant messages from refetch payload', () => {
  const normalized = normalizeThreadMessages([
    { message_id: 'status-1', type: 'status', content: 'done' },
    { message_id: 'assistant-1', type: 'assistant', content: 'hello', created_at: '2026-04-27T08:00:00.000Z' },
  ], 'thread-1');

  assert.equal(normalized.length, 1);
  assert.equal(normalized[0].message_id, 'assistant-1');
  assert.equal(normalized[0].thread_id, 'thread-1');
});

test('mergeThreadMessages adds server assistant output after terminal refetch', () => {
  const previous = [
    { message_id: 'user-1', type: 'user', content: 'second message', metadata: '{}', created_at: '2026-04-27T08:00:00.000Z' },
  ];
  const server = [
    ...previous,
    { message_id: 'assistant-1', type: 'assistant', content: 'agent output', metadata: '{}', created_at: '2026-04-27T08:00:05.000Z' },
  ];

  const merged = mergeThreadMessages(previous, server, new Date('2026-04-27T08:00:10.000Z').getTime());

  assert.equal(merged.length, 2);
  assert.equal(merged[1].message_id, 'assistant-1');
});


test('mergeThreadMessages keeps server canonical assistant when same id has local streaming content', () => {
  const previous = [
    { message_id: 'assistant-1', type: 'assistant', content: 'partial stream', metadata: '{}', created_at: '2026-04-27T08:00:05.000Z' },
  ];
  const server = [
    { message_id: 'assistant-1', type: 'assistant', content: 'server final', metadata: '{}', created_at: '2026-04-27T08:00:06.000Z' },
  ];

  const merged = mergeThreadMessages(previous, server, new Date('2026-04-27T08:00:10.000Z').getTime());

  assert.equal(merged.length, 1);
  assert.equal(merged[0].message_id, 'assistant-1');
  assert.equal(merged[0].content, 'server final');
});

test('mergeThreadMessages preserves local assistant content when server refetch still returns empty placeholder content', () => {
  const previous = [
    { message_id: 'assistant-1', type: 'assistant', content: 'real streamed output', metadata: '{}', created_at: '2026-04-27T08:00:05.000Z' },
  ];
  const server = [
    { message_id: 'assistant-1', type: 'assistant', content: '', metadata: '{}', created_at: '2026-04-27T08:00:06.000Z' },
  ];

  const merged = mergeThreadMessages(previous, server, new Date('2026-04-27T08:00:10.000Z').getTime());

  assert.equal(merged.length, 1);
  assert.equal(merged[0].message_id, 'assistant-1');
  assert.equal(merged[0].content, 'real streamed output');
});

test('mergeThreadMessages drops synthetic terminal assistant once canonical server assistant for same run arrives', () => {
  const previous = [
    {
      message_id: 'stream-terminal-recovery-run-1',
      type: 'assistant',
      content: JSON.stringify({ role: 'assistant', content: 'visible recovered reply' }),
      metadata: JSON.stringify({
        synthetic_kind: 'terminal_stream_recovery',
        thread_run_id: 'run-1',
      }),
      created_at: '2026-04-27T08:00:05.000Z',
    },
  ];
  const server = [
    {
      message_id: 'assistant-1',
      type: 'assistant',
      content: JSON.stringify({ role: 'assistant', content: 'visible recovered reply' }),
      metadata: JSON.stringify({ thread_run_id: 'run-1' }),
      created_at: '2026-04-27T08:00:06.000Z',
    },
  ];

  const merged = mergeThreadMessages(previous, server, new Date('2026-04-27T08:00:10.000Z').getTime());

  assert.deepEqual(
    merged.map((message) => message.message_id),
    ['assistant-1'],
  );
});

test('mergeThreadMessages keeps synthetic terminal assistant while server has not caught up', () => {
  const previous = [
    {
      message_id: 'stream-terminal-recovery-run-2',
      type: 'assistant',
      content: JSON.stringify({ role: 'assistant', content: 'visible recovered reply' }),
      metadata: JSON.stringify({
        synthetic_kind: 'terminal_stream_recovery',
        thread_run_id: 'run-2',
      }),
      created_at: '2026-04-27T08:00:05.000Z',
    },
  ];

  const merged = mergeThreadMessages(previous, [], new Date('2026-04-27T08:00:10.000Z').getTime());

  assert.deepEqual(
    merged.map((message) => message.message_id),
    ['stream-terminal-recovery-run-2'],
  );
});

test('areThreadMessagesEqual detects newly appended assistant output', () => {
  const previous = [
    { message_id: 'user-1', type: 'user', content: 'second message', metadata: '{}' },
  ];
  const next = [
    ...previous,
    { message_id: 'assistant-1', type: 'assistant', content: 'agent output', metadata: '{}' },
  ];

  assert.equal(areThreadMessagesEqual(previous, next), false);
});
