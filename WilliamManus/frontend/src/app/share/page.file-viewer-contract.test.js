import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const pagePath = path.join(testDir, '[threadId]', 'page.tsx');
const pageSource = readFileSync(pagePath, 'utf8');

test('share thread normalizes project file delivery source for the file viewer', () => {
  assert.match(
    pageSource,
    /resolveProjectFileDeliverySource/,
  );
  assert.match(
    pageSource,
    /file_delivery_source:\s*resolveProjectFileDeliverySource\(\s*projectData,\s*rawSandboxId,\s*\),/s,
  );
  assert.match(
    pageSource,
    /setSandboxId\(normalizedProject\.file_delivery_source\.browseSandboxId\);/,
  );
});

test('share thread preserves filePathList when opening the file viewer', () => {
  assert.match(
    pageSource,
    /const \[filePathList, setFilePathList\] = useState<string\[\] \| undefined>\(/,
  );
  assert.match(
    pageSource,
    /setFilePathList\(filePathList\);/,
  );
  assert.match(
    pageSource,
    /filePathList=\{filePathList\}/,
  );
});
