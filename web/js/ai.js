// AI features: analyst chat (RAG, streaming), AI settings & usage, story AI panel helpers
import { $, $$, esc, fa, ago, dt, hhmm, api, post, store, toast, makeChart, chartTheme, baseTooltip, LANG, VER } from "./core.js";

const T = () => chartTheme();
const md = (s) => (window.marked ? marked.parse(s || "") : esc(s));
const PHASE = { emerging: ["نوظهور", "#5E9FE8"], escalating: ["در حال تشدید", "#E97366"], plateau: ["تثبیت", "#E0A84E"], "de-escalating": ["کاهش تنش", "#4FB9C9"], resolved: ["پایان‌یافته", "#72BC8F"] };
const SIG = { low: "کم", medium: "متوسط", high: "زیاد" };

// replace [n] with clickable citation chips
function citeify(html, cites) {
  return html.replace(/\[(\d+)\]/g, (m, n) => {
    const c = cites.find((x) => x.n === +n);
    return c ? `<a class="cite" href="${esc(c.url)}" target="_blank" rel="noopener" title="${esc(c.source + " — " + c.title)}">${fa(+n)}</a>` : m;
  });
}
function labelify(html) {
  return html.replace(/\[(واقعیت مستند|استنباط|فرض)\]/g, (m, l) => `<span class="tag ${{ "واقعیت مستند": "documented_fact", "استنباط": "inference", "فرض": "assumption" }[l]}">${l}</span>`);
}
export function richText(text, cites = []) { return labelify(citeify(md(text), cites)); }

// ================= Ask Rasad (analyst chat) =================
export async function renderAsk({ view, side }) {
  const hist = [];
  let busy = false, ctrl = null, lastCites = [];
  const srcHtml = (cites) => cites.map((c) => `<div style="margin-bottom:4px"><b class="num">[${fa(c.n)}]</b> ${esc(c.source)} <span class="tag lang">${LANG[c.lang] || c.lang}</span> — <a href="#/stories/${c.story_id}">${esc(c.title.slice(0, 80))}</a></div>`).join("") || "—";
  const SUG = ["مهم‌ترین تحولات ۲۴ ساعت گذشته درباره تنگه هرمز و امنیت کشتیرانی چیست؟", "پوشش رسانه‌های فارسی، عربی و انگلیسی درباره تحریم‌های جدید چه تفاوتی دارد؟",
    "کدام داستان‌ها سریع‌ترین گسترش را داشته‌اند و چرا؟", "خلاصه وضعیت یمن و دریای سرخ با ذکر منابع", "چه نشانه‌هایی از تغییر در بازار نفت دیده می‌شود؟"];
  view.innerHTML = `<div class="chat-wrap"><div class="chat-log" id="chLog">
      <div class="chat-hello"><div class="ai-badge">✦ دستیار تحلیلی رصد</div><h2>از داده‌های پایش‌شده بپرسید</h2>
      <p class="muted small">پاسخ‌ها فقط بر اساس خبرهای بازیابی‌شده از پایگاه رصد نوشته می‌شوند و هر ادعا ارجاع قابل‌کلیک دارد. مدل: <b>${esc(store.meta?.llm?.model || "—")}</b></p>
      <div class="sugs">${SUG.map((s) => `<button class="chip sug">${esc(s)}</button>`).join("")}</div></div></div>
    <form class="chat-in" id="chForm"><textarea id="chQ" rows="2" placeholder="پرسش خود را بنویسید… (Enter ارسال، Shift+Enter خط جدید)"></textarea>
      <div class="chat-tools"><select id="chH"><option value="24">۲۴ ساعت</option><option value="72">۷۲ ساعت</option><option value="168" selected>۷ روز</option></select>
      <button class="btn primary" id="chSend" type="submit">ارسال ↵</button></div></form></div>`;
  const log = $("#chLog");
  const histSide = async () => {
    const h = await api("/api/ask/history?limit=15").catch(() => []);
    side.innerHTML = `<h2>دستیار تحلیلی</h2><p class="small muted" style="line-height:1.8">بازیابی ترکیبی (جست‌وجوی تمام‌متن + پیوند موجودیت) از خبرهای پایش‌شده؛ پاسخ استریم‌شده با ارجاع و برچسب شواهد. محتوای خبرها به‌عنوان داده نامطمئن به مدل داده می‌شود (محافظت از تزریق دستور).</p>
      <div class="h-sec">منابع بازیابی‌شده برای آخرین پاسخ</div><div id="chSrc" class="small">${srcHtml(lastCites)}</div><div class="h-sec">پرسش‌های اخیر</div>
      <div class="list">${h.map((x) => `<div class="row" data-q="${esc(x.question)}"><div style="min-width:0"><div class="t small">${esc(x.question.slice(0, 90))}</div><div class="m">${ago(x.ts)} · ${fa(x.citations.length)} ارجاع</div></div></div>`).join("") || '<div class="dim small">—</div>'}</div>`;
    $$("[data-q]", side).forEach((r) => (r.onclick = () => { $("#chQ").value = r.dataset.q; $("#chQ").focus(); }));
  };
  await histSide();
  const add = (role, html) => { const d = document.createElement("div"); d.className = "msg " + role; d.innerHTML = html; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; };
  async function ask(q) {
    if (busy || !q.trim()) return;
    busy = true; $("#chSend").disabled = true; $(".chat-hello", log)?.remove();
    add("user", esc(q));
    const box = add("ai", `<div class="ai-badge">✦ رصد</div><div class="mbody"><span class="typing"><i></i><i></i><i></i></span></div><div class="srcs"></div>`);
    let text = "", cites = [];
    ctrl = new AbortController();
    try {
      const headers = { "Content-Type": "application/json" };
      const tok = localStorage.getItem("rasad_admin_token"); if (tok) headers["X-Admin-Token"] = tok;
      const r = await fetch("/api/ask", { method: "POST", headers, body: JSON.stringify({ question: q, history: hist, hours: +$("#chH").value }), signal: ctrl.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await rd.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5));
          if (ev.type === "sources") {
            cites = ev.items; lastCites = cites;
            $("#chSrc").innerHTML = srcHtml(cites);
          } else if (ev.type === "delta") {
            text += ev.text; $(".mbody", box).innerHTML = richText(text, cites); log.scrollTop = log.scrollHeight;
          } else if (ev.type === "error") {
            $(".mbody", box).innerHTML = `<span style="color:var(--c-coral)">${esc(ev.error)}</span>`;
          } else if (ev.type === "done") {
            const used = cites.filter((c) => ev.used.includes(c.n));
            $(".srcs", box).innerHTML = used.length ? `<div class="small muted" style="margin-top:8px">منابع استفاده‌شده:</div>` + used.map((c) => `<a class="src-chip" href="${esc(c.url)}" target="_blank" rel="noopener"><b>${fa(c.n)}</b> ${esc(c.source)} · ${esc(c.title.slice(0, 60))}</a>`).join("") : "";
          }
        }
      }
      hist.push({ role: "user", content: q }, { role: "assistant", content: text });
    } catch (e) { if (e.name !== "AbortError") $(".mbody", box).innerHTML = `<span style="color:var(--c-coral)">خطا: ${esc(e.message)}</span>`; }
    busy = false; $("#chSend").disabled = false; histSide();
  }
  $("#chForm").onsubmit = (e) => { e.preventDefault(); const q = $("#chQ").value; $("#chQ").value = ""; ask(q); };
  $("#chQ").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chForm").requestSubmit(); } };
  log.onclick = (e) => { const b = e.target.closest(".sug"); if (b) ask(b.textContent); };
  return () => ctrl?.abort();
}

// ================= AI settings & usage =================
export async function renderSettings({ view, side }) {
  const [c, u] = await Promise.all([api("/api/ai/config"), api("/api/ai/usage")]);
  const pctUsed = Math.min(100, Math.round((u.tokens_today / Math.max(1, u.budget)) * 100));
  view.innerHTML = `<div class="grid" style="grid-template-columns:1.1fr 1fr">
   <div class="card"><h3>✦ اتصال مدل زبانی <span class="r"><span class="dot ${c.enabled && c.base_url ? "succeeded" : "failed"}"></span> ${c.enabled && c.base_url ? "فعال" : "غیرفعال"}</span></h3>
     <div class="form">
       <label>خروجی ساخت‌یافته سخت‌گیرانه (فقط فراهم‌کننده سازگار) <input type="checkbox" id="aStrict" ${c.structured_outputs ? "checked" : ""}></label>
       <label>فعال‌سازی هوش مصنوعی <input type="checkbox" id="aEn" ${c.enabled ? "checked" : ""}></label>
       <label>آدرس پایه (OpenAI-compatible)<input type="text" id="aUrl" value="${esc(c.base_url)}" dir="ltr"></label>
       <label>مدل اصلی<select id="aModel"><option>${esc(c.model)}</option></select></label>
       <label>مدل جایگزین (fallback)<select id="aFb"><option value="">— بدون جایگزین —</option>${c.fallback_model ? `<option selected>${esc(c.fallback_model)}</option>` : ""}</select></label>
       <label>کلید API <input type="password" id="aKey" placeholder="${c.api_key_set ? "تنظیم شده (" + esc(c.api_key_hint) + ") — برای تغییر مقدار جدید وارد کنید" : "وارد نشده"}" dir="ltr" autocomplete="off"></label>
       <label>سقف توکن روزانه<input type="text" id="aBud" value="${c.daily_budget}" dir="ltr"></label>
       <label>دمای پاسخ (temperature)<input type="text" id="aTemp" value="${c.temperature}" dir="ltr"></label>
       <label>سقف درخواست در دقیقه (RPM)<input type="text" id="aRpm" value="${c.rpm}" dir="ltr"></label>
     </div>
     <div class="toolbar" style="margin-top:12px"><button class="btn primary" id="aSave">ذخیره</button><button class="btn" id="aTest">آزمون اتصال</button><span id="aRes" class="small muted"></span></div></div>
   <div class="card"><h3>مصرف امروز</h3><div class="kpi"><div class="v">${fa(u.tokens_today)}</div><div class="d">از سقف ${fa(u.budget)} توکن · ${fa(pctUsed)}٪</div></div>
     <div class="meter"><i style="width:${pctUsed}%;background:${pctUsed > 85 ? "var(--c-coral)" : "var(--accent)"}"></i></div>
     ${Object.keys(u.cooldown || {}).length ? `<div class="small" style="margin-top:8px;color:var(--c-amber)">محدودیت نرخ فراهم‌کننده: ${Object.entries(u.cooldown).map(([m, t]) => `${esc(m)} (${fa(t)} ثانیه؛ درخواست‌ها به مدل جایگزین می‌روند)`).join("، ")}</div>` : `<div class="small dim" style="margin-top:8px">بدون محدودیت نرخ فعال · سقف ${fa(u.rpm)} درخواست در دقیقه</div>`}
     <div class="chart" id="aDay" style="height:180px;margin-top:10px"></div></div></div>
  <div class="grid" style="grid-template-columns:1fr 1fr;margin-top:var(--gap)">
   <div class="card"><h3>کاربردهای هوش مصنوعی در عامل‌ها</h3><div class="list">${Object.entries(c.agents_fa).map(([k, v]) => `<label class="row" style="cursor:pointer;align-items:center"><input type="checkbox" data-ag="${k}" ${c.agents[k] ? "checked" : ""}><div style="flex:1"><div class="t small">${esc(store.state?.pipeline?.find((p) => p.name === k)?.title_fa || { ask: "دستیار تحلیلی", analyze: "تحلیل عمیق و بریف سفارشی" }[k] || k)}</div><div class="m">${esc(v)}</div></div>
     ${(() => { const a = u.by_agent.find((x) => x.agent === k); return a ? `<span class="num small dim">${fa(a.calls)} فراخوانی · ${fa(a.tokens)} توکن</span>` : ""; })()}</label>`).join("")}</div></div>
   <div class="card" style="padding:0;overflow:auto;max-height:520px"><h3 style="padding:14px 14px 0">آخرین فراخوانی‌های مدل</h3><table class="t"><thead><tr><th>زمان</th><th>عامل</th><th>کاربرد</th><th>توکن</th><th>تأخیر</th><th></th></tr></thead><tbody>
     ${u.recent.map((r) => `<tr title="${esc(r.error || "")}"><td class="num small">${hhmm(r.ts)}</td><td class="small">${esc(r.agent)}</td><td class="small dim">${esc(r.purpose)}</td><td class="num">${fa(r.total_tokens)}</td><td class="num">${fa(Math.round(r.latency_ms / 100) / 10)}ث</td><td>${r.ok ? '<span class="dot succeeded"></span>' : '<span class="dot failed"></span>'}</td></tr>`).join("")}</tbody></table></div></div>`;
  side.innerHTML = `<h2>تنظیمات هوش مصنوعی</h2><p class="small muted" style="line-height:1.8">همه عامل‌های هوشمند از یک دروازه مشترک استفاده می‌کنند: تلاش مجدد با عقب‌نشینی نمایی، مدل جایگزین، سقف توکن روزانه، ثبت هزینه و تأخیر هر فراخوانی، و محافظ تزریق دستور. کلید API فقط نوشتنی است و هرگز به مرورگر برگردانده نمی‌شود.</p>
    <div class="h-sec">مصرف به تفکیک عامل (امروز)</div><div class="chart" id="aAg" style="height:${Math.max(160, 30 * u.by_agent.length + 40)}px"></div>`;
  api("/api/ai/models").then((r) => {
    if (!r.models?.length) return;
    const opts = (cur) => r.models.map((m) => `<option ${m.id === cur ? "selected" : ""}>${esc(m.id)}</option>`).join("");
    $("#aModel").innerHTML = opts(c.model); $("#aFb").innerHTML = `<option value="">— بدون جایگزین —</option>` + opts(c.fallback_model);
  });
  const d = makeChart($("#aDay"));
  d.setOption({ grid: { left: 50, right: 10, top: 10, bottom: 24 }, tooltip: { trigger: "axis", ...baseTooltip() }, xAxis: { type: "category", inverse: true, data: u.by_day.map((x) => x.d.slice(5)), axisLabel: { color: T().muted } },
    yAxis: { type: "value", axisLabel: { color: T().muted }, splitLine: { lineStyle: { color: T().line } } }, series: [{ type: "bar", data: u.by_day.map((x) => x.tokens), itemStyle: { color: "#A98BE0", borderRadius: 4 } }] });
  const ag = makeChart($("#aAg"));
  const ba = u.by_agent.slice().reverse();
  ag.setOption({ grid: { left: 80, right: 10, top: 6, bottom: 20 }, tooltip: { ...baseTooltip() }, xAxis: { type: "value", inverse: true, axisLabel: { color: T().muted }, splitLine: { lineStyle: { color: T().line } } },
    yAxis: { type: "category", data: ba.map((x) => x.agent), axisLabel: { color: T().muted, fontFamily: "Inter" } }, series: [{ type: "bar", data: ba.map((x) => x.tokens), itemStyle: { color: "#5E9FE8", borderRadius: 3 } }] });
  const collect = () => {
    const b = { structured_outputs: $("#aStrict").checked, enabled: $("#aEn").checked, base_url: $("#aUrl").value.trim(), model: $("#aModel").value, fallback_model: $("#aFb").value, daily_budget: parseInt($("#aBud").value) || 0, temperature: parseFloat($("#aTemp").value) || 0.2, rpm: parseInt($("#aRpm").value) || 20,
      agents: Object.fromEntries($$("[data-ag]", view).map((x) => [x.dataset.ag, x.checked])) };
    if ($("#aKey").value.trim()) b.api_key = $("#aKey").value.trim();
    return b;
  };
  $("#aSave").onclick = async () => { try { await post("/api/ai/config", collect()); toast("تنظیمات هوش مصنوعی ذخیره شد", "success"); store.meta = await api("/api/meta"); renderSettings({ view, side }); } catch (e) { toast(e.message, "error"); } };
  $$("[data-ag]", view).forEach((x) => (x.onchange = () => post("/api/ai/config", { agents: { [x.dataset.ag]: x.checked } }).then(() => toast("ذخیره شد", "success"))));
  $("#aTest").onclick = async () => { $("#aRes").textContent = "در حال آزمون…"; const r = await post("/api/ai/test"); $("#aRes").innerHTML = r.ok ? `<span style="color:var(--c-green)">✓ ${esc(r.model)} · ${fa(r.latency_ms)}ms · ${esc(r.reply)}</span>` : `<span style="color:var(--c-coral)">✕ ${esc(r.error)}</span>`; };
}

// ================= Story AI panel =================
export function storyAiCard(s) {
  const a = s.ai || {};
  const has = a.summary_fa || a.arc_fa || (a.framing || []).length || a.consensus_fa;
  const ph = PHASE[a.phase];
  return `<div class="card ai-card" style="margin-top:var(--gap)"><h3><span class="ai-badge">✦ تحلیل هوش مصنوعی</span>${ph ? `<span class="chip"><span class="sw" style="background:${ph[1]}"></span>${ph[0]}</span>` : ""}
     <span class="r"><a class="btn" href="#/analysis/${encodeURIComponent(s.id)}/story">تحلیل مستند و دفتر شواهد</a> <button class="btn" id="deepBtn">${a.deep ? "نمایش تحلیل عمیق" : "تحلیل عمیق ✦"}</button></span></h3>
   ${has ? `${a.headline_fa ? `<div style="font-weight:600;margin-bottom:6px">${esc(a.headline_fa)}</div>` : ""}
     ${a.summary_fa ? `<p style="margin:0 0 8px;line-height:1.9">${esc(a.summary_fa)}</p>` : ""}
     ${a.arc_fa ? `<div class="h-sec" style="margin-top:8px">روایت تحول</div><p class="small" style="line-height:1.9;margin:0">${esc(a.arc_fa)}</p>` : ""}
     ${a.watch_fa ? `<div class="small" style="margin-top:6px"><b>نشانگر پایش:</b> ${esc(a.watch_fa)}</div>` : ""}
     ${(a.framing || []).length ? `<div class="h-sec">مقایسه قاب‌بندی رسانه‌ها</div>${a.framing.map((f) => `<div class="small" style="padding:4px 0;border-bottom:1px solid var(--line)"><b>${esc(f.who)}:</b> ${esc(f.frame_fa)}</div>`).join("")}` : ""}
     ${a.consensus_fa ? `<div class="small" style="margin-top:8px"><b>اجماع منابع:</b> ${esc(a.consensus_fa)}</div>` : ""}${a.gaps_fa ? `<div class="small"><b>شکاف‌ها:</b> ${esc(a.gaps_fa)}</div>` : ""}
     ${(s.llm_entities || []).length ? `<div class="h-sec">موجودیت‌های کشف‌شده با مدل</div><div style="display:flex;gap:5px;flex-wrap:wrap">${s.llm_entities.map((e) => `<a class="chip" href="#/graph/${encodeURIComponent(e.id)}">${esc(e.name_fa)} <span class="dim">${esc(e.type)}</span></a>`).join("")}</div>` : ""}`
    : '<p class="small muted">این داستان هنوز توسط عامل‌های هوشمند پردازش نشده؛ می‌توانید تحلیل عمیق را همین حالا اجرا کنید.</p>'}
   <p class="small dim">تحلیل سریع قدیمی، صحت خارجی را تأیید نمی‌کند و کنترل محل دقیق نقل‌قول ندارد. مسیر «تحلیل مستند» برای بازبینی و انتشار کنترل‌شده است.</p><div id="deepOut"></div></div>`;
}
export function bindStoryAi(s, root) {
  const btn = $("#deepBtn", root); if (!btn) return;
  const show = (d) => {
    const cites = (d.refs || []).map((r) => ({ n: r.n, url: r.url, source: r.source, title: r.title }));
    $("#deepOut", root).innerHTML = `<div class="deep">
      ${d.summary_fa ? `<div class="h-sec">خلاصه</div><div>${richText(d.summary_fa, cites)}</div>` : ""}
      ${d.background_fa ? `<div class="h-sec">پیش‌زمینه</div><div class="small">${richText(d.background_fa, cites)}</div>` : ""}
      ${(d.actors || []).length ? `<div class="h-sec">بازیگران</div><table class="t"><tbody>${d.actors.map((x) => `<tr><td><b>${esc(x.name)}</b></td><td class="small">${esc(x.role_fa)}</td><td class="small muted">${esc(x.stance_fa)}</td></tr>`).join("")}</tbody></table>` : ""}
      ${(d.claims || []).length ? `<div class="h-sec">ادعاها</div>${d.claims.map((c) => `<div class="small" style="padding:4px 0"><span class="tag ${c.kind}">${VER[c.kind] || c.kind}</span> ${esc(c.text)} ${(c.refs || []).map((n) => citeify(`[${n}]`, cites)).join("")}</div>`).join("")}` : ""}
      ${(d.framing || []).length ? `<div class="h-sec">قاب‌بندی</div>${d.framing.map((f) => `<div class="small"><b>${esc(f.who)}:</b> ${esc(f.frame_fa)}</div>`).join("")}` : ""}
      ${d.implications_fa ? `<div class="h-sec">پیامدها</div><div class="small">${richText(d.implications_fa, cites)}</div>` : ""}
      ${(d.scenarios || []).length ? `<div class="h-sec">سناریوها</div><div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">${d.scenarios.map((x) => `<div class="card" style="background:var(--panel2)"><b>${esc(x.name_fa)}</b> <span class="tag ${x.likelihood === "high" ? "critical" : x.likelihood === "medium" ? "warning" : "lang"}">احتمال ${SIG[x.likelihood] || esc(x.likelihood)}</span><div class="small muted" style="margin-top:4px">نشانگرها: ${esc(x.indicators_fa)}</div></div>`).join("")}</div>` : ""}
      ${(d.open_questions || []).filter(Boolean).length ? `<div class="h-sec">پرسش‌های باز</div>${d.open_questions.map((q) => `<div class="small">• ${esc(q)}</div>`).join("")}` : ""}
      <div class="small dim" style="margin-top:8px">${dt(d.created)} · ${fa(d.tokens || 0)} توکن · <a href="#" id="deepRe">تولید دوباره</a></div></div>`;
    $("#deepRe", root).onclick = (e) => { e.preventDefault(); run(true); };
  };
  const run = async (force = false) => {
    btn.disabled = true; $("#deepOut", root).innerHTML = '<div class="small muted" style="padding:10px 0"><span class="typing"><i></i><i></i><i></i></span> عامل تحلیلگر در حال بررسی منابع…</div>';
    try { const d = await post(`/api/stories/${s.id}/analyze`, { force }); s.ai = { ...(s.ai || {}), deep: d }; show(d); btn.textContent = "نمایش تحلیل عمیق"; }
    catch (e) { $("#deepOut", root).innerHTML = `<div class="small" style="color:var(--c-coral)">${esc(e.message)}</div>`; }
    btn.disabled = false;
  };
  btn.onclick = () => (s.ai?.deep ? show(s.ai.deep) : run());
}
