"""Deterministic acceptance tests. Mocked models test contracts, not real-model accuracy."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from rasad import db, llm, notify
from rasad.analysis import jobs, providers, retrieval, schemas, store
from rasad.api import app

OWNER = {"id": 0, "role": "admin"}
OTHER = {"id": 9981, "role": "analyst"}


@pytest.fixture(autouse=True)
def clean_analysis(monkeypatch):
    db.init()
    for table in ('analysis_reviews', 'analysis_calls', 'analysis_reports', 'claim_evidence', 'analysis_claims', 'analysis_jobs', 'document_chunks', 'document_versions'):
        with db.tx() as c:
            c.execute('DELETE FROM ' + table)
    from rasad import supervisor
    monkeypatch.setattr(supervisor.get(), 'killed', False)
    yield
    with db.tx() as c:
        c.execute("DELETE FROM signals WHERE type='test'")
        c.execute("DELETE FROM deliveries WHERE channel_id=987654")


def doc(text='وزارت اعلام کرد شمار گزارش‌ها ۱۲ مورد است.', owner=0):
    return store.snapshot('گزارش نمونه', text, {'coverage': 'test', 'origin_verified': False}, owner)


def fake_model(monkeypatch, quote_valid=True, verdict='supported'):
    monkeypatch.setattr(llm, 'agent_enabled', lambda a: True)
    def call(system, payload, **kwargs):
        payload = json.loads(payload.removeprefix('<untrusted_content>\n').removesuffix('\n</untrusted_content>'))
        purpose = kwargs['purpose']
        if purpose.endswith(':extract'):
            chunk = payload['chunks'][0]
            result = {'claims': [{'text': chunk['text'], 'kind': 'attributed', 'speaker': 'وزارت', 'event_time': None,
                      'citations': [{'chunk_id': chunk['id'], 'quote': chunk['text'] if quote_valid else 'نقل قول ساختگی'}]}], 'observations': [], 'limitations': ['صحت خارجی بررسی نشده']}
        elif purpose.endswith(':assess'):
            result = {'items': [{'claim_id': p['claim_id'], 'chunk_id': p['chunk_id'], 'quote': p['text'], 'relation': 'different_time', 'explanation': 'زمان متفاوت است'} for p in payload['pairs']]}
        elif purpose.endswith(':synthesize'):
            c = payload['claims'][0]
            result = {'statements': [{'text': c['text'], 'kind': 'reported', 'claim_ids': [c['id']], 'assumptions': [], 'watch_indicators': []}], 'limitations': ['نمونه آزمون']}
        elif purpose.endswith(':quality'):
            result = {'items': [{'index': s['index'], 'verdict': verdict, 'explanation': 'داوری نمونه'} for s in payload['statements']]}
        else:
            raise AssertionError(purpose)
        return result, 10
    monkeypatch.setattr(llm, 'chat_json', call)


def process(document_ids, question=''):
    j = jobs.create({'document_ids': document_ids, 'question': question}, OWNER)
    jobs.process(j['id'])
    return store.get_job(j['id'], OWNER), db.q1('SELECT id FROM analysis_reports WHERE job_id=?', (j['id'],))['id']


def test_snapshot_is_immutable_private_and_deterministic():
    d = doc()
    assert d == doc()
    assert d != doc('متن اصلاح شده')
    with pytest.raises(KeyError):
        store.document(d, OTHER)
    assert store.document(d, OWNER)['content_hash'] == store.digest(store.document(d, OWNER)['text'])


def test_chunks_cover_entire_unicode_source_without_gaps():
    text = ('خبر فارسی و عدد ۱۲۳. گزارش رویداد\n' * 400)
    d = doc(text)
    chunks = db.q('SELECT * FROM document_chunks WHERE document_id=? ORDER BY ordinal', (d,))
    covered = set()
    for c in chunks:
        assert c['text'] == text[c['start_char']:c['end_char']]
        assert len(c['text']) <= 2200
        covered.update(range(c['start_char'], c['end_char']))
    assert covered == set(range(len(text)))
    _, manifest = retrieval.select([d], '', 60)
    assert manifest['available_characters'] == len(text)
    assert manifest['selected_characters'] == len(text)


@pytest.mark.parametrize('quote', ['غایب', '', 'تکرار'])
def test_missing_empty_ambiguous_quotes_rejected(quote):
    d = doc('تکرار و تکرار')
    c = db.q1('SELECT * FROM document_chunks WHERE document_id=?', (d,))
    with pytest.raises(ValueError):
        store.anchor({'chunk_id': c['id'], 'quote': quote}, {c['id']: c})


def test_unicode_anchor_uses_server_offsets():
    d = doc('مقدمه: شمار ۱۲ مورد اعلام شد.')
    c = db.q1('SELECT * FROM document_chunks WHERE document_id=?', (d,))
    ref = store.anchor({'chunk_id': c['id'], 'quote': '۱۲ مورد'}, {c['id']: c})
    assert store.document(d, OWNER)['text'][ref['start_char']:ref['end_char']] == '۱۲ مورد'


def test_unrelated_question_abstains_without_model(monkeypatch):
    monkeypatch.setattr(llm, 'chat_json', lambda *a, **k: pytest.fail('must not call provider'))
    j, rid = process([doc('زلزله در شهر گزارش شد.')], 'فوتبال')
    r = store.get_report(rid, OWNER)
    assert j['manifest']['selected_chunks'] == 0
    assert r['body']['statements'][0]['kind'] == 'gap'
    assert j['tokens'] == 0


def test_full_lifecycle_requires_human_review(monkeypatch):
    fake_model(monkeypatch)
    j, rid = process([doc()])
    r = store.get_report(rid, OWNER)
    assert j['status'] == 'needs_review'
    assert r['status'] == 'draft' and r['quality']['passed']
    assert not r['quality']['external_verification']
    assert len(r['calls']) == 3
    with pytest.raises(ValueError):
        jobs.publish(rid, OWNER)
    jobs.review(rid, schemas.Review(decision='approve', note='شواهد انسانی بررسی شد', revision=1), OWNER)
    assert jobs.publish(rid, OWNER)['status'] == 'published'
    with pytest.raises(ValueError):
        jobs.review(rid, schemas.Review(decision='reject', note='دوباره', revision=1), OWNER)


def test_negative_machine_audit_blocks_approval(monkeypatch):
    fake_model(monkeypatch, verdict='unsupported')
    j, rid = process([doc()])
    assert not store.get_report(rid, OWNER)['quality']['passed']
    with pytest.raises(ValueError):
        jobs.review(rid, schemas.Review(decision='approve', note='بازبینی', revision=1), OWNER)


def test_fabricated_quote_never_enters_evidence(monkeypatch):
    fake_model(monkeypatch, quote_valid=False)
    j, rid = process([doc()])
    assert not store.job_claims(j['id'])
    assert store.get_report(rid, OWNER)['body']['statements'][0]['kind'] == 'gap'
    assert store.get_report(rid, OWNER)['quality']['rejected_evidence']


def test_cross_document_assessment_does_not_claim_external_truth(monkeypatch):
    fake_model(monkeypatch)
    j, rid = process([doc(), doc('طبق گزارش قبلی شمار ۱۰ مورد بود.')])
    claims = store.job_claims(j['id'])
    assert any(e['relation'] == 'different_time' for c in claims for e in c['evidence'])
    assert all(c['status'] == 'reported_not_verified' for c in claims)
    assert store.get_report(rid, OWNER)['body']['manifest']['source_independence'] == 'not_established'


def test_cancel_checkpoint_prevents_model(monkeypatch):
    monkeypatch.setattr(llm, 'chat_json', lambda *a, **k: pytest.fail('cancelled'))
    j = jobs.create({'document_ids': [doc()]}, OWNER)
    with db.tx() as c:
        c.execute('UPDATE analysis_jobs SET cancel_requested=1 WHERE id=?', (j['id'],))
    with pytest.raises(jobs.Cancelled):
        jobs.process(j['id'])


def test_restart_resumes_checkpoint_and_stores_one_report(monkeypatch):
    fake_model(monkeypatch)
    j, rid = process([doc()])
    before = len(store.job_claims(j['id']))
    jobs.process(j['id'])
    assert len(store.job_claims(j['id'])) == before
    assert db.q1('SELECT COUNT(*) n FROM analysis_reports WHERE job_id=?', (j['id'],))['n'] == 1


def test_private_job_and_report_are_invisible(monkeypatch):
    fake_model(monkeypatch)
    j, rid = process([doc()])
    for fn, ident in [(store.get_job, j['id']), (store.get_report, rid)]:
        with pytest.raises(KeyError):
            fn(ident, OTHER)


def test_api_documents_validation_and_queue(monkeypatch):
    monkeypatch.setattr(jobs, 'run_one', lambda: False)
    with TestClient(app) as client:
        assert client.post('/api/analysis/documents', json={'title': 'x', 'text': ''}).status_code == 422
        d = client.post('/api/analysis/documents', json={'title': 'نمونه', 'text': 'متن نمونه خبر'}).json()['id']
        assert client.get('/api/analysis/documents/'+d).status_code == 200
        assert client.post('/api/analysis/jobs', json={}).status_code == 400
        j = client.post('/api/analysis/jobs', json={'document_ids':[d]}).json()
        assert client.post('/api/analysis/jobs/'+j['id']+'/cancel').status_code == 200
        assert client.get('/api/analysis/jobs/'+j['id']).json()['status'] == 'cancelled'
        assert client.post('/api/analysis/jobs/'+j['id']+'/retry').status_code == 200
        assert client.get('/api/analysis/capabilities').json()['external_verification'] is False


def test_upload_private_utf8_original_and_disabled_media(monkeypatch):
    monkeypatch.delenv('RASAD_MEDIA_API', raising=False)
    d = providers.ingest_media('محتوای سند'.encode(), 'text/plain', 'سند', OWNER)
    assert store.document(d, OWNER)['text'] == 'محتوای سند'
    with pytest.raises(ValueError):
        providers.ingest_media(b'fake', 'image/png', 'تصویر', OWNER)
    with pytest.raises(ValueError):
        providers.ingest_media(b'bad', 'application/pdf', 'pdf', OWNER)
    with pytest.raises(ValueError):
        providers.ingest_media(b'', 'text/plain', 'empty', OWNER)


def test_reservations_are_atomic_and_released(monkeypatch):
    monkeypatch.setenv('LLM_TOKEN_OVERHEAD','0')
    monkeypatch.setattr(llm, 'cfg', lambda: {'daily_budget': 200})
    monkeypatch.setattr(llm, 'tokens_today', lambda: 0)
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS llm_reservations(id TEXT PRIMARY KEY,day TEXT,amount INTEGER,expires REAL)')
        c.execute('DELETE FROM llm_reservations')
    def reserve(_):
        try:
            return llm._reserve([], 120)
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(reserve, range(2)))
    assert sum(v is not None for v in values) == 1
    for v in values:
        if v:
            llm._release(v)


def test_delivery_failure_can_retry_without_exposing_secrets(monkeypatch):
    ch={'id':987654}
    def fail(*a):
        raise RuntimeError('secret-key-must-not-be-stored')
    monkeypatch.setattr(notify, '_send', fail)
    assert notify._deliver(ch,'retry-case','title','text') == 0
    row=db.q1('SELECT * FROM deliveries WHERE channel_id=?', (ch['id'],))
    assert 'secret-key' not in row['error']
    monkeypatch.setattr(notify, '_send', lambda *a: None)
    assert notify._deliver(ch,'retry-case','title','text') == 1
    assert notify._deliver(ch,'retry-case','title','text') is None


def test_document_delete_preserves_referenced_evidence(monkeypatch):
    monkeypatch.setattr(jobs, 'run_one', lambda: False)
    with TestClient(app) as client:
        d = client.post('/api/analysis/documents', json={'title':'نمونه','text':'متن حریم خصوصی'}).json()['id']
        jid = client.post('/api/analysis/jobs', json={'document_ids':[d]}).json()['id']
        assert client.delete('/api/analysis/documents/'+d).status_code == 409
        assert client.delete('/api/analysis/jobs/'+jid).status_code == 409
        client.post('/api/analysis/jobs/'+jid+'/cancel')
        assert client.delete('/api/analysis/jobs/'+jid).status_code == 200
        assert client.delete('/api/analysis/documents/'+d).status_code == 200
        assert client.get('/api/analysis/documents/'+d).status_code == 404


def test_strict_schema_is_sent_and_refusal_is_not_repaired(monkeypatch):
    import httpx
    monkeypatch.setattr(llm, '_reserve', lambda *a: 'test')
    monkeypatch.setattr(llm, '_release', lambda *a: None)
    monkeypatch.setattr(llm, '_pace', lambda *a: None)
    cfg={'enabled':True,'base_url':'https://provider.example/v1','api_key':'private', 'model':'test-model','fallback_model':'', 'daily_budget':99999999, 'rpm':1000,'timeout':10,'temperature':0, 'structured_outputs':True}
    monkeypatch.setattr(llm, 'cfg', lambda:cfg)
    seen=[]
    def response(url, **kw):
        seen.append(kw['json'])
        return httpx.Response(200, request=httpx.Request('POST',url), json={'choices':[{'message':{'content':'{"items":[]}'},'finish_reason':'stop'}],'usage':{'total_tokens':5}})
    monkeypatch.setattr(llm.httpx, 'post', response)
    result,_=llm.chat_json('system','data',response_schema=schemas.Assessments.model_json_schema())
    assert result == {'items':[]}
    assert seen[0]['response_format']['type']=='json_schema'
    assert seen[0]['response_format']['json_schema']['strict']
    def refusal(url, **kw):
        return httpx.Response(200, request=httpx.Request('POST',url), json={'choices':[{'message':{'refusal':'not allowed','content':None},'finish_reason':'stop'}],'usage':{'total_tokens':4}})
    monkeypatch.setattr(llm.httpx,'post',refusal)
    with pytest.raises(RuntimeError, match='refused'):
        llm.chat_json('system','data',response_schema=schemas.Assessments.model_json_schema())


def test_embedding_validation_and_separate_request_cap(monkeypatch):
    import httpx
    from rasad.config import settings
    monkeypatch.setattr(settings,'embed_base_url','https://embedding.example/v1')
    monkeypatch.setattr(llm,'_pace',lambda *a: None)
    monkeypatch.setenv('RASAD_EMBEDDING_DAILY_REQUESTS','1')
    with db.tx() as c:
        c.execute("DELETE FROM kv WHERE k LIKE 'aux_requests:embedding:%'")
    def response(url, **kw):
        assert 'Authorization' not in kw['headers']  # chat key must never leak to a different provider
        return httpx.Response(200,request=httpx.Request('POST',url),json={'data':[{'index':0,'embedding':[0.1,1.0]}]})
    monkeypatch.delenv('EMBED_API_KEY',raising=False)
    monkeypatch.setattr(providers.httpx,'post',response)
    assert providers.embeddings(['test']) == [[.1,1.0]]
    with pytest.raises(ValueError,match='سقف'):
        providers.embeddings(['again'])


def test_pdf_page_locator_when_optional_adapter_installed():
    pymupdf=pytest.importorskip('pymupdf')
    pdf=pymupdf.open(); page=pdf.new_page(); page.insert_text((72,72),'Evidence example 12 cases')
    data=pdf.tobytes(); pdf.close()
    d=providers.ingest_media(data,'application/pdf','PDF',OWNER)
    value=store.document(d,OWNER)
    assert value['metadata']['segments'][0]['page']==1
    assert '12 cases' in value['text']


def test_scanned_pdf_fails_instead_of_inventing_text():
    pymupdf=pytest.importorskip('pymupdf')
    pdf=pymupdf.open();pdf.new_page();data=pdf.tobytes();pdf.close()
    with pytest.raises(ValueError, match='OCR'):
        providers.ingest_media(data,'application/pdf','scan',OWNER)


def test_scoped_alert_gate_does_not_approve_other_pending_signals(monkeypatch):
    from rasad import supervisor
    monkeypatch.setattr(notify,'background',lambda *a:None)
    with db.tx() as c:
        a=c.execute("INSERT INTO signals(type,key,label,status,created) VALUES('test',?,'first','pending',?)", ('gate-a'+str(time.time()),time.time())).lastrowid
        b=c.execute("INSERT INTO signals(type,key,label,status,created) VALUES('test',?,'other','pending',?)", ('gate-b'+str(time.time()),time.time())).lastrowid
    supervisor.get()._apply_gate({'kind':'alerts','signal_ids':[a]}, 'approve', None, 'test', {})
    assert db.q1('SELECT status FROM signals WHERE id=?',(a,))['status']=='approved'
    assert db.q1('SELECT status FROM signals WHERE id=?',(b,))['status']=='pending'


def test_expired_worker_lease_is_reclaimed(monkeypatch):
    j=jobs.create({'document_ids':[doc()]},OWNER)
    with db.tx() as c:
        c.execute("UPDATE analysis_jobs SET status='extracting',attempts=1,lease_until=? WHERE id=?",(time.time()-10,j['id']))
    seen=[]
    def finish(jid):
        seen.append(jid)
        with db.tx() as c:
            c.execute("UPDATE analysis_jobs SET status='needs_review' WHERE id=?",(jid,))
    monkeypatch.setattr(jobs,'process',finish)
    assert jobs.run_one()
    assert seen==[j['id']]
    assert store.get_job(j['id'],OWNER)['attempts']==2


def test_real_http_transport_with_local_mock_provider(monkeypatch):
    """Exercise gateway/contract/worker together over HTTP; NOT a real-model quality test."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body)
            raw=body['messages'][-1]['content']
            payload=json.loads(raw.removeprefix('<untrusted_content>\n').removesuffix('\n</untrusted_content>'))
            if 'chunks' in payload:
                chunk=payload['chunks'][0]
                result={'claims':[{'text':chunk['text'],'kind':'reported','speaker':None,'event_time':None,'citations':[{'chunk_id':chunk['id'],'quote':chunk['text']}]}],'observations':[],'limitations':[]}
            elif 'statements' in payload:
                result={'items':[{'index':s['index'],'verdict':'supported','explanation':'fixture'} for s in payload['statements']]}
            else:
                c=payload['claims'][0]
                result={'statements':[{'text':c['text'],'kind':'reported','claim_ids':[c['id']],'assumptions':[],'watch_indicators':[]}],'limitations':[]}
            response=json.dumps({'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)},'finish_reason':'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response)));self.end_headers();self.wfile.write(response)
        def log_message(self,*a):
            pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    cfg={**llm.cfg(),'enabled':True,'base_url':f'http://127.0.0.1:{server.server_port}/v1','api_key':'local-fixture','model':'fixture-model','fallback_model':'','daily_budget':99999999,'structured_outputs':True,'rpm':100000}
    monkeypatch.setattr(llm,'cfg',lambda:cfg)
    try:
        j,rid=process([doc()])
        r=store.get_report(rid,OWNER)
        assert r['quality']['passed'] and j['tokens']==450
        assert len(seen)==3
        assert all(b['response_format']['type']=='json_schema' for b in seen)
        assert all(c['model']=='fixture-model' for c in r['calls'])
    finally:
        server.shutdown();server.server_close()


def test_analysis_api_rbac_and_cross_user_privacy(monkeypatch):
    from rasad.config import settings
    monkeypatch.setattr(settings,'auth_enabled',True)
    monkeypatch.setattr(settings,'admin_token','fixture-admin')
    monkeypatch.setattr(jobs,'run_one',lambda:False)
    with TestClient(app) as client:
        admin={'X-Admin-Token':'fixture-admin'}
        for username,role in [('evidence-a','analyst'),('evidence-b','analyst'),('evidence-v','viewer')]:
            assert client.post('/api/users',headers=admin,json={'username':username,'password':'strong-fixture-password','role':role}).status_code==200
        assert client.post('/api/auth/login',json={'username':'evidence-a','password':'strong-fixture-password'}).status_code==200
        d=client.post('/api/analysis/documents',json={'title':'خصوصی','text':'فقط برای مالک'}).json()['id']
        jid=client.post('/api/analysis/jobs',json={'document_ids':[d]}).json()['id']
        client.post('/api/auth/logout')
        client.post('/api/auth/login',json={'username':'evidence-b','password':'strong-fixture-password'})
        assert client.get('/api/analysis/documents/'+d).status_code==404
        assert client.get('/api/analysis/jobs/'+jid).status_code==404
        assert client.post('/api/analysis/jobs/'+jid+'/cancel').status_code==404
        assert client.get('/api/analysis/jobs').json()==[]
        client.post('/api/auth/logout')
        client.post('/api/auth/login',json={'username':'evidence-v','password':'strong-fixture-password'})
        assert client.post('/api/analysis/documents',json={'title':'x','text':'x'}).status_code==403
        assert client.post('/api/analysis/jobs',json={'question':'x'}).status_code==403
