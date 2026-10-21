"""Authenticated REST surface. Ownership is checked again for every object read/write."""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import auth, db
from ..config import settings
from . import external, jobs, providers, reports, schemas, store, verification

router = APIRouter(prefix="/api/analysis", tags=["Evidence-grounded analysis"])


def user():
    u = auth.current_user.get()
    if not u:
        raise HTTPException(401, "ورود لازم است")
    return u


def guarded(fn, *args):
    try:
        return fn(*args)
    except KeyError:
        raise HTTPException(404, "یافت نشد")
    except ValueError as e:
        raise HTTPException(400, str(e))


def owned_job(job_id):
    row = store.get_job(job_id, user())
    if not store.allowed(row, user()):
        raise KeyError(job_id)
    return row


@router.get("/capabilities")
def capabilities():
    import importlib.util
    from .. import llm
    import os
    return {"chat_configured": llm.available(), "structured_outputs": llm.cfg().get("structured_outputs", False),
            "embeddings": providers.embedding_configured(), "pdf_text": importlib.util.find_spec("pymupdf") is not None,
            "media_api_enabled": os.environ.get("RASAD_MEDIA_API", "0") == "1",
            "external_verification": False, "external_intake_enabled": external.enabled(),
            "second_reviewer_required": reports.second_reviewer_required(), "native_citations": False, "video_frame_analysis": False,
            "max_upload_bytes": 8 * 1024 * 1024, "max_chunks": 60}


@router.post("/documents")
def import_text(body: schemas.TextDocument):
    did = guarded(store.snapshot, body.title, body.text,
                  {"language": body.language, "coverage": "user_supplied", "origin_verified": False}, user()["id"])
    db.audit(auth.actor(), "analysis:document", did)
    return {"id": did}


@router.post("/documents/upload")
async def import_file(request: Request, title: str = Query("سند بارگذاری‌شده", min_length=1, max_length=500)):
    u = user()
    # Explicitly bounded before constructing a full request body.
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "فایل بیش از ۸ مگابایت است")
    try:
        did = await asyncio.to_thread(providers.ingest_media, bytes(data), request.headers.get("content-type", ""), title, u)
    except (ValueError, UnicodeError) as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(502, "استخراج فایل ناموفق بود؛ تنظیمات API را بررسی کنید")
    db.audit(auth.actor(), "analysis:upload", did)
    return {"id": did}


@router.get("/documents/{doc_id}")
def read_document(doc_id: str):
    d = guarded(store.document, doc_id, user())
    d["chunks"] = db.q("SELECT * FROM document_chunks WHERE document_id=? ORDER BY ordinal", (doc_id,))
    return d


@router.get("/documents/{doc_id}/original")
def original(doc_id: str):
    d = guarded(store.document, doc_id, user())
    path = settings.db_path.parent / "analysis-media" / d["id"]
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=d["id"], media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/chunks/{chunk_id}")
def chunk(chunk_id: str):
    c = db.q1("SELECT * FROM document_chunks WHERE id=?", (chunk_id,))
    if not c:
        raise HTTPException(404)
    guarded(store.document, c["document_id"], user())
    c["locator"] = db.jload(c["locator"], [])
    return c


@router.post("/jobs", status_code=202)
def create_job(body: schemas.JobRequest):
    return guarded(jobs.create, body.model_dump(), user())


@router.get("/jobs")
def list_jobs(limit: int = Query(30, ge=1, le=100)):
    u = user()
    # Admins also see only their own jobs by default; IDs do not reveal other users' activity.
    return db.q("SELECT id,status,progress,created,updated,tokens,error FROM analysis_jobs WHERE owner_id=? ORDER BY created DESC LIMIT ?", (u["id"], limit))


@router.get("/jobs/{job_id}")
def job(job_id: str):
    row = guarded(store.get_job, job_id, user())
    report = db.q1("SELECT id,revision,status FROM analysis_reports WHERE job_id=?", (job_id,)) or {}
    row.update(report_id=report.get("id"), report_revision=report.get("revision"), report_status=report.get("status"))
    row["review_assignment"] = db.q1("SELECT reviewer_id,created FROM analysis_review_assignments WHERE report_id=?", (report.get("id"),))
    return row


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    current = guarded(owned_job, job_id)
    if current["status"] in jobs.TERMINAL:
        raise HTTPException(409, "تحلیل خاتمه یافته است")
    with db.tx() as c:
        c.execute("UPDATE analysis_jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?", (job_id,))
    db.audit(auth.actor(), "analysis:cancel", job_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str):
    row = guarded(owned_job, job_id)
    if row["status"] not in ("failed", "cancelled"):
        raise HTTPException(409, "فقط اجرای ناموفق یا لغوشده قابل تکرار است")
    with db.tx() as c:
        c.execute("UPDATE analysis_jobs SET status='queued',attempts=0,cancel_requested=0,error=NULL,updated=? WHERE id=?", (time.time(), job_id))
    db.audit(auth.actor(), "analysis:retry", job_id)
    return {"ok": True}


@router.get("/jobs/{job_id}/claims")
def claims(job_id: str):
    guarded(store.get_job, job_id, user())
    return store.job_claims(job_id)


@router.get("/reports/{report_id}")
def report(report_id: str):
    return guarded(store.get_report, report_id, user())


@router.post("/reports/{report_id}/review")
def review(report_id: str, body: schemas.Review):
    return guarded(jobs.review, report_id, body, user())


@router.post("/reports/{report_id}/publish")
def publish(report_id: str):
    return guarded(jobs.publish, report_id, user())


@router.delete('/jobs/{job_id}')
def delete_job(job_id: str):
    row = guarded(owned_job, job_id)
    if row['status'] not in jobs.TERMINAL:
        raise HTTPException(409, 'ابتدا تحلیل فعال را لغو کنید و منتظر پایان آن بمانید')
    with db.tx() as c:
        current = c.execute('SELECT status FROM analysis_jobs WHERE id=?', (job_id,)).fetchone()
        if not current or current[0] not in jobs.TERMINAL:
            raise HTTPException(409, 'وضعیت تحلیل تغییر کرده است')
        if c.execute("SELECT 1 FROM analysis_reports WHERE job_id=? AND status='validating'", (job_id,)).fetchone():
            raise HTTPException(409, 'اعتبارسنجی گزارش در حال اجرا است')
        for table in ('analysis_report_history', 'analysis_review_assignments', 'analysis_validation_leases'):
            c.execute('DELETE FROM '+table+' WHERE report_id IN (SELECT id FROM analysis_reports WHERE job_id=?)', (job_id,))
        c.execute('DELETE FROM analysis_stage_results WHERE job_id=?', (job_id,))
        c.execute('DELETE FROM analysis_reviews WHERE report_id IN (SELECT id FROM analysis_reports WHERE job_id=?)', (job_id,))
        for table in ('analysis_calls', 'claim_evidence', 'analysis_claims', 'analysis_reports'):
            c.execute('DELETE FROM ' + table + ' WHERE job_id=?', (job_id,))
        c.execute('DELETE FROM analysis_jobs WHERE id=?', (job_id,))
    db.audit(auth.actor(), 'analysis:delete_job', job_id)
    return {'ok': True}


@router.delete('/documents/{doc_id}')
def delete_document(doc_id: str):
    d = guarded(store.document, doc_id, user())
    if not store.allowed(d, user()):
        raise HTTPException(403, 'حذف سند فقط برای مالک یا مدیر مجاز است')
    with db.tx() as c:
        used = c.execute('''SELECT 1 FROM analysis_jobs j WHERE
            EXISTS(SELECT 1 FROM json_each(j.request,'$.document_ids') WHERE value=?) OR
            EXISTS(SELECT 1 FROM json_each(j.manifest,'$.documents') WHERE json_extract(value,'$.id')=?) LIMIT 1''', (doc_id, doc_id)).fetchone()
        if used:
            raise HTTPException(409, 'سند در دفتر شواهد تحلیل استفاده شده است؛ ابتدا تحلیل‌های وابسته را حذف کنید')
        c.execute('DELETE FROM document_provenance WHERE child_id=? OR parent_id=?', (doc_id, doc_id))
        c.execute('DELETE FROM document_chunks WHERE document_id=?', (doc_id,))
        c.execute('DELETE FROM document_versions WHERE id=?', (doc_id,))
        c.execute("DELETE FROM kv WHERE k LIKE 'embedding:%' AND instr(k,?)>0", (doc_id,))
    (settings.db_path.parent / 'analysis-media' / doc_id).unlink(missing_ok=True)
    db.audit(auth.actor(), 'analysis:delete_document', doc_id)
    return {'ok': True}


@router.post('/documents/external')
def import_external(body: schemas.ExternalDocument):
    try:
        return {"id": external.ingest(body, user())}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(502, 'دریافت امن سند بیرونی ناموفق بود') from None


@router.post('/provenance')
def add_provenance(body: schemas.Provenance):
    return guarded(verification.add, body, user())


@router.get('/provenance')
def get_provenance(document_ids: str = Query(..., max_length=2000)):
    ids = [d.strip() for d in document_ids.split(',') if d.strip()]
    if len(ids) > 12:
        raise HTTPException(400, 'حداکثر ۱۲ سند مجاز است')
    return guarded(verification.graph, ids, user())


@router.delete('/provenance/{edge_id}')
def remove_provenance(edge_id: str):
    return guarded(verification.remove, edge_id, user())


@router.post('/jobs/{job_id}/followup', status_code=202)
def followup(job_id: str, body: schemas.Followup):
    return guarded(verification.followup, job_id, body, user())


@router.get('/review-inbox')
def review_inbox():
    return db.q("SELECT r.id,r.job_id,r.revision,r.status,a.created FROM analysis_review_assignments a JOIN analysis_reports r ON r.id=a.report_id WHERE a.reviewer_id=? ORDER BY a.created DESC", (user()['id'],))


@router.post('/reports/{report_id}/assignment')
def assign_report(report_id: str, body: schemas.Assignment):
    return guarded(reports.assign, report_id, body, user())


@router.post('/reports/{report_id}/revise')
def revise_report(report_id: str, body: schemas.Revision):
    return guarded(reports.revise, report_id, body, user())


@router.post('/reports/{report_id}/revalidate')
def revalidate_report(report_id: str, body: schemas.RevisionNumber):
    return guarded(reports.revalidate, report_id, body, user())


@router.get('/reports/{report_id}/history')
def report_history(report_id: str):
    return guarded(reports.history, report_id, user())


@router.get('/reviewers')
def reviewers():
    if user()['role'] not in ('analyst', 'admin'):
        raise HTTPException(403)
    return db.q("SELECT id,name,username,role FROM users WHERE active=1 AND role IN ('analyst','admin') ORDER BY name,username LIMIT 200")
