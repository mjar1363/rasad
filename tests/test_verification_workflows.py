"""Phase-two workflow/security tests; no paid API or public website is used."""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from rasad import auth, db, llm
from rasad.analysis import external, jobs, reports, schemas, store, verification
from rasad.api import app
from rasad.config import settings

OWNER={'id':0,'role':'admin'}
OTHER={'id':8001,'role':'analyst'}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    db.init()
    tables=('analysis_validation_leases','analysis_report_history','analysis_review_assignments','analysis_stage_results','document_provenance','analysis_reviews','analysis_reports','analysis_calls','claim_evidence','analysis_claims','analysis_jobs','document_chunks','document_versions')
    with db.tx() as c:
        for t in tables:c.execute('DELETE FROM '+t)
    monkeypatch.delenv('RASAD_REQUIRE_SECOND_REVIEWER',raising=False)
    monkeypatch.delenv('RASAD_EXTERNAL_FETCH',raising=False)
    from rasad import supervisor
    monkeypatch.setattr(supervisor.get(),'killed',False)


def document(text='گزارش نمونه با عدد ۱۲', owner=0):
    return store.snapshot('نمونه',text,{'coverage':'test'},owner)


def gap_report():
    did=document()
    job=jobs.create({'document_ids':[did],'question':'فوتبال'},OWNER)
    jobs.process(job['id'])
    rid=db.q1('SELECT id FROM analysis_reports WHERE job_id=?',(job['id'],))['id']
    return job['id'],rid,did


def revision(r, **kw):
    data={'revision':r['revision'],'statements':r['body']['statements'],'limitations':r['body']['limitations'],'note':'اصلاح نسخه گزارش'}
    data.update(kw)
    return schemas.Revision.model_validate(data)


def account(uid=8001, role='analyst'):
    with db.tx() as c:
        c.execute('INSERT OR REPLACE INTO users(id,username,name,pw_hash,role,active,created) VALUES(?,?,?,?,?,1,0)',(uid,'workflow-'+str(uid),'بازبین','unused',role))
    return {'id':uid,'role':role}


def test_revision_invalidates_approval_and_preserves_previous_version():
    jid,rid,did=gap_report()
    jobs.review(rid,schemas.Review(decision='approve',note='بررسی شد',revision=1),OWNER)
    r=store.get_report(rid,OWNER)
    new=reports.revise(rid,revision(r),OWNER)
    assert new['revision']==2 and new['status']=='draft'
    assert not new['quality']['passed'] and new['quality']['requires_revalidation']
    with pytest.raises(ValueError):jobs.publish(rid,OWNER)
    history=reports.history(rid,OWNER)
    assert history['archived'][0]['revision']==1
    assert history['archived'][0]['status']=='approved'
    reports.revalidate(rid,schemas.RevisionNumber(revision=2),OWNER)
    jobs.review(rid,schemas.Review(decision='approve',note='نسخه دوم بررسی شد',revision=2),OWNER)
    assert jobs.publish(rid,OWNER)['status']=='published'
    with pytest.raises(ValueError):reports.revise(rid,revision(store.get_report(rid,OWNER)),OWNER)


def test_stale_revision_and_invented_claim_cannot_be_approved():
    jid,rid,did=gap_report()
    r=store.get_report(rid,OWNER)
    statement={'text':'ادعای تازه بدون منبع','kind':'reported','claim_ids':['forged'],'assumptions':[],'watch_indicators':[]}
    reports.revise(rid,revision(r,statements=[statement]),OWNER)
    with pytest.raises(ValueError):reports.revise(rid,revision(r),OWNER)
    r=reports.revalidate(rid,schemas.RevisionNumber(revision=2),OWNER)
    assert not r['quality']['passed'] and r['quality']['structural_errors']
    with pytest.raises(ValueError):jobs.review(rid,schemas.Review(decision='approve',revision=2,note='تأیید'),OWNER)


def test_assignment_grants_read_and_review_not_edit_publish_or_cancel(monkeypatch):
    jid,rid,did=gap_report(); reviewer=account()
    with pytest.raises(KeyError):store.get_report(rid,reviewer)
    reports.assign(rid,schemas.Assignment(reviewer_id=reviewer['id']),OWNER)
    assert store.get_report(rid,reviewer)['id']==rid
    assert store.get_job(jid,reviewer)['id']==jid
    assert store.document(did,reviewer)['id']==did
    with pytest.raises(KeyError):reports.revise(rid,revision(store.get_report(rid,reviewer)),reviewer)
    with pytest.raises(KeyError):jobs.publish(rid,reviewer)
    monkeypatch.setenv('RASAD_REQUIRE_SECOND_REVIEWER','1')
    with pytest.raises(ValueError):jobs.review(rid,schemas.Review(decision='approve',note='مالک',revision=1),OWNER)
    jobs.review(rid,schemas.Review(decision='approve',note='بازبینی مستقل',revision=1),reviewer)
    assert jobs.publish(rid,OWNER)['status']=='published'
    reports.assign(rid,schemas.Assignment(reviewer_id=None),OWNER)
    with pytest.raises(KeyError):store.get_report(rid,reviewer)
    with pytest.raises(KeyError):store.document(did,reviewer)


def test_second_reviewer_policy_checks_old_approvals(monkeypatch):
    jid,rid,did=gap_report()
    jobs.review(rid,schemas.Review(decision='approve',note='بررسی',revision=1),OWNER)
    monkeypatch.setenv('RASAD_REQUIRE_SECOND_REVIEWER','1')
    with pytest.raises(ValueError,match='سیاست'):jobs.publish(rid,OWNER)


def test_provenance_is_asserted_private_acyclic_and_not_truth():
    a,b,c=document('اول'),document('دوم'),document('سوم')
    result=verification.add(schemas.Provenance(child_id=b,parent_id=a,relation='republication',note='ارجاع به بیانیه اولیه'),OWNER)
    assert result['assertions_only'] and result['independence']=='not_established'
    verification.add(schemas.Provenance(child_id=c,parent_id=b,relation='translation',note='ترجمه همان متن'),OWNER)
    with pytest.raises(ValueError,match='چرخه'):
        verification.add(schemas.Provenance(child_id=a,parent_id=c,relation='cites',note='چرخه'),OWNER)
    with pytest.raises(KeyError):verification.graph([a,b],OTHER)
    edge=result['edges'][0]['id']
    with pytest.raises(KeyError):verification.remove(edge,OTHER)
    assert verification.remove(edge,OWNER)['ok']


def test_additional_evidence_creates_a_new_private_job():
    jid,rid,did=gap_report(); extra=document('شاهد تکمیلی')
    new=verification.followup(jid,schemas.Followup(document_ids=[extra]),OWNER)
    assert new['id']!=jid and new['request']['mode']=='compare'
    assert set(new['request']['document_ids'])=={did,extra}
    assert store.get_report(rid,OWNER)['revision']==1
    with pytest.raises(KeyError):verification.followup(jid,schemas.Followup(document_ids=[extra]),OTHER)


def test_stage_cache_reuses_successful_result_and_changes_with_prompt(monkeypatch):
    jid=jobs.create({'document_ids':[document()]},OWNER)['id']
    seen=[]
    monkeypatch.setattr(llm,'chat_json',lambda *a,**kw:(seen.append(kw) or {'items':[]},17))
    first=jobs.call(jid,'test','prompt',{'test':'one'},schemas.Assessments)
    assert jobs.call(jid,'test','prompt',{'test':'one'},schemas.Assessments)==first
    assert len(seen)==1 and store.get_job(jid,OWNER)['tokens']==17
    jobs.call(jid,'test','changed prompt',{'test':'one'},schemas.Assessments)
    assert len(seen)==2 and store.get_job(jid,OWNER)['tokens']==34


def test_cancel_after_provider_records_billed_usage_and_result(monkeypatch):
    jid=jobs.create({'document_ids':[document()]},OWNER)['id']
    def response(*a,**kw):
        with db.tx() as c:c.execute('UPDATE analysis_jobs SET cancel_requested=1 WHERE id=?',(jid,))
        return {'items':[]},27
    monkeypatch.setattr(llm,'chat_json',response)
    with pytest.raises(jobs.Cancelled):jobs.call(jid,'test','prompt',{},schemas.Assessments)
    assert store.get_job(jid,OWNER)['tokens']==27
    assert db.q1('SELECT result FROM analysis_stage_results WHERE job_id=?',(jid,))


def network(monkeypatch,address='93.184.216.34'):
    monkeypatch.setenv('RASAD_EXTERNAL_FETCH','1')
    monkeypatch.setenv('RASAD_EXTERNAL_ALLOWED_HOSTS','example.org')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))])


@pytest.mark.parametrize('url',['http://example.org/x','https://example.org:444/x','https://user:secret@example.org/x','https://not-allowed.example/x','https://example.org/\nheader'])
def test_external_target_rejects_unsafe_url(monkeypatch,url):
    network(monkeypatch)
    with pytest.raises(ValueError):external._target(url)


@pytest.mark.parametrize('ip',['127.0.0.1','10.1.2.3','169.254.169.254','::1','fc00::1','192.0.2.1'])
def test_external_target_blocks_private_and_reserved_dns(monkeypatch,ip):
    network(monkeypatch,ip)
    with pytest.raises(ValueError,match='خصوصی'):external._target('https://example.org/news')


def test_external_intake_checks_robots_redirect_and_records_hash(monkeypatch):
    network(monkeypatch)
    seen=[]
    def request(host,ip,path,limit=external._LIMIT):
        seen.append((host,ip,path))
        if path=='/robots.txt':return 200,{},b'User-agent: *\nDisallow: /private\n'
        if path=='/old':return 302,{'location':'/new'},b''
        return 200,{'content-type':'text/plain; charset=utf-8'},'بیانیه اولیه منتشر شد'.encode()
    monkeypatch.setattr(external,'_request',request)
    did=external.ingest(schemas.ExternalDocument(url='https://example.org/old',title='بیانیه',source_type='primary_claimed'),OWNER)
    d=store.document(did,OWNER)
    assert d['metadata']['url']=='https://example.org/new'
    assert not d['metadata']['origin_verified'] and d['metadata']['source_type_is_user_assertion']
    assert len(d['metadata']['raw_sha256'])==64
    assert len(seen)==4 and all(ip=='93.184.216.34' for _,ip,_ in seen)
    with pytest.raises(ValueError,match='robots'):external.ingest(schemas.ExternalDocument(url='https://example.org/private',title='ممنوع'),OWNER)


def test_external_redirect_cannot_escape_host_allowlist(monkeypatch):
    network(monkeypatch)
    def request(host,ip,path,limit=external._LIMIT):
        if path=='/robots.txt':return 404,{},b''
        return 302,{'location':'https://169.254.169.254/metadata'},b''
    monkeypatch.setattr(external,'_request',request)
    with pytest.raises(ValueError):external.ingest(schemas.ExternalDocument(url='https://example.org/x',title='redirect'),OWNER)


def test_external_transport_pins_address_but_uses_original_tls_host(monkeypatch):
    calls=[]
    class Raw:
        def close(self):calls.append('closed')
    monkeypatch.setattr(socket,'create_connection',lambda address,timeout:(calls.append(address) or Raw()))
    connection=external._PinnedHTTPS('example.org','93.184.216.34')
    class Context:
        def wrap_socket(self,raw,server_hostname):calls.append(server_hostname);return raw
    connection._context=Context()
    connection.connect();connection.close()
    assert calls==[('93.184.216.34',443),'example.org','closed']


def test_api_assigned_reviewer_cannot_mutate_source_job_or_document(monkeypatch):
    jid,rid,did=gap_report(); reviewer=account()
    reports.assign(rid,schemas.Assignment(reviewer_id=8001),OWNER)
    monkeypatch.setattr(jobs,'run_one',lambda:False)
    monkeypatch.setattr(auth,'user_from_token',lambda token:dict(reviewer,username='reviewer') if token=='review' else None)
    with TestClient(app) as client:
        headers={'Authorization':'Bearer review'}
        assert client.get('/api/analysis/reports/'+rid,headers=headers).status_code==200
        assert client.get('/api/analysis/review-inbox',headers=headers).json()[0]['id']==rid
        assert client.post('/api/analysis/jobs/'+jid+'/cancel',headers=headers).status_code==404
        assert client.delete('/api/analysis/jobs/'+jid,headers=headers).status_code==404
        assert client.delete('/api/analysis/documents/'+did,headers=headers).status_code==403
        assert client.get('/api/analysis/documents/'+did,headers=headers).status_code==200


def test_invalid_model_schema_is_billed_but_never_cached(monkeypatch):
    jid=jobs.create({'document_ids':[document()]},OWNER)['id']
    monkeypatch.setattr(llm,'chat_json',lambda *a,**kw:({'unexpected':True},33))
    with pytest.raises(ValueError):jobs.call(jid,'quality','prompt',{},schemas.Assessments)
    assert store.get_job(jid,OWNER)['tokens']==33
    assert db.q1('SELECT stage FROM analysis_calls WHERE job_id=?',(jid,))['stage']=='quality:invalid_schema'
    assert not db.q1('SELECT 1 FROM analysis_stage_results WHERE job_id=?',(jid,))


def test_expired_revalidation_lease_can_resume_but_active_one_cannot():
    import time
    jid,rid,did=gap_report()
    with db.tx() as c:
        c.execute("UPDATE analysis_reports SET status='validating' WHERE id=?",(rid,))
        c.execute('INSERT INTO analysis_validation_leases VALUES(?,?,?,?)',(rid,1,'old',time.time()+1000))
    with pytest.raises(ValueError):reports.revalidate(rid,schemas.RevisionNumber(revision=1),OWNER)
    with db.tx() as c:c.execute('UPDATE analysis_validation_leases SET expires=? WHERE report_id=?',(time.time()-1,rid))
    r=reports.revalidate(rid,schemas.RevisionNumber(revision=1),OWNER)
    assert r['status']=='draft' and r['quality']['passed']
    assert r['validation_lease'] is None


def test_equal_text_is_only_a_duplicate_signal_not_proof_of_origin():
    a=store.snapshot('one','متن کاملاً یکسان',{'url':'https://one.example'},0)
    b=store.snapshot('two','متن کاملاً یکسان',{'url':'https://two.example'},0)
    value=verification.graph([a,b],OWNER)
    assert value['exact_text_groups']==[[a,b]]
    assert value['independence']=='not_established' and not value['edges']


def test_revised_reported_statement_gets_new_semantic_audit(monkeypatch):
    jid,rid,did=gap_report()
    chunk=db.q1('SELECT * FROM document_chunks WHERE document_id=?',(did,))
    with db.tx() as c:
        c.execute('INSERT INTO analysis_claims VALUES(?,?,?,?,?,?,?)',('fixture-claim',jid,did,chunk['text'],'reported',None,None))
    jobs._evidence(jid,'fixture-claim',{'chunk_id':chunk['id'],'quote':chunk['text']},{chunk['id']:chunk},'reported','quote','extraction')
    r=store.get_report(rid,OWNER)
    content={'text':'عدد گزارش‌ها ۹۹۹ است','kind':'reported','claim_ids':['fixture-claim'],'assumptions':[],'watch_indicators':[]}
    reports.revise(rid,revision(r,statements=[content]),OWNER)
    monkeypatch.setattr(llm,'agent_enabled',lambda a:True)
    monkeypatch.setattr(llm,'chat_json',lambda *a,**kw:({'items':[{'index':0,'verdict':'unsupported','explanation':'عدد تغییر کرده'}]},10))
    r=reports.revalidate(rid,schemas.RevisionNumber(revision=2),OWNER)
    assert not r['quality']['passed']
    assert r['quality']['checks'][0]['verdict']=='unsupported'


def test_external_request_rejects_size_and_compression_before_read(monkeypatch):
    class Response:
        status=200
        def __init__(self,headers):self.headers=headers
        def getheaders(self):return self.headers
        def read1(self,n):pytest.fail('unsafe body must not be read')
    class Connection:
        def __init__(self,*a):pass
        def request(self,*a,**kw):pass
        def getresponse(self):return response
        def close(self):pass
    monkeypatch.setattr(external,'_PinnedHTTPS',Connection)
    for headers in [[('Content-Length',str(external._LIMIT+1))],[('Content-Encoding','gzip')]]:
        response=Response(headers)
        with pytest.raises(ValueError):external._request('example.org','93.184.216.34','/')


def test_followup_requires_ready_original_and_additional_document():
    did=document(); extra=document('سند تازه')
    queued=jobs.create({'document_ids':[did]},OWNER)
    with pytest.raises(ValueError,match='ابتدا'):
        verification.followup(queued['id'],schemas.Followup(document_ids=[extra]),OWNER)
    jid,rid,did=gap_report()
    with pytest.raises(ValueError,match='تازه'):
        verification.followup(jid,schemas.Followup(document_ids=[did]),OWNER)
