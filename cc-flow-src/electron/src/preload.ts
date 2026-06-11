import { contextBridge } from "electron";

function readArgument(name: string): string {
  const prefix = `--${name}=`;
  const match = process.argv.find((arg) => arg.startsWith(prefix));
  return match ? match.slice(prefix.length) : "";
}

const desktopApiBaseUrl = readArgument("claude-code-ui-api-base-url");
const desktopWsBaseUrl = readArgument("claude-code-ui-ws-base-url");

contextBridge.exposeInMainWorld("claudeCodeUI", {
  apiBaseUrl: desktopApiBaseUrl,
  wsBaseUrl: desktopWsBaseUrl,
  platform: process.platform,
});
