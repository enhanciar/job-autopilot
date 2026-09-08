"""Batch 19: heartbeat/health, answer correctness, hydration extraction, upsert refresh, outreach approval/caps, config validation."""
import json
import pytest
from backend.app.db import session
from backend.app.models import Job, Outreach, Contact
from backend.core import config, profile, worker, messages
from backend.core.apply import forms
from backend.core.hydration import extract_posting
from backend.core.normalize import upsert


def test_technology_specific_years_are_not_total_career_years():
    assert profile.answer_for("How many years of professional experience do you have?") == "3"
    assert profile.answer_for("Years of experience in software engineering") == "3"
    assert profile.answer_for("How many years of experience do you have with Rust?") is None
    assert profile.answer_for("Years of experience with Kubernetes:") is None
    prof = profile.load(); prof["skill_years"] = {"python": 3}
    assert profile.answer_for("How many years of experience do you have with Python?", prof) == "3"


def test_explicit_country_in_question_overrides_job_country():
    forms.CTX.clear(); forms.CTX["country"] = "India"
    log = lambda *a, **k: None
    assert forms.answer_question("Are you legally authorized to work in the United States?", "", None, log) == "No"
    assert forms.answer_question("Will you require sponsorship to work in India?", "", None, log) == "No"
    assert forms.answer_question("Are you authorized to work in India?", "", None, log) == "Yes"
    forms.CTX["country"] = "Germany"
    assert forms.answer_question("Do you require visa sponsorship?", "", None, log) == "Yes"
    forms.CTX.clear()


def test_numeric_salary_follows_currency_or_country():
    prefs = profile.load()["preferences"]
    forms.CTX.clear(); forms.CTX["country"] = "United States"
    assert forms.numeric_answer("Expected annual salary (USD)", "INR 30 LPA for India roles") == str(int(prefs["min_salary_usd_remote"]))
    forms.CTX["country"] = "India"
    assert forms.numeric_answer("Expected CTC", "INR 30 LPA") == str(int(prefs["expected_salary_lpa"] * 100000))
    assert forms.numeric_answer("Expected monthly salary in EUR", "anything") == str(int(round(prefs["wttj_min_salary_eur"] / 12)))
    forms.CTX["country"] = "Unknown"
    assert forms.numeric_answer("Salary expectation", "INR 30 LPA") is None          # jurisdiction unclear: leave to the human
    assert forms.numeric_answer("Years of experience", "3") == "3"
    assert forms.numeric_answer("Notice period", "1 month notice; 4 weeks") is None   # two numbers: ambiguous
    forms.CTX.clear()


def test_generated_screening_answer_must_pass_factcheck(monkeypatch):
    from backend.core import llm
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "I led a team of 40 at Google.")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"ok": False, "violations": [{"claim": "Google", "why": "never worked there"}]})
    with pytest.raises(ValueError):
        forms.generate_answer("Describe your leadership experience", "", None)
    assert forms.answer_question("Describe a system you built", "job", None, lambda *a, **k: None) is None  # __LLM__ path swallows into None
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"ok": True, "violations": []})
    assert forms.generate_answer("Describe your leadership experience", "", None) == "I led a team of 40 at Google."


def test_extract_posting_prefers_jsonld_and_detects_closed_pages():
    body = "x" * 300
    html = f"""<html><head><script type="application/ld+json">{json.dumps({"@context": "https://schema.org", "@type": "JobPosting",
        "title": "AI Engineer", "description": "<p>Build <b>LLM</b> agents.</p>" + "<p>" + body + "</p>", "validThrough": "2099-01-01T00:00:00Z"})}</script></head>
        <body><nav>Careers Home Sign in</nav><main>Generic chrome</main></body></html>"""
    out = extract_posting(html)
    assert out["source"] == "jsonld" and out["description"].startswith("Build LLM agents.") and not out["closed"]
    expired = html.replace("2099-01-01", "2001-01-01")
    assert extract_posting(expired)["closed"]
    closed_page = "<html><body><main>This job is no longer available. " + body + "</main></body></html>"
    assert extract_posting(closed_page)["closed"]
    blocked = "<html><body>Checking your browser before accessing the site.</body></html>"
    assert extract_posting(blocked)["blocked"] and extract_posting(blocked)["description"] == ""
    plain = "<html><body><header>Menu</header><main>" + "Role details " * 60 + "</main><footer>legal</footer></body></html>"
    plain_out = extract_posting(plain)
    assert plain_out["source"] == "page" and "Menu" not in plain_out["description"]


def test_upsert_recomputes_eligibility_when_location_changes():
    desc = "We build agents. " * 20
    with session() as db:
        job, created = upsert(db, source="greenhouse", company="Acme", title="AI Engineer", url="https://boards.greenhouse.io/acme/1", location="Mumbai, India", description=desc)
        assert created and job.eligible and job.country == "India"
        db.flush()
        same, created = upsert(db, source="greenhouse", company="Acme", title="AI Engineer", url="https://boards.greenhouse.io/acme/1", location="Austin, TX (US only)", description=desc)
        assert not created and same.id == job.id
        assert same.country == "United States" and same.eligible is False and same.status == "filtered"
        again, _ = upsert(db, source="greenhouse", company="Acme", title="AI Engineer", url="https://boards.greenhouse.io/acme/1", location="Austin, TX",
                          description=desc + " Visa sponsorship available for this role.")   # restriction lifted, sponsorship declared
        assert again.eligible is True and again.sponsor_flag is True and again.status == "new"


def test_changing_an_approved_message_resets_its_approval():
    with session() as db:
        c = Contact(company="Acme", name="Sam", linkedin_url="https://linkedin.com/in/sam"); db.add(c); db.flush()
        o = Outreach(contact_id=c.id, channel="linkedin_connect", step=1, body="Hi Sam, original note", status="approved"); db.add(o); db.flush()
        assert not messages.set_body(o, "Hi Sam, original note ")            # whitespace-only difference keeps the approval
        assert o.status == "approved"
        assert messages.set_body(o, "Hi Sam, a different note")
        assert o.status == "pending_review" and "Re-review" in o.error and o.body == "Hi Sam, a different note"
        draft = Outreach(contact_id=c.id, channel="linkedin_dm", step=2, body="draft", status="pending_review"); db.add(draft); db.flush()
        assert not messages.set_body(draft, "new draft") and draft.status == "pending_review"


def test_heartbeat_roundtrip_and_stale_detection(monkeypatch):
    worker.heartbeat(42)
    status = worker.heartbeat_status()
    assert status["run_id"] == 42 and status["fresh"] and status["heartbeat_age_s"] < 5
    from datetime import datetime, timedelta
    worker.HEARTBEAT.write_text(json.dumps({"ts": (datetime.utcnow() - timedelta(minutes=5)).isoformat(), "pid": 1, "run_id": None}))
    assert not worker.heartbeat_status()["fresh"]
    worker.HEARTBEAT.write_text("not json")
    assert worker.heartbeat_status() == {"heartbeat_age_s": None, "run_id": None, "pid": None, "fresh": False}


def test_health_endpoint_reports_heartbeat():
    from fastapi.testclient import TestClient
    from backend.app.main import app
    worker.heartbeat(None)
    data = TestClient(app).get("/api/runs/health").json()
    assert {"worker_active", "worker_heartbeat_age_s", "worker_heartbeat_fresh", "worker_run_id", "queued", "running", "browser_handoffs", "browser"} <= set(data)
    assert data["worker_heartbeat_fresh"] is True and data["worker_run_id"] is None


@pytest.mark.parametrize("patch,message", [
    ({"schedule": {"enabled": "yes"}}, "schedule.enabled"),
    ({"schedule": {"jobs": [{"name": "x", "kind": "rocket"}]}}, "unknown kind"),
    ({"schedule": {"jobs": [{"name": "x", "kind": "skill", "every_minutes": 0}]}}, "every_minutes"),
    ({"schedule": {"jobs": [{"name": "x", "kind": "skill"}, {"name": "x", "kind": "skill"}]}}, "twice"),
    ({"filters": {"titles_include": ["ok", 5]}}, "filters.titles_include"),
    ({"llm": {"models": {"claude-cli": 3}}}, "llm.models"),
    ({"llm": {"task_models": {"claude-cli": "not-a-dict"}}}, "task_models"),
    ({"business_hours": {"start": 25}}, "business_hours.start"),
])
def test_config_validation_rejects_nested_mistakes(patch, message):
    cfg = config.load(); cfg.update(patch)
    with pytest.raises(ValueError, match=message):
        config.validate(cfg)
    assert config.validate(config.load())


# ---------------------------------------------------------------- profile setup from a resume (multi-user)
def test_profile_templates_and_list_paths():
    assert profile.fill("{experience.0.company} / {education.0.end}") == f"{profile.load()['experience'][0]['company']} / {profile.load()['education'][0]['end']}"
    assert profile.answer_for("Current employer") == profile.load()["experience"][0]["company"]
    assert profile.answer_for("Graduation year") == str(profile.load()["education"][0]["end"])


def test_validate_profile_reports_every_problem():
    good = profile.load()
    assert profile.validate_profile(good) == []
    bad = {"identity": {"name": "", "email": "nope"}, "positioning": {}, "skills": {"Bad Key": "x"}, "experience": [{"company": "A"}], "preferences": {"experience_years": "3"}}
    errors = profile.validate_profile(bad)
    assert any("identity.name" in e for e in errors) and any("email" in e for e in errors) and any("summary" in e for e in errors)
    assert any("snake_case" in e for e in errors) and any("experience[0].title" in e for e in errors) and any("experience_years" in e for e in errors)


def test_normalise_draft_fills_shape_and_flags_unknown_preferences():
    raw = {"identity": {"name": "Ada Lovelace", "email": "ada@example.com", "phone": "+44 1", "location": "London, UK", "headline": "Backend Engineer"},
           "positioning": {"summary": "I build APIs."}, "skills": {"Back End": ["Go", ""], "Cloud": ["AWS"]},
           "experience": [{"company": "Analytical", "title": "Engineer", "start": 2021, "end": None, "bullets": ["Shipped X", " "]}, {"company": "Old", "title": "Intern", "start": "2019-06", "end": "2020-12"}],
           "education": [{"school": "Uni", "degree": "BSc, Mathematics", "end": 2019}], "projects": [{"name": "Engine", "summary": "..."}, {"summary": "nameless"}], "preferences": {}}
    draft = profile.normalise_draft(raw, profile.load(), "data/uploads/r.pdf")
    assert draft["identity"]["first_name"] == "Ada" and draft["identity"]["last_name"] == "Lovelace"
    assert set(draft["skills"]) == {"back_end", "cloud"} and draft["skills"]["back_end"] == ["Go"]
    assert draft["experience"][0]["current"] is True and draft["experience"][0]["start"] == "2021" and draft["experience"][0]["bullets"] == ["Shipped X"]
    assert draft["experience"][1]["current"] is False
    assert len(draft["projects"]) == 1 and draft["resume_base"] == "data/uploads/r.pdf"
    assert draft["preferences"]["expected_salary_lpa"] is None and "_review" in draft["preferences"]
    assert draft["preferences"]["experience_years"] > 3          # computed from the dates when the model gave none
    assert profile.validate_profile(draft) == []


def test_resume_text_reads_docx(tmp_path):
    import zipfile
    path = tmp_path / "cv.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>Ada Lovelace</w:t></w:r></w:p><w:p><w:r><w:t>Backend</w:t><w:tab/><w:t>Engineer</w:t></w:r></w:p></w:body></w:document>')
    text = profile.resume_text(path)
    assert "Ada Lovelace" in text and "Backend\tEngineer" in text
    with pytest.raises(ValueError):
        profile.resume_text(tmp_path / "cv.exe")


def test_profile_api_roundtrip_and_backup(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app.main import app
    import yaml
    client = TestClient(app)
    before = client.get("/api/profile").json()
    assert before["identity"]["name"] and before["answers_count"] > 0 and "stale_applications" in before
    data = yaml.safe_load(before["yaml"]); data["identity"]["headline"] = "Changed headline"
    r = client.put("/api/profile", json={"yaml": yaml.safe_dump(data)})
    assert r.status_code == 200 and r.json()["fingerprint"] != before["fingerprint"] and r.json()["backup"]
    assert profile.load()["identity"]["headline"] == "Changed headline"
    assert client.put("/api/profile", json={"yaml": "identity: {name: x}"}).status_code == 422
    assert client.put("/api/profile", json={"yaml": "identity: [unclosed"}).status_code == 422
    assert client.put("/api/profile/answers", json={"yaml": "- match: '('\n  answer: x"}).status_code == 422
    assert client.put("/api/profile/answers", json={"yaml": before["answers_yaml"]}).status_code == 200
    # upload path: extraction is mocked, the file is stored locally, nothing is saved
    from backend.core import llm
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"identity": {"name": "Ada Lovelace", "email": "ada@example.com", "phone": "1", "location": "London, UK", "headline": "Eng"},
                                                              "positioning": {"summary": "s"}, "skills": {"cloud": ["AWS"]}, "experience": [{"company": "A", "title": "E", "start": "2020-01"}], "preferences": {}})
    r = client.post("/api/profile/resume", files={"file": ("cv.txt", b"Ada Lovelace " * 40, "text/plain")})
    assert r.status_code == 200 and "Ada Lovelace" in r.json()["yaml"] and r.json()["errors"] == []
    assert profile.load()["identity"]["name"] != "Ada Lovelace"
    assert client.post("/api/profile/resume", files={"file": ("cv.exe", b"x" * 300, "application/octet-stream")}).status_code == 422
    assert client.put("/api/profile", json={"yaml": before["yaml"]}).status_code == 200      # restore for later tests


# ---------------------------------------------------------------- batch 20: queue repairs
def test_document_fingerprint_ignores_answer_bank_changes(tmp_path):
    """Editing a screening answer must not invalidate resumes: no word of them can have changed."""
    before, before_answers = profile.fingerprint(), profile.answers_fingerprint()
    rows = profile.answers()
    profile.save_answers(rows + [{"match": "a very specific new question", "answer": "Yes"}])
    assert profile.fingerprint() == before                      # documents stay valid
    assert profile.answers_fingerprint() != before_answers      # but the answer bank is versioned
    prof = profile.load(); prof["identity"]["headline"] = "New headline"
    profile.save(prof)
    assert profile.fingerprint() != before                      # changing the facts does invalidate them


def test_capability_answers_only_claim_what_the_profile_lists():
    prof = profile.load()
    prof["skills"] = {"backend": ["Docker", "PostgreSQL"]}
    prof["experience"] = [{"company": "A", "title": "E", "start": "2022-01", "current": True, "stack": ["RAG"], "bullets": []}]
    prof["capabilities"] = {"ai agents": True, "azure": False}
    assert profile.answer_for("Have you worked with Docker?", prof) == "Yes"
    assert profile.answer_for("Do you have experience with RAG?", prof) == "Yes"
    assert profile.answer_for("Have you developed AI Agents?", prof) == "Yes"
    assert profile.answer_for("Do you have experience with Azure?", prof) == "No"      # explicit No is honoured
    assert profile.answer_for("Have you used Kubernetes?", prof) is None               # unlisted stays for the human
    assert profile.answer_for("Have you worked with Snowflake in a production environment?", prof) is None
    # the technology can sit behind a verb phrase: "experience deploying applications on Azure"
    assert profile.answer_for("Do you have experience deploying applications on Azure?", prof) == "No"
    assert profile.answer_for("Do you have experience working with Docker?", prof) == "Yes"
    assert profile.answer_for("Do you have experience building applications in Rust?", prof) is None


def test_hn_apply_target_reads_the_link_out_of_the_post():
    from backend.core.collectors.public_apis import apply_target
    post = "Acme | Engineer | Remote\nWe build things.\nApply: https://jobs.ashbyhq.com/acme/123 or email jobs@acme.com"
    assert apply_target(post) == ("https://jobs.ashbyhq.com/acme/123", "jobs@acme.com")
    assert apply_target("No links here at all") == (None, None)
    assert apply_target("Discussion https://news.ycombinator.com/item?id=1 and https://acme.com/careers/")[0] == "https://acme.com/careers/"


def test_queue_repairs_are_reportonly_until_applied():
    from backend.app.models import Job, Application
    from backend.core import ops
    with session() as db:
        j = Job(dedupe_key="hn1", source="hackernews", company="Acme", title="Engineer", url="https://news.ycombinator.com/item?id=1",
                apply_url="https://news.ycombinator.com/item?id=1", description="Acme | Engineer\nApply at https://acme.com/careers/")
        db.add(j); db.flush()
        db.add(Application(job_id=j.id, platform="web", method="external", status="needs_human", error="Hacker News post: no application form", answers={}))
        stuck = Job(dedupe_key="q1", source="ashby", company="Beta", title="Engineer", url="https://b.example/1", country="India")
        db.add(stuck); db.flush()
        db.add(Application(job_id=stuck.id, platform="ashby", method="ats_form", status="needs_human",
                           error="Unanswered fields: Middle Name | Language Preference *", answers={}))
    assert len(ops.link_apply_urls()["linked"]) == 1
    with session() as db:
        assert db.query(Application).filter_by(status="needs_human").count() == 2      # reporting changed nothing
    ops.link_apply_urls(apply=True)
    ops.recheck_answers(apply=True)
    with session() as db:
        hn = db.query(Application).join(Job).filter(Job.source == "hackernews").one()
        assert hn.status == "pending_review" and hn.method == "ats_form" and hn.job.apply_url == "https://acme.com/careers/"
        other = db.query(Application).join(Job).filter(Job.source == "ashby").one()
        assert other.status == "pending_review" and other.error is None


def test_clear_stuck_protects_possibly_sent_applications():
    """Deleting the record is the only thing that remembers we touched an employer, so an uncertain submit must not
    let the same job be applied to again."""
    from backend.app.models import Job, Application
    from backend.core import ops
    with session() as db:
        for key, error in (("a", "CAPTCHA present; solve it manually"), ("b", "no confirmation text detected after submit")):
            j = Job(dedupe_key=key, source="ashby", company=key, title="Engineer", url=f"https://x/{key}", status="queued")
            db.add(j); db.flush()
            db.add(Application(job_id=j.id, platform="ashby", method="ats_form", status="needs_human", error=error, answers={}))
        keep = Job(dedupe_key="c", source="ashby", company="c", title="Engineer", url="https://x/c", status="queued")
        db.add(keep); db.flush()
        db.add(Application(job_id=keep.id, platform="ashby", method="ats_form", status="pending_review", answers={}))
    assert ops.clear_stuck()["deleted"] == 2
    with session() as db:
        assert db.query(Application).count() == 3               # report-only changed nothing
    ops.clear_stuck(apply=True)
    with session() as db:
        assert db.query(Application).count() == 1               # the reviewable one survives
        assert db.query(Job).filter_by(dedupe_key="a").one().status == "skipped"
        assert db.query(Job).filter_by(dedupe_key="b").one().status == "applied"    # never offered again


# ---------------------------------------------------------------- batch 21: question inbox and chat
def test_questions_group_rewordings_and_drop_stray_labels():
    from backend.core import questions
    assert questions.normalise("Notice period *") == questions.normalise("notice period?")
    assert questions.record(["Notice period *", "Notice period?", "Notice Period"], "Acme") == 1
    assert questions.record(["notice period"], "Beta") == 0
    q = questions.open_questions()[0]
    assert q["times_seen"] == 4 and set(q["companies"]) == {"Acme", "Beta"}
    # stray labels a form filler picks up by mistake are not questions
    assert questions.record(["Benzinga", "q", "YES", "Facebook"], "Benzinga") == 0
    assert questions.record(["Which team interests you? (choices: Platform, Product)"], "Acme") == 1
    assert questions.open_questions()[-1]["options"] == ["Platform", "Product"]


def test_chat_stores_each_kind_of_answer_where_it_belongs(monkeypatch):
    from backend.core import questions, llm
    questions.record(["Do you have experience with Kubernetes?"], "Acme")
    questions.record(["What is your current notice period in days?"], "Beta")
    questions.record(["Please confirm you have read our GDPR policy"], "Gamma")
    find = lambda word: next(q["id"] for q in questions.open_questions() if word in q["text"])

    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"understood": True, "reply": "Noted.", "answer": "No",
                                                               "store": "capabilities", "capability": "kubernetes", "yes": False, "skip": False})
    questions.apply_answer(find("Kubernetes"), questions.interpret({"text": "x"}, "no"))
    assert profile.load()["capabilities"]["kubernetes"] is False
    assert profile.answer_for("Have you used Kubernetes?") == "No"

    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"understood": True, "reply": "Noted.", "answer": "30",
                                                               "store": "answers", "match": "notice period.*days", "skip": False})
    questions.apply_answer(find("notice period"), questions.interpret({"text": "x"}, "30 days"))
    assert profile.answer_for("What is your current notice period in days?") == "30"

    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"understood": True, "reply": "Noted.", "answer": "I confirm I have read it.",
                                                               "store": "declarations", "skip": False})
    questions.apply_answer(find("GDPR"), questions.interpret({"text": "x"}, "yes I read it"))
    assert profile.answer_for("please confirm you have read our gdpr policy") == "I confirm I have read it."
    assert questions.summary() == {"open": 0, "answered": 3, "skipped": 0}


def test_chat_endpoint_asks_again_when_the_reply_is_unclear(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app.main import app
    from backend.core import questions, llm
    questions.record(["What is your permanent address?"], "Acme")
    qid = questions.open_questions()[0]["id"]
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"understood": False, "reply": "Which address should I use?", "skip": False})
    client = TestClient(app)
    r = client.post("/api/questions/chat", json={"question_id": qid, "message": "not sure"}).json()
    assert r["saved"] is None and "Which address" in r["reply"]
    assert r["summary"]["open"] == 1 and r["next"]["id"] == qid          # still waiting on them
    assert [t["role"] for t in questions.history()][-2:] == ["user", "assistant"]
    assert client.post("/api/questions/chat", json={"question_id": qid, "message": "  "}).status_code == 422
    assert client.post(f"/api/questions/{qid}/skip").json()["summary"]["skipped"] == 1


def test_answering_questions_never_invalidates_prepared_documents(monkeypatch):
    """The whole point of the inbox is answering mid-flight; it must not force 100 resumes to be rewritten."""
    from backend.core import questions, llm
    before = profile.fingerprint()
    questions.record(["Do you have experience with Terraform?"], "Acme")
    qid = questions.open_questions()[0]["id"]
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"understood": True, "reply": "ok", "answer": "Yes",
                                                              "store": "capabilities", "capability": "terraform", "yes": True, "skip": False})
    questions.apply_answer(qid, questions.interpret({"text": "x"}, "yes"))
    assert profile.fingerprint() == before


def test_reposted_role_is_not_prepared_twice(monkeypatch):
    """Boards repost the same vacancy under a new URL; two applications to one role reads as careless."""
    from backend.app.models import Job, Application
    from backend.core import pipeline, llm, resume, runner
    with session() as db:
        for suffix in ("", "-1"):
            db.add(Job(dedupe_key=f"k{suffix}", source="weworkremotely", company="Huzzle", title="Full-Stack Developer (Python, React, AI)",
                       url=f"https://w.example/huzzle-full-stack{suffix}", location="Anywhere", country="Remote (worldwide)",
                       description="d" * 400, eligible=True, fit_score=90, status="scored"))
        db.flush()          # this session does not autoflush
        ids = [j.id for j in db.query(Job).order_by(Job.id).all()]
    tailored = {"headline": "h", "summary": "s", "skills_order": [], "experience_bullets": {}, "cover_note": "c", "why_company": "w"}
    monkeypatch.setattr(llm, "complete_json", lambda task, *a, **k: tailored if task == "tailor" else {"ok": True, "violations": []})
    monkeypatch.setattr(resume, "render_pdf", lambda *a, **k: "data/artifacts/x.pdf")
    ctx = runner.RunContext("pipeline", "test")
    assert pipeline.tailor(ctx, ids[0]) is not None
    assert pipeline.tailor(ctx, ids[1]) is None                     # the repost is skipped, not prepared
    with session() as db:
        assert db.query(Application).count() == 1
        assert db.get(Job, ids[1]).status == "skipped"
        assert "already prepared" in db.get(Job, ids[1]).eligibility_reason


def test_projects_always_survive_tailoring_and_trimming():
    """The candidate's own projects are not a tailoring lever: no overlay and no page-fitting pass may drop them."""
    from backend.core import resume
    prof = profile.load()
    prof["projects"] = [{"name": "Ignite AI Backend", "summary": "LLM backend project (repo URL TODO from user)"},
                        {"name": "DevHive", "summary": "Developer tooling project"}]
    profile.save(prof)
    names = [p["name"] for p in resume.build_context()["projects"]]
    assert names == ["Ignite AI Backend", "DevHive"]
    assert "TODO" not in resume.build_context()["projects"][0]["summary"]      # the note to self is stripped, not the project
    trimmed = resume.build_context({"_max_bullets_current": 2, "_max_bullets_old": 1, "_short_summary": True})
    assert [p["name"] for p in trimmed["projects"]] == names                   # still there at the densest setting
    assert all(len(e["bullets"]) <= 2 for e in trimmed["experience"])          # bullets are what gets trimmed instead
    assert "Projects" in resume.render_html()


def test_resume_page_limit_comes_from_config(monkeypatch):
    from backend.core import config, resume
    assert resume.max_pages() == 2
    monkeypatch.setattr(config, "load", lambda: {"resume": {"max_pages": 1}})
    assert resume.max_pages() == 1
    monkeypatch.setattr(config, "load", lambda: {"resume": {"max_pages": 99}})
    assert resume.max_pages() == 2                                            # nonsense falls back, never unlimited


def test_clearing_the_inbox_never_undoes_stored_answers(monkeypatch):
    from backend.core import questions, ops, llm
    questions.record(["Do you have experience with Terraform?"], "Acme")
    questions.record(["What is your permanent address?"], "Beta")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"understood": True, "reply": "ok", "answer": "Yes",
                                                              "store": "capabilities", "capability": "terraform", "yes": True, "skip": False})
    first = next(q["id"] for q in questions.open_questions() if "Terraform" in q["text"])
    questions.apply_answer(first, questions.interpret({"text": "x"}, "yes"))
    questions.log_turn("user", "yes")
    assert ops.clear_questions()["questions"] == 1                     # report-only, and answered ones are left alone
    ops.clear_questions(apply=True)
    assert questions.summary() == {"open": 0, "answered": 1, "skipped": 0}
    assert questions.history() == []
    assert profile.answer_for("Have you used Terraform?") == "Yes"     # the answer itself survives
    ops.clear_questions(apply=True, answered=True)
    assert questions.summary()["answered"] == 0
    assert profile.answer_for("Have you used Terraform?") == "Yes"     # still in force even with no record of the question


def test_browser_opens_a_tab_when_chrome_has_none(monkeypatch):
    """Closing the last window leaves Chrome alive with no context; attaching then fails, so a blank tab is requested."""
    import json as _json, io
    from backend.core import browser
    state = {"targets": [], "asked": []}

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith("/json/version"): return Response(_json.dumps({"webSocketDebuggerUrl": "ws://x"}).encode())
        if "/json/list" in url: return Response(_json.dumps(state["targets"]).encode())
        if "/json/new" in url:
            state["asked"].append(getattr(request, "method", "GET"))
            state["targets"] = [{"type": "page", "url": "about:blank"}]
            return Response(b"{}")
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    captured = {}

    class FakeBrowser:
        contexts = ["context"]
        def close(self): captured["closed"] = True

    class FakePlaywright:
        chromium = type("C", (), {"connect_over_cdp": staticmethod(lambda url: FakeBrowser())})()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(browser, "sync_playwright", lambda: FakePlaywright())

    with browser.open_context("ats") as ctx:
        assert ctx == "context"
    assert state["asked"] and state["targets"], "a blank tab should have been requested"
    assert captured.get("closed")

    # a Chrome that still exposes no context is reported, not silently used
    FakeBrowser.contexts = []
    state["targets"] = [{"type": "page", "url": "about:blank"}]
    with pytest.raises(RuntimeError, match="no browser context"):
        with browser.open_context("ats"):
            pass


class FakePage:
    """Minimal page stand-in for guard tests: body text plus visible controls by label."""
    def __init__(self, body, controls=()):
        self._body, self._controls = body, [c.lower() for c in controls]
    def inner_text(self, sel, timeout=None): return self._body
    def locator(self, selector):
        import re as _re
        wanted = _re.findall(r"has-text\('([^']*)'\)", selector)
        page = self
        class L:
            def filter(self, **k): return self
            def count(self): return sum(1 for w in wanted if w.lower() in page._controls)
        return L()


def test_payment_pages_stop_the_application():
    """A board that routes Apply through a paid plan must never be filled in or clicked through."""
    from backend.core.apply import forms
    wwr = ("Step 3 of 3. Billed now $29.95. You'll be charged $29.95 on September 8, 2026 and monthly for the remaining "
           "months of this 12-month commitment. The plan auto-renews annually unless cancelled. Payment Method. "
           "I agree to the Terms & Conditions and the renewal terms above.")
    assert forms.payment_wall(FakePage(wwr))
    assert forms.payment_wall(FakePage("Choose your plan. $14.95/month subscription. Subtotal $29.95"))
    assert forms.payment_wall(FakePage("Upgrade to premium for $9.99 per month and unlock priority support. Order summary"))
    assert forms.payment_wall(FakePage("Complete your purchase for $49", controls=["Pay now"]))
    # ordinary application pages are untouched
    assert forms.payment_wall(FakePage("Apply for this position. Upload your resume. Expected salary $120,000 per year.")) is None
    assert forms.payment_wall(FakePage("Tell us about yourself. Full name, email, phone. Submit application")) is None
    assert forms.payment_wall(FakePage("What are your salary expectations? We offer $150,000/year plus equity.")) is None


def test_paywalled_board_becomes_an_outreach_target_not_a_form_submission(monkeypatch):
    """A board that charges to apply through it must never reach the form worker; the route is a person instead."""
    from backend.app.models import Job, Application, Contact, Outreach
    from backend.core import pipeline, llm, resume, config, runner, ops
    from backend.core.apply.worker import ATSApplySkill
    monkeypatch.setattr(config, "load", lambda: {**cfg_base(), "outreach_only_sources": ["weworkremotely"]})
    with session() as db:
        j = Job(dedupe_key="wwr1", source="weworkremotely", company="Evaboot", title="Agentic Python Engineer",
                url="https://weworkremotely.com/remote-jobs/evaboot", description="d" * 400, eligible=True, fit_score=88, status="scored")
        db.add(j); db.flush(); jid = j.id
    tailored = {"headline": "h", "summary": "s", "skills_order": [], "experience_bullets": {}, "cover_note": "c", "why_company": "w"}
    monkeypatch.setattr(llm, "complete_json", lambda task, *a, **k: tailored if task == "tailor" else {"ok": True, "violations": []})
    monkeypatch.setattr(resume, "render_pdf", lambda *a, **k: "data/artifacts/x.pdf")
    ctx = runner.RunContext("pipeline", "test")
    aid = pipeline.tailor(ctx, jid)
    with session() as db:
        assert db.get(Application, aid).method == "outreach"
        db.get(Application, aid).status = "approved"
    assert aid not in ATSApplySkill(ctx)._pending(50)          # the form worker never sees it

    # reaching a person on LinkedIn is what completes it
    with session() as db:
        c = Contact(company="Evaboot", name="Sam", linkedin_url="https://linkedin.com/in/sam"); db.add(c); db.flush()
        o = Outreach(job_id=jid, contact_id=c.id, channel="linkedin_connect", step=1, body="hi", status="sending"); db.add(o); db.flush(); oid = o.id
    from backend.core.skills.linkedin_people import LinkedInPeopleSkill
    LinkedInPeopleSkill(ctx)._set(oid, "sent")
    with session() as db:
        app = db.get(Application, aid)
        assert app.status == "submitted" and "LinkedIn invitation sent" in app.confirmation_text
        assert app.job.status == "applied"


def cfg_base():
    import yaml, pathlib
    from backend.core import config
    return yaml.safe_load(pathlib.Path(config.CONFIG_PATH).read_text())


def test_login_detection_tries_each_selector_separately():
    """A CSS list cannot contain a text= engine selector; mixing them threw and made the platform look logged out
    no matter how many times the user signed in."""
    from backend.core import browser
    for key, marker in browser.LOGIN_MARKERS.items():
        selectors = marker["logged_in_selector"]
        assert isinstance(selectors, list), f"{key}: selectors must be a list"
        for selector in selectors:
            engine_parts = [p for p in selector.split(",") if "=" in p and p.strip().split("=")[0].strip() in ("text", "xpath")]
            assert not (len(selector.split(",")) > 1 and engine_parts), f"{key}: '{selector}' mixes CSS with an engine selector"

    class Locator:
        def __init__(self, visible): self._visible = visible
        def filter(self, **k): return self
        def count(self): return 1 if self._visible else 0

    class Page:
        url = "https://cutshort.io/profile/all-jobs"
        def __init__(self, matching): self.matching, self.tried = matching, []
        def locator(self, selector):
            self.tried.append(selector)
            return Locator(selector == self.matching)
        def wait_for_timeout(self, ms): pass

    class LoginPage(Page):
        url = "https://cutshort.io/?redirect_url=/profile"
    assert not browser.is_logged_in(LoginPage("text=Dashboard"), "cutshort")   # a login URL is decisive


def test_popups_are_closed_but_never_bought():
    """Upsell popups cover the results. Close them; never press the button that costs money."""
    from backend.core import browser
    clicked = []

    class Button:
        def __init__(self, label, present=True): self.label, self.present = label, present
        def filter(self, **k): return self
        def count(self): return 1 if self.present else 0
        @property
        def first(self): return self
        def inner_text(self, timeout=None): return self.label
        def get_attribute(self, name): return ""
        def click(self, timeout=None): clicked.append(self.label)

    class Overlay:
        def __init__(self, buttons): self.buttons = buttons
        def locator(self, selector):
            for key, button in self.buttons.items():
                if key in selector: return button
            return Button("", present=False)

    class Page:
        def __init__(self, buttons): self.overlay = Overlay(buttons); self.keys = []
        def locator(self, selector):
            if "role='dialog'" in selector: 
                page = self
                class Overlays:
                    def filter(self, **k): return self
                    def count(self): return 1
                    @property
                    def first(self): return page.overlay
                return Overlays()
            return self.overlay.locator(selector)
        def wait_for_timeout(self, ms): pass
        keyboard = type("K", (), {"press": lambda self, k: None})()

    # a close control is used
    page = Page({"aria-label='Close'": Button("")})
    assert browser.dismiss_overlay(page) and clicked == [""]
    clicked.clear()

    # a close control whose label is really a purchase is skipped, and 'maybe later' is used instead
    page = Page({"aria-label='Close'": Button("Unlock Your Offer Now"), "maybe later": Button("Maybe later")})
    assert browser.dismiss_overlay(page)
    assert clicked == ["Maybe later"], clicked
    clicked.clear()

    # nothing safe to press: Escape, and no purchase is made
    page = Page({"aria-label='Close'": Button("Upgrade to Turbo")})
    browser.dismiss_overlay(page)
    assert clicked == []


def test_a_login_pause_does_not_discard_the_scoring_of_everything_collected():
    """One expired login on the last platform must not throw away the work of the ones before it: scoring and
    preparing touch no browser, so they still run over whatever was collected."""
    from backend.core import fullrun, registry, pipeline
    from backend.core.runner import RunContext
    ran = []
    ctx = RunContext("fullrun", "test")

    def collector(name):
        def go(c):
            ran.append(f"discover:{name}")
            if name == "cutshort": c.set_status("paused_for_human")     # login window opened, nobody signed in
        return go

    original = dict(registry.COLLECTORS)
    registry.COLLECTORS.clear()
    registry.COLLECTORS.update({"greenhouse": collector("greenhouse"), "cutshort": collector("cutshort"), "naukri": collector("naukri")})
    try:
        import backend.core.hydration as hydration
        real_score, real_prepare, real_hydrate = pipeline.score, pipeline.prepare, hydration.hydrate
        pipeline.score = lambda c, **k: ran.append(f"score:{sorted(k.get('sources') or [])}")
        pipeline.prepare = lambda c, **k: ran.append("prepare")
        hydration.hydrate = lambda c, **k: ran.append("hydrate")
        try:
            fullrun.all_platforms(ctx, apply=False, only=["greenhouse", "cutshort", "naukri"])
        finally:
            pipeline.score, pipeline.prepare, hydration.hydrate = real_score, real_prepare, real_hydrate
    finally:
        registry.COLLECTORS.clear(); registry.COLLECTORS.update(original)

    assert "discover:greenhouse" in ran
    assert "discover:naukri" not in ran            # the browser is held for the human, so later platforms wait
    assert "score:['cutshort', 'greenhouse']" in ran or "score:['greenhouse']" in ran
    assert "prepare" in ran, "preparing must still run over what was collected"


def test_user_stop_really_stops_everything():
    from backend.core import fullrun, registry, pipeline
    from backend.core.runner import RunContext
    ran = []
    ctx = RunContext("fullrun", "test")
    original = dict(registry.COLLECTORS)
    registry.COLLECTORS.clear()
    registry.COLLECTORS.update({"greenhouse": lambda c: (ran.append("discover"), ctx.stop_flag.set())})
    try:
        real_score = pipeline.score
        pipeline.score = lambda c, **k: ran.append("score")
        try:
            fullrun.all_platforms(ctx, apply=False, only=["greenhouse"])
        finally:
            pipeline.score = real_score
    finally:
        registry.COLLECTORS.clear(); registry.COLLECTORS.update(original)
    assert ran == ["discover"], "pressing Stop must not start a new stage"


def test_a_rejected_tailor_answer_is_retried_not_lost(monkeypatch):
    """A dropped field or a mis-keyed employer is the model slipping; quoting the complaint back recovers the job."""
    from backend.app.models import Job, Application
    from backend.core import pipeline, llm, resume, runner
    with session() as db:
        j = Job(dedupe_key="t1", source="greenhouse", company="Acme", title="AI Engineer", url="https://x/1",
                description="d" * 400, eligible=True, fit_score=90, status="scored")
        db.add(j); db.flush(); jid = j.id
    good = {"headline": "h", "summary": "s", "skills_order": [], "experience_bullets": {}, "cover_note": "c", "why_company": "w"}
    calls = {"tailor": 0}
    prompts = []

    def fake(task, prompt, *a, **k):
        if task != "tailor": return {"ok": True, "violations": []}
        calls["tailor"] += 1
        prompts.append(prompt)
        if calls["tailor"] == 1: return {k: v for k, v in good.items() if k != "cover_note"}   # model drops a field
        return good

    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setattr(resume, "render_pdf", lambda *a, **k: "data/artifacts/x.pdf")
    ctx = runner.RunContext("pipeline", "test")
    aid = pipeline.tailor(ctx, jid)
    assert aid is not None and calls["tailor"] == 2
    assert "REJECTED" in prompts[1] and "cover_note" in prompts[1]
    with session() as db:
        assert db.get(Application, aid).cover_note == "c"

    # a model that never complies still fails, rather than queueing something unchecked
    calls["tailor"] = 0
    monkeypatch.setattr(llm, "complete_json", lambda task, *a, **k: ({"ok": True, "violations": []} if task != "tailor"
                                                                     else {k: v for k, v in good.items() if k != "cover_note"}))
    with session() as db:
        j2 = Job(dedupe_key="t2", source="greenhouse", company="Beta", title="AI Engineer", url="https://x/2",
                 description="d" * 400, eligible=True, fit_score=90, status="scored")
        db.add(j2); db.flush(); jid2 = j2.id
    with pytest.raises(Exception):
        pipeline.tailor(ctx, jid2)


def test_parallel_preparation_still_catches_a_repost(monkeypatch):
    """Preparation runs five jobs at once. A company listing one role in three cities had all three checked before any
    existed, so all three were queued; the check has to happen inside the serialised write too."""
    from backend.app.models import Job, Application
    from backend.core import pipeline, llm, resume, runner
    with session() as db:
        for city in ("San Francisco", "Seattle", "New York"):
            db.add(Job(dedupe_key=f"brex-{city}", source="greenhouse", company="brex",
                       title="Software Engineer, Forward Deployed Agent Builder", url=f"https://brex/{city}",
                       location=city, description="d" * 400, eligible=True, fit_score=88, status="scored"))
        db.flush()
        ids = [j.id for j in db.query(Job).order_by(Job.id).all()]
    good = {"headline": "h", "summary": "s", "skills_order": [], "experience_bullets": {}, "cover_note": "c", "why_company": "w"}
    monkeypatch.setattr(llm, "complete_json", lambda task, *a, **k: good if task == "tailor" else {"ok": True, "violations": []})
    monkeypatch.setattr(resume, "render_pdf", lambda *a, **k: "data/artifacts/x.pdf")
    ctx = runner.RunContext("pipeline", "test")

    # simulate the race: every job passes the early check because none has been written yet
    import backend.core.pipeline as pl
    results = []
    for jid in ids:
        try: results.append(pl.tailor(ctx, jid))
        except Exception as e: results.append(e)
    with session() as db:
        assert db.query(Application).count() == 1, "only one application for a role listed in three cities"
        skipped = db.query(Job).filter(Job.status == "skipped").all()
        assert len(skipped) == 2 and all("already prepared" in j.eligibility_reason for j in skipped)


def test_approve_all_respects_filters_and_the_safety_gate():
    """Bulk approval is still approval: it may not wave through a failed fact-check, a stale document, or an
    application that stopped on a CAPTCHA and needs a human to say why retrying is safe."""
    from fastapi.testclient import TestClient
    from backend.app.main import app as api_app
    from backend.app.models import Job, Application
    with session() as db:
        def add(company, country, status, factcheck=True, stale=False):
            j = Job(dedupe_key=company, source="greenhouse", company=company, title="AI Engineer",
                    url=f"https://x/{company}", country=country, eligible=True, fit_score=90)
            db.add(j); db.flush()
            answers = {"factcheck": {"ok": factcheck, "violations": [] if factcheck else [{"claim": "c", "why": "w"}]},
                       "profile_hash": "stale" if stale else profile.fingerprint()}
            db.add(Application(job_id=j.id, platform="greenhouse", method="ats_form", status=status, answers=answers))
        add("good-in", "India", "pending_review")
        add("good-us", "United States", "pending_review")
        add("bad-factcheck", "India", "pending_review", factcheck=False)
        add("stale-docs", "India", "pending_review", stale=True)
        add("needs-me", "India", "needs_human")

    client = TestClient(api_app)
    r = client.post("/api/applications/approve-all", json={"country": "India"}).json()
    assert r["approved"] == 1, r                      # only the sound Indian one
    assert r["blocked_total"] == 2 and {b["company"] for b in r["blocked"]} == {"bad-factcheck", "stale-docs"}
    with session() as db:
        by_company = {a.job.company: a.status for a in db.query(Application).all()}
        assert by_company["good-in"] == "approved"
        assert by_company["good-us"] == "pending_review"      # outside the filter
        assert by_company["needs-me"] == "needs_human"        # never swept up
        assert by_company["bad-factcheck"] == "pending_review"

    assert client.post("/api/applications/approve-all", json={}).json()["approved"] == 1   # the US one


def test_people_are_ranked_across_roles_not_ten_recruiters():
    from backend.core.skills.linkedin_people import LinkedInPeopleSkill, role_of
    from backend.core.runner import RunContext
    assert role_of("Senior Technical Recruiter") == "recruiter"
    assert role_of("Engineering Manager, AI Platform") == "hiring manager"
    assert role_of("Co-Founder & CTO") == "founder"
    assert role_of("Head of People") == "people team"
    assert role_of("Marketing Lead") == "other"

    skill = LinkedInPeopleSkill(RunContext("skill", "test"))
    cards = [("R1", "Technical Recruiter at Acme", "https://li/in/r1"),
             ("R2", "Recruiter at Acme", "https://li/in/r2"),
             ("R3", "Talent Acquisition at Acme", "https://li/in/r3"),
             ("M1", "Engineering Manager at Acme", "https://li/in/m1"),
             ("F1", "Co-Founder at Acme", "https://li/in/f1"),
             ("E1", "Software Engineer at Acme", "https://li/in/e1")]
    skill._pick_cards = lambda page, company, on_company_page: cards
    skill.company_slug = lambda page, company: None
    skill.take = lambda action, ref=None: True
    skill.guard = lambda page: None

    class Page:
        def goto(self, *a, **k): pass
        def wait_for_timeout(self, ms): pass
    import backend.core.humanize as hz
    real_pause, real_scroll = hz.pause, hz.human_scroll
    hz.pause = lambda *a, **k: None; hz.human_scroll = lambda *a, **k: None
    try:
        people = skill.find_people(Page(), "Acme", want=4)
    finally:
        hz.pause, hz.human_scroll = real_pause, real_scroll
    roles = [p[3] for p in people]
    assert len(people) == 4
    assert len(set(roles)) >= 3, f"expected a spread of roles, got {roles}"
    assert roles[0] == "hiring manager", "a hiring manager should come before a recruiter"
    assert len({p[2] for p in people}) == 4, "no duplicate profiles"


def test_one_message_per_person_across_many_roles_at_one_company(monkeypatch):
    """Twelve open roles at one company used to mean twelve notes to the same recruiter."""
    from backend.app.models import Job, Contact, Outreach
    from backend.core.skills.linkedin_people import LinkedInPeopleSkill
    from backend.core.runner import RunContext
    with session() as db:
        for i, score in enumerate((70, 95, 80)):
            j = Job(dedupe_key=f"oa{i}", source="greenhouse", company="openai", title=f"Engineer {i}",
                    url=f"https://x/{i}", fit_score=score, eligible=True)
            db.add(j); db.flush()
            db.add(Outreach(job_id=j.id, channel="linkedin_connect", step=1, status="pending_review"))
        best = db.query(Job).filter_by(fit_score=95).one().id

    skill = LinkedInPeopleSkill(RunContext("skill", "test"))
    skill.find_people = lambda page, company, want: [("A", "Recruiter at openai", "https://li/in/a", "recruiter"),
                                                     ("B", "Engineering Manager at openai", "https://li/in/b", "hiring manager")]
    skill.draft_note = lambda name, title, job_id: f"Hi {name}, about job {job_id}"
    import backend.core.humanize as hz
    real_pause = hz.pause; hz.pause = lambda *a, **k: None
    try:
        skill.find(page=None, limit=5)
    finally:
        hz.pause = real_pause

    with session() as db:
        live = db.query(Outreach).filter(Outreach.status == "pending_review").all()
        assert len(live) == 2, "one row per person, not one per job"
        assert {db.get(Contact, o.contact_id).linkedin_url for o in live} == {"https://li/in/a", "https://li/in/b"}
        assert all(o.job_id == best for o in live), "each note should reference the best-fitting role"
        stopped = db.query(Outreach).filter(Outreach.status == "stopped").all()
        assert len(stopped) == 1 and "one message per person" in stopped[0].error


def test_emails_are_drafted_per_person_and_only_with_a_real_address(monkeypatch):
    from backend.app.models import Job, Contact, Outreach
    from backend.core.skills.linkedin_people import LinkedInPeopleSkill
    from backend.core.runner import RunContext
    from backend.core import llm
    with session() as db:
        j = Job(dedupe_key="e1", source="greenhouse", company="Acme", title="AI Engineer", url="https://x/1",
                description="d" * 300, fit_score=90, eligible=True)
        db.add(j); db.flush()
        people = [("Ann", "ann@acme.com", "found"), ("Bob", "bob@acme.com", "found"),
                  ("Cara", None, None), ("Dan", "careers@acme.com", "pattern")]
        for name, email, conf in people:
            c = Contact(company="Acme", name=name, title="Engineering Manager", email=email,
                        email_confidence=conf, linkedin_url=f"https://li/in/{name.lower()}")
            db.add(c); db.flush()
            db.add(Outreach(job_id=j.id, contact_id=c.id, channel="linkedin_connect", step=1, status="pending_review"))
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"subject": "s", "body": "b", "linkedin_note": "n"})
    assert LinkedInPeopleSkill(RunContext("skill", "t")).email_the_found() == 2
    with session() as db:
        emailed = {db.get(Contact, o.contact_id).name for o in db.query(Outreach).filter_by(channel="email").all()}
        assert emailed == {"Ann", "Bob"}, emailed        # no address, or a guessed one, means no email row
        assert all(o.status == "pending_review" for o in db.query(Outreach).filter_by(channel="email").all())


def test_weekly_cap_stops_sending_before_linkedin_does(monkeypatch):
    """LinkedIn allows ~100 invitations per rolling week and counts from your first one, so a daily cap alone lets the
    system trip the real limit in three days."""
    from datetime import datetime, timedelta
    from backend.app.models import ActionLog
    from backend.core import humanize, config
    monkeypatch.setattr(config, "load", lambda: {"caps": {"linkedin": {"connects": 15}},
                                                 "weekly_caps": {"linkedin": {"connects": 20}},
                                                 "humanize": {}})
    # yesterday's sends still count against the rolling week
    with session() as db:
        for i in range(18):
            db.add(ActionLog(platform="linkedin", action="connects", ts=datetime.utcnow() - timedelta(days=2)))
    assert humanize.take("linkedin", "connects", "a")      # 19
    assert humanize.take("linkedin", "connects", "b")      # 20
    assert not humanize.take("linkedin", "connects", "c"), "the weekly ceiling must stop it"
    # an action from more than seven days ago has rolled off
    with session() as db:
        old = db.query(ActionLog).limit(5).all()
        for row in old: row.ts = datetime.utcnow() - timedelta(days=9)
    assert humanize.take("linkedin", "connects", "d")


def test_config_rejects_a_daily_cap_larger_than_its_weekly_cap():
    from backend.core import config
    cfg = config.load()
    cfg["caps"]["linkedin"]["connects"] = 100          # a day's cap above the whole week's is nonsense
    cfg["weekly_caps"]["linkedin"]["connects"] = 80
    with pytest.raises(ValueError, match="exceeds weekly"):
        config.validate(cfg)
    cfg["caps"]["linkedin"]["connects"] = 15
    assert config.validate(cfg)


def test_outreach_can_be_viewed_per_channel_and_per_source():
    from fastapi.testclient import TestClient
    from backend.app.main import app as api_app
    from backend.app.models import Job, Contact, Outreach
    with session() as db:
        c = Contact(company="Acme", name="Ann", email="a@acme.com"); db.add(c); db.flush()
        for source, channel in (("greenhouse", "email"), ("greenhouse", "linkedin_connect"),
                                ("linkedin", "linkedin_connect"), ("naukri", "email")):
            j = Job(dedupe_key=f"{source}-{channel}", source=source, company="Acme", title="AI Engineer",
                    url=f"https://x/{source}{channel}")
            db.add(j); db.flush()
            db.add(Outreach(job_id=j.id, contact_id=c.id, channel=channel, step=1, status="pending_review"))
    client = TestClient(api_app)
    facets = client.get("/api/outreach/facets").json()
    assert facets["channel"] == {"email": 2, "linkedin_connect": 2}
    assert facets["pending_by_channel"] == {"email": 2, "linkedin_connect": 2}
    assert facets["source"] == {"greenhouse": 2, "linkedin": 1, "naukri": 1}
    assert client.get("/api/outreach?channel=email").json()["total"] == 2
    assert client.get("/api/outreach?source=greenhouse").json()["total"] == 2
    assert client.get("/api/outreach?channel=email&source=naukri").json()["total"] == 1
    assert client.get("/api/outreach?channel=email&source=linkedin").json()["total"] == 0


def test_people_view_shows_what_actually_reached_each_person():
    from fastapi.testclient import TestClient
    from backend.app.main import app as api_app
    from backend.app.models import Job, Contact, Outreach
    with session() as db:
        j = Job(dedupe_key="p1", source="greenhouse", company="Acme", title="AI Engineer", url="https://x/1")
        db.add(j); db.flush()
        def person(name, company, rows):
            c = Contact(company=company, name=name, title="Recruiter", linkedin_url=f"https://li/in/{name}")
            db.add(c); db.flush()
            for channel, status in rows:
                db.add(Outreach(job_id=j.id, contact_id=c.id, channel=channel, step=1, status=status))
        person("sent-both", "Acme", [("linkedin_connect", "sent"), ("email", "replied")])
        person("only-drafted", "Acme", [("linkedin_connect", "pending_review")])
        person("elsewhere", "Beta", [("email", "bounced")])

    client = TestClient(api_app)
    data = client.get("/api/outreach/people").json()
    assert data["total"] == 3 and data["by_company"] == {"Acme": 2, "Beta": 1}
    by_name = {p["name"]: p for p in data["items"]}
    assert by_name["sent-both"]["linkedin_state"] == "sent" and by_name["sent-both"]["email_state"] == "replied"
    assert by_name["only-drafted"]["linkedin_state"] == "pending_review"
    assert by_name["only-drafted"]["email_state"] is None      # nothing was ever aimed at them by email
    assert by_name["elsewhere"]["email_state"] == "bounced"
    assert by_name["sent-both"]["roles"] == ["AI Engineer"]

    assert client.get("/api/outreach/people?company=Acme").json()["total"] == 2
    assert client.get("/api/outreach/people?q=elsew").json()["total"] == 1


def test_phone_drops_the_country_code_when_the_form_already_asks_for_it():
    """A form with its own +91 selector plus '+91 9711324698' typed in the box gives an unusable number."""
    from backend.core.apply import forms

    class El:
        def __init__(self, detected): self.detected = detected
        def evaluate(self, js): return self.detected
        def get_attribute(self, name): return "tel" if name == "type" else None

    assert forms.phone_value(None, El("widget")) == "9711324698"     # intl-tel-input style flag picker
    assert forms.phone_value(None, El("sibling")) == "9711324698"    # a separate country dropdown next to it
    assert forms.phone_value(None, El("")) == "+91 9711324698"       # a plain phone box keeps the country code
    assert forms.PHONE_LABEL_RX.search("Phone *") and forms.PHONE_LABEL_RX.search("Mobile Number")
    assert not forms.PHONE_LABEL_RX.search("Full name")


def test_a_payments_company_careers_page_is_not_a_checkout():
    """Stripe's careers page says 'payment method' throughout. Stopping there cost a Forward Deployed application."""
    from backend.core.apply import forms

    class Page:
        def __init__(self, body): self.body = body
        def inner_text(self, sel, timeout=None): return self.body
        def locator(self, sel):
            class L:
                def filter(self, **k): return self
                def count(self): return 0
            return L()

    stripe = ("Forward Deployed Engineer, Professional Services. Apply for this job. Resume/CV. Cover letter. "
              "Stripe builds payment method infrastructure; we process payments and handle billing for millions.")
    assert forms.payment_wall(Page(stripe)) is None
    checkout = ("Billed now $29.95. You'll be charged $29.95 and monthly for the remaining months of this "
                "12-month commitment. Payment Method. I agree to the Terms & Conditions and the renewal terms above.")
    assert forms.payment_wall(Page(checkout)) == "billed now"
    # a checkout that also mentions a job still stops: an actual charge outranks the job wording
    assert forms.payment_wall(Page(checkout + " Apply for this job. Upload your resume.")) is not None


def test_choose_option_never_picks_an_untrue_answer(monkeypatch):
    from backend.core.apply import forms
    from backend.core import llm
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"option": "3-5 years", "why": "profile says 3"})
    assert forms.choose_option("Experience", ["0-2 years", "3-5 years", "6+ years"], "3", lambda *a: None) == "3-5 years"
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"option": None, "why": "not true of them"})
    assert forms.choose_option("Clearance", ["Top Secret", "Secret"], None, lambda *a: None) is None
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"option": "Invented option"})
    assert forms.choose_option("Anything", ["A", "B"], None, lambda *a: None) is None   # must come from the list
    assert forms.choose_option("Anything", [], "x", lambda *a: None) is None


def test_internal_wording_never_becomes_a_separate_question():
    from backend.core import questions
    assert questions.strip_internal("Select a verified autocomplete option for How did you hear about us?") == "How did you hear about us?"
    assert questions.record(["Select a verified autocomplete option for How did you hear about us?"], "Acme") == 1
    assert questions.record(["How did you hear about us?"], "Beta") == 0      # the same question, not a second one
    assert questions.open_questions()[0]["times_seen"] == 2


def test_profile_answerable_questions_never_reach_the_person():
    from backend.core import questions
    questions.record(["How did you hear about us?"], "Acme")                    # answer bank has this
    questions.record(["Are you authorized to work in the stated location?"], "Acme")   # depends on the job
    questions.record(["What is your favourite colour?"], "Acme")                # nobody can answer this but them
    resolved = questions.auto_resolve()
    settled = {r["question"] for r in resolved}
    assert "How did you hear about us?" in settled
    open_now = {q["text"] for q in questions.open_questions()}
    assert "Are you authorized to work in the stated location?" in open_now, "authorisation differs per country"
    assert "What is your favourite colour?" in open_now


def test_a_bundle_answer_only_settles_what_it_addresses(monkeypatch):
    from backend.core import questions, llm
    questions.record(["Do you consent to processing your personal information?"], "Brex")
    questions.record(["This role requires three days a week in the office. Do you agree?"], "Brex")
    questions.record(["What is your expected salary in USD?"], "Brex")
    groups = questions.bundles()
    assert {g["theme"] for g in groups} >= {"Consents and agreements", "Salary and notice"}
    assert len(next(g for g in groups if g["theme"] == "Consents and agreements")["ids"]) == 2
    consents = next(g for g in groups if g["theme"] == "Consents and agreements")
    assert "1." in consents["prompt"] and "Brex" in consents["prompt"]

    # the reply covers only the first question of the group
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "answers": [{"n": 1, "answer": "Yes", "store": "declarations"}],
        "unanswered": [2], "reply": "Stored your consent."})
    result = questions.answer_bundle(consents["ids"], "yes I consent")
    assert len(result["stored"]) == 1
    assert result["still_open"], "a question the reply did not address must stay open"
    assert questions.summary()["open"] >= 2
