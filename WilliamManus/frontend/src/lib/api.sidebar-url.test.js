const assert = require('assert');

function buildApiUrlLikeProduction(path, rawApiUrl, origin = 'http://localhost') {
  const base = rawApiUrl || '/api';
  const normalizedBase = base.endsWith('/') ? base : `${base}/`;
  const normalizedPath = path.startsWith('/') ? path.slice(1) : path;
  if (normalizedBase.startsWith('/')) {
    return new URL(`${normalizedBase}${normalizedPath}`, origin);
  }
  return new URL(`${normalizedBase}${normalizedPath}`);
}

function oldSidebarThreadsUrl(rawApiUrl) {
  return new URL(`${rawApiUrl}/sidebar/threads`);
}

assert.throws(
  () => oldSidebarThreadsUrl('/api'),
  /Invalid URL/,
  'old sidebar URL construction should reproduce the relative /api failure',
);

const relativeUrl = buildApiUrlLikeProduction('/sidebar/threads', '/api', 'http://example.test');
assert.strictEqual(relativeUrl.toString(), 'http://example.test/api/sidebar/threads');

const absoluteUrl = buildApiUrlLikeProduction('/sidebar/threads', 'http://127.0.0.1:8002/api');
assert.strictEqual(absoluteUrl.toString(), 'http://127.0.0.1:8002/api/sidebar/threads');

console.log('api.sidebar-url contract passed');
