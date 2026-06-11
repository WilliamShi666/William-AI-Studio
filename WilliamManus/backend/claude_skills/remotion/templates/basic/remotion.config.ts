import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);

// Use Playwright's Chromium if available (pre-installed in sandbox)
const chromePath = process.env.REMOTION_CHROME_EXECUTABLE;
if (chromePath) {
  Config.setBrowserExecutable(chromePath);
}
