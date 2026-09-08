"""Pipeline stages that need no browser: re-evaluate eligibility, LLM fit scoring, tailoring (+fact check), queueing for review.
Each function takes a RunContext so the dashboard can trigger and watch it."""
from __future__ import annotations
import json, re
from datetime import datetime
from sqlalchemy import or_
from backend.app.db import session
from backend.app.models import Job, Application
from backend.core import config, llm, profile, resume
from backend.core.normalize import eligibility

SCORE_SYSTEM = """You are a strict technical recruiter scoring job fit for ONE candidate. Use only the candidate profile given.
Scoring (0-100): 85+ = near-perfect FDE/Applied-AI match; 70-84 = strong AI/backend match; 50-69 = plausible but stretch (seniority, stack, or domain gap);
<50 = poor (wrong seniority by 2+ levels, unrelated stack, non-engineering, or hard blockers like US-citizen-only / on-site with no sponsorship).
Use the candidate's stated years of experience: 'Senior' roles asking for 2+ more years than they have score at most 65; 'Staff/Principal/Lead/Head' at most 40; internships/new-grad at most 45 for anyone with 2+ years.
Return JSON: {"score": int, "reasons": [3 short strings], "blockers": [strings], "seniority": "junior|mid|senior|staff", "resume_focus": [2-4 profile skills/experiences to emphasise], "keywords": [up to 10 JD keywords the resume should mirror]}"""

TAILOR_SYSTEM = """You tailor ONE candidate's resume and write a short cover note for ONE job. HARD RULES:
- You may only reorder, select, or rephrase facts present in the profile. Never invent employers, titles, dates, numbers, tools, degrees or results.
- Never add a metric that is not in the profile. If a bullet has no metric, keep it without one.
- Keep bullets truthful to the original meaning; you may mirror the job description's vocabulary when the underlying fact matches.
Return JSON: {"headline": str (<=90 chars), "summary": str (3-4 sentences), "skills_order": [subset/order of the profile's skill category keys],
"experience_bullets": {<current employer name exactly as in the profile>: [4 bullets], <most recent previous employer>: [1 bullet]}, "cover_note": str (120-180 words, first person, specific to the company and role, no flattery, ends with availability), "why_company": str (1 sentence)}"""

FACTCHECK_SYSTEM = """You are a fact checker. Compare CANDIDATE OUTPUT against the PROFILE (ground truth). Flag any claim in the output that is not supported by the profile:
new numbers/metrics, tools, employers, titles, dates, degrees, certifications, achievements or responsibilities the CANDIDATE claims to have that are not present.
Only statements ABOUT THE CANDIDATE count. Sentences describing the target company, its mission, product or the role ("your platform does X", "I am drawn to your mission of Y") are NOT violations.
Rephrasing and mirroring the job's vocabulary are fine if the underlying candidate fact exists.
Return JSON: {"ok": bool, "violations": [{"claim": str, "why": str}]}"""


def _job_text(j: Job, max_desc: int = 6000) -> str:
    return (f"Company: {j.company}\nTitle: {j.title}\nLocation: {j.location or ''} | remote_scope: {j.remote_scope or ''}\n"
            f"Salary: {j.salary or ''}\nSource: {j.source} | ATS: {j.ats or ''}\nURL: {j.url}\n\nDESCRIPTION:\n{(j.description or '')[:max_desc]}")


# ---------------------------------------------------------------- stage 1: eligibility re-evaluation
def reevaluate(ctx, only_filtered: bool = False):
    """Recompute eligibility for stored jobs with the current rules (after config/filter changes)."""
    changed = 0
    with session() as db:
        qs = db.query(Job).filter(Job.status.in_(["new", "filtered"]))
        rows = qs.all()
        ctx.log("info", f"re-evaluating {len(rows)} jobs")
        for j in rows:
            ok, reason = eligibility(j.title, j.location, j.remote_scope, j.description, j.sponsor_flag)
            if ok != bool(j.eligible) or reason != j.eligibility_reason:
                j.eligible, j.eligibility_reason = ok, reason
                j.status = "new" if ok else "filtered"
                changed += 1
            if ctx.should_stop(): break
    ctx.bump("changed", changed)
    ctx.log("info", f"re-evaluation done: {changed} changed")


# ---------------------------------------------------------------- stage 2: LLM fit scoring
def score(ctx, limit: int = 40, min_desc_len: int = 200, provider: str | None = None, batch: int = 8, workers: int = 4, sources: list[str] | None = None, job_ids: list[int] | None = None):
    """Score jobs against the profile. Each LLM call costs a process spawn with the Claude CLI, so jobs are scored in
    batches of `batch` and `workers` batches run in parallel — roughly 30x the throughput of one job per call."""
    import concurrent.futures as cf
    prof_text = profile.as_text()
    with session() as db:
        ids = [j.id for j in db.query(Job).filter(Job.eligible == True, Job.fit_score.is_(None), Job.status == "new", Job.source.in_(sources) if sources else True, Job.id.in_(job_ids) if job_ids is not None else True)  # noqa: E712
               .order_by(Job.posted_at.desc().nullslast(), Job.created_at.desc()).limit(limit).all()]
    # title-only jobs need no LLM call
    todo = []
    with session() as db:
        for jid in ids:
            j = db.get(Job, jid)
            if len(j.description or "") < min_desc_len:
                j.fit_reasons = json.dumps({"reasons": ["Description missing; awaiting hydration"], "blockers": []})
                ctx.bump("title_only")
            else:
                todo.append(jid)
    ctx.log("info", f"scoring {len(todo)} jobs ({ctx.stats.get('title_only', 0)} title-only auto-scored), batches of {batch} x {workers} workers")
    groups = [todo[i:i + batch] for i in range(0, len(todo), batch)]

    def run_batch(group):
        with session() as db:
            jobs = [(jid, _job_text(db.get(Job, jid), 2500)) for jid in group]
        listing = "\n\n".join(f"### JOB {n + 1}\n{t}" for n, (_, t) in enumerate(jobs))
        prompt = (f"CANDIDATE PROFILE:\n{prof_text}\n\nScore EACH of the {len(jobs)} jobs below.\n"
                  f'Return JSON: {{"results": [{{"job": 1, "score": 0-100, "reasons": [], "blockers": [], "seniority": "", "resume_focus": [], "keywords": []}}, ...]}} '
                  f"with exactly {len(jobs)} entries in order.\n\n{listing}")
        if ctx.should_stop(): return []
        out = llm.complete_json("score", prompt, SCORE_SYSTEM, provider=provider)
        results = out.get("results") or []
        mapped = {}
        for result in results:
            if not isinstance(result, dict): raise ValueError("Invalid score entry")
            index = result.get("job")
            value = result.get("score")
            if type(index) is not int or index in mapped or not 1 <= index <= len(jobs): raise ValueError("Invalid or duplicate score job index")
            if type(value) is not int or not 0 <= value <= 100: raise ValueError("Invalid score")
            for field in ("reasons", "blockers", "resume_focus", "keywords"):
                if not isinstance(result.get(field, []), list) or not all(isinstance(x, str) for x in result.get(field, [])): raise ValueError(f"Invalid {field}")
            mapped[index] = result
        if len(mapped) != len(jobs): raise ValueError("Missing score entries; batch remains retryable")
        if ctx.should_stop(): return []
        done = []
        with session() as db:
            for n, (jid, _) in enumerate(jobs):
                r = mapped[n + 1]
                j = db.get(Job, jid)
                if not r:
                    j.fit_score, j.status = 55, "scored"
                    j.fit_reasons = json.dumps({"reasons": ["batch scoring returned no entry for this job"], "blockers": []})
                    done.append((55, j.company, j.title)); continue
                sc = int(max(0, min(100, r.get("score", 0))))
                j.fit_score = sc; j.status = "scored"
                j.fit_reasons = json.dumps({k: r.get(k) for k in ("reasons", "blockers", "seniority", "resume_focus", "keywords")})
                done.append((sc, j.company, j.title))
        return done

    thr = config.load().get("fit_threshold", 65)
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_batch, g): g for g in groups}
        for fut in cf.as_completed(futures):
            if ctx.should_stop(): break
            try:
                for sc, company, title in fut.result():
                    ctx.bump("scored"); ctx.bump("high" if sc >= thr else "low")
                    ctx.log("info", f"{sc:3d}  {company} — {title}")
            except Exception as e:  # noqa: BLE001
                ctx.log("warn", f"score batch failed: {str(e)[:200]}"); ctx.bump("failed", len(futures[fut]))
    ctx.log("info", f"scoring done: {ctx.stats}")


# ---------------------------------------------------------------- stage 3: tailoring + fact check -> review queue
def tailor(ctx, job_id: int, provider: str | None = None, replace_existing: bool = False) -> int | None:
    """Create a pending_review Application with a tailored resume PDF and cover note. Returns application id.
    All LLM/PDF work happens outside any DB transaction; SQLite writes are short."""
    profile_hash = profile.fingerprint()
    prof_text = profile.as_text()
    with session() as db:
        j = db.get(Job, job_id)
        if not j: return None
        original = db.query(Application).filter_by(job_id=job_id).first()
        if original and not replace_existing:
            exists = True
        else:
            exists = False
        # Boards repost the same role under a new URL, so a second job row is not a second vacancy. Applying twice to the
        # same title at the same company reads as careless, so the duplicate is skipped rather than prepared.
        twin = None
        if not original:
            from backend.core.normalize import norm
            for other in db.query(Application).join(Job).filter(Job.company == j.company, Application.job_id != job_id).all():
                if norm(other.job.title) == norm(j.title):
                    twin = other.job_id; break
        if original and replace_existing and original.status not in ("pending_review", "needs_human", "failed", "rejected_by_user"):
            raise ValueError("Unapprove the application before regenerating documents; submitted applications cannot be regenerated")
        previous = (original.id, original.status, original.updated_at) if original else None
        hints, jt, company, title, ats, source = j.fit_reasons or "", _job_text(j), j.company, j.title, j.ats, j.source
    if exists:
        ctx.log("info", f"already has application for job {job_id}"); return None
    if twin:
        with session() as db:
            job = db.get(Job, job_id)
            job.status = "skipped"; job.eligibility_reason = f"same role already prepared as job {twin} (reposted under a second URL)"
        ctx.log("info", f"skipped duplicate posting: {company} — {title} (already prepared as job {twin})")
        ctx.bump("duplicate"); return None
    prompt = f"PROFILE (ground truth):\n{prof_text}\n\nSCORING HINTS: {hints}\n\nJOB:\n{jt}"
    from backend.core.validation import TailoredResume, FactCheck
    employers = [e["company"] for e in profile.load()["experience"]]
    categories = list(profile.load()["skills"])

    def _tailor(text: str, cached: bool = True):
        """One attempt, then one corrective retry. A dropped field or a mis-keyed employer is the model slipping, not a
        reason to lose the job: quoting the exact complaint back to it recovers almost all of them."""
        last = None
        for attempt in range(2):
            try:
                raw = llm.complete_json("tailor", text, TAILOR_SYSTEM, provider=provider, use_cache=cached and attempt == 0)
                return TailoredResume.model_validate(raw).validate_profile(profile.load())
            except Exception as e:  # noqa: BLE001 — pydantic validation or an unknown employer/skill key
                last = e
                text = (text + f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {str(e)[:400]}\n"
                                f"Return every required key. Use these employer names exactly as keys of experience_bullets: "
                                f"{employers}. Use only these skill category names in skills_order: {categories}.")
                ctx.log("warn", f"tailor output rejected for {company}; asking again ({str(e)[:90]})")
        raise last

    out = _tailor(prompt)
    def _check(o):
        ci = json.dumps({k: o.get(k) for k in ("headline", "summary", "experience_bullets", "cover_note")}, indent=1)
        return FactCheck.model_validate(llm.complete_json("factcheck", f"PROFILE:\n{prof_text}\n\nCANDIDATE OUTPUT:\n{ci}", FACTCHECK_SYSTEM, use_cache=False)).model_dump()
    fc = _check(out)
    if not fc.get("ok", False) and fc.get("violations"):
        ctx.log("warn", f"fact-check flagged {len(fc['violations'])} claim(s) for {company}; repairing")
        fix = prompt + "\n\nREMOVE OR REWRITE THESE UNSUPPORTED CLAIMS:\n" + "\n".join(f"- {v.get('claim')}: {v.get('why')}" for v in fc["violations"])
        out = _tailor(fix, cached=False)
        fc = _check(out)
    if fc.get("ok") is not True or fc.get("violations"):
        raise ValueError("Unsupported resume claims remain; application was not queued")
    if ctx.should_stop(): return None
    overlay = {k: out.get(k) for k in ("headline", "summary", "skills_order", "experience_bullets")}
    pdf = resume.render_pdf(overlay, tag=f"{company}_{title}"[:40])
    PLATFORM_SKILLS = {"linkedin", "wellfound", "ycombinator", "naukri", "instahyre", "cutshort", "hirist", "peerlist"}
    # Some boards charge the candidate to apply through them. Their listings are still worth pursuing, but the way in is a
    # person at the company, so the application is prepared for outreach and no form worker will ever touch it.
    if source in (config.load().get("outreach_only_sources") or []): method = "outreach"
    elif source in ("hackernews", "reddit") and re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", jt): method = "email"
    elif ats == "linkedin": method = "easy_apply"
    elif ats in PLATFORM_SKILLS or source in PLATFORM_SKILLS: method = "platform_apply"
    elif ats in {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "rippling", "recruitee", "teamtailor", "personio", "bamboohr", "breezy", "jazzhr"}: method = "ats_form"
    else: method = "ats_form"   # generic form filler handles company career pages / WTTJ-native apply too; falls back to needs_human
    kw = (json.loads(hints) if hints.startswith("{") else {}).get("keywords")
    if profile.fingerprint() != profile_hash: raise ValueError("Profile changed during preparation; retry preparation")
    if ctx.should_stop(): return None
    with session() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        # Re-check for a twin here, inside the serialised write. Preparation runs five jobs at once, so a company that
        # lists one role in three cities had all three checked before any of them existed, and all three were queued.
        from backend.core.normalize import norm
        if not previous:
            for other in db.query(Application).join(Job).filter(Job.company == company, Application.job_id != job_id).all():
                if norm(other.job.title) == norm(title):
                    db.get(Job, job_id).status = "skipped"
                    db.get(Job, job_id).eligibility_reason = f"same role already prepared as job {other.job_id} (listed again for another location)"
                    ctx.log("info", f"skipped duplicate posting: {company} — {title} (already prepared as job {other.job_id})")
                    ctx.bump("duplicate")
                    return None
        j = db.get(Job, job_id)
        platform = ats if ats else (source if source in PLATFORM_SKILLS else "web")
        app = Application(job_id=j.id, platform=platform, method=method, status="pending_review", resume_path=pdf,
                          cover_note=out.get("cover_note"), answers={"why_company": out.get("why_company"), "overlay": overlay, "factcheck": fc, "keywords": kw, "profile_hash": profile_hash, "answers_hash": profile.answers_fingerprint(), "prompt_version": "2026-09-07-v2"})
        if previous:
            existing = db.get(Application, previous[0])
            if not existing or (existing.status, existing.updated_at) != previous[1:]:
                raise ValueError("Application changed during regeneration; original record preserved")
            existing.resume_path = app.resume_path; existing.cover_note = app.cover_note; existing.answers = app.answers
            existing.error = None
            # Preserve needs_human/failed so uncertain submissions still require reconciliation.
            existing.status = previous[1] if previous[1] in ("needs_human", "failed") else "pending_review"
            aid = existing.id
        else:
            db.add(app); db.flush(); aid = app.id
        j.status = "queued"
    ctx.log("info", f"queued for review: {company} — {title}  (factcheck ok={fc.get('ok')})")
    ctx.bump("queued")
    return aid


def prepare(ctx, limit: int = 20, min_score: int | None = None, provider: str | None = None, workers: int = 5, sources: list[str] | None = None, job_ids: list[int] | None = None):
    """Tailor every scored job above threshold that has no application yet."""
    thr = min_score if min_score is not None else config.load().get("fit_threshold", 65)
    with session() as db:
        sub = db.query(Application.job_id)
        ids = [j.id for j in db.query(Job).filter(Job.status == "scored", Job.fit_score >= thr, ~Job.id.in_(sub), Job.source.in_(sources) if sources else True, Job.id.in_(job_ids) if job_ids is not None else True)
               .order_by(Job.fit_score.desc()).limit(limit).all()]
    ctx.log("info", f"tailoring {len(ids)} jobs with score >= {thr} ({workers} in parallel)")
    import concurrent.futures as cf

    def one(jid):
        if ctx.should_stop(): return
        try:
            tailor(ctx, jid, provider=provider)
        except Exception as e:  # noqa: BLE001
            ctx.log("warn", f"tailor failed for job {jid}: {e}"); ctx.bump("failed")

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, jid) for jid in ids]
        for f in cf.as_completed(futures):
            if ctx.should_stop():
                for g in futures: g.cancel()
                break
    ctx.log("info", f"prepare done: {ctx.stats}")


def daily(ctx, score_limit: int = 60, prepare_limit: int = 30, sources=None):
    """score -> prepare in one run (the scheduler calls this)."""
    from backend.core.hydration import hydrate
    hydrate(ctx, limit=score_limit, sources=sources)
    score(ctx, limit=score_limit, sources=sources)
    if not ctx.should_stop(): prepare(ctx, limit=prepare_limit, sources=sources)


def prepare_selected(ctx, job_ids: list[int]):
    """Prepare exactly the jobs selected by the user; never submit or approve."""
    from backend.core.hydration import hydrate
    hydrate(ctx, limit=len(job_ids), job_ids=job_ids)
    if ctx.should_stop(): return
    score(ctx, limit=len(job_ids), job_ids=job_ids)
    if ctx.should_stop(): return
    prepare(ctx, limit=len(job_ids), min_score=0, job_ids=job_ids)


def regenerate(ctx, application_id: int, provider: str | None = None):
    with session() as db:
        app = db.get(Application, application_id)
        if not app: raise ValueError("Unknown application")
        job_id = app.job_id
    tailor(ctx, job_id, provider=provider, replace_existing=True)


PIPELINES = {"regenerate": regenerate, "prepare_selected": prepare_selected, "reevaluate": reevaluate, "score": score, "prepare": prepare, "daily": daily}
