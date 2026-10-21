import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, name TEXT, url TEXT, homepage TEXT, lang TEXT, country TEXT,
  type TEXT, ownership_group TEXT, credibility REAL DEFAULT 0.6, bias TEXT,
  topics TEXT, status TEXT DEFAULT 'active', robots_ok INTEGER DEFAULT 1,
  fail_count INTEGER DEFAULT 0, last_fetch REAL, last_ok REAL, last_error TEXT,
  item_count INTEGER DEFAULT 0, etag TEXT, modified TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, source_id TEXT, url TEXT, canonical_url TEXT, title TEXT,
  lead TEXT, text TEXT, authors TEXT, lang TEXT, published_at REAL, fetched_at REAL,
  extracted_at REAL, content_hash TEXT, extraction_method TEXT, extraction_quality REAL,
  duplicate_of TEXT, story_id TEXT, topics TEXT, regions TEXT, urgency TEXT,
  sentiment REAL, category TEXT, title_fa TEXT, lead_fa TEXT, translated_by TEXT,
  stage TEXT DEFAULT 'raw'
);
CREATE INDEX IF NOT EXISTS ix_art_pub ON articles(published_at);
CREATE INDEX IF NOT EXISTS ix_art_story ON articles(story_id);
CREATE INDEX IF NOT EXISTS ix_art_stage ON articles(stage);
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(id UNINDEXED, title, lead, text, title_fa, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS minhash(article_id TEXT PRIMARY KEY, sig BLOB, created REAL);
CREATE TABLE IF NOT EXISTS stories(
  id TEXT PRIMARY KEY, title TEXT, title_fa TEXT, summary TEXT, first_seen REAL, last_update REAL,
  article_count INTEGER, source_count INTEGER, lang_count INTEGER, topics TEXT, regions TEXT,
  entities TEXT, urgency TEXT, category TEXT, score REAL, sentiment REAL, status TEXT DEFAULT 'active',
  verification TEXT, centroid TEXT
);
CREATE TABLE IF NOT EXISTS storyline_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id TEXT, t REAL, type TEXT, summary TEXT,
  article_id TEXT, source_id TEXT
);
CREATE TABLE IF NOT EXISTS entities(
  id TEXT PRIMARY KEY, type TEXT, name_fa TEXT, name_en TEXT, name_ar TEXT,
  lat REAL, lon REAL, iso TEXT, mention_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mentions(article_id TEXT, entity_id TEXT, surface TEXT, PRIMARY KEY(article_id, entity_id));
CREATE INDEX IF NOT EXISTS ix_men_ent ON mentions(entity_id);
CREATE TABLE IF NOT EXISTS relations(
  subject_id TEXT, predicate TEXT, object_id TEXT, weight REAL, first_seen REAL, last_seen REAL,
  evidence TEXT, PRIMARY KEY(subject_id, predicate, object_id)
);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id TEXT, text TEXT, kind TEXT, confidence TEXT,
  evidence TEXT, contradicted_by TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, key TEXT, label TEXT, score REAL, baseline REAL,
  observed REAL, window TEXT, severity TEXT, story_ids TEXT, explanation TEXT, status TEXT DEFAULT 'new',
  created REAL, UNIQUE(type, key, window)
);
CREATE TABLE IF NOT EXISTS briefs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, title TEXT, markdown TEXT, status TEXT,
  run_id TEXT, stats TEXT, edited_by_user INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, mode TEXT, trigger TEXT, status TEXT, started REAL, ended REAL,
  cost_tokens INTEGER DEFAULT 0, summary TEXT
);
CREATE TABLE IF NOT EXISTS steps(
  id TEXT PRIMARY KEY, run_id TEXT, agent TEXT, idx INTEGER, status TEXT, progress REAL,
  started REAL, ended REAL, input_summary TEXT, output_summary TEXT, output TEXT,
  reasoning TEXT, error TEXT, tokens INTEGER DEFAULT 0, edited_by_user INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_steps_run ON steps(run_id);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, target TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, purpose TEXT, model TEXT, prompt_tokens INTEGER,
  completion_tokens INTEGER, total_tokens INTEGER, latency_ms INTEGER, ok INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_ts ON llm_calls(ts);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, question TEXT, answer TEXT, citations TEXT, tokens INTEGER
);
"""

_local = threading.local()
_write_lock = threading.RLock()


def connect():
    c = getattr(_local, "conn", None)
    if c is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


MIGRATIONS = [("stories", "ai", "TEXT"), ("claims", "method", "TEXT"), ("signals", "ai", "TEXT"), ("chats", "user_id", "INTEGER")]


def init():
    with _write_lock:
        c = connect()
        c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.commit()
    from . import auth, notify, triage, watch  # noqa: E402  (module schemas)
    for m in (auth, notify, watch, triage):
        m.init()
    from .analysis import store
    store.init()


def checkpoint():
    """Flush WAL into the main DB file (keeps snapshots/backups consistent)."""
    with _write_lock:
        connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def merge_json(table, key_col, key, col, patch):
    """Shallow-merge a dict into a JSON column."""
    with tx() as c:
        r = c.execute(f"SELECT {col} FROM {table} WHERE {key_col}=?", (key,)).fetchone()
        cur = jload(r[0], {}) if r else {}
        cur = cur if isinstance(cur, dict) else {}
        cur.update(patch)
        c.execute(f"UPDATE {table} SET {col}=? WHERE {key_col}=?", (jdump(cur), key))
        return cur


@contextmanager
def tx():
    c = connect()
    with _write_lock:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql, args=()):
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


def jdump(x):
    return json.dumps(x, ensure_ascii=False)


def jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def audit(actor, action, target="", detail=None):
    if actor in ("user", None):
        from .auth import actor as _actor
        actor = _actor()
    with tx() as c:
        c.execute("INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                  (time.time(), actor, action, target, jdump(detail or {})))


def kv_get(k, default=None):
    r = q1("SELECT v FROM kv WHERE k=?", (k,))
    return jload(r["v"], default) if r else default


def kv_set(k, v):
    with tx() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, jdump(v)))
