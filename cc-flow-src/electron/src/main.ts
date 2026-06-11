import { app, BrowserWindow, dialog } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "child_process";
import http from "http";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let mainWindow: BrowserWindow | null = null;
let serverProcess: ChildProcessWithoutNullStreams | null = null;
let serverPort = 3001;

function resolveResourcePath(...segments: string[]): string {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "app", ...segments);
  }
  return path.join(__dirname, "..", "..", ...segments);
}

function getFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = http.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 3001;
      server.close(() => resolve(port));
    });
  });
}

function waitForHealth(port: number, timeoutMs = 30_000): Promise<void> {
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    const check = () => {
      const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
        res.resume();
        if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
          resolve();
          return;
        }
        retry();
      });

      req.on("error", retry);
      req.setTimeout(1_000, () => {
        req.destroy();
        retry();
      });
    };

    const retry = () => {
      if (Date.now() - startedAt > timeoutMs) {
        reject(new Error(`Backend did not become healthy on port ${port}`));
        return;
      }
      setTimeout(check, 300);
    };

    check();
  });
}

async function startBackend(): Promise<void> {
  if (process.env.VITE_DEV_SERVER_URL) {
    serverPort = Number(process.env.PORT) || 3001;
    return;
  }

  serverPort = await getFreePort();
  const serverEntry = resolveResourcePath("server", "dist", "index.js");

  serverProcess = spawn(process.execPath, [serverEntry], {
    env: {
      ...process.env,
      PORT: String(serverPort),
      NODE_ENV: "production",
      ELECTRON_RUN_AS_NODE: "1",
    },
    stdio: "pipe",
  });

  serverProcess.stdout.on("data", (data) => {
    console.log(`[server] ${data.toString().trimEnd()}`);
  });

  serverProcess.stderr.on("data", (data) => {
    console.error(`[server] ${data.toString().trimEnd()}`);
  });

  serverProcess.on("exit", (code, signal) => {
    console.log(`[server] exited code=${code ?? "null"} signal=${signal ?? "null"}`);
    serverProcess = null;
  });

  await waitForHealth(serverPort);
}

async function createWindow(): Promise<void> {
  const apiBaseUrl = `http://127.0.0.1:${serverPort}/api`;
  const wsBaseUrl = `ws://127.0.0.1:${serverPort}/ws`;

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1100,
    minHeight: 720,
    show: false,
    title: "Claude Code UI",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      additionalArguments: [
        `--claude-code-ui-api-base-url=${apiBaseUrl}`,
        `--claude-code-ui-ws-base-url=${wsBaseUrl}`,
      ],
    },
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    await mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    await mainWindow.loadFile(resolveResourcePath("client", "dist", "index.html"));
  }
}

app.whenReady().then(async () => {
  try {
    await startBackend();
    await createWindow();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await dialog.showMessageBox({
      type: "error",
      title: "Claude Code UI 启动失败",
      message: "本地服务启动失败",
      detail: message,
    });
    app.quit();
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", async () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    await createWindow();
  }
});

app.on("before-quit", () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
});
