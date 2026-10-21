"""Immutable source snapshots, exact quote anchors and private analysis ownership."""
import hashlib
import json
import time
import uuid

from .. import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_schema(version INTEGER PRIMARY KEY, applied REAL NOT NULL);
CREATE TABLE IF NOT EXISTS document_versions(
 id TEXT PRIMARY KEY, article_id TEXT, owner_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
 title TEXT NOT NULL, text TEXT NOT NULL, metadata TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_doc_article ON document_versions(article_id,created);
CREATE TABLE IF NOT EXISTS document_chunks(
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, text TEXT NOT NULL,
 locator TEXT NOT NULL, UNIQUE(document_id,ordinal));
CREATE INDEX IF NOT EXISTS ix_chunks_doc ON document_chunks(document_id,ordinal);
CREATE TABLE IF NOT EXISTS analysis_jobs(
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 progress REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, updated REAL NOT NULL, lease_until REAL, error TEXT,
 manifest TEXT NOT NULL DEFAULT '{}', extraction TEXT NOT NULL DEFAULT '[]', tokens INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_analysis_queue ON analysis_jobs(status,created);
CREATE TABLE IF NOT EXISTS analysis_claims(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, document_id TEXT NOT NULL, text TEXT NOT NULL,
 kind TEXT NOT NULL, speaker TEXT, event_time TEXT);
CREATE INDEX IF NOT EXISTS ix_claim_job ON analysis_claims(job_id);
CREATE TABLE IF NOT EXISTS claim_evidence(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, claim_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
 quote TEXT NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
 relation TEXT NOT NULL, explanation TEXT NOT NULL, method TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_evidence_claim ON claim_evidence(claim_id);
CREATE TABLE IF NOT EXISTS analysis_reports(
 id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL, body TEXT NOT NULL, quality TEXT NOT NULL, created REAL NOT NULL, published REAL);
CREATE TABLE IF NOT EXISTS analysis_reviews(
 id INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL, revision INTEGER NOT NULL,
 actor_id INTEGER NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_calls(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL, prompt_version TEXT NOT NULL,
 input_hash TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL, schema_mode TEXT NOT NULL,
 tokens INTEGER NOT NULL, created REAL NOT NULL);
"""


def init():
    with db.tx() as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO analysis_schema VALUES(1,?)", (time.time(),))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def uid(prefix):
    return prefix + uuid.uuid4().hex


def allowed(row, user):
    # Imported documents and jobs are private even from other analysts. Admin access is explicit.
    return bool(row and user and (row["owner_id"] == user["id"] or user["role"] == "admin"))


def document(doc_id, user):
    row = db.q1("SELECT * FROM document_versions WHERE id=?", (doc_id,))
    if not row or not (row["owner_id"] == -1 or allowed(row, user)):
        raise KeyError(doc_id)
    row["metadata"] = db.jload(row["metadata"], {})
    return row


def snapshot(title, text, metadata, owner_id, article_id=None):
    if not text.strip():
        raise ValueError("سند متن قابل تحلیل ندارد")
    if len(text) > 200000:
        raise ValueError("متن بیش از حد مجاز است؛ سند را بخش‌بندی کنید")
    # Include metadata: changes in attribution/time/coverage create a new immutable version too.
    fingerprint = digest(db.jdump([title, text, metadata, owner_id, article_id]))
    doc_id = "dv_" + fingerprint
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO document_versions VALUES(?,?,?,?,?,?,?,?)",
                  (doc_id, article_id, owner_id, digest(text), title, text, db.jdump(metadata), time.time()))
        if not c.execute("SELECT 1 FROM document_chunks WHERE document_id=?", (doc_id,)).fetchone():
            pos, ordinal = 0, 0
            while pos < len(text):
                end = min(pos + 2200, len(text))
                if end < len(text):
                    boundary = max(text.rfind("\n", pos + 1100, end), text.rfind(". ", pos + 1100, end))
                    if boundary > pos:
                        end = boundary + 1
                chunk_id = f"{doc_id}:c{ordinal}"
                locators = [s for s in metadata.get("segments", [])
                            if s["start_char"] < end and s["end_char"] > pos]
                c.execute("INSERT INTO document_chunks VALUES(?,?,?,?,?,?,?)",
                          (chunk_id, doc_id, ordinal, pos, end, text[pos:end], db.jdump(locators)))
                if end == len(text):
                    break
                pos, ordinal = end - 180, ordinal + 1
    return doc_id


def snapshot_article(article_id):
    a = db.q1("""SELECT a.*,s.name source_name,s.ownership_group FROM articles a
                  JOIN sources s ON s.id=a.source_id WHERE a.id=?""", (article_id,))
    if not a:
        raise KeyError(article_id)
    text = a["text"] or a["lead"] or ""
    metadata = {"url": a["url"], "source_id": a["source_id"], "source_name": a["source_name"],
                "ownership_group": a["ownership_group"], "published_at": a["published_at"],
                "fetched_at": a["fetched_at"], "language": a["lang"],
                "extraction_method": a.get("extraction_method"),
                "coverage": "fulltext" if a.get("extraction_method") == "trafilatura" else "summary_or_unknown",
                "origin_verified": False}
    return snapshot(a["title"], text, metadata, -1, article_id)


def anchor(citation, chunks):
    chunk = chunks.get(citation["chunk_id"])
    quote = citation["quote"]
    if not chunk or not quote or len(quote) > 2200:
        raise ValueError("ارجاع یا نقل‌قول نامعتبر")
    offset = chunk["text"].find(quote)
    if offset < 0:
        raise ValueError("نقل‌قول در نسخه سند وجود ندارد")
    if chunk["text"].find(quote, offset + 1) >= 0:
        raise ValueError("نقل‌قول مبهم است؛ عبارت بلندتر لازم است")
    return {"chunk_id": chunk["id"], "quote": quote,
            "start_char": chunk["start_char"] + offset,
            "end_char": chunk["start_char"] + offset + len(quote)}


def get_job(job_id, user):
    row = db.q1("SELECT * FROM analysis_jobs WHERE id=?", (job_id,))
    if not allowed(row, user):
        raise KeyError(job_id)
    for key in ("request", "manifest", "extraction"):
        row[key] = db.jload(row[key], {} if key != "extraction" else [])
    return row


def get_report(report_id, user):
    row = db.q1("SELECT * FROM analysis_reports WHERE id=?", (report_id,))
    if not allowed(row, user):
        raise KeyError(report_id)
    row["body"], row["quality"] = db.jload(row["body"], {}), db.jload(row["quality"], {})
    row["reviews"] = db.q("SELECT revision,actor_id,decision,note,created FROM analysis_reviews WHERE report_id=? ORDER BY id", (report_id,))
    row["calls"] = db.q("SELECT stage,prompt_version,input_hash,model,provider,schema_mode,tokens,created FROM analysis_calls WHERE job_id=? ORDER BY created", (row["job_id"],))
    return row


def job_claims(job_id):
    rows = db.q("SELECT * FROM analysis_claims WHERE job_id=? ORDER BY id", (job_id,))
    for row in rows:
        row["evidence"] = db.q("SELECT * FROM claim_evidence WHERE claim_id=? ORDER BY id", (row["id"],))
        row["status"] = "disputed" if any(e["relation"] == "contradicts" for e in row["evidence"]) else "reported_not_verified"
    return rows
