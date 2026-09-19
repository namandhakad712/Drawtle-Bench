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
"""

CSS = """
:root{
  --ink:#16181c; --ink2:#3d444d; --muted:#6b737d; --faint:#98a0aa;
  --rule:#e4e7eb; --rule2:#eef0f3; --panel:#fbfbfc; --panel2:#f5f6f8;
  --white:#fff; --tint:#f0f4f9;
  --green:#1d6b3d; --green-bg:#eef7f1; --green-rule:#cee5d6;
  --red:#a8261d;   --red-bg:#fdf0ef;   --red-rule:#f0d2cf;
  --amber:#8a5900; --amber-bg:#fdf6e7; --amber-rule:#f0e0bd;
  --blue:#1a4f8a;  --blue-bg:#eef4fb;  --blue-rule:#d0e0f2;
  --violet:#5b3a8e;--violet-bg:#f3eefb;--violet-rule:#ded1f2;
  --sans:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
  --r:9px; --r2:6px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:var(--sans); color:var(--ink); background:var(--white);
  font-size:13.5px; line-height:1.5; -webkit-font-smoothing:antialiased;
}
a{color:var(--blue); text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3{margin:0; font-weight:640; letter-spacing:-0.011em}
h1{font-size:19px}
h2{font-size:14px}
h3{font-size:12.5px}
code,.mono{font-family:var(--mono); font-size:12px}
.dim{color:var(--muted)}
.faint{color:var(--faint)}
.tiny{font-size:11.5px}
.nowrap{white-space:nowrap}
.right{text-align:right}
.wrap{max-width:1180px; margin:0 auto; padding:0 22px}

/* ---------- top bar ---------- */
header.top{
  position:sticky; top:0; z-index:40; background:rgba(255,255,255,.94);
  backdrop-filter:saturate(180%) blur(8px); border-bottom:1px solid var(--rule);
}
.top .wrap{display:flex; align-items:center; gap:18px; height:53px}
.brand{display:flex; align-items:baseline; gap:9px; font-weight:660; font-size:14.5px}
.brand .ver{font-size:10.5px; color:var(--faint); font-weight:500; letter-spacing:.04em}
nav.tabs{display:flex; gap:2px; margin-left:8px; flex-wrap:wrap}
nav.tabs button{
  font:inherit; font-size:12.5px; border:0; background:none; cursor:pointer;
  color:var(--muted); padding:7px 11px; border-radius:var(--r2);
}
nav.tabs button:hover{background:var(--panel2); color:var(--ink)}
nav.tabs button[aria-selected=true]{background:var(--tint); color:var(--blue); font-weight:600}
.top .spacer{flex:1}
.pill{
  display:inline-flex; align-items:center; gap:6px; font-size:11.5px;
  padding:3px 9px; border-radius:100px; border:1px solid var(--rule);
  background:var(--panel); color:var(--ink2); white-space:nowrap;
}
.pill b{font-weight:640}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex:none}
.dot.g{background:var(--green)} .dot.a{background:#d19b1a}
.dot.r{background:var(--red)}  .dot.n{background:var(--faint)}
.dot.b{background:var(--blue)}

/* ---------- layout ---------- */
main{padding:22px 0 72px}
.panel{
  border:1px solid var(--rule); border-radius:var(--r); background:var(--white);
  overflow:hidden; margin-bottom:18px;
}
.panel > .head{
  display:flex; align-items:center; gap:11px; padding:12px 15px;
  border-bottom:1px solid var(--rule2); background:var(--panel);
}
.panel > .head .t{font-weight:640; font-size:13px}
.panel > .head .sub{color:var(--muted); font-size:11.5px; margin-left:auto; text-align:right}
.panel .body{padding:15px}
.panel .body.tight{padding:0}
.split{display:grid; grid-template-columns:1fr 1fr; gap:18px}
@media(max-width:860px){.split{grid-template-columns:1fr}}

/* ---------- stat cards ---------- */
.stats{display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); gap:11px}
.stat{border:1px solid var(--rule); border-radius:var(--r); padding:12px 13px; background:var(--panel)}
.stat .k{font-size:10.5px; text-transform:uppercase; letter-spacing:.055em; color:var(--muted); font-weight:600}
.stat .v{font-size:20px; font-weight:650; margin-top:5px; letter-spacing:-0.02em}
.stat .s{font-size:11px; color:var(--muted); margin-top:2px}
.stat.good{background:var(--green-bg); border-color:var(--green-rule)}
.stat.good .v{color:var(--green)}
.stat.warn{background:var(--amber-bg); border-color:var(--amber-rule)}
.stat.warn .v{color:var(--amber)}
.stat.bad{background:var(--red-bg); border-color:var(--red-rule)}
.stat.bad .v{color:var(--red)}

/* ---------- table ---------- */
table{width:100%; border-collapse:collapse; font-size:12.5px}
th{
  text-align:left; font-weight:620; color:var(--muted); font-size:10.5px;
  text-transform:uppercase; letter-spacing:.05em; padding:9px 11px;
  border-bottom:1px solid var(--rule); background:var(--panel);
  position:sticky; top:53px; z-index:5;
}
td{padding:8px 11px; border-bottom:1px solid var(--rule2); vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fafbfc}
td.num,th.num{text-align:right; font-variant-numeric:tabular-nums}
.rank{color:var(--faint); font-variant-numeric:tabular-nums}
.model-cell{font-family:var(--mono); font-size:11.5px; word-break:break-all}

/* ---------- tags ---------- */
.tag{
  display:inline-block; font-size:10px; font-weight:620; letter-spacing:.03em;
  padding:2px 6px; border-radius:4px; border:1px solid transparent;
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
  padding:9px 12px; font-size:12px; color:var(--ink2); border-radius:0 var(--r2) var(--r2) 0;
}
.note.warn{border-left-color:#d19b1a; background:var(--amber-bg)}
.note.err{border-left-color:var(--red); background:var(--red-bg)}
.note.ok{border-left-color:var(--green); background:var(--green-bg)}
.note.info{border-left-color:var(--blue); background:var(--blue-bg)}
.note b{font-weight:640}
.note+.note{margin-top:8px}

/* ---------- form ---------- */
button.btn{
  font:inherit; font-size:12.5px; cursor:pointer; padding:6px 12px;
  border:1px solid var(--rule); background:var(--white); color:var(--ink);
  border-radius:var(--r2); font-weight:560;
}
button.btn:hover{background:var(--panel2); border-color:#d5d9de}
button.btn.primary{background:var(--blue); border-color:var(--blue); color:#fff}
button.btn.primary:hover{background:#16437a}
button.btn.danger{color:var(--red); border-color:var(--red-rule)}
button.btn.danger:hover{background:var(--red-bg)}
button.btn:disabled{opacity:.5; cursor:not-allowed}
button.lnk{
  border:0; background:none; color:var(--blue); cursor:pointer; font:inherit;
  font-size:12px; padding:2px 5px; border-radius:4px;
}
button.lnk:hover{background:var(--blue-bg)}
button.lnk.danger{color:var(--red)}
button.lnk.danger:hover{background:var(--red-bg)}
input,select,textarea{
  font:inherit; font-size:12.5px; padding:6px 9px; border:1px solid var(--rule);
  border-radius:var(--r2); background:var(--white); color:var(--ink); width:100%;
}
input:focus,select:focus,textarea:focus{outline:2px solid var(--blue-bg); border-color:var(--blue)}
input.mono,textarea.mono{font-family:var(--mono); font-size:11.5px}
label.f{display:block; margin-bottom:11px}
label.f > .l{display:block; font-size:11px; font-weight:620; color:var(--muted); margin-bottom:4px; letter-spacing:.02em}
label.f > .h{font-size:11px; color:var(--faint); margin-top:3px; line-height:1.45}
.grid2{display:grid; grid-template-columns:1fr 1fr; gap:0 13px}
.grid3{display:grid; grid-template-columns:1fr 1fr 1fr; gap:0 13px}
@media(max-width:700px){.grid2,.grid3{grid-template-columns:1fr}}
.row{display:flex; gap:9px; align-items:center; flex-wrap:wrap}
.check{display:flex; align-items:center; gap:7px; font-size:12.5px; margin:5px 0}
.check input{width:auto; margin:0}

/* ---------- misc ---------- */
.toolbar{display:flex; gap:9px; align-items:center; flex-wrap:wrap; margin-bottom:14px}
.spin{
  width:13px;height:13px;border:2px solid var(--rule); border-top-color:var(--blue);
  border-radius:50%; display:inline-block; animation:sp .7s linear infinite; flex:none;
}
@keyframes sp{to{transform:rotate(360deg)}}
details{border:1px solid var(--rule); border-radius:var(--r2); margin-top:9px; background:var(--white)}
details > summary{
  cursor:pointer; padding:9px 12px; font-size:12.5px; font-weight:580;
  list-style:none; display:flex; align-items:center; gap:8px;
}
details > summary::-webkit-details-marker{display:none}
details > summary::before{content:"\\25B8"; color:var(--faint); font-size:10px; transition:transform .12s}
details[open] > summary::before{transform:rotate(90deg)}
details > .dbody{padding:0 12px 12px; border-top:1px solid var(--rule2)}
pre.log{
  font-family:var(--mono); font-size:11px; line-height:1.55; margin:0;
  background:var(--panel); border:1px solid var(--rule); border-radius:var(--r2);
  padding:11px; max-height:420px; overflow:auto; white-space:pre-wrap; word-break:break-all;
}
.empty{padding:34px 20px; text-align:center; color:var(--muted); font-size:13px}
.bar{height:5px; background:var(--panel2); border-radius:3px; overflow:hidden; min-width:56px}
.bar > i{display:block; height:100%; background:var(--green)}
.bar.warn > i{background:#d19b1a}
.tabs2{display:flex; gap:5px; border-bottom:1px solid var(--rule); margin-bottom:13px; flex-wrap:wrap}
.tabs2 button{
  font:inherit; font-size:12.5px; border:0; background:none; cursor:pointer;
  padding:7px 10px; color:var(--muted); border-bottom:2px solid transparent; margin-bottom:-1px;
}
.tabs2 button[aria-selected=true]{color:var(--ink); border-bottom-color:var(--blue); font-weight:600}
.kv{display:grid; grid-template-columns:auto 1fr; gap:3px 14px; font-size:12.5px}
.kv .k{color:var(--muted); white-space:nowrap}
.kv .v{font-family:var(--mono); font-size:11.5px; word-break:break-all}
.toast{
  position:fixed; bottom:20px; left:50%; transform:translateX(-50%); z-index:100;
  background:var(--ink); color:#fff; font-size:12.5px; padding:9px 16px;
  border-radius:100px; box-shadow:0 5px 22px rgba(0,0,0,.22); max-width:88vw;
}
.toast.bad{background:var(--red)} .toast.good{background:var(--green)}

/* ---------- storyboard ---------- */
.sb-grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(252px,1fr)); gap:14px}
.sb-card{
  text-align:left; cursor:pointer; font:inherit; color:var(--ink);
  border:1px solid var(--rule); border-radius:var(--r); background:var(--white);
  padding:13px 14px; transition:border-color .12s, box-shadow .12s, transform .12s;
}
.sb-card:hover{border-color:var(--blue); box-shadow:0 4px 16px rgba(26,79,138,.12); transform:translateY(-2px)}
.sb-card .row{justify-content:space-between}

/* ---------- replay filmstrip ---------- */
.striprow{display:flex; gap:6px; overflow-x:auto; padding:4px 0 10px; margin-bottom:6px}
img.strip{
  width:84px; height:84px; object-fit:cover; flex:none; cursor:pointer;
  border:1px solid var(--rule); border-radius:var(--r2);
  transition:border-color .12s, transform .12s;
}
img.strip:hover{border-color:var(--blue); transform:scale(1.06)}
"""
