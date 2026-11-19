"""The creator breakdown as a view: a clean report card that matches the Claude chat it sits in.

Two ways to show it:
  * MCP App (claude.ai and the Claude apps): scout_creator_breakdown carries _meta.ui.resourceUri = APP_URI, the host loads
    app_page() in a sandboxed frame and passes the tool result in. It follows the chat's theme, colours and font.
  * Standalone page: GET /breakdown?user=... renders render(data) with the data baked in (screen-record it, press R to replay).

No external requests: no fonts, no scripts, no images. Everything is drawn from the tool result.
"""
import html
import json

APP_URI = "ui://kestrel/breakdown.html"
APP_MIME = "text/html;profile=mcp-app"
APP_CSP = {"resourceDomains": []}
CREDIT = "Kestrel by @harshilrekhi"   # open-source credit shown small at the bottom of every breakdown

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>@@TITLE@@</title>
<style>
:root{
  --bg: var(--color-background-primary, #ffffff);
  --bg2: var(--color-background-secondary, #f6f6f4);
  --fg: var(--color-text-primary, #1f1f1e);
  --fg2: var(--color-text-secondary, #6b6a66);
  --fg3: var(--color-text-tertiary, #9a9893);
  --line: var(--color-border-primary, #e6e4df);
  --good: #1f8a4c;
  --font: var(--font-sans, ui-sans-serif, -apple-system, "Segoe UI", system-ui, Roboto, "Helvetica Neue", Arial, sans-serif);
  --r: var(--border-radius-md, 10px);
  --acc: #C13584;
  color-scheme: light;
}
:root[data-theme="dark"]{
  --bg: var(--color-background-primary, #262624); --bg2: var(--color-background-secondary, #2f2f2c);
  --fg: var(--color-text-primary, #ecebe6); --fg2: var(--color-text-secondary, #a8a69f); --fg3: var(--color-text-tertiary, #7d7b75);
  --line: var(--color-border-primary, #3b3a37); --good: #4cc27e; color-scheme: dark;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg: var(--color-background-primary, #262624); --bg2: var(--color-background-secondary, #2f2f2c);
  --fg: var(--color-text-primary, #ecebe6); --fg2: var(--color-text-secondary, #a8a69f); --fg3: var(--color-text-tertiary, #7d7b75);
  --line: var(--color-border-primary, #3b3a37); --good: #4cc27e; color-scheme: dark; } }
:root[data-platform="youtube"]{ --acc: #E62117 }
:root[data-platform="x"]{ --acc: var(--fg) }
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:transparent;color:var(--fg);font:14px/1.5 var(--font);-webkit-font-smoothing:antialiased}
body.page{background:var(--bg2);min-height:100vh;display:flex;justify-content:center;padding:28px 16px}
.card{position:relative;width:100%;max-width:640px;background:var(--bg);border:1px solid var(--line);border-radius:calc(var(--r) + 4px);overflow:hidden}
body.app .card{border:0;border-radius:0;max-width:none}
.num{font-variant-numeric:tabular-nums}
section{padding:18px 20px;border-top:1px solid var(--line)}
section:first-child{border-top:0}
h2{font-size:13px;font-weight:600;margin:0 0 10px;color:var(--fg)}
h2 small{font-weight:400;color:var(--fg2);font-size:13px;margin-left:6px}
.muted{color:var(--fg2)}

/* header */
.head{display:flex;gap:14px;align-items:center}
.av{width:44px;height:44px;border-radius:50%;flex:none;display:grid;place-items:center;font-weight:650;font-size:18px;color:#fff;background:var(--acc)}
:root[data-platform="x"] .av{color:var(--bg)}
.who{min-width:0;flex:1}
.who b{font-size:17px;font-weight:650;letter-spacing:-.01em;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.who span{color:var(--fg2);font-size:13px}
.plat{font-size:12px;color:var(--fg2);border:1px solid var(--line);border-radius:999px;padding:3px 10px;white-space:nowrap;display:flex;gap:6px;align-items:center}
.plat i{width:7px;height:7px;border-radius:50%;background:var(--acc);display:inline-block}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:16px}
.stat{padding:2px 0}
.stat b{display:block;font-size:18px;font-weight:600;letter-spacing:-.01em}
.stat span{font-size:12px;color:var(--fg2)}

/* step log */
.log{list-style:none;margin:0;padding:0;font-size:13px}
.log li{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:8px;padding:5px 0;align-items:start}
.log li>span:nth-child(2){min-width:0}
.sum{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--fg2);background:none;border:0;padding:0;cursor:pointer;font-family:var(--font)}
.sum .ok{width:16px;height:16px;border-radius:50%;display:grid;place-items:center;background:color-mix(in srgb,var(--good) 16%,transparent);color:var(--good)}
.sum .ok svg{width:10px;height:10px}
.sum b{font-weight:500;color:var(--fg)}
.sum:hover b{text-decoration:underline}
.sum:focus-visible{outline:2px solid var(--acc);outline-offset:3px;border-radius:4px}
.log li[hidden]{display:none}
.log .ok{width:16px;height:16px;border-radius:50%;margin-top:2px;display:grid;place-items:center;background:color-mix(in srgb,var(--good) 16%,transparent);color:var(--good)}
.log .ok svg{width:10px;height:10px}
.log .run{width:14px;height:14px;margin:3px 1px;border-radius:50%;border:2px solid var(--line);border-top-color:var(--acc);animation:spin .8s linear infinite}
.log em{font-style:normal;color:var(--fg2);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log time{color:var(--fg3);font-size:12px}
@keyframes spin{to{transform:rotate(360deg)}}
.bar{height:2px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:12px}
.bar i{display:block;height:100%;width:30%;background:var(--acc);animation:slide 1.2s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}

/* outlier strip */
.strip{width:100%;height:auto;display:block;overflow:visible}
.strip .dot{fill:var(--fg3);opacity:.55}
.strip .hit{fill:var(--acc);opacity:1}
.strip .axis{stroke:var(--line)}
.strip .usual{stroke:var(--fg2);stroke-dasharray:3 3}
.strip text{fill:var(--fg2);font:11px var(--font)}
.strip .lab{fill:var(--fg);font-weight:600}
.strip .dot,.strip .hit{transform-box:fill-box;transform-origin:center;animation:drop .5s cubic-bezier(.2,.8,.2,1) both}
@keyframes drop{from{opacity:0;transform:translateY(-14px)}}

/* top posts */
.top{list-style:none;margin:0;padding:0}
.top li{display:grid;grid-template-columns:22px 1fr auto;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.top li:first-child{border-top:0;padding-top:2px}
.top .rk{color:var(--fg3);font-size:13px}
.top a{color:var(--fg);text-decoration:none;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.top a:hover{text-decoration:underline}
.top .meta{font-size:12px;color:var(--fg2);margin-top:2px}
.top .x{text-align:right;font-weight:600;white-space:nowrap}
.top .x small{display:block;font-weight:400;color:var(--fg2);font-size:12px}

/* two columns */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:28px}
.hb{display:grid;grid-template-columns:76px 1fr 44px;gap:8px;align-items:center;font-size:13px;padding:3px 0}
.hb .t{height:6px;border-radius:3px;background:var(--bg2);overflow:hidden}
.hb .t i{display:block;height:100%;background:var(--acc);border-radius:3px;width:0;transition:width .7s cubic-bezier(.2,.8,.2,1)}
.hb .v{text-align:right;color:var(--fg2)}
.vs{width:100%;border-collapse:collapse;font-size:13px}
.vs th{text-align:left;font-weight:500;color:var(--fg2);padding:4px 0}
.vs td{padding:5px 0;border-top:1px solid var(--line)}
.vs td:first-child{color:var(--fg2)}
.vs th:not(:first-child),.vs td:not(:first-child){text-align:right}

/* the one to borrow */
.quote{font-size:16px;line-height:1.45;font-weight:550;letter-spacing:-.005em;padding-left:12px;border-left:3px solid var(--acc);margin:0 0 8px}
.why{font-size:13px;color:var(--fg2);margin:0 0 12px}
.tx{background:var(--bg2);border-radius:var(--r);padding:12px 14px;font-size:13px;line-height:1.55;color:var(--fg)}
.tx .k{display:block;font-size:12px;color:var(--fg2);margin-bottom:4px}

.moves{margin:0;padding-left:18px}
.moves li{padding:3px 0}
.foot{display:flex;justify-content:space-between;gap:12px;padding:12px 20px;font-size:12px;color:var(--fg3);border-top:1px solid var(--line)}
.foot b{font-weight:500;color:var(--fg2)}
.next{color:var(--fg2);font-size:13px;margin-top:12px}
.err{color:#c4372b}
.shot{position:absolute;inset:0;background:color-mix(in srgb,var(--bg) 94%,transparent);display:flex;align-items:flex-start;justify-content:center;padding:20px;z-index:5;overflow:auto}
.shotin{max-width:420px;width:100%;text-align:center}
.shotin img{width:100%;height:auto;border-radius:var(--r);border:1px solid var(--line);display:block}
.shotin p{font-size:13px;color:var(--fg2);margin:10px 0}
.shotin button{font:13px var(--font);color:var(--fg);background:var(--bg2);border:1px solid var(--line);border-radius:999px;padding:5px 14px;cursor:pointer}
.reveal{opacity:0;transition:opacity .45s ease}
.reveal.on{opacity:1}
.ctl{display:flex;gap:6px}
.ctl button{font:12px var(--font);color:var(--fg2);background:transparent;border:1px solid var(--line);border-radius:999px;padding:3px 10px;cursor:pointer}
.ctl button:hover{color:var(--fg)}
.ctl button:focus-visible,.top a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
@media (max-width:520px){ .stats{grid-template-columns:repeat(2,1fr)} .cols{grid-template-columns:1fr;gap:18px} section{padding:16px} }
@media (prefers-reduced-motion:reduce){ *,*:before,*:after{animation:none!important;transition:none!important} }
</style></head>
<body class="@@MODE@@">
<main class="card" id="card">
  <section>
    <div class="head">
      <div class="av" id="av">@@INITIAL@@</div>
      <div class="who"><b id="handle">@@@USER@@</b><span id="sub">Reading their recent posts</span></div>
      <div class="plat" id="plat"><i></i><span id="platname">@@SITE@@</span></div>
    </div>
    <div class="stats num" id="stats" hidden></div>
  </section>
  <section>
    <button class="sum" id="sum" type="button" hidden aria-expanded="false"></button>
    <ol class="log" id="log"></ol>
    <div class="bar" id="bar"><i></i></div>
  </section>
  <div id="body"></div>
  <footer class="foot"><span>@@CREDIT@@</span><span class="ctl" id="ctl" hidden><button id="img" type="button">Save image</button><button id="rp" type="button">Replay</button><button id="fs" type="button">Full screen</button></span><span id="took"></span></footer>
  <div class="shot" id="shot" hidden><div class="shotin"><img id="shotimg" alt="Shareable image of this breakdown"><p>Right-click or long-press the image to save it, then post it anywhere.</p><button id="shotx" type="button">Close</button></div></div>
</main>
<script>
const D0 = @@DATA@@, SPEED = @@SPEED@@, APP = @@APP@@, CTA = @@CTA_JS@@;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = n => n == null ? "n/a" : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M" : n >= 1e4 ? Math.round(n / 1e3) + "K" : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K" : String(n);
const fx = x => (x >= 10 ? Math.round(x) : Math.round(x * 10) / 10) + "×";
const CHECK = '<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 5.2 4.2 7.4 8 3"/></svg>';
let RUN = 0;
const wait = ms => { const t = RUN; return new Promise((r, j) => setTimeout(() => t === RUN ? r() : j(0), ms / SPEED)); };
const PLAT = {instagram: "Instagram", youtube: "YouTube", x: "X"};

function setPlatform(p) { p = (p || "instagram").toLowerCase(); p = {ig: "instagram", yt: "youtube", twitter: "x"}[p] || p;
  document.documentElement.dataset.platform = p; $("#platname").textContent = PLAT[p] || p; return p; }
function setUser(u) { u = String(u || "").replace(/^@/, ""); if (!u) return; $("#handle").textContent = "@" + u; $("#av").textContent = u[0].toUpperCase(); }

function strip(D) {
  const xs = (D.distribution || []).filter(x => x > 0), hits = (D.viral_reels || []).map(r => r.vs_their_usual);
  if (xs.length < 3) return "";
  const W = Math.max(280, Math.min(640, ($("#card").clientWidth || 640) - 40)), H = 92, L = 8, R = 8, lo = Math.min(0.1, Math.min(...xs)), hi = Math.max(10, Math.max(...xs) * 1.15);
  const X = v => L + (Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo)) * (W - L - R);
  const bins = {}; let out = "", n = 0;
  const hitLeft = hits.slice();
  for (const v of xs) {
    const cx = X(v), b = Math.round(cx / 9), k = bins[b] = (bins[b] || 0) + 1, cy = 58 - (k - 1) * 9;
    const i = hitLeft.indexOf(v), hit = i >= 0 && v >= 1.5; if (hit) hitLeft.splice(i, 1);
    out += `<circle class="${hit ? "hit" : "dot"}" cx="${cx.toFixed(1)}" cy="${Math.max(6, cy)}" r="4" style="animation-delay:${(n++ * 18 / SPEED) | 0}ms"/>`;
  }
  const ticks = [0.1, 1, 10, 100, 1000].filter(t => t >= lo && t <= hi).map(t => `<text x="${X(t)}" y="86" text-anchor="middle">${t < 1 ? t : t}×</text>`).join("");
  const top = D.viral_reels && D.viral_reels[0];
  const lab = top ? `<text class="lab" x="${Math.min(X(top.vs_their_usual), W - 4)}" y="${Math.max(6, 58 - ((bins[Math.round(X(top.vs_their_usual) / 9)] || 1) - 1) * 9) - 10}" text-anchor="end">${fx(top.vs_their_usual)}</text>` : "";
  return `<svg class="strip" viewBox="0 0 ${W} ${H}" role="img" aria-label="Each ${esc(D.unit || "post")} against their usual">
    <line class="axis" x1="${L}" x2="${W - R}" y1="66" y2="66"/><line class="usual" x1="${X(1)}" x2="${X(1)}" y1="2" y2="70"/>
    <text x="${X(1) + 5}" y="12">their usual</text>${out}${lab}${ticks}</svg>`;
}

function body(D) {
  const W = D.what_works_for_them || {}, Y = D.you, B = D.script_brief || {}, unit = D.unit || "posts", metric = D.metric || "likes";
  const top = (D.viral_reels || []).map((r, i) => `<li><span class="rk num">${i + 1}</span><div><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.hook || "(no caption)")}</a>
      <div class="meta num">${fmt(r.likes)} ${esc(metric)}${r.comments != null ? ", " + fmt(r.comments) + " comments" : ""}${r.transcript ? ", transcribed" : ""}${r.boosted ? ", likely paid boost" : ""}</div></div>
      <div class="x num">${fx(r.vs_their_usual)}<small>their usual</small></div></li>`).join("");
  const ht = (W.hook_types || []).slice(0, 5), hmax = Math.max(1, ...ht.map(h => h.median_x));
  const hooks = ht.map(h => `<div class="hb num"><span>${esc(h.hook_type)}</span><span class="t"><i data-w="${Math.max(4, h.median_x / hmax * 100)}"></i></span><span class="v">${fx(h.median_x)}</span></div>`).join("");
  const vs = Y ? `<table class="vs num"><tr><th></th><th>Them</th><th>You</th></tr>
      <tr><td>${esc(unit[0].toUpperCase() + unit.slice(1))} a week</td><td>${W.reels_per_week ?? "n/a"}</td><td>${Y.reels_per_week ?? "n/a"}</td></tr>
      <tr><td>Best hook</td><td>${esc(W.best_hook || "n/a")}</td><td>${esc(Y.best_hook || "n/a")}</td></tr>
      <tr><td>Best time</td><td>${esc(W.best_slot || "n/a")}</td><td>${esc(Y.best_slot || "n/a")}</td></tr></table>`
    : `<p class="muted" style="margin:0">Posts about ${W.reels_per_week ?? "?"} ${esc(unit)} a week. Best time ${esc(W.best_slot || "unclear")}.${(W.winning_topics || []).length ? " Topics that win: " + esc(W.winning_topics.slice(0, 4).join(", ")) + "." : ""}</p>`;
  const tx = B.text && B.source !== "caption" && B.source !== "title" ? `<div class="tx"><span class="k">${B.source === "post" ? "The post" : "Transcript"}${B.their_length_seconds ? ", about " + B.their_length_seconds + " seconds" : ""}</span>${esc(B.text.length > 420 ? B.text.slice(0, 420).replace(/\s\S*$/, "") + "…" : B.text)}</div>` : "";
  return `
  <section class="reveal"><h2>Every ${esc(D.unit ? D.unit.replace(/s$/, "") : "post")} against their usual<small>${D.reels_read} read, usual is ${fmt(D.usual)} ${esc(metric)}</small></h2>${strip(D)}</section>
  <section class="reveal"><h2>Top ${esc(unit)}</h2><ol class="top">${top}</ol></section>
  <section class="reveal"><div class="cols"><div><h2>Hooks that work for them</h2>${hooks || '<p class="muted" style="margin:0">Not enough hooks to compare.</p>'}</div><div><h2>${Y ? "Them and you" : "How they post"}</h2>${vs}</div></div></section>
  ${B.hook ? `<section class="reveal"><h2>The one to borrow<small>${fx(B.vs_their_usual)} their usual</small></h2><p class="quote">${esc(B.hook)}</p><p class="why">${esc((B.why_this_one || "").replace(/^[\d.]+x their usual[,.]?\s*/, "").replace(/^./, c => c.toUpperCase()))}</p>${tx}</section>` : ""}
  <section class="reveal"><h2>What to do next</h2><ul class="moves">${(D.what_you_should_do || []).map(a => `<li>${esc(a)}</li>`).join("")}</ul>${APP ? '<p class="next">Your script is written below this card.</p>' : ""}</section>`;
}

function header(D) {
  setPlatform(D.platform); setUser(D.creator);
  const W = D.what_works_for_them || {};
  $("#sub").textContent = (D.name && D.name.toLowerCase() !== String(D.creator).replace("@", "").toLowerCase() ? D.name + ", " : "") + "breakdown of " + D.reels_read + " recent " + (D.unit || "posts");
  const st = [[fmt(D.followers ?? W.followers), D.platform === "youtube" ? "subscribers" : "followers"], [fmt(D.usual), "usual " + (D.metric || "likes")],
              [W.reels_per_week != null ? W.reels_per_week : "n/a", (D.unit || "posts") + " a week"], [fx((D.viral_reels || [{}])[0].vs_their_usual || 0), "best vs usual"]];
  $("#stats").innerHTML = st.map(s => `<div class="stat"><b>${esc(s[0])}</b><span>${esc(s[1])}</span></div>`).join("");
}

async function play(D) {
  header(D);
  const log = $("#log"); log.innerHTML = ""; log.hidden = false; $("#sum").hidden = true; $("#body").innerHTML = ""; $("#bar").hidden = false;
  for (const s of D.steps || []) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="run"></span><span>${esc(s.did)}<em>${esc(s.found)}</em></span><time class="num">${(s.at_seconds || 0).toFixed(1)}s</time>`;
    log.appendChild(li); await wait(170);
    li.firstElementChild.outerHTML = `<span class="ok">${CHECK}</span>`;
  }
  $("#bar").hidden = true; $("#stats").hidden = false;
  const n = (D.steps || []).length, sum = $("#sum");
  sum.innerHTML = `<span class="ok">${CHECK}</span><span><b>${n} steps in ${D.seconds != null ? D.seconds + "s" : "a few seconds"}</b>, show the log</span>`;
  $("#took").textContent = D.seconds != null ? "Done in " + D.seconds + "s" : "";
  $("#body").innerHTML = body(D);
  await wait(60);
  document.querySelectorAll(".reveal").forEach(e => e.classList.add("on"));
  document.querySelectorAll(".hb .t i").forEach(e => e.style.width = e.dataset.w + "%");
  await wait(2600);   // let the log be read (and filmed), then fold it away
  log.hidden = true; sum.hidden = false;
}
$("#sum").onclick = () => { const l = $("#log"), open = l.hidden; l.hidden = !open; $("#sum").setAttribute("aria-expanded", String(open));
  $("#sum").querySelector("span:last-child").innerHTML = $("#sum").querySelector("b").outerHTML + (open ? ", hide the log" : ", show the log"); };

/* ---- shareable image: 1080x1350, made in the browser from the same data, no network ---- */
function shareImage(D) {
  const W = 1080, H = 1350, c = document.createElement("canvas"); c.width = W; c.height = H;
  const x = c.getContext("2d"), F = 'ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif';
  const acc = {instagram: "#C13584", youtube: "#E62117", x: "#ECEBE6"}[D.platform] || "#C13584";
  const fg = "#ECEBE6", fg2 = "#A8A69F", fg3 = "#6E6C66", bg = "#1B1B1A", line = "#33322F";
  x.fillStyle = bg; x.fillRect(0, 0, W, H);
  const P = 84, font = (w, sz) => x.font = `${w} ${sz}px ${F}`;
  const wrap = (t, maxW, maxL) => { const ws = String(t || "").split(/\s+/), out = []; let l = "";
    for (const w of ws) { const tl = l ? l + " " + w : w; if (x.measureText(tl).width > maxW && l) { out.push(l); l = w; if (out.length === maxL) break; } else l = tl; }
    if (out.length < maxL && l) out.push(l); else if (out.length === maxL) out[maxL - 1] = out[maxL - 1].replace(/\s*\S*$/, "") + "…"; return out; };
  // header
  x.fillStyle = acc; x.beginPath(); x.arc(P + 44, 128, 44, 0, Math.PI * 2); x.fill();
  font(700, 44); x.fillStyle = D.platform === "x" ? bg : "#fff"; x.textAlign = "center"; x.textBaseline = "middle";
  x.fillText(String(D.creator || "@").replace("@", "").slice(0, 1).toUpperCase(), P + 44, 130); x.textAlign = "left"; x.textBaseline = "alphabetic";
  font(700, 52); x.fillStyle = fg; x.fillText(D.creator || "", P + 112, 122);
  font(400, 28); x.fillStyle = fg2; x.fillText(`${D.site || "Instagram"} breakdown of ${D.reels_read} recent ${D.unit || "posts"}`, P + 112, 166);
  // stats
  const W0 = D.what_works_for_them || {}, top = (D.viral_reels || [])[0] || {};
  const st = [[fmt(D.followers ?? W0.followers), D.platform === "youtube" ? "subscribers" : "followers"], [fmt(D.usual), "usual " + (D.metric || "likes")],
              [fx(top.vs_their_usual || 0), "best vs usual"]];
  st.forEach((s2, i) => { const sx = P + i * 310; font(700, 64); x.fillStyle = i === 2 ? acc : fg; x.fillText(s2[0], sx, 300); font(400, 26); x.fillStyle = fg2; x.fillText(s2[1], sx, 340); });
  x.fillStyle = line; x.fillRect(P, 392, W - 2 * P, 2);
  // strip chart
  font(600, 28); x.fillStyle = fg; x.fillText(`Every ${String(D.unit || "post").replace(/s$/, "")} against their usual`, P, 450);
  const xs = (D.distribution || []).filter(v => v > 0), lo = Math.min(0.1, ...xs), hi = Math.max(10, Math.max(...xs, 1) * 1.15);
  const X = v => P + (Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo)) * (W - 2 * P), base = 590, bins = {};
  x.fillStyle = line; x.fillRect(P, base + 14, W - 2 * P, 2);
  x.setLineDash([6, 6]); x.strokeStyle = fg3; x.lineWidth = 2; x.beginPath(); x.moveTo(X(1), 476); x.lineTo(X(1), base + 20); x.stroke(); x.setLineDash([]);
  font(400, 22); x.fillStyle = fg2; x.fillText("their usual", X(1) + 10, 494);
  const hits = (D.viral_reels || []).map(r => r.vs_their_usual);
  for (const v of xs) { const cx = X(v), b = Math.round(cx / 16), k = bins[b] = (bins[b] || 0) + 1, cy = base - (k - 1) * 16, hit = hits.includes(v) && v >= 1.5;
    x.fillStyle = hit ? acc : fg3; x.globalAlpha = hit ? 1 : .6; x.beginPath(); x.arc(cx, Math.max(500, cy), 7, 0, Math.PI * 2); x.fill(); x.globalAlpha = 1; }
  [0.1, 1, 10, 100, 1000].filter(t => t >= lo && t <= hi).forEach(t => { font(400, 22); x.fillStyle = fg2; x.textAlign = "center"; x.fillText(t + "×", X(t), base + 52); });
  x.textAlign = "left";
  // top 3
  font(600, 28); x.fillStyle = fg; x.fillText(`Top ${D.unit || "posts"}`, P, 712);
  let y = 770;
  (D.viral_reels || []).slice(0, 3).forEach((r, i) => {
    font(400, 30); x.fillStyle = fg3; x.fillText(String(i + 1), P, y);
    font(500, 32); x.fillStyle = fg; const ls = wrap(r.hook || "(no caption)", W - 2 * P - 230, 2);
    ls.forEach((l, j) => x.fillText(l, P + 48, y + j * 42));
    font(700, 40); x.fillStyle = r.boosted ? fg3 : acc; x.textAlign = "right"; x.fillText(fx(r.vs_their_usual), W - P, y + 4);
    font(400, 22); x.fillStyle = fg2; x.fillText(r.boosted ? "paid boost" : fmt(r.likes) + " " + (D.metric || "likes"), W - P, y + 38); x.textAlign = "left";
    y += Math.max(ls.length, 1) * 42 + 52;
  });
  // what works
  y = Math.max(y + 10, 1110); x.fillStyle = line; x.fillRect(P, y - 50, W - 2 * P, 2);
  font(400, 28); x.fillStyle = fg2; x.fillText("What works for them", P, y);
  font(600, 34); x.fillStyle = fg;
  x.fillText(`${W0.best_hook ? W0.best_hook[0].toUpperCase() + W0.best_hook.slice(1) + " hooks" : "Hooks vary"}, ${W0.reels_per_week ?? "?"} ${D.unit || "posts"} a week, best at ${String(W0.best_slot || "any time").replace(/ \d.*/, "")}`, P, y + 50);
  // footer credit
  font(600, 26); x.fillStyle = fg; x.fillText("Made with Kestrel", P, H - 70);
  font(400, 24); x.fillStyle = fg2; x.textAlign = "right"; x.fillText("Made with Kestrel. Free and open source on GitHub.", W - P, H - 70); x.textAlign = "left";
  return c.toDataURL("image/png");
}
function saveImage() {
  if (!LAST) return;
  const url = shareImage(LAST);
  if (!APP) { const a = document.createElement("a"); a.href = url; a.download = String(LAST.creator || "breakdown").replace("@", "") + "-kestrel.png"; document.body.appendChild(a); a.click(); a.remove(); }
  $("#shotimg").src = url; $("#shot").hidden = false;
}
$("#img").onclick = saveImage;
$("#shotx").onclick = () => { $("#shot").hidden = true; };

let LAST = null;
async function run(D) { LAST = D; RUN++; try { await play(D); } catch (e) { if (e !== 0) throw e; } }
function replay() { if (LAST) run(LAST); }
document.addEventListener("keydown", e => { if ((e.key === "r" || e.key === "R") && !e.metaKey && !e.ctrlKey) replay(); });
function fail(msg) { $("#bar").hidden = true; $("#log").innerHTML = `<li><span></span><span class="err">${esc(msg)}</span><time></time></li>`; }

/* ---- inside a Claude chat (MCP Apps) ---- */
function boot() {
  const root = document.documentElement; let rid = 0, mode = "inline", ready = false; const pend = {};
  const post = m => parent.postMessage(Object.assign({jsonrpc: "2.0"}, m), "*");
  const rpc = (method, params, ms) => new Promise((res, rej) => { const id = ++rid; pend[id] = res; post({id, method, params});
    setTimeout(() => { if (pend[id]) { delete pend[id]; rej(0); } }, ms); });
  const size = () => { if (ready) post({method: "ui/notifications/size-changed", params: {height: Math.ceil($("#card").getBoundingClientRect().height)}}); };
  new ResizeObserver(size).observe($("#card"));
  function ctx(c) { if (!c) return;
    if (c.theme) root.dataset.theme = c.theme;
    const v = c.styles && c.styles.variables; if (v) for (const k in v) if (v[k]) root.style.setProperty(k, v[k]);
    if (c.displayMode) mode = c.displayMode; }
  const go = () => { if (ready) return; ready = true; post({method: "ui/notifications/initialized", params: {}}); size(); };
  addEventListener("message", e => { const m = e.data; if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && !m.method && pend[m.id]) { const r = pend[m.id]; delete pend[m.id]; r(m.result || {}); return; }
    const p = m.params || {};
    if (m.method === "ui/notifications/tool-input" || m.method === "ui/notifications/tool-input-partial") {
      const a = p.arguments || {}; setUser(a.user); setPlatform(a.platform);
      $("#log").innerHTML = `<li><span class="run"></span><span>Reading @${esc(String(a.user || "").replace(/^@/, ""))} on ${esc(PLAT[setPlatform(a.platform)] || "Instagram")}<em>Profile, recent posts, the top one's audio</em></span><time></time></li>`;
    } else if (m.method === "ui/notifications/tool-result") {
      let d = p.structuredContent; if (!d) try { d = JSON.parse((p.content || [])[0].text); } catch (x) { d = null; }
      if (!d || d.error || p.isError || !d.steps) return fail("Couldn't read this account: " + ((d && d.error) || "no data came back"));
      if (LAST && LAST.creator === d.creator && LAST.seconds === d.seconds) return;   // already showing it (ChatGPT can send it twice)
      $("#ctl").hidden = false; run(d);
    } else if (m.method === "ui/notifications/tool-cancelled") fail("Stopped.");
    else if (m.method === "ui/notifications/host-context-changed") ctx(p);
    else if (m.method === "ui/resource-teardown" || m.method === "ping") post({id: m.id, result: {}});
  });
  $("#rp").onclick = replay;
  $("#fs").onclick = () => rpc("ui/request-display-mode", {mode: mode === "fullscreen" ? "inline" : "fullscreen"}, 3000).then(r => { mode = r.mode || mode; }, () => {});
  rpc("ui/initialize", {protocolVersion: "2026-01-26", appInfo: {name: "kestrel-breakdown", version: "1.4.0"},
      appCapabilities: {availableDisplayModes: ["inline", "fullscreen"]}}, 5000).then(r => { ctx(r && r.hostContext); go(); }, go);
  setTimeout(go, 250);
}
/* ChatGPT also exposes the same data on window.openai; use it if the standard message doesn't arrive first */
function openaiCompat() {
  const oa = window.openai; if (!oa) return;
  const apply = () => {
    if (oa.theme) document.documentElement.dataset.theme = oa.theme;
    const a = oa.toolInput || {}; if (a.user && !LAST) { setUser(a.user); setPlatform(a.platform); }
    let d = oa.toolOutput; if (d && d.structuredContent) d = d.structuredContent;
    if (d && d.steps && !LAST) { $("#ctl").hidden = false; run(d); }
  };
  apply(); addEventListener("openai:set_globals", apply);
}
if (APP) { boot(); openaiCompat(); } else { $("#fs").remove(); $("#ctl").hidden = false; run(D0); }
</script></body></html>
"""


def _fill(data_js, user, cta, speed, app, site="Instagram"):
    cta = (cta or "").strip().lstrip("@")
    user = (user or "").lstrip("@")
    return (PAGE.replace("@@SPEED@@", str(max(0.25, min(float(speed or 1), 4))))
            .replace("@@APP@@", "true" if app else "false").replace("@@MODE@@", "app" if app else "page")
            .replace("@@CTA_JS@@", json.dumps(cta)).replace("@@SITE@@", html.escape(site))
            .replace("@@TITLE@@", html.escape(("@%s on %s" % (user, site)) if user and user != "..." else "Creator breakdown"))
            .replace("@@CREDIT@@", html.escape(CREDIT if not cta or cta.lower() in CREDIT.lower() else "%s. Follow @%s" % (CREDIT, cta)))
            .replace("@@USER@@", html.escape(user)).replace("@@INITIAL@@", html.escape((user or "K")[:1].upper()))
            .replace("@@DATA@@", data_js))


def render(data, cta="", speed=1.0):
    """Standalone page with the data baked in (GET /breakdown)."""
    return _fill(json.dumps(data, ensure_ascii=False).replace("</", "<\\/"), data.get("creator", ""), cta, speed, False,
                 data.get("site") or "Instagram")


def app_page(cta="", speed=1.0):
    """The same view as an MCP App: Claude chats render it inline when scout_creator_breakdown runs."""
    return _fill("null", "...", cta, speed, True)
