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
