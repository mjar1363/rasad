"""Versioned editorial workflow; editing always invalidates prior approval and QA."""
import os
import time

from .. import db
from . import schemas, store


def second_reviewer_required():
    return os.environ.get('RASAD_REQUIRE_SECOND_REVIEWER', '0') == '1'


def editor(report):
    return report['body'].get('editor_id', report['owner_id'])


def check_reviewer(report, user):
    if second_reviewer_required() and user['id'] in (report['owner_id'], editor(report)):
        raise ValueError('این سیاست به بازبین دیگری غیر از مالک و ویرایشگر نسخه نیاز دارد')


def check_approval(report):
    row = db.q1("SELECT actor_id FROM analysis_reviews WHERE report_id=? AND revision=? AND decision='approve' ORDER BY id DESC LIMIT 1", (report['id'], report['revision']))
    if not row:
        raise ValueError('تأیید انسانی همین نسخه موجود نیست')
    if second_reviewer_required() and row['actor_id'] in (report['owner_id'], editor(report)):
        raise ValueError('تأیید قبلی با سیاست بازبین مستقل سازگار نیست')
    if second_reviewer_required():
        account = db.q1('SELECT active,role FROM users WHERE id=?', (row['actor_id'],))
        if not account or not account['active'] or account['role'] not in ('analyst', 'admin'):
            raise ValueError('بازبین تأییدکننده دیگر مجوز فعال ندارد')


def assign(report_id, body, user):
    report = store.get_report(report_id, user)
    if not store.allowed(report, user):
        raise KeyError(report_id)
    with db.tx() as c:
        if body.reviewer_id is None:
            c.execute('DELETE FROM analysis_review_assignments WHERE report_id=?', (report_id,))
        else:
            account = c.execute('SELECT active,role FROM users WHERE id=?', (body.reviewer_id,)).fetchone()
            if not account or not account['active'] or account['role'] not in ('analyst', 'admin'):
                raise ValueError('بازبین باید تحلیلگر یا مدیر فعال باشد')
            if body.reviewer_id in (report['owner_id'], editor(report)):
                raise ValueError('بازبین باید شخص دیگری باشد')
            c.execute('INSERT INTO analysis_review_assignments VALUES(?,?,?,?) ON CONFLICT(report_id) DO UPDATE SET reviewer_id=excluded.reviewer_id,assigned_by=excluded.assigned_by,created=excluded.created', (report_id, body.reviewer_id, user['id'], time.time()))
    _touch(report["job_id"])
    db.audit(str(user['id']), 'analysis:assign_review', report_id, {'reviewer_id': body.reviewer_id})
    return store.get_report(report_id, user)


def revise(report_id, body, user):
    report = store.get_report(report_id, user)
    if not store.allowed(report, user):
        raise KeyError(report_id)
    content = dict(report['body'])
    content.update(statements=[s.model_dump() for s in body.statements], limitations=body.limitations,
                   editor_id=user['id'], revision_note=body.note)
    quality = {'passed': False, 'checks': [], 'structural_errors': [], 'requires_revalidation': True,
               'external_verification': False, 'machine_judge_only': True, 'requires_human_review': True}
    with db.tx() as c:
        current = c.execute('SELECT * FROM analysis_reports WHERE id=?', (report_id,)).fetchone()
        if current['revision'] != body.revision or current['status'] not in ('draft', 'approved', 'rejected'):
            raise ValueError('نسخه تغییر کرده یا منتشرشده/در حال اعتبارسنجی است')
        c.execute('INSERT INTO analysis_report_history VALUES(?,?,?,?,?,?,?,?)', (report_id, current['revision'], current['body'], current['quality'], current['status'], time.time(), user['id'], body.note))
        c.execute("UPDATE analysis_reports SET revision=revision+1,status='draft',body=?,quality=?,published=NULL WHERE id=? AND revision=?", (db.jdump(content), db.jdump(quality), report_id, body.revision))
    _touch(report["job_id"])
    db.audit(str(user['id']), 'analysis:revise', report_id, {'revision': body.revision+1})
    return store.get_report(report_id, user)


def revalidate(report_id, body, user):
    from . import jobs
    report = store.get_report(report_id, user)
    if not store.allowed(report, user):
        raise KeyError(report_id)
    token = store.uid("vl_")
    with db.tx() as c:
        if c.execute("""UPDATE analysis_reports SET status='validating' WHERE id=? AND revision=? AND
            (status='draft' OR (status='validating' AND EXISTS(SELECT 1 FROM analysis_validation_leases l WHERE l.report_id=analysis_reports.id AND l.expires<?)))""", (report_id, body.revision, time.time())).rowcount != 1:
            raise ValueError('نسخه تغییر کرده یا برای اعتبارسنجی آماده نیست')
        c.execute('INSERT OR REPLACE INTO analysis_validation_leases VALUES(?,?,?,?)', (report_id, body.revision, token, time.time()+1800))
    try:
        statements = [schemas.Statement.model_validate(s).model_dump() for s in report['body']['statements']]
        claims = store.job_claims(report['job_id'])
        valid = {c['id'] for c in claims}
        errors = []
        for s in statements:
            if not set(s['claim_ids']) <= valid or (s['kind'] != 'gap' and not s['claim_ids']):
                errors.append('شناسه ادعای نامعتبر یا ادعای بدون شاهد')
            if s['kind'] in ('inference', 'scenario') and not s['assumptions']:
                errors.append('فرض لازم موجود نیست')
            if s['kind'] == 'scenario' and not s['watch_indicators']:
                errors.append('نشانگر سناریو موجود نیست')
        factual = [{'index': i, **s} for i, s in enumerate(statements) if s['kind'] == 'reported']
        checks = []
        if factual and not errors:
            if not jobs.llm.agent_enabled('analyze'):
                raise ValueError('مدل برای اعتبارسنجی فعال نیست')
            checks = jobs.call(report['job_id'], 'revision_quality', jobs.AUDIT_SYSTEM, {'statements': factual, 'claims': claims}, schemas.QualityChecks)['items']
        expected = {s['index'] for s in factual}
        indexes = [c['index'] for c in checks]
        passed = bool(statements) and not errors and set(indexes) == expected and len(indexes) == len(expected) and all(c['verdict'] == 'supported' for c in checks)
        quality = {'passed': passed, 'checks': checks, 'structural_errors': errors, 'requires_revalidation': False,
                   'external_verification': False, 'machine_judge_only': True, 'requires_human_review': True}
    except Exception:
        with db.tx() as c:
            c.execute("UPDATE analysis_reports SET status='draft',quality=? WHERE id=? AND revision=? AND status='validating' AND EXISTS(SELECT 1 FROM analysis_validation_leases WHERE report_id=? AND token=?)", (db.jdump({'passed': False, 'requires_revalidation': True, 'error': 'revalidation failed'}), report_id, body.revision, report_id, token))
            c.execute('DELETE FROM analysis_validation_leases WHERE report_id=? AND token=?', (report_id, token))
        raise ValueError('اعتبارسنجی نسخه ناموفق بود؛ تأیید قبلی معتبر نیست') from None
    with db.tx() as c:
        changed = c.execute("UPDATE analysis_reports SET status='draft',quality=? WHERE id=? AND revision=? AND status='validating' AND EXISTS(SELECT 1 FROM analysis_validation_leases WHERE report_id=? AND token=?)", (db.jdump(quality), report_id, body.revision, report_id, token)).rowcount
        if changed != 1:
            raise ValueError('مهلت اعتبارسنجی قبلی تمام شده و اجرای دیگری جایگزین شده است')
        c.execute('DELETE FROM analysis_validation_leases WHERE report_id=? AND token=?', (report_id, token))
    _touch(report["job_id"])
    db.audit(str(user['id']), 'analysis:revalidate', report_id, {'revision': body.revision, 'passed': passed})
    return store.get_report(report_id, user)


def history(report_id, user):
    current = store.get_report(report_id, user)
    rows = db.q('SELECT * FROM analysis_report_history WHERE report_id=? ORDER BY revision', (report_id,))
    for row in rows:
        row['body'], row['quality'] = db.jload(row['body']), db.jload(row['quality'])
    return {'archived': rows, 'current': current}


def _touch(job_id):
    with db.tx() as c:
        c.execute('UPDATE analysis_jobs SET updated=? WHERE id=?', (time.time(), job_id))
