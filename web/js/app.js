import { $, $$, esc, fa, ago, hhmm, api, post, on, store, connectEvents, toast, disposeCharts, LANG, STATUS_FA, MODE_FA, catChip, reducedMotion } from "./core.js";
import * as V from "./views.js";
import { renderPipeline } from "./pipeline.js";
import { renderAsk, renderSettings } from "./ai.js";
import { renderGraph } from "./graph.js";
import { renderAlerts, initBell } from "./alerts.js";
import { renderExplore } from "./explore.js";
import { renderAdmin, renderWatch } from "./admin.js";
import { authStatus, showLogin, renderUserMenu, applyRole } from "./auth.js";
import { can } from "./core.js";
import { renderAnalysis } from "./analysis.js";

const I = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const ROUTES = [
  { id: "analysis", fa: "تحلیل مستند و شواهد", icon: I('<path d="M6 3h9l4 4v14H6zM9 11h7M9 15h7"/>'), render: renderAnalysis, noDrawer: true },
  { id: "command", fa: "اتاق فرمان", icon: I('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'), render: V.renderCommand },
  { id: "map", fa: "کره و نقشه رویدادها", icon: I('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'), render: V.renderMap, noDrawer: true },
  { id: "graph", fa: "گراف دانش", icon: I('<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="7" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8 7l7.5.5M7.5 8l3.5 8M16.8 9.2 13.3 16"/>'), render: renderGraph, noDrawer: true },
  { id: "stories", fa: "خط زمانی و داستان‌ها", icon: I('<path d="M4 6h16M4 12h10M4 18h13"/><circle cx="19" cy="12" r="1.5"/>'), render: V.renderStories },
  { id: "trends", fa: "رودخانه موضوعی و روندها", icon: I('<path d="M3 17c3-6 5 2 9-4s6-2 9-6"/><path d="M3 21c3-4 6 0 9-3s6-1 9-4"/>'), render: V.renderTrends },
  { id: "sources", fa: "مقایسه پوشش منابع", icon: I('<path d="M4 5h16v4H4zM4 11h10v4H4zM4 17h13v3H4z"/>'), render: V.renderSources },
  { id: "pipeline", fa: "خط لوله عامل‌ها", icon: I('<rect x="2.5" y="9" width="5" height="6" rx="1.5"/><rect x="9.5" y="4" width="5" height="6" rx="1.5"/><rect x="9.5" y="14" width="5" height="6" rx="1.5"/><rect x="16.5" y="9" width="5" height="6" rx="1.5"/><path d="M7.5 12h2M14.5 7l2 3M14.5 17l2-3"/>'), render: renderPipeline },
  { id: "ask", fa: "دستیار تحلیلی (پرسش از رصد)", icon: I('<path d="M12 3l1.8 4.2L18 9l-4.2 1.8L12 15l-1.8-4.2L6 9l4.2-1.8z"/><path d="M18.5 15l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8z"/>'), render: renderAsk, noDrawer: true },
  { id: "alerts", fa: "مرکز هشدارها", icon: I('<path d="M12 3.5 2.8 19.5h18.4z"/><path d="M12 10v4.2M12 17.2v.1"/>'), render: renderAlerts, noDrawer: true },
  { id: "explore", fa: "کاوش و جست‌وجوی پیشرفته", icon: I('<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8M8 10.5h5M10.5 8v5"/>'), render: renderExplore, noDrawer: true },
  { id: "watch", fa: "فهرست‌های پایش", icon: I('<path d="M2.5 12s3.5-6.5 9.5-6.5 9.5 6.5 9.5 6.5-3.5 6.5-9.5 6.5S2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>'), render: renderWatch, noDrawer: true },
  { id: "briefs", fa: "بریف و گزارش", icon: I('<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 12h7M9 16h7"/>'), render: V.renderBriefs },
];

function buildRail() {
  $("#rail").innerHTML = ROUTES.filter((r) => !r.hidden).map((r) => `<a href="#/${r.id}" data-r="${r.id}">${r.icon}<span class="tip">${r.fa}</span></a>`).join("") +
    `<div class="sep"></div>${can("admin") ? `<a href="#/admin" data-r="admin">${I('<circle cx="9" cy="8" r="3.2"/><path d="M3 20c.6-3.4 3-5.5 6-5.5s5.4 2.1 6 5.5"/><path d="M16.5 8.5h5M19 6v5"/>')}<span class="tip">مدیریت سامانه</span></a>` : ""}<a href="#/qa" data-r="qa">${I('<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>')}<span class="tip">ارزیابی کیفیت و ممیزی</span></a><a href="#/settings" data-r="settings">${I('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>')}<span class="tip">تنظیمات و مصرف هوش مصنوعی</span></a>`;
}
ROUTES.push({ id: "admin", fa: "مدیریت سامانه", render: renderAdmin, hidden: true, noDrawer: true }, { id: "qa", fa: "کیفیت و ممیزی", render: V.renderQA, hidden: true }, { id: "settings", fa: "تنظیمات هوش مصنوعی", render: renderSettings, hidden: true, noDrawer: true });

let current = null, cleanup = null;
async function route() {
  const [, id, ...rest] = (location.hash || "#/command").split("/");
  const r = ROUTES.find((x) => x.id === id) || ROUTES[0];
  if (cleanup) { try { cleanup(); } catch {} cleanup = null; }
  disposeCharts();
  current = r;
  $$("#rail a").forEach((a) => a.classList.toggle("on", a.dataset.r === r.id));
  $("#main").classList.toggle("no-drawer", !!r.noDrawer);
  $("#body").classList.remove("no-panel");
  $("#view").innerHTML = '<div class="view-skel"><div class="skel h"></div><div class="skel-row"><div class="skel b"></div><div class="skel b"></div><div class="skel b"></div></div><div class="skel t"></div></div>';
  $("#view").classList.remove("enter"); void $("#view").offsetWidth; $("#view").classList.add("enter");
  $("#side").innerHTML = "";
  try { cleanup = (await r.render({ view: $("#view"), side: $("#side"), params: rest.map(decodeURIComponent), body: $("#body") })) || null; }
  catch (e) { console.error(e); $("#view").innerHTML = `<div class="empty">خطا در بارگذاری نما: ${esc(e.message)}</div>`; }
  document.title = `${r.fa} · رصد`;
}
window.addEventListener("hashchange", route);

// ---------- topbar ----------
function renderState(st) {
  if (!st) return;
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === st.mode));
  const pill = $("#runPill");
  let label = STATUS_FA[st.status] || st.status;
  if (st.status === "idle" && st.next_run_at) label += ` · اجرای بعد ${hhmm(st.next_run_at)}`;
  if (st.status === "running" && st.current_step) label += ` · ${store.state.pipeline.find((p) => st.current_step.endsWith(":" + p.name))?.title_fa || ""}`;
  pill.innerHTML = `<span class="dot ${st.status}"></span><span>${esc(label)} · ${MODE_FA[st.mode]}</span>`;
  const kb = $("#killBtn");
  kb.textContent = st.killed ? "⟲ فعال‌سازی مجدد" : "⏻ توقف اضطراری";
  kb.classList.toggle("solid", st.killed);
}
$("#modeSeg").addEventListener("click", async (e) => {
  const m = e.target.dataset.mode; if (!m) return;
  if (m === "autopilot" && !confirm("حالت خودکار کامل: همه مراحل و دروازه‌های تأیید بدون توقف اجرا می‌شوند. ادامه؟")) return;
  await post("/api/control", { action: "set_mode", mode: m }); toast(`سطح اجرا: ${MODE_FA[m]}`, "info");
});
$("#killBtn").addEventListener("click", async () => {
  if (store.state?.killed) {
    const c = prompt("برای فعال‌سازی مجدد زمان‌بند، عبارت ARM را تایپ کنید:");
    if (c === "ARM") { await post("/api/control", { action: "arm", confirm: "ARM" }); toast("سامانه دوباره مسلح شد", "success"); }
  } else if (confirm("توقف اضطراری: همه اجراها لغو و زمان‌بند غیرفعال می‌شود. مطمئنید؟")) await post("/api/control", { action: "kill" });
});
$("#themeBtn").addEventListener("click", () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t; localStorage.setItem("rasad_theme", t); route();
});

// ---------- drawer ----------
let drawerTab = "feed";
const logBuf = [];
async function renderDrawer() {
  $$(".drawer-h .tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === drawerTab));
  const b = $("#drawerBody");
  if (drawerTab === "feed") {
    const rows = await api("/api/articles?limit=80&hours=48");
    $("#drawerMeta").textContent = `${fa(rows.length)} خبر اخیر`;
    b.innerHTML = `<div class="list">${rows.map(feedRow).join("")}</div>`;
  } else if (drawerTab === "log") {
    $("#drawerMeta").textContent = "رویدادهای زنده عامل‌ها";
    b.innerHTML = `<div class="log">${logBuf.slice(-300).reverse().map((l) => `<div class="lv-${l.level}"><time>${hhmm(l.ts)}</time><b>${esc(l.agent || "")}</b><span>${esc(l.msg)}</span></div>`).join("") || '<div class="dim">هنوز رویدادی ثبت نشده.</div>'}</div>`;
  } else {
    const s = await api("/api/signals?hours=48");
    $("#drawerMeta").textContent = `${fa(s.length)} سیگنال`;
    b.innerHTML = `<div class="list">${s.map((g) => `<div class="row"><span class="bar" style="background:${g.severity === "critical" ? "var(--c-coral)" : "var(--c-amber)"}"></span><div style="flex:1"><div class="t">${esc(g.label)}</div><div class="m"><span class="tag ${g.severity}">${g.severity === "critical" ? "بحرانی" : "هشدار"}</span><span>وضعیت: ${esc(g.status)}</span><span class="num">score ${g.score} · پایه ${g.baseline}</span><span>${ago(g.created)}</span><span class="tag inference">استنباط</span></div></div></div>`).join("") || '<div class="empty">سیگنالی نیست.</div>'}</div>`;
  }
}
export function feedRow(a, fresh = false) {
  return `<div class="row ${fresh ? "fresh" : ""}" onclick="location.hash='#/stories/${a.story_id}'"><span class="bar" style="background:${a.color || "var(--line2)"}"></span>
  <div style="flex:1;min-width:0"><div class="t">${esc(a.title_fa || a.title)}</div><div class="m"><span>${esc(a.source_name)}</span><span class="tag lang">${LANG[a.lang] || a.lang}</span>${catChip(a.category)}
  ${a.urgency === "flash" ? '<span class="tag critical">فوری</span>' : a.urgency === "high" ? '<span class="tag warning">مهم</span>' : ""}<time>${hhmm(a.published_at)}</time><span class="dim">${ago(a.published_at)}</span></div></div></div>`;
}
$(".drawer-h").addEventListener("click", (e) => { if (e.target.dataset.tab) { drawerTab = e.target.dataset.tab; renderDrawer(); } });

// ---------- command palette (cmdk-style) ----------
const pal = { open: false, items: [], idx: 0 };
const ACTIONS = [
  { g: "فرمان", t: "اجرای فوری خط لوله", run: () => post("/api/control", { action: "start" }) },
  { g: "فرمان", t: "توقف موقت (Pause)", run: () => post("/api/control", { action: "pause" }) },
  { g: "فرمان", t: "ادامه (Resume)", run: () => post("/api/control", { action: "resume" }) },
  { g: "فرمان", t: "پس‌گرفتن کنترل (Take over → دستی)", run: () => post("/api/control", { action: "takeover" }) },
  { g: "فرمان", t: "حالت نمایش روایی", run: () => startPresentation() },
  { g: "فرمان", t: "تغییر تم تیره/روشن", run: () => $("#themeBtn").click() },
  { g: "فرمان", t: "جست‌وجوی پیشرفته در خبرها", run: () => (location.hash = "#/explore/" + encodeURIComponent($("#palIn").value || "")) },
  ...ROUTES.filter((r) => !r.hidden).map((r) => ({ g: "رفتن به", t: r.fa, run: () => (location.hash = "#/" + r.id) })),
];
function openPal() { pal.open = true; $("#palOv").classList.add("open"); $("#palIn").value = ""; $("#palIn").focus(); palSearch(""); }
function closePal() { pal.open = false; $("#palOv").classList.remove("open"); }
let palT;
async function palSearch(q) {
  const acts = ACTIONS.filter((a) => !q || a.t.includes(q));
  pal.items = acts;
  drawPal();
  clearTimeout(palT);
  if (q.trim().length >= 2) palT = setTimeout(async () => {
    const r = await api("/api/search?q=" + encodeURIComponent(q));
    pal.items = [...acts,
      ...r.entities.map((e) => ({ g: "موجودیت", t: `${e.name_fa} · ${e.name_en}`, run: () => (location.hash = "#/graph/" + e.id) })),
      ...r.stories.map((s) => ({ g: "داستان", t: s.title_fa || s.title, run: () => (location.hash = "#/stories/" + s.id) })),
      { g: "کاوش", t: `جست‌وجوی تمام‌متن «${q}» در کاوش پیشرفته`, run: () => (location.hash = "#/explore/" + encodeURIComponent(q)) },
      ...r.articles.slice(0, 10).map((a) => ({ g: "خبر", t: a.title, run: () => (location.hash = "#/stories/" + a.story_id) }))];
    pal.idx = 0; drawPal();
  }, 180);
}
function drawPal() {
  let g = null;
  $("#palItems").innerHTML = pal.items.map((it, i) => { const h = it.g !== g ? `<div class="grp">${esc((g = it.g))}</div>` : ""; return h + `<div class="it ${i === pal.idx ? "on" : ""}" data-i="${i}">${esc(it.t)}</div>`; }).join("") || '<div class="empty">نتیجه‌ای نیست</div>';
}
$("#palIn").addEventListener("input", (e) => { pal.idx = 0; palSearch(e.target.value); });
$("#palItems").addEventListener("click", (e) => { const i = e.target.closest(".it")?.dataset.i; if (i != null) { closePal(); pal.items[i].run(); } });
$("#palOv").addEventListener("click", (e) => { if (e.target.id === "palOv") closePal(); });
$("#openPalette").addEventListener("click", openPal);
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); pal.open ? closePal() : openPal(); }
  if (e.key === "Escape") { closePal(); stopPresentation(); }
  if (pal.open && ["ArrowDown", "ArrowUp"].includes(e.key)) { e.preventDefault(); pal.idx = (pal.idx + (e.key === "ArrowDown" ? 1 : -1) + pal.items.length) % Math.max(1, pal.items.length); drawPal(); }
  if (pal.open && e.key === "Enter" && pal.items[pal.idx]) { closePal(); pal.items[pal.idx].run(); }
});

// ---------- presentation mode (guided camera) ----------
let pres = null;
async function startPresentation() {
  const data = await api("/api/presentation");
  const scenes = data.scenes || [];
  if (!scenes.length) return toast("صحنه‌ای برای نمایش نیست؛ ابتدا یک اجرای کامل لازم است", "warn");
  $("#present").classList.add("open");
  await V.loadGlobeLib();
  const el = $("#presentGlobe"); el.innerHTML = "";
  const g = Globe()(el).globeImageUrl("/static/vendor/earth-night.jpg").backgroundColor("#05070a").atmosphereColor("#5E9FE8").atmosphereAltitude(0.18)
    .arcColor((d) => d.color).arcStroke(0.6).arcDashLength(0.5).arcDashGap(0.25).arcDashAnimateTime(reducedMotion() ? 0 : 1800)
    .pointColor((d) => d.color).pointAltitude(0.02).pointRadius(0.6).width(innerWidth).height(innerHeight);
  pres = { g, scenes, i: 0, timer: null };
  const show = (i) => {
    pres.i = (i + scenes.length) % scenes.length;
    const s = scenes[pres.i], color = store.topics[s.category]?.color || "#5E9FE8";
    g.arcsData((s.arcs || []).map((a) => ({ startLat: a[0], startLng: a[1], endLat: a[2], endLng: a[3], color }))).pointsData([{ lat: s.camera.lat, lng: s.camera.lng, color }]);
    g.pointOfView({ lat: s.camera.lat, lng: s.camera.lng, altitude: s.camera.altitude }, reducedMotion() ? 0 : 2200);
    $("#pCap").innerHTML = `${esc(s.caption_fa)}${s.narration_fa ? `<div class="narr">${esc(s.narration_fa)}</div>` : ""}<small>${esc(store.topics[s.category]?.fa || "")} · صحنه ${fa(pres.i + 1)} از ${fa(scenes.length)}</small>`;
    $("#pSteps").innerHTML = scenes.map((_, k) => `<i class="${k === pres.i ? "on" : ""}"></i>`).join("");
    clearTimeout(pres.timer);
    if (!reducedMotion()) pres.timer = setTimeout(() => show(pres.i + 1), s.duration_ms || 6000);
  };
  show(0);
  $("#pNext").onclick = () => show(pres.i + 1); $("#pPrev").onclick = () => show(pres.i - 1); $("#pClose").onclick = stopPresentation;
}
function stopPresentation() { if (!pres) return; clearTimeout(pres.timer); pres.g._destructor?.(); pres = null; $("#present").classList.remove("open"); }
$("#presentBtn").addEventListener("click", startPresentation);

// ---------- live events ----------
on("state", (ev) => renderState(ev.state));
on("toast", (ev) => toast(ev.msg, ev.level));
on("log", (ev) => { logBuf.push(ev); if (drawerTab === "log") renderDrawer(); });
on("step", (ev) => { if (ev.status === "failed") logBuf.push({ ts: ev.ts, level: "error", agent: ev.agent, msg: ev.error }); if (["succeeded", "skipped"].includes(ev.status) && ev.output_summary) logBuf.push({ ts: ev.ts, level: "info", agent: ev.agent, msg: ev.output_summary }); });
on("data_changed", () => { renderDrawer(); if (current && ["command", "stories", "trends", "sources", "briefs", "qa"].includes(current.id)) route(); });
on("conn", (ev) => { if (!ev.ok) $("#runPill").innerHTML = '<span class="dot failed"></span><span>اتصال قطع است؛ تلاش مجدد…</span>'; });

let authShown = false;
on("auth_required", () => { if (authShown) return; authShown = true; showLogin(false, "نشست شما پایان یافته است؛ لطفاً دوباره وارد شوید."); });

(async function boot() {
  document.documentElement.dataset.theme = localStorage.getItem("rasad_theme") || "dark";
  let st;
  try { st = await authStatus(); } catch (e) { $("#view").innerHTML = `<div class="empty">اتصال به سرور برقرار نشد: ${esc(e.message)}</div>`; return; }
  if (st.setup_required) { authShown = true; return showLogin(true); }
  if (st.auth_enabled && !st.user) { authShown = true; return showLogin(false); }
  applyRole(); renderUserMenu(); buildRail(); initBell();
  const meta = await api("/api/meta");
  store.meta = meta; store.topics = meta.topics; store.state = meta.state;
  renderState(meta.state);
  connectEvents();
  renderDrawer();
  setInterval(() => renderState(store.state), 30000);
  route();
})();
