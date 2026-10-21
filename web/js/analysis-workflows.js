import { api, post, esc, toast, can, store } from './core.js';

const section = (title, body) => `<section class="card" style="padding:18px;margin-block:14px"><h3>${esc(title)}</h3>${body}</section>`;
const split = s => s.split(/[\s,،]+/).map(x=>x.trim()).filter(Boolean);
export function externalForm() {
  return section('منبع بیرونی و منشأ شواهد', `<p class="dim">دریافت URL نیازمند فعال‌سازی مدیر و دامنه مجاز است. «منبع اولیه» ادعای واردکننده است، نه گواه اصالت. منشأ مشترک مانع شمردن بازنشرها به‌عنوان تأیید مستقل است.</p>
  <form id="aexternal" class="form"><label>URL عمومی HTTPS <input type="text" name="url" required maxlength="2000" dir="ltr" /></label><label>عنوان <input type="text" name="title" required maxlength="500" /></label><label>نوع منبع ادعاشده <select name="source_type"><option value="unknown">نامعلوم</option><option value="primary_claimed">اولیه به تشخیص واردکننده</option><option value="secondary">ثانویه</option></select></label><button class="btn">دریافت امن و انتخاب سند</button></form>
  <details style="margin-top:12px"><summary>ثبت رابطه منشأ</summary><form id="aprovenance" class="form"><label>شناسه سند مشتق <input type="text" name="child_id" required /></label><label>شناسه سند مبدأ <input type="text" name="parent_id" required /></label><label>رابطه <select name="relation"><option value="republication">بازنشر</option><option value="translation">ترجمه</option><option value="cites">ارجاع</option><option value="same_origin_claimed">منشأ مشترک ادعاشده</option></select></label><label>توضیح و مبنای تشخیص <textarea name="note" required minlength="3" maxlength="4000"></textarea></label><button class="btn">ثبت رابطه به نام کاربر</button></form><div id="aprovresult"></div></details>`);
}

export function bindExternal(view, capabilities) {
  const q=s=>view.querySelector(s);
  const safe=fn=>async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;try{await fn(e);}catch(err){toast(err.message,'error');}finally{b.disabled=false;}};
  const form=q('#aexternal');if(!form)return;
  form.querySelector('button').disabled=!capabilities.external_intake_enabled;
  form.addEventListener('submit',safe(async e=>{
    const f=e.currentTarget;
    const d=await post('/api/analysis/documents/external',{url:f.elements.url.value,title:f.elements.title.value,source_type:f.elements.source_type.value});
    const input=q('#anew [name=documents]');input.value=[...new Set([...split(input.value),d.id])].join(', ');
    toast('متن منبع بیرونی وارد شد؛ صحت خارجی همچنان بررسی نشده است','success');
  }));
  q('#aprovenance').addEventListener('submit',safe(async e=>{
    const f=e.currentTarget;
    const result=await post('/api/analysis/provenance',{child_id:f.elements.child_id.value.trim(),parent_id:f.elements.parent_id.value.trim(),relation:f.elements.relation.value,note:f.elements.note.value});
    q('#aprovresult').textContent=JSON.stringify(result,null,2);toast('رابطه منشأ به‌صورت تشخیص انسانی ثبت شد','success');
  }));
}

export async function bindReportWorkflow(root, report, job, capabilities, refresh) {
  const q=s=>root.querySelector(s);
  if(!report){root.innerHTML='';return;}
  const own=!store.auth?.auth_enabled || report.owner_id===store.user?.id || can('admin');
  const editable=own&&can('analyst')&&['draft','approved','rejected'].includes(report.status);
  root.innerHTML=section('نسخه‌بندی و بازبینی', `<p>نسخه جاری: ${report.revision} · سیاست بازبین دیگر: ${capabilities.second_reviewer_required?'اجباری':'اختیاری'} · بازبین ارجاع‌شده: ${esc(report.assignment?.reviewer_id??'ندارد')}</p>
    <button class="btn" id="ahistory">نمایش نسخه‌های پیشین</button><div id="ahistoryout"></div>
    ${editable?`<details><summary>اصلاح متن گزارش (ابطال تأیید قبلی)</summary><form id="arevision" class="form">${report.body.statements.map((s,i)=>`<label>جمله ${i+1} (${esc(s.kind)}) <textarea name="s${i}" required maxlength="4000">${esc(s.text)}</textarea></label>`).join('')}<label>محدودیت‌ها، هر مورد در یک خط <textarea name="limitations">${esc(report.body.limitations.join('\n'))}</textarea></label><label>علت ویرایش <textarea name="note" required minlength="3" maxlength="4000"></textarea></label><button class="btn">ثبت نسخه تازه بدون تأیید</button></form></details>`:''}
    ${own&&can('analyst')&&(report.status==='draft'||(report.status==='validating'&&report.validation_lease?.expires<Date.now()/1000))?'<button class="btn" id="arevalidate">اعتبارسنجی مجدد همین نسخه با مدل</button>':''}
    ${own&&can('analyst')?'<form id="aassign" class="form" style="margin-top:12px"><label>ارجاع به بازبین <select name="reviewer_id"><option value="">لغو ارجاع</option></select></label><button class="btn">ثبت ارجاع</button><p class="dim">ارجاع، دسترسی به متن کامل اسناد منتخب این تحلیل را به بازبین می‌دهد. لغو ارجاع دسترسی بعدی را می‌بندد، نه نسخه‌های قبلاً دریافت‌شده را.</p></form>':''}`)+
    section('منشأ شواهد ثبت‌شده', `<p class="dim">روابط زیر تشخیص ثبت‌کننده‌اند، نه اثبات استقلال. تطابق کامل متن نیز فقط نشانه تکرار است.</p>${(report.body.manifest?.provenance?.edges||[]).map(e=>`<p><code>${esc(e.child_id)}</code> ← ${esc(e.relation)} ← <code>${esc(e.parent_id)}</code><br>${esc(e.note)}</p>`).join('')||'<p>رابطه‌ای در زمان ساخت گزارش ثبت نشده بود.</p>'}${(report.body.manifest?.provenance?.exact_text_groups||[]).map(g=>`<p>گروه متن یکسان: ${g.map(esc).join(' · ')}</p>`).join('')}`)+
    section('بررسی تکمیلی با شواهد تازه', '<form id="afollowup" class="form"><label>شناسه اسناد تکمیلی واردشده <input type="text" name="documents" required /></label><label>پرسش تکمیلی (اختیاری) <textarea name="question" maxlength="1200"></textarea></label><button class="btn">ساخت تحلیل مقایسه‌ای تازه</button><p class="dim">گزارش اصلی بازنویسی نمی‌شود. این کار صحت خبر یا استقلال منبع را خودکار اثبات نمی‌کند.</p></form>');
  const safe=fn=>async e=>{e.preventDefault();const b=e.currentTarget.tagName==='FORM'?e.currentTarget.querySelector('button'):e.currentTarget;b.disabled=true;try{await fn(e);}catch(err){toast(err.message,'error');}finally{b.disabled=false;}};
  q('#ahistory').onclick=safe(async()=>{
    const history=await api(`/api/analysis/reports/${report.id}/history`);
    q('#ahistoryout').innerHTML=history.archived.map(r=>`<details><summary>نسخه ${r.revision} · ${esc(r.status)}</summary><p>${esc(r.note)}</p>${r.body.statements.map(s=>`<p>${esc(s.text)}</p>`).join('')}</details>`).join('')||'<p>نسخه پیشین ثبت نشده است.</p>';
  });
  q('#arevision')?.addEventListener('submit',safe(async e=>{
    const f=e.currentTarget;
    const statements=report.body.statements.map((s,i)=>({...s,text:f.elements['s'+i].value}));
    await post(`/api/analysis/reports/${report.id}/revise`,{revision:report.revision,statements,limitations:f.elements.limitations.value.split('\n').filter(Boolean),note:f.elements.note.value});
    toast('نسخه تازه ثبت شد؛ اعتبارسنجی و تأیید قبلی باطل است','success');await refresh(true);
  }));
  q('#arevalidate')?.addEventListener('click',safe(async()=>{await post(`/api/analysis/reports/${report.id}/revalidate`,{revision:report.revision});await refresh(true);}));
  if(q('#aassign')){
    const accounts=await api('/api/analysis/reviewers');
    if(!root.isConnected)return;
    const select=q('#aassign [name=reviewer_id]');
    select.innerHTML+=[...accounts].filter(u=>u.id!==report.owner_id&&u.id!==(report.body.editor_id??report.owner_id)).map(u=>`<option value="${u.id}" ${u.id===report.assignment?.reviewer_id?'selected':''}>${esc(u.name||u.username)} (${u.id})</option>`).join('');
    q('#aassign').addEventListener('submit',safe(async e=>{await post(`/api/analysis/reports/${report.id}/assignment`,{reviewer_id:e.currentTarget.elements.reviewer_id.value?Number(e.currentTarget.elements.reviewer_id.value):null});await refresh(true);}));
  }
  q('#afollowup').addEventListener('submit',safe(async e=>{
    const f=e.currentTarget;const j=await post(`/api/analysis/jobs/${job.id}/followup`,{document_ids:split(f.elements.documents.value),question:f.elements.question.value});
    location.hash='#/analysis/'+encodeURIComponent(j.id);
  }));
}
