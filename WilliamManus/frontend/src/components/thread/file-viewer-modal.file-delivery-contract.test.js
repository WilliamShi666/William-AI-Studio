import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const sourcePath = path.join(testDir, 'file-viewer-modal.tsx');
const source = readFileSync(sourcePath, 'utf8');

test('file viewer reuses resolved browse sandbox id for follow-up file actions', () => {
  assert.match(
    source,
    /const \[resolvedBrowseSandboxId, setResolvedBrowseSandboxId\] = useState<string \| null>\(null\);/,
    'expected file viewer to retain a resolved browse sandbox id',
  );

  assert.match(
    source,
    /const activeBrowseSandboxId = resolvedBrowseSandboxId \?\? browseSandboxId;/,
    'expected file viewer to promote the resolved browse sandbox id into the active source',
  );

  assert.match(
    source,
    /const nextResolvedBrowseSandboxId =\s*listDiagnostics\?\.resolvedSandboxId\?\.trim\(\) \|\| null;/,
    'expected file viewer to hydrate the resolved browse sandbox id from list diagnostics',
  );

  assert.match(
    source,
    /useFileContentQuery\(\s*effectiveBrowseSandboxId,/s,
    'expected file content reads to use the effective browse sandbox id',
  );

  assert.match(
    source,
    /await listSandboxFiles\(effectiveBrowseSandboxId, parentPath\);/,
    'expected sibling directory probes to use the effective browse sandbox id',
  );

  assert.match(
    source,
    /await listSandboxFiles\(effectiveBrowseSandboxId, normalizedTargetPath\);/,
    'expected direct path probes to use the effective browse sandbox id',
  );

  assert.match(
    source,
    /const downloadResult = await downloadSandboxFile\(\s*effectiveBrowseSandboxId,/s,
    'expected single-file downloads to use the effective browse sandbox id',
  );
});
