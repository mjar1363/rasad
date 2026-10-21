import math
import time
from collections import Counter, defaultdict

from .. import db, llm, ai
from ..taxonomy import ENTITY, TOPICS, has_denial
from ..text import sentences, tokens
from .base import Agent


class StorylineBuilder(Agent):
    name, title_fa = "storyline", "سازنده خط داستان"
    role_fa = "پیوند رویدادها در زمان: اولین گزارش، ادامه، تشدید، تکذیب"

    def run(self, ctx):
        touched = ctx.state.get("touched_stories") or []
        added, kinds = 0, Counter()
        with db.tx() as c:
            for i, sid in enumerate(touched):
                known = {r["article_id"] for r in c.execute("SELECT article_id FROM storyline_events WHERE story_id=?", (sid,))}
                arts = c.execute("""SELECT id,source_id,title,lead,published_at,urgency,duplicate_of FROM articles
                                 WHERE story_id=? ORDER BY published_at""", (sid,)).fetchall()
                prev_urg = None
                for j, a in enumerate(arts):
                    if a["duplicate_of"]:
                        continue
                    if a["id"] in known:
                        prev_urg = a["urgency"]
                        continue
                    if j == 0 and not known:
                        typ = "first_report"
                    elif has_denial(a["title"] + " " + (a["lead"] or "")):
                        typ = "denial"
                    elif a["urgency"] in ("flash", "high") and prev_urg == "normal":
                        typ = "escalation"
                    else:
                        typ = "continuation"
                    prev_urg = a["urgency"]
                    c.execute("INSERT INTO storyline_events(story_id,t,type,summary,article_id,source_id) VALUES(?,?,?,?,?,?)",
                              (sid, a["published_at"], typ, a["title"], a["id"], a["source_id"]))
                    added += 1
                    kinds[typ] += 1
                if i % 30 == 0:
                    ctx.checkpoint()
                    ctx.progress((i + 1) / max(1, len(touched)))
        arcs = 0
        if llm.agent_enabled("storyline") and touched:
            cand = db.q("SELECT id,title,COALESCE(title_fa,title) t,article_count,ai FROM stories WHERE id IN (%s) AND article_count>=3 ORDER BY score DESC LIMIT 20"
                        % ",".join("?" * len(touched)), touched)
            cand = [x for x in cand if (db.jload(x["ai"], {}) or {}).get("arc_n") != x["article_count"]][:6]
            if cand:
                try:
                    items, used = ai.story_arcs(cand)
                    ctx.tokens += used
                    for it in items:
                        try:
                            st = cand[int(it.get("i"))]
                        except (TypeError, ValueError, IndexError):
                            continue
                        db.merge_json("stories", "id", st["id"], "ai", {"arc_fa": it.get("arc_fa"), "phase": it.get("phase"),
                                                                      "watch_fa": it.get("watch_fa"), "arc_n": st["article_count"]})
                        arcs += 1
                except Exception as e:
                    ctx.log(f"LLM storyline: {e}", "warn")
        return {"summary": f"{added} رویداد به {len(touched)} خط داستان افزوده شد" + (f"؛ {arcs} روایت تحول داستان" if arcs else ""),
                "input_summary": f"{len(touched)} داستان به‌روزشده", "output": {**dict(kinds), "arcs": arcs},
                "reasoning": "نوع رویداد با قاعده تعیین می‌شود: اولین گزارش، تکذیب (واژگان نفی در سه زبان)، تشدید (افزایش فوریت) یا ادامه. مدل زبانی برای داستان‌های پرخبر روایت تحول، فاز (نوظهور/تشدید/ثبات/کاهش/پایان) و نشانگر پایش بعدی را می‌نویسد."}


def _z(obs, series):
    if len(series) < 6:
        return 0.0, 0.0
    mu = sum(series) / len(series)
    sd = math.sqrt(sum((x - mu) ** 2 for x in series) / len(series)) or 0.5
    return (obs - mu) / sd, mu


class PatternAnomaly(Agent):
    name, title_fa = "anomaly", "تحلیلگر الگو و ناهنجاری"
    role_fa = "کشف جهش حجم موضوعی، بازیگر نوظهور و داستان‌های با گسترش سریع"
    gate = "alerts"

    def run(self, ctx):
        now = time.time()
        hour = lambda t: int(t // 3600)
        H = hour(now)
        sigs = []
        first = db.q1("SELECT MIN(fetched_at) t FROM articles")["t"] or now
        hist_h = (now - first) / 3600
        warm = hist_h >= 12  # spikes need >=12h of continuously observed history (feeds are recency-biased on cold start)
        base_start = max(H - 47, int(first // 3600) + 1)
        # 1) topic volume spikes (last 3h vs prior 45h, per-hour)
        rows = db.q("SELECT category,published_at FROM articles WHERE duplicate_of IS NULL AND published_at>?", (now - 48 * 3600,))
        per = defaultdict(Counter)
        for r in rows:
            per[r["category"]][hour(r["published_at"])] += 1
        for cat, cnt in (per.items() if warm else []):
            base = [cnt.get(h, 0) for h in range(base_start, H - 2)]
            obs = sum(cnt.get(h, 0) for h in range(H - 2, H + 1)) / 3
            z, mu = _z(obs, base)
            if z >= 2.5 and obs * 3 >= 5:
                sigs.append({"type": "volume_spike", "key": cat, "label": f"جهش حجم «{TOPICS.get(cat, {}).get('fa', cat)}»",
                             "score": round(z, 2), "baseline": round(mu, 2), "observed": round(obs, 2), "window": f"3h@{H}",
                             "severity": "critical" if z >= 4 else "warning"})
        ctx.progress(0.35)
        # 2) entity spikes / new actors (last 6h vs prior 42h)
        m = db.q("""SELECT m.entity_id e, a.published_at t FROM mentions m JOIN articles a ON a.id=m.article_id
                    WHERE a.duplicate_of IS NULL AND a.published_at>?""", (now - 48 * 3600,))
        recent, prior = Counter(), Counter()
        for r in m:
            if r["t"] > now - 6 * 3600:
                recent[r["e"]] += 1
            elif r["t"] >= base_start * 3600:
                prior[r["e"]] += 1
        prior_windows = max(1.0, (now - 6 * 3600 - base_start * 3600) / (6 * 3600))
        for e, n in (recent.items() if warm else []):
            rate_prior = prior[e] / prior_windows
            name = ENTITY.get(e, {}).get("name_fa") or (db.q1("SELECT name_fa FROM entities WHERE id=?", (e,)) or {}).get("name_fa") or e
            ent_stories = [r["story_id"] for r in db.q("""SELECT a.story_id, COUNT(*) n FROM mentions mm JOIN articles a ON a.id=mm.article_id
                           WHERE mm.entity_id=? AND a.published_at>? AND a.story_id IS NOT NULL GROUP BY a.story_id ORDER BY n DESC LIMIT 8""", (e, now - 6 * 3600))]
            if prior[e] == 0 and n >= 3:
                sigs.append({"type": "new_actor", "key": e, "label": f"ظهور ناگهانی «{name}»", "score": float(n), "baseline": 0.0,
                             "observed": float(n), "window": f"6h@{H // 6}", "severity": "warning", "story_ids": ent_stories})
            elif n >= 6 and n >= 3 * max(1.0, rate_prior):
                sigs.append({"type": "entity_spike", "key": e, "label": f"افزایش شدید اشاره به «{name}»", "score": round(n / max(1.0, rate_prior), 2),
                             "baseline": round(rate_prior, 2), "observed": float(n), "window": f"6h@{H // 6}",
                             "severity": "critical" if n >= 6 * max(1.0, rate_prior) else "warning", "story_ids": ent_stories})
        ctx.progress(0.7)
        # 3) fast-spreading stories: >=4 independent sources within 3h of first report
        for s in db.q("SELECT id,title,title_fa,first_seen,source_count,urgency,category FROM stories WHERE last_update>? AND source_count>=4", (now - 12 * 3600,)):
            arts = db.q("""SELECT DISTINCT s.ownership_group g FROM articles a JOIN sources s ON s.id=a.source_id
                           WHERE a.story_id=? AND a.published_at<=?""", (s["id"], s["first_seen"] + 3 * 3600))
            if len(arts) >= 4:
                sigs.append({"type": "fast_spread", "key": s["id"], "label": f"گسترش سریع: {(s['title_fa'] or s['title'])[:70]}",
                             "score": float(len(arts)), "baseline": 1.0, "observed": float(len(arts)), "window": "story",
                             "severity": "critical" if s["urgency"] == "flash" or (s["category"] == "conflict" and len(arts) >= 6) else "warning",
                             "story_ids": [s["id"]]})
        try:
            from .. import watch
            sigs += watch.match(now)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"watchlists: {e}", "warn")
        new = 0
        critical_ids = []
        with db.tx() as c:
            for g in sigs:
                expl = {"text": g.get("explain") or g["label"], "kind": "inference", "confidence": "medium" if g["severity"] == "warning" else "high",
                        "alternatives": ["افزایش تعداد منابع یا تغییر زمان‌بندی فیدها", "پوشش رسانه‌ای یک رویداد واحد در چند بخش خبری"]}
                cur = c.execute("""INSERT OR IGNORE INTO signals(type,key,label,score,baseline,observed,window,severity,story_ids,explanation,status,created)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (g["type"], g["key"], g["label"], g["score"], g["baseline"], g["observed"], g["window"], g["severity"],
                                 db.jdump(g.get("story_ids", [])), db.jdump(expl), "pending" if g["severity"] == "critical" else "new", now))
                new += cur.rowcount
                if g["severity"] == "critical":
                    row = c.execute("SELECT id FROM signals WHERE type=? AND key=? AND status='pending'", (g["type"], g["key"])).fetchone()
                    if row:
                        critical_ids.append(row[0])
        ctx.state["critical_signals"] = [g for g in sigs if g["severity"] == "critical"]
        explained = 0
        if llm.agent_enabled("anomaly"):
            todo = db.q("SELECT * FROM signals WHERE ai IS NULL AND created>? ORDER BY score DESC LIMIT 8", (now - 12 * 3600,))
            if todo:
                try:
                    items, used = ai.explain_signals(todo)
                    ctx.tokens += used
                    with db.tx() as c:
                        for it in items:
                            try:
                                g = todo[int(it.get("i"))]
                            except (TypeError, ValueError, IndexError):
                                continue
                            expl = db.jload(g["explanation"], {}) or {}
                            if it.get("explanation_fa"):
                                expl.update({"text": it["explanation_fa"], "alternatives": it.get("alternatives_fa") or expl.get("alternatives"), "by": "llm"})
                            c.execute("UPDATE signals SET ai=?,explanation=? WHERE id=?", (db.jdump(it), db.jdump(expl), g["id"]))
                            explained += 1
                except Exception as e:
                    ctx.log(f"LLM anomaly: {e}", "warn")
        ctx.progress(1)
        crit = len(ctx.state["critical_signals"])
        cold = "" if warm else f" — شروع سرد: {hist_h:.1f} ساعت تاریخچه (برای جهش حجم ≥۱۲ ساعت لازم است)"
        return {"summary": f"{len(sigs)} سیگنال ({new} جدید، {crit} بحرانی){cold}" + (f"؛ {explained} سیگنال با توضیح مدل زبانی" if explained else ""), "input_summary": f"{len(rows)} خبر ۴۸ ساعت اخیر",
                "output": {"signals": sigs[:30]},
                "reasoning": "z-score حجم ساعتی (۳ ساعت اخیر در برابر ۴۵ ساعت قبل)، نسبت نرخ اشاره به موجودیت (۶ در برابر ۴۲ ساعت)، و گسترش داستان در ≥۴ گروه مالکیت مستقل ظرف ۳ ساعت. هشدار بحرانی در حالت نظارتی منتظر تأیید می‌ماند. مدل زبانی برای هر سیگنال جدید علت محتمل، دو فرضیه جایگزین و نشانگر پایش می‌نویسد (برچسب استنباط).",
                "approval": {"kind": "alerts", "signal_ids": critical_ids, "items": [g["label"] for g in ctx.state["critical_signals"]]} if crit else None}


class Verifier(Agent):
    name, title_fa = "verifier", "راستی‌آزما"
    role_fa = "تطبیق چندمنبعی با منابع مستقل (گروه مالکیت)، تشخیص تناقض و برچسب اطمینان"

    def run(self, ctx):
        touched = ctx.state.get("touched_stories") or []
        stats = Counter()
        with db.tx() as c:
            for i, sid in enumerate(touched):
                arts = [dict(r) for r in c.execute("""SELECT a.id,a.title,a.lead,a.text,a.source_id,s.ownership_group g,s.credibility cred,s.name sname
                    FROM articles a JOIN sources s ON s.id=a.source_id WHERE a.story_id=? AND a.duplicate_of IS NULL ORDER BY a.published_at""", (sid,))]
                if not arts:
                    continue
                groups = {a["g"] for a in arts}
                c.execute("DELETE FROM claims WHERE story_id=?", (sid,))
                denials = [a for a in arts if has_denial(a["title"] + " " + (a["lead"] or ""))]
                for a in arts[:4]:
                    sent = (sentences(a["lead"] or "") or [a["title"]])[0][:300]
                    toks = set(tokens(sent))
                    corrob = []
                    for b in arts:
                        if b["g"] == a["g"]:
                            continue
                        bt = set(tokens(f"{b['title']} {b['lead'] or ''} {(b['text'] or '')[:3000]}"))
                        ov = len(toks & bt) / max(1, len(toks))
                        if ov >= 0.25 or len(groups) >= 3:
                            corrob.append({"article_id": b["id"], "source": b["sname"], "overlap": round(ov, 2)})
                    if len({x["source"] for x in corrob}) >= 1 and len(groups) >= 2:
                        kind, conf = "documented_fact", "high" if len(groups) >= 3 else "medium"
                    elif (a["cred"] or 0) >= 0.75:
                        kind, conf = "inference", "medium"
                    else:
                        kind, conf = "assumption", "low"
                    contra = [{"article_id": d["id"], "quote": d["title"]} for d in denials if d["id"] != a["id"]]
                    c.execute("INSERT INTO claims(story_id,text,kind,confidence,evidence,contradicted_by,created) VALUES(?,?,?,?,?,?,?)",
                              (sid, sent, kind, conf, db.jdump([{"article_id": a["id"], "quote": sent, "source": a["sname"]}] + corrob[:4]),
                               db.jdump(contra), time.time()))
                    stats[kind] += 1
                label = "documented_fact" if len(groups) >= 2 else ("inference" if max(a["cred"] or 0 for a in arts) >= 0.75 else "assumption")
                conf = "high" if len(groups) >= 3 and not denials else ("medium" if len(groups) >= 2 else "low")
                c.execute("UPDATE stories SET verification=? WHERE id=?",
                          (db.jdump({"label": label, "confidence": conf, "independent_sources": len(groups), "contradictions": len(denials)}), sid))
                if i % 30 == 0:
                    ctx.checkpoint()
                    ctx.progress((i + 1) / max(1, len(touched)))
        llm_n = self._llm_verify(ctx, touched, stats) if llm.agent_enabled("verifier") and touched else 0
        return {"summary": f"{sum(stats.values())} ادعا بررسی شد: {stats['documented_fact']} مستند، {stats['inference']} استنباط، {stats['assumption']} فرض" + (f"؛ {llm_n} داستان با راستی‌آزمایی مدل زبانی و مقایسه قاب‌بندی" if llm_n else ""),
                "input_summary": f"{len(touched)} داستان", "output": {**dict(stats), "llm_verified": llm_n},
                "reasoning": "منابع با گروه مالکیت یکسان (مثلاً BBC فارسی و BBC World) مستقل محسوب نمی‌شوند. «مستند» = حداقل ۲ گروه مستقل با هم‌پوشانی محتوا؛ تکذیب در هر منبع به‌عنوان تناقض ثبت می‌شود. برای داستان‌های چندمنبعی، مدل زبانی ادعاهای کلیدی را با شماره منبع پشتیبان/متناقض استخراج می‌کند؛ برچسب «مستند» فقط وقتی پذیرفته می‌شود که منابع پشتیبان واقعاً از دو گروه مالکیت مستقل باشند."}

    def _llm_verify(self, ctx, touched, stats):
        cand = db.q("SELECT id,title,article_count,source_count,ai FROM stories WHERE id IN (%s) AND source_count>=2 ORDER BY score DESC LIMIT 20"
                    % ",".join("?" * len(touched)), touched)
        cand = [x for x in cand if (db.jload(x["ai"], {}) or {}).get("ver_n") != x["article_count"]][:6]
        done = 0
        for batch, res in ai.parallel(ai.verify_stories, [cand[b:b + 2] for b in range(0, len(cand), 2)]):
            ctx.checkpoint()
            if isinstance(res, Exception):
                ctx.log(f"LLM verifier: {res}", "warn"); continue
            items, refmap, used = res
            ctx.tokens += used
            for it in items:
                try:
                    i = int(it.get("i")); st, arts = batch[i], refmap[i]
                except (TypeError, ValueError, IndexError):
                    continue
                claims = []
                for cl in (it.get("claims") or [])[:6]:
                    src = [arts[j] for j in cl.get("sources") or [] if isinstance(j, int) and 0 <= j < len(arts)]
                    con = [arts[j] for j in cl.get("contra") or [] if isinstance(j, int) and 0 <= j < len(arts)]
                    if not src or not cl.get("text"):
                        continue
                    kind = cl.get("kind") if cl.get("kind") in ai.LBL else "assumption"
                    if kind == "documented_fact" and len({a["g"] for a in src}) < 2:  # enforce independence rule
                        kind = "inference" if len(src) >= 1 else "assumption"
                    conf = cl.get("confidence") if cl.get("confidence") in ("high", "medium", "low") else "low"
                    claims.append((st["id"], cl["text"][:400], kind, conf,
                                   db.jdump([{"article_id": a["id"], "quote": a["title"], "source": a["sname"]} for a in src]),
                                   db.jdump([{"article_id": a["id"], "quote": a["title"]} for a in con]), time.time(), "llm"))
                    stats[kind] += 1
                if claims:
                    with db.tx() as c:
                        c.execute("DELETE FROM claims WHERE story_id=?", (st["id"],))
                        c.executemany("INSERT INTO claims(story_id,text,kind,confidence,evidence,contradicted_by,created,method) VALUES(?,?,?,?,?,?,?,?)", claims)
                db.merge_json("stories", "id", st["id"], "ai", {"ver_n": st["article_count"], "framing": it.get("framing") or [],
                                                              "consensus_fa": it.get("consensus_fa"), "gaps_fa": it.get("gaps_fa")})
                done += 1
        return done
