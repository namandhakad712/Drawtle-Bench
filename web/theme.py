"""The control centre's stylesheet, as a Python string.

Kept apart from the page templates so the two can be read independently. There
is no build step and no external request: a benchmark UI that needs a CDN to
render is a UI that breaks in the environment the benchmark is meant to run in.

Design constraints this obeys:

* **No dark surfaces.** The document is light, ink is near-black, and every
  panel is a light tint. A benchmark reads numbers; numbers on a dark panel are
  harder to read and print badly, and every figure in `results/` may be printed.
* **Colour carries meaning, and only one meaning each.** Green = a clean
  measurement, amber = excluded or unverified, red = failed, grey = unknown.
  No decorative colour, so a reader can trust that a coloured figure is telling
  them something about the data rather than about the design.
* **Unknown is visibly unknown.** A `null` renders as a dash with a tooltip, not
  as a zero and not as blank space. Blank space reads as "zero" to a skimming
  reader and that is a false claim about an unmeasured model.

The visual system is deliberately "modern flat": one hue for action (blue),
one for success (green), generous whitespace, large rounded corners, soft
borders instead of heavy shadows, and tabular numerals everywhere a number
can be compared. No gradients used as decoration -- a gradient appears only
inside a data bar, where it communicates magnitude.
"""

CSS = """
:root{
  --ink:#111318; --ink2:#343a44; --muted:#5c6470; --faint:#8b939f;
  --rule:#e2e5ea; --rule2:#eef0f4; --panel:#fafbfc; --panel2:#f3f5f8;
  --white:#fff; --tint:#eef4fd;
  --green:#177a46; --green-bg:#ecf7f0; --green-rule:#cde7d8;
  --red:#b02a20;   --red-bg:#fdf1ef;   --red-rule:#f2d3cf;
  --amber:#8a5900; --amber-bg:#fdf7e8; --amber-rule:#f1e2bf;
  --blue:#2559b0;  --blue-bg:#eef4fd;  --blue-rule:#d4e2f6;
  --violet:#5b3a8e;--violet-bg:#f4effc;--violet-rule:#e0d4f4;
  --sans:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
  --r:12px; --r2:8px; --r3:6px;
  --shadow:0 1px 2px rgba(17,19,24,.04),0 4px 16px rgba(17,19,24,.05);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:var(--sans); color:var(--ink); background:#f7f8fa;
  font-size:13.5px; line-height:1.5; -webkit-font-smoothing:antialiased;
}
a{color:var(--blue); text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3{margin:0; font-weight:640; letter-spacing:-0.011em}
h1{font-size:20px}
h2{font-size:14px}
h3{font-size:12.5px}
code,.mono{font-family:var(--mono); font-size:12px}
.dim{color:var(--muted)}
.faint{color:var(--faint)}
.tiny{font-size:11.5px}
.nowrap{white-space:nowrap}
.right{text-align:right}
.wrap{max-width:1220px; margin:0 auto; padding:0 24px}

/* ---------- top bar ---------- */
header.top{
  position:sticky; top:0; z-index:40; background:rgba(255,255,255,.92);
  backdrop-filter:saturate(160%) blur(10px); border-bottom:1px solid var(--rule);
  box-shadow:0 1px 8px rgba(17,19,24,.03);
}
.top .wrap{display:flex; align-items:center; gap:20px; height:56px}
.brand{display:flex; align-items:baseline; gap:9px; font-weight:700; font-size:14.5px}
.brand .logo{
  width:22px;height:22px;border-radius:6px;background:var(--blue);
  align-self:center; position:relative; flex:none;
}
.brand .logo::after{
  content:""; position:absolute; inset:6px; border-radius:3px;
  border:2px solid rgba(255,255,255,.85);
}
.brand .ver{font-size:10.5px; color:var(--faint); font-weight:500; letter-spacing:.04em}
nav.tabs{display:flex; gap:3px; margin-left:10px; flex-wrap:nowrap; background:var(--panel2);
  padding:3px; border-radius:10px; overflow-x:auto; max-width:100%}
nav.tabs button{
  font:inherit; font-size:12.5px; font-weight:550; border:0; background:none; cursor:pointer;
  color:var(--muted); padding:6px 12px; border-radius:7px;
  transition:color .12s, background .12s;
}
nav.tabs button:hover{color:var(--ink)}
nav.tabs button[aria-selected=true]{background:var(--white); color:var(--blue); font-weight:620; box-shadow:0 1px 3px rgba(17,19,24,.08)}
.top .spacer{flex:1}
.pill{
  display:inline-flex; align-items:center; gap:6px; font-size:11.5px;
  padding:4px 10px; border-radius:100px; border:1px solid var(--rule);
  background:var(--white); color:var(--ink2); white-space:nowrap;
}
.pill b{font-weight:640}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex:none; box-shadow:0 0 0 3px rgba(0,0,0,.03)}
.dot.g{background:var(--green)} .dot.a{background:#d19b1a}
.dot.r{background:var(--red)}  .dot.n{background:var(--faint)}
.dot.b{background:var(--blue)}

/* ---------- layout ---------- */
main{padding:26px 0 80px}
.panel{
  border:1px solid var(--rule); border-radius:var(--r); background:var(--white);
  overflow:hidden; margin-bottom:20px; box-shadow:var(--shadow);
}
.panel > .head{
  display:flex; align-items:center; gap:11px; padding:13px 17px;
  border-bottom:1px solid var(--rule2); background:var(--panel);
}
.panel > .head .t{font-weight:650; font-size:13.5px}
.panel > .head .sub{color:var(--muted); font-size:11.5px; margin-left:auto; text-align:right}
.panel .body{padding:17px}
.panel .body.tight{padding:0}
.split{display:grid; grid-template-columns:1fr 1fr; gap:20px}
@media(max-width:860px){.split{grid-template-columns:1fr}}

/* ---------- stat cards ---------- */
.stats{display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px}
.stat{
  border:1px solid var(--rule); border-radius:var(--r); padding:13px 15px;
  background:var(--white); box-shadow:var(--shadow); position:relative; overflow:hidden;
}
.stat::before{content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--rule)}
.stat .k{font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:640}
.stat .v{font-size:22px; font-weight:680; margin-top:5px; letter-spacing:-0.02em; font-variant-numeric:tabular-nums}
.stat .s{font-size:11px; color:var(--muted); margin-top:2px}
.stat.good{border-color:var(--green-rule)}
.stat.good .v{color:var(--green)}
.stat.good::before{background:var(--green)}
.stat.warn{border-color:var(--amber-rule)}
.stat.warn .v{color:var(--amber)}
.stat.warn::before{background:#d19b1a}
.stat.bad{border-color:var(--red-rule)}
.stat.bad .v{color:var(--red)}
.stat.bad::before{background:var(--red)}

/* ---------- table ---------- */
table{width:100%; border-collapse:collapse; font-size:12.5px}
th{
  text-align:left; font-weight:620; color:var(--muted); font-size:10.5px;
  text-transform:uppercase; letter-spacing:.05em; padding:10px 13px;
  border-bottom:1px solid var(--rule); background:var(--panel);
  position:sticky; top:56px; z-index:5; white-space:nowrap;
}
td{padding:9px 13px; border-bottom:1px solid var(--rule2); vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#f6f8fb}
tbody tr.sel{background:var(--blue-bg)}
td.num,th.num{text-align:right; font-variant-numeric:tabular-nums}
.rank{color:var(--faint); font-variant-numeric:tabular-nums}
.model-cell{font-family:var(--mono); font-size:11.5px; word-break:break-all}

/* ---------- tags ---------- */
.tag{
  display:inline-block; font-size:10px; font-weight:640; letter-spacing:.03em;
  padding:2.5px 7px; border-radius:5px; border:1px solid transparent;
  text-transform:uppercase; white-space:nowrap;
}
.tag.ok{background:var(--green-bg); color:var(--green); border-color:var(--green-rule)}
.tag.no{background:var(--panel2); color:var(--muted); border-color:var(--rule)}
.tag.unk{background:var(--amber-bg); color:var(--amber); border-color:var(--amber-rule)}
.tag.err{background:var(--red-bg); color:var(--red); border-color:var(--red-rule)}
.tag.info{background:var(--blue-bg); color:var(--blue); border-color:var(--blue-rule)}
.tag.cap{background:var(--violet-bg); color:var(--violet); border-color:var(--violet-rule); text-transform:none; font-weight:560}
.tag+.tag{margin-left:4px}

/* ---------- note ---------- */
.note{
  border-left:3px solid var(--rule); background:var(--panel2);
  padding:10px 13px; font-size:12px; color:var(--ink2); border-radius:0 var(--r2) var(--r2) 0;
}
.note.warn{border-left-color:#d19b1a; background:var(--amber-bg)}
.note.err{border-left-color:var(--red); background:var(--red-bg)}
.note.ok{border-left-color:var(--green); background:var(--green-bg)}
.note.info{border-left-color:var(--blue); background:var(--blue-bg)}
.note b{font-weight:640}
.note+.note{margin-top:8px}

/* ---------- form ---------- */
button.btn{
  font:inherit; font-size:12.5px; cursor:pointer; padding:7px 14px;
  border:1px solid var(--rule); background:var(--white); color:var(--ink);
  border-radius:var(--r2); font-weight:580; transition:border-color .12s, background .12s, box-shadow .12s;
}
button.btn:hover{background:var(--panel2); border-color:#d2d7dd}
button.btn.primary{background:var(--blue); border-color:var(--blue); color:#fff}
button.btn.primary:hover{background:#1d4a99}
button.btn.danger{color:var(--red); border-color:var(--red-rule)}
button.btn.danger:hover{background:var(--red-bg)}
button.btn:disabled{opacity:.5; cursor:not-allowed}
button.lnk{
  border:0; background:none; color:var(--blue); cursor:pointer; font:inherit;
  font-size:12px; padding:2.5px 6px; border-radius:5px;
}
button.lnk:hover{background:var(--blue-bg)}
button.lnk.danger{color:var(--red)}
button.lnk.danger:hover{background:var(--red-bg)}
input,select,textarea{
  font:inherit; font-size:12.5px; padding:7px 10px; border:1px solid var(--rule);
  border-radius:var(--r2); background:var(--white); color:var(--ink); width:100%;
  transition:border-color .12s, box-shadow .12s;
}
input:focus,select:focus,textarea:focus{outline:none; border-color:var(--blue); box-shadow:0 0 0 3px var(--blue-bg)}
input.mono,textarea.mono{font-family:var(--mono); font-size:11.5px}
label.f{display:block; margin-bottom:12px}
label.f > .l{display:block; font-size:11px; font-weight:640; color:var(--muted); margin-bottom:5px; letter-spacing:.02em}
label.f > .h{font-size:11px; color:var(--faint); margin-top:4px; line-height:1.45}
.grid2{display:grid; grid-template-columns:1fr 1fr; gap:0 14px}
.grid3{display:grid; grid-template-columns:1fr 1fr 1fr; gap:0 14px}
@media(max-width:700px){.grid2,.grid3{grid-template-columns:1fr}}
.row{display:flex; gap:9px; align-items:center; flex-wrap:wrap}
.check{display:flex; align-items:center; gap:8px; font-size:12.5px; margin:5px 0}
.check input{width:auto; margin:0; accent-color:var(--blue)}

/* ---------- misc ---------- */
.toolbar{display:flex; gap:9px; align-items:center; flex-wrap:wrap; margin-bottom:15px}
.spin{
  width:13px;height:13px;border:2px solid var(--rule); border-top-color:var(--blue);
  border-radius:50%; display:inline-block; animation:sp .7s linear infinite; flex:none;
}
@keyframes sp{to{transform:rotate(360deg)}}
details{border:1px solid var(--rule); border-radius:var(--r2); margin-top:9px; background:var(--white); overflow:hidden}
details > summary{
  cursor:pointer; padding:10px 13px; font-size:12.5px; font-weight:600;
  list-style:none; display:flex; align-items:center; gap:8px;
}
details > summary::-webkit-details-marker{display:none}
details > summary::before{content:"\\25B8"; color:var(--faint); font-size:10px; transition:transform .12s}
details[open] > summary::before{transform:rotate(90deg)}
details > .dbody{padding:0 13px 13px; border-top:1px solid var(--rule2)}
pre.log{
  font-family:var(--mono); font-size:11px; line-height:1.55; margin:0;
  background:var(--panel); border:1px solid var(--rule); border-radius:var(--r2);
  padding:11px; max-height:420px; overflow:auto; white-space:pre-wrap; word-break:break-all;
}
.empty{padding:36px 20px; text-align:center; color:var(--muted); font-size:13px}
.bar{height:6px; background:var(--panel2); border-radius:4px; overflow:hidden; min-width:56px}
.bar > i{display:block; height:100%; background:var(--green)}
.bar.warn > i{background:#d19b1a}
.tabs2{display:flex; gap:5px; border-bottom:1px solid var(--rule); margin-bottom:13px; flex-wrap:wrap}
.tabs2 button{
  font:inherit; font-size:12.5px; border:0; background:none; cursor:pointer;
  padding:8px 11px; color:var(--muted); border-bottom:2px solid transparent; margin-bottom:-1px;
}
.tabs2 button[aria-selected=true]{color:var(--ink); border-bottom-color:var(--blue); font-weight:600}
.kv{display:grid; grid-template-columns:auto 1fr; gap:3px 14px; font-size:12.5px}
.kv .k{color:var(--muted); white-space:nowrap}
.kv .v{font-family:var(--mono); font-size:11.5px; word-break:break-all}
.toast{
  position:fixed; bottom:22px; left:50%; transform:translateX(-50%); z-index:100;
  background:var(--ink); color:#fff; font-size:12.5px; padding:10px 18px;
  border-radius:100px; box-shadow:0 6px 24px rgba(0,0,0,.24); max-width:88vw;
  animation:pop .18s ease-out;
}
@keyframes pop{from{opacity:0; transform:translateX(-50%) translateY(6px)}}
.toast.bad{background:var(--red)} .toast.good{background:var(--green)}

/* ---------- leaderboard bar chart ---------- */
.lb{display:flex; flex-direction:column; gap:14px; padding:16px}
.lb-row{display:grid; grid-template-columns:30px minmax(150px,1.1fr) minmax(190px,1.4fr) auto; gap:10px; align-items:center}
.lb-rank{font-variant-numeric:tabular-nums; color:var(--faint); font-size:12.5px; text-align:right}
.lb-model{font-family:var(--mono); font-size:11.5px; word-break:break-all; line-height:1.3}
.lb-model a{color:var(--ink)}
.lb-model a:hover{color:var(--blue); text-decoration:underline}
.lb-model .p{display:block; font-family:var(--sans); font-size:10px; color:var(--faint); margin-top:2px}
.lb-track{position:relative; height:24px; background:var(--panel2); border-radius:7px; overflow:hidden}
/* Quarter gridlines behind the bars, so a 60% bar is visibly 60% of the
   track and not "most of whatever width happens to be left". */
.lb-grid{position:absolute; inset:0; pointer-events:none}
.lb-grid i{position:absolute; top:0; bottom:0; width:1px; background:rgba(17,19,24,.07)}
/* The 95% CI as a shaded band the bar sits inside: a wide band is read as
   "somewhere in here", which is the honest reading of a per-episode interval. */
.lb-band{
  position:absolute; top:0; bottom:0;
  background:rgba(37,89,176,.13);
  border-left:1px solid rgba(37,89,176,.4);
  border-right:1px solid rgba(37,89,176,.4);
}
.lb-fill{
  position:absolute; left:0; top:0; bottom:0; border-radius:7px 0 0 7px;
  background:linear-gradient(90deg, #3b76c4, var(--blue));
  transition:width .5s cubic-bezier(.22,.9,.35,1);
  min-width:6px;
}
.lb-fill.hi{background:linear-gradient(90deg,#2a8a54,var(--green))}
.lb-fill.mid{background:linear-gradient(90deg,#c29b1d,#d19b1a)}
.lb-fill.lo{background:linear-gradient(90deg,#c75a4c,var(--red))}
.lb-fill.unk{background:repeating-linear-gradient(45deg,var(--rule) 0 6px,var(--panel2) 6px 12px)}
.lb-val{font-variant-numeric:tabular-nums; text-align:right; font-weight:680; font-size:13.5px; white-space:nowrap}
.lb-val small{display:block; font-weight:500; font-size:10px; color:var(--muted)}
/* The axis lives under the chart, aligned to the same track width, so the
   gridlines mean what they look like. */
.lb-axis{display:grid; grid-template-columns:30px minmax(150px,1.1fr) minmax(190px,1.4fr) auto; gap:10px; padding:2px 16px 0}
.lb-axis .ticks{position:relative; height:14px; grid-column:3}
.lb-axis .ticks span{
  position:absolute; top:0; transform:translateX(-50%);
  font-size:9.5px; color:var(--faint); font-variant-numeric:tabular-nums;
}
.lb-legend{display:flex; gap:14px; align-items:center; margin-top:10px; padding:8px 16px 2px; font-size:11px; color:var(--muted)}
.lb-legend i{display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:5px; vertical-align:-1px}
@media(max-width:760px){
  .lb-row{grid-template-columns:24px minmax(110px,1fr) 1fr auto; gap:7px}
  .lb-axis{grid-template-columns:24px minmax(110px,1fr) 1fr auto}
}

/* ---------- storyboard ---------- */
.sb-grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(252px,1fr)); gap:14px}
.sb-card{
  text-align:left; cursor:pointer; font:inherit; color:var(--ink);
  border:1px solid var(--rule); border-radius:var(--r); background:var(--white);
  padding:14px 15px; transition:border-color .12s, box-shadow .12s, transform .12s;
  box-shadow:var(--shadow);
}
.sb-card:hover{border-color:var(--blue); box-shadow:0 6px 20px rgba(37,89,176,.14); transform:translateY(-2px)}
.sb-card .row{justify-content:space-between}

/* ---------- replay filmstrip ---------- */
.striprow{display:flex; gap:6px; overflow-x:auto; padding:4px 0 10px; margin-bottom:6px}
img.strip{
  width:84px; height:84px; object-fit:cover; flex:none; cursor:pointer;
  border:1px solid var(--rule); border-radius:var(--r2);
  transition:border-color .12s, transform .12s;
}
img.strip:hover{border-color:var(--blue); transform:scale(1.06)}

/* ---------- batch selection ---------- */
.sel-col{width:34px; text-align:center}
.sel-col input{accent-color:var(--blue); width:15px; height:15px; cursor:pointer}
.batchbar{
  display:flex; align-items:center; gap:10px; padding:9px 13px; margin:0 0 12px;
  background:var(--blue-bg); border:1px solid var(--blue-rule); border-radius:var(--r2);
  font-size:12.5px; color:var(--blue);
}
.batchbar b{font-weight:650}
.batchbar .spacer{flex:1}
.batchbar button{margin-left:6px}
"""
