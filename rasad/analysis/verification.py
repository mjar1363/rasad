"""Human-asserted provenance graph and additional-evidence follow-up jobs."""
import time
from .. import db
from . import schemas, store


def graph(document_ids, user):
    ids = list(dict.fromkeys(document_ids))
    for did in ids:
        store.document(did, user)
    if not ids:
        return {'edges': [], 'independence': 'not_established', 'assertions_only': True}
    ph = ','.join('?' for _ in ids)
    groups = {}
    for did in ids:
        d = store.document(did, user)
        groups.setdefault(d['content_hash'], []).append(did)
    rows = db.q('SELECT * FROM document_provenance WHERE child_id IN ('+ph+') AND parent_id IN ('+ph+') AND (owner_id=? OR ?=1)', (*ids, *ids, user['id'], int(user['role'] == 'admin')))
    return {'edges': rows, 'independence': 'not_established', 'assertions_only': True,
            'exact_text_groups': [v for v in groups.values() if len(v)>1],
            'text_match_notice': 'متن یکسان نشانه تکرار است؛ منشأ مشترک یا استقلال را ثابت نمی‌کند'}


def add(body, user):
    for did in (body.child_id, body.parent_id):
        store.document(did, user)
    if body.child_id == body.parent_id:
        raise ValueError('سند نمی‌تواند منشأ خودش باشد')
    with db.tx() as c:
        # Privacy boundary: cycles checked on this actor's assertions only, not hidden edges.
        rows = c.execute('SELECT child_id,parent_id FROM document_provenance WHERE owner_id=?', (user['id'],)).fetchall()
        links = {}
        for row in rows:
            links.setdefault(row[0], []).append(row[1])
        todo, seen = [body.parent_id], set()
        while todo:
            node = todo.pop()
            if node == body.child_id:
                raise ValueError('این پیوند چرخه منشأ ایجاد می‌کند')
            if node not in seen:
                seen.add(node)
                todo.extend(links.get(node, []))
        eid = store.uid('pr_')
        existing = c.execute('SELECT owner_id FROM document_provenance WHERE child_id=? AND parent_id=? AND relation=? AND owner_id=?', (body.child_id, body.parent_id, body.relation, user["id"])).fetchone()
        if existing:
            raise ValueError('این رابطه قبلاً ثبت شده است')
        c.execute('INSERT INTO document_provenance VALUES(?,?,?,?,?,?,?,?)', (eid, user['id'], body.child_id, body.parent_id, body.relation, body.note, user['id'], time.time()))
    db.audit(str(user['id']), 'analysis:provenance', eid)
    return graph([body.child_id, body.parent_id], user)


def remove(edge_id, user):
    row = db.q1('SELECT * FROM document_provenance WHERE id=?', (edge_id,))
    if not store.allowed(row, user):
        raise KeyError(edge_id)
    with db.tx() as c:
        c.execute('DELETE FROM document_provenance WHERE id=?', (edge_id,))
    db.audit(str(user['id']), 'analysis:remove_provenance', edge_id)
    return {'ok': True}


def followup(job_id, body, user):
    from . import jobs
    original = store.get_job(job_id, user)
    original_docs = [d['id'] for d in original['manifest'].get('documents', [])]
    if original['status'] not in ('needs_review', 'completed') or not original_docs:
        raise ValueError('ابتدا تحلیل اولیه باید با دامنه اسناد ثبت‌شده آماده بازبینی شود')
    if not set(body.document_ids) - set(original_docs):
        raise ValueError('حداقل یک سند تازه خارج از دامنه تحلیل اولیه لازم است')
    docs = list(dict.fromkeys(original_docs + original['request']['document_ids'] + body.document_ids))
    if len(docs) > 12:
        raise ValueError('مجموع اسناد تکمیلی و اولیه بیش از ۱۲ است؛ دامنه را کوچک‌تر کنید')
    for did in docs:
        store.document(did, user)
    request = schemas.JobRequest(document_ids=docs, question=body.question or original['request']['question'], mode='compare', max_chunks=60)
    result = jobs.create(request.model_dump(), user)
    db.audit(str(user['id']), 'analysis:followup', result['id'], {'parent_job_id': job_id})
    # The child is independent and private to its creator; no truth verdict is inferred.
    return result
