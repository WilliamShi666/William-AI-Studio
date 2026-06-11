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
  applyShadowCloneActivity,
  createEmptyShadowClonePanelState,
} = await import('./shadow-clone-panel.ts');

test('Claude SDK child tool_call_chunk activity appears in selected subagent tool panel', () => {
  const panelState = applyShadowCloneActivity(createEmptyShadowClonePanelState(), {
    subtask_id: 'agent-call-1',
    sequence: 1,
    message_type: 'assistant',
    content: {
      role: 'assistant',
      content: '',
      tool_calls: [
        {
          id: 'write-call-1',
          function: {
            name: 'Write',
            arguments: JSON.stringify({
              file_path: '/workspace/subagent.md',
              content: 'SUB_UI_OK',
            }),
          },
        },
      ],
    },
    metadata: {
      stream_status: 'tool_call_chunk',
      activity_owner: 'claude_sdk_subagent',
      parent_tool_use_id: 'agent-call-1',
    },
  });

  assert.equal(panelState.toolCalls.length, 1);
  assert.equal(panelState.toolCalls[0].assistantCall.name, 'write');
  assert.equal(panelState.toolCalls[0].assistantCall.toolCallId, 'write-call-1');
  assert.match(panelState.toolCalls[0].assistantCall.content || '', /subagent\.md/);
  assert.match(panelState.streamingText, /SUB_UI_OK/);
});

test('Claude SDK child tool result finalizes the matching selected subagent tool call', () => {
  const streamingState = applyShadowCloneActivity(createEmptyShadowClonePanelState(), {
    subtask_id: 'agent-call-1',
    sequence: 1,
    message_type: 'assistant',
    content: {
      role: 'assistant',
      content: '',
      tool_calls: [
        {
          id: 'bash-call-1',
          function: {
            name: 'Bash',
            arguments: JSON.stringify({ command: 'echo SUB_UI_OK' }),
          },
        },
      ],
    },
    metadata: {
      stream_status: 'tool_call_chunk',
      activity_owner: 'claude_sdk_subagent',
      parent_tool_use_id: 'agent-call-1',
    },
  });

  const completedState = applyShadowCloneActivity(streamingState, {
    subtask_id: 'agent-call-1',
    sequence: 2,
    message_type: 'tool',
    content: {
      tool_name: 'Bash',
      tool_call_id: 'bash-call-1',
      result: 'SUB_UI_OK',
    },
    metadata: {
      stream_status: 'complete',
      activity_owner: 'claude_sdk_subagent',
      parent_tool_use_id: 'agent-call-1',
    },
  });

  assert.equal(completedState.toolCalls.length, 1);
  assert.equal(completedState.toolCalls[0].assistantCall.name, 'bash');
  assert.equal(completedState.toolCalls[0].toolResult?.content, 'SUB_UI_OK');
});
