import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const helperUrl = pathToFileURL(path.join(testDir, 'file-delivery-run-selection.js'));

const {
  selectPreferredAgentRun,
  selectPreferredFileDeliveryRun,
  selectShadowCloneBootstrapRun,
} = await import(helperUrl.href);

test('selectPreferredAgentRun keeps current streaming semantics', () => {
  const runs = [
    {
      id: 'completed-newer',
      status: 'completed',
      started_at: '2026-05-19T00:00:00.000Z',
      completed_at: '2026-05-19T00:05:00.000Z',
    },
    {
      id: 'running-older',
      status: 'running',
      started_at: '2026-05-18T23:00:00.000Z',
      completed_at: null,
    },
  ];

  assert.equal(selectPreferredAgentRun(runs, null).id, 'running-older');
});

test('selectPreferredFileDeliveryRun prefers latest artifact-backed run after streaming run id is cleared', () => {
  const runs = [
    {
      id: 'newer-no-files',
      status: 'completed',
      started_at: '2026-05-19T00:10:00.000Z',
      completed_at: '2026-05-19T00:11:00.000Z',
      file_delivery_source: {
        browseSandboxId: null,
      },
    },
    {
      id: 'older-with-files',
      status: 'completed',
      started_at: '2026-05-19T00:00:00.000Z',
      completed_at: '2026-05-19T00:05:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'claude-local:older-with-files',
        identitySource: 'workspace_artifacts',
      },
    },
  ];

  const selected = selectPreferredFileDeliveryRun(runs, null);

  assert.equal(selected.id, 'older-with-files');
  assert.equal(
    selected.file_delivery_source.browseSandboxId,
    'claude-local:older-with-files',
  );
});

test('selectPreferredFileDeliveryRun honors preferred run when it can deliver files', () => {
  const runs = [
    {
      id: 'latest-with-files',
      status: 'completed',
      started_at: '2026-05-19T00:20:00.000Z',
      completed_at: '2026-05-19T00:21:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'claude-local:latest-with-files',
      },
    },
    {
      id: 'preferred-with-files',
      status: 'completed',
      started_at: '2026-05-19T00:00:00.000Z',
      completed_at: '2026-05-19T00:05:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'claude-local:preferred-with-files',
      },
    },
  ];

  assert.equal(
    selectPreferredFileDeliveryRun(runs, 'preferred-with-files').id,
    'preferred-with-files',
  );
});


test('selectPreferredFileDeliveryRun ignores runs from other threads when thread id is provided', () => {
  const runs = [
    {
      id: 'other-thread-run',
      thread_id: 'thread-other',
      status: 'completed',
      started_at: '2026-05-19T00:20:00.000Z',
      completed_at: '2026-05-19T00:21:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'claude-local:other-thread-run',
      },
    },
    {
      id: 'current-thread-run',
      thread_id: 'thread-current',
      status: 'completed',
      started_at: '2026-05-19T00:00:00.000Z',
      completed_at: '2026-05-19T00:05:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'claude-local:current-thread-run',
      },
    },
  ];

  assert.equal(
    selectPreferredFileDeliveryRun(runs, null, 'thread-current').id,
    'current-thread-run',
  );
});

test('selectPreferredFileDeliveryRun prefers thread workspace source over latest single run source', () => {
  const runs = [
    {
      id: 'latest-single-run',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-05-21T00:00:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'claude-local:latest-single-run',
        identitySource: 'workspace_artifacts',
      },
    },
    {
      id: 'thread-workspace',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-05-20T00:00:00.000Z',
      file_delivery_source: {
        browseSandboxId: 'thread-workspace:thread-123',
        identitySource: 'thread_workspace_artifacts',
      },
    },
  ];

  const selected = selectPreferredFileDeliveryRun(runs, null, 'thread-123');

  assert.equal(selected.id, 'thread-workspace');
  assert.equal(selected.file_delivery_source.browseSandboxId, 'thread-workspace:thread-123');
});

test('selectShadowCloneBootstrapRun restores latest completed Shadow Clone V2 run for historical thread reload', () => {
  const runs = [
    {
      id: 'older-v2',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-06-03T00:05:00.000Z',
      metadata: {
        shadow_clone_mode: 'on',
        shadow_clone_runtime: 'v2',
      },
    },
    {
      id: 'latest-regular',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-06-03T00:15:00.000Z',
      metadata: {
        shadow_clone_mode: 'off',
      },
    },
    {
      id: 'latest-v2',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-06-03T00:10:00.000Z',
      metadata: JSON.stringify({
        shadow_clone_mode: 'auto',
        shadow_clone_runtime: 'v2',
      }),
    },
  ];

  const selected = selectShadowCloneBootstrapRun(runs, null, 'thread-123');

  assert.equal(selected.id, 'latest-v2');
});

test('selectShadowCloneBootstrapRun keeps running Shadow Clone run preferred over terminal runs', () => {
  const runs = [
    {
      id: 'completed-v2',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-06-03T00:10:00.000Z',
      metadata: { shadow_clone_mode: 'on', shadow_clone_runtime: 'v2' },
    },
    {
      id: 'running-v2',
      thread_id: 'thread-123',
      status: 'running',
      started_at: '2026-06-03T00:09:00.000Z',
      completed_at: null,
      metadata: { shadow_clone_mode: 'auto', shadow_clone_runtime: 'v2' },
    },
  ];

  const selected = selectShadowCloneBootstrapRun(runs, null, 'thread-123');

  assert.equal(selected.id, 'running-v2');
});

test('selectShadowCloneBootstrapRun returns null for non Shadow Clone V2 runs', () => {
  const runs = [
    {
      id: 'regular',
      thread_id: 'thread-123',
      status: 'completed',
      completed_at: '2026-06-03T00:10:00.000Z',
      metadata: { shadow_clone_mode: 'off' },
    },
  ];

  assert.equal(selectShadowCloneBootstrapRun(runs, null, 'thread-123'), null);
});
