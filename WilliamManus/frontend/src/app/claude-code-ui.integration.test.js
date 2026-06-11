const assert = require('assert');
const fs = require('fs');
const path = require('path');

const frontendRoot = path.resolve(__dirname, '../..');
const workspaceRoot = path.resolve(frontendRoot, '../..');
const heroPath = path.join(frontendRoot, 'src/components/home/sections/hero-section.tsx');
const middlewarePath = path.join(frontendRoot, 'src/middleware.ts');
const pagePath = path.join(frontendRoot, 'src/app/(home)/claude-code-ui/page.tsx');
const sourceDir = path.join(workspaceRoot, 'cc-flow-src');
const oldSourceDir = path.join(workspaceRoot, 'fufan-cc-flow-src');
const zipPath = path.join(frontendRoot, 'public/downloads/cc-flow-src.zip');

assert.ok(fs.existsSync(sourceDir), 'Claude Code UI source directory should be renamed to cc-flow-src');
assert.ok(!fs.existsSync(oldSourceDir), 'legacy fufan-cc-flow-src directory should not remain at repo root');

const hero = fs.readFileSync(heroPath, 'utf8');
assert.match(hero, /Claude Code UI/, 'home hero should introduce Claude Code UI on the third card');
assert.match(hero, /<Link[\s\S]*href=\"\/claude-code-ui\"/, 'third card should be a real public Link to /claude-code-ui');
assert.doesNotMatch(hero, /handleProjectClick\('\/claude-code-ui'/, 'third card should not use the auth-gated project click handler');
assert.doesNotMatch(hero, /cursor-not-allowed opacity-60/, 'third card should not be a disabled placeholder');

const middleware = fs.readFileSync(middlewarePath, 'utf8');
assert.match(middleware, /'\/claude-code-ui'/, 'middleware should allow public access to the introduction page');
assert.match(middleware, /'\/downloads'/, 'middleware should allow public access to download assets');
assert.doesNotMatch(middleware, /webp\|zip/, 'middleware should not globally bypass every .zip path; only /downloads is public');

assert.ok(fs.existsSync(pagePath), 'Claude Code UI introduction page should exist');
const page = fs.readFileSync(pagePath, 'utf8');
assert.match(page, /Claude Code UI/, 'introduction page should use Claude Code UI as the hero title');
assert.match(page, /\/downloads\/cc-flow-src\.zip/, 'introduction page should link to the source zip download');
assert.match(page, /Download Source Package|下载源码包/, 'introduction page should expose a clear download CTA');
assert.match(page, /不需要登录William老师的AI梦工厂/, 'Build faster copy should say no William AI Dream Factory login is required');
assert.doesNotMatch(page, /不依赖 WilliamManus 或登录权限/, 'old Build faster copy should be removed');

assert.match(page, /\/claude-code-ui\/claude-code-ai\.gif/, 'introduction page should use the live Claude Code UI gif');
assert.match(page, /实时运行截图/, 'introduction page should label the live run screenshots section');
for (const sample of ['run_sample_1.png', 'run_sample_2.png', 'run_sample_3.png']) {
  assert.match(page, new RegExp(`/claude-code-ui/${sample}`), `introduction page should render ${sample}`);
  assert.ok(fs.existsSync(path.join(frontendRoot, 'public/claude-code-ui', sample)), `${sample} should be available as a public asset`);
}
assert.ok(fs.existsSync(path.join(frontendRoot, 'public/claude-code-ui/claude-code-ai.gif')), 'claude-code-ai.gif should be available as a public asset');

assert.match(page, /space-y-8/, 'run screenshots should be stacked vertically');
assert.doesNotMatch(page, /lg:grid-cols-3/, 'run screenshots should not be arranged horizontally on large screens');
assert.doesNotMatch(page, /h-72/, 'run screenshots should not be forced into a small fixed height');
assert.doesNotMatch(page, /object-cover/, 'run screenshots should not be cropped');

assert.ok(fs.existsSync(zipPath), 'public download zip should exist');
const listing = fs.readFileSync(zipPath).toString('latin1');
assert.match(listing, /cc-flow-src\/README\.md/, 'zip should contain cc-flow-src source root');
assert.match(listing, /cc-flow-src\/client\/src\/App\.tsx/, 'zip should contain frontend source');
assert.match(listing, /cc-flow-src\/server\/src\/app\.ts/, 'zip should contain server source');
assert.doesNotMatch(listing, /node_modules\//, 'zip should not include dependencies');
assert.doesNotMatch(listing, /release\//, 'zip should not include packaged app artifacts');
assert.doesNotMatch(listing, /\.claude\/settings\.local\.json/, 'zip should not include personal Claude settings');
assert.doesNotMatch(listing, /\.(exe|msi)\b/i, 'zip should not include Windows installers');

console.log('claude-code-ui integration contract passed');
