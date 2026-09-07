from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import pytest
from backend.app.db import session
from backend.app.models import Job, Application, Run, Outreach, Contact
from backend.core import config, profile, humanize, runner
from backend.core.apply import forms
from backend.core.submissions import claim, confirmation
from backend.core.normalize import eligibility, sponsorship_signal, dedupe_key


def application(status='approved', factcheck=True):
    with session() as db:
        j = Job(dedupe_key='test', source='ashby', company='Example', title='AI Engineer', url='https://example.com/job', country='India', eligible=True, fit_score=90)
        db.add(j); db.flush()
        a = Application(job_id=j.id, platform='ashby', method='ats_form', status=status, answers={'factcheck': {'ok': factcheck}, 'profile_hash': profile.fingerprint()})
        db.add(a); db.flush(); return a.id


def test_country_isolation():
    barrier=threading.Barrier(2)
    def answer(country):
        forms.CTX.clear(); forms.CTX['country']=country; barrier.wait()
        return forms.answer_question('Do you require visa sponsorship?', '', None, lambda *a: None)
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(answer,'India'); b=pool.submit(answer,'United States')
        assert a.result() == 'No'; assert b.result() == 'Yes'


def test_experience_and_sensitive_declarations():
    assert profile.answer_for('Do you have a minimum of 8 years experience?') == 'No'
    assert profile.answer_for('Do you have 3+ years experience?') == 'Yes'
    assert profile.answer_for('Do you accept arbitration?') is None


def test_confirmation_requires_evidence():
    assert not confirmation('Please correct your form', '')
    assert not confirmation('Thank you for visiting our careers page')
    assert confirmation('Your application has been received')


def test_claim_once_and_factcheck():
    aid=application()
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim,[aid,aid])) == [False, True]


def test_failed_factcheck_cannot_claim():
    assert not claim(application(factcheck=False))


def test_atomic_caps(monkeypatch):
    monkeypatch.setattr(humanize,'cap_for',lambda *a: 1)
    with ThreadPoolExecutor(4) as pool: assert sum(pool.map(lambda i: humanize.take('test','apply',str(i)),range(4))) == 1


def test_eligibility():
    assert not eligibility('AI engineer','Remote (US)','remote','Must be located in the US',False)[0]
    assert not sponsorship_signal('Visa sponsorship: false')
    assert not eligibility('AI engineer','Tokyo, Japan','remote japan','',False)[0]
    assert eligibility('AI engineer','Worldwide','worldwide','',False)[0]
    assert eligibility('AI engineer','Mumbai, India','','',False)[0]
    assert dedupe_key('A','Senior Engineer','London') != dedupe_key('A','Engineer','London')


def test_queue_cancel_and_recovery():
    rid=runner.enqueue('pipeline','score')
    assert runner.enqueue('pipeline','score') == rid
    assert runner.stop_run(rid)
    with session() as db: assert db.get(Run,rid).status == 'stopped'
    rid=runner.enqueue('pipeline','score')
    with session() as db: db.get(Run,rid).status='running'
    from backend.app.db import init_db
    init_db()
    with session() as db: assert db.get(Run,rid).status == 'running'
    from backend.core.worker import recover
    recover()
    with session() as db: assert db.get(Run,rid).status == 'interrupted'


def test_message_claim_requires_approval_and_due_date():
    from backend.core.messages import claim
    with session() as db:
        c=Contact(company='Example',name='Hiring',email='hiring@example.com',email_confidence='found');db.add(c);db.flush()
        o=Outreach(channel='email',body='Hello',status='pending_review',contact_id=c.id);db.add(o);db.flush();oid=o.id
    assert not claim(oid)
    with session() as db:
        o=db.get(Outreach,oid);o.status='approved';o.scheduled_for=datetime.utcnow()+timedelta(days=1)
    assert not claim(oid)
    with session() as db: db.get(Outreach,oid).scheduled_for=None
    assert claim(oid);assert not claim(oid)


def test_config_rejects_invalid():
    with pytest.raises(ValueError): config.validate({'review_mode':'false'})
    with pytest.raises(ValueError): config.validate({'caps': {'ats': {'applies': -1}}})
    with pytest.raises(ValueError): config.validate({'fit_threshold':101})


def test_api_contracts():
    from fastapi.testclient import TestClient
    from backend.app.main import app
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
        assert client.post('/api/runs/pipeline/score', headers={'Origin':'https://evil.example'}).status_code == 403
        assert client.post('/api/runs/fullrun',json={'params':{'platforms':['missing']}}).status_code == 422
        aid=application(factcheck=False)
        assert client.post(f'/api/applications/{aid}/status',json={'status':'approved'}).status_code == 409


def test_recovery_only_changes_owned_operations():
    from backend.core.worker import recover
    aid = application()
    rid = runner.enqueue('pipeline', 'score')
    with session() as db:
        db.get(Run, rid).status = 'running'
        app = db.get(Application, aid)
        app.status = 'submitting'  # A legacy/manual process owns this operation.
        owned = Outreach(channel='email', body='Owned', status='sending', claim_run_id=rid)
        manual = Outreach(channel='email', body='Manual', status='sending')
        db.add_all([owned, manual]); db.flush(); owned_id, manual_id = owned.id, manual.id
    recover()
    with session() as db:
        assert db.get(Application, aid).status == 'submitting'
        assert db.get(Outreach, owned_id).status == 'submission_unverified'
        assert db.get(Outreach, manual_id).status == 'sending'


def test_claim_records_run_and_recovery_requires_reconciliation():
    from backend.core.worker import recover
    aid = application()
    rid = runner.enqueue('pipeline', 'score')
    with session() as db: db.get(Run, rid).status = 'running'
    assert claim(aid, rid)
    recover()
    with session() as db:
        assert db.get(Application, aid).claim_run_id == rid
        assert db.get(Application, aid).status == 'needs_human'
    assert not claim(aid, rid)


def test_backup_restore_preserves_data_and_refuses_overwrite(tmp_path):
    import sqlite3
    from backend.core import ops
    aid = application()
    snapshot, restored = tmp_path/'backup.db', tmp_path/'restored.db'
    ops.backup(snapshot)
    ops.restore(snapshot, restored)
    with sqlite3.connect(restored) as db:
        assert db.execute('SELECT id FROM applications').fetchone()[0] == aid
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with pytest.raises(ValueError): ops.backup(snapshot)
    with pytest.raises(ValueError): ops.restore(snapshot, restored)
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_queue_identity_includes_parameters_and_retry_preserves_active_checkpoints():
    first = runner.enqueue('skill', 'linkedin', {'mode': 'discover'})
    second = runner.enqueue('skill', 'linkedin', {'mode': 'easy_apply'})
    assert first != second
    assert runner.enqueue('skill', 'linkedin', {'mode': 'discover'}) == first
    with session() as db:
        old = db.get(Run, first); old.status = 'failed'; old.ended_at = datetime.utcnow(); old.checkpoints = ['old-stage']
    active = runner.enqueue('skill', 'linkedin', {'mode': 'discover'})
    with session() as db: db.get(Run, active).checkpoints = ['active-stage']
    assert runner.retry(first) == active
    with session() as db: assert db.get(Run, active).checkpoints == ['active-stage']


def test_paused_stage_is_not_checkpointed_and_blocks_downstream(monkeypatch):
    from backend.core import fullrun
    ctx = runner.RunContext('fullrun', 'test')
    called = []
    def pause(c, key):
        called.append(key); c.set_status('paused_for_human')
    monkeypatch.setattr(fullrun, '_discover', pause)
    monkeypatch.setattr(fullrun.pipeline, 'score', lambda *a, **kw: called.append('score'))
    fullrun.platform(ctx, 'linkedin', apply=True)
    assert called == ['linkedin']
    assert not ctx.checkpoint_done('discover · linkedin')
    ctx.finish()
    with session() as db: assert db.get(Run, ctx.run_id).status == 'paused_for_human'


def test_failed_stage_blocks_downstream(monkeypatch):
    from backend.core import fullrun
    ctx = runner.RunContext('fullrun', 'test')
    def fail(c, key): c.bump('failed')
    monkeypatch.setattr(fullrun, '_discover', fail)
    monkeypatch.setattr(fullrun.pipeline, 'score', lambda *a, **kw: pytest.fail('score ran after failure'))
    fullrun.platform(ctx, 'linkedin', apply=True)
    assert not ctx.checkpoint_done('discover · linkedin')
    ctx.finish()
    with session() as db: assert db.get(Run, ctx.run_id).status == 'partially_completed'


def test_browser_wait_is_cancellable_without_launch():
    from backend.core import browser
    with pytest.raises(InterruptedError):
        with browser.open_context('ats', should_stop=lambda: True):
            pytest.fail('Cancelled browser must not open')


def test_login_completion_restores_running_state(monkeypatch):
    from backend.core import browser
    from types import SimpleNamespace
    ctx = runner.RunContext('login', 'linkedin')
    ctx.set_status('paused_for_human')
    monkeypatch.setattr(browser, 'is_logged_in', lambda *a: True)
    monkeypatch.setattr(browser.time, 'sleep', lambda *a: None)
    page = SimpleNamespace(goto=lambda *a, **kw: None)
    assert browser.ensure_login(page, 'linkedin', ctx.log, on_ready=lambda: ctx.set_status('running'))
    assert not ctx.is_paused()


def test_browser_handoff_blocks_browser_queue_but_allows_offline_work(monkeypatch):
    from backend.core import worker
    handoff = runner.enqueue('skill', 'linkedin')
    with session() as db:
        row = db.get(Run, handoff); row.status = 'paused_for_human'; row.ended_at = datetime.utcnow()
    browser_run = runner.enqueue('skill', 'ats_apply')
    offline = runner.enqueue('pipeline', 'score')
    seen = []
    monkeypatch.setattr(worker, 'dispatch', lambda ctx, spec: seen.append(ctx.run_id))
    assert worker.run_one()
    assert seen == [offline]
    assert not worker.run_one()
    runner.stop_run(handoff)
    assert worker.run_one()
    assert seen == [offline, browser_run]


def test_retry_releases_handoff_without_reapproving_application():
    aid = application('needs_human')
    original = runner.enqueue('skill', 'linkedin')
    with session() as db:
        row = db.get(Run, original); row.status = 'paused_for_human'; row.ended_at = datetime.utcnow()
    new = runner.retry(original)
    assert new != original
    with session() as db:
        assert db.get(Run, original).status == 'superseded'
        assert db.get(Run, new).status == 'queued'
        assert db.get(Application, aid).status == 'needs_human'


def test_login_aliases_and_worker_health():
    from fastapi.testclient import TestClient
    from backend.app.main import app
    with TestClient(app) as client:
        response = client.post('/api/platforms/x_outreach/login')
        assert response.status_code == 200
        with session() as db: assert db.get(Run, response.json()['run_id']).name == 'x'
        health = client.get('/api/runs/health').json()
        assert health['worker_active'] is False
        assert health['queued'] == 1
        assert health['browser']['profile'] == 'shared'


def test_recipe_signature_changes_with_question_but_not_entered_value():
    from backend.core.apply import recipes
    import copy
    original = {'fields': [{'selector': '#answer', 'tag': 'input', 'type': 'text', 'label': 'Full name', 'filled': False}], 'buttons': []}
    filled = copy.deepcopy(original); filled['fields'][0]['filled'] = True
    assert recipes.signature(original) == recipes.signature(filled)
    changed = copy.deepcopy(original); changed['fields'][0]['label'] = 'Years of Python experience'
    assert recipes.signature(original) != recipes.signature(changed)
    assert not confirmation('Please complete the application form', 'Please complete the application form')


def test_recipe_rejects_model_selected_field_as_advance(monkeypatch):
    from backend.core.apply import recipes
    snapshot = {'fields': [{'selector': '#name', 'tag': 'input', 'type': 'text', 'label': 'Full name'}], 'buttons': [{'selector': '#next', 'text': 'Next'}]}
    monkeypatch.setattr(recipes, 'describe', lambda *a: snapshot)
    monkeypatch.setattr(recipes.llm, 'complete_json', lambda *a: {'fields': [], 'advance': {'selector': '#name'}, 'is_final': False})
    assert recipes.derive(None, 'test', lambda *a: None) is None


def test_invalid_run_params_and_pagination_rejected_before_queueing():
    from fastapi.testclient import TestClient
    from backend.app.main import app
    with TestClient(app) as client:
        for url, params in [('/api/runs/fullrun', {'apply': 'false'}), ('/api/runs/pipeline/score', {'workers': 0}), ('/api/runs/skill/linkedin', {'mode': 'invalid'}), ('/api/runs/skill/ats_apply', {'dry_run': 'false'})]:
            assert client.post(url, json={'params': params}).status_code == 422
        for url in ('/api/jobs?sort=applications', '/api/jobs?size=-1', '/api/applications?page=0', '/api/outreach?size=999'):
            assert client.get(url).status_code == 422
    with session() as db: assert db.query(Run).count() == 0


def test_job_queue_targets_only_selected_job():
    from fastapi.testclient import TestClient
    from backend.app.main import app
    with session() as db:
        jobs = [Job(dedupe_key=f'job-{i}', source='ashby', company='Example', title='Engineer', url=f'https://example.com/{i}', eligible=True) for i in range(2)]
        db.add_all(jobs); db.flush(); selected, other = [j.id for j in jobs]
    with TestClient(app) as client:
        response = client.post(f'/api/jobs/{selected}/status', json={'status': 'queued'})
        assert response.status_code == 200
    with session() as db:
        row = db.get(Run, response.json()['run_id'])
        assert row.spec == {'kind': 'pipeline', 'name': 'prepare_selected', 'params': {'job_ids': [selected]}}
        assert db.get(Job, other).status == 'new'


def test_stale_profile_blocks_claim_and_approval(monkeypatch):
    from backend.core.transitions import application_transition
    aid = application('pending_review')
    monkeypatch.setattr(profile, 'fingerprint', lambda: 'new-profile-version')
    with session() as db:
        with pytest.raises(ValueError, match='Regenerate'):
            application_transition(db.get(Application, aid), 'approved')
        db.get(Application, aid).status = 'approved'
    assert not claim(aid)
    with session() as db: assert 'Regenerate' in db.get(Application, aid).error


def test_regeneration_preserves_uncertain_status_and_refreshes_profile_version(monkeypatch):
    from backend.core import pipeline
    aid = application('needs_human')
    with session() as db:
        a = db.get(Application, aid); a.answers = {'factcheck': {'ok': True}, 'profile_hash': 'old'}
    output = {'headline': 'Engineer', 'summary': 'Builds software.', 'skills_order': [], 'experience_bullets': {}, 'cover_note': 'Hello', 'why_company': 'Engineering role'}
    monkeypatch.setattr(pipeline.llm, 'complete_json', lambda task, *a, **kw: {'ok': True, 'violations': []} if task == 'factcheck' else output)
    monkeypatch.setattr(pipeline.resume, 'render_pdf', lambda *a, **kw: 'data/artifacts/test.pdf')
    ctx = runner.RunContext('pipeline', 'regenerate')
    pipeline.regenerate(ctx, aid)
    with session() as db:
        app = db.get(Application, aid)
        assert app.status == 'needs_human'
        assert app.answers['profile_hash'] == profile.fingerprint()
        assert app.resume_path == 'data/artifacts/test.pdf'
        assert db.query(Application).count() == 1


def test_terminal_application_cannot_be_reapproved_through_pending_review():
    from backend.core.transitions import application_transition
    aid = application('submitted')
    with session() as db:
        for target in ('pending_review', 'approved', 'failed'):
            with pytest.raises(ValueError): application_transition(db.get(Application, aid), target)


def email_outreach(job_id=None, step=1, thread_id=None):
    with session() as db:
        contact = Contact(company='Example', name='Hiring', email='hiring@example.com', email_confidence='found')
        db.add(contact); db.flush()
        message = Outreach(job_id=job_id, contact_id=contact.id, channel='email', step=step, subject='Engineer application', body='Hello hiring team', status='approved', thread_id=thread_id)
        db.add(message); db.flush(); return message.id


@pytest.mark.parametrize('failure,expected', [('metadata', 'approved'), ('send', 'submission_unverified'), (None, 'sent')])
def test_gmail_failure_boundary_and_email_application_lifecycle(monkeypatch, failure, expected):
    from backend.core import gmail
    aid = application()
    with session() as db:
        app = db.get(Application, aid); app.method = 'email'; jid = app.job_id
    oid = email_outreach(jid, thread_id='thread-1')
    class FakeGmail:
        operation = ''
        def users(self): return self
        def threads(self): return self
        def messages(self): return self
        def get(self, **kw): self.operation = 'metadata'; return self
        def send(self, **kw): self.operation = 'send'; return self
        def execute(self):
            if self.operation == failure: raise TimeoutError('Simulated timeout')
            return {'messages': [{'payload': {'headers': [{'name': 'Message-ID', 'value': '<message@example.com>'}]}}]} if self.operation == 'metadata' else {'id': 'message-2', 'threadId': 'thread-1'}
    monkeypatch.setattr(gmail.google_auth, 'configured', lambda: True)
    monkeypatch.setattr(gmail, '_svc', FakeGmail)
    monkeypatch.setattr(gmail.humanize, 'take', lambda *a: True)
    monkeypatch.setattr(gmail.humanize, 'pause', lambda *a: None)
    ctx = runner.RunContext('service', 'gmail_send')
    if failure == 'metadata':
        with pytest.raises(TimeoutError): gmail.send(ctx)
    else: gmail.send(ctx)
    with session() as db:
        assert db.get(Outreach, oid).status == expected
        assert db.get(Application, aid).status == {'approved': 'approved', 'submission_unverified': 'needs_human', 'sent': 'submitted'}[expected]


def test_followup_waits_for_previous_step_and_normalizes_recipient():
    from backend.core import gmail
    oid = email_outreach(thread_id='thread-1')
    with session() as db:
        first = db.get(Outreach, oid); first.status = 'sent'; first.sent_at = datetime.utcnow()-timedelta(days=20)
    ctx = runner.RunContext('service', 'gmail_followups')
    gmail.draft_followups(ctx); gmail.draft_followups(ctx)
    with session() as db:
        assert [o.step for o in db.query(Outreach).order_by(Outreach.step)] == [1, 2]
        second = db.query(Outreach).filter_by(step=2).one()
        second.status = 'sent'; second.sent_at = datetime.utcnow()-timedelta(days=6)
    gmail.draft_followups(ctx)
    with session() as db: assert db.query(Outreach).filter_by(step=3).one().status == 'pending_review'


def test_duplicate_recipient_claim_is_atomic_across_contacts():
    from backend.core.messages import claim as message_claim
    first, second = email_outreach(), email_outreach()
    with session() as db:
        other = db.get(Contact, db.get(Outreach, second).contact_id)
        other.email = '  Hiring@EXAMPLE.com  '
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(message_claim, [first, second]))
    assert sorted(results) == [False, True]
