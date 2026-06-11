import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { registerHooks } from 'node:module';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const frontendRoot = path.resolve(testDir, '../../..');
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

const apiModuleUrl = `data:text/javascript,${encodeURIComponent(`
const readMock = () => {
  if (!globalThis.__shadowCloneApiMock) {
    throw new Error('Missing __shadowCloneApiMock');
  }

  return globalThis.__shadowCloneApiMock;
};

export const confirmShadowClone = async (...args) => readMock().confirmShadowClone(...args);
export const denyShadowClone = async (...args) => readMock().denyShadowClone(...args);
export const getShadowCloneFullResult = async (...args) => readMock().getShadowCloneFullResult(...args);
export const getShadowCloneResults = async (...args) => readMock().getShadowCloneResults(...args);
export const getShadowCloneStatus = async (...args) => readMock().getShadowCloneStatus(...args);
`)}`;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@/lib/api') {
      return {
        shortCircuit: true,
        url: apiModuleUrl,
      };
    }

    if (specifier.startsWith('@/')) {
      return {
        shortCircuit: true,
        url: resolveAliasPath(specifier),
      };
    }

    return nextResolve(specifier, context);
  },
});

const createStorage = () => {
  const values = new Map();

  return {
    clear() {
      values.clear();
    },
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    removeItem(key) {
      values.delete(key);
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
  };
};

globalThis.localStorage = createStorage();
globalThis.__shadowCloneApiMock = {
  confirmShadowClone: async () => ({ status: 'confirmed' }),
  denyShadowClone: async () => ({ status: 'denied', reason: null }),
  getShadowCloneFullResult: async () => ({ result: '' }),
  getShadowCloneResults: async () => ({ results: [] }),
  getShadowCloneStatus: async () => ({
    status: 'not_found',
    proposal: null,
    subagents: {},
    recovery: { pending: false },
  }),
};

const { useShadowCloneStore } = await import('./shadow-clone-store.ts');

const createSubtask = (id, status) => ({
  id,
  role: `${id}-role`,
  task_description: `${id}-task`,
  status,
});

const createLiveActivity = (phase = 'execution', reason = 'test-live-activity') => ({
  scope: 'shadow_clone_main',
  phase,
  reason,
  subtask_id: null,
  epoch: 1,
  updated_at: null,
});

const createDeferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });

  return { promise, resolve, reject };
};

const resetStore = (partialState = {}) => {
  globalThis.localStorage.clear();
  useShadowCloneStore.getState().resetRuntime();
  useShadowCloneStore.setState({
    mode: 'off',
    modeExplicitlySet: false,
    ...partialState,
  });
};

test.beforeEach(() => {
  globalThis.__shadowCloneApiMock = {
    confirmShadowClone: async () => ({ status: 'confirmed' }),
    denyShadowClone: async () => ({ status: 'denied', reason: null }),
    getShadowCloneFullResult: async () => ({ result: '' }),
    getShadowCloneResults: async () => ({ results: [] }),
    getShadowCloneStatus: async () => ({
      status: 'not_found',
      proposal: null,
      subagents: {},
      recovery: { pending: false },
    }),
  };
  resetStore();
});

test('applyLiveActivity clears stale live activity when explicitly set to null', () => {
  resetStore({
    currentRunId: 'run-1',
    liveActivity: createLiveActivity(),
  });

  useShadowCloneStore.getState().applyLiveActivity(null, 'run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-1');
  assert.equal(state.liveActivity, null);
});

test('heartbeat keeps pendingCount derived from subtasks instead of event payload', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    subtasks: [
      createSubtask('subtask-pending', 'pending'),
      createSubtask('subtask-running', 'running'),
    ],
    pendingCount: 99,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'heartbeat',
    { pending: 42 },
    undefined,
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.pendingCount, 1);
});

test('first subagent_activity moves a pending subtask into running state', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    subtasks: [createSubtask('subtask-1', 'pending')],
    pendingCount: 1,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'subtask-1',
      sequence: 1,
      message_type: 'assistant',
      content: {
        content: 'working',
      },
      metadata: {
        stream_status: 'chunk',
      },
    },
    undefined,
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(state.pendingCount, 0);
});

test('Claude SDK subagent_started enters running state immediately for visible activity', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'idle',
    pendingCount: 0,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'toolu-claude-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'Write a file in the sandbox.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'running');
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(state.pendingCount, 0);
});

test('Shadow Clone V2 subagent_started creates a visible in-progress transcript message', () => {
  resetStore({ mode: 'on', currentRunId: 'run-v2-visible' });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'gpu-story-1',
      source: 'shadow_clone_v2',
      agent_name: 'teammate-1',
      role: 'Markdown writer',
      task_description: 'Write one markdown story about GPU life.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-v2-visible',
  );

  useShadowCloneStore.getState().setActiveSubtask('gpu-story-1');

  const state = useShadowCloneStore.getState();
  const transcript = state.subtaskTranscriptStates['gpu-story-1'];

  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(state.activeSubtaskId, 'gpu-story-1');
  assert.ok(transcript, 'expected selected subagent transcript state');
  assert.equal(transcript.messages.length, 1);

  const message = transcript.messages[0];
  const content = JSON.parse(message.content);
  const metadata = JSON.parse(message.metadata);

  assert.equal(message.type, 'assistant');
  assert.equal(message.is_llm_message, false);
  assert.equal(
    message.message_id,
    'shadow-clone-v2-progress:run-v2-visible:gpu-story-1:started',
  );
  assert.match(content.content, /系统进度/);
  assert.match(content.content, /开始执行|收到任务/);
  assert.equal(metadata.shadow_clone_system_progress, true);
  assert.equal(metadata.message_kind, 'orchestration_progress');
  assert.equal(metadata.agent_run_id, 'run-v2-visible');
  assert.equal(metadata.shadow_clone_subtask_id, 'gpu-story-1');
  assert.equal(metadata.stream_status, 'complete');
});

test('Shadow Clone V2 started event activates planning UI state', () => {
  resetStore();

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_started',
    {
      agent_run_id: 'run-v2',
      thread_run_id: 'thread-run-v2',
    },
    undefined,
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-v2');
  assert.equal(state.phase, 'planning');
  assert.equal(state.liveActivity?.scope, 'shadow_clone_main');
  assert.equal(state.liveActivity?.phase, 'planning');
  assert.equal(state.subtasks.length, 0);
});

test('Shadow Clone V2 plan event creates visible clickable team member ledger', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'planning',
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_plan_created',
    {
      subtasks: [
        {
          id: 'task-1',
          role: 'softmax storyteller',
          task_description: 'Execute assigned analysis task.',
          status: 'pending',
        },
        {
          id: 'task-2',
          role: 'tensor comedian',
          task_description: 'Execute assigned review task.',
          status: 'running',
        },
      ],
      dependencies: [],
    },
    undefined,
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-v2');
  assert.equal(state.phase, 'running');
  assert.equal(state.liveActivity?.scope, 'shadow_clone_main');
  assert.equal(state.liveActivity?.phase, 'execution');
  assert.equal(state.subtasks.length, 2);
  assert.equal(state.subtasks[0]?.id, 'task-1');
  assert.equal(state.subtasks[0]?.role, 'softmax storyteller');
  assert.equal(state.subtasks[0]?.task_description, 'Execute assigned analysis task.');
  assert.equal(state.subtasks[0]?.status, 'pending');
  assert.equal(state.subtasks[1]?.status, 'running');
  assert.equal(state.pendingCount, 1);
});

test('Shadow Clone V2 projection creates main chat progress transcript from event log replay', () => {
  resetStore({ currentRunId: 'run-v2-projection-progress' });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 1,
      event_id: '1000-0',
      event_cursor: '1000-0',
      v2_event_type: 'run_started',
      agent_run_id: 'run-v2-projection-progress',
      projection: {
        status: 'running',
        mode: 'v2',
        updated_at: '2026-06-04T05:17:00+00:00',
        team: { members: {} },
        tasks: {},
        agents: {},
        proposal: { subtasks: [] },
      },
    },
    undefined,
    'run-v2-projection-progress',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.mainTranscriptState.messages.length, 1);

  const message = state.mainTranscriptState.messages[0];
  const content = JSON.parse(message.content);
  const metadata = JSON.parse(message.metadata);

  assert.equal(message.message_id, 'shadow-clone-v2-main-progress:run-v2-projection-progress:run_started:1');
  assert.equal(message.is_llm_message, false);
  assert.match(content.content, /系统进度/);
  assert.match(content.content, /收到请求|创建团队|规划任务/);
  assert.equal(metadata.shadow_clone_system_progress, true);
  assert.equal(metadata.message_kind, 'orchestration_progress');
  assert.equal(metadata.source_event_type, 'run_started');
});

test('Shadow Clone V2 projection creates selected subagent progress transcript from task claim', () => {
  resetStore({ currentRunId: 'run-v2-subagent-projection' });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 9,
      event_id: '1009-0',
      event_cursor: '1009-0',
      v2_event_type: 'task_claimed',
      agent_run_id: 'run-v2-subagent-projection',
      projection: {
        status: 'running',
        mode: 'v2',
        updated_at: '2026-06-04T05:17:45+00:00',
        team: {
          members: {
            'realtime-agent-1': {
              agent_name: 'realtime-agent-1',
              status: 'working',
              current_task_ids: ['realtime-task-1'],
            },
          },
        },
        agents: {
          'realtime-agent-1': {
            agent_name: 'realtime-agent-1',
            status: 'working',
            current_task_ids: ['realtime-task-1'],
          },
        },
        tasks: {
          'realtime-task-1': {
            id: 'realtime-task-1',
            subject: 'Realtime writer',
            description: 'Write a markdown file.',
            status: 'in_progress',
            owner_agent: 'realtime-agent-1',
            agent_name: 'realtime-agent-1',
          },
        },
        proposal: { subtasks: [] },
      },
    },
    undefined,
    'run-v2-subagent-projection',
  );

  useShadowCloneStore.getState().setActiveSubtask('realtime-task-1');

  const state = useShadowCloneStore.getState();
  const transcript = state.subtaskTranscriptStates['realtime-task-1'];
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.ok(transcript, 'expected projection-created subagent transcript');
  assert.equal(transcript.messages.length, 1);

  const message = transcript.messages[0];
  const content = JSON.parse(message.content);
  const metadata = JSON.parse(message.metadata);

  assert.equal(message.message_id, 'shadow-clone-v2-progress:run-v2-subagent-projection:realtime-task-1:task_claimed:9');
  assert.equal(message.is_llm_message, false);
  assert.match(content.content, /系统进度/);
  assert.match(content.content, /已收到任务|开始执行/);
  assert.equal(metadata.shadow_clone_system_progress, true);
  assert.equal(metadata.message_kind, 'orchestration_progress');
  assert.equal(metadata.source_event_type, 'task_claimed');
  assert.equal(metadata.shadow_clone_subtask_id, 'realtime-task-1');
});


test('Shadow Clone V2 projection treats claimed pending task with owner_agent as visible subagent progress', () => {
  resetStore({ currentRunId: 'run-v2-pending-owner-claim' });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 12,
      event_id: '1012-0',
      event_cursor: '1012-0',
      v2_event_type: 'task_claimed',
      agent_run_id: 'run-v2-pending-owner-claim',
      projection: {
        status: 'running',
        mode: 'v2',
        updated_at: '2026-06-04T05:30:51+00:00',
        team: {
          members: {
            'realtime-agent-1': {
              agent_name: 'realtime-agent-1',
              status: 'working',
              current_task_ids: ['realtime-e2e-task-1'],
            },
          },
        },
        agents: {
          'realtime-agent-1': {
            agent_name: 'realtime-agent-1',
            status: 'working',
            current_task_ids: ['realtime-e2e-task-1'],
          },
        },
        tasks: {
          'realtime-e2e-task-1': {
            id: 'realtime-e2e-task-1',
            subject: 'E2E realtime progress for realtime-agent-1',
            description: 'Run sleep 20, then write markdown.',
            status: 'pending',
            owner_agent: 'realtime-agent-1',
            agent_name: 'realtime-agent-1',
          },
        },
        proposal: { subtasks: [] },
      },
    },
    undefined,
    'run-v2-pending-owner-claim',
  );

  useShadowCloneStore.getState().setActiveSubtask('realtime-e2e-task-1');

  const state = useShadowCloneStore.getState();
  const transcript = state.subtaskTranscriptStates['realtime-e2e-task-1'];
  assert.ok(transcript, 'task_claimed pending+owner_agent task should create selected subagent transcript');
  assert.equal(transcript.messages.length, 1);

  const message = transcript.messages[0];
  const content = JSON.parse(message.content);
  const metadata = JSON.parse(message.metadata);

  assert.equal(message.message_id, 'shadow-clone-v2-progress:run-v2-pending-owner-claim:realtime-e2e-task-1:task_claimed:12');
  assert.match(content.content, /系统进度/);
  assert.match(content.content, /已收到任务|开始执行/);
  assert.equal(metadata.shadow_clone_subtask_id, 'realtime-e2e-task-1');
  assert.equal(metadata.source_event_type, 'task_claimed');
});

test('Shadow Clone V2 completion preserves team member list and active inspection', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    subtasks: [
      createSubtask('task-1', 'running'),
      createSubtask('task-2', 'pending'),
    ],
    activeSubtaskId: 'task-1',
    viewScope: 'shadow_clone_subagent',
    pendingCount: 1,
    liveActivity: createLiveActivity(),
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_execution_completed',
    {
      executed_task_count: 2,
    },
    undefined,
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'completed');
  assert.equal(state.subtasks.length, 2);
  assert.equal(state.subtasks[0]?.status, 'completed');
  assert.equal(state.subtasks[1]?.status, 'completed');
  assert.equal(state.activeSubtaskId, 'task-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(state.pendingCount, 0);
});

test('Shadow Clone V2 failure marks failed teammate and keeps team inspection available', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    subtasks: [
      createSubtask('task-1', 'running'),
      createSubtask('task-2', 'completed'),
    ],
    activeSubtaskId: 'task-1',
    viewScope: 'shadow_clone_subagent',
    pendingCount: 0,
    liveActivity: createLiveActivity(),
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_execution_failed',
    {
      failed_task_id: 'task-1',
      error: 'worker failed',
      subtasks: [
        {
          id: 'task-1',
          role: 'builder',
          task_description: 'Build the artifact.',
          status: 'failed',
          error: 'worker failed',
        },
        {
          id: 'task-2',
          role: 'reviewer',
          task_description: 'Review the artifact.',
          status: 'completed',
        },
      ],
    },
    undefined,
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'error');
  assert.equal(state.subtasks.length, 2);
  assert.equal(state.subtasks[0]?.status, 'failed');
  assert.equal(state.subtasks[0]?.error, 'worker failed');
  assert.equal(state.subtasks[1]?.status, 'completed');
  assert.equal(state.activeSubtaskId, 'task-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(state.lastError, 'worker failed');
});

test('selectSubtask enters teammate inspection while preserving the selected teammate', async () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    subtasks: [
      {
        ...createSubtask('toolu-claude-1', 'running'),
        role: 'teammate-1',
        task_description: 'Inspect the sandbox.',
      },
    ],
  });

  await useShadowCloneStore.getState().selectSubtask('run-1', 'toolu-claude-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.activeSubtaskId, 'toolu-claude-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
});

test('Shadow Clone V2 replayed plan event preserves selected running subagent inspection', async () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    subtasks: [
      {
        ...createSubtask('e2e-realtime-agent-1', 'running'),
        role: 'realtime-agent-1',
        task_description: 'Emit a realtime marker.',
      },
      {
        ...createSubtask('e2e-realtime-agent-2', 'running'),
        role: 'realtime-agent-2',
        task_description: 'Emit a second realtime marker.',
      },
    ],
  });

  await useShadowCloneStore.getState().selectSubtask('run-v2', 'e2e-realtime-agent-1');

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_plan_created',
    {
      agent_run_id: 'run-v2',
      subtasks: [
        {
          id: 'e2e-realtime-agent-1',
          role: 'realtime-agent-1',
          task_description: 'Emit a realtime marker.',
          status: 'running',
        },
        {
          id: 'e2e-realtime-agent-2',
          role: 'realtime-agent-2',
          task_description: 'Emit a second realtime marker.',
          status: 'running',
        },
      ],
    },
    createLiveActivity('execution', 'shadow_clone_v2_plan_created'),
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.activeSubtaskId, 'e2e-realtime-agent-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
});

test('repeated Claude SDK Agent tool deltas do not downgrade a running subagent to pending', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    subtasks: [
      {
        ...createSubtask('toolu-claude-1', 'running'),
        role: 'teammate-1',
        task_description: 'Initial task',
      },
    ],
    pendingCount: 0,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'toolu-claude-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'Refined streamed Agent prompt.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(state.subtasks[0]?.task_description, 'Refined streamed Agent prompt.');
  assert.equal(state.pendingCount, 0);
});

test('later empty Claude SDK Agent deltas do not erase resolved teammate labels', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    subtasks: [
      {
        ...createSubtask('toolu-claude-1', 'running'),
        role: 'teammate-1',
        task_description: 'Resolved streamed Agent prompt.',
      },
    ],
    pendingCount: 0,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'toolu-claude-1',
      source: 'claude_sdk',
      role: 'teammate',
      task_description: '',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(state.subtasks[0]?.role, 'teammate-1');
  assert.equal(state.subtasks[0]?.task_description, 'Resolved streamed Agent prompt.');
});

test('legacy AgentScope subagent_started still keeps new workers pending until activity arrives', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'idle',
    pendingCount: 0,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'agentscope-subtask-1',
      role: 'researcher',
      task_description: 'Research task.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'pending');
  assert.equal(state.pendingCount, 1);
});

test('Shadow Clone V2 subagent lifecycle events update visible teammate status', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    pendingCount: 1,
    subtasks: [
      {
        ...createSubtask('task-1', 'pending'),
        role: 'analyst',
        task_description: 'Execute assigned task.',
      },
    ],
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'task-1',
      source: 'shadow_clone_v2',
      role: 'analyst',
      task_description: 'Execute assigned task.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-v2',
  );

  let state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(state.pendingCount, 0);

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_completed',
    {
      subtask_id: 'task-1',
      source: 'shadow_clone_v2',
      result_summary: 'Task completed.',
    },
    createLiveActivity('execution', 'subagent_completed'),
    'run-v2',
  );

  state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'completed');
  assert.equal(state.subtasks[0]?.result_summary, 'Task completed.');
});

test('Shadow Clone V2 subagent activity populates teammate inspection transcript', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    pendingCount: 0,
    subtasks: [
      {
        ...createSubtask('task-1', 'running'),
        role: 'analyst',
        task_description: 'Execute assigned task.',
      },
    ],
    activeSubtaskId: 'task-1',
    viewScope: 'shadow_clone_subagent',
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'task-1',
      sequence: 3,
      source: 'shadow_clone_v2',
      message_type: 'assistant',
      content: {
        role: 'assistant',
        content: 'Subagent final result.',
      },
      metadata: {
        stream_status: 'complete',
        source: 'shadow_clone_v2',
      },
      created_at: '2026-05-31T12:05:00+00:00',
      updated_at: '2026-05-31T12:05:00+00:00',
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(state.activeSubtaskId, 'task-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(
    state.subtaskTranscriptStates['task-1']?.messages[0]?.type,
    'assistant',
  );
  assert.match(
    state.subtaskTranscriptStates['task-1']?.messages[0]?.content || '',
    /Subagent final result\./,
  );
  assert.equal(
    state.subtaskPanelStates['task-1']?.latestLabel,
    '已生成阶段结论',
  );
});

test('Shadow Clone V2 subagent activity exposes agent name on monitor row before projection sync', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    subtasks: [],
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'realtime-task-1',
      sequence: 86,
      source: 'shadow_clone_v2',
      agent_name: 'realtime-agent-1',
      message_type: 'assistant',
      content: {
        role: 'assistant',
        content: 'SUB_STREAM_MARKER_realtime_agent_1_123',
      },
      metadata: {
        stream_status: 'chunk',
        source: 'shadow_clone_v2',
      },
      created_at: '2026-05-31T12:05:00+00:00',
      updated_at: '2026-05-31T12:05:00+00:00',
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  const subtask = state.subtasks.find((item) => item.id === 'realtime-task-1');
  assert.equal(subtask?.role, 'realtime-agent-1');
  assert.equal(subtask?.status, 'running');
});

test('Shadow Clone V2 subagent incremental chunks accumulate in selected transcript before completion', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    pendingCount: 0,
    subtasks: [
      {
        ...createSubtask('realtime-task-1', 'running'),
        role: 'realtime-agent-1',
        task_description: 'Stream a marker, then sleep.',
      },
    ],
    activeSubtaskId: 'realtime-task-1',
    viewScope: 'shadow_clone_subagent',
  });

  const chunks = [
    'SUB_STREAM',
    '_MARKER',
    '_realtime',
    '_agent_1',
    '_1780598331722',
  ];

  for (const [index, chunk] of chunks.entries()) {
    useShadowCloneStore.getState().handleSSEEvent(
      'subagent_activity',
      {
        subtask_id: 'realtime-task-1',
        sequence: index + 10,
        source: 'shadow_clone_v2',
        agent_name: 'realtime-agent-1',
        message_type: 'assistant',
        content: {
          role: 'assistant',
          content: chunk,
        },
        metadata: {
          stream_status: 'chunk',
          source: 'shadow_clone_v2',
        },
        created_at: '2026-05-31T12:05:00+00:00',
        updated_at: '2026-05-31T12:05:00+00:00',
      },
      createLiveActivity('execution', 'subagent_activity'),
      'run-v2',
    );
  }

  const state = useShadowCloneStore.getState();
  assert.equal(state.activeSubtaskId, 'realtime-task-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(state.subtasks[0]?.status, 'running');
  assert.equal(
    state.subtaskTranscriptStates['realtime-task-1']?.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780598331722',
  );
  assert.equal(
    state.subtaskTranscriptStates['realtime-task-1']?.messages.length,
    0,
  );
});

test('Shadow Clone V2 selected transcript keeps lower-sequence assistant chunk after higher-sequence tool progress', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
    pendingCount: 0,
    subtasks: [
      {
        ...createSubtask('realtime-e2e-task-1', 'running'),
        role: 'realtime-agent-1',
        task_description: 'Stream marker and write file.',
      },
    ],
    activeSubtaskId: 'realtime-e2e-task-1',
    viewScope: 'shadow_clone_subagent',
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'realtime-e2e-task-1',
      sequence: 250,
      source: 'shadow_clone_v2',
      agent_name: 'realtime-agent-1',
      message_type: 'assistant',
      content: {
        role: 'assistant',
        tool_calls: [
          {
            id: 'write-file-call',
            index: 0,
            function: {
              name: 'write_file',
              arguments: JSON.stringify({ path: '/workspace/realtime_e2e_agent1.md' }),
            },
          },
        ],
      },
      metadata: {
        stream_status: 'tool_call_chunk',
        source: 'shadow_clone_v2',
      },
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-v2',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'realtime-e2e-task-1',
      sequence: 125,
      source: 'shadow_clone_v2',
      agent_name: 'realtime-agent-1',
      message_type: 'assistant',
      content: {
        role: 'assistant',
        content: 'SUB_STREAM_MARKER_realtime_agent_1_1780605457772',
      },
      metadata: {
        stream_status: 'chunk',
        source: 'shadow_clone_v2',
      },
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.activeSubtaskId, 'realtime-e2e-task-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(
    state.subtaskTranscriptStates['realtime-e2e-task-1']?.streamingTextContent,
    'SUB_STREAM_MARKER_realtime_agent_1_1780605457772',
  );
});

test('duplicate reasoning chunks keep transcript content stable while advancing sequence watermark', () => {
  const liveActivity = createLiveActivity('planning', 'planning_started');
  const createReasoningMessage = (messageId, sequence) => ({
    type: 'assistant',
    role: 'assistant',
    message_id: messageId,
    thread_id: 'thread-1',
    sequence,
    is_llm_message: true,
    content: JSON.stringify({
      reasoning_content: 'Drafting the execution plan',
    }),
    metadata: JSON.stringify({
      stream_status: 'reasoning_chunk',
    }),
    created_at: '2026-04-07T00:00:00.000Z',
    updated_at: '2026-04-07T00:00:00.000Z',
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'planning',
    liveActivity,
  });

  useShadowCloneStore.getState().applyMainTranscriptMessage(
    createReasoningMessage('reasoning-1', 1),
    'run-1',
    liveActivity,
  );

  const firstTranscriptState = useShadowCloneStore.getState().mainTranscriptState;

  useShadowCloneStore.getState().applyMainTranscriptMessage(
    createReasoningMessage('reasoning-2', 2),
    'run-1',
    liveActivity,
  );

  const secondTranscriptState = useShadowCloneStore.getState().mainTranscriptState;
  assert.notEqual(secondTranscriptState, firstTranscriptState);
  assert.equal(
    secondTranscriptState.streamingReasoningContent,
    firstTranscriptState.streamingReasoningContent,
  );
  assert.equal(secondTranscriptState.latestLabel, firstTranscriptState.latestLabel);
  assert.equal(secondTranscriptState.lastSequence, 2);
});

test('duplicate tool-call chunks keep semantic panel state stable while advancing sequence watermark', () => {
  const createToolCallActivity = (sequence, updatedAt = '2026-04-07T00:00:00.000Z') => ({
    subtask_id: 'subtask-1',
    sequence,
    message_type: 'assistant',
    content: {
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
    },
    metadata: {
      stream_status: 'tool_call_chunk',
    },
    created_at: '2026-04-07T00:00:00.000Z',
    updated_at: updatedAt,
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    subtasks: [createSubtask('subtask-1', 'running')],
    liveActivity: createLiveActivity('execution', 'subagent_activity'),
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    createToolCallActivity(1, '2026-04-07T00:00:00.000Z'),
    undefined,
    'run-1',
  );

  const firstState = useShadowCloneStore.getState();
  const firstPanelState = firstState.subtaskPanelStates['subtask-1'];
  const firstToolCalls = firstPanelState.toolCalls;
  const firstTranscriptState = firstState.subtaskTranscriptStates['subtask-1'];

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    createToolCallActivity(2, '2026-04-07T00:00:01.000Z'),
    undefined,
    'run-1',
  );

  const secondState = useShadowCloneStore.getState();
  const secondPanelState = secondState.subtaskPanelStates['subtask-1'];
  const secondTranscriptState = secondState.subtaskTranscriptStates['subtask-1'];
  assert.notEqual(secondPanelState, firstPanelState);
  assert.equal(secondPanelState.toolCalls, firstToolCalls);
  assert.equal(secondPanelState.streamingText, firstPanelState.streamingText);
  assert.equal(secondPanelState.latestLabel, firstPanelState.latestLabel);
  assert.equal(secondPanelState.lastSequence, 2);
  assert.notEqual(secondTranscriptState, firstTranscriptState);
  assert.deepEqual(
    secondTranscriptState.streamingToolCall,
    firstTranscriptState.streamingToolCall,
  );
  assert.equal(secondTranscriptState.latestLabel, firstTranscriptState.latestLabel);
  assert.equal(secondTranscriptState.lastSequence, 2);

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      ...createToolCallActivity(1, '2026-04-07T00:00:02.000Z'),
      content: {
        tool_calls: [
          {
            id: 'tool-call-1',
            index: 0,
            function: {
              name: 'write_file',
              arguments: JSON.stringify({
                file_path: '/workspace/report.md',
                file_contents: 'older payload should be ignored',
              }),
            },
          },
        ],
      },
    },
    undefined,
    'run-1',
  );

  const thirdPanelState = useShadowCloneStore.getState().subtaskPanelStates['subtask-1'];
  assert.equal(thirdPanelState, secondPanelState);
  assert.equal(thirdPanelState.toolCalls[0]?.assistantCall.content, firstToolCalls[0]?.assistantCall.content);
});

test('authoritative sync prunes stale subtasks and clears stale inspection ownership', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'completed',
    live_activity: null,
    proposal: null,
    subagents: {},
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    activeSubtaskId: 'subtask-stale',
    viewScope: 'shadow_clone_subagent',
    subtasks: [createSubtask('subtask-stale', 'pending')],
    pendingCount: 1,
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'completed');
  assert.equal(state.subtasks.length, 0);
  assert.equal(state.pendingCount, 0);
  assert.equal(state.activeSubtaskId, null);
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.equal(state.liveActivity, null);
});

test('canonical main_agent_continuation status drives aggregating main-agent live activity', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'running',
    ui_phase: 'main_agent_continuation',
    activity_owner: 'main_agent',
    phase_reason: 'waiting_for_final_summary',
    live_activity: {
      scope: 'shadow_clone_main',
      phase: 'execution',
      reason: 'legacy_execution',
    },
    proposal: null,
    subagents: {
      'subtask-1': {
        role: 'research',
        status: 'completed',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [
      {
        subtask_id: 'subtask-1',
        role: 'research',
        status: 'completed',
        summary: 'done',
        submitted_at: '2026-04-02T00:00:00Z',
      },
    ],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'aggregating');
  assert.deepEqual(state.liveActivity, {
    scope: 'main_agent',
    phase: 'aggregate',
    reason: 'waiting_for_final_summary',
    subtask_id: null,
    epoch: undefined,
    updated_at: null,
  });
});

test('canonical terminal status clears stale live activity even when legacy status still looks active', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'running',
    ui_phase: 'completed',
    activity_owner: 'none',
    phase_reason: 'final_delivery_sent',
    live_activity: {
      scope: 'shadow_clone_main',
      phase: 'execution',
      reason: 'legacy_execution',
    },
    proposal: null,
    subagents: {},
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'completed');
  assert.equal(state.liveActivity, null);
});

test('canonical cancelled status stays cancelled and preserves resolved subtasks', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'cancelled',
    ui_phase: 'cancelled',
    activity_owner: 'none',
    phase_reason: 'user_stop_requested',
    live_activity: {
      scope: 'shadow_clone_main',
      phase: 'execution',
      reason: 'legacy_execution',
    },
    proposal: null,
    subagents: {
      'subtask-1': {
        role: 'research',
        status: 'completed',
        result_summary: 'draft saved',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [
      {
        subtask_id: 'subtask-1',
        role: 'research',
        status: 'completed',
        summary: 'draft saved',
        submitted_at: '2026-04-02T00:00:00Z',
      },
    ],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'cancelled');
  assert.equal(state.liveActivity, null);
  assert.equal(state.subtasks.length, 1);
  assert.equal(state.subtasks[0]?.status, 'completed');
  assert.equal(state.subtasks[0]?.result_summary, 'draft saved');
});

test('legacy timeout status falls back to timeout instead of generic error', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'timeout',
    proposal: null,
    subagents: {
      'subtask-1': {
        role: 'research',
        status: 'completed',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'timeout');
  assert.equal(state.liveActivity, null);
});

test('late subagent events do not reopen a cancelled run', () => {
  resetStore({
    currentRunId: 'run-1',
    phase: 'cancelled',
    subtasks: [createSubtask('subtask-1', 'pending')],
    pendingCount: 1,
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'subtask-1',
      role: 'research',
    },
    undefined,
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'cancelled');
});

test('late activity does not reopen terminal subtask statuses restored from authoritative sync', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'cancelled',
    ui_phase: 'cancelled',
    activity_owner: 'none',
    proposal: null,
    subagents: {
      'subtask-cancelled': {
        role: 'research',
        status: 'cancelled',
      },
      'subtask-timeout': {
        role: 'analysis',
        status: 'timeout',
      },
      'subtask-denied': {
        role: 'synthesis',
        status: 'denied',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [
      {
        subtask_id: 'subtask-cancelled',
        role: 'research',
        status: 'cancelled',
        summary: 'stopped before final answer',
        submitted_at: '2026-04-02T00:00:00Z',
      },
      {
        subtask_id: 'subtask-timeout',
        role: 'analysis',
        status: 'timeout',
        summary: 'timed out before final answer',
        submitted_at: '2026-04-02T00:00:00Z',
      },
      {
        subtask_id: 'subtask-denied',
        role: 'synthesis',
        status: 'denied',
        summary: 'skipped after planner denial',
        submitted_at: '2026-04-02T00:00:00Z',
      },
    ],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  for (const subtaskId of [
    'subtask-cancelled',
    'subtask-timeout',
    'subtask-denied',
  ]) {
    useShadowCloneStore.getState().handleSSEEvent(
      'subagent_activity',
      {
        subtask_id: subtaskId,
        sequence: 1,
        message_type: 'assistant',
        content: {
          content: 'late activity should be ignored for terminal subtasks',
        },
        metadata: {
          stream_status: 'chunk',
        },
      },
      undefined,
      'run-1',
    );
  }

  const state = useShadowCloneStore.getState();
  assert.deepEqual(
    state.subtasks.map((subtask) => [subtask.id, subtask.status]),
    [
      ['subtask-cancelled', 'cancelled'],
      ['subtask-timeout', 'timeout'],
      ['subtask-denied', 'denied'],
    ],
  );
});

test('legacy status payloads still fall back to pre-canonical inference', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'aggregating',
    proposal: null,
    subagents: {
      'subtask-1': {
        role: 'research',
        status: 'completed',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    liveActivity: null,
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'aggregating');
  assert.deepEqual(state.liveActivity, {
    scope: 'main_agent',
    phase: 'aggregate',
    reason: 'status_aggregating',
    subtask_id: null,
    epoch: undefined,
    updated_at: null,
  });
});

test('syncRunData reuses an in-flight bootstrap sync for the same run', async () => {
  const statusDeferred = createDeferred();
  const resultsDeferred = createDeferred();
  let statusCalls = 0;
  let resultsCalls = 0;

  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => {
    statusCalls += 1;
    return statusDeferred.promise;
  };
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => {
    resultsCalls += 1;
    return resultsDeferred.promise;
  };

  const firstSync = useShadowCloneStore.getState().syncRunData('run-1');
  const secondSync = useShadowCloneStore.getState().syncRunData('run-1');

  await Promise.resolve();

  assert.equal(statusCalls, 1);
  assert.equal(resultsCalls, 1);

  statusDeferred.resolve({
    status: 'completed',
    ui_phase: 'completed',
    activity_owner: 'none',
    proposal: null,
    subagents: {},
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  resultsDeferred.resolve({
    results: [],
  });

  await Promise.all([firstSync, secondSync]);

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-1');
  assert.equal(state.phase, 'completed');
  assert.equal(state.isSyncing, false);
});

test('Claude SDK subagent mapped events populate existing shadow clone monitor state', async () => {
  resetStore({ mode: 'on' });
  const { resolveClaudeSDKSubagentRoute } = await import('../claude-sdk-subagent-routing.ts');

  const started = resolveClaudeSDKSubagentRoute({
    messageType: 'assistant',
    sequence: 1,
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
              prompt: 'Check the file.',
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

  useShadowCloneStore.getState().handleSSEEvent(
    started.streamStatus,
    started.content,
    started.liveActivity,
    'run-1',
  );

  const activity = resolveClaudeSDKSubagentRoute({
    messageType: 'assistant',
    sequence: 2,
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

  useShadowCloneStore.getState().handleSSEEvent(
    activity.streamStatus,
    activity.content,
    activity.liveActivity,
    'run-1',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'running');
  assert.equal(state.liveActivity?.scope, 'shadow_clone_main');
  assert.equal(state.subtasks.length, 1);
  assert.equal(state.subtasks[0].id, 'toolu-1');
  assert.equal(state.subtasks[0].role, 'teammate-1');
  assert.equal(state.subtasks[0].task_description, 'Check the file.');
  assert.equal(state.subtasks[0].status, 'running');
  assert.equal(
    state.subtaskTranscriptStates['toolu-1'].streamingTextContent,
    'FULL_ALPHA_OK',
  );
});

test('selecting a Claude SDK subagent keeps local transcript when authoritative status is not a shadow clone run', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'not_found',
    proposal: null,
    subagents: {},
    recovery: { pending: false },
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });
  let statusCalls = 0;
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => {
    statusCalls += 1;
    return {
      status: 'not_found',
      proposal: null,
      subagents: {},
      recovery: { pending: false },
    };
  };

  resetStore({ mode: 'on' });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'toolu-claude-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'Inspect the sandbox.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'toolu-claude-1',
      source: 'claude_sdk',
      sequence: 2,
      message_type: 'assistant',
      content: {
        content: 'SUB_UI_OK',
      },
      metadata: {
        stream_status: 'chunk',
      },
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-1',
  );

  await useShadowCloneStore.getState().selectSubtask('run-1', 'toolu-claude-1');
  await Promise.resolve();
  await Promise.resolve();

  const state = useShadowCloneStore.getState();
  assert.equal(statusCalls, 0);
  assert.equal(state.activeSubtaskId, 'toolu-claude-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(state.subtasks.length, 1);
  assert.equal(state.subtasks[0].id, 'toolu-claude-1');
  assert.equal(state.subtasks[0].status, 'running');
  assert.equal(
    state.subtaskTranscriptStates['toolu-claude-1'].streamingTextContent,
    'SUB_UI_OK',
  );
});

test('selected Claude SDK subagent exposes assistant chat and child tool activity separately', () => {
  resetStore({ mode: 'on', currentRunId: 'run-1' });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'Create a file and report the token.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      sequence: 2,
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
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-1',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      sequence: 3,
      message_type: 'assistant',
      content: {
        role: 'assistant',
        content: 'SUB_UI_OK',
      },
      metadata: {
        stream_status: 'chunk',
        activity_owner: 'claude_sdk_subagent',
        parent_tool_use_id: 'agent-call-1',
      },
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-1',
  );

  useShadowCloneStore.getState().setActiveSubtask('agent-call-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(
    state.subtaskTranscriptStates['agent-call-1'].streamingTextContent,
    'SUB_UI_OK',
  );
  assert.equal(state.subtaskPanelStates['agent-call-1'].toolCalls.length, 1);
  assert.equal(
    state.subtaskPanelStates['agent-call-1'].toolCalls[0].assistantCall.name,
    'write',
  );
  assert.match(
    state.subtaskPanelStates['agent-call-1'].toolCalls[0].assistantCall.content || '',
    /subagent\.md/,
  );
  assert.match(state.subtaskPanelStates['agent-call-1'].streamingText, /SUB_UI_OK/);
});

test('Claude SDK local subagent ledger survives empty authoritative sync and return to main view', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'not_found',
    proposal: null,
    subagents: {},
    recovery: { pending: false },
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({ mode: 'on', currentRunId: 'run-1' });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'Return the token.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      sequence: 1,
      message_type: 'assistant',
      content: {
        role: 'assistant',
        content: 'SUB_LEDGER_OK',
      },
      metadata: {
        stream_status: 'chunk',
        activity_owner: 'claude_sdk_subagent',
        parent_tool_use_id: 'agent-call-1',
      },
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-1',
  );

  await useShadowCloneStore.getState().selectSubtask('run-1', 'agent-call-1');
  useShadowCloneStore.getState().returnToMainView();
  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.activeSubtaskId, null);
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.equal(state.subtasks.length, 1);
  assert.equal(state.subtasks[0].id, 'agent-call-1');
  assert.equal(state.subtasks[0].role, 'teammate-1');
  assert.equal(
    state.subtaskTranscriptStates['agent-call-1'].streamingTextContent,
    'SUB_LEDGER_OK',
  );
});

test('Claude SDK partial authoritative sync preserves earlier local subagents while creation is still running', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'running',
    ui_phase: 'subagents_running',
    activity_owner: 'shadow_clone',
    proposal: null,
    subagents: {
      'agent-call-2': {
        role: 'teammate-2',
        status: 'running',
        task_description: 'Second task.',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({ mode: 'on', currentRunId: 'run-1', phase: 'running' });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'First task.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );
  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_activity',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      sequence: 1,
      message_type: 'assistant',
      content: {
        role: 'assistant',
        content: 'FIRST_LOCAL_OK',
      },
      metadata: {
        stream_status: 'chunk',
        activity_owner: 'claude_sdk_subagent',
        parent_tool_use_id: 'agent-call-1',
      },
    },
    createLiveActivity('execution', 'subagent_activity'),
    'run-1',
  );

  await useShadowCloneStore.getState().selectSubtask('run-1', 'agent-call-1');
  useShadowCloneStore.getState().returnToMainView();
  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.deepEqual(
    state.subtasks.map((subtask) => subtask.id).sort(),
    ['agent-call-1', 'agent-call-2'],
  );
  assert.equal(state.subtasks.find((subtask) => subtask.id === 'agent-call-1')?.role, 'teammate-1');
  assert.equal(
    state.subtaskTranscriptStates['agent-call-1'].streamingTextContent,
    'FIRST_LOCAL_OK',
  );
});

test('Claude SDK local subagent ledger survives empty completed sync for post-run review', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'completed',
    ui_phase: 'completed',
    activity_owner: 'none',
    proposal: null,
    subagents: {},
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({ mode: 'on', currentRunId: 'run-1', phase: 'running' });

  useShadowCloneStore.getState().handleSSEEvent(
    'subagent_started',
    {
      subtask_id: 'agent-call-1',
      source: 'claude_sdk',
      role: 'teammate-1',
      task_description: 'Reviewable completed task.',
    },
    createLiveActivity('execution', 'subagent_started'),
    'run-1',
  );

  await useShadowCloneStore.getState().selectSubtask('run-1', 'agent-call-1');
  useShadowCloneStore.getState().returnToMainView();
  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'completed');
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.equal(state.subtasks.length, 1);
  assert.equal(state.subtasks[0].id, 'agent-call-1');
  assert.equal(state.subtasks[0].role, 'teammate-1');
});

test('teammate inspection survives partial syncs while the selected subtask still exists', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'running',
    ui_phase: 'subagents_running',
    activity_owner: 'shadow_clone',
    proposal: null,
    subagents: {
      'toolu-claude-1': {
        role: 'teammate-1',
        status: 'running',
        task_description: 'Inspect the sandbox.',
      },
    },
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({
    mode: 'on',
    currentRunId: 'run-1',
    phase: 'running',
  });

  useShadowCloneStore.setState({
    activeSubtaskId: 'toolu-claude-1',
    viewScope: 'shadow_clone_subagent',
  });

  await useShadowCloneStore.getState().syncRunData('run-1');

  const state = useShadowCloneStore.getState();
  assert.equal(state.activeSubtaskId, 'toolu-claude-1');
  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(state.subtasks[0]?.id, 'toolu-claude-1');
});

test('authoritative sync still clears stale inspection ownership when the subtask is truly gone', async () => {
  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'completed',
    proposal: null,
    subagents: {},
    recovery: { pending: false },
    environment: {
      status: 'ready',
      ready: true,
      manifest: {
        confirmation_ready: true,
      },
    },
    sandbox: {},
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [],
  });

  resetStore({
    currentRunId: 'run-1',
    phase: 'running',
    activeSubtaskId: 'subtask-stale',
    viewScope: 'shadow_clone_subagent',
    subtasks: [createSubtask('subtask-stale', 'pending')],
    pendingCount: 1,
    liveActivity: createLiveActivity(),
  });

  await useShadowCloneStore.getState().syncRunData('run-1', { force: true });

  const state = useShadowCloneStore.getState();
  assert.equal(state.phase, 'completed');
  assert.equal(state.subtasks.length, 0);
  assert.equal(state.pendingCount, 0);
  assert.equal(state.activeSubtaskId, null);
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.equal(state.liveActivity, null);
});

test('Shadow Clone V2 projection maps canonical tasks and idle agents into monitor rows', () => {
  resetStore();

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      projection: {
        status: 'running',
        mode: 'v2',
        ui_phase: 'subagents_running',
        team: {
          team_id: 'team-1',
          members: {
            researcher: {
              agent_id: 'researcher@thread-1',
              agent_name: 'researcher',
              role: 'Research analyst',
              status: 'working',
              current_task_ids: ['task-1'],
            },
            reviewer: {
              agent_id: 'reviewer@thread-1',
              agent_name: 'reviewer',
              role: 'Quality reviewer',
              status: 'idle',
              idle_since: '2026-06-03T00:10:00+00:00',
              idle_expires_at: '2026-06-03T00:30:00+00:00',
            },
          },
        },
        tasks: {
          'task-1': {
            id: 'task-1',
            role: 'Research market',
            description: 'Gather evidence from the source memo.',
            status: 'in_progress',
            agent_name: 'researcher',
          },
        },
        agents: {
          researcher: {
            agent_id: 'researcher@thread-1',
            agent_name: 'researcher',
            role: 'Research analyst',
            status: 'working',
            current_task_ids: ['task-1'],
          },
          reviewer: {
            agent_id: 'reviewer@thread-1',
            agent_name: 'reviewer',
            role: 'Quality reviewer',
            status: 'idle',
            idle_since: '2026-06-03T00:10:00+00:00',
            idle_expires_at: '2026-06-03T00:30:00+00:00',
          },
        },
        messages: {
          'msg-1': {
            message_id: 'msg-1',
            sender: 'facilitator@thread-1',
            recipient: 'researcher',
            status: 'unread',
            summary: 'Please continue research.',
            text: 'Read the inbox-only token INBOX_ONLY_RESEARCH_TOKEN and apply it.',
            payload: { source: 'mailbox-only' },
          },
        },
        model: {
          requested: 'frontend-selected-model',
          effective: 'frontend-selected-model',
        },
        final_output: {
          content: 'Final synthesized answer from the main agent.',
          created_at: '2026-06-03T00:40:00+00:00',
        },
        live_activity: {
          scope: 'shadow_clone_main',
          phase: 'execution',
          reason: 'shadow_clone_v2_event_projection',
        },
      },
    },
    undefined,
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-v2');
  assert.equal(state.phase, 'running');
  assert.equal(state.pendingCount, 0);
  assert.equal(
    JSON.parse(state.mainTranscriptState.messages[0].content).content,
    'Final synthesized answer from the main agent.',
  );
  const projectedMessage = state.subtaskTranscriptStates['task-1']?.messages[0];
  assert.ok(projectedMessage);
  assert.equal(
    JSON.parse(projectedMessage.content).content,
    'Read the inbox-only token INBOX_ONLY_RESEARCH_TOKEN and apply it.',
  );
  assert.deepEqual(
    JSON.parse(projectedMessage.metadata),
    {
      stream_status: 'complete',
      shadow_clone_subtask_id: 'task-1',
      shadow_clone_v2_projection_message: true,
      mailbox_message_id: 'msg-1',
      sender: 'facilitator@thread-1',
      recipient: 'researcher',
      status: 'unread',
      summary: 'Please continue research.',
      text: 'Read the inbox-only token INBOX_ONLY_RESEARCH_TOKEN and apply it.',
    },
  );
  assert.deepEqual(
    state.subtasks.map((subtask) => ({
      id: subtask.id,
      role: subtask.role,
      task_description: subtask.task_description,
      status: subtask.status,
      agent_name: subtask.agent_name,
      agent_status: subtask.agent_status,
      idle_expires_at: subtask.idle_expires_at,
    })),
    [
      {
        id: 'task-1',
        role: 'Research market',
        task_description: 'Gather evidence from the source memo.',
        status: 'running',
        agent_name: 'researcher',
        agent_status: 'working',
        idle_expires_at: undefined,
      },
      {
        id: 'agent:reviewer',
        role: 'Quality reviewer',
        task_description: 'Idle teammate reviewer',
        status: 'completed',
        agent_name: 'reviewer',
        agent_status: 'idle',
        idle_expires_at: '2026-06-03T00:30:00+00:00',
      },
    ],
  );
});

test('Shadow Clone V2 projection restores durable subagent transcript output', () => {
  resetStore({
    currentRunId: 'run-v2',
    phase: 'running',
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        total: 1,
        completed: 1,
        tasks: {
          'gpu-story-1': {
            id: 'gpu-story-1',
            subject: 'Write GPU story',
            description: 'Write markdown.',
            status: 'completed',
            agent_name: 'storyteller',
          },
        },
        agents: {
          storyteller: {
            agent_id: 'storyteller@thread-1',
            agent_name: 'storyteller',
            role: 'GPU storyteller',
            status: 'idle',
            current_task_ids: ['gpu-story-1'],
          },
        },
        transcripts: [
          {
            message_id: 'task-completed:gpu-story-1:4',
            task_id: 'gpu-story-1',
            agent_name: 'storyteller',
            role: 'assistant',
            content: 'TEAM_MEMBER_GPU_STORY_MARKER raw markdown content',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-02T00:04:00+00:00',
          },
        ],
      },
    },
    25,
    'run-v2',
  );

  const projectedMessage =
    useShadowCloneStore.getState().subtaskTranscriptStates['gpu-story-1']
      ?.messages[0];

  assert.ok(projectedMessage);
  assert.equal(
    JSON.parse(projectedMessage.content).content,
    'storyteller\n\nTEAM_MEMBER_GPU_STORY_MARKER raw markdown content',
  );
  assert.deepEqual(JSON.parse(projectedMessage.metadata), {
    stream_status: 'complete',
    shadow_clone_subtask_id: 'gpu-story-1',
    shadow_clone_v2_projection_transcript: true,
    v2_transcript_message_id: 'task-completed:gpu-story-1:4',
    agent_name: 'storyteller',
    source_event_type: 'task_completed',
  });
});

test('Shadow Clone V2 projection attaches write_file artifacts to subagent natural-language transcript messages', () => {
  resetStore({ currentRunId: 'run-v2', phase: 'running' });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        tasks: {
          'writer-task-1': {
            id: 'writer-task-1',
            subject: 'Write delivery',
            description: 'Create markdown.',
            status: 'completed',
            agent_name: 'writer-agent-1',
            tool_call_ids: ['write-tool-1'],
          },
        },
        agents: {
          'writer-agent-1': {
            agent_name: 'writer-agent-1',
            role: 'Writer',
            status: 'idle',
            tool_call_ids: ['write-tool-1'],
          },
        },
        team: { members: {} },
        tool_calls: {
          'write-tool-1': {
            tool_call_id: 'write-tool-1',
            tool_name: 'write_file',
            task_id: 'writer-task-1',
            agent_name: 'writer-agent-1',
            status: 'completed',
            arguments_summary: {
              path: '/workspace/writer_agent_1_delivery.md',
            },
            result_summary: 'wrote /workspace/writer_agent_1_delivery.md',
            completed_at: '2026-06-04T01:00:02+00:00',
          },
        },
        transcripts: [
          {
            message_id: 'task-completed:writer-task-1:4',
            task_id: 'writer-task-1',
            agent_name: 'writer-agent-1',
            role: 'assistant',
            content:
              'SUBAGENT_NATURAL_LANGUAGE_OK_writer-agent-1 delivered the markdown file.',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-04T01:00:03+00:00',
          },
        ],
      },
    },
    50,
    'run-v2',
  );

  const message = useShadowCloneStore
    .getState()
    .subtaskTranscriptStates['writer-task-1']?.messages.find((item) => {
      const parsed = JSON.parse(item.content);
      return String(parsed.content || '').includes(
        'SUBAGENT_NATURAL_LANGUAGE_OK_writer-agent-1',
      );
    });

  assert.ok(message, 'expected real subagent natural-language transcript message');
  assert.equal(message.is_llm_message, true);
  assert.deepEqual(JSON.parse(message.metadata).shadow_clone_file_artifacts, [
    {
      path: '/workspace/writer_agent_1_delivery.md',
      tool_call_id: 'write-tool-1',
      tool_name: 'write_file',
      task_id: 'writer-task-1',
      agent_name: 'writer-agent-1',
    },
  ]);
});

test('Shadow Clone V2 projection carries prior same-agent transcript into follow-up subtask panel', () => {
  resetStore({ currentRunId: 'run-v2-first', phase: 'completed' });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        agent_run_id: 'run-v2-first',
        tasks: {
          'draft-task': {
            id: 'draft-task',
            subject: 'Draft delivery',
            description: 'Create initial delivery.',
            status: 'completed',
            agent_name: 'teammate-1',
          },
        },
        agents: {
          'teammate-1': {
            agent_id: 'teammate-1@thread-1',
            agent_name: 'teammate-1',
            role: 'Writer',
            status: 'idle',
            current_task_ids: ['draft-task'],
          },
        },
        transcripts: [
          {
            message_id: 'draft-output',
            task_id: 'draft-task',
            agent_name: 'teammate-1',
            content: 'IDLE_DRAFT_V1_UNIQUE_FROM_PRIOR_DELIVERY',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-03T01:00:00+00:00',
          },
        ],
      },
    },
    10,
    'run-v2-first',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        agent_run_id: 'run-v2-second',
        tasks: {
          'follow-up-task': {
            id: 'follow-up-task',
            subject: 'Improve prior delivery',
            description: 'Improve the previous delivery based on feedback.',
            status: 'completed',
            agent_name: 'teammate-1',
          },
        },
        agents: {
          'teammate-1': {
            agent_id: 'teammate-1@thread-1',
            agent_name: 'teammate-1',
            role: 'Writer',
            status: 'idle',
            current_task_ids: ['follow-up-task'],
          },
        },
        transcripts: [
          {
            message_id: 'follow-up-output',
            task_id: 'follow-up-task',
            agent_name: 'teammate-1',
            content: 'IDLE_FEEDBACK_APPLIED_UNIQUE_NEW_DELIVERY',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-03T01:10:00+00:00',
          },
        ],
      },
    },
    20,
    'run-v2-second',
  );

  useShadowCloneStore.getState().setActiveSubtask('follow-up-task');
  const messages = useShadowCloneStore.getState().subtaskTranscriptStates['follow-up-task']?.messages || [];
  const messageTexts = messages.map((message) => JSON.parse(message.content).content);

  assert.deepEqual(messageTexts, [
    'teammate-1\n\nIDLE_DRAFT_V1_UNIQUE_FROM_PRIOR_DELIVERY',
    'teammate-1\n\nIDLE_FEEDBACK_APPLIED_UNIQUE_NEW_DELIVERY',
  ]);
  assert.notEqual(messages[0]?.message_id, messages[1]?.message_id);
});

test('Shadow Clone V2 started reset keeps prior same-agent transcript available for follow-up projection', () => {
  resetStore({ currentRunId: 'run-v2-first', phase: 'completed' });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        agent_run_id: 'run-v2-first',
        tasks: {
          'draft-task': {
            id: 'draft-task',
            subject: 'Draft delivery',
            description: 'Create initial delivery.',
            status: 'completed',
            agent_name: 'teammate-1',
          },
        },
        agents: {
          'teammate-1': {
            agent_id: 'teammate-1@thread-1',
            agent_name: 'teammate-1',
            role: 'Writer',
            status: 'idle',
            current_task_ids: ['draft-task'],
          },
        },
        transcripts: [
          {
            message_id: 'draft-output',
            task_id: 'draft-task',
            agent_name: 'teammate-1',
            content: 'IDLE_DRAFT_V1_SURVIVES_STARTED_RESET',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-03T01:00:00+00:00',
          },
        ],
      },
    },
    10,
    'run-v2-first',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_started',
    { agent_run_id: 'run-v2-second' },
    undefined,
    'run-v2-second',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        agent_run_id: 'run-v2-second',
        tasks: {
          'follow-up-task': {
            id: 'follow-up-task',
            subject: 'Improve prior delivery',
            description: 'Improve the previous delivery based on feedback.',
            status: 'completed',
            agent_name: 'teammate-1',
          },
        },
        agents: {
          'teammate-1': {
            agent_id: 'teammate-1@thread-1',
            agent_name: 'teammate-1',
            role: 'Writer',
            status: 'idle',
            current_task_ids: ['follow-up-task'],
          },
        },
        transcripts: [
          {
            message_id: 'follow-up-output',
            task_id: 'follow-up-task',
            agent_name: 'teammate-1',
            content: 'IDLE_FEEDBACK_APPLIED_AFTER_STARTED_RESET',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-03T01:10:00+00:00',
          },
        ],
      },
    },
    20,
    'run-v2-second',
  );

  useShadowCloneStore.getState().setActiveSubtask('follow-up-task');
  const messages = useShadowCloneStore.getState().subtaskTranscriptStates['follow-up-task']?.messages || [];
  const messageTexts = messages.map((message) => JSON.parse(message.content).content);

  assert.deepEqual(messageTexts, [
    'teammate-1\n\nIDLE_DRAFT_V1_SURVIVES_STARTED_RESET',
    'teammate-1\n\nIDLE_FEEDBACK_APPLIED_AFTER_STARTED_RESET',
  ]);
});

test('Shadow Clone V2 started reset keeps selected prior full result available for follow-up projection', async () => {
  resetStore({ currentRunId: 'run-v2-first', phase: 'completed' });
  globalThis.__shadowCloneApiMock.getShadowCloneFullResult = async () => ({
    result: 'LIVE_CLICK_SUBAGENT_OK\nIDLE_DRAFT_V1_FROM_SELECTED_FULL_RESULT',
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        agent_run_id: 'run-v2-first',
        tasks: {
          'draft-task': {
            id: 'draft-task',
            subject: 'Initial delivery',
            description: 'Create initial delivery.',
            status: 'completed',
            agent_name: 'teammate-1',
          },
        },
        agents: {
          'teammate-1': {
            agent_id: 'teammate-1@thread-1',
            agent_name: 'teammate-1',
            role: 'Writer',
            status: 'idle',
            current_task_ids: ['draft-task'],
          },
        },
      },
    },
    10,
    'run-v2-first',
  );

  await useShadowCloneStore.getState().selectSubtask('run-v2-first', 'draft-task');
  await Promise.resolve();
  await Promise.resolve();

  useShadowCloneStore.getState().returnToMainView();
  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_started',
    { agent_run_id: 'run-v2-second' },
    undefined,
    'run-v2-second',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      projection: {
        status: 'completed',
        mode: 'v2',
        agent_run_id: 'run-v2-second',
        tasks: {
          'follow-up-task': {
            id: 'follow-up-task',
            subject: 'Improve prior delivery',
            description: 'Improve the previous delivery based on feedback.',
            status: 'completed',
            agent_name: 'teammate-1',
          },
        },
        agents: {
          'teammate-1': {
            agent_id: 'teammate-1@thread-1',
            agent_name: 'teammate-1',
            role: 'Writer',
            status: 'idle',
            current_task_ids: ['follow-up-task'],
          },
        },
        transcripts: [
          {
            message_id: 'follow-up-output',
            task_id: 'follow-up-task',
            agent_name: 'teammate-1',
            content: 'IDLE_FEEDBACK_APPLIED_AFTER_SELECTED_FULL_RESULT',
            stream_status: 'complete',
            source_event_type: 'task_completed',
            created_at: '2026-06-03T01:10:00+00:00',
          },
        ],
      },
    },
    20,
    'run-v2-second',
  );

  useShadowCloneStore.getState().setActiveSubtask('follow-up-task');
  const messages = useShadowCloneStore.getState().subtaskTranscriptStates['follow-up-task']?.messages || [];
  const messageTexts = messages.map((message) => JSON.parse(message.content).content).join('\n');

  assert.match(messageTexts, /IDLE_DRAFT_V1_FROM_SELECTED_FULL_RESULT/);
  assert.match(messageTexts, /IDLE_FEEDBACK_APPLIED_AFTER_SELECTED_FULL_RESULT/);
});

test('syncRunData restores completed Shadow Clone V2 monitor rows from status projection on reload', async () => {
  resetStore();

  globalThis.__shadowCloneApiMock.getShadowCloneStatus = async () => ({
    status: 'completed',
    mode: 'v2',
    ui_phase: 'completed',
    total: 2,
    completed: 2,
    team: {
      members: {
        storyteller_1: {
          agent_id: 'storyteller_1@thread-1',
          agent_name: 'storyteller_1',
          role: 'Write GPU story 1',
          status: 'idle',
          current_task_ids: ['gpu_story_1'],
        },
        storyteller_2: {
          agent_id: 'storyteller_2@thread-1',
          agent_name: 'storyteller_2',
          role: 'Write GPU story 2',
          status: 'idle',
          current_task_ids: ['gpu_story_2'],
        },
      },
    },
    tasks: {
      gpu_story_1: {
        id: 'gpu_story_1',
        role: 'Write GPU story 1',
        description: 'Story 1 task',
        status: 'completed',
        agent_name: 'storyteller_1',
      },
      gpu_story_2: {
        id: 'gpu_story_2',
        role: 'Write GPU story 2',
        description: 'Story 2 task',
        status: 'completed',
        agent_name: 'storyteller_2',
      },
    },
    agents: {
      storyteller_1: {
        agent_id: 'storyteller_1@thread-1',
        agent_name: 'storyteller_1',
        role: 'Write GPU story 1',
        status: 'idle',
        current_task_ids: ['gpu_story_1'],
      },
      storyteller_2: {
        agent_id: 'storyteller_2@thread-1',
        agent_name: 'storyteller_2',
        role: 'Write GPU story 2',
        status: 'idle',
        current_task_ids: ['gpu_story_2'],
      },
    },
  });
  globalThis.__shadowCloneApiMock.getShadowCloneResults = async () => ({
    results: [
      {
        subtask_id: 'gpu_story_1',
        role: 'Write GPU story 1',
        status: 'completed',
        summary: 'TEAM_MEMBER_GPU_STORY_1 finished',
      },
      {
        subtask_id: 'gpu_story_2',
        role: 'Write GPU story 2',
        status: 'completed',
        summary: 'TEAM_MEMBER_GPU_STORY_2 finished',
      },
    ],
  });

  await useShadowCloneStore.getState().syncRunData('run-v2', { force: true });

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-v2');
  assert.equal(state.phase, 'completed');
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.deepEqual(
    state.subtasks.map((subtask) => ({
      id: subtask.id,
      agent_name: subtask.agent_name,
      status: subtask.status,
      result_summary: subtask.result_summary,
    })),
    [
      {
        id: 'gpu_story_1',
        agent_name: 'storyteller_1',
        status: 'completed',
        result_summary: 'TEAM_MEMBER_GPU_STORY_1 finished',
      },
      {
        id: 'gpu_story_2',
        agent_name: 'storyteller_2',
        status: 'completed',
        result_summary: 'TEAM_MEMBER_GPU_STORY_2 finished',
      },
    ],
  );
});

test('Shadow Clone V2 projection deduplicates proposal task rows against stable agent identities', () => {
  resetStore();

  const members = {};
  const proposalSubtasks = [];
  for (let index = 1; index <= 5; index += 1) {
    const agentName = `gpu_story_${index}`;
    const taskId = `gpu_story_task_${index}`;
    members[agentName] = {
      agent_id: `${agentName}@thread-1`,
      agent_name: agentName,
      role: `GPU Storyteller ${index}`,
      status: 'idle',
      current_task_ids: [taskId],
    };
    proposalSubtasks.push({
      id: taskId,
      role: `Write GPU story ${index}`,
      task_description: `Write GPU story markdown ${index}`,
      status: 'completed',
    });
  }

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 50,
      agent_run_id: 'run-v2-dedupe',
      projection: {
        status: 'completed',
        mode: 'v2',
        team: { members },
        agents: members,
        proposal: { subtasks: proposalSubtasks },
      },
    },
    undefined,
    'run-v2-dedupe',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.subtasks.length, 5);
  assert.deepEqual(
    state.subtasks.map((subtask) => ({
      id: subtask.id,
      agent_name: subtask.agent_name,
      agent_id: subtask.agent_id,
      agent_status: subtask.agent_status,
    })),
    [1, 2, 3, 4, 5].map((index) => ({
      id: `gpu_story_task_${index}`,
      agent_name: `gpu_story_${index}`,
      agent_id: `gpu_story_${index}@thread-1`,
      agent_status: 'idle',
    })),
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 51,
      agent_run_id: 'run-v2-dedupe',
      projection: {
        status: 'completed',
        mode: 'v2',
        team: { members },
        agents: members,
        proposal: { subtasks: proposalSubtasks },
      },
    },
    undefined,
    'run-v2-dedupe',
  );

  assert.equal(useShadowCloneStore.getState().subtasks.length, 5);
});

test('Shadow Clone V2 projection for a new run resets stale monitor rows and final output', () => {
  resetStore({
    currentRunId: 'run-old',
    phase: 'completed',
    subtasks: [createSubtask('old-task', 'completed')],
    activeSubtaskId: 'old-task',
    viewScope: 'shadow_clone_subagent',
    mainTranscriptState: {
      messages: [
        {
          message_id: 'old-main-output',
          thread_id: 'shadow-clone:main',
          type: 'assistant',
          role: 'assistant',
          is_llm_message: true,
          content: JSON.stringify({
            role: 'assistant',
            content: 'Old run final output must not leak.',
          }),
          metadata: JSON.stringify({
            stream_status: 'complete',
            shadow_clone_subtask_id: 'main',
            shadow_clone_synthetic: true,
          }),
          created_at: '2026-06-03T00:00:00+00:00',
          updated_at: '2026-06-03T00:00:00+00:00',
        },
      ],
      streamingMessageId: null,
      lastSequence: 1,
    },
  });

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      projection: {
        status: 'running',
        mode: 'v2',
        tasks: {},
        agents: {},
        team: { members: {} },
      },
    },
    undefined,
    'run-new',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(state.currentRunId, 'run-new');
  assert.equal(state.phase, 'running');
  assert.deepEqual(state.subtasks, []);
  assert.equal(state.pendingCount, 0);
  assert.equal(state.activeSubtaskId, null);
  assert.equal(state.viewScope, 'shadow_clone_main');
  assert.equal(state.mainTranscriptState.messages.length, 0);
});

test('Shadow Clone V2 late lower-sequence projection keeps previously visible parallel task when snapshot is complete', () => {
  resetStore();

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 200,
      event_cursor: '1001-0',
      projection: {
        status: 'running',
        mode: 'v2',
        tasks: {
          fast: {
            id: 'fast',
            role: 'Fast task',
            description: 'Completed first in append order.',
            status: 'completed',
            agent_name: 'fast-agent',
          },
        },
        agents: {
          'fast-agent': {
            agent_name: 'fast-agent',
            status: 'idle',
          },
        },
        team: { members: {} },
      },
    },
    undefined,
    'run-v2',
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 101,
      event_cursor: '1002-0',
      projection: {
        status: 'running',
        mode: 'v2',
        tasks: {
          fast: {
            id: 'fast',
            role: 'Fast task',
            description: 'Completed first in append order.',
            status: 'completed',
            agent_name: 'fast-agent',
          },
          slow: {
            id: 'slow',
            role: 'Slow task',
            description: 'Arrived later with lower logical sequence.',
            status: 'completed',
            agent_name: 'slow-agent',
          },
        },
        agents: {
          'fast-agent': {
            agent_name: 'fast-agent',
            status: 'idle',
          },
          'slow-agent': {
            agent_name: 'slow-agent',
            status: 'idle',
          },
        },
        team: { members: {} },
      },
    },
    undefined,
    'run-v2',
  );

  const ids = useShadowCloneStore.getState().subtasks.map((subtask) => subtask.id);
  assert.ok(ids.includes('fast'));
  assert.ok(ids.includes('slow'));
});


test('Shadow Clone V2 projection replays durable tool calls into subagent panel state', () => {
  resetStore();

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 42,
      projection: {
        status: 'running',
        mode: 'v2',
        tasks: {
          'task-1': {
            id: 'task-1',
            role: 'Writer',
            description: 'Write a markdown file.',
            status: 'in_progress',
            agent_name: 'writer',
            tool_call_ids: ['tool-started', 'tool-completed', 'tool-failed'],
          },
        },
        agents: {
          writer: {
            agent_name: 'writer',
            role: 'Writer',
            status: 'working',
            tool_call_ids: ['tool-started', 'tool-completed', 'tool-failed'],
          },
        },
        team: { members: {} },
        tool_calls: {
          'tool-started': {
            tool_call_id: 'tool-started',
            tool_name: 'write_file',
            task_id: 'task-1',
            agent_name: 'writer',
            status: 'running',
            arguments_summary: {
              path: '/workspace/story.md',
            },
            arguments_redacted: true,
            arguments_size_bytes: 4096,
            started_at: '2026-06-03T01:00:00+00:00',
          },
          'tool-completed': {
            tool_call_id: 'tool-completed',
            tool_name: 'read_file',
            task_id: 'task-1',
            agent_name: 'writer',
            status: 'completed',
            arguments_summary: 'Read /workspace/story.md',
            result_summary: 'Read 12 lines from /workspace/story.md',
            completed_at: '2026-06-03T01:00:02+00:00',
          },
          'tool-failed': {
            tool_call_id: 'tool-failed',
            tool_name: 'shell_command',
            task_id: 'task-1',
            agent_name: 'writer',
            status: 'failed',
            arguments_summary: {
              command: 'cat missing.md',
            },
            error_type: 'FileNotFoundError',
            error: 'missing.md not found',
            failed_at: '2026-06-03T01:00:03+00:00',
          },
        },
      },
    },
    undefined,
    'run-v2',
  );

  useShadowCloneStore.getState().setActiveSubtask('task-1');

  const state = useShadowCloneStore.getState();
  const panelToolCalls = state.subtaskPanelStates['task-1']?.toolCalls || [];

  assert.equal(state.viewScope, 'shadow_clone_subagent');
  assert.equal(panelToolCalls.length, 3);
  assert.deepEqual(
    panelToolCalls.map((toolCall) => ({
      name: toolCall.assistantCall.name,
      id: toolCall.assistantCall.toolCallId,
      result: toolCall.toolResult?.content,
      success: toolCall.toolResult?.isSuccess,
    })),
    [
      {
        name: 'write-file',
        id: 'tool-started',
        result: 'STREAMING',
        success: true,
      },
      {
        name: 'read-file',
        id: 'tool-completed',
        result: 'Read 12 lines from /workspace/story.md',
        success: true,
      },
      {
        name: 'shell-command',
        id: 'tool-failed',
        result: 'FileNotFoundError: missing.md not found',
        success: false,
      },
    ],
  );
  assert.match(
    panelToolCalls[0]?.assistantCall.content || '',
    /"path": "\/workspace\/story\.md"/,
  );
  assert.match(
    panelToolCalls[0]?.assistantCall.content || '',
    /arguments redacted/i,
  );
  assert.doesNotMatch(
    panelToolCalls[0]?.assistantCall.content || '',
    /file_contents|secret/i,
  );
});

test('Shadow Clone V2 repeated projection prunes stale mailbox messages', () => {
  resetStore();

  const baseProjection = {
    status: 'running',
    mode: 'v2',
    tasks: {
      'task-1': {
        id: 'task-1',
        role: 'Research market',
        description: 'Gather evidence.',
        status: 'in_progress',
        agent_name: 'researcher',
      },
    },
    agents: {
      researcher: {
        agent_id: 'researcher@thread-1',
        agent_name: 'researcher',
        role: 'Research analyst',
        status: 'working',
      },
    },
  };

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 10,
      projection: {
        ...baseProjection,
        messages: {
          'msg-1': {
            message_id: 'msg-1',
            sender: 'facilitator@thread-1',
            recipient: 'researcher',
            status: 'unread',
            summary: 'First mailbox message.',
          },
        },
      },
    },
    undefined,
    'run-v2',
  );

  assert.equal(
    useShadowCloneStore.getState().subtaskTranscriptStates['task-1']?.messages.length,
    1,
  );

  useShadowCloneStore.getState().handleSSEEvent(
    'shadow_clone_v2_projection',
    {
      type: 'shadow_clone_v2_projection',
      event_index: 11,
      projection: {
        ...baseProjection,
        messages: {},
      },
    },
    undefined,
    'run-v2',
  );

  const state = useShadowCloneStore.getState();
  assert.equal(
    state.subtaskTranscriptStates['task-1']?.messages.length || 0,
    0,
  );
});
