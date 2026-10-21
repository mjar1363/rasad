"""Operations: backups, retention, Prometheus metrics, system info, exports (CSV / GEXF / OPML) and source management."""
import csv
import io
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xesc

from . import db
from .config import settings

START = time.time()
VERSION = "0.5.0"


# ---------------- backups ----------------
def backup(tag="manual"):
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"rasad-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.db"
    dst = settings.backup_dir / name
    src = db.connect()
    with db._write_lock:
        out = sqlite3.connect(dst)
        src.backup(out)
        out.close()
    prune_backups()
    db.kv_set("last_backup", {"t": time.time(), "file": name, "size": dst.stat().st_size})
    return {"file": name, "size": dst.stat().st_size}


def list_backups():
    if not settings.backup_dir.exists():
        return []
    return [{"file": p.name, "size": p.stat().st_size, "t": p.stat().st_mtime}
            for p in sorted(settings.backup_dir.glob("rasad-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)]


def prune_backups():
    for b in list_backups()[settings.backup_keep:]:
        (settings.backup_dir / b["file"]).unlink(missing_ok=True)


def backup_path(name):
    if not re.fullmatch(r"rasad-[\w\-]+\.db", name or ""):
        raise ValueError("bad name")
    p = settings.backup_dir / name
    if not p.exists():
        raise FileNotFoundError(name)
    return p


# ---------------- retention ----------------
def retention_days():
    return int(db.kv_get("retention_days", settings.retention_days) or settings.retention_days)


def purge(days=None):
    days = days or retention_days()
    cut = time.time() - days * 86400
    with db.tx() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM articles WHERE published_at<?", (cut,))]
        for k in range(0, len(ids), 500):
            ch = ids[k:k + 500]
            ph = ",".join("?" * len(ch))
            c.execute(f"DELETE FROM mentions WHERE article_id IN ({ph})", ch)
            c.execute(f"DELETE FROM minhash WHERE article_id IN ({ph})", ch)
            c.execute(f"DELETE FROM articles_fts WHERE id IN ({ph})", ch)
            c.execute(f"DELETE FROM articles WHERE id IN ({ph})", ch)
        st = c.execute("DELETE FROM stories WHERE last_update<? AND id NOT IN (SELECT DISTINCT story_id FROM articles WHERE story_id IS NOT NULL)", (cut,)).rowcount
        c.execute("DELETE FROM storyline_events WHERE story_id NOT IN (SELECT id FROM stories)")
        c.execute("DELETE FROM claims WHERE story_id NOT IN (SELECT id FROM stories)")
        c.execute("DELETE FROM relations WHERE last_seen<?", (cut,))
        c.execute("DELETE FROM llm_calls WHERE ts<?", (cut,))
        c.execute("DELETE FROM signals WHERE created<?", (cut,))
    db.kv_set("last_purge", {"t": time.time(), "articles": len(ids), "stories": st, "days": days})
    return {"articles": len(ids), "stories": st, "days": days}


def daily_maintenance():
    """Called after each pipeline run; does backup + purge at most once per 24h."""
    last = (db.kv_get("last_maintenance") or {}).get("t", 0)
    if time.time() - last < 86400:
        return None
    db.kv_set("last_maintenance", {"t": time.time()})
    res = {"purge": purge(), "backup": backup("auto")}
    return res


# ---------------- info & metrics ----------------
def _count(sql, args=()):
    return db.q1(sql, args)["n"]


def info():
    size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    wal = settings.db_path.with_suffix(".db-wal")
    return {"version": VERSION, "uptime_s": int(time.time() - START), "db_path": str(settings.db_path), "db_size": size,
            "wal_size": wal.stat().st_size if wal.exists() else 0,
            "counts": {t: _count(f"SELECT COUNT(*) n FROM {t}") for t in ("articles", "stories", "entities", "relations", "signals", "briefs", "sources", "runs", "llm_calls")},
            "retention_days": retention_days(), "backup_keep": settings.backup_keep, "last_backup": db.kv_get("last_backup"),
            "last_purge": db.kv_get("last_purge"), "auth": settings.auth_enabled, "public_url": settings.public_url}


def metrics(sup=None):
    now = time.time()
    lines = []

    def g(name, val, help_, labels=None, typ="gauge"):
        if not any(ln.startswith(f"# HELP {name} ") for ln in lines):
            lines.extend([f"# HELP {name} {help_}", f"# TYPE {name} {typ}"])
        lab = "{" + ",".join(f'{k}="{v}"' for k, v in (labels or {}).items()) + "}" if labels else ""
        lines.append(f"{name}{lab} {val}")
    g("rasad_up", 1, "Rasad process is up")
    g("rasad_uptime_seconds", int(now - START), "Process uptime")
    for t in ("articles", "stories", "entities", "relations", "signals", "briefs"):
        g("rasad_rows", _count(f"SELECT COUNT(*) n FROM {t}"), "Row counts per table", {"table": t})
    g("rasad_articles_last_hour", _count("SELECT COUNT(*) n FROM articles WHERE fetched_at>?", (now - 3600,)), "Articles fetched in the last hour")
    for st in ("active", "pending", "quarantine", "disabled"):
        g("rasad_sources", _count("SELECT COUNT(*) n FROM sources WHERE status=?", (st,)), "Sources by status", {"status": st})
    for sev in ("info", "warning", "critical"):
        g("rasad_signals_24h", _count("SELECT COUNT(*) n FROM signals WHERE created>? AND severity=?", (now - 86400, sev)), "Signals created in 24h", {"severity": sev})
    r = db.q1("SELECT status, started, ended, cost_tokens FROM runs ORDER BY started DESC LIMIT 1")
    if r:
        g("rasad_last_run_timestamp", int(r["started"]), "Start time of the last pipeline run")
        if r["ended"]:
            g("rasad_last_run_duration_seconds", round(r["ended"] - r["started"], 1), "Duration of the last run")
        g("rasad_last_run_tokens", r["cost_tokens"] or 0, "LLM tokens used by the last run")
    for s in ("succeeded", "failed", "killed", "partial"):
        g("rasad_runs_24h", _count("SELECT COUNT(*) n FROM runs WHERE started>? AND status=?", (now - 86400, s)), "Runs in 24h by status", {"status": s})
    t0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    for row in db.q("SELECT agent, COUNT(*) calls, COALESCE(SUM(total_tokens),0) tok, SUM(1-ok) err FROM llm_calls WHERE ts>? GROUP BY agent", (t0,)):
        g("rasad_llm_tokens_today", row["tok"], "LLM tokens today per agent", {"agent": row["agent"]})
        g("rasad_llm_calls_today", row["calls"], "LLM calls today per agent", {"agent": row["agent"]})
        g("rasad_llm_errors_today", row["err"] or 0, "LLM failed calls today per agent", {"agent": row["agent"]})
    fr = db.q1("SELECT AVG(fetched_at-published_at) v FROM articles WHERE fetched_at>? AND published_at<=fetched_at", (now - 6 * 3600,))
    if fr and fr["v"] is not None:
        g("rasad_freshness_seconds_avg", round(fr["v"], 1), "Average fetch lag over 6h")
    if sup is not None:
        g("rasad_killed", 1 if sup.killed else 0, "Kill switch engaged")
        g("rasad_mode", 1, "Execution mode", {"mode": sup.mode})
    return "\n".join(lines) + "\n"


# ---------------- exports ----------------
def _csv(rows, cols):
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel-friendly UTF-8 BOM (Persian text)
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    return buf.getvalue()


def stories_csv(hours=168):
    rows = db.q("""SELECT id,COALESCE(title_fa,title) title,category,urgency,article_count,source_count,lang_count,score,
                   datetime(first_seen,'unixepoch') first_seen,datetime(last_update,'unixepoch') last_update,
                   json_extract(verification,'$.label') verification FROM stories WHERE last_update>? ORDER BY score DESC""", (time.time() - hours * 3600,))
    return _csv(rows, ["id", "title", "category", "urgency", "article_count", "source_count", "lang_count", "score", "first_seen", "last_update", "verification"])


def articles_csv(hours=48):
    rows = db.q("""SELECT a.id,s.name source,a.lang,a.title,a.title_fa,a.category,a.urgency,a.sentiment,a.story_id,a.url,
                   datetime(a.published_at,'unixepoch') published_at FROM articles a JOIN sources s ON s.id=a.source_id
                   WHERE a.published_at>? AND a.duplicate_of IS NULL ORDER BY a.published_at DESC""", (time.time() - hours * 3600,))
    return _csv(rows, ["id", "source", "lang", "title", "title_fa", "category", "urgency", "sentiment", "story_id", "url", "published_at"])


def gexf(graph):
    """GEXF 1.3 for Gephi / Cytoscape from /api/graph payload."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<gexf xmlns="http://gexf.net/1.3" version="1.3">',
           f'<meta lastmodifieddate="{time.strftime("%Y-%m-%d")}"><creator>Rasad {VERSION}</creator></meta>',
           '<graph mode="static" defaultedgetype="undirected">',
           '<attributes class="node"><attribute id="type" title="type" type="string"/><attribute id="mentions" title="mentions" type="integer"/>'
           '<attribute id="name_en" title="name_en" type="string"/></attributes>',
           '<attributes class="edge"><attribute id="predicate" title="predicate" type="string"/></attributes><nodes>']
    for n in graph["nodes"]:
        out.append(f'<node id="{xesc(n["id"])}" label="{xesc(n.get("name_fa") or n["id"])}"><attvalues>'
                   f'<attvalue for="type" value="{xesc(n.get("type") or "")}"/><attvalue for="mentions" value="{int(n.get("mentions") or 0)}"/>'
                   f'<attvalue for="name_en" value="{xesc(n.get("name_en") or "")}"/></attvalues></node>')
    out.append("</nodes><edges>")
    for i, l in enumerate(graph["links"]):
        typed = l.get("predicate") and l["predicate"] != "co_mentioned"
        out.append(f'<edge id="e{i}" source="{xesc(l["source"])}" target="{xesc(l["target"])}" weight="{l.get("weight") or 1}"'
                   f'{" type=\"directed\"" if typed else ""} label="{xesc(l.get("predicate_fa") or "")}"><attvalues>'
                   f'<attvalue for="predicate" value="{xesc(l.get("predicate") or "")}"/></attvalues></edge>')
    out.append("</edges></graph></gexf>")
    return "\n".join(out)


# ---------------- sources ----------------
SRC_FIELDS = ("name", "url", "homepage", "lang", "country", "type", "ownership_group", "credibility", "bias")
_slug = lambda s: re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]  # noqa: E731


def validate_feed(url, kind="rss", query=None):
    import feedparser
    import httpx
    if kind == "gdelt":
        return {"ok": True, "entries": None, "title": "GDELT"}
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": settings.user_agent})
        d = feedparser.parse(r.content)
        return {"ok": r.status_code < 400 and len(d.entries) > 0, "status": r.status_code, "entries": len(d.entries),
                "title": (d.feed.get("title") or "")[:120], "sample": [e.get("title", "")[:120] for e in d.entries[:3]]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def save_source(body, sid=None, check=True):
    url = (body.get("url") or "").strip()
    if not sid and not url.startswith(("http://", "https://")):
        raise ValueError("آدرس فید معتبر نیست")
    if body.get("lang") and body["lang"] not in ("fa", "ar", "en", "other"):
        raise ValueError("زبان باید fa/ar/en/other باشد")
    cred = body.get("credibility")
    if cred is not None and not (0 <= float(cred) <= 1):
        raise ValueError("اعتبار باید بین ۰ و ۱ باشد")
    kind = body.get("kind") or "rss"
    val = validate_feed(url, kind, body.get("query")) if (check and url) else None
    if sid:
        old = db.q1("SELECT * FROM sources WHERE id=?", (sid,))
        if not old:
            raise KeyError(sid)
        sets = [(k, body[k]) for k in SRC_FIELDS if k in body]
        meta = db.jload(old["meta"], {}) or {}
        if "topics" in body:
            sets.append(("topics", db.jdump(body["topics"] or [])))
        if "query" in body:
            meta["query"] = body["query"]
        meta["edited"] = time.time()
        sets.append(("meta", db.jdump(meta)))
        with db.tx() as c:
            c.execute(f"UPDATE sources SET {','.join(k + '=?' for k, _ in sets)} WHERE id=?", (*[v for _, v in sets], sid))
    else:
        sid = _slug(body.get("id") or body.get("name") or url.split("//")[-1]) or f"src-{int(time.time())}"
        base = sid
        k = 2
        while db.q1("SELECT 1 FROM sources WHERE id=?", (sid,)):
            sid = f"{base}-{k}"; k += 1
        status = "active" if (body.get("activate") and (val is None or val.get("ok"))) else "pending"
        meta = {"kind": kind, "query": body.get("query"), "origin": "ui", "validation": val}
        with db.tx() as c:
            c.execute("""INSERT INTO sources(id,name,url,homepage,lang,country,type,ownership_group,credibility,bias,topics,status,meta)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sid, body.get("name") or (val or {}).get("title") or sid, url, body.get("homepage"), body.get("lang") or "other",
                       body.get("country"), body.get("type") or "news", body.get("ownership_group") or sid, float(cred if cred is not None else 0.6),
                       body.get("bias"), db.jdump(body.get("topics") or []), status, db.jdump(meta)))
    return {"id": sid, "validation": val, "source": db.q1("SELECT id,name,url,lang,status,credibility FROM sources WHERE id=?", (sid,))}


def delete_source(sid):
    with db.tx() as c:
        c.execute("UPDATE sources SET status='disabled' WHERE id=?", (sid,))
        n = c.execute("SELECT COUNT(*) FROM articles WHERE source_id=?", (sid,)).fetchone()[0]
        if n == 0:
            c.execute("DELETE FROM sources WHERE id=?", (sid,))
    return {"deleted": n == 0, "disabled": n > 0}


def opml_export():
    rows = db.q("SELECT id,name,url,homepage,lang,status FROM sources WHERE COALESCE(json_extract(meta,'$.kind'),'rss')='rss' ORDER BY lang,name")
    body = "".join(f'<outline type="rss" text="{xesc(r["name"] or r["id"])}" title="{xesc(r["name"] or r["id"])}" xmlUrl="{xesc(r["url"])}"'
                   f' htmlUrl="{xesc(r["homepage"] or "")}" language="{xesc(r["lang"] or "")}"/>\n' for r in rows)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0"><head><title>Rasad sources</title></head><body>\n{body}</body></opml>\n'


def opml_import(text, lang=None):
    try:
        root = ET.fromstring(text.encode() if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise ValueError(f"OPML نامعتبر: {e}")
    added, skipped = [], 0
    for o in root.iter("outline"):
        url = o.get("xmlUrl")
        if not url:
            continue
        if db.q1("SELECT 1 FROM sources WHERE url=?", (url,)):
            skipped += 1
            continue
        r = save_source({"name": o.get("title") or o.get("text"), "url": url, "homepage": o.get("htmlUrl"),
                         "lang": o.get("language") if o.get("language") in ("fa", "ar", "en") else (lang or "other")}, check=False)
        added.append(r["id"])
    return {"added": added, "skipped": skipped}
