import test from 'node:test';
import assert from 'node:assert/strict';

import { classifySandboxPathFromEntries } from './sandbox-path-kind.ts';

const normalizePath = (path) => path.replace(/\/+$/, '') || '/';

test('classifies a known directory entry as directory', () => {
  const result = classifySandboxPathFromEntries(
    '/workspace/reports',
    [
      {
        name: 'reports',
        path: '/workspace/reports',
        is_dir: true,
        size: 0,
        mod_time: '2026-04-02T00:00:00Z',
      },
    ],
    normalizePath,
  );

  assert.equal(result.kind, 'directory');
  assert.equal(result.entry?.path, '/workspace/reports');
});

test('classifies a known file entry as file', () => {
  const result = classifySandboxPathFromEntries(
    '/workspace/reports/summary.md',
    [
      {
        name: 'summary.md',
        path: '/workspace/reports/summary.md',
        is_dir: false,
        size: 10,
        mod_time: '2026-04-02T00:00:00Z',
      },
    ],
    normalizePath,
  );

  assert.equal(result.kind, 'file');
  assert.equal(result.entry?.path, '/workspace/reports/summary.md');
});

test('returns unknown for paths not present in known entries', () => {
  const result = classifySandboxPathFromEntries(
    '/workspace/missing',
    [],
    normalizePath,
  );

  assert.equal(result.kind, 'unknown');
  assert.equal(result.entry, null);
  assert.equal(result.normalizedPath, '/workspace/missing');
});
