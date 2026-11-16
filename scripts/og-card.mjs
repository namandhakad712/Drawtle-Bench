// scripts/og-card.mjs — render the Open Graph card once (1200×630 PNG).
// The card is a build artifact like any other: the frame embedded below is
// the real episode-0 turn-0 frame from the committed results, exported
// from the site build. Run: bun scripts/og-card.mjs <path-to-task.html>
import * as fs from "node:fs";
import { chromium } from "playwright";

const taskHtml = process.argv[2] ?? "/tmp/site-build/task.html";
const out = process.argv[3] ?? "og-card.png";

const html = fs.readFileSync(taskHtml, "utf8");
// pull the first player frame (turn 0) from the embedded payload
const m = html.match(/<script type="application\/json" id="pl-data">(.*?)<\/script>/s);
if (!m) throw new Error(`pl-data payload not found in ${taskHtml}`);
const data = JSON.parse(m.group ? m[1] : m[1]);
const frame = data.turns[0].svg.replace(/<\/?svg[^>]*>/g, (s) =>
  s.includes("xmlns") ? '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 560">' : "</svg>",
);

const card = `<!doctype html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:1200px;height:630px;background:#0f141a;font-family:ui-sans-serif,system-ui,sans-serif;overflow:hidden;position:relative}
.grid{position:absolute;inset:0;background-image:linear-gradient(#1a222b 1px,transparent 1px),linear-gradient(90deg,#1a222b 1px,transparent 1px);background-size:40px 40px;opacity:.5}
.frame{position:absolute;right:24px;top:50%;transform:translateY(-50%) rotate(3deg);width:560px;border-radius:14px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.55);border:1px solid #2a3541}
.frame svg{display:block;width:100%;height:auto}
.t{position:absolute;left:72px;top:118px;width:520px}
.t h1{font-size:64px;line-height:1.08;color:#e9e6df;font-weight:700;letter-spacing:-.02em;font-family:ui-sans-serif,system-ui,sans-serif}
.t h1 em{color:#7ec8ff;font-style:normal}
.t .s{margin-top:22px;font-size:24px;line-height:1.45;color:#8b98a5}
.t .b{margin-top:34px;display:inline-block;padding:12px 26px;border:1px solid #2d5273;border-radius:10px;background:#16222e;color:#cfe7fb;font-size:21px;font-weight:650}
.badge{position:absolute;left:72px;top:64px;display:flex;align-items:center;gap:12px}
.badge svg{width:40px;height:40px}
.badge span{color:#a7b7c4;font-size:22px;font-weight:700;letter-spacing:.04em}
.foot{position:absolute;left:72px;bottom:52px;color:#5a6773;font-size:19px;letter-spacing:.02em}
</style></head><body>
<div class="grid"></div>
<div class="badge">
<svg viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#151c24"/><circle cx="32" cy="34" r="15" fill="#4d94c9"/><path d="M32 34 L46 26 L42 40 Z" fill="#e8f4ff"/><path d="M8 52 Q32 44 56 52" stroke="#58c98f" stroke-width="4" fill="none" stroke-linecap="round"/></svg>
<span>Drawtle&nbsp;Bench</span>
</div>
<div class="t">
<h1>Is the model reading the frame, <em>or answering from memory?</em></h1>
<div class="s">Rotating mazes. No heading drawn. BFS scoring, no judge.<br>Play the task yourself — then watch a measured model try.</div>
<div class="b">▶ Play it: namandhakad712.github.io/Drawtle-Bench</div>
</div>
<div class="frame">${frame}</div>
<div class="foot">one image · one question · repeated</div>
</body></html>`;

fs.writeFileSync("/tmp/og-card.html", card);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
await page.goto("file:///tmp/og-card.html");
await page.waitForTimeout(300);
await page.screenshot({ path: out, type: "png" });
await browser.close();
const sz = fs.statSync(out).size;
console.log(`${out}: ${sz} bytes`);
