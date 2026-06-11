import test from 'node:test';
import assert from 'node:assert/strict';

// Mock localStorage for Zustand persist middleware in Node.js test environment
const storage = new Map();
global.localStorage = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => { storage.set(key, value); },
  removeItem: (key) => { storage.delete(key); },
  clear: () => { storage.clear(); },
  get length() { return storage.size; },
  key: (index) => [...storage.keys()][index] ?? null,
};

// Register alias to avoid Next.js module resolution issues
import { registerHooks } from 'node:module';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const srcRoot = path.resolve(path.dirname(testFilePath), '../../..');
const resolveAlias = (specifier) => {
  const relativePath = specifier.slice(2);
  const candidates = [
    path.join(srcRoot, `${relativePath}.ts`),
    path.join(srcRoot, `${relativePath}.tsx`),
    path.join(srcRoot, relativePath, 'index.ts'),
    path.join(srcRoot, relativePath, 'index.tsx'),
  ];
  const match = candidates.find((c) => existsSync(c));
  if (!match) throw new Error(`Cannot resolve: ${specifier}`);
  return pathToFileURL(match).href;
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@/lib/api') {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(`export const confirmShadowClone = async () => {};
export const denyShadowClone = async () => {};
export const getShadowCloneFullResult = async () => ({});
export const getShadowCloneResults = async () => ({});
export const getShadowCloneStatus = async () => ({});`)}` };
    }
    if (specifier === '@/lib/shadow-clone-panel') {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(`export const applyShadowCloneActivity = () => ({});
export const buildShadowCloneCompleteToolCall = () => ({});
export const createEmptyShadowClonePanelState = () => ({});
export const SHADOW_CLONE_PANEL_TOOL_CALL = {};`)}` };
    }
    if (specifier === '@/lib/shadow-clone-transcript') {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(`export const applyShadowCloneTranscriptActivity = (s, a) => ({...s});
export const applyShadowCloneTranscriptMessage = (s, m, r, a) => ({...s});
export const createEmptyShadowCloneTranscriptState = () => ({ messages: [], streamingTextContent: '', streamingReasoningContent: '', streamingToolCall: null });
export const upsertSyntheticCompleteTranscriptMessage = (s, m) => ({...s});
export const retainV2ProjectionOutputTranscriptMessages = (s) => s;`)}` };
    }
    if (specifier.startsWith('@/')) {
      return { shortCircuit: true, url: resolveAlias(specifier) };
    }
    return nextResolve(specifier, context);
  },
});

import { useShadowCloneStore } from './shadow-clone-store.ts';

// Reset store before each test
function resetStore() {
  useShadowCloneStore.getState().resetRuntime();
}

test('initSubagentInspection creates inspection state with connecting status', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-abc');

  const state = store.inspectionStreamStates['task-abc'];
  assert.ok(state, 'inspection state should exist');
  assert.equal(state.status, 'connecting');
  assert.equal(state.textContent, '');
  assert.equal(state.reasoningContent, '');
  assert.equal(state.isWritingFile, false);
  assert.equal(state.messages.length, 0);
});

test('initSubagentInspection is idempotent — re-initializes on second call', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-1');
  store.applySubagentStreamEvent('task-1', { status: 'streaming', textContent: 'Hello' });

  // Re-initialize should reset
  store.initSubagentInspection('task-1');
  const state = store.inspectionStreamStates['task-1'];
  assert.equal(state.textContent, '');
  assert.equal(state.status, 'connecting');
});

test('applySubagentStreamEvent accumulates text content', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-x');
  store.applySubagentStreamEvent('task-x', { textContent: 'Hello ' });
  store.applySubagentStreamEvent('task-x', { textContent: 'World' });

  const state = store.inspectionStreamStates['task-x'];
  assert.equal(state.textContent, 'Hello World');
});

test('applySubagentStreamEvent updates status', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-x');
  store.applySubagentStreamEvent('task-x', { status: 'streaming' });

  assert.equal(store.inspectionStreamStates['task-x'].status, 'streaming');
});

test('applySubagentStreamEvent accumulates reasoning content', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-r');
  store.applySubagentStreamEvent('task-r', { reasoningContent: 'I need to think ' });
  store.applySubagentStreamEvent('task-r', { reasoningContent: 'about this carefully.' });

  const state = store.inspectionStreamStates['task-r'];
  assert.equal(state.reasoningContent, 'I need to think about this carefully.');
});

test('applySubagentStreamEvent appends messages', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-m');
  const msg1 = { message_id: 'm1', thread_id: 't1', type: 'assistant', content: 'Hello', metadata: '{}', created_at: '2025-01-01', updated_at: '2025-01-01', is_llm_message: true };
  const msg2 = { message_id: 'm2', thread_id: 't1', type: 'assistant', content: 'World', metadata: '{}', created_at: '2025-01-01', updated_at: '2025-01-01', is_llm_message: true };

  store.applySubagentStreamEvent('task-m', { message: msg1 });
  store.applySubagentStreamEvent('task-m', { message: msg2 });

  assert.equal(store.inspectionStreamStates['task-m'].messages.length, 2);
  assert.equal(store.inspectionStreamStates['task-m'].messages[0].message_id, 'm1');
  assert.equal(store.inspectionStreamStates['task-m'].messages[1].message_id, 'm2');
});

test('applySubagentStreamEvent is a no-op for unknown subtaskId', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  // Should not throw
  store.applySubagentStreamEvent('nonexistent', { textContent: 'hello' });
  assert.equal(store.inspectionStreamStates['nonexistent'], undefined);
});

test('clearSubagentInspection removes inspection state', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-del');
  assert.ok(store.inspectionStreamStates['task-del']);

  store.clearSubagentInspection('task-del');
  assert.equal(store.inspectionStreamStates['task-del'], undefined);
});

test('clearSubagentInspection is a no-op for unknown subtaskId', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  // Should not throw
  store.clearSubagentInspection('never-existed');
  assert.equal(store.inspectionStreamStates['never-existed'], undefined);
});

test('multiple subtask inspections are independent', () => {
  resetStore();
  const store = useShadowCloneStore.getState();

  store.initSubagentInspection('task-a');
  store.initSubagentInspection('task-b');

  store.applySubagentStreamEvent('task-a', { textContent: 'A content' });
  store.applySubagentStreamEvent('task-b', { textContent: 'B content' });

  assert.equal(store.inspectionStreamStates['task-a'].textContent, 'A content');
  assert.equal(store.inspectionStreamStates['task-b'].textContent, 'B content');
});

test('resetRuntime clears all inspection states', () => {
  const store = useShadowCloneStore.getState();
  store.initSubagentInspection('task-z');
  store.applySubagentStreamEvent('task-z', { textContent: 'data' });
  assert.ok(store.inspectionStreamStates['task-z']);

  store.resetRuntime();
  assert.deepEqual(store.inspectionStreamStates, {});
});
