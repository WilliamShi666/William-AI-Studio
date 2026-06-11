import fs from "fs/promises";
import path from "path";
import os from "os";

// Stored separately from ~/.claude/settings.json to avoid file-lock
// contention with the Claude Code process on Windows.
const PROXY_FILE = path.join(os.homedir(), ".claude_code_ui", "proxy.json");
const LEGACY_PROXY_FILE = path.join(os.homedir(), ".fufan-cc-flow", "proxy.json");

export interface ProxyData {
  httpProxy: string;
  httpsProxy: string;
  socksProxy: string;
}

async function readProxyFile(filePath: string): Promise<ProxyData> {
  const raw = await fs.readFile(filePath, "utf-8");
  const d = JSON.parse(raw) as Record<string, unknown>;
  return {
    httpProxy: (d.httpProxy as string) || "",
    httpsProxy: (d.httpsProxy as string) || "",
    socksProxy: (d.socksProxy as string) || "",
  };
}

async function migrateLegacyProxy(proxy: ProxyData): Promise<void> {
  try {
    const dir = path.dirname(PROXY_FILE);
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(PROXY_FILE, JSON.stringify(proxy, null, 2), "utf-8");
  } catch {
    // Best-effort migration only. The caller can still use the legacy data.
  }
}

export async function readProxy(): Promise<ProxyData> {
  try {
    return await readProxyFile(PROXY_FILE);
  } catch {
    try {
      const legacyProxy = await readProxyFile(LEGACY_PROXY_FILE);
      await migrateLegacyProxy(legacyProxy);
      return legacyProxy;
    } catch {
      return { httpProxy: "", httpsProxy: "", socksProxy: "" };
    }
  }
}

export async function writeProxy(proxy: ProxyData): Promise<void> {
  const dir = path.dirname(PROXY_FILE);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(PROXY_FILE, JSON.stringify(proxy, null, 2), "utf-8");
  // intentional console so the server terminal confirms the exact path used
  console.log(`[proxyConfig] saved → ${PROXY_FILE}`);
}
