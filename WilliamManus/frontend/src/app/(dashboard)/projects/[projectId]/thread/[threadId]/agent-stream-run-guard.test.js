import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const helperUrl = pathToFileURL(path.join(testDir, 'agent-stream-run-guard.js'));

const {
  shouldApplyTerminalStatus,
  shouldAdoptLatestRunningRun,
  hasAssistantMessageForRun,
  shouldShowCompletionHint,
  shouldRetryTerminalMessageRefetch,
} = await import(helperUrl.href);

test('old run terminal status must not clear a newer active run', () => {
  assert.equal(
    shouldApplyTerminalStatus({
      statusRunId: 'old-run',
      activeRunId: 'new-run',
      isSending: false,
      userInitiatedRun: false,
      agentStatus: 'running',
    }),
    false,
  );
});

test('missing run id terminal status must not clear active run signals', () => {
  assert.equal(
    shouldApplyTerminalStatus({
      statusRunId: null,
      activeRunId: 'new-run',
      isSending: false,
      userInitiatedRun: true,
      agentStatus: 'running',
    }),
    false,
  );
});

test('matching terminal status can clear the active run', () => {
  assert.equal(
    shouldApplyTerminalStatus({
      statusRunId: 'new-run',
      activeRunId: 'new-run',
      isSending: false,
      userInitiatedRun: false,
      agentStatus: 'running',
    }),
    true,
  );
});

test('latest running run can be adopted only when no active stream exists', () => {
  assert.equal(
    shouldAdoptLatestRunningRun({
      latestRunningRunId: 'run-2',
      currentAgentRunId: null,
      agentStatus: 'idle',
      isStreamingOrRecentlyStreamed: false,
    }),
    true,
  );

  assert.equal(
    shouldAdoptLatestRunningRun({
      latestRunningRunId: 'run-2',
      currentAgentRunId: 'run-1',
      agentStatus: 'running',
      isStreamingOrRecentlyStreamed: true,
    }),
    false,
  );
});


test('assistant with matching run metadata is visible even before run start fallback window', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: 'final answer',
          metadata: JSON.stringify({ thread_run_id: 'run-1' }),
          created_at: '2026-04-27T07:59:00.000Z',
        },
      ],
      terminalRunId: 'run-1',
      activeRunId: 'run-1',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    true,
  );
});


test('assistant events fallback metadata can satisfy current run by timestamp fallback', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: 'fallback answer',
          metadata: JSON.stringify({ source: 'events_fallback', event_id: 'event-1' }),
          created_at: '2026-04-27T08:00:03.000Z',
        },
      ],
      terminalRunId: 'run-3',
      activeRunId: 'run-3',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    true,
  );
});

test('assistant without run metadata cannot satisfy expected run when run start is unknown', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: 'unknown age answer',
          metadata: JSON.stringify({ source: 'events_fallback', event_id: 'event-2' }),
          created_at: '2026-04-27T08:00:03.000Z',
        },
      ],
      terminalRunId: 'run-4',
      activeRunId: 'run-4',
      runStartedAt: null,
    }),
    false,
  );
});

test('assistant with stale run metadata does not satisfy current terminal run', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: 'old answer',
          metadata: JSON.stringify({ agent_run_id: 'old-run' }),
          created_at: '2026-04-27T08:00:05.000Z',
        },
      ],
      terminalRunId: 'new-run',
      activeRunId: 'new-run',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    false,
  );
});

test('assistant with matching run_id metadata satisfies current terminal run', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: 'final answer',
          metadata: { run_id: 'run-2' },
          created_at: '2026-04-27T07:59:00.000Z',
        },
      ],
      terminalRunId: 'run-2',
      activeRunId: 'run-2',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    true,
  );
});

test('empty assistant for matching run does not satisfy visible terminal output', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: JSON.stringify({ role: 'assistant', content: '' }),
          metadata: JSON.stringify({ thread_run_id: 'run-empty' }),
          created_at: '2026-04-27T08:00:03.000Z',
        },
      ],
      terminalRunId: 'run-empty',
      activeRunId: 'run-empty',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    false,
  );
});

test('tool-only assistant for matching run does not stop terminal refetch', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: JSON.stringify({
            role: 'assistant',
            content: '',
            tool_calls: [
              {
                function: {
                  name: 'read_file',
                  arguments: '{}',
                },
              },
            ],
          }),
          metadata: JSON.stringify({ thread_run_id: 'run-tool-only' }),
          created_at: '2026-04-27T08:00:03.000Z',
        },
      ],
      terminalRunId: 'run-tool-only',
      activeRunId: 'run-tool-only',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    false,
  );
});

test('plain string assistant content counts as visible terminal output', () => {
  assert.equal(
    hasAssistantMessageForRun({
      messages: [
        {
          type: 'assistant',
          content: 'final visible answer',
          metadata: JSON.stringify({ thread_run_id: 'run-string' }),
          created_at: '2026-04-27T08:00:03.000Z',
        },
      ],
      terminalRunId: 'run-string',
      activeRunId: 'run-string',
      runStartedAt: new Date('2026-04-27T08:00:00.000Z').getTime(),
    }),
    true,
  );
});

test('completion hint shows after normal assistant output is visible', () => {
  assert.equal(
    shouldShowCompletionHint({
      terminalRunId: 'run-1',
      activeRunId: 'run-1',
      hasSeenRun: true,
      hasAssistantMessageAfterRunStart: true,
      hasStreamingContent: false,
      finalStatus: 'completed',
    }),
    true,
  );
});

test('completion hint stays hidden until completed output is visible', () => {
  assert.equal(
    shouldShowCompletionHint({
      terminalRunId: 'run-1',
      activeRunId: 'run-1',
      hasSeenRun: true,
      hasAssistantMessageAfterRunStart: false,
      hasStreamingContent: false,
      finalStatus: 'completed',
    }),
    false,
  );
});

test('completion hint ignores terminal status from a previous run', () => {
  assert.equal(
    shouldShowCompletionHint({
      terminalRunId: 'old-run',
      activeRunId: 'new-run',
      hasSeenRun: true,
      hasAssistantMessageAfterRunStart: false,
      hasStreamingContent: false,
      finalStatus: 'completed',
    }),
    false,
  );
});

test('completion hint stays hidden for abnormal terminal statuses without visible output', () => {
  assert.equal(
    shouldShowCompletionHint({
      terminalRunId: 'run-1',
      activeRunId: 'run-1',
      hasSeenRun: true,
      hasAssistantMessageAfterRunStart: false,
      hasStreamingContent: false,
      finalStatus: 'failed',
    }),
    false,
  );
});

test('terminal message refetch retries while completed output is not visible', () => {
  assert.equal(
    shouldRetryTerminalMessageRefetch({
      terminalRunId: 'run-1',
      activeRunId: 'run-1',
      isTerminalStatus: true,
      hasAssistantMessageAfterRunStart: false,
      attempt: 2,
      maxAttempts: 6,
    }),
    true,
  );
});

test('terminal message refetch stops after assistant output is visible', () => {
  assert.equal(
    shouldRetryTerminalMessageRefetch({
      terminalRunId: 'run-1',
      activeRunId: 'run-1',
      isTerminalStatus: true,
      hasAssistantMessageAfterRunStart: true,
      attempt: 2,
      maxAttempts: 6,
    }),
    false,
  );
});

test('terminal message refetch ignores stale terminal run', () => {
  assert.equal(
    shouldRetryTerminalMessageRefetch({
      terminalRunId: 'old-run',
      activeRunId: 'new-run',
      isTerminalStatus: true,
      hasAssistantMessageAfterRunStart: false,
      attempt: 2,
      maxAttempts: 6,
    }),
    false,
  );
});
