const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const sourcePath = path.join(__dirname, 'client.ts');
const source = fs.readFileSync(sourcePath, 'utf8');

assert.ok(
  /function\s+redactAuthLogData|const\s+redactAuthLogData/.test(source),
  'auth client should define a redaction helper for auth log payloads',
);
assert.doesNotMatch(
  source,
  /console\.log\([^\n]*(登录数据|注册数据)[^\n]*,\s*credentials\s*\)/,
  'auth client must not log raw credentials because they contain passwords',
);
assert.doesNotMatch(
  source,
  /console\.log\([^\n]*响应数据[^\n]*,\s*data\s*\)/,
  'auth client must not log raw auth response data because it can contain tokens',
);
assert.match(
  source,
  /redactAuthLogData\(data\)/,
  'auth client should log only redacted auth response data',
);
assert.match(
  source,
  /redactCredentialsForLog\(credentials\)/,
  'auth client should log only redacted credential metadata',
);

console.log('auth client log redaction contract passed');
