const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const appDir = path.join(root, 'release', 'linux-unpacked');
const resourcesApp = path.join(appDir, 'resources', 'app');
const electronCacheZip = path.join(process.env.HOME || '', '.cache', 'electron', 'electron-v42.2.0-linux-x64.zip');

function rm(target) {
  fs.rmSync(target, { recursive: true, force: true });
}

function cp(src, dest, opts = {}) {
  fs.cpSync(src, dest, { recursive: true, dereference: false, ...opts });
}

function mkdir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function copyRuntimeNodeModules() {
  const nodeModulesRoot = path.join(root, 'node_modules');
  cp(path.join(nodeModulesRoot, '.pnpm'), path.join(resourcesApp, 'node_modules', '.pnpm'), {
    filter: (file) => !file.includes(`${path.sep}.cache${path.sep}`),
  });
  cp(path.join(nodeModulesRoot, '.bin'), path.join(resourcesApp, 'node_modules', '.bin'));
}

if (!fs.existsSync(electronCacheZip)) {
  throw new Error(`Electron runtime zip not found at ${electronCacheZip}. Run electron-builder once or install Electron first.`);
}

rm(appDir);
mkdir(resourcesApp);
execFileSync('unzip', ['-q', electronCacheZip, '-d', appDir], { stdio: 'inherit' });

const electronBinary = path.join(appDir, 'electron');
const appBinary = path.join(appDir, 'claude-code-ui');
if (fs.existsSync(electronBinary)) fs.renameSync(electronBinary, appBinary);

cp(path.join(root, 'electron', 'dist'), path.join(resourcesApp, 'electron', 'dist'));
cp(path.join(root, 'client', 'dist'), path.join(resourcesApp, 'client', 'dist'));
cp(path.join(root, 'server', 'dist'), path.join(resourcesApp, 'server', 'dist'));
cp(path.join(root, 'server', 'package.json'), path.join(resourcesApp, 'server', 'package.json'));
cp(path.join(root, 'package.json'), path.join(resourcesApp, 'package.json'));
copyRuntimeNodeModules();

console.log(`Packed desktop directory: ${appDir}`);
try {
  const size = execFileSync('du', ['-sh', appDir], { encoding: 'utf8' }).trim();
  console.log(size);
} catch {}
