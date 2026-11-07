// Render demo/chat.html frame by frame, then build demo.gif (README) and demo.mp4 with ffmpeg.
//   cd demo && npm install && PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install chromium && node record.mjs
import { chromium } from "playwright";
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const frames = path.join(here, "frames");
const FPS = 15;

rmSync(frames, { recursive: true, force: true });
mkdirSync(frames);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 400, height: 720 }, deviceScaleFactor: 2 });
await page.goto("file://" + path.join(here, "chat.html"));
await page.evaluate(() => document.fonts.ready);
const end = await page.evaluate(() => window.END);
const n = Math.ceil(end * FPS);
for (let i = 0; i <= n; i++) {
  await page.evaluate((t) => window.render(t), i / FPS);
  await page.screenshot({ path: path.join(frames, String(i).padStart(4, "0") + ".png") });
}
await browser.close();
console.log(`${n + 1} frames`);

const input = ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", path.join(frames, "%04d.png")];
execFileSync("ffmpeg", [...input, "-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "20", "-movflags", "+faststart",
  path.join(here, "demo.mp4")]);
execFileSync("ffmpeg", [...input, "-vf",
  "fps=12,scale=400:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
  path.join(here, "demo.gif")]);
rmSync(frames, { recursive: true, force: true });
console.log("wrote demo.gif and demo.mp4");
