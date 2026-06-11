import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const modelSelectionSource = readFileSync(
  path.join(testDir, '_use-model-selection.ts'),
  'utf8',
);
const menuSource = readFileSync(
  path.join(testDir, 'unified-config-menu.tsx'),
  'utf8',
);
const shadowCloneToggleSource = readFileSync(
  path.join(testDir, 'shadow-clone-toggle.tsx'),
  'utf8',
);
const apiSource = readFileSync(
  path.join(testDir, '../../../lib/api.ts'),
  'utf8',
);

const backendSupportedRequestedModels = [
  'openrouter/moonshotai/kimi-k2.6',
  'openrouter/xiaomi/mimo-v2.5-pro',
  'openrouter/deepseek/deepseek-v4-pro',
  'openrouter/deepseek/deepseek-v4-flash',
  'kimi-k2.6',
  'kimi-k2.5',
  'deepseek-v4-pro-high',
  'deepseek-v4-pro-max',
  'deepseek-v4-flash-high',
  'deepseek-v4-flash-max',
];

const expectedChatPickerVisibleModels = [
  'deepseek-v4-pro-high',
  'deepseek-v4-pro-max',
  'deepseek-v4-flash-high',
  'deepseek-v4-flash-max',
  'kimi-k2.6',
  'kimi-k2.5',
  'openrouter/xiaomi/mimo-v2.5-pro',
];

const hiddenFromChatPickerModels = [
  'openrouter/deepseek/deepseek-v4-pro',
  'openrouter/deepseek/deepseek-v4-flash',
  'openrouter/moonshotai/kimi-k2.6',
  'openrouter/moonshotai/kimi-k2.5',
  'openrouter/google/gemini-3.1-pro-preview',
  'openrouter/qwen/qwen3.6-plus',
  'openrouter/z-ai/glm-5.1',
  'mystery-model',
];

const escapedModelId = (modelId) => modelId.replaceAll('/', '\\/').replaceAll('.', '\\.');
const quotedModelIdPattern = (modelId) => new RegExp(`'${escapedModelId(modelId)}'`);

test('canonical model mapping adds new models without breaking qwen3.5-plus compatibility', () => {
  assert.match(
    modelSelectionSource,
    /'glm-5\.1': 'openrouter\/z-ai\/glm-5\.1'/,
    'expected glm-5.1 alias to canonicalize to the new OpenRouter model id',
  );

  assert.match(
    modelSelectionSource,
    /'qwen3\.6-plus': 'openrouter\/qwen\/qwen3\.6-plus'/,
    'expected qwen3.6-plus alias to canonicalize to the new OpenRouter model id',
  );

  assert.match(
    modelSelectionSource,
    /'openrouter\/qwen\/qwen3\.5-plus': 'dashscope\/qwen3\.5-plus'/,
    'expected qwen3.5-plus OpenRouter alias to preserve DashScope backend compatibility',
  );

  assert.match(
    modelSelectionSource,
    /'dashscope\/qwen3\.5-plus': 'dashscope\/qwen3\.5-plus'/,
    'expected qwen3.5-plus canonical target to remain DashScope',
  );
});

test('curated inventory keeps hidden models selectable while adding new visible models', () => {
  assert.match(
    modelSelectionSource,
    /id: 'openrouter\/z-ai\/glm-5\.1'/,
    'expected curated inventory to include GLM 5.1',
  );

  assert.match(
    modelSelectionSource,
    /id: 'openrouter\/qwen\/qwen3\.6-plus'/,
    'expected curated inventory to include Qwen 3.6 Plus',
  );

  assert.match(
    modelSelectionSource,
    /id: 'openrouter\/z-ai\/glm-4\.7'/,
    'expected hidden GLM 4.7 to remain in shared curated inventory',
  );

  assert.match(
    modelSelectionSource,
    /id: 'openrouter\/minimax\/minimax-m2\.1'/,
    'expected hidden Minimax M2.1 to remain in shared curated inventory',
  );

  assert.match(
    modelSelectionSource,
    /id: 'dashscope\/qwen3\.5-plus'/,
    'expected hidden qwen3.5-plus to remain in shared curated inventory',
  );
});

test('chat input picker uses a short positive allowlist in the requested order', () => {
  assert.match(
    menuSource,
    /const CHAT_MODEL_PICKER_VISIBLE_MODEL_IDS = \[/,
    'expected chat picker to define a positive allowlist instead of exposing the shared backend inventory',
  );

  const allowlistMatch = menuSource.match(
    /const CHAT_MODEL_PICKER_VISIBLE_MODEL_IDS = \[([\s\S]*?)\];/,
  );
  assert.ok(allowlistMatch, 'expected to find the chat picker visible allowlist block');

  const actualVisibleModels = Array.from(
    allowlistMatch[1].matchAll(/'([^']+)'/g),
    match => match[1],
  );

  assert.deepEqual(
    actualVisibleModels,
    expectedChatPickerVisibleModels,
    'expected chat picker visible models to exactly match the short user-facing list',
  );

  for (const modelId of hiddenFromChatPickerModels) {
    assert.notEqual(
      actualVisibleModels.includes(modelId),
      true,
      `expected ${modelId} to be hidden from the chat picker allowlist`,
    );
  }
});

test('unified config menu filters to the chat picker allowlist only at the display layer', () => {
  assert.match(
    menuSource,
    /const CHAT_MODEL_PICKER_VISIBLE_MODEL_ID_SET = new Set\(CHAT_MODEL_PICKER_VISIBLE_MODEL_IDS\);/,
    'expected unified config menu to derive a Set from the visible allowlist',
  );

  assert.match(
    menuSource,
    /return CHAT_MODEL_PICKER_VISIBLE_MODEL_IDS\.flatMap\(modelId => \{[\s\S]*modelById\.get\(modelId\)[\s\S]*return model \? \[model\] : \[\];[\s\S]*\}\);/,
    'expected display filtering to preserve the requested allowlist order and omit missing models',
  );

  assert.match(
    menuSource,
    /const visibleCombinedModels = useMemo[\s\S]*filterUserFacingModelOptions\(combinedModels\)[\s\S]*\[combinedModels\][\s\S]*\);/,
    'expected unified config menu to derive a display-only filtered model list',
  );

  assert.match(
    menuSource,
    /const source = visibleCombinedModels;/,
    'expected top model calculations to use the display-filtered model list',
  );
});

test('chat input menu resets persisted hidden selected models to the visible default', () => {
  assert.match(
    menuSource,
    /const selectedModelIsVisible = visibleCombinedModels\.some\(model => model\.id === selectedModel\);/,
    'expected chat input menu to detect whether the saved selected model is still visible',
  );

  assert.match(
    menuSource,
    /const fallbackModel = visibleCombinedModels\.find\(model => canAccessModel\(model\.id\)\);/,
    'expected chat input menu to choose the first accessible visible model as fallback',
  );

  assert.match(
    menuSource,
    /onModelChange\(fallbackModel\.id\);/,
    'expected chat input menu to reset hidden persisted selections through the normal model-change path',
  );
});

test('chat-picker visible models are curated as recommended picker options', () => {
  for (const modelId of expectedChatPickerVisibleModels) {
    assert.match(
      modelSelectionSource,
      new RegExp(`id: '${escapedModelId(modelId)}'[\\s\\S]*?recommended: true`),
      `expected ${modelId} to be a recommended curated picker option`,
    );
  }
});

test('official Kimi K2.5 is recommended in model metadata and curated picker inventory', () => {
  assert.match(
    modelSelectionSource,
    /'kimi-k2\.5': \{[\s\S]*?tier: 'free',[\s\S]*?priority: 105,[\s\S]*?recommended: true,[\s\S]*?lowQuality: false\s*\}/,
    'expected official Kimi K2.5 model metadata to be recommended',
  );

  assert.match(
    modelSelectionSource,
    /id: 'kimi-k2\.5',\s*label: 'Kimi K2\.5 \(Moonshot Official\)',\s*recommended: true,/,
    'expected official Kimi K2.5 curated picker entry to be recommended',
  );
});

test('new requested models canonicalize from provider aliases and localStorage values', () => {
  const expectedAliases = {
    'moonshotai/kimi-k2.6': 'kimi-k2.6',
    'moonshot/kimi-k2.6': 'kimi-k2.6',
    'openrouter/moonshotai/kimi-k2.6': 'openrouter/moonshotai/kimi-k2.6',
    'xiaomi/mimo-v2.5-pro': 'openrouter/xiaomi/mimo-v2.5-pro',
    'deepseek/deepseek-v4-pro': 'deepseek-v4-pro-high',
    'deepseek/deepseek-v4-flash': 'deepseek-v4-flash-high',
    'deepseek-v4-pro': 'deepseek-v4-pro-high',
    'deepseek-v4-flash': 'deepseek-v4-flash-high',
  };

  for (const [alias, canonical] of Object.entries(expectedAliases)) {
    assert.match(
      modelSelectionSource,
      new RegExp(`'${alias.replaceAll('/', '\\/').replaceAll('.', '\\.')}': '${canonical.replaceAll('/', '\\/').replaceAll('.', '\\.')}'`),
      `expected ${alias} to canonicalize to ${canonical}`,
    );
  }
});

test('chat defaults use official DeepSeek V4 Pro High instead of Gemini or bare ollama', () => {
  for (const constantName of [
    'DEFAULT_PREMIUM_MODEL_ID',
    'DEFAULT_FREE_MODEL_ID',
    'DEFAULT_LOCAL_CHAT_MODEL_ID',
  ]) {
    assert.match(
      modelSelectionSource,
      new RegExp(`export const ${constantName} = 'deepseek-v4-pro-high';`),
      `expected ${constantName} to use official DeepSeek V4 Pro High`,
    );
  }

  assert.match(
    modelSelectionSource,
    /const getDefaultModelId = \(subscriptionStatus: SubscriptionStatus\): string =>[\s\S]*isLocalMode\(\)[\s\S]*\? DEFAULT_LOCAL_CHAT_MODEL_ID/,
    'expected default model helper to route LOCAL fallback through the safe DeepSeek default',
  );

  assert.doesNotMatch(
    modelSelectionSource,
    /defaultModel\s*=\s*'ollama'/,
    'LOCAL fallback must not write bare ollama into the preferred chat model',
  );

  assert.doesNotMatch(
    modelSelectionSource,
    /isLocalMode\(\)\s*\?\s*'ollama'\s*:/,
    'unknown-model fallback must not poison later chat submissions with bare ollama',
  );

  assert.match(
    modelSelectionSource,
    /if \(modelId === 'ollama'\) \{[\s\S]*return 'ollama';/,
    'explicit user-selected ollama should remain supported',
  );
});

test('new requested models are Shadow Clone selectable while qwen remains excluded', () => {
  for (const modelId of backendSupportedRequestedModels) {
    assert.match(
      modelSelectionSource,
      new RegExp(`'${modelId.replaceAll('/', '\\/').replaceAll('.', '\\.')}'`),
      `expected Shadow Clone selection logic to mention ${modelId}`,
    );
  }

  assert.match(
    modelSelectionSource,
    /normalizedModelId\.includes\('qwen3\.5-plus'\)/,
    'expected qwen3.5-plus exclusion behavior to remain',
  );

  assert.doesNotMatch(
    shadowCloneToggleSource,
    /Kimi K2\.5 or OpenRouter-backed models only/,
    'expected Shadow Clone copy to stop claiming only Kimi K2.5/OpenRouter models are supported',
  );
});

test('available-models fallback exposes new requested backend ids', () => {
  for (const modelId of backendSupportedRequestedModels) {
    assert.match(
      apiSource,
      new RegExp(`id: '${modelId.replaceAll('/', '\\/').replaceAll('.', '\\.')}'`),
      `expected getAvailableModels fallback to include ${modelId}`,
    );
  }
});
