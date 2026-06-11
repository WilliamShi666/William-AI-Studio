import { expect, test, type Page, type Route } from '@playwright/test';

const TEST_EMAIL = process.env.E2E_TEST_EMAIL || 'codex.test+ssr1@local.dev';
const TEST_PASSWORD = process.env.E2E_TEST_PASSWORD || 'CodexTest!12345';
const BASE_URL = process.env.BASE_URL || 'http://localhost';
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost/api';

const assistantMarkdown = (page: Page) => page.locator('.chat-markdown').filter({ hasNotText: /^$/ });
const chatInput = (page: Page) => page.locator('textarea').last();
const sendButton = (page: Page) => page.locator('button[type="submit"]').last();
const terminalRunStatuses = new Set(['completed', 'stopped', 'error', 'failed']);


async function installBackendProxy(page: Page) {
  const targetBase = new URL(BACKEND_URL);
  const proxyRequest = async (route: Route) => {
    const request = route.request();
    const sourceUrl = new URL(request.url());
    const targetUrl = `${targetBase.origin}${sourceUrl.pathname}${sourceUrl.search}`;

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

    const response = await page.request.fetch(targetUrl, {
      method: request.method(),
      headers: request.headers(),
      data: request.postDataBuffer() ?? undefined,
      timeout: 120000,
    });

    await route.fulfill({
      status: response.status(),
      headers: {
        ...response.headers(),
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
        'access-control-allow-headers': 'authorization,content-type',
      },
      body: await response.body(),
    });
  };

  await page.route('http://localhost/api/**', proxyRequest);
  await page.route('http://localhost:8081/api/**', proxyRequest);
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

  await page.addInitScript(({ sessionValue, tokenValue }) => {
    localStorage.setItem('auth_session', sessionValue);
    localStorage.setItem('auth_token', tokenValue);
  }, {
    sessionValue: JSON.stringify(session),
    tokenValue: authData.access_token,
  });

  return authData.access_token;
}

async function waitForBackendHealth(page: Page) {
  const healthUrl = `${BACKEND_URL}/health`;
  const response = await page.request.get(healthUrl, { timeout: 10000 });
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

async function waitForLatestRunToFinish(page: Page, threadId: string, authToken: string) {
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`${BACKEND_URL}/thread/${threadId}/agent-runs`, {
          headers: {
            Authorization: `Bearer ${authToken}`,
          },
          timeout: 15000,
        });
        if (!response.ok()) {
          return `http-${response.status()}`;
        }

        const payload = await response.json();
        const runs = Array.isArray(payload)
          ? payload
          : Array.isArray(payload?.agent_runs)
            ? payload.agent_runs
            : Array.isArray(payload?.data)
              ? payload.data
              : [];
        const latestRun = runs[0];
        const status = latestRun?.status;
        return terminalRunStatuses.has(status) ? status : status || 'missing';
      },
      {
        timeout: 120000,
        intervals: [1000, 1500, 2000, 3000, 5000],
        message: `latest agent run should finish for thread ${threadId}`,
      },
    )
    .toMatch(/^(completed|stopped|error|failed)$/);
}

async function waitUntilComposerCanSendNextMessage(page: Page) {
  await expect
    .poll(
      async () => {
        const button = sendButton(page);
        const label = await button.evaluate((element) => element.innerHTML);
        const inputValue = await chatInput(page).inputValue();
        return label.includes('animate-spin') || label.includes('rounded-sm bg-current')
          ? 'busy'
          : inputValue.trim()
            ? ((await button.isEnabled()) ? 'ready' : 'disabled-with-text')
            : 'ready-empty';
      },
      {
        timeout: 30000,
        intervals: [500, 1000, 1500],
        message: 'composer submit button should return from stop/loading mode to send mode',
      },
    )
    .toMatch(/^ready/);
}

async function sendMessageAndWaitForAssistant(page: Page, authToken: string, message: string, expectedAssistantCount: number) {
  await expect(chatInput(page)).toBeVisible({ timeout: 30000 });
  await chatInput(page).fill(message);
  await waitUntilComposerCanSendNextMessage(page);
  await sendButton(page).click();

  await expect(page.getByText(message, { exact: false })).toBeVisible({ timeout: 15000 });
  await expect
    .poll(async () => await assistantMarkdown(page).count(), {
      timeout: 90000,
      intervals: [1000, 1500, 2000, 3000, 5000],
      message: `assistant reply should be visible after: ${message}`,
    })
    .toBeGreaterThanOrEqual(expectedAssistantCount);

  const threadId = await getThreadIdFromUrl(page);
  await waitForLatestRunToFinish(page, threadId, authToken);
  await waitUntilComposerCanSendNextMessage(page);
}

test.describe('thread agent reply visibility', () => {
  test('shows agent replies after the first, second, and third user messages', async ({ page }) => {
    test.setTimeout(240000);

    const consoleMessages: string[] = [];
    page.on('console', (msg) => {
      const text = msg.text();
      if (msg.type() === 'error' || text.includes('[useAgentStream]') || text.includes('[useThreadData]')) {
        consoleMessages.push(`[${msg.type()}] ${text}`);
      }
    });
    page.on('pageerror', (error) => {
      consoleMessages.push(`[pageerror] ${error.message}`);
    });

    await waitForBackendHealth(page);
    await installBackendProxy(page);
    const authToken = await login(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await enterRoysAlphaIfNeeded(page);

    const unique = Date.now();
    await sendMessageAndWaitForAssistant(
      page,
      authToken,
      `E2E ${unique} message 1: Reply with exactly: alpha-one`,
      1,
    );

    await expect(page).toHaveURL(/\/projects\/[^/]+\/thread\/[^/]+/, { timeout: 30000 });

    await sendMessageAndWaitForAssistant(
      page,
      authToken,
      `E2E ${unique} message 2: Reply with exactly: beta-two`,
      2,
    );

    await sendMessageAndWaitForAssistant(
      page,
      authToken,
      `E2E ${unique} message 3: Reply with exactly: gamma-three`,
      3,
    );

    const countBeforeReload = await assistantMarkdown(page).count();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect
      .poll(async () => await assistantMarkdown(page).count(), {
        timeout: 30000,
        message: 'assistant replies should still be present after reload',
      })
      .toBeGreaterThanOrEqual(countBeforeReload);

    console.log(`Captured relevant console messages: ${consoleMessages.length}`);
    for (const message of consoleMessages.slice(-20)) {
      console.log(message);
    }
  });
});
