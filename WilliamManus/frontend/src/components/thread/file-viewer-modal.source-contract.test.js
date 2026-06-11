import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const modalPath = path.join(testDir, 'file-viewer-modal.tsx');
const modalSource = readFileSync(modalPath, 'utf8');

test('file viewer reuses the resolved browse sandbox id for source-sensitive operations', () => {
  assert.match(
    modalSource,
    /const \[resolvedBrowseSandboxId, setResolvedBrowseSandboxId\] = useState<string \| null>\(null\);\s*const activeBrowseSandboxId = resolvedBrowseSandboxId \?\? browseSandboxId;/s,
  );
  assert.match(
    modalSource,
    /useDirectoryQuery\(activeBrowseSandboxId, currentPath, \{/,
  );
  assert.match(
    modalSource,
    /useFileContentQuery\(\s*effectiveBrowseSandboxId,\s*selectedFilePath \|\| undefined,/s,
  );
  assert.match(
    modalSource,
    /downloadSandboxFile\(\s*effectiveBrowseSandboxId,\s*selectedFilePath,/s,
  );
  assert.match(
    modalSource,
    /listSandboxFiles\(effectiveBrowseSandboxId, parentPath\)/,
  );
  assert.match(
    modalSource,
    /await listSandboxFiles\(effectiveBrowseSandboxId, normalizedTargetPath\);/,
  );
});

test('file viewer tracks and clears the resolved browse sandbox id from list diagnostics', () => {
  assert.match(
    modalSource,
    /listDiagnostics\?\.resolvedSandboxId\?\.trim\(\) \|\| null/,
  );
  assert.match(
    modalSource,
    /setResolvedBrowseSandboxId\(null\);/,
  );
});
