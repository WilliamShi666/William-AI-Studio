import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { registerHooks } from 'node:module';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const frontendRoot = path.resolve(testDir, '../..');
const srcRoot = path.join(frontendRoot, 'src');

const resolveAliasPath = (specifier) => {
  const relativePath = specifier.slice(2);
  const candidates = [
    path.join(srcRoot, `${relativePath}.ts`),
    path.join(srcRoot, `${relativePath}.tsx`),
    path.join(srcRoot, `${relativePath}.js`),
    path.join(srcRoot, relativePath, 'index.ts'),
    path.join(srcRoot, relativePath, 'index.tsx'),
    path.join(srcRoot, relativePath, 'index.js'),
  ];

  const match = candidates.find((candidate) => existsSync(candidate));
  if (!match) {
    throw new Error(`Unable to resolve alias import: ${specifier}`);
  }

  return pathToFileURL(match).href;
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@/')) {
      return {
        shortCircuit: true,
        url: resolveAliasPath(specifier),
      };
    }

    return nextResolve(specifier, context);
  },
});

const {
  applyShadowCloneTranscriptActivity,
  createEmptyShadowCloneTranscriptState,
} = await import('./shadow-clone-transcript.ts');

const createPlanningActivity = (streamStatus, sequence, content) => ({
  subtask_id: 'subtask-1',
  sequence,
  message_type: 'assistant',
  content,
  metadata: {
    stream_status: streamStatus,
  },
  created_at: '2026-04-07T00:00:00.000Z',
  updated_at: '2026-04-07T00:00:00.000Z',
});

const assertWatermarkOnlyAdvance = (firstState, nextState, expectedSequence) => {
  assert.notEqual(nextState, firstState);
  assert.equal(nextState.lastSequence, expectedSequence);
  assert.equal(nextState.messages, firstState.messages);
  assert.equal(nextState.streamingTextContent, firstState.streamingTextContent);
  assert.equal(nextState.streamingReasoningContent, firstState.streamingReasoningContent);
  assert.equal(nextState.streamingToolCall, firstState.streamingToolCall);
  assert.equal(nextState.latestLabel, firstState.latestLabel);
};

test('unchanged reasoning chunks advance the watermark without semantic churn and reject stale follow-ups', () => {
  const firstState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('reasoning_chunk', 1, {
      reasoning_content: 'Drafting the execution plan',
    }),
  );

  const secondState = applyShadowCloneTranscriptActivity(
    firstState,
    createPlanningActivity('reasoning_chunk', 2, {
      reasoning_content: 'Drafting the execution plan',
    }),
  );

  assertWatermarkOnlyAdvance(firstState, secondState, 2);

  const staleState = applyShadowCloneTranscriptActivity(
    secondState,
    createPlanningActivity('reasoning_chunk', 1, {
      reasoning_content: 'This stale update should be ignored',
    }),
  );

  assert.equal(staleState, secondState);
});

test('unchanged reply chunks advance the watermark without semantic churn and reject stale follow-ups', () => {
  const firstState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('chunk', 1, {
      content: 'Still preparing the response',
    }),
  );

  const secondState = applyShadowCloneTranscriptActivity(
    firstState,
    createPlanningActivity('chunk', 2, {
      content: 'Still preparing the response',
    }),
  );

  assertWatermarkOnlyAdvance(firstState, secondState, 2);

  const staleState = applyShadowCloneTranscriptActivity(
    secondState,
    createPlanningActivity('chunk', 1, {
      content: 'This stale update should be ignored',
    }),
  );

  assert.equal(staleState, secondState);
});

test('Shadow Clone V2 incremental assistant chunks are accumulated for realtime subagent chat visibility', () => {
  const marker = 'SUB_STREAM_MARKER_realtime_agent_1_1780598331722';
  const chunks = [
    'SUB_STREAM',
    '_MARKER',
    '_realtime',
    '_agent_1',
    '_1780598331722',
  ];

  const finalState = chunks.reduce(
    (state, chunk, index) =>
      applyShadowCloneTranscriptActivity(
        state,
        createPlanningActivity('chunk', index + 1, {
          role: 'assistant',
          content: chunk,
        }),
      ),
    createEmptyShadowCloneTranscriptState(),
  );

  assert.equal(finalState.streamingTextContent, marker);
  assert.equal(finalState.messages.length, 0);
  assert.equal(finalState.latestLabel, '正在输出回复');
});

test('Shadow Clone V2 assistant text stays visible when a subagent starts a tool call', () => {
  const textState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('chunk', 1, {
      role: 'assistant',
      content: 'SUB_STREAM_MARKER_realtime_agent_1_1780598331722',
    }),
  );

  const toolState = applyShadowCloneTranscriptActivity(
    textState,
    createPlanningActivity('tool_call_chunk', 2, {
      role: 'assistant',
      tool_calls: [
        {
          id: 'sleep-call-1',
          index: 0,
          function: {
            name: 'execute_command',
            arguments: JSON.stringify({
              command: 'sleep 45',
            }),
          },
        },
      ],
    }),
  );

  assert.equal(
    toolState.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780598331722',
  );
  assert.equal(toolState.streamingToolCall?.name, 'execute_command');
  assert.equal(toolState.latestLabel, '正在调用工具');
});

test('Shadow Clone V2 assistant complete message preserves accumulated incremental text', () => {
  const partialState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('chunk', 1, {
      role: 'assistant',
      content: 'SUB_STREAM_MARKER_realtime_agent_1_',
    }),
  );

  const completeState = applyShadowCloneTranscriptActivity(
    partialState,
    createPlanningActivity('complete', 2, {
      role: 'assistant',
      content: '1780600181963',
    }),
  );

  assert.equal(completeState.streamingTextContent, '');
  assert.equal(completeState.messages.length, 1);
  const messageContent = JSON.parse(completeState.messages[0].content);
  assert.equal(
    messageContent.content,
    'SUB_STREAM_MARKER_realtime_agent_1_1780600181963',
  );
});

test('Shadow Clone V2 assistant chunks are not dropped after higher-sequence tool progress for the same subtask', () => {
  const toolProgressState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('tool_call_chunk', 250, {
      tool_calls: [
        {
          id: 'tool-call-1',
          index: 0,
          function: {
            name: 'write_file',
            arguments: JSON.stringify({ path: '/workspace/realtime.md' }),
          },
        },
      ],
    }),
  );

  const assistantChunkState = applyShadowCloneTranscriptActivity(
    toolProgressState,
    createPlanningActivity('chunk', 125, {
      role: 'assistant',
      content: 'SUB_STREAM_MARKER_realtime_agent_1_1780605457772',
    }),
  );

  assert.equal(
    assistantChunkState.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780605457772',
  );
  assert.equal(assistantChunkState.latestLabel, '正在输出回复');
  assert.equal(
    assistantChunkState.lastSequence,
    250,
    'accepting a semantically valid lower-sequence assistant chunk must not move the global replay watermark backwards',
  );

  const staleToolReplayState = applyShadowCloneTranscriptActivity(
    assistantChunkState,
    createPlanningActivity('tool_call_chunk', 200, {
      tool_calls: [
        {
          id: 'stale-tool-call',
          index: 0,
          function: {
            name: 'write_file',
            arguments: JSON.stringify({ path: '/workspace/stale.md' }),
          },
        },
      ],
    }),
  );

  assert.equal(
    staleToolReplayState.lastSequence,
    250,
    'stale replay below the monotonic watermark must not advance or rewind lastSequence',
  );
  assert.equal(
    staleToolReplayState.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780605457772',
  );
  assert.equal(
    staleToolReplayState.streamingToolCall?.id,
    'tool-call-1',
    'the accepted higher-sequence tool state remains stable after a stale lower-sequence replay',
  );
});

test('Shadow Clone V2 lower-sequence cumulative assistant chunks continue after a higher-sequence tool watermark', () => {
  const toolProgressState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('tool_call_chunk', 300, {
      tool_calls: [
        {
          id: 'sleep-call-1',
          index: 0,
          function: {
            name: 'execute_command',
            arguments: JSON.stringify({ command: 'sleep 45' }),
          },
        },
      ],
    }),
  );

  const finalState = [
    [125, 'SUB'],
    [126, 'SUB_STREAM_MARKER'],
    [127, 'SUB_STREAM_MARKER_realtime_agent_1'],
    [128, 'SUB_STREAM_MARKER_realtime_agent_1_1780608380513'],
  ].reduce(
    (state, [sequence, content]) =>
      applyShadowCloneTranscriptActivity(
        state,
        createPlanningActivity('chunk', sequence, {
          role: 'assistant',
          content,
        }),
      ),
    toolProgressState,
  );

  assert.equal(
    finalState.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780608380513',
  );
  assert.equal(
    finalState.lastSequence,
    300,
    'lower-sequence cumulative assistant chunks must not rewind the replay watermark',
  );
});

test('Shadow Clone V2 stale lower-sequence non-cumulative assistant chunk is rejected after later tool progress', () => {
  const firstToolProgressState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('tool_call_chunk', 300, {
      tool_calls: [
        {
          id: 'sleep-call-1',
          index: 0,
          function: {
            name: 'execute_command',
            arguments: JSON.stringify({ command: 'sleep 45' }),
          },
        },
      ],
    }),
  );

  const markerState = [
    [125, 'SUB'],
    [126, 'SUB_STREAM_MARKER'],
    [127, 'SUB_STREAM_MARKER_realtime_agent_1'],
    [128, 'SUB_STREAM_MARKER_realtime_agent_1_1780608380513'],
  ].reduce(
    (state, [sequence, content]) =>
      applyShadowCloneTranscriptActivity(
        state,
        createPlanningActivity('chunk', sequence, {
          role: 'assistant',
          content,
        }),
      ),
    firstToolProgressState,
  );

  const laterToolProgressState = applyShadowCloneTranscriptActivity(
    markerState,
    createPlanningActivity('tool_call_chunk', 310, {
      tool_calls: [
        {
          id: 'write-call-1',
          index: 0,
          function: {
            name: 'write_file',
            arguments: JSON.stringify({ path: '/workspace/realtime.md' }),
          },
        },
      ],
    }),
  );

  const staleAssistantChunkState = applyShadowCloneTranscriptActivity(
    laterToolProgressState,
    createPlanningActivity('chunk', 180, {
      role: 'assistant',
      content: 'old partial',
    }),
  );

  assert.equal(
    staleAssistantChunkState.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780608380513',
    'stale lower-sequence non-cumulative assistant text must not be appended to the selected subagent live transcript',
  );
  assert.equal(
    staleAssistantChunkState.lastSequence,
    310,
    'stale lower-sequence assistant text must not rewind or advance the monotonic replay watermark',
  );
  assert.equal(staleAssistantChunkState.streamingToolCall?.id, 'write-call-1');
}
);

test('unchanged tool-call chunks advance the watermark without semantic churn and reject stale follow-ups', () => {
  const firstState = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    createPlanningActivity('tool_call_chunk', 1, {
      tool_calls: [
        {
          id: 'tool-call-1',
          index: 0,
          function: {
            name: 'write_file',
            arguments: JSON.stringify({
              file_path: '/workspace/report.md',
              file_contents: 'hello world',
            }),
          },
        },
      ],
    }),
  );

  const secondState = applyShadowCloneTranscriptActivity(
    firstState,
    createPlanningActivity('tool_call_chunk', 2, {
      tool_calls: [
        {
          id: 'tool-call-1',
          index: 0,
          function: {
            name: 'write_file',
            arguments: JSON.stringify({
              file_path: '/workspace/report.md',
              file_contents: 'hello world',
            }),
          },
        },
      ],
    }),
  );

  assertWatermarkOnlyAdvance(firstState, secondState, 2);

  const staleState = applyShadowCloneTranscriptActivity(
    secondState,
    createPlanningActivity('tool_call_chunk', 1, {
      tool_calls: [
        {
          id: 'tool-call-1',
          index: 0,
          function: {
            name: 'write_file',
            arguments: JSON.stringify({
              file_path: '/workspace/stale.md',
              file_contents: 'stale update',
            }),
          },
        },
      ],
    }),
  );

  assert.equal(staleState, secondState);
});

test('Claude SDK Agent tool result is surfaced as readable subagent assistant output', () => {
  const state = applyShadowCloneTranscriptActivity(
    createEmptyShadowCloneTranscriptState(),
    {
      subtask_id: 'agent-call-1',
      sequence: 1,
      message_type: 'tool',
      content: {
        tool_name: 'Agent',
        tool_call_id: 'agent-call-1',
        result: 'FINAL_SUBAGENT_OK',
      },
      metadata: {
        stream_status: 'complete',
        activity_owner: 'claude_sdk_subagent',
        parent_tool_use_id: 'agent-call-1',
      },
      created_at: '2026-04-07T00:00:00.000Z',
      updated_at: '2026-04-07T00:00:00.000Z',
    },
  );

  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].type, 'assistant');
  const content = JSON.parse(state.messages[0].content);
  assert.equal(content.content, 'FINAL_SUBAGENT_OK');
});
