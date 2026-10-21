import asyncio
import json
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import ai, auth, db, explore, llm, notify, ops, triage, watch
from . import supervisor as sup_mod
from .config import settings
from .supervisor import bus
from .taxonomy import ENTITY, TOPICS
from .text import normalize

@asynccontextmanager
async def lifespan(app):
    db.init()
    from .agents.ingest import sync_sources
    from .analysis.jobs import worker
    sync_sources()
    s = sup_mod.get()
    tasks = [asyncio.create_task(s.scheduler()), asyncio.create_task(_digest_ticker()),
             asyncio.create_task(worker())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A running synchronous analysis call may finish after cancellation; leases
        # and checkpoints make it resumable on restart. Run one ASGI process.


app = FastAPI(title="Rasad · رصد", version="0.5.0", lifespan=lifespan)
from .analysis.routes import router as analysis_router
app.include_router(analysis_router)


async def _digest_ticker():
    """Checks every 5 minutes whether the daily shift digest is due (works in every execution mode)."""
    while True:
        try:
            await asyncio.to_thread(notify.dispatch_digest)
        except Exception as e:  # noqa: BLE001
            print("digest:", e)
        await asyncio.sleep(300)


def _auth(token):
    """Kept for backwards compatibility; access control is enforced by AuthMiddleware (RBAC)."""
    return None


class AuthMiddleware:
    """Pure-ASGI RBAC middleware (keeps streaming responses intact and propagates the user via contextvars)."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        hdr = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        user = None
        if settings.admin_token and hdr.get("x-admin-token") == settings.admin_token:
            user = auth.SYSTEM_ADMIN
        else:
            tok = None
            if hdr.get("authorization", "").lower().startswith("bearer "):
                tok = hdr["authorization"][7:].strip()
            else:
                for part in hdr.get("cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == auth.COOKIE:
                        tok = v
            user = await asyncio.to_thread(auth.user_from_token, tok) if tok else None
        if not auth.enabled():
            user = user or auth.OPEN_USER
        need = auth.required_role(scope["method"], scope["path"])
        if not auth.allowed(user, need):
            code, msg = (401, "ورود لازم است") if user is None else (403, "دسترسی این نقش کافی نیست")
            body = json.dumps({"detail": msg, "need": need}, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": code, "headers": [(b"content-type", b"application/json; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
            return
        token = auth.current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.current_user.reset(token)


app.add_middleware(AuthMiddleware)


def H(hours):
    return time.time() - hours * 3600


_ecache = {"t": 0, "d": {}}


def ent(e):
    """Gazetteer entity or LLM-discovered entity from DB."""
    if e in ENTITY:
        return ENTITY[e]
    if time.time() - _ecache["t"] > 30:
        _ecache["d"] = {r["id"]: r for r in db.q("SELECT id,type,name_fa,name_en,name_ar,lat,lon,iso FROM entities WHERE id LIKE 'X:%'")}
        _ecache["t"] = time.time()
    r = _ecache["d"].get(e)
    return dict(r, llm=True) if r else None


def story_row(s):
    s = dict(s)
    for k in ("topics", "regions", "entities", "verification", "ai"):
        s[k] = db.jload(s.get(k), [] if k not in ("verification", "ai") else {})
    s.pop("centroid", None)
    s["category_fa"] = TOPICS.get(s.get("category"), {}).get("fa")
    s["color"] = TOPICS.get(s.get("category"), {}).get("color")
    return s


# ---------------- meta / stats ----------------
@app.get("/api/meta")
def meta():
    return {"topics": {k: {"fa": v["fa"], "color": v["color"]} for k, v in TOPICS.items()},
            "llm": {"configured": llm.available(), "model": llm.cfg()["model"] if llm.available() else None, "tokens_today": llm.tokens_today(),
                    "budget": llm.cfg()["daily_budget"], "agents": llm.cfg()["agents"]},
            "version": app.version, "state": sup_mod.get().state()}


@app.get("/api/stats")
def stats(hours: int = 24):
    since = H(hours)
    a = db.q1("""SELECT COUNT(*) n, SUM(duplicate_of IS NULL) uniq, COUNT(DISTINCT source_id) src FROM articles WHERE published_at>?""", (since,))
    last_h = db.q1("SELECT COUNT(*) n FROM articles WHERE published_at>? AND duplicate_of IS NULL", (H(1),))["n"]
    st = db.q1("SELECT COUNT(*) n FROM stories WHERE last_update>?", (since,))["n"]
    sig = db.q1("SELECT COUNT(*) n, SUM(severity='critical') c FROM signals WHERE created>? AND status!='dismissed'", (since,))
    langs = {r["lang"]: r["n"] for r in db.q("SELECT lang, COUNT(*) n FROM articles WHERE published_at>? AND duplicate_of IS NULL GROUP BY lang", (since,))}
    cats = {r["category"]: r["n"] for r in db.q("SELECT category, COUNT(*) n FROM articles WHERE published_at>? AND duplicate_of IS NULL GROUP BY category", (since,))}
    hourly = db.q("""SELECT CAST(published_at/3600 AS INT) h, COUNT(*) n FROM articles WHERE published_at>? AND duplicate_of IS NULL GROUP BY h ORDER BY h""", (since,))
    qa = db.kv_get("qa", {})
    return {"articles": a["n"], "unique": a["uniq"] or 0, "sources": a["src"], "per_hour_last": last_h, "stories": st,
            "signals": sig["n"], "critical": sig["c"] or 0, "languages": langs, "categories": cats,
            "hourly": [{"t": r["h"] * 3600, "n": r["n"]} for r in hourly], "qa": qa,
            "agents_last": {r["agent"]: r for r in db.q("""SELECT agent,status,output_summary,ended FROM steps WHERE id IN
                            (SELECT id FROM steps s WHERE ended IS NOT NULL AND ended = (SELECT MAX(ended) FROM steps s2 WHERE s2.agent=s.agent))""")}}


# ---------------- articles / stories ----------------
@app.get("/api/articles")
def articles(limit: int = 100, offset: int = 0, category: str = None, lang: str = None, q: str = None, source: str = None, hours: int = 72):
    where, args = ["a.published_at>?", "a.duplicate_of IS NULL"], [H(hours)]
    if category:
        where.append("a.category=?"); args.append(category)
    if lang:
        where.append("a.lang=?"); args.append(lang)
    if source:
        where.append("a.source_id=?"); args.append(source)
    if q:
        where.append("a.id IN (SELECT id FROM articles_fts WHERE articles_fts MATCH ?)")
        args.append(" ".join(f'"{t}"' for t in normalize(q).split()))
    rows = db.q(f"""SELECT a.id,a.title,a.title_fa,a.lead,a.url,a.lang,a.published_at,a.fetched_at,a.category,a.urgency,a.sentiment,a.story_id,
                    a.source_id,s.name source_name FROM articles a JOIN sources s ON s.id=a.source_id WHERE {' AND '.join(where)}
                    ORDER BY a.published_at DESC LIMIT ? OFFSET ?""", (*args, limit, offset))
    for r in rows:
        r["color"] = TOPICS.get(r["category"], {}).get("color")
        r["lead"] = (r["lead"] or "")[:280]
    return rows


@app.get("/api/articles/{aid}")
def article(aid: str):
    a = db.q1("""SELECT a.*, s.name source_name, s.credibility, s.ownership_group FROM articles a JOIN sources s ON s.id=a.source_id WHERE a.id=?""", (aid,))
    if not a:
        raise HTTPException(404)
    a["topics"] = db.jload(a["topics"], [])
    a["entities"] = [dict(ent(m["entity_id"]) or {"id": m["entity_id"]}, surface=m["surface"]) for m in db.q("SELECT * FROM mentions WHERE article_id=?", (aid,))]
    a["duplicates"] = db.q("SELECT a.id,a.url,s.name source_name FROM articles a JOIN sources s ON s.id=a.source_id WHERE duplicate_of=?", (aid,))
    return a


@app.get("/api/stories")
def stories(limit: int = 60, hours: int = 48, category: str = None, entity: str = None, min_sources: int = 1):
    where, args = ["last_update>?", "source_count>=?"], [H(hours), min_sources]
    if category:
        where.append("category=?"); args.append(category)
    if entity:
        where.append("entities LIKE ?"); args.append(f'%"{entity}"%')
    return [story_row(s) for s in db.q(f"SELECT * FROM stories WHERE {' AND '.join(where)} ORDER BY score DESC LIMIT ?", (*args, limit))]


@app.get("/api/stories/{sid}")
def story(sid: str):
    s = db.q1("SELECT * FROM stories WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404)
    s = story_row(s)
    s["articles"] = db.q("""SELECT a.id,a.title,a.title_fa,a.lead,a.url,a.lang,a.published_at,a.duplicate_of,a.urgency,a.sentiment,a.extraction_method,
                         s.name source_name,s.id source_id,s.ownership_group,s.credibility FROM articles a JOIN sources s ON s.id=a.source_id
                         WHERE a.story_id=? ORDER BY a.published_at""", (sid,))
    s["timeline"] = db.q("SELECT * FROM storyline_events WHERE story_id=? ORDER BY t", (sid,))
    s["claims"] = [dict(c, evidence=db.jload(c["evidence"], []), contradicted_by=db.jload(c["contradicted_by"], []))
                   for c in db.q("SELECT * FROM claims WHERE story_id=?", (sid,))]
    s["entity_objs"] = [x for x in (ent(e) for e in s["entities"]) if x]
    s["llm_entities"] = db.q("""SELECT DISTINCT e.id,e.type,e.name_fa,e.name_en FROM mentions m JOIN entities e ON e.id=m.entity_id
                                JOIN articles a ON a.id=m.article_id WHERE a.story_id=? AND e.id LIKE 'X:%'""", (sid,))
    return s


# ---------------- graph / geo / trends ----------------
@app.get("/api/graph")
def graph(hours: int = 72, min_weight: int = 2, focus: str = None, limit: int = 400):
    since = H(hours)
    ents = {r["entity_id"]: r["n"] for r in db.q("""SELECT m.entity_id, COUNT(*) n FROM mentions m JOIN articles a ON a.id=m.article_id
                                                  WHERE a.published_at>? AND a.duplicate_of IS NULL GROUP BY m.entity_id""", (since,))}
    rels = db.q("""SELECT subject_id s, predicate p, object_id o, weight w, evidence FROM relations WHERE last_seen>? AND (weight>=? OR predicate!='co_mentioned')
                   ORDER BY (predicate!='co_mentioned') DESC, weight DESC LIMIT ?""", (since, min_weight, limit * 3))
    if focus:
        nb = {focus} | {r["o"] for r in rels if r["s"] == focus} | {r["s"] for r in rels if r["o"] == focus}
        rels = [r for r in rels if r["s"] in nb and r["o"] in nb]
    used = {r["s"] for r in rels} | {r["o"] for r in rels} | ({focus} if focus else set())
    nodes = [dict(ent(e), mentions=ents.get(e, 0)) for e in used if ent(e)]
    ok = {n["id"] for n in nodes}
    return {"nodes": nodes, "predicates": ai.PREDICATES,
            "links": [{"source": r["s"], "target": r["o"], "weight": r["w"], "predicate": r["p"], "predicate_fa": ai.PREDICATES.get(r["p"]),
                       "evidence": db.jload(r["evidence"], [])[-3:]} for r in rels if r["s"] in ok and r["o"] in ok]}


@app.get("/api/path")
def path(a: str, b: str, hours: int = 168):
    rels = db.q("SELECT subject_id s, object_id o, weight w FROM relations WHERE last_seen>?", (H(hours),))
    adj = defaultdict(set)
    for r in rels:
        adj[r["s"]].add(r["o"]); adj[r["o"]].add(r["s"])
    prev, frontier, seen = {}, [a], {a}
    while frontier and b not in seen:
        nxt = []
        for x in frontier:
            for y in adj[x]:
                if y not in seen:
                    seen.add(y); prev[y] = x; nxt.append(y)
        frontier = nxt
    if b not in seen:
        return {"path": []}
    p = [b]
    while p[-1] != a:
        p.append(prev[p[-1]])
    return {"path": [ent(x) or {"id": x} for x in reversed(p)]}


@app.get("/api/geo")
def geo(hours: int = 48):
    since = H(hours)
    pts = db.q("""SELECT m.entity_id e, a.category c, COUNT(*) n, AVG(a.sentiment) s FROM mentions m JOIN articles a ON a.id=m.article_id
                  WHERE a.published_at>? AND a.duplicate_of IS NULL GROUP BY m.entity_id, a.category""", (since,))
    agg = defaultdict(lambda: {"n": 0, "cats": Counter(), "s": 0.0})
    for p in pts:
        E = ent(p["e"])
        if E and E.get("lat") is not None:
            g = agg[p["e"]]
            g["n"] += p["n"]; g["cats"][p["c"]] += p["n"]; g["s"] += (p["s"] or 0) * p["n"]
    points = []
    for e, g in agg.items():
        E = ent(e)
        cat = g["cats"].most_common(1)[0][0]
        points.append({**E, "n": g["n"], "category": cat, "color": TOPICS.get(cat, {}).get("color"), "sentiment": round(g["s"] / g["n"], 2)})
    arcs = []
    for r in db.q("SELECT subject_id s, object_id o, weight w FROM relations WHERE last_seen>? AND weight>=2 ORDER BY weight DESC LIMIT 150", (since,)):
        A, B = ent(r["s"]), ent(r["o"])
        if A and B and A["type"] == "GPE" and B["type"] == "GPE" and A.get("lat") is not None and B.get("lat") is not None:
            arcs.append({"from": A["id"], "to": B["id"], "fromName": A["name_fa"], "toName": B["name_fa"], "coords": [[A["lon"], A["lat"]], [B["lon"], B["lat"]]], "w": r["w"]})
    return {"points": points, "arcs": arcs}


@app.get("/api/trends")
def trends(hours: int = 48, bucket: int = 3600):
    rows = db.q("""SELECT CAST(published_at/? AS INT)*? t, category c, COUNT(*) n FROM articles WHERE published_at>? AND duplicate_of IS NULL
                   GROUP BY t, c ORDER BY t""", (bucket, bucket, H(hours)))
    reg = db.q("""SELECT m.entity_id e, a.category c, COUNT(*) n FROM mentions m JOIN articles a ON a.id=m.article_id
                  WHERE a.published_at>? AND a.duplicate_of IS NULL AND length(m.entity_id)=2 GROUP BY e, c ORDER BY n DESC""", (H(hours),))
    top_regions = [e for e, _ in Counter({r["e"]: 0 for r in reg}).items()]
    tot = Counter()
    for r in reg:
        tot[r["e"]] += r["n"]
    top_regions = [e for e, _ in tot.most_common(14)]
    return {"river": [{"t": r["t"], "category": r["c"], "n": r["n"]} for r in rows],
            "heat": [{"region": r["e"], "region_fa": ENTITY[r["e"]]["name_fa"], "category": r["c"], "n": r["n"]} for r in reg if r["e"] in top_regions],
            "regions": [{"id": e, "fa": ENTITY[e]["name_fa"]} for e in top_regions]}


# ---------------- sources ----------------
@app.get("/api/sources")
def sources():
    rows = db.q("SELECT * FROM sources ORDER BY status, name")
    day = H(24)
    for s in rows:
        st = db.q1("""SELECT COUNT(*) n, AVG(extraction_quality) q FROM articles WHERE source_id=? AND published_at>?""", (s["id"], day))
        s["items_24h"], s["extract_q"] = st["n"], round(st["q"] or 0, 2)
        # speed: median minutes behind the first report of shared stories
        lags = db.q("""SELECT a.published_at - st.first_seen lag FROM articles a JOIN stories st ON st.id=a.story_id
                       WHERE a.source_id=? AND st.source_count>=2 AND a.published_at>?""", (s["id"], day))
        L = sorted(max(0, r["lag"]) / 60 for r in lags)
        s["lag_median_min"] = round(L[len(L) // 2], 1) if L else None
        s["first_reports"] = sum(1 for x in L if x == 0)
        s["meta"] = db.jload(s["meta"], {})
        s.pop("etag", None); s.pop("modified", None)
    return {"sources": rows, "scout": db.kv_get("scout_report", {})}


@app.post("/api/sources/{sid}/status")
def set_source_status(sid: str, body: dict = Body(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    status = body.get("status")
    if status not in ("active", "disabled", "pending"):
        raise HTTPException(400)
    with db.tx() as c:
        c.execute("UPDATE sources SET status=?, fail_count=0 WHERE id=?", (status, sid))
    db.audit("user", f"source:{status}", sid)
    return {"ok": True}


@app.get("/api/compare/{sid}")
def compare(sid: str):
    s = story(sid)
    groups = defaultdict(list)
    for a in s["articles"]:
        groups[a["source_name"]].append(a)
    first = s["first_seen"]
    return {"story": {k: s[k] for k in ("id", "title", "title_fa", "first_seen")},
            "sources": [{"source": k, "lag_min": round((v[0]["published_at"] - first) / 60, 1), "n": len(v), "lang": v[0]["lang"],
                         "sentiment": round(sum(x["sentiment"] or 0 for x in v) / len(v), 2), "headline": v[0]["title"], "url": v[0]["url"]}
                        for k, v in groups.items()]}


# ---------------- signals / briefs / qa ----------------
@app.get("/api/signals")
def signals(hours: int = 72):
    rows = db.q("SELECT * FROM signals WHERE created>? ORDER BY created DESC LIMIT 200", (H(hours),))
    for r in rows:
        r["story_ids"] = db.jload(r["story_ids"], []); r["explanation"] = db.jload(r["explanation"], {}); r["ai"] = db.jload(r.get("ai"), None)
    return rows


@app.post("/api/signals/{gid}/status")
def signal_status(gid: int, body: dict = Body(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    with db.tx() as c:
        c.execute("UPDATE signals SET status=? WHERE id=?", (body.get("status", "ack"), gid))
    db.audit("user", "signal:" + body.get("status", "ack"), str(gid))
    return {"ok": True}


@app.get("/api/briefs")
def briefs(limit: int = 30):
    return db.q("SELECT id,created,title,status,stats,edited_by_user FROM briefs ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/briefs/{bid}")
def brief(bid: int):
    b = db.q1("SELECT * FROM briefs WHERE id=?", (bid,))
    if not b:
        raise HTTPException(404)
    return b


@app.get("/api/briefs/{bid}.md", response_class=PlainTextResponse)
def brief_md(bid: int):
    return brief(bid)["markdown"]


@app.get("/api/qa")
def qa():
    return {"current": db.kv_get("qa", {}), "history": db.kv_get("qa_history", [])[-100:], "judge": db.kv_get("qa_judge")}


@app.get("/api/presentation")
def presentation():
    return db.kv_get("presentation", {"scenes": []})


@app.get("/api/search")
def search(q: str, limit: int = 20):
    nq = normalize(q)
    ents = [e for e in ENTITY.values() if nq and (nq in normalize(e["name_fa"]) or nq in e["name_en"].lower() or nq in normalize(e["name_ar"]))][:8]
    sts = db.q("SELECT id,title,title_fa,category FROM stories WHERE title LIKE ? OR title_fa LIKE ? ORDER BY score DESC LIMIT ?", (f"%{q}%", f"%{q}%", limit))
    arts = articles(limit=limit, q=q, hours=24 * 30) if q.strip() else []
    return {"entities": ents, "stories": sts, "articles": arts}


# ---------------- AI: config / usage / interactive ----------------
@app.get("/api/ai/config")
def ai_config():
    return {**llm.public_cfg(), "agents_fa": llm.AI_AGENTS}


@app.post("/api/ai/config")
def ai_config_set(body: dict = Body(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    db.audit("user", "ai:config", "", {k: v for k, v in body.items() if k != "api_key"} | ({"api_key": "***"} if body.get("api_key") else {}))
    return llm.update_cfg(body)


@app.post("/api/ai/test")
async def ai_test(x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    try:
        return await asyncio.to_thread(llm.test_connection)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.get("/api/ai/models")
async def ai_models():
    try:
        return {"models": await asyncio.to_thread(llm.list_models)}
    except Exception as e:
        return {"models": [], "error": str(e)[:200]}


@app.get("/api/ai/usage")
def ai_usage(days: int = 7):
    return llm.usage(days)


def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/ask")
async def ask(body: dict = Body(...)):
    question = (body.get("question") or "").strip()[:1200]
    history = [m for m in (body.get("history") or [])[-6:] if m.get("role") in ("user", "assistant")]
    hours = int(body.get("hours") or 168)
    if not question:
        raise HTTPException(400, "question required")

    chat_user_id = (auth.current_user.get() or {}).get("id", 0)

    async def gen():
        if not llm.agent_enabled("ask"):
            yield _sse({"type": "error", "error": "دستیار تحلیلی غیرفعال است یا سقف توکن روزانه تمام شده؛ تنظیمات هوش مصنوعی را بررسی کنید."}); return
        arts = await asyncio.to_thread(ai.retrieve, question, 14, hours)
        if not arts:
            yield _sse({"type": "delta", "text": "شواهد مرتبط کافی در بازه درخواستی یافت نشد."})
            yield _sse({"type": "done", "chat_id": None, "used": []})
            return
        cites = [{"n": i + 1, "id": a["id"], "story_id": a["story_id"], "source": a["sname"], "title": a.get("title_fa") or a["title"],
                  "url": a["url"], "lang": a["lang"], "t": a["published_at"]} for i, a in enumerate(arts)]
        yield _sse({"type": "sources", "items": cites})
        msgs = [{"role": m["role"], "content": str(m.get("content", ""))[:3000]} for m in history]
        msgs.append({"role": "user", "content": f"اسناد بازیابی‌شده:\n{llm.wrap(ai.ask_context(arts), 14000)}\n\nپرسش: {question}"})
        out = []
        try:
            async for d in llm.stream(ai.ASK_SYSTEM.format(now=time.strftime("%Y-%m-%d %H:%M")), msgs, 1600, "ask", "chat"):
                out.append(d)
                yield _sse({"type": "delta", "text": d})
        except Exception as e:
            yield _sse({"type": "error", "error": str(e)[:300]}); return
        ans = "".join(out)
        used = sorted({int(n) for n in __import__("re").findall(r"\[(\d+)\]", ans) if 1 <= int(n) <= len(cites)})
        with db.tx() as c:
            cur = c.execute("INSERT INTO chats(ts,question,answer,citations,tokens,user_id) VALUES(?,?,?,?,?,?)",
                            (time.time(), question, ans, db.jdump([x for x in cites if x["n"] in used]), 0, chat_user_id))
        yield _sse({"type": "done", "chat_id": cur.lastrowid, "used": used})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/ask/history")
def ask_history(limit: int = 20):
    return [dict(r, citations=db.jload(r["citations"], [])) for r in db.q("SELECT * FROM chats WHERE user_id=? ORDER BY id DESC LIMIT ?", ((auth.current_user.get() or {}).get("id", 0), max(1,min(limit,100))))]


@app.post("/api/stories/{sid}/analyze")
async def story_analyze(sid: str, body: dict = Body(default={})):
    s = db.q1("SELECT ai FROM stories WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404)
    cur = db.jload(s["ai"], {}) or {}
    if cur.get("deep") and not body.get("force"):
        return cur["deep"]
    if not llm.agent_enabled("analyze"):
        raise HTTPException(503, "تحلیل هوش مصنوعی غیرفعال است")
    try:
        d, used = await asyncio.to_thread(ai.deep_analyze, sid)
    except Exception as e:
        raise HTTPException(502, f"خطای مدل زبانی: {str(e)[:200]}")
    d["tokens"] = used
    db.merge_json("stories", "id", sid, "ai", {"deep": d})
    db.audit("user", "ai:analyze", sid, {"tokens": used})
    return d


@app.post("/api/briefs/custom")
async def brief_custom(body: dict = Body(...)):
    topic, entity, hours = body.get("topic") or None, body.get("entity") or None, int(body.get("hours") or 24)
    if not llm.agent_enabled("analyze"):
        raise HTTPException(503, "تحلیل هوش مصنوعی غیرفعال است")

    def build():
        stories, refs, lines = ai.custom_brief(topic, entity, hours)
        if not stories:
            return None
        d, used = ai.write_brief(refs, "\n".join(lines), "-", agent="analyze")
        n = len(refs)
        scope = " · ".join(x for x in ((TOPICS.get(topic, {}).get("fa") if topic else None), ((ent(entity) or {}).get("name_fa") if entity else None)) if x) or "همه موضوع‌ها"
        title = f"بریف موضوعی — {scope} — {hours} ساعت اخیر — {time.strftime('%Y-%m-%d %H:%M')}"
        md = [f"# {title}", ""]
        for key, head in (("bluf", "جمع‌بندی (BLUF)"), ("executive_summary", "خلاصه مدیریتی")):
            t, _ = ai.citation_guard(d.get(key, ""), n)
            if t:
                md += [f"## {head}", t, ""]
        sec = []
        for a in (d.get("analysis") or [])[:6]:
            t, _ = ai.citation_guard(a.get("text", ""), n)
            if t:
                sec += [f"### {a.get('heading', '')}", t, ""]
        if sec:
            md += ["## تحلیل", *sec]
        if d.get("outlook"):
            md += ["## چشم‌انداز", d["outlook"], ""]
        md += ["## داستان‌های مبنا", *lines, "", "## منابع"]
        md += [f"{i+1}. [{r['title']}]({r['url']}) — {r['sname']}" for i, r in enumerate(refs)]
        with db.tx() as c:
            cur = c.execute("INSERT INTO briefs(created,title,markdown,status,run_id,stats) VALUES(?,?,?,?,?,?)",
                            (time.time(), title, "\n".join(md), "draft", "custom",
                             db.jdump({"stories": len(stories), "refs": n, "ai": True, "custom": {"topic": topic, "entity": entity, "hours": hours}, "tokens": used,
                                       "refs_list": [{"sname": r["sname"], "title": r["title"]} for r in refs]})))
        return cur.lastrowid

    try:
        bid = await asyncio.to_thread(build)
    except Exception as e:
        raise HTTPException(502, f"خطای مدل زبانی: {str(e)[:200]}")
    if not bid:
        raise HTTPException(404, "داستانی با این فیلتر یافت نشد")
    db.audit("user", "ai:custom_brief", str(bid), body)
    bus.publish({"type": "data_changed"})
    return {"id": bid}


@app.post("/api/articles/{aid}/translate")
async def article_translate(aid: str):
    a = db.q1("SELECT id,title,lead,text,lang,title_fa,lead_fa FROM articles WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404)
    if a["lead_fa"]:
        return {"title_fa": a["title_fa"], "lead_fa": a["lead_fa"]}
    if not llm.agent_enabled("translator"):
        raise HTTPException(503, "مترجم غیرفعال است")

    def run():
        d, _ = llm.chat_json('عنوان و متن خبر را به فارسی معیار ترجمه کن؛ متن را حداکثر در ۱۲۰ کلمه خلاصه-ترجمه کن. {"title_fa":"","lead_fa":""}',
                             llm.wrap(f"TITLE: {a['title']}\nTEXT: {(a['text'] or a['lead'] or '')[:3500]}", 4000), 900, "translator", "article_on_demand")
        with db.tx() as c:
            c.execute("UPDATE articles SET title_fa=COALESCE(title_fa,?),lead_fa=?,translated_by=? WHERE id=?",
                      (d.get("title_fa"), d.get("lead_fa"), "llm:" + llm.cfg()["model"], aid))
        return d
    try:
        return await asyncio.to_thread(run)
    except Exception as e:
        raise HTTPException(502, str(e)[:200])


# ---------------- runs / pipeline control ----------------
@app.get("/api/state")
def state():
    return sup_mod.get().state()


@app.get("/api/runs")
def runs(limit: int = 30):
    rows = db.q("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,))
    for r in rows:
        r["summary"] = db.jload(r["summary"], {})
    return rows


@app.get("/api/runs/{rid}")
def run(rid: str):
    r = db.q1("SELECT * FROM runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404)
    r["summary"] = db.jload(r["summary"], {})
    r["steps"] = [dict(s, output=db.jload(s["output"], s["output"])) for s in db.q("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (rid,))]
    return r


@app.post("/api/control")
async def control(body: dict = Body(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    action = body.pop("action", None)
    return await sup_mod.get().control(action, **body)


@app.post("/api/steps/{step_id}/decision")
async def decision(step_id: str, body: dict = Body(...), x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    return sup_mod.get().decide(step_id, body.get("decision", "approve"), body.get("edited_output"))


@app.post("/api/rerun/{agent}")
async def rerun(agent: str, x_admin_token: str = Header(None)):
    _auth(x_admin_token)
    return await sup_mod.get().rerun(agent)


@app.get("/api/audit")
def audit(limit: int = 100):
    return db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/events")
async def events(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'state', 'state': sup_mod.get().state()}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": str(settings.db_path), "state": sup_mod.get().status}


# ---------------- auth & users ----------------
def _client_ip(request: Request):
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()


def _set_session(resp, tok):
    resp.set_cookie(auth.COOKIE, tok, max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")


@app.get("/api/auth/status")
def auth_status():
    u = auth.current_user.get()
    real = u if (u and u.get("id")) or not auth.enabled() or (u and u is auth.SYSTEM_ADMIN) else None
    return {"auth_enabled": auth.enabled(), "setup_required": auth.enabled() and auth.user_count() == 0, "user": real,
            "roles": auth.ROLES_FA}


@app.post("/api/auth/setup")
def auth_setup(request: Request, body: dict = Body(...)):
    if auth.user_count() > 0:
        raise HTTPException(409, "راه‌اندازی اولیه قبلاً انجام شده است")
    try:
        auth.create_user(body.get("username"), body.get("password"), "admin", body.get("name"))
        tok, u = auth.login(body.get("username"), body.get("password"), request.headers.get("user-agent", ""), _client_ip(request))
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    db.audit(u["username"], "auth:setup", u["username"])
    resp = JSONResponse({"user": u})
    _set_session(resp, tok)
    return resp


@app.post("/api/auth/login")
def auth_login(request: Request, body: dict = Body(...)):
    try:
        tok, u = auth.login(body.get("username"), body.get("password"), request.headers.get("user-agent", ""), _client_ip(request))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    db.audit(u["username"], "auth:login", _client_ip(request))
    resp = JSONResponse({"user": u, "token": tok if body.get("want_token") else None})
    _set_session(resp, tok)
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/auth/password")
def auth_password(request: Request, body: dict = Body(...)):
    u = auth.user_from_token(request.cookies.get(auth.COOKIE))
    if not u:
        raise HTTPException(401, "ورود لازم است")
    row = db.q1("SELECT pw_hash FROM users WHERE id=?", (u["id"],))
    if not auth.verify_pw(body.get("old") or "", row["pw_hash"]):
        raise HTTPException(400, "رمز فعلی نادرست است")
    try:
        auth.update_user(u["id"], {"password": body.get("new")})
        tok, _ = auth.login(u["username"], body.get("new"), request.headers.get("user-agent", ""), _client_ip(request))
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    db.audit(u["username"], "auth:password", u["username"])
    resp = JSONResponse({"ok": True})
    _set_session(resp, tok)
    return resp


@app.get("/api/users")
def users_list():
    return [auth.public(u) for u in db.q("SELECT * FROM users ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'analyst' THEN 1 ELSE 2 END, username")]


@app.post("/api/users")
def users_create(body: dict = Body(...)):
    try:
        uid = auth.create_user(body.get("username"), body.get("password"), body.get("role", "viewer"), body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "user:create", body.get("username"), {"role": body.get("role")})
    return auth.public(db.q1("SELECT * FROM users WHERE id=?", (uid,)))


@app.post("/api/users/{uid}")
def users_update(uid: int, body: dict = Body(...)):
    try:
        u = auth.update_user(uid, body)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "user:update", u["username"], {k: v for k, v in body.items() if k != "password"} | ({"password": "***"} if body.get("password") else {}))
    return u


@app.delete("/api/users/{uid}")
def users_delete(uid: int):
    me = auth.current_user.get()
    if me and me.get("id") == uid:
        raise HTTPException(400, "حذف حساب خودتان مجاز نیست")
    try:
        auth.delete_user(uid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "user:delete", str(uid))
    return {"ok": True}


# ---------------- sources management ----------------
@app.post("/api/sources/validate")
async def sources_validate(body: dict = Body(...)):
    return await asyncio.to_thread(ops.validate_feed, body.get("url", ""), body.get("kind", "rss"), body.get("query"))


@app.get("/api/sources/export.opml")
def sources_opml():
    return Response(ops.opml_export(), media_type="text/x-opml; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="rasad-sources.opml"'})


@app.post("/api/sources/import")
async def sources_import(body: dict = Body(...)):
    try:
        r = await asyncio.to_thread(ops.opml_import, body.get("opml", ""), body.get("lang"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "source:import", "", {"added": len(r["added"]), "skipped": r["skipped"]})
    return r


@app.post("/api/sources")
async def sources_create(body: dict = Body(...)):
    try:
        r = await asyncio.to_thread(ops.save_source, body, None, body.get("check", True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "source:create", r["id"], {"url": body.get("url"), "status": r["source"]["status"]})
    bus.publish({"type": "data_changed"})
    return r


@app.post("/api/sources/{sid}")
async def sources_edit(sid: str, body: dict = Body(...)):
    try:
        r = await asyncio.to_thread(ops.save_source, body, sid, False)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "source:edit", sid, {k: v for k, v in body.items()})
    return r


@app.delete("/api/sources/{sid}")
def sources_delete(sid: str):
    r = ops.delete_source(sid)
    db.audit("user", "source:delete", sid, r)
    return r


# ---------------- notifications ----------------
@app.get("/api/notify/channels")
def notify_channels():
    return {"channels": notify.list_channels(), "types": notify.TYPES, "severities": notify.SEV_FA}


@app.post("/api/notify/channels")
def notify_create(body: dict = Body(...)):
    try:
        ch = notify.save_channel(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "notify:create", str(ch["id"]), {"type": ch["type"], "name": ch["name"]})
    return ch


@app.post("/api/notify/channels/{cid}")
def notify_update(cid: int, body: dict = Body(...)):
    try:
        ch = notify.save_channel(body, cid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "notify:update", str(cid), {"name": ch["name"], "enabled": ch["enabled"]})
    return ch


@app.delete("/api/notify/channels/{cid}")
def notify_delete(cid: int):
    notify.delete_channel(cid)
    db.audit("user", "notify:delete", str(cid))
    return {"ok": True}


@app.post("/api/notify/channels/{cid}/test")
async def notify_test(cid: int):
    try:
        return await asyncio.to_thread(notify.test_channel, cid)
    except KeyError:
        raise HTTPException(404)


@app.get("/api/notify/deliveries")
def notify_deliveries(limit: int = 100):
    return notify.deliveries(limit)


# ---------------- watchlists ----------------
@app.get("/api/watchlists")
def watch_list():
    out = watch.list_all()
    since = H(24)
    for w in out:
        w["hits_24h"] = len(watch.hits(w, since, limit=500))
    return out


@app.post("/api/watchlists")
def watch_create(body: dict = Body(...)):
    try:
        w = watch.save(body, owner=auth.actor())
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "watch:create", str(w["id"]), {"name": w["name"]})
    return w


@app.post("/api/watchlists/{wid}")
def watch_update(wid: int, body: dict = Body(...)):
    try:
        w = watch.save(body, wid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user", "watch:update", str(wid), {"name": w["name"]})
    return w


@app.delete("/api/watchlists/{wid}")
def watch_delete(wid: int):
    watch.delete(wid)
    db.audit("user", "watch:delete", str(wid))
    return {"ok": True}


@app.get("/api/watchlists/{wid}/hits")
def watch_hits(wid: int, hours: int = 48, limit: int = 100):
    w = db.q1("SELECT * FROM watchlists WHERE id=?", (wid,))
    if not w:
        raise HTTPException(404)
    return watch.hits(w, H(hours), limit)


# ---------------- system / ops ----------------
@app.get("/api/system/info")
def system_info():
    return ops.info()


@app.post("/api/system/backup")
async def system_backup():
    r = await asyncio.to_thread(ops.backup, "manual")
    db.audit("user", "system:backup", r["file"])
    return r


@app.get("/api/system/backups")
def system_backups():
    return ops.list_backups()


@app.get("/api/system/backups/{name}")
def system_backup_download(name: str):
    try:
        p = ops.backup_path(name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404)
    return FileResponse(p, filename=name, media_type="application/octet-stream")


@app.post("/api/system/retention")
def system_retention(body: dict = Body(...)):
    d = int(body.get("days") or 0)
    if not 3 <= d <= 3650:
        raise HTTPException(400, "بازه نگهداری باید بین ۳ تا ۳۶۵۰ روز باشد")
    db.kv_set("retention_days", d)
    db.audit("user", "system:retention", str(d))
    return {"retention_days": d}


@app.post("/api/system/purge")
async def system_purge(body: dict = Body(default={})):
    r = await asyncio.to_thread(ops.purge, int(body.get("days") or 0) or None)
    db.audit("user", "system:purge", "", r)
    bus.publish({"type": "data_changed"})
    return r


# ---------------- v0.4: alert triage ----------------
@app.get("/api/alerts")
def alerts_list(hours: int = 168, status: str = None, severity: str = None, type: str = None, assignee: str = None, q: str = None):
    return triage.listing(hours, status, severity, type, assignee, q)


@app.get("/api/alerts/{gid}")
def alerts_detail(gid: int):
    try:
        return triage.detail(gid)
    except KeyError:
        raise HTTPException(404)


@app.post("/api/alerts/bulk")
def alerts_bulk(body: dict = Body(...)):
    if body.get("status") not in triage.STATUSES:
        raise HTTPException(400, "وضعیت نامعتبر")
    n = triage.bulk(body.get("ids") or [], body["status"])
    bus.publish({"type": "alerts_changed"})
    return {"updated": n}


@app.post("/api/alerts/{gid}")
def alerts_update(gid: int, body: dict = Body(...)):
    try:
        r = triage.update(gid, body)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    bus.publish({"type": "alerts_changed"})
    return r


@app.get("/api/people")
def people():
    """Minimal directory (for assignment pickers) — available to every signed-in role."""
    return db.q("SELECT username, name, role FROM users WHERE active=1 ORDER BY name")


# ---------------- v0.4: personal API tokens ----------------
def _me():
    u = auth.current_user.get()
    if not u or not u.get("id"):
        raise HTTPException(400, "توکن API فقط برای حساب‌های کاربری واقعی قابل ساخت است")
    return u


@app.get("/api/me/tokens")
def my_tokens():
    return auth.list_api_tokens(_me()["id"])


@app.post("/api/me/tokens")
def my_token_create(body: dict = Body(...)):
    u = _me()
    days = body.get("days")
    if days is not None and not 1 <= int(days) <= 3650:
        raise HTTPException(400, "مدت اعتبار نامعتبر است")
    tok, tid = auth.create_api_token(u["id"], body.get("name"), days)
    db.audit(u["username"], "token:create", str(tid), {"name": body.get("name"), "days": days})
    return {"id": tid, "token": tok}


@app.delete("/api/me/tokens/{tid}")
def my_token_revoke(tid: int):
    u = _me()
    if not auth.revoke_api_token(u["id"], tid):
        raise HTTPException(404)
    db.audit(u["username"], "token:revoke", str(tid))
    return {"ok": True}


# ---------------- v0.4: explore (advanced search) ----------------
@app.get("/api/explore")
def explore_api(request: Request):
    try:
        return explore.search(dict(request.query_params))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/explore.csv")
def explore_csv(request: Request):
    try:
        return _csv_resp(explore.csv_export(dict(request.query_params)), "rasad-search.csv")
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- v0.4: scheduled digest ----------------
@app.get("/api/notify/digest")
def digest_get():
    text, stats = notify.digest_text(notify.digest_cfg()["hours"], notify.digest_cfg()["top"])
    return {"config": notify.digest_cfg(), "last": db.kv_get("last_digest"), "preview": text, "stats": stats, "tz": settings.tz}


@app.post("/api/notify/digest")
def digest_set(body: dict = Body(...)):
    try:
        cfg = notify.set_digest_cfg(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    db.audit("user", "notify:digest", "", cfg)
    return cfg


@app.post("/api/notify/digest/send")
async def digest_send():
    r = await asyncio.to_thread(notify.dispatch_digest, True)
    db.audit("user", "notify:digest_send", "", {"sent": r["sent"]})
    return r


@app.get("/metrics", response_class=PlainTextResponse)
def prom_metrics():
    return PlainTextResponse(ops.metrics(sup_mod.get()), media_type="text/plain; version=0.0.4")


# ---------------- exports ----------------
def _csv_resp(text, name):
    return Response(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export/stories.csv")
def export_stories(hours: int = 168):
    return _csv_resp(ops.stories_csv(hours), "rasad-stories.csv")


@app.get("/api/export/articles.csv")
def export_articles(hours: int = 48):
    return _csv_resp(ops.articles_csv(hours), "rasad-articles.csv")


@app.get("/api/graph/export.gexf")
def export_gexf(hours: int = 72, min_weight: int = 2):
    return Response(ops.gexf(graph(hours=hours, min_weight=min_weight, limit=2000)), media_type="application/gexf+xml",
                    headers={"Content-Disposition": 'attachment; filename="rasad-graph.gexf"'})


app.mount("/static", StaticFiles(directory=str(settings.web_dir)), name="static")


@app.get("/")
def index():
    return FileResponse(settings.web_dir / "index.html")
