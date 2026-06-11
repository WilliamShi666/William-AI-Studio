import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveClaudeSDKSubagentRoute } from './claude-sdk-subagent-routing.ts';

test('Agent tool start becomes a shadow clone subagent_started event', () => {
  const route = resolveClaudeSDKSubagentRoute({
    messageType: 'assistant',
    sequence: 7,
    parsedContent: {
      role: 'assistant',
      content: '',
      tool_calls: [
        {
          id: 'toolu-1',
          function: {
            name: 'Agent',
            arguments: JSON.stringify({
              agent: 'teammate-1',
              prompt: 'Read the document and summarize key risks.',
            }),
          },
        },
      ],
    },
    parsedMetadata: {
      stream_status: 'tool_call_chunk',
      activity_owner: 'claude_sdk_subagent',
      subagent_tool_call_id: 'toolu-1',
    },
  });

  assert.equal(route.streamStatus, 'subagent_started');
  assert.equal(route.content.subtask_id, 'toolu-1');
  assert.equal(route.content.role, 'teammate-1');
  assert.equal(route.content.task_description, 'Read the document and summarize key risks.');
  assert.equal(route.content.source, 'claude_sdk');
  assert.deepEqual(route.liveActivity, {
    scope: 'shadow_clone_main',
    phase: 'execution',
    reason: 'subagent_started',
    subtask_id: 'toolu-1',
  });
});


test('Agent tool argument delta refines teammate name and task description for same subtask', () => {
  const route = resolveClaudeSDKSubagentRoute({
    messageType: 'assistant',
    sequence: 8,
    parsedContent: {
      role: 'assistant',
      content: '',
      tool_calls: [
        {
          id: 'toolu-delta',
          function: {
            name: 'Agent',
            arguments: JSON.stringify({
              subagent_type: 'teammate-2',
              prompt: 'Produce the exact token DELTA_OK.',
            }),
          },
        },
      ],
    },
    parsedMetadata: {
      stream_status: 'tool_call_chunk',
      activity_owner: 'claude_sdk_subagent',
      subagent_tool_call_id: 'toolu-delta',
    },
  });

  assert.equal(route.streamStatus, 'subagent_started');
  assert.equal(route.content.subtask_id, 'toolu-delta');
  assert.equal(route.content.role, 'teammate-2');
  assert.equal(route.content.task_description, 'Produce the exact token DELTA_OK.');
});

test('subagent child stream event becomes subagent_activity by parent tool id', () => {
  const route = resolveClaudeSDKSubagentRoute({
    messageType: 'assistant',
    sequence: 9,
    parsedContent: {
      role: 'assistant',
      content: 'FULL_ALPHA_OK',
    },
    parsedMetadata: {
      stream_status: 'chunk',
      activity_owner: 'claude_sdk_subagent',
      parent_tool_use_id: 'toolu-1',
    },
  });

  assert.equal(route.streamStatus, 'subagent_activity');
  assert.equal(route.content.subtask_id, 'toolu-1');
  assert.equal(route.content.message_type, 'assistant');
  assert.equal(route.content.content.content, 'FULL_ALPHA_OK');
  assert.equal(route.content.source, 'claude_sdk');
  assert.equal(route.liveActivity.reason, 'subagent_activity');
});

test('ordinary tool call is ignored when it is not Claude SDK subagent-owned', () => {
  const route = resolveClaudeSDKSubagentRoute({
    messageType: 'assistant',
    sequence: 1,
    parsedContent: { role: 'assistant', content: 'hello' },
    parsedMetadata: { stream_status: 'chunk' },
  });

  assert.equal(route, null);
});
