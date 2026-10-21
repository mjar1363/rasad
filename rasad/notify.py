"""Alert & brief delivery: Telegram, generic webhook (JSON / Slack / Mattermost-compatible) and e-mail (SMTP).

Channels carry routing rules (minimum severity, signal types, topics). Each (channel, ref) pair is delivered at most
once, so re-runs never spam. Secrets (bot tokens, SMTP passwords, webhook URLs) are write-only through the API.
"""
import json
import smtplib
import threading
import time
from email.message import EmailMessage

import httpx

from . import db
from .config import settings

TYPES = {"telegram": "تلگرام", "webhook": "وب‌هوک (JSON/Slack)", "email": "ایمیل (SMTP)"}
SECRET_KEYS = {"bot_token", "password", "url", "header_value"}
SEV = {"info": 0, "warning": 1, "critical": 2}
SEV_FA = {"info": "اطلاع", "warning": "هشدار", "critical": "بحرانی"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, type TEXT, config TEXT, enabled INTEGER DEFAULT 1,
  min_severity TEXT DEFAULT 'warning', signal_types TEXT, briefs INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel_id INTEGER, ref TEXT, title TEXT, ok INTEGER, error TEXT,
  UNIQUE(channel_id, ref));
"""
_lock = threading.Lock()


def init():
    with db.tx() as c:
        c.executescript(SCHEMA)


def _mask(v):
    v = str(v or "")
    return ("•••" + v[-4:]) if len(v) > 4 else ("•••" if v else "")


def public(ch):
    cfg = db.jload(ch["config"], {}) or {}
    return {"id": ch["id"], "name": ch["name"], "type": ch["type"], "type_fa": TYPES.get(ch["type"], ch["type"]),
            "enabled": bool(ch["enabled"]), "min_severity": ch["min_severity"], "signal_types": db.jload(ch["signal_types"], []) or [],
            "briefs": bool(ch["briefs"]), "created": ch["created"],
            "config": {k: (_mask(v) if k in SECRET_KEYS else v) for k, v in cfg.items()},
            "secrets_set": sorted(k for k in SECRET_KEYS if cfg.get(k))}


def list_channels():
    return [public(c) for c in db.q("SELECT * FROM channels ORDER BY id")]


def _validate(typ, cfg):
    if typ not in TYPES:
        raise ValueError("نوع کانال نامعتبر است")
    need = {"telegram": ("bot_token", "chat_id"), "webhook": ("url",), "email": ("host", "to", "sender")}[typ]
    miss = [k for k in need if not cfg.get(k)]
    if miss:
        raise ValueError("فیلدهای لازم: " + "، ".join(miss))
    if typ == "webhook" and not str(cfg["url"]).startswith(("http://", "https://")):
        raise ValueError("آدرس وب‌هوک باید با http(s) شروع شود")


def save_channel(body, cid=None):
    old = db.q1("SELECT * FROM channels WHERE id=?", (cid,)) if cid else None
    if cid and not old:
        raise KeyError(cid)
    cfg = (db.jload(old["config"], {}) or {}) if old else {}
    for k, v in (body.get("config") or {}).items():
        if k in SECRET_KEYS and (v is None or v == "" or str(v).startswith("•••")):
            continue  # keep stored secret
        cfg[k] = v
    typ = body.get("type") or (old and old["type"])
    _validate(typ, cfg)
    row = (body.get("name") or (old and old["name"]) or TYPES[typ], typ, db.jdump(cfg),
           1 if body.get("enabled", old["enabled"] if old else True) else 0,
           body.get("min_severity") or (old and old["min_severity"]) or "warning",
           db.jdump(body.get("signal_types", db.jload(old["signal_types"], []) if old else [])),
           1 if body.get("briefs", old["briefs"] if old else True) else 0)
    if row[4] not in SEV:
        raise ValueError("سطح نامعتبر")
    with db.tx() as c:
        if old:
            c.execute("UPDATE channels SET name=?,type=?,config=?,enabled=?,min_severity=?,signal_types=?,briefs=? WHERE id=?", (*row, cid))
        else:
            cid = c.execute("INSERT INTO channels(name,type,config,enabled,min_severity,signal_types,briefs,created) VALUES(?,?,?,?,?,?,?,?)",
                            (*row, time.time())).lastrowid
    return public(db.q1("SELECT * FROM channels WHERE id=?", (cid,)))


def delete_channel(cid):
    with db.tx() as c:
        c.execute("DELETE FROM channels WHERE id=?", (cid,))


# ---------------- senders ----------------
def _send(ch, title, text, link=None, payload=None):
    cfg = db.jload(ch["config"], {}) or {}
    if ch["type"] == "telegram":
        body = f"<b>{_h(title)}</b>\n{_h(text)}" + (f'\n<a href="{_h(link)}">مشاهده در رصد</a>' if link else "")
        r = httpx.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage", timeout=15,
                       json={"chat_id": cfg["chat_id"], "text": body[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:160]}")
    elif ch["type"] == "webhook":
        data = {"text": f"*{title}*\n{text}" + (f"\n{link}" if link else ""), "title": title, "body": text, "link": link,
                "source": "rasad", **(payload or {})}
        hdr = {"Content-Type": "application/json", "User-Agent": settings.user_agent}
        if cfg.get("header_name") and cfg.get("header_value"):
            hdr[cfg["header_name"]] = cfg["header_value"]
        r = httpx.post(cfg["url"], content=json.dumps(data, ensure_ascii=False).encode(), headers=hdr, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"Webhook HTTP {r.status_code}: {r.text[:160]}")
    elif ch["type"] == "email":
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = f"[رصد] {title}", cfg["sender"], cfg["to"]
        m.set_content(text + (f"\n\n{link}" if link else ""))
        port = int(cfg.get("port") or 587)
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(cfg["host"], port, timeout=20) as s:
            if port != 465 and cfg.get("starttls", True):
                s.starttls()
            if cfg.get("user"):
                s.login(cfg["user"], cfg.get("password", ""))
            s.send_message(m)


def _h(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _deliver(ch, ref, title, text, link=None, payload=None, force=False):
    with _lock:
        if not force and db.q1("SELECT 1 FROM deliveries WHERE channel_id=? AND ref=? AND ok=1", (ch["id"], ref)):
            return None
        try:
            _send(ch, title, text, link, payload)
            ok, err = 1, None
        except Exception as e:  # noqa: BLE001
            ok, err = 0, "delivery failed: " + type(e).__name__
        with db.tx() as c:
            c.execute("""INSERT INTO deliveries(ts,channel_id,ref,title,ok,error) VALUES(?,?,?,?,?,?)
                         ON CONFLICT(channel_id,ref) DO UPDATE SET ts=excluded.ts,ok=excluded.ok,error=excluded.error""",
                      (time.time(), ch["id"], ref, title[:200], ok, err))
        return ok


def test_channel(cid):
    ch = db.q1("SELECT * FROM channels WHERE id=?", (cid,))
    if not ch:
        raise KeyError(cid)
    ok = _deliver(ch, f"test:{time.time():.0f}", "پیام آزمایشی رصد", "اتصال کانال اعلان برقرار است ✓", settings.public_url, force=True)
    row = db.q1("SELECT error FROM deliveries WHERE channel_id=? ORDER BY id DESC LIMIT 1", (cid,))
    return {"ok": bool(ok), "error": row and row["error"]}


def _match(ch, sig):
    if SEV.get(sig["severity"], 0) < SEV.get(ch["min_severity"], 1):
        return False
    types = db.jload(ch["signal_types"], []) or []
    return not types or sig["type"] in types


def dispatch_signals():
    """Deliver approved/new signals from the last 24h that have not been delivered yet (pending critical ones wait for the gate)."""
    chans = db.q("SELECT * FROM channels WHERE enabled=1")
    if not chans:
        return 0
    sigs = db.q("SELECT * FROM signals WHERE created>? AND status IN ('new','approved') ORDER BY score DESC LIMIT 50", (time.time() - 86400,))
    n = 0
    for g in sigs:
        expl = db.jload(g["explanation"], {}) or {}
        text = f"سطح: {SEV_FA.get(g['severity'], g['severity'])} · امتیاز {g['score']:.1f}\n{expl.get('text') or ''}"
        link = f"{settings.public_url}/#/trends"
        for ch in chans:
            if _match(ch, g) and _deliver(ch, f"signal:{g['id']}", g["label"], text, link,
                                          {"kind": "signal", "signal": {k: g[k] for k in ("id", "type", "severity", "score", "label")}}):
                n += 1
    return n


def dispatch_brief(bid):
    b = db.q1("SELECT id,title,markdown FROM briefs WHERE id=?", (bid,))
    if not b:
        return 0
    lines = [ln for ln in b["markdown"].splitlines() if ln.strip() and not ln.startswith(("#", ">"))]
    text = "\n".join(lines[:8])[:1500]
    n = 0
    for ch in db.q("SELECT * FROM channels WHERE enabled=1 AND briefs=1"):
        if _deliver(ch, f"brief:{bid}", b["title"] or f"بریف #{bid}", text, f"{settings.public_url}/#/briefs/{bid}", {"kind": "brief", "brief_id": bid}):
            n += 1
    return n


def background(fn, *a):
    threading.Thread(target=lambda: _safe(fn, *a), daemon=True).start()


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001
        pass


def deliveries(limit=100):
    return db.q("""SELECT d.*, c.name channel_name, c.type channel_type FROM deliveries d LEFT JOIN channels c ON c.id=d.channel_id
                   ORDER BY d.id DESC LIMIT ?""", (limit,))


# ---------------- scheduled digest ----------------
DIGEST_DEFAULT = {"enabled": False, "hour": 8, "hours": 24, "top": 8}


def digest_cfg():
    return {**DIGEST_DEFAULT, **(db.kv_get("digest_cfg") or {})}


def set_digest_cfg(body):
    cfg = digest_cfg()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    for k, lo, hi in (("hour", 0, 23), ("hours", 6, 168), ("top", 3, 20)):
        if k in body:
            v = int(body[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} باید بین {lo} و {hi} باشد")
            cfg[k] = v
    db.kv_set("digest_cfg", cfg)
    return cfg


def _now_local():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(settings.tz))
    except Exception:  # noqa: BLE001
        return datetime.now()


def digest_text(hours=24, top=8):
    since = time.time() - hours * 3600
    stories = db.q("""SELECT id,title,title_fa,source_count,lang_count,urgency FROM stories WHERE last_update>?
                      ORDER BY score DESC LIMIT ?""", (since, top))
    sigs = db.q("""SELECT label,severity FROM signals WHERE created>? AND severity IN ('warning','critical') AND status IN ('new','approved')
                   ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, score DESC LIMIT 6""", (since,))
    n_art = db.q1("SELECT COUNT(*) n FROM articles WHERE published_at>? AND duplicate_of IS NULL", (since,))["n"]
    lines = [f"{n_art} خبر یکتا · {len(stories)} داستان برتر · {len(sigs)} هشدار در {hours} ساعت گذشته", ""]
    for i, s in enumerate(stories, 1):
        flag = " ⚡" if s["urgency"] == "flash" else ""
        lines.append(f"{i}. {s['title_fa'] or s['title']} ({s['source_count']} منبع){flag}")
    if sigs:
        lines += ["", "هشدارها:"] + [("🔴 " if g["severity"] == "critical" else "🟠 ") + g["label"] for g in sigs]
    return "\n".join(lines), {"stories": len(stories), "signals": len(sigs), "articles": n_art}


def dispatch_digest(force=False):
    """Send the shift digest to brief-enabled channels once per local day (at/after the configured hour)."""
    cfg = digest_cfg()
    now = _now_local()
    if not force and (not cfg["enabled"] or now.hour < cfg["hour"]):
        return None
    day = now.strftime("%Y-%m-%d")
    ref = f"digest:{day}" + (f":{int(time.time())}" if force else "")
    text, stats = digest_text(cfg["hours"], cfg["top"])
    title = f"خلاصه روزانه رصد · {day}"
    n = 0
    for ch in db.q("SELECT * FROM channels WHERE enabled=1 AND briefs=1"):
        if _deliver(ch, ref, title, text, settings.public_url, {"kind": "digest", **stats}):
            n += 1
    if n or force:
        db.kv_set("last_digest", {"t": time.time(), "day": day, "sent": n})
    return {"sent": n, "title": title, "text": text, "stats": stats}
