const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const sourcePath = path.join(__dirname, 'actions.ts');
const source = fs.readFileSync(sourcePath, 'utf8');

assert.ok(
  /function\s+redactAuthLogData|const\s+redactAuthLogData/.test(source),
  'actions.ts should define a shared redaction helper for auth log payloads',
);
assert.doesNotMatch(
  source,
  /console\.log\([^\n]*响应数据[^\n]*,\s*data\s*\)/,
  'server actions must not log raw auth response data because it contains tokens',
);
assert.match(
  source,
  /redactAuthLogData\(data\)/,
  'server actions should log only redacted auth response data',
);

console.log('auth actions log redaction contract passed');
