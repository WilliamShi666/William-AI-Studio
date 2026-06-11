import { expect, test, type Page, type Route } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

const TEST_EMAIL = process.env.E2E_TEST_EMAIL || 'codex.test+ssr1@local.dev';
const TEST_PASSWORD = process.env.E2E_TEST_PASSWORD || 'CodexTest!12345';
const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8002/api';
const SELECTED_MODEL = 'deepseek-v4-pro-max';

const terminalRunStatuses = new Set(['completed', 'stopped', 'error', 'failed']);
const terminalTaskStatuses = new Set(['completed', 'done', 'succeeded', 'success', 'failed', 'error', 'cancelled', 'canceled', 'stopped']);

type CapturedStart = {
  requestBody?: Record<string, unknown>;
  responseBody?: Record<string, unknown>;
};

type ShadowCloneStatus = {
  status?: string;
  mode?: string;
  model?: { requested?: string; effective?: string };
  team?: { members?: Record<string, { agent_id?: string; status?: string }> };
  agents?: Record<string, Record<string, unknown>>;
  tasks?: Record<string, Record<string, unknown>>;
  messages?: Record<string, Record<string, unknown>>;
  transcripts?: Array<Record<string, unknown>>;
  tool_calls?: Record<string, Record<string, unknown>>;
  final_output?: { content?: string } | null;
  live_activity?: { phase?: string; reason?: string };
  team_shutdown?: unknown;
};

type RawV2Event = {
  id: string;
  sequence?: number;
  type?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
};

type SchedulerEvidence = {
  createdTasks: number;
  independentTasks: number;
  claimedTasks: number;
  doneTasks: number;
  claimsBeforeFirstDone: number;
  maxActive: number;
  dependentTask: string;
  blocker: string;
  blockerDoneAt: string;
  dependentClaimedAt: string;
  dependencyEdges: Array<{
    taskId: string;
    blocker: string;
    blockerDoneAt: string;
    taskClaimedAt: string;
  }>;
};

function parseRequestBody(request: { postData(): string | null; postDataJSON(): unknown; headers(): Record<string, string> }) {
  const contentType = request.headers()['content-type'] || '';
  if (contentType.includes('application/json')) {
    return request.postDataJSON() as Record<string, unknown>;
  }

  const raw = request.postData() || '';
  const fields: Record<string, string> = {};

  for (const match of raw.matchAll(/name="([^"]+)"\r?\n\r?\n([\s\S]*?)(?=\r?\n--)/g)) {
    fields[match[1]] = match[2].trim();
  }

  if (!Object.keys(fields).length) {
    for (const pair of new URLSearchParams(raw).entries()) {
      fields[pair[0]] = pair[1];
    }
  }

  return fields;
}

async function installBackendProxy(page: Page, capturedStart: CapturedStart) {
  page.on('request', (request) => {
    if (!request.url().match(/\/agent\/(start|initiate)/) || !request.postData()) return;
    try {
      capturedStart.requestBody = parseRequestBody(request);
    } catch {
      capturedStart.requestBody = { parse_error: true };
    }
  });
  page.on('response', async (response) => {
    if (!response.url().match(/\/agent\/(start|initiate)/)) return;
    try {
      capturedStart.responseBody = await response.json() as Record<string, unknown>;
    } catch {
      capturedStart.responseBody = { parse_error: true };
    }
  });

  const targetBase = new URL(BACKEND_URL);
  const proxyRequest = async (route: Route) => {
    const request = route.request();
    const sourceUrl = new URL(request.url());
    const targetUrl = `${targetBase.origin}${sourceUrl.pathname}${sourceUrl.search}`;
    const isAgentStart = /\/agent\/(start|initiate)$/.test(sourceUrl.pathname);

    if (isAgentStart && request.postData()) {
      capturedStart.requestBody = parseRequestBody(request);
    }

    if (request.method() === 'OPTIONS') {
      await route.fulfill({
        status: 204,
        headers: {
          'access-control-allow-origin': '*',
          'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
          'access-control-allow-headers': 'authorization,content-type',
        },
        body: '',
      });
      return;
    }

    const acceptHeader = request.headers().accept || '';
    if (acceptHeader.includes('text/event-stream') || /\/agent-run\/[^/]+\/stream$/.test(sourceUrl.pathname)) {
      await route.continue({ url: targetUrl });
      return;
    }

    const response = await page.request.fetch(targetUrl, {
      method: request.method(),
      headers: request.headers(),
      data: request.postDataBuffer() ?? undefined,
      timeout: 180000,
    });
    const responseBody = await response.body();

    if (isAgentStart) {
      try {
        capturedStart.responseBody = JSON.parse(responseBody.toString('utf8')) as Record<string, unknown>;
      } catch {
        capturedStart.responseBody = { parse_error: true };
      }
    }

    await route.fulfill({
      status: response.status(),
      headers: {
        ...response.headers(),
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
        'access-control-allow-headers': 'authorization,content-type',
      },
      body: responseBody,
    });
  };

  await page.route('http://localhost/api/**', proxyRequest);
  await page.route('http://localhost:8081/api/**', proxyRequest);
  await page.route('**/api/**', proxyRequest);
}

async function login(page: Page) {
  const response = await page.request.post(`${BACKEND_URL}/auth/login`, {
    data: { email: TEST_EMAIL, password: TEST_PASSWORD },
    timeout: 30000,
  });
  expect(response.ok(), `auth login ${response.status()}`).toBeTruthy();

  const authData = await response.json() as {
    access_token: string;
    refresh_token: string;
    expires_at: number;
    user: unknown;
  };

  const session = {
    access_token: authData.access_token,
    refresh_token: authData.refresh_token,
    expires_at: authData.expires_at,
    user: authData.user,
  };

  await page.context().addCookies([
    { name: 'auth_token', value: authData.access_token, url: BASE_URL },
    { name: 'auth_session', value: encodeURIComponent(JSON.stringify(session)), url: BASE_URL },
  ]);

  await page.addInitScript(({ sessionValue, tokenValue, selectedModel }) => {
    localStorage.setItem('auth_session', sessionValue);
    localStorage.setItem('auth_token', tokenValue);
    localStorage.setItem('suna-preferred-model-v3', selectedModel);
    localStorage.setItem(
      'shadow-clone-store',
      JSON.stringify({ state: { mode: 'on', modeExplicitlySet: true }, version: 1 }),
    );
  }, {
    sessionValue: JSON.stringify(session),
    tokenValue: authData.access_token,
    selectedModel: SELECTED_MODEL,
  });

  return authData.access_token;
}

async function waitForBackendHealth(page: Page) {
  const response = await page.request.get(`${BACKEND_URL}/health`, { timeout: 10000 });
  expect(response.ok(), `backend health ${response.status()}`).toBeTruthy();
}

async function enterRoysAlphaIfNeeded(page: Page) {
  const roysAlphaLink = page.getByRole('link', { name: /Roys Alpha/i }).first();
  const roysAlphaButton = page.getByRole('button', { name: /Roys Alpha/i }).first();
  if (await roysAlphaLink.isVisible().catch(() => false)) {
    await roysAlphaLink.click();
    return;
  }
  if (await roysAlphaButton.isVisible().catch(() => false)) {
    await roysAlphaButton.click();
  }
}

async function getThreadIdFromUrl(page: Page): Promise<string> {
  const url = new URL(page.url());
  const match = url.pathname.match(/\/thread\/([^/]+)/);
  expect(match?.[1], `thread id should be present in URL: ${url.pathname}`).toBeTruthy();
  return match![1];
}

async function getAgentRuns(page: Page, threadId: string, authToken: string) {
  const response = await page.request.get(`${BACKEND_URL}/thread/${threadId}/agent-runs`, {
    headers: { Authorization: `Bearer ${authToken}` },
    timeout: 15000,
  });
  expect(response.ok(), `agent-runs ${response.status()}`).toBeTruthy();
  const payload = await response.json();
  return Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.agent_runs)
      ? payload.agent_runs
      : Array.isArray(payload?.data)
        ? payload.data
        : [];
}

async function waitForLatestRunId(page: Page, threadId: string, authToken: string, previousRunId?: string) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const runs = await getAgentRuns(page, threadId, authToken);
    const latestRun = runs[0];
    const runId = latestRun?.agent_run_id || latestRun?.id;
    if (runId && runId !== previousRunId) return String(runId);
    await page.waitForTimeout(750);
  }
  throw new Error(`latest run id did not appear for thread ${threadId}`);
}

async function getRunStatus(page: Page, threadId: string, authToken: string, runId: string) {
  const runs = await getAgentRuns(page, threadId, authToken);
  const run = runs.find((candidate: any) => (candidate.agent_run_id || candidate.id) === runId);
  return String(run?.status || 'missing');
}

async function waitForRunToFinish(page: Page, threadId: string, authToken: string, runId: string) {
  await expect
    .poll(
      async () => {
        const status = await getRunStatus(page, threadId, authToken, runId);
        return terminalRunStatuses.has(status) ? status : status || 'missing';
      },
      {
        timeout: 300000,
        intervals: [1000, 1500, 2000, 3000, 5000],
        message: `run ${runId} should finish`,
      },
    )
    .toBe('completed');
}

async function waitForShadowCloneStatus(
  page: Page,
  authToken: string,
  runId: string,
  expectedStatus = 'completed',
): Promise<ShadowCloneStatus> {
  let latest: ShadowCloneStatus = {};
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`${BACKEND_URL}/agent-run/${runId}/shadow-clone/status`, {
          headers: { Authorization: `Bearer ${authToken}` },
          timeout: 15000,
        });
        if (!response.ok()) return `http-${response.status()}`;
        latest = await response.json() as ShadowCloneStatus;
        return latest.status || 'missing';
      },
      {
        timeout: 180000,
        intervals: [1000, 1500, 2000, 3000, 5000],
        message: `shadow clone status should become ${expectedStatus}`,
      },
    )
    .toBe(expectedStatus);
  return latest;
}

function parseSseEvents(text: string) {
  return text
    .split(/\n\n+/)
    .map((block) => block
      .split('\n')
      .filter((line) => line.startsWith('data: '))
      .map((line) => line.slice('data: '.length))
      .join('\n'))
    .filter(Boolean)
    .map((raw) => {
      try {
        return JSON.parse(raw);
      } catch {
        return { parse_error: true, raw };
      }
    });
}

async function replayRunStream(page: Page, authToken: string, runId: string) {
  const response = await page.request.get(`${BACKEND_URL}/agent-run/${runId}/stream?from_index=0`, {
    headers: { Authorization: `Bearer ${authToken}` },
    timeout: 60000,
  });
  expect(response.ok(), `stream replay ${response.status()}`).toBeTruthy();
  return parseSseEvents(await response.text());
}

async function sendPrompt(page: Page, prompt: string) {
  const input = page.locator('textarea').last();
  await expect(input).toBeVisible({ timeout: 30000 });
  await input.fill(prompt);
  await input.press('Enter');
}

function values(record: Record<string, unknown> | undefined) {
  return Object.values(record || {});
}

function taskStatus(task: any) {
  return String(task?.status || task?.new_status || task?.state || task?.metadata?.status || '').toLowerCase();
}

function escapeRegex(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseEnvFile(filePath: string) {
  if (!existsSync(filePath)) return {};
  const env: Record<string, string> = {};
  for (const rawLine of readFileSync(filePath, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const [key, ...rest] = line.split('=');
    env[key.trim()] = rest.join('=').trim().replace(/^['"]|['"]$/g, '');
  }
  return env;
}

function readV2RawEventsFromRedis(runId: string): RawV2Event[] {
  const backendEnvPath = path.resolve(process.cwd(), '../backend/.env');
  const backendEnv = parseEnvFile(backendEnvPath);
  const redisHost = backendEnv.REDIS_HOST || process.env.REDIS_HOST || 'localhost';
  const redisPort = backendEnv.REDIS_PORT || process.env.REDIS_PORT || '6379';
  const redisDb = backendEnv.REDIS_DB || process.env.REDIS_DB || '0';
  const redisPassword = backendEnv.REDIS_PASSWORD || process.env.REDIS_PASSWORD || '';
  const output = execFileSync(
    'redis-cli',
    [
      '--raw',
      '-h',
      redisHost,
      '-p',
      redisPort,
      '-n',
      redisDb,
      'XRANGE',
      `sc_v2:run:${runId}:events`,
      '-',
      '+',
    ],
    {
      encoding: 'utf8',
      env: {
        ...process.env,
        ...(redisPassword ? { REDISCLI_AUTH: redisPassword } : {}),
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );

  const lines = output.split(/\r?\n/).filter(Boolean);
  const events: RawV2Event[] = [];
  for (let index = 0; index + 2 < lines.length; index += 3) {
    const streamId = lines[index];
    const fieldName = lines[index + 1];
    const rawEvent = lines[index + 2];
    if (fieldName !== 'event') continue;
    const event = JSON.parse(rawEvent) as RawV2Event;
    events.push({ ...event, id: streamId });
  }
  return events;
}

function rawPayload(event: RawV2Event) {
  return event.payload && typeof event.payload === 'object' ? event.payload : {};
}

function rawTaskSnapshot(payload: Record<string, unknown>) {
  return payload.task && typeof payload.task === 'object'
    ? payload.task as Record<string, unknown>
    : {};
}

function rawTaskId(payload: Record<string, unknown>) {
  const task = rawTaskSnapshot(payload);
  return String(payload.task_id || payload.id || task.id || '');
}

function rawTaskStatus(payload: Record<string, unknown>) {
  const task = rawTaskSnapshot(payload);
  return String(payload.new_status || payload.status || task.status || '').toLowerCase();
}

function rawTaskBlockedBy(payload: Record<string, unknown>) {
  const task = rawTaskSnapshot(payload);
  const value = Array.isArray(task.blocked_by) ? task.blocked_by : payload.blocked_by;
  return Array.isArray(value) ? value.map(String) : [];
}

function rawTaskText(taskId: string, payload: Record<string, unknown>) {
  const task = rawTaskSnapshot(payload);
  return [
    taskId,
    String(task.subject || ''),
    String(task.description || ''),
  ].join(' ').toLowerCase();
}

function rawEventTime(event: RawV2Event) {
  return new Date(String(event.created_at || 0)).getTime();
}

function normalizeToolNameForUi(value: unknown) {
  return String(value || 'unknown')
    .trim()
    .replace(/_/g, '-')
    .toLowerCase();
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function expectToolCallLifecycleEvidence(
  rawEvents: RawV2Event[],
  projectionToolCalls: unknown[],
) {
  expect(
    rawEvents.some((event) => event.type === 'tool_call_started'),
    'raw Redis event log should include ordinary worker tool_call_started',
  ).toBeTruthy();
  expect(
    rawEvents.some((event) => event.type === 'tool_call_completed' || event.type === 'tool_call_failed'),
    'raw Redis event log should include ordinary worker terminal tool call event',
  ).toBeTruthy();
  expect(
    projectionToolCalls.some((toolCall: any) => toolCall?.task_id && toolCall?.agent_name),
    'projected tool_calls should be associated with task_id and agent_name',
  ).toBeTruthy();
  expect(
    projectionToolCalls.some((toolCall: any) => toolCall?.arguments_summary || toolCall?.arguments_redacted === true),
    'projected tool_calls should expose bounded/redacted argument summary',
  ).toBeTruthy();
}

function analyzeV2SchedulerEvidence(events: RawV2Event[]): SchedulerEvidence {
  const created: Array<{ taskId: string; event: RawV2Event; payload: Record<string, unknown> }> = [];
  const claimed: Array<{ taskId: string; event: RawV2Event; payload: Record<string, unknown> }> = [];
  const done: Array<{ taskId: string; event: RawV2Event; payload: Record<string, unknown> }> = [];
  const blockersByTask = new Map<string, string[]>();

  for (const event of events) {
    const payload = rawPayload(event);
    const taskId = rawTaskId(payload);
    if (taskId && rawTaskBlockedBy(payload).length) {
      blockersByTask.set(taskId, rawTaskBlockedBy(payload));
    }
    if (event.type === 'task_created' && taskId) {
      created.push({ taskId, event, payload });
    } else if (event.type === 'task_claimed' && taskId) {
      claimed.push({ taskId, event, payload });
    } else if (
      taskId
      && (
        event.type === 'task_completed'
        || event.type === 'task_failed'
        || (event.type === 'task_updated' && ['completed', 'failed'].includes(rawTaskStatus(payload)))
      )
    ) {
      done.push({ taskId, event, payload });
    }
  }

  const firstDoneAt = Math.min(...done.map(({ event }) => rawEventTime(event)));
  const claimsBeforeFirstDone = claimed.filter(({ event }) => rawEventTime(event) < firstDoneAt).length;
  const activeWindows = claimed
    .map(({ taskId, event }) => {
      const doneEvent = done.find((candidate) => candidate.taskId === taskId)?.event;
      return doneEvent ? { taskId, startedAt: rawEventTime(event), doneAt: rawEventTime(doneEvent) } : null;
    })
    .filter(Boolean) as Array<{ taskId: string; startedAt: number; doneAt: number }>;
  const maxActive = activeWindows.reduce((currentMax, window) => {
    const activeAtStart = activeWindows.filter(
      (candidate) => candidate.startedAt <= window.startedAt && window.startedAt < candidate.doneAt,
    ).length;
    return Math.max(currentMax, activeAtStart);
  }, 0);

  const dependentTask = Array.from(blockersByTask.keys()).find((taskId) => taskId.includes('dependent-after-1'))
    || Array.from(blockersByTask.keys())[0]
    || '';
  const blocker = dependentTask ? blockersByTask.get(dependentTask)?.[0] || '' : '';
  const blockerDoneAt = done.find((candidate) => candidate.taskId === blocker)?.event.created_at || '';
  const dependentClaimedAt = claimed.find((candidate) => candidate.taskId === dependentTask)?.event.created_at || '';
  const dependencyEdges = Array.from(blockersByTask.entries()).flatMap(([taskId, blockers]) =>
    blockers.map((blockerId) => ({
      taskId,
      blocker: blockerId,
      blockerDoneAt: done.find((candidate) => candidate.taskId === blockerId)?.event.created_at || '',
      taskClaimedAt: claimed.find((candidate) => candidate.taskId === taskId)?.event.created_at || '',
    })),
  );

  return {
    createdTasks: created.length,
    independentTasks: created.filter(({ taskId, payload }) => rawTaskText(taskId, payload).includes('independent-')).length,
    claimedTasks: claimed.length,
    doneTasks: done.length,
    claimsBeforeFirstDone,
    maxActive,
    dependentTask,
    blocker,
    blockerDoneAt,
    dependentClaimedAt,
    dependencyEdges,
  };
}

function expectRegularSupervisorMetadata(run: Record<string, any> | undefined) {
  const metadata = typeof run?.metadata === 'string'
    ? JSON.parse(run.metadata)
    : run?.metadata || {};
  expect(metadata.shadow_clone_runtime).toBe('v2');
  expect(metadata.shadow_clone_v2_execution_chain).toBe('regular_supervisor');
  expect(metadata.regular_execution_mode).toBe('phase2_supervisor');
}

async function returnToMainViewIfNeeded(page: Page) {
  const backButton = page.getByRole('button', { name: /返回主视图|Back to main view/i }).first();
  if (await backButton.isVisible({ timeout: 1000 }).catch(() => false)) {
    await backButton.evaluate((element) => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
  }
  await expect(page.locator('textarea').last()).toBeVisible({ timeout: 30000 });
}

test.describe('Shadow Clone V2 acceptance', () => {
  test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => undefined);
  });

  test('shows true natural-language output and generated file cards in actual chat panels', async ({ page }) => {
    test.setTimeout(900000);

    const capturedStart: CapturedStart = {};
    await waitForBackendHealth(page);
    await installBackendProxy(page, capturedStart);
    const authToken = await login(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await enterRoysAlphaIfNeeded(page);

    const unique = Date.now();
    const mainPlanningMarker = `MAIN_PLANNING_NATURAL_LANGUAGE_OK_${unique}`;
    const mainFinalMarker = `MAIN_FINAL_NATURAL_LANGUAGE_OK_${unique}`;
    const subagentOneMarker = `SUBAGENT_NATURAL_LANGUAGE_OK_writer_agent_1_${unique}`;
    const subagentTwoMarker = `SUBAGENT_NATURAL_LANGUAGE_OK_writer_agent_2_${unique}`;
    const fileOneName = `writer-agent-1_delivery_${unique}.md`;
    const fileTwoName = `writer-agent-2_delivery_${unique}.md`;
    const fileOneMarker = `FILE_ARTIFACT_CARD_OK_writer_agent_1_${unique}`;
    const fileTwoMarker = `FILE_ARTIFACT_CARD_OK_writer_agent_2_${unique}`;

    const prompt = [
      `E2E Shadow Clone V2 true chat-panel output and file cards ${unique}.`,
      '必须严格使用 Shadow Clone V2 的 TeamCreate、TaskCreate 和 SendMessage 工具来创建团队和任务，不要只在文本里描述计划。',
      '创建两个 teammate，名字必须正好是 writer-agent-1 和 writer-agent-2。',
      `主 agent 在完成 durable TeamCreate/TaskCreate/SendMessage 工具调用后的自然语言规划输出里必须包含 ${mainPlanningMarker}。这必须是给用户看的自然语言，不是 [系统进度] 日志。`,
      `writer-agent-1 的任务：在自己的自然语言输出中包含 ${subagentOneMarker}；使用 write_file 创建 /workspace/${fileOneName}；文件内容必须包含 ${fileOneMarker}。`,
      `writer-agent-2 的任务：在自己的自然语言输出中包含 ${subagentTwoMarker}；使用 write_file 创建 /workspace/${fileTwoName}；文件内容必须包含 ${fileTwoMarker}。`,
      `最终 main agent 回答必须包含 ${mainFinalMarker}、${subagentOneMarker}、${subagentTwoMarker}、${fileOneName} 和 ${fileTwoName}。`,
      '保持输出简短。不要把 subagent 的自然语言输出替换成系统进度或工具日志。',
    ].join('\n');

    await sendPrompt(page, prompt);

    await expect.poll(() => capturedStart.requestBody ? 'captured' : '', {
      timeout: 30000,
      message: 'agent/start request should be captured',
    }).toBe('captured');

    await expect(page).toHaveURL(/\/projects\/[^/]+\/thread\/[^/]+/, { timeout: 30000 });
    const url = new URL(page.url());
    const urlMatch = url.pathname.match(/\/projects\/([^/]+)\/thread\/([^/]+)/);
    expect(urlMatch, `project/thread ids should be present in URL: ${url.pathname}`).toBeTruthy();
    const projectId = urlMatch![1];
    const threadId = urlMatch![2];
    const runId = String(capturedStart.responseBody?.agent_run_id || await waitForLatestRunId(page, threadId, authToken));

    const mainAssistantGroups = page.locator('[data-testid="thread-assistant-message-group"]');
    await expect
      .poll(
        async () => {
          const runStatus = await getRunStatus(page, threadId, authToken, runId);
          const text = (await mainAssistantGroups.allTextContents()).join('\n');
          if (!terminalRunStatuses.has(runStatus) && text.includes(mainPlanningMarker)) {
            return 'natural-language-visible-before-terminal';
          }
          return terminalRunStatuses.has(runStatus) ? `terminal:${runStatus}` : 'waiting';
        },
        {
          timeout: 180000,
          intervals: [1000, 1500, 2000, 3000, 5000],
          message: 'main-agent planning natural-language marker should appear in the actual chat panel before run completion',
        },
      )
      .toBe('natural-language-visible-before-terminal');

    const preTerminalMainText = (await mainAssistantGroups.allTextContents()).join('\n');
    expect(preTerminalMainText).toContain(mainPlanningMarker);
    expect(preTerminalMainText).not.toContain(`[系统进度] ${mainPlanningMarker}`);

    await waitForRunToFinish(page, threadId, authToken, runId);
    const status = await waitForShadowCloneStatus(page, authToken, runId);
    const runMetadata = (await getAgentRuns(page, threadId, authToken))
      .find((candidate: any) => (candidate.agent_run_id || candidate.id) === runId);
    expectRegularSupervisorMetadata(runMetadata);
    expect(status.mode).toBe('v2');
    expect(status.final_output?.content || '').toContain(mainFinalMarker);

    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => undefined);

    await expect(mainAssistantGroups.last()).toContainText(mainFinalMarker, { timeout: 60000 });
    await expect(
      mainAssistantGroups.last().getByRole('button', { name: new RegExp(escapeRegex(fileOneName)) }),
    ).toBeVisible({ timeout: 60000 });
    await expect(
      mainAssistantGroups.last().getByRole('button', { name: new RegExp(escapeRegex(fileTwoName)) }),
    ).toBeVisible({ timeout: 60000 });

    const statusTasks = values(status.tasks) as any[];
    const taskOne = statusTasks.find((task: any) => JSON.stringify(task).includes('writer-agent-1')) as any;
    const taskTwo = statusTasks.find((task: any) => JSON.stringify(task).includes('writer-agent-2')) as any;
    expect(taskOne?.id || taskOne?.task_id, JSON.stringify(status.tasks)).toBeTruthy();
    expect(taskTwo?.id || taskTwo?.task_id, JSON.stringify(status.tasks)).toBeTruthy();
    const taskOneSubtaskId = String(taskOne.id || taskOne.task_id);
    const taskTwoSubtaskId = String(taskTwo.id || taskTwo.task_id);

    const monitorRows = page.locator('[data-testid="shadow-clone-monitor-row"]');
    await expect(monitorRows).toHaveCount(2, { timeout: 60000 });

    await page
      .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${taskOneSubtaskId}"]`)
      .first()
      .evaluate((element) => {
        element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      });
    await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
    const subagentOnePanel = page.locator('[data-testid="thread-assistant-message-group"]').last();
    await expect(subagentOnePanel).toContainText(subagentOneMarker, { timeout: 60000 });
    await expect(
      subagentOnePanel.getByRole('button', { name: new RegExp(escapeRegex(fileOneName)) }),
    ).toBeVisible({ timeout: 60000 });
    await expect(subagentOnePanel).not.toContainText(`[系统进度] ${subagentOneMarker}`);

    await returnToMainViewIfNeeded(page);
    await page
      .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${taskTwoSubtaskId}"]`)
      .first()
      .evaluate((element) => {
        element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      });
    await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
    const subagentTwoPanel = page.locator('[data-testid="thread-assistant-message-group"]').last();
    await expect(subagentTwoPanel).toContainText(subagentTwoMarker, { timeout: 60000 });
    await expect(
      subagentTwoPanel.getByRole('button', { name: new RegExp(escapeRegex(fileTwoName)) }),
    ).toBeVisible({ timeout: 60000 });
    await expect(subagentTwoPanel).not.toContainText(`[系统进度] ${subagentTwoMarker}`);
  });

  test('shows realtime main and selected subagent progress in actual chat panels before completion', async ({ page }) => {
    test.setTimeout(420000);

    const capturedStart: CapturedStart = {};
    await waitForBackendHealth(page);
    await installBackendProxy(page, capturedStart);
    const authToken = await login(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await enterRoysAlphaIfNeeded(page);

    const unique = Date.now();
    const mainRealtimeMarker = `MAIN_STREAM_MARKER_${unique}`;
    const subagentOneRealtimeMarker = `SUB_STREAM_MARKER_realtime_agent_1_${unique}`;
    const subagentTwoRealtimeMarker = `SUB_STREAM_MARKER_realtime_agent_2_${unique}`;
    const subagentOneMarkerParts = ['SUB_STREAM_MARKER', 'realtime_agent_1', String(unique)];
    const subagentTwoMarkerParts = ['SUB_STREAM_MARKER', 'realtime_agent_2', String(unique)];
    await sendPrompt(
      page,
      [
        `E2E Shadow Clone V2 realtime chat-panel visibility ${unique}.`,
        '必须严格使用 Shadow Clone V2 的 Team/Task 工具，不要只在文本里描述计划。',
        '创建两个 teammate，名字必须正好是 realtime-agent-1 和 realtime-agent-2。',
        `主 agent 在完成 durable Team/Task 工具调用后的自然语言规划输出里必须包含 ${mainRealtimeMarker}。这必须是 assistant 自然语言，不是 [系统进度] 日志。`,
        '主 agent 的规划输出绝对不能包含任何以 SUB_STREAM_MARKER 开头的完整拼接字符串；规划阶段只能描述片段规则，完整拼接字符串只能由对应 subagent 自己在 assistant 自然语言输出中产生，最终总结阶段才可以引用已完成的完整拼接字符串。',
        `realtime-agent-1 的任务：开始执行时必须先用自己的 assistant 自然语言输出一个由这些片段按下划线连接得到的标记：${JSON.stringify(subagentOneMarkerParts)}。不要在任务描述或系统进度中直接写出拼接后的完整标记；只有 realtime-agent-1 自己的 assistant 输出可以写出拼接后的完整标记。然后调用普通 shell/command 工具执行一个约 45 秒的等待命令，为 UI 实时观察留出时间；最后写一个 markdown 文件，内容包含 REALTIME_PROGRESS_E2E_OK_AGENT_1。`,
        `realtime-agent-2 的任务：开始执行时必须先用自己的 assistant 自然语言输出一个由这些片段按下划线连接得到的标记：${JSON.stringify(subagentTwoMarkerParts)}。不要在任务描述或系统进度中直接写出拼接后的完整标记；只有 realtime-agent-2 自己的 assistant 输出可以写出拼接后的完整标记。然后调用普通 shell/command 工具执行一个约 45 秒的等待命令，为 UI 实时观察留出时间；最后写一个 markdown 文件，内容包含 REALTIME_PROGRESS_E2E_OK_AGENT_2。`,
        `主 agent 最终回答必须包含 REALTIME_MAIN_DONE、REALTIME_PROGRESS_E2E_OK_AGENT_1、REALTIME_PROGRESS_E2E_OK_AGENT_2、${mainRealtimeMarker}，并包含两个 subagent 各自拼接后的完整标记。`,
      ].join('\n'),
    );

    await expect.poll(() => capturedStart.requestBody ? 'captured' : '', {
      timeout: 30000,
      message: 'agent/start request should be captured',
    }).toBe('captured');

    await expect(page).toHaveURL(/\/projects\/[^/]+\/thread\/[^/]+/, { timeout: 30000 });
    const threadId = await getThreadIdFromUrl(page);
    const runId = String(capturedStart.responseBody?.agent_run_id || await waitForLatestRunId(page, threadId, authToken));

    const assistantGroups = page.locator('[data-testid="thread-assistant-message-group"]');
    await expect
      .poll(
        async () => {
          const status = await getRunStatus(page, threadId, authToken, runId);
          const text = (await assistantGroups.allTextContents()).join('\n');
          if (
            !terminalRunStatuses.has(status)
            && text.includes(mainRealtimeMarker)
            && !text.includes(`[系统进度] ${mainRealtimeMarker}`)
          ) {
            return 'natural-language-visible-before-terminal';
          }
          return terminalRunStatuses.has(status) ? `terminal:${status}` : 'waiting';
        },
        {
          timeout: 180000,
          intervals: [500, 750, 1000, 1500, 2000],
          message: 'main chat panel should show Shadow Clone V2 assistant natural language before run completion',
        },
      )
      .toBe('natural-language-visible-before-terminal');

    const monitorRows = page.locator('[data-testid="shadow-clone-monitor-row"]');
    await expect(monitorRows.first()).toBeVisible({ timeout: 180000 });
    let selectedSubtaskId = '';
    await expect
      .poll(
        async () => {
          const rowCount = await monitorRows.count();
          for (let index = 0; index < rowCount; index += 1) {
            const row = monitorRows.nth(index);
            const rowText = await row.textContent().catch(() => '');
            if (!String(rowText || '').includes('realtime-agent-1')) continue;
            const rowId = await row.getAttribute('data-subtask-id');
            if (rowId && await row.isVisible().catch(() => false)) {
              selectedSubtaskId = rowId;
              return 'realtime-agent-1-task-row-ready';
            }
          }
          return 'waiting';
        },
        {
          timeout: 180000,
          intervals: [500, 750, 1000, 1500, 2000],
          message: 'monitor should expose realtime-agent-1 task row before selecting subagent chat',
        },
      )
      .toBe('realtime-agent-1-task-row-ready');
    expect(selectedSubtaskId, 'selected monitor row should expose a real task data-subtask-id').toBeTruthy();
    const selectedMonitorRow = page
      .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${selectedSubtaskId}"]`)
      .first();
    await expect(selectedMonitorRow).toBeVisible({ timeout: 30000 });
    await selectedMonitorRow.click({ force: true });
    await expect(
      page.getByRole('button', { name: /返回主视图|Back to main view/i }).first(),
    ).toBeVisible({ timeout: 30000 });
    await expect(page.getByText(/当前正在查看 .* 的实时输出|live output/i)).toBeVisible({ timeout: 30000 });
    await expect(
      page.locator(`[data-testid="shadow-clone-selected-subagent-tool-panel"][data-subtask-id="${selectedSubtaskId}"]`),
    ).toBeVisible({ timeout: 30000 });

    let selectedPollLastNonTerminalState = 'not-polled';
    await expect
      .poll(
        async () => {
          const status = await getRunStatus(page, threadId, authToken, runId);
          if (terminalRunStatuses.has(status)) {
            return `terminal:${status}:${selectedPollLastNonTerminalState}`;
          }

          const shadowStatusResponse = await page.request.get(`${BACKEND_URL}/agent-run/${runId}/shadow-clone/status`, {
            headers: { Authorization: `Bearer ${authToken}` },
            timeout: 15000,
          });
          if (!shadowStatusResponse.ok()) return `shadow-status-http-${shadowStatusResponse.status()}`;
          const shadowStatus = await shadowStatusResponse.json() as ShadowCloneStatus;
          const selectedTask = (values(shadowStatus.tasks) as any[]).find((candidate: any) => {
            const candidateId = String(candidate?.id || candidate?.task_id || '');
            return candidateId === selectedSubtaskId;
          });
          const selectedStatus = taskStatus(selectedTask);
          if (!selectedTask) {
            selectedPollLastNonTerminalState = 'selected-task-missing';
            return selectedPollLastNonTerminalState;
          }
          const selectedPanelVisible = await page
            .locator(`[data-testid="shadow-clone-selected-subagent-tool-panel"][data-subtask-id="${selectedSubtaskId}"]`)
            .isVisible()
            .catch(() => false);
          if (!selectedPanelVisible) {
            selectedPollLastNonTerminalState = 'selected-panel-not-active';
            return selectedPollLastNonTerminalState;
          }

          const assistantGroupTexts = await page.locator('[data-testid="thread-assistant-message-group"]').allTextContents();
          const text = assistantGroupTexts.join('\n');
          if (
            text.includes(subagentOneRealtimeMarker)
            && !text.includes(subagentTwoRealtimeMarker)
            && !text.includes(`[系统进度] ${subagentOneRealtimeMarker}`)
            && !terminalTaskStatuses.has(selectedStatus)
          ) {
            return 'natural-language-visible-before-terminal';
          }
          const selectedPanelText = await page
            .locator(`[data-testid="shadow-clone-selected-subagent-tool-panel"][data-subtask-id="${selectedSubtaskId}"]`)
            .textContent()
            .catch(() => '');
          const bodyHasSelectedMarker = await page
            .locator('body')
            .textContent()
            .then((bodyText) => String(bodyText || '').includes(subagentOneRealtimeMarker))
            .catch(() => false);
          const debugSnapshot = [
            `groups-${assistantGroupTexts.length}`,
            `lastGroup-${String(assistantGroupTexts.at(-1) || '').slice(0, 120).replace(/\s+/g, ' ')}`,
            `panelText-${String(selectedPanelText || '').slice(0, 120).replace(/\s+/g, ' ')}`,
            `bodyMarker-${bodyHasSelectedMarker ? 'present' : 'missing'}`,
          ].join(':');
          selectedPollLastNonTerminalState = [
            `waiting:selected-task-${selectedStatus || 'unknown'}`,
            `taskTerminal-${terminalTaskStatuses.has(selectedStatus) ? 'yes' : 'no'}`,
            `panel-${selectedPanelVisible ? 'active' : 'inactive'}`,
            `marker-${text.includes(subagentOneRealtimeMarker) ? 'present' : 'missing'}`,
            `other-${text.includes(subagentTwoRealtimeMarker) ? 'present' : 'missing'}`,
            `system-${text.includes(`[系统进度] ${subagentOneRealtimeMarker}`) ? 'present' : 'missing'}`,
            debugSnapshot,
          ].join(':');
          return selectedPollLastNonTerminalState;
        },
        {
          timeout: 180000,
          intervals: [500, 750, 1000, 1500, 2000],
          message: 'selected subagent chat panel should show assistant natural language while that selected subtask is still running',
        },
      )
      .toBe('natural-language-visible-before-terminal');

    await waitForRunToFinish(page, threadId, authToken, runId);
    const finalStatus = await waitForShadowCloneStatus(page, authToken, runId);
    expect(finalStatus.mode).toBe('v2');
    expect(finalStatus.final_output?.content || '').toContain('REALTIME_MAIN_DONE');
    expect(finalStatus.final_output?.content || '').toContain(subagentOneRealtimeMarker);
    expect(finalStatus.final_output?.content || '').toContain(subagentTwoRealtimeMarker);

    const replayEvents = await replayRunStream(page, authToken, runId);
    const selectedSubagentChunkIndex = replayEvents.findIndex((event: any) => {
      const metadata = typeof event.metadata === 'string'
        ? JSON.parse(event.metadata || '{}')
        : event.metadata || {};
      return event.type === 'subagent_activity'
        && event.subtask_id === selectedSubtaskId
        && event.message_type === 'assistant'
        && metadata.stream_status === 'chunk'
        && JSON.stringify(event.content || '').includes(subagentOneRealtimeMarker);
    });
    const selectedSubagentAssistantCompleteIndex = replayEvents.findIndex((event: any) => {
      const metadata = typeof event.metadata === 'string'
        ? JSON.parse(event.metadata || '{}')
        : event.metadata || {};
      return event.type === 'subagent_activity'
        && event.subtask_id === selectedSubtaskId
        && event.message_type === 'assistant'
        && metadata.stream_status === 'complete';
    });
    expect(selectedSubagentChunkIndex, 'selected subagent marker should be replayed as a chunk event').toBeGreaterThanOrEqual(0);
    expect(
      selectedSubagentAssistantCompleteIndex,
      'selected subagent assistant completion should be replayed after the marker chunk',
    ).toBeGreaterThan(selectedSubagentChunkIndex);
  });

  test('replays completed V2 subagent transcripts into chat panel and preserves five-member ledger', async ({ page }) => {
    test.setTimeout(120000);

    const completedRunId = 'b5685393-a1ea-4a02-8939-99a06840cbb0';
    const projectId = '3ba94525-cf0e-4caf-9e24-f91ea470d3b9';
    const threadId = '53ba708a-d222-4c80-a5ae-2d19a66cc3a4';
    const selectedSubtaskId = 'gpu_story_2';
    const selectedMarker = 'TEAM_MEMBER_GPU_STORY_1780510204136_2';

    const capturedStart: CapturedStart = {};
    await waitForBackendHealth(page);
    await installBackendProxy(page, capturedStart);
    const authToken = await login(page);

    const statusResponse = await page.request.get(`${BACKEND_URL}/agent-run/${completedRunId}/shadow-clone/status`, {
      headers: { Authorization: `Bearer ${authToken}` },
      timeout: 30000,
    });
    expect(statusResponse.ok(), `completed V2 status ${statusResponse.status()}`).toBeTruthy();
    const status = await statusResponse.json() as ShadowCloneStatus;
    expect(status.mode).toBe('v2');
    expect(status.status).toBe('completed');
    expect(status.transcripts?.length).toBe(5);
    expect(
      status.transcripts?.some(
        (transcript) =>
          transcript.task_id === selectedSubtaskId &&
          String(transcript.content || '').includes(selectedMarker),
      ),
      'status projection should expose selected subagent durable transcript marker',
    ).toBeTruthy();

    await page.goto(`/projects/${projectId}/thread/${threadId}`, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    });
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => undefined);

    const monitorRows = page.locator('[data-testid="shadow-clone-monitor-row"]');
    await expect(monitorRows.first()).toBeVisible({ timeout: 30000 });
    await expect(monitorRows).toHaveCount(5, { timeout: 30000 });

    await page
      .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${selectedSubtaskId}"]`)
      .evaluate((element) => {
        element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      });

    await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
    const assistantGroups = page.locator('[data-testid="thread-assistant-message-group"]');
    await expect(assistantGroups).toHaveCount(1, { timeout: 30000 });
    await expect(assistantGroups.first()).toContainText(selectedMarker, { timeout: 30000 });

    await returnToMainViewIfNeeded(page);
    await expect(monitorRows).toHaveCount(5, { timeout: 30000 });
    await expect(
      page.locator('[data-testid="shadow-clone-monitor-row"][data-subtask-id="gpu_story_1"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="shadow-clone-monitor-row"][data-subtask-id="gpu_story_5"]'),
    ).toBeVisible();
  });

  test('verifies model propagation, event projection, peer message, UI inspect, and same-thread wake', async ({ page }) => {
    test.setTimeout(600000);

    const capturedStart: CapturedStart = {};
    await waitForBackendHealth(page);
    await installBackendProxy(page, capturedStart);
    const authToken = await login(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await enterRoysAlphaIfNeeded(page);

    const unique = Date.now();
    const idleDraftMarker = `IDLE_DRAFT_V1_${unique}`;
    const idleFeedbackMarker = `IDLE_FEEDBACK_REQUEST_${unique}`;
    const idleImprovedMarker = `IDLE_FEEDBACK_APPLIED_${unique}`;

    await sendPrompt(
      page,
      `E2E Shadow Clone V2 acceptance ${unique}: 请使用 teammate-1 通过 Team/Task/SendMessage 工具完成 live-click-subtask。teammate-1 必须调用普通文件写入工具创建 /workspace/live-click-subtask-${unique}.md，文件内容包含 LIVE_CLICK_SUBAGENT_OK 和 ${idleDraftMarker}。子任务结果必须包含 LIVE_CLICK_SUBAGENT_OK 和 ${idleDraftMarker}。主 agent 最终回答必须包含 MAIN_LIVE_CLICK_OK、LIVE_CLICK_SUBAGENT_OK 和 ${idleDraftMarker}。`,
    );

    await expect.poll(() => capturedStart.requestBody ? 'captured' : '', {
      timeout: 30000,
      message: 'agent/start request should be captured',
    }).toBe('captured');
    expect(capturedStart.requestBody?.model_name).toBe(SELECTED_MODEL);
    expect(capturedStart.requestBody?.shadow_clone_mode).toBe('on');

    await expect(page).toHaveURL(/\/projects\/[^/]+\/thread\/[^/]+/, { timeout: 30000 });
    const threadId = await getThreadIdFromUrl(page);
    const firstRunId = String(capturedStart.responseBody?.agent_run_id || await waitForLatestRunId(page, threadId, authToken));

    await waitForRunToFinish(page, threadId, authToken, firstRunId);
    const firstStatus = await waitForShadowCloneStatus(page, authToken, firstRunId);
    const liveClickTask = values(firstStatus.tasks).find((task: any) => JSON.stringify(task).includes('LIVE_CLICK_SUBAGENT_OK')) as any;
    expect(liveClickTask?.id || liveClickTask?.task_id, JSON.stringify(firstStatus.tasks)).toBeTruthy();
    const liveClickSubtaskId = String(liveClickTask.id || liveClickTask.task_id);

    const row = page.locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${liveClickSubtaskId}"]`).first();
    await expect(row).toBeVisible({ timeout: 60000 });
    await row.evaluate((element) => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
    await expect(page.locator('[data-testid="thread-assistant-message-group"]').last()).toContainText('LIVE_CLICK_SUBAGENT_OK', { timeout: 90000 });
    await expect(page.locator('[data-testid="thread-assistant-message-group"]').last()).toContainText(idleDraftMarker, { timeout: 90000 });
    const firstRunMetadata = (await getAgentRuns(page, threadId, authToken))
      .find((candidate: any) => (candidate.agent_run_id || candidate.id) === firstRunId);
    expectRegularSupervisorMetadata(firstRunMetadata);

    expect(firstStatus.mode).toBe('v2');
    expect(firstStatus.model?.requested).toBe(SELECTED_MODEL);
    expect(firstStatus.model?.effective).toBe(SELECTED_MODEL);
    expect(firstStatus.final_output?.content || '').toContain('MAIN_LIVE_CLICK_OK');
    expect(firstStatus.final_output?.content || '').toContain('LIVE_CLICK_SUBAGENT_OK');
    expect(firstStatus.final_output?.content || '').toContain(idleDraftMarker);

    const firstMembers = firstStatus.team?.members || {};
    const teammate = firstMembers['teammate-1'] || values(firstMembers).find((member: any) => member?.agent_id?.includes('teammate-1')) as any;
    expect(teammate?.agent_id, 'stable teammate agent id should be present').toBeTruthy();
    const stableTeammateAgentId = String(teammate.agent_id);

    const firstTasks = values(firstStatus.tasks);
    expect(firstTasks.some((task: any) => task?.status === 'completed')).toBeTruthy();
    expect(firstTasks.some((task: any) => JSON.stringify(task).includes('LIVE_CLICK_SUBAGENT_OK'))).toBeTruthy();

    const firstMessages = values(firstStatus.messages);
    expect(firstMessages.some((message: any) => String(message?.sender || '').includes('facilitator') && String(message?.recipient || '').includes('teammate'))).toBeTruthy();

    const firstAgents = values(firstStatus.agents);
    expect(firstAgents.some((agent: any) => agent?.status === 'idle' && agent?.idle_expires_at)).toBeTruthy();
    expect(firstStatus.live_activity?.phase).toBe('completed');

    const streamEvents = await replayRunStream(page, authToken, firstRunId);
    const projectionEvents = streamEvents.filter((event: any) => event?.type === 'shadow_clone_v2_projection');
    expect(projectionEvents.length, JSON.stringify(streamEvents.slice(-5))).toBeGreaterThan(0);
    expect(projectionEvents.some((event: any) => event?.v2_event_type === 'team_created')).toBeTruthy();
    expect(projectionEvents.some((event: any) => event?.v2_event_type === 'task_created')).toBeTruthy();
    expect(projectionEvents.some((event: any) => event?.v2_event_type === 'mailbox_sent')).toBeTruthy();
    expect(projectionEvents.some((event: any) => event?.v2_event_type === 'tool_call_started')).toBeTruthy();
    expect(
      projectionEvents.some((event: any) => ['tool_call_completed', 'tool_call_failed'].includes(String(event?.v2_event_type || ''))),
    ).toBeTruthy();
    expect(projectionEvents.some((event: any) => event?.v2_event_type === 'run_completed')).toBeTruthy();
    const terminalProjection = projectionEvents[projectionEvents.length - 1]?.projection;
    expect(terminalProjection?.model?.effective).toBe(SELECTED_MODEL);
    expect(terminalProjection?.final_output?.content || '').toContain('LIVE_CLICK_SUBAGENT_OK');
    const firstRawEvents = readV2RawEventsFromRedis(firstRunId);
    const projectedToolCalls = values(terminalProjection?.tool_calls || firstStatus.tool_calls);
    expectToolCallLifecycleEvidence(firstRawEvents, projectedToolCalls);

    await returnToMainViewIfNeeded(page);
    const visibleToolCall = projectedToolCalls
      .slice()
      .reverse()
      .find((toolCall: any) => toolCall?.tool_name && (toolCall?.result_summary || toolCall?.error))
      || projectedToolCalls.find((toolCall: any) => toolCall?.tool_name) as any;
    const visibleToolName = normalizeToolNameForUi(visibleToolCall?.tool_name);
    expect(visibleToolName, JSON.stringify(projectedToolCalls)).toBeTruthy();
    const selectedRow = page.locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${liveClickSubtaskId}"]`).first();
    await expect(selectedRow).toBeVisible({ timeout: 30000 });
    await selectedRow.evaluate((element) => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
    await expect(page.locator('[data-testid="thread-assistant-message-group"]').last()).toContainText('LIVE_CLICK_SUBAGENT_OK', { timeout: 60000 });
    await expect(page.locator('[data-testid="thread-assistant-message-group"]').last()).toContainText(idleDraftMarker, { timeout: 60000 });
    await expect(
      page.locator(`[data-testid="shadow-clone-selected-subagent-tool-panel"][data-subtask-id="${liveClickSubtaskId}"]`),
    ).toContainText(/subagent[_-]final[_-]result|Task Completion|Task Complete/i, { timeout: 60000 });
    await returnToMainViewIfNeeded(page);

    const previousRequestBody = capturedStart.requestBody;
    capturedStart.requestBody = undefined;
    capturedStart.responseBody = undefined;

    await sendPrompt(
      page,
      `E2E Shadow Clone V2 same-thread wake ${unique}: 请复用并唤醒上一轮的 teammate-1（不要创建无关冷启动团队）。这是给 idle teammate 的进一步反馈：${idleFeedbackMarker}。请让同一个 teammate 基于上一轮已经完成的交付继续改进；不要在新的任务描述里复制上一轮完整交付标记。子任务结果必须包含 ${idleFeedbackMarker}、${idleImprovedMarker} 和 WAKE_REUSED_TEAMMATE_OK。主 agent 最终回答必须包含 WAKE_REUSED_TEAMMATE_OK、${idleFeedbackMarker}、${idleImprovedMarker}。`,
    );
    await expect.poll(() => capturedStart.requestBody ? 'captured' : '', {
      timeout: 30000,
      message: 'follow-up agent/start request should be captured',
    }).toBe('captured');
    expect(capturedStart.requestBody?.model_name).toBe(previousRequestBody?.model_name);
    expect(capturedStart.requestBody?.shadow_clone_mode).toBe('on');

    const secondRunId = String(capturedStart.responseBody?.agent_run_id || await waitForLatestRunId(page, threadId, authToken, firstRunId));
    expect(secondRunId).not.toBe(firstRunId);
    await waitForRunToFinish(page, threadId, authToken, secondRunId);
    const secondStatus = await waitForShadowCloneStatus(page, authToken, secondRunId);
    const secondRunMetadata = (await getAgentRuns(page, threadId, authToken))
      .find((candidate: any) => (candidate.agent_run_id || candidate.id) === secondRunId);
    expectRegularSupervisorMetadata(secondRunMetadata);

    const secondMembers = secondStatus.team?.members || {};
    const secondTeammate = secondMembers['teammate-1'] || values(secondMembers).find((member: any) => member?.agent_id === stableTeammateAgentId) as any;
    expect(secondTeammate?.agent_id).toBe(stableTeammateAgentId);
    const secondStatusJson = JSON.stringify(secondStatus);
    expect(secondStatusJson).toContain('WAKE_REUSED_TEAMMATE_OK');
    expect(secondStatusJson).toContain(idleFeedbackMarker);
    expect(secondStatusJson).toContain(idleImprovedMarker);
    expect(secondStatus.final_output?.content || '').toContain(idleFeedbackMarker);
    expect(secondStatus.final_output?.content || '').toContain(idleImprovedMarker);
    expect(values(secondStatus.agents).some((agent: any) => agent?.status === 'idle' || agent?.wake_reason)).toBeTruthy();

    const secondImprovedTask = values(secondStatus.tasks).find((task: any) => JSON.stringify(task).includes(idleImprovedMarker)) as any;
    expect(secondImprovedTask?.id || secondImprovedTask?.task_id, JSON.stringify(secondStatus.tasks)).toBeTruthy();
    const secondTaskDescriptions = values(secondStatus.tasks)
      .map((task: any) => String(task?.description || task?.task_description || ''))
      .join('\n');
    expect(secondTaskDescriptions).not.toContain(idleDraftMarker);
    const secondImprovedSubtaskId = String(secondImprovedTask.id || secondImprovedTask.task_id);
    const secondImprovedRow = page.locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${secondImprovedSubtaskId}"]`).first();
    await expect(secondImprovedRow).toBeVisible({ timeout: 60000 });
    await secondImprovedRow.evaluate((element) => {
      element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    const secondSelectedPanel = page.locator('[data-testid="thread-assistant-message-group"]').last();
    await expect(secondSelectedPanel).toContainText('WAKE_REUSED_TEAMMATE_OK', { timeout: 60000 });
    await expect(secondSelectedPanel).toContainText(idleFeedbackMarker, { timeout: 60000 });
    await expect(secondSelectedPanel).toContainText(idleImprovedMarker, { timeout: 60000 });
  });

  test('verifies main-agent broadcast reaches all five subagents through inbox-only replay in UI panels', async ({ page }) => {
    test.setTimeout(900000);

    await waitForBackendHealth(page);
    const authToken = await login(page);

    const threadResponse = await page.request.post(`${BACKEND_URL}/threads`, {
      headers: { Authorization: `Bearer ${authToken}` },
      form: { name: `E2E Shadow Clone V2 broadcast ${Date.now()}` },
      timeout: 120000,
    });
    expect(threadResponse.ok(), `create thread ${threadResponse.status()}`).toBeTruthy();
    const threadPayload = await threadResponse.json() as { thread_id: string; project_id?: string };
    const threadId = threadPayload.thread_id;
    const projectId = threadPayload.project_id || '3ba94525-cf0e-4caf-9e24-f91ea470d3b9';

    const unique = Date.now();
    const broadcastSecret = `E2E_BROADCAST_INBOX_ONLY_SECRET_${unique}`;
    const secretSuffix = broadcastSecret.slice(-6);
    const consumedMarker = `CONSUMED_${secretSuffix}`;
    const mainMarker = `E2E_BROADCAST_MAIN_DONE_${unique}`;
    const agentNames = Array.from({ length: 5 }, (_unused, index) => `broadcast-agent-${index + 1}`);
    const prompt = [
      `E2E Shadow Clone V2 five-agent broadcast acceptance ${unique}.`,
      '必须严格使用 Shadow Clone V2 的 Team/Task/SendMessage 工具，不要只在文本里描述计划。',
      `创建五个 teammate，名字必须正好是 ${agentNames.join(', ')}。`,
      `在创建任务之后，主/team-lead agent 必须调用 SendMessage 广播给所有 teammate：recipient="*", message_type="broadcast", text 必须包含这个 inbox-only secret：${broadcastSecret}。`,
      '给每个 teammate 的任务描述禁止包含上述 inbox-only secret，也不要直接写出最终 consumed marker；任务描述只允许说：读取 inbox/broadcast，把收到的 secret 的最后 6 个字符拼成 CONSUMED_<last6> 并输出，同时写入自己的 agent 名字。',
      `最终 main agent 回答必须包含 ${mainMarker}，并汇总五个 teammate 的输出；不要在没有 teammate 输出的情况下伪造结果。`,
      '保持输出简短，不要执行外部长任务。',
    ].join('\n');

    const messageResponse = await page.request.post(`${BACKEND_URL}/threads/${threadId}/messages`, {
      headers: { Authorization: `Bearer ${authToken}` },
      data: { type: 'user', content: prompt, is_llm_message: true },
      timeout: 120000,
    });
    expect(messageResponse.ok(), `create message ${messageResponse.status()}`).toBeTruthy();

    const startResponse = await page.request.post(`${BACKEND_URL}/thread/${threadId}/agent/start`, {
      headers: { Authorization: `Bearer ${authToken}` },
      data: {
        stream: false,
        model_name: SELECTED_MODEL,
        shadow_clone_mode: 'on',
        enable_context_manager: false,
      },
      timeout: 120000,
    });
    expect(startResponse.ok(), `start agent ${startResponse.status()}`).toBeTruthy();
    const startPayload = await startResponse.json() as { agent_run_id: string };
    const runId = startPayload.agent_run_id;

    await page.goto(`/projects/${projectId}/thread/${threadId}`, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    });
    const preBroadcastRows = page.locator('[data-testid="shadow-clone-monitor-row"]');
    await expect(preBroadcastRows).toHaveCount(5, { timeout: 120000 });
    const preBroadcastVisibleAgentNames = (await preBroadcastRows.evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-agent-name') || '').filter(Boolean),
    )).sort();
    expect(preBroadcastVisibleAgentNames).toEqual(agentNames.slice().sort());

    await waitForRunToFinish(page, threadId, authToken, runId);
    const status = await waitForShadowCloneStatus(page, authToken, runId);
    const runMetadata = (await getAgentRuns(page, threadId, authToken))
      .find((candidate: any) => (candidate.agent_run_id || candidate.id) === runId);
    expectRegularSupervisorMetadata(runMetadata);

    const statusJson = JSON.stringify(status);
    expect(status.final_output?.content || '').toContain(mainMarker);
    expect(statusJson).toContain(consumedMarker);
    expect(statusJson).toContain(broadcastSecret);

    const statusTasks = values(status.tasks) as any[];
    const taskDescriptions = statusTasks.map((task: any) => String(task?.description || task?.task_description || '')).join('\n');
    expect(taskDescriptions).not.toContain(broadcastSecret);
    expect(taskDescriptions).not.toContain(consumedMarker);

    const agentSubtaskIds = new Map<string, string>();
    for (const agentName of agentNames) {
      const task = statusTasks.find((candidate: any) => String(candidate?.agent_name || candidate?.metadata?.agent_name || '').includes(agentName));
      expect(task?.id || task?.task_id, `missing task for ${agentName}: ${JSON.stringify(status.tasks)}`).toBeTruthy();
      agentSubtaskIds.set(agentName, String(task.id || task.task_id));
    }

    const statusMessages = values(status.messages) as any[];
    const broadcastMessages = statusMessages.filter((message: any) =>
      String(message?.message_type || '').toLowerCase() === 'broadcast'
      && String(message?.text || '').includes(broadcastSecret),
    );
    expect(broadcastMessages.length, JSON.stringify(statusMessages)).toBeGreaterThanOrEqual(5);
    for (const agentName of agentNames) {
      expect(
        broadcastMessages.some((message: any) => String(message?.recipient || '').includes(agentName)),
        `status broadcast message should target ${agentName}`,
      ).toBeTruthy();
    }

    const streamEvents = await replayRunStream(page, authToken, runId);
    const projectionEvents = streamEvents.filter((event: any) => event?.type === 'shadow_clone_v2_projection');
    expect(projectionEvents.some((event: any) => event?.v2_event_type === 'mailbox_sent')).toBeTruthy();
    const replayJson = JSON.stringify(projectionEvents);
    expect(replayJson).toContain(broadcastSecret);
    for (const agentName of agentNames) {
      expect(replayJson).toContain(agentName);
    }

    const rawEvents = readV2RawEventsFromRedis(runId);
    const teamCreatedIndex = rawEvents.findIndex((event) => event.type === 'team_created');
    const firstBroadcastIndex = rawEvents.findIndex((event) => {
      const payload = rawPayload(event);
      return event.type === 'mailbox_sent'
        && String(payload.message_type || '').toLowerCase() === 'broadcast';
    });
    expect(teamCreatedIndex, JSON.stringify(rawEvents.slice(0, 10))).toBeGreaterThanOrEqual(0);
    expect(firstBroadcastIndex, JSON.stringify(rawEvents.slice(0, 20))).toBeGreaterThan(teamCreatedIndex);
    const teamPayload = rawPayload(rawEvents[teamCreatedIndex]);
    const createdMemberNames = Array.isArray(teamPayload.members)
      ? teamPayload.members.map((member: any) => String(member?.agent_name || '')).filter(Boolean)
      : [];
    expect(createdMemberNames.sort()).toEqual(agentNames.slice().sort());
    expect(createdMemberNames.sort()).toEqual(preBroadcastVisibleAgentNames);

    const rawBroadcastEvents = rawEvents.filter((event) => {
      const payload = rawPayload(event);
      return event.type === 'mailbox_sent'
        && String(payload.message_type || '').toLowerCase() === 'broadcast'
        && String(payload.text || '').includes(broadcastSecret);
    });
    expect(rawBroadcastEvents.length, JSON.stringify(rawEvents.slice(-30))).toBeGreaterThanOrEqual(5);
    for (const agentName of preBroadcastVisibleAgentNames) {
      expect(
        rawBroadcastEvents.some((event) => String(rawPayload(event).recipient || '').includes(agentName)),
        `raw broadcast event should target pre-broadcast visible UI identity ${agentName}`,
      ).toBeTruthy();
    }

    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => undefined);

    const mainAssistantGroups = page.locator('[data-testid="thread-assistant-message-group"]');
    await expect(mainAssistantGroups.last()).toContainText(mainMarker, { timeout: 60000 });
    await expect(mainAssistantGroups.last()).toContainText(consumedMarker, { timeout: 60000 });

    const monitorRows = page.locator('[data-testid="shadow-clone-monitor-row"]');
    await expect(monitorRows).toHaveCount(5, { timeout: 60000 });

    for (const agentName of agentNames) {
      const subtaskId = agentSubtaskIds.get(agentName);
      expect(subtaskId, `missing subtask id for ${agentName}`).toBeTruthy();
      await page
        .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${subtaskId}"]`)
        .first()
        .evaluate((element) => {
          element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        });
      await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
      const selectedPanel = page.locator('[data-testid="thread-assistant-message-group"]').last();
      await expect(selectedPanel).toContainText(agentName, { timeout: 60000 });
      await expect(selectedPanel).toContainText(consumedMarker, { timeout: 60000 });
      await expect(selectedPanel).toContainText(broadcastSecret, { timeout: 60000 });
      await returnToMainViewIfNeeded(page);
      await expect(monitorRows).toHaveCount(5, { timeout: 30000 });
    }
  });


  test('verifies real peer communication is replayed in sender recipient and main UI panels', async ({ page }) => {
    test.setTimeout(900000);

    await waitForBackendHealth(page);
    const authToken = await login(page);

    const threadResponse = await page.request.post(`${BACKEND_URL}/threads`, {
      headers: { Authorization: `Bearer ${authToken}` },
      form: { name: `E2E Shadow Clone V2 peer communication ${Date.now()}` },
      timeout: 120000,
    });
    expect(threadResponse.ok(), `create thread ${threadResponse.status()}`).toBeTruthy();
    const threadPayload = await threadResponse.json() as { thread_id: string; project_id?: string };
    const threadId = threadPayload.thread_id;
    const projectId = threadPayload.project_id || '3ba94525-cf0e-4caf-9e24-f91ea470d3b9';

    const unique = Date.now();
    const aToBSecret = `P2P_FROM_A_TO_B_INBOX_ONLY_${unique}`;
    const bConsumedMarker = `P2P_B_CONSUMED_A_MESSAGE_${unique}`;
    const bToMainMarker = `P2P_B_TO_MAIN_${unique}`;
    const mainMarker = `P2P_MAIN_DONE_${unique}`;
    const prompt = [
      `E2E Shadow Clone V2 peer communication acceptance ${unique}.`,
      '必须严格使用 Shadow Clone V2 的 Team/Task/SendMessage 工具，不要只在文本里描述计划。',
      '创建两个 teammate，名字必须正好是 comm-agent-a 和 comm-agent-b。',
      `给 comm-agent-a 创建任务：通过真实 send_message tool 把 inbox-only secret ${aToBSecret} 发给 comm-agent-b，任务结果写出自己已发送给 comm-agent-b。`,
      '给 comm-agent-b 创建任务，必须 blocked_by comm-agent-a；B 的任务描述禁止包含 A 发给 B 的 inbox-only secret，也禁止直接包含 B 的 consumed marker；只允许说：读取 inbox 中来自 A 的消息，收到后输出 P2P_B_CONSUMED_A_MESSAGE_<本次 unique>，并通过真实 send_message tool 向 facilitator/main 发送 B_TO_MAIN marker。',
      `B 收到 inbox 后必须输出 ${bConsumedMarker}，并向 facilitator/main 发送 ${bToMainMarker}。`,
      `最终 main agent 回答必须包含 ${mainMarker}、${bConsumedMarker} 和 ${bToMainMarker}。`,
      '保持输出简短，不要执行外部长任务。',
    ].join('\n');

    const messageResponse = await page.request.post(`${BACKEND_URL}/threads/${threadId}/messages`, {
      headers: { Authorization: `Bearer ${authToken}` },
      data: { type: 'user', content: prompt, is_llm_message: true },
      timeout: 120000,
    });
    expect(messageResponse.ok(), `create message ${messageResponse.status()}`).toBeTruthy();

    const startResponse = await page.request.post(`${BACKEND_URL}/thread/${threadId}/agent/start`, {
      headers: { Authorization: `Bearer ${authToken}` },
      data: {
        stream: false,
        model_name: SELECTED_MODEL,
        shadow_clone_mode: 'on',
        enable_context_manager: false,
      },
      timeout: 120000,
    });
    expect(startResponse.ok(), `start agent ${startResponse.status()}`).toBeTruthy();
    const startPayload = await startResponse.json() as { agent_run_id: string };
    const runId = startPayload.agent_run_id;

    await waitForRunToFinish(page, threadId, authToken, runId);
    const status = await waitForShadowCloneStatus(page, authToken, runId);
    const runMetadata = (await getAgentRuns(page, threadId, authToken))
      .find((candidate: any) => (candidate.agent_run_id || candidate.id) === runId);
    expectRegularSupervisorMetadata(runMetadata);

    const statusTasks = values(status.tasks) as any[];
    const taskA = statusTasks.find((task: any) => String(task?.agent_name || task?.metadata?.agent_name || '').includes('comm-agent-a')) as any;
    const taskB = statusTasks.find((task: any) => String(task?.agent_name || task?.metadata?.agent_name || '').includes('comm-agent-b')) as any;
    expect(taskA?.id || taskA?.task_id, JSON.stringify(status.tasks)).toBeTruthy();
    expect(taskB?.id || taskB?.task_id, JSON.stringify(status.tasks)).toBeTruthy();
    const taskASubtaskId = String(taskA.id || taskA.task_id);
    const taskBSubtaskId = String(taskB.id || taskB.task_id);
    const taskBDescription = String(taskB?.description || taskB?.task_description || '');
    expect(taskBDescription).not.toContain(aToBSecret);
    expect(taskBDescription).not.toContain(bConsumedMarker);

    expect(status.final_output?.content || '').toContain(mainMarker);
    expect(status.final_output?.content || '').toContain(bConsumedMarker);
    expect(status.final_output?.content || '').toContain(bToMainMarker);

    const statusMessages = values(status.messages) as any[];
    expect(statusMessages.some((message: any) =>
      String(message?.sender || '').includes('comm-agent-a')
      && String(message?.recipient || '').includes('comm-agent-b')
      && String(message?.text || '').includes(aToBSecret),
    )).toBeTruthy();
    expect(statusMessages.some((message: any) =>
      String(message?.sender || '').includes('comm-agent-b')
      && (String(message?.recipient || '').includes('facilitator') || String(message?.recipient || '').includes('team-lead'))
      && String(message?.text || '').includes(bToMainMarker),
    )).toBeTruthy();

    const streamEvents = await replayRunStream(page, authToken, runId);
    const projectionEvents = streamEvents.filter((event: any) => event?.type === 'shadow_clone_v2_projection');
    const replayJson = JSON.stringify(projectionEvents);
    expect(replayJson).toContain(aToBSecret);
    expect(replayJson).toContain(bToMainMarker);

    const rawEvents = readV2RawEventsFromRedis(runId);
    const rawMessages = rawEvents.filter((event) => event.type === 'mailbox_sent').map(rawPayload);
    expect(rawMessages.some((payload) =>
      String(payload.sender || '').includes('comm-agent-a')
      && String(payload.recipient || '').includes('comm-agent-b')
      && String(payload.text || '').includes(aToBSecret),
    )).toBeTruthy();
    expect(rawMessages.some((payload) =>
      String(payload.sender || '').includes('comm-agent-b')
      && (String(payload.recipient || '').includes('facilitator') || String(payload.recipient || '').includes('team-lead'))
      && String(payload.text || '').includes(bToMainMarker),
    )).toBeTruthy();

    await page.goto(`/projects/${projectId}/thread/${threadId}`, {
      waitUntil: 'domcontentloaded',
      timeout: 60000,
    });
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => undefined);

    const mainAssistantGroups = page.locator('[data-testid="thread-assistant-message-group"]');
    await expect(mainAssistantGroups.last()).toContainText(mainMarker, { timeout: 60000 });
    await expect(mainAssistantGroups.last()).toContainText(bConsumedMarker, { timeout: 60000 });
    await expect(mainAssistantGroups.last()).toContainText(bToMainMarker, { timeout: 60000 });

    const monitorRows = page.locator('[data-testid="shadow-clone-monitor-row"]');
    await expect(monitorRows).toHaveCount(2, { timeout: 60000 });

    await page
      .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${taskASubtaskId}"]`)
      .first()
      .evaluate((element) => {
        element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      });
    await expect(page.locator('body')).toContainText(/返回主视图|Back to main view/, { timeout: 30000 });
    const senderPanel = page.locator('[data-testid="thread-assistant-message-group"]').last();
    await expect(senderPanel).toContainText('comm-agent-b', { timeout: 60000 });
    await expect(senderPanel).toContainText(aToBSecret, { timeout: 60000 });

    await returnToMainViewIfNeeded(page);
    await page
      .locator(`[data-testid="shadow-clone-monitor-row"][data-subtask-id="${taskBSubtaskId}"]`)
      .first()
      .evaluate((element) => {
        element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      });
    const recipientPanel = page.locator('[data-testid="thread-assistant-message-group"]').last();
    await expect(recipientPanel).toContainText(aToBSecret, { timeout: 60000 });
    await expect(recipientPanel).toContainText(bConsumedMarker, { timeout: 60000 });
    await expect(recipientPanel).toContainText(bToMainMarker, { timeout: 60000 });
  });


  test('verifies regular-supervisor live parallel batching and blocked layering from V2 event log', async ({ page }) => {
    test.setTimeout(900000);

    await waitForBackendHealth(page);
    const authToken = await login(page);

    const threadResponse = await page.request.post(`${BACKEND_URL}/threads`, {
      headers: { Authorization: `Bearer ${authToken}` },
      form: { name: `E2E Shadow Clone V2 live parallelism ${Date.now()}` },
      timeout: 120000,
    });
    expect(threadResponse.ok(), `create thread ${threadResponse.status()}`).toBeTruthy();
    const threadPayload = await threadResponse.json() as { thread_id: string };
    const threadId = threadPayload.thread_id;

    const unique = Date.now();
    const prompt = [
      `E2E Shadow Clone V2 live concurrency acceptance ${unique}.`,
      '请严格通过 Shadow Clone V2 Team/Task/SendMessage 工具执行，不要只在文本中描述计划。',
      '1. 创建或使用 10 个 teammate，名字必须是 e2e-agent-1 到 e2e-agent-10。',
      '2. 创建 12 个互相独立、没有 blocked_by 的任务，ID 或 subject 中包含 independent-1 到 independent-12；将它们 round-robin 分给这 10 个 teammate。',
      '3. 另外创建 2 个多层依赖任务：dependent-after-1 必须 blocked_by independent-1；dependent-after-2 必须 blocked_by dependent-after-1。dependent-after-2 只能在 dependent-after-1 完成后执行。',
      '4. 每个 worker 只返回很短的完成标记，例如 TASK_OK_<编号>，不要做外部长任务。',
      '5. 最终 main agent 回答必须包含 E2E_PARALLEL_BATCH_OK 和 E2E_BLOCKED_LAYER_OK。',
    ].join('\n');

    const messageResponse = await page.request.post(`${BACKEND_URL}/threads/${threadId}/messages`, {
      headers: { Authorization: `Bearer ${authToken}` },
      data: { type: 'user', content: prompt, is_llm_message: true },
      timeout: 120000,
    });
    expect(messageResponse.ok(), `create message ${messageResponse.status()}`).toBeTruthy();

    const startResponse = await page.request.post(`${BACKEND_URL}/thread/${threadId}/agent/start`, {
      headers: { Authorization: `Bearer ${authToken}` },
      data: {
        stream: false,
        model_name: SELECTED_MODEL,
        shadow_clone_mode: 'on',
        enable_context_manager: false,
      },
      timeout: 120000,
    });
    expect(startResponse.ok(), `start agent ${startResponse.status()}`).toBeTruthy();
    const startPayload = await startResponse.json() as { agent_run_id: string };
    const runId = startPayload.agent_run_id;

    await waitForRunToFinish(page, threadId, authToken, runId);
    const runMetadata = (await getAgentRuns(page, threadId, authToken))
      .find((candidate: any) => (candidate.agent_run_id || candidate.id) === runId);
    expectRegularSupervisorMetadata(runMetadata);

    const status = await waitForShadowCloneStatus(page, authToken, runId);
    expect(status.final_output?.content || '').toContain('E2E_PARALLEL_BATCH_OK');
    expect(status.final_output?.content || '').toContain('E2E_BLOCKED_LAYER_OK');

    const rawEvents = readV2RawEventsFromRedis(runId);
    const evidence = analyzeV2SchedulerEvidence(rawEvents);
    expect(evidence.createdTasks).toBeGreaterThanOrEqual(14);
    expect(evidence.independentTasks).toBeGreaterThanOrEqual(12);
    expect(evidence.claimedTasks).toBeGreaterThan(10);
    expect(evidence.doneTasks).toBeGreaterThanOrEqual(14);
    expect(evidence.claimsBeforeFirstDone).toBeGreaterThanOrEqual(5);
    expect(evidence.maxActive).toBeLessThanOrEqual(10);
    expect(evidence.claimedTasks).toBeGreaterThan(evidence.maxActive);
    expect(evidence.dependentTask).toContain('dependent-after-1');
    expect(evidence.blocker).toBe('independent-1');
    expect(new Date(evidence.dependentClaimedAt).getTime()).toBeGreaterThan(
      new Date(evidence.blockerDoneAt).getTime(),
    );
    const requiredEdges = [
      ['dependent-after-1', 'independent-1'],
      ['dependent-after-2', 'dependent-after-1'],
    ];
    for (const [taskId, blocker] of requiredEdges) {
      const edge = evidence.dependencyEdges.find(
        (candidate) => candidate.taskId === taskId && candidate.blocker === blocker,
      );
      expect(edge, `missing dependency edge ${blocker} -> ${taskId}`).toBeTruthy();
      expect(edge?.blockerDoneAt, `missing blocker completion time for ${blocker}`).toBeTruthy();
      expect(edge?.taskClaimedAt, `missing claim time for ${taskId}`).toBeTruthy();
      expect(new Date(edge!.taskClaimedAt).getTime()).toBeGreaterThan(
        new Date(edge!.blockerDoneAt).getTime(),
      );
    }
  });
});
