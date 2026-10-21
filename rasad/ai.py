"""AI task library used by agents and API. Every function batches work to amortise provider overhead,
uses short numeric ids in prompts, validates output against inputs, and never invents citations."""
import hashlib
import re
import time

from . import db, llm
from .taxonomy import ENTITY, TOPICS
from .text import normalize

PREDICATES = {"sanctions": "تحریم می‌کند", "attacks": "حمله می‌کند", "threatens": "تهدید می‌کند", "negotiates_with": "مذاکره می‌کند با",
              "meets": "دیدار می‌کند با", "supports": "حمایت می‌کند از", "condemns": "محکوم می‌کند", "accuses": "متهم می‌کند",
              "cooperates_with": "همکاری می‌کند با", "trades_with": "تجارت می‌کند با", "member_of": "عضو", "located_in": "واقع در",
              "leads": "رهبری می‌کند", "opposes": "مخالفت می‌کند با"}
ETYPES = {"PERSON", "ORG", "GPE", "LOC", "EVENT", "PRODUCT"}
LBL = {"documented_fact": "واقعیت مستند", "inference": "استنباط", "assumption": "فرض"}


def parallel(fn, batches, workers=3):
    """Run batched LLM calls concurrently; yields (batch, result or Exception) in input order."""
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fn, b) for b in batches]
        for b, f in zip(batches, futs):
            try:
                yield b, f.result()
            except Exception as e:  # noqa: BLE001
                yield b, e


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:48]


def story_articles(sid, n=4, chars=500):
    return db.q("""SELECT a.id,a.title,a.title_fa,a.lead,a.text,a.lang,a.url,a.published_at,s.name sname,s.ownership_group g,s.country
                   FROM articles a JOIN sources s ON s.id=a.source_id WHERE a.story_id=? AND a.duplicate_of IS NULL
                   ORDER BY s.credibility DESC, a.published_at LIMIT ?""", (sid, n))


def _art_txt(a, chars=450):
    body = a["text"] or a["lead"] or ""
    return f"{a['title']} — {body[:chars]}"


# ---------------- scout ----------------
def profile_sources(srcs, agent="scout"):
    items = "\n".join(f"{i}. {s['name']} | {s['url']} | lang={s.get('lang')} | country={s.get('country')}" for i, s in enumerate(srcs))
    d, used = llm.chat_json(
        "برای هر منبع خبری فهرست‌شده یک پروفایل کوتاه فارسی بساز: معرفی، مالکیت/وابستگی شناخته‌شده، گرایش سیاسی تقریبی، "
        "اعتبار پیشنهادی بین 0 تا 1 و ریسک‌ها. اگر منبع را نمی‌شناسی، صریحاً بگو و اعتبار 0.5 بده. "
        'خروجی: {"items":[{"i":0,"description_fa":"","ownership":"","bias":"","credibility":0.6,"risks":"","known":true}]}',
        llm.wrap(items, 3000), 1200, agent, "source_profile")
    out = {}
    for it in d.get("items", []):
        try:
            out[srcs[int(it["i"])]["id"]] = {k: it.get(k) for k in ("description_fa", "ownership", "bias", "credibility", "risks", "known")}
        except (KeyError, IndexError, ValueError, TypeError):
            pass
    return out, used


# ---------------- classifier ----------------
def classify_batch(arts, agent="classifier"):
    labels = {k: v["fa"] for k, v in TOPICS.items()}
    items = "\n".join(f"{i}. {a['title']} — {(a.get('lead') or '')[:220]}" for i, a in enumerate(arts))
    d, used = llm.chat_json(
        f"هر خبر را با ۱ تا ۳ برچسب از این فهرست (کلید انگلیسی) طبقه‌بندی کن: {labels}. فوریت را از بین flash/high/normal و احساس را بین -1 تا 1 بده. "
        'خروجی: {"items":[{"i":0,"topics":["sanctions"],"urgency":"normal","sentiment":0}]}',
        llm.wrap(items, 9000), 1500, agent, "classify")
    res = {}
    for it in d.get("items", []):
        try:
            t = [x for x in it.get("topics", []) if x in TOPICS][:3]
            if t:
                res[arts[int(it["i"])]["id"]] = {"topics": t, "urgency": it.get("urgency") if it.get("urgency") in ("flash", "high", "normal") else None,
                                                 "sentiment": max(-1.0, min(1.0, float(it.get("sentiment") or 0)))}
        except (IndexError, ValueError, TypeError):
            pass
    return res, used


# ---------------- entities ----------------
def ner_batch(stories, agent="entities"):
    blocks = []
    for i, s in enumerate(stories):
        arts = story_articles(s["id"], 3)
        blocks.append(f"## {i}\n" + "\n".join("- " + _art_txt(a, 350) for a in arts))
    d, used = llm.chat_json(
        "از هر داستان، موجودیت‌های نام‌دار مهم (حداکثر ۸) و روابط صریح بین آن‌ها را استخراج کن. نوع موجودیت: PERSON, ORG, GPE, LOC, EVENT, PRODUCT. "
        f"گزاره رابطه فقط از این فهرست: {list(PREDICATES)}. رابطه فقط اگر صریحاً در متن آمده باشد. "
        "برای مکان‌ها (GPE/LOC) مختصات تقریبی فقط اگر مطمئنی. "
        'خروجی: {"items":[{"i":0,"entities":[{"name_fa":"","name_en":"","name_ar":"","type":"PERSON","lat":null,"lon":null}],'
        '"relations":[{"s":"name_en","p":"sanctions","o":"name_en","quote":"<عبارت کوتاه از متن>"}]}]}',
        llm.wrap("\n\n".join(blocks), 12000), 3000, agent, "ner")
    return d.get("items", []), used


def resolve_entity(e):
    """Map an LLM entity to a gazetteer id when possible; otherwise a stable X: id."""
    en, fa_ = normalize(e.get("name_en") or ""), normalize(e.get("name_fa") or "")
    for gid, g in ENTITY.items():
        if en and normalize(g["name_en"]) == en or fa_ and normalize(g["name_fa"]) == fa_:
            return gid, True
    return "X:" + (_slug(e.get("name_en")) or _slug(e.get("name_fa")) or hashlib.sha256(fa_.encode("utf-8")).hexdigest()[:24]), False


# ---------------- dedup ----------------
def same_event(pairs, agent="dedup"):
    blocks = []
    for i, (a, b) in enumerate(pairs):
        blocks.append(f"## {i}\nA: {a['title']} — {(a.get('summary') or '')[:200]}\nB: {b['title']} — {(b.get('summary') or '')[:200]}")
    d, used = llm.chat_json(
        "برای هر جفت داستان (ممکن است به زبان‌های مختلف باشند) تعیین کن آیا هر دو دقیقاً درباره همان رویداد مشخص هستند "
        "(نه فقط همان کشور یا موضوع کلی). "
        'خروجی: {"items":[{"i":0,"same":true,"confidence":0.9,"why":""}]}',
        llm.wrap("\n\n".join(blocks), 8000), 900, agent, "dedup_adjudicate")
    return d.get("items", []), used


# ---------------- translator ----------------
def translate_stories(stories, glossary, agent="translator"):
    blocks = []
    for i, s in enumerate(stories):
        arts = story_articles(s["id"], 2)
        blocks.append(f"## {i}\nTITLE: {s['title']}\n" + "\n".join("- " + _art_txt(a, 400) for a in arts))
    d, used = llm.chat_json(
        "برای هر داستان: (۱) title_fa ترجمه دقیق و روان عنوان به فارسی؛ (۲) headline_fa تیتر بی‌طرف فارسی که رویداد را بدون قضاوت بیان کند؛ "
        "(۳) summary_fa خلاصه دو جمله‌ای فارسی فقط بر اساس متن. قیدهای تردید (گزارش شده، ادعا شده) را حفظ کن. واژه‌نامه را رعایت کن:\n" + glossary +
        '\nخروجی: {"items":[{"i":0,"title_fa":"","headline_fa":"","summary_fa":""}]}',
        llm.wrap("\n\n".join(blocks), 12000), 2600, agent, "translate_story")
    return d.get("items", []), used


def translate_titles(arts, glossary, agent="translator"):
    items = "\n".join(f"{i}. [{a['lang']}] {a['title']}" for i, a in enumerate(arts))
    d, used = llm.chat_json("عنوان‌های خبری زیر را به فارسی معیار و روان ترجمه کن. واژه‌نامه:\n" + glossary +
                            '\nخروجی: {"items":[{"i":0,"fa":""}]}', llm.wrap(items, 8000), 2200, agent, "translate_titles")
    return d.get("items", []), used


# ---------------- storyline ----------------
def story_arcs(stories, agent="storyline"):
    blocks = []
    for i, s in enumerate(stories):
        ev = db.q("""SELECT e.t,e.type,e.summary,src.name FROM storyline_events e LEFT JOIN sources src ON src.id=e.source_id
                     WHERE e.story_id=? ORDER BY e.t LIMIT 14""", (s["id"],))
        blocks.append(f"## {i} {s['title']}\n" + "\n".join(f"- {time.strftime('%m-%d %H:%M', time.localtime(e['t']))} [{e['type']}] {e['name']}: {e['summary']}" for e in ev))
    d, used = llm.chat_json(
        "برای هر خط داستان بر اساس رویدادهای زمان‌دار، روایت تحول را به فارسی بنویس: arc_fa (۲ تا ۳ جمله: شروع، تحول، وضعیت فعلی)، "
        "phase یکی از emerging/escalating/plateau/de-escalating/resolved، و watch_fa (یک نشانگر مشخص برای پایش). "
        'خروجی: {"items":[{"i":0,"arc_fa":"","phase":"emerging","watch_fa":""}]}',
        llm.wrap("\n\n".join(blocks), 10000), 1800, agent, "story_arc")
    return d.get("items", []), used


# ---------------- anomaly ----------------
def explain_signals(sigs, agent="anomaly"):
    blocks = []
    for i, g in enumerate(sigs):
        heads = []
        ids = db.jload(g.get("story_ids"), []) if isinstance(g.get("story_ids"), str) else (g.get("story_ids") or [])
        if ids:
            heads = [r["t"] for r in db.q("SELECT COALESCE(title_fa,title) t FROM stories WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)]
        elif g["type"] in ("new_actor", "entity_spike"):
            heads = [r["t"] for r in db.q("""SELECT COALESCE(a.title_fa,a.title) t FROM mentions m JOIN articles a ON a.id=m.article_id
                     WHERE m.entity_id=? AND a.duplicate_of IS NULL ORDER BY a.published_at DESC LIMIT 6""", (g["key"],))]
        else:
            heads = [r["t"] for r in db.q("""SELECT COALESCE(title_fa,title) t FROM articles WHERE category=? AND duplicate_of IS NULL
                     ORDER BY published_at DESC LIMIT 6""", (g["key"],))]
        blocks.append(f"## {i} {g['label']} (score={g['score']}, baseline={g['baseline']}, observed={g['observed']})\n" + "\n".join("- " + h for h in heads[:6]))
    d, used = llm.chat_json(
        "برای هر سیگنال آماری، با تکیه فقط بر عنوان‌های مرتبط: explanation_fa (محتمل‌ترین علت، ۱ تا ۲ جمله)، alternatives_fa (۲ فرضیه جایگزین شامل علت‌های فنی/رسانه‌ای)، "
        "watch_fa (نشانگر بعدی برای تأیید یا رد)، significance یکی از low/medium/high. "
        'خروجی: {"items":[{"i":0,"explanation_fa":"","alternatives_fa":["",""],"watch_fa":"","significance":"medium"}]}',
        llm.wrap("\n\n".join(blocks), 8000), 1600, agent, "explain_signal")
    return d.get("items", []), used


# ---------------- verifier ----------------
def verify_stories(stories, agent="verifier"):
    blocks, refmap = [], []
    for i, s in enumerate(stories):
        arts = story_articles(s["id"], 6)
        refmap.append(arts)
        blocks.append(f"## {i} {s['title']}\n" + "\n".join(f"[{j}] ({a['sname']}, گروه {a['g']}, {a['lang']}) {_art_txt(a, 380)}" for j, a in enumerate(arts)))
    d, used = llm.chat_json(
        "برای هر داستان: ادعاهای کلیدی قابل‌راستی‌آزمایی (۲ تا ۵) را به فارسی استخراج کن. برای هر ادعا sources (شماره منابع پشتیبان)، contra (شماره منابع متناقض یا تکذیب‌کننده)، "
        "kind: documented_fact اگر حداقل دو منبع از گروه‌های متفاوت آن را گزارش کرده‌اند، inference اگر نتیجه‌گیری از شواهد است، assumption اگر فقط یک منبع یا ادعای یک طرف است؛ "
        "confidence: high/medium/low. سپس framing: برای هر زبان/رسانه، قاب‌بندی متفاوت را در یک جمله توضیح بده (تفاوت لحن، تأکید، واژه‌گزینی). "
        'خروجی: {"items":[{"i":0,"claims":[{"text":"","sources":[0,1],"contra":[],"kind":"documented_fact","confidence":"high"}],'
        '"framing":[{"who":"","frame_fa":""}],"consensus_fa":"","gaps_fa":""}]}',
        llm.wrap("\n\n".join(blocks), 14000), 3000, agent, "verify")
    return d.get("items", []), refmap, used


# ---------------- writer ----------------
def write_brief(refs, stories_txt, signals_txt, agent="writer"):
    ref_txt = "\n".join(f"[{i+1}] {r['sname']}: {r['title']}" for i, r in enumerate(refs))
    d, used = llm.chat_json(
        "تو تحلیلگر ارشد اطلاعات منبع‌باز هستی. بر اساس داستان‌ها، سیگنال‌ها و فهرست منابع شماره‌دار، بریف فارسی بنویس. قواعد سخت: "
        "هر جمله ادعایی باید حداقل یک ارجاع [n] از فهرست داشته باشد و با یکی از برچسب‌های [واقعیت مستند] یا [استنباط] یا [فرض] مشخص شود؛ "
        "عدد، نام یا تاریخی خارج از داده نیاور. "
        'خروجی: {"bluf":"<جمع‌بندی یک‌جمله‌ای>","executive_summary":"<۳ تا ۵ جمله>","analysis":[{"heading":"","text":"<۲ تا ۴ جمله>"}],'
        '"outlook":"<چشم‌انداز ۲۴ تا ۷۲ ساعت آینده، با برچسب [استنباط]>","key_judgments":["",""]}',
        llm.wrap(f"# داستان‌ها\n{stories_txt}\n\n# سیگنال‌ها\n{signals_txt}\n\n# منابع\n{ref_txt}", 16000), 2600, agent, "brief")
    return d, used


def citation_guard(text, n_refs):
    """Keep only sentences whose citations all exist; returns (kept_text, dropped_count)."""
    if not text:
        return "", 0
    parts = [x.strip() for x in re.split(r"(?<=[\.\!؟\?])\s+", text) if x.strip()]
    keep = [x for x in parts if re.search(r"\[(\d+)\]", x) and all(1 <= int(n) <= n_refs for n in re.findall(r"\[(\d+)\]", x))]
    return " ".join(keep), len(parts) - len(keep)


# ---------------- viz ----------------
def narrate_scenes(scenes, agent="viz"):
    items = "\n".join(f"{i}. {s['caption_fa']}" for i, s in enumerate(scenes))
    d, used = llm.chat_json("برای هر صحنه حالت نمایش یک روایت کوتاه فارسی (حداکثر ۲۵ کلمه، لحن گزارشگر خبری بی‌طرف) بنویس که فقط بر عنوان تکیه کند. "
                            '{"items":[{"i":0,"narration_fa":""}]}', llm.wrap(items, 4000), 900, agent, "narrate")
    return d.get("items", []), used


# ---------------- qa ----------------
def judge_brief(markdown, refs, agent="qa"):
    ref_txt = "\n".join(f"[{i+1}] {r['sname']}: {r['title']}" for i, r in enumerate(refs))
    d, used = llm.chat_json(
        "تو ارزیاب کیفیت هستی. جمله‌های دارای ارجاع در بریف را با عنوان منابع ارجاع‌شده مقایسه کن. faithfulness بین 0 و 1 (سهم جمله‌هایی که منبعشان واقعاً آن را پشتیبانی می‌کند)، "
        "flagged: جمله‌های مشکل‌دار با دلیل، label_misuse: تعداد برچسب‌های نادرست (مثلاً ادعای تک‌منبعی با برچسب واقعیت مستند). "
        '{"faithfulness":0.9,"flagged":[{"sentence":"","reason":""}],"label_misuse":0,"notes_fa":""}',
        llm.wrap(f"# بریف\n{markdown[:9000]}\n\n# منابع\n{ref_txt}", 14000), 1400, agent, "judge")
    return d, used


# ---------------- on-demand ----------------
def deep_analyze(sid, agent="analyze"):
    s = db.q1("SELECT * FROM stories WHERE id=?", (sid,))
    arts = story_articles(sid, 8)
    ev = db.q("SELECT t,type,summary FROM storyline_events WHERE story_id=? ORDER BY t LIMIT 20", (sid,))
    ctx = "\n".join(f"[{i+1}] ({a['sname']}, {a['lang']}, {time.strftime('%m-%d %H:%M', time.localtime(a['published_at']))}) {_art_txt(a, 700)}" for i, a in enumerate(arts))
    evt = "\n".join(f"- {time.strftime('%m-%d %H:%M', time.localtime(e['t']))} [{e['type']}] {e['summary']}" for e in ev)
    d, used = llm.chat_json(
        "تحلیل عمیق فارسی این داستان خبری را بر اساس منابع شماره‌دار بنویس. هر ادعا ارجاع [n] و برچسب [واقعیت مستند|استنباط|فرض] داشته باشد. "
        '{"summary_fa":"","background_fa":"","actors":[{"name":"","role_fa":"","stance_fa":""}],"claims":[{"text":"","refs":[1],"kind":"documented_fact"}],'
        '"framing":[{"who":"","frame_fa":""}],"implications_fa":"","scenarios":[{"name_fa":"","likelihood":"low|medium|high","indicators_fa":""}],"open_questions":[""]}',
        llm.wrap(f"# داستان: {s['title']}\n# خط زمانی\n{evt}\n# منابع\n{ctx}", 16000), 3200, agent, "deep_analysis")
    d["refs"] = [{"n": i + 1, "id": a["id"], "source": a["sname"], "title": a["title"], "url": a["url"], "lang": a["lang"]} for i, a in enumerate(arts)]
    d["created"] = time.time()
    return d, used


def retrieve(question, k=14, hours=168):
    """Hybrid retrieval: FTS over normalised tokens + entity matches; returns article rows."""
    from .taxonomy import find_entities
    from .text import tokens
    toks = [t for t in tokens(question) if len(t) > 2][:12]
    rows, seen = [], set()
    since = time.time() - hours * 3600
    if toks:
        qexpr = " OR ".join('"%s"' % t.replace('"', "") for t in toks)
        try:
            for r in db.q("""SELECT a.id FROM articles_fts f JOIN articles a ON a.id=f.id WHERE articles_fts MATCH ? AND a.published_at>?
                             AND a.duplicate_of IS NULL ORDER BY bm25(articles_fts) LIMIT ?""", (qexpr, since, k * 2)):
                if r["id"] not in seen:
                    seen.add(r["id"]); rows.append(r["id"])
        except Exception:
            pass
    ents = list(find_entities(question))
    if ents:
        for r in db.q("""SELECT a.id FROM mentions m JOIN articles a ON a.id=m.article_id JOIN stories s ON s.id=a.story_id
                         WHERE m.entity_id IN (%s) AND a.published_at>? AND a.duplicate_of IS NULL ORDER BY s.score DESC, a.published_at DESC LIMIT ?"""
                      % ",".join("?" * len(ents)), (*ents, since, k)):
            if r["id"] not in seen:
                seen.add(r["id"]); rows.append(r["id"])
    rows = rows[:k]
    if not rows:
        return []
    arts = db.q("""SELECT a.id,a.title,a.title_fa,a.lead,a.text,a.lang,a.url,a.published_at,a.story_id,s.name sname FROM articles a
                   JOIN sources s ON s.id=a.source_id WHERE a.id IN (%s)""" % ",".join("?" * len(rows)), rows)
    order = {r: i for i, r in enumerate(rows)}
    return sorted(arts, key=lambda a: order[a["id"]])


ASK_SYSTEM = ("تو «دستیار تحلیلی رصد» هستی و فقط بر اساس اسناد شماره‌دار داخل <untrusted_content> پاسخ می‌دهی. پاسخ فارسی، ساختاریافته (Markdown کوتاه) و دقیق باشد. "
              "بعد از هر جمله ادعایی ارجاع [n] بگذار و در صورت لزوم برچسب [واقعیت مستند|استنباط|فرض] بزن. اگر اسناد پاسخ را پوشش نمی‌دهند، صریحاً بگو و حدس نزن. "
              "تاریخ و زمان امروز: {now}.")


def ask_context(arts):
    return "\n".join(f"[{i+1}] ({a['sname']}, {a['lang']}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(a['published_at']))}) "
                     f"{a['title']}" + (f" | {a['title_fa']}" if a.get("title_fa") and a["lang"] != "fa" else "") + f" — {(a['text'] or a['lead'] or '')[:600]}"
                     for i, a in enumerate(arts))


def custom_brief(topic=None, entity=None, hours=24, agent="analyze"):
    since = time.time() - hours * 3600
    where, args = ["s.last_update>?"], [since]
    if topic:
        where.append("s.category=?"); args.append(topic)
    if entity:
        where.append("s.entities LIKE ?"); args.append(f'%"{entity}"%')
    stories = db.q(f"SELECT s.* FROM stories s WHERE {' AND '.join(where)} ORDER BY s.score DESC LIMIT 8", args)
    refs, lines = [], []
    for s in stories:
        arts = story_articles(s["id"], 2)
        cites = []
        for a in arts:
            refs.append({"sname": a["sname"], "title": a["title"], "url": a["url"], "published_at": a["published_at"]}); cites.append(f"[{len(refs)}]")
        v = db.jload(s["verification"], {}) or {}
        lines.append(f"- {s['title_fa'] or s['title']} ({s['source_count']} منبع، برچسب {LBL.get(v.get('label'), 'فرض')}) {''.join(cites)}")
    return stories, refs, lines
