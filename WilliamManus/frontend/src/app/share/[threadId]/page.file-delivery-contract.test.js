import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const sourcePath = path.join(testDir, 'page.tsx');
const source = readFileSync(sourcePath, 'utf8');

test('share thread page normalizes project file delivery source before opening the file viewer', () => {
  assert.match(
    source,
    /resolveProjectFileDeliverySource\(/,
    'expected shared thread page to normalize project file delivery source',
  );

  assert.match(
    source,
    /setSandboxId\(normalizedProject\.file_delivery_source\.browseSandboxId\);/,
    'expected shared thread page to use the normalized browse sandbox id',
  );
});

test('share thread page preserves file list mode when opening the file viewer', () => {
  assert.match(
    source,
    /const \[filePathList, setFilePathList\] = useState<string\[\] \| undefined>\(/,
    'expected shared thread page to track filePathList state',
  );

  assert.match(
    source,
    /setFilePathList\(filePathList\);/,
    'expected handleOpenFileViewer to preserve the attachment file list',
  );

  assert.match(
    source,
    /filePathList=\{filePathList\}/,
    'expected FileViewerModal to receive filePathList on the shared thread page',
  );
});
