import { api, post, esc, toast, can, fa, store } from './core.js';
import { externalForm, bindExternal, bindReportWorkflow } from './analysis-workflows.js';

const modes = { article:'تحلیل خبر', compare:'مقایسه منابع', timeline:'خط زمانی', framing:'قاب‌بندی', impact:'پیامد مشروط', scenario:'سناریو', brief:'بریف مستند', question:'پرسش از اسناد' };
const states = { queued:'در صف', extracting:'استخراج ادعا', assessing:'تطبیق شواهد', synthesizing:'نگارش', validating:'کنترل کیفیت', needs_review:'نیازمند بازبینی', failed:'ناموفق', cancelled:'لغوشده' };
const kinds = { reported:'گزارش‌شده، نه تأیید خارجی', inference:'استنباط مشروط', scenario:'سناریو', gap:'شکاف داده' };
const relations = { reported:'نقل از سند', supports:'پشتیبانی محتوایی', contradicts:'تعارض احتمالی', related:'مرتبط', different_time:'زمان متفاوت', shared_origin:'منشأ مشترک احتمالی', insufficient:'ناکافی' };
const ids = text => text.split(/[\s,،]+/).map(s=>s.trim()).filter(Boolean);
const block = (title, body) => `<section class="card" style="padding:18px;margin-block:14px"><h3>${esc(title)}</h3>${body}</section>`;

export async function renderAnalysis({view, body, params=[]}) {
  body.classList.add("no-panel");
  view.classList.add("evidence-workspace");
  let disposed=false, busy=false, selected=params[0] || null, timer;
  view.innerHTML = `<h2>تحلیل مستند محتوا</h2><p class="dim">نقل‌قول‌ها با نسخه ثابت سند تطبیق داده می‌شوند. وجود شاهد و داوری مدل، اثبات صحت خبر در جهان واقعی نیست. انتشار فقط پس از کنترل کیفیت و تصمیم انسانی است؛ حالت خودکار این دروازه را دور نمی‌زند.</p><div id="acap"></div>
  ${can('analyst') ? block('تحلیل جدید', `<form id="anew" class="form" style="display:grid;gap:12px">
    <label>نوع تحلیل <select name="mode">${Object.entries(modes).map(([v,t])=>`<option value="${v}">${t}</option>`).join('')}</select></label>
    <label>شناسه خبرها (با فاصله یا ویرگول) <input type="text" name="articles" placeholder="از فهرست خبرها" /></label>
    <label>شناسه داستان <input type="text" name="story" /></label>
    <label>شناسه اسناد واردشده <input type="text" name="documents" /></label>
    <label>پرسش یا تمرکز تحلیل <textarea name="question" rows="3" maxlength="1200"></textarea></label>
    <label>سقف قطعه‌ها <input type="number" name="max_chunks" min="1" max="60" value="24" /></label>
    <button type="submit" class="btn solid">افزودن به صف تحلیل</button><span class="dim">حداکثر ۱۲ سند؛ نبود منبع مرتبط باعث امتناع از پاسخ می‌شود. ارسال به API خارجی تابع تنظیمات مدیر است.</span>
  </form>`) + block('ورود سند خصوصی', `<form id="adoc" class="form" style="display:grid;gap:10px"><label>عنوان <input type="text" name="title" required maxlength="500" /></label><label>متن <textarea name="text" rows="5" maxlength="200000"></textarea></label><label>یا فایل (حداکثر ۸ مگابایت) <input type="file" name="file" accept=".txt,.pdf,.png,.jpg,.jpeg,.webp,.mp3,.wav,.mp4,.webm" /></label><p class="dim">PDF متنی با افزونه محلی؛ تصویر و صوت فقط با فعال‌سازی صریح API رسانه. برای PDF اسکن‌شده OCR خودکار نداریم. ویدئو فقط رونویسی صوت است.</p><button class="btn" type="submit">ورود و انتخاب سند</button></form>`) : '<p>ایجاد و تأیید تحلیل به نقش تحلیلگر نیاز دارد.</p>'}
  ${can("analyst")?externalForm():""}<div id="ainbox"></div><div id="ajobs"></div><div id="adetail" aria-live="polite"></div>`;
  const q = s => view.querySelector(s);
  if(params[1]==='story' && q('[name=story]')) { q('[name=story]').value=params[0]; selected=null; }
  if(params[1]==='article' && q('[name=articles]')) { q('[name=articles]').value=params[0]; selected=null; }
  const cap=await api('/api/analysis/capabilities');
  if(disposed)return;
  q('#acap').textContent=`مدل ${cap.chat_configured?'پیکربندی شده (اتصال آزموده نشده)':'پیکربندی نشده'} · جست‌وجو ${cap.embeddings?'ترکیبی':'واژگانی'} · PDF ${cap.pdf_text?'فعال':'نیازمند افزونه'} · API رسانه ${cap.media_api_enabled?'فعال':'خاموش'}`;
  bindExternal(view,cap);
  const safely = fn => async e => { e?.preventDefault(); try { await fn(e); } catch(err){toast(err.message,'error');} };
  q('#anew')?.addEventListener('submit',safely(async e=>{
    const f=e.currentTarget, button=f.querySelector('button'); button.disabled=true;
    try { const j=await post('/api/analysis/jobs',{article_ids:ids(f.elements.articles.value),story_id:f.elements.story.value.trim()||null,document_ids:ids(f.elements.documents.value),question:f.elements.question.value,mode:f.elements.mode.value,max_chunks:Number(f.elements.max_chunks.value)});selected=j.id; await refresh(); } finally {button.disabled=false;}
  }));
  q('#adoc')?.addEventListener('submit',safely(async e=>{
    const f=e.currentTarget,button=f.querySelector('button');button.disabled=true;
    try {
      let d; const file=f.elements.file.files[0];
      if(file){
        if(file.size>8*1024*1024)throw new Error('فایل بیش از ۸ مگابایت است');
        const headers={'Content-Type':file.type||'application/octet-stream'};
        const token=localStorage.getItem('rasad_admin_token');if(token)headers['X-Admin-Token']=token;
        const response=await fetch('/api/analysis/documents/upload?title='+encodeURIComponent(f.elements.title.value),{method:'POST',credentials:'same-origin',headers,body:file});
        d=await response.json();if(!response.ok)throw new Error(d.detail||'ورود فایل ناموفق');
      } else d=await post('/api/analysis/documents',{title:f.elements.title.value,text:f.elements.text.value});
      q('[name=documents]').value=[...ids(q('[name=documents]').value),d.id].join(', ');toast('سند خصوصی وارد و انتخاب شد','success');
    } finally {button.disabled=false;}
  }));
  async function openEvidence(button){
    const chunk=await api('/api/analysis/chunks/'+encodeURIComponent(button.dataset.chunk));
    const doc=await api('/api/analysis/documents/'+encodeURIComponent(chunk.document_id));
    if(disposed)return;
    const start=Number(button.dataset.start),end=Number(button.dataset.end);
    q('#asource').innerHTML=block(doc.title, `<p class="dim">${esc(doc.id)} · بازه نویسه ${start}–${end} · ${esc(doc.metadata.coverage)} · ${esc(JSON.stringify(chunk.locator))}</p><pre style="white-space:pre-wrap;max-height:400px;overflow:auto">${esc(doc.text.slice(Math.max(0,start-250),start))}<mark>${esc(doc.text.slice(start,end))}</mark>${esc(doc.text.slice(end,end+250))}</pre><p class="dim">${esc(doc.metadata.url||'سند واردشده خصوصی')} · منشأ مستقلاً تأیید نشده</p>`);
    q('#asource').scrollIntoView({block:'nearest'});
  }
  async function detail(j){
    const claims=await api(`/api/analysis/jobs/${encodeURIComponent(j.id)}/claims`);
    const report=j.report_id?await api('/api/analysis/reports/'+encodeURIComponent(j.report_id)):null;
    if(disposed || j.id!==selected)return;
    const own=!store.auth?.auth_enabled || j.owner_id===store.user?.id || can("admin");
    const live=['queued','extracting','assessing','synthesizing','validating'].includes(j.status);
    const manifest=report?.body.manifest||j.manifest||{};
    q('#adetail').innerHTML=block('وضعیت تحلیل',`<p><code>${esc(j.id)}</code> · ${esc(states[j.status]||j.status)} · ${fa(Math.round(j.progress*100))}٪ · توکن ثبت‌شده: ${fa(j.tokens)}</p><p>${esc(j.error||'')}</p><p class="dim">قطعات منتخب ${fa(manifest.selected_chunks||0)} از ${fa(manifest.available_chunks||0)} · پوشش کامل ${manifest.complete_coverage?'بله':'خیر'} · استقلال منابع اثبات نشده</p>${own&&can('analyst')?`<button class="btn" id="ajaction" data-action="${live?'cancel':'retry'}" ${!live&&!['failed','cancelled'].includes(j.status)?'disabled':''}>${live?'لغو در نقطه امن بعدی':'تلاش مجدد'}</button>`:''}`)+
      (report?block('گزارش و بازبینی', `<p>وضعیت: <b>${esc(report.status)}</b> · کنترل ماشینی: ${report.quality.passed?'قبول؛ تأیید انسانی هنوز لازم است':'رد؛ انتشار مسدود است'}</p><p>${esc(report.body.notice)}</p>${report.body.statements.map(s=>`<article style="padding:12px;border-bottom:1px solid var(--line)"><b>${esc(kinds[s.kind])}</b><p>${esc(s.text)}</p><small>ادعاها: ${s.claim_ids.map(esc).join(' · ')}</small>${s.assumptions.length?`<p>فرض‌ها: ${s.assumptions.map(esc).join('؛ ')}</p>`:''}${s.watch_indicators.length?`<p>نشانگرها: ${s.watch_indicators.map(esc).join('؛ ')}</p>`:''}</article>`).join('')}<h4>محدودیت‌ها</h4><ul>${report.body.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><details><summary>ممیزی و اجرای مدل</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify({quality:report.quality,manifest,calls:report.calls,reviews:report.reviews},null,2))}</pre></details>${can('analyst')&&report.status==='draft'&&(!cap.second_reviewer_required||(store.user?.id!==report.owner_id&&store.user?.id!==(report.body.editor_id??report.owner_id)))?`<form id="areview" class="form"><label>یادداشت بازبین <textarea name="note" required minlength="3" maxlength="4000"></textarea></label><button class="btn" name="decision" value="approve" ${!report.quality.passed?'disabled':''}>تأیید انسانی</button><button class="btn" name="decision" value="reject">رد گزارش</button></form>`:''}${own&&can('analyst')&&report.status==='approved'?'<button id="apublish" class="btn solid">انتشار در سامانه (بدون ارسال خارجی)</button>':''}`):'')+
      (report?.body.observations?.length ? block('مشاهدات مستند', report.body.observations.map(o=>`<article><b>${esc(o.kind)}</b><p>${esc(o.text)}</p>${o.citations.map(e=>`<blockquote><p>${esc(e.quote)}</p>${Number.isInteger(e.start_char)?`<button class="btn aevidence" data-chunk="${esc(e.chunk_id)}" data-start="${e.start_char}" data-end="${e.end_char}">مشاهده محل دقیق در سند</button>`:''}</blockquote>`).join('')}</article>`).join('')) : '')+
      block('دفتر ادعاها و شواهد',claims.map(c=>`<article style="padding:12px"><p><b>${esc(c.text)}</b></p><small>${esc(c.id)} · ${c.status==='disputed'?'تعارض محتمل':'گزارش‌شده، صحت بررسی نشده'}</small>${c.evidence.map(e=>`<blockquote><p>${esc(e.quote)}</p><p>${esc(relations[e.relation]||e.relation)}: ${esc(e.explanation)}</p><button class="btn aevidence" data-chunk="${esc(e.chunk_id)}" data-start="${e.start_char}" data-end="${e.end_char}">مشاهده محل دقیق در سند</button></blockquote>`).join('')}</article>`).join('')||'<p>هنوز ادعای مستند ثبت نشده است.</p>')+'<div id="asource"></div><div id="aworkflow"></div>';
    q('#ajaction')?.addEventListener('click',safely(async e=>{await post(`/api/analysis/jobs/${j.id}/${e.currentTarget.dataset.action}`);await refresh();}));
    view.querySelectorAll('.aevidence').forEach(b=>b.addEventListener('click',safely(()=>openEvidence(b))));
    q('#areview')?.addEventListener('submit',safely(async e=>{await post(`/api/analysis/reports/${report.id}/review`,{decision:e.submitter.value,note:e.currentTarget.elements.note.value,revision:report.revision});await refresh(true);}));
    await bindReportWorkflow(q('#aworkflow'),report,j,cap,refresh);
    q('#apublish')?.addEventListener('click',safely(async()=>{if(confirm('گزارش را پس از بررسی شواهد منتشر می‌کنید؟')){await post(`/api/analysis/reports/${report.id}/publish`);await refresh(true);}}));
  }
  let lastVersion='';
  async function refresh(force=false){
    if(disposed||busy)return;busy=true;
    try{
      const [rows,inbox]=await Promise.all([api('/api/analysis/jobs'),can('analyst')?api('/api/analysis/review-inbox'):Promise.resolve([])]);if(disposed)return;
      q('#ainbox').innerHTML=inbox.length?block('ارجاع‌های بازبینی من',inbox.map(r=>`<button class="btn ajob" data-id="${esc(r.job_id)}">نسخه ${r.revision} · ${esc(r.status)} · ${esc(r.id.slice(0,12))}</button>`).join('')):'';
      q('#ajobs').innerHTML=block('تحلیل‌های من',rows.map(j=>`<button class="btn ajob" data-id="${esc(j.id)}" style="margin:5px">${esc(j.id.slice(0,12))} · ${esc(states[j.status]||j.status)}</button>`).join('')||'<p>تحلیلی ثبت نشده است.</p>');
      view.querySelectorAll('.ajob').forEach(b=>b.onclick=safely(async()=>{selected=b.dataset.id;lastVersion='';await loadDetail();}));
      await loadDetail(force);
    } finally {busy=false;}
  }
  async function loadDetail(force=false){
    if(!selected)return;const j=await api('/api/analysis/jobs/'+encodeURIComponent(selected));
    const version=JSON.stringify([j.id,j.status,j.progress,j.updated,j.report_revision,j.report_status,j.review_assignment]);
    if(force||version!==lastVersion){await detail(j);lastVersion=version;}
  }
  await refresh();timer=setInterval(()=>refresh().catch(e=>{if(!disposed)console.warn(e.message);}),4000);
  return ()=>{disposed=true;clearInterval(timer);view.classList.remove("evidence-workspace");};
}
