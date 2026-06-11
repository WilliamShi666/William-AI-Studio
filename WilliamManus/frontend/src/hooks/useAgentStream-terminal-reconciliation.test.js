import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const helperUrl = pathToFileURL(
  path.join(testDir, 'useAgentStream-terminal-reconciliation.js'),
);

const {
  SYNTHETIC_TERMINAL_ASSISTANT_KIND,
  buildSyntheticTerminalAssistantMessage,
  getRenderableAssistantText,
  hasCanonicalAssistantForRun,
  mergeTerminalReconciledMessages,
} = await import(helperUrl.href);

test('buildSyntheticTerminalAssistantMessage captures streamed text and run metadata', () => {
  const message = buildSyntheticTerminalAssistantMessage({
    runId: 'run-1',
    threadId: 'thread-1',
    textContent: 'final visible reply',
    reasoningContent: 'brief reasoning',
    createdAt: '2026-06-02T00:00:00.000Z',
  });

  assert.equal(message.message_id, 'stream-terminal-recovery-run-1');
  assert.equal(message.thread_id, 'thread-1');
  assert.equal(message.type, 'assistant');
  assert.equal(message.is_llm_message, true);

  const content = JSON.parse(message.content);
  const metadata = JSON.parse(message.metadata);
  assert.equal(content.content, 'final visible reply');
  assert.equal(content.reasoning_content, 'brief reasoning');
  assert.equal(metadata.thread_run_id, 'run-1');
  assert.equal(metadata.synthetic_kind, SYNTHETIC_TERMINAL_ASSISTANT_KIND);
});

test('getRenderableAssistantText extracts JSON and plain string assistant text', () => {
  assert.equal(
    getRenderableAssistantText({
      type: 'assistant',
      content: '{"role":"assistant","content":"hello json"}',
    }),
    'hello json',
  );
  assert.equal(
    getRenderableAssistantText({
      type: 'assistant',
      content: 'hello plain',
    }),
    'hello plain',
  );
});

test('mergeTerminalReconciledMessages replaces synthetic terminal assistant with canonical server assistant for same run', () => {
  const synthetic = buildSyntheticTerminalAssistantMessage({
    runId: 'run-1',
    threadId: 'thread-1',
    textContent: 'final visible reply',
    createdAt: '2026-06-02T00:00:02.000Z',
  });
  const previous = [
    {
      message_id: 'user-1',
      thread_id: 'thread-1',
      type: 'user',
      content: '{"content":"hello"}',
      metadata: '{}',
      created_at: '2026-06-02T00:00:00.000Z',
      updated_at: '2026-06-02T00:00:00.000Z',
      is_llm_message: false,
    },
    synthetic,
  ];
  const server = [
    {
      message_id: 'user-1',
      thread_id: 'thread-1',
      type: 'user',
      content: '{"content":"hello"}',
      metadata: '{}',
      created_at: '2026-06-02T00:00:00.000Z',
      updated_at: '2026-06-02T00:00:00.000Z',
      is_llm_message: false,
    },
    {
      message_id: 'assistant-1',
      thread_id: 'thread-1',
      type: 'assistant',
      content: '{"role":"assistant","content":"final visible reply"}',
      metadata: '{"thread_run_id":"run-1"}',
      created_at: '2026-06-02T00:00:03.000Z',
      updated_at: '2026-06-02T00:00:03.000Z',
      is_llm_message: true,
    },
  ];

  const merged = mergeTerminalReconciledMessages(previous, server, {
    threadId: 'thread-1',
  });

  assert.deepEqual(
    merged.map((message) => message.message_id),
    ['user-1', 'assistant-1'],
  );
  assert.equal(hasCanonicalAssistantForRun(merged, 'run-1'), true);
});

test('mergeTerminalReconciledMessages keeps synthetic assistant when matching server assistant is not renderable yet', () => {
  const synthetic = buildSyntheticTerminalAssistantMessage({
    runId: 'run-empty-server',
    threadId: 'thread-1',
    textContent: 'visible streamed fallback',
    createdAt: '2026-06-02T00:00:02.000Z',
  });
  const server = [
    {
      message_id: 'assistant-empty',
      thread_id: 'thread-1',
      type: 'assistant',
      content: '{"role":"assistant","content":""}',
      metadata: '{"thread_run_id":"run-empty-server"}',
      created_at: '2026-06-02T00:00:03.000Z',
      updated_at: '2026-06-02T00:00:03.000Z',
      is_llm_message: true,
    },
  ];

  const merged = mergeTerminalReconciledMessages([synthetic], server, {
    threadId: 'thread-1',
  });

  assert.deepEqual(
    merged.map((message) => message.message_id),
    ['stream-terminal-recovery-run-empty-server', 'assistant-empty'],
  );
});

test('mergeTerminalReconciledMessages keeps local synthetic assistant while server has not caught up yet', () => {
  const synthetic = buildSyntheticTerminalAssistantMessage({
    runId: 'run-2',
    threadId: 'thread-1',
    textContent: 'streamed reply',
    createdAt: '2026-06-02T00:00:02.000Z',
  });
  const previous = [
    {
      message_id: 'temp-local-user',
      thread_id: 'thread-1',
      type: 'user',
      content: '{"content":"hello"}',
      metadata: '{}',
      created_at: '2026-06-02T00:00:00.000Z',
      updated_at: '2026-06-02T00:00:00.000Z',
      is_llm_message: false,
    },
    synthetic,
  ];
  const server = [];

  const merged = mergeTerminalReconciledMessages(previous, server, {
    threadId: 'thread-1',
  });

  assert.deepEqual(
    merged.map((message) => message.message_id),
    ['temp-local-user', 'stream-terminal-recovery-run-2'],
  );
});
