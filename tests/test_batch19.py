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
