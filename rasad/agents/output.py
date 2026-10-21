import re
import statistics
import time

from .. import db, llm, ai
from ..taxonomy import ENTITY, TOPICS
from .base import Agent

LBL = {"documented_fact": "واقعیت مستند", "inference": "استنباط", "assumption": "فرض"}
CONF = {"high": "بالا", "medium": "متوسط", "low": "پایین"}


def fmt_t(t):
    return time.strftime("%H:%M", time.localtime(t))


class AnalystWriter(Agent):
    name, title_fa = "writer", "تحلیلگر نویسنده"
    role_fa = "تولید بریف فارسی با برچسب ادعا، سطح اطمینان و ارجاع کامل"
    gate = "writer"

    def run(self, ctx):
        since = time.time() - 24 * 3600
        stories = db.q("""SELECT * FROM stories WHERE last_update>? ORDER BY score DESC LIMIT 10""", (since,))
        refs, lines = [], []
        cat_count = {}

        def ref(article_id):
            a = db.q1("SELECT a.url,a.title,s.name sname,a.published_at FROM articles a JOIN sources s ON s.id=a.source_id WHERE a.id=?", (article_id,))
            if not a:
                return ""
            refs.append(a)
            return f"[{len(refs)}]"

        for s in stories:
            v = db.jload(s["verification"], {}) or {}
            cat_count[s["category"]] = cat_count.get(s["category"], 0) + 1
            arts = db.q("SELECT id FROM articles WHERE story_id=? AND duplicate_of IS NULL ORDER BY published_at LIMIT 3", (s["id"],))
            cites = "".join(ref(a["id"]) for a in arts)
            title = s["title_fa"] or s["title"]
            tag = LBL.get(v.get("label"), "فرض")
            lines.append(f"- **{title}** — {TOPICS.get(s['category'], {}).get('fa', '')} · {s['source_count']} منبع · "
                         f"[{tag}] اطمینان {CONF.get(v.get('confidence'), 'پایین')} {cites}"
                         + (" · ⚠️ تکذیب/تناقض گزارش شده" if v.get("contradictions") else ""))
        sigs = db.q("SELECT * FROM signals WHERE created>? AND status IN ('new','approved') ORDER BY score DESC LIMIT 6", (since,))
        watch = [f"- {g['label']} (z/نسبت = {g['score']}، پایه {g['baseline']}) — [استنباط] اطمینان {'بالا' if g['severity'] == 'critical' else 'متوسط'}" for g in sigs]
        unc = [f"- {s['title_fa'] or s['title']}: فقط {(db.jload(s['verification'], {}) or {}).get('independent_sources', 1)} منبع مستقل"
               for s in stories if (db.jload(s["verification"], {}) or {}).get("independent_sources", 1) < 2][:5]
        n_art = db.q1("SELECT COUNT(*) n FROM articles WHERE fetched_at>? AND duplicate_of IS NULL", (since,))["n"]
        exec_summary, ai_md, dropped, ai_used = "", [], 0, False
        if llm.agent_enabled("writer") and stories:
            try:
                st_txt = "\n".join(f"- {s['title_fa'] or s['title']} | {(db.jload(s['ai'], {}) or {}).get('summary_fa') or (s['summary'] or '')[:200]} | "
                                   f"{s['source_count']} منبع | برچسب راستی‌آزما: {LBL.get((db.jload(s['verification'], {}) or {}).get('label'), 'فرض')}" for s in stories)
                sg_txt = "\n".join(f"- {g['label']} (score {g['score']})" for g in sigs) or "-"
                d, used = ai.write_brief(refs, st_txt, sg_txt)
                ctx.tokens += used
                n = len(refs)
                bluf, d1 = ai.citation_guard(d.get("bluf", ""), n)
                exec_summary, d2 = ai.citation_guard(d.get("executive_summary", ""), n)
                dropped = d1 + d2
                if bluf:
                    ai_md += ["## جمع‌بندی (BLUF)", bluf, ""]
                if exec_summary:
                    ai_md += ["## خلاصه مدیریتی", exec_summary, ""]
                sec = []
                for a in (d.get("analysis") or [])[:6]:
                    t, dd = ai.citation_guard(a.get("text", ""), n); dropped += dd
                    if t:
                        sec += [f"### {a.get('heading', '').strip()}", t, ""]
                if sec:
                    ai_md += ["## تحلیل", *sec]
                kj = []
                for k in (d.get("key_judgments") or [])[:5]:
                    guarded, dd = ai.citation_guard(str(k), n); dropped += dd
                    if guarded:
                        kj.append(guarded)
                if kj:
                    ai_md += ["## داوری‌های کلیدی", *[f"- {k}" for k in kj], ""]
                if d.get("outlook"):
                    outlook, dd = ai.citation_guard(d["outlook"].strip(), n); dropped += dd
                    if outlook:
                        ai_md += ["## چشم‌انداز ۲۴ تا ۷۲ ساعت", outlook, ""]
                ai_used = True
            except Exception as e:
                ctx.log(f"LLM writer: {e}", "warn")
        top_cat = max(cat_count, key=cat_count.get) if cat_count else None
        md = [f"# بریف رصد — {time.strftime('%Y-%m-%d %H:%M')}", "",
              f"**نتیجه‌گیری:** {len(stories)} داستان برتر از {n_art} خبر یکتای ۲۴ ساعت اخیر؛ "
              f"پرتکرارترین حوزه: {TOPICS.get(top_cat, {}).get('fa', '—')}. [واقعیت مستند] اطمینان بالا (شمارش سامانه)", ""]
        if ai_md:
            md += ai_md + [f"> متن تحلیلی بالا را مدل زبانی ({llm.cfg()['model']}) نوشته است؛ {dropped} جمله فاقد ارجاع معتبر حذف شد. بخش‌های بعدی را سامانه به‌صورت قطعی تولید کرده است.", ""]
        md += ["> این بریف قدیمی کنترل شماره ارجاع دارد، نه اعتبارسنجی نقل‌قول و استلزام. برای تحلیل ممیزی‌پذیر از نمای تحلیل مستند استفاده کنید.", ""]
        md += ["## نکات کلیدی", *lines, "", "## چه چیزی را باید پایید", *(watch or ["- سیگنال معناداری ثبت نشد."]), "",
               "## عدم‌قطعیت‌ها", *(unc or ["- همه داستان‌های برتر حداقل دو منبع مستقل دارند."]), "", "## منابع"]
        md += [f"{i+1}. [{r['title']}]({r['url']}) — {r['sname']} · {fmt_t(r['published_at'])}" for i, r in enumerate(refs)]
        markdown = "\n".join(md)
        claim_lines = [l for l in lines]
        cited = sum(1 for l in claim_lines if re.search(r"\[\d+\]", l))
        with db.tx() as c:
            cur = c.execute("INSERT INTO briefs(created,title,markdown,status,run_id,stats) VALUES(?,?,?,?,?,?)",
                            (time.time(), md[0][2:], markdown, "draft", ctx.run_id,
                             db.jdump({"stories": len(stories), "refs": len(refs), "citation_coverage": cited / max(1, len(claim_lines)), "ai": ai_used, "ai_dropped": dropped,
                                       "refs_list": [{"sname": r["sname"], "title": r["title"]} for r in refs]})))
            bid = cur.lastrowid
        ctx.state["brief_id"] = bid
        return {"summary": f"بریف #{bid} با {len(stories)} داستان و {len(refs)} ارجاع تولید شد (پیش‌نویس)",
                "input_summary": f"{len(stories)} داستان، {len(sigs)} سیگنال", "output": {"brief_id": bid, "markdown": markdown},
                "reasoning": "رتبه داستان = تعداد منابع مستقل، تنوع زبان، فوریت و تازگی. مدل زبانی BLUF، خلاصه، تحلیل، داوری‌ها و چشم‌انداز را می‌نویسد؛ هر جمله‌ای که ارجاع معتبر [n] نداشته باشد حذف می‌شود و بخش‌های شمارشی قطعی باقی می‌مانند.",
                "approval": {"kind": "writer", "brief_id": bid, "editable": "markdown"}}


class VizDirector(Agent):
    name, title_fa = "viz", "کارگردان بصری"
    role_fa = "انتخاب بهترین نما برای هر بینش و ساخت صحنه‌های روایی حالت نمایش"

    def run(self, ctx):
        stories = db.q("SELECT * FROM stories WHERE last_update>? ORDER BY score DESC LIMIT 6", (time.time() - 24 * 3600,))
        scenes, specs = [], []
        for s in stories:
            ents = [ENTITY[e] for e in (db.jload(s["entities"], []) or []) if e in ENTITY and ENTITY[e]["lat"] is not None]
            if ents:
                lat = sum(e["lat"] for e in ents[:4]) / len(ents[:4])
                lon = sum(e["lon"] for e in ents[:4]) / len(ents[:4])
                scenes.append({"story_id": s["id"], "camera": {"lat": lat, "lng": lon, "altitude": 1.6 if len(ents) > 2 else 1.1},
                               "caption_fa": s["title_fa"] or s["title"], "category": s["category"], "duration_ms": 6000,
                               "arcs": [[ents[0]["lat"], ents[0]["lon"], e["lat"], e["lon"]] for e in ents[1:4]]})
            view = "globe" if len(ents) >= 2 else ("timeline" if s["article_count"] >= 5 else "table")
            specs.append({"story_id": s["id"], "view": view, "why": "چند بازیگر جغرافیایی" if view == "globe" else ("روایت چندنسخه‌ای" if view == "timeline" else "داده کم")})
        for g in db.q("SELECT * FROM signals WHERE created>? ORDER BY score DESC LIMIT 4", (time.time() - 12 * 3600,)):
            specs.append({"signal_id": g["id"], "view": "theme_river" if g["type"] == "volume_spike" else "graph",
                          "why": "تغییر حجم در زمان" if g["type"] == "volume_spike" else "بازیگر و روابط"})
        narrated = 0
        if scenes and llm.agent_enabled("viz"):
            try:
                items, used = ai.narrate_scenes(scenes)
                ctx.tokens += used
                for it in items:
                    try:
                        scenes[int(it.get("i"))]["narration_fa"] = it.get("narration_fa"); narrated += 1
                    except (TypeError, ValueError, IndexError):
                        pass
            except Exception as e:
                ctx.log(f"LLM viz: {e}", "warn")
        db.kv_set("presentation", {"created": time.time(), "scenes": scenes})
        db.kv_set("viz_specs", specs)
        return {"summary": f"{len(scenes)} صحنه روایی ({narrated} با روایت مدل زبانی) و {len(specs)} پیشنهاد نما", "input_summary": f"{len(stories)} داستان برتر",
                "output": {"scenes": len(scenes), "specs": specs},
                "reasoning": "بیش از ۲ بازیگر مکانی ← کره با کمان؛ ≥۵ نسخه ← خط زمانی؛ جهش حجم ← رودخانه موضوعی؛ بازیگر نوظهور ← گراف؛ داده کم ← جدول."}


class QAEvaluator(Agent):
    name, title_fa = "qa", "ارزیاب کیفیت"
    role_fa = "سنجش تازگی، نرخ تکرار، کیفیت استخراج، پوشش ارجاع و سلامت منابع"

    def run(self, ctx):
        since = time.time() - 24 * 3600
        arts = db.q("SELECT fetched_at,published_at,duplicate_of,extraction_quality,topics,source_id,extraction_method FROM articles WHERE fetched_at>?", (since,))
        first = db.q1("SELECT MIN(fetched_at) t FROM articles")["t"] or 0
        # exclude the cold-start backlog: only items published after observation began reflect pipeline freshness
        fresh = sorted(max(0, (a["fetched_at"] - a["published_at"]) / 60) for a in arts if a["published_at"] and a["published_at"] >= first - 600)
        p = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None
        dup = sum(1 for a in arts if a["duplicate_of"]) / max(1, len(arts))
        cov = sum(1 for a in arts if a["topics"] and a["topics"] != "[]") / max(1, len(arts))
        eq = statistics.mean([a["extraction_quality"] or 0 for a in arts]) if arts else 0
        srcs = db.q("SELECT id,name,status,fail_count FROM sources WHERE status IN ('active','quarantine')")
        healthy = sum(1 for s in srcs if s["status"] == "active" and s["fail_count"] == 0) / max(1, len(srcs))
        b = db.q1("SELECT stats FROM briefs ORDER BY id DESC LIMIT 1")
        cit = (db.jload(b["stats"], {}) or {}).get("citation_coverage") if b else None
        ment = db.q1("SELECT COUNT(DISTINCT article_id) n FROM mentions m JOIN articles a ON a.id=m.article_id WHERE a.fetched_at>?", (since,))["n"]
        issues = []
        for s in srcs:
            if s["fail_count"]:
                issues.append(f"منبع {s['name']}: {s['fail_count']} خطای پیاپی")
        low = db.q("""SELECT s.name, AVG(a.extraction_quality) q, COUNT(*) n FROM articles a JOIN sources s ON s.id=a.source_id
                      WHERE a.fetched_at>? AND a.extraction_method LIKE 'feed_summary(%' GROUP BY s.id HAVING n>=3 ORDER BY q LIMIT 5""", (since,))
        issues += [f"استخراج متن کامل برای {r['name']} ناموفق/محدود (robots یا خطا)" for r in low]
        if cov < 0.7:
            issues.append("پوشش طبقه‌بندی زیر ۷۰٪ — واژه‌نامه را گسترش دهید یا LLM را فعال کنید")
        m = {"freshness_p50_min": round(p(fresh, 0.5), 1) if fresh else None, "freshness_p95_min": round(p(fresh, 0.95), 1) if fresh else None,
             "duplicate_rate": round(dup, 3), "classification_coverage": round(cov, 3), "extraction_quality": round(eq, 3),
             "entity_coverage": round(ment / max(1, len(arts)), 3), "source_health": round(healthy, 3),
             "citation_coverage": cit, "llm_tokens_today": llm.tokens_today(), "articles_24h": len(arts)}
        judge = None
        if llm.agent_enabled("qa"):
            lb = db.q1("SELECT id,markdown,stats FROM briefs ORDER BY id DESC LIMIT 1")
            st_ = (db.jload(lb["stats"], {}) or {}) if lb else {}
            done = db.kv_get("qa_judged_brief")
            if lb and st_.get("ai") and done != lb["id"] and st_.get("refs_list"):
                try:
                    judge, used = ai.judge_brief(lb["markdown"], st_["refs_list"])
                    ctx.tokens += used
                    db.kv_set("qa_judged_brief", lb["id"])
                    db.kv_set("qa_judge", {"brief_id": lb["id"], **judge, "t": time.time()})
                    for f in (judge.get("flagged") or [])[:3]:
                        issues.append(f"بریف #{lb['id']}: {f.get('reason', '')[:120]}")
                except Exception as e:
                    ctx.log(f"LLM judge: {e}", "warn")
        prev_j = db.kv_get("qa_judge") or {}
        m["llm_faithfulness"] = (judge or prev_j).get("faithfulness")
        hist = db.kv_get("qa_history", [])[-200:]
        hist.append({"t": time.time(), **m})
        db.kv_set("qa_history", hist)
        db.kv_set("qa", {"metrics": m, "issues": issues, "t": time.time()})
        return {"summary": f"تازگی p50 = {m['freshness_p50_min']} دقیقه · تکرار {m['duplicate_rate']*100:.1f}٪ · پوشش طبقه‌بندی {m['classification_coverage']*100:.0f}٪",
                "input_summary": f"{len(arts)} خبر ۲۴ ساعت اخیر", "output": {"metrics": m, "issues": issues},
                "reasoning": "تازگی = زمان دریافت منهای زمان انتشار؛ تکرار = سهم خبرهای علامت‌خورده با MinHash؛ پوشش ارجاع از آخرین بریف. وفاداری بریف به منابع را مدل زبانی به‌عنوان داور می‌سنجد (LLM-as-judge). مشکلات به‌عنوان بازخورد برای عامل‌ها ثبت می‌شوند."}
