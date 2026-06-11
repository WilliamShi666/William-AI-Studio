import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { buildUploadedImageMediaRefs } from './media-refs.ts';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);

test('builds image media refs from prepared attachments and sandbox uploads', () => {
  const refs = buildUploadedImageMediaRefs([
    {
      name: 'prepared.png',
      path: '/workspace/prepared.png',
      type: 'image/png',
      preparedAttachment: {
        attachment_id: 'artifact-1',
        name: 'prepared.png',
        path: '/workspace/prepared.png',
        size: 12,
        content_type: 'image/png',
        kind: 'image',
        mime_type: 'image/png',
        filename: 'prepared.png',
        sha256: 'abc123',
      },
    },
    {
      name: 'sandbox.jpg',
      path: '/workspace/sandbox.jpg',
      type: 'image/jpeg',
      size: 34,
    },
    {
      name: 'notes.txt',
      path: '/workspace/notes.txt',
      type: 'text/plain',
      size: 56,
    },
  ]);

  assert.deepEqual(refs, [
    {
      kind: 'image',
      path: '/workspace/prepared.png',
      mime_type: 'image/png',
      filename: 'prepared.png',
      sha256: 'abc123',
    },
    {
      kind: 'image',
      path: '/workspace/sandbox.jpg',
      mime_type: 'image/jpeg',
      filename: 'sandbox.jpg',
    },
  ]);
});

test('deduplicates image media refs by workspace path', () => {
  const refs = buildUploadedImageMediaRefs([
    {
      name: 'first.png',
      path: '/workspace/dup.png',
      type: 'image/png',
      size: 12,
    },
    {
      name: 'second.png',
      path: '/workspace/dup.png',
      type: 'image/png',
      size: 12,
    },
  ]);

  assert.deepEqual(refs, [
    {
      kind: 'image',
      path: '/workspace/dup.png',
      mime_type: 'image/png',
      filename: 'first.png',
    },
  ]);
});

test('thread page seeds chat input attachment preparation with the current project id', () => {
  const chatInputSource = readFileSync(path.join(testDir, 'chat-input.tsx'), 'utf8');
  const threadPageSource = readFileSync(
    path.join(
      testDir,
      '../../../app/(dashboard)/projects/[projectId]/thread/[threadId]/page.tsx',
    ),
    'utf8',
  );

  assert.match(
    chatInputSource,
    /projectId\?: string;/,
    'expected ChatInput to accept the current thread project id',
  );
  assert.match(
    threadPageSource,
    /<ChatInput[\s\S]*projectId=\{projectId\}/,
    'expected existing thread uploads to prepare attachments against the current project',
  );
});

test('continue-message flows save the user event before starting the agent', () => {
  const continueFlowFiles = [
    '../../../components/agents/agent-preview.tsx',
    '../../../components/agents/agent-builder-chat.tsx',
  ];

  for (const relativePath of continueFlowFiles) {
    const source = readFileSync(path.join(testDir, relativePath), 'utf8');

    assert.doesNotMatch(
      source,
      /Promise\.allSettled\(\[messagePromise,\s*agentPromise\]\)/,
      `${relativePath} must not start the agent before media_refs are written to the user event`,
    );
    const saveIndex = source.indexOf('const savedMessage = await addUserMessageMutation.mutateAsync({');
    const mediaRefIndex = source.indexOf('media_refs: options?.media_refs', saveIndex);
    const startIndex = source.indexOf('await startAgentMutation.mutateAsync', mediaRefIndex);

    assert.ok(
      saveIndex >= 0 && mediaRefIndex > saveIndex,
      `${relativePath} should persist the user message with media_refs`,
    );
    assert.ok(
      startIndex > mediaRefIndex,
      `${relativePath} should start the agent only after the user event is saved`,
    );
  }
});

test('official DeepSeek high/max models submit with high reasoning controls', () => {
  const chatInputSource = readFileSync(path.join(testDir, 'chat-input.tsx'), 'utf8');

  for (const modelId of [
    'deepseek-v4-pro-high',
    'deepseek-v4-pro-max',
    'deepseek-v4-flash-high',
    'deepseek-v4-flash-max',
  ]) {
    assert.match(
      chatInputSource,
      new RegExp(`normalizedModelName === '${modelId}'`),
      `expected ${modelId} to enable reasoning controls in ChatInput submit payload`,
    );
  }

  assert.match(
    chatInputSource,
    /enable_thinking: thinkingEnabled,[\s\S]*reasoning_effort: thinkingEnabled \? 'high' : 'low'/,
    'expected reasoning-enabled chat submissions to request high effort',
  );
});

test('chat input forwards smart and shadow clone modes to the agent start request', () => {
  const chatInputSource = readFileSync(path.join(testDir, 'chat-input.tsx'), 'utf8');
  const shadowCloneToggleSource = readFileSync(
    path.join(testDir, 'shadow-clone-toggle.tsx'),
    'utf8',
  );
  const apiSource = readFileSync(
    path.join(testDir, '../../../lib/api.ts'),
    'utf8',
  );

  assert.match(
    shadowCloneToggleSource,
    /value:\s*'auto'[\s\S]*shortLabel:\s*'智能'/,
    'expected the frontend Shadow Clone toggle to expose 智能/auto mode',
  );
  assert.match(
    shadowCloneToggleSource,
    /value:\s*'on'[\s\S]*shortLabel:\s*'影分身'/,
    'expected the frontend Shadow Clone toggle to expose 影分身/on mode',
  );
  assert.match(
    chatInputSource,
    /\.\.\.\(enableShadowCloneUI[\s\S]*\?\s*\{\s*shadow_clone_mode:\s*shadowCloneMode\s*\}/,
    'expected ChatInput to submit the selected Shadow Clone mode when the UI is enabled',
  );
  assert.match(
    apiSource,
    /if\s*\(finalOptions\.shadow_clone_mode\)\s*\{[\s\S]*body\.shadow_clone_mode\s*=\s*finalOptions\.shadow_clone_mode;/,
    'expected startAgent API to forward shadow_clone_mode to the backend body',
  );
});
