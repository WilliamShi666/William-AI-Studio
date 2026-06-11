import { createRequire } from "module";

const require = createRequire(import.meta.url);

const dependencies = [
  "@anthropic-ai/claude-agent-sdk",
  "cors",
  "express",
  "multer",
  "node-pty",
  "ws",
] as const;

for (const dependency of dependencies) {
  try {
    require(dependency);
  } catch {
    // This file is bundled for Electron packaging only. Runtime imports in the
    // real services surface actionable errors if a dependency is genuinely missing.
  }
}
